"""Memory viewer: a live hex dump of process memory."""

import struct
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QFont, QKeySequence, QShortcut  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from hermes.core.memory import describe_address, parse_address, read_memory
from hermes.platform import read_mappings
from hermes.core.value_types import VALUE_TYPES
from hermes.ui.widgets.icons import set_button_icon, theme_color, theme_qcolor

BYTES_PER_ROW = 16
REFRESH_MS = 500
# How long a changed byte stays highlighted.
CHANGE_HIGHLIGHT_S = 2.0
# Bytes read beyond the visible rows, so values at the end can be shown.
_TAIL = 8
# Rows shown until the view knows its size.
DEFAULT_ROWS = 16

# Theme colours (see icons.theme_color) of changed and unreadable bytes and of the text
_CHANGED = "orange"
_UNREADABLE = "disabled_text"
_ASCII = "muted"


class MemoryModel(QAbstractTableModel):
    """Rows of 16 bytes from ``base`` on: 16 hex columns and one ASCII column."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.base = 0
        self.rows = 0
        self.data_bytes = b""
        self.readable = bytearray()
        self._changed_at = {}

    # --- Qt model --------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else self.rows

    def columnCount(self, parent=QModelIndex()):  # pylint: disable=invalid-name
        return 0 if parent.isValid() else BYTES_PER_ROW + 1

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # pylint: disable=invalid-name
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return f"{section:X}" if section < BYTES_PER_ROW else "ASCII"
        return f"{self.base + section * BYTES_PER_ROW:016X}"

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, column = index.row(), index.column()
        if column == BYTES_PER_ROW:
            if role == Qt.ItemDataRole.DisplayRole:
                return self._ascii(row)
            if role == Qt.ItemDataRole.ForegroundRole:
                return theme_qcolor(_ASCII)
            return None
        offset = row * BYTES_PER_ROW + column
        if offset >= len(self.data_bytes):
            return None
        readable = self.readable[offset]
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{self.data_bytes[offset]:02X}" if readable else "??"
        if role == Qt.ItemDataRole.ForegroundRole:
            if not readable:
                return theme_qcolor(_UNREADABLE)
            if self.changed(offset):
                return theme_qcolor(_CHANGED)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter
        return None

    # --- Content ---------------------------------------------------------
    def set_view(self, base, rows):
        """Show ``rows`` rows from ``base`` on (contents follow with ``update``)."""
        if base != self.base:
            self._changed_at.clear()
        self.beginResetModel()
        self.base = base
        self.rows = rows
        size = rows * BYTES_PER_ROW + _TAIL
        self.data_bytes = bytes(size)
        self.readable = bytearray(size)
        self.endResetModel()

    def update(self, data, readable, now=None):
        """Set freshly read bytes and remember which of them changed."""
        now = time.monotonic() if now is None else now
        old, old_readable = self.data_bytes, self.readable
        if len(old) == len(data):
            for offset, (before, after) in enumerate(zip(old, data)):
                if before != after and readable[offset] and old_readable[offset]:
                    self._changed_at[offset] = now
        self.data_bytes, self.readable = data, readable
        if self.rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(self.rows - 1, BYTES_PER_ROW))

    def changed(self, offset, now=None):
        """Whether the byte at ``offset`` changed a moment ago."""
        changed_at = self._changed_at.get(offset)
        now = time.monotonic() if now is None else now
        return changed_at is not None and now - changed_at < CHANGE_HIGHLIGHT_S

    def bytes_at(self, offset, size):
        """Return ``size`` readable bytes at ``offset``, or None."""
        end = offset + size
        if offset < 0 or end > len(self.data_bytes) or not all(self.readable[offset:end]):
            return None
        return self.data_bytes[offset:end]

    def _ascii(self, row):
        start = row * BYTES_PER_ROW
        return "".join(
            chr(byte) if self.readable[start + i] and 0x20 <= byte < 0x7F else "."
            for i, byte in enumerate(self.data_bytes[start:start + BYTES_PER_ROW])
        )


class MemoryView(QTableView):
    """Hex table that scrolls through memory with the wheel and page keys."""

    def __init__(self, dialog):
        super().__init__(dialog)
        self._dialog = dialog

    def wheelEvent(self, event):  # pylint: disable=invalid-name
        steps = event.angleDelta().y() // 120
        if steps:
            self._dialog.scroll_rows(-3 * steps)
        event.accept()

    def keyPressEvent(self, event):  # pylint: disable=invalid-name
        rows = self.model().rowCount()
        moves = {
            Qt.Key.Key_PageDown: rows, Qt.Key.Key_PageUp: -rows,
        }
        index = self.currentIndex()
        if event.key() in moves:
            self._dialog.scroll_rows(moves[event.key()])
            return
        # Moving the cursor past the first or last row scrolls the memory.
        if event.key() == Qt.Key.Key_Down and index.row() == rows - 1:
            self._dialog.scroll_rows(1)
            return
        if event.key() == Qt.Key.Key_Up and index.row() == 0:
            self._dialog.scroll_rows(-1)
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):  # pylint: disable=invalid-name
        super().resizeEvent(event)
        self._dialog.fit_rows()


class MemoryViewerDialog(QDialog):
    """Live hex dump of a process; not modal, so the scanner stays usable.

    ``set_process`` switches to another process (or None while there is none);
    ``go_to`` shows an address.
    """

    # Widgets are created by _build_ui called from __init__.
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    def __init__(self, translations, pid, address=None, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.pid = pid
        self._mappings = []
        self.model = MemoryModel(self)
        self.setWindowTitle(translations["memview.title"])
        self.resize(860, 640)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._load_mappings()
        if address is not None:
            self.go_to(address)

    # --- UI ----------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel(self.translations["memview.address"]))
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText(self.translations["memview.address_placeholder"])
        self.address_input.returnPressed.connect(self._go_to_input)
        bar.addWidget(self.address_input, 1)
        go = QPushButton(self.translations["memview.go"])
        set_button_icon(go, "arrow_forward")
        go.clicked.connect(self._go_to_input)
        bar.addWidget(go)
        self.live = QCheckBox(self.translations["memview.live"])
        self.live.setToolTip(self.translations["memview.live_tooltip"])
        self.live.setChecked(True)
        self.live.toggled.connect(self._toggle_live)
        bar.addWidget(self.live)
        layout.addLayout(bar)
        QShortcut(QKeySequence("Ctrl+G"), self, self.address_input.setFocus)

        self.view = MemoryView(self)
        self.view.setModel(self.model)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.view.setFont(font)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setShowGrid(False)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.verticalHeader().setFont(font)
        self.view.verticalHeader().setDefaultSectionSize(self.view.fontMetrics().height() + 6)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.view.setTextElideMode(Qt.TextElideMode.ElideNone)
        metrics = self.view.fontMetrics()
        header = self.view.horizontalHeader()
        header.setStretchLastSection(False)
        # The byte columns are fixed: no lines that suggest resizing them.
        header.setStyleSheet("QHeaderView::section { border-right: none; }")
        for column in range(BYTES_PER_ROW + 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(column, metrics.horizontalAdvance("00") + 12)
        header.resizeSection(BYTES_PER_ROW, metrics.horizontalAdvance("W" * BYTES_PER_ROW) + 24)
        self.view.selectionModel().currentChanged.connect(self._show_selection)
        layout.addWidget(self.view, 1)

        self.region_label = QLabel("")
        self.region_label.setStyleSheet(f"color: {theme_color('muted')};")
        layout.addWidget(self.region_label)

        # Values at the selected byte.
        values = QFormLayout()
        self.value_labels = {}
        for name in VALUE_TYPES:
            label = QLabel("—")
            label.setFont(font)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.value_labels[name] = label
        row = QHBoxLayout()
        left, right = QFormLayout(), QFormLayout()
        for name, label in self.value_labels.items():
            # Signed integers and float on the left, unsigned and double on the right.
            (right if name.startswith("uint") or name == "double" else left).addRow(name, label)
        row.addLayout(left)
        row.addLayout(right)
        values.addRow(row)
        self.selection_label = QLabel(self.translations["memview.no_selection"])
        values.addRow(self.selection_label)
        layout.addLayout(values)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    # --- Navigation --------------------------------------------------------
    def go_to(self, address):
        """Show ``address`` in the first row and select its byte."""
        address = max(0, address)
        base = address - address % BYTES_PER_ROW
        self._set_base(base)
        self.address_input.setText(f"0x{address:X}")
        self.view.setCurrentIndex(self.model.index(0, address - base))

    def scroll_rows(self, rows):
        """Move the view by ``rows`` rows (negative: towards lower addresses)."""
        index = self.view.currentIndex()
        self._set_base(max(0, self.model.base + rows * BYTES_PER_ROW))
        if index.isValid():
            self.view.setCurrentIndex(self.model.index(index.row(), index.column()))

    def fit_rows(self):
        """Show as many rows as fit into the view."""
        height = self.view.viewport().height()
        row_height = self.view.verticalHeader().defaultSectionSize()
        rows = max(1, height // max(1, row_height))
        if rows != self.model.rows:
            self._set_base(self.model.base, rows)

    def _set_base(self, base, rows=None):
        current = self.view.currentIndex()
        self.model.set_view(base, rows if rows is not None else self.model.rows or DEFAULT_ROWS)
        self.refresh()
        if current.isValid() and current.row() < self.model.rows:
            self.view.setCurrentIndex(self.model.index(current.row(), current.column()))
        self._show_region()

    def _go_to_input(self):
        if self._mappings == [] and self.pid is not None:
            self._load_mappings()
        try:
            address = parse_address(self.address_input.text(), self._mappings)
        except ValueError as error:
            self.status_label.setText(str(error))
            return
        self.status_label.setText("")
        self.go_to(address)

    # --- Process -----------------------------------------------------------
    def set_process(self, pid):
        """Show another process' memory, or nothing while ``pid`` is None."""
        self.pid = pid
        self._mappings = []
        if pid is None:
            self.status_label.setText(self.translations["memview.process_exited"])
        else:
            self.status_label.setText("")
            self._load_mappings()
        self.refresh()

    def _load_mappings(self):
        if self.pid is None:
            return
        try:
            self._mappings = read_mappings(self.pid)
        except OSError:
            self._mappings = []

    def _toggle_live(self, live):
        if live:
            self._timer.start()
            self.refresh()
        else:
            self._timer.stop()

    def refresh(self):
        """Read the visible memory again."""
        size = self.model.rows * BYTES_PER_ROW + _TAIL
        if self.pid is None or not self.model.rows:
            self.model.update(bytes(size), bytearray(size))
            return
        try:
            data, readable = read_memory(self.pid, self.model.base, size)
        except OSError as error:
            self.status_label.setText(
                self.translations["memview.read_failed"].format(message=error)
            )
            data, readable = bytes(size), bytearray(size)
        self.model.update(data, readable)
        self._show_selection(self.view.currentIndex())

    # --- Details -----------------------------------------------------------
    def selected_address(self):
        """Return the address of the selected byte, or None."""
        index = self.view.currentIndex()
        if not index.isValid() or index.column() >= BYTES_PER_ROW:
            return None
        return self.model.base + index.row() * BYTES_PER_ROW + index.column()

    def _show_region(self):
        where = describe_address(self._mappings, self.model.base)
        self.region_label.setText(
            self.translations["memview.region"].format(region=where) if where
            else self.translations["memview.unmapped"]
        )

    def _show_selection(self, index, _previous=None):
        if not index.isValid() or index.column() >= BYTES_PER_ROW:
            self.selection_label.setText(self.translations["memview.no_selection"])
            for label in self.value_labels.values():
                label.setText("—")
            return
        offset = index.row() * BYTES_PER_ROW + index.column()
        address = self.model.base + offset
        where = describe_address(self._mappings, address)
        self.selection_label.setText(
            self.translations["memview.selected"].format(
                address=f"0x{address:X}", region=where or "—"
            )
        )
        for name, label in self.value_labels.items():
            kind = VALUE_TYPES[name]
            data = self.model.bytes_at(offset, kind.size)
            if data is None:
                label.setText("??")
                continue
            value = struct.unpack(kind.struct_format, data)[0]
            if kind.is_integer:
                label.setText(str(value))
            else:
                label.setText(f"{value:.{7 if kind.size == 4 else 15}g}")

    def showEvent(self, event):  # pylint: disable=invalid-name
        super().showEvent(event)
        # All columns must fit; there is no horizontal scroll bar.
        width = (self.view.verticalHeader().width() + self.view.horizontalHeader().length()
                 + 2 * self.view.frameWidth() + 2)
        self.view.setMinimumWidth(width)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._timer.stop()
        super().closeEvent(event)
