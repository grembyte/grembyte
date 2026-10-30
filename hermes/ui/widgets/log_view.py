"""The application log in a window, and a dialog for unexpected errors."""

import time
from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QDesktopServices, QFontDatabase, QGuiApplication, QTextCursor  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from hermes.core import activity, logs
from hermes.ui.widgets.icons import set_button_icon


# Lines of the log the window shows at most (the newest).
MAX_LINES = 3000
_PROBLEMS = (" WARNING ", " ERROR ", " CRITICAL ")


def read_log(path, problems_only=False, max_lines=MAX_LINES):
    """The newest lines of the log; with ``problems_only`` only warnings and
    errors (with their tracebacks). None if there is no log file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError:
        return None
    if problems_only:
        kept = []
        keep = False
        for line in lines:
            # A record starts with its date; other lines continue the last one.
            if line[:4].isdigit() and line[4:5] == "-":
                keep = any(level in line[:60] for level in _PROBLEMS)
            if keep:
                kept.append(line)
        lines = kept
    return lines[-max_lines:]


def activity_lines(path, max_lines=MAX_LINES):
    """The newest entries of ``activity.jsonl`` as lines, or None without the file."""
    entries = activity.read_entries(path, max_lines)
    if entries is None:
        return None
    lines = []
    run = None
    for entry in entries:
        if entry.get("run") != run:
            run = entry.get("run")
            lines.append(f"--- {entry.get('t', '')[:10]} run {run} ---")
        lines.append(activity.describe_entry(entry))
    return lines


class LogDialog(QDialog):
    """The newest entries of ``hermes.log`` or of the activity log."""

    def __init__(self, translations, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.path = logs.log_path()
        self.activity_path = activity.activity_path()
        self.setWindowTitle(translations["log.title"])
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem(translations["log.source_log"], "log")
        self.source_combo.addItem(translations["log.source_activity"], "activity")
        self.source_combo.setToolTip(translations["log.source_tooltip"])
        self.source_combo.currentIndexChanged.connect(lambda _index: self.refresh())
        top.addWidget(self.source_combo)
        self.path_label = QLabel()
        self.path_label.setStyleSheet("color: #8b949e;")
        top.addWidget(self.path_label, 1)
        layout.addLayout(top)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.text, 1)
        buttons = QHBoxLayout()
        self.problems_check = QCheckBox(translations["log.problems_only"])
        self.problems_check.toggled.connect(lambda _checked: self.refresh())
        buttons.addWidget(self.problems_check)
        buttons.addStretch()
        refresh = QPushButton(translations["log.refresh"])
        set_button_icon(refresh, "refresh")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        copy = QPushButton(translations["log.copy"])
        set_button_icon(copy, "content_copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        buttons.addWidget(copy)
        folder = QPushButton(translations["log.open_folder"])
        set_button_icon(folder, "folder_open")
        folder.setToolTip(translations["log.open_folder_tooltip"])
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent))))
        buttons.addWidget(folder)
        export = QPushButton(translations["log.export"])
        set_button_icon(export, "upload_file")
        export.setToolTip(translations["log.export_tooltip"])
        export.clicked.connect(self._export)
        buttons.addWidget(export)
        layout.addLayout(buttons)
        self.refresh()

    def showing_activity(self):
        return self.source_combo.currentData() == "activity"

    def refresh(self):
        activity_shown = self.showing_activity()
        path = self.activity_path if activity_shown else self.path
        self.path_label.setText(self.translations["log.path"].format(path=path))
        self.problems_check.setEnabled(not activity_shown)
        lines = (activity_lines(path) if activity_shown
                 else read_log(path, self.problems_check.isChecked()))
        # Switched off in the settings: say so rather than only that the file is missing.
        note = (self.translations["log.activity_off"]
                if activity_shown and not activity.enabled() else None)
        if lines is None:
            missing = self.translations["log.missing"].format(path=path)
            self.text.setPlainText(missing if note is None else f"{note}\n\n{missing}")
            return
        if note is not None:
            lines = [note, ""] + lines
        self.text.setPlainText("\n".join(lines) if lines else self.translations["log.empty"])
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def _export(self):
        name = time.strftime("hermes-report-%Y%m%d-%H%M%S.zip")
        target, _filter = QFileDialog.getSaveFileName(
            self, self.translations["log.export"], str(Path.home() / name),
            self.translations["log.export_filter"])
        if not target:
            return
        try:
            files = activity.export_report(target, self.path.parent)
        except OSError as error:
            QMessageBox.warning(self, self.translations["log.export"],
                                self.translations["log.export_failed"].format(error=error))
            return
        QMessageBox.information(self, self.translations["log.export"],
                                self.translations["log.exported"].format(
                                    path=target, count=len(files)))


class ErrorReporter(QObject):
    """Shows exceptions nothing caught (see ``logs.install_exception_hooks``).

    They may happen in any thread; the dialog opens in the GUI thread. While
    one is shown, further ones are only counted (they are in the log).
    """

    unhandled = pyqtSignal(str, str)

    def __init__(self, translations, open_log, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.open_log = open_log
        self._box = None
        self._more = 0
        self.unhandled.connect(self._show)
        logs.on_unhandled(self.unhandled.emit)

    def _show(self, summary, details):
        if self._box is not None:
            self._more += 1
            self._box.setInformativeText(self.translations["error.more"].format(count=self._more))
            return
        box = QMessageBox(self.parent())
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(self.translations["error.title"])
        box.setText(self.translations["error.text"].format(summary=summary))
        box.setDetailedText(details)
        show_log = box.addButton(self.translations["log.open"],
                                 QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.finished.connect(lambda _result: self._closed(box, show_log))
        self._box = box
        self._more = 0
        box.open()

    def _closed(self, box, show_log):
        self._box = None
        if box.clickedButton() is show_log:
            self.open_log()


class LogSignals(QObject):
    """``logs.subscribe`` as a Qt signal: an error was logged."""

    error_logged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        emit = self.error_logged.emit
        logs.subscribe(emit)
        self.destroyed.connect(lambda _object=None: logs.unsubscribe(emit))
