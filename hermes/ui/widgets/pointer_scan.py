"""Dialog for finding pointer chains to an address-list entry."""

from PyQt6.QtCore import Qt, QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.core.pointer import (
    PointerMap,
    ProcessMemory,
    ScanCancelled,
    module_bases,
    read_mappings,
    resolve_chain,
)
from hermes.db.repository.addresses import SavedPointerScan
from hermes.ui.widgets.labels import WrappedLabel
from hermes.ui.widgets.table import set_row_height


# Rows shown in the result table; all candidates are kept and filtered.
DISPLAY_LIMIT = 2000


class PointerScanWorker(QThread):
    """Run a pointer scan, or filter candidates, outside the GUI thread."""

    # phase ("reading", "searching", "filtering") and percent or level
    progress = pyqtSignal(str, int)
    chains_ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, pid, target, *, chains=None, max_depth=5, max_offset=0x1000,
                 max_results=10000, parent=None):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.pid = pid
        self.target = target
        self.chains = chains
        self.max_depth = max_depth
        self.max_offset = max_offset
        self.max_results = max_results

    def run(self):
        try:
            if self.chains is None:
                result = self._scan()
            else:
                result = self._filter()
        except ScanCancelled:
            return
        except (OSError, ValueError, MemoryError) as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error))
            return
        if result is not None and not self.isInterruptionRequested():
            self.chains_ready.emit(result)

    def _scan(self):
        pointer_map = PointerMap.build(
            self.pid,
            progress=lambda percent: self.progress.emit("reading", percent),
            cancelled=self.isInterruptionRequested,
        )
        return pointer_map.find_chains(
            self.target,
            max_depth=self.max_depth,
            max_offset=self.max_offset,
            max_results=self.max_results,
            progress=lambda level: self.progress.emit("searching", level),
            cancelled=self.isInterruptionRequested,
        )

    def _filter(self):
        bases = module_bases(read_mappings(self.pid))
        kept = []
        with ProcessMemory(self.pid) as memory:
            for index, chain in enumerate(self.chains):
                if self.isInterruptionRequested():
                    return None
                if resolve_chain(memory, bases, chain) == self.target:
                    kept.append(chain)
                if index % 500 == 0:
                    self.progress.emit("filtering", index * 100 // len(self.chains))
        return kept


class PointerScanDialog(QDialog):
    """Search, filter and pick a pointer chain for an address-list entry.

    The candidates are saved for the entry, so after the program was
    restarted they can be filtered with the address the value has now.
    ``chain`` holds the chosen PointerChain after the dialog was accepted.
    """

    # Widgets are created by _build_ui called from __init__.
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    def __init__(self, translations, repository, pid, entry, parent=None):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.translations = translations
        self.repository = repository
        self.pid = pid
        self.entry = entry
        self.chain = None
        self._worker = None
        self._scan = self._load_scan()
        self.setWindowTitle(translations["pointer.title"].format(label=entry["label"]))
        self.resize(820, 620)
        self._build_ui()
        self._show_candidates()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        hint = WrappedLabel(self.translations["pointer.hint"])
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        form = QFormLayout()
        self.address_input = QLineEdit(f"0x{self.entry['address']:X}")
        self.address_input.setToolTip(self.translations["pointer.address_tooltip"])
        form.addRow(self.translations["pointer.address"], self.address_input)
        self.depth_input = QSpinBox()
        self.depth_input.setRange(1, 7)
        self.depth_input.setValue(self._scan.max_depth if self._scan else 5)
        self.depth_input.setToolTip(self.translations["pointer.depth_tooltip"])
        form.addRow(self.translations["pointer.depth"], self.depth_input)
        self.offset_input = QLineEdit(f"0x{self._scan.max_offset if self._scan else 0x1000:X}")
        self.offset_input.setToolTip(self.translations["pointer.offset_tooltip"])
        form.addRow(self.translations["pointer.offset"], self.offset_input)
        self.results_input = QSpinBox()
        self.results_input.setRange(100, 1_000_000)
        self.results_input.setSingleStep(1000)
        self.results_input.setValue(10000)
        form.addRow(self.translations["pointer.max_results"], self.results_input)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.btn_scan = QPushButton(self.translations["pointer.scan"])
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_filter = QPushButton(self.translations["pointer.filter"])
        self.btn_filter.setToolTip(self.translations["pointer.filter_tooltip"])
        self.btn_filter.clicked.connect(self.start_filter)
        self.btn_cancel = QPushButton(self.translations["pointer.cancel"])
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setVisible(False)
        actions.addWidget(self.btn_scan)
        actions.addWidget(self.btn_filter)
        actions.addWidget(self.btn_cancel)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        actions.addWidget(self.progress, 1)
        layout.addLayout(actions)

        self.status_label = WrappedLabel("")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([
            self.translations["pointer.column_base"],
            self.translations["pointer.column_offsets"],
            self.translations["pointer.column_depth"],
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(self._accept)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        close = QPushButton(self.translations["templates.close"])
        close.clicked.connect(self.reject)
        self.btn_use = QPushButton(self.translations["pointer.use"])
        self.btn_use.setObjectName("btn_primary")
        self.btn_use.clicked.connect(self._accept)
        bottom.addWidget(close)
        bottom.addWidget(self.btn_use)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _load_scan(self):
        try:
            return self.repository.get_pointer_scan(self.entry["id"])
        except SQLAlchemyError:
            return None

    def candidates(self):
        """Return the current candidate chains."""
        return [] if self._scan is None else list(self._scan.chains)

    def _show_candidates(self, message=None):
        chains = self.candidates()
        self.table.setRowCount(0)
        for chain in chains[:DISPLAY_LIMIT]:
            row = self.table.rowCount()
            self.table.insertRow(row)
            base = QTableWidgetItem(f"{chain.module}+0x{chain.base_offset:x}")
            base.setData(Qt.ItemDataRole.UserRole, chain)
            self.table.setItem(row, 0, base)
            offsets = " → ".join(f"{offset:#x}" for offset in chain.offsets) or "—"
            self.table.setItem(row, 1, QTableWidgetItem(offsets))
            self.table.setItem(row, 2, QTableWidgetItem(str(len(chain.offsets))))
        if message is None:
            if self._scan is None:
                message = self.translations["pointer.no_scan"]
            else:
                message = self.translations["pointer.candidates"].format(
                    count=len(chains), rounds=self._scan.rounds
                )
        if len(chains) > DISPLAY_LIMIT:
            message += "  " + self.translations["pointer.display_limit"].format(
                count=DISPLAY_LIMIT
            )
        self.status_label.setText(message)
        self._update_buttons()

    def _update_buttons(self):
        running = self._worker is not None
        self.btn_scan.setEnabled(not running)
        self.btn_filter.setEnabled(not running and bool(self.candidates()))
        self.btn_cancel.setVisible(running)
        self.progress.setVisible(running)
        self.btn_use.setEnabled(not running and self._selected_chain() is not None)
        self.btn_scan.setProperty("primary", self._scan is None)
        self.btn_filter.setProperty("primary", self._scan is not None)
        for button in (self.btn_scan, self.btn_filter):
            button.style().unpolish(button)
            button.style().polish(button)

    def _selected_chain(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.selectionModel().hasSelection():
            return None
        item = self.table.item(row, 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _target(self):
        try:
            return int(self.address_input.text().strip(), 0)
        except ValueError as error:
            raise ValueError(self.translations["pointer.invalid_address"]) from error

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def start_scan(self):
        """Search chains to the entered address, replacing saved candidates."""
        try:
            target = self._target()
            max_offset = int(self.offset_input.text().strip(), 0)
            if max_offset <= 0:
                raise ValueError
        except ValueError:
            self.status_label.setText(self.translations["pointer.invalid_input"])
            return
        if self._scan is not None and self._scan.chains:
            answer = QMessageBox.question(
                self,
                self.windowTitle(),
                self.translations["pointer.replace_confirm"].format(
                    count=len(self._scan.chains)
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        settings = {
            "max_depth": self.depth_input.value(),
            "max_offset": max_offset,
        }
        self._start(PointerScanWorker(
            self.pid, target, max_results=self.results_input.value(), **settings
        ), lambda chains: self._scan_done(chains, settings))

    def start_filter(self):
        """Keep the candidates that lead to the entered address now."""
        try:
            target = self._target()
        except ValueError as error:
            self.status_label.setText(str(error))
            return
        self._start(
            PointerScanWorker(self.pid, target, chains=self.candidates()),
            self._filter_done,
        )

    def _start(self, worker, on_done):
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.chains_ready.connect(on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self._on_finished(worker))
        self.progress.setValue(0)
        worker.start()
        self._update_buttons()

    def _on_progress(self, phase, value):
        if phase == "searching":
            self.progress.setRange(0, self.depth_input.value())
            self.status_label.setText(
                self.translations["pointer.searching"].format(level=value)
            )
        else:
            self.progress.setRange(0, 100)
            self.status_label.setText(self.translations[f"pointer.{phase}"])
        self.progress.setValue(value)

    def _scan_done(self, chains, settings):
        self._scan = SavedPointerScan(chains, settings["max_depth"], settings["max_offset"], 0)
        self._save()
        message = self.translations["pointer.found"].format(count=len(chains))
        if not chains:
            message = self.translations["pointer.none_found"]
        self._show_candidates(message)

    def _filter_done(self, chains):
        before = len(self._scan.chains)
        self._scan = SavedPointerScan(
            chains, self._scan.max_depth, self._scan.max_offset, self._scan.rounds + 1
        )
        self._save()
        self._show_candidates(self.translations["pointer.filtered"].format(
            before=before, count=len(chains), rounds=self._scan.rounds
        ))

    def _save(self):
        try:
            self.repository.save_pointer_scan(self.entry["id"], self._scan)
        except SQLAlchemyError as error:
            self.status_label.setText(
                self.translations["hooks.db_failed"].format(message=error)
            )

    def _on_failed(self, message):
        self.status_label.setText(self.translations["pointer.failed"].format(message=message))

    def _on_finished(self, worker):
        worker.deleteLater()
        if worker is self._worker:
            self._worker = None
        self._update_buttons()

    def _cancel(self):
        if self._worker is not None:
            self._worker.requestInterruption()
            self.status_label.setText(self.translations["pointer.cancelled"])

    def wait_for_worker(self, timeout_ms=None):
        """Wait until a running scan or filter has finished (for tests)."""
        worker = self._worker
        if worker is not None:
            if timeout_ms is None:
                worker.wait()
            else:
                worker.wait(timeout_ms)

    def _accept(self):
        chain = self._selected_chain()
        if chain is None or self._worker is not None:
            return
        self.chain = chain
        self.accept()

    def reject(self):  # pylint: disable=invalid-name
        """Stop a running scan before closing."""
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait(5000)
        super().reject()
