"""Searchable catalog of the C# methods of a Mono game (e.g. a Unity game)."""

from datetime import datetime

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from hermes.engines.registry import resolve_engine
from hermes.features.assemblies import missing_assemblies
from hermes.features.catalog import CachedCatalogWorker, CatalogWorker
from hermes.features.favorites import CATALOG
from hermes.ui.widgets.call_graph import CallGraphDialog
from hermes.ui.widgets.favorites import (
    FAVORITE_COLUMN,
    FAVORITE_COLUMN_KEY,
    FavoriteColumn,
    set_up_favorite_column,
)
from hermes.ui.widgets.table import copy_menu, resizable_columns


_FAVORITE, _CLASS, _METHOD, _RETURN, _STATIC, _ASSEMBLY = range(6)
_COLUMNS = (
    FAVORITE_COLUMN_KEY,
    "catalog.column_class",
    "catalog.column_method",
    "catalog.column_return",
    "catalog.column_static",
    "catalog.column_assembly",
)
# Typing pauses this long before the catalog is filtered.
FILTER_DELAY_MS = 200

# Workers still reading when their dialog closed; kept until they finish.
_running_workers = set()


def matches(words, method):
    """Whether every search word occurs in the method's class, name or assembly."""
    text = f"{method.class_name}:{method.name} {method.assembly}".lower()
    return all(word in text for word in words)


class CatalogModel(FavoriteColumn, QAbstractTableModel):
    """All methods read so far; shows those matching the search."""

    def __init__(self, translations, engine, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.engine = engine
        self.methods = []
        self.visible = []
        self._words = []
        self._framework = False
        # The user's assembly selection; empty for none
        self.selection = frozenset()

    # --- Qt model --------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.visible)

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.translations[_COLUMNS[section]]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if index.column() == FAVORITE_COLUMN:
            return self.favorite_data(index.row(), role)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        method = self.visible[index.row()]
        return (
            None,
            method.class_name,
            f"{method.name} {method.parameters}",
            method.return_type,
            "✓" if method.static else "",
            method.assembly,
        )[index.column()]

    # --- Content ---------------------------------------------------------
    def add(self, methods):
        # Only the new methods are filtered: refiltering all of them for
        # every assembly took half a minute for a few hundred assemblies.
        self.methods.extend(methods)
        shown = [method for method in methods if self._shows(method)]
        if shown:
            first = len(self.visible)
            self.beginInsertRows(QModelIndex(), first, first + len(shown) - 1)
            self.visible.extend(shown)
            self.endInsertRows()

    def clear(self):
        self.methods = []
        self._apply()

    def set_filter(self, text, framework, selection=None):
        self._words = text.lower().split()
        self._framework = framework
        if selection is not None:
            self.selection = frozenset(selection)
        self._apply()

    def shows_assembly(self, assembly):
        """Whether methods of ``assembly`` are shown (selection, else framework switch)."""
        if self.selection:
            return assembly in self.selection
        return self._framework or not self.engine.is_framework_assembly(assembly)

    def method_at(self, row):
        return self.visible[row] if 0 <= row < len(self.visible) else None

    def favorite_item(self, row):
        return self.method_at(row)

    def _shows(self, method):
        return self.shows_assembly(method.assembly) and matches(self._words, method)

    def _apply(self):
        self.beginResetModel()
        self.visible = [method for method in self.methods if self._shows(method)]
        self.endResetModel()


class CatalogDialog(QDialog):
    """Search the classes and methods a Mono game has loaded.

    Not modal, so the scanner stays usable. Game assemblies are read when the
    dialog opens, framework assemblies only when they are to be shown. With
    an assembly selection (see hermes.ui.widgets.assemblies) only the
    selected assemblies are read and shown. Assemblies read before come from
    the catalog cache; "Reload" reads them anew.

    Offline, ``game`` (a KnownGame, see hermes.features.games) is shown from
    the catalog cache as it was read last, without a process (``pid`` None):
    nothing can be monitored or read anew then.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # CatalogMethod to log the calls of (double click or "Monitor")
    monitor_requested = pyqtSignal(object)

    def __init__(self, translations, pid, parent=None, engine=None, favorites=None,
                 assembly_selection=None, game=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.game = game
        self.engine = resolve_engine(pid, engine) if game is None else engine
        self.model = CatalogModel(translations, self.engine, self)
        self._favorites = favorites
        self._assembly_selection = assembly_selection
        self._assemblies = []
        self._loaded = set()
        # Loaded assemblies that came from the catalog cache
        self._cached = set()
        self._failed = {}
        # Offline: assemblies whose build is not in the cache
        self._not_cached = set()
        self._error = None
        self._worker = None
        title = translations["catalog.title"].format(engine=self.engine.display_name)
        if game is not None:
            title = translations["catalog.title_offline"].format(
                title=title, game=game.name,
                time=datetime.fromtimestamp(game.updated).strftime("%d.%m.%Y %H:%M"))
        self.setWindowTitle(title)
        self.resize(1000, 640)
        self._build_ui()
        if game is not None:
            self.reload_button.hide()
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DELAY_MS)
        self._filter_timer.timeout.connect(self._apply_filter)
        if assembly_selection is not None:
            assembly_selection.changed.connect(self.reload)
        self.reload()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(self.translations["catalog.search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(lambda _text: self._filter_timer.start())
        bar.addWidget(self.search_input, 1)
        self.framework_check = QCheckBox(self.translations["catalog.framework"])
        self.framework_check.setToolTip(self.translations["catalog.framework_tooltip"])
        self.framework_check.toggled.connect(self._toggle_framework)
        bar.addWidget(self.framework_check)
        self.reload_button = QPushButton(self.translations["catalog.reload"])
        self.reload_button.setToolTip(self.translations["catalog.reload_tooltip"])
        self.reload_button.clicked.connect(lambda: self.reload(fresh=True))
        bar.addWidget(self.reload_button)
        layout.addLayout(bar)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setWordWrap(False)
        self.view.verticalHeader().setVisible(False)
        header = self.view.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Stretch if column in (_CLASS, _METHOD)
                else QHeaderView.ResizeMode.Interactive,
            )
        header.resizeSection(_RETURN, 140)
        header.resizeSection(_STATIC, 70)
        header.resizeSection(_ASSEMBLY, 170)
        set_up_favorite_column(self.view, self.model, self._favorites, CATALOG)
        resizable_columns(self.view)
        copy_menu(self.view, self.translations)
        self.view.selectionModel().currentRowChanged.connect(self._show_details)
        self.view.doubleClicked.connect(
            lambda index: index.column() != FAVORITE_COLUMN and self.game is None
            and self._request_monitor())
        self.model.modelReset.connect(lambda: self._show_details(QModelIndex()))
        layout.addWidget(self.view, 1)

        details = QHBoxLayout()
        self.details_label = QLabel("")
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details.addWidget(self.details_label, 1)
        self.copy_button = QPushButton(self.translations["catalog.copy"])
        self.copy_button.setToolTip(self.translations["catalog.copy_tooltip"])
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_signature)
        details.addWidget(self.copy_button)
        self.calls_button = QPushButton(self.translations["catalog.calls"])
        self.calls_button.setToolTip(self.translations["catalog.calls_tooltip"])
        self.calls_button.setEnabled(False)
        self.calls_button.clicked.connect(self._show_calls)
        details.addWidget(self.calls_button)
        self.monitor_button = QPushButton(self.translations["catalog.monitor"])
        self.monitor_button.setObjectName("btn_primary")
        self.monitor_button.setToolTip(self.translations[
            "catalog.offline_no_monitor" if self.game is not None else "catalog.monitor_tooltip"])
        self.monitor_button.setEnabled(False)
        self.monitor_button.clicked.connect(self._request_monitor)
        details.addWidget(self.monitor_button)
        layout.addLayout(details)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # --- Reading -------------------------------------------------------------
    def selection(self):
        """The user's assembly selection; empty for none."""
        store = self._assembly_selection
        return store.selection if store is not None else frozenset()

    def reload(self, fresh=False):
        """Read the catalog again, e.g. after the game loaded more assemblies.

        ``fresh`` reads every assembly anew instead of taking it from the cache.
        """
        self._stop_worker()
        self._assemblies = []
        self._loaded.clear()
        self._cached.clear()
        self._failed.clear()
        self._not_cached.clear()
        self._error = None
        self.model.clear()
        selection = self.selection()
        self.model.set_filter(self.search_input.text(), self.framework_check.isChecked(),
                              selection)
        # The selection decides which assemblies are shown, framework ones too.
        self.framework_check.setEnabled(not selection)
        self.framework_check.setToolTip(self.translations[
            "catalog.framework_selection_tooltip" if selection else "catalog.framework_tooltip"])
        self._start_worker(None, fresh)

    def _start_worker(self, assemblies, fresh=False):
        if self.game is not None:
            worker = CachedCatalogWorker(self.game, assemblies, engine=self.engine)
        else:
            worker = CatalogWorker(self.pid, assemblies, engine=self.engine,
                                   selection=self.selection(), fresh=fresh)
        worker.assemblies_found.connect(self._on_assemblies_found)
        worker.assembly_cached.connect(self._cached.add)
        worker.assembly_loaded.connect(self._on_assembly_loaded)
        worker.assembly_failed.connect(self._on_assembly_failed)
        worker.catalog_failed.connect(self._on_failed)
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self._worker = worker
        _running_workers.add(worker)
        self.reload_button.setEnabled(False)
        self.status_label.setText(
            self.translations["catalog.starting"].format(engine=self.engine.display_name))
        worker.start()

    def _stop_worker(self):
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker = None

    def _on_assemblies_found(self, assemblies):
        self._assemblies = assemblies
        self._show_progress()

    def _on_assembly_loaded(self, assembly, methods):
        self._loaded.add(assembly)
        self.model.add(methods)
        self._show_progress()

    def _on_assembly_failed(self, assembly, message):
        if message == CachedCatalogWorker.NOT_CACHED:
            self._not_cached.add(assembly)
        else:
            self._failed[assembly] = message
        self._show_progress()

    def _on_failed(self, message):
        self._error = message
        self.status_label.setText(self.translations["catalog.failed"].format(message=message))

    def _on_worker_finished(self, worker):
        _running_workers.discard(worker)
        if worker is not self._worker:
            return
        self._worker = None
        self.reload_button.setEnabled(True)
        if self._error is None:
            self._show_summary()

    def _toggle_framework(self, shown):
        missing = [a["name"] for a in self._assemblies
                   if self.engine.is_framework_assembly(a["name"]) and a["name"] not in self._loaded]
        if shown and missing and self._worker is None and not self.selection():
            self._start_worker(missing)
        self._apply_filter()

    # --- Display -------------------------------------------------------------
    def _apply_filter(self):
        self.model.set_filter(self.search_input.text(), self.framework_check.isChecked())
        if self._worker is None and self._error is None:
            self._show_summary()

    def _show_progress(self):
        pending = [a["name"] for a in self._assemblies
                   if a["name"] not in self._loaded and a["name"] not in self._failed
                   and a["name"] not in self._not_cached
                   and self.model.shows_assembly(a["name"])]
        if pending:
            self.status_label.setText(self.translations["catalog.loading"].format(
                assembly=pending[0], done=len(self._loaded), total=len(self._loaded) + len(pending)
            ))

    def _show_summary(self):
        classes = {(m.assembly, m.class_name) for m in self.model.visible}
        text = self.translations["catalog.summary"].format(
            shown=len(self.model.visible), total=len(self.model.methods),
            classes=len(classes), assemblies=len(self._loaded),
        )
        if self._cached and self.game is None:
            text += "  ·  " + self.translations["catalog.from_cache"].format(
                count=len(self._cached))
        shown_missing = sorted(name for name in self._not_cached
                               if self.model.shows_assembly(name))
        if shown_missing:
            text += "  ·  " + self.translations["catalog.not_cached"].format(
                assemblies=", ".join(shown_missing))
        selection = self.selection()
        if selection:
            text += "  ·  " + self.translations["catalog.assembly_selection"].format(
                count=len(selection))
            missing = missing_assemblies([a["name"] for a in self._assemblies], selection)
            if missing and self._assemblies:
                text += "  ·  " + self.translations["catalog.assemblies_missing"].format(
                    assemblies=", ".join(missing))
        if self._failed:
            text += "  ·  " + self.translations["catalog.assemblies_failed"].format(
                assemblies=", ".join(sorted(self._failed))
            )
        self.status_label.setText(text)

    def selected_method(self):
        index = self.view.currentIndex()
        return self.model.method_at(index.row()) if index.isValid() else None

    def _show_details(self, current, _previous=None):
        method = self.model.method_at(current.row()) if current.isValid() else None
        self.copy_button.setEnabled(method is not None)
        self.calls_button.setEnabled(method is not None)
        self.monitor_button.setEnabled(method is not None and self.game is None)
        if method is None:
            self.details_label.setText("")
            return
        self.details_label.setText(self.translations["catalog.details"].format(
            signature=method.signature,
            returns=method.return_type or "?",
            kind=self.translations["catalog.static" if method.static else "catalog.instance"],
            token=f"0x{method.token:08x}",
            assembly=method.assembly,
        ))

    def _request_monitor(self):
        method = self.selected_method()
        if method is not None and self.game is None:
            self.monitor_requested.emit(method)

    def _show_calls(self):
        """Open the call graph of the selected method in a window of its own."""
        method = self.selected_method()
        if method is None:
            return
        graph = CallGraphDialog(self.translations, self.pid, method, parent=self,
                                engine=self.engine, selection=self.selection(), game=self.game)
        graph.monitor_requested.connect(self.monitor_requested.emit)
        graph.show()

    def _copy_signature(self):
        method = self.selected_method()
        if method is not None:
            QGuiApplication.clipboard().setText(method.signature)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._stop_worker()
        super().closeEvent(event)
