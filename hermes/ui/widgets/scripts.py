"""The scripts tab: JavaScript that calls game code by time (see hermes.features.scripts)."""

import html
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor, QFontDatabase  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextBrowser,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.features.scripts import MIN_REPEAT_SECONDS, MODES, ScriptSpec, ScriptWorker
from hermes.hooks.syntax import BODY
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.inspection import Inspections
from hermes.ui.widgets.labels import WrappedLabel
from hermes.ui.widgets.syntax_hint import SyntaxHint
from hermes.ui.widgets.table import resizable_columns, set_row_height

STATUS_COLORS = {
    "starting": "#8b949e",
    "active": "#3fb950",
    "running": "#58a6ff",
    "paused": "#d29922",
    "failed": "#f85149",
}
# Lines the log keeps
MAX_LOG_LINES = 2000
# Characters of a result the table shows
MAX_RESULT_TEXT = 120

EXAMPLE = """\
// Avowed: heal the player by 50
const cheats = ctx.find("AlabamaCheatManager");
cheats.call("GiveHealth", 50);
"""


def timing_text(translations, spec):
    """When a script runs, as the table shows it."""
    if spec.mode == "manual":
        return translations["scripts.timing_manual"]
    seconds = f"{spec.seconds:g}"
    return translations[f"scripts.timing_{spec.mode}"].format(seconds=seconds)


class ScriptDialog(QDialog):
    """Create or edit a script: description, when it runs, its code."""

    def __init__(self, translations, parent=None, spec=None):
        super().__init__(parent)
        self.translations = translations
        self.spec = None
        self.setWindowTitle(translations["scripts.dialog_edit" if spec else "scripts.dialog_new"])
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.label_edit = QLineEdit(spec.label if spec else "")
        self.label_edit.setPlaceholderText(translations["scripts.label_placeholder"])
        form.addRow(translations["scripts.label"], self.label_edit)

        timing = QHBoxLayout()
        self.mode_combo = QComboBox()
        for mode in MODES:
            self.mode_combo.addItem(translations[f"scripts.mode_{mode}"], mode)
        self.mode_combo.setCurrentIndex(MODES.index(spec.mode) if spec else MODES.index("repeat"))
        self.seconds_spin = QDoubleSpinBox()
        self.seconds_spin.setRange(0, 24 * 3600)
        self.seconds_spin.setDecimals(1)
        self.seconds_spin.setSuffix(" s")
        self.seconds_spin.setValue(spec.seconds if spec else 60)
        timing.addWidget(self.mode_combo, 1)
        timing.addWidget(self.seconds_spin)
        form.addRow(translations["scripts.when"], timing)
        self.mode_combo.currentIndexChanged.connect(self._update_seconds)
        self._update_seconds()

        self.code_edit = QPlainTextEdit(spec.source if spec else EXAMPLE)
        self.code_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.code_edit.setMinimumHeight(200)
        form.addRow(translations["scripts.code"], self.code_edit)
        self.syntax_hint = SyntaxHint(translations, self.code_edit, BODY)
        form.addRow("", self.syntax_hint)
        layout.addLayout(form)

        help_label = WrappedLabel(translations["scripts.code_help"])
        help_label.setStyleSheet("color:#8b949e;")
        help_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(help_label)
        self.error_label = WrappedLabel("")
        self.error_label.setStyleSheet("color:#f85149;")
        layout.addWidget(self.error_label)
        layout.addStretch()
        layout.addLayout(dialog_button_row(translations, "button.save" if spec
                                           else "scripts.create", self._accept, self.reject))
        self._enabled = spec.enabled if spec else True

    def _update_seconds(self):
        self.seconds_spin.setEnabled(self.mode_combo.currentData() != "manual")

    def _accept(self):
        source = self.code_edit.toPlainText()
        if not source.strip():
            self.error_label.setText(self.translations["scripts.error_empty"])
            return
        problem = self.syntax_hint.problem()
        if problem is not None:
            self.error_label.setText(problem)
            return
        mode = self.mode_combo.currentData()
        seconds = self.seconds_spin.value() if mode != "manual" else 0.0
        if mode == "repeat" and seconds < MIN_REPEAT_SECONDS:
            self.error_label.setText(
                self.translations["scripts.error_interval"].format(seconds=MIN_REPEAT_SECONDS))
            return
        self.spec = ScriptSpec(source, mode, seconds, self._enabled,
                               self.label_edit.text().strip())
        self.accept()


class ScriptPanel(QGroupBox):
    """Table of the scripts of the attached process; owns their ScriptWorker.

    With a ``repository`` scripts are saved per program and started again
    whenever it is attached.
    """

    # pylint: disable=too-many-instance-attributes

    count_changed = pyqtSignal(int)
    # a field of an inspected object to put on the address list (see ObjectInspectorDialog)
    address_requested = pyqtSignal(object)

    _COLUMNS = (
        "scripts.column_label",
        "scripts.column_timing",
        "scripts.column_status",
        "scripts.column_runs",
        "scripts.column_last",
    )

    def __init__(self, translations, parent=None, *, repository=None):
        super().__init__(translations["scripts.title"], parent)
        self.translations = translations
        self._repository = repository
        self._pid = None
        self._process_id = None
        self._worker = None
        # the inspectors of objects scripts returned (with the worker's agent)
        self.inspections = None
        # objects scripts returned, by their number in the log's links
        self._results = []
        # script id -> {spec, saved_id, status, runs, last}
        self._scripts = {}
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        self.btn_new = QPushButton(self.translations["scripts.new"])
        set_button_icon(self.btn_new, "add")
        self.btn_new.clicked.connect(self._new_script)
        self.btn_edit = QPushButton(self.translations["scripts.edit"])
        set_button_icon(self.btn_edit, "edit")
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_run = QPushButton(self.translations["scripts.run"])
        set_button_icon(self.btn_run, "play_circle")
        self.btn_run.setToolTip(self.translations["scripts.run_tooltip"])
        self.btn_run.clicked.connect(self._run_selected)
        self.btn_toggle = QPushButton(self.translations["scripts.pause"])
        set_button_icon(self.btn_toggle, "pause")
        self.btn_toggle.clicked.connect(self._toggle_selected)
        self.btn_remove = QPushButton(self.translations["scripts.remove"])
        self.btn_remove.setObjectName("btn_danger")
        set_button_icon(self.btn_remove, "delete")
        self.btn_remove.clicked.connect(self._remove_selected)
        for button in (self.btn_new, self.btn_edit, self.btn_run, self.btn_toggle):
            controls.addWidget(button)
        self.status_label = QLabel(self.translations["scripts.hint"])
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        controls.addWidget(self.btn_remove)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels([self.translations[key] for key in self._COLUMNS])
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        if header is not None:
            for column in range(len(self._COLUMNS)):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(len(self._COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
            resizable_columns(self.table)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(self._edit_selected)
        splitter.addWidget(self.table)

        self.log_view = QTextBrowser()
        self.log_view.setOpenLinks(False)
        self.log_view.anchorClicked.connect(self._on_link)
        self.log_view.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText(self.translations["scripts.log_placeholder"])
        splitter.addWidget(self.log_view)
        splitter.setSizes([160, 100])
        layout.addWidget(splitter)
        self._update_buttons()

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _row_of(self, script_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == script_id:
                return row
        return None

    def _set_cell(self, script_id, column, text, color=None, tooltip=None):
        row = self._row_of(script_id)
        if row is None:
            return
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, column, item)
        item.setText(text)
        if color is not None:
            item.setForeground(QColor(color))
        item.setToolTip(tooltip or text)

    def _show(self, script_id):
        state = self._scripts[script_id]
        spec = state["spec"]
        self._set_cell(script_id, 0, spec.label or self.translations["scripts.unnamed"],
                       tooltip=spec.source)
        self._set_cell(script_id, 1, timing_text(self.translations, spec))
        self._set_status(script_id, state["status"], state.get("message"))
        self._set_cell(script_id, 3, str(state["runs"]))
        last = state.get("last") or ""
        self._set_cell(script_id, 4, last[:MAX_RESULT_TEXT], tooltip=last)

    def _set_status(self, script_id, status, message=None):
        state = self._scripts.get(script_id)
        if state is None:
            return
        state["status"] = status
        state["message"] = message
        self._set_cell(script_id, 2, self.translations[f"scripts.status_{status}"],
                       STATUS_COLORS[status], tooltip=message)
        self._update_buttons()

    def _update_buttons(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        attached = self._pid is not None
        self.btn_new.setEnabled(attached)
        for button in (self.btn_edit, self.btn_run, self.btn_toggle, self.btn_remove):
            button.setEnabled(state is not None)
        if state is not None:
            self.btn_run.setEnabled(state["status"] not in {"failed", "starting"})
            paused = not state["spec"].enabled
            self.btn_toggle.setText(self.translations[
                "scripts.resume" if paused else "scripts.pause"])
            set_button_icon(self.btn_toggle, "play_arrow" if paused else "pause")

    def _log(self, text, target=None, name=""):
        """Add a line to the log; with ``target`` (an object) a link that inspects it."""
        line = f'<span style="white-space: pre-wrap">{html.escape(text)}</span>'
        if target and self.inspections is not None:
            self._results.append((self.inspections, target, name))
            line += (f'  <a href="inspect:{len(self._results) - 1}">'
                     f'{html.escape(self.translations["scripts.inspect"])}</a>')
        self.log_view.append(line)

    def _on_link(self, url):
        kind, _, number = url.toString().partition(":")
        if kind == "inspect" and number.isdigit() and int(number) < len(self._results):
            inspections, target, name = self._results[int(number)]
            if inspections is self.inspections:  # the game is still attached
                inspections.inspect(target, name=name)

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------
    def set_process(self, pid, process_id=None):
        """Switch to another process; its saved scripts start right away."""
        if pid == self._pid:
            return
        self.shutdown()
        self._pid = pid
        self._process_id = process_id
        self._scripts.clear()
        self.table.setRowCount(0)
        self.status_label.setText(self.translations["scripts.hint"])
        self.count_changed.emit(0)
        self._update_buttons()
        self._restore_saved_scripts()

    def shutdown(self):
        """Stop the worker; the game keeps nothing of the scripts."""
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.requestInterruption()
            worker.wait(3000)

    def _store(self, action, *args, **kwargs):
        """Run a repository call; report failures instead of raising."""
        if self._repository is None:
            return None
        try:
            return action(*args, **kwargs)
        except SQLAlchemyError as error:
            self.status_label.setText(self.translations["scripts.db_failed"].format(message=error))
            return None

    def _restore_saved_scripts(self):
        if self._repository is None or self._process_id is None:
            return
        saved_scripts = self._store(self._repository.list_scripts, self._process_id) or []
        for saved in saved_scripts:
            try:
                spec = ScriptSpec(saved.source, saved.mode, saved.seconds, saved.enabled,
                                  saved.label)
            except ValueError as error:
                self.status_label.setText(self.translations["scripts.restore_failed"].format(
                    label=saved.label or saved.id, message=error))
                continue
            self.add_script(spec, saved_id=saved.id)
        if saved_scripts:
            self.status_label.setText(
                self.translations["scripts.restored"].format(count=len(saved_scripts)))

    def _ensure_worker(self):
        worker = self._worker
        if worker is not None and worker.isRunning():
            return worker
        worker = ScriptWorker(self._pid, self)
        worker.script_added.connect(lambda script_id: self._on_added(worker, script_id))
        worker.script_updated.connect(lambda script_id: self._on_added(worker, script_id))
        worker.script_removed.connect(lambda script_id: self._on_removed(worker, script_id))
        worker.script_failed.connect(
            lambda script_id, message: self._on_failed(worker, script_id, message))
        worker.script_ran.connect(lambda script_id, run: self._on_ran(worker, script_id, run))
        worker.worker_failed.connect(lambda message: self._on_worker_failed(worker, message))
        self.inspections = Inspections(self.translations, worker, self.window(),
                                       time=lambda: datetime.now().strftime("%H:%M:%S"))
        self.inspections.address_requested.connect(self.address_requested.emit)
        self._worker = worker
        worker.start()
        return worker

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------
    def add_script(self, spec, saved_id=None):
        """Start a script (ScriptSpec); saved unless it is a restored one."""
        if self._pid is None:
            return None
        if saved_id is None and self._process_id is not None:
            saved_id = self._store(self._repository.add_script, self._process_id,
                                   label=spec.label, source=spec.source, mode=spec.mode,
                                   seconds=spec.seconds, enabled=spec.enabled)
        script_id = self._ensure_worker().add_script(spec)
        self._scripts[script_id] = {"spec": spec, "saved_id": saved_id, "status": "starting",
                                    "runs": 0, "last": ""}
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, script_id)
        self.table.setItem(row, 0, item)
        self._show(script_id)
        self.table.selectRow(row)
        self.count_changed.emit(len(self._scripts))
        return script_id

    def _new_script(self):
        if self._pid is None:
            return
        dialog = ScriptDialog(self.translations, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.spec is not None:
            self.add_script(dialog.spec)

    def _edit_selected(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        if state is None:
            return
        dialog = ScriptDialog(self.translations, self, state["spec"])
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.spec is None:
            return
        if script_id not in self._scripts:  # the game exited meanwhile
            return
        self._replace_spec(script_id, dialog.spec)

    def _replace_spec(self, script_id, spec):
        state = self._scripts[script_id]
        state["spec"] = spec
        if state["saved_id"] is not None:
            self._store(self._repository.update_script, state["saved_id"], label=spec.label,
                        source=spec.source, mode=spec.mode, seconds=spec.seconds,
                        enabled=spec.enabled)
        if state["status"] == "failed":
            # Its code did not compile: try it as a new script of the worker.
            self._drop_row(script_id)
            self.add_script(spec, saved_id=state["saved_id"])
            return
        self._ensure_worker().update_script(script_id, spec)
        self._show(script_id)

    def _toggle_selected(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        if state is None:
            return
        spec = state["spec"]
        self._replace_spec(script_id, ScriptSpec(spec.source, spec.mode, spec.seconds,
                                                 not spec.enabled, spec.label))

    def _run_selected(self):
        script_id = self._selected_id()
        if script_id in self._scripts and self._worker is not None:
            self._set_status(script_id, "running")
            self._worker.run_now(script_id)

    def _remove_selected(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        if state is None:
            return
        if state["saved_id"] is not None:
            self._store(self._repository.delete_script, state["saved_id"])
        if self._worker is not None and state["status"] != "failed":
            self._worker.remove_script(script_id)
        self._drop_row(script_id)

    def _drop_row(self, script_id):
        row = self._row_of(script_id)
        if row is not None:
            self.table.removeRow(row)
        self._scripts.pop(script_id, None)
        self.count_changed.emit(len(self._scripts))
        self._update_buttons()

    # ------------------------------------------------------------------
    # Worker signals
    # ------------------------------------------------------------------
    def _on_added(self, worker, script_id):
        if worker is not self._worker or script_id not in self._scripts:
            return
        enabled = self._scripts[script_id]["spec"].enabled
        self._set_status(script_id, "active" if enabled else "paused")

    def _on_removed(self, worker, script_id):
        if worker is self._worker:
            self._drop_row(script_id)

    def _on_failed(self, worker, script_id, message):
        if worker is not self._worker or script_id not in self._scripts:
            return
        message = message.splitlines()[0] if message else message
        self._set_status(script_id, "failed", message)
        self.status_label.setText(self.translations["scripts.failed"].format(message=message))

    def _on_ran(self, worker, script_id, run):
        state = self._scripts.get(script_id)
        if worker is not self._worker or state is None:
            return
        state["runs"] = run.get("runs", state["runs"] + 1)
        error = run.get("error")
        label = state["spec"].label or f"#{script_id}"
        time = datetime.fromtimestamp(run.get("time", 0) / 1000).strftime("%H:%M:%S")
        for line in run.get("logs") or []:
            self._log(f"{time} {label}: {line}")
        if error:
            state["last"] = error.splitlines()[0]
            self._log(f"{time} {label}: {self.translations['scripts.log_error']} {error}")
        else:
            state["last"] = run.get("result") or "—"
            if run.get("result"):
                self._log(f"{time} {label}: = {run['result']}", run.get("target"), label)
        self._show(script_id)
        if state["status"] == "running":
            self._set_status(script_id, "active" if state["spec"].enabled else "paused")

    def _on_worker_failed(self, worker, message):
        if worker is not self._worker:
            return
        self._worker = None
        for script_id in list(self._scripts):
            self._set_status(script_id, "failed", message)
        self.status_label.setText(self.translations["scripts.worker_failed"].format(
            message=message.splitlines()[0] if message else message))
