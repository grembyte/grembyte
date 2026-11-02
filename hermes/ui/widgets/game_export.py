"""The windows that export what is saved for a game as JSON and import it (File menu).

Export: the user picks a game (all processes of one name, see
hermes.db.repository.game_export), whether the pointer scans go along, and
the target file. The file name is made from the game's platform, its name
and today's date until the user changes it.

Import: the user picks an export file and the saved process that takes its
entries (it keeps its own path), and whether they replace its entries or
are mixed in.
"""

from datetime import date
from pathlib import Path

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.db.repository.game_export import (
    ImportFormatError,
    export_counts,
    export_file_name,
    read_export,
)
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.table import resizable_columns, set_row_height

# Columns after the game's name and platform: the counts of its entries
# Pointer scans last: the import's table leaves them out
_COUNT_COLUMNS = ("addresses", "hooks", "method_hooks", "favorites", "scripts", "pointer_scans")


class GameExportDialog(QDialog):
    """Pick a game and a file, then write the game's export there.

    ``current`` is the name of the attached process: its game is selected
    first. ``exported_path`` holds the file written last.
    """

    def __init__(self, translations, repository, current=None, today=None, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.repository = repository
        self.today = today or date.today()
        self.exported_path = None
        self._suggested = ""
        self.setWindowTitle(translations["export.title"])
        self.setMinimumWidth(760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(translations["export.hint"]))

        headers = ["name", "platform", *_COUNT_COLUMNS]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(
            [translations[f"export.column_{key}"] for key in headers])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._sizer = resizable_columns(self.table)
        self.table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self.table)

        self.pointer_scans = QCheckBox(translations["export.pointer_scans"])
        self.pointer_scans.setToolTip(translations["export.pointer_scans_tooltip"])
        self.pointer_scans.setChecked(False)
        layout.addWidget(self.pointer_scans)

        form = QFormLayout()
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.home()))
        folder_row.addWidget(self.folder)
        browse = QPushButton(translations["export.browse"])
        set_button_icon(browse, "folder_open")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        form.addRow(translations["export.folder"], folder_row)
        self.file_name = QLineEdit()
        self.file_name.setToolTip(translations["export.file_name_tooltip"])
        self.file_name.textChanged.connect(lambda _text: self._update_buttons())
        form.addRow(translations["export.file_name"], self.file_name)
        layout.addLayout(form)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = dialog_button_row(translations, "export.button", self._export, self.reject)
        self.export_button = buttons.itemAt(buttons.count() - 1).widget()
        layout.addLayout(buttons)

        self.games = []
        self._load(current)

    def _load(self, current):
        try:
            self.games = self.repository.list_games()
        except SQLAlchemyError as error:
            self.status.setText(self.translations["export.failed"].format(error=error))
            self.games = []
        self.table.setRowCount(len(self.games))
        for row, game in enumerate(self.games):
            values = [game.name, game.platform] + [
                str(game.counts.get(key, 0)) for key in _COUNT_COLUMNS]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip("\n".join(game.paths))
                self.table.setItem(row, column, item)
        if not self.games and not self.status.text():
            self.status.setText(self.translations["export.empty"])
        names = [game.name for game in self.games]
        if names:
            self.table.selectRow(names.index(current) if current in names else 0)
        self._update_buttons()

    @property
    def selected_game(self):
        rows = self.table.selectionModel().selectedRows()
        return self.games[rows[0].row()] if rows else None

    def _on_selection(self):
        game = self.selected_game
        if game is None:
            return
        scans = game.counts.get("pointer_scans", 0)
        self.pointer_scans.setEnabled(scans > 0)
        if not scans:
            self.pointer_scans.setChecked(False)
        # A name the user typed stays; the suggestion follows the game.
        if self.file_name.text() in ("", self._suggested):
            self._suggested = export_file_name(game.platform, game.name, self.today)
            self.file_name.setText(self._suggested)
        self._update_buttons()

    def _update_buttons(self):
        self.export_button.setEnabled(
            self.selected_game is not None and bool(self.file_name.text().strip()))

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, self.translations["export.folder"], self.folder.text())
        if folder:
            self.folder.setText(folder)

    @property
    def target_path(self):
        name = self.file_name.text().strip()
        if not name.lower().endswith(".json"):
            name += ".json"
        return Path(self.folder.text().strip() or Path.home()).expanduser() / name

    def _export(self):
        game = self.selected_game
        if game is None:
            return
        path = self.target_path
        if path.exists() and QMessageBox.question(
                self, self.translations["export.title"],
                self.translations["export.overwrite"].format(path=path),
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.repository.write_export(game.name, path, self.pointer_scans.isChecked())
        except (OSError, SQLAlchemyError) as error:
            self.status.setText(self.translations["export.failed"].format(error=error))
            return
        self.exported_path = path
        self.accept()


class GameImportDialog(QDialog):
    """Pick an export file and the saved process that takes its entries.

    ``current`` is the database id of the attached process: it is selected
    first, else the first process of the file's game. ``result`` holds the
    ImportResult and ``process_id`` the target after an import.
    """

    def __init__(self, translations, repository, current=None, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.repository = repository
        self.current = current
        self.export = None
        self.result = None
        self.process_id = None
        self.setWindowTitle(translations["import.title"])
        self.setMinimumWidth(760)
        layout = QVBoxLayout(self)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel(translations["import.file"]))
        self.path = QLineEdit()
        self.path.setReadOnly(True)
        file_row.addWidget(self.path)
        browse = QPushButton(translations["import.browse"])
        set_button_icon(browse, "folder_open")
        browse.clicked.connect(self._browse)
        file_row.addWidget(browse)
        layout.addLayout(file_row)
        self.summary = QLabel(translations["import.no_file"])
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        layout.addWidget(QLabel(translations["import.target"]))
        headers = ["name", "platform", "path", *_COUNT_COLUMNS[:-1]]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(
            [translations[f"import.column_{key}"] if key == "path"
             else translations[f"export.column_{key}"] for key in headers])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._sizer = resizable_columns(self.table)
        self.table.itemSelectionChanged.connect(self._update)
        layout.addWidget(self.table)

        self.merge = QRadioButton(translations["import.merge"])
        self.merge.setToolTip(translations["import.merge_tooltip"])
        self.merge.setChecked(True)
        self.replace = QRadioButton(translations["import.replace"])
        self.replace.setToolTip(translations["import.replace_tooltip"])
        layout.addWidget(self.merge)
        layout.addWidget(self.replace)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = dialog_button_row(translations, "import.button", self._import, self.reject)
        self.import_button = buttons.itemAt(buttons.count() - 1).widget()
        layout.addLayout(buttons)

        self.processes = []
        self._load_processes()

    def _load_processes(self):
        try:
            self.processes = self.repository.list_processes()
        except SQLAlchemyError as error:
            self.status.setText(self.translations["import.failed"].format(error=error))
            self.processes = []
        self.table.setRowCount(len(self.processes))
        for row, process in enumerate(self.processes):
            values = [process.name, process.platform, process.path] + [
                str(process.counts.get(key, 0)) for key in _COUNT_COLUMNS[:-1]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setToolTip(value)
                self.table.setItem(row, column, item)
        self._select_target()

    def _select_target(self):
        ids = [process.process_id for process in self.processes]
        game = None if self.export is None else self.export.get("game")
        if self.current in ids and (
                game is None or self.processes[ids.index(self.current)].name == game):
            self.table.selectRow(ids.index(self.current))
        elif game is not None and any(process.name == game for process in self.processes):
            self.table.selectRow(next(row for row, process in enumerate(self.processes)
                                      if process.name == game))
        elif self.current in ids:
            self.table.selectRow(ids.index(self.current))
        self._update()

    @property
    def selected_process(self):
        rows = self.table.selectionModel().selectedRows()
        return self.processes[rows[0].row()] if rows else None

    def _browse(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, self.translations["import.title"], self.path.text() or str(Path.home()),
            self.translations["import.filter"])
        if path:
            self.load_file(path)

    def load_file(self, path):
        """Read the export in ``path`` and show what it holds."""
        self.path.setText(str(path))
        try:
            self.export = read_export(path)
        except (OSError, ImportFormatError) as error:
            self.export = None
            self.summary.setText(self.translations["import.bad_file"].format(error=error))
            self._update()
            return
        counts = export_counts(self.export)
        self.summary.setText(self.translations["import.summary"].format(
            game=self.export.get("game", "?"), platform=self.export.get("platform", "?"),
            date=str(self.export.get("exported_at", "?"))[:10],
            processes=len(self.export["processes"]), **counts))
        self._select_target()

    def _update(self):
        process = self.selected_process
        warning = ""
        if self.export is not None and process is not None:
            if process.name != self.export.get("game"):
                warning = self.translations["import.other_game"].format(
                    game=self.export.get("game"), name=process.name)
            elif process.platform != self.export.get("platform"):
                warning = self.translations["import.other_platform"].format(
                    platform=self.export.get("platform"), target=process.platform)
        self.status.setText(warning)
        self.import_button.setEnabled(self.export is not None and process is not None)

    def _import(self):
        process = self.selected_process
        if self.export is None or process is None:
            return
        replace = self.replace.isChecked()
        if replace and QMessageBox.question(
                self, self.translations["import.title"],
                self.translations["import.replace_confirm"].format(
                    name=process.name, path=process.path),
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.result = self.repository.import_export(self.export, process.process_id, replace)
        except (SQLAlchemyError, ValueError, TypeError) as error:
            self.status.setText(self.translations["import.failed"].format(error=error))
            return
        self.process_id = process.process_id
        self.accept()
