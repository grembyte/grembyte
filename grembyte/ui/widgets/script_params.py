"""Parameters of scripts: defining them (ParamEditor, in the script dialog)
and typing them in when a script is run by hand (ScriptRunDialog).

See ScriptParam in grembyte.features.scripts. A choice's options come from its
code in the game; the run dialog asks the script worker for them once per
session (``options_cache``) and shows them in a combo box that filters by
any part of a choice's text.
"""

from PySide6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from grembyte.core import messages
from grembyte.features.scripts import PARAM_KINDS, ScriptParam, choice_matches
from grembyte.hooks.syntax import BODY
from grembyte.ui.widgets.code_editor import CodeEditor
from grembyte.ui.widgets.dialog_buttons import dialog_button_row
from grembyte.ui.widgets.icons import set_button_icon, theme_color
from grembyte.ui.widgets.labels import DocsHint, WrappedLabel
from grembyte.ui.widgets.syntax_hint import SyntaxHint, syntax_problem

OPTIONS_EXAMPLE = """\
// The choices: values, or {label, value}
return [
  { label: "Strength (melee damage)", value: "Strength" },
  { label: "Dexterity (hit chance)", value: "Dexterity" },
];
"""


class ParamEditor(QWidget):
    """The parameters of a script: a row each (name, label, type, default)
    and, for the selected choice, the code that gives its options."""

    _COLUMNS = ("scripts.param_name", "scripts.param_label", "scripts.param_kind",
                "scripts.param_default")

    def __init__(self, translations, params=(), parent=None):
        super().__init__(parent)
        self.translations = translations
        # the options code of each row (the editor shows the selected row's)
        self._options = []
        self._shown = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        hint = DocsHint(translations, translations["scripts.params_help"], "guide/scripts/#parameters")
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels([translations[key] for key in self._COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(60)
        self.table.itemSelectionChanged.connect(self._show_options)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.btn_add = QPushButton(translations["scripts.param_add"])
        set_button_icon(self.btn_add, "add")
        self.btn_add.clicked.connect(lambda: self.add_param(ScriptParam(self._free_name())))
        self.btn_remove = QPushButton(translations["scripts.param_remove"])
        set_button_icon(self.btn_remove, "remove")
        self.btn_remove.clicked.connect(self._remove_selected)
        buttons.addWidget(self.btn_add)
        buttons.addWidget(self.btn_remove)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.options_label = WrappedLabel("")
        layout.addWidget(self.options_label)
        self.options_edit = CodeEditor("")
        self.options_edit.setMinimumHeight(60)
        layout.addWidget(self.options_edit, 1)
        self.options_hint = SyntaxHint(translations, self.options_edit, BODY)
        layout.addWidget(self.options_hint)

        for param in params:
            self.add_param(param)
        if params:
            self.table.selectRow(0)
        self._show_options()

    def _free_name(self):
        names = {self._cell(row, 0) for row in range(self.table.rowCount())}
        return next(f"param{n}" for n in range(1, len(names) + 2) if f"param{n}" not in names)

    def _cell(self, row, column):
        item = self.table.item(row, column)
        return "" if item is None else item.text()

    def _kind(self, row):
        combo = self.table.cellWidget(row, 2)
        return combo.currentData() if combo is not None else "text"

    def add_param(self, param):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(param.name))
        self.table.setItem(row, 1, QTableWidgetItem(param.label))
        kind = QComboBox()
        for name in PARAM_KINDS:
            kind.addItem(self.translations[f"scripts.param_kind_{name}"], name)
        kind.setCurrentIndex(PARAM_KINDS.index(param.kind))
        kind.currentIndexChanged.connect(self._show_options)
        self.table.setCellWidget(row, 2, kind)
        self.table.setItem(row, 3, QTableWidgetItem(param.default))
        self._options.append(param.options)
        self.table.selectRow(row)

    def _remove_selected(self):
        row = self._selected_row()
        if row is None:
            return
        self._shown = None  # its code goes with it
        self.table.removeRow(row)
        del self._options[row]
        self._show_options()

    def _selected_row(self):
        model = self.table.selectionModel()
        rows = model.selectedRows() if model is not None else []
        return rows[0].row() if rows else None

    def _keep_options(self):
        if self._shown is not None and self._shown < len(self._options):
            self._options[self._shown] = self.options_edit.toPlainText()

    def _show_options(self):
        """Show the selected choice's options code (keeping the one shown before)."""
        self._keep_options()
        row = self._selected_row()
        self.btn_remove.setEnabled(row is not None)
        choice = row is not None and self._kind(row) == "choice"
        self._shown = row if choice else None
        self.options_edit.setEnabled(choice)
        if choice:
            self.options_label.setText(self.translations["scripts.param_options"].format(
                name=self._cell(row, 0)))
            self.options_edit.setPlainText(self._options[row] or OPTIONS_EXAMPLE)
        else:
            self.options_label.setText(self.translations["scripts.param_options_none"])
            self.options_edit.setPlainText("")

    def params(self):
        """The parameters as ScriptParams; raises LocalizedValueError (or a
        ValueError with the options code's syntax problem)."""
        self._keep_options()
        params = []
        for row in range(self.table.rowCount()):
            kind = self._kind(row)
            options = self._options[row] if kind == "choice" else ""
            param = ScriptParam(self._cell(row, 0).strip(), self._cell(row, 1).strip(), kind,
                                self._cell(row, 3).strip(), options)
            if kind == "choice":
                problem = syntax_problem(self.translations, BODY, options)
                if problem is not None:
                    raise ValueError(f"{param.name}: {problem}")
            params.append(param)
        return tuple(params)


def option_text(option):
    """How a choice's option shows: its label, and its value where that differs."""
    label = option["label"]
    value = option["value"]
    return label if choice_matches(value, label) else f"{label}  ({value})"


class ScriptRunDialog(QDialog):
    """Type in the parameters of a script before running it: ``args`` (by
    name) once accepted.

    ``worker`` gives a choice's options (script_options), ``options_cache``
    keeps them by parameter name for the next run, ``last`` holds the values
    of the last run (else the defaults are shown).
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, translations, spec, *, worker, script_id, options_cache, last=None,
                 parent=None):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.translations = translations
        self.spec = spec
        self.args = None
        self._worker = worker
        self._script_id = script_id
        self._cache = options_cache
        self._last = last or {}
        self._inputs = {}
        self._requests = {}
        self.setWindowTitle(translations["scripts.run_title"].format(
            label=spec.label or translations["scripts.unnamed"]))
        self.resize(520, 120 + 44 * len(spec.params))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for param in spec.params:
            widget = self._input(param)
            self._inputs[param.name] = widget
            form.addRow(param.label or param.name, widget)
        layout.addLayout(form)
        self.status_label = WrappedLabel("")
        self.status_label.setStyleSheet(f"color:{theme_color('muted')};")
        layout.addWidget(self.status_label)
        self.error_label = WrappedLabel("")
        self.error_label.setStyleSheet(f"color:{theme_color('danger')};")
        layout.addWidget(self.error_label)
        layout.addStretch()
        buttons = dialog_button_row(translations, "scripts.run_button", self._accept, self.reject)
        if any(param.kind == "choice" for param in spec.params):
            reload_button = QPushButton(translations["scripts.options_reload"])
            set_button_icon(reload_button, "refresh")
            reload_button.clicked.connect(self._reload)
            buttons.insertWidget(0, reload_button)
        layout.addLayout(buttons)
        worker.options_listed.connect(self._on_options)
        worker.options_failed.connect(self._on_options_failed)
        for param in spec.params:
            if param.kind == "choice":
                self._load(param)

    def _start_text(self, param):
        last = self._last.get(param.name)
        if last is None:
            return param.default
        if isinstance(last, bool):
            return "true" if last else "false"
        return str(last)

    def _input(self, param):
        if param.kind == "bool":
            box = QCheckBox()
            box.setChecked(param.value(self._start_text(param)))
            return box
        if param.kind == "choice":
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            completer = QCompleter(combo.model(), combo)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            combo.setCompleter(completer)
            combo.lineEdit().setPlaceholderText(self.translations["scripts.options_loading"])
            combo.setEnabled(False)
            return combo
        edit = QLineEdit(self._start_text(param))
        return edit

    # --- options of choices ----------------------------------------------
    def _load(self, param, fresh=False):
        options = None if fresh else self._cache.get(param.name)
        if options is not None:
            self._fill(param, options)
            return
        combo = self._inputs[param.name]
        combo.setEnabled(False)
        combo.clear()
        combo.lineEdit().setPlaceholderText(self.translations["scripts.options_loading"])
        request = self._worker.script_options(self._script_id, param.name)
        self._requests[request] = param
        self.status_label.setText(self.translations["scripts.options_loading"])

    def _reload(self):
        for param in self.spec.params:
            if param.kind == "choice":
                self._load(param, fresh=True)

    def _on_options(self, request, options):
        param = self._requests.pop(request, None)
        if param is None:
            return
        self._cache[param.name] = options
        self._fill(param, options)
        if not self._requests:
            self.status_label.setText("")

    def _on_options_failed(self, request, message):
        param = self._requests.pop(request, None)
        if param is None:
            return
        self.error_label.setText(self.translations["scripts.options_failed"].format(
            name=param.label or param.name, message=messages.describe(message)))
        if not self._requests:
            self.status_label.setText("")

    def _fill(self, param, options):
        combo = self._inputs[param.name]
        combo.clear()
        for option in options:
            combo.addItem(option_text(option), option["value"])
        wanted = self._start_text(param)
        index = next((i for i, option in enumerate(options)
                      if choice_matches(option["value"], wanted)), -1)
        combo.setCurrentIndex(index)
        if index < 0:
            combo.setEditText("")
        combo.lineEdit().setPlaceholderText(self.translations["scripts.options_search"].format(
            count=len(options)))
        combo.setEnabled(True)

    # --- the values ---------------------------------------------------------
    def _value(self, param):
        widget = self._inputs[param.name]
        if param.kind == "bool":
            return widget.isChecked()
        if param.kind == "choice":
            index = widget.findText(widget.currentText(), Qt.MatchFlag.MatchExactly)
            if not widget.isEnabled() or index < 0:
                raise ValueError(self.translations["scripts.options_pick"].format(
                    name=param.label or param.name))
            return widget.itemData(index)
        return param.value(widget.text())

    def done(self, result):
        """Closed: answers still on their way no longer concern it."""
        for signal, slot in ((self._worker.options_listed, self._on_options),
                             (self._worker.options_failed, self._on_options_failed)):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass
        super().done(result)

    def _accept(self):
        args = {}
        try:
            for param in self.spec.params:
                args[param.name] = self._value(param)
        except ValueError as error:
            self.error_label.setText(messages.describe(error))
            return
        self.args = args
        self.accept()
