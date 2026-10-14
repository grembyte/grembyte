"""hermes-hook – Main Window: application main UI (PyQt6)."""

from dataclasses import dataclass

from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTableWidget, QTableWidgetItem, QLabel,
    QPushButton, QLineEdit, QComboBox, QGroupBox,
    QHeaderView, QStatusBar, QToolBar, QDialog,
    QAbstractItemView,
    QMessageBox, QFrame, QProgressBar,
)
from PyQt6.QtCore import Qt, QSize, QTimer  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor  # pylint: disable=import-error,no-name-in-module

from hermes.ui.localization import load_translations
from hermes.ui.styles.styles import STYLE
from hermes.ui.widgets.dialog_buttons import dialog_button_row
from hermes.ui.widgets.process_list import ProcessSelectDialog
from hermes.core.scanner import ScanWorker, ValueRefreshWorker
from hermes.core.memory import write_process_value


@dataclass(frozen=True)
class ScanRequest:
    """Inputs required to start an initial or follow-up scan."""

    pid: int | None
    value_type: str
    scan_mode: str
    value: str
    value2: str = ""
    previous_results: list[dict[str, int | float | str]] | None = None


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
        self._current_proc = self.translations["app.no_process"]
        self._scan_results = []
        self._scan_worker = None
        self._scan_failed = False
        self._refresh_worker = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh_memory_values)

        # Initialize UI attributes assigned in builder methods.
        self.lbl_result_count = None
        self.result_table = None
        self.combo_type = None
        self.combo_scan_type = None
        self.input_value = None
        self.lbl_value2 = None
        self.input_value2 = None
        self.btn_first_scan = None
        self.btn_next_scan = None
        self.btn_reset = None
        self.lbl_scan_status = None
        self.progress_scan = None
        self.addr_table = None

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

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
        main_splitter.addWidget(self._build_address_table())
        main_splitter.setSizes([420, 220])

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
        self.lbl_result_count = QLabel(self.translations["results.count"].format(count=0))
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
        header = self.result_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.result_table.setAlternatingRowColors(True)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

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

        # Data type selector
        layout.addWidget(QLabel(self.translations["search.datatype"]))
        self.combo_type = QComboBox()
        for value_type in ("int32", "int64", "float", "double"):
            self.combo_type.addItem(value_type, value_type)
        self.combo_type.addItem(
            self.translations["search.auto_type"], "auto"
        )
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
        ]
        for text_key, mode in scan_modes:
            self.combo_scan_type.addItem(self.translations[text_key], mode)
        layout.addWidget(self.combo_scan_type)

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
        self.combo_scan_type.currentIndexChanged.connect(self._toggle_second_value)

        # Divider
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        self._build_scan_actions(layout)

        return box

    def _build_scan_actions(self, layout):
        """Add scan buttons, progress display, and status text."""
        self.btn_first_scan = QPushButton(self.translations["search.first_scan"])
        self.btn_first_scan.setObjectName("btn_primary")
        self.btn_first_scan.clicked.connect(self._first_scan)
        layout.addWidget(self.btn_first_scan)

        self.btn_next_scan = QPushButton(self.translations["search.next_scan"])
        self.btn_next_scan.setEnabled(False)
        self.btn_next_scan.clicked.connect(self._next_scan)
        layout.addWidget(self.btn_next_scan)

        self.btn_reset = QPushButton(self.translations["search.reset"])
        self.btn_reset.setObjectName("btn_danger")
        self.btn_reset.setEnabled(False)
        self.btn_reset.clicked.connect(self._reset_scan)
        layout.addWidget(self.btn_reset)

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

    def _toggle_second_value(self, index):
        """Show the upper-bound input only for range scans."""
        if (self.combo_scan_type is None or self.lbl_value2 is None or
            self.input_value2 is None):
            return
        visible = self.combo_scan_type.itemData(index) == "between"
        self.lbl_value2.setVisible(visible)
        self.input_value2.setVisible(visible)

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
        self.addr_table = QTableWidget(0, 5)
        self.addr_table.setHorizontalHeaderLabels(
            [
                self.translations["address_table.description"],
                self.translations["address_table.address"],
                self.translations["address_table.type"],
                self.translations["address_table.value"],
                self.translations["address_table.frozen"],
            ]
        )
        header = self.addr_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        self.addr_table.setAlternatingRowColors(True)
        self.addr_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        header = self.addr_table.verticalHeader()
        if header is not None:
            header.setVisible(False)

        self.addr_table.doubleClicked.connect(self._write_value)
        layout.addWidget(self.addr_table)

        return box

    def _add_address_row(self, desc, addr, vtype, value, frozen):
        """Append an address and its current state to the tracking table."""
        if self.addr_table is None:
            return

        row = self.addr_table.rowCount()
        self.addr_table.insertRow(row)

        desc_item = QTableWidgetItem(desc)
        desc_item.setFlags(desc_item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 0, desc_item)

        addr_item = QTableWidgetItem(addr)
        addr_item.setForeground(QColor("#3fb950"))
        addr_item.setFlags(addr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 1, addr_item)

        type_item = QTableWidgetItem(vtype)
        type_item.setForeground(QColor("#f0883e"))
        type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 2, type_item)

        val_item = QTableWidgetItem(value)
        val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 3, val_item)

        freeze_item = QTableWidgetItem(
            self.translations["address_table.yes"]
            if frozen else self.translations["address_table.no"]
        )
        freeze_item.setData(Qt.ItemDataRole.UserRole, frozen)
        freeze_item.setForeground(QColor("#58a6ff") if frozen else QColor("#8b949e"))
        freeze_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        freeze_item.setFlags(freeze_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.addr_table.setItem(row, 4, freeze_item)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _build_statusbar(self):
        """Create the status bar and its process-status labels."""
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel(self.translations["status.no_process_attached"])
        sb.addWidget(self.lbl_status)
        sb.addPermanentWidget(QLabel(self.translations["app.version_status"]))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _open_process_dialog(self):
        """Open process selection and update the attached-process state."""
        dlg = ProcessSelectDialog(self, self.translations)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._current_pid = dlg.selected_pid
            self._current_proc = self.translations["app.process_display"].format(
                name=dlg.selected_name, pid=dlg.selected_pid
            )
            self.lbl_proc_info.setText(f"  ⬢  {self._current_proc}")
            self.lbl_status.setText(
                self.translations["app.process_attached"].format(process=self._current_proc)
            )
            if self.btn_first_scan is not None:
                self.btn_first_scan.setEnabled(True)

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
        self.progress_scan.setValue(0)
        self.progress_scan.setVisible(True)
        self._scan_worker = ScanWorker(
            request.pid,
            request.value_type,
            request.scan_mode,
            request.value,
            request.value2,
            request.previous_results,
            self,
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
        self.result_table.setUpdatesEnabled(False)
        self.result_table.setRowCount(len(results))
        for row, result in enumerate(results):
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
            self.translations["results.count"].format(count=len(results))
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
        if self.progress_scan is not None:
            self.progress_scan.setVisible(False)
        if not self._scan_failed:
            if self.lbl_scan_status is not None:
                self.lbl_scan_status.setText(self.translations["search.ready"])

        self._scan_worker.deleteLater()
        self._scan_worker = None
        if self._current_pid is not None and self._has_refresh_targets():
            self._refresh_timer.start()

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
        last_row = self.result_table.rowAt(self.result_table.viewport().height() - 1)
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
            try:
                item1 = self.addr_table.item(row, 1)
                item2 = self.addr_table.item(row, 2)
                if item1 is None or item2 is None:
                    continue
                address = int(item1.text(), 0)
                value_type = item2.text()
            except (AttributeError, ValueError):
                continue
            entries[address] = {"address": address, "type": value_type}

        if not entries:
            return

        self._refresh_worker = ValueRefreshWorker(
            self._current_pid,
            list(entries.values()),
            self,
        )
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
        if self.lbl_scan_status is None:
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
                a0.ignore()
                return

            if self.lbl_scan_status is not None:
                self.lbl_scan_status.setText(
                    self.translations["search.canceling"]
                )
            worker.requestInterruption()

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
            self.lbl_result_count.setText(self.translations["results.count"].format(count=0))
        if self.lbl_scan_status is not None:
            self.lbl_scan_status.setText(self.translations["search.ready"])
        if self.btn_first_scan is not None:
            self.btn_first_scan.setEnabled(True)
        if self.btn_next_scan is not None:
            self.btn_next_scan.setEnabled(False)
        if self.btn_reset is not None:
            self.btn_reset.setEnabled(False)

    def _add_selected_to_table(self):
        """Add selected scan result entries to the address table."""
        if self.result_table is None:
            return

        selected_rows = {
            index.row()
            for index in self.result_table.selectionModel().selectedRows()
        }
        for row in selected_rows:
            item = self.result_table.item(row, 0)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if data:
                self._add_address_row(
                    self.translations["address_table.new_address"],
                    data["address"],
                    data["type"],
                    data["value"],
                    False
                )

    def _write_value(self):
        """Prompt for and write a replacement value to process memory."""
        row = self.addr_table.currentRow()
        if row < 0:
            return
        addr = self.addr_table.item(row, 1).text()
        vtype = self.addr_table.item(row, 2).text()
        cur = self.addr_table.item(row, 3).text()
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

            self.addr_table.item(row, 3).setText(str(written_value))
            self.lbl_status.setText(
                self.translations["status.written"].format(
                    address=addr, value=written_value
                )
            )

    def _toggle_freeze(self):
        """Toggle the displayed frozen state of the selected address."""
        row = self.addr_table.currentRow()
        if row < 0:
            return
        item = self.addr_table.item(row, 4)
        frozen = bool(item.data(Qt.ItemDataRole.UserRole))
        item.setData(Qt.ItemDataRole.UserRole, not frozen)
        item.setText(
            self.translations["address_table.no"]
            if frozen else self.translations["address_table.yes"]
        )
        item.setForeground(QColor("#8b949e") if frozen else QColor("#58a6ff"))
        # Pending: start or stop a FreezeWorker thread.

    def _remove_address(self):
        """Remove the selected address from the tracking table."""
        row = self.addr_table.currentRow()
        if row >= 0:
            self.addr_table.removeRow(row)
