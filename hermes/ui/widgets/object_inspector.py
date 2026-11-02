"""Object inspector: the fields of a managed object in a Mono game."""

import json
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor, QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from hermes.core import messages
from hermes.ui import window_state
from hermes.ui.widgets.icons import set_button_icon
from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.table import add_copy_actions, copy_menu, resizable_columns, set_row_height


LIVE_INTERVAL_MS = 500

_FIELD, _TYPE, _VALUE, _OFFSET, _OPEN = range(5)
_P_NAME, _P_TYPE, _P_VALUE, _P_LINK, _P_SET = range(5)
_M_NAME, _M_PARAMS, _M_RETURNS, _M_OWNER, _M_LINK = range(5)
# Data role of the offset cell: the field's {address, valueType} (see monitor.js)
_LOCATION_ROLE = Qt.ItemDataRole.UserRole + 1
_STATIC = QColor("#8b949e")
# What a static field shows when it has no value to read, by its storage
_STATIC_KINDS = {"const": "inspector.static_const", "thread": "inspector.static_thread"}
_ERROR = QColor("#f85149")
# Qt's default link blue is unreadable on the dark theme.
LINK_COLOR = "#58a6ff"
# Unreal's containers, whose entries the inspector shows as fields
_CONTAINERS = ("TArray<", "TMap<", "TSet<")
# Largest height Qt allows a widget (QWIDGETSIZE_MAX)
_NO_LIMIT = 16777215


def link_label(text, on_click):
    """A table cell widget with one link."""
    label = QLabel(f'<a href="#" style="color: {LINK_COLOR}; text-decoration: none;">{text}</a>')
    label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    label.setContentsMargins(8, 0, 8, 0)
    label.setCursor(Qt.CursorShape.PointingHandCursor)
    label.linkActivated.connect(lambda _link: on_click())
    return label


def link_width(widget, texts):
    """Column width that fits a link with any of ``texts`` in ``widget``."""
    widths = []
    for text in texts:
        label = link_label(text, lambda: None)
        label.hide()
        label.setParent(widget)  # styled like the table's cells
        label.ensurePolished()
        # The cells' font may be wider (e.g. monospace) than the label's.
        widths.append(max(label.sizeHint().width(),
                          widget.fontMetrics().horizontalAdvance(text) + 16))
        label.setParent(None)
        label.deleteLater()
    return max(widths) + 48


class InspectorSection(QWidget):
    """A part of the inspector: a header (title, its own controls) above a body.

    With a ``key`` the section folds away to its header through the button
    at the header's right; whether it is open is kept between runs (under
    ``inspector/<key>_open``, ``default`` the first time).
    """

    toggled = pyqtSignal(bool)

    def __init__(self, translations, title, tooltip, body, controls=(), key=None, default=True):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__()
        self.translations = translations
        self.body = body
        self.key = key
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        label.setToolTip(tooltip)
        header.addWidget(label)
        header.addStretch()
        self.controls = list(controls)
        for control in self.controls:
            header.addWidget(control)
        self.fold_button = None
        if key is not None:
            self.fold_button = QToolButton()
            self.fold_button.setAutoRaise(True)
            self.fold_button.clicked.connect(lambda: self.set_open(not self.is_open))
            header.addWidget(self.fold_button)
        layout.addWidget(self.header)
        layout.addWidget(body, 1)
        self.is_open = True
        if key is not None:
            self.set_open(window_state.settings().value(
                f"inspector/{key}_open", default, type=bool), remember=False)

    def set_open(self, is_open, remember=True):
        """Show the body, or fold the section away to its header."""
        self.is_open = is_open
        self.body.setVisible(is_open)
        for control in self.controls:
            control.setVisible(is_open)
        # Folded, the splitter must not give it more than the header.
        self.setMaximumHeight(_NO_LIMIT if is_open else self.header.sizeHint().height())
        if self.fold_button is not None:
            set_button_icon(self.fold_button, "expand_less" if is_open else "expand_more")
            self.fold_button.setToolTip(self.translations[
                "inspector.fold" if is_open else "inspector.unfold"])
        if remember and self.key is not None:
            window_state.settings().setValue(f"inspector/{self.key}_open", is_open)
        self.toggled.emit(is_open)


class ObjectInspectorDialog(QDialog):
    """Show the fields of an object, array or struct.

    Either ``contents`` recorded at a call (``captured`` names the time) or
    the current fields of ``target``, read through ``request(target, dialog)``
    whose answer comes back through ``show_contents`` or ``show_error``. A
    field holding an object gets a link; ``open_object(target, dialog, name)``
    shows that object in a dialog of its own. ``name`` (the variable, field or
    property the object came from) and ``depth`` (1 for an object of a call,
    one more for each object opened from it) go into the window title.

    Below the fields, the object's properties (getters) can be evaluated on
    the game's main thread through ``evaluate(target, names, dialog)``; the
    values come back through ``show_properties``. A property with a setter
    can be set, and below the getters the object's methods are listed
    (``list_methods(target, dialog)``, answered through ``show_methods``) to
    call: ``call_method(target, entry, dialog, object_text)`` offers either
    in a method call dialog, whose ``call_done`` comes to ``on_call_done``.
    Getters and methods fold away.

    A field holding a number can be put on the address list, to watch,
    change or freeze its value there. A string (field or property) gets a
    link that shows it whole: ``show_text(value, name, time)``.
    """

    # pylint: disable=too-many-instance-attributes,too-many-arguments,too-many-public-methods
    # pylint: disable=attribute-defined-outside-init  # the _build_* helpers of _build_ui

    # {"address": int, "value_type": str, "label": str} to add to the address list
    address_requested = pyqtSignal(object)

    def __init__(self, translations, open_object, target=None, request=None,
                 contents=None, captured=None, evaluate=None, name="", depth=1, parent=None,
                 show_text=None, list_methods=None, call_method=None):
        # pylint: disable=too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        self.target = target
        self._open_object = open_object
        self._request = request
        self._evaluate = evaluate
        self._show_text = show_text
        self._list_methods = list_methods
        self._call_method = call_method
        self._captured = captured
        self.name = name
        self.depth = depth
        self._pending = False
        # property name -> value, once evaluated
        self._property_values = {}
        # the object's instance methods once listed; None: not asked yet
        self._methods = None
        self._methods_pending = False
        # the property whose setter was called last, to show its new value
        self._setting = None
        self._type_name = "?"
        self._set_window_title("?")
        self.resize(860, 760)
        self._build_ui(live=contents is None)
        self._timer = QTimer(self)
        self._timer.setInterval(LIVE_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        if contents is not None:
            self.show_contents(contents)
        else:
            self.title_label.setText(target["address"])
            self.refresh()
        self._load_methods()

    def _build_ui(self, live):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.title_label = SelectableText()
        bar.addWidget(self.title_label, 1)
        self.live_check = QCheckBox(self.translations["inspector.live"])
        self.live_check.setToolTip(self.translations["inspector.live_tooltip"])
        self.live_check.toggled.connect(self._toggle_live)
        self.refresh_button = QPushButton(self.translations["inspector.refresh"])
        set_button_icon(self.refresh_button, "refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.add_address_button = QPushButton(self.translations["inspector.add_address"])
        set_button_icon(self.add_address_button, "bookmark_add")
        self.add_address_button.setToolTip(self.translations["inspector.add_address_tooltip"])
        self.add_address_button.setEnabled(False)
        self.add_address_button.clicked.connect(lambda: self.add_address(self.table.currentRow()))
        bar.addWidget(self.add_address_button)
        if live:
            bar.addWidget(self.live_check)
            bar.addWidget(self.refresh_button)
        else:
            self.live_check.hide()
            self.refresh_button.hide()
        layout.addLayout(bar)

        self.fields_section = InspectorSection(
            self.translations, self.translations["inspector.fields"],
            self.translations["inspector.fields_tooltip"], self._build_fields_table())
        self.properties_section = InspectorSection(
            self.translations, self.translations["inspector.properties"],
            self.translations["inspector.properties_tooltip"], self._build_properties_table(),
            controls=[self.evaluate_button], key="getters")
        self.methods_section = InspectorSection(
            self.translations, self.translations["inspector.methods"],
            self.translations["inspector.methods_tooltip"], self._build_methods_table(),
            controls=[self.method_filter, self.base_classes_check], key="methods",
            default=False)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.sections = (self.fields_section, self.properties_section, self.methods_section)
        for section in self.sections:
            self.splitter.addWidget(section)
            section.toggled.connect(self._on_section_toggled)
        self._share_space()
        layout.addWidget(self.splitter, 1)

        self.status_label = QLabel(self.translations[
            "inspector.hint" if live else "inspector.hint_captured"])
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_fields_table(self):
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            self.translations["inspector.column_field"],
            self.translations["inspector.column_type"],
            self.translations["inspector.column_value"],
            self.translations["inspector.column_offset"],
            "",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_FIELD, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_TYPE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_VALUE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_OFFSET, QHeaderView.ResizeMode.ResizeToContents)
        # Cell widgets do not count for ResizeToContents; keep a measured start width,
        # but let the user adjust it like the other columns.
        header.setSectionResizeMode(_OPEN, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_OPEN, link_width(self.table, [
            self.translations["inspector.open"], self.translations["inspector.text"]]))
        header.resizeSection(_TYPE, 200)
        resizable_columns(self.table)
        self.table.cellDoubleClicked.connect(lambda row, _column: self.open_row(row))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_field_menu)
        self.table.currentCellChanged.connect(
            lambda row, *_args: self.add_address_button.setEnabled(
                self.address_entry(row) is not None))
        return self.table

    def _build_properties_table(self):
        self.evaluate_button = QPushButton(self.translations["inspector.evaluate_all"])
        set_button_icon(self.evaluate_button, "calculate")
        self.evaluate_button.setToolTip(self.translations["inspector.properties_tooltip"])
        self.evaluate_button.clicked.connect(lambda: self.evaluate_properties(None))
        self.evaluate_button.setEnabled(False)
        self.properties_table = QTableWidget(0, 5)
        self.properties_table.setHorizontalHeaderLabels([
            self.translations["inspector.column_property"],
            self.translations["inspector.column_type"],
            self.translations["inspector.column_value"],
            "",
            "",
        ])
        self.properties_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.properties_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.properties_table.verticalHeader().setVisible(False)
        set_row_height(self.properties_table)
        properties_header = self.properties_table.horizontalHeader()
        properties_header.setSectionResizeMode(_P_NAME, QHeaderView.ResizeMode.ResizeToContents)
        properties_header.setSectionResizeMode(_P_TYPE, QHeaderView.ResizeMode.Interactive)
        properties_header.setSectionResizeMode(_P_VALUE, QHeaderView.ResizeMode.Stretch)
        properties_header.setSectionResizeMode(_P_LINK, QHeaderView.ResizeMode.Interactive)
        properties_header.setSectionResizeMode(_P_SET, QHeaderView.ResizeMode.Interactive)
        properties_header.resizeSection(_P_TYPE, 200)
        properties_header.resizeSection(_P_LINK, link_width(self.properties_table, [
            self.translations["inspector.open"], self.translations["inspector.evaluate"],
            self.translations["inspector.text"]]))
        properties_header.resizeSection(_P_SET, link_width(
            self.properties_table, [self.translations["inspector.set"]]))
        resizable_columns(self.properties_table)
        copy_menu(self.properties_table, self.translations)
        return self.properties_table

    def _build_methods_table(self):
        self.method_filter = QLineEdit()
        self.method_filter.setPlaceholderText(self.translations["inspector.method_filter"])
        self.method_filter.setClearButtonEnabled(True)
        self.method_filter.textChanged.connect(lambda _text: self._filter_methods())
        self.base_classes_check = QCheckBox(self.translations["inspector.base_classes"])
        self.base_classes_check.setToolTip(self.translations["inspector.base_classes_tooltip"])
        self.base_classes_check.toggled.connect(lambda _checked: self._filter_methods())
        self.methods_table = QTableWidget(0, 5)
        self.methods_table.setHorizontalHeaderLabels([
            self.translations["inspector.column_method"],
            self.translations["inspector.column_params"],
            self.translations["inspector.column_returns"],
            self.translations["inspector.column_owner"],
            "",
        ])
        self.methods_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.methods_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.methods_table.verticalHeader().setVisible(False)
        set_row_height(self.methods_table)
        header = self.methods_table.horizontalHeader()
        header.setSectionResizeMode(_M_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_M_PARAMS, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_M_RETURNS, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_M_OWNER, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_M_LINK, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(_M_NAME, 200)
        header.resizeSection(_M_RETURNS, 140)
        header.resizeSection(_M_OWNER, 160)
        header.resizeSection(_M_LINK, link_width(
            self.methods_table, [self.translations["inspector.call"]]))
        resizable_columns(self.methods_table)
        copy_menu(self.methods_table, self.translations)
        self.methods_table.cellDoubleClicked.connect(lambda row, _column: self._call_row(row))
        return self.methods_table

    # --- Sections ----------------------------------------------------------------
    def _on_section_toggled(self, is_open):
        self._share_space()
        if is_open:
            self._load_methods()

    def _share_space(self):
        """Give the open sections equal parts of the height; folded ones keep their header."""
        heights = [section.header.sizeHint().height() for section in self.sections]
        total = max(sum(self.splitter.sizes()), self.height())
        folded = sum(height for section, height in zip(self.sections, heights)
                     if not section.is_open)
        opened = [section for section in self.sections if section.is_open]
        share = max(0, total - folded) // max(1, len(opened))
        self.splitter.setSizes([share if section.is_open else height
                                for section, height in zip(self.sections, heights)])

    # --- Reading -----------------------------------------------------------------
    @property
    def live(self):
        return self._captured is None

    def refresh(self):
        if self.live and not self._pending:
            self._pending = True
            self._request(self.target, self)

    def _set_window_title(self, type_name):
        # e.g. "Objekt-Inspektor - attacker (UnityEngine.GameObject) (1)"
        self.setWindowTitle(self.translations["inspector.window_title"].format(
            name=self.name or "?", type=type_name, depth=self.depth))

    def _toggle_live(self, live):
        if live:
            self._timer.start()
        else:
            self._timer.stop()

    def open_row(self, row):
        """Open the object in field ``row`` in a dialog of its own."""
        item = self.table.item(row, _FIELD)
        target = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if target:
            self._open_object(target, self, item.text())

    # --- Address list ------------------------------------------------------------
    def _field_location(self, row):
        """``(address, value type or None)`` of the field in ``row``; address None if unknown."""
        item = self.table.item(row, _OFFSET) if row >= 0 else None
        location = item.data(_LOCATION_ROLE) if item is not None else None
        if not location or not location.get("address"):
            return None, None
        return int(location["address"], 16), location.get("valueType")

    def address_entry(self, row):
        """What ``address_requested`` sends for the field in ``row``, or None if no number."""
        address, value_type = self._field_location(row)
        if address is None or value_type is None:
            return None
        # e.g. "CharacterStats.BaseMight"
        owner = self._type_name.rsplit(".", 1)[-1]
        field = self.table.item(row, _FIELD).text()
        if self._type_name.startswith(_CONTAINERS) and self.name:
            # an entry of a TArray, TMap or TSet: "Inventory[3]", "Spread[Might (…)]"
            label = self.name + (field if field.startswith("[") else f"[{field}]")
        elif owner == "?":
            label = field
        elif field.startswith("[") and owner.endswith("[]"):
            label = owner[:-2] + field  # an element, e.g. "Int32[3]"
        else:
            label = f"{owner}.{field}"
        return {"address": address, "value_type": value_type, "label": label}

    def add_address(self, row):
        """Ask to put the field in ``row`` on the address list."""
        entry = self.address_entry(row)
        if entry is not None:
            self.address_requested.emit(entry)
            self.status_label.setText(self.translations["inspector.address_added"].format(
                label=entry["label"], address=f"0x{entry['address']:X}",
                type=entry["value_type"]))

    def _show_field_menu(self, position):
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        address, _value_type = self._field_location(row)
        menu = QMenu(self)
        add = menu.addAction(self.translations["inspector.add_address"])
        add.setEnabled(self.address_entry(row) is not None)
        add.triggered.connect(lambda: self.add_address(row))
        copy = menu.addAction(self.translations["inspector.copy_address"])
        copy.setEnabled(address is not None)
        copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(f"0x{address:X}"))
        open_object = menu.addAction(self.translations["inspector.open"])
        item = self.table.item(row, _FIELD)
        open_object.setEnabled(bool(item and item.data(Qt.ItemDataRole.UserRole)))
        open_object.triggered.connect(lambda: self.open_row(row))
        add_copy_actions(menu, self.table, position, self.translations)
        menu.exec(self.table.viewport().mapToGlobal(position))

    # --- Answers -----------------------------------------------------------------
    def show_contents(self, contents):
        self._pending = False
        if "error" in contents:
            self.show_error(messages.describe(contents["error"]))
            return
        fields = contents.get("fields", [])
        class_name = contents.get("name", "?")
        address = contents.get("address", "")
        instance_name = contents.get("instanceName")
        title = (f"{class_name} {json.dumps(instance_name, ensure_ascii=False)}@{address}"
                 if instance_name is not None else f"{class_name} @ {address}")
        if "length" in contents:
            title += "  ·  " + self.translations["inspector.length"].format(
                length=contents["length"])
        if not self.live:
            title += "  ·  " + self.translations["inspector.captured"].format(
                time=self._captured)
        self.title_label.setText(title)
        known = self._type_name != "?"
        self._type_name = contents.get("name", "?")
        self._set_window_title(self._type_name)
        selected = self.table.currentRow()
        self.table.setRowCount(len(fields))
        for row, field in enumerate(fields):
            value = field.get("value", {})
            static = field.get("static", False)
            # A static field without a place to read it from: a constant, a
            # thread static, or the class's static data is not known.
            unread = static and not field.get("address") and not value.get("text")
            target = None if unread else value.get("target")
            if unread:
                text = self.translations[_STATIC_KINDS.get(field.get("storage"), "inspector.static")]
            else:
                text = messages.describe(value.get("text", ""))
            texts = (
                field.get("name", ""),
                field.get("type", ""),
                text,
                f"0x{field.get('offset', 0):x}",
            )
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                if static:
                    item.setForeground(_STATIC)
                self.table.setItem(row, column, item)
            self.table.item(row, _FIELD).setData(Qt.ItemDataRole.UserRole, target)
            self.table.item(row, _OFFSET).setData(_LOCATION_ROLE, {
                "address": field.get("address"), "valueType": field.get("valueType")})
            if field.get("address"):
                self.table.item(row, _OFFSET).setToolTip(
                    self.translations["inspector.static_offset"].format(address=field["address"])
                    if static else field["address"])
            self.table.removeCellWidget(row, _OPEN)
            if target:
                self.table.setCellWidget(row, _OPEN, link_label(
                    self.translations["inspector.open"], lambda row=row: self.open_row(row)))
            elif not unread and self._shows_text(value):
                self.table.setCellWidget(row, _OPEN, self._text_link(value, field.get("name", "")))
        if 0 <= selected < len(fields):
            self.table.selectRow(selected)
        self.add_address_button.setEnabled(self.address_entry(self.table.currentRow()) is not None)

        self._show_property_names(contents.get("properties", []))
        if not known:
            self._filter_methods()  # the own class is known now

    def show_error(self, message):
        self._pending = False
        self.status_label.setText(self.translations["inspector.failed"].format(message=message))

    def _restore_hint(self):
        self.status_label.setText(self.translations[
            "inspector.hint" if self.live else "inspector.hint_captured"])

    # --- Properties --------------------------------------------------------------
    def _show_property_names(self, properties):
        names = [prop["name"] for prop in properties]
        if names == self._property_names():
            return
        self.properties_table.setRowCount(len(properties))
        for row, prop in enumerate(properties):
            name = QTableWidgetItem(prop["name"])
            name.setData(Qt.ItemDataRole.UserRole, prop)
            kind = QTableWidgetItem(prop["type"] + (
                f"  {self.translations['inspector.static']}" if prop.get("static") else ""))
            self.properties_table.setItem(row, _P_NAME, name)
            self.properties_table.setItem(row, _P_TYPE, kind)
            self.properties_table.setItem(row, _P_VALUE, QTableWidgetItem(""))
            self._show_property(row, self._property_values.get(prop["name"]))
            self.properties_table.removeCellWidget(row, _P_SET)
            if prop.get("setter") and not prop.get("static") and self._can_call():
                link = link_label(self.translations["inspector.set"],
                                  lambda prop=prop: self.set_property(prop))
                link.setToolTip(self.translations["inspector.set_tooltip"])
                self.properties_table.setCellWidget(row, _P_SET, link)
        self.evaluate_button.setEnabled(bool(properties) and self._can_evaluate())

    def _property_names(self):
        return [self.properties_table.item(row, _P_NAME).text()
                for row in range(self.properties_table.rowCount())]

    def _can_evaluate(self):
        return self._evaluate is not None and self.target is not None

    def _show_property(self, row, value):
        """Show an evaluated value (or a link to evaluate it) in ``row``."""
        name = self.properties_table.item(row, _P_NAME).text()
        item = self.properties_table.item(row, _P_VALUE)
        self.properties_table.removeCellWidget(row, _P_LINK)
        if value is None:
            item.setText("")
            if self._can_evaluate():
                self.properties_table.setCellWidget(row, _P_LINK, link_label(
                    self.translations["inspector.evaluate"],
                    lambda name=name: self.evaluate_properties([name])))
            return
        text = messages.describe(value.get("text", ""))
        item.setText(text)
        item.setToolTip(text)
        item.setForeground(_ERROR if value.get("error") else self.properties_table.palette().text())
        if value.get("target"):
            self.properties_table.setCellWidget(row, _P_LINK, link_label(
                self.translations["inspector.open"],
                lambda target=value["target"]: self._open_object(target, self, name)))
        elif self._shows_text(value):
            self.properties_table.setCellWidget(row, _P_LINK, self._text_link(value, name))

    def _shows_text(self, value):
        """Whether ``value`` is a string the text link can show whole."""
        return self._show_text is not None and bool(value.get("string"))

    def _text_link(self, value, name):
        """A link that shows the string ``value`` (of field or property ``name``) whole."""
        link = link_label(self.translations["inspector.text"], lambda: self._show_text(
            value, name, self._captured if not self.live
            else datetime.now().strftime("%H:%M:%S")))
        link.setToolTip(self.translations["inspector.text_tooltip"])
        return link

    def evaluate_properties(self, names):
        """Evaluate the named properties, or all of them for ``None``."""
        if not self._can_evaluate():
            return
        names = self._property_names() if names is None else names
        self.status_label.setText(self.translations["inspector.evaluating"])
        self._evaluate(self.target, names, self)

    def show_properties(self, results):
        rows = {name: row for row, name in enumerate(self._property_names())}
        for result in results:
            self._property_values[result["name"]] = result["value"]
            if result["name"] in rows:
                self._show_property(rows[result["name"]], result["value"])
        self._restore_hint()

    def show_property_error(self, message):
        self.status_label.setText(self.translations["inspector.evaluate_failed"].format(
            message=message))

    def set_property(self, prop):
        """Offer to call the setter of the property ``prop`` (an entry of the contents)."""
        self._setting = prop["name"]
        self._call_method(self.target, {
            "name": f"set_{prop['name']}", "method": prop["setter"], "owner": self._type_name,
            "params": f"{prop['type']} value", "returns": "System.Void",
        }, self, self.title_label.text())

    # --- Methods -----------------------------------------------------------------
    def _can_call(self):
        return self._call_method is not None and self.target is not None

    def _load_methods(self):
        """List the object's methods once their section is open."""
        if (self._methods is not None or self._methods_pending or not self.methods_section.is_open
                or self._list_methods is None or self.target is None):
            return
        self._methods_pending = True
        self.status_label.setText(self.translations["inspector.methods_loading"])
        self._list_methods(self.target, self)

    def show_methods(self, methods):
        self._methods_pending = False
        self._methods = methods
        self.methods_table.setRowCount(len(methods))
        for row, entry in enumerate(methods):
            texts = (entry["name"], entry["params"], entry["returns"], entry["owner"])
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.methods_table.setItem(row, column, item)
            self.methods_table.item(row, _M_NAME).setData(Qt.ItemDataRole.UserRole, entry)
            if self._can_call():
                self.methods_table.setCellWidget(row, _M_LINK, link_label(
                    self.translations["inspector.call"], lambda row=row: self._call_row(row)))
        self._filter_methods()
        self._restore_hint()

    def show_methods_error(self, message):
        self._methods_pending = False
        self.status_label.setText(self.translations["inspector.methods_failed"].format(
            message=message))

    def _filter_methods(self):
        """Show the methods matching the search, of the own class unless base classes are on."""
        text = self.method_filter.text().strip().lower()
        own_only = not self.base_classes_check.isChecked() and self._type_name != "?"
        for row in range(self.methods_table.rowCount()):
            entry = self.methods_table.item(row, _M_NAME).data(Qt.ItemDataRole.UserRole)
            hidden = ((own_only and entry["owner"] != self._type_name)
                      or (text and text not in entry["name"].lower()))
            self.methods_table.setRowHidden(row, bool(hidden))

    def _call_row(self, row):
        item = self.methods_table.item(row, _M_NAME)
        if item is None or not self._can_call():
            return
        self._setting = None
        self._call_method(self.target, item.data(Qt.ItemDataRole.UserRole), self,
                          self.title_label.text())

    def on_call_done(self):
        """A method called from here returned: show what it changed."""
        self.refresh()
        if self._setting is not None:
            self.evaluate_properties([self._setting])

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._timer.stop()
        super().closeEvent(event)
