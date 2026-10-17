"""Dialog for maintaining hook templates (JavaScript manipulations)."""

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QFontDatabase  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.hooks.transforms import PARAMETER_NAME, TransformDefinition, TransformParameter
from hermes.hooks.sandbox import TransformSandbox
from hermes.ui.widgets.hooks import TransformForm, format_number, parse_number
from hermes.ui.widgets.labels import WrappedLabel
from hermes.ui.widgets.table import set_row_height


NEW_TEMPLATE_SOURCE = """function (ctx) {
  return ctx.new;
}"""

# Parameter table columns
_NAME, _LABEL, _KIND, _DEFAULT, _OPTIONAL, _CHOICES = range(6)
_KINDS = ("number", "choice")


def format_choices(parameter):
    """Return the choices of a parameter as ``value=Label; value=Label``."""
    return "; ".join(
        f"{choice}={parameter.choice_label(index)}"
        for index, choice in enumerate(parameter.choices)
    )


def parse_choices(text):
    """Parse ``value=Label; value`` into (values, labels)."""
    values, labels = [], []
    for part in text.split(";"):
        value, _, label = part.partition("=")
        value = value.strip()
        if value:
            values.append(value)
            labels.append(label.strip() or value)
    return tuple(values), tuple(labels)


class TemplateDialog(QDialog):
    """List, create, edit and delete hook templates.

    Hooks keep a copy of their template, so changes here do not alter existing
    hooks; the hook dialog offers to adopt a changed template.
    """

    # Widgets are created by the _build_* helpers called from __init__.
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    def __init__(self, translations, repository, parent=None):
        super().__init__(parent)
        self.translations = translations
        self._repository = repository
        self._templates = []
        self._current_id = None
        self._snapshot = None
        self.setWindowTitle(translations["templates.title"])
        self.resize(980, 640)
        self._build_ui()
        self._reload()
        if self._templates:
            self.template_list.setCurrentRow(0)
        else:
            self._load(None)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.template_list = QListWidget()
        self.template_list.currentRowChanged.connect(self._on_row_changed)
        left_layout.addWidget(self.template_list)
        buttons = QHBoxLayout()
        self.btn_new = QPushButton(self.translations["templates.new"])
        self.btn_new.clicked.connect(self._new_template)
        self.btn_delete = QPushButton(self.translations["templates.delete"])
        self.btn_delete.setObjectName("btn_danger")
        self.btn_delete.clicked.connect(self._delete_template)
        buttons.addWidget(self.btn_new)
        buttons.addWidget(self.btn_delete)
        left_layout.addLayout(buttons)
        splitter.addWidget(left)

        splitter.addWidget(self._build_editor())
        splitter.setSizes([240, 740])

        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        close_button = QPushButton(self.translations["templates.close"])
        close_button.clicked.connect(self.reject)
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

    def _build_editor(self):
        editor = QGroupBox()
        layout = QVBoxLayout(editor)

        form = QFormLayout()
        self.name_input = QLineEdit()
        form.addRow(self.translations["templates.name"], self.name_input)
        self.description_input = QLineEdit()
        self.description_input.setPlaceholderText(self.translations["hooks.optional"])
        form.addRow(self.translations["templates.description"], self.description_input)
        layout.addLayout(form)

        layout.addWidget(QLabel(self.translations["templates.parameters"]))
        self.param_table = QTableWidget(0, 6)
        self.param_table.setHorizontalHeaderLabels([
            self.translations["templates.column_name"],
            self.translations["templates.column_label"],
            self.translations["templates.column_kind"],
            self.translations["templates.column_default"],
            self.translations["templates.column_optional"],
            self.translations["templates.column_choices"],
        ])
        self.param_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.param_table.verticalHeader().setVisible(False)
        set_row_height(self.param_table)
        self.param_table.setToolTip(self.translations["templates.parameters_hint"])
        header = self.param_table.horizontalHeader()
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_CHOICES, QHeaderView.ResizeMode.Stretch)
        # ResizeToContents ignores cell widgets; size the type column for its combo box.
        header.setSectionResizeMode(_KIND, QHeaderView.ResizeMode.Fixed)
        metrics = self.param_table.fontMetrics()
        widest = max(
            metrics.horizontalAdvance(self.translations[f"templates.kind_{kind}"])
            for kind in _KINDS
        )
        # text + padding, drop-down arrow and cell margins
        self.param_table.setColumnWidth(_KIND, widest + 64)
        self.param_table.setMinimumHeight(140)
        layout.addWidget(self.param_table)
        param_buttons = QHBoxLayout()
        add_param = QPushButton(self.translations["templates.param_add"])
        add_param.clicked.connect(lambda: self._add_parameter_row(None))
        remove_param = QPushButton(self.translations["templates.param_remove"])
        remove_param.clicked.connect(self._remove_parameter_row)
        param_buttons.addWidget(add_param)
        param_buttons.addWidget(remove_param)
        param_buttons.addStretch()
        layout.addLayout(param_buttons)

        layout.addWidget(QLabel(self.translations["templates.source"]))
        self.source_input = QPlainTextEdit()
        self.source_input.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.source_input.setMinimumHeight(180)
        layout.addWidget(self.source_input, 1)
        layout.addWidget(WrappedLabel(self.translations["hooks.script_hint"]))

        self.error_label = WrappedLabel()
        self.error_label.setStyleSheet("color: #f85149;")
        layout.addWidget(self.error_label)

        save_row = QHBoxLayout()
        save_row.addStretch()
        self.btn_test = QPushButton(self.translations["templates.test"])
        self.btn_test.setToolTip(self.translations["templates.test_tooltip"])
        self.btn_test.clicked.connect(self._test_template)
        save_row.addWidget(self.btn_test)
        self.btn_save = QPushButton(self.translations["templates.save"])
        self.btn_save.setObjectName("btn_primary")
        self.btn_save.clicked.connect(self.save)
        save_row.addWidget(self.btn_save)
        layout.addLayout(save_row)
        return editor

    def _add_parameter_row(self, parameter):
        parameter = parameter or TransformParameter("", default=0)
        row = self.param_table.rowCount()
        self.param_table.insertRow(row)
        self.param_table.setItem(row, _NAME, QTableWidgetItem(parameter.name))
        self.param_table.setItem(row, _LABEL, QTableWidgetItem(parameter.label))
        kind = self._kind_combo()
        kind.setCurrentIndex(max(kind.findData(parameter.kind), 0))
        self.param_table.setCellWidget(row, _KIND, kind)
        default = "" if parameter.default is None else (
            format_number(parameter.default) if parameter.kind == "number"
            else str(parameter.default)
        )
        self.param_table.setItem(row, _DEFAULT, QTableWidgetItem(default))
        optional = QTableWidgetItem()
        optional.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        optional.setCheckState(
            Qt.CheckState.Checked if parameter.optional else Qt.CheckState.Unchecked
        )
        self.param_table.setItem(row, _OPTIONAL, optional)
        self.param_table.setItem(row, _CHOICES, QTableWidgetItem(format_choices(parameter)))

    def _kind_combo(self):
        combo = QComboBox()
        for value in _KINDS:
            combo.addItem(self.translations[f"templates.kind_{value}"], value)
        return combo

    def _remove_parameter_row(self):
        row = self.param_table.currentRow()
        if row >= 0:
            self.param_table.removeRow(row)

    # ------------------------------------------------------------------
    # Loading and state
    # ------------------------------------------------------------------
    def _reload(self, select_id=None):
        try:
            self._templates = self._repository.list_templates()
        except SQLAlchemyError as error:
            self._templates = []
            self.status_label.setText(
                self.translations["hooks.db_failed"].format(message=error)
            )
        self.template_list.blockSignals(True)
        self.template_list.clear()
        for definition in self._templates:
            item = QListWidgetItem(definition.name)
            item.setData(Qt.ItemDataRole.UserRole, definition.template_id)
            item.setToolTip(definition.description)
            self.template_list.addItem(item)
            if definition.template_id == select_id:
                self.template_list.setCurrentItem(item)
        self.template_list.blockSignals(False)
        self.btn_delete.setEnabled(bool(self._templates))

    def _load(self, definition):
        """Show ``definition`` in the editor; ``None`` starts a new template."""
        if definition is None:
            definition = TransformDefinition(
                self.translations["templates.new_name"], NEW_TEMPLATE_SOURCE
            )
        self._current_id = definition.template_id
        self.name_input.setText(definition.name)
        self.description_input.setText(definition.description)
        self.source_input.setPlainText(definition.source)
        self.param_table.setRowCount(0)
        for parameter in definition.parameters:
            self._add_parameter_row(parameter)
        self.error_label.setText("")
        self._snapshot = self._editor_state()

    def _editor_state(self):
        rows = []
        for row in range(self.param_table.rowCount()):
            cells = []
            for column in range(6):
                if column == _KIND:
                    cells.append(self.param_table.cellWidget(row, column).currentData())
                elif column == _OPTIONAL:
                    cells.append(
                        self.param_table.item(row, column).checkState()
                        == Qt.CheckState.Checked
                    )
                else:
                    item = self.param_table.item(row, column)
                    cells.append("" if item is None else item.text().strip())
            rows.append(tuple(cells))
        return (
            self.name_input.text().strip(),
            self.description_input.text().strip(),
            self.source_input.toPlainText(),
            tuple(rows),
        )

    def is_dirty(self):
        """Return True when the editor has unsaved changes."""
        return self._snapshot is not None and self._editor_state() != self._snapshot

    def _confirm_discard(self):
        """Ask what to do with unsaved changes; False means cancel."""
        if not self.is_dirty():
            return True
        answer = QMessageBox.question(
            self,
            self.translations["templates.unsaved_title"],
            self.translations["templates.unsaved_message"],
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def _select_row_of(self, template_id):
        self.template_list.blockSignals(True)
        row = next(
            (
                i for i, definition in enumerate(self._templates)
                if definition.template_id == template_id
            ),
            -1,
        )
        self.template_list.setCurrentRow(row)
        self.template_list.blockSignals(False)

    def _on_row_changed(self, row):
        if row < 0 or row >= len(self._templates):
            return
        target_id = self._templates[row].template_id
        if not self._confirm_discard():
            self._select_row_of(self._current_id)
            return
        # Saving reloads the list; look the clicked template up again.
        definition = next(
            (d for d in self._templates if d.template_id == target_id), None
        )
        if definition is not None:
            self._select_row_of(target_id)
            self._load(definition)

    def reject(self):  # pylint: disable=invalid-name
        """Close the dialog after asking about unsaved changes."""
        if self._confirm_discard():
            super().reject()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _new_template(self):
        if not self._confirm_discard():
            return
        self._select_row_of(None)
        self._load(None)
        self.name_input.setFocus()
        self.name_input.selectAll()

    def _delete_template(self):
        if self._current_id is None:
            self._new_template()
            return
        name = self.name_input.text().strip()
        answer = QMessageBox.question(
            self,
            self.translations["templates.title"],
            self.translations["templates.delete_confirm"].format(name=name),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repository.delete_template(self._current_id)
        except SQLAlchemyError as error:
            self.error_label.setText(self.translations["hooks.db_failed"].format(message=error))
            return
        self._snapshot = None
        self._reload()
        if self._templates:
            self.template_list.setCurrentRow(0)
        else:
            self._load(None)
        self.status_label.setText(self.translations["templates.deleted"].format(name=name))

    def editor_definition(self):  # pylint: disable=too-many-locals
        """Build a TransformDefinition from the editor; raises ValueError."""
        name, description, source, rows = self._editor_state()
        if not name:
            raise ValueError(self.translations["templates.name_required"])
        if not source.strip():
            raise ValueError(self.translations["templates.source_required"])
        parameters, names = [], set()
        for param_name, label, kind, default, optional, choices in rows:
            if not PARAMETER_NAME.match(param_name):
                raise ValueError(
                    self.translations["templates.param_name_invalid"].format(name=param_name)
                )
            if param_name in names:
                raise ValueError(
                    self.translations["templates.param_name_duplicate"].format(name=param_name)
                )
            names.add(param_name)
            values, labels = (), ()
            if kind == "choice":
                values, labels = parse_choices(choices)
                if not values:
                    raise ValueError(
                        self.translations["templates.param_choices_required"].format(
                            name=param_name
                        )
                    )
                if default and default not in values:
                    raise ValueError(
                        self.translations["templates.param_default_invalid"].format(
                            name=param_name
                        )
                    )
                value = default or values[0]
            elif default:
                try:
                    value = parse_number(default)
                except ValueError as error:
                    raise ValueError(
                        self.translations["templates.param_default_invalid"].format(
                            name=param_name
                        )
                    ) from error
            else:
                value = None
            parameters.append(TransformParameter(
                name=param_name,
                kind=kind,
                default=value,
                choices=values,
                optional=optional,
                label=label,
                choice_labels=labels,
            ))
        return TransformDefinition(
            name=name,
            source=source.strip(),
            parameters=tuple(parameters),
            description=description,
            template_id=self._current_id,
        )

    def _test_template(self):
        """Try the edited (possibly unsaved) template in the sandbox."""
        try:
            definition = self.editor_definition()
        except ValueError as error:
            self.error_label.setText(str(error))
            return
        self.error_label.setText("")
        TemplateTestDialog(self.translations, definition, self).exec()

    def save(self):
        """Save the edited template; returns True on success."""
        try:
            definition = self.editor_definition()
            template_id = self._repository.save_template(definition, self._current_id)
        except ValueError as error:
            message = str(error)
            if "already exists" in message:
                message = self.translations["templates.name_exists"].format(
                    name=self.name_input.text().strip()
                )
            self.error_label.setText(message)
            return False
        except SQLAlchemyError as error:
            self.error_label.setText(self.translations["hooks.db_failed"].format(message=error))
            return False
        self._reload(select_id=template_id)
        self._load(next(d for d in self._templates if d.template_id == template_id))
        self.status_label.setText(
            self.translations["templates.saved"].format(name=definition.name)
        )
        return True


class TemplateTestDialog(QDialog):
    """Run a template with sample values outside the target process.

    ``ctx.state`` is kept between runs until it is reset or the dialog closes.
    """

    # Widgets are created by _build_ui called from __init__.
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    VALUE_TYPES = ("int32", "int64", "float", "double")

    def __init__(self, translations, definition, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.definition = definition
        self.sandbox = TransformSandbox()
        self.setWindowTitle(
            translations["templates.test_title"].format(name=definition.name)
        )
        self.resize(560, 560)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.value_type_combo = QComboBox()
        for value_type in self.VALUE_TYPES:
            self.value_type_combo.addItem(value_type, value_type)
        form.addRow(self.translations["templates.test_value_type"], self.value_type_combo)
        self.old_input = QLineEdit("100")
        form.addRow(self.translations["templates.test_old"], self.old_input)
        self.new_input = QLineEdit("110")
        form.addRow(self.translations["templates.test_new"], self.new_input)
        layout.addLayout(form)

        if self.definition.parameters:
            params_box = QGroupBox(self.translations["templates.parameters"])
            params_layout = QVBoxLayout(params_box)
            self.params_form = TransformForm(self.definition, self.translations)
            params_layout.addWidget(self.params_form)
            layout.addWidget(params_box)
        else:
            self.params_form = TransformForm(self.definition, self.translations)

        buttons = QHBoxLayout()
        reset = QPushButton(self.translations["templates.test_reset"])
        reset.setToolTip(self.translations["templates.test_reset_tooltip"])
        reset.clicked.connect(self.reset_state)
        buttons.addWidget(reset)
        buttons.addStretch()
        self.btn_run = QPushButton(self.translations["templates.test_run"])
        self.btn_run.setObjectName("btn_primary")
        self.btn_run.clicked.connect(self.run)
        buttons.addWidget(self.btn_run)
        layout.addLayout(buttons)

        self.result_label = WrappedLabel("")
        layout.addWidget(self.result_label)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.output, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_button = QPushButton(self.translations["templates.close"])
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

    def _number(self, editor, key):
        try:
            return parse_number(editor.text().strip())
        except ValueError as error:
            raise ValueError(
                self.translations["hooks.invalid_number"].format(
                    name=self.translations[key]
                )
            ) from error

    def reset_state(self):
        """Forget ctx.state."""
        self.sandbox.reset_state()
        self.output.setPlainText("")
        self.result_label.setText(self.translations["templates.test_state_reset"])
        self.result_label.setStyleSheet("")

    def run(self):
        """Call the template once with the entered values and show the outcome."""
        try:
            old = self._number(self.old_input, "templates.test_old")
            new = self._number(self.new_input, "templates.test_new")
            params = self.params_form.create_transform().params
        except (TypeError, ValueError) as error:
            self._show(self.translations["templates.test_error"].format(message=error), True)
            return None
        value_type = self.value_type_combo.currentData()
        result = self.sandbox.run(self.definition.source, params, old, new, value_type)
        if result.error is not None:
            self._show(
                self.translations["templates.test_error"].format(message=result.error), True
            )
        elif result.kept:
            self._show(self.translations["templates.test_kept"].format(
                value=format_number(normalize_for_display(new, value_type))
            ), False)
        else:
            self._show(self.translations["templates.test_result"].format(
                old=format_number(old), new=format_number(new),
                value=format_number(result.value),
            ), False)
        lines = [f"{self.translations['templates.test_logs']}:"]
        lines += [f"  {line}" for line in result.logs] or ["  —"]
        lines += ["", f"ctx.state = {result.state}"]
        self.output.setPlainText("\n".join(lines))
        return result

    def _show(self, text, error):
        self.result_label.setText(text)
        self.result_label.setStyleSheet("color: #f85149;" if error else "color: #3fb950;")


def normalize_for_display(value, value_type):
    """Show an entered value the way the game would store it."""
    return int(value) if value_type.startswith("int") else float(value)
