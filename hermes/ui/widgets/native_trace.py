"""Window of the native trace: which functions of the game's code a scenario runs."""

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from hermes.platform import is_wine_process
from hermes.features.favorites import NATIVE_TRACE
from hermes.features.native_trace import NativeTraceWorker, apply_names
from hermes.features.trace import analyze, recording_calls
from hermes.ui.widgets.favorites import FAVORITE_COLUMN
from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.trace import (
    _R_BASELINE,
    _R_CALLS,
    _R_METHOD,
    _R_ORDER,
    _R_RATE,
    _R_VERDICT,
    _S_CALLER,
    _S_METHOD,
    _S_THREAD,
    _S_TIME,
    RowFilterBar,
    TraceRowModel,
    TraceSequenceModel,
    trace_table,
)


# States of the window
_LOADING, _IDLE, _PREPARING, _READY, _RECORDING = range(5)


class NativeRowModel(TraceRowModel):
    """The functions the scenario called (TraceRows of NativeFunctions)."""

    def __init__(self, translations, parent=None):
        super().__init__(translations, parent)
        self.columns = tuple("native_trace.column_function" if column == "trace.column_method"
                             else column for column in self.columns)

    def method_name(self, index):
        return self.methods[index].label


class NativeSequenceModel(TraceSequenceModel):
    """The first calls of the scenario, the call instruction as the caller."""

    def __init__(self, translations, parent=None):
        super().__init__(translations, parent)
        self.columns = tuple("native_trace.column_function" if column == "trace.column_method"
                             else column for column in self.columns)

    def method_name(self, index):
        return self.methods[index].label


class NativeTraceDialog(QDialog):
    """Follow threads of the game with Stalker, record a baseline and a scenario, compare.

    Not modal. The worker and its Frida session live as long as the window;
    closing it stops following the threads.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # address (int) to show in the memory viewer
    memory_requested = pyqtSignal(object)
    # NativeFunction whose calls to monitor
    monitor_requested = pyqtSignal(object)

    def __init__(self, translations, pid, parent=None, favorites=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.wine = is_wine_process(pid)
        self._favorites = favorites
        self.functions = []
        self.baseline = None
        self.scenario = None
        self.rows = []
        self.followed_modules = set()
        self._state = _LOADING
        self._recording = None
        self._selected = None
        self._positions = {}
        self.setWindowTitle(translations["native_trace.title"])
        self.resize(1100, 760)
        self._build_ui()
        self._worker = NativeTraceWorker(pid)
        self._worker.inventory_found.connect(self._on_inventory)
        self._worker.started.connect(self._on_started)
        self._worker.began.connect(self._on_began)
        self._worker.live.connect(self._on_live)
        self._worker.recorded.connect(self._on_recorded)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.command_failed.connect(self._on_command_failed)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self._worker.start()
        self._worker.inventory()
        self.status_label.setText(translations["native_trace.loading"])
        self._set_state(_LOADING)

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        hint = QLabel(t["native_trace.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)
        if self.wine:
            warning = QLabel(t["native_trace.wine_warning"])
            warning.setWordWrap(True)
            layout.addWidget(warning)

        scope = QHBoxLayout()
        threads = QVBoxLayout()
        threads.addWidget(QLabel(t["native_trace.threads"]))
        self.thread_list = QListWidget()
        self.thread_list.setToolTip(t["native_trace.threads_tooltip"])
        threads.addWidget(self.thread_list)
        scope.addLayout(threads, 1)
        modules = QVBoxLayout()
        modules.addWidget(QLabel(t["native_trace.modules"]))
        self.module_list = QListWidget()
        self.module_list.setToolTip(t["native_trace.modules_tooltip"])
        modules.addWidget(self.module_list)
        scope.addLayout(modules, 1)
        scope_widget = QWidget()
        scope_widget.setLayout(scope)
        scope_widget.setMaximumHeight(180)
        layout.addWidget(scope_widget)

        steps = QHBoxLayout()
        self.trace_button = QPushButton(t["native_trace.start"])
        self.trace_button.setObjectName("btn_primary")
        self.trace_button.clicked.connect(self._toggle_trace)
        steps.addWidget(self.trace_button)
        self.baseline_button = QPushButton(t["trace.baseline"])
        self.baseline_button.setToolTip(t["trace.baseline_tooltip"])
        self.baseline_button.setCheckable(True)
        self.baseline_button.clicked.connect(lambda: self._toggle_recording("baseline"))
        steps.addWidget(self.baseline_button)
        self.scenario_button = QPushButton(t["trace.scenario"])
        self.scenario_button.setToolTip(t["trace.scenario_tooltip"])
        self.scenario_button.setCheckable(True)
        self.scenario_button.clicked.connect(lambda: self._toggle_recording("scenario"))
        steps.addWidget(self.scenario_button)
        self.recording_label = QLabel("")
        steps.addWidget(self.recording_label, 1)
        layout.addLayout(steps)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        rows_bar = QHBoxLayout()
        rows_bar.addWidget(QLabel(t["native_trace.functions"]))
        rows_bar.addStretch()
        self.game_code_check = QCheckBox(t["native_trace.game_code_only"])
        self.game_code_check.setToolTip(t["native_trace.game_code_tooltip"])
        self.game_code_check.setChecked(True)
        self.game_code_check.toggled.connect(lambda _checked: self._show_results())
        rows_bar.addWidget(self.game_code_check)
        self.notable_check = QCheckBox(t["native_trace.notable_only"])
        self.notable_check.setToolTip(t["native_trace.notable_tooltip"])
        self.notable_check.setChecked(True)
        self.notable_check.toggled.connect(lambda _checked: self._show_rows())
        rows_bar.addWidget(self.notable_check)
        top_layout.addLayout(rows_bar)
        self.row_filter = RowFilterBar(t, self)
        self.row_filter.changed.connect(self._show_rows)
        top_layout.addWidget(self.row_filter)
        self.row_model = NativeRowModel(t, self)
        self.rows_view = self._table(self.row_model, _R_METHOD, sortable=True)
        header = self.rows_view.horizontalHeader()
        header.resizeSection(_R_ORDER, 60)
        header.resizeSection(_R_CALLS, 80)
        header.resizeSection(_R_RATE, 90)
        header.resizeSection(_R_BASELINE, 130)
        header.resizeSection(_R_VERDICT, 170)
        self.rows_view.selectionModel().currentRowChanged.connect(self._on_row_selected)
        top_layout.addWidget(self.rows_view, 1)
        splitter.addWidget(top)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(QLabel(t["native_trace.sequence"]))
        self.sequence_model = NativeSequenceModel(t, self)
        self.sequence_view = self._table(self.sequence_model, _S_METHOD)
        sequence_header = self.sequence_view.horizontalHeader()
        sequence_header.resizeSection(_S_TIME, 110)
        sequence_header.resizeSection(_S_THREAD, 80)
        sequence_header.resizeSection(_S_CALLER, 260)
        self.sequence_view.selectionModel().currentRowChanged.connect(self._on_call_selected)
        bottom_layout.addWidget(self.sequence_view, 1)
        splitter.addWidget(bottom)
        splitter.setSizes([330, 230])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.selection_label = SelectableText()
        actions.addWidget(self.selection_label, 1)
        self.monitor_button = QPushButton(t["native_trace.monitor"])
        self.monitor_button.setObjectName("btn_primary")
        self.monitor_button.setToolTip(t["native_trace.monitor_tooltip"])
        self.monitor_button.clicked.connect(self._request_monitor)
        actions.addWidget(self.monitor_button)
        self.memory_button = QPushButton(t["native_trace.show_memory"])
        self.memory_button.setToolTip(t["native_trace.show_memory_tooltip"])
        self.memory_button.clicked.connect(self._request_memory)
        actions.addWidget(self.memory_button)
        self.copy_button = QPushButton(t["native_trace.copy_address"])
        self.copy_button.clicked.connect(self._copy_address)
        actions.addWidget(self.copy_button)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._show_selection()

    def _table(self, model, stretch_column, sortable=False):
        view = trace_table(model, stretch_column, sortable, self._favorites, NATIVE_TRACE,
                           self.translations)
        view.doubleClicked.connect(
            lambda index: index.column() != FAVORITE_COLUMN and self._request_memory())
        return view

    def _set_state(self, state):
        self._state = state
        tracing = state in (_READY, _RECORDING)
        self.thread_list.setEnabled(state == _IDLE)
        self.module_list.setEnabled(state == _IDLE)
        self.trace_button.setEnabled(state in (_IDLE, _READY))
        self.trace_button.setText(self.translations[
            "native_trace.stop" if tracing else "native_trace.start"])
        for phase, button in (("baseline", self.baseline_button),
                              ("scenario", self.scenario_button)):
            active = state == _RECORDING and self._recording == phase
            button.setEnabled(state == _READY or active)
            button.setChecked(active)
            button.setText(self.translations[f"trace.{phase}_stop" if active else f"trace.{phase}"])
        if state != _RECORDING:
            self.recording_label.setText("")

    # --- Choosing threads and modules -------------------------------------------
    def _on_inventory(self, threads, modules, _platform):
        t = self.translations
        self.thread_list.clear()
        for thread in threads:
            text = t["native_trace.thread_entry"].format(name=thread.name or "—", id=thread.id)
            if thread.load is not None:
                text += "  " + t["native_trace.thread_load"].format(percent=thread.load * 100)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, thread.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # Idle threads wait somewhere, maybe for long: a followed thread
            # blocked in a call delays the end of the trace until it returns.
            busy = thread.active is not False
            item.setCheckState(Qt.CheckState.Checked if busy else Qt.CheckState.Unchecked)
            self.thread_list.addItem(item)
        self.module_list.clear()
        for module in sorted(modules, key=lambda module: (not module.game, module.name.lower())):
            item = QListWidgetItem(module.name)
            item.setToolTip(module.path)
            item.setData(Qt.ItemDataRole.UserRole, module.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if module.game else Qt.CheckState.Unchecked)
            self.module_list.addItem(item)
        self.status_label.setText(t["native_trace.inventory"].format(
            threads=len(self._checked(self.thread_list)), total=len(threads),
            modules=sum(module.game for module in modules)))
        self._set_state(_IDLE)

    @staticmethod
    def _checked(widget):
        return [widget.item(row).data(Qt.ItemDataRole.UserRole) for row in range(widget.count())
                if widget.item(row).checkState() == Qt.CheckState.Checked]

    # --- Trace -----------------------------------------------------------------
    def _toggle_trace(self):
        if self._state == _IDLE:
            threads = self._checked(self.thread_list)
            modules = self._checked(self.module_list)
            if not threads or not modules:
                self.status_label.setText(self.translations["native_trace.choose"])
                return
            self.followed_modules = set(modules)
            self._set_state(_PREPARING)
            self.status_label.setText(self.translations["native_trace.preparing"])
            self._worker.trace(threads, modules)
        elif self._state == _READY:
            self._set_state(_PREPARING)
            self.status_label.setText(self.translations["native_trace.stopping"])
            self._worker.stop()

    def _on_started(self, info):
        self.functions = []
        self.baseline = None
        self.scenario = None
        self._show_results()
        self._set_state(_READY)
        self.status_label.setText(self.translations["native_trace.started"].format(
            threads=info.get("threads", 0), modules=info.get("modules", 0),
            failed=info.get("failed", 0)))

    def _on_stopped(self):
        self._set_state(_IDLE)
        self.status_label.setText(self.translations["native_trace.stopped"])

    def _on_command_failed(self, action, message):
        # A failed start leaves no trace; other commands keep it.
        self._set_state(_IDLE if action in ("start", "inventory", "stop") else _READY)
        self.status_label.setText(self.translations["trace.failed"].format(message=message))

    def _on_worker_failed(self, message):
        self._set_state(_IDLE)
        self.trace_button.setEnabled(False)
        self.status_label.setText(self.translations["native_trace.worker_failed"].format(
            message=message))

    # --- Recording -------------------------------------------------------------
    def _toggle_recording(self, phase):
        if self._state == _READY:
            self._recording = phase
            self._set_state(_RECORDING)
            self.recording_label.setText(self.translations["trace.recording_starting"])
            self._worker.begin(phase)
        elif self._state == _RECORDING and self._recording == phase:
            self.baseline_button.setEnabled(False)
            self.scenario_button.setEnabled(False)
            self._worker.finish()
        else:
            self._set_state(self._state)  # undo the click's check mark

    def _on_began(self, phase):
        self.recording_label.setText(self.translations[f"trace.recording_{phase}"])

    def _on_live(self, live):
        if self._state == _RECORDING:
            self.recording_label.setText(self.translations["native_trace.live"].format(
                phase=self.translations[f"trace.phase_{live.get('phase', '')}"],
                calls=live.get("calls", 0), functions=live.get("methods", 0)))

    def _on_recorded(self, result):
        self._recording = None
        self._set_state(_READY)
        apply_names(self.functions, result)
        seconds = result.get("duration", 0) / 1000
        if result.get("phase") == "baseline":
            self.baseline = result
            self.status_label.setText(self.translations["native_trace.baseline_done"].format(
                functions=len(result.get("counts", {})), seconds=seconds)
                + self._lost_text(result))
            if self.scenario is not None:
                self._show_results()  # compare the scenario with the new baseline
            return
        self.scenario = result
        self._show_results()
        text = self.translations["native_trace.scenario_done"].format(
            functions=len(self.rows), notable=sum(row.notable for row in self.rows),
            calls=len(self.sequence_model.items), seconds=seconds)
        if result.get("dropped"):
            text += "  ·  " + self.translations["trace.dropped"].format(count=result["dropped"])
        self.status_label.setText(text + self._lost_text(result))

    def _lost_text(self, result):
        """A warning when the table of functions was full, or ''."""
        if not result.get("lost"):
            return ""
        return "\n" + self.translations["native_trace.lost"].format(count=result["lost"])

    # --- Results -----------------------------------------------------------------
    def _in_game_code(self, number):
        # Code outside modules (generated at run time) counts as the game's.
        module = self.functions[number].module
        return not module or module in self.followed_modules

    def _show_results(self):
        rows = analyze(self.scenario, self.baseline) if self.scenario else []
        calls = recording_calls(self.scenario) if self.scenario else []
        if self.game_code_check.isChecked():
            rows = [row for row in rows if self._in_game_code(row.method)]
            calls = [call for call in calls if self._in_game_code(call.method)]
        self.rows = rows
        # The sequence holds the first call per thread: where the row starts
        positions = {}
        for position, call in enumerate(calls):
            positions.setdefault(call.method, position)
        self._positions = positions
        self.sequence_model.set_items(self.functions, calls)
        self._show_rows()

    def _show_rows(self):
        notable_only = self.notable_check.isChecked()
        rows = [row for row in self.rows if (row.notable or not notable_only)
                and self.row_filter.accepts(row, self.functions[row.method].signature)]
        self.row_model.set_items(self.functions, rows)
        self._selected = None
        self._show_selection()

    def _on_row_selected(self, current, _previous=None):
        row = self.row_model.item_at(current.row()) if current.isValid() else None
        if row is None:
            return
        self._selected = row.method
        self._show_selection()
        position = self._positions.get(row.method)
        if position is not None:
            index = self.sequence_model.index(position, 0)
            self.sequence_view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _on_call_selected(self, current, _previous=None):
        call = self.sequence_model.item_at(current.row()) if current.isValid() else None
        if call is not None:
            self._selected = call.method
            self._show_selection()

    def selected_function(self):
        """The NativeFunction chosen in either table, or None."""
        return self.functions[self._selected] if self._selected is not None else None

    def _show_selection(self):
        function = self.selected_function()
        self.monitor_button.setEnabled(function is not None)
        self.memory_button.setEnabled(function is not None)
        self.copy_button.setEnabled(function is not None)
        self.selection_label.setText(function.signature if function is not None else "")

    def _request_monitor(self):
        function = self.selected_function()
        if function is not None:
            self.monitor_requested.emit(function)

    def _request_memory(self):
        function = self.selected_function()
        if function is not None:
            self.memory_requested.emit(function.address)

    def _copy_address(self):
        function = self.selected_function()
        if function is not None:
            QGuiApplication.clipboard().setText(f"0x{function.address:x}")

    def closeEvent(self, event):  # pylint: disable=invalid-name
        # Stalker lets go of the threads only when they leave its code; do
        # not block the window for that.
        worker = self._worker
        worker.requestInterruption()
        if worker.isRunning():
            _closing_workers.add(worker)
            worker.finished.connect(lambda: _closing_workers.discard(worker))
        super().closeEvent(event)


# Workers of closed windows, kept until they have let go of the threads.
_closing_workers = set()


def wait_for_closing_workers(timeout_ms):
    """Wait for the workers of closed windows; True if all have finished.

    A QThread still running when the application exits aborts it.
    """
    return all(worker.wait(timeout_ms) for worker in list(_closing_workers))
