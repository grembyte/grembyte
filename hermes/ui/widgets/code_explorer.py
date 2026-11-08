"""Window of the code explorer: strings, references and functions of a native module.

See hermes/features/code_explorer.py. It reads the module's file, so it works
on any game (also without attaching) and cannot disturb it. Addresses shown
are the file's; the monitor and memory viewer get the module's address in
the process.
"""

import bisect
import os
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, Qt, QThread, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QColor,
    QFontDatabase,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from hermes.core import messages
from hermes.features.code_explorer import (
    CodeIndex,
    Function,
    ModuleChoice,
    cached_index,
    load_image,
    parse_location,
    process_modules,
    remember_index,
)
from hermes.features.pseudocode import render as render_pseudocode
from hermes.ui.widgets.code_editor import PseudocodeHighlighter, code_theme_colors
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.labels import DocsHint
from hermes.ui.widgets.table import copy_menu, resizable_columns
from hermes.ui.workers import keep_until_finished

# The address comment of a pseudocode line
_PSEUDOCODE_ADDRESS = re.compile(r"// 0x([0-9a-f]+)")

class CodeIndexWorker(QThread):
    """Read a module's file and build its CodeIndex in the background."""

    progress = Signal(int)
    built = Signal(object)
    failed = Signal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            image = load_image(self.path)
            index = CodeIndex(image, progress=self._progress)
        except InterruptedError:
            return
        except (OSError, ValueError, MemoryError) as error:
            self.failed.emit(messages.describe(error))
            return
        remember_index(self.path, index)
        self.built.emit(index)

    def _progress(self, fraction):
        self.progress.emit(int(fraction * 100))
        return self.isInterruptionRequested()


class _RowsModel(QAbstractTableModel):
    """Rows of texts by column, with an optional filter by words."""

    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.headers = headers
        self.rows = []      # (key, cells)
        self.shown = []     # indexes into rows
        self._words = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self._apply()
        self.endResetModel()

    def set_filter(self, text):
        self.beginResetModel()
        self._words = text.lower().split()
        self._apply()
        self.endResetModel()

    def _apply(self):
        if not self._words:
            self.shown = list(range(len(self.rows)))
            return
        self.shown = [index for index, (_key, cells) in enumerate(self.rows)
                      if all(word in " ".join(cells).lower() for word in self._words)]

    def key(self, row):
        return self.rows[self.shown[row]][0]

    def row_of(self, key):
        for row, index in enumerate(self.shown):
            if self.rows[index][0] == key:
                return row
        return None

    def rowCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.shown)

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else len(self.headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole,
                                               Qt.ItemDataRole.ToolTipRole):
            return None
        return self.rows[self.shown[index.row()]][1][index.column()]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None


class BoundedComboBox(QComboBox):
    """A combo box whose list reaches at most to the bottom of its window.

    A styled QComboBox lets its list fill the screen and ignores
    maxVisibleItems; combobox-popup: 0 makes it count rows again, and each
    time it opens they are as many as fit below it in the window. The list
    gets its scroll bar back.
    """

    MIN_ROWS = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")

    def showPopup(self):  # pylint: disable=invalid-name
        window = self.window()
        bottom = window.mapToGlobal(QPoint(0, window.height())).y()
        top = self.mapToGlobal(QPoint(0, self.height())).y()
        row = self.view().sizeHintForRow(0) if self.count() else 0
        row = max(row, self.fontMetrics().height() + 6)
        self.setMaxVisibleItems(max(self.MIN_ROWS, (bottom - top) // row - 1))
        # Qt switches the list's scroll bar off for scroll arrows that the
        # styles hide; with it, a long list shows where it is and can be dragged
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        super().showPopup()
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)


def _table(model, stretch_column, translations, monospace=False):
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(24)
    header = view.horizontalHeader()
    for column in range(model.columnCount()):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch
                                    if column == stretch_column
                                    else QHeaderView.ResizeMode.Interactive)
    if monospace:
        view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    resizable_columns(view)
    copy_menu(view, translations)
    return view


class CodeExplorerDialog(QDialog):
    """Strings, references and functions of a module (see the module's docstring). Not modal."""

    # pylint: disable=too-many-instance-attributes

    # an address in the process (int) to show in the memory viewer
    memory_requested = Signal(object)
    # an address in the process (int) whose calls or runs to monitor
    monitor_requested = Signal(object)

    def __init__(self, translations, pid=None, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self.index = None
        self.module = None
        self._worker = None
        self._history = []
        self._function = None
        self._lines = []
        self._pseudocode_of = None  # the Function the pseudocode tab shows
        self._pseudocode_lines = []  # (instruction address, line), by address
        self._syncing = False  # moving one view to the other's instruction
        self.setWindowTitle(translations["code.title"])
        self.resize(1250, 800)
        self._build_ui()
        self._fill_modules()

    # --- UI ------------------------------------------------------------------
    def _build_ui(self):
        # pylint: disable=too-many-statements
        t = self.translations
        layout = QVBoxLayout(self)
        layout.addWidget(DocsHint(t, t["code.hint"], "guide/code-explorer/"))

        source = QHBoxLayout()
        source.addWidget(QLabel(t["code.module"]))
        self.module_combo = BoundedComboBox()
        self.module_combo.setMinimumWidth(320)
        self.module_combo.activated.connect(self._module_chosen)
        source.addWidget(self.module_combo, 1)
        self.open_button = QPushButton(t["code.open_file"])
        set_button_icon(self.open_button, "folder_open")
        self.open_button.setToolTip(t["code.open_file_tooltip"])
        self.open_button.clicked.connect(self._open_file)
        source.addWidget(self.open_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(180)
        self.progress.hide()
        source.addWidget(self.progress)
        layout.addLayout(source)

        navigation = QHBoxLayout()
        self.goto_input = QLineEdit()
        self.goto_input.setPlaceholderText(t["code.goto_placeholder"])
        self.goto_input.setToolTip(t["code.goto_tooltip"])
        self.goto_input.returnPressed.connect(self._goto)
        navigation.addWidget(self.goto_input, 1)
        self.goto_button = QPushButton(t["code.goto"])
        set_button_icon(self.goto_button, "search")
        self.goto_button.clicked.connect(self._goto)
        navigation.addWidget(self.goto_button)
        layout.addLayout(navigation)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tabs = QTabWidget()
        self.string_filter = QLineEdit()
        self.string_filter.setPlaceholderText(t["code.string_filter"])
        self.string_filter.setClearButtonEnabled(True)
        self.string_model = _RowsModel([t["code.column_address"], t["code.column_string"]], self)
        self.string_filter.textChanged.connect(self.string_model.set_filter)
        self.string_view = _table(self.string_model, 1, t)
        self.string_view.horizontalHeader().resizeSection(0, 130)
        self.string_view.activated.connect(self._string_activated)
        self.string_view.selectionModel().currentRowChanged.connect(
            lambda current, _previous: self._string_activated(current))
        self.tabs.addTab(self._with_filter(self.string_filter, self.string_view),
                         t["code.strings"])
        self.function_filter = QLineEdit()
        self.function_filter.setPlaceholderText(t["code.function_filter"])
        self.function_filter.setClearButtonEnabled(True)
        self.function_model = _RowsModel([t["code.column_address"], t["code.column_name"],
                                          t["code.column_size"]], self)
        self.function_filter.textChanged.connect(self.function_model.set_filter)
        self.function_view = _table(self.function_model, 1, t)
        self.function_view.horizontalHeader().resizeSection(0, 130)
        self.function_view.activated.connect(
            lambda index: self._show_function(self.function_model.key(index.row())))
        self.tabs.addTab(self._with_filter(self.function_filter, self.function_view),
                         t["code.functions"])
        splitter.addWidget(self.tabs)

        right = QSplitter(Qt.Orientation.Vertical)
        refs_box = QWidget()
        refs_layout = QVBoxLayout(refs_box)
        refs_layout.setContentsMargins(0, 0, 0, 0)
        self.refs_label = QLabel(t["code.references_none"])
        self.refs_label.setWordWrap(True)
        refs_layout.addWidget(self.refs_label)
        self.ref_model = _RowsModel([t["code.column_address"], t["code.column_function"],
                                     t["code.column_kind"], t["code.column_instruction"]], self)
        self.ref_view = _table(self.ref_model, 3, t, monospace=True)
        header = self.ref_view.horizontalHeader()
        header.resizeSection(0, 130)
        header.resizeSection(1, 220)
        header.resizeSection(2, 70)
        self.ref_view.activated.connect(self._reference_activated)
        refs_layout.addWidget(self.ref_view)
        right.addWidget(refs_box)

        function_box = QWidget()
        function_layout = QVBoxLayout(function_box)
        function_layout.setContentsMargins(0, 0, 0, 0)
        self.function_label = QLabel(t["code.function_none"])
        self.function_label.setWordWrap(True)
        self.function_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        function_layout.addWidget(self.function_label)
        actions = QHBoxLayout()
        self.callers_button = self._action(actions, "code.callers", "account_tree",
                                           self._show_callers)
        self.pattern_button = self._action(actions, "code.copy_pattern", "content_copy",
                                           self._copy_pattern)
        self.script_button = self._action(actions, "code.copy_script", "article",
                                          self._copy_script)
        self.monitor_button = self._action(actions, "code.monitor", "monitor_heart",
                                           self._monitor)
        self.memory_button = self._action(actions, "code.memory", "memory", self._memory)
        actions.addStretch()
        # Back to the function shown before, apart from the actions on this one
        self.back_button = QPushButton(t["code.back"])
        set_button_icon(self.back_button, "arrow_back")
        self.back_button.clicked.connect(self._back)
        actions.addWidget(self.back_button)
        function_layout.addLayout(actions)
        self.line_model = _RowsModel([t["code.column_address"], t["code.column_bytes"],
                                      t["code.column_instruction"], t["code.column_comment"]],
                                     self)
        self.line_view = _table(self.line_model, 3, t, monospace=True)
        header = self.line_view.horizontalHeader()
        header.resizeSection(0, 130)
        header.resizeSection(1, 190)
        header.resizeSection(2, 330)
        self.line_view.activated.connect(self._line_activated)
        self.line_view.selectionModel().currentRowChanged.connect(
            lambda _current, _previous: self._update_actions())
        self.function_tabs = QTabWidget()
        self.function_tabs.addTab(self.line_view, t["code.assembler"])
        self.pseudocode_view = QPlainTextEdit()
        self.pseudocode_view.setReadOnly(True)
        self.pseudocode_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.pseudocode_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.pseudocode_view.pseudocode_highlighter = PseudocodeHighlighter(
            self.pseudocode_view.document(), code_theme_colors())
        self.pseudocode_view.cursorPositionChanged.connect(self._pseudocode_cursor_moved)
        self.find_input = QLineEdit()
        self.find_status = QLabel("")
        self.pseudocode_page = self._pseudocode_page()
        self.function_tabs.addTab(self.pseudocode_page, t["code.pseudocode"])
        self.function_tabs.currentChanged.connect(lambda _index: self._update_pseudocode())
        QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self, self._focus_find)
        function_layout.addWidget(self.function_tabs)
        right.addWidget(function_box)
        right.setSizes([260, 520])
        splitter.addWidget(right)
        splitter.setSizes([420, 830])
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        self._update_actions()

    def _pseudocode_page(self):
        """The pseudocode with a find bar above it."""
        t = self.translations
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.find_input.setPlaceholderText(t["code.find_placeholder"])
        self.find_input.setClearButtonEnabled(True)
        self.find_input.textChanged.connect(lambda _text: self._find(again=False))
        self.find_input.returnPressed.connect(self._find)
        previous = QShortcut(QKeySequence("Shift+Return"), self.find_input,
                             lambda: self._find(backward=True))
        previous.setContext(Qt.ShortcutContext.WidgetShortcut)
        row.addWidget(self.find_input, 1)
        for icon, key, backward in (("expand_less", "code.find_previous", True),
                                    ("expand_more", "code.find_next", False)):
            button = QPushButton()
            set_button_icon(button, icon)
            button.setToolTip(t[key])
            button.clicked.connect(lambda _checked=False, back=backward: self._find(backward=back))
            row.addWidget(button)
        row.addWidget(self.find_status)
        box.addLayout(row)
        box.addWidget(self.pseudocode_view)
        return page

    @staticmethod
    def _with_filter(filter_input, view):
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(filter_input)
        box.addWidget(view)
        return widget

    def _action(self, layout, key, icon, handler):
        button = QPushButton(self.translations[key])
        set_button_icon(button, icon)
        button.setToolTip(self.translations[f"{key}_tooltip"])
        button.clicked.connect(handler)
        layout.addWidget(button)
        return button

    # --- modules -------------------------------------------------------------
    def _fill_modules(self):
        self.modules = process_modules(self.pid)
        self.module_combo.clear()
        for choice in self.modules:
            self.module_combo.addItem(choice.name, choice)
            self.module_combo.setItemData(self.module_combo.count() - 1, choice.path,
                                          Qt.ItemDataRole.ToolTipRole)
        if self.modules:
            self._load(self.modules[0])
        else:
            self.status_label.setText(self.translations["code.no_process"])

    def _module_chosen(self, position):
        if 0 <= position < len(self.modules) and self.modules[position] != self.module:
            self._load(self.modules[position])

    def _open_file(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, self.translations["code.open_file"],
            os.path.dirname(self.module.path) if self.module else "",
            self.translations["code.open_file_filter"])
        if not path:
            return
        position = next((position for position, module in enumerate(self.modules)
                         if module.path == path), None)
        if position is None:
            choice = ModuleChoice(os.path.basename(path), path, None)
            self.modules.append(choice)
            self.module_combo.addItem(choice.name, choice)
            self.module_combo.setItemData(self.module_combo.count() - 1, path,
                                          Qt.ItemDataRole.ToolTipRole)
            position = len(self.modules) - 1
        self.module_combo.setCurrentIndex(position)
        self._load(self.modules[position])

    def _load(self, choice):
        self._stop_worker()
        self.module = choice
        self.index = None
        self._history.clear()
        self._clear_views()
        cached = cached_index(choice.path)
        if cached is not None:
            self._on_built(cached)
            return
        self.progress.setValue(0)
        self.progress.show()
        self.status_label.setText(self.translations["code.loading"].format(path=choice.path))
        worker = CodeIndexWorker(choice.path)
        worker.progress.connect(self.progress.setValue)
        worker.built.connect(self._on_built)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        keep_until_finished(worker)
        worker.start()

    def _stop_worker(self):
        if self._worker is not None:
            self._worker.built.disconnect(self._on_built)
            self._worker.failed.disconnect(self._on_failed)
            self._worker.requestInterruption()
            self._worker = None

    def _on_built(self, index):
        if self.sender() is not None and self.sender() is not self._worker:
            return
        self._worker = None
        self.progress.hide()
        self.index = index
        image = index.image
        self.string_model.set_rows([(address, (f"0x{address:x}", text))
                                    for address, text, _kind in index.strings])
        self.function_model.set_rows([
            (function.start, (f"0x{function.start:x}", index.name(function.start),
                              f"{function.end - function.start}"))
            for function in index.functions if function.parent is None])
        self.status_label.setText(self.translations["code.loaded"].format(
            module=self.module.name, format=image.format.upper(), bits=image.bits,
            base=f"0x{image.base:x}", functions=len(index.functions),
            strings=len(index.strings), references=index.reference_count))
        self._update_actions()

    def _on_failed(self, message):
        if self.sender() is not self._worker:
            return
        self._worker = None
        self.progress.hide()
        self.status_label.setText(self.translations["code.load_failed"].format(message=message))

    def _clear_views(self):
        for model in (self.string_model, self.function_model, self.ref_model, self.line_model):
            model.set_rows([])
        self._function = None
        self._lines = []
        self._pseudocode_of = None
        self._pseudocode_lines = []
        self.pseudocode_view.clear()
        self.refs_label.setText(self.translations["code.references_none"])
        self.function_label.setText(self.translations["code.function_none"])
        self._update_actions()

    # --- addresses -------------------------------------------------------------
    def runtime(self, address):
        """Where file address ``address`` is in the process, or None."""
        if self.module is None or self.module.base is None or self.index is None:
            return None
        return address - self.index.image.base + self.module.base

    def _location(self, address):
        """module+0x… of a file address."""
        return f"{self.module.name}+0x{address - self.index.image.base:x}"

    # --- navigation ------------------------------------------------------------
    def _remember(self):
        if self._function is not None:
            self._history.append(("function", self._function.start, self._selected_address()))
        self._update_actions()

    def _back(self):
        if not self._history:
            return
        _kind, start, selected = self._history.pop()
        self._show_function(start, selected, remember=False)

    def _goto(self):
        if self.index is None:
            return
        address = parse_location(self.goto_input.text(), self.index.image, self.module.base,
                                 self.module.name)
        if address is None:
            self.status_label.setText(self.translations["code.goto_unknown"].format(
                text=self.goto_input.text().strip()))
            return
        self._navigate(address)

    def _navigate(self, address):
        """Code: show its function; data: who refers to it."""
        section = self.index.image.section_at(address)
        if section is not None and section.executable:
            self._show_function(address, address)
        else:
            self._show_references(address)

    def _string_activated(self, model_index):
        if self.index is None or not model_index.isValid():
            return
        self._show_references(self.string_model.key(model_index.row()))

    def _show_references(self, target):
        index = self.index
        what = index.describe(target)
        location = self._location(target)
        if not what:
            what = location
        elif not what.startswith(('"', "→")) and index.function_starting(target) is None:
            # A named global: module+offset is where a pointer chain starts
            what = f"{what}, {location}"
        refs = index.references(target)
        self.refs_label.setText(self.translations["code.references_to"].format(
            target=f"0x{target:x}", what=what, count=len(refs)))
        rows = []
        for ref in refs:
            section = index.image.section_at(ref.address)
            if section is not None and section.executable:
                line = next((line for line in index.disassemble(
                    _span(ref.address)) if line.address == ref.address), None)
                text = line.text if line else ""
            else:
                text = self.translations["code.pointer_in"].format(section=section.name
                                                                   if section else "?")
            rows.append((ref.address, (f"0x{ref.address:x}", index.where(ref.address),
                                       self.translations[f"code.kind_{ref.kind}"], text)))
        self.ref_model.set_rows(rows)
        self._update_actions()

    def _reference_activated(self, model_index):
        address = self.ref_model.key(model_index.row())
        section = self.index.image.section_at(address)
        if section is not None and section.executable:
            self._show_function(address, address)
        else:
            # A pointer in the data: who reads it
            self._show_references(address)

    def _show_function(self, address, selected=None, remember=True):
        index = self.index
        function = index.function_at(address)
        if function is None:
            self.status_label.setText(self.translations["code.no_function"].format(
                address=f"0x{address:x}"))
            return
        if remember:
            self._remember()
        owner = index.owner(function)
        self._function = function
        self._lines = index.disassemble(function)
        part = "" if owner is function else self.translations["code.function_part"].format(
            name=index.name(owner.start))
        bounded = "" if function.bounded else self.translations["code.function_unbounded"]
        self.function_label.setText(self.translations["code.function_header"].format(
            name=index.name(owner.start), start=f"0x{function.start:x}",
            end=f"0x{function.end:x}", location=self._location(function.start),
            callers=len(index.references(owner.start))) + part + bounded)
        signature = index.signature(owner.start)
        if signature is not None:
            self.function_label.setText(self.function_label.text() + "\n" + self.translations[
                "code.signature"].format(signature=signature.text(index.name(owner.start))))
        self.line_model.set_rows([(line.address, (f"0x{line.address:x}", line.raw.hex(" "),
                                                  line.text, line.comment))
                                  for line in self._lines])
        row = self.line_model.row_of(selected if selected is not None else function.start)
        if row is not None:
            self.line_view.selectRow(row)
            self.line_view.scrollTo(self.line_model.index(row, 0),
                                    QAbstractItemView.ScrollHint.PositionAtCenter)
        self._update_pseudocode()
        self._update_actions()

    def _update_pseudocode(self):
        """Render the function's pseudocode when its tab is shown: a large
        function with many calls takes a moment (their signatures are guessed)."""
        function = self._function
        if function is None or self.function_tabs.currentWidget() is not self.pseudocode_page:
            return
        if self._pseudocode_of is not function:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                text = render_pseudocode(self.index, function)
            finally:
                QGuiApplication.restoreOverrideCursor()
            self._syncing = True
            self.pseudocode_view.setPlainText(text)
            self._syncing = False
            self._pseudocode_of = function
            lines = []
            for number, line in enumerate(text.splitlines()):
                match = _PSEUDOCODE_ADDRESS.search(line)
                if match is not None:
                    lines.append((int(match.group(1), 16), number))
            self._pseudocode_lines = sorted(lines)
        line = self._selected_line()
        if line is not None:
            self._reveal_in_pseudocode(line.address)

    def _reveal_in_pseudocode(self, address):
        """Put the cursor on the pseudocode line of the instruction at
        ``address``; an instruction without its own line (folded into a later
        one, or prologue) shows the next line."""
        lines = self._pseudocode_lines
        if not lines:
            return
        position = bisect.bisect_left(lines, (address, -1))
        number = lines[min(position, len(lines) - 1)][1]
        block = self.pseudocode_view.document().findBlockByNumber(number)
        self._syncing = True
        self.pseudocode_view.setTextCursor(QTextCursor(block))
        self._syncing = False
        self._highlight_pseudocode_line()
        self.pseudocode_view.centerCursor()

    def _highlight_pseudocode_line(self):
        selection = QTextEdit.ExtraSelection()
        color = QColor(self.palette().highlight().color())
        color.setAlpha(70)
        selection.format.setBackground(color)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.pseudocode_view.textCursor()
        selection.cursor.clearSelection()
        self.pseudocode_view.setExtraSelections([selection])

    def _pseudocode_cursor_moved(self):
        """Select the instruction of the pseudocode line the cursor is on, so
        the actions (Monitor, Copy pattern …) work on it."""
        if self._syncing:
            return
        self._highlight_pseudocode_line()
        match = _PSEUDOCODE_ADDRESS.search(self.pseudocode_view.textCursor().block().text())
        if match is None:
            return
        row = self.line_model.row_of(int(match.group(1), 16))
        if row is not None:
            self.line_view.selectRow(row)
            self.line_view.scrollTo(self.line_model.index(row, 0),
                                    QAbstractItemView.ScrollHint.PositionAtCenter)

    def _focus_find(self):
        if self._function is None:
            return
        self.function_tabs.setCurrentWidget(self.pseudocode_page)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def _find(self, backward=False, again=True):
        """Find the text of the find bar in the pseudocode, from the cursor
        on (``again``: after the current match), wrapping around."""
        text = self.find_input.text()
        view = self.pseudocode_view
        self.find_status.setText("")
        if not text:
            return
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        cursor = view.textCursor()
        if not again:  # typing: the match may grow where it is
            cursor.setPosition(cursor.selectionStart())
            view.setTextCursor(cursor)
        if not view.find(text, flags):
            cursor.movePosition(QTextCursor.MoveOperation.End if backward
                                else QTextCursor.MoveOperation.Start)
            view.setTextCursor(cursor)
            if not view.find(text, flags):
                self.find_status.setText(self.translations["code.find_none"])
                return
        view.centerCursor()

    def _line_activated(self, model_index):
        line = self._line(model_index.row())
        if line is not None and line.target is not None:
            self._navigate(line.target)

    def _show_callers(self):
        if self._function is not None:
            self._show_references(self.index.owner(self._function).start)

    # --- the selected instruction ------------------------------------------------
    def _line(self, row):
        if row is None or row < 0 or row >= len(self._lines):
            return None
        return next((line for line in self._lines
                     if line.address == self.line_model.key(row)), None)

    def _selected_line(self):
        current = self.line_view.currentIndex()
        return self._line(current.row()) if current.isValid() else None

    def _selected_address(self):
        """The selected instruction, else the function's start."""
        line = self._selected_line()
        if line is not None:
            return line.address
        return self._function.start if self._function is not None else None

    def _update_actions(self):
        has_function = self.index is not None and self._function is not None
        for button in (self.callers_button, self.pattern_button, self.script_button):
            button.setEnabled(has_function)
        attached = has_function and self.module is not None and self.module.base is not None
        self.monitor_button.setEnabled(attached)
        self.memory_button.setEnabled(attached)
        self.back_button.setEnabled(bool(self._history))
        self.goto_button.setEnabled(self.index is not None)

    def _copy_pattern(self):
        address = self._selected_address()
        if address is None:
            return
        try:
            pattern = self.index.pattern(address)
        except ValueError as error:
            self.status_label.setText(messages.describe(error))
            return
        QGuiApplication.clipboard().setText(pattern.text)
        key = "code.pattern_copied" if pattern.matches == 1 else "code.pattern_ambiguous"
        self.status_label.setText(self.translations[key].format(
            address=f"0x{address:x}", length=pattern.length, matches=pattern.matches))

    def _copy_script(self):
        address = self._selected_address()
        if address is None:
            return
        module = None if self.module is None or self.module.program else self.module.name
        try:
            script = self.index.script(address, module)
        except ValueError as error:
            self.status_label.setText(messages.describe(error))
            return
        QGuiApplication.clipboard().setText(script)
        self.status_label.setText(self.translations["code.script_copied"].format(
            address=f"0x{address:x}"))

    def _monitor(self):
        address = self._selected_address()
        runtime = self.runtime(address) if address is not None else None
        if runtime is not None:
            self.monitor_requested.emit(runtime)

    def _memory(self):
        line = self._selected_line()
        address = line.target if line is not None and line.target is not None \
            else self._selected_address()
        runtime = self.runtime(address) if address is not None else None
        if runtime is not None:
            self.memory_requested.emit(runtime)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._stop_worker()
        super().closeEvent(event)


def _span(address):
    """A Function of one instruction's room, to disassemble it alone."""
    return Function(address, address + 16)
