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

from hermes.ui.styles.styles import STYLE
from hermes.ui.widgets.process_list import ProcessSelectDialog


# ---------------------------------------------------------------------------
# Dialog: edit value
# ---------------------------------------------------------------------------
class ValueInputDialog(QDialog):
    """Simple dialog for entering a new value for a tracked address."""

    def __init__(self, address: str, current_value: str, value_type: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Wert ändern")
        self.setFixedSize(340, 160)
        self.new_value = None
        self._build_ui(address, current_value, value_type)

    def _build_ui(self, address, current, vtype):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(f"Adresse: <span style='color:#3fb950'>{address}</span>"
                      f"  &nbsp;Typ: <span style='color:#f0883e'>{vtype}</span>")
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        self.input = QLineEdit(current)
        self.input.selectAll()
        self.input.setPlaceholderText("Neuen Wert eingeben …")
        layout.addWidget(self.input)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("Schreiben")
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

    def __init__(self):
        super().__init__()
        self.setWindowTitle("hermes-hook  //  v0.1.0")
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)
        self.setStyleSheet(STYLE)

        self._current_pid = None
        self._current_proc = "Kein Prozess"

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
        tb = QToolBar("Hauptleiste")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        title = QLabel("⚡ hermes-hook")
        title.setObjectName("lbl_title")
        title.setContentsMargins(8, 0, 16, 0)
        tb.addWidget(title)

        tb.addSeparator()

        self.btn_process = QPushButton("⬡  Prozess auswählen")
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
        box = QGroupBox("Scan-Ergebnisse")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        # Control bar above the list
        ctrl = QHBoxLayout()
        self.lbl_result_count = QLabel("0 Treffer")
        self.lbl_result_count.setStyleSheet("color:#8b949e; font-size:11px;")
        ctrl.addWidget(self.lbl_result_count)
        ctrl.addStretch()

        btn_add = QPushButton("+ Merken")
        btn_add.setToolTip("Markierte Adresse in die untere Tabelle übernehmen")
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
        box = QGroupBox("Wertsuche")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        # Data type selector
        layout.addWidget(QLabel("Datentyp"))
        self.combo_type = QComboBox()
        self.combo_type.addItems(["int32", "int64", "float", "double"])
        layout.addWidget(self.combo_type)

        # Scan mode
        layout.addWidget(QLabel("Scan-Art"))
        self.combo_scan_type = QComboBox()
        self.combo_scan_type.addItems([
            "Exakter Wert",
            "Größer als",
            "Kleiner als",
            "Zwischen",
            "Unbekannter Startwert",
            "Unveränderter Wert",
            "Veränderter Wert",
        ])
        layout.addWidget(self.combo_scan_type)

        # Value input
        layout.addWidget(QLabel("Suchwert"))
        self.input_value = QLineEdit()
        self.input_value.setPlaceholderText("z. B. 1500")
        layout.addWidget(self.input_value)

        # Second value (for "between" range scan)
        self.lbl_value2 = QLabel("Bis-Wert")
        self.lbl_value2.setVisible(False)
        layout.addWidget(self.lbl_value2)
        self.input_value2 = QLineEdit()
        self.input_value2.setPlaceholderText("z. B. 2000")
        self.input_value2.setVisible(False)
        layout.addWidget(self.input_value2)
        self.combo_scan_type.currentTextChanged.connect(self._toggle_second_value)

        # Divider
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        # Scan buttons
        self.btn_first_scan = QPushButton("Erster Scan")
        self.btn_first_scan.setObjectName("btn_primary")
        self.btn_first_scan.clicked.connect(self._first_scan)
        layout.addWidget(self.btn_first_scan)

        self.btn_next_scan = QPushButton("Nächster Scan")
        self.btn_next_scan.setEnabled(False)
        self.btn_next_scan.clicked.connect(self._next_scan)
        layout.addWidget(self.btn_next_scan)

        self.btn_reset = QPushButton("Zurücksetzen")
        self.btn_reset.setObjectName("btn_danger")
        self.btn_reset.setEnabled(False)
        self.btn_reset.clicked.connect(self._reset_scan)
        layout.addWidget(self.btn_reset)

        layout.addStretch()

        # Status label
        self.lbl_scan_status = QLabel("Bereit.")
        self.lbl_scan_status.setStyleSheet("color:#8b949e; font-size:11px;")
        self.lbl_scan_status.setWordWrap(True)
        layout.addWidget(self.lbl_scan_status)

        return box

    def _toggle_second_value(self, text):
        visible = text == "Zwischen"
        self.lbl_value2.setVisible(visible)
        self.input_value2.setVisible(visible)

    # ------------------------------------------------------------------
    # Bottom panel: tracked addresses
    # ------------------------------------------------------------------
    def _build_address_table(self):
        box = QGroupBox("Gemerkte Adressen")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        # Table toolbar
        ctrl = QHBoxLayout()
        btn_write = QPushButton("✎  Wert schreiben")
        btn_write.clicked.connect(self._write_value)
        btn_freeze = QPushButton("❄  Einfrieren")
        btn_freeze.clicked.connect(self._toggle_freeze)
        btn_remove = QPushButton("✕  Entfernen")
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
            ["Beschreibung", "Adresse", "Typ", "Wert", "Eingefroren"]
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

        freeze_item = QTableWidgetItem("Ja" if frozen else "Nein")
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
        self.lbl_status = QLabel("Kein Prozess angehängt")
        sb.addWidget(self.lbl_status)
        sb.addPermanentWidget(QLabel("hermes-hook v0.1.0  |  Linux x86-64"))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _open_process_dialog(self):
        dlg = ProcessSelectDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._current_pid = dlg.selected_pid
            self._current_proc = f"{dlg.selected_name}  (PID {dlg.selected_pid})"
            self.lbl_proc_info.setText(f"  ⬢  {self._current_proc}")
            self.lbl_status.setText(f"Angehängt: {self._current_proc}")
            self.btn_first_scan.setEnabled(True)

    def _first_scan(self):
        if not self._current_pid:
            QMessageBox.warning(self, "Kein Prozess", "Bitte zuerst einen Prozess auswählen.")
            return
        value = self.input_value.text().strip()
        vtype = self.combo_type.currentText()
        scan_mode = self.combo_scan_type.currentText()
        self.lbl_scan_status.setText(f"Scanne … [{vtype}] {scan_mode} = {value}")
        self.btn_next_scan.setEnabled(True)
        self.btn_reset.setEnabled(True)
        self.btn_first_scan.setEnabled(False)
        # TODO: start ScanWorker thread and populate result_list.

    def _next_scan(self):
        value = self.input_value.text().strip()
        self.lbl_scan_status.setText(f"Verfeinere Suche … Wert = {value}")
        # TODO: run follow-up scan on existing addresses.

    def _reset_scan(self):
        self.result_list.clear()
        self.lbl_result_count.setText("0 Treffer")
        self.lbl_scan_status.setText("Bereit.")
        self.btn_first_scan.setEnabled(True)
        self.btn_next_scan.setEnabled(False)
        self.btn_reset.setEnabled(False)

    def _add_selected_to_table(self):
        """Add selected scan result entries to the address table."""
        for item in self.result_list.selectedItems():
            data = item.data(Qt.ItemDataRole.UserRole)
            if data:
                self._add_address_row(
                    "Neu",
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
        dlg = ValueInputDialog(addr, cur, vtype, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_value:
            self.addr_table.item(row, 3).setText(dlg.new_value)
            self.lbl_status.setText(f"Geschrieben: {addr} = {dlg.new_value}")
            # TODO: memory.write(pid, addr, dlg.new_value, vtype)

    def _toggle_freeze(self):
        row = self.addr_table.currentRow()
        if row < 0:
            return
        item = self.addr_table.item(row, 4)
        frozen = item.text() == "Ja"
        item.setText("Nein" if frozen else "Ja")
        item.setForeground(QColor("#8b949e") if frozen else QColor("#58a6ff"))
        # TODO: start/stop FreezeWorker thread.

    def _remove_address(self):
        row = self.addr_table.currentRow()
        if row >= 0:
            self.addr_table.removeRow(row)