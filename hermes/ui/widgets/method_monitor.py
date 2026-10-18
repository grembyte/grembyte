"""Window of the method monitor: monitored C# methods and their calls."""

from datetime import datetime

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hermes.core.method_monitor import MethodMonitorWorker
from hermes.ui.widgets.table import set_row_height


# Calls kept in the log; older ones are dropped.
MAX_CALLS = 5000

# Columns of the monitored methods and of the call log.
_M_METHOD, _M_SIGNATURE, _M_CALLS, _M_RETURNS, _M_STATUS = range(5)
_C_TIME, _C_THREAD, _C_METHOD, _C_SELF, _C_ARGS, _C_RESULT, _C_CALLER = range(7)
_CALL_COLUMNS = (
    "monitor.column_time", "monitor.column_thread", "monitor.column_method",
    "monitor.column_self", "monitor.column_args", "monitor.column_result",
    "monitor.column_caller",
)


def short_name(signature):
    """``Class:Method`` of ``Namespace.Class:Method (params)``."""
    class_name, _, method = signature.split(" (")[0].partition(":")
    return f"{class_name.rsplit('.', 1)[-1]}:{method}"


def arguments_text(args):
    return ", ".join(f"{name}={value}" for name, value in args)


class CallLogModel(QAbstractTableModel):
    """The latest calls, newest last."""

    def __init__(self, translations, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.calls = []
        self.names = {}

    def rowCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.calls)

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(_CALL_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.translations[_CALL_COLUMNS[section]]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole,
                                               Qt.ItemDataRole.ToolTipRole):
            return None
        call = self.calls[index.row()]
        column = index.column()
        if column == _C_TIME:
            return datetime.fromtimestamp(call.get("time", 0) / 1000).strftime("%H:%M:%S.%f")[:-3]
        if column == _C_THREAD:
            return str(call.get("threadId", ""))
        if column == _C_METHOD:
            return self.names.get(call.get("monitorId"), "?")
        if column == _C_SELF:
            return call.get("self") or ""
        if column == _C_ARGS:
            return arguments_text(call.get("args", []))
        if column == _C_RESULT:
            return call.get("result", "")
        return call.get("caller", "")

    def append(self, calls):
        if not calls:
            return
        overflow = len(self.calls) + len(calls) - MAX_CALLS
        if overflow > 0:
            removed = min(overflow, len(self.calls))
            self.beginRemoveRows(QModelIndex(), 0, removed - 1)
            del self.calls[:removed]
            self.endRemoveRows()
            calls = calls[-MAX_CALLS:]
        start = len(self.calls)
        self.beginInsertRows(QModelIndex(), start, start + len(calls) - 1)
        self.calls.extend(calls)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.calls = []
        self.endResetModel()


class MethodMonitorDialog(QDialog):
    """Monitored methods above, their calls below; not modal.

    ``add_method`` starts monitoring a ``CatalogMethod``. The worker and its
    Frida session live as long as the dialog.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    def __init__(self, translations, pid, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.log = CallLogModel(translations, self)
        # monitor id -> {"method", "returns", "row", "calls", "dropped"}
        self._monitors = {}
        self._paused = False
        self.setWindowTitle(translations["monitor.title"])
        self.resize(1100, 700)
        self._build_ui()
        self._worker = MethodMonitorWorker(pid)
        self._worker.monitor_added.connect(self._on_added)
        self._worker.monitor_failed.connect(self._on_failed)
        self._worker.monitor_ended.connect(self._on_ended)
        self._worker.calls.connect(self._on_calls)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self._worker.start()

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        hint = QLabel(self.translations["monitor.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.methods_table = QTableWidget(0, 5)
        self.methods_table.setHorizontalHeaderLabels([
            self.translations["monitor.column_method"],
            self.translations["monitor.column_signature"],
            self.translations["monitor.column_calls"],
            self.translations["monitor.column_returns"],
            self.translations["monitor.column_status"],
        ])
        self.methods_table.horizontalHeaderItem(_M_RETURNS).setToolTip(
            self.translations["monitor.returns_tooltip"])
        self.methods_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.methods_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.methods_table.verticalHeader().setVisible(False)
        set_row_height(self.methods_table)
        header = self.methods_table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column == _M_SIGNATURE
                else QHeaderView.ResizeMode.ResizeToContents)
        self.methods_table.itemSelectionChanged.connect(self._update_buttons)
        top_layout.addWidget(self.methods_table)
        buttons = QHBoxLayout()
        self.remove_button = QPushButton(self.translations["monitor.remove"])
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        top_layout.addLayout(buttons)
        splitter.addWidget(top)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel(self.translations["monitor.calls"]))
        log_bar.addStretch()
        self.pause_button = QPushButton(self.translations["monitor.pause"])
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(self._toggle_pause)
        log_bar.addWidget(self.pause_button)
        clear_button = QPushButton(self.translations["monitor.clear"])
        clear_button.clicked.connect(self.log.clear)
        log_bar.addWidget(clear_button)
        bottom_layout.addLayout(log_bar)
        self.log_view = QTableView()
        self.log_view.setModel(self.log)
        self.log_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.log_view.setWordWrap(False)
        self.log_view.verticalHeader().setVisible(False)
        log_header = self.log_view.horizontalHeader()
        for column in range(len(_CALL_COLUMNS)):
            log_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column == _C_ARGS
                else QHeaderView.ResizeMode.Interactive)
        log_header.resizeSection(_C_TIME, 100)
        log_header.resizeSection(_C_THREAD, 70)
        log_header.resizeSection(_C_METHOD, 200)
        log_header.resizeSection(_C_SELF, 160)
        log_header.resizeSection(_C_RESULT, 110)
        log_header.resizeSection(_C_CALLER, 240)
        self.log_view.selectionModel().currentRowChanged.connect(self._show_call)
        bottom_layout.addWidget(self.log_view, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(110)
        bottom_layout.addWidget(self.details)
        splitter.addWidget(bottom)
        splitter.setSizes([180, 520])
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # --- Monitors --------------------------------------------------------------
    def add_method(self, method, returns=False):
        """Monitor a ``CatalogMethod``; a method is monitored at most once."""
        for state in self._monitors.values():
            if state["method"] == method:
                return
        row = self.methods_table.rowCount()
        self.methods_table.insertRow(row)
        name = QTableWidgetItem(short_name(method.signature))
        name.setToolTip(method.signature)
        self.methods_table.setItem(row, _M_METHOD, name)
        for column in (_M_SIGNATURE, _M_CALLS, _M_STATUS):
            self.methods_table.setItem(row, column, QTableWidgetItem(""))
        self.methods_table.item(row, _M_SIGNATURE).setText(
            f"{method.parameters} → {method.return_type}")
        self.methods_table.item(row, _M_CALLS).setText("0")
        check = QCheckBox()
        check.setChecked(returns)
        check.setToolTip(self.translations["monitor.returns_tooltip"])
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        holder_layout.addWidget(check)
        self.methods_table.setCellWidget(row, _M_RETURNS, holder)
        state = {"method": method, "returns": returns, "check": check,
                 "calls": 0, "dropped": 0}
        check.toggled.connect(lambda checked: self._set_returns(state, checked))
        self._start(state)
        self.raise_()

    def _start(self, state):
        monitor_id = self._worker.add_monitor(state["method"].identity(), state["returns"])
        self._monitors[monitor_id] = state
        self.log.names[monitor_id] = short_name(state["method"].signature)
        self._set_status(state, self.translations["monitor.status_starting"])

    def _set_returns(self, state, returns):
        # Frida needs onLeave from the start: monitor the method anew.
        monitor_id = self._id_of(state)
        if monitor_id is not None:
            self._worker.remove_monitor(monitor_id)
            del self._monitors[monitor_id]
        state["returns"] = returns
        self._start(state)

    def _id_of(self, state):
        return next((key for key, value in self._monitors.items() if value is state), None)

    def _row_of(self, state):
        for row in range(self.methods_table.rowCount()):
            if self.methods_table.item(row, _M_METHOD).toolTip() == state["method"].signature:
                return row
        return -1

    def _set_status(self, state, text, tooltip=""):
        row = self._row_of(state)
        if row >= 0:
            item = self.methods_table.item(row, _M_STATUS)
            item.setText(text)
            item.setToolTip(tooltip)

    def _remove_selected(self):
        row = self.methods_table.currentRow()
        if row < 0:
            return
        signature = self.methods_table.item(row, _M_METHOD).toolTip()
        for monitor_id, state in list(self._monitors.items()):
            if state["method"].signature == signature:
                self._worker.remove_monitor(monitor_id)
                del self._monitors[monitor_id]
        self.methods_table.removeRow(row)
        self._update_buttons()

    def _update_buttons(self):
        self.remove_button.setEnabled(self.methods_table.currentRow() >= 0)

    # --- Worker ----------------------------------------------------------------
    def _on_added(self, monitor_id, info):
        state = self._monitors.get(monitor_id)
        if state is None:
            return
        text = self.translations["monitor.status_active"]
        if info.get("undecoded"):
            text = self.translations["monitor.status_undecoded"].format(
                count=info["undecoded"])
        self._set_status(state, text, self.translations["monitor.status_tooltip"].format(
            address=info.get("address", ""),
            params=", ".join(info.get("params", [])) or "—",
            returns=info.get("returns", ""),
        ))

    def _on_failed(self, monitor_id, message):
        state = self._monitors.get(monitor_id)
        if state is not None:
            self._set_status(state, self.translations["monitor.status_failed"], message)
            self.status_label.setText(self.translations["monitor.failed"].format(
                method=short_name(state["method"].signature), message=message))

    def _on_ended(self, monitor_id):
        state = self._monitors.pop(monitor_id, None)
        if state is not None:
            self._set_status(state, self.translations["monitor.status_ended"],
                             self.translations["monitor.status_ended_tooltip"])

    def _on_worker_failed(self, message):
        self.status_label.setText(self.translations["monitor.worker_failed"].format(
            message=message))
        for state in self._monitors.values():
            self._set_status(state, self.translations["monitor.status_failed"], message)

    def _on_calls(self, calls, dropped, counts):
        for monitor_id, count in counts.items():
            state = self._monitors.get(monitor_id)
            if state is None:
                continue
            state["calls"] += count
            state["dropped"] += dropped.get(monitor_id, 0)
            row = self._row_of(state)
            if row >= 0:
                self.methods_table.item(row, _M_CALLS).setText(str(state["calls"]))
        calls = [call for call in calls if call.get("monitorId") in self._monitors]
        if not self._paused:
            follow = self._at_bottom()
            self.log.append(calls)
            if follow:
                self.log_view.scrollToBottom()
        missed = sum(state["dropped"] for state in self._monitors.values())
        if missed:
            self.status_label.setText(self.translations["monitor.dropped"].format(count=missed))

    def _at_bottom(self):
        bar = self.log_view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 2

    def _toggle_pause(self, paused):
        self._paused = paused
        self.pause_button.setText(
            self.translations["monitor.resume" if paused else "monitor.pause"])

    def _show_call(self, current, _previous=None):
        if not current.isValid():
            self.details.clear()
            return
        call = self.log.calls[current.row()]
        lines = [self.log.names.get(call.get("monitorId"), "?")]
        if call.get("self"):
            lines.append(f"this = {call['self']}")
        lines += [f"{name} = {value}" for name, value in call.get("args", [])]
        if "result" in call:
            lines.append(f"→ {call['result']}")
        lines.append(f"{self.translations['monitor.column_caller']}: {call.get('caller', '')}")
        self.details.setPlainText("\n".join(lines))

    def closeEvent(self, event):  # pylint: disable=invalid-name
        # Removing the interceptors can wait for calls in progress; do not
        # block the window for that.
        worker = self._worker
        worker.requestInterruption()
        if worker.isRunning():
            _closing_workers.add(worker)
            worker.finished.connect(lambda: _closing_workers.discard(worker))
        super().closeEvent(event)


# Workers of closed windows, kept until they have removed their interceptors.
_closing_workers = set()
