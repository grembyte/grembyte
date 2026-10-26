"""The window that deletes entries saved for processes (File > Delete saved entries).

One row per saved process (name and path). Each count of entries has a
check box; the box in front of a row ticks all of them. "Remove process"
deletes the process itself with everything saved for it. Deleting
addresses deletes their pointer scans as well.
"""

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.db.repository.game_export import DELETABLE
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.table import resizable_columns, set_row_height

ROW_COLUMN, NAME_COLUMN, PLATFORM_COLUMN, PATH_COLUMN = range(4)
# Columns of the kinds of entries, then the one that removes the process
KIND_COLUMNS = {kind: 4 + index for index, kind in enumerate(DELETABLE)}
PROCESS_COLUMN = 4 + len(DELETABLE)
CHECKED, UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked


class DeleteEntriesDialog(QDialog):
    """Tick the entries to delete per saved process, then delete them.

    ``current`` is the database id of the attached process: it cannot be
    removed, only its entries. ``deleted`` holds {process_id: {kind: count}}
    after a deletion.
    """

    def __init__(self, translations, repository, current=None, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.repository = repository
        self.current = current
        self.deleted = {}
        self._updating = False
        self.setWindowTitle(translations["delete_entries.title"])
        self.setMinimumWidth(900)
        layout = QVBoxLayout(self)
        hint = QLabel(translations["delete_entries.hint"])
        hint.setWordWrap(True)
        layout.addWidget(hint)

        headers = (["", translations["export.column_name"], translations["export.column_platform"],
                    translations["import.column_path"]]
                   + [translations[f"export.column_{kind}"] for kind in DELETABLE]
                   + [translations["delete_entries.column_process"]])
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PATH_COLUMN, QHeaderView.ResizeMode.Stretch)
        self._sizer = resizable_columns(self.table)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = dialog_button_row(translations, "delete_entries.button", self._delete,
                                    self.reject)
        self.delete_button = buttons.itemAt(buttons.count() - 1).widget()
        layout.addLayout(buttons)

        self.processes = []
        self._load()

    def _load(self):
        try:
            self.processes = self.repository.list_processes()
        except SQLAlchemyError as error:
            self.summary.setText(self.translations["delete_entries.failed"].format(error=error))
            self.processes = []
        self._updating = True
        self.table.setRowCount(len(self.processes))
        for row, process in enumerate(self.processes):
            self.table.setItem(row, ROW_COLUMN, self._check_item(""))
            for column, value in ((NAME_COLUMN, process.name),
                                  (PLATFORM_COLUMN, process.platform),
                                  (PATH_COLUMN, process.path)):
                item = QTableWidgetItem(value)
                item.setToolTip(process.path)
                self.table.setItem(row, column, item)
            for kind, column in KIND_COLUMNS.items():
                count = process.counts.get(kind, 0)
                self.table.setItem(row, column, self._check_item(str(count), enabled=count > 0))
            item = self._check_item("", enabled=process.process_id != self.current)
            if process.process_id == self.current:
                item.setToolTip(self.translations["delete_entries.attached"])
            self.table.setItem(row, PROCESS_COLUMN, item)
        self._updating = False
        if not self.processes and not self.summary.text():
            self.summary.setText(self.translations["delete_entries.empty"])
        self._update_summary()

    @staticmethod
    def _check_item(text, enabled=True):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                      if enabled else Qt.ItemFlag.NoItemFlags)
        item.setCheckState(UNCHECKED)
        return item

    def _checked(self, row, column):
        item = self.table.item(row, column)
        return item is not None and item.checkState() == CHECKED

    def _set_checked(self, row, column, checked):
        item = self.table.item(row, column)
        if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            item.setCheckState(CHECKED if checked else UNCHECKED)

    def _on_item_changed(self, item):
        if self._updating:
            return
        self._updating = True
        row, column = item.row(), item.column()
        checked = item.checkState() == CHECKED
        if column == ROW_COLUMN:
            for kind_column in KIND_COLUMNS.values():
                self._set_checked(row, kind_column, checked)
            if not checked:
                self._set_checked(row, PROCESS_COLUMN, False)
        elif column == PROCESS_COLUMN and checked:
            # The process goes with all its entries.
            for kind_column in (ROW_COLUMN, *KIND_COLUMNS.values()):
                self._set_checked(row, kind_column, True)
        elif column in KIND_COLUMNS.values():
            if column == KIND_COLUMNS["addresses"] and checked:
                # Pointer scans belong to their addresses.
                self._set_checked(row, KIND_COLUMNS["pointer_scans"], True)
            if not checked:
                self._set_checked(row, PROCESS_COLUMN, False)
                if column == KIND_COLUMNS["pointer_scans"]:
                    self._set_checked(row, KIND_COLUMNS["addresses"], False)
        self._sync_row_box(row)
        self._updating = False
        self._update_summary()

    def _sync_row_box(self, row):
        """The row's box is ticked while every count that can be is ticked."""
        boxes = [column for column in KIND_COLUMNS.values()
                 if self.table.item(row, column).flags() & Qt.ItemFlag.ItemIsUserCheckable]
        self.table.item(row, ROW_COLUMN).setCheckState(
            CHECKED if boxes and all(self._checked(row, column) for column in boxes)
            else UNCHECKED)

    def selection(self):
        """[(SavedProcess, kinds, remove_process)] of the rows with something ticked."""
        chosen = []
        for row, process in enumerate(self.processes):
            kinds = [kind for kind, column in KIND_COLUMNS.items() if self._checked(row, column)]
            remove = self._checked(row, PROCESS_COLUMN)
            if kinds or remove:
                chosen.append((process, kinds, remove))
        return chosen

    def _update_summary(self):
        chosen = self.selection()
        entries = sum(process.counts.get(kind, 0)
                      for process, kinds, remove in chosen
                      for kind in (DELETABLE if remove else kinds))
        removed = sum(1 for _process, _kinds, remove in chosen if remove)
        if chosen:
            self.summary.setText(self.translations["delete_entries.summary"].format(
                entries=entries, processes=len(chosen), removed=removed))
        elif self.processes:
            self.summary.setText(self.translations["delete_entries.nothing"])
        self.delete_button.setEnabled(bool(chosen))

    def _delete(self):
        chosen = self.selection()
        if not chosen:
            return
        lines = []
        for process, kinds, remove in chosen:
            what = (self.translations["delete_entries.column_process"] if remove else
                    ", ".join(self.translations[f"export.column_{kind}"] for kind in kinds))
            lines.append(f"• {process.name} ({process.path}): {what}")
        if QMessageBox.question(
                self, self.translations["delete_entries.title"],
                self.translations["delete_entries.confirm"].format(list="\n".join(lines)),
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            for process, kinds, remove in chosen:
                self.deleted[process.process_id] = self.repository.delete_entries(
                    process.process_id, kinds, remove_process=remove)
        except SQLAlchemyError as error:
            self.summary.setText(self.translations["delete_entries.failed"].format(error=error))
            return
        self.accept()
