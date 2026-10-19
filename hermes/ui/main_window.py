"""hermes-hook – Main Window: application main UI (PyQt6)."""

import itertools
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTableWidget, QTableWidgetItem, QLabel,
    QPushButton, QLineEdit, QComboBox, QGroupBox,
    QHeaderView, QStatusBar, QToolBar, QDialog,
    QAbstractItemView,
    QMessageBox, QFrame, QProgressBar, QMenu, QCheckBox, QTabWidget, QSizePolicy,
)
from PyQt6.QtCore import Qt, QSize, QTimer, QSettings  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor, QActionGroup  # pylint: disable=import-error,no-name-in-module

from hermes.ui.localization import load_translations
from hermes.ui.styles.styles import STYLE
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.glyph_icon import glyph_icon
from hermes.ui.widgets.process_list import ProcessSelectDialog
from hermes.ui.widgets.table import set_row_height
from hermes.core.pointer import (
    ProcessMemory,
    chain_for_static_address,
    module_bases,
    read_mappings,
    resolve_chain,
)
from hermes.core.freeze import FreezeWorker, FrozenValue
from hermes.core.scanner import (
    FLOAT_ROUNDING_MODES,
    FUZZY_MODES,
    PREVIOUS_VALUE_MODES,
    MemorySnapshot,
    ScanWorker,
    ValueRefreshWorker,
)
from hermes.core.memory import write_process_value
from hermes.engines import base as engine_base
from hermes.engines.registry import ENGINES, detect_engine
from hermes.features.catalog import catalog_method_from_identity
from hermes.core.process import is_wine_process, process_start_time
from hermes.core.wine_gadget import wine_bits
from hermes.core.value_types import DEFAULT_VALUE_TYPE, VALUE_TYPES, value_type as get_value_type
from hermes.core.write_watch import (
    WATCH_BACKENDS,
    BACKEND_FRIDA,
    BACKEND_PTRACE,
    create_watch_worker,
    watch_size_for_type,
)
from hermes.db.repository.addresses import AddressRepository, address_pointer
from hermes.db.repository.hooks import HookRepository
from hermes.db.repository.trace_filters import TraceFilterRepository
from hermes.hooks.engine import HookSpec
from hermes.hooks.transforms import example_templates
from hermes.ui.widgets.catalog import CatalogDialog
from hermes.ui.widgets.hooks import HookDialog, HookPanel
from hermes.ui.widgets.memory_view import MemoryViewerDialog
from hermes.core import logs, sessions
from hermes.ui.widgets import method_monitor, trace as trace_dialog
from hermes.ui.widgets.log_view import ErrorReporter, LogDialog, LogSignals
from hermes.ui.widgets.sessions import SessionsDialog, ShutdownDialog
from hermes.ui.widgets.method_monitor import MethodMonitorDialog
from hermes.ui.widgets.trace import TraceDialog
from hermes.ui.widgets.pointer_scan import PointerScanDialog
from hermes.ui.widgets.templates import TemplateDialog
from hermes.ui.widgets.write_watch import WriteWatchDialog


# How often to check whether the attached process is still running.
PROCESS_CHECK_INTERVAL_MS = 1000
# How long closing the window waits for the Frida workers of the C# windows
# (trace, method monitor) to remove their interceptors before it goes on;
# the shutdown dialog waits for what still runs then.
CLOSING_WORKER_TIMEOUT_MS = 3_000

# Upper bound for joining a write monitor from the GUI thread.
WRITE_WATCH_STOP_TIMEOUT_MS = 3000
# Scan results beyond this many rows are kept but not shown in the table.
RESULT_DISPLAY_LIMIT = 1000


@dataclass(frozen=True)
class ScanRequest:
    """Inputs required to start an initial or follow-up scan."""

    pid: int | None
    value_type: str
    scan_mode: str
    value: str
    value2: str = ""
    previous_results: list[dict[str, int | float | str]] | MemorySnapshot | None = None
    aligned: bool = True
    tolerance: str = ""
    float_rounding: str = "exact"


# ---------------------------------------------------------------------------
# Dialog: edit value
# ---------------------------------------------------------------------------
class ValueInputDialog(QDialog):
    # Dialogs expose behavior through Qt signals rather than public methods.
    # pylint: disable=too-few-public-methods
    """Simple dialog for entering a new value for a tracked address."""

    def __init__(self, address: str, current_value: str, value_type: str,
                 parent=None, translations=None):
        """Initialize the dialog and build its value editor."""
        super().__init__(parent)
        self.translations = translations or load_translations()
        self.setWindowTitle(self.translations["value_dialog.title"])
        self.setFixedSize(340, 160)
        self.new_value = None
        self._build_ui(address, current_value, value_type)

    def _build_ui(self, address, current, vtype):
        """Build the address details, input field, and dialog buttons."""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            f"{self.translations['value_dialog.address']}: "
            f"<span style='color:#3fb950'>{address}</span>"
            f"  &nbsp;{self.translations['value_dialog.type']}: "
            f"<span style='color:#f0883e'>{vtype}</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        self.input = QLineEdit(current)
        self.input.selectAll()
        self.input.setPlaceholderText(self.translations["value_dialog.placeholder"])
        layout.addWidget(self.input)

        layout.addLayout(dialog_button_row(
            self.translations, "button.write", self._accept, self.reject
        ))

    def _accept(self):
        """Store the entered value and accept the dialog."""
        self.new_value = self.input.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# Dialog: edit description
# ---------------------------------------------------------------------------
class DescriptionInputDialog(QDialog):
    # Dialogs expose behavior through Qt signals rather than public methods.
    # pylint: disable=too-few-public-methods
    """Simple dialog for entering a new description for a tracked address."""

    def __init__(self, address: str, current_description: str,
                 parent=None, translations=None):
        """Initialize the dialog and build its description editor."""
        super().__init__(parent)
        self.translations = translations or load_translations()
        self.setWindowTitle(self.translations["description_dialog.title"])
        self.setFixedSize(340, 160)
        self.new_description = None
        self._build_ui(address, current_description)

    def _build_ui(self, address, current):
        """Build the address details, input field, and dialog buttons."""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            f"{self.translations['value_dialog.address']}: "
            f"<span style='color:#3fb950'>{address}</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        self.input = QLineEdit(current)
        self.input.selectAll()
        self.input.setPlaceholderText(
            self.translations["description_dialog.placeholder"]
        )
        layout.addWidget(self.input)

        layout.addLayout(dialog_button_row(
            self.translations, "button.save", self._accept, self.reject
        ))

    def _accept(self):
        """Store the entered description and accept the dialog."""
        self.new_description = self.input.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Main application window for scanning and managing addresses."""

    # pylint: disable=too-many-instance-attributes,too-few-public-methods

    def __init__(self, language: str | None = None):
        """Initialize the main window and construct its interface."""
        super().__init__()
        self.translations = load_translations(language)
        self.setWindowTitle(self.translations["app.window_title"])
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)
        self.setStyleSheet(STYLE)

        self._current_pid = None
        self._process_start_time = None
        self._hook_repository = HookRepository()
        self._trace_filter_repository = TraceFilterRepository()
        self._address_repository = AddressRepository()
        self._process_id = None
        self._address_keys = itertools.count(1)
        self._filling_addresses = False
        self._pointer_dialogs = {}
        self._memory_viewer = None
        self._catalog = None
        self._method_monitor = None
        self._trace_dialog = None
        self._sessions_dialog = None
        self._log_dialog = None
        # The engine adapter of the attached game, once its runtime is found.
        self._engine = None
        # Database id of the program the open pointer dialogs belong to.
        self._pointer_program = None
        self._ensure_example_templates()
        self._current_proc = self.translations["app.no_process"]
        self._scan_results = []
        self._scan_worker = None
        self._scan_failed = False
        self._refresh_worker = None
        self._freeze_worker = None
        self._write_watch_worker = None
        self._write_watch_dialog = None
        self._write_watch_stopping = False
        self._settings = QSettings("hermes-hook", "hermes-hook")
        self._watch_backend = self._settings.value("watch_backend", BACKEND_FRIDA)
        if self._watch_backend not in WATCH_BACKENDS:
            self._watch_backend = BACKEND_FRIDA
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh_memory_values)
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(PROCESS_CHECK_INTERVAL_MS)
        self._process_timer.timeout.connect(self._check_process_alive)

        # Initialize UI attributes assigned in builder methods.
        self.lbl_result_count = None
        self.result_table = None
        self.combo_type = None
        self.combo_scan_type = None
        self.chk_aligned = None
        self.input_value = None
        self.lbl_value2 = None
        self.input_value2 = None
        self.lbl_tolerance = None
        self.input_tolerance = None
        self.lbl_float_rounding = None
        self.combo_float_rounding = None
        self.btn_first_scan = None
        self.btn_next_scan = None
        self.btn_reset = None
        self.lbl_scan_status = None
        self.progress_scan = None
        self.addr_table = None
        self.hook_panel = None
        self.bottom_tabs = None
        self.lbl_backend = None

        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def _build_menu(self):
        """Create the menu bar and the watch-backend switch."""
        menu_bar = self.menuBar()
        settings_menu = menu_bar.addMenu(self.translations["menu.settings"])
        # "&&" keeps Qt from reading the "&" as a mnemonic marker.
        backend_menu = settings_menu.addMenu(
            self.translations["menu.watch_backend"].replace("&", "&&"))

        group = QActionGroup(self)
        group.setExclusive(True)
        self._backend_actions = {}
        for backend, label in (
            (BACKEND_FRIDA, "menu.backend_frida"),
            (BACKEND_PTRACE, "menu.backend_ptrace"),
        ):
            action = backend_menu.addAction(self.translations[label])
            action.setCheckable(True)
            action.setChecked(self._watch_backend == backend)
            action.setToolTip(self.translations[f"{label}_tooltip"])
            action.triggered.connect(
                lambda _checked, value=backend: self._set_watch_backend(value)
            )
            group.addAction(action)
            self._backend_actions[backend] = action

        view_menu = menu_bar.addMenu(self.translations["menu.view"])
        view_menu.setToolTipsVisible(True)

        self.act_templates = view_menu.addAction(
            glyph_icon("⧉"), self.translations["menu.templates"])
        self.act_templates.setToolTip(self.translations["templates.button_tooltip"])
        self.act_templates.triggered.connect(self._open_template_dialog)

        view_menu.addSeparator()

        self.act_catalog = view_menu.addAction(
            glyph_icon("{ }"), self.translations["menu.catalog"])
        self.act_catalog.triggered.connect(self._open_catalog)

        self.act_trace = view_menu.addAction(
            glyph_icon("◉"), self.translations["menu.trace"])
        self.act_trace.triggered.connect(self._open_trace)
        self._update_catalog_button()

    def _set_watch_backend(self, backend):
        """Apply a backend the user picked by hand and remember it."""
        self._apply_backend(backend, persist=True)

    def _apply_backend(self, backend, *, persist):
        """Make ``backend`` the active watch/hook backend and reflect it in the UI.

        ``persist`` writes the choice to settings (a manual pick); automatic
        selection on attach leaves the saved default untouched.
        """
        self._watch_backend = backend
        if persist:
            self._settings.setValue("watch_backend", backend)
        if self.hook_panel is not None:
            self.hook_panel.set_backend(backend)
        action = self._backend_actions.get(backend)
        if action is not None:
            action.setChecked(True)  # keeps the menu in sync on auto-select
        self._update_backend_label()

    def _auto_select_backend(self, pid):
        """Pick Frida, except for 32-bit Wine/Proton games: ptrace.

        Frida reaches 64-bit Wine games through its Windows gadget
        (wine_gadget.py), which cannot be loaded into 32-bit ones yet.
        """
        wine_32bit = is_wine_process(pid) and wine_bits(pid) == 32
        backend = BACKEND_PTRACE if wine_32bit else BACKEND_FRIDA
        self._apply_backend(backend, persist=False)

    def _update_backend_label(self):
        """Show the active backend in the status bar, if it exists yet."""
        if getattr(self, "lbl_backend", None) is None:
            return
        name = self.translations[f"status.backend_{self._watch_backend}"]
        self.lbl_backend.setText(self.translations["status.backend"].format(backend=name))

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------
    def _build_toolbar(self):
        """Create the application toolbar and process controls."""
        tb = QToolBar(self.translations["app.brand"])
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        title = QLabel(self.translations["app.brand"])
        title.setObjectName("lbl_title")
        title.setContentsMargins(8, 0, 16, 0)
        tb.addWidget(title)

        tb.addSeparator()

        self.btn_process = QPushButton(self.translations["process.select"])
        self.btn_process.clicked.connect(self._open_process_dialog)
        tb.addWidget(self.btn_process)

        tb.addSeparator()

        self.lbl_proc_info = QLabel(f"  {self._current_proc}")
        self.lbl_proc_info.setObjectName("lbl_process")
        tb.addWidget(self.lbl_proc_info)

        # The memory viewer stands apart on the right.
        spacer = QWidget()
        spacer.setObjectName("toolbar_spacer")
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.btn_sessions = QPushButton()
        self.btn_sessions.clicked.connect(self._open_sessions)
        tb.addWidget(self.btn_sessions)
        self._session_signals = sessions.SessionSignals(self)
        self._session_signals.changed.connect(self._update_sessions_button)
        # A session asked to stop becomes "stuck" with time alone.
        self._sessions_timer = QTimer(self)
        self._sessions_timer.setInterval(1000)
        self._sessions_timer.timeout.connect(self._update_sessions_button)
        self._sessions_timer.start()
        self._update_sessions_button()

        self.btn_memory = QPushButton(self.translations["memview.button"])
        self.btn_memory.setToolTip(self.translations["memview.button_tooltip"])
        self.btn_memory.clicked.connect(lambda: self._open_memory_viewer())
        tb.addWidget(self.btn_memory)

    # ------------------------------------------------------------------
    # Central layout
    # ------------------------------------------------------------------
    def _build_central(self):
        """Assemble the central split-pane layout."""
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Main splitter: top (left + right panels), bottom (address table)
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(main_splitter)

        # --- Upper area ---
        top_splitter = QSplitter(Qt.Orientation.Horizontal)

        top_splitter.addWidget(self._build_results_panel())
        top_splitter.addWidget(self._build_search_panel())
        top_splitter.setSizes([520, 340])

        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(self._build_bottom_tabs())
        main_splitter.setSizes([420, 220])

    def _build_bottom_tabs(self):
        """Put tracked addresses and hooks into tabs below the scan area."""
        self.bottom_tabs = QTabWidget()
        address_box = self._build_address_table()
        address_box.setTitle("")
        address_box.setObjectName("tab_page")
        self.bottom_tabs.addTab(address_box, self.translations["address_table.title"])

        self.hook_panel = HookPanel(
            self.translations, self, repository=self._hook_repository
        )
        self.hook_panel.set_backend(self._watch_backend)
        self.hook_panel.setTitle("")
        self.hook_panel.setObjectName("tab_page")
        self.hook_panel.count_changed.connect(self._on_hook_count_changed)
        self.hook_panel.watch_requested.connect(self._watch_hook_target)
        self.hook_panel.monitor_requested.connect(
            lambda identity: self._monitor_method(catalog_method_from_identity(identity)))
        self.bottom_tabs.addTab(self.hook_panel, self.translations["hooks.title"])
        return self.bottom_tabs

    def _on_hook_count_changed(self, count):
        """Show the number of hooks in the tab title."""
        if self.bottom_tabs is None or self.hook_panel is None:
            return
        title = self.translations["hooks.title"]
        self.bottom_tabs.setTabText(
            self.bottom_tabs.indexOf(self.hook_panel),
            f"{title} ({count})" if count else title,
        )

    # ------------------------------------------------------------------
    # Left panel: scan results
    # ------------------------------------------------------------------
    def _build_results_panel(self):
        """Create the scan-results table and its controls."""
        box = QGroupBox(self.translations["results.title"])
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        # Control bar above the list
        ctrl = QHBoxLayout()
        self.lbl_result_count = QLabel(
            self.translations["results.count"].format(count=0, shown=0)
        )
        self.lbl_result_count.setStyleSheet("color:#8b949e; font-size:11px;")
        ctrl.addWidget(self.lbl_result_count)
        ctrl.addStretch()

        btn_add = QPushButton(self.translations["results.add"])
        btn_add.setToolTip(self.translations["results.add_tooltip"])
        btn_add.clicked.connect(self._add_selected_to_table)
        ctrl.addWidget(btn_add)

        layout.addLayout(ctrl)

        self.result_table = QTableWidget(0, 3)

        self.result_table.setHorizontalHeaderLabels([
            self.translations["address_table.address"],
            self.translations["address_table.type"],
            self.translations["address_table.value"],
        ])

        self._style_table(self.result_table)
        self.result_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.result_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.result_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.result_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.result_table.customContextMenuRequested.connect(
            self._show_result_context_menu
        )
        header = self.result_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        header = self.result_table.verticalHeader()
        if header is not None:
            header.setVisible(False)

        self.result_table.doubleClicked.connect(self._add_selected_to_table)
        layout.addWidget(self.result_table)

        return box

    # ------------------------------------------------------------------
    # Right panel: search
    # ------------------------------------------------------------------
    def _build_search_panel(self):
        """Create scan options, input fields, actions, and progress display."""
        box = QGroupBox(self.translations["search.title"])
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        self._build_scan_buttons(layout)

        # Data type selector
        layout.addWidget(QLabel(self.translations["search.datatype"]))
        self.combo_type = QComboBox()
        for value_type in VALUE_TYPES:
            self.combo_type.addItem(value_type, value_type)
        self.combo_type.addItem(
            self.translations["search.auto_type"], "auto"
        )
        self.combo_type.setItemData(
            self.combo_type.count() - 1,
            self.translations["search.auto_type_tooltip"],
            Qt.ItemDataRole.ToolTipRole,
        )
        self.combo_type.setCurrentIndex(self.combo_type.findData(DEFAULT_VALUE_TYPE))
        layout.addWidget(self.combo_type)

        # Scan mode
        layout.addWidget(QLabel(self.translations["search.scan_type"]))
        self.combo_scan_type = QComboBox()
        scan_modes = [
            ("search.exact", "exact"),
            ("search.greater_than", "greater_than"),
            ("search.less_than", "less_than"),
            ("search.between", "between"),
            ("search.unknown_start", "unknown_start"),
            ("search.unchanged", "unchanged"),
            ("search.changed", "changed"),
            ("search.increased", "increased"),
            ("search.decreased", "decreased"),
            ("search.increased_by", "increased_by"),
            ("search.decreased_by", "decreased_by"),
            ("search.changed_by", "changed_by"),
        ]
        for text_key, mode in scan_modes:
            self.combo_scan_type.addItem(self.translations[text_key], mode)
        layout.addWidget(self.combo_scan_type)

        # Only addresses that are a multiple of the value size (first scan)
        self.chk_aligned = QCheckBox(self.translations["search.aligned"])
        self.chk_aligned.setToolTip(self.translations["search.aligned_tooltip"])
        self.chk_aligned.setChecked(True)
        layout.addWidget(self.chk_aligned)

        # Value input
        layout.addWidget(QLabel(self.translations["search.value"]))
        self.input_value = QLineEdit()
        self.input_value.setPlaceholderText(self.translations["search.value_placeholder"])
        layout.addWidget(self.input_value)

        # Second value (for "between" range scan)
        self.lbl_value2 = QLabel(self.translations["search.upper_bound"])
        self.lbl_value2.setVisible(False)
        layout.addWidget(self.lbl_value2)
        self.input_value2 = QLineEdit()
        self.input_value2.setPlaceholderText(
            self.translations["search.upper_bound_placeholder"]
        )
        self.input_value2.setVisible(False)
        layout.addWidget(self.input_value2)

        # Tolerance and float comparison (for exact and "changed by" scans)
        self.lbl_tolerance = QLabel(self.translations["search.tolerance"])
        layout.addWidget(self.lbl_tolerance)
        self.input_tolerance = QLineEdit()
        self.input_tolerance.setPlaceholderText(
            self.translations["search.tolerance_placeholder"]
        )
        self.input_tolerance.setToolTip(self.translations["search.tolerance_tooltip"])
        layout.addWidget(self.input_tolerance)
        self.lbl_float_rounding = QLabel(self.translations["search.float_rounding"])
        layout.addWidget(self.lbl_float_rounding)
        self.combo_float_rounding = QComboBox()
        for mode in FLOAT_ROUNDING_MODES:
            self.combo_float_rounding.addItem(
                self.translations[f"search.float_{mode}"], mode
            )
        self.combo_float_rounding.setToolTip(
            self.translations["search.float_rounding_tooltip"]
        )
        self.combo_float_rounding.setCurrentIndex(
            self.combo_float_rounding.findData("rounded")
        )
        layout.addWidget(self.combo_float_rounding)

        self.combo_scan_type.currentIndexChanged.connect(self._update_value_inputs)
        self.combo_type.currentIndexChanged.connect(self._update_value_inputs)
        self._update_value_inputs()

        # Enter in a value field starts the highlighted scan.
        for field in (self.input_value, self.input_value2, self.input_tolerance):
            field.returnPressed.connect(self._start_default_scan)

        self._build_scan_progress(layout)

        return box

    def _build_scan_buttons(self, layout):
        """Add the scan buttons side by side."""
        row = QHBoxLayout()
        self.btn_first_scan = QPushButton(self.translations["search.first_scan"])
        self.btn_first_scan.clicked.connect(self._first_scan)
        row.addWidget(self.btn_first_scan)

        self.btn_next_scan = QPushButton(self.translations["search.next_scan"])
        self.btn_next_scan.setEnabled(False)
        self.btn_next_scan.clicked.connect(self._next_scan)
        row.addWidget(self.btn_next_scan)

        self.btn_reset = QPushButton(self.translations["search.reset"])
        self.btn_reset.setObjectName("btn_danger")
        self.btn_reset.setEnabled(False)
        self.btn_reset.clicked.connect(self._reset_scan)
        row.addWidget(self.btn_reset)
        layout.addLayout(row)

        # Divider
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

    def _start_default_scan(self):
        """Run the next scan if there are results, otherwise the first scan."""
        if self.btn_next_scan is not None and self.btn_next_scan.isEnabled():
            self._next_scan()
        elif self.btn_first_scan is not None and self.btn_first_scan.isEnabled():
            self._first_scan()

    def _build_scan_progress(self, layout):
        """Add the progress display and status text below the scan options."""
        layout.addStretch()

        self.progress_scan = QProgressBar()
        self.progress_scan.setRange(0, 100)
        self.progress_scan.setValue(0)
        self.progress_scan.setVisible(False)
        layout.addWidget(self.progress_scan)

        # Status label
        self.lbl_scan_status = QLabel(self.translations["search.ready"])
        self.lbl_scan_status.setStyleSheet("color:#8b949e; font-size:11px;")
        self.lbl_scan_status.setWordWrap(True)
        layout.addWidget(self.lbl_scan_status)

        self._set_primary(self.btn_first_scan, True)

    def _update_value_inputs(self, _index=None):
        """Show only the value inputs that the selected scan type uses."""
        if (self.combo_scan_type is None or self.combo_type is None or
            self.lbl_value2 is None or self.input_value2 is None or
            self.lbl_tolerance is None or self.input_tolerance is None or
            self.lbl_float_rounding is None or self.combo_float_rounding is None):
            return
        scan_mode = self.combo_scan_type.currentData()
        between = scan_mode == "between"
        self.lbl_value2.setVisible(between)
        self.input_value2.setVisible(between)
        fuzzy = scan_mode in FUZZY_MODES
        self.lbl_tolerance.setVisible(fuzzy)
        self.input_tolerance.setVisible(fuzzy)
        value_type = self.combo_type.currentData()
        floats = value_type == "auto" or not VALUE_TYPES[value_type].is_integer
        self.lbl_float_rounding.setVisible(fuzzy and floats)
        self.combo_float_rounding.setVisible(fuzzy and floats)

    def _fuzzy_options(self):
        """Return the tolerance and float rounding entered for a scan."""
        tolerance = (
            self.input_tolerance.text().strip()
            if self.input_tolerance is not None else ""
        )
        float_rounding = (
            self.combo_float_rounding.currentData()
            if self.combo_float_rounding is not None else "exact"
        )
        return {"tolerance": tolerance, "float_rounding": float_rounding}

    # ------------------------------------------------------------------
    # Bottom panel: tracked addresses
    # ------------------------------------------------------------------
    def _build_address_table(self):
        """Create the tracked-address table and its action buttons."""
        box = QGroupBox(self.translations["address_table.title"])
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        # Table toolbar
        ctrl = QHBoxLayout()
        btn_write = QPushButton(self.translations["address_table.write"])
        btn_write.clicked.connect(self._write_value)
        btn_freeze = QPushButton(self.translations["address_table.freeze"])
        btn_freeze.setToolTip(self.translations["address_table.freeze_tooltip"])
        btn_freeze.clicked.connect(self._toggle_freeze)
        btn_remove = QPushButton(self.translations["address_table.remove"])
        btn_remove.setObjectName("btn_danger")
        btn_remove.clicked.connect(self._remove_address)
        ctrl.addWidget(btn_write)
        ctrl.addWidget(btn_freeze)
        ctrl.addStretch()
        ctrl.addWidget(btn_remove)
        layout.addLayout(ctrl)

        # Table
        self.addr_table = QTableWidget(0, 6)
        self.addr_table.setHorizontalHeaderLabels(
            [
                self.translations["address_table.description"],
                self.translations["address_table.address"],
                self.translations["address_table.type"],
                self.translations["address_table.value"],
                self.translations["address_table.frozen"],
                self.translations["address_table.pointer"],
            ]
        )

        self._style_table(self.addr_table)
        self.addr_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.addr_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.addr_table.customContextMenuRequested.connect(
            self._show_tracked_context_menu
        )
        header = self.addr_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            self.addr_table.setColumnWidth(0, 260)
            for col in (1, 2, 3, 4):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        header = self.addr_table.verticalHeader()
        if header is not None:
            header.setVisible(False)

        self.addr_table.doubleClicked.connect(self._write_value)
        self.addr_table.itemChanged.connect(self._on_address_item_changed)
        layout.addWidget(self.addr_table)

        return box

    def _add_address_row(self, desc, addr, vtype, value, frozen_value=None,
                         saved_id=None, pointer=None):
        # pylint: disable=too-many-arguments
        """Append an address and its current state to the tracking table.

        ``frozen_value`` is the value (text) the entry is frozen at, or None.
        ``saved_id`` is the entry's database id, ``pointer`` its PointerChain;
        ``addr`` may be None when the pointer does not resolve right now.
        """
        if self.addr_table is None:
            return

        self._filling_addresses = True
        row = self.addr_table.rowCount()
        self.addr_table.insertRow(row)

        desc_item = QTableWidgetItem(desc)
        desc_item.setFlags(desc_item.flags() | Qt.ItemFlag.ItemIsEditable)
        desc_item.setData(Qt.ItemDataRole.UserRole, {
            "key": next(self._address_keys), "id": saved_id, "pointer": pointer,
        })
        self.addr_table.setItem(row, 0, desc_item)

        addr_item = QTableWidgetItem()
        addr_item.setFlags(addr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 1, addr_item)
        self._set_row_address(row, addr)

        type_item = QTableWidgetItem(vtype)
        type_item.setForeground(QColor("#f0883e"))
        type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 2, type_item)

        val_item = QTableWidgetItem(value)
        val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 3, val_item)

        freeze_item = QTableWidgetItem()
        freeze_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        freeze_item.setFlags(freeze_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 4, freeze_item)

        pointer_item = QTableWidgetItem()
        pointer_item.setFlags(pointer_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 5, pointer_item)
        self._set_row_pointer(row, pointer)
        self._filling_addresses = False
        self._set_row_frozen(row, frozen_value)

    # ------------------------------------------------------------------
    # Saved address list and pointers
    # ------------------------------------------------------------------
    def _address_meta(self, row):
        """Return ``{"key", "id", "pointer"}`` of an address-list row."""
        item = self.addr_table.item(row, 0) if self.addr_table is not None else None
        meta = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return meta or {"key": None, "id": None, "pointer": None}

    def _set_row_address(self, row, address):
        """Show an address; None marks a pointer that does not resolve now."""
        item = self.addr_table.item(row, 1)
        if item is None:
            return
        self._filling_addresses = True
        if address is None:
            item.setText(self.translations["address_table.unresolved"])
            item.setForeground(QColor("#8b949e"))
            item.setToolTip(self.translations["address_table.unresolved_tooltip"])
        else:
            item.setText(address if isinstance(address, str) else f"0x{address:X}")
            item.setForeground(QColor("#3fb950"))
            item.setToolTip("")
        self._filling_addresses = False

    def _set_row_pointer(self, row, pointer):
        """Store and show the PointerChain of a row."""
        desc_item = self.addr_table.item(row, 0)
        item = self.addr_table.item(row, 5)
        if desc_item is None or item is None:
            return
        self._filling_addresses = True
        meta = dict(self._address_meta(row))
        meta["pointer"] = pointer
        desc_item.setData(Qt.ItemDataRole.UserRole, meta)
        if pointer is None:
            item.setText("—")
            item.setForeground(QColor("#8b949e"))
            item.setToolTip(self.translations["address_table.pointer_none"])
        else:
            item.setText(pointer.text())
            item.setForeground(QColor("#d2a8ff"))
            item.setToolTip(self.translations["address_table.pointer_tooltip"])
        self._filling_addresses = False

    def _store_address(self, action, *args, **kwargs):
        """Run an address repository call; report failures instead of raising."""
        try:
            return action(*args, **kwargs)
        except SQLAlchemyError as error:
            self.lbl_status.setText(self.translations["hooks.db_failed"].format(message=error))
            return None

    def _load_saved_addresses(self):
        """Show the saved address list of the attached program."""
        if self.addr_table is None or self._process_id is None:
            return
        saved = self._store_address(self._address_repository.list_addresses, self._process_id)
        if saved is None:
            return
        # The rows are rebuilt with new keys; frozen ones register again.
        self._stop_freeze_worker()
        self.addr_table.setRowCount(0)
        bases, memory = None, None
        try:
            if any(entry.pointer is not None for entry in saved):
                bases = module_bases(read_mappings(self._current_pid))
                memory = ProcessMemory(self._current_pid)
            for entry in saved:
                pointer = address_pointer(entry)
                address = entry.address
                if pointer is not None:
                    address = resolve_chain(memory, bases, pointer) if memory else None
                # Without a pointer the saved address may belong to an earlier
                # run of the program; writing a frozen value there is unsafe.
                frozen_value = entry.frozen_value if entry.frozen else None
                if pointer is None and frozen_value is not None:
                    frozen_value = None
                    self._store_address(
                        self._address_repository.update_address, entry.id,
                        frozen=False, frozen_value=None,
                    )
                self._add_address_row(
                    entry.label, address, entry.value_type, "?", frozen_value,
                    saved_id=entry.id, pointer=pointer,
                )
        except OSError as error:
            self.lbl_status.setText(
                self.translations["address_table.load_failed"].format(message=error)
            )
        finally:
            if memory is not None:
                memory.close()
        if self.addr_table.rowCount():
            self._refresh_timer.start()

    def _on_address_item_changed(self, item):
        """Save an edited description."""
        if self._filling_addresses or item.column() != 0:
            return
        meta = self._address_meta(item.row())
        if meta["id"] is not None:
            self._store_address(
                self._address_repository.update_address, meta["id"], label=item.text()
            )

    def _on_addresses_resolved(self, resolved):
        """Show where pointer entries point now; remember changed addresses."""
        if self.addr_table is None:
            return
        for row in range(self.addr_table.rowCount()):
            meta = self._address_meta(row)
            if meta["key"] not in resolved:
                continue
            address = resolved[meta["key"]]
            item = self.addr_table.item(row, 1)
            shown = item.text() if item is not None else ""
            new_text = self.translations["address_table.unresolved"] if address is None \
                else f"0x{address:X}"
            if shown == new_text:
                continue
            self._set_row_address(row, address)
            if address is not None and meta["id"] is not None:
                self._store_address(
                    self._address_repository.update_address, meta["id"], address=address
                )

    def _open_pointer_scan(self, row):
        """Search pointer chains for an address-list entry."""
        meta = self._address_meta(row)
        if self._current_pid is None or meta["id"] is None:
            return
        try:
            address = int(self.addr_table.item(row, 1).text(), 0)
        except (AttributeError, ValueError):
            address = 0
        entry = {
            "id": meta["id"],
            "label": self.addr_table.item(row, 0).text(),
            "address": address,
            "type": self.addr_table.item(row, 2).text(),
        }
        dialog = self._pointer_dialogs.get(meta["id"])
        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = PointerScanDialog(
            self.translations, self._address_repository, self._current_pid, entry, self,
            address_source=self._selected_scan_address,
        )
        entry_id = meta["id"]
        self._pointer_dialogs[entry_id] = dialog
        self._pointer_program = self._process_id
        dialog.chain_chosen.connect(lambda chain: self._set_pointer_for(entry_id, chain))
        dialog.chains_adopted.connect(
            lambda chains: dialog.show_adopted(self._add_pointer_entries(entry_id, chains))
        )
        dialog.finished.connect(lambda _result: self._pointer_dialogs.pop(entry_id, None))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _selected_scan_address(self):
        """Return the address of the selected scan result, or None."""
        if self.result_table is None:
            return None
        row = self.result_table.currentRow()
        if row < 0 or not self.result_table.selectionModel().hasSelection():
            return None
        item = self.result_table.item(row, 0)
        metadata = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return metadata["address_value"] if metadata else None

    def _set_pointer_for(self, entry_id, chain):
        """Set the pointer of the address-list entry with database id ``entry_id``."""
        for row in range(self.addr_table.rowCount()):
            if self._address_meta(row)["id"] == entry_id:
                self._set_pointer(row, chain)
                return

    def _add_pointer_entries(self, entry_id, chains):
        """Add pointer candidates of an entry as entries of their own.

        After a restart the address list then shows which of them still lead
        to a sensible value. Chains already in the list are skipped; returns
        the number of entries added, or None if the entry is gone.
        """
        source = next(
            (row for row in range(self.addr_table.rowCount())
             if self._address_meta(row)["id"] == entry_id),
            None,
        )
        if source is None or self._current_pid is None or self._process_id is None:
            return None
        label = self.addr_table.item(source, 0).text()
        value_type = self.addr_table.item(source, 2).text()
        existing = {
            self._address_meta(row)["pointer"] for row in range(self.addr_table.rowCount())
        }
        chains = [chain for chain in chains if chain not in existing]
        try:
            bases = module_bases(read_mappings(self._current_pid))
            with ProcessMemory(self._current_pid) as memory:
                addresses = [resolve_chain(memory, bases, chain) for chain in chains]
        except OSError:
            addresses = [None] * len(chains)
        for index, (chain, address) in enumerate(zip(chains, addresses), start=1):
            entry_label = self.translations["address_table.pointer_candidate"].format(
                label=label, index=index
            )
            saved_id = self._store_address(
                self._address_repository.add_address, self._process_id,
                label=entry_label, value_type=value_type, address=address or 0,
                pointer=chain,
            )
            self._add_address_row(
                entry_label, address, value_type, "?", saved_id=saved_id, pointer=chain,
            )
        if chains:
            self._refresh_timer.start()
        return len(chains)

    def _update_catalog_button(self):
        """Enable the catalog and the trace for games whose engine hermes supports."""
        if self._current_pid is not None and self._engine is None:
            self._engine = detect_engine(self._current_pid)
        unsupported = self.translations["catalog.button_no_engine"].format(
            engines=", ".join(engine.display_name for engine in ENGINES))
        for action, tooltip, capability in (
                (self.act_catalog, "catalog.button_tooltip", engine_base.CATALOG),
                (self.act_trace, "trace.button_tooltip", engine_base.TRACE)):
            enabled = (self._current_pid is not None and self._engine is not None
                       and self._engine.supports(capability))
            action.setEnabled(enabled)
            action.setToolTip(self.translations[tooltip] if enabled else unsupported)

    def _open_catalog(self):
        """Show the catalog of the C# methods of the attached game (see hermes/engines)."""
        if self._current_pid is None or self._engine is None:
            return
        catalog = self._catalog
        if catalog is None:
            catalog = CatalogDialog(self.translations, self._current_pid, parent=self,
                                    engine=self._engine)
            catalog.finished.connect(lambda _result: setattr(self, "_catalog", None))
            catalog.monitor_requested.connect(self._monitor_method)
            catalog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._catalog = catalog
        catalog.show()
        catalog.raise_()
        catalog.activateWindow()

    def _update_sessions_button(self):
        """Show how many workers are attached, and whether one hangs."""
        running = sessions.SESSIONS.sessions()
        stuck = any(session.stuck() for session in running)
        self.btn_sessions.setText(
            self.translations["sessions.button"].format(count=len(running)))
        self.btn_sessions.setToolTip(self.translations[
            "sessions.button_stuck_tooltip" if stuck else "sessions.button_tooltip"])
        name = "btn_danger" if stuck else ""
        if self.btn_sessions.objectName() != name:
            self.btn_sessions.setObjectName(name)
            self.btn_sessions.style().unpolish(self.btn_sessions)
            self.btn_sessions.style().polish(self.btn_sessions)

    def _update_log_button(self):
        """Name the errors logged since the start, if any."""
        count = logs.error_count()
        self.btn_log.setText(self.translations["log.button_errors"].format(count=count)
                             if count else self.translations["log.button"])
        self.btn_log.setToolTip(self.translations["log.button_tooltip"].format(
            path=logs.log_path()))
        name = "btn_danger" if count else ""
        if self.btn_log.objectName() != name:
            self.btn_log.setObjectName(name)
            self.btn_log.setFlat(not count)
            self.btn_log.style().unpolish(self.btn_log)
            self.btn_log.style().polish(self.btn_log)

    def _open_log(self):
        """Show the application log."""
        dialog = self._log_dialog
        if dialog is None:
            dialog = LogDialog(self.translations, parent=self)
            dialog.finished.connect(lambda _result: setattr(self, "_log_dialog", None))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._log_dialog = dialog
        else:
            dialog.refresh()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_sessions(self):
        """Show the workers attached to processes, to end them if need be."""
        dialog = self._sessions_dialog
        if dialog is None:
            dialog = SessionsDialog(self.translations, lambda: self._current_pid, parent=self)
            dialog.finished.connect(lambda _result: setattr(self, "_sessions_dialog", None))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._sessions_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_trace(self):
        """Show the trace of the C# methods a scenario in the game runs."""
        if self._current_pid is None or self._engine is None:
            return
        trace = self._trace_dialog
        if trace is None:
            trace = TraceDialog(self.translations, self._current_pid, parent=self,
                                engine=self._engine,
                                filter_repository=self._trace_filter_repository)
            trace.finished.connect(lambda _result: setattr(self, "_trace_dialog", None))
            trace.monitor_requested.connect(self._monitor_method)
            if self.hook_panel is not None:
                trace.hook_requested.connect(self.hook_panel.create_method_hook)
            trace.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._trace_dialog = trace
        trace.show()
        trace.raise_()
        trace.activateWindow()

    def _monitor_method(self, method):
        """Log the calls of a C# method chosen in the catalog."""
        if self._current_pid is None:
            return
        monitor = self._method_monitor
        if monitor is None:
            monitor = MethodMonitorDialog(self.translations, self._current_pid, parent=self)
            if self.hook_panel is not None:
                monitor.hook_requested.connect(self.hook_panel.create_method_hook)
            monitor.finished.connect(lambda _result: setattr(self, "_method_monitor", None))
            monitor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._method_monitor = monitor
        monitor.show()
        monitor.add_method(method)
        monitor.activateWindow()

    def _close_catalog(self):
        """Close the catalog, the trace and the method monitor (process gone or switched)."""
        for name in ("_catalog", "_trace_dialog", "_method_monitor"):
            dialog = getattr(self, name)
            if dialog is not None:
                dialog.close()
                setattr(self, name, None)

    def _open_memory_viewer(self, address=None):
        """Show the memory viewer, at ``address`` if given."""
        if self._current_pid is None:
            QMessageBox.warning(
                self,
                self.translations["warning.no_process_title"],
                self.translations["warning.select_process"],
            )
            return
        viewer = self._memory_viewer
        if viewer is None:
            viewer = MemoryViewerDialog(self.translations, self._current_pid, parent=self)
            viewer.finished.connect(lambda _result: setattr(self, "_memory_viewer", None))
            viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._memory_viewer = viewer
        if address is not None:
            viewer.go_to(address)
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()

    def _close_pointer_dialogs(self):
        """Close open pointer scans; they belong to another program."""
        for dialog in list(self._pointer_dialogs.values()):
            dialog.reject()
        self._pointer_dialogs.clear()
        self._pointer_program = None

    def _switch_pointer_dialogs(self, pid, process_id):
        """Keep pointer scans open for a restarted program, close them otherwise.

        Their candidates belong to an address-list entry of the program, so
        they are filtered in the new process. ``pid`` None means the program
        is not running at the moment.
        """
        if pid is not None and process_id != self._pointer_program:
            self._close_pointer_dialogs()
            return
        for dialog in self._pointer_dialogs.values():
            dialog.set_process(pid)

    def _set_pointer(self, row, chain):
        """Use ``chain`` (or None) to locate an address-list entry."""
        meta = self._address_meta(row)
        self._set_row_pointer(row, chain)
        address = None
        if chain is not None:
            try:
                with ProcessMemory(self._current_pid) as memory:
                    address = resolve_chain(
                        memory, module_bases(read_mappings(self._current_pid)), chain
                    )
            except OSError:
                address = None
            self._set_row_address(row, address)
        self._sync_freeze(row)
        if meta["id"] is not None:
            self._store_address(
                self._address_repository.update_address, meta["id"],
                pointer=chain, address=address,
            )
        if chain is not None:
            self.lbl_status.setText(
                self.translations["address_table.pointer_set"].format(chain=chain.text())
            )

    def _show_result_context_menu(self, position):
        """Show address actions for the scan result under the pointer."""
        self._show_address_context_menu(self.result_table, position, 0)

    def _show_tracked_context_menu(self, position):
        """Show address actions for the tracked address under the pointer."""
        self._show_address_context_menu(self.addr_table, position, 1)

    def _show_address_context_menu(self, table, position, address_column):
        """Select the clicked address row and display its context menu."""
        if table is None:
            return
        row = table.rowAt(position.y())
        if row < 0:
            return

        table.clearSelection()
        table.selectRow(row)
        table.setCurrentCell(row, address_column)
        item = table.item(row, address_column)
        if item is None:
            return

        if table is self.result_table:
            metadata = item.data(Qt.ItemDataRole.UserRole)
            address = metadata["address"] if metadata else item.text()
            type_item = table.item(row, 1)
            value_type = (
                metadata["type"]
                if metadata
                else type_item.text() if type_item is not None else ""
            )
        else:
            address = item.text()
            type_item = table.item(row, 2)
            value_type = type_item.text() if type_item is not None else ""

        pointer = self._address_meta(row)["pointer"] if table is self.addr_table else None
        menu = QMenu(self)
        try:
            address_value = int(address, 0)
        except (TypeError, ValueError):
            address_value = None  # e.g. a pointer that does not resolve now
        menu.addAction(
            self.translations["memview.show"],
            lambda: self._open_memory_viewer(address_value),
        ).setEnabled(address_value is not None and self._current_pid is not None)
        menu.addSeparator()
        if table is self.addr_table:
            menu.addAction(
                self.translations["address_table.edit_description"],
                self._edit_description,
            )
            menu.addAction(
                self.translations["address_table.edit_value"], self._write_value
            ).setEnabled(self._current_pid is not None)
            menu.addSeparator()
        menu.addAction(
            self.translations["address_table.watch"],
            lambda: self._start_write_monitor(address, value_type, pointer),
        )
        menu.addAction(
            self.translations["address_table.watch_access"],
            lambda: self._start_write_monitor(address, value_type, pointer, reads=True),
        )
        if table is self.addr_table:
            menu.addSeparator()
            meta = self._address_meta(row)
            scan = menu.addAction(
                self.translations["address_table.pointer_scan"],
                lambda: self._open_pointer_scan(row),
            )
            scan.setEnabled(self._current_pid is not None and meta["id"] is not None)
            if meta["pointer"] is not None:
                menu.addAction(
                    self.translations["address_table.pointer_remove"],
                    lambda: self._set_pointer(row, None),
                )
        menu.exec(table.viewport().mapToGlobal(position))

    def _watch_hook_target(self, spec, reads):
        """Watch the address a value hook targets (its pointer chain resolved now)."""
        if self._current_pid is None:
            return
        address = spec.address
        if spec.pointer is not None:
            try:
                with ProcessMemory(self._current_pid) as memory:
                    address = resolve_chain(
                        memory, module_bases(read_mappings(self._current_pid)), spec.pointer
                    )
            except OSError:
                address = None
            if address is None:
                QMessageBox.warning(
                    self,
                    self.translations["hooks.title"],
                    self.translations["hooks.pointer_unresolved"].format(
                        chain=spec.pointer.text()),
                )
                return
        self._start_write_monitor(hex(address), spec.value_type, spec.pointer, reads=reads)

    def _start_write_monitor(self, address, value_type, pointer=None, reads=False):
        """Start a live hardware-watchpoint monitor for one address.

        With ``reads`` it reports reads as well as writes.

        Hooks created from the monitor target ``pointer`` (a PointerChain of
        the address-list entry) if there is one.
        """
        if self._current_pid is None:
            QMessageBox.warning(
                self,
                self.translations["warning.no_process_title"],
                self.translations["warning.select_process"],
            )
            return

        try:
            address_value = int(address, 0)
            size = watch_size_for_type(value_type)
            if address_value % size:
                raise ValueError(
                    self.translations["watch.misaligned"].format(size=size)
                )
        except (TypeError, ValueError) as error:
            QMessageBox.warning(
                self,
                self.translations["watch.title"].format(address=address),
                self.translations["watch.failed"].format(message=error),
            )
            return

        worker = self._write_watch_worker
        if worker is not None and worker.isRunning():
            dialog = self._write_watch_dialog
            if dialog is not None:
                if worker.address != address_value:
                    dialog.show_warning(
                        self.translations["watch.already_active"]
                    )
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                return
            # The previous monitor's window is closed but its teardown is
            # still running; give it a moment before starting a new one.
            if not worker.wait(WRITE_WATCH_STOP_TIMEOUT_MS):
                QMessageBox.information(
                    self,
                    self.translations["watch.title"].format(address=address),
                    self.translations["watch.still_stopping"],
                )
                return

        # Hooks in training watch the process too; they rest meanwhile.
        if self.hook_panel is not None:
            self.hook_panel.pause_trainings(True)
        worker = create_watch_worker(
            self._watch_backend,
            self._current_pid,
            address_value,
            size,
            self,
            reads=reads,
        )
        dialog = WriteWatchDialog(
            f"0x{address_value:X}",
            size,
            self.translations,
            self,
            reads=reads,
        )
        dialog.stop_requested.connect(worker.requestInterruption)
        dialog.hook_requested.connect(
            lambda trap_pc, site: self._create_hook(
                address_value, value_type, trap_pc, site, pointer
            )
        )
        dialog.finished.connect(
              lambda: self._on_write_watch_dialog_finished(dialog, worker)
        )
        worker.monitor_ready.connect(dialog.set_monitoring)
        worker.hits_ready.connect(
            lambda hits: self._on_write_watch_hits(dialog, hits)
        )
        worker.monitor_warning.connect(dialog.show_warning)
        worker.monitor_failed.connect(dialog.show_error)
        worker.monitor_stopped.connect(dialog.set_stopped)
        worker.finished.connect(
            lambda: self._on_write_watch_worker_finished(worker)
        )

        self._write_watch_worker = worker
        self._write_watch_dialog = dialog
        self._write_watch_stopping = False
        worker.start()
        dialog.show()

    def _create_hook(self, address_value, value_type, trap_pc, site, pointer=None):
        # pylint: disable=too-many-arguments
        """Ask for a transform and hook the write instruction before trap_pc."""
        if self._current_pid is None or self.hook_panel is None:
            return
        dialog = HookDialog(
            self.translations,
            f"0x{address_value:X}",
            value_type,
            site,
            label=self._tracked_description(address_value),
            templates=self.hook_panel.templates(),
            pointer=pointer,
            parent=self._write_watch_dialog or self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        spec = HookSpec(
            address_value,
            value_type,
            dialog.transform,
            trap_pc=trap_pc,
            caller_depth=dialog.caller_depth,
            pointer=pointer,
            label=dialog.label,
        )
        self.hook_panel.add_hook(spec, site)
        if self.bottom_tabs is not None:
            self.bottom_tabs.setCurrentWidget(self.hook_panel)
        self.lbl_status.setText(
            self.translations["hooks.status_created"].format(
                address=f"0x{address_value:X}",
                transform=dialog.transform.describe(),
            )
        )

    def _tracked_description(self, address_value):
        """Return the description of a tracked address, if it is in the table."""
        if self.addr_table is None:
            return ""
        for row in range(self.addr_table.rowCount()):
            address_item = self.addr_table.item(row, 1)
            description_item = self.addr_table.item(row, 0)
            try:
                matches = (
                    address_item is not None
                    and int(address_item.text(), 0) == address_value
                )
            except ValueError:
                continue
            if matches and description_item is not None:
                return description_item.text()
        return ""

    def _on_write_watch_hits(self, dialog, hits):
        """Ignore hit batches already queued when monitoring was stopped."""
        if dialog is self._write_watch_dialog and not self._write_watch_stopping:
            if self.hook_panel is not None:
                hits = self.hook_panel.translate_watch_hits(hits)
            dialog.add_hits(hits)

    def _on_write_watch_dialog_finished(self, dialog, worker):
        """Stop the monitor when its results window is closed."""
        # A worker that already finished (e.g. stopped via the dialog's stop
        # button) has been released with deleteLater() and must not be touched.
        if self._write_watch_worker is worker:
            self._write_watch_stopping = True
            worker.requestInterruption()
        if self._write_watch_dialog is dialog:
            self._write_watch_dialog = None

    def _on_write_watch_worker_finished(self, worker):
        """Release the completed monitor worker."""
        worker.deleteLater()
        if self._write_watch_worker is worker:
            self._write_watch_worker = None
            self._write_watch_stopping = False
            if self.hook_panel is not None:
                self.hook_panel.pause_trainings(False)

    def _stop_write_monitor(self, timeout_ms=WRITE_WATCH_STOP_TIMEOUT_MS):
        """Stop the active monitor and wait for it to finish.

        A ``timeout_ms`` of ``None`` waits until the worker has finished.
        Otherwise the wait is bounded so an unresponsive target cannot
        freeze the UI; the worker then finishes in the background.
        """
        worker = self._write_watch_worker
        if worker is not None and worker.isRunning():
            self._write_watch_stopping = True
            worker.requestInterruption()
            if timeout_ms is None:
                worker.wait()
            else:
                worker.wait(timeout_ms)
        dialog = self._write_watch_dialog
        if dialog is not None:
            dialog.close()
            self._write_watch_dialog = None

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _build_statusbar(self):
        """Create the status bar and its process-status labels."""
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel(self.translations["status.no_process_attached"])
        sb.addWidget(self.lbl_status)
        self.lbl_backend = QLabel()
        self.lbl_backend.setObjectName("lbl_backend")
        sb.addWidget(self.lbl_backend)
        self._update_backend_label()
        self.btn_log = QPushButton()
        self.btn_log.setFlat(True)
        self.btn_log.clicked.connect(self._open_log)
        sb.addPermanentWidget(self.btn_log)
        self._log_signals = LogSignals(self)
        self._log_signals.error_logged.connect(self._update_log_button)
        self._error_reporter = ErrorReporter(self.translations, self._open_log, self)
        self._update_log_button()
        sb.addPermanentWidget(QLabel(self.translations["app.version_status"]))
        sb.setSizeGripEnabled(False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _open_process_dialog(self):
        """Open process selection and update the attached-process state."""
        dlg = ProcessSelectDialog(self, self.translations)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            changed = dlg.selected_pid != self._current_pid
            process_id = self._known_process_id(dlg) if changed else self._process_id
            if changed:
                self._stop_write_monitor()
                self._stop_freeze_worker()
                self._close_catalog()
                self._engine = None
                self._switch_pointer_dialogs(dlg.selected_pid, process_id)
                if self._memory_viewer is not None:
                    self._memory_viewer.set_process(dlg.selected_pid)
                # Wine/Proton cannot take Frida; native processes prefer it.
                self._auto_select_backend(dlg.selected_pid)
            if self.hook_panel is not None:
                self.hook_panel.set_process(dlg.selected_pid, process_id)
            self._current_pid = dlg.selected_pid
            if changed:
                self._process_start_time = process_start_time(dlg.selected_pid)
                self._process_timer.start()
                self._process_id = process_id
                self._load_saved_addresses()
            self._current_proc = self.translations["app.process_display"].format(
                name=dlg.selected_name, pid=dlg.selected_pid
            )
            self.lbl_proc_info.setText(f"  ⬢  {self._current_proc}")
            self.lbl_status.setText(
                self.translations["app.process_attached"].format(process=self._current_proc)
            )
            if self.btn_first_scan is not None:
                self.btn_first_scan.setEnabled(True)
            self._update_catalog_button()

    def _check_process_alive(self):
        """Reset the window if the attached process has exited; return whether it runs."""
        if self._current_pid is None:
            return False
        start_time = process_start_time(self._current_pid)
        if start_time is not None and start_time == self._process_start_time:
            if self._engine is None:
                # The runtime may load after attaching early during the game's start.
                self._update_catalog_button()
            return True
        process = self._current_proc
        self._detach_process()
        self.lbl_status.setText(
            self.translations["app.process_exited"].format(process=process)
        )
        return False

    def _detach_process(self):
        """Stop all work on the attached process and reset the window."""
        self._process_timer.stop()
        self._refresh_timer.stop()
        scan_worker = self._scan_worker
        if scan_worker is not None and scan_worker.isRunning():
            # Results of the old process must not show up afterwards.
            scan_worker.results_ready.disconnect()
            scan_worker.scan_failed.disconnect()
            scan_worker.requestInterruption()
            scan_worker.wait()
        refresh_worker = self._refresh_worker
        if refresh_worker is not None and refresh_worker.isRunning():
            refresh_worker.requestInterruption()
            refresh_worker.wait()
        self._stop_write_monitor()
        self._stop_freeze_worker()
        self._switch_pointer_dialogs(None, None)
        if self._memory_viewer is not None:
            self._memory_viewer.set_process(None)
        if self.hook_panel is not None:
            self.hook_panel.set_process(None)
        self._close_catalog()

        self._current_pid = None
        self._engine = None
        self._update_catalog_button()
        self._process_start_time = None
        self._process_id = None
        self._reset_scan()
        if self.addr_table is not None:
            self.addr_table.setRowCount(0)
        self._current_proc = self.translations["app.no_process"]
        self.lbl_proc_info.setText(f"  {self._current_proc}")

    def _ensure_example_templates(self):
        """Create the example hook templates that do not exist yet."""
        try:
            self._hook_repository.ensure_templates(example_templates(self.translations))
        except SQLAlchemyError:
            # The status bar does not exist yet; dialogs report DB errors later.
            pass

    def _open_template_dialog(self):
        """Show the dialog for maintaining hook templates."""
        TemplateDialog(self.translations, self._hook_repository, self).exec()

    def _known_process_id(self, dlg):
        """Return the database id of the selected program, saving it if new."""
        try:
            return self._hook_repository.get_or_create_process(
                dlg.selected_name, dlg.selected_path
            )
        except SQLAlchemyError as error:
            self.lbl_status.setText(
                self.translations["hooks.db_failed"].format(message=error)
            )
            return None

    def _first_scan(self):
        """Validate the selected scan options and start the first scan."""
        if not self._current_pid:
            QMessageBox.warning(
                self,
                self.translations["warning.no_process_title"],
                self.translations["warning.select_process"],
            )
            return

        if (
            self.input_value is None or self.combo_type is None or
            self.combo_scan_type is None
        ):
            return
        if (
            self.input_value2 is None or self.lbl_scan_status is None or
            self.result_table is None
        ):
            return

        value = self.input_value.text().strip()
        vtype = self.combo_type.currentData()
        scan_mode = self.combo_scan_type.currentData()
        value2 = self.input_value2.text().strip()
        if vtype == "auto" and scan_mode not in {
            "exact", "greater_than", "less_than", "between"
        }:
            QMessageBox.warning(
                self,
                self.translations["search.auto_type"],
                self.translations["search.auto_type_requires_value"],
            )
            return
        if scan_mode in PREVIOUS_VALUE_MODES:
            QMessageBox.warning(
                self,
                self.translations["search.first_scan"],
                self.translations["search.requires_previous_scan"],
            )
            return
        self.lbl_scan_status.setText(
            self.translations["search.scanning"].format(
                type=self.combo_type.currentText(),
                mode=self.combo_scan_type.currentText(),
                value=value,
            )
        )
        self.result_table.setRowCount(0)
        self._scan_results = []
        self._start_scan_worker(
            ScanRequest(
                pid=self._current_pid,
                value_type=vtype,
                scan_mode=scan_mode,
                value=value,
                value2=value2,
                aligned=self.chk_aligned is None or self.chk_aligned.isChecked(),
                **self._fuzzy_options(),
            )
        )

    def _next_scan(self):
        """Start a follow-up scan using the previous results."""
        if (
            self.input_value is None or self.combo_type is None or
            self.combo_scan_type is None
        ):
            return
        if (
            self.input_value2 is None or self.lbl_scan_status is None or
            self.result_table is None
        ):
            return

        value = self.input_value.text().strip()
        vtype = self.combo_type.currentData()
        scan_mode = self.combo_scan_type.currentData()
        value2 = self.input_value2.text().strip()
        self.lbl_scan_status.setText(
            self.translations["search.refining"].format(value=value)
        )
        self._start_scan_worker(
            ScanRequest(
                pid=self._current_pid,
                value_type=vtype,
                scan_mode=scan_mode,
                value=value,
                value2=value2,
                previous_results=self._scan_results,
                **self._fuzzy_options(),
            )
        )

    def _start_scan_worker(self, request: ScanRequest):
        """Configure and start a background scan worker."""
        if (
            self.btn_first_scan is None or self.btn_next_scan is None or
            self.btn_reset is None or self.progress_scan is None
        ):
            return

        self._refresh_timer.stop()
        self._scan_failed = False
        self.btn_first_scan.setEnabled(False)
        self.btn_next_scan.setEnabled(False)
        self.btn_reset.setEnabled(False)
        if self.chk_aligned is not None:
            self.chk_aligned.setEnabled(False)
        self.progress_scan.setValue(0)
        self.progress_scan.setVisible(True)
        self._scan_worker = ScanWorker(
            request.pid,
            request.value_type,
            request.scan_mode,
            request.value,
            request.value2,
            request.previous_results,
            aligned=request.aligned,
            tolerance=request.tolerance,
            float_rounding=request.float_rounding,
            parent=self,
        )
        self._scan_worker.results_ready.connect(self._on_scan_results)
        self._scan_worker.scan_failed.connect(self._on_scan_failed)
        self._scan_worker.scan_cancelled.connect(self._on_scan_cancelled)
        self._scan_worker.progress_changed.connect(self.progress_scan.setValue)
        self._scan_worker.finished.connect(self._on_scan_worker_finished)
        self._scan_worker.start()

    def _on_scan_results(self, results):
        """Display returned scan results and update the result count."""
        if self.result_table is None or self.lbl_result_count is None:
            return
        self._scan_results = results
        shown_results = results[:RESULT_DISPLAY_LIMIT]
        self.result_table.setUpdatesEnabled(False)
        self.result_table.setRowCount(len(shown_results))
        for row, result in enumerate(shown_results):
            address_item = QTableWidgetItem(f"0x{result['address']:X}")
            address_item.setData(Qt.ItemDataRole.UserRole, {
                "address": f"0x{result['address']:X}",
                "address_value": result["address"],
                "type": result["type"],
                "value": str(result["value"]),
            })
            self.result_table.setItem(row, 0, address_item)
            self.result_table.setItem(row, 1, QTableWidgetItem(result["type"]))
            self.result_table.setItem(row, 2, QTableWidgetItem(str(result["value"])))
        self.result_table.setUpdatesEnabled(True)
        self.lbl_result_count.setText(
            self.translations["results.count"].format(
                count=len(results), shown=len(shown_results)
            )
        )

    def _on_scan_failed(self, message):
        """Record a scan failure and display its message."""
        self._scan_failed = True
        if self.lbl_scan_status is not None:
            self.lbl_scan_status.setText(message)

    def _on_scan_cancelled(self):
        """Restore the ready status after a scan is cancelled."""
        if self.lbl_scan_status is not None:
            self.lbl_scan_status.setText(self.translations["search.ready"])

    def _on_scan_worker_finished(self):
        """Restore scan controls and resume value refresh when appropriate."""
        if self._scan_worker is None:
            return

        if self.btn_first_scan is not None:
            self.btn_first_scan.setEnabled(self._current_pid is not None)
        has_results = bool(self._scan_results)
        if self.btn_next_scan is not None:
            self.btn_next_scan.setEnabled(has_results)
        if self.btn_reset is not None:
            self.btn_reset.setEnabled(has_results)
        if self.chk_aligned is not None:
            self.chk_aligned.setEnabled(not has_results)
        if self.progress_scan is not None:
            self.progress_scan.setVisible(False)
        if not self._scan_failed:
            if self.lbl_scan_status is not None:
                self.lbl_scan_status.setText(self.translations["search.ready"])

        self._scan_worker.deleteLater()
        self._scan_worker = None
        if self._current_pid is not None and self._has_refresh_targets():
            self._refresh_timer.start()

        self._set_primary(self.btn_next_scan, has_results)
        self._set_primary(self.btn_first_scan, not has_results)

    def _has_refresh_targets(self) -> bool:
        """Return whether either table contains values to refresh."""
        if self.result_table is None or self.addr_table is None:
            return False
        return self.result_table.rowCount() > 0 or self.addr_table.rowCount() > 0

    def _visible_result_rows(self) -> range:
        """Return the row indexes currently visible in the results table."""
        if self.result_table is None:
            return range(0)
        first_row = self.result_table.rowAt(0)
        if first_row < 0:
            return range(0)
        viewport = self.result_table.viewport()
        last_row = 0
        if viewport is None:
            last_row = 0
        else:
            last_row = self.result_table.rowAt(viewport.height() - 1)
        if last_row < first_row:
            last_row = self.result_table.rowCount() - 1
        return range(first_row, last_row + 1)

    def _refresh_memory_values(self):
        """Start a worker to refresh visible and tracked memory values."""
        if self._current_pid is None or self._refresh_worker is not None \
           or self.result_table is None or self.addr_table is None:
            return

        entries = {}
        for row in self._visible_result_rows():
            item = self.result_table.item(row, 0)
            metadata = item.data(Qt.ItemDataRole.UserRole) if item else None
            if metadata:
                address = metadata["address_value"]
                entries[address] = {"address": address, "type": metadata["type"]}

        for row in range(self.addr_table.rowCount()):
            meta = self._address_meta(row)
            item2 = self.addr_table.item(row, 2)
            if item2 is None:
                continue
            if meta["pointer"] is not None:
                entries[("pointer", meta["key"])] = {
                    "key": meta["key"], "pointer": meta["pointer"], "type": item2.text(),
                }
                continue
            try:
                item1 = self.addr_table.item(row, 1)
                if item1 is None:
                    continue
                address = int(item1.text(), 0)
            except (AttributeError, ValueError):
                continue
            entries[address] = {"address": address, "type": item2.text()}

        if not entries:
            return

        self._refresh_worker = ValueRefreshWorker(
            self._current_pid,
            list(entries.values()),
            self,
        )
        self._refresh_worker.addresses_resolved.connect(self._on_addresses_resolved)
        self._refresh_worker.values_ready.connect(self._on_refreshed_values)
        self._refresh_worker.refresh_failed.connect(self._on_refresh_failed)
        self._refresh_worker.finished.connect(self._on_refresh_worker_finished)
        self._refresh_worker.start()

    def _on_refreshed_values(self, values):
        """Update both tables with values returned by the refresh worker."""
        if self.result_table is None or self.addr_table is None:
            return
        for row in self._visible_result_rows():
            item0 = self.result_table.item(row, 0)
            metadata = item0.data(Qt.ItemDataRole.UserRole) if item0 else None
            if metadata and metadata["address_value"] in values:
                item2 = self.result_table.item(row, 2)
                if item2:
                    item2.setText(
                        str(values[metadata["address_value"]])
                    )

        for row in range(self.addr_table.rowCount()):
            try:
                item1 = self.addr_table.item(row, 1)
                if item1 is None:
                    continue
                address = int(item1.text(), 0)
            except (AttributeError, ValueError):
                continue
            if address in values:
                item3 = self.addr_table.item(row, 3)
                if item3:
                    item3.setText(str(values[address]))

    def _on_refresh_failed(self, message):
        """Stop periodic refresh and display the worker error."""
        if not self._check_process_alive() or self.lbl_scan_status is None:
            return
        self._refresh_timer.stop()
        self.lbl_scan_status.setText(message)

    def _on_refresh_worker_finished(self):
        """Dispose of the finished refresh worker."""
        worker = self.sender()
        if worker is self._refresh_worker and worker is not None:
            worker.deleteLater()
            self._refresh_worker = None

    def closeEvent(self, a0):  # pylint: disable=invalid-name
        """Handle the window close event."""

        if a0 is None:
            return

        self._refresh_timer.stop()
        self._process_timer.stop()
        worker = self._scan_worker
        if worker is not None and worker.isRunning():
            answer = QMessageBox.question(
                self,
                self.translations["search.close_scan_title"],
                self.translations["search.close_scan_message"],
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                if self._has_refresh_targets():
                    self._refresh_timer.start()
                if self._current_pid is not None:
                    self._process_timer.start()
                a0.ignore()
                return

            if self.lbl_scan_status is not None:
                self.lbl_scan_status.setText(
                    self.translations["search.canceling"]
                )
            worker.requestInterruption()

        # A running QThread must not be destroyed. The workers attached to
        # the target remove their hooks first; that waits for the target,
        # so the waits here are bounded and the rest happens in a dialog.
        self._sessions_timer.stop()
        self._close_catalog()
        for module in (trace_dialog, method_monitor):
            module.wait_for_closing_workers(CLOSING_WORKER_TIMEOUT_MS)
        self._stop_write_monitor()
        self._stop_freeze_worker()
        if self.hook_panel is not None:
            self.hook_panel.shutdown()
        if self._sessions_dialog is not None:
            self._sessions_dialog.close()
        if sessions.SESSIONS.sessions():
            # Rejected: workers still run; main() then exits hard.
            ShutdownDialog(self.translations, parent=self).exec()
        refresh_worker = self._refresh_worker
        if refresh_worker is not None and refresh_worker.isRunning():
            refresh_worker.requestInterruption()
            refresh_worker.wait()
        if worker is not None and worker.isRunning():
            worker.wait()

        a0.accept()

    def _reset_scan(self):
        """Clear scan results and restore the initial scan controls."""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return

        if self.result_table is not None:
            self.result_table.setRowCount(0)
        self._scan_results = []
        if self.lbl_result_count is not None:
            self.lbl_result_count.setText(
                self.translations["results.count"].format(count=0, shown=0)
            )
        if self.lbl_scan_status is not None:
            self.lbl_scan_status.setText(self.translations["search.ready"])
        if self.btn_first_scan is not None:
            self.btn_first_scan.setEnabled(True)
        if self.btn_next_scan is not None:
            self.btn_next_scan.setEnabled(False)
        if self.btn_reset is not None:
            self.btn_reset.setEnabled(False)
        if self.chk_aligned is not None:
            self.chk_aligned.setEnabled(True)

        has_results = False
        self._set_primary(self.btn_next_scan, has_results)
        self._set_primary(self.btn_first_scan, not has_results)

    def _add_selected_to_table(self):
        """Add selected scan result entries to the address table."""
        if self.result_table is None:
            return

        selection_model = self.result_table.selectionModel()
        if selection_model is None:
            return

        selected_rows = {
            index.row()
            for index in selection_model.selectedRows()
        }
        for row in selected_rows:
            item = self.result_table.item(row, 0)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if data:
                self._add_tracked_address(data)

    def _add_tracked_address(self, data):
        """Add a scan result to the address list and save it for the program.

        An address inside a module's static data is saved as module + offset,
        so it is found again after a restart without a pointer scan.
        """
        label = self.translations["address_table.new_address"]
        address = data["address_value"]
        pointer = None
        try:
            pointer = chain_for_static_address(read_mappings(self._current_pid), address)
        except (OSError, TypeError):
            pointer = None
        saved_id = None
        if self._process_id is not None:
            saved_id = self._store_address(
                self._address_repository.add_address, self._process_id,
                label=label, value_type=data["type"], address=address, pointer=pointer,
            )
        self._add_address_row(
            label, data["address"], data["type"], data["value"],
            saved_id=saved_id, pointer=pointer,
        )

    def _edit_description(self):
        """Prompt for a new description of the selected tracked address."""
        if self.addr_table is None:
            return
        row = self.addr_table.currentRow()
        if row < 0:
            return
        description_item = self.addr_table.item(row, 0)
        address_item = self.addr_table.item(row, 1)
        if description_item is None or address_item is None:
            return
        dlg = DescriptionInputDialog(
            address_item.text(), description_item.text(), self, self.translations
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_description:
            # Saved by _on_address_item_changed like an inline edit.
            description_item.setText(dlg.new_description)

    def _write_value(self):
        """Prompt for and write a replacement value to process memory."""
        if self.addr_table is None:
            return
        row = self.addr_table.currentRow()
        if row < 0:
            return
        item1 = self.addr_table.item(row, 1)
        item2 = self.addr_table.item(row, 2)
        item3 = self.addr_table.item(row, 3)
        if item1 is None or item2 is None or item3 is None:
            return
        addr = item1.text()
        vtype = item2.text()
        cur = item3.text()
        dlg = ValueInputDialog(addr, cur, vtype, self, self.translations)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_value:
            try:
                written_value = write_process_value(
                    self._current_pid,
                    int(addr, 0),
                    dlg.new_value,
                    vtype,
                )
            except (OSError, ValueError) as error:
                QMessageBox.warning(
                    self,
                    self.translations["warning.write_failed_title"],
                    self.translations["warning.write_failed_message"].format(
                        error=error
                    ),
                )
                return

            item = self.addr_table.item(row, 3)
            if item is not None:
                item.setText(str(written_value))
            if self._row_frozen_value(row) is not None:
                self._freeze_row(row, str(written_value))
            self.lbl_status.setText(
                self.translations["status.written"].format(
                    address=addr, value=written_value
                )
            )

    def _toggle_freeze(self):
        """Freeze the selected address at its current value, or release it."""
        if self.addr_table is None:
            return

        row = self.addr_table.currentRow()
        if row < 0:
            return
        if self._row_frozen_value(row) is not None:
            self._freeze_row(row, None)
            return
        if self._current_pid is None:
            QMessageBox.warning(
                self,
                self.translations["warning.no_process_title"],
                self.translations["warning.select_process"],
            )
            return
        value_item = self.addr_table.item(row, 3)
        type_item = self.addr_table.item(row, 2)
        try:
            value = get_value_type(type_item.text()).parse(value_item.text())
        except (AttributeError, ValueError):
            self.lbl_status.setText(self.translations["address_table.freeze_no_value"])
            return
        self._freeze_row(row, str(value))

    def _freeze_row(self, row, frozen_value):
        """Freeze a row at ``frozen_value`` (text) or release it (None) and save that."""
        self._set_row_frozen(row, frozen_value)
        meta = self._address_meta(row)
        if meta["id"] is not None:
            self._store_address(
                self._address_repository.update_address, meta["id"],
                frozen=frozen_value is not None, frozen_value=frozen_value,
            )

    def _row_frozen_value(self, row):
        """Return the value (text) a row is frozen at, or None."""
        item = self.addr_table.item(row, 4) if self.addr_table is not None else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _set_row_frozen(self, row, frozen_value):
        """Show the frozen state of a row and hand it to the freeze worker."""
        item = self.addr_table.item(row, 4)
        if item is None:
            return
        self._filling_addresses = True
        item.setData(Qt.ItemDataRole.UserRole, frozen_value)
        if frozen_value is None:
            item.setText(self.translations["address_table.no"])
            item.setForeground(QColor("#8b949e"))
        else:
            item.setText(
                self.translations["address_table.frozen_value"].format(value=frozen_value)
            )
            item.setForeground(QColor("#58a6ff"))
        self._filling_addresses = False
        self._sync_freeze(row)

    def _sync_freeze(self, row):
        """Start, update or stop writing back the frozen value of a row."""
        key = self._address_meta(row)["key"]
        entry = self._frozen_entry(row)
        if entry is None:
            if self._freeze_worker is not None:
                self._freeze_worker.remove_entry(key)
            return
        worker = self._ensure_freeze_worker()
        if worker is not None:
            worker.set_entry(key, entry)

    def _frozen_entry(self, row):
        """Return the FrozenValue of a frozen row, or None."""
        frozen_value = self._row_frozen_value(row)
        type_item = self.addr_table.item(row, 2)
        if frozen_value is None or type_item is None:
            return None
        pointer = self._address_meta(row)["pointer"]
        address = None
        try:
            kind = get_value_type(type_item.text())
            data = kind.pack(kind.parse(frozen_value))
            if pointer is None:
                address = int(self.addr_table.item(row, 1).text(), 0)
        except (AttributeError, ValueError):
            return None
        return FrozenValue(data, address, pointer)

    def _ensure_freeze_worker(self):
        """Return the running freeze worker, starting one for the attached process."""
        if self._freeze_worker is None and self._current_pid is not None:
            worker = FreezeWorker(self._current_pid, self)
            worker.write_failed.connect(self._on_freeze_failed)
            worker.finished.connect(lambda: self._on_freeze_worker_finished(worker))
            self._freeze_worker = worker
            worker.start()
        return self._freeze_worker

    def _stop_freeze_worker(self):
        """Stop writing frozen values, e.g. before switching the process."""
        worker = self._freeze_worker
        self._freeze_worker = None
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait()

    def _on_freeze_failed(self, message):
        if not self._check_process_alive():
            return
        self.lbl_status.setText(
            self.translations["address_table.freeze_failed"].format(message=message)
        )

    def _on_freeze_worker_finished(self, worker):
        """Release a finished freeze worker; the next freeze starts a new one."""
        worker.deleteLater()
        if self._freeze_worker is worker:
            self._freeze_worker = None

    def _remove_address(self):
        """Remove the selected addresses from the tracking table."""
        if self.addr_table is None:
            return

        rows = sorted({index.row() for index in self.addr_table.selectedIndexes()},
                      reverse=True)
        if not rows and self.addr_table.currentRow() >= 0:
            rows = [self.addr_table.currentRow()]
        candidates = sum(self._pointer_candidate_count(row) for row in rows)
        question = None
        if candidates:
            question = self.translations["address_table.remove_candidates_confirm"].format(
                count=len(rows), candidates=candidates
            )
        elif len(rows) > 1:
            question = self.translations["address_table.remove_confirm"].format(count=len(rows))
        if question is not None and QMessageBox.question(
            self, self.translations["address_table.title"], question,
        ) != QMessageBox.StandardButton.Yes:
            return
        for row in rows:
            meta = self._address_meta(row)
            if self._freeze_worker is not None:
                self._freeze_worker.remove_entry(meta["key"])
            if meta["id"] is not None:
                # Its pointer scan is deleted with it; an open dialog must not
                # save the candidates again.
                dialog = self._pointer_dialogs.pop(meta["id"], None)
                if dialog is not None:
                    dialog.reject()
                self._store_address(self._address_repository.delete_address, meta["id"])
            self.addr_table.removeRow(row)

    def _pointer_candidate_count(self, row):
        """Return how many pointer candidates are saved for an address-list row."""
        entry_id = self._address_meta(row)["id"]
        if entry_id is None:
            return 0
        dialog = self._pointer_dialogs.get(entry_id)
        if dialog is not None:
            return len(dialog.candidates())
        try:
            scan = self._address_repository.get_pointer_scan(entry_id)
        except SQLAlchemyError:
            return 0
        return 0 if scan is None else len(scan.chains)

    def _style_table(self, table):
        """Apply the shared look and feel to a table widget."""
        table.setShowGrid(False)
        set_row_height(table)
        header = table.horizontalHeader()
        # Prevent bold header text for columns containing selected cells
        header.setHighlightSections(False)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

    def _set_primary(self, button, primary):
        """Mark a button as the primary action and re-apply its style."""
        button.setProperty("primary", primary)
        button.style().unpolish(button)
        button.style().polish(button)
