"""Favorites: a star column for the tables of methods and functions, and their window.

The catalog, the Mono trace and the native trace show a star in their first
column; a click on it saves the method or function as a favorite of the
program (see hermes.features.favorites) or removes it. The favorites window
lists them, keeps a note for each and hands them to the monitors.
"""

from PyQt6.QtCore import QItemSelectionModel, QObject, Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
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

from hermes.features.favorites import METHOD, favorite_key, favorite_of
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.table import copy_menu, resizable_columns


# The star column comes first in every table that has one.
FAVORITE_COLUMN = 0
FAVORITE_COLUMN_KEY = "favorites.column"
STAR = "★"
NO_STAR = "☆"


class FavoriteStore(QObject):
    """The favorites of the program last attached, shared by all windows.

    Keeps the keys in memory, so the tables can ask for every row. Stays
    with the program after it exits: a game that crashed can still have its
    favorites looked at.
    """

    # The set of favorites changed (added or removed)
    changed = pyqtSignal()
    # A database error, as text
    failed = pyqtSignal(str)

    def __init__(self, repository, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.process_id = None
        self._keys = set()

    def set_process(self, process_id):
        """Show the favorites of another program (its database id)."""
        self.process_id = process_id
        self._keys = set()
        if process_id is not None:
            self._keys = {favorite.key for favorite in self.favorites()}
        self.changed.emit()

    def favorites(self):
        """The Favorites of the program, oldest first."""
        if self.process_id is None:
            return []
        try:
            return self.repository.list_favorites(self.process_id)
        except SQLAlchemyError as error:
            self.failed.emit(str(error))
            return []

    def is_favorite(self, item):
        """Whether a CatalogMethod or NativeFunction is a favorite."""
        return item is not None and favorite_key(item) in self._keys

    def toggle(self, item, source=""):
        """Save a CatalogMethod or NativeFunction as a favorite, or remove it; the new state."""
        if item is None or self.process_id is None:
            return False
        key = favorite_key(item)
        try:
            if key in self._keys:
                self.repository.delete_favorite(self.process_id, key)
                self._keys.discard(key)
            else:
                self.repository.add_favorite(self.process_id, favorite_of(item, source))
                self._keys.add(key)
        except SQLAlchemyError as error:
            self.failed.emit(str(error))
            return key in self._keys
        self.changed.emit()
        return key in self._keys

    def remove(self, favorite):
        """Remove a Favorite."""
        try:
            self.repository.delete_favorite(self.process_id, favorite.key)
        except SQLAlchemyError as error:
            self.failed.emit(str(error))
            return
        self._keys.discard(favorite.key)
        self.changed.emit()

    def set_note(self, favorite, note):
        """Change the note of a Favorite."""
        try:
            self.repository.set_note(favorite.favorite_id, note)
        except SQLAlchemyError as error:
            self.failed.emit(str(error))
            return
        favorite.note = note


class FavoriteColumn:
    """For table models whose first column is the star.

    The model sets ``favorites`` (a FavoriteStore or None) and implements
    ``favorite_item(row)``: the CatalogMethod or NativeFunction of a row.
    """

    favorites = None

    def favorite_item(self, row):
        raise NotImplementedError

    def favorite_data(self, row, role):
        """What the star cell of ``row`` shows for ``role``."""
        if role == Qt.ItemDataRole.DisplayRole:
            store = self.favorites
            return STAR if store is not None and store.is_favorite(self.favorite_item(row)) \
                else NO_STAR
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.translations["favorites.star_tooltip"]
        return None

    def favorites_changed(self):
        """Repaint the stars."""
        rows = self.rowCount()
        if rows:
            self.dataChanged.emit(self.index(0, FAVORITE_COLUMN),
                                  self.index(rows - 1, FAVORITE_COLUMN))


def set_up_favorite_column(view, model, store, source):
    """Let a click on the star column of ``view`` mark the row's item as a favorite.

    Hides the column without a ``store``.
    """
    header = view.horizontalHeader()
    header.setSectionResizeMode(FAVORITE_COLUMN, QHeaderView.ResizeMode.Fixed)
    header.resizeSection(FAVORITE_COLUMN, 32)
    model.favorites = store
    if store is None:
        view.setColumnHidden(FAVORITE_COLUMN, True)
        return
    store.changed.connect(model.favorites_changed)
    view.clicked.connect(lambda index: index.column() == FAVORITE_COLUMN and store.toggle(
        model.favorite_item(index.row()), source))


_KIND, _NAME, _LOCATION, _NOTE, _SOURCE, _ADDED = range(6)
_COLUMNS = (
    "favorites.column_kind", "favorites.column_name", "favorites.column_location",
    "favorites.column_note", "favorites.column_source", "favorites.column_added",
)


class FavoritesDialog(QDialog):
    """The favorites of the program: search, annotate, monitor, remove.

    Not modal. Monitoring needs the game running; without it the list stays
    readable, e.g. after the game crashed.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # [CatalogMethod] to log the calls of
    method_monitor_requested = pyqtSignal(list)
    # [NativeFunction] to log the calls of
    native_monitor_requested = pyqtSignal(list)
    # static CatalogMethod to call ("Call …")
    call_requested = pyqtSignal(object)

    def __init__(self, translations, store, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.store = store
        self.attached = False
        self._shown = []
        self._filling = False
        self.setWindowTitle(translations["favorites.title"])
        self.resize(1000, 520)
        self._build_ui()
        store.changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        hint = QLabel(t["favorites.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t["favorites.search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(lambda _text: self.refresh())
        layout.addWidget(self.search_input)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([t[column] for column in _COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Ctrl and Shift select several, e.g. to monitor them at once.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column in (_NAME, _NOTE)
                else QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_KIND, 70)
        header.resizeSection(_LOCATION, 220)
        header.resizeSection(_SOURCE, 120)
        header.resizeSection(_ADDED, 140)
        # A click on a column header sorts by it, a third one restores the order added in.
        # The rows are sorted here, not by the table: rows stand for ``_shown``.
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicatorClearable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sortIndicatorChanged.connect(lambda _column, _order: self.refresh())
        resizable_columns(self.table)
        copy_menu(self.table, self.translations)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        self.table.itemSelectionChanged.connect(self._show_selection)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.selection_label = SelectableText()
        actions.addWidget(self.selection_label, 1)
        self.monitor_button = QPushButton(t["favorites.monitor"])
        self.monitor_button.setObjectName("btn_primary")
        set_button_icon(self.monitor_button, "visibility")
        self.monitor_button.setToolTip(t["favorites.monitor_tooltip"])
        self.monitor_button.clicked.connect(self._request_monitor)
        actions.addWidget(self.monitor_button)
        self.call_button = QPushButton(t["favorites.call"])
        set_button_icon(self.call_button, "call_made")
        self.call_button.clicked.connect(self._request_call)
        actions.addWidget(self.call_button)
        self.copy_button = QPushButton(t["favorites.copy"])
        set_button_icon(self.copy_button, "content_copy")
        self.copy_button.setToolTip(t["favorites.copy_tooltip"])
        self.copy_button.clicked.connect(self._copy)
        actions.addWidget(self.copy_button)
        self.remove_button = QPushButton(t["favorites.remove"])
        set_button_icon(self.remove_button, "delete")
        self.remove_button.clicked.connect(self._remove)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # --- Content -----------------------------------------------------------------
    def set_attached(self, attached):
        """Whether the program runs, so its favorites can be monitored."""
        self.attached = attached
        self._show_selection()

    def refresh(self):
        """Show the favorites anew, keeping the selection."""
        t = self.translations
        selected = {favorite.key for favorite in self.selected_favorites()}
        current = self.selected_favorite()
        words = self.search_input.text().lower().split()
        favorites = self.store.favorites()
        self._shown = self._sorted([favorite for favorite in favorites if favorite.matches(words)])
        self._filling = True
        self.table.setRowCount(len(self._shown))
        for row, favorite in enumerate(self._shown):
            kind = t["favorites.kind_method" if favorite.kind == METHOD else "favorites.kind_native"]
            location = favorite.location
            location_tip = location
            if not favorite.lasting:
                location += "  ⚠"
                location_tip = t["favorites.address_only"]
            added = favorite.created_at.strftime("%Y-%m-%d %H:%M") if favorite.created_at else ""
            for column, text, tooltip in (
                    (_KIND, kind, None),
                    (_NAME, favorite.label, favorite.signature),
                    (_LOCATION, location, location_tip),
                    (_NOTE, favorite.note, t["favorites.note_tooltip"]),
                    (_SOURCE, t.get(f"favorites.source_{favorite.source}", favorite.source),
                     None),
                    (_ADDED, added, None)):
                item = QTableWidgetItem(text)
                if tooltip:
                    item.setToolTip(tooltip)
                if column != _NOTE:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
        self._filling = False
        self._select({favorite.key for favorite in self._shown} & selected,
                     current.key if current is not None else None)
        self.status_label.setText(t["favorites.count"].format(
            shown=len(self._shown), total=len(favorites)))
        self._show_selection()

    def _sorted(self, favorites):
        """``favorites`` in the order the header's sort indicator asks for."""
        header = self.table.horizontalHeader()
        column = header.sortIndicatorSection()
        if column < 0 or column >= len(_COLUMNS):
            return favorites
        t = self.translations
        keys = {
            _KIND: lambda favorite: favorite.kind,
            _NAME: lambda favorite: favorite.label.lower(),
            _LOCATION: lambda favorite: favorite.location.lower(),
            _NOTE: lambda favorite: (not favorite.note, favorite.note.lower()),
            _SOURCE: lambda favorite: t.get(f"favorites.source_{favorite.source}",
                                            favorite.source).lower(),
            _ADDED: lambda favorite: (favorite.created_at is None,
                                      favorite.created_at or 0),
        }
        return sorted(favorites, key=keys[column],
                      reverse=header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder)

    def _select(self, keys, current_key):
        """Select the rows of the favorites with ``keys``; ``current_key`` gets the focus."""
        selection = self.table.selectionModel()
        flags = (QItemSelectionModel.SelectionFlag.Select
                 | QItemSelectionModel.SelectionFlag.Rows)
        self.table.blockSignals(True)
        self.table.clearSelection()
        for row, favorite in enumerate(self._shown):
            if favorite.key in keys:
                selection.select(self.table.model().index(row, 0), flags)
            if favorite.key == current_key:
                selection.setCurrentIndex(self.table.model().index(row, 0),
                                          QItemSelectionModel.SelectionFlag.NoUpdate)
        self.table.blockSignals(False)

    def selected_favorite(self):
        """The favorite of the current row (the one last clicked), or None."""
        row = self.table.currentRow()
        return self._shown[row] if 0 <= row < len(self._shown) else None

    def selected_favorites(self):
        """The selected favorites, in the order of the table."""
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        return [self._shown[row] for row in rows if row < len(self._shown)]

    def _show_selection(self):
        favorites = self.selected_favorites()
        self.monitor_button.setEnabled(bool(favorites) and self.attached)
        self.monitor_button.setToolTip(self.translations[
            "favorites.monitor_tooltip" if self.attached else "favorites.not_attached"])
        callable_method = self._callable_method(favorites)
        self.call_button.setEnabled(callable_method is not None and self.attached)
        self.call_button.setToolTip(self.translations[
            "favorites.not_attached" if not self.attached
            else "favorites.call_tooltip" if callable_method is not None
            else "favorites.call_static_only"])
        self.copy_button.setEnabled(bool(favorites))
        self.remove_button.setEnabled(bool(favorites))
        if len(favorites) == 1:
            text = favorites[0].signature
        elif favorites:
            text = self.translations["favorites.selected"].format(count=len(favorites))
        else:
            text = ""
        self.selection_label.setText(text)

    # --- Actions -----------------------------------------------------------------
    def _on_item_changed(self, item):
        if self._filling or item.column() != _NOTE:
            return
        favorite = self._shown[item.row()] if item.row() < len(self._shown) else None
        if favorite is not None and item.text() != favorite.note:
            self.store.set_note(favorite, item.text())

    def _on_double_click(self, item):
        if item.column() != _NOTE:
            self._request_monitor()

    def _request_monitor(self):
        if not self.attached:
            return
        favorites = self.selected_favorites()
        methods = [favorite.method() for favorite in favorites if favorite.kind == METHOD]
        functions = [favorite.function() for favorite in favorites if favorite.kind != METHOD]
        if methods:
            self.method_monitor_requested.emit(methods)
        if functions:
            self.native_monitor_requested.emit(functions)

    @staticmethod
    def _callable_method(favorites):
        """The CatalogMethod of the only selected favorite if it is static, else None."""
        if len(favorites) != 1 or favorites[0].kind != METHOD:
            return None
        method = favorites[0].method()
        return method if method.static else None

    def _request_call(self):
        method = self._callable_method(self.selected_favorites())
        if method is not None and self.attached:
            self.call_requested.emit(method)

    def _copy(self):
        favorites = self.selected_favorites()
        if favorites:
            QGuiApplication.clipboard().setText("\n".join(
                favorite.signature if favorite.kind == METHOD else favorite.location
                for favorite in favorites))

    def _remove(self):
        favorites = self.selected_favorites()
        if len(favorites) > 1 and QMessageBox.question(
                self, self.translations["favorites.title"],
                self.translations["favorites.remove_several"].format(count=len(favorites)),
        ) != QMessageBox.StandardButton.Yes:
            return
        for favorite in favorites:
            self.store.remove(favorite)
