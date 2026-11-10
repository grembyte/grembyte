"""Dialog for finding pointer chains to an address-list entry."""

import shiboken6  # pylint: disable=import-error,no-name-in-module
from PySide6.QtCore import Qt, QThread, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from hermes.core import chain_score, logs, memory_regions, messages
from hermes.core.pointer import (
    PointerMap,
    ScanCancelled,
    chain_for_static_address,
    chain_bases,
    missing_managed_bases,
    pointer_size,
    resolve_chain,
    split_managed,
)
from hermes.platform import (
    ProcessMemory,
    main_module,
    read_mappings,
    thread_stack_pointers,
)
from hermes.db.repository.addresses import SavedPointerScan
from hermes.features import managed_statics
from hermes.ui import window_state
from hermes.ui.widgets.icons import set_button_icon, theme_color
from hermes.ui.widgets.labels import DocsHint, WrappedLabel
from hermes.ui.widgets.spin_boxes import SpinBox
from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height
from hermes.ui.workers import keep_until_finished


# Rows shown in the result table; all candidates are kept and filtered.
DISPLAY_LIMIT = 2000
# Candidates added to the address list at most at once; each one is
# resolved again on every refresh of the list.
ADOPT_LIMIT = 500

# How long closing the dialog waits for a cancelled scan.
STOP_TIMEOUT_MS = 5000

# Modules listed in the tooltip of a memory category at most.
_TOOLTIP_MODULES = 25
# The memory categories chosen last (see hermes.core.memory_regions), apart
# for Mono games with marked assemblies, whose defaults differ.
_CATEGORIES_KEY = "pointer_scan/categories"
_ASSEMBLY_CATEGORIES_KEY = "pointer_scan/categories_assemblies"


def _remembered_categories(with_assemblies):
    """The categories chosen last, else the defaults."""
    key, default = ((_ASSEMBLY_CATEGORIES_KEY, memory_regions.ASSEMBLY_DEFAULT_CATEGORIES)
                    if with_assemblies else
                    (_CATEGORIES_KEY, memory_regions.DEFAULT_CATEGORIES))
    stored = window_state.settings().value(key)
    if not isinstance(stored, str) or not stored:
        return set(default)
    chosen = set(stored.split(",")) & set(memory_regions.CATEGORIES)
    return chosen or set(default)


class PointerScanWorker(QThread):
    """Run a pointer scan, or filter candidates, outside the GUI thread."""

    # phase ("reading", "searching", "filtering") and percent or level
    progress = Signal(str, int)
    chains_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, pid, target, *, chains=None, max_depth=5, max_offset=0x1000,
                 max_results=10000, categories=None, main_module=None, location_step=None,
                 value_alignment=None, assemblies=(), parent=None):
        # pylint: disable=too-many-arguments
        """``categories`` (see hermes.core.memory_regions) limit the memory read
        and the modules chains may start in; None reads all writable memory.
        ``location_step`` and ``value_alignment`` see ``PointerMap.build``.
        With the category ``assemblies``, chains may also start in the static
        data of the classes of ``assemblies`` (Mono)."""
        super().__init__(parent)
        logs.log_failures(self)
        self.pid = pid
        self.target = target
        self.chains = chains
        self.max_depth = max_depth
        self.max_offset = max_offset
        self.max_results = max_results
        self.categories = categories
        self.main_module = main_module
        self.location_step = location_step
        self.value_alignment = value_alignment
        self.assemblies = tuple(assemblies)
        # Modules chains may start in, from ``categories`` (set by the scan).
        self.base_modules = None
        # The StaticAreas of ``assemblies`` (set by the scan).
        self.static_areas = []
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
        except managed_statics.READ_ERRORS + (MemoryError,) as error:
            if not self.isInterruptionRequested():
                self.failed.emit(messages.describe(error))
            return
        if result is not None and not self.isInterruptionRequested():
            self.chains_ready.emit(result)

    def _scan(self):
        if (self.categories is not None and memory_regions.ASSEMBLIES in self.categories
                and self.assemblies):
            self.static_areas = managed_statics.read_static_areas(
                self.pid, {assembly: None for assembly in self.assemblies})
        pointer_map = PointerMap.build(
            self.pid,
            progress=lambda percent: self.progress.emit("reading", percent),
            cancelled=self.isInterruptionRequested,
            location_step=self.location_step,
            value_alignment=self.value_alignment,
            select=None if self.categories is None else self._select,
            static_areas=[(area.address, area.address + area.size, area.name)
                          for area in self.static_areas],
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

    def _select(self, mappings):
        regions = memory_regions.classify(mappings, self.main_module,
                                          thread_stack_pointers(self.pid))
        ranges, self.base_modules = memory_regions.selection(regions, self.categories)
        self.base_modules |= {area.name for area in self.static_areas}
        return ranges

    def _filter(self):
        missing = missing_managed_bases(self.pid, self.chains)
        if missing:
            managed_statics.read_static_areas(self.pid, missing)
        bases = chain_bases(self.pid, read_mappings(self.pid))
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

    chain_chosen = Signal(object)
    # Candidates (list of PointerChain) to add to the address list as entries.
    chains_adopted = Signal(object)
    # The pin button: True to dock the dialog in the main window, False to
    # float it again (see PointerScanDock).
    pin_toggled = Signal(bool)

    def __init__(self, translations, repository, pid, entry, parent=None,
                 address_source=None, assemblies=None):
        # pylint: disable=too-many-arguments
        """``assemblies``: the assemblies marked for a Mono game (their classes'
        static fields can start chains), None for other games."""
        super().__init__(parent)
        self.assemblies = None if assemblies is None else tuple(sorted(assemblies))
        self.setModal(False)
        self.address_source = address_source
        self.translations = translations
        self.repository = repository
        self.pid = pid
        self.entry = entry
        self.chain = None
        self._worker = None
        self._scan = self._load_scan()
        # Candidates with their Score, best first (see _rank).
        self._ranking = []
        # Memory category per module of the process, for the scores.
        self._module_categories = {}
        # The StaticAreas of the last scan by base name, to name their fields.
        self._areas = {}
        self.setWindowTitle(translations["pointer.title"].format(label=entry["label"]))
        self.resize(820, 620)
        self._static_chain = None
        self._main_module = main_module(pid)
        self._build_ui()
        self._show_regions()
        self._check_static(entry["address"])
        self._show_candidates()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        hint = DocsHint(self.translations, self.translations["pointer.hint"], "guide/pointer-scan/")
        layout.addWidget(hint)

        # Shown when the address lies in a module's static data: no scan needed.
        self.static_notice = QHBoxLayout()
        self.static_label = WrappedLabel("")
        self.static_label.setStyleSheet(f"color: {theme_color('success')};")
        self.btn_static = QPushButton(self.translations["pointer.static_use"])
        set_button_icon(self.btn_static, "push_pin")
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
        set_button_icon(self.btn_from_scan, "input")
        self.btn_from_scan.setToolTip(self.translations["pointer.from_scan_tooltip"])
        self.btn_from_scan.clicked.connect(self.take_scan_address)
        self.btn_from_scan.setVisible(self.address_source is not None)
        address_row.addWidget(self.btn_from_scan)
        form.addRow(self.translations["pointer.address"], address_row)
        self.depth_input = SpinBox()
        self.depth_input.setRange(1, 7)
        self.depth_input.setValue(self._scan.max_depth if self._scan else 5)
        self.depth_input.setToolTip(self.translations["pointer.depth_tooltip"])
        form.addRow(self.translations["pointer.depth"], self.depth_input)
        self.offset_input = QLineEdit(f"0x{self._scan.max_offset if self._scan else 0x1000:X}")
        self.offset_input.setToolTip(self.translations["pointer.offset_tooltip"])
        form.addRow(self.translations["pointer.offset"], self.offset_input)
        self.results_input = SpinBox()
        self.results_input.setRange(100, 1_000_000)
        self.results_input.setSingleStep(1000)
        self.results_input.setValue(10000)
        self.results_input.setToolTip(self.translations["pointer.max_results_tooltip"])
        form.addRow(self.translations["pointer.max_results"], self.results_input)
        # Memory regions: where chains may start, and what lies between
        chosen = _remembered_categories(bool(self.assemblies))
        self.region_boxes = {}
        starts = [category for category in memory_regions.START_CATEGORIES
                  if category != memory_regions.ASSEMBLIES or self.assemblies is not None]
        for label, categories in (
                ("pointer.regions_start", starts),
                ("pointer.regions_between", memory_regions.DYNAMIC_CATEGORIES)):
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            for position, category in enumerate(categories):
                box = QCheckBox()
                box.setChecked(category in chosen
                               or category in memory_regions.REQUIRED_CATEGORIES)
                box.setEnabled(category not in memory_regions.REQUIRED_CATEGORIES
                               and (category != memory_regions.ASSEMBLIES
                                    or bool(self.assemblies)))
                box.toggled.connect(self._remember_categories)
                self.region_boxes[category] = box
                grid.addWidget(box, position // 3, position % 3)
            form.addRow(self.translations[label], grid)
        # Where pointers lie and where they point to, in bytes
        size = self._pointer_size()
        self.location_step = QComboBox()
        for step in (8, 4):
            if step <= size:
                self.location_step.addItem(
                    self.translations["pointer.alignment_bytes"].format(bytes=step), step)
        self.location_step.setCurrentIndex(self.location_step.findData(size))
        self.location_step.setToolTip(self.translations["pointer.location_step_tooltip"])
        form.addRow(self.translations["pointer.location_step"], self.location_step)
        self.value_alignment = QComboBox()
        for alignment in (16, 8, 4, 1):
            label = (self.translations["pointer.alignment_any"] if alignment == 1
                     else self.translations["pointer.alignment_bytes"].format(bytes=alignment))
            self.value_alignment.addItem(label, alignment)
        self.value_alignment.setCurrentIndex(self.value_alignment.findData(size))
        self.value_alignment.setToolTip(self.translations["pointer.value_alignment_tooltip"])
        form.addRow(self.translations["pointer.value_alignment"], self.value_alignment)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.btn_scan = QPushButton(self.translations["pointer.scan"])
        set_button_icon(self.btn_scan, "search")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_filter = QPushButton(self.translations["pointer.filter"])
        set_button_icon(self.btn_filter, "filter_alt")
        self.btn_filter.setToolTip(self.translations["pointer.filter_tooltip"])
        self.btn_filter.clicked.connect(self.start_filter)
        self.btn_cancel = QPushButton(self.translations["pointer.cancel"])
        set_button_icon(self.btn_cancel, "close")
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

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            self.translations["pointer.column_score"],
            self.translations["pointer.column_base"],
            self.translations["pointer.column_offsets"],
            self.translations["pointer.column_depth"],
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        resizable_columns(self.table)
        copy_menu(self.table, self.translations)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(self._accept)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.btn_adopt = QPushButton(self.translations["pointer.adopt_all"])
        set_button_icon(self.btn_adopt, "playlist_add")
        self.btn_adopt.setToolTip(self.translations["pointer.adopt_all_tooltip"])
        self.btn_adopt.clicked.connect(self.adopt_candidates)
        bottom.addWidget(self.btn_adopt)
        # Once a stable chain is in use, the candidates are no longer needed.
        self.btn_clear = QPushButton(self.translations["pointer.clear_scan"])
        set_button_icon(self.btn_clear, "delete_sweep")
        self.btn_clear.setToolTip(self.translations["pointer.clear_scan_tooltip"])
        self.btn_clear.clicked.connect(self.clear_scan)
        bottom.addWidget(self.btn_clear)
        bottom.addStretch()
        self.btn_pin = QPushButton(self.translations["pointer.pin"])
        set_button_icon(self.btn_pin, "push_pin")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip(self.translations["pointer.pin_tooltip"])
        self.btn_pin.toggled.connect(self._on_pin_toggled)
        bottom.addWidget(self.btn_pin)
        close = QPushButton(self.translations["templates.close"])
        set_button_icon(close, "close")
        close.clicked.connect(self.reject)
        self.btn_use = QPushButton(self.translations["pointer.use"])
        self.btn_use.setObjectName("btn_primary")
        set_button_icon(self.btn_use, "check")
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
        """Return the current candidate chains, the most promising first."""
        return [chain for chain, _score in self._ranking]

    def _rank(self):
        """Score the candidates (see hermes.core.chain_score), best first."""
        chains = [] if self._scan is None else self._scan.chains
        categories = dict(self._module_categories)
        categories.update((chain.module, memory_regions.ASSEMBLIES) for chain in chains
                          if split_managed(chain.module) is not None)
        self._ranking = chain_score.ranked(chains, categories, self._pointer_size())

    def _base_text(self, chain):
        """The base of ``chain`` as the table shows it; a static field by its name."""
        managed = split_managed(chain.module)
        if managed is None:
            return f"{chain.module}+0x{chain.base_offset:x}"
        area = self._areas.get(chain.module)
        field = area.field_at(chain.base_offset) if area is not None else None
        if field is not None:
            return f"[{managed[0]}] {managed[1]}.{field}"
        return f"[{managed[0]}] {managed[1]}+0x{chain.base_offset:x}"

    def _pointer_size(self):
        return pointer_size(self.pid)

    def _score_tooltip(self, result):
        t = self.translations
        lines = []
        for reason, points, detail in result.parts:
            if reason == "start":
                text = t["pointer.score_start"].format(group=t[f"pointer.region.{detail}"])
            elif reason == "length":
                text = t["pointer.score_length"].format(count=detail)
            elif reason == "offsets":
                text = t["pointer.score_offsets"].format(limit=f"{detail:#x}")
            else:
                text = t["pointer.score_aligned"]
            lines.append(f"{points:+d}  {text}")
        return "\n".join(lines)

    def _show_candidates(self, message=None):
        self._rank()
        chains = self.candidates()
        self.table.setRowCount(0)
        for chain, result in self._ranking[:DISPLAY_LIMIT]:
            row = self.table.rowCount()
            self.table.insertRow(row)
            score = QTableWidgetItem(str(result.total))
            score.setData(Qt.ItemDataRole.UserRole, chain)
            score.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            score.setToolTip(self._score_tooltip(result))
            self.table.setItem(row, 0, score)
            self.table.setItem(row, 1, QTableWidgetItem(self._base_text(chain)))
            offsets = " → ".join(f"{offset:#x}" for offset in chain.offsets) or "—"
            self.table.setItem(row, 2, QTableWidgetItem(offsets))
            self.table.setItem(row, 3, QTableWidgetItem(str(len(chain.offsets))))
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
            self._main_module = main_module(pid)
            self._show_regions()
            self._show_candidates(self.translations["pointer.process_attached"])
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
        selected = len(self._selected_chains()) > 1
        self.btn_adopt.setText(self.translations[
            "pointer.adopt_selected" if selected else "pointer.adopt_all"])
        self.btn_adopt.setToolTip(self.translations[
            "pointer.adopt_selected_tooltip" if selected else "pointer.adopt_all_tooltip"])
        self.btn_clear.setEnabled(not running and self._scan is not None)
        self.btn_scan.setProperty("primary", self._scan is None)
        self.btn_filter.setProperty("primary", self._scan is not None)
        for button in (self.btn_scan, self.btn_filter):
            button.style().unpolish(button)
            button.style().polish(button)

    def _show_regions(self):
        """Label each memory category with its size and list its modules."""
        summary = None
        if self.pid is not None:
            try:
                mappings = read_mappings(self.pid)
                summary = memory_regions.summarize(memory_regions.classify(
                    mappings, self._main_module, thread_stack_pointers(self.pid)))
                self._module_categories = memory_regions.module_categories(
                    mappings, self._main_module)
            except OSError:
                summary = None
        t = self.translations
        for category, box in self.region_boxes.items():
            name = t[f"pointer.region.{category}"]
            tooltip = t[f"pointer.region.{category}_tooltip"]
            if category == memory_regions.ASSEMBLIES:
                name, tooltip = self._assemblies_text(name, tooltip)
            elif summary is not None:
                entry = summary[category]
                size = t["pointer.region_size"].format(size=entry.size / 2**20)
                if category == memory_regions.MAIN and entry.modules:
                    name += f" ({entry.modules[0]}, {size})"
                elif category in memory_regions.MODULE_CATEGORIES:
                    name += " (" + t["pointer.region_modules"].format(
                        count=len(entry.modules), size=size) + ")"
                else:
                    name += f" ({size})"
                if entry.modules and category != memory_regions.MAIN:
                    shown = entry.modules[:_TOOLTIP_MODULES]
                    tooltip += "\n\n" + ", ".join(shown)
                    if len(entry.modules) > len(shown):
                        tooltip += " " + t["pointer.region_more"].format(
                            count=len(entry.modules) - len(shown))
            box.setText(name)
            box.setToolTip(tooltip)

    def _assemblies_text(self, name, tooltip):
        """Label and tooltip of the marked assemblies: their names, after a
        scan the number and size of their classes' static data."""
        t = self.translations
        if not self.assemblies:
            return name + " (" + t["pointer.region.assemblies_none"] + ")", tooltip
        if self._areas:
            size = t["pointer.region_size"].format(
                size=sum(area.size for area in self._areas.values()) / 2**20)
            name += " (" + t["pointer.region_classes"].format(
                count=len(self._areas), size=size) + ")"
        else:
            shown = ", ".join(self.assemblies[:2])
            name += f" ({shown}{', …' if len(self.assemblies) > 2 else ''})"
        return name, tooltip + "\n\n" + ", ".join(self.assemblies)

    def _chosen_categories(self):
        return {category for category, box in self.region_boxes.items() if box.isChecked()}

    def _remember_categories(self):
        window_state.settings().setValue(
            _ASSEMBLY_CATEGORIES_KEY if self.assemblies else _CATEGORIES_KEY,
            ",".join(sorted(self._chosen_categories())))

    def _selected_chains(self):
        """The chains of the selected rows, in table order."""
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        items = (self.table.item(row, 0) for row in rows)
        return [item.data(Qt.ItemDataRole.UserRole) for item in items if item is not None]

    def _selected_chain(self):
        """The selected chain if exactly one is selected, else None."""
        chains = self._selected_chains()
        return chains[0] if len(chains) == 1 else None

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
        max_results = self.results_input.value()
        worker = PointerScanWorker(
            self.pid, target, max_results=max_results,
            categories=self._chosen_categories(), main_module=self._main_module,
            location_step=self.location_step.currentData(),
            value_alignment=self.value_alignment.currentData(),
            assemblies=self.assemblies or (), **settings
        )
        self._start(worker, lambda chains: self._scan_done(
            chains, settings, max_results, worker.complete_depth, worker.static_areas
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
        # The dialog may be deleted before a cancelled worker has finished.
        keep_until_finished(worker)
        worker.finished.connect(worker.deleteLater)
        self.progress.setValue(0)
        worker.start()
        self._update_buttons()

    def _if_alive(self, callback):
        """Wrap a worker callback so it is skipped once the dialog is deleted."""
        return lambda *args: None if not getattr(shiboken6, "isValid")(self) else callback(*args)

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

    def _scan_done(self, chains, settings, max_results, complete_depth, static_areas=()):
        # pylint: disable=too-many-arguments
        if static_areas:
            self._areas = {area.name: area for area in static_areas}
            self._show_regions()
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
        """Add the candidates to the address list, to check them after a restart.

        With several rows selected only those are added.
        """
        selected = self._selected_chains()
        chains = selected if len(selected) > 1 else self.candidates()
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

    def set_pinned(self, pinned):
        """Show whether the dialog is docked, without asking to change it."""
        self.btn_pin.blockSignals(True)
        self.btn_pin.setChecked(pinned)
        self.btn_pin.blockSignals(False)
        self.btn_pin.setText(self.translations["pointer.unpin" if pinned else "pointer.pin"])

    def _on_pin_toggled(self, pinned):
        self.btn_pin.setText(self.translations["pointer.unpin" if pinned else "pointer.pin"])
        self.pin_toggled.emit(pinned)

    def clear_scan(self):
        """Remove the saved candidates of this entry; its pointer stays."""
        if self._scan is None or self._worker is not None:
            return
        question = self.translations["pointer.clear_scan_confirm"].format(
            count=len(self._scan.chains))
        if QMessageBox.question(self, self.windowTitle(), question) \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.delete_pointer_scan(self.entry["id"])
        except SQLAlchemyError as error:
            self.status_label.setText(
                self.translations["hooks.db_failed"].format(message=error))
            return
        self._scan = None
        self._show_candidates(self.translations["pointer.scan_cleared"])

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


class PointerScanDock(QDockWidget):
    """A side of the main window holding one pointer scan dialog.

    Docked, the dialog stays next to the scanner while the value is searched
    again after a restart; a window of its own could not be kept there, since
    Wayland does not let programs place their windows. The dock holds one
    dialog: pinning another floats the one before. Closing the dock closes
    its dialog, and a dialog that finishes leaves the dock.
    """

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("pointer_scan_dock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self._window = window
        window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self)
        self.hide()

    def dialog(self):
        """The docked PointerScanDialog, or None."""
        widget = self.widget()
        return widget if isinstance(widget, PointerScanDialog) else None

    def pin(self, dialog):
        """Dock ``dialog`` (floating the one docked before)."""
        current = self.dialog()
        if current is dialog:
            self.show()
            return
        if current is not None:
            self.unpin(current)
        dialog.setParent(self, Qt.WindowType.Widget)
        self.setWidget(dialog)
        self.setWindowTitle(dialog.windowTitle())
        dialog.set_pinned(True)
        dialog.show()
        self.show()
        self.raise_()

    def unpin(self, dialog):
        """Give ``dialog`` a window of its own again."""
        if self.dialog() is not dialog:
            return
        self.setWidget(QWidget())
        dialog.setParent(self._window, Qt.WindowType.Dialog)
        dialog.set_pinned(False)
        dialog.show()
        self.hide()

    def release(self, dialog):
        """``dialog`` finished: empty the dock if it held it."""
        if self.dialog() is dialog:
            self.setWidget(QWidget())
            self.hide()

    def closeEvent(self, event):  # pylint: disable=invalid-name
        """The dock's close button closes its dialog."""
        dialog = self.dialog()
        if dialog is not None:
            dialog.reject()
        super().closeEvent(event)
