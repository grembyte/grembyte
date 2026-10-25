"""Object inspector: the fields of a managed object in a Mono game."""

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
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hermes.ui.widgets.labels import SelectableText
from hermes.ui.widgets.table import resizable_columns, set_row_height


LIVE_INTERVAL_MS = 500

_FIELD, _TYPE, _VALUE, _OFFSET, _OPEN = range(5)
_P_NAME, _P_TYPE, _P_VALUE, _P_LINK = range(4)
# Data role of the offset cell: the field's {address, valueType} (see monitor.js)
_LOCATION_ROLE = Qt.ItemDataRole.UserRole + 1
_STATIC = QColor("#8b949e")
_ERROR = QColor("#f85149")
# Qt's default link blue is unreadable on the dark theme.
LINK_COLOR = "#58a6ff"


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
    values come back through ``show_properties``.

    A field holding a number can be put on the address list, to watch,
    change or freeze its value there. A string (field or property) gets a
    link that shows it whole: ``show_text(value, name, time)``.
    """

    # pylint: disable=too-many-instance-attributes,too-many-arguments

    # {"address": int, "value_type": str, "label": str} to add to the address list
    address_requested = pyqtSignal(object)

    def __init__(self, translations, open_object, target=None, request=None,
                 contents=None, captured=None, evaluate=None, name="", depth=1, parent=None,
                 show_text=None):
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
        self._captured = captured
        self.name = name
        self.depth = depth
        self._pending = False
        # property name -> value, once evaluated
        self._property_values = {}
        self._type_name = "?"
        self._set_window_title("?")
        self.resize(860, 680)
        self._build_ui(live=contents is None)
        self._timer = QTimer(self)
        self._timer.setInterval(LIVE_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        if contents is not None:
            self.show_contents(contents)
        else:
            self.title_label.setText(target["address"])
            self.refresh()

    def _build_ui(self, live):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.title_label = SelectableText()
        bar.addWidget(self.title_label, 1)
        self.live_check = QCheckBox(self.translations["inspector.live"])
        self.live_check.setToolTip(self.translations["inspector.live_tooltip"])
        self.live_check.toggled.connect(self._toggle_live)
        self.refresh_button = QPushButton(self.translations["inspector.refresh"])
        self.refresh_button.clicked.connect(self.refresh)
        self.add_address_button = QPushButton(self.translations["inspector.add_address"])
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
        # Fixed: the size of cell widgets does not count for ResizeToContents.
        header.setSectionResizeMode(_OPEN, QHeaderView.ResizeMode.Fixed)
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

        properties = QWidget()
        properties_layout = QVBoxLayout(properties)
        properties_layout.setContentsMargins(0, 0, 0, 0)
        properties_bar = QHBoxLayout()
        properties_label = QLabel(self.translations["inspector.properties"])
        properties_label.setToolTip(self.translations["inspector.properties_tooltip"])
        properties_bar.addWidget(properties_label)
        properties_bar.addStretch()
        self.evaluate_button = QPushButton(self.translations["inspector.evaluate_all"])
        self.evaluate_button.setToolTip(self.translations["inspector.properties_tooltip"])
        self.evaluate_button.clicked.connect(lambda: self.evaluate_properties(None))
        self.evaluate_button.setEnabled(False)
        properties_bar.addWidget(self.evaluate_button)
        properties_layout.addLayout(properties_bar)
        self.properties_table = QTableWidget(0, 4)
        self.properties_table.setHorizontalHeaderLabels([
            self.translations["inspector.column_property"],
            self.translations["inspector.column_type"],
            self.translations["inspector.column_value"],
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
        properties_header.setSectionResizeMode(_P_LINK, QHeaderView.ResizeMode.Fixed)
        properties_header.resizeSection(_P_TYPE, 200)
        properties_header.resizeSection(_P_LINK, link_width(self.properties_table, [
            self.translations["inspector.open"], self.translations["inspector.evaluate"],
            self.translations["inspector.text"]]))
        resizable_columns(self.properties_table)
        properties_layout.addWidget(self.properties_table, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(properties)
        splitter.setSizes([250, 350])
        layout.addWidget(splitter, 1)

        self.status_label = QLabel(self.translations[
            "inspector.hint" if live else "inspector.hint_captured"])
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

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
        if owner == "?":
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
        menu.exec(self.table.viewport().mapToGlobal(position))

    # --- Answers -----------------------------------------------------------------
    def show_contents(self, contents):
        self._pending = False
        if "error" in contents:
            self.show_error(contents["error"])
            return
        fields = contents.get("fields", [])
        title = f"{contents.get('name', '?')} @ {contents.get('address', '')}"
        if "length" in contents:
            title += "  ·  " + self.translations["inspector.length"].format(
                length=contents["length"])
        if not self.live:
            title += "  ·  " + self.translations["inspector.captured"].format(
                time=self._captured)
        self.title_label.setText(title)
        self._type_name = contents.get("name", "?")
        self._set_window_title(self._type_name)
        selected = self.table.currentRow()
        self.table.setRowCount(len(fields))
        for row, field in enumerate(fields):
            value = field.get("value", {})
            static = field.get("static", False)
            target = None if static else value.get("target")
            texts = (
                field.get("name", ""),
                field.get("type", ""),
                self.translations["inspector.static"] if static else value.get("text", ""),
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
                self.table.item(row, _OFFSET).setToolTip(field["address"])
            self.table.removeCellWidget(row, _OPEN)
            if target:
                self.table.setCellWidget(row, _OPEN, link_label(
                    self.translations["inspector.open"], lambda row=row: self.open_row(row)))
            elif not static and self._shows_text(value):
                self.table.setCellWidget(row, _OPEN, self._text_link(value, field.get("name", "")))
        if 0 <= selected < len(fields):
            self.table.selectRow(selected)
        self.add_address_button.setEnabled(self.address_entry(self.table.currentRow()) is not None)

        self._show_property_names(contents.get("properties", []))

    def show_error(self, message):
        self._pending = False
        self.status_label.setText(self.translations["inspector.failed"].format(message=message))

    # --- Properties --------------------------------------------------------------
    def _show_property_names(self, properties):
        names = [prop["name"] for prop in properties]
        if names == self._property_names():
            return
        self.properties_table.setRowCount(len(properties))
        for row, prop in enumerate(properties):
            name = QTableWidgetItem(prop["name"])
            kind = QTableWidgetItem(prop["type"] + (
                f"  {self.translations['inspector.static']}" if prop.get("static") else ""))
            self.properties_table.setItem(row, _P_NAME, name)
            self.properties_table.setItem(row, _P_TYPE, kind)
            self.properties_table.setItem(row, _P_VALUE, QTableWidgetItem(""))
            self._show_property(row, self._property_values.get(prop["name"]))
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
        item.setText(value.get("text", ""))
        item.setToolTip(value.get("text", ""))
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
        self.status_label.setText(self.translations[
            "inspector.hint" if self.live else "inspector.hint_captured"])

    def show_property_error(self, message):
        self.status_label.setText(self.translations["inspector.evaluate_failed"].format(
            message=message))

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._timer.stop()
        super().closeEvent(event)
