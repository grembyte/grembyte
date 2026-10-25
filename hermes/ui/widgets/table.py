"""Shared settings for table widgets."""

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractButton,
    QComboBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTableWidget,
)

# Leaves room for the inline editors (line edits, combo boxes) of editable cells.
TABLE_ROW_HEIGHT = 36


def set_row_height(table):
    """Give all rows of ``table`` the shared row height."""
    table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)


def cell_text(view, row, column):
    """What a cell of ``view`` (a QTableView or QTableWidget) shows, as text.

    Cell widgets count too: a ticked check box is "✓", a combo box or line
    edit its text. A link (e.g. "Objekt-Details") is an action, not a value:
    it gives "".
    """
    text = view.model().index(row, column).data(Qt.ItemDataRole.DisplayRole)
    text = "" if text is None else str(text)
    widget = view.cellWidget(row, column) if isinstance(view, QTableWidget) else None
    if text or widget is None:
        return text
    for kind in (QComboBox, QLineEdit, QAbstractButton, QLabel):
        found = widget if isinstance(widget, kind) else widget.findChild(kind)
        if found is None:
            continue
        if isinstance(found, QComboBox):
            return found.currentText()
        if isinstance(found, QAbstractButton) and found.isCheckable():
            return "✓" if found.isChecked() else ""
        if isinstance(found, QLabel) and "<a " in found.text():
            return ""
        return found.text()
    return ""


def row_text(view, row):
    """The cells of ``row`` that are not hidden, separated by tabs (for a spreadsheet)."""
    header = view.horizontalHeader()
    return "\t".join(cell_text(view, row, column) for column in range(header.count())
                     if not header.isSectionHidden(column))


def add_copy_actions(menu, view, position, translations):
    """Add "copy row" and "copy cell" for the cell at ``position`` (viewport
    coordinates of ``view``) to ``menu``; nothing outside the rows."""
    index = view.indexAt(position)
    if not index.isValid():
        return
    if not menu.isEmpty():
        menu.addSeparator()
    row, column = index.row(), index.column()
    menu.addAction(translations["table.copy_row"],
                   lambda: QGuiApplication.clipboard().setText(row_text(view, row)))
    menu.addAction(translations["table.copy_cell"],
                   lambda: QGuiApplication.clipboard().setText(cell_text(view, row, column)))


def copy_menu(view, translations):
    """Give ``view`` (a table without a context menu of its own) one that copies."""
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(position):
        menu = QMenu(view)
        add_copy_actions(menu, view, position, translations)
        if not menu.isEmpty():
            menu.exec(view.viewport().mapToGlobal(position))

    view.customContextMenuRequested.connect(show)


def resizable_columns(view):
    """Let the user resize the columns of ``view`` that Qt sizes by itself.

    Call it once the header's resize modes are set. Qt does not let the user
    resize a ``Stretch`` or ``ResizeToContents`` column; these become
    ``Interactive`` and keep behaving as before until the user resizes them:
    stretch columns share the free width, content columns fit their contents.
    ``Fixed`` columns (stars, buttons, combo boxes) stay fixed.
    """
    return _ColumnSizer(view)


class _ColumnSizer(QObject):
    """Sizes the columns of a view like Qt's automatic modes; see ``resizable_columns``."""

    def __init__(self, view):
        header = view.horizontalHeader()
        super().__init__(header)
        self._view = view
        self._header = header
        self._fill = []
        self._fit = []
        # Columns the user sized: they keep their width.
        self._sized = set()
        self._adjusting = False
        for column in range(header.count()):
            mode = header.sectionResizeMode(column)
            if mode == QHeaderView.ResizeMode.Stretch:
                self._fill.append(column)
            elif mode == QHeaderView.ResizeMode.ResizeToContents:
                self._fit.append(column)
            else:
                continue
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._adjust)
        header.sectionResized.connect(self._on_section_resized)
        header.installEventFilter(self)
        model = view.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset,
                       model.layoutChanged, model.dataChanged):
            signal.connect(self._schedule)
        self._schedule()

    def eventFilter(self, watched, event):  # pylint: disable=invalid-name
        """Share the free width again when the table changes its width."""
        if event.type() == QEvent.Type.Resize:
            self._schedule()
        return super().eventFilter(watched, event)

    def _on_section_resized(self, column, _old, _new):
        if not self._adjusting:
            self._sized.add(column)
            self._schedule()

    def _schedule(self, *_args):
        if not self._timer.isActive():
            self._timer.start()

    def _adjust(self):
        header = self._header

        def shown(column):
            return column not in self._sized and not header.isSectionHidden(column)

        self._adjusting = True
        try:
            for column in filter(shown, self._fit):
                header.resizeSection(column, max(header.sectionSizeHint(column),
                                                 self._view.sizeHintForColumn(column)))
            fill = list(filter(shown, self._fill))
            if not fill:
                return
            others = sum(header.sectionSize(column) for column in range(header.count())
                         if column not in fill and not header.isSectionHidden(column))
            share = (header.viewport().width() - others) // len(fill)
            for column in fill:
                header.resizeSection(column, max(header.sectionSizeHint(column), share))
        finally:
            self._adjusting = False
