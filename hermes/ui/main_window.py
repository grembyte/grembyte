"""hermes-hook – Main Window: application main UI (PyQt6)."""

import itertools
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTableWidget, QTableWidgetItem, QLabel,
    QPushButton, QLineEdit, QComboBox, QGroupBox, QToolButton,
    QHeaderView, QStatusBar, QToolBar, QDialog,
    QAbstractItemView, QFormLayout,
    QMessageBox, QFrame, QProgressBar, QMenu, QCheckBox, QTabWidget, QSizePolicy,
    QInputDialog, QApplication, QProgressDialog,
)
from PyQt6.QtCore import (  # pylint: disable=import-error,no-name-in-module
    Qt, QSize, QTimer, QSettings, pyqtSlot,
)
from PyQt6.QtGui import QColor, QActionGroup, QIcon  # pylint: disable=import-error,no-name-in-module

from hermes import __version__
from hermes.ui.localization import language_code, load_translations
from hermes.ui.styles.themes import ThemeManager
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.code_editor import apply_code_theme
from hermes.ui.widgets.about_dialog import AboutDialog
from hermes.ui.widgets.icons import apply_icon_theme, material_icon, set_button_icon
from hermes.ui.widgets.process_list import ProcessSelectDialog
from hermes.ui.widgets.table import (
    add_copy_actions, add_delete_action, resizable_columns, set_row_height)
from hermes.core.pointer import chain_for_static_address, module_bases, resolve_chain
from hermes.core.freeze import FreezeWorker, FrozenValue
from hermes.core.scanner import (
    FLOAT_ROUNDING_MODES,
    FUZZY_MODES,
    PREVIOUS_VALUE_MODES,
    MemorySnapshot,
    ScanResults,
    ScanWorker,
    ValueRefreshWorker,
)
from hermes.core.memory import UnknownModuleError, parse_address, write_process_value
from hermes.engines import base as engine_base
from hermes.engines.registry import ENGINES, detect_engine, engine_by_id
from hermes.features.catalog import (
    catalog_method_from_identity,
    generic_parameters,
    split_type_arguments,
    with_type_arguments,
)
from hermes.features.method_call import call_capability
from hermes.core.wine_gadget import gadget_loader, gadget_path, gadget_port, wine_bits
from hermes.platform import (
    ProcessMemory, is_wine_process, process_start_time, program_bits, read_mappings,
)
from hermes.core.value_types import DEFAULT_VALUE_TYPE, VALUE_TYPES, value_type as get_value_type
from hermes.core.write_watch import (
    WATCH_BACKENDS,
    BACKEND_FRIDA,
    BACKEND_PTRACE,
    create_watch_worker,
    watch_size_for_type,
)
from hermes.db.engine import DB_PATH, database_size, vacuum_db
from hermes.db.repository.addresses import AddressRepository, address_pointer
from hermes.db.repository.assembly_selections import AssemblySelectionRepository
from hermes.db.repository.favorites import FavoriteRepository
from hermes.db.repository.game_export import GameExportRepository
from hermes.db.repository.hooks import HookRepository
from hermes.db.repository.scripts import ScriptRepository
from hermes.db.repository.settings import SettingsRepository
from hermes.db.repository.trace_filters import TraceFilterRepository
from hermes.hooks.engine import HookSpec
from hermes.hooks.transforms import example_templates
from hermes.ui.widgets.assemblies import AssemblyDialog, AssemblySelectionStore
from hermes.ui.widgets.catalog import CatalogDialog
from hermes.ui.widgets.call_graph import CallGraphDialog
from hermes.ui.widgets.known_games import KnownGamesDialog
from hermes.ui.widgets.database_entries import DeleteEntriesDialog
from hermes.ui.widgets.favorites import FavoritesDialog, FavoriteStore
from hermes.ui.widgets.gas_attributes import GasAttributesDialog
from hermes.ui.widgets.game_export import GameExportDialog, GameImportDialog
from hermes.ui.widgets.hooks import HookDialog, HookPanel
from hermes.ui.widgets.scripts import ScriptPanel
from hermes.ui.widgets.memory_view import MemoryViewerDialog
from hermes.ui.widgets.settings_dialog import SettingsDialog
from hermes.ui.widgets.theme_dialog import ThemeDialog
from hermes.core import activity, logs, messages, sessions
from hermes.core import settings as app_settings
from hermes.ui import workers
from hermes.ui.widgets.log_view import ErrorReporter, LogDialog, LogSignals
from hermes.ui.widgets.sessions import SessionsDialog, ShutdownDialog
from hermes.ui.widgets.method_call import MethodCallDialog
from hermes.ui.widgets.method_monitor import MethodMonitorDialog
from hermes.ui.widgets.native_monitor import NativeMonitorDialog
from hermes.ui.widgets.native_trace import NativeTraceDialog
from hermes.ui.widgets.trace import TraceDialog
from hermes.ui.widgets.pointer_scan import PointerScanDialog
from hermes.ui.widgets.templates import TemplateDialog
from hermes.ui.widgets.write_watch import WriteWatchDialog


# How often to check whether the attached process is still running.
PROCESS_CHECK_INTERVAL_MS = 1000
# A game that ends this long after Hermes last used debug registers in it may
# have detected them (anti-debug code, e.g. Arxan in Dark Souls III, checks
# them every few seconds).
DEBUG_REGISTER_CRASH_WINDOW_S = 30
# How long closing the window waits for the Frida workers of the C# windows
# (trace, method monitor) to remove their interceptors before it goes on;
# the shutdown dialog waits for what still runs then.
CLOSING_WORKER_TIMEOUT_MS = 3_000

# Upper bound for joining a write monitor from the GUI thread.
WRITE_WATCH_STOP_TIMEOUT_MS = 3000
# Scan results beyond this many rows are kept but not shown in the table.
RESULT_DISPLAY_LIMIT = 1000

# The app icon (window and taskbar, see hermes.main; toolbar)
ICON_PATH = Path(__file__).parent / "resources" / "favicon.svg"
# Its size next to the name in the toolbar
BRAND_ICON_SIZE = 22


class GadgetProgressDialog(QProgressDialog):
    """Progress dialog updated by the gadget download worker."""

    @pyqtSlot(int, int)
    def set_download_progress(self, received, total):
        if total <= 0:
            self.setRange(0, 0)
            return
        self.setRange(0, total)
        self.setValue(min(received, total))


def _platform_text():
    """The system Hermes runs on for the status bar, e.g. ``Windows x86-64``."""
    system = "Windows" if sys.platform == "win32" else "Linux"
    machine = platform.machine()
    if machine.lower() in ("x86_64", "amd64"):
        machine = "x86-64"
    return f"{system} {machine}"


def _disconnect(*signals):
    """Disconnect every slot of ``signals``; those without any are left as they are."""
    for signal in signals:
        try:
            signal.disconnect()
        except TypeError:
            pass  # disconnected before


@dataclass(frozen=True)
class ScanRequest:
    """Inputs required to start an initial or follow-up scan."""

    pid: int | None
    value_type: str
    scan_mode: str
    value: str
    value2: str = ""
    previous_results: ScanResults | MemorySnapshot | None = None
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
# Dialog: add an address by hand
# ---------------------------------------------------------------------------
class AddressInputDialog(QDialog):
    # Dialogs expose behavior through Qt signals rather than public methods.
    # pylint: disable=too-few-public-methods
    """Dialog for putting an address on the address list by hand.

    ``parse(text)`` turns the entered address into a number or raises
    ValueError; it also accepts ``module+0x…``.
    """

    def __init__(self, parse, value_type, parent=None, translations=None):
        super().__init__(parent)
        self.translations = translations or load_translations()
        self.setWindowTitle(self.translations["address_dialog.title"])
        self.setMinimumWidth(420)
        self._parse = parse
        # (description, address, value type) once accepted
        self.result_entry = None
        self._build_ui(value_type)

    def _build_ui(self, value_type):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        form = QFormLayout()
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText(
            self.translations["address_dialog.address_placeholder"])
        form.addRow(self.translations["address_table.address"], self.address_input)
        self.type_combo = QComboBox()
        for name in VALUE_TYPES:
            self.type_combo.addItem(name, name)
        self.type_combo.setCurrentIndex(max(self.type_combo.findData(value_type), 0))
        form.addRow(self.translations["address_table.type"], self.type_combo)
        self.description_input = QLineEdit(self.translations["address_table.new_address"])
        form.addRow(self.translations["address_table.description"], self.description_input)
        layout.addLayout(form)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #f85149;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addLayout(dialog_button_row(
            self.translations, "button.add", self._accept, self.reject
        ))

    def _accept(self):
        try:
            address = self._parse(self.address_input.text())
        except UnknownModuleError as error:
            self.error_label.setText(
                self.translations["address_dialog.unknown_module"].format(module=error.module))
            self.error_label.show()
            self.address_input.setFocus()
            return
        except ValueError:
            self.error_label.setText(self.translations["address_dialog.invalid"])
            self.error_label.show()
            self.address_input.setFocus()
            return
        description = (self.description_input.text().strip()
                       or self.translations["address_table.new_address"])
        self.result_entry = (description, address, self.type_combo.currentData())
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
        self.language = language_code(language)
        self.translations = load_translations(self.language)
        messages.use(self.translations)
        self.setWindowTitle(self.translations["app.window_title"])
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)
        self._settings = QSettings("hermes-hook", "hermes-hook")
        self._theme_manager = ThemeManager(settings=self._settings)
        QApplication.instance().setStyleSheet(self._theme_manager.stylesheet())
        apply_icon_theme(self._theme_manager.load_theme()["colors"])
        apply_code_theme(self._theme_manager.load_theme()["colors"])

        self._current_pid = None
        self._process_start_time = None
        # Name and bitness (32/64, None if unknown) of the attached program
        self._current_name = None
        self._process_bits = None
        self._hook_repository = HookRepository()
        self._script_repository = ScriptRepository()
        self._trace_filter_repository = TraceFilterRepository()
        self._address_repository = AddressRepository()
        # Favorites of the program last attached; kept after it exits.
        self._favorite_store = FavoriteStore(FavoriteRepository(), self)
        self._favorite_store.failed.connect(lambda message: self.lbl_status.setText(
            self.translations["hooks.db_failed"].format(message=message)))
        self._favorites_dialog = None
        self._gas_dialog = None
        # Assemblies the catalog and the trace of the program are limited to.
        self._assembly_selection = AssemblySelectionStore(AssemblySelectionRepository(), self)
        self._assembly_selection.failed.connect(lambda message: self.lbl_status.setText(
            self.translations["hooks.db_failed"].format(message=message)))
        self._assemblies_dialog = None
        self._process_id = None
        self._process_name = None
        self._address_keys = itertools.count(1)
        self._filling_addresses = False
        self._pointer_dialogs = {}
        self._memory_viewer = None
        self._catalog = None
        self._method_monitor = None
        self._method_call = None
        self._trace_dialog = None
        self._native_trace_dialog = None
        self._native_monitor = None
        self._sessions_dialog = None
        self._log_dialog = None
        # The engine adapter of the attached game, once its runtime is found.
        self._engine = None
        # The process whose native-tool risk the user accepted (see _confirm_native_tools)
        self._native_risk_accepted = None
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
        # time.monotonic() when debug registers were last seen in use
        self._debug_registers_used = None
        self._watch_backend = self._settings.value("watch_backend", BACKEND_FRIDA)
        if self._watch_backend not in WATCH_BACKENDS:
            self._watch_backend = BACKEND_FRIDA
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(app_settings.get("refresh_interval_ms"))
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
        self.btn_write_value = None
        self.btn_freeze = None
        self.btn_remove_address = None
        self.hook_panel = None
        self.script_panel = None
        self.bottom_tabs = None
        self._address_box = None
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
        file_menu = menu_bar.addMenu(self.translations["menu.file"])
        file_menu.setToolTipsVisible(True)
        self.act_export = file_menu.addAction(
            material_icon("upload"), self.translations["menu.export"])
        self.act_export.setToolTip(self.translations["menu.export_tooltip"])
        self.act_export.triggered.connect(self._open_export)
        self.act_import = file_menu.addAction(
            material_icon("download"), self.translations["menu.import"])
        self.act_import.setToolTip(self.translations["menu.import_tooltip"])
        self.act_import.triggered.connect(self._open_import)
        file_menu.addSeparator()
        self.act_delete_entries = file_menu.addAction(
            material_icon("delete_sweep"), self.translations["menu.delete_entries"])
        self.act_delete_entries.setToolTip(self.translations["menu.delete_entries_tooltip"])
        self.act_delete_entries.triggered.connect(self._open_delete_entries)
        self.act_vacuum = file_menu.addAction(
            material_icon("compress"), self.translations["menu.vacuum"])
        self.act_vacuum.setToolTip(self.translations["menu.vacuum_tooltip"])
        self.act_vacuum.triggered.connect(self._vacuum_database)
        file_menu.addSeparator()
        self.act_check_gadget = file_menu.addAction(
            material_icon("refresh"), self.translations["menu.check_gadget"])
        self.act_check_gadget.setEnabled(sys.platform != "win32")
        self.act_check_gadget.triggered.connect(self._check_wine_gadgets)

        settings_menu = menu_bar.addMenu(self.translations["menu.settings"])
        settings_menu.setToolTipsVisible(True)
        self.act_settings = settings_menu.addAction(
            material_icon("settings"), self.translations["menu.general_settings"])
        self.act_settings.setToolTip(self.translations["menu.general_settings_tooltip"])
        self.act_settings.triggered.connect(self._open_settings)
        self.act_theme = settings_menu.addAction(
            material_icon("palette"), self.translations["menu.theme"])
        self.act_theme.triggered.connect(self._open_theme)
        settings_menu.addSeparator()
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
            if backend not in WATCH_BACKENDS:
                continue
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

        # Shown or hidden by _set_scan_area_visible once the layout exists
        self.act_scan_area = view_menu.addAction(self.translations["menu.scan_area"])
        self.act_scan_area.setCheckable(True)
        self.act_scan_area.setToolTip(self.translations["scan_area.tooltip"])
        self.act_scan_area.setShortcut("Ctrl+Shift+F")

        self.act_templates = view_menu.addAction(
            material_icon("library_books"), self.translations["menu.templates"])
        self.act_templates.setToolTip(self.translations["templates.button_tooltip"])
        self.act_templates.triggered.connect(self._open_template_dialog)

        # What the game's engine offers (see _update_catalog_button)
        view_menu.addSeparator()

        self.act_assemblies = view_menu.addAction(
            material_icon("inventory_2"), self.translations["menu.assemblies"])
        self.act_assemblies.triggered.connect(self._open_assemblies)

        self.act_catalog = view_menu.addAction(
            material_icon("data_object"), self.translations["menu.catalog"])
        self.act_catalog.triggered.connect(self._open_catalog)

        self.act_offline_catalog = view_menu.addAction(
            material_icon("data_object"), self.translations["menu.offline_catalog"])
        self.act_offline_catalog.setToolTip(self.translations["games.menu_tooltip"])
        self.act_offline_catalog.triggered.connect(self._open_offline_catalog)

        self.act_trace = view_menu.addAction(
            material_icon("track_changes"), self.translations["menu.trace"])
        self.act_trace.triggered.connect(self._open_trace)

        self.act_gas = view_menu.addAction(
            material_icon("tune"), self.translations["menu.gas"])
        self.act_gas.triggered.connect(self._open_gas_attributes)

        # Native tools: any engine, they follow the machine code
        view_menu.addSeparator()

        self.act_native_trace = view_menu.addAction(
            material_icon("radar"), self.translations["menu.native_trace"])
        self.act_native_trace.triggered.connect(self._open_native_trace)

        self.act_native_monitor = view_menu.addAction(
            material_icon("monitor_heart"), self.translations["menu.native_monitor"])
        self.act_native_monitor.triggered.connect(lambda: self._monitor_native_function())

        view_menu.addSeparator()

        self.act_favorites = view_menu.addAction(
            material_icon("star_border"), self.translations["menu.favorites"])
        self.act_favorites.setToolTip(self.translations["favorites.button_tooltip"])
        self.act_favorites.triggered.connect(self._open_favorites)
        self._update_catalog_button()

        help_menu = menu_bar.addMenu(self.translations["menu.help"])
        help_menu.setToolTipsVisible(True)
        self.act_about = help_menu.addAction(
            material_icon("article"), self.translations["menu.about"])
        self.act_about.triggered.connect(self._open_about)

    def _open_about(self):
        """Show application and third-party component credits."""
        AboutDialog(self.translations, parent=self).exec()

    def _open_export(self):
        """Export what is saved for a game as JSON; the attached one comes first."""
        dialog = GameExportDialog(self.translations, GameExportRepository(),
                                  current=self._process_name, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.lbl_status.setText(
                self.translations["export.done"].format(path=dialog.exported_path))

    def _open_import(self):
        """Import an export file into a saved process; the attached one comes first."""
        dialog = GameImportDialog(self.translations, GameExportRepository(),
                                  current=self._process_id, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        added = dialog.result.added
        self.lbl_status.setText(self.translations["import.done"].format(
            skipped=sum(dialog.result.skipped.values()), templates=dialog.result.templates,
            **added))
        if dialog.process_id == self._process_id:
            self._reload_saved_entries()

    def _reload_saved_entries(self):
        """Show the saved entries of the attached program anew after the database changed.

        Its hooks are removed and installed again as saved.
        """
        if self._current_pid is None:
            return
        self._close_pointer_dialogs()
        self._load_saved_addresses()
        self._favorite_store.set_process(self._process_id)
        if self.hook_panel is not None:
            self.hook_panel.set_process(None)
            self.hook_panel.set_process(self._current_pid, self._process_id)
        if self.script_panel is not None:
            self.script_panel.set_process(None)
            self.script_panel.set_process(self._current_pid, self._process_id)

    def _open_delete_entries(self):
        """Delete chosen entries of saved processes, or the processes themselves."""
        dialog = DeleteEntriesDialog(self.translations, GameExportRepository(),
                                     current=self._process_id, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        totals = {}
        for counts in dialog.deleted.values():
            for kind, count in counts.items():
                totals[kind] = totals.get(kind, 0) + count
        self.lbl_status.setText(self.translations["delete_entries.done"].format(
            entries=sum(count for kind, count in totals.items() if kind != "process"),
            processes=totals.get("process", 0)))
        if self._process_id in dialog.deleted:
            self._reload_saved_entries()

    def _vacuum_database(self):
        """Shrink the database file by the space of deleted entries, after asking."""
        title = self.translations["vacuum.title"]
        try:
            size, free = database_size()
        except SQLAlchemyError as error:
            QMessageBox.warning(self, title, self.translations["vacuum.failed"].format(error=error))
            return
        if QMessageBox.question(self, title, self.translations["vacuum.confirm"].format(
                path=DB_PATH, size=size / 1e6, free=free / 1e6),
        ) != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            before, after = vacuum_db()
        except SQLAlchemyError as error:
            QMessageBox.warning(self, title, self.translations["vacuum.failed"].format(error=error))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.lbl_status.setText(self.translations["vacuum.done"].format(
            before=before / 1e6, after=after / 1e6))

    def _open_settings(self):
        """Show the general settings; what changes takes effect at once (or after a restart)."""
        dialog = SettingsDialog(self.translations, SettingsRepository(), parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings(dialog.changed)

    def _open_theme(self):
        """Show the theme editor and preview changes until it is closed."""
        ThemeDialog(self.translations, self._theme_manager, parent=self).exec()

    def _apply_settings(self, changed):
        if "refresh_interval_ms" in changed:
            self._refresh_timer.setInterval(changed["refresh_interval_ms"])
        if changed.get("wine_gadget_download") and self._current_pid is not None:
            ready, downloaded = self._prepare_wine_gadget(self._current_pid)
            if ready and downloaded:
                self._restart_current_frida_workers()

    def _set_watch_backend(self, backend):
        """Apply a backend the user picked by hand and remember it."""
        self._apply_backend(backend, persist=True)

    def _apply_backend(self, backend, *, persist):
        """Make ``backend`` the active watch/hook backend and reflect it in the UI.

        ``persist`` writes the choice to settings (a manual pick); automatic
        selection on attach leaves the saved default untouched.
        """
        self._watch_backend = backend
        activity.record("watch_backend", backend=backend, chosen="by hand" if persist else "auto",
                        pid=self._current_pid)
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

        Frida reaches Wine games through its Windows gadget (wine_gadget.py),
        32-bit ones too, but its hook engine only handles x64 code. The Mono
        features use the gadget whichever backend watch and hooks use.
        """
        wine_32bit = is_wine_process(pid) and wine_bits(pid) == 32
        backend = BACKEND_PTRACE if wine_32bit else BACKEND_FRIDA
        self._apply_backend(backend, persist=False)

    def _update_backend_label(self):
        """Show the active backend in the status bar, if it exists yet."""
        if getattr(self, "lbl_backend", None) is None:
            return
        name = self.translations[f"status.backend_{self._watch_backend}"]
        text = self.translations["status.backend"].format(backend=name)
        self.lbl_backend.setText(text + self._gadget_note())

    def _gadget_note(self):
        """Say whether Frida reaches the attached Wine game through its gadget.

        Checked on every process check, because the gadget is only loaded
        once a Frida feature first needs it.
        """
        pid = self._current_pid
        if pid is None or not is_wine_process(pid) or gadget_port(pid) is None:
            return ""
        loader = gadget_loader(pid)
        note = self.translations[
            f"status.gadget_{loader}" if loader else "status.gadget"]
        bits = wine_bits(pid)
        if bits in (32, 64) and not gadget_path(bits).is_file():
            note += self.translations["status.gadget_update"]
        return note

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------
    def _build_toolbar(self):
        """Create the application toolbar and process controls."""
        tb = QToolBar(self.translations["app.brand"])
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        logo = QLabel()
        logo.setPixmap(QIcon(str(ICON_PATH)).pixmap(
            QSize(BRAND_ICON_SIZE, BRAND_ICON_SIZE), self.devicePixelRatioF()))
        logo.setContentsMargins(8, 0, 0, 0)
        tb.addWidget(logo)
        title = QLabel(self.translations["app.brand"])
        title.setObjectName("lbl_title")
        title.setContentsMargins(6, 0, 16, 0)
        tb.addWidget(title)

        tb.addSeparator()

        self.btn_process = QPushButton(self.translations["process.select"])
        set_button_icon(self.btn_process, "memory")
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
        set_button_icon(self.btn_memory, "grid_on")
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
        self._scan_area = top_splitter

        # The scan area can be folded away; the last choice is kept.
        self.btn_scan_area = QToolButton()
        self.btn_scan_area.setAutoRaise(True)
        self.btn_scan_area.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_scan_area.clicked.connect(
            lambda: self._set_scan_area_visible(self._scan_area.isHidden()))
        self.bottom_tabs.setCornerWidget(self.btn_scan_area, Qt.Corner.TopRightCorner)
        self.act_scan_area.toggled.connect(self._set_scan_area_visible)
        self._set_scan_area_visible(
            self._settings.value("scan_area_visible", True, type=bool))

    def _build_bottom_tabs(self):
        """Put tracked addresses and hooks into tabs below the scan area."""
        self.bottom_tabs = QTabWidget()
        address_box = self._build_address_table()
        address_box.setTitle("")
        address_box.setObjectName("tab_page")
        self.bottom_tabs.addTab(address_box, self.translations["address_table.title"])
        self._address_box = address_box
        # Whoever adds or removes rows (scan, inspector, saved addresses), the
        # tab shows how many there are, like those of hooks and scripts.
        model = self.addr_table.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            signal.connect(self._on_address_count_changed)

        self.hook_panel = HookPanel(
            self.translations, self, repository=self._hook_repository
        )
        self.hook_panel.set_backend(self._watch_backend)
        self.hook_panel.setTitle("")
        self.hook_panel.setObjectName("tab_page")
        self.hook_panel.count_changed.connect(self._on_hook_count_changed)
        self.hook_panel.watch_requested.connect(self._watch_hook_target)
        self.hook_panel.address_requested.connect(
            lambda address, value_type, label: self._add_tracked_address(
                {"address": f"0x{address:X}", "address_value": address,
                 "type": value_type, "value": ""},
                label,
            ))
        self.hook_panel.monitor_requested.connect(
            lambda identity: self._monitor_method(catalog_method_from_identity(identity)))
        self.bottom_tabs.addTab(self.hook_panel, self.translations["hooks.title"])

        self.script_panel = ScriptPanel(
            self.translations, self, repository=self._script_repository, language=self.language
        )
        self.script_panel.setTitle("")
        self.script_panel.setObjectName("tab_page")
        self.script_panel.count_changed.connect(self._on_script_count_changed)
        self.script_panel.address_requested.connect(self._add_inspected_address)
        self.bottom_tabs.addTab(self.script_panel, self.translations["scripts.title"])
        return self.bottom_tabs

    def _on_script_count_changed(self, count):
        """Show the number of scripts in the tab title."""
        if self.bottom_tabs is None or self.script_panel is None:
            return
        title = self.translations["scripts.title"]
        self.bottom_tabs.setTabText(
            self.bottom_tabs.indexOf(self.script_panel),
            f"{title} ({count})" if count else title,
        )

    def _set_scan_area_visible(self, visible):
        """Show or fold away the scan results and value search; remembered."""
        self._scan_area.setVisible(visible)
        self.act_scan_area.blockSignals(True)
        self.act_scan_area.setChecked(visible)
        self.act_scan_area.blockSignals(False)
        self.btn_scan_area.setText(
            self.translations["scan_area.hide" if visible else "scan_area.show"])
        set_button_icon(self.btn_scan_area, "expand_less" if visible else "expand_more")
        self.btn_scan_area.setToolTip(self.translations["scan_area.tooltip"])
        self._settings.setValue("scan_area_visible", visible)

    def _on_address_count_changed(self, *_args):
        """Show the number of tracked addresses in the tab title."""
        if self.bottom_tabs is None or self.addr_table is None:
            return
        count = self.addr_table.rowCount()
        title = self.translations["address_table.title"]
        self.bottom_tabs.setTabText(
            self.bottom_tabs.indexOf(self._address_box),
            f"{title} ({count})" if count else title,
        )

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
        set_button_icon(btn_add, "bookmark_add")
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
            resizable_columns(self.result_table)

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
        set_button_icon(self.btn_first_scan, "search")
        self.btn_first_scan.clicked.connect(self._first_scan)
        row.addWidget(self.btn_first_scan)

        self.btn_next_scan = QPushButton(self.translations["search.next_scan"])
        set_button_icon(self.btn_next_scan, "filter_alt")
        self.btn_next_scan.setEnabled(False)
        self.btn_next_scan.clicked.connect(self._next_scan)
        row.addWidget(self.btn_next_scan)

        self.btn_reset = QPushButton(self.translations["search.reset"])
        self.btn_reset.setObjectName("btn_danger")
        set_button_icon(self.btn_reset, "restart_alt")
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
        btn_add = QPushButton(self.translations["address_table.add"])
        set_button_icon(btn_add, "add")
        btn_add.setToolTip(self.translations["address_table.add_tooltip"])
        btn_add.clicked.connect(self._add_address_by_hand)
        ctrl.addWidget(btn_add)
        self.btn_write_value = QPushButton(self.translations["address_table.write"])
        set_button_icon(self.btn_write_value, "edit")
        self.btn_write_value.clicked.connect(self._write_value)
        self.btn_freeze = QPushButton(self.translations["address_table.freeze"])
        set_button_icon(self.btn_freeze, "ac_unit")
        self.btn_freeze.setToolTip(self.translations["address_table.freeze_tooltip"])
        self.btn_freeze.clicked.connect(self._toggle_freeze)
        self.btn_remove_address = QPushButton(self.translations["address_table.remove"])
        self.btn_remove_address.setObjectName("btn_danger")
        set_button_icon(self.btn_remove_address, "delete")
        self.btn_remove_address.clicked.connect(self._remove_address)
        ctrl.addWidget(self.btn_write_value)
        ctrl.addWidget(self.btn_freeze)
        ctrl.addStretch()
        ctrl.addWidget(self.btn_remove_address)
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
            resizable_columns(self.addr_table)

        header = self.addr_table.verticalHeader()
        if header is not None:
            header.setVisible(False)

        self.addr_table.doubleClicked.connect(self._write_value)
        self.addr_table.itemChanged.connect(self._on_address_item_changed)
        # Removing rows changes the selection, too
        self.addr_table.itemSelectionChanged.connect(self._update_address_buttons)
        layout.addWidget(self.addr_table)
        self._update_address_buttons()

        return box

    def _update_address_buttons(self):
        """Enable the address actions only while an address is selected."""
        selected = self.addr_table is not None and self.addr_table.selectionModel().hasSelection()
        for button in (self.btn_write_value, self.btn_freeze, self.btn_remove_address):
            if button is not None:
                button.setEnabled(selected)

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
            if self._engine is not None:
                activity.record("engine_detected", pid=self._current_pid, engine=self._engine.id,
                                name=self._engine.display_name,
                                native_tools=self._engine.native_tools)
                self._show_attached()
        unsupported = self.translations["catalog.button_no_engine"].format(
            engines=", ".join(engine.display_name for engine in ENGINES))
        for action, tooltip, capability in (
                (self.act_assemblies, "assemblies.button_tooltip", engine_base.CATALOG),
                (self.act_catalog, "catalog.button_tooltip", engine_base.CATALOG),
                (self.act_trace, "trace.button_tooltip", engine_base.TRACE)):
            enabled = (self._current_pid is not None and self._engine is not None
                       and self._engine.supports(capability))
            action.setEnabled(enabled)
            action.setToolTip(self.translations[tooltip] if enabled else unsupported)
        # Needs no engine: it follows the machine code. Some engines may not bear
        # it (native_tools): there it asks first (see _confirm_native_tools).
        for action, tooltip in ((self.act_native_trace, "native_trace.button_tooltip"),
                                (self.act_native_monitor, "native_monitor.button_tooltip")):
            action.setEnabled(self._current_pid is not None)
            text = self.translations[tooltip]
            if self._native_tools_risky():
                text += "\n\n" + self._native_risk_text()
            action.setToolTip(text)
        gas = (self._current_pid is not None and self._engine is not None
               and self._engine.supports(engine_base.GAS))
        self.act_gas.setEnabled(gas)
        self.act_gas.setToolTip(self.translations["gas.button_tooltip"] if gas else unsupported)
        self.act_favorites.setEnabled(self._favorite_store.process_id is not None)
        if self._favorites_dialog is not None:
            self._favorites_dialog.set_attached(self._current_pid is not None)

    def _native_tools_risky(self):
        """Whether the native trace and monitor may crash the attached game's engine."""
        return self._engine is not None and not self._engine.native_tools

    def _native_risk_text(self):
        return self.translations["native.risky_engine"].format(
            engine=self._engine.display_name)

    def _confirm_native_tools(self, title):
        """Whether the native trace or monitor may start: in a game whose engine
        may not bear them, once the user agreed (asked once per process)."""
        if not self._native_tools_risky() or self._native_risk_accepted == self._current_pid:
            return True
        answer = QMessageBox.warning(
            self, title,
            self._native_risk_text() + "\n\n" + self.translations["native.risky_question"],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._native_risk_accepted = self._current_pid
        activity.record("native_tools_risk_accepted", pid=self._current_pid,
                        engine=self._engine.id)
        return True

    def _show_attached(self):
        """Show the attached program with its bitness and engine in the status bar."""
        pid = self._current_pid
        if pid is None:
            return
        if self._process_bits is None:
            self._process_bits = wine_bits(pid) if is_wine_process(pid) else program_bits(pid)
        details = []
        if self._process_bits is not None:
            details.append(self.translations["status.bits"].format(bits=self._process_bits))
        if self._engine is not None:
            details.append(self._engine.display_name)
        process = (self.translations["app.process_display_details"].format(
            name=self._current_name, pid=pid, details=", ".join(details))
            if details else self._current_proc)
        self.lbl_status.setText(self.translations["app.process_attached"].format(process=process))

    def _open_catalog(self):
        """Show the catalog of the C# methods of the attached game (see hermes/engines)."""
        if self._current_pid is None or self._engine is None:
            return
        catalog = self._catalog
        if catalog is None:
            catalog = CatalogDialog(self.translations, self._current_pid, parent=self,
                                    engine=self._engine, favorites=self._favorite_store,
                                    assembly_selection=self._assembly_selection)
            catalog.finished.connect(lambda _result: setattr(self, "_catalog", None))
            catalog.monitor_requested.connect(self._monitor_method)
            catalog.hook_requested.connect(self._hook_method)
            catalog.call_requested.connect(self._call_method)
            catalog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._catalog = catalog
        catalog.show()
        catalog.raise_()
        catalog.activateWindow()

    def _open_offline_catalog(self):
        """Show the catalog of a game read before, from the cache (no process needed)."""
        dialog = KnownGamesDialog(self.translations, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.game is None:
            return
        game = dialog.game
        catalog = CatalogDialog(self.translations, None, parent=self,
                                engine=engine_by_id(game.engine), favorites=self._favorite_store,
                                game=game)
        catalog.monitor_requested.connect(self._monitor_method)
        catalog.hook_requested.connect(self._hook_method)
        catalog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        catalog.show()

    def _open_assemblies(self):
        """Show the assemblies of the attached game, to limit the catalog and the trace."""
        if self._current_pid is None or self._engine is None:
            return
        dialog = self._assemblies_dialog
        if dialog is None:
            dialog = AssemblyDialog(self.translations, self._current_pid,
                                    self._assembly_selection, parent=self, engine=self._engine)
            dialog.finished.connect(lambda _result: setattr(self, "_assemblies_dialog", None))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._assemblies_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

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
        set_button_icon(self.btn_sessions, "sync_alt")

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
        set_button_icon(self.btn_log, "warning_amber" if count else "article")

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
                                filter_repository=self._trace_filter_repository,
                                favorites=self._favorite_store,
                                assembly_selection=self._assembly_selection)
            trace.finished.connect(lambda _result: setattr(self, "_trace_dialog", None))
            trace.monitor_requested.connect(self._monitor_method)
            if self.hook_panel is not None:
                trace.hook_requested.connect(self.hook_panel.create_method_hook)
            trace.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._trace_dialog = trace
        trace.show()
        trace.raise_()
        trace.activateWindow()

    def _open_native_trace(self):
        """Show the trace of the native functions a scenario in the game runs."""
        if self._current_pid is None:
            return
        if not self._confirm_native_tools(self.translations["menu.native_trace"]):
            return
        trace = self._native_trace_dialog
        if trace is None:
            trace = NativeTraceDialog(self.translations, self._current_pid, parent=self,
                                      favorites=self._favorite_store)
            trace.finished.connect(lambda _result: setattr(self, "_native_trace_dialog", None))
            trace.memory_requested.connect(self._open_memory_viewer)
            trace.monitor_requested.connect(self._monitor_native_function)
            trace.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._native_trace_dialog = trace
        trace.show()
        trace.raise_()
        trace.activateWindow()

    def _open_gas_attributes(self):
        """Show the player's GAS attributes (Unreal games); one window at a time."""
        dialog = self._gas_dialog
        if dialog is None:
            dialog = GasAttributesDialog(self.translations, self.script_panel, parent=self)
            dialog.finished.connect(lambda _result: setattr(self, "_gas_dialog", None))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._gas_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_favorites(self):
        """Show the favorite methods and functions of the program."""
        dialog = self._favorites_dialog
        if dialog is None:
            dialog = FavoritesDialog(self.translations, self._favorite_store, parent=self)
            dialog.method_monitor_requested.connect(self._monitor_methods)
            dialog.native_monitor_requested.connect(self._monitor_native_functions)
            dialog.method_hook_requested.connect(self._hook_method)
            dialog.calls_requested.connect(self._show_method_calls)
            dialog.call_requested.connect(self._call_method)
            dialog.finished.connect(lambda _result: setattr(self, "_favorites_dialog", None))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._favorites_dialog = dialog
        dialog.set_attached(self._current_pid is not None)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _monitor_native_address(self, address):
        """Monitor the instruction at ``address`` (e.g. a write watch's trap PC)."""
        monitor = self._monitor_native_function()
        if monitor is not None:
            monitor.add_address(address)

    def _monitor_native_functions(self, functions):
        """Log the calls of several native functions in one monitor."""
        for function in functions:
            if self._monitor_native_function(function) is None:
                break  # no process, or not allowed in this game: said once

    def _monitor_methods(self, methods):
        """Log the calls of several C# methods in one monitor."""
        for method in methods:
            self._monitor_method(method)

    def _monitor_native_function(self, function=None):
        """Log the calls of a native function (a NativeFunction of the native trace).

        Returns the monitor window, or None without a process.
        """
        if self._current_pid is None:
            return None
        if not self._confirm_native_tools(self.translations["menu.native_monitor"]):
            return None
        monitor = self._native_monitor
        if monitor is None:
            monitor = NativeMonitorDialog(self.translations, self._current_pid, parent=self)
            monitor.memory_requested.connect(self._open_memory_viewer)
            monitor.finished.connect(lambda _result: setattr(self, "_native_monitor", None))
            monitor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._native_monitor = monitor
        monitor.show()
        if function is not None:
            monitor.add_function(function)
        monitor.raise_()
        monitor.activateWindow()
        return monitor

    def _engine_can(self, method, capability, title):
        """Whether the engine of ``method`` offers ``capability``; says so if not."""
        engine = engine_by_id(method.engine)
        if engine is not None and engine.supports(capability):
            return True
        QMessageBox.information(self, title, self.translations["engine.unsupported"].format(
            engine=engine.display_name if engine is not None else method.engine))
        return False

    def _hook_method(self, method):
        """Create a method hook for a method chosen in the catalog or favorites."""
        if self._current_pid is None or self.hook_panel is None:
            return
        if not self._engine_can(method, engine_base.METHOD_HOOKS,
                               self.translations["monitor.create_hook"]):
            return
        parameters = generic_parameters(method)
        if parameters:
            method = self._ask_type_arguments(method, parameters)
            if method is None:
                return
        self.hook_panel.create_method_hook(method)

    def _show_method_calls(self, method):
        """Show a method's call graph from the favorites dialog."""
        if self._current_pid is None:
            return
        if not self._engine_can(method, engine_base.CALL_GRAPH,
                               self.translations["catalog.calls"]):
            return
        selection = (self._assembly_selection.selection
                     if self._assembly_selection is not None else ())
        graph = CallGraphDialog(self.translations, self._current_pid, method, parent=self,
                                engine=self._engine, selection=selection)
        graph.monitor_requested.connect(self._monitor_method)
        graph.show()

    def _monitor_method(self, method):
        """Log the calls of a C# method chosen in the catalog."""
        if self._current_pid is None:
            return
        if not self._engine_can(method, engine_base.MONITOR, self.translations["catalog.monitor"]):
            return
        parameters = generic_parameters(method)
        if parameters:
            method = self._ask_type_arguments(method, parameters)
            if method is None:
                return
        monitor = self._method_monitor
        if monitor is None:
            monitor = MethodMonitorDialog(self.translations, self._current_pid, parent=self)
            monitor.hook_requested.connect(self._hook_method)
            monitor.address_requested.connect(self._add_inspected_address)
            monitor.finished.connect(lambda _result: setattr(self, "_method_monitor", None))
            monitor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._method_monitor = monitor
        monitor.show()
        monitor.add_method(method)
        monitor.activateWindow()

    def _call_method(self, method):
        """Call a method chosen in the catalog or the favorites: a static one, or
        (Unreal) an instance method on a live object of its class."""
        if self._current_pid is None:
            return
        if not self._engine_can(method, call_capability(method), self.translations["catalog.call"]):
            return
        parameters = generic_parameters(method)
        if parameters:
            method = self._ask_type_arguments(method, parameters)
            if method is None:
                return
        dialog = self._method_call
        if dialog is None:
            dialog = MethodCallDialog(self.translations, self._current_pid, parent=self)
            dialog.address_requested.connect(self._add_inspected_address)
            dialog.finished.connect(lambda _result: setattr(self, "_method_call", None))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._method_call = dialog
        dialog.set_method(method)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _ask_type_arguments(self, method, parameters):
        """The instance of a generic class's method the user names, or None."""
        text = ""
        while True:
            text, accepted = QInputDialog.getText(
                self, self.translations["monitor.type_arguments_title"],
                self.translations["monitor.type_arguments_label"].format(
                    method=f"{method.class_name}:{method.name}",
                    parameters=", ".join(parameters)),
                text=text)
            if not accepted:
                return None
            arguments = split_type_arguments(text)
            if len(arguments) == len(parameters):
                return with_type_arguments(method, arguments)
            QMessageBox.warning(
                self, self.translations["monitor.type_arguments_title"],
                self.translations["monitor.type_arguments_count"].format(
                    count=len(parameters), parameters=", ".join(parameters)))

    def _close_catalog(self):
        """Close the catalog, the traces and the method monitor (process gone or switched)."""
        for name in ("_assemblies_dialog", "_catalog", "_trace_dialog", "_native_trace_dialog",
                     "_method_monitor", "_native_monitor", "_method_call"):
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
            add_delete_action(menu, table, self.translations, self._remove_address)
        add_copy_actions(menu, table, position, self.translations)
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
        self._debug_registers_used = time.monotonic()
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
        dialog.monitor_requested.connect(self._monitor_native_address)
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
            breakpoint=dialog.breakpoint,
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
        sb.addPermanentWidget(QLabel(
            self.translations["app.version_status"].format(
                version=__version__, platform=_platform_text())))
        sb.setSizeGripEnabled(False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _open_process_dialog(self):
        """Open process selection and update the attached-process state."""
        dlg = ProcessSelectDialog(self, self.translations)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.selected_pid is None:
                return
            ready, downloaded = self._prepare_wine_gadget(dlg.selected_pid)
            if not ready:
                return
            changed = dlg.selected_pid != self._current_pid
            activity.record("process_selected", name=dlg.selected_name, changed=changed,
                            **activity.describe_process(dlg.selected_pid))
            process_id = self._known_process_id(dlg) if changed else self._process_id
            if downloaded and not changed:
                self._restart_current_frida_workers()
            if changed:
                self._stop_scan_and_refresh()
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
            if self.script_panel is not None:
                self.script_panel.set_process(dlg.selected_pid, process_id)
            self._current_pid = dlg.selected_pid
            if changed:
                self._process_start_time = process_start_time(dlg.selected_pid)
                self._process_bits = None
                self._process_timer.start()
                self._process_id = process_id
                self._process_name = dlg.selected_name
                self._favorite_store.set_process(process_id)
                self._assembly_selection.set_process(process_id)
                self._load_saved_addresses()
            self._current_proc = self.translations["app.process_display"].format(
                name=dlg.selected_name, pid=dlg.selected_pid
            )
            self._current_name = dlg.selected_name
            self.lbl_proc_info.setText(f"  ⬢  {self._current_proc}")
            if self.btn_first_scan is not None:
                self.btn_first_scan.setEnabled(True)
            self._update_catalog_button()
            self._show_attached()

    def _prepare_wine_gadget(self, pid):
        """Ask permission and fetch a missing Wine gadget before workers attach."""
        if not is_wine_process(pid):
            return True, False
        if pid == self._current_pid and gadget_port(pid) is not None:
            return True, False
        bits = wine_bits(pid)
        if bits not in (32, 64):
            return True, False
        path = gadget_path(bits)
        if path.is_file():
            return True, False
        if not app_settings.get("wine_gadget_download"):
            answer = QMessageBox.question(
                self, self.translations["gadget.download_title"],
                self.translations["gadget.download_prompt"].format(path=path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return True, False
            try:
                SettingsRepository().save({"wine_gadget_download": True})
            except SQLAlchemyError as error:
                QMessageBox.warning(
                    self, self.translations["gadget.download_title"], str(error))
                return False, False
            app_settings.apply({"wine_gadget_download": True})
        downloaded = self._download_wine_gadget(bits)
        return downloaded, downloaded

    def _download_wine_gadget(self, bits):
        """Show a modal progress dialog while the download runs in a worker."""
        dialog = GadgetProgressDialog(
            self.translations["gadget.download_progress"], None, 0, 100, self)
        dialog.setWindowTitle(self.translations["gadget.download_title"])
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setCancelButton(None)
        dialog.setRange(0, 0)
        worker = workers.GadgetDownloadWorker(bits, self)
        worker.progress.connect(dialog.set_download_progress)
        worker.completed.connect(dialog.accept)
        QTimer.singleShot(0, worker.start)
        dialog.exec()
        worker.wait()
        dialog.deleteLater()
        if worker.error:
            QMessageBox.warning(
                self, self.translations["gadget.download_title"],
                worker.error)
            return False
        return worker.path is not None

    def _restart_current_frida_workers(self):
        """Restart workers after a newly downloaded gadget becomes available."""
        for panel in (self.hook_panel, self.script_panel):
            if panel is not None:
                panel.set_process(None)
                panel.set_process(self._current_pid, self._process_id)

    def _check_wine_gadgets(self):
        """Check cached gadgets for this Frida version and offer missing ones."""
        if sys.platform == "win32":
            return
        bits = None
        if self._current_pid is not None and is_wine_process(self._current_pid):
            bits = wine_bits(self._current_pid)
        architectures = (bits,) if bits in (32, 64) else (32, 64)
        missing = []
        for architecture in architectures:
            path = gadget_path(architecture)
            if not path.is_file():
                missing.append((architecture, path))
        if not missing:
            self._update_backend_label()
            QMessageBox.information(
                self, self.translations["gadget.check_title"],
                self.translations["gadget.check_current"],
            )
            return
        paths = "\n".join(str(path) for _bits, path in missing)
        answer = QMessageBox.question(
            self, self.translations["gadget.check_title"],
            self.translations["gadget.check_prompt"].format(paths=paths),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        downloaded = []
        for architecture, _path in missing:
            if not self._download_wine_gadget(architecture):
                return
            downloaded.append(architecture)
        if (self._current_pid is not None
                and is_wine_process(self._current_pid)
                and wine_bits(self._current_pid) in downloaded):
            self._restart_current_frida_workers()
        self._update_backend_label()

    def _check_process_alive(self):
        """Reset the window if the attached process has exited; return whether it runs."""
        if self._current_pid is None:
            return False
        start_time = process_start_time(self._current_pid)
        if start_time is not None and start_time == self._process_start_time:
            if self._debug_registers_in_use():
                self._debug_registers_used = time.monotonic()
            if self._engine is None:
                # The runtime may load after attaching early during the game's start.
                self._update_catalog_button()
            self._update_backend_label()
            return True
        process = self._current_proc
        debug_registers = (
            self._debug_registers_in_use() or (
                self._debug_registers_used is not None
                and time.monotonic() - self._debug_registers_used
                < DEBUG_REGISTER_CRASH_WINDOW_S))
        activity.record("process_exited", pid=self._current_pid, name=self._current_name,
                        debug_registers=debug_registers)
        self._detach_process()
        message = self.translations["app.process_exited"].format(process=process)
        if debug_registers:
            message += " " + self.translations["app.process_exited_debug_registers"]
            self.lbl_status.setToolTip(self.translations["app.debug_registers_hint"])
        self.lbl_status.setText(message)
        return False

    def _debug_registers_in_use(self):
        """Whether a write watch, a hook in training or a breakpoint hook runs."""
        watch = self._write_watch_worker
        return ((watch is not None and watch.isRunning())
                or (self.hook_panel is not None and self.hook_panel.uses_debug_registers()))

    def _detach_process(self):
        """Stop all work on the attached process and reset the window."""
        activity.record("process_detached", pid=self._current_pid, name=self._current_name)
        self._process_timer.stop()
        self._stop_scan_and_refresh()
        self._stop_write_monitor()
        self._stop_freeze_worker()
        self._switch_pointer_dialogs(None, None)
        if self._memory_viewer is not None:
            self._memory_viewer.set_process(None)
        if self.hook_panel is not None:
            self.hook_panel.set_process(None)
        if self.script_panel is not None:
            self.script_panel.set_process(None)
        self._close_catalog()

        self._current_pid = None
        self._debug_registers_used = None
        self._engine = None
        self._current_name = None
        self._process_bits = None
        self._update_catalog_button()
        self._process_start_time = None
        self._process_id = None
        self._process_name = None
        if self.addr_table is not None:
            self.addr_table.setRowCount(0)
        self._current_proc = self.translations["app.no_process"]
        self.lbl_proc_info.setText(f"  {self._current_proc}")
        self._update_backend_label()

    def _stop_scan_and_refresh(self):
        """Stop the scan and the value refresh, and clear the scan results.

        Results of the old process must not show up afterwards, also those a
        worker that has just finished still has queued.
        """
        self._refresh_timer.stop()
        scan_worker = self._scan_worker
        if scan_worker is not None:
            _disconnect(scan_worker.results_ready, scan_worker.scan_failed)
            scan_worker.requestInterruption()
            scan_worker.wait()
        refresh_worker = self._refresh_worker
        if refresh_worker is not None:
            _disconnect(refresh_worker.addresses_resolved, refresh_worker.values_ready,
                        refresh_worker.refresh_failed)
            refresh_worker.requestInterruption()
            refresh_worker.wait()
        self._reset_scan()

    def _ensure_example_templates(self):
        """Create the example hook templates that do not exist yet."""
        # The status bar does not exist yet; the log keeps what went wrong.
        try:
            self._hook_repository.translate_examples()
        except SQLAlchemyError:
            logs.log.exception("Translating the example templates failed")
        try:
            self._hook_repository.ensure_templates(example_templates())
        except SQLAlchemyError:
            logs.log.exception("Creating the example templates failed")

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
                entries[address, metadata["type"]] = {"address": address, "type": metadata["type"]}

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
            entries[address, item2.text()] = {"address": address, "type": item2.text()}

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
            key = None if not metadata else (metadata["address_value"], metadata["type"])
            if key in values:
                item2 = self.result_table.item(row, 2)
                if item2:
                    item2.setText(str(values[key]))

        for row in range(self.addr_table.rowCount()):
            try:
                item1 = self.addr_table.item(row, 1)
                item2 = self.addr_table.item(row, 2)
                if item1 is None or item2 is None:
                    continue
                key = (int(item1.text(), 0), item2.text())
            except (AttributeError, ValueError):
                continue
            if key in values:
                item3 = self.addr_table.item(row, 3)
                if item3:
                    item3.setText(str(values[key]))

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
        workers.stop_workers(CLOSING_WORKER_TIMEOUT_MS)
        self._stop_write_monitor()
        self._stop_freeze_worker()
        if self.hook_panel is not None:
            self.hook_panel.shutdown()
        if self.script_panel is not None:
            self.script_panel.shutdown()
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

    def _add_address_by_hand(self):
        """Ask for an address (hex or module+offset) and put it on the address list."""
        if self._current_pid is None:
            QMessageBox.warning(
                self,
                self.translations["warning.no_process_title"],
                self.translations["warning.select_process"],
            )
            return
        pid = self._current_pid

        def parse(text):
            try:
                mappings = read_mappings(pid)
            except OSError:
                mappings = []
            return parse_address(text, mappings)

        value_type = self.combo_type.currentData() if self.combo_type is not None else None
        dialog = AddressInputDialog(parse, value_type, self, self.translations)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_entry is None:
            return
        label, address, value_type = dialog.result_entry
        self._add_tracked_address(
            {"address": f"0x{address:X}", "address_value": address,
             "type": value_type, "value": ""},
            label,
        )

    def _add_inspected_address(self, entry):
        """Put a field of an object in the object inspector on the address list."""
        if self._current_pid is None:
            return
        address = entry["address"]
        self._add_tracked_address(
            {"address": f"0x{address:X}", "address_value": address,
             "type": entry["value_type"], "value": ""},
            entry["label"],
        )

    def _add_tracked_address(self, data, label=None):
        """Add a scan result to the address list and save it for the program.

        An address inside a module's static data is saved as module + offset,
        so it is found again after a restart without a pointer scan.
        """
        label = label or self.translations["address_table.new_address"]
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
        if self._current_pid is not None and self._scan_worker is None:
            self._refresh_timer.start()

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
            # The list may have been rebuilt meanwhile, e.g. when the game exited.
            if sip.isdeleted(description_item):
                return
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
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )

    def _set_primary(self, button, primary):
        """Mark a button as the primary action and re-apply its style."""
        button.setProperty("primary", primary)
        button.style().unpolish(button)
        button.style().polish(button)
