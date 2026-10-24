"""Window that lists the calls of hooks in debug mode."""

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QColor, QFontDatabase  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from hermes.ui.widgets.table import resizable_columns, set_row_height


# Oldest entries are discarded beyond this many.
MAX_ENTRIES = 2000
# Pseudo hook id for console.log output that belongs to no hook.
CONSOLE = -1

_COLUMNS = (
    "debug.column_time",
    "debug.column_hook",
    "debug.column_thread",
    "debug.column_address",
    "debug.column_caller",
    "debug.column_old",
    "debug.column_new",
    "debug.column_result",
    "debug.column_note",
)
_COLORS = {"error": "#f85149", "skipped": "#8b949e", "kept": "#d29922", "applied": "#3fb950"}


def _number(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def entry_kind(entry):
    """Classify a debug entry: error, skipped, kept, applied or log."""
    if entry.get("error"):
        return "error"
    if entry.get("skipped"):
        return "skipped"
    if entry.get("level"):
        return "log"
    return "kept" if entry.get("kept") else "applied"


class HookDebugWindow(QDialog):
    """Live protocol of hook calls; closing it ends debug mode for all hooks."""

    # Widgets are created by _build_ui called from __init__.
    # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init

    closed = pyqtSignal()
    # From the context menu: put the address of an entry on the address list
    add_address_requested = pyqtSignal(object)

    def __init__(self, translations, parent=None):
        super().__init__(parent)
        self.translations = translations
        self.setWindowFlag(Qt.WindowType.Window)
        self.setWindowTitle(translations["debug.title"])
        self.resize(1100, 620)
        self._entries = []
        self._labels = {CONSOLE: translations["debug.console"]}
        self._dropped = 0
        self._ignored = 0
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self.btn_pause = QPushButton(self.translations["debug.pause"])
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause_toggled)
        controls.addWidget(self.btn_pause)
        clear = QPushButton(self.translations["debug.clear"])
        clear.clicked.connect(self.clear)
        controls.addWidget(clear)
        controls.addWidget(QLabel(self.translations["debug.filter"]))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem(self.translations["debug.filter_all"], None)
        self.filter_combo.currentIndexChanged.connect(self._render)
        controls.addWidget(self.filter_combo)
        self.count_label = QLabel()
        controls.addWidget(self.count_label, 1)
        layout.addLayout(controls)

        hint = QLabel(self.translations["debug.hint"])
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([self.translations[key] for key in _COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        set_row_height(self.table)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        resizable_columns(self.table)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        splitter.addWidget(self.table)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.details.setPlaceholderText(self.translations["debug.details_placeholder"])
        splitter.addWidget(self.details)
        splitter.setSizes([440, 160])
        layout.addWidget(splitter, 1)
        self._update_count()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_hooks(self, labels):
        """Set the hooks offered by the filter as ``{hook_id: label}``."""
        self._labels = {CONSOLE: self.translations["debug.console"], **labels}
        current = self.filter_combo.currentData()
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItem(self.translations["debug.filter_all"], None)
        for hook_id, label in labels.items():
            self.filter_combo.addItem(label, hook_id)
        self.filter_combo.addItem(self.translations["debug.console"], CONSOLE)
        self.filter_combo.setCurrentIndex(max(self.filter_combo.findData(current), 0))
        self.filter_combo.blockSignals(False)

    def add_events(self, events, dropped=None):
        """Append debug events sent by the hook agent."""
        self._dropped += sum((dropped or {}).values())
        if self.btn_pause.isChecked():
            self._ignored += len(events)
            self._update_count()
            return
        self._append(events)

    def add_console(self, level, text):
        """Append console.log output of the agent or a transform."""
        self.add_events([{
            "hookId": CONSOLE,
            "time": datetime.now().timestamp() * 1000,
            "level": level,
            "logs": [text],
        }])

    def context_menu(self, entry):
        """The context menu of a debug entry."""
        menu = QMenu(self)
        menu.addAction(
            self.translations["debug.add_address"],
            lambda: self.add_address_requested.emit(entry),
        ).setEnabled(bool(entry.get("address")) and entry.get("hookId") != CONSOLE)
        return menu

    def entries(self):
        """Return the stored debug entries, oldest first."""
        return list(self._entries)

    def clear(self):
        """Remove all entries."""
        self._entries.clear()
        self._dropped = 0
        self._ignored = 0
        self.table.setRowCount(0)
        self.details.clear()
        self._update_count()

    def closeEvent(self, a0):  # pylint: disable=invalid-name
        """Tell the hook panel to end debug mode."""
        self.closed.emit()
        super().closeEvent(a0)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _on_pause_toggled(self, paused):
        self.btn_pause.setText(
            self.translations["debug.resume" if paused else "debug.pause"]
        )

    def _matches(self, entry):
        selected = self.filter_combo.currentData()
        return selected is None or entry.get("hookId") == selected

    def _append(self, events):
        self._entries.extend(events)
        overflow = len(self._entries) - MAX_ENTRIES
        if overflow > 0:
            del self._entries[:overflow]
        scrollbar = self.table.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        if overflow > 0:
            self._render()
        else:
            for entry in events:
                if self._matches(entry):
                    self._add_row(entry)
        if at_bottom:
            self.table.scrollToBottom()
        self._update_count()

    def _render(self):
        self.table.setRowCount(0)
        for entry in self._entries:
            if self._matches(entry):
                self._add_row(entry)
        self.details.clear()
        self._update_count()

    def _note(self, entry):
        kind = entry_kind(entry)
        logs = entry.get("logs") or []
        if kind == "error":
            return self.translations["debug.note_error"].format(
                message=entry["error"].splitlines()[0]
            )
        if kind == "skipped":
            return self.translations[f"debug.skipped_{entry['skipped']}"]
        if kind == "log":
            return logs[0] if logs else ""
        note = self.translations[f"debug.note_{kind}"]
        if logs:
            note += "  ·  " + self.translations["debug.note_logs"].format(
                count=len(logs), first=logs[0]
            )
        return note

    def _add_row(self, entry):
        row = self.table.rowCount()
        self.table.insertRow(row)
        kind = entry_kind(entry)
        time = datetime.fromtimestamp(entry.get("time", 0) / 1000).strftime("%H:%M:%S.%f")[:-3]
        values = (
            time,
            self._labels.get(entry.get("hookId"), f"#{entry.get('hookId')}"),
            str(entry.get("threadId", "")),
            entry.get("address") or "",
            entry.get("caller") or "",
            _number(entry.get("old")),
            _number(entry.get("new")),
            _number(entry.get("result")) if kind == "applied" else "",
            self._note(entry),
        )
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, entry)
            if column == len(values) - 1 and kind in _COLORS:
                item.setForeground(QColor(_COLORS[kind]))
            self.table.setItem(row, column, item)

    def _show_context_menu(self, position):
        row = self.table.rowAt(position.y())
        item = self.table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        self.table.selectRow(row)
        self.context_menu(item.data(Qt.ItemDataRole.UserRole)).exec(
            self.table.viewport().mapToGlobal(position))

    def _show_details(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        if item is None:
            self.details.clear()
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        lines = [
            f"{self.translations['debug.column_hook']}: "
            f"{self._labels.get(entry.get('hookId'), entry.get('hookId'))}",
        ]
        for key, label in (
            ("address", "debug.column_address"),
            ("caller", "debug.column_caller"),
            ("old", "debug.column_old"),
            ("new", "debug.column_new"),
            ("result", "debug.column_result"),
        ):
            if entry.get(key) is not None:
                value = entry[key]
                text = _number(value) if isinstance(value, (int, float)) else str(value)
                lines.append(f"{self.translations[label]}: {text}")
        lines.append(self._note(entry))
        if entry.get("logs"):
            lines += ["", self.translations["debug.logs"]]
            lines += [f"  {line}" for line in entry["logs"]]
        if entry.get("error"):
            lines += ["", entry["error"]]
        self.details.setPlainText("\n".join(lines))

    def _update_count(self):
        text = self.translations["debug.count"].format(count=len(self._entries))
        if self._dropped:
            text += "  ·  " + self.translations["debug.dropped"].format(count=self._dropped)
        if self._ignored:
            text += "  ·  " + self.translations["debug.ignored"].format(count=self._ignored)
        self.count_label.setText(text)
