"""A window for a long text, e.g. a string a monitored method returned.

JSON (an object or an array) is shown as a tree and formatted; any text can
be copied or saved.
"""

import json

from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QFontDatabase, QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hermes.ui.widgets.icons import set_button_icon

# Characters of a value the tree shows in one line
_VALUE_PREVIEW = 200
# Item data: the JSON value of a tree item
_VALUE = Qt.ItemDataRole.UserRole


def parse_json(text):
    """The object or array ``text`` holds as JSON, else None."""
    if text.lstrip()[:1] not in ("{", "["):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def summary(value):
    """One line for a JSON value: ``{3}``, ``[12]`` or the value itself."""
    if isinstance(value, dict):
        return f"{{{len(value)}}}"
    if isinstance(value, list):
        return f"[{len(value)}]"
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _VALUE_PREVIEW else text[:_VALUE_PREVIEW] + "…"


def _children(value):
    if isinstance(value, dict):
        return list(value.items())
    if isinstance(value, list):
        return [(f"[{index}]", item) for index, item in enumerate(value)]
    return []


class TextViewerDialog(QDialog):
    """Show a text that is still coming (``set_text``) or known (``text``).

    ``length`` is the length of the original text, when the text is only
    its start.
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, translations, title, text=None, length=None, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        self.text = None
        self.data = None
        self.setWindowTitle(title)
        self.resize(900, 640)
        self._build_ui()
        if text is not None:
            self.set_text(text, length)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.info_label = QLabel(self.translations["text_viewer.loading"])
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.tabs = QTabWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([self.translations["text_viewer.column_key"],
                                   self.translations["text_viewer.column_value"]])
        self.tree.setColumnWidth(0, 280)
        self.tree.itemExpanded.connect(self._fill)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.plain = QPlainTextEdit()
        self.plain.setReadOnly(True)
        self.plain.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.tabs.addTab(self.plain, self.translations["text_viewer.tab_text"])
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.copy_button = QPushButton(self.translations["text_viewer.copy"])
        set_button_icon(self.copy_button, "content_copy")
        self.copy_button.setToolTip(self.translations["text_viewer.copy_tooltip"])
        self.copy_button.clicked.connect(self._copy)
        self.save_button = QPushButton(self.translations["text_viewer.save"])
        set_button_icon(self.save_button, "save")
        self.save_button.clicked.connect(self._save)
        close_button = QPushButton(self.translations["text_viewer.close"])
        set_button_icon(close_button, "close")
        close_button.clicked.connect(self.close)
        for button in (self.copy_button, self.save_button, close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._enable(False)

    def _enable(self, enabled):
        self.copy_button.setEnabled(enabled)
        self.save_button.setEnabled(enabled)

    # --- Content -------------------------------------------------------------
    def set_text(self, text, length=None):
        """Show ``text``; ``length`` of the original if ``text`` is only its start."""
        self.text = text
        self.data = parse_json(text)
        info = self.translations["text_viewer.characters"].format(count=len(text))
        if length is not None and length > len(text):
            info = self.translations["text_viewer.cut"].format(count=len(text), length=length)
        if self.data is not None:
            info += "  ·  " + self.translations["text_viewer.json"]
            self.plain.setPlainText(json.dumps(self.data, indent=2, ensure_ascii=False))
            self.tree.clear()
            self._add_children(self.tree.invisibleRootItem(), self.data)
            self.tabs.insertTab(0, self.tree, self.translations["text_viewer.tab_tree"])
            self.tabs.setCurrentIndex(0)
        else:
            self.plain.setPlainText(text)
        self.info_label.setText(info)
        self._enable(True)

    def show_missing(self):
        """The text is no longer kept (see MethodMonitorWorker.full_text)."""
        self.info_label.setText(self.translations["text_viewer.missing"])

    def show_error(self, message):
        self.info_label.setText(self.translations["text_viewer.failed"].format(message=message))

    def _add_children(self, parent, value):
        for key, child in _children(value):
            item = QTreeWidgetItem([str(key), summary(child)])
            item.setData(0, _VALUE, child)
            item.setToolTip(1, summary(child))
            if _children(child):
                # Filled when expanded: a save game has far too many nodes.
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            parent.addChild(item)

    def _fill(self, item):
        if item.childCount() == 0:
            self._add_children(item, item.data(0, _VALUE))

    # --- Actions -------------------------------------------------------------
    def _copy(self):
        """Copy the text as it came, or the selected node of the tree."""
        if self.tabs.currentWidget() is self.tree and self.tree.currentItem() is not None:
            self._copy_node(self.tree.currentItem())
        elif self.text is not None:
            QGuiApplication.clipboard().setText(self.text)

    @staticmethod
    def _copy_node(item):
        value = item.data(0, _VALUE)
        text = value if isinstance(value, str) else json.dumps(value, indent=2,
                                                                ensure_ascii=False)
        QGuiApplication.clipboard().setText(text)

    def _tree_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction(self.translations["text_viewer.copy_node"],
                       lambda: self._copy_node(item))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _save(self):
        if self.text is None:
            return
        suffix = "json" if self.data is not None else "txt"
        path, _filter = QFileDialog.getSaveFileName(
            self, self.translations["text_viewer.save"], f"text.{suffix}")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(self.text)
        except OSError as error:
            self.show_error(str(error))
