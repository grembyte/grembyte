"""Process selection dialog widget."""

import ntpath
import os
import re

import psutil

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog, QVBoxLayout,
    QTableWidget, QTableWidgetItem, QLineEdit,
    QHeaderView, QAbstractItemView, QMessageBox,
)

from grembyte.engines.unreal.adapter import bootstrapped_game
from grembyte.ui.widgets.dialog_buttons import dialog_button_row
from grembyte.ui.widgets.table import copy_menu, resizable_columns, set_row_height
from grembyte.ui.localization import load_translations
from grembyte.ui.widgets.icons import theme_qcolor


_PYTHON_NAME = re.compile(r"^python[\d.]*w?$")
# The Linux programs that run Windows programs under Wine/Proton
_WINE_LOADER = re.compile(r"^wine(64)?(-preloader)?$")
# Python options that consume the following argument.
_PYTHON_OPTS_WITH_ARG = {"-c", "-m", "-W", "-X", "--check-hash-based-pycs"}


def _resolve_display(name, exe, cmdline, cwd):
    """Return (name, path) to display: the script of a Python process, the
    Windows program of a Wine process.

    For interpreters, the kernel only knows the interpreter binary; the
    script is merely an argument in the command line.
    """
    if cmdline and _WINE_LOADER.match(os.path.basename(exe)):
        # The kernel's name of a Wine process is that of its main thread,
        # which games rename (Unreal: "GameThread"); its program is the
        # Windows path in the command line.
        program = cmdline[0].strip()
        if program.lower().endswith(".exe"):
            return ntpath.basename(program), program
    if not cmdline or not _PYTHON_NAME.match(os.path.basename(cmdline[0]) or name):
        return name, exe
    args = iter(cmdline[1:])
    for arg in args:
        if arg == "-m":
            module = next(args, "")
            return (f"{name} -m {module}" if module else name), exe
        if arg in _PYTHON_OPTS_WITH_ARG:
            next(args, None)
            continue
        if arg.startswith("-"):
            continue
        script = arg if os.path.isabs(arg) or not cwd else os.path.join(cwd, arg)
        return os.path.basename(arg), os.path.normpath(script)
    return name, exe


class ProcessSelectDialog(QDialog):
    """Lists running processes and lets the user select one."""

    # Dialog behavior is exposed through Qt signals rather than public methods.
    # pylint: disable=too-few-public-methods

    def __init__(self, parent=None, translations=None):
        super().__init__(parent)
        self.translations = translations or load_translations()
        self.setWindowTitle(self.translations["process.dialog_title"])
        self.setMinimumSize(480, 400)
        self.selected_pid = None
        self.selected_name = None
        self.selected_path = None
        self._build_ui()
        self._load_processes()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(self.translations["process.search_placeholder"])
        self.search_input.textChanged.connect(self._filter)
        layout.addWidget(self.search_input)

        # Process list
        self.list = QTableWidget(0, 3)
        # One line per process: a long path is shortened, not broken at a space.
        self.list.setWordWrap(False)
        self.list.setHorizontalHeaderLabels([
            self.translations["process.column_pid"],
            self.translations["process.column_name"],
            self.translations["process.column_path"],
        ])
        self.list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        resizable_columns(self.list)
        copy_menu(self.list, self.translations)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setAlternatingRowColors(True)
        self.list.verticalHeader().setVisible(False)
        set_row_height(self.list)
        self.list.doubleClicked.connect(self._accept)
        layout.addWidget(self.list)

        # Buttons
        layout.addLayout(dialog_button_row(
            self.translations, "process.attach", self._accept, self.reject
        ))

    def _load_processes(self):
        """Loads the process list via psutil."""
        try:
            self._all_procs = []
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "cwd"]):
                try:
                    info = proc.info
                    name, path = _resolve_display(
                        info["name"] or "",
                        info["exe"] or "",
                        info["cmdline"],
                        info["cwd"],
                    )
                    self._all_procs.append((str(info["pid"]), name, path))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self._all_procs.sort(key=lambda x: x[1].lower())
            self._populate(self._all_procs)
        except ImportError:
            QMessageBox.warning(
                self,
                self.translations["process.psutil_missing_title"],
                self.translations["process.psutil_missing_message"],
            )

    def _populate(self, procs):
        self.list.setRowCount(0)
        for pid, name, exe in procs:
            row = self.list.rowCount()
            self.list.insertRow(row)
            pid_item = QTableWidgetItem(pid)
            pid_item.setForeground(theme_qcolor("accent"))
            self.list.setItem(row, 0, pid_item)
            self.list.setItem(row, 1, QTableWidgetItem(name))
            path_item = QTableWidgetItem(exe)
            path_item.setForeground(theme_qcolor("muted"))
            self.list.setItem(row, 2, path_item)

    def _filter(self, text):
        text = text.lower()
        filtered = [
            p for p in self._all_procs
            if text in p[1].lower() or text in p[2].lower()
        ]
        self._populate(filtered)

    def _accept(self):
        row = self.list.currentRow()
        if row >= 0:
            pid, name, path = (self.list.item(row, column).text() for column in range(3))
            # An Unreal game's bootstrap only starts the game; attach to that.
            game = bootstrapped_game(path, [proc[2] for proc in self._all_procs])
            if game is not None:
                pid, name, path = next(proc for proc in self._all_procs if proc[2] == game)
            self.selected_pid = int(pid)
            self.selected_name = name
            self.selected_path = path
            self.accept()
