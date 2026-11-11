"""The player's Gameplay Ability System attributes (Unreal games): read, set, hold.

GAS keeps an attribute's base value and its current value, the base with
the active effects (equipment, food, talents) applied; GAS computes the
current one anew and damage calculations read it through GAS. So a value
written to memory does not last. This window sets the base value through
the game's own code instead (see the Unreal adapter's ``gasSetBase``): a
damage multiplier of 3 with a +40 % bonus active becomes 4.2.

The game sets base values itself, too (loading a save, levelling up, a
respec). "Hold" adds a script to the scripts tab that sets the value again
whenever it changed; it is saved with the game's scripts and runs whenever
hermes attaches. Unreal may name a set's object with a number it makes anew
on every load ("AttributeSet_Health_2147445233"); the script names the set
without it, as ``ctx.gas`` accepts it.
"""

import json
import re

from PySide6.QtCore import Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from hermes.features.scripts import ScriptSpec
from hermes.ui.widgets.docking import PinButton
from hermes.ui.widgets.icons import set_button_icon, theme_color, theme_qcolor
from hermes.ui.widgets.table import copy_menu, resizable_columns, set_row_height
from hermes.ui.widgets.labels import DocsHint

_SET, _ATTRIBUTE, _BASE, _CURRENT, _HELD = range(5)
_COLUMNS = ("gas.column_set", "gas.column_attribute", "gas.column_base",
            "gas.column_current", "gas.column_held")
# How often "Live" reads the values
LIVE_INTERVAL_MS = 1000
# How often a hold script looks whether the game changed the value
HOLD_SECONDS = 2.0
# Labels of the hold scripts: "GAS: AttributeSetCore.Might = 14"
HOLD_PREFIX = "GAS: "
# Theme colours (see icons.theme_color) of a held and of a changed value
_HELD_COLOR = "success"
_CHANGED_COLOR = "warning"

_HOLD_SOURCE = """\
// Made by the GAS attributes window: sets the base value of the player's
// attribute again whenever the game changed it (loading, levelling up ...).
const name = {name}, value = {value};
const attribute = ctx.gas.get(name);
if (attribute === null || Math.abs(attribute.base - value) <= 1e-4 * Math.max(1, Math.abs(value)))
  return;
ctx.gas.setBase(name, value);
return `${{name}} = ${{value}}`;
"""


_OBJECT_NUMBER = re.compile(r"_\d+$")


def script_set_name(name, names):
    """How scripts name set ``name`` among the player's sets ``names``: without
    the number of its object ("AttributeSet_Health" for
    "AttributeSet_Health_2147445233") unless another set has the same name
    without it."""
    bare = _OBJECT_NUMBER.sub("", name)
    if bare == name or not bare:
        return name
    if sum(_OBJECT_NUMBER.sub("", other) == bare for other in names) > 1:
        return name
    return bare


def hold_label(name, value):
    """The label of the script holding attribute ``name`` at ``value``."""
    return f"{HOLD_PREFIX}{name} = {format_value(value)}"


def hold_spec(name, value):
    """The script holding the player's attribute ``name`` ("Set.Attribute") at ``value``."""
    source = _HOLD_SOURCE.format(name=json.dumps(name), value=json.dumps(float(value)))
    return ScriptSpec(source, "repeat", HOLD_SECONDS, True, hold_label(name, value))


def held_value(label):
    """The value a hold script's label names, or None."""
    try:
        return float(label.rsplit(" = ", 1)[1])
    except (IndexError, ValueError):
        return None


def format_value(value):
    """A float as short as it reads (3, 1.4, 217.6), not 1.399999976."""
    return f"{value:.6g}"


def parse_value(text):
    """A number typed in either locale (1.5 or 1,5); ValueError otherwise."""
    return float(text.strip().replace(",", "."))


class GasAttributesDialog(QDialog):
    """The attribute sets of the player in an Unreal game with GAS.

    Not modal. Requests go through the scripts tab's worker (``panel``, a
    ScriptPanel), which also runs the hold scripts.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, translations, panel, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.panel = panel
        self._worker = None
        self._pending = None  # request id of the list asked for
        self._setting = {}  # request id -> attribute name
        self._state = None
        self._problem = None  # what the status line says is wrong, if anything
        self._filling = False
        self.setWindowTitle(translations["gas.title"])
        self.resize(820, 620)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(LIVE_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self.live_check.setChecked(True)
        self.refresh()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        hint = DocsHint(t, t["gas.hint"], "engines/unreal/#gas-attributes")
        layout.addWidget(hint)

        top = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t["gas.search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(lambda _text: self._fill())
        top.addWidget(self.search_input, 1)
        self.changed_check = QCheckBox(t["gas.only_changed"])
        self.changed_check.setToolTip(t["gas.only_changed_tooltip"])
        self.changed_check.toggled.connect(lambda _checked: self._fill())
        top.addWidget(self.changed_check)
        self.live_check = QCheckBox(t["gas.live"])
        self.live_check.setToolTip(t["gas.live_tooltip"])
        self.live_check.toggled.connect(self._set_live)
        top.addWidget(self.live_check)
        refresh = QPushButton(t["gas.refresh"])
        set_button_icon(refresh, "refresh")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([t[column] for column in _COLUMNS])
        self.table.horizontalHeaderItem(_BASE).setToolTip(t["gas.base_tooltip"])
        self.table.horizontalHeaderItem(_CURRENT).setToolTip(t["gas.current_tooltip"])
        self.table.horizontalHeaderItem(_HELD).setToolTip(t["gas.held_tooltip"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch if column == _ATTRIBUTE
                else QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_SET, 180)
        header.resizeSection(_BASE, 110)
        header.resizeSection(_CURRENT, 110)
        header.resizeSection(_HELD, 110)
        resizable_columns(self.table)
        copy_menu(self.table, t)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.owner_label = QLabel("")
        self.owner_label.setStyleSheet(f"color: {theme_color('muted')};")
        actions.addWidget(self.owner_label, 1)
        self.inspect_button = QPushButton(t["gas.inspect_owner"])
        set_button_icon(self.inspect_button, "person_search")
        self.inspect_button.setToolTip(t["gas.inspect_owner_tooltip"])
        self.inspect_button.clicked.connect(self.inspect_owner)
        actions.addWidget(self.inspect_button)
        self.hold_button = QPushButton(t["gas.hold"])
        self.hold_button.setObjectName("btn_primary")
        set_button_icon(self.hold_button, "push_pin")
        self.hold_button.setToolTip(t["gas.hold_tooltip"])
        self.hold_button.clicked.connect(self._hold_selected)
        actions.addWidget(self.hold_button)
        self.release_button = QPushButton(t["gas.release"])
        set_button_icon(self.release_button, "link_off")
        self.release_button.setToolTip(t["gas.release_tooltip"])
        self.release_button.clicked.connect(self._release_selected)
        actions.addWidget(self.release_button)
        self.pin_button = PinButton(t)
        actions.addWidget(self.pin_button)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._update_buttons()

    # --- Requests ------------------------------------------------------------------
    def _current_worker(self):
        """The scripts tab's worker, its answers connected; None when detached."""
        worker = self.panel.worker()
        if worker is not None and worker is not self._worker:
            self._worker = worker
            worker.gas_listed.connect(lambda request, state: self._on_listed(
                worker, request, state))
            worker.gas_failed.connect(lambda request, message: self._on_failed(
                worker, request, message))
            worker.gas_set.connect(lambda request, values: self._on_set(
                worker, request, values))
            worker.gas_set_failed.connect(lambda request, message: self._on_set_failed(
                worker, request, message))
            self._pending = None
            self._setting.clear()
        return worker

    def refresh(self):
        """Read the attributes anew (unless an answer is still due)."""
        if self._pending is not None:
            return
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["gas.detached"])
            return
        self._pending = worker.gas_attributes()

    def _set_live(self, live):
        if live:
            self._timer.start()
        else:
            self._timer.stop()

    def set_base(self, name, value):
        """Set the base value of attribute ``name`` ("Set.Attribute"); a hold
        script of it holds the new value from now on."""
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["gas.detached"])
            return
        if self.panel.scripts_labelled(f"{HOLD_PREFIX}{name} = "):
            self.panel.put_script(hold_spec(name, value), f"{HOLD_PREFIX}{name} = ")
        self._setting[worker.gas_set_base(name, value)] = name
        self.status_label.setText(self.translations["gas.setting"].format(
            name=name, value=format_value(value)))

    # --- Answers -------------------------------------------------------------------
    def _on_listed(self, worker, request, state):
        if worker is not self._worker or request != self._pending:
            return
        self._pending = None
        self._state = state
        t = self.translations
        self.owner_label.setText("" if state is None else t["gas.owner"].format(
            owner=state.get("owner", "?"), cls=state.get("ownerClass", "?")))
        # Without sets there is nothing to set: that one is said, not the setter
        problem = ("gas.no_player" if state is None
                   else "gas.no_sets" if not state.get("sets")
                   else "gas.no_setter" if not state.get("setter")
                   else "gas.no_component" if not state.get("component") else None)
        if problem is not None:
            self.status_label.setText(t[problem])
        elif self._problem is not None:
            self.status_label.setText("")
        self._problem = problem
        self._fill()

    def _on_failed(self, worker, request, message):
        if worker is not self._worker or request != self._pending:
            return
        self._pending = None
        self.status_label.setText(self.translations["gas.failed"].format(
            message=message.splitlines()[0] if message else message))

    def _on_set(self, worker, request, values):
        if worker is not self._worker:
            return
        name = self._setting.pop(request, None)
        if name is None:
            return
        self.status_label.setText(self.translations["gas.set"].format(
            name=name, base=format_value(values.get("base", 0)),
            current=format_value(values.get("current", 0))))
        self.refresh()

    def _on_set_failed(self, worker, request, message):
        if worker is not self._worker:
            return
        name = self._setting.pop(request, None)
        if name is None:
            return
        self.status_label.setText(self.translations["gas.set_failed"].format(
            name=name, message=message.splitlines()[0] if message else message))
        self._fill()  # the old value again

    # --- Table ---------------------------------------------------------------------
    def rows(self):
        """``[(set, attribute dict)]`` shown, after the search and the filter."""
        if not self._state:
            return []
        words = self.search_input.text().lower().split()
        changed = self.changed_check.isChecked()
        held = self._held()
        shown = []
        for attribute_set in self._state.get("sets", []):
            for attribute in attribute_set.get("attributes", []):
                text = f"{attribute_set['name']}.{attribute['name']}"
                if words and not all(word in text.lower() for word in words):
                    continue
                name = self.attribute_name(attribute_set["name"], attribute)
                if changed and name not in held and abs(
                        attribute["current"] - attribute["base"]) < 1e-6:
                    continue
                shown.append((attribute_set["name"], attribute))
        return shown

    def attribute_name(self, set_name, attribute):
        """The "Set.Attribute" name scripts use (see ``script_set_name``)."""
        names = [entry["name"] for entry in (self._state or {}).get("sets", [])]
        return f"{script_set_name(set_name, names)}.{attribute['name']}"

    def _held(self):
        """``{"Set.Attribute": value}`` of the attributes a hold script holds."""
        held = {}
        for spec in self.panel.scripts_labelled(HOLD_PREFIX).values():
            name = spec.label[len(HOLD_PREFIX):].rsplit(" = ", 1)[0]
            held[name] = held_value(spec.label)
        return held

    def _fill(self):
        """Show the values read last; a cell being edited keeps its text."""
        if self.table.state() == QAbstractItemView.State.EditingState:
            return
        held = self._held()
        rows = self.rows()
        selected = set(self.selected_names())
        self._filling = True
        try:
            self.table.setRowCount(len(rows))
            for row, (set_name, attribute) in enumerate(rows):
                name = self.attribute_name(set_name, attribute)
                hold = held.get(name)
                texts = (set_name, attribute["name"], format_value(attribute["base"]),
                         format_value(attribute["current"]),
                         "" if name not in held else
                         "✓" if hold is None else f"✓ {format_value(hold)}")
                for column, text in enumerate(texts):
                    item = self.table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem()
                        self.table.setItem(row, column, item)
                    item.setText(text)
                    flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    if column == _BASE:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                self.table.item(row, _ATTRIBUTE).setData(Qt.ItemDataRole.UserRole, name)
                self.table.item(row, _HELD).setForeground(theme_qcolor(_HELD_COLOR))
                # The effects change it: shown in the colour of a changed value
                differs = abs(attribute["current"] - attribute["base"]) >= 1e-6
                # An unchanged one in the table's own text colour: no colour of its own
                current = self.table.item(row, _CURRENT)
                if differs:
                    current.setForeground(theme_qcolor(_CHANGED_COLOR))
                else:
                    current.setData(Qt.ItemDataRole.ForegroundRole, None)
                if name in selected:
                    self.table.selectRow(row)
        finally:
            self._filling = False
        self._update_buttons()

    def selected_names(self):
        """The "Set.Attribute" names of the selected rows."""
        names = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), _ATTRIBUTE)
            if item is not None and item.data(Qt.ItemDataRole.UserRole):
                names.append(item.data(Qt.ItemDataRole.UserRole))
        return names

    def _on_item_changed(self, item):
        if self._filling or item.column() != _BASE:
            return
        name = self.table.item(item.row(), _ATTRIBUTE).data(Qt.ItemDataRole.UserRole)
        try:
            value = parse_value(item.text())
        except ValueError:
            self.status_label.setText(self.translations["gas.not_a_number"].format(
                text=item.text()))
            self._fill()
            return
        self.set_base(name, value)

    def inspect_owner(self):
        """Open the player's object in the object inspector (its fields)."""
        address = (self._state or {}).get("ownerAddress")
        inspections = self.panel.inspections
        if address and inspections is not None and self.panel.worker() is self._worker:
            inspections.inspect({"address": address}, name=self._state.get("owner", ""))

    def _update_buttons(self):
        names = self.selected_names()
        held = self._held()
        self.inspect_button.setEnabled(bool((self._state or {}).get("ownerAddress")))
        self.hold_button.setEnabled(bool(names))
        self.release_button.setEnabled(any(name in held for name in names))

    def _hold_selected(self):
        """Hold the selected attributes at their base value now."""
        values = {self.attribute_name(set_name, attribute): attribute["base"]
                  for set_name, attribute in self.rows()}
        names = [name for name in self.selected_names() if name in values]
        for name in names:
            self.panel.put_script(hold_spec(name, values[name]), f"{HOLD_PREFIX}{name} = ")
        if names:
            self.status_label.setText(self.translations["gas.held"].format(
                count=len(names)))
        self._fill()

    def _release_selected(self):
        names = set(self.selected_names())
        for name in names:
            for script_id in self.panel.scripts_labelled(f"{HOLD_PREFIX}{name} = "):
                self.panel.remove_script(script_id)
        if names:
            self.status_label.setText(self.translations["gas.released"].format(
                count=len(names)))
        self._fill()

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._timer.stop()
        super().closeEvent(event)
