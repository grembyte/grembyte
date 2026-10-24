"""Dialog for finding pointer chains to an address-list entry."""

from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtCore import Qt, QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
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

from hermes.core import logs
from hermes.core.pointer import (
    PointerMap,
    ScanCancelled,
    chain_for_static_address,
    module_bases,
    resolve_chain,
)
from hermes.platform import ProcessMemory, main_module, read_mappings
from hermes.db.repository.addresses import SavedPointerScan
from hermes.ui.widgets.labels import WrappedLabel
from hermes.ui.widgets.table import resizable_columns, set_row_height


# Rows shown in the result table; all candidates are kept and filtered.
DISPLAY_LIMIT = 2000
# Candidates added to the address list at most at once; each one is
# resolved again on every refresh of the list.
ADOPT_LIMIT = 500

# How long closing the dialog waits for a cancelled scan.
STOP_TIMEOUT_MS = 5000

# Workers that are still running, possibly after their dialog was deleted;
# a QThread must not be destroyed while it runs.
_RUNNING_WORKERS = set()


class PointerScanWorker(QThread):
    """Run a pointer scan, or filter candidates, outside the GUI thread."""

    # phase ("reading", "searching", "filtering") and percent or level
    progress = pyqtSignal(str, int)
    chains_ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, pid, target, *, chains=None, max_depth=5, max_offset=0x1000,
                 max_results=10000, base_modules=None, aligned_values=True, parent=None):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        logs.log_failures(self)
        self.pid = pid
        self.target = target
        self.chains = chains
        self.max_depth = max_depth
        self.max_offset = max_offset
        self.max_results = max_results
        self.base_modules = base_modules
        self.aligned_values = aligned_values
        # Chain length up to which the results are complete if the scan
        # stopped at its memory limit, else None (set by the scan).
        self.complete_depth = None

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
            aligned_values=self.aligned_values,
        )
        chains = pointer_map.find_chains(
            self.target,
            max_depth=self.max_depth,
            max_offset=self.max_offset,
            max_results=self.max_results,
            base_modules=self.base_modules,
            progress=lambda level: self.progress.emit("searching", level),
            cancelled=self.isInterruptionRequested,
        )
        self.complete_depth = pointer_map.complete_depth
        return chains

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

    The dialog is not modal, so the value can be searched again with the
    scanner while it is open, even after the program was restarted
    (``set_process``); ``address_source()`` returns the address
    selected there (or None). The chosen chain is emitted with
    ``chain_chosen`` and kept in ``chain``.
    """

    # Widgets are created by _build_ui called from __init__.
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    chain_chosen = pyqtSignal(object)
    # Candidates (list of PointerChain) to add to the address list as entries.
    chains_adopted = pyqtSignal(list)

    def __init__(self, translations, repository, pid, entry, parent=None,
                 address_source=None):
        # pylint: disable=too-many-arguments
        super().__init__(parent)
        self.setModal(False)
        self.address_source = address_source
        self.translations = translations
        self.repository = repository
        self.pid = pid
        self.entry = entry
        self.chain = None
        self._worker = None
        self._scan = self._load_scan()
        self.setWindowTitle(translations["pointer.title"].format(label=entry["label"]))
        self.resize(820, 620)
        self._static_chain = None
        self._main_module = main_module(pid)
        self._build_ui()
        self._check_static(entry["address"])
        self._show_candidates()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        hint = WrappedLabel(self.translations["pointer.hint"])
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        # Shown when the address lies in a module's static data: no scan needed.
        self.static_notice = QHBoxLayout()
        self.static_label = WrappedLabel("")
        self.static_label.setStyleSheet("color: #3fb950;")
        self.btn_static = QPushButton(self.translations["pointer.static_use"])
        self.btn_static.clicked.connect(self._use_static)
        self.static_notice.addWidget(self.static_label, 1)
        self.static_notice.addWidget(self.btn_static)
        layout.addLayout(self.static_notice)

        form = QFormLayout()
        self.address_input = QLineEdit(f"0x{self.entry['address']:X}")
        self.address_input.setToolTip(self.translations["pointer.address_tooltip"])
        address_row = QHBoxLayout()
        address_row.addWidget(self.address_input, 1)
        self.btn_from_scan = QPushButton(self.translations["pointer.from_scan"])
        self.btn_from_scan.setToolTip(self.translations["pointer.from_scan_tooltip"])
        self.btn_from_scan.clicked.connect(self.take_scan_address)
        self.btn_from_scan.setVisible(self.address_source is not None)
        address_row.addWidget(self.btn_from_scan)
        form.addRow(self.translations["pointer.address"], address_row)
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
        self.results_input.setToolTip(self.translations["pointer.max_results_tooltip"])
        form.addRow(self.translations["pointer.max_results"], self.results_input)
        self.main_module_only = QCheckBox(self.translations["pointer.main_module_only"].format(
            module=self._main_module or "?"
        ))
        self.main_module_only.setToolTip(self.translations["pointer.main_module_only_tooltip"])
        self.main_module_only.setChecked(self._main_module is not None)
        self.main_module_only.setEnabled(self._main_module is not None)
        form.addRow("", self.main_module_only)
        self.aligned_values = QCheckBox(self.translations["pointer.aligned_values"])
        self.aligned_values.setToolTip(self.translations["pointer.aligned_values_tooltip"])
        self.aligned_values.setChecked(True)
        form.addRow("", self.aligned_values)
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
        resizable_columns(self.table)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(self._accept)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.btn_adopt = QPushButton(self.translations["pointer.adopt_all"])
        self.btn_adopt.setToolTip(self.translations["pointer.adopt_all_tooltip"])
        self.btn_adopt.clicked.connect(self.adopt_candidates)
        bottom.addWidget(self.btn_adopt)
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

    def set_process(self, pid):
        """Switch to the restarted program, or to None while it is not running.

        A running scan or filter of the previous process is cancelled.
        """
        if pid == self.pid:
            return
        if self._worker is not None:
            self._worker.requestInterruption()
        self.pid = pid
        if pid is None:
            self._static_chain = None
            self.static_label.setVisible(False)
            self.btn_static.setVisible(False)
            self.status_label.setText(self.translations["pointer.process_exited"])
        else:
            self.status_label.setText(self.translations["pointer.process_attached"])
        self._update_buttons()

    def _update_buttons(self):
        running = self._worker is not None
        attached = self.pid is not None
        self.btn_scan.setEnabled(not running and attached)
        self.btn_filter.setEnabled(not running and attached and bool(self.candidates()))
        self.btn_cancel.setVisible(running)
        self.progress.setVisible(running)
        self.btn_use.setEnabled(
            not running and attached and self._selected_chain() is not None
        )
        self.btn_adopt.setEnabled(not running and attached and bool(self.candidates()))
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

    def _check_static(self, address):
        """Show whether ``address`` is static, i.e. needs no pointer at all."""
        try:
            self._static_chain = chain_for_static_address(read_mappings(self.pid), address)
        except (OSError, TypeError):
            self._static_chain = None
        static = self._static_chain is not None
        if static:
            self.static_label.setText(self.translations["pointer.static"].format(
                chain=self._static_chain.text()
            ))
        self.static_label.setVisible(static)
        self.btn_static.setVisible(static)
        return static

    def _use_static(self):
        if self._static_chain is not None and self._worker is None and self.pid is not None:
            self._choose(self._static_chain)

    def take_scan_address(self):
        """Use the address selected in the scanner results."""
        address = self.address_source() if self.address_source is not None else None
        if address is None:
            self.status_label.setText(self.translations["pointer.from_scan_none"])
            return
        self.address_input.setText(f"0x{address:X}")
        self.status_label.setText(self.translations["pointer.from_scan_done"].format(
            address=f"0x{address:X}"
        ))

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
        self._check_static(target)
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
        base_modules = (
            {self._main_module} if self.main_module_only.isChecked() else None
        )
        max_results = self.results_input.value()
        worker = PointerScanWorker(
            self.pid, target, max_results=max_results, base_modules=base_modules,
            aligned_values=self.aligned_values.isChecked(), **settings
        )
        self._start(worker, lambda chains: self._scan_done(
            chains, settings, max_results, worker.complete_depth
        ))

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
        # The dialog may be deleted before a cancelled worker has finished.
        worker.progress.connect(self._if_alive(self._on_progress))
        worker.chains_ready.connect(self._if_alive(on_done))
        worker.failed.connect(self._if_alive(self._on_failed))
        worker.finished.connect(self._if_alive(lambda: self._on_finished(worker)))
        _RUNNING_WORKERS.add(worker)
        worker.finished.connect(lambda: _RUNNING_WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        self.progress.setValue(0)
        worker.start()
        self._update_buttons()

    def _if_alive(self, callback):
        """Wrap a worker callback so it is skipped once the dialog is deleted."""
        return lambda *args: None if sip.isdeleted(self) else callback(*args)

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

    def _scan_done(self, chains, settings, max_results, complete_depth):
        self._scan = SavedPointerScan(chains, settings["max_depth"], settings["max_offset"], 0)
        self._save()
        message = self.translations["pointer.found"].format(count=len(chains))
        if len(chains) >= max_results:
            message += "  " + self.translations["pointer.limit_reached"].format(
                depth=len(chains[-1].offsets)
            )
        if complete_depth is not None:
            message += "  " + self.translations["pointer.truncated"].format(
                depth=complete_depth
            )
        if not chains:
            message = self.translations[
                "pointer.none_found_static" if self._static_chain else "pointer.none_found"
            ]
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
        if self.pid is None:
            return  # the process exited; that is reported already
        self.status_label.setText(self.translations["pointer.failed"].format(message=message))

    def _on_finished(self, worker):
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
        if chain is None or self._worker is not None or self.pid is None:
            return
        self._choose(chain)

    def adopt_candidates(self):
        """Add the candidates to the address list, to check them after a restart."""
        chains = self.candidates()
        if not chains or self._worker is not None or self.pid is None:
            return
        if len(chains) > ADOPT_LIMIT:
            question = self.translations["pointer.adopt_limit_confirm"].format(
                count=len(chains), limit=ADOPT_LIMIT
            )
        else:
            question = self.translations["pointer.adopt_confirm"].format(count=len(chains))
        if QMessageBox.question(self, self.windowTitle(), question) \
                != QMessageBox.StandardButton.Yes:
            return
        self.chains_adopted.emit(chains[:ADOPT_LIMIT])

    def show_adopted(self, count):
        """Report how many candidates were added to the address list (None: entry gone)."""
        if count is None:
            self.status_label.setText(self.translations["pointer.entry_missing"])
        else:
            self.status_label.setText(self.translations["pointer.adopted"].format(count=count))

    def _choose(self, chain):
        self.chain = chain
        self.chain_chosen.emit(chain)
        self.accept()

    def done(self, result):  # pylint: disable=invalid-name
        """Stop a running scan before closing (it may finish after a timeout)."""
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait(STOP_TIMEOUT_MS)
        super().done(result)
