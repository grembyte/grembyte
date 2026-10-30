"""Call a static C# method of the running game with arguments typed in."""

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

from hermes.features.method_call import (
    BOOLEAN,
    CHAR,
    INTEGER_RANGES,
    REFERENCES,
    R4,
    R8,
    STRING,
    ArgumentError,
    argument_text,
    is_guid,
    parse_argument,
    supported,
)
from hermes.features.monitor import MethodMonitorWorker
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.inspection import Inspections
from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.method_monitor import stop_worker

# The arguments last typed per method (its signature), for this run of Hermes
_last_arguments = {}


def placeholder_key(param_type):
    """The translation of the hint an empty argument field shows."""
    # pylint: disable=too-many-return-statements
    kind = param_type["kind"]
    if not supported(param_type):
        return "call.placeholder_struct"
    if is_guid(param_type):
        return "call.placeholder_guid"
    if param_type.get("enumName"):
        return "call.placeholder_enum"
    if kind == STRING:
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
    """Call a static method of the game, e.g. ``Scripts:GivePlayerMoney (int)``.

    Not modal. The call runs on the game's main thread, like the property
    getters of the object inspector (see hermes.features.monitor); its result
    or exception goes to the log below. An object it returns opens in the
    object inspector, and its log line links to it. One method at a time:
    ``set_method`` shows another one.
    """

    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    # a field of an inspected object to put on the address list (see ObjectInspectorDialog)
    address_requested = pyqtSignal(object)

    def __init__(self, translations, pid, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.method = None
        self._params = None
        self._editors = []
        self._describe_request = None
        # call request id -> (the log line's start, the method's name)
        self._calls = {}
        # objects the calls returned, by their number in the log's links
        self._results = []
        self._failed = False
        self.setWindowTitle(translations["call.title"])
        self.resize(640, 480)
        self._build_ui()
        self._worker = MethodMonitorWorker(pid)
        self._worker.call_described.connect(self._on_described)
        self._worker.called.connect(self._on_called)
        self._worker.call_failed.connect(self._on_call_failed)
        self._worker.worker_failed.connect(self._on_worker_failed)
        self.inspections = Inspections(translations, self._worker, self,
                                       time=lambda: datetime.now().strftime("%H:%M:%S"))
        self.inspections.address_requested.connect(self.address_requested.emit)
        self._worker.start()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        hint = QLabel(t["call.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)
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
        """Show ``method`` (a static CatalogMethod) and read its parameters."""
        self.method = method
        self._params = None
        self._clear_form()
        self.signature_label.setText(method.signature)
        self.call_button.setEnabled(False)
        if self._failed:
            return
        self.status_label.setText(self.translations["call.reading"])
        self._describe_request = self._worker.describe_call(method.identity())

    def _clear_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self._editors = []

    def _on_described(self, request_id, info):
        if request_id != self._describe_request:
            return  # another method was chosen since
        self._describe_request = None
        self._params = info.get("params", [])
        last = _last_arguments.get(self.method.signature, [])
        for i, param in enumerate(self._params):
            self.form.addRow(self._param_label(param), self._editor(
                param["type"], last[i] if i < len(last) else None))
        if not self._params:
            self.form.addRow(QLabel(self.translations["call.no_parameters"]))
        usable = all(supported(param["type"]) for param in self._params)
        self.call_button.setEnabled(usable)
        self.status_label.setText("" if usable else self.translations["call.unsupported"])
        if self._editors and isinstance(self._editors[0], QLineEdit):
            self._editors[0].setFocus()

    def _param_label(self, param):
        param_type = param["type"]
        name = param_type.get("enumName") or param_type["name"]
        if param_type.get("byref"):
            name += " (ref)"
        return f"{param['name']}  ({name})"

    def _editor(self, param_type, last):
        t = self.translations
        if param_type["kind"] == BOOLEAN and not param_type.get("byref"):
            editor = QCheckBox()
            editor.setChecked(last == "true")
        else:
            editor = QLineEdit(last or "")
            editor.setPlaceholderText(t[placeholder_key(param_type)].format(
                type=param_type.get("enumName") or param_type["name"]))
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
        texts = self._texts()
        arguments = []
        for param, text in zip(self._params, texts):
            try:
                arguments.append(parse_argument(param["type"], text))
            except ArgumentError as error:
                self.status_label.setText(self.translations["call.bad_argument"].format(
                    name=param["name"],
                    message=self.translations[error.key].format(**error.values)))
                return
        _last_arguments[self.method.signature] = texts
        name = self.method.signature.split(" (")[0]
        line = (f"{datetime.now().strftime('%H:%M:%S')}  {name}("
                f"{', '.join(argument_text(value) for value in arguments)})")
        self._calls[self._worker.call(self.method.identity(), arguments)] = (line, name)
        self.call_button.setEnabled(False)
        self.status_label.setText(self.translations["call.calling"])

    def _on_called(self, request_id, answer):
        line, name = self._calls.pop(request_id, (None, None))
        if line is None:
            return
        t = self.translations
        if "exception" in answer:
            self._log(f"{line}  ⚠ {answer['exception']}")
            self.status_label.setText(t["call.threw"])
        else:
            result = answer.get("result")
            target = result.get("target") if result else None
            self._log(f"{line}  → {result.get('text', '') if result else t['call.void']}",
                      target, f"{name}()")
            self.status_label.setText(t["call.done"])
            if target:
                self.inspections.inspect(target, name=f"{name}()")
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
        stop_worker(self._worker)
        super().closeEvent(event)
