"""Choosing a saved game (a process in the database), e.g. for its notes.

Like the offline catalog's choice of a game (see known_games), but it lists
every process Grembyte has saved, whatever its engine: a game is saved once
Grembyte attaches to it.
"""

from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from grembyte.db.repository.game_export import GameExportRepository
from grembyte.ui.widgets.dialog_buttons import dialog_button_row
from grembyte.ui.widgets.table import copy_menu, resizable_columns, set_row_height

_GAME, _PLATFORM, _NOTES, _PATH = range(4)


class SavedGamesDialog(QDialog):
    """List the saved processes; ``process`` is the SavedProcess chosen.

    ``current`` is the database id of a process to select first.
    """

    def __init__(self, translations, repository=None, current=None, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.process = None
        repository = repository or GameExportRepository()
        error = None
        try:
            self.processes = repository.list_processes()
        except SQLAlchemyError as failure:
            self.processes = []
            error = failure
        self.setWindowTitle(translations["saved_games.title"])
        self.resize(820, 420)
        layout = QVBoxLayout(self)
        if error is not None:
            hint = QLabel(translations["saved_games.failed"].format(error=error))
        else:
            hint = QLabel(translations["saved_games.hint" if self.processes else "saved_games.none"])
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(len(self.processes), 4)
        self.table.setHorizontalHeaderLabels([
            translations["export.column_name"], translations["export.column_platform"],
            translations["export.column_notes"], translations["import.column_path"]])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        for row, process in enumerate(self.processes):
            for column, text in ((_GAME, process.name), (_PLATFORM, process.platform),
                                 (_NOTES, str(process.counts.get("notes", 0))),
                                 (_PATH, process.path)):
                item = QTableWidgetItem(text)
                if column == _NOTES:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if column == _PATH:
                    item.setToolTip(text)
                self.table.setItem(row, column, item)
        header = self.table.horizontalHeader()
        for column in (_GAME, _PLATFORM, _NOTES):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_PATH, QHeaderView.ResizeMode.Stretch)
        resizable_columns(self.table)
        copy_menu(self.table, translations)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self._accept())
        ids = [process.process_id for process in self.processes]
        if ids:
            self.table.selectRow(ids.index(current) if current in ids else 0)
        layout.addWidget(self.table, 1)
        buttons = dialog_button_row(translations, "saved_games.open", self._accept, self.reject)
        self.open_button = buttons.itemAt(buttons.count() - 1).widget()
        self.open_button.setEnabled(bool(self.processes))
        layout.addLayout(buttons)

    def _accept(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.processes):
            self.process = self.processes[row]
            self.accept()
