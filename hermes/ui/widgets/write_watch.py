"""Live results dialog for a process-memory write (or access) watch."""

from datetime import datetime

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

from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height


# Result table columns.
_COUNT, _PC, _SITE, _READ, _WRITE, _VALUE, _THREAD, _TIME = range(8)


class WriteWatchDialog(QDialog):
    """Display grouped write-watch hits and let the user stop monitoring.

    With ``reads`` the watch reports reads as well; the "read" and "write"
    columns tell what each instruction does with the target.
    """

    stop_requested = pyqtSignal()
    # trap PC (instruction after the write), module/offset description
    # (a Qt int has 32 bits and would cut off 64-bit addresses)
    hook_requested = pyqtSignal(object, str)
    # trap PC to monitor in the native function monitor
    monitor_requested = pyqtSignal(object)

    def __init__(self, address, size, translations, parent=None, reads=False):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.translations = translations
        self._rows_by_pc = {}
        self._failed = False
        self.setWindowTitle(
            translations["watch.title_access" if reads else "watch.title"].format(
                address=address
            )
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

        self.results_table = QTableWidget(0, 8)
        self.results_table.setHorizontalHeaderLabels(
            [
                self.translations["watch.column_count"],
                self.translations["watch.column_trap_pc"],
                self.translations["watch.column_site"],
                self.translations["watch.column_read"],
                self.translations["watch.column_write"],
                self.translations["watch.column_value"],
                self.translations["watch.column_thread"],
                self.translations["watch.column_time"],
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
            for column in range(self.results_table.columnCount()):
                header.setSectionResizeMode(
                    column,
                    QHeaderView.ResizeMode.Stretch if column == _SITE
                    else QHeaderView.ResizeMode.ResizeToContents,
                )
            resizable_columns(self.results_table)
            copy_menu(self.results_table, self.translations)
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
        self.monitor_button = QPushButton(self.translations["watch.monitor"])
        self.monitor_button.setToolTip(self.translations["watch.monitor_tooltip"])
        self.monitor_button.setEnabled(False)
        self.monitor_button.clicked.connect(self._request_monitor)
        buttons.addWidget(self.monitor_button)
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
                for column in range(self.results_table.columnCount()):
                    self.results_table.setItem(row, column, QTableWidgetItem(""))
                self.results_table.item(row, _COUNT).setText("0")
                self.results_table.item(row, _PC).setText(pc)
                self.results_table.item(row, _SITE).setText(hit.get("site", pc))
                for column in (_READ, _WRITE):
                    item = self.results_table.item(row, column)
                    # A check box the user cannot change.
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)

            if hit.get("method"):
                # JIT code of a Mono game: the C# method says more than an address.
                site_item = self.results_table.item(row, _SITE)
                site_item.setText(hit["method"])
                site_item.setToolTip(hit.get("site", pc))
            count_item = self.results_table.item(row, _COUNT)
            count_item.setText(str(int(count_item.text()) + hit.get("count", 1)))
            # Older agents report writes only.
            for column, key, default in ((_READ, "read", False), (_WRITE, "write", True)):
                if hit.get(key, default):
                    self.results_table.item(row, column).setCheckState(Qt.CheckState.Checked)
            self.results_table.item(row, _VALUE).setText(hit.get("value", ""))
            self.results_table.item(row, _THREAD).setText(str(hit.get("threadId", "")))
            self.results_table.item(row, _TIME).setText(self._hit_time(hit))
        self._update_hook_button()

    @staticmethod
    def _hit_time(hit):
        """Return the local time of a hit's last write, e.g. ``14:03:27.418``."""
        milliseconds = hit.get("time")
        moment = datetime.now() if milliseconds is None \
            else datetime.fromtimestamp(milliseconds / 1000)
        return moment.strftime("%H:%M:%S.%f")[:-3]

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

    def _selected_writes(self):
        """Return whether the selected row's instruction writes the target."""
        row = self.results_table.currentRow()
        item = self.results_table.item(row, _WRITE) if row >= 0 else None
        return (self.results_table.selectionModel().hasSelection() and item is not None
                and item.checkState() == Qt.CheckState.Checked)

    def _update_hook_button(self):
        # Only writes can be hooked: the hook rewrites the written value.
        self.hook_button.setEnabled(self._selected_writes())
        self.monitor_button.setEnabled(self.results_table.currentRow() >= 0
                                       and self.results_table.selectionModel().hasSelection())

    def _request_monitor(self):
        row = self.results_table.currentRow()
        pc_item = self.results_table.item(row, _PC) if row >= 0 else None
        if pc_item is not None:
            self.monitor_requested.emit(int(pc_item.text(), 16))

    def _request_hook(self):
        row = self.results_table.currentRow()
        if row < 0 or not self._selected_writes():
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