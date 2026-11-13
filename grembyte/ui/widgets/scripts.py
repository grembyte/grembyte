"""The scripts tab: JavaScript that calls game code by time (see grembyte.features.scripts)."""

import dataclasses
import html
import json
from datetime import datetime

from PySide6.QtCore import Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QColor,
    QFontDatabase,
    QKeySequence,
)
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTextBrowser,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from grembyte.core import hotkeys, messages
from grembyte.features.scripts import (
    MIN_REPEAT_SECONDS, MODES, ScriptSpec, ScriptWorker, params_to_json, spec_from_saved)
from grembyte.platform.keyboard import open_reader
from grembyte.hooks.syntax import BODY
from grembyte.ui.widgets.code_editor import CodeEditor
from grembyte.ui.widgets.dialog_buttons import dialog_button_row
from grembyte.ui.widgets.docking import close_dialog
from grembyte.ui.widgets.icons import material_icon, set_button_icon, theme_color
from grembyte.ui.widgets.inspection import Inspections
from grembyte.ui.widgets.labels import DocsHint, WrappedLabel
from grembyte.ui.widgets.script_params import ParamEditor, ScriptRunDialog
from grembyte.ui.widgets.spin_boxes import DoubleSpinBox
from grembyte.ui.widgets.syntax_hint import SyntaxHint
from grembyte.ui.widgets.table import (
    add_copy_actions, add_delete_action, resizable_columns, select_clicked_row, set_row_height)

# Theme colours (see icons.theme_color) of the states
STATUS_COLORS = {
    "starting": "muted",
    "active": "success",
    "running": "accent",
    "paused": "warning",
    "failed": "danger",
    "disconnected": "muted",
}
# Statuses of scripts the worker no longer runs: saving an edit starts them anew
_STOPPED = frozenset({"failed", "disconnected"})
# Lines the log keeps
MAX_LOG_LINES = 2000
# Characters of a result the table shows
MAX_RESULT_TEXT = 120

# The code a new script starts with: an example for the test game HermesQuest
# (see docs/examples/hermesquest-scripts.md).
EXAMPLE = """\
// HermesQuest: heal the player by 10 health
const player = ctx.static("HermesQuest.GameState.Current").Player;
const healed = player.Heal(10);
return `Healed ${healed.toFixed(1)} health`;
"""


def timing_text(translations, spec):
    """When a script runs, as the table shows it."""
    if spec.mode == "manual":
        if spec.hotkey:
            return translations["scripts.timing_hotkey"].format(hotkey=spec.hotkey)
        return translations["scripts.timing_manual"]
    seconds = f"{spec.seconds:g}"
    return translations[f"scripts.timing_{spec.mode}"].format(seconds=seconds)


class ScriptDialog(QDialog):
    """Create or edit a script: description, when it runs, its code and parameters."""

    def __init__(self, translations, parent=None, spec=None):
        super().__init__(parent)
        self.translations = translations
        self.spec = None
        self.setWindowTitle(translations["scripts.dialog_edit" if spec else "scripts.dialog_new"])
        self.resize(700, 600)
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
        self.seconds_spin = DoubleSpinBox()
        self.seconds_spin.setRange(0, 24 * 3600)
        self.seconds_spin.setDecimals(1)
        self.seconds_spin.setSuffix(" s")
        self.seconds_spin.setValue(spec.seconds if spec else 60)
        timing.addWidget(self.mode_combo, 1)
        timing.addWidget(self.seconds_spin)
        form.addRow(translations["scripts.when"], timing)
        self.hotkey_edit = QKeySequenceEdit(QKeySequence(spec.hotkey if spec else ""))
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setClearButtonEnabled(True)
        self.hotkey_edit.setToolTip(translations["scripts.hotkey_tooltip"])
        form.addRow(translations["scripts.hotkey"], self.hotkey_edit)
        self.mode_combo.currentIndexChanged.connect(self._update_seconds)
        self._update_seconds()

        layout.addLayout(form)

        # The code and the parameters take the height the window grows by
        self.tabs = QTabWidget()
        code_page = QWidget()
        code_layout = QVBoxLayout(code_page)
        code_layout.setContentsMargins(0, 6, 0, 0)
        self.code_edit = CodeEditor(spec.source if spec else EXAMPLE)
        self.code_edit.setMinimumHeight(200)
        code_layout.addWidget(self.code_edit, 1)
        self.syntax_hint = SyntaxHint(translations, self.code_edit, BODY)
        code_layout.addWidget(self.syntax_hint)
        help_label = DocsHint(translations, translations["scripts.code_help"],
                              "reference/script-context/")
        code_layout.addWidget(help_label)
        self.tabs.addTab(code_page, translations["scripts.code"])
        self.param_editor = ParamEditor(translations, spec.params if spec else ())
        self.tabs.addTab(self.param_editor, translations["scripts.params"])
        layout.addWidget(self.tabs, 1)

        self.error_label = WrappedLabel("")
        self.error_label.setStyleSheet(f"color:{theme_color('danger')};")
        layout.addWidget(self.error_label)
        layout.addLayout(dialog_button_row(translations, "button.save" if spec
                                           else "scripts.create", self._accept, self.reject))
        self._enabled = spec.enabled if spec else True

    def _update_seconds(self):
        manual = self.mode_combo.currentData() == "manual"
        self.seconds_spin.setEnabled(not manual)
        self.hotkey_edit.setEnabled(manual)

    def hotkey(self):
        """The hotkey typed in, as Qt writes it ("" for none)."""
        return self.hotkey_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText)

    def _accept(self):
        source = self.code_edit.toPlainText()
        if not source.strip():
            self.error_label.setText(self.translations["scripts.error_empty"])
            return
        problem = self.syntax_hint.problem()
        if problem is not None:
            self.tabs.setCurrentIndex(0)
            self.error_label.setText(problem)
            return
        try:
            params = self.param_editor.params()
        except ValueError as error:
            self.tabs.setCurrentIndex(1)
            self.error_label.setText(messages.describe(error))
            return
        mode = self.mode_combo.currentData()
        seconds = self.seconds_spin.value() if mode != "manual" else 0.0
        if mode == "repeat" and seconds < MIN_REPEAT_SECONDS:
            self.error_label.setText(
                self.translations["scripts.error_interval"].format(seconds=MIN_REPEAT_SECONDS))
            return
        try:
            hotkey = hotkeys.normalized(self.hotkey()) if mode == "manual" else ""
        except ValueError as error:
            self.error_label.setText(messages.describe(error))
            return
        try:
            self.spec = ScriptSpec(source, mode, seconds, self._enabled,
                                   self.label_edit.text().strip(), params, hotkey)
        except ValueError as error:
            self.tabs.setCurrentIndex(1)
            self.error_label.setText(messages.describe(error))
            return
        self.accept()


class ScriptPanel(QGroupBox):
    """Table of the scripts of the attached process; owns their ScriptWorker.

    With a ``repository`` scripts are saved per program and started again
    whenever it is attached.
    """

    # pylint: disable=too-many-instance-attributes

    count_changed = Signal(int)
    # a field of an inspected object to put on the address list (see ObjectInspectorDialog)
    address_requested = Signal(object)

    _COLUMNS = (
        "scripts.column_label",
        "scripts.column_timing",
        "scripts.column_status",
        "scripts.column_runs",
        "scripts.column_last",
    )

    def __init__(self, translations, parent=None, *, repository=None, language="en"):
        super().__init__(translations["scripts.title"], parent)
        self.translations = translations
        self.language = language
        self._repository = repository
        self._pid = None
        self._process_id = None
        self._worker = None
        # runs manual scripts by their hotkeys while the game has the focus
        self._hotkeys = None
        # the inspectors of objects scripts returned (with the worker's agent)
        self.inspections = None
        # objects scripts returned, by their number in the log's links
        self._results = []
        # script id -> {spec, saved_id, status, runs, last, last_args, options}
        # (last_args: the values typed in for its last run; options: its
        # choices' options by parameter name, while the worker runs)
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
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        splitter.addWidget(self.table)

        self.log_view = QTextBrowser()
        self.log_view.setOpenLinks(False)
        self.log_view.anchorClicked.connect(self._on_link)
        self.log_view.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText(self.translations["scripts.log_placeholder"])
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel(self.translations["scripts.log"]))
        log_bar.addStretch()
        clear_button = QPushButton(self.translations["scripts.log_clear"])
        set_button_icon(clear_button, "clear_all")
        clear_button.clicked.connect(self._clear_log)
        log_bar.addWidget(clear_button)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_panel)
        splitter.setSizes([160, 130])
        layout.addWidget(splitter)
        self._update_buttons()

    def _show_context_menu(self, position):
        """The buttons' actions for the script under the pointer, and copying."""
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        select_clicked_row(self.table, row)
        menu = QMenu(self)
        state = self._scripts.get(self._selected_id())
        paused = state is not None and (not state["spec"].enabled
                                        or state["status"] == "disconnected")
        for button, icon in ((self.btn_edit, "edit"), (self.btn_run, "play_circle"),
                             (self.btn_toggle, "play_arrow" if paused else "pause")):
            menu.addAction(material_icon(icon), button.text(), button.click).setEnabled(
                button.isEnabled())
        menu.addAction(material_icon("content_copy"), self.translations["scripts.copy"],
                   self._copy_selected).setEnabled(state is not None)
        add_delete_action(menu, self.table, self.translations, self._remove_selected)
        add_copy_actions(menu, self.table, position, self.translations)
        menu.exec(self.table.viewport().mapToGlobal(position))

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
                       theme_color(STATUS_COLORS[status]), tooltip=message)
        self._update_buttons()

    def _update_buttons(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        attached = self._pid is not None
        self.btn_new.setEnabled(attached)
        for button in (self.btn_edit, self.btn_run, self.btn_toggle, self.btn_remove):
            button.setEnabled(state is not None)
        if state is not None:
            self.btn_run.setEnabled(state["status"] not in _STOPPED | {"starting"})
            # A disconnected script is resumed: started anew, running
            paused = not state["spec"].enabled or state["status"] == "disconnected"
            self.btn_toggle.setText(self.translations[
                "scripts.resume" if paused else "scripts.pause"])
            set_button_icon(self.btn_toggle, "play_arrow" if paused else "pause")

    def _clear_log(self):
        """Empty the log; the objects its links pointed to go with it."""
        self.log_view.clear()
        self._results.clear()

    def _log(self, text, target=None, name=""):
        """Add a line to the log; with ``target`` (an object) a link that inspects it."""
        line = f'<span style="white-space: pre-wrap">{html.escape(text)}</span>'
        if target and self.inspections is not None:
            targets = target if isinstance(target, (list, tuple)) else [target]
            for inspect_target in targets:
                self._results.append((self.inspections, inspect_target, name))
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
        self._update_hotkeys()

    def shutdown(self):
        """Stop the worker; the game keeps nothing of the scripts. The object
        inspectors opened from its results close: their objects are gone."""
        worker = self._worker
        self._worker = None
        if self._hotkeys is not None:
            self._hotkeys.close()
            self._hotkeys = None
        self._close_inspections()
        if worker is not None:
            worker.requestInterruption()
            worker.wait(3000)

    def _close_inspections(self):
        inspections = self.inspections
        self.inspections = None
        if inspections is not None:
            for inspector in list(inspections.inspectors):
                close_dialog(inspector)
            inspections.deleteLater()

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
                spec = spec_from_saved(saved)
                last_args = json.loads(saved.last_args) if saved.last_args else {}
            except (ValueError, KeyError, TypeError) as error:
                self.status_label.setText(self.translations["scripts.restore_failed"].format(
                    label=saved.label or saved.id, message=messages.describe(error)))
                continue
            self.add_script(spec, saved_id=saved.id, last_args=last_args)
        if saved_scripts:
            self.status_label.setText(
                self.translations["scripts.restored"].format(count=len(saved_scripts)))

    def _ensure_worker(self):
        worker = self._worker
        if worker is not None and worker.isRunning():
            return worker
        worker = ScriptWorker(self._pid, self, self.language)
        worker.script_added.connect(lambda script_id: self._on_added(worker, script_id))
        worker.script_updated.connect(lambda script_id: self._on_added(worker, script_id))
        worker.script_removed.connect(lambda script_id: self._on_removed(worker, script_id))
        worker.script_failed.connect(
            lambda script_id, message: self._on_failed(worker, script_id, message))
        worker.script_ran.connect(lambda script_id, run: self._on_ran(worker, script_id, run))
        worker.worker_failed.connect(lambda message: self._on_worker_failed(worker, message))
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self.inspections = Inspections(self.translations, worker, self.window(),
                                       time=lambda: datetime.now().strftime("%H:%M:%S"))
        self.inspections.address_requested.connect(self.address_requested.emit)
        self._worker = worker
        worker.start()
        return worker

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------
    def add_script(self, spec, saved_id=None, last_args=None):
        """Start a script (ScriptSpec); saved unless it is a restored one."""
        if self._pid is None:
            return None
        if saved_id is None and self._process_id is not None:
            saved_id = self._store(self._repository.add_script, self._process_id,
                                   label=spec.label, source=spec.source, mode=spec.mode,
                                   seconds=spec.seconds, enabled=spec.enabled,
                                   params=params_to_json(spec.params), hotkey=spec.hotkey)
        script_id = self._ensure_worker().add_script(spec)
        self._scripts[script_id] = {"spec": spec, "saved_id": saved_id, "status": "starting",
                                    "runs": 0, "last": "", "last_args": last_args or {},
                                    "options": {}}
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, script_id)
        self.table.setItem(row, 0, item)
        self._show(script_id)
        self.table.selectRow(row)
        self.count_changed.emit(len(self._scripts))
        self._update_hotkeys()
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

    def _copy_selected(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        if state is None:
            return
        label = state["spec"].label or self.translations["scripts.unnamed"]
        spec = dataclasses.replace(
            state["spec"], label=f"{label} {self.translations['scripts.copy_suffix']}"
        )
        self.add_script(spec)

    def _replace_spec(self, script_id, spec):
        state = self._scripts[script_id]
        if state["spec"].params != spec.params:
            state["options"] = {}  # their code may have changed
        state["spec"] = spec
        if state["saved_id"] is not None:
            self._store(self._repository.update_script, state["saved_id"], label=spec.label,
                        source=spec.source, mode=spec.mode, seconds=spec.seconds,
                        enabled=spec.enabled, params=params_to_json(spec.params) or "",
                        hotkey=spec.hotkey)
        if state["status"] in _STOPPED:
            # Its code did not compile, or its session ended: try it as a new
            # script of the (new) worker.
            self._drop_row(script_id)
            self.add_script(spec, saved_id=state["saved_id"], last_args=state["last_args"])
            return
        self._ensure_worker().update_script(script_id, spec)
        self._show(script_id)
        self._update_hotkeys()

    def _toggle_selected(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        if state is None:
            return
        spec = state["spec"]
        enabled = True if state["status"] == "disconnected" else not spec.enabled
        self._replace_spec(script_id, dataclasses.replace(spec, enabled=enabled))

    def _run_selected(self):
        script_id = self._selected_id()
        state = self._scripts.get(script_id)
        if state is None or self._worker is None:
            return
        args = None
        if state["spec"].params:
            # Ask for its parameters; the choices' options come from the game
            dialog = ScriptRunDialog(self.translations, state["spec"], worker=self._worker,
                                     script_id=script_id, options_cache=state["options"],
                                     last=state["last_args"], parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.args is None:
                return
            if self._scripts.get(script_id) is not state or self._worker is None:
                return  # the game exited meanwhile
            args = dialog.args
            state["last_args"] = args
            if state["saved_id"] is not None:
                self._store(self._repository.update_script, state["saved_id"],
                            last_args=json.dumps(args))
        self._set_status(script_id, "running")
        self._worker.run_now(script_id, args)

    def _remove_selected(self):
        self.remove_script(self._selected_id())

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------
    def _hotkey_scripts(self, hotkey=None):
        """The ids of the active manual scripts with a hotkey (``hotkey`` only)."""
        return [script_id for script_id, state in self._scripts.items()
                if state["spec"].hotkey and state["spec"].enabled
                and state["status"] not in _STOPPED
                and (hotkey is None or state["spec"].hotkey == hotkey)]

    def _update_hotkeys(self):
        """Listen for the hotkeys of the scripts, or stop when none has one."""
        wanted = {self._scripts[script_id]["spec"].hotkey
                  for script_id in self._hotkey_scripts()}
        if self._pid is None or (not wanted and self._hotkeys is None):
            return
        if self._hotkeys is None:
            try:
                reader = open_reader()
            except OSError as error:
                reader = None
                self.status_label.setText(
                    self.translations["scripts.hotkeys_unavailable"].format(message=error))
            self._hotkeys = hotkeys.HotkeyListener({self._pid}, reader, self)
            self._hotkeys.triggered.connect(self._on_hotkey)
        self._hotkeys.set_hotkeys(sorted(wanted))

    def _on_hotkey(self, hotkey):
        if self._worker is None:
            return
        for script_id in self._hotkey_scripts(hotkey):
            if self._scripts[script_id]["status"] == "starting":
                continue
            label = self._scripts[script_id]["spec"].label or f"#{script_id}"
            self._log(f"{datetime.now().strftime('%H:%M:%S')} " + self.translations[
                "scripts.hotkey_pressed"].format(hotkey=hotkey, label=label))
            self._set_status(script_id, "running")
            self._worker.run_now(script_id)

    # ------------------------------------------------------------------
    # For other windows (e.g. the GAS attributes window)
    # ------------------------------------------------------------------
    def worker(self):
        """The script worker of the attached game, started if needed; None if detached."""
        if self._pid is None:
            return None
        return self._ensure_worker()

    def scripts_labelled(self, prefix):
        """``{script id: ScriptSpec}`` of the scripts whose label starts with ``prefix``."""
        return {script_id: state["spec"] for script_id, state in self._scripts.items()
                if state["spec"].label.startswith(prefix)}

    def put_script(self, spec, prefix):
        """Replace the script whose label starts with ``prefix`` by ``spec``,
        or add ``spec`` if there is none; its id (None when detached)."""
        for script_id in self.scripts_labelled(prefix):
            self._replace_spec(script_id, spec)
            # A replaced stopped script was dropped and added anew
            return next(iter(self.scripts_labelled(prefix)), None)
        return self.add_script(spec)

    def remove_script(self, script_id):
        """Stop and delete a script, also from the saved ones."""
        state = self._scripts.get(script_id)
        if state is None:
            return
        if state["saved_id"] is not None:
            self._store(self._repository.delete_script, state["saved_id"])
        if self._worker is not None and state["status"] != "failed":
            self._worker.remove_script(script_id)
        self._drop_row(script_id)
        self._update_hotkeys()

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
        error = messages.describe(run["error"]) if run.get("error") else None
        label = state["spec"].label or f"#{script_id}"
        time = datetime.fromtimestamp(run.get("time", 0) / 1000).strftime("%H:%M:%S")
        thread = run.get("gameThread")
        if thread:
            key = "scripts.native_thread" if thread.get("main") else "scripts.native_thread_other"
            self._log(f"{time} " + self.translations[key].format(
                name=thread.get("name", "?"), thread=thread.get("thread", "?")))
        for entry in run.get("logs") or []:
            if isinstance(entry, dict):
                self._log(f"{time} {label}: {entry.get('text', '')}",
                          entry.get("targets"), label)
            else:
                self._log(f"{time} {label}: {entry}")
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

    def _on_worker_finished(self, worker):
        """The worker ended (e.g. its session was ended in the sessions
        window): its scripts no longer run."""
        # Not our child any more: deleting the worker releases its slots, and
        # with them maybe the last reference to this panel; a panel without
        # a parent would then delete the worker a second time (PySide6).
        worker.setParent(None)
        worker.deleteLater()
        if worker is not self._worker:
            return
        self._worker = None
        tooltip = self.translations["scripts.disconnected_tooltip"]
        for script_id, state in self._scripts.items():
            if state["status"] != "failed":
                self._set_status(script_id, "disconnected", tooltip)
        if self._scripts:
            self.status_label.setText(tooltip)
