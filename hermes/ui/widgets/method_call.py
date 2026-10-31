"""Call a method of the running game with arguments typed in: a static C# method
or UFunction, an instance method of an inspected object, or (Unreal) an
instance UFunction of the catalog on a live object of its class."""

import html
from datetime import datetime

from PyQt6.QtCore import pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QFontDatabase  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from hermes.core import messages
from hermes.features.method_call import (
    CHAR,
    ENGINE_NAME,
    INTEGER_RANGES,
    REFERENCES,
    R4,
    R8,
    ArgumentError,
    argument_text,
    is_bool,
    is_guid,
    is_number_struct,
    is_output,
    is_text,
    parse_argument,
    struct_field_names,
    supported,
)
from hermes.features.monitor import MethodMonitorWorker
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.inspection import Inspections
from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.method_monitor import stop_worker

# The arguments last typed per method (its signature), for this run of Hermes
_last_arguments = {}
# How many of an enum's names the empty argument field shows
_PLACEHOLDER_NAMES = 4


def placeholder_key(param_type):
    """The translation of the hint an empty argument field shows."""
    # pylint: disable=too-many-return-statements
    kind = param_type["kind"]
    if not supported(param_type):
        return ("call.placeholder_unsupported" if "engine" in param_type
                else "call.placeholder_struct")
    if is_guid(param_type):
        return "call.placeholder_guid"
    if is_number_struct(param_type):
        return "call.placeholder_struct_fields"
    if param_type.get("enumValues"):
        return "call.placeholder_enum_values"
    if param_type.get("enumName"):
        return "call.placeholder_enum"
    if param_type.get("engine") == ENGINE_NAME:
        return "call.placeholder_name"
    if is_text(param_type):
        return "call.placeholder_string"
    if kind == CHAR:
        return "call.placeholder_char"
    if kind in (R4, R8):
        return "call.placeholder_float"
    if kind in INTEGER_RANGES:
        return "call.placeholder_integer"
    if kind in REFERENCES:
        return "call.placeholder_object"
    return "call.placeholder_address"


class MethodCallDialog(QDialog):
    """Call a static method of the game, e.g. ``Scripts:GivePlayerMoney (int)``,
    or an instance method on an object of the object inspector.

    Not modal. The call runs on the game's main thread, like the property
    getters of the object inspector (see hermes.features.monitor); its result
    or exception goes to the log below, with the values of outputs (Unreal).
    An object it returns opens in the object inspector, and its log line links
    to it. One method at a time: ``set_method`` (static, or an instance
    method of an engine that finds objects: then an object field comes first)
    or ``set_instance_method`` shows another one.

    The dialog runs a MethodMonitorWorker of its own for ``pid``, or uses
    ``worker`` (one of an object inspector's window, which keeps it running).
    ``call_done`` tells when a call returned without an exception.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # a field of an inspected object to put on the address list (see ObjectInspectorDialog)
    address_requested = pyqtSignal(object)
    call_done = pyqtSignal()

    def __init__(self, translations, pid=None, parent=None, worker=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.method = None
        # what is called: {key, name, describe(), call(arguments), found?}
        self._spec = None
        self._params = None
        # the parameters typed in (not outputs) and their editors
        self._inputs = []
        self._editors = []
        # where the object of a found call is typed in (see set_method)
        self.object_edit = None
        self._describe_request = None
        # call request id -> (the log line's start, the method's name)
        self._calls = {}
        # objects the calls returned, by their number in the log's links
        self._results = []
        self._failed = False
        self.setWindowTitle(translations["call.title"])
        self.resize(640, 480)
        self._build_ui()
        self._owns_worker = worker is None
        self._worker = MethodMonitorWorker(pid) if worker is None else worker
        self._worker.call_described.connect(self._on_described)
        self._worker.called.connect(self._on_called)
        self._worker.call_failed.connect(self._on_call_failed)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self.inspections = Inspections(translations, self._worker, self,
                                       time=lambda: datetime.now().strftime("%H:%M:%S"))
        self.inspections.address_requested.connect(self.address_requested.emit)
        if self._owns_worker:
            self._worker.start()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        self.hint_label = QLabel(t["call.hint"])
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #8b949e;")
        layout.addWidget(self.hint_label)
        self.signature_label = SelectableText()
        layout.addWidget(self.signature_label)

        self.form_widget = QWidget()
        self.form = QFormLayout(self.form_widget)
        self.form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.form_widget)

        actions = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)
        self.call_button = QPushButton(t["call.call"])
        self.call_button.setObjectName("btn_primary")
        set_button_icon(self.call_button, "call_made")
        self.call_button.setToolTip(t["call.call_tooltip"])
        self.call_button.setEnabled(False)
        self.call_button.clicked.connect(self.call)
        actions.addWidget(self.call_button)
        layout.addLayout(actions)

        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.anchorClicked.connect(self._on_link)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setPlaceholderText(t["call.log_placeholder"])
        layout.addWidget(self.log, 1)

    # --- The method --------------------------------------------------------------
    def set_method(self, method):
        """Show ``method`` (a CatalogMethod) and read its parameters.

        An instance method runs on a live object of its class (an engine with
        ``CALL_FOUND``): the first one found, or the one whose name or
        address is typed in above the arguments.
        """
        self.method = method
        if method.static:
            self.hint_label.setText(self.translations["call.hint"])
            self._show({
                "key": method.signature, "text": method.signature,
                "name": method.signature.split(" (")[0],
                "describe": lambda: self._worker.describe_call(method.identity()),
                "call": lambda arguments: self._worker.call(method.identity(), arguments),
            })
            return
        self.hint_label.setText(self.translations["call.hint_found"].format(
            type=method.class_name))
        self._show({
            "key": method.signature, "text": method.signature,
            "name": method.signature.split(" (")[0], "found": method.class_name,
            "describe": lambda: self._worker.describe_call(method.identity(), instance=True),
            "call": lambda arguments: self._worker.call_found(
                method.identity(), method.class_name, self.object_edit.text(), arguments),
        })

    def set_instance_method(self, target, entry, object_text):
        """Show an instance method (an entry of ``list_methods``: name, method,
        owner, params, returns) to call on the object ``target``, shown as
        ``object_text``."""
        self.method = None
        self.hint_label.setText(self.translations["call.hint_instance"].format(
            object=object_text))
        signature = f"{entry['owner']}:{entry['name']} ({entry['params']})"
        self._show({
            "key": signature, "text": f"{signature} → {entry['returns']}",
            "name": entry["name"],
            "describe": lambda: self._worker.describe_method(entry["method"]),
            "call": lambda arguments: self._worker.call_on(target, entry["method"], arguments),
        })

    def _show(self, spec):
        """Show the method ``spec`` describes and read its parameters."""
        self._spec = spec
        self._params = None
        self._clear_form()
        self.signature_label.setText(spec["text"])
        self.call_button.setEnabled(False)
        if self._failed:
            return
        self.status_label.setText(self.translations["call.reading"])
        self._describe_request = spec["describe"]()

    def _clear_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self._inputs = []
        self._editors = []
        self.object_edit = None

    def _on_described(self, request_id, info):
        if request_id != self._describe_request:
            return  # another method was chosen since
        self._describe_request = None
        t = self.translations
        self._params = info.get("params", [])
        last = _last_arguments.get(self._spec["key"], [])
        if "found" in self._spec:
            # The object comes first; its text is kept with the arguments.
            self.object_edit = QLineEdit(last[0] if last else "")
            self.object_edit.setPlaceholderText(t["call.placeholder_found"].format(
                type=self._spec["found"]))
            self.object_edit.returnPressed.connect(self.call)
            self.form.addRow(t["call.object"].format(type=self._spec["found"]),
                             self.object_edit)
            last = last[1:]
        self._inputs = [param for param in self._params if not is_output(param)]
        for param in self._params:
            if is_output(param):
                hint = QLabel(t["call.output"])
                hint.setStyleSheet("color: #8b949e;")
                self.form.addRow(self._param_label(param), hint)
                continue
            i = len(self._editors)
            self.form.addRow(self._param_label(param), self._editor(
                param["type"], last[i] if i < len(last) else None))
        if not self._params:
            self.form.addRow(QLabel(t["call.no_parameters"]))
        usable = all(supported(param["type"]) for param in self._inputs)
        self.call_button.setEnabled(usable)
        self.status_label.setText("" if usable else t["call.unsupported"])
        first = self.object_edit or (self._editors[0] if self._editors else None)
        if isinstance(first, QLineEdit):
            first.setFocus()

    def _param_label(self, param):
        param_type = param["type"]
        name = param_type.get("enumName") or param_type["name"]
        if param.get("out"):
            name += " (out)"
        elif param_type.get("byref"):
            name += " (ref)"
        return f"{param['name']}  ({name})"

    def _editor(self, param_type, last):
        t = self.translations
        if is_bool(param_type) and ("engine" in param_type or not param_type.get("byref")):
            editor = QCheckBox()
            editor.setChecked(last == "true")
        else:
            editor = QLineEdit(last or "")
            values = param_type.get("enumValues") or {}
            fields = struct_field_names(param_type) if is_number_struct(param_type) else []
            editor.setPlaceholderText(t[placeholder_key(param_type)].format(
                type=param_type.get("enumName") or param_type["name"],
                values=", ".join(list(values.values())[:_PLACEHOLDER_NAMES])
                + (", …" if len(values) > _PLACEHOLDER_NAMES else ""),
                fields=" ".join(fields),
                fields_example="; ".join(f"{name}={i + 1}" for i, name in enumerate(fields[:2]))))
            if values:
                editor.setToolTip("\n".join(f"{value} = {name}" for value, name in values.items()))
            editor.setEnabled(supported(param_type))
            editor.returnPressed.connect(self.call)
        self._editors.append(editor)
        return editor

    def _texts(self):
        return [("true" if editor.isChecked() else "false") if isinstance(editor, QCheckBox)
                else editor.text() for editor in self._editors]

    # --- Calling -----------------------------------------------------------------
    def call(self):
        """Call the method with the arguments typed in."""
        if self._params is None or not self.call_button.isEnabled():
            return
        texts = iter(self._texts())
        arguments = []
        typed = []
        for param in self._params:
            if is_output(param):
                arguments.append(None)
                continue
            text = next(texts)
            typed.append(text)
            try:
                arguments.append(parse_argument(param["type"], text))
            except ArgumentError as error:
                self.status_label.setText(self.translations["call.bad_argument"].format(
                    name=param["name"],
                    message=self.translations[error.key].format(**error.values)))
                return
        found = [self.object_edit.text()] if self.object_edit is not None else []
        _last_arguments[self._spec["key"]] = found + typed
        name = self._spec["name"]
        shown = [argument_text(value) for param, value in zip(self._params, arguments)
                 if not is_output(param)]
        line = f"{datetime.now().strftime('%H:%M:%S')}  {name}({', '.join(shown)})"
        self._calls[self._spec["call"](arguments)] = (line, name)
        self.call_button.setEnabled(False)
        self.status_label.setText(self.translations["call.calling"])

    def _on_called(self, request_id, answer):
        line, name = self._calls.pop(request_id, (None, None))
        if line is None:
            return
        t = self.translations
        if "exception" in answer:
            self._log(f"{line}  ⚠ {messages.describe(answer['exception'])}")
            self.status_label.setText(t["call.threw"])
        else:
            result = answer.get("result")
            target = result.get("target") if result else None
            if answer.get("object"):
                line += t["call.on_object"].format(object=answer["object"]["text"])
            text = f"{line}  → {result.get('text', '') if result else t['call.void']}"
            for output in answer.get("outputs", []):
                text += f"; {output['name']} = {output['value'].get('text', '')}"
            self._log(text, target, f"{name}()")
            self.status_label.setText(t["call.done"])
            if target:
                self.inspections.inspect(target, name=f"{name}()")
            self.call_done.emit()
        self.call_button.setEnabled(self._params is not None)

    def _on_call_failed(self, request_id, message):
        if request_id == self._describe_request:
            self._describe_request = None
            self.status_label.setText(self.translations["call.describe_failed"].format(
                message=message))
            return
        line, _name = self._calls.pop(request_id, (None, None))
        if line is not None:
            self._log(f"{line}  ✗ {message}")
            self.status_label.setText(self.translations["call.failed"].format(message=message))
            self.call_button.setEnabled(self._params is not None)

    def _on_worker_failed(self, message):
        self._failed = True
        self.call_button.setEnabled(False)
        self.status_label.setText(self.translations["call.failed"].format(message=message))

    def _log(self, text, target=None, name=""):
        """Add a line to the log; with ``target`` (an object) a link that inspects it."""
        line = f'<span style="white-space: pre-wrap">{html.escape(text)}</span>'
        if target:
            self._results.append((target, name))
            line += (f'  <a href="inspect:{len(self._results) - 1}">'
                     f'{html.escape(self.translations["call.inspect"])}</a>')
        self.log.append(line)

    def _on_link(self, url):
        kind, _, number = url.toString().partition(":")
        if kind == "inspect" and number.isdigit() and int(number) < len(self._results):
            target, name = self._results[int(number)]
            self.inspections.inspect(target, name=name)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        if self._owns_worker:
            stop_worker(self._worker)
        super().closeEvent(event)
