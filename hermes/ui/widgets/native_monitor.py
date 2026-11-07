"""Window of the native function monitor: monitored functions and their calls."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hermes.platform import is_wine_process
from hermes.features.native_monitor import (
    AUTO,
    DEFAULT_ARGUMENTS,
    DEFAULT_DUMP,
    FUNCTION,
    INSTRUCTION,
    MAX_DUMP,
    NativeMonitorWorker,
    argument_name,
    change_numbers,
    parse_location,
    signed,
)
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.labels import DocsHint, SelectableText
from hermes.ui.widgets.method_monitor import call_time
from hermes.ui.widgets.spin_boxes import SpinBox
from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height
from hermes.ui.workers import keep_until_finished


# Calls kept in the log; older ones are dropped.
MAX_CALLS = 5000
# Register values below this are shown as numbers rather than addresses,
# from the top on they are no user-space addresses either.
_SMALL = 0x10000
_TOP = 0x800000000000

# Columns of the monitored functions, the call log, the values and the changes.
_F_FUNCTION, _F_LOCATION, _F_CALLS, _F_SKIPPED, _F_STATUS = range(5)
_FUNCTION_COLUMNS = (
    "native_monitor.column_function", "native_monitor.column_location",
    "native_monitor.column_calls", "native_monitor.column_skipped",
    "native_monitor.column_status",
)
_C_TIME, _C_THREAD, _C_FUNCTION, _C_ARGS, _C_RESULT, _C_CALLER, _C_CHANGES = range(7)
_CALL_COLUMNS = (
    "monitor.column_time", "monitor.column_thread", "native_monitor.column_function",
    "monitor.column_args", "monitor.column_result", "monitor.column_caller",
    "native_monitor.column_changes",
)
_V_NAME, _V_VALUE, _V_NUMBER, _V_MEANING = range(4)
_VALUE_COLUMNS = (
    "native_monitor.column_name", "native_monitor.column_value",
    "native_monitor.column_number", "native_monitor.column_meaning",
)
_X_ARGUMENT, _X_OFFSET, _X_ADDRESS, _X_BEFORE, _X_AFTER, _X_BYTES, _X_NUMBER = range(7)
_CHANGE_COLUMNS = (
    "native_monitor.column_argument", "native_monitor.column_offset",
    "native_monitor.column_address", "native_monitor.column_before",
    "native_monitor.column_after", "native_monitor.column_bytes",
    "native_monitor.column_number_le",
)


def value_text(value):
    """A register value for the log: a small number, or the address and what it is."""
    number = int(value["value"], 16)
    if number < _SMALL or -_SMALL < signed(value["value"]) < 0:
        text = str(signed(value["value"]))
    else:
        text = value["value"]
    if value.get("text"):
        text += f' "{value["text"]}"'
    elif value.get("where"):
        text += f" ({value['where']})"
    return text


def registers_text(call):
    """The registers of an instruction pass that point somewhere or hold small numbers."""
    return ", ".join(f"{name}={value_text(value)}" for name, value in call["registers"])


def caller_text(call):
    return call.get("callerSymbol") or call.get("callerWhere") or call.get("caller", "")


def byte_numbers(hex_text):
    """``10 10`` for the bytes ``0a0a``."""
    return " ".join(str(byte) for byte in bytes.fromhex(hex_text))


class NativeCallModel(QAbstractTableModel):
    """The latest calls, newest last."""

    def __init__(self, translations, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.calls = []
        self.names = {}
        self.platform = None
        self.arch = None

    def rowCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.calls)

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(_CALL_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.translations[_CALL_COLUMNS[section]]
        return None

    def arguments_text(self, call):
        if "registers" in call:
            return registers_text(call)
        return ", ".join(f"{argument_name(index, self.platform, self.arch)}={value_text(value)}"
                         for index, value in enumerate(call.get("args", [])))

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole,
                                               Qt.ItemDataRole.ToolTipRole):
            return None
        call = self.calls[index.row()]
        column = index.column()
        if column == _C_TIME:
            return call_time(call)
        if column == _C_THREAD:
            return str(call.get("thread", ""))
        if column == _C_FUNCTION:
            return self.names.get(call.get("monitorId"), "?")
        if column == _C_ARGS:
            return self.arguments_text(call)
        if "registers" in call:
            return "—" if column in (_C_RESULT, _C_CHANGES) else ""
        if column == _C_RESULT:
            return value_text(call["result"])
        if column == _C_CALLER:
            return caller_text(call)
        count = len(call.get("changes", []))
        return f"{count}+" if call.get("moreChanges") else str(count)

    def set_calls(self, calls):
        self.beginResetModel()
        self.calls = list(calls[-MAX_CALLS:])
        self.endResetModel()

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

    def call_at(self, row):
        return self.calls[row] if 0 <= row < len(self.calls) else None


def _table(translations, columns, stretch_column, delete=None):
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels([translations[column] for column in columns])
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    for column in range(len(columns)):
        header.setSectionResizeMode(
            column, QHeaderView.ResizeMode.Stretch if column == stretch_column
            else QHeaderView.ResizeMode.ResizeToContents)
    resizable_columns(table)
    copy_menu(table, translations, delete)
    return table


def _item(text, address=None, tooltip=None):
    item = QTableWidgetItem(text)
    if address is not None:
        item.setData(Qt.ItemDataRole.UserRole, address)
    if tooltip:
        item.setToolTip(tooltip)
    return item


class NativeMonitorDialog(QDialog):
    """Monitored functions above, their calls in the middle, the selected call below.

    Not modal. ``add_function`` starts monitoring a NativeFunction, the input
    field anything ``parse_location`` reads. The worker and its Frida
    session live as long as the dialog.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # address (int) to show in the memory viewer
    memory_requested = Signal(object)

    def __init__(self, translations, pid, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.log = NativeCallModel(translations, self)
        # monitor id -> {"label", "key", "row", "address": (address, kind), "function"}
        self._monitors = {}
        self._next_id = 1
        # every call received (the log may show only those with changes)
        self._calls = []
        self._memory_address = None
        self.setWindowTitle(translations["native_monitor.title"])
        self.resize(1150, 780)
        self._build_ui()
        self._worker = NativeMonitorWorker(pid)
        self._worker.added.connect(self._on_added)
        self._worker.add_failed.connect(self._on_add_failed)
        self._worker.removed.connect(self._on_removed)
        self._worker.calls.connect(self._on_calls)
        self._worker.command_failed.connect(self._on_command_failed)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self._worker.start()

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        hint = DocsHint(t, t["native_monitor.hint"], "guide/native-tools/#native-function-monitor")
        layout.addWidget(hint)
        if is_wine_process(self.pid):
            warning = QLabel(t["native_monitor.wine_warning"])
            warning.setWordWrap(True)
            layout.addWidget(warning)

        add_bar = QHBoxLayout()
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText(t["native_monitor.location_placeholder"])
        self.location_input.setToolTip(t["native_monitor.location_tooltip"])
        self.location_input.returnPressed.connect(self._add_typed)
        add_bar.addWidget(self.location_input, 1)
        self.kind_combo = QComboBox()
        for kind in (AUTO, FUNCTION, INSTRUCTION):
            self.kind_combo.addItem(t[f"native_monitor.kind_{kind}"], kind)
        self.kind_combo.setToolTip(t["native_monitor.kind_tooltip"])
        add_bar.addWidget(self.kind_combo)
        add_bar.addWidget(QLabel(t["native_monitor.arguments"]))
        self.arguments_spin = SpinBox()
        self.arguments_spin.setRange(0, 8)
        self.arguments_spin.setValue(DEFAULT_ARGUMENTS)
        self.arguments_spin.setToolTip(t["native_monitor.arguments_tooltip"])
        add_bar.addWidget(self.arguments_spin)
        add_bar.addWidget(QLabel(t["native_monitor.dump"]))
        self.dump_spin = SpinBox()
        self.dump_spin.setRange(0, MAX_DUMP)
        self.dump_spin.setSingleStep(0x100)
        self.dump_spin.setValue(DEFAULT_DUMP)
        self.dump_spin.setToolTip(t["native_monitor.dump_tooltip"])
        add_bar.addWidget(self.dump_spin)
        self.add_button = QPushButton(t["native_monitor.add"])
        self.add_button.setObjectName("btn_primary")
        set_button_icon(self.add_button, "add")
        self.add_button.clicked.connect(self._add_typed)
        add_bar.addWidget(self.add_button)
        layout.addLayout(add_bar)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.functions_table = _table(t, _FUNCTION_COLUMNS, _F_LOCATION,
                                      delete=self._remove_selected)
        self.functions_table.horizontalHeaderItem(_F_SKIPPED).setToolTip(
            t["native_monitor.skipped_tooltip"])
        set_row_height(self.functions_table)
        self.functions_table.itemSelectionChanged.connect(self._update_buttons)
        top_layout.addWidget(self.functions_table)
        buttons = QHBoxLayout()
        self.remove_button = QPushButton(t["monitor.remove"])
        set_button_icon(self.remove_button, "delete")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove_button)
        self.function_button = QPushButton(t["native_monitor.whole_function"])
        set_button_icon(self.function_button, "functions")
        self.function_button.setToolTip(t["native_monitor.whole_function_tooltip"])
        self.function_button.setEnabled(False)
        self.function_button.clicked.connect(self._add_enclosing)
        buttons.addWidget(self.function_button)
        buttons.addStretch()
        top_layout.addLayout(buttons)
        splitter.addWidget(top)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel(t["monitor.calls"]))
        log_bar.addStretch()
        self.changes_check = QCheckBox(t["native_monitor.changes_only"])
        self.changes_check.setToolTip(t["native_monitor.changes_only_tooltip"])
        self.changes_check.toggled.connect(lambda _checked: self._show_calls())
        log_bar.addWidget(self.changes_check)
        self.pause_button = QPushButton(t["monitor.pause"])
        set_button_icon(self.pause_button, "pause")
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(lambda paused: self.pause_button.setText(
            t["monitor.resume" if paused else "monitor.pause"]))
        self.pause_button.toggled.connect(lambda paused: set_button_icon(
            self.pause_button, "play_arrow" if paused else "pause"))
        log_bar.addWidget(self.pause_button)
        clear_button = QPushButton(t["monitor.clear"])
        set_button_icon(clear_button, "clear_all")
        clear_button.clicked.connect(self._clear)
        log_bar.addWidget(clear_button)
        middle_layout.addLayout(log_bar)
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
        log_header.resizeSection(_C_FUNCTION, 220)
        log_header.resizeSection(_C_RESULT, 130)
        log_header.resizeSection(_C_CALLER, 240)
        log_header.resizeSection(_C_CHANGES, 80)
        resizable_columns(self.log_view)
        copy_menu(self.log_view, self.translations)
        self.log_view.selectionModel().currentRowChanged.connect(self._show_call)
        # A reset (clearing the log) does not report a new current row.
        self.log.modelReset.connect(lambda: self._show_call(QModelIndex()))
        middle_layout.addWidget(self.log_view, 1)
        splitter.addWidget(middle)

        details = QSplitter(Qt.Orientation.Horizontal)
        values = QWidget()
        values_layout = QVBoxLayout(values)
        values_layout.setContentsMargins(0, 0, 0, 0)
        values_layout.addWidget(QLabel(t["native_monitor.values"]))
        self.values_table = _table(t, _VALUE_COLUMNS, _V_MEANING)
        self.values_table.itemSelectionChanged.connect(
            lambda: self._pick_memory(self.values_table))
        self.values_table.cellDoubleClicked.connect(lambda _row, _column: self._request_memory())
        values_layout.addWidget(self.values_table)
        details.addWidget(values)
        changes = QWidget()
        changes_layout = QVBoxLayout(changes)
        changes_layout.setContentsMargins(0, 0, 0, 0)
        changes_layout.addWidget(QLabel(t["native_monitor.changes"]))
        self.changes_table = _table(t, _CHANGE_COLUMNS, _X_NUMBER)
        self.changes_table.setToolTip(t["native_monitor.changes_tooltip"])
        self.changes_table.itemSelectionChanged.connect(
            lambda: self._pick_memory(self.changes_table))
        self.changes_table.cellDoubleClicked.connect(lambda _row, _column: self._request_memory())
        changes_layout.addWidget(self.changes_table)
        details.addWidget(changes)
        details.setSizes([450, 650])
        splitter.addWidget(details)
        splitter.setSizes([150, 330, 260])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.selection_label = SelectableText()
        actions.addWidget(self.selection_label, 1)
        self.memory_button = QPushButton(t["native_trace.show_memory"])
        set_button_icon(self.memory_button, "grid_on")
        self.memory_button.setToolTip(t["native_monitor.show_memory_tooltip"])
        self.memory_button.setEnabled(False)
        self.memory_button.clicked.connect(self._request_memory)
        actions.addWidget(self.memory_button)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # --- Monitors --------------------------------------------------------------
    def add_function(self, function):
        """Monitor a NativeFunction of the native trace."""
        location = ({"module": function.module, "offset": function.offset} if function.module
                    else {"address": f"0x{function.address:x}"})
        # A call target of the trace: the start of a function
        self._add(location, function.label, function.signature, FUNCTION)

    def add_address(self, address):
        """Monitor what runs at ``address``: an instruction, or a function if it starts there."""
        text = f"0x{address:x}"
        self._add({"address": text}, text, text, AUTO)

    def _add_typed(self):
        text = self.location_input.text()
        try:
            location = parse_location(text)
        except ValueError:
            self.status_label.setText(self.translations["native_monitor.location_missing"])
            return
        self._add(location, text.strip(), text.strip(), self.kind_combo.currentData())

    def _add_enclosing(self):
        monitor_id = self._selected_monitor()
        function = None if monitor_id is None else self._monitors[monitor_id]["function"]
        if function is not None:
            start, name = function
            self._add({"address": start}, name, name, FUNCTION)

    def _add(self, location, label, tooltip, kind):
        key = tuple(sorted(location.items()))
        for state in self._monitors.values():
            if state["key"] == key:
                return  # monitored already
        monitor_id = self._next_id
        self._next_id += 1
        row = self.functions_table.rowCount()
        self.functions_table.insertRow(row)
        self.functions_table.setItem(row, _F_FUNCTION, _item(label, tooltip=tooltip))
        for column in (_F_LOCATION, _F_CALLS, _F_SKIPPED):
            self.functions_table.setItem(row, column, _item(""))
        self.functions_table.setItem(row, _F_STATUS, _item(
            self.translations["native_monitor.status_adding"]))
        self._monitors[monitor_id] = {"label": label, "key": key, "row": row, "address": None,
                                      "function": None}
        self.log.names[monitor_id] = label
        self._worker.add(monitor_id, location, self.arguments_spin.value(), self.dump_spin.value(),
                         kind)

    def _row_of(self, monitor_id):
        state = self._monitors.get(monitor_id)
        return None if state is None else state["row"]

    def _on_added(self, monitor_id, info):
        row = self._row_of(monitor_id)
        if row is None:
            return
        state = self._monitors[monitor_id]
        found = (info.get("address"), info.get("kind"))
        if any(other["address"] == found for other in self._monitors.values()):
            # The same function, written differently (name, module+offset, address)
            state["key"] = None
            self._worker.remove(monitor_id)
            self.status_label.setText(self.translations["native_monitor.already"].format(
                function=state["label"]))
            return
        state["address"] = found
        if info.get("symbol") and state["label"] != info["symbol"]:
            # Typed as an address: the symbol tells more.
            state["label"] = info["symbol"]
            self.log.names[monitor_id] = info["symbol"]
            self.functions_table.item(row, _F_FUNCTION).setText(info["symbol"])
        location = info.get("where") or info.get("address", "")
        self.functions_table.item(row, _F_LOCATION).setText(
            f"{location}  ({info.get('address', '')})")
        self.functions_table.item(row, _F_CALLS).setText("0")
        self.functions_table.item(row, _F_SKIPPED).setText("0")
        kind = info.get("kind", FUNCTION)
        self.functions_table.item(row, _F_STATUS).setText(
            self.translations[f"native_monitor.status_{kind}"])
        self.status_label.setText("")
        if info.get("function"):
            state["function"] = (info["function"], info["functionName"])
            self.status_label.setText(self.translations["native_monitor.inside_function"].format(
                function=info["functionName"]))
        self._update_buttons()

    def _on_add_failed(self, monitor_id, message):
        row = self._row_of(monitor_id)
        if row is not None:
            self.functions_table.item(row, _F_STATUS).setText(
                self.translations["native_monitor.status_failed"])
            self.functions_table.item(row, _F_STATUS).setToolTip(message)
            # Not monitored: may be added again (e.g. after a typo)
            self._monitors[monitor_id]["key"] = None
        self.status_label.setText(self.translations["native_monitor.add_failed"].format(
            message=message))

    def _selected_monitor(self):
        row = self.functions_table.currentRow()
        for monitor_id, state in self._monitors.items():
            if state["row"] == row:
                return monitor_id
        return None

    def _update_buttons(self):
        monitor_id = self._selected_monitor()
        self.remove_button.setEnabled(monitor_id is not None)
        self.function_button.setEnabled(
            monitor_id is not None and self._monitors[monitor_id]["function"] is not None)

    def _remove_selected(self):
        monitor_id = self._selected_monitor()
        if monitor_id is not None:
            self._worker.remove(monitor_id)

    def _on_removed(self, monitor_id):
        row = self._row_of(monitor_id)
        if row is None:
            return
        del self._monitors[monitor_id]
        self.functions_table.removeRow(row)
        for state in self._monitors.values():
            if state["row"] > row:
                state["row"] -= 1
        self._update_buttons()

    def _on_command_failed(self, action, message):
        self.status_label.setText(self.translations["trace.failed"].format(
            message=f"{action}: {message}"))

    def _on_worker_failed(self, message):
        self.add_button.setEnabled(False)
        self.status_label.setText(self.translations["native_monitor.worker_failed"].format(
            message=message))

    # --- Calls -------------------------------------------------------------------
    def _on_calls(self, calls, counts):
        self.log.platform = self._worker.platform
        self.log.arch = self._worker.arch
        for monitor_id, numbers in counts.items():
            row = self._row_of(monitor_id)
            if row is not None:
                self.functions_table.item(row, _F_CALLS).setText(str(numbers.get("calls", 0)))
                self.functions_table.item(row, _F_SKIPPED).setText(
                    str(numbers.get("skipped", 0)))
        if not calls or self.pause_button.isChecked():
            return
        self._calls.extend(calls)
        del self._calls[:-MAX_CALLS]
        self.log.append([call for call in calls if self._shown(call)])

    def _shown(self, call):
        return call.get("changes") or not self.changes_check.isChecked()

    def _show_calls(self):
        self.log.set_calls([call for call in self._calls if self._shown(call)])

    def _clear(self):
        self._calls = []
        self.log.set_calls([])

    def _show_call(self, current, _previous=None):
        call = self.log.call_at(current.row()) if current.isValid() else None
        self._fill_values(call)
        self._fill_changes(call)
        self._memory_address = None
        self.memory_button.setEnabled(False)
        if call is None or "registers" in call:
            self.selection_label.setText("" if call is None else
                                         self.log.names.get(call.get("monitorId"), "?"))
            return
        self.selection_label.setText(
            f"{self.log.names.get(call.get('monitorId'), '?')} ← {caller_text(call)}"
            f"  ({call.get('callerWhere') or call.get('caller', '')})")

    def _fill_values(self, call):
        table = self.values_table
        table.setRowCount(0)
        if call is None:
            return
        platform, arch = self._worker.platform, self._worker.arch
        if "registers" in call:
            rows = [tuple(register) for register in call["registers"]]
        else:
            rows = [(argument_name(index, platform, arch), value)
                    for index, value in enumerate(call.get("args", []))]
            rows.append((self.translations["monitor.column_result"], call["result"]))
        for name, value in rows:
            number = int(value["value"], 16)
            meaning = (f'"{value["text"]}"' if value.get("text") else value.get("where", ""))
            row = table.rowCount()
            table.insertRow(row)
            pointer = number if _SMALL <= number < _TOP else None
            table.setItem(row, _V_NAME, _item(name))
            table.setItem(row, _V_VALUE, _item(value["value"], pointer))
            table.setItem(row, _V_NUMBER, _item(str(signed(value["value"]))))
            table.setItem(row, _V_MEANING, _item(meaning, tooltip=meaning))
        if "registers" in call:
            return
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, _V_NAME, _item(self.translations["monitor.column_caller"]))
        table.setItem(row, _V_VALUE, _item(call.get("caller", ""),
                                           int(call.get("caller", "0x0"), 16)))
        table.setItem(row, _V_NUMBER, _item(""))
        caller = " · ".join(text for text in (call.get("callerSymbol"),
                                              call.get("callerWhere")) if text)
        table.setItem(row, _V_MEANING, _item(caller, tooltip=caller))

    def _fill_changes(self, call):
        table = self.changes_table
        table.setRowCount(0)
        if call is None:
            return
        platform, arch = self._worker.platform, self._worker.arch
        for change in call.get("changes", []):
            row = table.rowCount()
            table.insertRow(row)
            address = int(change["address"], 16)
            numbers = change_numbers(change["before"], change["after"])
            table.setItem(row, _X_ARGUMENT, _item(argument_name(change["arg"], platform, arch)))
            table.setItem(row, _X_OFFSET, _item(f"+0x{change['offset']:x}"))
            table.setItem(row, _X_ADDRESS, _item(change["address"], address))
            table.setItem(row, _X_BEFORE, _item(change["before"]))
            table.setItem(row, _X_AFTER, _item(change["after"]))
            table.setItem(row, _X_BYTES, _item(
                f"{byte_numbers(change['before'])} → {byte_numbers(change['after'])}"))
            table.setItem(row, _X_NUMBER, _item(
                "" if numbers is None else f"{numbers[0]} → {numbers[1]}"))
        if call.get("moreChanges"):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, _X_ARGUMENT, _item(self.translations["native_monitor.more_changes"]))

    # --- Memory ------------------------------------------------------------------
    def _pick_memory(self, table):
        """The address of the row selected in ``table`` (values or changes), if any."""
        address = None
        row = table.currentRow()
        if row >= 0:
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) is not None:
                    address = item.data(Qt.ItemDataRole.UserRole)
                    break
        self._memory_address = address
        self.memory_button.setEnabled(address is not None)

    def _request_memory(self):
        if self._memory_address is not None:
            self.memory_requested.emit(self._memory_address)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        # Removing the interceptors can wait for calls in progress; do not
        # block the window for that.
        worker = self._worker
        worker.requestInterruption()
        if worker.isRunning():
            keep_until_finished(worker)
        super().closeEvent(event)
