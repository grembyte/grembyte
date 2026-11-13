"""The window offering to delete pointer scans that are no longer needed.

Shown after the start when grembyte.db.repository.addresses finds obsolete
scans the user was not asked about yet: each saved scan can take megabytes
in the database. The address-list entries and their pointers stay.
"""

from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from grembyte.db.repository.addresses import EMPTY
from grembyte.ui import window_state
from grembyte.ui.widgets.dialog_buttons import dialog_button_row
from grembyte.ui.widgets.labels import WrappedLabel
from grembyte.ui.widgets.table import resizable_columns, set_row_height

ENTRY_COLUMN, GAME_COLUMN, COUNT_COLUMN, SIZE_COLUMN, REASON_COLUMN = range(5)

# The scans (ObsoleteScan.key) the user chose to keep; not offered again
# until they are filtered or scanned anew.
_KEPT_KEY = "pointer_scan/kept_obsolete"


def kept_scans():
    """The keys of the obsolete scans the user chose to keep."""
    stored = window_state.settings().value(_KEPT_KEY)
    if isinstance(stored, str):
        return set(filter(None, stored.split("\n")))
    if isinstance(stored, (list, tuple)):
        return {str(key) for key in stored}
    return set()


def keep_scans(scans, current):
    """Remember ``scans`` as kept; ``current`` are all obsolete scans now (the rest
    is forgotten, so the list does not grow)."""
    keys = (kept_scans() & {scan.key() for scan in current}) | {scan.key() for scan in scans}
    window_state.settings().setValue(_KEPT_KEY, "\n".join(sorted(keys)))


def scans_to_offer(scans):
    """The obsolete ``scans`` the user did not choose to keep."""
    kept = kept_scans()
    return [scan for scan in scans if scan.key() not in kept]


class PointerCleanupDialog(QDialog):
    """List obsolete pointer scans with a check box each; ``chosen()`` after accept."""

    def __init__(self, translations, scans, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.scans = list(scans)
        self.setWindowTitle(translations["pointer_cleanup.title"])
        self.resize(760, 360)
        layout = QVBoxLayout(self)
        total = sum(scan.size for scan in self.scans)
        layout.addWidget(WrappedLabel(translations["pointer_cleanup.hint"].format(
            count=len(self.scans), size=total / 1e6)))

        self.table = QTableWidget(len(self.scans), 5)
        self.table.setHorizontalHeaderLabels([
            translations["pointer_cleanup.column_entry"],
            translations["pointer_cleanup.column_game"],
            translations["pointer_cleanup.column_count"],
            translations["pointer_cleanup.column_size"],
            translations["pointer_cleanup.column_reason"],
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        for row, scan in enumerate(self.scans):
            entry = QTableWidgetItem(scan.label)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, ENTRY_COLUMN, entry)
            self.table.setItem(row, GAME_COLUMN, QTableWidgetItem(scan.process))
            count = QTableWidgetItem("–" if scan.count is None else str(scan.count))
            size = QTableWidgetItem(translations["pointer_cleanup.size"].format(
                size=scan.size / 1e6))
            for item in (count, size):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, COUNT_COLUMN, count)
            self.table.setItem(row, SIZE_COLUMN, size)
            reason = translations[f"pointer_cleanup.reason_{scan.reason}"]
            if scan.reason != EMPTY:
                reason = reason.format(days=scan.days)
            self.table.setItem(row, REASON_COLUMN, QTableWidgetItem(reason))
        header = self.table.horizontalHeader()
        for column in (ENTRY_COLUMN, GAME_COLUMN, COUNT_COLUMN, SIZE_COLUMN):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(REASON_COLUMN, QHeaderView.ResizeMode.Stretch)
        resizable_columns(self.table)
        layout.addWidget(self.table, 1)
        layout.addLayout(dialog_button_row(
            translations, "pointer_cleanup.delete", self.accept, self.reject))

    def chosen(self):
        """The scans whose box is ticked."""
        return [scan for row, scan in enumerate(self.scans)
                if self.table.item(row, ENTRY_COLUMN).checkState() == Qt.CheckState.Checked]
