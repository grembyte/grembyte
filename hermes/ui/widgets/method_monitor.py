"""Window of the method monitor: monitored C# methods and their calls."""

from datetime import datetime

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QPoint, Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hermes.features.catalog import short_name
from hermes.features.monitor import MethodMonitorWorker
from hermes.ui.widgets.object_inspector import ObjectInspectorDialog, link_label, link_width
from hermes.ui.widgets.table import resizable_columns, set_row_height


# Calls kept in the log; older ones are dropped.
MAX_CALLS = 5000

# Columns of the monitored methods and of the call log.
_M_METHOD, _M_SIGNATURE, _M_CALLS, _M_RETURNS, _M_OBJECTS, _M_STATUS = range(6)
_C_TIME, _C_THREAD, _C_METHOD, _C_SELF, _C_ARGS, _C_RESULT, _C_CALLER = range(7)
_CALL_COLUMNS = (
    "monitor.column_time", "monitor.column_thread", "monitor.column_method",
    "monitor.column_self", "monitor.column_args", "monitor.column_result",
    "monitor.column_caller",
)


def call_time(call):
    """Local time of a call, e.g. ``09:47:10.738``."""
    return datetime.fromtimestamp(call.get("time", 0) / 1000).strftime("%H:%M:%S.%f")[:-3]


def value_text(value):
    """Text of a logged value (``{text, target?, snapshot?}``), '' for none."""
    return value.get("text", "") if value else ""


def argument_text(arg):
    """``name=value``, plus ``⇒ value`` for what a ref/out argument held after the call."""
    text = f"{arg.get('name', '?')}={value_text(arg)}"
    if "after" in arg:
        text += f" ⇒ {value_text(arg['after'])}"
    return text


def arguments_text(args):
    return ", ".join(argument_text(arg) for arg in args)


def call_values(call, translations):
    """(label, value) of everything a call logged, for the details."""
    values = []
    if call.get("self"):
        values.append(("this", call["self"]))
    for arg in call.get("args", []):
        values.append((arg.get("name", "?"), arg))
        if "after" in arg:
            values.append((f"{arg.get('name', '?')} ⇒", arg["after"]))
    if "result" in call:
        values.append((translations["monitor.column_result"], call["result"]))
    return values


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
            return call_time(call)
        if column == _C_THREAD:
            return str(call.get("threadId", ""))
        if column == _C_METHOD:
            return self.names.get(call.get("monitorId"), "?")
        if column == _C_SELF:
            return value_text(call.get("self"))
        if column == _C_ARGS:
            if role == Qt.ItemDataRole.ToolTipRole:
                return "\n".join(argument_text(arg) for arg in call.get("args", []))
            return arguments_text(call.get("args", []))
        if column == _C_RESULT:
            return value_text(call.get("result"))
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

    # CatalogMethod to create a method hook for
    hook_requested = pyqtSignal(object)
    # a field of an inspected object to put on the address list (see ObjectInspectorDialog)
    address_requested = pyqtSignal(object)

    def __init__(self, translations, pid, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.log = CallLogModel(translations, self)
        # monitor id -> {"method", "returns", "row", "calls", "dropped"}
        self._monitors = {}
        self._paused = False
        # open object inspectors; inspection request id -> waiting inspector
        self.inspectors = []
        self._inspections = {}
        self._detail_time = ""
        self.setWindowTitle(translations["monitor.title"])
        self.resize(1100, 700)
        self._build_ui()
        self._worker = MethodMonitorWorker(pid)
        self._worker.monitor_added.connect(self._on_added)
        self._worker.monitor_failed.connect(self._on_failed)
        self._worker.monitor_ended.connect(self._on_ended)
        self._worker.calls.connect(self._on_calls)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self._worker.inspected.connect(self._on_inspected)
        self._worker.inspect_failed.connect(self._on_inspect_failed)
        self._worker.evaluated.connect(self._on_evaluated)
        self._worker.evaluate_failed.connect(self._on_evaluate_failed)
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
        self.methods_table = QTableWidget(0, 6)
        self.methods_table.setHorizontalHeaderLabels([
            self.translations["monitor.column_method"],
            self.translations["monitor.column_signature"],
            self.translations["monitor.column_calls"],
            self.translations["monitor.column_returns"],
            self.translations["monitor.column_objects"],
            self.translations["monitor.column_status"],
        ])
        self.methods_table.horizontalHeaderItem(_M_RETURNS).setToolTip(
            self.translations["monitor.returns_tooltip"])
        self.methods_table.horizontalHeaderItem(_M_OBJECTS).setToolTip(
            self.translations["monitor.objects_tooltip"])
        self.methods_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.methods_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.methods_table.verticalHeader().setVisible(False)
        set_row_height(self.methods_table)
        header = self.methods_table.horizontalHeader()
        for column in range(6):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column == _M_SIGNATURE
                else QHeaderView.ResizeMode.ResizeToContents)
        resizable_columns(self.methods_table)
        self.methods_table.itemSelectionChanged.connect(self._update_buttons)
        top_layout.addWidget(self.methods_table)
        buttons = QHBoxLayout()
        self.remove_button = QPushButton(self.translations["monitor.remove"])
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove_button)
        self.hook_button = QPushButton(self.translations["monitor.create_hook"])
        self.hook_button.setObjectName("btn_primary")
        self.hook_button.setToolTip(self.translations["monitor.create_hook_tooltip"])
        self.hook_button.setEnabled(False)
        self.hook_button.clicked.connect(self._request_hook)
        buttons.addWidget(self.hook_button)
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
        resizable_columns(self.log_view)
        self.log_view.selectionModel().currentRowChanged.connect(self._show_call)
        # A reset (clearing the log) does not report a new current row.
        self.log.modelReset.connect(lambda: self._show_call(QModelIndex()))
        bottom_layout.addWidget(self.log_view, 1)
        # The values of the selected call; double click inspects an object.
        self.details = QTableWidget(0, 3)
        self.details.setHorizontalHeaderLabels([
            self.translations["monitor.details_name"],
            self.translations["monitor.details_value"],
            self.translations["monitor.details_fields"],
        ])
        self.details.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.details.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.details.verticalHeader().setVisible(False)
        self.details.setWordWrap(False)
        details_header = self.details.horizontalHeader()
        details_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        details_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        details_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        details_header.resizeSection(
            2, link_width(self.details, [self.translations["monitor.object_details"]]))
        resizable_columns(self.details)
        self.details.setMaximumHeight(190)
        self.details.cellDoubleClicked.connect(self._inspect_detail)
        bottom_layout.addWidget(self.details)
        splitter.addWidget(bottom)
        splitter.setSizes([180, 520])
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # --- Monitors --------------------------------------------------------------
    def _option_check(self, row, column, checked, tooltip, toggled):
        check = QCheckBox()
        check.setChecked(checked)
        check.setToolTip(tooltip)
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        holder_layout.addWidget(check)
        self.methods_table.setCellWidget(row, column, holder)
        check.toggled.connect(toggled)
        return check

    def add_method(self, method, returns=None, objects=None):
        """Monitor a ``CatalogMethod``; a method is monitored at most once.

        ``returns`` and ``objects`` default to "if the method has any": a
        return value or ref/out arguments, objects among ``this``, the
        arguments and the result.
        """
        for state in self._monitors.values():
            if state["method"].signature == method.signature:
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
        state = {"method": method, "returns": returns, "objects": objects,
                 "calls": 0, "dropped": 0}
        state["returns_check"] = self._option_check(
            row, _M_RETURNS, bool(returns), self.translations["monitor.returns_tooltip"],
            lambda checked: self._set_option(state, "returns", checked))
        state["objects_check"] = self._option_check(
            row, _M_OBJECTS, bool(objects), self.translations["monitor.objects_tooltip"],
            lambda checked: self._set_option(state, "objects", checked))
        self._start(state)
        self.raise_()

    def _start(self, state):
        monitor_id = self._worker.add_monitor(
            state["method"].identity(), state["returns"], state["objects"])
        self._monitors[monitor_id] = state
        self.log.names[monitor_id] = short_name(state["method"].signature)
        self._set_status(state, self.translations["monitor.status_starting"])

    def _set_option(self, state, option, enabled):
        # Frida needs onLeave from the start: monitor the method anew.
        monitor_id = self._id_of(state)
        if monitor_id is not None:
            self._worker.remove_monitor(monitor_id)
            del self._monitors[monitor_id]
        state[option] = enabled
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
        selected = self.methods_table.currentRow() >= 0
        self.remove_button.setEnabled(selected)
        self.hook_button.setEnabled(selected)

    def _request_hook(self):
        row = self.methods_table.currentRow()
        if row < 0:
            return
        signature = self.methods_table.item(row, _M_METHOD).toolTip()
        state = next((state for state in self._monitors.values()
                      if state["method"].signature == signature), None)
        if state is not None:
            self.hook_requested.emit(state["method"])

    # --- Worker ----------------------------------------------------------------
    def _on_added(self, monitor_id, info):
        state = self._monitors.get(monitor_id)
        if state is None:
            return
        # The agent decided the options left open; show them, and grey out
        # what the method has nothing for.
        for option, check, records, has, key in (
                ("returns", state["returns_check"], "recordsResults", "hasResults",
                 "monitor.no_results"),
                ("objects", state["objects_check"], "recordsObjects", "hasObjects",
                 "monitor.no_objects")):
            state[option] = info.get(records, state[option])
            check.blockSignals(True)
            check.setChecked(bool(state[option]))
            check.blockSignals(False)
            if not info.get(has, True) and not state[option]:
                check.setEnabled(False)
                check.setToolTip(self.translations[key])
        row = self._row_of(state)
        if row >= 0 and info.get("returns"):
            self.methods_table.item(row, _M_SIGNATURE).setText(
                f"{state['method'].parameters} → {info['returns']}")
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
        call = self.log.calls[current.row()] if current.isValid() else None
        values = call_values(call, self.translations) if call else []
        self._detail_time = call_time(call) if call else ""
        self.details.setRowCount(len(values))
        for row, (label, value) in enumerate(values):
            name = QTableWidgetItem(label)
            name.setData(Qt.ItemDataRole.UserRole, value)
            text = QTableWidgetItem(value_text(value))
            text.setToolTip(value_text(value))
            self.details.setItem(row, 0, name)
            self.details.setItem(row, 1, text)
            self.details.removeCellWidget(row, 2)
            if value.get("target"):
                link = link_label(self.translations["monitor.object_details"],
                                  lambda row=row: self._inspect_detail(row, 2))
                link.setToolTip(self.translations[
                    "monitor.fields_captured" if "snapshot" in value else "monitor.fields_current"])
                self.details.setCellWidget(row, 2, link)

    # --- Object inspector ------------------------------------------------------
    def _inspect_detail(self, row, _column):
        """Show the fields of the object in details ``row``: as recorded at the
        call if the monitor recorded objects, else as they are now."""
        item = self.details.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not value or not value.get("target"):
            return
        name = item.text().removesuffix(" ⇒")  # "info ⇒": what info held after the call
        if "snapshot" in value:
            self._show_inspector(ObjectInspectorDialog(
                self.translations, self.inspect, target=value["target"],
                contents=value["snapshot"], captured=self._detail_time,
                evaluate=self._request_evaluation, name=name, parent=self))
        else:
            self.inspect(value["target"], name=name)

    def inspect(self, target, origin=None, name=""):
        """Show the current fields of ``target`` in a dialog of its own.

        ``origin`` is the dialog it was opened from (one level less deep).
        """
        self._show_inspector(ObjectInspectorDialog(
            self.translations, self.inspect, target=target, request=self._request_inspection,
            evaluate=self._request_evaluation, name=name,
            depth=1 if origin is None else origin.depth + 1, parent=self), origin)

    def _show_inspector(self, inspector, origin=None):
        self.inspectors.append(inspector)
        inspector.address_requested.connect(self.address_requested.emit)
        inspector.destroyed.connect(lambda _object, inspector=inspector: self._forget(inspector))
        if origin is not None:
            # Opened from another dialog: a little offset, so both stay visible.
            inspector.move(origin.pos() + QPoint(32, 32))
        inspector.show()
        inspector.activateWindow()

    def _forget(self, inspector):
        if inspector in self.inspectors:
            self.inspectors.remove(inspector)
        for request_id, waiting in list(self._inspections.items()):
            if waiting is inspector:
                del self._inspections[request_id]

    def _request_inspection(self, target, inspector):
        self._inspections[self._worker.inspect(target)] = inspector

    def _request_evaluation(self, target, names, inspector):
        self._inspections[self._worker.evaluate(target, names)] = inspector

    def _on_evaluated(self, request_id, results):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_properties(results)

    def _on_evaluate_failed(self, request_id, message):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_property_error(message)

    def _on_inspected(self, request_id, contents):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_contents(contents)

    def _on_inspect_failed(self, request_id, message):
        inspector = self._inspections.pop(request_id, None)
        if inspector is not None:
            inspector.show_error(message)

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


def wait_for_closing_workers(timeout_ms):
    """Wait for the workers of closed windows; True if all have finished.

    A QThread still running when the application exits aborts it.
    """
    return all(worker.wait(timeout_ms) for worker in list(_closing_workers))
