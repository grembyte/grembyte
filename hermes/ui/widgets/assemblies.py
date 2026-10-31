"""Assembly selection: which assemblies of a Mono game the catalog and the trace read.

The window lists the assemblies the game has loaded; the ticked ones are
saved per program (see hermes.features.assemblies). The catalog and the
trace follow the saved selection at once.
"""

from PyQt6.QtCore import QObject, Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.core import messages
from hermes.engines.registry import resolve_engine
from hermes.features.assemblies import missing_assemblies
from hermes.features.catalog import CatalogWorker
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.table import copy_menu, resizable_columns
from hermes.ui.workers import keep_until_finished



class AssemblySelectionStore(QObject):
    """The assembly selection of the program last attached, shared by all windows."""

    # The saved selection changed
    changed = pyqtSignal()
    # A database error, as text
    failed = pyqtSignal(str)

    def __init__(self, repository, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.process_id = None
        self.selection = frozenset()

    def set_process(self, process_id):
        """Use the selection of another program (its database id)."""
        self.process_id = process_id
        self.selection = frozenset()
        if process_id is not None:
            try:
                self.selection = frozenset(self.repository.list_assemblies(process_id))
            except SQLAlchemyError as error:
                self.failed.emit(messages.describe(error))
        self.changed.emit()

    def save(self, names):
        """Save ``names`` as the selection; returns whether it worked."""
        if self.process_id is None:
            return False
        try:
            self.repository.save_assemblies(self.process_id, names)
        except SQLAlchemyError as error:
            self.failed.emit(messages.describe(error))
            return False
        self.selection = frozenset(names)
        self.changed.emit()
        return True


_NAME, _CLASSES, _KIND, _FILE = range(4)
_COLUMNS = (
    "assemblies.column_name", "assemblies.column_classes", "assemblies.column_kind",
    "assemblies.column_file",
)


class AssemblyDialog(QDialog):
    """Tick the assemblies of a Mono game to limit the catalog and the trace to.

    Not modal. Assemblies selected earlier that the game has not loaded (yet)
    are listed too, so they can be unticked.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    def __init__(self, translations, pid, store, parent=None, engine=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.store = store
        self.engine = resolve_engine(pid, engine)
        # {name, file, classes} of the loaded assemblies; None while unknown
        self.assemblies = None
        self._checked = set(store.selection)
        self._filling = False
        self._worker = None
        self.setWindowTitle(translations["assemblies.title"].format(
            engine=self.engine.display_name))
        self.resize(820, 600)
        self._build_ui()
        self.reload()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        hint = QLabel(t["assemblies.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        bar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t["assemblies.search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(lambda _text: self._fill())
        bar.addWidget(self.search_input, 1)
        self.select_all_button = QPushButton(t["assemblies.select_all"])
        set_button_icon(self.select_all_button, "select_all")
        self.select_all_button.setToolTip(t["assemblies.select_all_tooltip"])
        self.select_all_button.clicked.connect(lambda: self._check_shown(True))
        bar.addWidget(self.select_all_button)
        self.clear_button = QPushButton(t["assemblies.clear"])
        set_button_icon(self.clear_button, "deselect")
        self.clear_button.setToolTip(t["assemblies.clear_tooltip"])
        self.clear_button.clicked.connect(lambda: self._check_shown(False))
        bar.addWidget(self.clear_button)
        self.reload_button = QPushButton(t["assemblies.reload"])
        set_button_icon(self.reload_button, "refresh")
        self.reload_button.setToolTip(t["assemblies.reload_tooltip"])
        self.reload_button.clicked.connect(self.reload)
        bar.addWidget(self.reload_button)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([t[column] for column in _COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column == _FILE
                else QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_NAME, 260)
        header.resizeSection(_CLASSES, 80)
        header.resizeSection(_KIND, 110)
        resizable_columns(self.table)
        copy_menu(self.table, self.translations)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        actions.addStretch()
        self.close_button = QPushButton(t["assemblies.close"])
        set_button_icon(self.close_button, "close")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        self.save_button = QPushButton(t["assemblies.save"])
        self.save_button.setObjectName("btn_primary")
        set_button_icon(self.save_button, "save")
        self.save_button.setToolTip(t["assemblies.save_tooltip"])
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    # --- Reading -------------------------------------------------------------
    def reload(self):
        """List the loaded assemblies anew, e.g. after the game loaded more."""
        if self._worker is not None:
            self._worker.requestInterruption()
        # Only the list of assemblies: no assembly is read.
        worker = CatalogWorker(self.pid, [], engine=self.engine)
        worker.assemblies_found.connect(self._on_assemblies_found)
        worker.catalog_failed.connect(self._on_failed)
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self._worker = worker
        keep_until_finished(worker)
        self.reload_button.setEnabled(False)
        self.status_label.setText(self.translations["assemblies.loading"])
        worker.start()

    def _on_assemblies_found(self, assemblies):
        self.assemblies = sorted(assemblies, key=lambda a: a["name"].lower())
        self._fill()

    def _on_failed(self, message):
        self.assemblies = []
        self._fill()
        self.status_label.setText(self.translations["assemblies.failed"].format(
            message=message))

    def _on_worker_finished(self, worker):
        if worker is self._worker:
            self._worker = None
            self.reload_button.setEnabled(True)

    # --- Display -------------------------------------------------------------
    def _rows(self):
        """(name, file, classes or None, loaded) of every assembly to list."""
        loaded = self.assemblies or []
        rows = [(a["name"], a.get("file") or "", a.get("classes"), True) for a in loaded]
        rows += [(name, "", None, False) for name in
                 missing_assemblies([a["name"] for a in loaded], self._checked)]
        words = self.search_input.text().lower().split()
        return [row for row in rows
                if all(word in f"{row[0]} {row[1]}".lower() for word in words)]

    def _fill(self):
        t = self.translations
        self._filling = True
        rows = self._rows()
        self.table.setRowCount(len(rows))
        for row, (name, file, classes, loaded) in enumerate(rows):
            framework = self.engine.is_framework_assembly(name)
            kind = t["assemblies.kind_framework" if framework else "assemblies.kind_game"]
            if not loaded:
                kind = t["assemblies.kind_missing"]
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, name)
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            name_item.setCheckState(Qt.CheckState.Checked if name in self._checked
                                    else Qt.CheckState.Unchecked)
            classes_item = QTableWidgetItem("" if classes is None else str(classes))
            classes_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
            kind_item = QTableWidgetItem(kind)
            if not loaded:
                kind_item.setToolTip(t["assemblies.missing_tooltip"])
            file_item = QTableWidgetItem(file)
            file_item.setToolTip(file)
            for column, item in ((_NAME, name_item), (_CLASSES, classes_item),
                                 (_KIND, kind_item), (_FILE, file_item)):
                self.table.setItem(row, column, item)
        self._filling = False
        self._show_status()

    def _show_status(self):
        t = self.translations
        dirty = self.is_dirty()
        self.save_button.setEnabled(dirty and self.store.process_id is not None)
        if self.assemblies is None:
            return
        if self._checked:
            text = t["assemblies.count"].format(
                selected=len(self._checked), total=len(self.assemblies))
        else:
            text = t["assemblies.none_selected"].format(total=len(self.assemblies))
        if dirty:
            text += "  ·  " + t["assemblies.unsaved"]
        self.status_label.setText(text)

    def checked(self):
        """The names of the ticked assemblies."""
        return frozenset(self._checked)

    def is_dirty(self):
        """Whether the ticks differ from the saved selection."""
        return self._checked != set(self.store.selection)

    # --- Actions -------------------------------------------------------------
    def _on_item_changed(self, item):
        if self._filling or item.column() != _NAME:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(name)
        else:
            self._checked.discard(name)
        self._show_status()

    def _check_shown(self, checked):
        """Tick or untick every assembly the search shows."""
        for name, _file, _classes, _loaded in self._rows():
            if checked:
                self._checked.add(name)
            else:
                self._checked.discard(name)
        self._fill()

    def save(self):
        """Save the ticked assemblies as the program's selection."""
        if self.store.save(self._checked):
            self._fill()

    def closeEvent(self, event):  # pylint: disable=invalid-name
        # No cancel: the main window also closes the dialog when the game exits.
        if self.is_dirty() and self.store.process_id is not None:
            answer = QMessageBox.question(
                self, self.windowTitle(), self.translations["assemblies.confirm_unsaved"],
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
                QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                self.save()
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker = None
        super().closeEvent(event)
