"""hermes-hook – Main Window: application main UI (PyQt6)."""

from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTableWidget, QTableWidgetItem, QLabel,
    QPushButton, QLineEdit, QComboBox, QGroupBox,
    QHeaderView, QStatusBar, QToolBar, QDialog,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QMessageBox, QFrame,
)
from PyQt6.QtCore import Qt, QSize  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor  # pylint: disable=import-error,no-name-in-module

from hermes.ui.localization import load_translations
from hermes.ui.styles.styles import STYLE
from hermes.ui.widgets.process_list import ProcessSelectDialog


# ---------------------------------------------------------------------------
# Dialog: edit value
# ---------------------------------------------------------------------------
class ValueInputDialog(QDialog):
    """Simple dialog for entering a new value for a tracked address."""

    def __init__(self, address: str, current_value: str, value_type: str,
                 parent=None, translations=None):
        super().__init__(parent)
        self.translations = translations or load_translations()
        self.setWindowTitle(self.translations["value_dialog.title"])
        self.setFixedSize(340, 160)
        self.new_value = None
        self._build_ui(address, current_value, value_type)

    def _build_ui(self, address, current, vtype):
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

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton(self.translations["button.cancel"])
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton(self.translations["button.write"])
        btn_ok.setObjectName("btn_primary")
        btn_ok.clicked.connect(self._accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)

    def _accept(self):
        self.new_value = self.input.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):

    def __init__(self, language: str | None = None):
        super().__init__()
        self.translations = load_translations(language)
        self.setWindowTitle(self.translations["app.window_title"])
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)
        self.setStyleSheet(STYLE)

        self._current_pid = None
        self._current_proc = self.translations["app.no_process"]

        # Initialize UI attributes assigned in builder methods.
        self.lbl_result_count = None
        self.result_list = None
        self.combo_type = None
        self.combo_scan_type = None
        self.input_value = None
        self.lbl_value2 = None
        self.input_value2 = None
        self.btn_first_scan = None
        self.btn_next_scan = None
        self.btn_reset = None
        self.lbl_scan_status = None
        self.addr_table = None

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------
    def _build_toolbar(self):
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

        self.result_list = QListWidget()
        self.result_list.setAlternatingRowColors(True)
        self.result_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.result_list.doubleClicked.connect(self._add_selected_to_table)
        layout.addWidget(self.result_list)

        return box

    # ------------------------------------------------------------------
    # Right panel: search
    # ------------------------------------------------------------------
    def _build_search_panel(self):
        box = QGroupBox(self.translations["search.title"])
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        # Data type selector
        layout.addWidget(QLabel(self.translations["search.datatype"]))
        self.combo_type = QComboBox()
        self.combo_type.addItems(["int32", "int64", "float", "double"])
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

        # Scan buttons
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

        # Status label
        self.lbl_scan_status = QLabel(self.translations["search.ready"])
        self.lbl_scan_status.setStyleSheet("color:#8b949e; font-size:11px;")
        self.lbl_scan_status.setWordWrap(True)
        layout.addWidget(self.lbl_scan_status)

        return box

    def _toggle_second_value(self, index):
        visible = self.combo_scan_type.itemData(index) == "between"
        self.lbl_value2.setVisible(visible)
        self.input_value2.setVisible(visible)

    # ------------------------------------------------------------------
    # Bottom panel: tracked addresses
    # ------------------------------------------------------------------
    def _build_address_table(self):
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
        hdr = self.addr_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.addr_table.setAlternatingRowColors(True)
        self.addr_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.addr_table.verticalHeader().setVisible(False)
        self.addr_table.doubleClicked.connect(self._write_value)
        layout.addWidget(self.addr_table)

        return box

    def _add_address_row(self, desc, addr, vtype, value, frozen):
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
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel(self.translations["status.no_process_attached"])
        sb.addWidget(self.lbl_status)
        sb.addPermanentWidget(QLabel(self.translations["app.version_status"]))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _open_process_dialog(self):
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
            self.btn_first_scan.setEnabled(True)

    def _first_scan(self):
        if not self._current_pid:
            QMessageBox.warning(
                self,
                self.translations["warning.no_process_title"],
                self.translations["warning.select_process"],
            )
            return
        value = self.input_value.text().strip()
        vtype = self.combo_type.currentText()
        scan_mode = self.combo_scan_type.currentText()
        self.lbl_scan_status.setText(
            self.translations["search.scanning"].format(
                type=vtype, mode=scan_mode, value=value
            )
        )
        self.btn_next_scan.setEnabled(True)
        self.btn_reset.setEnabled(True)
        self.btn_first_scan.setEnabled(False)
        # TODO: start ScanWorker thread and populate result_list.

    def _next_scan(self):
        value = self.input_value.text().strip()
        self.lbl_scan_status.setText(
            self.translations["search.refining"].format(value=value)
        )
        # TODO: run follow-up scan on existing addresses.

    def _reset_scan(self):
        self.result_list.clear()
        self.lbl_result_count.setText(self.translations["results.count"].format(count=0))
        self.lbl_scan_status.setText(self.translations["search.ready"])
        self.btn_first_scan.setEnabled(True)
        self.btn_next_scan.setEnabled(False)
        self.btn_reset.setEnabled(False)

    def _add_selected_to_table(self):
        """Add selected scan result entries to the address table."""
        for item in self.result_list.selectedItems():
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
        row = self.addr_table.currentRow()
        if row < 0:
            return
        addr = self.addr_table.item(row, 1).text()
        vtype = self.addr_table.item(row, 2).text()
        cur = self.addr_table.item(row, 3).text()
        dlg = ValueInputDialog(addr, cur, vtype, self, self.translations)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_value:
            self.addr_table.item(row, 3).setText(dlg.new_value)
            self.lbl_status.setText(
                self.translations["status.written"].format(
                    address=addr, value=dlg.new_value
                )
            )
            # TODO: memory.write(pid, addr, dlg.new_value, vtype)

    def _toggle_freeze(self):
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
        # TODO: start/stop FreezeWorker thread.

    def _remove_address(self):
        row = self.addr_table.currentRow()
        if row >= 0:
            self.addr_table.removeRow(row)