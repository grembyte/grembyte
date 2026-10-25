"""Choosing a game read before, for the offline catalog (see hermes.features.games)."""

from datetime import datetime

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from hermes.engines.registry import engine_by_id
from hermes.features.games import known_games
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height

_GAME, _ENGINE, _UPDATED, _ASSEMBLIES, _PATH = range(5)


class KnownGamesDialog(QDialog):
    """List the games whose catalog was read; ``game`` is the one chosen.

    Only games of an engine Hermes knows are offered (``engine_by_id``).
    """

    def __init__(self, translations, parent=None, directory=None):
        super().__init__(parent)
        self.translations = translations
        self.game = None
        self.games = [game for game in known_games(directory)
                      if engine_by_id(game.engine) is not None]
        self.setWindowTitle(translations["games.title"])
        self.resize(900, 420)
        layout = QVBoxLayout(self)
        hint = QLabel(translations["games.hint" if self.games else "games.none"])
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(len(self.games), 5)
        self.table.setHorizontalHeaderLabels([translations[f"games.column_{key}"] for key in
                                              ("game", "engine", "updated", "assemblies", "path")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        for row, game in enumerate(self.games):
            cached = sum(1 for assembly in game.assemblies if assembly.get("mvid"))
            for column, text in (
                    (_GAME, game.name),
                    (_ENGINE, engine_by_id(game.engine).display_name),
                    (_UPDATED, datetime.fromtimestamp(game.updated).strftime("%d.%m.%Y %H:%M")),
                    (_ASSEMBLIES, str(cached)),
                    (_PATH, game.path)):
                item = QTableWidgetItem(text)
                if column == _ASSEMBLIES:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        header = self.table.horizontalHeader()
        for column in (_GAME, _ENGINE, _UPDATED, _ASSEMBLIES):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_PATH, QHeaderView.ResizeMode.Stretch)
        resizable_columns(self.table)
        copy_menu(self.table, translations)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self._accept())
        if self.games:
            self.table.selectRow(0)
        layout.addWidget(self.table, 1)
        layout.addLayout(dialog_button_row(translations, "games.open", self._accept, self.reject))

    def _accept(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.games):
            self.game = self.games[row]
            self.accept()
