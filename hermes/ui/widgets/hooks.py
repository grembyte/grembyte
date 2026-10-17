"""Dialogs and panel for creating and managing value hooks."""

import dataclasses

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor  # pylint: disable=import-error,no-name-in-module
from sqlalchemy.exc import SQLAlchemyError
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
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hermes.db.repository.hooks import hook_transform
from hermes.hooks.engine import MAX_CALLER_DEPTH, HookSpec, HookWorker
from hermes.hooks.transforms import (
    TemplateTransform,
    as_template_transform,
    example_templates,
)
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.hook_debug import HookDebugWindow
from hermes.ui.widgets.table import set_row_height


# Upper bound for joining a hook worker from the GUI thread.
HOOK_STOP_TIMEOUT_MS = 3000

_STATUS_COLORS = {
    "installing": "#d29922",
    "active": "#3fb950",
    "paused": "#8b949e",
    "failed": "#f85149",
    "error": "#f85149",
    "disconnected": "#8b949e",
}


def parse_number(text):
    """Parse an int (also hex, e.g. 0x10) or a float."""
    try:
        return int(text, 0)
    except ValueError:
        return float(text)


def format_number(value):
    """Format a number for an input field."""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class TransformForm(QWidget):
    """Input form generated from a transform definition's parameters."""

    def __init__(self, definition, translations, values=None, parent=None):
        super().__init__(parent)
        self.definition = definition
        self.translations = translations
        self._editors = {}
        values = values or {}

        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for parameter in definition.parameters:
            value = values.get(parameter.name, parameter.default)
            editor = self._create_editor(parameter, value)
            self._editors[parameter.name] = (parameter, editor)
            layout.addRow(parameter.label or parameter.name, editor)

        if definition.description:
            hint = QLabel(definition.description)
            hint.setWordWrap(True)
            layout.addRow(hint)

    def _create_editor(self, parameter, value):
        if parameter.kind == "choice":
            editor = QComboBox()
            for index, choice in enumerate(parameter.choices):
                editor.addItem(parameter.choice_label(index), choice)
            index = editor.findData(value)
            editor.setCurrentIndex(max(index, 0))
            return editor
        if parameter.kind == "code":
            editor = QPlainTextEdit("" if value is None else str(value))
            editor.setMinimumHeight(120)
            return editor
        editor = QLineEdit("" if value is None else format_number(value))
        if parameter.optional:
            editor.setPlaceholderText(self.translations["hooks.optional"])
        return editor

    def create_transform(self):
        """Build the transform; raises ValueError with a readable message."""
        values = {}
        for name, (parameter, editor) in self._editors.items():
            label = parameter.label or name
            if parameter.kind == "choice":
                values[name] = editor.currentData()
            elif parameter.kind == "code":
                values[name] = editor.toPlainText()
            else:
                text = editor.text().strip()
                if not text:
                    if not parameter.optional:
                        raise ValueError(
                            self.translations["hooks.required"].format(name=label)
                        )
                    values[name] = None
                    continue
                try:
                    values[name] = parse_number(text)
                except ValueError as error:
                    raise ValueError(
                        self.translations["hooks.invalid_number"].format(name=label)
                    ) from error
        return TemplateTransform(self.definition, **values)


class HookDialog(QDialog):
    # Dialogs expose their result through attributes after exec().
    # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Choose a hook template and its parameters for a write hook.

    ``templates`` are the TransformDefinitions to choose from (the example
    templates if omitted). When editing, the hook's own copy of its template
    is shown; if the template has changed since, the dialog offers to adopt it.
    """

    def __init__(self, translations, address, value_type, site,
                 transform=None, label="", templates=None, parent=None):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.translations = translations
        self.transform = None
        self.label = label
        self.caller_depth = 1
        self._editing = transform is not None
        if templates is None:
            templates = list(example_templates(translations).values())
        self._options, self._values, self._copy_index, self._template = (
            self._build_options(templates, transform)
        )
        self.setWindowTitle(
            translations["hooks.edit_title" if self._editing else "hooks.create_title"]
        )
        self.resize(520, 420)
        self._build_ui(address, value_type, site, label)

    @staticmethod
    def _build_options(templates, transform):
        """Return the definitions to offer, the hook's values and copy index.

        The hook's copy replaces the template it was created from; a copy whose
        template no longer exists is offered in addition.
        """
        options = list(templates)
        if transform is None:
            return options, {}, None, None
        current = as_template_transform(transform)
        copy = current.definition
        index = next(
            (
                i for i, option in enumerate(options)
                if copy.template_id is not None and option.template_id == copy.template_id
            ),
            None,
        )
        template = None
        if index is None:
            options.insert(0, copy)
            index = 0
        else:
            template = options[index]
            options[index] = copy
        return options, current.form_values(), index, template

    def _build_ui(self, address, value_type, site, label):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            self.translations["hooks.dialog_target"].format(
                address=address, type=value_type, site=site
            )
        )
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)

        form = QFormLayout()
        self.label_input = QLineEdit(label)
        self.label_input.setPlaceholderText(self.translations["hooks.label_placeholder"])
        form.addRow(self.translations["hooks.label"], self.label_input)
        if not self._editing:
            self.caller_depth_input = QSpinBox()
            self.caller_depth_input.setRange(0, MAX_CALLER_DEPTH)
            self.caller_depth_input.setValue(self.caller_depth)
            self.caller_depth_input.setSpecialValueText(
                self.translations["hooks.caller_depth_off"]
            )
            self.caller_depth_input.setToolTip(self.translations["hooks.caller_depth_hint"])
            form.addRow(self.translations["hooks.caller_depth"], self.caller_depth_input)
        self.template_combo = QComboBox()
        form.addRow(self.translations["hooks.template"], self.template_combo)
        layout.addLayout(form)

        self.template_notice = QWidget()
        notice_layout = QHBoxLayout(self.template_notice)
        notice_layout.setContentsMargins(0, 0, 0, 0)
        self.template_notice_label = QLabel()
        self.template_notice_label.setWordWrap(True)
        self.template_notice_label.setStyleSheet("color: #d29922;")
        self.btn_adopt_template = QPushButton(self.translations["hooks.template_adopt"])
        self.btn_adopt_template.clicked.connect(self._adopt_template)
        notice_layout.addWidget(self.template_notice_label, 1)
        notice_layout.addWidget(self.btn_adopt_template)
        layout.addWidget(self.template_notice)

        self.forms = QStackedWidget()
        for index, definition in enumerate(self._options):
            self.template_combo.addItem(definition.name, index)
            values = self._values if index == self._copy_index else None
            self.forms.addWidget(TransformForm(definition, self.translations, values))
        self.template_combo.currentIndexChanged.connect(self._on_template_selected)
        if self._copy_index is not None:
            self.template_combo.setCurrentIndex(self._copy_index)
        layout.addWidget(self.forms)
        layout.addStretch()
        self._update_template_notice()

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #f85149;")
        layout.addWidget(self.error_label)

        layout.addLayout(dialog_button_row(
            self.translations,
            "hooks.apply" if self._editing else "hooks.create",
            self._accept,
            self.reject,
        ))

    def template_state(self):
        """Return how the hook's copy relates to its template.

        ``"changed"``, ``"deleted"`` or ``None`` (unchanged, or not editing).
        """
        if self._copy_index is None:
            return None
        copy = self._options[self._copy_index]
        if copy.template_id is None:
            return None
        if self._template is None:
            return "deleted"
        return "changed" if self._template != copy else None

    def _on_template_selected(self, index):
        self.forms.setCurrentIndex(index)
        self._update_template_notice()

    def _update_template_notice(self):
        state = self.template_state()
        visible = state is not None and self.template_combo.currentIndex() == self._copy_index
        self.template_notice.setVisible(visible)
        if not visible:
            return
        name = self._options[self._copy_index].name
        if state == "changed":
            self.template_notice_label.setText(
                self.translations["hooks.template_changed"].format(
                    name=self._template.name
                )
            )
        else:
            self.template_notice_label.setText(
                self.translations["hooks.template_deleted"].format(name=name)
            )
        self.btn_adopt_template.setVisible(state == "changed")

    def _adopt_template(self):
        """Replace the hook's copy by the current template, keeping values."""
        index = self._copy_index
        if index is None or self._template is None:
            return
        old_form = self.forms.widget(index)
        values = dict(self._values)
        try:
            values.update(old_form.create_transform().form_values())
        except (TypeError, ValueError):
            pass
        names = {parameter.name for parameter in self._template.parameters}
        values = {name: value for name, value in values.items() if name in names}
        self._options[index] = self._template
        new_form = TransformForm(self._template, self.translations, values)
        self.forms.insertWidget(index, new_form)
        self.forms.removeWidget(old_form)
        old_form.deleteLater()
        self.forms.setCurrentIndex(index)
        self.template_combo.setItemText(index, self._template.name)
        self._update_template_notice()

    def _accept(self):
        try:
            self.transform = self.forms.currentWidget().create_transform()
        except (TypeError, ValueError) as error:
            self.error_label.setText(str(error))
            return
        self.label = self.label_input.text().strip()
        if not self._editing:
            self.caller_depth = self.caller_depth_input.value()
        self.accept()


def persistable_callers(callers):
    """Keep the caller signatures whose frames all lie inside a module."""
    return [
        [tuple(frame) for frame in frames]
        for frames in callers
        if all(frame is not None for frame in frames)
    ]


def describe_callers(callers):
    """Return a readable list of caller signatures."""
    return "\n".join(
        " ← ".join(f"{module}+0x{offset:x}" for module, offset in frames)
        for frames in callers
    )


def spec_from_saved(saved):
    """Build the HookSpec that reinstalls a hook saved in the database."""
    return HookSpec(
        None,
        saved.value_type,
        hook_transform(saved),
        module=saved.module,
        module_offset=saved.module_offset,
        instruction_bytes=saved.instruction_bytes,
        enabled=saved.enabled,
        caller_depth=saved.caller_depth,
        callers=persistable_callers(saved.callers),
        label=saved.label,
    )


class HookPanel(QGroupBox):
    """Table of active hooks; owns the HookWorker of the attached process.

    With a ``repository`` new hooks are saved as module + offset and the saved
    hooks of a process are reinstalled whenever it is attached again.
    """

    # pylint: disable=too-many-instance-attributes

    count_changed = pyqtSignal(int)

    _COLUMNS = (
        "hooks.column_label",
        "hooks.column_address",
        "hooks.column_site",
        "hooks.column_transform",
        "hooks.column_status",
        "hooks.column_count",
        "hooks.column_last",
    )

    def __init__(self, translations, parent=None, *, repository=None):
        super().__init__(translations["hooks.title"], parent)
        self.translations = translations
        self._repository = repository
        self._pid = None
        self._process_id = None
        self._debug_window = None
        self._worker = None
        self._hooks = {}
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        self.btn_edit = QPushButton(self.translations["hooks.edit"])
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_toggle = QPushButton(self.translations["hooks.pause"])
        self.btn_toggle.clicked.connect(self._toggle_selected)
        self.btn_debug = QPushButton(self.translations["hooks.debug"])
        self.btn_debug.setCheckable(True)
        self.btn_debug.setToolTip(self.translations["hooks.debug_tooltip"])
        self.btn_debug.clicked.connect(self._toggle_debug_selected)
        self.btn_remove = QPushButton(self.translations["hooks.remove"])
        self.btn_remove.setObjectName("btn_danger")
        self.btn_remove.clicked.connect(self._remove_selected)
        controls.addWidget(self.btn_edit)
        controls.addWidget(self.btn_toggle)
        controls.addWidget(self.btn_debug)
        self.status_label = QLabel(self.translations["hooks.hint"])
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        controls.addWidget(self.btn_remove)
        layout.addLayout(controls)

        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(
            [self.translations[key] for key in self._COLUMNS]
        )
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
                header.setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents
                )
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(self._edit_selected)
        layout.addWidget(self.table)
        self._update_buttons()

    def _row_of(self, hook_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == hook_id:
                return row
        return -1

    def _selected_hook_id(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.selectionModel().hasSelection():
            return None
        item = self.table.item(row, 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _set_cell(self, hook_id, column, text, color=None, tooltip=None):
        row = self._row_of(hook_id)
        if row < 0:
            return
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, column, item)
        item.setText(text)
        if color is not None:
            item.setForeground(QColor(color))
        if tooltip is not None:
            item.setToolTip(tooltip)

    def _set_status(self, hook_id, status, tooltip=""):
        state = self._hooks.get(hook_id)
        if state is None:
            return
        state["status"] = status
        self._set_cell(
            hook_id,
            4,
            self.translations[f"hooks.status_{status}"],
            _STATUS_COLORS[status],
            tooltip,
        )
        self._update_buttons()

    def _update_buttons(self):
        state = self._hooks.get(self._selected_hook_id())
        live = state is not None and state["status"] in {"active", "paused", "error"}
        self.btn_edit.setEnabled(live)
        self.btn_toggle.setEnabled(live)
        self.btn_debug.setEnabled(live)
        self.btn_debug.setChecked(state is not None and state.get("debug", False))
        self.btn_remove.setEnabled(state is not None)
        self.btn_toggle.setText(
            self.translations[
                "hooks.resume" if state is not None and state["status"] == "paused"
                else "hooks.pause"
            ]
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_process(self, pid, process_id=None):
        """Switch to another process; hooks of the previous one are removed.

        ``process_id`` is the database id of the program; its saved hooks are
        installed right away.
        """
        if pid == self._pid:
            return
        self.shutdown()
        self._pid = pid
        self._process_id = process_id
        self._hooks.clear()
        self.table.setRowCount(0)
        self.status_label.setText(self.translations["hooks.hint"])
        self._update_buttons()
        self.count_changed.emit(0)
        self._update_debug_hooks()
        self._restore_saved_hooks()

    def add_hook(self, spec, site, saved_id=None):
        """Install a hook described by ``spec`` in the current process.

        ``saved_id`` is the database id of a restored hook; hooks without one
        are saved once they are installed.
        """
        worker = self._ensure_worker()
        hook_id = worker.add_hook(spec)
        self._hooks[hook_id] = {
            "spec": spec, "status": "installing", "count": 0, "saved_id": saved_id,
        }

        row = self.table.rowCount()
        self.table.insertRow(row)
        label_item = QTableWidgetItem(self._label_text(spec, False))
        label_item.setData(Qt.ItemDataRole.UserRole, hook_id)
        self.table.setItem(row, 0, label_item)
        self._set_cell(hook_id, 1, self._address_text(spec), "#3fb950")
        self._show_callers(hook_id)
        self._set_cell(hook_id, 2, site)
        self._set_cell(hook_id, 3, spec.transform.describe(), "#f0883e")
        self._set_cell(hook_id, 5, "0")
        self._set_cell(hook_id, 6, "")
        self._set_status(hook_id, "installing")
        self.table.selectRow(row)
        self.count_changed.emit(len(self._hooks))
        self._update_debug_hooks()
        return hook_id

    def shutdown(self, timeout_ms=HOOK_STOP_TIMEOUT_MS):
        """Remove all hooks and stop the worker.

        A ``timeout_ms`` of ``None`` waits until the worker has finished;
        otherwise it finishes in the background after the bounded wait.
        """
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        if worker.isRunning():
            worker.requestInterruption()
            if timeout_ms is None:
                worker.wait()
            else:
                worker.wait(timeout_ms)
        for hook_id, state in self._hooks.items():
            self._set_debug_flag(hook_id, False)
            if state["status"] != "failed":
                self._set_status(hook_id, "disconnected")

    # ------------------------------------------------------------------
    # Debugging
    # ------------------------------------------------------------------
    @staticmethod
    def _label_text(spec, debug):
        label = spec.label or "—"
        return f"🐞 {label}" if debug else label

    def _hook_name(self, hook_id):
        state = self._hooks.get(hook_id)
        label = state["spec"].label if state is not None else ""
        return f"#{hook_id} {label}".strip()

    def _set_debug_flag(self, hook_id, debug):
        state = self._hooks.get(hook_id)
        if state is None:
            return
        state["debug"] = debug
        self._set_cell(hook_id, 0, self._label_text(state["spec"], debug))

    def debug_window(self):
        """Return the debug window, creating it on first use."""
        if self._debug_window is None:
            self._debug_window = HookDebugWindow(self.translations, self)
            self._debug_window.closed.connect(self._on_debug_window_closed)
        self._update_debug_hooks()
        return self._debug_window

    def _update_debug_hooks(self):
        if self._debug_window is not None:
            self._debug_window.set_hooks(
                {hook_id: self._hook_name(hook_id) for hook_id in self._hooks}
            )

    def set_debug(self, hook_id, enabled):
        """Switch debug mode of a hook; opens the debug window when enabled."""
        state = self._hooks.get(hook_id)
        if state is None or self._worker is None:
            return
        self._worker.set_debug(hook_id, enabled)
        self._set_debug_flag(hook_id, enabled)
        if enabled:
            window = self.debug_window()
            window.show()
            window.raise_()
        self._update_buttons()

    def _toggle_debug_selected(self):
        hook_id = self._selected_hook_id()
        state = self._hooks.get(hook_id)
        if state is None:
            return
        self.set_debug(hook_id, not state.get("debug", False))

    def _on_debug_window_closed(self):
        """Closing the window ends debug mode: it costs time in the game."""
        for hook_id, state in self._hooks.items():
            if state.get("debug"):
                if self._worker is not None:
                    self._worker.set_debug(hook_id, False)
                self._set_debug_flag(hook_id, False)
        self._update_buttons()

    def _on_debug_events(self, worker, events, dropped):
        if worker is self._worker and self._debug_window is not None:
            self._debug_window.add_events(events, dropped)

    def _on_agent_log(self, worker, level, text):
        if worker is self._worker and self._debug_window is not None:
            self._debug_window.add_console(level, text)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _address_text(self, spec):
        if spec.address is not None:
            return f"0x{spec.address:X}"
        if spec.caller_depth > 0 and spec.callers:
            return self.translations["hooks.address_callers"].format(count=len(spec.callers))
        return self.translations["hooks.address_dynamic"]

    def _show_callers(self, hook_id):
        spec = self._hooks[hook_id]["spec"]
        if spec.caller_depth == 0:
            tooltip = self.translations["hooks.callers_off"]
        elif spec.callers:
            tooltip = self.translations["hooks.callers_tooltip"].format(
                callers=describe_callers(spec.callers)
            )
        else:
            tooltip = self.translations["hooks.callers_none"]
        self._set_cell(hook_id, 1, self._address_text(spec), tooltip=tooltip)

    def _store(self, action, *args, **kwargs):
        """Run a repository call; report failures instead of raising."""
        if self._repository is None:
            return None
        try:
            return action(*args, **kwargs)
        except SQLAlchemyError as error:
            self.status_label.setText(
                self.translations["hooks.db_failed"].format(message=error)
            )
            return None

    def templates(self):
        """Return the hook templates to choose from."""
        if self._repository is None:
            return list(example_templates(self.translations).values())
        templates = self._store(self._repository.list_templates)
        if templates is None:
            return list(example_templates(self.translations).values())
        return templates

    def _restore_saved_hooks(self):
        if self._repository is None or self._process_id is None:
            return
        saved_hooks = self._store(self._repository.list_hooks, self._process_id) or []
        for saved in saved_hooks:
            try:
                spec = spec_from_saved(saved)
            except (TypeError, ValueError) as error:
                self.status_label.setText(
                    self.translations["hooks.restore_failed"].format(
                        label=saved.label or saved.id, message=error
                    )
                )
                continue
            self.add_hook(
                spec, f"{saved.module}+0x{saved.module_offset:x}", saved_id=saved.id
            )
        if saved_hooks:
            self.status_label.setText(
                self.translations["hooks.restored"].format(count=len(saved_hooks))
            )

    def _save_installed_hook(self, state, write):
        """Save a newly created hook as module + offset of its write site."""
        if self._repository is None or self._process_id is None:
            return
        if write.get("module") is None:
            self.status_label.setText(self.translations["hooks.not_saved"])
            return
        spec = state["spec"]
        state["saved_id"] = self._store(
            self._repository.add_hook,
            self._process_id,
            label=spec.label,
            module=write["module"],
            module_offset=int(write["moduleOffset"], 0),
            instruction_bytes=write["bytes"],
            instruction_text=write.get("text", ""),
            value_type=spec.value_type,
            transform=spec.transform,
            enabled=spec.enabled,
            caller_depth=spec.caller_depth,
            callers=spec.callers,
        )

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------
    def _ensure_worker(self):
        if self._worker is not None and self._worker.isRunning():
            return self._worker
        worker = HookWorker(self._pid, self)
        worker.hook_installed.connect(
            lambda hook_id, info: self._on_installed(worker, hook_id, info)
        )
        worker.hook_failed.connect(
            lambda hook_id, message: self._on_failed(worker, hook_id, message)
        )
        worker.hook_removed.connect(
            lambda hook_id: self._on_removed(worker, hook_id)
        )
        worker.hook_events.connect(
            lambda events: self._on_events(worker, events)
        )
        worker.callers_learned.connect(
            lambda hook_id, callers: self._on_callers_learned(worker, hook_id, callers)
        )
        worker.callers_ambiguous.connect(
            lambda hook_id: self._on_callers_ambiguous(worker, hook_id)
        )
        worker.hook_debug.connect(
            lambda events, dropped: self._on_debug_events(worker, events, dropped)
        )
        worker.agent_log.connect(
            lambda level, text: self._on_agent_log(worker, level, text)
        )
        worker.transform_error.connect(
            lambda hook_id, message: self._on_transform_error(worker, hook_id, message)
        )
        worker.agent_warning.connect(
            lambda message: self._on_worker_message(worker, "hooks.agent_warning", message)
        )
        worker.worker_failed.connect(
            lambda message: self._on_worker_message(worker, "hooks.worker_failed", message)
        )
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self._worker = worker
        worker.start()
        return worker

    def _on_installed(self, worker, hook_id, info):
        if worker is not self._worker or hook_id not in self._hooks:
            return
        write = info.get("writeInstruction", {})
        self._set_cell(
            hook_id,
            2,
            f"{write.get('site', '')}   {write.get('text', '')}",
            tooltip=self.translations["hooks.site_tooltip"].format(
                address=write.get("address", ""),
                trampoline=info.get("trampoline", ""),
                size=info.get("patchSize", ""),
            ),
        )
        state = self._hooks[hook_id]
        if state["saved_id"] is None and not state.get("removing"):
            self._save_installed_hook(state, write)
        self._set_status(hook_id, "active" if state["spec"].enabled else "paused")

    def _on_failed(self, worker, hook_id, message):
        if worker is not self._worker:
            return
        state = self._hooks.get(hook_id)
        if state is None:
            return
        message = message.splitlines()[0] if message else message
        if state["status"] == "installing":
            self._set_status(hook_id, "failed", message)
        self.status_label.setText(
            self.translations["hooks.action_failed"].format(message=message)
        )

    def _on_removed(self, worker, hook_id):
        if worker is not self._worker:
            return
        self._drop_row(hook_id)

    def _on_events(self, worker, events):
        if worker is not self._worker:
            return
        for event in events:
            hook_id = event.get("hookId")
            state = self._hooks.get(hook_id)
            if state is None:
                continue
            state["count"] += event.get("count", 0)
            self._set_cell(hook_id, 5, str(state["count"]))
            last = event.get("last") or {}
            if last:
                self._set_cell(
                    hook_id,
                    6,
                    f"{format_number(last.get('old'))} → "
                    f"{format_number(last.get('new'))} ⇒ "
                    f"{format_number(last.get('result'))}",
                )
            if state["status"] == "error":
                self._set_status(hook_id, "active")

    def _on_callers_learned(self, worker, hook_id, callers):
        if worker is not self._worker:
            return
        state = self._hooks.get(hook_id)
        if state is None:
            return
        callers = persistable_callers(callers)
        if callers == state["spec"].callers:
            return
        state["spec"] = dataclasses.replace(state["spec"], callers=callers)
        self._show_callers(hook_id)
        if state["saved_id"] is not None:
            self._store(self._repository.update_hook, state["saved_id"], callers=callers)

    def _on_callers_ambiguous(self, worker, hook_id):
        if worker is not self._worker:
            return
        state = self._hooks.get(hook_id)
        if state is None:
            return
        state["ambiguous"] = True
        self.status_label.setText(
            self.translations["hooks.callers_ambiguous"].format(
                label=state["spec"].label or hook_id
            )
        )

    def _on_transform_error(self, worker, hook_id, message):
        if worker is not self._worker:
            return
        self._set_status(hook_id, "error", message)
        self.status_label.setText(
            self.translations["hooks.transform_error"].format(message=message)
        )

    def _on_worker_message(self, worker, key, message):
        if worker is not self._worker:
            return
        self.status_label.setText(self.translations[key].format(message=message))

    def _on_worker_finished(self, worker):
        worker.deleteLater()
        if worker is not self._worker:
            return
        self._worker = None
        for hook_id, state in self._hooks.items():
            self._set_debug_flag(hook_id, False)
            if state["status"] != "failed":
                self._set_status(hook_id, "disconnected")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _drop_row(self, hook_id):
        row = self._row_of(hook_id)
        if row >= 0:
            self.table.removeRow(row)
        self._hooks.pop(hook_id, None)
        self._update_buttons()
        self.count_changed.emit(len(self._hooks))
        self._update_debug_hooks()

    def _edit_selected(self):
        hook_id = self._selected_hook_id()
        state = self._hooks.get(hook_id)
        if state is None or state["status"] not in {"active", "paused", "error"}:
            return
        spec = state["spec"]
        site_item = self.table.item(self._row_of(hook_id), 2)
        dialog = HookDialog(
            self.translations,
            self._address_text(spec),
            spec.value_type,
            site_item.text() if site_item is not None else "",
            transform=spec.transform,
            label=spec.label,
            templates=self.templates(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        state["spec"] = dataclasses.replace(
            spec, transform=dialog.transform, label=dialog.label
        )
        self._worker.set_transform(hook_id, dialog.transform)
        if state["saved_id"] is not None:
            self._store(
                self._repository.update_hook,
                state["saved_id"],
                label=dialog.label,
                transform=dialog.transform,
            )
        self._set_cell(
            hook_id, 0, self._label_text(state["spec"], state.get("debug", False))
        )
        self._update_debug_hooks()
        self._set_cell(hook_id, 3, dialog.transform.describe())
        if state["status"] == "error":
            self._set_status(hook_id, "active")

    def _toggle_selected(self):
        hook_id = self._selected_hook_id()
        state = self._hooks.get(hook_id)
        if state is None or self._worker is None:
            return
        enable = state["status"] == "paused"
        self._worker.set_enabled(hook_id, enable)
        state["spec"] = dataclasses.replace(state["spec"], enabled=enable)
        if state["saved_id"] is not None:
            self._store(self._repository.update_hook, state["saved_id"], enabled=enable)
        self._set_status(hook_id, "active" if enable else "paused")

    def _remove_selected(self):
        hook_id = self._selected_hook_id()
        state = self._hooks.get(hook_id)
        if state is None:
            return
        state["removing"] = True
        if state["saved_id"] is not None:
            self._store(self._repository.delete_hook, state["saved_id"])
            state["saved_id"] = None
        if (self._worker is not None
                and state["status"] in {"installing", "active", "paused", "error"}):
            self._worker.remove_hook(hook_id)
        else:
            self._drop_row(hook_id)
