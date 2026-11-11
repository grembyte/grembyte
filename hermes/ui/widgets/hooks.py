"""Dialogs and panel for creating and managing value hooks and method hooks."""

import dataclasses
import html
from datetime import datetime

from PySide6.QtCore import QThread, Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QColor, QFontDatabase  # pylint: disable=import-error,no-name-in-module
from sqlalchemy.exc import SQLAlchemyError
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QSplitter,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from hermes.core.pointer import PointerChain
from hermes.db.repository.hooks import hook_transform
from hermes.hooks.engine import MAX_CALLER_DEPTH, HookSpec, create_hook_worker
from hermes.features.method_hooks import PHASES, MethodHookSpec, MethodHookWorker
from hermes.attach.backends import BACKEND_FRIDA
from hermes.hooks.transforms import (
    TemplateTransform,
    as_template_transform,
    example_templates,
)
from hermes.hooks.syntax import BODY
from hermes.ui.widgets.code_editor import CodeEditor
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.docking import close_dialog
from hermes.ui.widgets.hook_debug import HookDebugWindow
from hermes.ui.widgets.icons import set_button_icon, theme_color
from hermes.ui.widgets.inspection import Inspections
from hermes.ui.widgets.spin_boxes import SpinBox
from hermes.ui.widgets.syntax_hint import SyntaxHint
from hermes.ui.widgets.table import (
    add_copy_actions, add_delete_action, resizable_columns, set_row_height)


# Upper bound for joining a hook worker from the GUI thread.
HOOK_STOP_TIMEOUT_MS = 3000
# The agent allocates one page per trampoline (x86-64 Linux).
TRAMPOLINE_SIZE = 0x1000

# States in which a hook can be edited, paused or debugged
_LIVE = frozenset({"active", "paused", "error", "training", "reverted"})
# Lines the log below the table keeps
MAX_LOG_LINES = 2000

# Theme colours (see icons.theme_color) of the states
_STATUS_COLORS = {
    "installing": "warning",
    "training": "purple",
    "active": "success",
    "paused": "muted",
    "failed": "danger",
    "error": "danger",
    "reverted": "orange",
    "disconnected": "muted",
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
        # Parameter name -> SyntaxHint of a code parameter
        self._syntax_hints = {}
        values = values or {}

        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for parameter in definition.parameters:
            value = values.get(parameter.name, parameter.default)
            editor = self._create_editor(parameter, value)
            self._editors[parameter.name] = (parameter, editor)
            layout.addRow(parameter.label or parameter.name, editor)
            if parameter.kind == "code":
                # Run as a function body (see hermes.hooks.syntax)
                hint = SyntaxHint(translations, editor, BODY, self)
                self._syntax_hints[parameter.name] = hint
                layout.addRow("", hint)

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
            editor = CodeEditor("" if value is None else str(value))
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
                problem = self._syntax_hints[name].problem()
                if problem is not None:
                    raise ValueError(f"{label}: {problem}")
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
    """Choose a hook template and its parameters for a write or method hook.

    ``templates`` are the TransformDefinitions to choose from (the example
    templates if omitted); only those for the kind of hook are offered. When
    editing, the hook's own copy of its template is shown; if the template
    has changed since, the dialog offers to adopt it.

    With ``method`` (a method's signature) the dialog creates a method hook:
    ``address``, ``value_type`` and ``site`` are not shown then, and ``phase``
    (before or after the call) can be chosen. A write hook chooses how it is
    installed: a jump patched into the code or a hardware ``breakpoint``.
    """

    def __init__(self, translations, address, value_type, site,
                 transform=None, label="", templates=None, pointer=None, method=None,
                 phase="after", breakpoint=False, parent=None):
        # pylint: disable=too-many-arguments,redefined-builtin
        super().__init__(parent)
        self.translations = translations
        self.pointer = pointer
        self.method = method
        self.phase = phase
        self.breakpoint = breakpoint
        self.transform = None
        self.label = label
        self.caller_depth = 1
        self._editing = transform is not None
        if templates is None:
            templates = list(example_templates().values())
        kind = "value" if method is None else "method"
        templates = [template for template in templates if template.kind == kind]
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
            ) if self.method is None
            else self.translations["hooks.dialog_method"].format(method=self.method)
        )
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)

        form = QFormLayout()
        self.label_input = QLineEdit(label)
        self.label_input.setPlaceholderText(self.translations["hooks.label_placeholder"])
        form.addRow(self.translations["hooks.label"], self.label_input)
        if self.pointer is not None:
            target = QLabel(self.pointer.text())
            target.setStyleSheet(f"color: {theme_color('purple')};")
            target.setToolTip(self.translations["hooks.pointer_hint"])
            target.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(self.translations["hooks.pointer"], target)
        elif not self._editing and self.method is None:
            self.caller_depth_input = SpinBox()
            self.caller_depth_input.setRange(0, MAX_CALLER_DEPTH)
            self.caller_depth_input.setValue(self.caller_depth)
            self.caller_depth_input.setSpecialValueText(
                self.translations["hooks.caller_depth_off"]
            )
            self.caller_depth_input.setToolTip(self.translations["hooks.caller_depth_hint"])
            form.addRow(self.translations["hooks.caller_depth"], self.caller_depth_input)
        if self.method is None:
            self.install_combo = QComboBox()
            for hardware in (False, True):
                key = "breakpoint" if hardware else "patch"
                self.install_combo.addItem(self.translations[f"hooks.install_{key}"], hardware)
            self.install_combo.setCurrentIndex(1 if self.breakpoint else 0)
            self.install_combo.setToolTip(self.translations["hooks.install_tooltip"])
            form.addRow(self.translations["hooks.install"], self.install_combo)
        if self.method is not None:
            self.phase_combo = QComboBox()
            for phase in PHASES:
                self.phase_combo.addItem(self.translations[f"hooks.phase_{phase}"], phase)
            self.phase_combo.setCurrentIndex(max(self.phase_combo.findData(self.phase), 0))
            self.phase_combo.setToolTip(self.translations["hooks.phase_tooltip"])
            form.addRow(self.translations["hooks.phase"], self.phase_combo)
        self.template_combo = QComboBox()
        form.addRow(self.translations["hooks.template"], self.template_combo)
        layout.addLayout(form)

        self.template_notice = QWidget()
        notice_layout = QHBoxLayout(self.template_notice)
        notice_layout.setContentsMargins(0, 0, 0, 0)
        self.template_notice_label = QLabel()
        self.template_notice_label.setWordWrap(True)
        self.template_notice_label.setStyleSheet(f"color: {theme_color('warning')};")
        self.btn_adopt_template = QPushButton(self.translations["hooks.template_adopt"])
        set_button_icon(self.btn_adopt_template, "sync")
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
        # A code parameter takes the height the window grows by
        layout.addWidget(self.forms, 1)
        self._update_template_notice()

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme_color('danger')};")
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
        if self.method is not None:
            self.phase = self.phase_combo.currentData()
        else:
            self.breakpoint = self.install_combo.currentData()
        if self.pointer is not None or self.method is not None:
            self.caller_depth = 0
        elif not self._editing:
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


def managed_site(managed):
    """``Class:Method+0x1a`` of a saved C# method location, or None."""
    if not managed:
        return None
    method = managed.get("method", "").split(" (")[0]
    return f"{method}+0x{managed.get('offset', 0):x}"


def method_name(signature):
    """``Class:Method`` of ``Namespace.Class:Method (params)``."""
    class_name, _, method = signature.split(" (")[0].partition(":")
    return f"{class_name.rsplit('.', 1)[-1]}:{method}"


def method_display_signature(signature, parameters):
    """Use the runtime's named parameters for a method's display signature."""
    head, separator, _ = signature.partition(" (")
    return f"{head} ({','.join(parameters)})" if separator and parameters else signature


def spec_from_saved(saved):
    """Build the HookSpec that reinstalls a hook saved in the database.

    A hook on JIT code (no module) is found again by its C# method if it
    has one (Mono games), else trained again via its signature.
    """
    jit = not saved.module
    return HookSpec(
        None,
        saved.value_type,
        hook_transform(saved),
        module=None if jit else saved.module,
        module_offset=None if jit else saved.module_offset,
        signature=saved.signature if jit else None,
        managed=saved.managed if jit else None,
        breakpoint=bool(saved.breakpoint),
        instruction_bytes=saved.instruction_bytes,
        enabled=saved.enabled,
        caller_depth=saved.caller_depth,
        callers=persistable_callers(saved.callers),
        pointer=None if saved.pointer is None else PointerChain.from_json(saved.pointer),
        label=saved.label,
    )


class HookPanel(QGroupBox):
    """Table of active hooks; owns the HookWorker of the attached process.

    With a ``repository`` new hooks are saved as module + offset and the saved
    hooks of a process are reinstalled whenever it is attached again.

    Method hooks (C# methods of Mono games) share the table; a second worker,
    the MethodHookWorker, runs them. Their state has ``"kind": "method"``.
    """

    # pylint: disable=too-many-instance-attributes

    count_changed = Signal(int)
    # From the context menu: watch the target of a value hook (HookSpec, reads)
    watch_requested = Signal(object, bool)
    # ... or monitor the method of a method hook (its identity)
    monitor_requested = Signal(object)
    # From the debug window: add an address to the address list
    # (address, value type, description)
    address_requested = Signal(object, str, str)  # object: int is 32-bit in Qt

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
        self._backend = BACKEND_FRIDA
        self._debug_window = None
        self._worker: QThread | None = None
        self._method_worker = None
        self._method_inspections = None
        self._log_targets = []
        self._hooks = {}
        # Hook trampolines of this process, also of removed hooks:
        # start address -> (trap pc, site) of the hooked write instruction.
        self._trampolines = {}
        self._trainings_paused = False
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        self.btn_edit = QPushButton(self.translations["hooks.edit"])
        set_button_icon(self.btn_edit, "edit")
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_toggle = QPushButton(self.translations["hooks.pause"])
        set_button_icon(self.btn_toggle, "pause")
        self.btn_toggle.clicked.connect(self._toggle_selected)
        self.btn_debug = QPushButton(self.translations["hooks.debug"])
        set_button_icon(self.btn_debug, "bug_report")
        self.btn_debug.setCheckable(True)
        self.btn_debug.setToolTip(self.translations["hooks.debug_tooltip"])
        self.btn_debug.clicked.connect(self._toggle_debug_selected)
        self.btn_remove = QPushButton(self.translations["hooks.remove"])
        self.btn_remove.setObjectName("btn_danger")
        set_button_icon(self.btn_remove, "delete")
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
        # One line per hook: a long write site is shortened, not broken in two.
        self.table.setWordWrap(False)
        self.table.setHorizontalHeaderLabels(
            [self.translations[key] for key in self._COLUMNS]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Several hooks at once: pause, debug or remove them together
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
            resizable_columns(self.table)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(self._edit_selected)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

        # What the hooks' scripts log (ctx.log, console.log) and their errors,
        # also outside debug mode; the debug window has every call.
        self.log_view = QTextBrowser()
        self.log_view.setOpenLinks(False)
        self.log_view.anchorClicked.connect(self._on_log_link)
        self.log_view.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText(self.translations["hooks.log_placeholder"])
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel(self.translations["hooks.log"]))
        log_bar.addStretch()
        clear_button = QPushButton(self.translations["hooks.log_clear"])
        set_button_icon(clear_button, "clear_all")
        clear_button.clicked.connect(self._clear_log)
        log_bar.addWidget(clear_button)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log_view)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(log_panel)
        splitter.setSizes([160, 130])
        layout.addWidget(splitter)
        self._update_buttons()

    def _log(self, text, time=None, targets=None, name=""):
        """Add a line to the log below the table; ``time`` in ms (else now)."""
        moment = datetime.now() if time is None else datetime.fromtimestamp(time / 1000)
        line = html.escape(f"{moment.strftime('%H:%M:%S')} {text}")
        line = f'<span style="white-space: pre-wrap">{line}</span>'
        if targets and self._method_inspections is not None:
            for target in targets:
                self._log_targets.append((self._method_worker, self._method_inspections,
                                          target, name))
                line += (f'  <a href="inspect:{len(self._log_targets) - 1}">'
                         f'{html.escape(self.translations["scripts.inspect"])}</a>')
        self.log_view.append(line)

    def _clear_log(self):
        self.log_view.clear()
        self._log_targets.clear()

    def _on_log_link(self, url):
        kind, _, number = url.toString().partition(":")
        if kind != "inspect" or not number.isdigit() or int(number) >= len(self._log_targets):
            return
        worker, inspections, target, name = self._log_targets[int(number)]
        if worker is self._method_worker and inspections is self._method_inspections:
            inspections.inspect(target, name=name)

    def _on_inspector_address_requested(self, entry):
        self.address_requested.emit(entry["address"], entry["value_type"], entry["label"])

    def _on_hook_logs(self, worker, lines, dropped):
        if not self._is_current(worker):
            return
        for line in lines:
            hook_name = self._hook_name(line.get('hookId'))
            targets = (line.get("targets", [])
                       if worker is self._method_worker else [])
            self._log(f"{hook_name}: {line.get('text', '')}", line.get("time"),
                      targets=targets, name=hook_name)
        for hook_id, count in dropped.items():
            self._log(self.translations["hooks.log_dropped"].format(
                hook=self._hook_name(hook_id), count=count))

    def _is_current(self, worker):
        """Whether signals of ``worker`` still concern this panel."""
        return worker is not None and worker in (self._worker, self._method_worker)

    def _worker_of(self, state):
        """The worker running a hook (None while there is none)."""
        return self._method_worker if state.get("kind") == "method" else self._worker

    def _still_installed(self, hook_id, state):
        """Whether a hook a dialog was opened for is still there.

        The game may have exited, or another process been chosen, while the
        dialog was open: then its edit is only saved.
        """
        return self._hooks.get(hook_id) is state and self._worker_of(state) is not None

    def _show_context_menu(self, position):
        """Offer to watch a value hook's target or to monitor a method hook's method."""
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        if not self.table.selectionModel().isRowSelected(row):
            self.table.selectRow(row)
        item = self.table.item(row, 0)
        hook_id = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        menu = self.context_menu(hook_id) or QMenu(self)
        add_delete_action(menu, self.table, self.translations, self._remove_selected)
        add_copy_actions(menu, self.table, position, self.translations)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def context_menu(self, hook_id):
        """The context menu of a hook (None for an unknown id)."""
        state = self._hooks.get(hook_id)
        if state is None:
            return None
        spec = state["spec"]
        method = state.get("kind") == "method"
        dynamic = not method and spec.address is None and spec.pointer is None
        suffix = ""
        if dynamic:
            # A hook for every address its code writes: watch the one it
            # wrote last, as a fixed address.
            last = state.get("last_address")
            spec = None if last is None else dataclasses.replace(spec, address=last)
            suffix = " · " + (
                self.translations["hooks.menu_no_write"] if last is None
                else self.translations["hooks.menu_last_address"].format(address=f"0x{last:X}")
            )
        menu = QMenu(self)
        # Same labels as in the address list
        for key, reads in (("address_table.watch", False), ("address_table.watch_access", True)):
            action = menu.addAction(
                self.translations[key] + suffix,
                lambda reads=reads: self.watch_requested.emit(spec, reads))
            action.setEnabled(not method and spec is not None and self._pid is not None)
            if dynamic:
                action.setToolTip(self.translations["hooks.menu_no_target"])
        menu.addSeparator()
        menu.addAction(
            self.translations["hooks.menu_monitor"],
            lambda: self.monitor_requested.emit(spec.identity),
        ).setEnabled(method and self._pid is not None)
        menu.setToolTipsVisible(True)
        return menu

    def _row_of(self, hook_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == hook_id:
                return row
        return -1

    def _selected_hook_ids(self):
        """The ids of the selected hooks, in table order."""
        ids = []
        for index in sorted(self.table.selectionModel().selectedRows(), key=lambda i: i.row()):
            item = self.table.item(index.row(), 0)
            if item is not None:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids

    def _selected_hook_id(self):
        """The selected hook, if exactly one is selected."""
        ids = self._selected_hook_ids()
        return ids[0] if len(ids) == 1 else None

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
            theme_color(_STATUS_COLORS[status]),
            tooltip,
        )
        self._update_buttons()

    def _update_buttons(self):
        # Edit works on one hook; pause, debug and remove on all selected.
        states = [state for state in map(self._hooks.get, self._selected_hook_ids())
                  if state is not None]
        live = bool(states) and all(state["status"] in _LIVE for state in states)
        # Hooks whose session ended: resuming installs them in a new one
        disconnected = self._pid is not None and bool(states) and all(
            state["status"] == "disconnected" for state in states)
        self.btn_edit.setEnabled(live and len(states) == 1)
        self.btn_toggle.setEnabled(live or disconnected)
        self.btn_debug.setEnabled(live)
        self.btn_debug.setChecked(bool(states) and all(state.get("debug") for state in states))
        self.btn_remove.setEnabled(bool(states))
        paused = disconnected or (
            bool(states) and all(state["status"] == "paused" for state in states))
        self.btn_toggle.setText(self.translations["hooks.resume" if paused else "hooks.pause"])
        set_button_icon(self.btn_toggle, "play_arrow" if paused else "pause")

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
        self._trampolines.clear()
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
        self._set_cell(hook_id, 1, self._address_text(spec), theme_color("success"))
        self._show_callers(hook_id)
        self._set_cell(hook_id, 2, site)
        self._set_cell(hook_id, 3, spec.transform.describe(), theme_color("orange"))
        self._set_cell(hook_id, 5, "0")
        self._set_cell(hook_id, 6, "")
        self._set_status(hook_id, "installing")
        self.table.selectRow(row)
        self.count_changed.emit(len(self._hooks))
        self._update_debug_hooks()
        return hook_id

    def create_method_hook(self, method):
        """Ask for a manipulation of a C# method (a CatalogMethod) and hook it."""
        if self._pid is None:
            return None
        dialog = HookDialog(
            self.translations, "", "", "", templates=self.templates(),
            method=method.display_signature, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        spec = MethodHookSpec(method.identity(), dialog.transform,
                              label=dialog.label or method_name(method.signature),
                              phase=dialog.phase)
        return self.add_method_hook(spec)

    def add_method_hook(self, spec, saved_id=None):
        """Install a method hook (MethodHookSpec); saved once it is installed."""
        worker = self._ensure_method_worker()
        hook_id = worker.add_hook(spec)
        self._hooks[hook_id] = {
            "kind": "method", "spec": spec, "status": "installing", "count": 0,
            "saved_id": saved_id,
        }
        row = self.table.rowCount()
        self.table.insertRow(row)
        label_item = QTableWidgetItem(self._label_text(spec, False))
        label_item.setData(Qt.ItemDataRole.UserRole, hook_id)
        self.table.setItem(row, 0, label_item)
        self._show_method_phase(hook_id, spec)
        self._set_cell(hook_id, 2, method_name(spec.signature), tooltip=spec.signature)
        self._set_cell(hook_id, 3, spec.transform.describe(), theme_color("orange"))
        self._set_cell(hook_id, 5, "0")
        self._set_cell(hook_id, 6, "")
        self._set_status(hook_id, "installing")
        self.table.selectRow(row)
        self.count_changed.emit(len(self._hooks))
        self._update_debug_hooks()
        return hook_id

    def _show_method_phase(self, hook_id, spec):
        self._set_cell(
            hook_id, 1,
            f"{self.translations['hooks.address_method']} · "
            f"{self.translations[f'hooks.phase_short_{spec.phase}']}",
            theme_color("purple"), tooltip=self.translations[f"hooks.method_tooltip_{spec.phase}"])

    def _ensure_method_worker(self):
        worker = self._method_worker
        if worker is not None and worker.isRunning():
            return worker
        worker = MethodHookWorker(self._pid, self)
        worker.hook_installed.connect(
            lambda hook_id, info: self._on_method_installed(worker, hook_id, info))
        worker.hook_failed.connect(
            lambda hook_id, message: self._on_failed(worker, hook_id, message))
        worker.hook_removed.connect(lambda hook_id: self._on_removed(worker, hook_id))
        worker.hook_events.connect(lambda events: self._on_events(worker, events))
        worker.hook_debug.connect(
            lambda events, dropped: self._on_debug_events(worker, events, dropped))
        worker.hook_logs.connect(
            lambda lines, dropped: self._on_hook_logs(worker, lines, dropped))
        worker.hook_ended.connect(lambda hook_id: self._on_method_ended(worker, hook_id))
        worker.agent_log.connect(lambda level, text: self._on_agent_log(worker, level, text))
        self._method_inspections = Inspections(self.translations, worker, self.window())
        self._method_inspections.address_requested.connect(self._on_inspector_address_requested)
        worker.transform_error.connect(
            lambda hook_id, message: self._on_transform_error(worker, hook_id, message))
        worker.agent_warning.connect(
            lambda message: self._on_worker_message(worker, "hooks.agent_warning", message))
        worker.worker_failed.connect(
            lambda message: self._on_worker_message(worker, "hooks.worker_failed", message))
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self._method_worker = worker
        worker.start()
        return worker

    def _on_method_installed(self, worker, hook_id, info):
        state = self._hooks.get(hook_id)
        if not self._is_current(worker) or state is None:
            return
        spec = state["spec"]
        state["display_signature"] = method_display_signature(
            spec.signature, info.get("params", []))
        self._set_cell(hook_id, 2, method_name(spec.signature),
                       tooltip=self.translations["hooks.method_site_tooltip"].format(
                           method=spec.signature, address=info.get("address", ""),
                           params=", ".join(info.get("params", [])) or "—",
                           returns=info.get("returns", "")))
        if (state["saved_id"] is None and not state.get("removing")
                and self._repository is not None and self._process_id is not None):
            state["saved_id"] = self._store(
                self._repository.add_method_hook, self._process_id, label=spec.label,
                managed=spec.identity, transform=spec.transform, enabled=spec.enabled,
                phase=spec.phase)
        self._set_status(hook_id, "active" if spec.enabled else "paused")

    def _on_method_ended(self, worker, hook_id):
        if self._is_current(worker) and hook_id in self._hooks:
            self._set_status(hook_id, "disconnected", self.translations["hooks.method_ended"])

    def _edit_method_hook(self, hook_id, state):
        spec = state["spec"]
        dialog = HookDialog(
            self.translations, "", "", "", transform=spec.transform, label=spec.label,
            templates=self.templates(),
            method=state.get("display_signature", spec.signature), phase=spec.phase, parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if state["saved_id"] is not None:
            self._store(self._repository.update_method_hook, state["saved_id"],
                        label=dialog.label, transform=dialog.transform, phase=dialog.phase)
        if not self._still_installed(hook_id, state):
            return
        state["spec"] = dataclasses.replace(
            spec, transform=dialog.transform, label=dialog.label, phase=dialog.phase)
        self._method_worker.set_transform(hook_id, dialog.transform)
        if dialog.phase != spec.phase:
            self._method_worker.set_phase(hook_id, dialog.phase)
            self._show_method_phase(hook_id, state["spec"])
        self._set_cell(hook_id, 0, self._label_text(state["spec"], state.get("debug", False)))
        self._update_debug_hooks()
        self._set_cell(hook_id, 3, dialog.transform.describe())
        if state["status"] == "error":
            self._set_status(hook_id, "active")

    def shutdown(self, timeout_ms=HOOK_STOP_TIMEOUT_MS):
        """Remove all hooks and stop the worker.

        A ``timeout_ms`` of ``None`` waits until the worker has finished;
        otherwise it finishes in the background after the bounded wait.
        """
        self._close_method_inspections()
        workers = [worker for worker in (self._worker, self._method_worker)
                   if worker is not None]
        if not workers:
            return
        self._worker = None
        self._method_worker = None
        for worker in workers:
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
        return f"◉ {label}" if debug else label

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
            self._debug_window.add_address_requested.connect(self._on_debug_add_address)
        self._update_debug_hooks()
        return self._debug_window

    def _update_debug_hooks(self):
        if self._debug_window is not None:
            self._debug_window.set_hooks(
                {hook_id: self._hook_name(hook_id) for hook_id in self._hooks}
            )

    def _on_debug_add_address(self, entry):
        """Offer the address of a debug entry for the address list, typed like its hook."""
        hook_id = entry.get("hookId")
        state = self._hooks.get(hook_id)
        if state is None or state.get("kind") == "method" or not entry.get("address"):
            return
        label = state["spec"].label or self._hook_name(hook_id)
        if entry.get("caller"):
            label = self.translations["debug.address_label"].format(
                hook=label, caller=entry["caller"])
        self.address_requested.emit(int(entry["address"], 16), state["spec"].value_type, label)

    def set_debug(self, hook_id, enabled):
        """Switch debug mode of a hook; opens the debug window when enabled."""
        state = self._hooks.get(hook_id)
        worker = None if state is None else self._worker_of(state)
        if worker is None:
            return
        worker.set_debug(hook_id, enabled)
        self._set_debug_flag(hook_id, enabled)
        if enabled:
            window = self.debug_window()
            window.show()
            window.raise_()
        self._update_buttons()

    def _toggle_debug_selected(self):
        """Debug mode for all selected hooks: on unless all have it, then off."""
        hook_ids = [hook_id for hook_id in self._selected_hook_ids() if hook_id in self._hooks]
        if not hook_ids:
            return
        enable = not all(self._hooks[hook_id].get("debug") for hook_id in hook_ids)
        for hook_id in hook_ids:
            self.set_debug(hook_id, enable)

    def _on_debug_window_closed(self):
        """Closing the window ends debug mode: it costs time in the game."""
        for hook_id, state in self._hooks.items():
            if state.get("debug"):
                worker = self._worker_of(state)
                if worker is not None:
                    worker.set_debug(hook_id, False)
                self._set_debug_flag(hook_id, False)
        self._update_buttons()

    def _on_debug_events(self, worker, events, dropped):
        if self._is_current(worker) and self._debug_window is not None:
            self._debug_window.add_events(events, dropped)

    def _on_agent_log(self, worker, level, text):
        if not self._is_current(worker):
            return
        self._log(f"[{level}] {text}")
        if self._debug_window is not None:
            self._debug_window.add_console(level, text)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _address_text(self, spec):
        if spec.pointer is not None:
            return self.translations["hooks.address_pointer"].format(chain=spec.pointer.text())
        if spec.address is not None:
            return f"0x{spec.address:X}"
        if spec.caller_depth > 0 and spec.callers:
            return self.translations["hooks.address_callers"].format(count=len(spec.callers))
        return self.translations["hooks.address_dynamic"]

    def _show_callers(self, hook_id):
        spec = self._hooks[hook_id]["spec"]
        if spec.pointer is not None:
            tooltip = self.translations["hooks.pointer_hint"]
        elif spec.caller_depth == 0:
            tooltip = self.translations["hooks.callers_off"]
        elif spec.callers:
            tooltip = self.translations["hooks.callers_tooltip"].format(
                callers=describe_callers(spec.callers)
            )
        elif spec.address is None:
            # Restored without callers: it has no address to learn them by.
            tooltip = self.translations["hooks.callers_none_dynamic"]
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
            return list(example_templates().values())
        templates = self._store(self._repository.list_templates)
        if templates is None:
            return list(example_templates().values())
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
            site = (f"{saved.module}+0x{saved.module_offset:x}" if saved.module
                    else managed_site(saved.managed) or saved.instruction_text)
            self.add_hook(spec, site, saved_id=saved.id)
        saved_method_hooks = self._store(
            self._repository.list_method_hooks, self._process_id) or []
        for saved in saved_method_hooks:
            try:
                spec = MethodHookSpec(saved.managed, hook_transform(saved, kind="method"),
                                      enabled=saved.enabled, label=saved.label,
                                      phase=saved.phase or "after")
            except (TypeError, ValueError) as error:
                self.status_label.setText(
                    self.translations["hooks.restore_failed"].format(
                        label=saved.label or saved.id, message=error
                    )
                )
                continue
            self.add_method_hook(spec, saved_id=saved.id)
        if saved_hooks or saved_method_hooks:
            self.status_label.setText(
                self.translations["hooks.restored"].format(
                    count=len(saved_hooks) + len(saved_method_hooks))
            )

    def _save_installed_hook(self, state, write):
        """Save a newly created hook as module + offset of its write site."""
        if self._repository is None or self._process_id is None:
            return
        spec = state["spec"]
        jit = write.get("module") is None
        if jit and (spec.pointer is None or not write.get("context")):
            self.status_label.setText(self.translations["hooks.not_saved"])
            return
        state["saved_id"] = self._store(
            self._repository.add_hook,
            self._process_id,
            label=spec.label,
            # JIT code: saved with the code around it and trained after a restart.
            module="" if jit else write["module"],
            module_offset=0 if jit else int(write["moduleOffset"], 0),
            signature=write["context"] if jit else None,
            managed=(spec.managed or state.get("managed")) if jit else None,
            instruction_bytes=write["bytes"],
            instruction_text=write.get("text", ""),
            value_type=spec.value_type,
            transform=spec.transform,
            enabled=spec.enabled,
            caller_depth=spec.caller_depth,
            callers=spec.callers,
            pointer=spec.pointer,
            breakpoint=spec.breakpoint,
        )
        if jit and state["saved_id"] is not None:
            self.status_label.setText(self.translations["hooks.saved_for_training"])

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------
    def set_backend(self, backend):
        """Choose the hook backend ('frida' or 'ptrace').

        A running worker keeps its backend; the choice applies the next time a
        worker is created (after the process is re-attached or the panel is
        shut down), so no hooks are lost by switching mid-session.
        """
        self._backend = backend

    def _ensure_worker(self):
        if self._worker is not None and self._worker.isRunning():
            return self._worker
        worker = create_hook_worker(self._backend, self._pid, self)
        worker.hook_installed.connect(
            lambda hook_id, info: self._on_installed(worker, hook_id, info)
        )
        worker.hook_failed.connect(
            lambda hook_id, message: self._on_failed(worker, hook_id, message)
        )
        worker.hook_training.connect(
            lambda hook_id, state: self._on_training(worker, hook_id, state)
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
        worker.callers_dropped.connect(
            lambda hook_id, count: self._on_callers_dropped(worker, hook_id, count)
        )
        worker.site_method.connect(
            lambda hook_ids, method, managed:
            self._on_site_method(worker, hook_ids, method, managed)
        )
        worker.managed_failed.connect(
            lambda hook_id, message: self._on_managed_failed(worker, hook_id, message)
        )
        worker.hook_profile.connect(
            lambda sites: self._on_profile(worker, sites)
        )
        worker.site_state.connect(
            lambda hook_ids, site_state, reverts, error:
            self._on_site_state(worker, hook_ids, site_state, reverts, error)
        )
        worker.hook_debug.connect(
            lambda events, dropped: self._on_debug_events(worker, events, dropped)
        )
        worker.hook_logs.connect(
            lambda lines, dropped: self._on_hook_logs(worker, lines, dropped)
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
        if self._trainings_paused:
            worker.set_trainings_paused(True, timeout=None)  # before any hook
        return worker

    def _on_installed(self, worker, hook_id, info):
        if worker is not self._worker or hook_id not in self._hooks:
            return
        write = info.get("writeInstruction", {})
        state = self._hooks[hook_id]
        state["training"] = False
        state["installed"] = info
        self._remember_trampoline(info, write)
        self._show_site(hook_id)
        if state["saved_id"] is None and not state.get("removing"):
            self._save_installed_hook(state, write)
        self._set_status(hook_id, "active" if state["spec"].enabled else "paused")

    def _show_site(self, hook_id):
        """Show the hooked write instruction, by its C# method in Mono JIT code."""
        state = self._hooks[hook_id]
        info = state["installed"]
        write = info.get("writeInstruction", {})
        site = write.get("method") or state.get("method") or write.get("site", "")
        if info.get("breakpoint") is not None:
            tooltip = self.translations["hooks.site_tooltip_breakpoint"].format(
                address=write.get("address", ""), slot=info["breakpoint"])
        else:
            tooltip = self.translations["hooks.site_tooltip"].format(
                address=write.get("address", ""),
                trampoline=info.get("trampoline", ""),
                size=info.get("patchSize", ""),
            )
        self._set_cell(hook_id, 2, f"{site}   {write.get('text', '')}", tooltip=tooltip)

    def _on_site_method(self, worker, hook_ids, method, managed):
        """Name the write site once the game ran it (Mono JIT code only).

        A saved hook keeps the method, so it is found by it after a restart.
        """
        if worker is not self._worker:
            return
        for hook_id in hook_ids:
            state = self._hooks.get(hook_id)
            if state is None:
                continue
            # The message may overtake the installation (and the saving).
            state["method"] = method
            state["managed"] = managed
            if "installed" in state:
                self._show_site(hook_id)
            if managed and state["saved_id"] is not None and state["spec"].managed != managed:
                state["spec"] = dataclasses.replace(state["spec"], managed=managed)
                self._store(self._repository.update_hook, state["saved_id"], managed=managed)

    def _on_managed_failed(self, worker, hook_id, message):
        if worker is not self._worker or hook_id not in self._hooks:
            return
        self.status_label.setText(self.translations["hooks.managed_failed"].format(
            label=self._hooks[hook_id]["spec"].label or hook_id, message=message
        ))

    def _on_site_state(self, worker, hook_ids, site_state, reverts, error=""):
        """The game reverted a hooked write site, or it is patched again.

        A reverted site runs the game's original code: its hooks no longer
        apply although nothing failed. Paused hooks show it once resumed.
        ``error`` tells why patching it again failed.
        """
        if not self._is_current(worker):
            return
        reverted = site_state in {"reverted", "changed"}
        labels = []
        for hook_id in hook_ids:
            state = self._hooks.get(hook_id)
            if state is None:
                continue
            labels.append(state["spec"].label or str(hook_id))
            if reverted:
                state["reverted"] = site_state
                state["repatch_error"] = error
                if state["status"] in {"active", "error", "reverted"}:
                    self._set_status(hook_id, "reverted", self._reverted_tooltip(state))
            else:
                state.pop("reverted", None)
                if state["status"] in {"reverted", "active"}:
                    self._set_status(hook_id, "active", self.translations[
                        "hooks.repatched_tooltip"].format(count=reverts))
        if labels and site_state != "restored":
            text = self.translations[f"hooks.site_{site_state}"].format(
                label=", ".join(labels), count=reverts)
            if error:
                text += " " + self.translations["hooks.repatch_failed"].format(message=error)
            self.status_label.setText(text)

    def _on_profile(self, worker, sites):
        """Show the timings of each hook's write site on its count of applications."""
        if not self._is_current(worker):
            return
        for site in sites:
            tooltip = self.translations["hooks.profile_tooltip"].format(
                calls=site.get("calls", 0), skipped=site.get("skipped", 0),
                wait_avg=site.get("waitAvg", 0), wait_max=site.get("waitMax", 0),
                run_avg=site.get("runAvg", 0), run_max=site.get("runMax", 0))
            for hook_id in site.get("hookIds", []):
                state = self._hooks.get(hook_id)
                if state is not None:
                    self._set_cell(hook_id, 5, str(state["count"]), tooltip=tooltip)

    def _reverted_tooltip(self, state):
        tooltip = self.translations[f"hooks.status_reverted_{state['reverted']}"]
        if state.get("repatch_error"):
            tooltip += "\n\n" + self.translations["hooks.repatch_failed"].format(
                message=state["repatch_error"])
        return tooltip

    def uses_debug_registers(self):
        """Whether a hook holds hardware breakpoints or watchpoints in the process.

        Hooks in training watch their target, breakpoint hooks their write.
        """
        return any(
            state["status"] == "training"
            or (state.get("kind") != "method" and state["spec"].breakpoint
                and state["status"] in {"active", "paused", "error"})
            for state in self._hooks.values()
        )

    def _remember_trampoline(self, info, write):
        try:
            start = int(info["trampoline"], 16)
            trap_pc = int(info["resumePc"], 16)
            length = trap_pc - int(write["address"], 16)
        except (KeyError, TypeError, ValueError):
            return
        site = write.get("site", "")
        module, separator, offset = site.rpartition("+")
        try:
            site = f"{module}+{int(offset, 16) + length:#x}" if separator else f"{trap_pc:#x}"
        except ValueError:
            site = f"{trap_pc:#x}"
        self._trampolines[start] = (trap_pc, site)

    def _original_write(self, pc):
        """Return (trap pc, site) of the hooked write if ``pc`` is in a trampoline."""
        original = None
        # A hook created from a trampoline hit hooks that trampoline in turn.
        for _ in range(len(self._trampolines)):
            start = next(
                (start for start in self._trampolines
                 if start <= pc < start + TRAMPOLINE_SIZE),
                None,
            )
            if start is None:
                break
            original = self._trampolines[start]
            pc = original[0]
        return original

    def pause_trainings(self, paused):
        """Pause hooks in training while a write watch runs, or resume them.

        Both watch the process with hardware watchpoints; running at the same
        time they can deadlock Frida and crash the program.
        """
        self._trainings_paused = paused
        worker = self._worker
        if worker is not None and worker.isRunning():
            # The agent remembers the pause for trainings that start later.
            worker.set_trainings_paused(paused)

    def translate_watch_hits(self, hits):
        """Report write-watch hits in hook trampolines at the hooked write.

        A hooked write runs relocated in its trampoline, so a watchpoint
        reports a PC there instead of in the game's code. Hits of the hooks'
        own accesses to the target are dropped.
        """
        translated = []
        worker = self._worker
        own = [] if worker is None else [
            code for code in (worker.access_stub, getattr(worker, "fast_code", None))
            if code is not None
        ]
        for hit in hits:
            try:
                pc = int(hit["pc"], 16)
            except (KeyError, TypeError, ValueError):
                pc = None
            if pc is not None and any(start <= pc < end for start, end in own):
                continue  # a hook's own access to the target
            try:
                original = self._original_write(int(hit["pc"], 16))
            except (KeyError, TypeError, ValueError):
                original = None
            if original is not None:
                hit = {**hit, "pc": f"{original[0]:#x}", "site": original[1]}
            translated.append(hit)
        return translated

    def _on_training(self, worker, hook_id, training):
        """Show that a hook waits for a write from its code (or its progress)."""
        if worker is not self._worker:
            return
        state = self._hooks.get(hook_id)
        if state is None or state["status"] not in {"installing", "training", "paused"}:
            return
        state["training"] = True
        if state["status"] == "paused":
            return
        if training.get("paused"):
            tooltip = self.translations["hooks.training_paused"]
        elif not training.get("resolved", False):
            key = "hooks.training_waiting_slot" if training.get("waiting") \
                else "hooks.training_unresolved"
            tooltip = self.translations[key]
        else:
            tooltip = self.translations["hooks.training_tooltip"].format(
                seen=training.get("seen", 0), rejected=training.get("rejected", 0)
            )
        self._set_status(hook_id, "training", tooltip)

    def _on_failed(self, worker, hook_id, message):
        if not self._is_current(worker):
            return
        state = self._hooks.get(hook_id)
        if state is None:
            return
        message = message.splitlines()[0] if message else message
        self._log(self.translations["hooks.log_error"].format(
            hook=self._hook_name(hook_id), message=message))
        if state["status"] in {"installing", "training"}:
            self._set_status(hook_id, "failed", message)
        self.status_label.setText(
            self.translations["hooks.action_failed"].format(message=message)
        )

    def _on_removed(self, worker, hook_id):
        if not self._is_current(worker):
            return
        self._drop_row(hook_id)

    def _on_events(self, worker, events):
        if not self._is_current(worker):
            return
        for event in events:
            hook_id = event.get("hookId")
            state = self._hooks.get(hook_id)
            if state is None:
                continue
            state["count"] += event.get("count", 0)
            self._set_cell(hook_id, 5, str(state["count"]))
            last = event.get("last") or {}
            if last.get("address"):
                state["last_address"] = int(last["address"], 16)
            if last and state.get("kind") == "method":
                # The method's result and what the hook made of it
                self._set_cell(hook_id, 6, f"{last.get('old', '')} ⇒ {last.get('result', '')}")
            elif last:
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

    def _on_callers_dropped(self, worker, hook_id, count):
        if worker is not self._worker:
            return
        state = self._hooks.get(hook_id)
        if state is None:
            return
        key = "hooks.callers_dropped" if state["spec"].callers else "hooks.callers_dropped_all"
        self.status_label.setText(
            self.translations[key].format(label=state["spec"].label or hook_id, count=count)
        )

    def _on_transform_error(self, worker, hook_id, message):
        if not self._is_current(worker):
            return
        self._log(self.translations["hooks.log_error"].format(
            hook=self._hook_name(hook_id), message=message))
        self._set_status(hook_id, "error", message)
        self.status_label.setText(
            self.translations["hooks.transform_error"].format(message=message)
        )

    def _on_worker_message(self, worker, key, message):
        if not self._is_current(worker):
            return
        self._log(self.translations[key].format(message=message))
        self.status_label.setText(self.translations[key].format(message=message))

    def _on_worker_finished(self, worker):
        # Not our child any more: deleting the worker releases its slots, and
        # with them maybe the last reference to this panel; a panel without
        # a parent would then delete the worker a second time (PySide6).
        worker.setParent(None)
        worker.deleteLater()
        if not self._is_current(worker):
            return
        method = worker is self._method_worker
        if method:
            self._method_worker = None
            self._close_method_inspections()
        else:
            self._worker = None
        for hook_id, state in self._hooks.items():
            if (state.get("kind") == "method") != method:
                continue
            self._set_debug_flag(hook_id, False)
            if state["status"] != "failed":
                self._set_status(hook_id, "disconnected",
                                 self.translations["hooks.disconnected_tooltip"])

    def _close_method_inspections(self):
        inspections = self._method_inspections
        self._method_inspections = None
        self._log_targets.clear()
        if inspections is not None:
            for inspector in list(inspections.inspectors):
                close_dialog(inspector)
            inspections.deleteLater()

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
        if state is None or state["status"] not in {
            "active", "paused", "error", "training", "reverted"
        }:
            return
        spec = state["spec"]
        if state.get("kind") == "method":
            self._edit_method_hook(hook_id, state)
            return
        site_item = self.table.item(self._row_of(hook_id), 2)
        dialog = HookDialog(
            self.translations,
            self._address_text(spec),
            spec.value_type,
            site_item.text() if site_item is not None else "",
            transform=spec.transform,
            label=spec.label,
            templates=self.templates(),
            pointer=spec.pointer,
            breakpoint=spec.breakpoint,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if state["saved_id"] is not None:
            self._store(
                self._repository.update_hook,
                state["saved_id"],
                label=dialog.label,
                transform=dialog.transform,
                breakpoint=dialog.breakpoint,
            )
        if not self._still_installed(hook_id, state):
            return
        state["spec"] = dataclasses.replace(
            spec, transform=dialog.transform, label=dialog.label,
            breakpoint=dialog.breakpoint,
        )
        if dialog.breakpoint != spec.breakpoint:
            self._reinstall(hook_id)
            return
        self._worker.set_transform(hook_id, dialog.transform)
        self._set_cell(
            hook_id, 0, self._label_text(state["spec"], state.get("debug", False))
        )
        self._update_debug_hooks()
        self._set_cell(hook_id, 3, dialog.transform.describe())
        if state["status"] == "error":
            self._set_status(hook_id, "active")

    def _reinstall(self, hook_id):
        """Install a hook anew, e.g. by breakpoint instead of a patched jump.

        The new hook keeps the saved one; its row replaces the old one.
        """
        state = self._hooks[hook_id]
        site_item = self.table.item(self._row_of(hook_id), 2)
        site = site_item.text() if site_item is not None else ""
        saved_id = state["saved_id"]
        state["saved_id"] = None
        state["removing"] = True
        self._worker.remove_hook(hook_id)
        self.add_hook(state["spec"], site, saved_id=saved_id)

    def _reconnect(self, hook_id):
        """Install a hook whose session ended anew, enabled, in a new session.

        It keeps its saved hook; its row moves to the end of the table.
        """
        state = self._hooks[hook_id]
        site_item = self.table.item(self._row_of(hook_id), 2)
        site = site_item.text() if site_item is not None else ""
        spec = dataclasses.replace(state["spec"], enabled=True)
        method = state.get("kind") == "method"
        if state["saved_id"] is not None and not state["spec"].enabled:
            update = (self._repository.update_method_hook if method
                      else self._repository.update_hook)
            self._store(update, state["saved_id"], enabled=True)
        self._drop_row(hook_id)
        if method:
            self.add_method_hook(spec, saved_id=state["saved_id"])
        else:
            self.add_hook(spec, site, saved_id=state["saved_id"])

    def _toggle_selected(self):
        """Pause the selected hooks, or resume them if all are paused."""
        hook_ids = [hook_id for hook_id in self._selected_hook_ids() if hook_id in self._hooks]
        if not hook_ids:
            return
        if all(self._hooks[hook_id]["status"] == "disconnected" for hook_id in hook_ids):
            if self._pid is not None:
                for hook_id in hook_ids:
                    self._reconnect(hook_id)
            return
        enable = all(self._hooks[hook_id]["status"] == "paused" for hook_id in hook_ids)
        for hook_id in hook_ids:
            if (self._hooks[hook_id]["status"] == "paused") == enable:
                self._set_enabled(hook_id, enable)

    def _set_enabled(self, hook_id, enable):
        state = self._hooks.get(hook_id)
        worker = None if state is None else self._worker_of(state)
        if worker is None:
            return
        worker.set_enabled(hook_id, enable)
        state["spec"] = dataclasses.replace(state["spec"], enabled=enable)
        if state["saved_id"] is not None:
            update = (self._repository.update_method_hook if state.get("kind") == "method"
                      else self._repository.update_hook)
            self._store(update, state["saved_id"], enabled=enable)
        if not enable:
            self._set_status(hook_id, "paused")
        elif state.get("reverted"):
            self._set_status(hook_id, "reverted", self._reverted_tooltip(state))
        else:
            self._set_status(hook_id, "training" if state.get("training") else "active")

    def _remove_selected(self):
        for hook_id in self._selected_hook_ids():
            self._remove(hook_id)

    def _remove(self, hook_id):
        state = self._hooks.get(hook_id)
        if state is None:
            return
        state["removing"] = True
        if state["saved_id"] is not None:
            delete = (self._repository.delete_method_hook if state.get("kind") == "method"
                      else self._repository.delete_hook)
            self._store(delete, state["saved_id"])
            state["saved_id"] = None
        worker = self._worker_of(state)
        if (worker is not None
                and state["status"] in {
                    "installing", "active", "paused", "error", "training", "reverted"
                }):
            worker.remove_hook(hook_id)
        else:
            self._drop_row(hook_id)
