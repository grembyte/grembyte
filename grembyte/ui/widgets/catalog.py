"""Searchable catalog of the methods and fields of a game's classes (Unity, Unreal)."""

import re
from datetime import datetime
from functools import lru_cache

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from grembyte.engines import base as engine_base
from grembyte.engines.registry import resolve_engine
from grembyte.features.assemblies import missing_assemblies
from grembyte.features.catalog import (
    FIELDS,
    METHODS,
    CachedCatalogWorker,
    CatalogWorker,
    csharp_name,
)
from grembyte.features.favorites import CATALOG
from grembyte.features.method_call import call_capability
from grembyte.ui.widgets.call_graph import CallGraphDialog
from grembyte.ui.widgets.favorites import (
    FAVORITE_COLUMN,
    FAVORITE_COLUMN_KEY,
    FavoriteColumn,
    set_up_favorite_column,
)
from grembyte.ui.widgets.icons import set_button_icon
from grembyte.ui.widgets.table import copy_menu, resizable_columns
from grembyte.ui.workers import keep_until_finished


_FAVORITE, _CLASS, _METHOD, _RETURN, _STATIC, _ASSEMBLY = range(6)
_COLUMNS = (
    FAVORITE_COLUMN_KEY,
    "catalog.column_class",
    "catalog.column_method",
    "catalog.column_return",
    "catalog.column_static",
    "catalog.column_assembly",
)
_F_CLASS, _F_FIELD, _F_TYPE, _F_STATIC, _F_OFFSET, _F_ASSEMBLY = range(6)
_FIELD_COLUMNS = (
    "catalog.column_class",
    "catalog.column_field",
    "catalog.column_type",
    "catalog.column_static",
    "catalog.column_offset",
    "catalog.column_assembly",
)
# Typing pauses this long before the catalog is filtered.
FILTER_DELAY_MS = 200


@lru_cache(maxsize=256)
def _wildcard_pattern(word):
    """A search word with ``*`` (any text) or ``?`` (one character) as a regex."""
    return re.compile("".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char) for char in word))


def word_occurs(word, text):
    """Whether one search word occurs in ``text``; ``player*damage`` allows anything between."""
    if "*" in word or "?" in word:
        return _wildcard_pattern(word).search(text) is not None
    return word in text


TYPE_PREFIX = "type:"


def matches(words, method):
    """Whether every search word occurs in the method's class, name or assembly,
    as the table shows them.

    A word ``type:<type>`` occurs in its parameter or return types instead.
    """
    # As the table shows them (nested Mono classes as C# writes them)
    name = csharp_name(f"{method.class_name}:{method.name}", method.engine)
    text = f"{name} {method.assembly}".lower()
    types = csharp_name(f"{method.parameters} {method.return_type}", method.engine).lower()
    return all(word_occurs(word[len(TYPE_PREFIX):], types) if word.startswith(TYPE_PREFIX)
               else word_occurs(word, text) for word in words)


def field_matches(words, field):
    """Whether every search word occurs in the field's class, name or assembly;
    a word ``type:<type>`` in its type instead (as ``matches`` for methods)."""
    name = csharp_name(f"{field.class_name}.{field.name}", field.engine)
    text = f"{name} {field.assembly}".lower()
    types = csharp_name(field.type_name, field.engine).lower()
    return all(word_occurs(word[len(TYPE_PREFIX):], types) if word.startswith(TYPE_PREFIX)
               else word_occurs(word, text) for word in words)


class CatalogModel(FavoriteColumn, QAbstractTableModel):
    """All methods read so far; shows those matching the search.

    Sorts by a column when the view asks; column -1 is the order they were read in.
    """

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
        self._sort_column = -1
        self._sort_order = Qt.SortOrder.AscendingOrder

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
            csharp_name(method.class_name, method.engine),
            csharp_name(f"{method.name} {method.parameters}", method.engine),
            csharp_name(method.return_type, method.engine),
            "✓" if method.static else "",
            method.assembly,
        )[index.column()]

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self._sort_column = column
        self._sort_order = order
        self._resort()

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
            if self._sort_column >= 0:
                self._resort()

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
        self.visible = self._sorted(
            [method for method in self.methods if self._shows(method)])
        self.endResetModel()

    def _sorted(self, methods):
        column = self._sort_column
        if column < 0 or column >= len(_COLUMNS):
            return methods
        keys = {
            _FAVORITE: lambda method: (self.favorites is not None
                                       and self.favorites.is_favorite(method)),
            _CLASS: lambda method: method.class_name.lower(),
            _METHOD: lambda method: (method.name.lower(), method.parameters),
            _RETURN: lambda method: method.return_type.lower(),
            _STATIC: lambda method: method.static,
            _ASSEMBLY: lambda method: method.assembly.lower(),
        }
        return sorted(methods, key=keys[column],
                      reverse=self._sort_order == Qt.SortOrder.DescendingOrder)

    def _resort(self):
        """Sort the shown methods anew; selection and current row stay on their methods."""
        self.layoutAboutToBeChanged.emit()
        before = self.persistentIndexList()
        old = self.visible
        if self._sort_column < 0:
            # Back to the order they were read in
            self.visible = [method for method in self.methods if self._shows(method)]
        else:
            self.visible = self._sorted(old)
        rows = {id(method): row for row, method in enumerate(self.visible)}
        self.changePersistentIndexList(
            before, [self.index(rows[id(old[index.row()])], index.column())
                     if index.isValid() and index.row() < len(old) else QModelIndex()
                     for index in before])
        self.layoutChanged.emit()


class FieldCatalogModel(CatalogModel):
    """All fields read so far (CatalogFields); shows those matching the search.

    The methods' model with other columns and no favorites: what is kept,
    filtered and sorted is the same.
    """

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(_FIELD_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.translations[_FIELD_COLUMNS[section]]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        field = self.visible[index.row()]
        if index.column() == _F_STATIC:
            if not field.static:
                return ""
            # constants and thread statics have no value to read or change
            return {"const": self.translations["catalog.static_const"],
                    "thread": self.translations["catalog.static_thread"]}.get(
                        field.storage, "✓")
        return (
            csharp_name(field.class_name, field.engine),
            field.name,
            csharp_name(field.type_name, field.engine),
            None,
            f"0x{field.offset:x}" if field.storage in ("", "data") else "",
            field.assembly,
        )[index.column()]

    def favorite_item(self, row):
        return None

    def _shows(self, method):
        return self.shows_assembly(method.assembly) and field_matches(self._words, method)

    def _sorted(self, methods):
        column = self._sort_column
        if column < 0 or column >= len(_FIELD_COLUMNS):
            return methods
        keys = {
            _F_CLASS: lambda field: field.class_name.lower(),
            _F_FIELD: lambda field: field.name.lower(),
            _F_TYPE: lambda field: field.type_name.lower(),
            _F_STATIC: lambda field: (field.static, field.storage),
            _F_OFFSET: lambda field: field.offset,
            _F_ASSEMBLY: lambda field: field.assembly.lower(),
        }
        return sorted(methods, key=keys[column],
                      reverse=self._sort_order == Qt.SortOrder.DescendingOrder)


class _Reading:  # pylint: disable=too-few-public-methods
    """How far the catalog of one kind (methods or fields) was read."""

    def __init__(self):
        self.assemblies = []
        self.loaded = set()
        # Loaded assemblies that came from the catalog cache
        self.cached = set()
        self.failed = {}
        # Offline: assemblies whose build is not in the cache
        self.not_cached = set()
        self.error = None
        self.worker = None
        # The assembly selection it was read for; None: not read yet
        self.selection = None


class CatalogDialog(QDialog):
    """Search the classes and methods a Mono game has loaded.

    Not modal, so the scanner stays usable. Game assemblies are read when the
    dialog opens, framework assemblies only when they are to be shown. With
    an assembly selection (see grembyte.ui.widgets.assemblies) only the
    selected assemblies are read and shown. Assemblies read before come from
    the catalog cache; "Reload" reads them anew.

    Offline, ``game`` (a KnownGame, see grembyte.features.games) is shown from
    the catalog cache as it was read last, without a process (``pid`` None):
    nothing can be monitored or read anew then.

    A switch shows the fields instead (``set_kind(FIELDS)``): those each class
    declares, read the same way (assemblies, cache, search) the first time
    they are shown. Methods and fields keep what was read of each.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # The CatalogMethods to log the calls of, a list (double click or "Monitor")
    monitor_requested = Signal(list)
    # static CatalogMethod to call ("Call …")
    call_requested = Signal(object)
    # CatalogMethod to create a method hook for
    hook_requested = Signal(object)
    # A class name: show its live objects in the object browser
    objects_requested = Signal(str)

    def __init__(self, translations, pid, parent=None, engine=None, favorites=None,
                 assembly_selection=None, game=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.game = game
        self.engine = resolve_engine(pid, engine) if game is None else engine
        self.kind = METHODS
        self.models = {METHODS: CatalogModel(translations, self.engine, self),
                       FIELDS: FieldCatalogModel(translations, self.engine, self)}
        self._favorites = favorites
        self._assembly_selection = assembly_selection
        self._reads = {METHODS: _Reading(), FIELDS: _Reading()}
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

    # What was read of the kind shown (see _Reading)
    model = property(lambda self: self.models[self.kind])
    _read = property(lambda self: self._reads[self.kind])
    _assemblies = property(lambda self: self._read.assemblies,
                           lambda self, value: setattr(self._read, "assemblies", value))
    _loaded = property(lambda self: self._read.loaded)
    _cached = property(lambda self: self._read.cached)
    _failed = property(lambda self: self._read.failed)
    _not_cached = property(lambda self: self._read.not_cached)
    _error = property(lambda self: self._read.error,
                      lambda self, value: setattr(self._read, "error", value))
    _worker = property(lambda self: self._read.worker,
                       lambda self, value: setattr(self._read, "worker", value))

    def _build_ui(self):
        # pylint: disable=too-many-statements
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.kind_buttons = {}
        group = QButtonGroup(self)
        for kind, key, icon in ((METHODS, "catalog.kind_methods", "functions"),
                                (FIELDS, "catalog.kind_fields", "data_object")):
            button = QPushButton(self.translations[key])
            button.setCheckable(True)
            button.setChecked(kind == METHODS)
            set_button_icon(button, icon)
            button.setToolTip(self.translations[f"{key}_tooltip"])
            button.clicked.connect(lambda _checked, kind=kind: self.set_kind(kind))
            group.addButton(button)
            bar.addWidget(button)
            self.kind_buttons[kind] = button
        self._show_kind()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(self.translations["catalog.search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(lambda _text: self._filter_timer.start())
        bar.addWidget(self.search_input, 1)
        # "&" would mark a keyboard shortcut ("Framework _libraries"): doubled it shows.
        self.framework_check = QCheckBox(self.translations["catalog.framework"].replace("&", "&&"))
        self.framework_check.setToolTip(self.translations["catalog.framework_tooltip"])
        self.framework_check.toggled.connect(self._toggle_framework)
        bar.addWidget(self.framework_check)
        self.reload_button = QPushButton(self.translations["catalog.reload"])
        set_button_icon(self.reload_button, "refresh")
        self.reload_button.setToolTip(self.translations["catalog.reload_tooltip"])
        self.reload_button.clicked.connect(lambda: self.reload(fresh=True))
        bar.addWidget(self.reload_button)
        layout.addLayout(bar)

        self.view = QTableView()
        self.view.setModel(self.models[METHODS])
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Several rows (Ctrl, Shift) go to the monitor together
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        set_up_favorite_column(self.view, self.models[METHODS], self._favorites, CATALOG)
        # A click on a column header sorts by it, a third one restores the order read in.
        header.setSortIndicatorClearable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.view.setSortingEnabled(True)
        resizable_columns(self.view)
        copy_menu(self.view, self.translations)
        self.view.selectionModel().currentRowChanged.connect(
            lambda _current, _previous: self._show_details())
        self.view.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self._show_details())
        self.view.doubleClicked.connect(
            lambda index: index.column() != FAVORITE_COLUMN and self._request_monitor())
        self.models[METHODS].modelReset.connect(self._show_details)
        self.views = QStackedWidget()
        self.views.addWidget(self.view)
        self.views.addWidget(self._build_field_view())
        layout.addWidget(self.views, 1)

        details_row = QHBoxLayout()
        self.details_label = QLabel("")
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_row.addWidget(self.details_label, 1)
        self.action_stack = QStackedWidget()
        method_actions = QWidget()
        details = QHBoxLayout(method_actions)
        details.setContentsMargins(0, 0, 0, 0)
        self.monitor_button = QPushButton(self.translations["catalog.monitor"])
        self.monitor_button.setObjectName("btn_primary")
        set_button_icon(self.monitor_button, "visibility")
        self.monitor_button.setToolTip(self.translations[
            "catalog.offline_no_monitor" if self.game is not None else "catalog.monitor_tooltip"])
        self.monitor_button.setEnabled(False)
        self.monitor_button.clicked.connect(self._request_monitor)
        details.addWidget(self.monitor_button)
        self.hook_button = QPushButton(self.translations["monitor.create_hook"])
        set_button_icon(self.hook_button, "webhook")
        self.hook_button.setToolTip(self.translations["monitor.create_hook_tooltip"])
        self.hook_button.setEnabled(False)
        self.hook_button.clicked.connect(self._request_hook)
        details.addWidget(self.hook_button)
        self.call_button = QPushButton(self.translations["catalog.call"])
        set_button_icon(self.call_button, "call_made")
        self.call_button.setToolTip(self.translations[
            "catalog.offline_no_call" if self.game is not None else "catalog.call_tooltip"])
        self.call_button.setEnabled(False)
        self.call_button.clicked.connect(self._request_call)
        details.addWidget(self.call_button)
        self.calls_button = QPushButton(self.translations["catalog.calls"])
        set_button_icon(self.calls_button, "account_tree")
        self.calls_button.setToolTip(self.translations["catalog.calls_tooltip"])
        self.calls_button.setEnabled(False)
        self.calls_button.clicked.connect(self._show_calls)
        details.addWidget(self.calls_button)
        self.copy_button = QPushButton(self.translations["catalog.copy"])
        set_button_icon(self.copy_button, "content_copy")
        self.copy_button.setToolTip(self.translations["catalog.copy_tooltip"])
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_signature)
        details.addWidget(self.copy_button)
        self.action_stack.addWidget(method_actions)
        self.action_stack.addWidget(self._build_field_actions())
        details_row.addWidget(self.action_stack)
        layout.addLayout(details_row)
        # What the engine's adapter cannot do yet says so instead.
        unsupported = self.translations["engine.unsupported"].format(
            engine=self.engine.display_name)
        for button, capability in ((self.calls_button, engine_base.CALL_GRAPH),
                                   (self.call_button, engine_base.CALL),
                                   (self.monitor_button, engine_base.MONITOR),
                                   (self.hook_button, engine_base.METHOD_HOOKS)):
            if not self.engine.supports(capability):
                button.setToolTip(unsupported)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_field_view(self):
        model = self.models[FIELDS]
        self.field_view = QTableView()
        self.field_view.setModel(model)
        self.field_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.field_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.field_view.setWordWrap(False)
        self.field_view.verticalHeader().setVisible(False)
        header = self.field_view.horizontalHeader()
        for column in range(len(_FIELD_COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column in (_F_CLASS, _F_FIELD)
                else QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_F_TYPE, 180)
        header.resizeSection(_F_STATIC, 90)
        header.resizeSection(_F_OFFSET, 80)
        header.resizeSection(_F_ASSEMBLY, 170)
        header.setSortIndicatorClearable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.field_view.setSortingEnabled(True)
        resizable_columns(self.field_view)
        copy_menu(self.field_view, self.translations)
        self.field_view.selectionModel().currentRowChanged.connect(
            lambda _current, _previous: self._show_details())
        self.field_view.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self._show_details())
        self.field_view.doubleClicked.connect(lambda _index: self._request_objects())
        model.modelReset.connect(self._show_details)
        return self.field_view

    def _build_field_actions(self):
        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        self.objects_button = QPushButton(self.translations["catalog.objects"])
        self.objects_button.setObjectName("btn_primary")
        set_button_icon(self.objects_button, "manage_search")
        self.objects_button.setToolTip(self.translations[
            "catalog.offline_no_objects" if self.game is not None else
            "catalog.objects_tooltip" if self.engine.supports(engine_base.OBJECT_BROWSER)
            else "engine.unsupported"].format(engine=self.engine.display_name))
        self.objects_button.setEnabled(False)
        self.objects_button.clicked.connect(self._request_objects)
        row.addWidget(self.objects_button)
        self.copy_field_button = QPushButton(self.translations["catalog.copy_field"])
        set_button_icon(self.copy_field_button, "content_copy")
        self.copy_field_button.setToolTip(self.translations["catalog.copy_field_tooltip"])
        self.copy_field_button.setEnabled(False)
        self.copy_field_button.clicked.connect(self._copy_field)
        row.addWidget(self.copy_field_button)
        return actions

    def _show_kind(self):
        """The kind shown as the primary button, the other as a plain one (as
        the reference graph's directions)."""
        icons = {METHODS: "functions", FIELDS: "data_object"}
        for kind, button in self.kind_buttons.items():
            button.setObjectName("btn_primary" if kind == self.kind else "")
            set_button_icon(button, icons[kind])
            button.style().unpolish(button)
            button.style().polish(button)

    def set_kind(self, kind):
        """Show the methods (METHODS) or the fields (FIELDS); read them the
        first time, or anew if the assembly selection changed since."""
        self.kind = kind
        self.kind_buttons[kind].setChecked(True)
        self._show_kind()
        self.views.setCurrentIndex(0 if kind == METHODS else 1)
        self.action_stack.setCurrentIndex(0 if kind == METHODS else 1)
        self.search_input.setPlaceholderText(self.translations[
            "catalog.search_placeholder" if kind == METHODS
            else "catalog.field_search_placeholder"])
        if self._read.selection != self.selection():
            self.reload()
            return
        self.reload_button.setEnabled(self._worker is None)
        self._toggle_framework(self.framework_check.isChecked())
        self._show_details()

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
        self._read.selection = selection
        self.model.set_filter(self.search_input.text(), self.framework_check.isChecked(),
                              selection)
        # The selection decides which assemblies are shown, framework ones too.
        self.framework_check.setEnabled(not selection)
        self.framework_check.setToolTip(self.translations[
            "catalog.framework_selection_tooltip" if selection else "catalog.framework_tooltip"])
        self._start_worker(None, fresh)

    def _start_worker(self, assemblies, fresh=False):
        kind = self.kind
        reading = self._read
        if self.game is not None:
            worker = CachedCatalogWorker(self.game, assemblies, engine=self.engine, kind=kind)
        else:
            worker = CatalogWorker(self.pid, assemblies, engine=self.engine,
                                   selection=self.selection(), fresh=fresh, kind=kind)
        # What a worker finds goes to its kind, also once the other kind is shown
        worker.assemblies_found.connect(
            lambda assemblies: self._on_assemblies_found(kind, assemblies))
        worker.assembly_cached.connect(reading.cached.add)
        worker.assembly_loaded.connect(
            lambda assembly, entries: self._on_assembly_loaded(kind, assembly, entries))
        worker.assembly_failed.connect(
            lambda assembly, message: self._on_assembly_failed(kind, assembly, message))
        worker.catalog_failed.connect(lambda message: self._on_failed(kind, message))
        worker.finished.connect(lambda: self._on_worker_finished(kind, worker))
        self._worker = worker
        keep_until_finished(worker)
        self.reload_button.setEnabled(False)
        self.status_label.setText(
            self.translations["catalog.starting"].format(engine=self.engine.display_name))
        worker.start()

    def _stop_worker(self, reading=None):
        reading = reading or self._read
        if reading.worker is not None:
            reading.worker.requestInterruption()
            reading.worker = None

    def _on_assemblies_found(self, kind, assemblies):
        self._reads[kind].assemblies = assemblies
        if kind == self.kind:
            self._show_progress()

    def _on_assembly_loaded(self, kind, assembly, entries):
        self._reads[kind].loaded.add(assembly)
        self.models[kind].add(entries)
        if kind == METHODS and self._favorites is not None:
            self._favorites.enrich_methods(entries)
        if kind == self.kind:
            self._show_progress()

    def _on_assembly_failed(self, kind, assembly, message):
        reading = self._reads[kind]
        if message == CachedCatalogWorker.NOT_CACHED:
            reading.not_cached.add(assembly)
        else:
            reading.failed[assembly] = message
        if kind == self.kind:
            self._show_progress()

    def _on_failed(self, kind, message):
        self._reads[kind].error = message
        if kind == self.kind:
            self.status_label.setText(
                self.translations["catalog.failed"].format(message=message))

    def _on_worker_finished(self, kind, worker):
        reading = self._reads[kind]
        if worker is not reading.worker:
            return
        reading.worker = None
        if kind != self.kind:
            return
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
        key = "catalog.summary_one_class" if len(classes) == 1 else "catalog.summary"
        if self.kind == FIELDS:
            key = "catalog.fields_" + key.split(".", 1)[1]
        text = self.translations[key].format(
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

    def selected_methods(self):
        """The methods of the selected rows, top down (several with Ctrl or
        Shift), else the one of the current row."""
        model = self.models[METHODS]
        rows = sorted({index.row() for index in self.view.selectionModel().selectedRows()})
        methods = [method for method in map(model.method_at, rows) if method is not None]
        if not methods:
            index = self.view.currentIndex()
            method = model.method_at(index.row()) if index.isValid() else None
            methods = [method] if method is not None else []
        return methods

    def selected_method(self):
        """The method chosen, or None when there is none or several are selected."""
        methods = self.selected_methods()
        return methods[0] if len(methods) == 1 else None

    def selected_field(self):
        """The field chosen in the field table (the current row), or None."""
        index = self.field_view.currentIndex()
        return self.models[FIELDS].method_at(index.row()) if index.isValid() else None

    def _show_details(self):
        if self.kind == FIELDS:
            self._show_field_details()
            return
        methods = self.selected_methods()
        # Only the monitor takes several methods at once
        self.monitor_button.setEnabled(
            bool(methods) and self._can_run(methods[0], engine_base.MONITOR))
        method = methods[0] if len(methods) == 1 else None
        self.copy_button.setEnabled(method is not None)
        self.calls_button.setEnabled(method is not None
                                     and self.engine.supports(engine_base.CALL_GRAPH))
        self.hook_button.setEnabled(method is not None
                        and self._can_run(method, engine_base.METHOD_HOOKS))
        self.call_button.setEnabled(method is not None
                                    and self._can_run(method, call_capability(method)))
        if len(methods) > 1:
            self.details_label.setText(
                self.translations["catalog.selected_count"].format(count=len(methods)))
            return
        if method is None:
            self.details_label.setText("")
            return
        self.details_label.setText(self.translations["catalog.details"].format(
            signature=csharp_name(method.signature, method.engine),
            returns=csharp_name(method.return_type, method.engine) or "?",
            kind=self.translations["catalog.static" if method.static else "catalog.instance"],
            token=f"0x{method.token:08x}",
            assembly=method.assembly,
        ))

    def _can_run(self, _method, capability):
        """Whether a method can be monitored or called (``capability``): in a
        running game whose engine adapter can."""
        return self.game is None and self.engine.supports(capability)

    def _request_monitor(self):
        methods = self.selected_methods()
        if methods and self._can_run(methods[0], engine_base.MONITOR):
            self.monitor_requested.emit(methods)

    def _request_hook(self):
        method = self.selected_method()
        if method is not None and self._can_run(method, engine_base.METHOD_HOOKS):
            self.hook_requested.emit(method)

    def _request_call(self):
        method = self.selected_method()
        if method is not None and self._can_run(method, call_capability(method)):
            self.call_requested.emit(method)

    def _show_calls(self):
        """Open the call graph of the selected method in a window of its own."""
        method = self.selected_method()
        if method is None:
            return
        graph = CallGraphDialog(self.translations, self.pid, method, parent=self,
                                engine=self.engine, selection=self.selection(), game=self.game)
        graph.monitor_requested.connect(lambda method: self.monitor_requested.emit([method]))
        graph.show()

    def _copy_signature(self):
        method = self.selected_method()
        if method is not None:
            QGuiApplication.clipboard().setText(method.signature)

    def _show_field_details(self):
        field = self.selected_field()
        self.copy_field_button.setEnabled(field is not None)
        self.objects_button.setEnabled(
            field is not None and self.game is None
            and self.engine.supports(engine_base.OBJECT_BROWSER))
        if field is None:
            self.details_label.setText("")
            return
        if field.static:
            kind = self.translations[{"const": "catalog.static_const",
                                      "thread": "catalog.static_thread"}.get(
                                          field.storage, "catalog.static")]
        else:
            kind = self.translations["catalog.instance_field"]
        self.details_label.setText(self.translations["catalog.field_details"].format(
            signature=csharp_name(field.signature, field.engine),
            type=csharp_name(field.type_name, field.engine), kind=kind,
            offset=f"0x{field.offset:x}", assembly=field.assembly))

    def _request_objects(self):
        """Show the live objects of the selected field's class in the object browser."""
        field = self.selected_field()
        if (field is not None and self.game is None
                and self.engine.supports(engine_base.OBJECT_BROWSER)):
            # As the runtime names it, as the object browser does (Outer/Inner)
            self.objects_requested.emit(field.class_name)

    def _copy_field(self):
        field = self.selected_field()
        if field is not None:
            QGuiApplication.clipboard().setText(csharp_name(field.signature, field.engine))

    def closeEvent(self, event):  # pylint: disable=invalid-name
        for reading in self._reads.values():
            self._stop_worker(reading)
        super().closeEvent(event)
