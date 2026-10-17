"""Live results dialog for a process-memory write watch."""

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QHeaderView,
)

from hermes.ui.widgets.table import set_row_height


class WriteWatchDialog(QDialog):
    """Display grouped write-watch hits and let the user stop monitoring."""

    stop_requested = pyqtSignal()
    # trap PC (instruction after the write), module/offset description
    hook_requested = pyqtSignal(int, str)

    def __init__(self, address, size, translations, parent=None):
        super().__init__(parent)
        self.translations = translations
        self._rows_by_pc = {}
        self._failed = False
        self.setWindowTitle(
            translations["watch.title"].format(address=address)
        )
        self.resize(900, 460)
        self._build_ui(address, size)

    def _build_ui(self, address, size):
        layout = QVBoxLayout(self)
        target = QLabel(
            self.translations["watch.target"].format(
                address=address,
                size=size,
            )
        )
        target.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(target)

        self.status_label = QLabel(self.translations["watch.starting"])
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(
            [
                self.translations["watch.column_count"],
                self.translations["watch.column_trap_pc"],
                self.translations["watch.column_site"],
                self.translations["watch.column_value"],
                self.translations["watch.column_thread"],
            ]
        )
        self.results_table.setSelectionBehavior(
            self.results_table.SelectionBehavior.SelectRows
        )
        self.results_table.setEditTriggers(
            self.results_table.EditTrigger.NoEditTriggers
        )
        header = self.results_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.results_table.verticalHeader().setVisible(False)
        set_row_height(self.results_table)
        self.results_table.itemSelectionChanged.connect(self._update_hook_button)
        self.results_table.doubleClicked.connect(self._request_hook)
        layout.addWidget(self.results_table)

        buttons = QHBoxLayout()
        self.hook_button = QPushButton(self.translations["watch.create_hook"])
        self.hook_button.setObjectName("btn_primary")
        self.hook_button.setToolTip(self.translations["watch.create_hook_tooltip"])
        self.hook_button.setEnabled(False)
        self.hook_button.clicked.connect(self._request_hook)
        buttons.addWidget(self.hook_button)
        buttons.addStretch()
        self.stop_button = QPushButton(self.translations["watch.stop"])
        self.stop_button.clicked.connect(self._request_stop)
        buttons.addWidget(self.stop_button)
        close_button = QPushButton(self.translations["watch.close"])
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def add_hits(self, hits):
        """Append a batch of hits, grouping repeated PCs into a single row."""
        for hit in hits:
            pc = hit["pc"]
            row = self._rows_by_pc.get(pc)
            if row is None:
                row = self.results_table.rowCount()
                self.results_table.insertRow(row)
                self._rows_by_pc[pc] = row
                self.results_table.setItem(row, 0, QTableWidgetItem("0"))
                self.results_table.setItem(row, 1, QTableWidgetItem(pc))
                self.results_table.setItem(
                    row, 2, QTableWidgetItem(hit.get("site", pc))
                )
                self.results_table.setItem(row, 3, QTableWidgetItem(""))
                self.results_table.setItem(row, 4, QTableWidgetItem(""))

            count_item = self.results_table.item(row, 0)
            value_item = self.results_table.item(row, 3)
            thread_item = self.results_table.item(row, 4)
            if count_item is not None:
                count = int(count_item.text()) + hit.get("count", 1)
                count_item.setText(str(count))
            if value_item is not None:
                value_item.setText(hit.get("value", ""))
            if thread_item is not None:
                thread_item.setText(str(hit.get("threadId", "")))

    def set_monitoring(self, thread_count, details=""):
        text = self.translations["watch.running"].format(
            threads=thread_count
        )
        if details:
            text = f"{text} {details}"
        self.status_label.setText(text)

    def show_warning(self, message):
        self.status_label.setText(
            self.translations["watch.warning"].format(message=message)
        )

    def show_error(self, message):
        self._failed = True
        self.status_label.setText(
            self.translations["watch.failed"].format(message=message)
        )

    def set_stopped(self):
        self.stop_button.setEnabled(False)
        if not self._failed:
            self.status_label.setText(self.translations["watch.stopped"])

    def _update_hook_button(self):
        self.hook_button.setEnabled(
            self.results_table.selectionModel().hasSelection()
        )

    def _request_hook(self):
        row = self.results_table.currentRow()
        if row < 0:
            return
        pc_item = self.results_table.item(row, 1)
        site_item = self.results_table.item(row, 2)
        if pc_item is None:
            return
        self.hook_requested.emit(
            int(pc_item.text(), 16),
            site_item.text() if site_item is not None else pc_item.text(),
        )

    def _request_stop(self):
        self.stop_button.setEnabled(False)
        self.status_label.setText(self.translations["watch.stopping"])
        self.stop_requested.emit()