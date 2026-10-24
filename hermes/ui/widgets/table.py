"""Shared settings for table widgets."""

from PyQt6.QtCore import QEvent, QObject, QTimer  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import QHeaderView  # pylint: disable=import-error,no-name-in-module

# Leaves room for the inline editors (line edits, combo boxes) of editable cells.
TABLE_ROW_HEIGHT = 36


def set_row_height(table):
    """Give all rows of ``table`` the shared row height."""
    table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)


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
