"""Process selection dialog widget."""

import psutil

from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QLineEdit,
    QPushButton, QHeaderView, QAbstractItemView, QMessageBox,
)
from PyQt6.QtGui import QColor  # pylint: disable=import-error,no-name-in-module


class ProcessSelectDialog(QDialog):
    """Lists running processes and lets the user select one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Prozess auswählen")
        self.setMinimumSize(480, 400)
        self.selected_pid = None
        self.selected_name = None
        self._build_ui()
        self._load_processes()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Prozessname filtern …")
        self.search_input.textChanged.connect(self._filter)
        layout.addWidget(self.search_input)

        # Process list
        self.list = QTableWidget(0, 3)
        self.list.setHorizontalHeaderLabels(["PID", "Name", "Pfad"])
        self.list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setAlternatingRowColors(True)
        self.list.verticalHeader().setVisible(False)
        self.list.doubleClicked.connect(self._accept)
        layout.addWidget(self.list)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("Anhängen")
        btn_ok.setObjectName("btn_primary")
        btn_ok.clicked.connect(self._accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)

    def _load_processes(self):
        """Loads the process list via psutil."""
        try:
            self._all_procs = []
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    info = proc.info
                    self._all_procs.append((
                        str(info["pid"]),
                        info["name"] or "",
                        info["exe"] or "",
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self._all_procs.sort(key=lambda x: x[1].lower())
            self._populate(self._all_procs)
        except ImportError:
            QMessageBox.warning(self, "psutil fehlt", "pip install psutil")

    def _populate(self, procs):
        self.list.setRowCount(0)
        for pid, name, exe in procs:
            row = self.list.rowCount()
            self.list.insertRow(row)
            pid_item = QTableWidgetItem(pid)
            pid_item.setForeground(QColor("#58a6ff"))
            self.list.setItem(row, 0, pid_item)
            self.list.setItem(row, 1, QTableWidgetItem(name))
            path_item = QTableWidgetItem(exe)
            path_item.setForeground(QColor("#8b949e"))
            self.list.setItem(row, 2, path_item)

    def _filter(self, text):
        filtered = [p for p in self._all_procs if text.lower() in p[1].lower()]
        self._populate(filtered)

    def _accept(self):
        row = self.list.currentRow()
        if row >= 0:
            self.selected_pid = int(self.list.item(row, 0).text())
            self.selected_name = self.list.item(row, 1).text()
            self.accept()
