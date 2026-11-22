"""Which objects addresses lie in: scan results or address-list entries named by
the engine as object + field.

A field found so can locate an address-list entry (``target_requested``, see
grembyte.core.pointer, object bases): the entry then names the class, the field
and the object, and finds the object again after a restart.

The engine adapter's ``locateReferences`` (through ``ScriptWorker.resolve_references``,
the same lookup the reference graph names a pointer's holder with) finds the live
object around each address and the field at its offset: ``PrevSoul`` of the one
``UIW_ProfileItem_Ergo_C``, ``m_gold`` of a Unity component. Memory that no object
covers (native allocations, the element blocks of Unreal's containers) stays unknown.
"""

from PySide6.QtCore import Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from grembyte.ui.widgets.docking import PinButton
from grembyte.ui.widgets.graph_canvas import short_class
from grembyte.ui.widgets.icons import set_button_icon, theme_qcolor
from grembyte.ui.widgets.reference_graph import RESOLVE_BATCH, find_reference_opener
from grembyte.ui.widgets.table import copy_menu, resizable_columns, set_row_height
from grembyte.ui.workers import connect_while_alive

# Addresses looked up at most at once (the first of a long result list)
MAX_ADDRESSES = 20000

_ADDRESS, _VALUE, _CLASS, _OBJECT, _FIELD = range(5)
# What an answer of the agent says about its location
IN_OBJECT = ("field", "element")
STATIC = "static"


def describe(entry, translations):
    """``(class, object, field)`` texts of an answer of ``locateReferences``,
    or None for a location in no object."""
    kind = entry.get("kind")
    if kind in IN_OBJECT:
        name = entry.get("name") or entry.get("object") or ""
        return short_class(entry.get("className")), name, entry.get("field") or ""
    if kind == STATIC:
        return (short_class(entry.get("className")), translations["owning.static"],
                entry.get("field") or "")
    return None


class OwningObjectsDialog(QDialog):
    """List the object (class, name, field) each of some addresses lies in.

    ``panel`` is the scripts tab (grembyte.ui.widgets.scripts.ScriptPanel): its
    worker looks the addresses up, its inspections open an object.
    ``entries`` are ``[{"address": int, "value": str, "type": value type,
    "entry_id": the address-list entry or None, "label": its description}]``;
    ``truncated`` how many more there were (not looked up).
    """

    # pylint: disable=too-many-instance-attributes

    # A field target for the address list: {address, object, className, field,
    # name, type, label, entry_id} (see MainWindow.add_field_target)
    target_requested = Signal(object)

    def __init__(self, translations, panel, entries, framework=(), truncated=0, parent=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        self.panel = panel
        self.framework = tuple(framework)
        self.entries = list(entries)
        self.truncated = truncated
        # location (hex text) -> answer of the agent
        self.answers = {}
        self._pending = {}  # request id -> the locations asked about
        self._worker = None
        self._relays = []
        self.setWindowTitle(translations["owning.title"].format(count=len(self.entries)))
        self.resize(900, 420)
        self._build_ui()
        self._fill()
        self.resolve()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.only_objects = QCheckBox(t["owning.only_objects"])
        self.only_objects.setChecked(True)
        self.only_objects.toggled.connect(self._apply_filter)
        layout.addWidget(self.only_objects)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            t["owning.column_address"], t["owning.column_value"], t["owning.column_class"],
            t["owning.column_object"], t["owning.column_field"]])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column in (_OBJECT, _FIELD)
                else QHeaderView.ResizeMode.ResizeToContents)
        resizable_columns(self.table)
        copy_menu(self.table, t)
        set_row_height(self.table)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _index: self.inspect())
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        # Entries of the address list get the target; scan results are added
        in_list = bool(self.entries) and all(entry.get("entry_id") is not None
                                             for entry in self.entries)
        self.target_button = QPushButton(t["owning.set_target" if in_list
                                           else "owning.add_target"])
        self.target_button.setObjectName("btn_primary")
        set_button_icon(self.target_button, "playlist_add")
        self.target_button.setToolTip(t["owning.add_target_tooltip"])
        self.target_button.clicked.connect(self.request_target)
        buttons.addWidget(self.target_button)
        self.inspect_button = QPushButton(t["references.inspect"])
        set_button_icon(self.inspect_button, "manage_search")
        self.inspect_button.setToolTip(t["references.inspect_tooltip"])
        self.inspect_button.clicked.connect(self.inspect)
        buttons.addWidget(self.inspect_button)
        self.references_button = QPushButton(t["objects.references"])
        set_button_icon(self.references_button, "hub")
        self.references_button.clicked.connect(self.show_references)
        buttons.addWidget(self.references_button)
        buttons.addStretch()
        self.pin_button = PinButton(t)
        buttons.addWidget(self.pin_button)
        close_button = QPushButton(t["watch.close"])
        set_button_icon(close_button, "close")
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self._update_buttons()

    def _fill(self):
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            address = QTableWidgetItem(f"0x{entry['address']:X}")
            address.setData(Qt.ItemDataRole.UserRole, entry["address"])
            self.table.setItem(row, _ADDRESS, address)
            self.table.setItem(row, _VALUE, QTableWidgetItem(str(entry.get("value", ""))))
            for column in (_CLASS, _OBJECT, _FIELD):
                self.table.setItem(row, column, QTableWidgetItem(""))

    # --- Looking up --------------------------------------------------------------
    def _current_worker(self):
        """The scripts tab's worker, its answers connected; None when detached."""
        worker = self.panel.worker()
        if worker is not None and worker is not self._worker:
            self._worker = worker
            for relay in self._relays:
                relay.deleteLater()
            self._relays = [
                connect_while_alive(signal, self, lambda request, answer, handler=handler:
                                    handler(worker, request, answer))
                for signal, handler in ((worker.references_resolved, self._on_resolved),
                                        (worker.references_failed, self._on_failed))]
            self._pending.clear()
        return worker

    def resolve(self):
        """Have the agent name the object of every address (in batches)."""
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["references.detached"])
            return
        locations = [entry["address"] for entry in self.entries]
        for at in range(0, len(locations), RESOLVE_BATCH):
            batch = locations[at:at + RESOLVE_BATCH]
            request = worker.resolve_references(batch, self.framework)
            self._pending[request] = [hex(location) for location in batch]
        self.status_label.setText(self.translations["owning.resolving"].format(
            count=len(locations)))

    def _on_resolved(self, worker, request, answer):
        if worker is not self._worker or request not in self._pending:
            return
        batch = self._pending.pop(request)
        for location, entry in zip(batch, answer):
            self.answers[location] = entry
        if not self._pending:
            self._show_answers()

    def _on_failed(self, worker, request, message):
        if worker is not self._worker or request not in self._pending:
            return
        self._pending.clear()
        self.status_label.setText(message)

    def _show_answers(self):
        found = 0
        muted = theme_qcolor("muted")
        for row in range(self.table.rowCount()):
            items = [self.table.item(row, column) for column in (_ADDRESS, _CLASS, _OBJECT, _FIELD)]
            if None in items:
                continue
            address_item, class_item, object_item, field_item = items
            entry = self.answers.get(hex(address_item.data(Qt.ItemDataRole.UserRole))) or {}
            texts = describe(entry, self.translations)
            if texts is None:
                class_item.setText(self.translations["owning.unknown"])
                class_item.setForeground(muted)
                continue
            found += 1
            for item, text in zip((class_item, object_item, field_item), texts):
                item.setText(text)
            class_item.setToolTip(entry.get("className") or "")
        text = self.translations["owning.found"].format(found=found, count=len(self.entries))
        if self.truncated:
            text = f"{text} " + self.translations["owning.truncated"].format(
                limit=len(self.entries), more=self.truncated)
        self.status_label.setText(text)
        self._apply_filter()

    def _apply_filter(self):
        only = self.only_objects.isChecked() and bool(self.answers)
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, only and self._answer(row) is None)
        self._update_buttons()

    def _answer(self, row):
        """The agent's answer for ``row`` if it names an object or static field, else None."""
        item = self.table.item(row, _ADDRESS)
        if item is None:
            return None
        entry = self.answers.get(hex(item.data(Qt.ItemDataRole.UserRole)))
        return entry if entry is not None and describe(entry, self.translations) else None

    def _selected_object(self):
        """The answer of the selected row if it lies in a live object, else None."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        entry = self._answer(rows[0].row())
        return entry if entry is not None and entry.get("kind") in IN_OBJECT else None

    def _update_buttons(self):
        entry = self._selected_object()
        self.target_button.setEnabled(self._selected_target() is not None)
        self.inspect_button.setEnabled(entry is not None and self.panel.inspections is not None)
        self.references_button.setEnabled(entry is not None
                                          and find_reference_opener(self) is not None)

    def _selected_target(self):
        """``target_requested``'s entry for the selected row, or None: a field
        of a live object (not an array's element) of a value with a type."""
        rows = self.table.selectionModel().selectedRows()
        entry = self._selected_object()
        if not rows or entry is None or entry.get("kind") != "field":
            return None
        source = self.entries[rows[0].row()]
        if not source.get("type"):
            return None
        class_name = entry.get("className") or ""
        return {
            "address": source["address"], "object": entry["object"],
            "className": class_name, "field": entry.get("field") or "",
            "name": entry.get("name") or "", "type": source["type"],
            "label": source.get("label") or f"{short_class(class_name)}.{entry.get('field')}",
            "entry_id": source.get("entry_id"),
        }

    # --- Actions -----------------------------------------------------------------
    def request_target(self):
        """Locate an address-list entry by the selected row's field."""
        target = self._selected_target()
        if target is not None:
            self.target_requested.emit(target)
            self.status_label.setText(self.translations["owning.target_added"].format(
                label=target["label"]))

    def inspect(self):
        """Open the selected row's object in the object inspector."""
        entry = self._selected_object()
        inspections = self.panel.inspections
        if entry is None or inspections is None:
            return
        inspections.inspect({"address": entry["object"]},
                            name=short_class(entry.get("className")),
                            instance_name=entry.get("name") or None)

    def show_references(self):
        """Show what holds the selected row's object (the reference graph)."""
        entry = self._selected_object()
        opener = find_reference_opener(self)
        if entry is None or opener is None:
            return
        opener(int(entry["object"], 16), entry.get("className") or "", entry.get("name"))
