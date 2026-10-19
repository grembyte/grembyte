"""Window of the Mono trace: which C# methods a scenario in the game runs."""

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from hermes.core.process import is_wine_process
from hermes.engines.registry import resolve_engine
from hermes.features.trace import (
    MORE,
    NEW,
    TraceWorker,
    analyze,
    calls_filter,
    recording_calls,
)
from hermes.ui.widgets.method_monitor import short_name
from hermes.ui.widgets.trace_filters import TraceFilterPanel


# Above this many methods the trace asks first: Mono compiles them all.
LARGE_TRACE = 5000
# Under Wine/Proton no more than this: compiling and hooking tens of
# thousands of methods there froze or crashed the game (Mono's garbage
# collector waiting in SuspendThread; Frida's Windows gadget).
WINE_TRACE_LIMIT = 5000

_R_ORDER, _R_METHOD, _R_CALLS, _R_RATE, _R_BASELINE, _R_VERDICT = range(6)
_ROW_COLUMNS = (
    "trace.column_order", "trace.column_method", "trace.column_calls",
    "trace.column_rate", "trace.column_baseline", "trace.column_verdict",
)
_S_TIME, _S_THREAD, _S_METHOD, _S_CALLER = range(4)
_SEQUENCE_COLUMNS = (
    "trace.column_time", "trace.column_thread", "trace.column_method", "trace.column_caller",
)

# States of the window
_IDLE, _PREPARING, _READY, _RECORDING = range(4)


class _TableModel(QAbstractTableModel):
    """Read-only rows with translated column headers."""

    def __init__(self, translations, columns, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.columns = columns
        self.methods = []
        self.items = []

    def rowCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.translations[self.columns[section]]
        return None

    def set_items(self, methods, items):
        self.beginResetModel()
        self.methods = methods
        self.items = items
        self.endResetModel()

    def item_at(self, row):
        return self.items[row] if 0 <= row < len(self.items) else None

    def method_name(self, index):
        return short_name(self.methods[index].signature)

    def signature(self, index):
        return self.methods[index].signature


class TraceRowModel(_TableModel):
    """The methods the scenario called (TraceRows).

    Sorts by a column when the view asks; column -1 is the order of ``analyze``.
    """

    def __init__(self, translations, parent=None):
        super().__init__(translations, _ROW_COLUMNS, parent)
        self._unsorted = []
        self._sort_column = -1
        self._sort_order = Qt.SortOrder.AscendingOrder

    def set_items(self, methods, items):
        self._unsorted = list(items)
        self.methods = methods  # the method names sort by them
        super().set_items(methods, self._sorted(self._unsorted))

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self._sort_column = column
        self._sort_order = order
        self.set_items(self.methods, self._unsorted)

    def _sorted(self, rows):
        column = self._sort_column
        if column < 0 or column >= len(self.columns):
            return rows
        keys = {
            _R_ORDER: lambda row: (row.first is None, row.first or 0),
            _R_METHOD: lambda row: self.method_name(row.method).lower(),
            _R_CALLS: lambda row: row.calls,
            _R_RATE: lambda row: row.rate,
            _R_BASELINE: lambda row: (row.baseline_rate is not None, row.baseline_rate or 0),
            _R_VERDICT: lambda row: ({NEW: 2, MORE: 1}.get(row.verdict, 0), row.factor or 0),
        }
        return sorted(rows, key=keys[column],
                      reverse=self._sort_order == Qt.SortOrder.DescendingOrder)

    def verdict_text(self, row):
        if row.verdict == NEW:
            return self.translations["trace.verdict_new"]
        if row.verdict == MORE:
            return self.translations["trace.verdict_more"].format(factor=row.factor)
        return self.translations["trace.verdict_same"]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.items[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.ToolTipRole and column == _R_METHOD:
            return self.signature(row.method)
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (_R_ORDER, _R_CALLS, _R_RATE,
                                                                    _R_BASELINE):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == _R_ORDER:
            return "" if row.first is None else str(row.first + 1)
        if column == _R_METHOD:
            return self.method_name(row.method)
        if column == _R_CALLS:
            return str(row.calls)
        if column == _R_RATE:
            return f"{row.rate:.1f}"
        if column == _R_BASELINE:
            return "—" if row.baseline_rate is None else f"{row.baseline_rate:.1f}"
        return self.verdict_text(row)


class TraceSequenceModel(_TableModel):
    """The calls of the scenario in order (TraceCalls)."""

    def __init__(self, translations, parent=None):
        super().__init__(translations, _SEQUENCE_COLUMNS, parent)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        call = self.items[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.ToolTipRole and column == _S_METHOD:
            return self.signature(call.method)
        if role == Qt.ItemDataRole.ToolTipRole and column == _S_CALLER:
            return call.caller
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == _S_TIME:
            return f"{call.time / 1000:.3f} ms"
        if column == _S_THREAD:
            return str(call.thread)
        if column == _S_METHOD:
            return self.method_name(call.method)
        return call.caller


def trace_table(model, stretch_column, sortable=False):
    """A table of trace results: whole rows, one at a time.

    ``sortable``: a click on a column header sorts by it; until then the
    rows keep the model's order.
    """
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    header = view.horizontalHeader()
    for column in range(model.columnCount()):
        header.setSectionResizeMode(
            column, QHeaderView.ResizeMode.Stretch if column == stretch_column
            else QHeaderView.ResizeMode.Interactive)
    if sortable:
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        view.setSortingEnabled(True)
    return view


class RowFilterBar(QWidget):
    """Narrow the rows of a trace down: by words in the name, by call count."""

    changed = pyqtSignal()

    def __init__(self, translations, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(translations["trace.rows_name_placeholder"])
        self.name_input.setToolTip(translations["trace.rows_name_tooltip"])
        self.name_input.setClearButtonEnabled(True)
        self.name_input.textChanged.connect(lambda _text: self.changed.emit())
        layout.addWidget(self.name_input, 3)
        self.calls_input = QLineEdit()
        self.calls_input.setPlaceholderText(translations["trace.rows_calls_placeholder"])
        self.calls_input.setToolTip(translations["trace.rows_calls_tooltip"])
        self.calls_input.setClearButtonEnabled(True)
        self.calls_input.textChanged.connect(self._on_calls_changed)
        layout.addWidget(self.calls_input, 1)
        self._calls = None

    def _on_calls_changed(self, text):
        try:
            self._calls = calls_filter(text)
            self.calls_input.setStyleSheet("")
        except ValueError:
            # Keep the last valid filter while the text is being typed.
            self.calls_input.setStyleSheet("border: 1px solid #f85149;")
            return
        self.changed.emit()

    def accepts(self, row, text):
        """Whether to show ``row`` (a TraceRow), its function or method described by ``text``."""
        if self._calls is not None and not self._calls(row.calls):
            return False
        text = text.lower()
        return all(word in text for word in self.name_input.text().lower().split())


class TraceDialog(QDialog):
    """Hook many C# methods, record a baseline and a scenario, compare.

    Not modal. The worker and its Frida session live as long as the window;
    closing it removes the interceptors.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # CatalogMethod to log the calls of, or to create a method hook for
    monitor_requested = pyqtSignal(object)
    hook_requested = pyqtSignal(object)

    def __init__(self, translations, pid, parent=None, engine=None, filter_repository=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.engine = resolve_engine(pid, engine)
        self.limit = WINE_TRACE_LIMIT if is_wine_process(pid) else None
        self._filter_repository = filter_repository
        self.methods = []
        self.baseline = None
        self.scenario = None
        self.rows = []
        self._state = _IDLE
        self._recording = None
        self._selected = None
        self.setWindowTitle(self._title())
        self.resize(1100, 720)
        self._build_ui()
        self._worker = TraceWorker(pid, engine=self.engine)
        self._worker.planned.connect(self._on_planned)
        self._worker.started.connect(self._on_started)
        self._worker.progress.connect(self._on_progress)
        self._worker.began.connect(self._on_began)
        self._worker.live.connect(self._on_live)
        self._worker.recorded.connect(self._on_recorded)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.ended.connect(self._on_ended)
        self._worker.command_failed.connect(self._on_command_failed)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self._worker.candidates_found.connect(self._on_candidates)
        self._worker.start()
        self._worker.candidates()
        self._set_state(_IDLE)

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        hint = QLabel(self.translations["trace.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        scope = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText(self.translations["trace.filter_placeholder"])
        self.filter_input.setToolTip(self.translations["trace.filter_tooltip"])
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.returnPressed.connect(self._toggle_trace)
        scope.addWidget(self.filter_input, 1)
        self.trace_button = QPushButton(self.translations["trace.start"])
        self.trace_button.setObjectName("btn_primary")
        self.trace_button.clicked.connect(self._toggle_trace)
        scope.addWidget(self.trace_button)
        layout.addLayout(scope)

        self.filter_panel = TraceFilterPanel(self.translations, self._filter_repository, self)
        self.filter_panel.selection_changed.connect(self._show_choice)
        layout.addWidget(self.filter_panel)
        self.choice_label = QLabel(self.translations["trace.filters_loading"])
        self.choice_label.setStyleSheet("color: #8b949e;")
        layout.addWidget(self.choice_label)
        self.filter_input.textChanged.connect(lambda _text: self._show_choice())

        steps = QHBoxLayout()
        self.baseline_button = QPushButton(self.translations["trace.baseline"])
        self.baseline_button.setToolTip(self.translations["trace.baseline_tooltip"])
        self.baseline_button.setCheckable(True)
        self.baseline_button.clicked.connect(lambda: self._toggle_recording("baseline"))
        steps.addWidget(self.baseline_button)
        self.scenario_button = QPushButton(self.translations["trace.scenario"])
        self.scenario_button.setToolTip(self.translations["trace.scenario_tooltip"])
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
        rows_bar.addWidget(QLabel(self.translations["trace.methods"]))
        rows_bar.addStretch()
        self.notable_check = QCheckBox(self.translations["trace.notable_only"])
        self.notable_check.setToolTip(self.translations["trace.notable_tooltip"])
        self.notable_check.setChecked(True)
        self.notable_check.toggled.connect(lambda _checked: self._show_rows())
        rows_bar.addWidget(self.notable_check)
        top_layout.addLayout(rows_bar)
        self.row_filter = RowFilterBar(self.translations, self)
        self.row_filter.changed.connect(self._show_rows)
        top_layout.addWidget(self.row_filter)
        self.row_model = TraceRowModel(self.translations, self)
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
        bottom_layout.addWidget(QLabel(self.translations["trace.sequence"]))
        self.sequence_model = TraceSequenceModel(self.translations, self)
        self.sequence_view = self._table(self.sequence_model, _S_CALLER)
        sequence_header = self.sequence_view.horizontalHeader()
        sequence_header.resizeSection(_S_TIME, 110)
        sequence_header.resizeSection(_S_THREAD, 80)
        sequence_header.resizeSection(_S_METHOD, 280)
        self.sequence_view.selectionModel().currentRowChanged.connect(self._on_call_selected)
        bottom_layout.addWidget(self.sequence_view, 1)
        splitter.addWidget(bottom)
        splitter.setSizes([330, 290])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.selection_label = QLabel("")
        self.selection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        actions.addWidget(self.selection_label, 1)
        self.monitor_button = QPushButton(self.translations["trace.monitor"])
        self.monitor_button.setToolTip(self.translations["trace.monitor_tooltip"])
        self.monitor_button.clicked.connect(self._request_monitor)
        actions.addWidget(self.monitor_button)
        self.hook_button = QPushButton(self.translations["monitor.create_hook"])
        self.hook_button.setToolTip(self.translations["monitor.create_hook_tooltip"])
        self.hook_button.clicked.connect(self._request_hook)
        actions.addWidget(self.hook_button)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._show_selection()

    def _table(self, model, stretch_column, sortable=False):
        view = trace_table(model, stretch_column, sortable)
        view.doubleClicked.connect(lambda _index: self._request_monitor())
        return view

    def _set_state(self, state):
        self._state = state
        tracing = state in (_READY, _RECORDING)
        self.filter_input.setEnabled(state == _IDLE)
        self.filter_panel.setEnabled(state == _IDLE)
        self.trace_button.setEnabled(state in (_IDLE, _PREPARING, _READY))
        self.trace_button.setText(self.translations[
            "trace.cancel" if state == _PREPARING else "trace.stop" if tracing else "trace.start"])
        for phase, button in (("baseline", self.baseline_button),
                              ("scenario", self.scenario_button)):
            active = state == _RECORDING and self._recording == phase
            button.setEnabled(state == _READY or active)
            button.setChecked(active)
            button.setText(self.translations[f"trace.{phase}_stop" if active else f"trace.{phase}"])
        if state != _RECORDING:
            self.recording_label.setText("")

    # --- Trace -----------------------------------------------------------------
    def _words(self):
        return self.filter_input.text().lower().split()

    def _toggle_trace(self):
        if self._state == _IDLE:
            chosen = self.filter_panel.selection(self._words())
            if chosen is None:
                # The method list is still loading: let the agent choose by words.
                self._set_state(_PREPARING)
                self.status_label.setText(self.translations["trace.planning"])
                self._worker.plan(self._words())
                return
            if not chosen:
                self.status_label.setText(self.translations["trace.filters_nothing"])
                return
            if not self._may_trace(len(chosen)):
                return
            self._set_state(_PREPARING)
            self.status_label.setText(self.translations["trace.preparing"].format(
                count=len(chosen)))
            self._worker.trace_selection(chosen, self._words())
        elif self._state == _PREPARING:
            self.trace_button.setEnabled(False)
            self._worker.cancel()
        elif self._state == _READY:
            self._set_state(_PREPARING)
            self._worker.stop()

    def _title(self):
        return self.translations["trace.title"].format(engine=self.engine.display_name)

    def _may_trace(self, count):
        """Whether ``count`` methods may be traced: within the limit, and confirmed."""
        if self.limit is not None and count > self.limit:
            self.status_label.setText(self.translations["trace.wine_limit"].format(
                count=count, limit=self.limit))
            return False
        if count > LARGE_TRACE and not self._confirm_large(count):
            self.status_label.setText("")
            return False
        return True

    def _on_candidates(self, methods, _skipped):
        self.filter_panel.set_candidates(methods)

    def _show_choice(self):
        chosen = self.filter_panel.selection(self._words())
        if chosen is None:
            self.choice_label.setText(self.translations["trace.filters_loading"])
            return
        text = self.translations["trace.filters_count"].format(
            count=len(chosen), total=len(self.filter_panel.candidates))
        if self.limit is not None and len(chosen) > self.limit:
            text += " " + self.translations["trace.filters_over_limit"].format(limit=self.limit)
        self.choice_label.setText(text)

    def _confirm_large(self, count):
        """Whether to trace ``count`` methods, more than LARGE_TRACE."""
        answer = QMessageBox.question(
            self, self._title(),
            self.translations["trace.confirm_large"].format(count=count))
        return answer == QMessageBox.StandardButton.Yes

    def _on_planned(self, plan):
        count = plan.get("methods", 0)
        if count == 0:
            self._set_state(_IDLE)
            self.status_label.setText(self.translations["trace.nothing"].format(
                assemblies=", ".join(plan.get("assemblies", [])) or "—"))
            return
        if not self._may_trace(count):
            self._set_state(_IDLE)
            return
        self.status_label.setText(self.translations["trace.preparing"].format(count=count))
        self._worker.trace(self._words())

    def _on_progress(self, step, done, total, current):
        text = self.translations[f"trace.progress_{step}"].format(done=done, total=total)
        if current:
            text += f"\n{current}"
        self.status_label.setText(text)

    def _on_started(self, methods, info):
        self.methods = methods
        self.baseline = None
        self.scenario = None
        self._show_results()
        self._set_state(_READY)
        skipped = sum(info.get("skipped", {}).values())
        self.status_label.setText(self.translations["trace.started"].format(
            count=len(methods), skipped=skipped,
            failed=info.get("notCompiled", 0) + info.get("failed", 0)))

    def _on_stopped(self):
        self._set_state(_IDLE)
        self.status_label.setText(self.translations["trace.stopped"])

    def _on_ended(self):
        self._set_state(_IDLE)
        self.status_label.setText(self.translations["trace.ended"])

    def _on_command_failed(self, action, message):
        # A failed start leaves no trace; other commands keep it.
        self._set_state(_IDLE if action in ("plan", "trace", "start") else _READY)
        if message.endswith("cancelled"):
            self.status_label.setText(self.translations["trace.cancelled"])
            return
        self.status_label.setText(self.translations["trace.failed"].format(message=message))

    def _on_worker_failed(self, message):
        self._set_state(_IDLE)
        self.trace_button.setEnabled(False)
        self.status_label.setText(self.translations["trace.worker_failed"].format(
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
            self.recording_label.setText(self.translations["trace.live"].format(
                phase=self.translations[f"trace.phase_{live.get('phase', '')}"],
                calls=live.get("calls", 0), methods=live.get("methods", 0)))

    def _on_recorded(self, result):
        self._recording = None
        self._set_state(_READY)
        seconds = result.get("duration", 0) / 1000
        if result.get("phase") == "baseline":
            self.baseline = result
            self.status_label.setText(self.translations["trace.baseline_done"].format(
                methods=len(result.get("counts", {})), seconds=seconds))
            if self.scenario is not None:
                self._show_results()  # compare the scenario with the new baseline
            return
        self.scenario = result
        self._show_results()
        text = self.translations["trace.scenario_done"].format(
            methods=len(self.rows), notable=sum(row.notable for row in self.rows),
            calls=len(self.sequence_model.items), seconds=seconds)
        if result.get("dropped"):
            text += "  ·  " + self.translations["trace.dropped"].format(count=result["dropped"])
        self.status_label.setText(text)

    # --- Results -----------------------------------------------------------------
    def _show_results(self):
        self.rows = analyze(self.scenario, self.baseline) if self.scenario else []
        calls = recording_calls(self.scenario) if self.scenario else []
        self.sequence_model.set_items(self.methods, calls)
        self._show_rows()

    def _show_rows(self):
        notable_only = self.notable_check.isChecked()
        rows = [row for row in self.rows if (row.notable or not notable_only)
                and self.row_filter.accepts(row, self.methods[row.method].signature)]
        self.row_model.set_items(self.methods, rows)
        self._selected = None
        self._show_selection()

    def _on_row_selected(self, current, _previous=None):
        row = self.row_model.item_at(current.row()) if current.isValid() else None
        if row is None:
            return
        self._selected = row.method
        self._show_selection()
        if row.first is not None:
            # Its first call in the sequence
            index = self.sequence_model.index(row.first, 0)
            self.sequence_view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _on_call_selected(self, current, _previous=None):
        call = self.sequence_model.item_at(current.row()) if current.isValid() else None
        if call is not None:
            self._selected = call.method
            self._show_selection()

    def selected_method(self):
        """The CatalogMethod chosen in either table, or None."""
        return self.methods[self._selected] if self._selected is not None else None

    def _show_selection(self):
        method = self.selected_method()
        self.monitor_button.setEnabled(method is not None)
        self.hook_button.setEnabled(method is not None)
        self.selection_label.setText(method.signature if method is not None else "")

    def _request_monitor(self):
        method = self.selected_method()
        if method is not None:
            self.monitor_requested.emit(method)

    def _request_hook(self):
        method = self.selected_method()
        if method is not None:
            self.hook_requested.emit(method)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        # Removing thousands of interceptors takes a moment; do not block the
        # window for that.
        worker = self._worker
        worker.requestInterruption()
        if worker.isRunning():
            _closing_workers.add(worker)
            worker.finished.connect(lambda: _closing_workers.discard(worker))
        super().closeEvent(event)


# Workers of closed windows, kept until they have removed their interceptors.
_closing_workers = set()


def wait_for_closing_workers(timeout_ms):
    """Wait for the workers of closed windows; True if all have finished.

    A QThread still running when the application exits aborts it.
    """
    return all(worker.wait(timeout_ms) for worker in list(_closing_workers))
