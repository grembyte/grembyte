"""Object browser: every live object of the game, by its class (Unreal).

The tree shows the classes with live objects on its first level, with how
many there are, and below a class its objects as ``"name"@address``, read
when the class is opened (games keep hundreds of thousands of objects). An
object opens in the object inspector. Above the tree: the package to focus
on (or the game's own ones), a filter on class and object names, and
singletons only.

The filter runs on both sides: a class whose name has all its words shows
with all its objects; the agent counts for every class how many objects are
named so, and a class with such objects shows with only those. A singleton
is an object of a class the engine has one of (``role``: GameInstance, a
subsystem, the game mode ...), or of a game class with exactly one object.

Requests go through the scripts tab's worker (``panel``, a ScriptPanel), as
the GAS window's; the agent reads the object table on its own thread.
"""

from PySide6.QtCore import Qt, QTimer  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hermes.ui.widgets.code_explorer import BoundedComboBox
from hermes.ui.widgets.icons import set_button_icon, theme_qcolor
from hermes.ui.widgets.labels import DocsHint

_NAME, _COUNT, _PACKAGE, _SINGLETON = range(4)
_COLUMNS = ("objects.column_name", "objects.column_count", "objects.column_package",
            "objects.column_singleton")
# Item data: the class entry (a class item) or the object entry (an object item)
_CLASS = Qt.ItemDataRole.UserRole
_OBJECT = Qt.ItemDataRole.UserRole + 1
# Of a class item: the filter its objects are listed with, whether they were asked for
_WORDS = Qt.ItemDataRole.UserRole + 2
_ASKED = Qt.ItemDataRole.UserRole + 3
# Package choices besides the packages themselves
ALL_PACKAGES = ""
GAME_PACKAGES = "\0game"
# How long the filter waits for more typing before it asks the agent
FILTER_DELAY_MS = 400
# Classes found only by their objects' names that open by themselves, at most
AUTO_EXPAND = 20


def filter_words(text):
    """The words of a filter, in lower case."""
    return text.lower().split()


def name_matches(entry, words):
    """Whether a class's name (or C++ name) has all ``words``."""
    names = f"{entry['name']} {entry.get('cppName', '')}".lower()
    return all(word in names for word in words)


def is_singleton(entry, is_framework):
    """Whether a class (an entry of browse_classes) has the one object of its
    kind: an engine singleton (``role``) or a game class with one object."""
    if entry["count"] != 1:
        return False
    return entry.get("role") is not None or not is_framework(entry.get("package", ""))


def object_text(entry, class_name="?"):
    """``"BP_Hero_C_0"@0x7f…``, as the scripts show objects; an object
    without a name (a plain C# object) as ``Game.Inventory@0x7f…``."""
    if not entry.get("name"):
        return f'{class_name}@{entry["address"]}'
    return f'{json_quote(entry["name"])}@{entry["address"]}'


def json_quote(text):
    """A name in quotes, one line (strings may hold line breaks)."""
    return '"' + text.replace("\n", " ").replace("\r", " ") + '"'


class ObjectBrowserDialog(QDialog):
    """The live objects of a game by class, to open in the object inspector.

    Not modal. ``is_framework(package)`` says whether a package belongs to
    the engine rather than the game (the adapter's is_framework_assembly).
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, translations, panel, is_framework=lambda _name: False, engine="unreal",
                 parent=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.setModal(False)
        self.translations = translations
        self.panel = panel
        self.is_framework = is_framework
        self.engine = engine
        self._worker = None
        self._pending = None  # request id of the classes asked for
        self._loading = {}  # request id -> klass whose objects were asked for
        self._classes = []
        self._answered_words = []  # the filter the agent counted ``matched`` for
        self._sent_words = []  # the filter of the classes asked for
        self._objects = 0
        self._items = {}  # klass -> its tree item
        self._stats = None  # Mono: how long the game stood still for the last read
        self._truncated = False
        self.setWindowTitle(translations["objects.title"])
        self.resize(900, 700)
        self._build_ui()
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DELAY_MS)
        self._filter_timer.timeout.connect(self._filter_typed)
        self.refresh()

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        mono = self.engine == "mono"
        layout.addWidget(DocsHint(
            t, t["objects.hint_mono" if mono else "objects.hint"],
            "engines/unity-mono/#object-browser" if mono else "engines/unreal/#object-browser"))

        top = QHBoxLayout()
        # Hundreds of packages: the list stays in the window, with a scroll bar
        self.package_combo = BoundedComboBox()
        self.package_combo.setToolTip(t["objects.package_tooltip"])
        self.package_combo.setMinimumWidth(220)
        self.package_combo.currentIndexChanged.connect(lambda _index: self._fill())
        top.addWidget(self.package_combo)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText(t["objects.filter_placeholder"])
        self.filter_input.setToolTip(t["objects.filter_tooltip"])
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._filter_changed)
        top.addWidget(self.filter_input, 1)
        layout.addLayout(top)

        options = QHBoxLayout()
        self.singleton_check = QCheckBox(t["objects.singletons"])
        self.singleton_check.setToolTip(t["objects.singletons_tooltip"])
        self.singleton_check.toggled.connect(lambda _checked: self._fill())
        options.addWidget(self.singleton_check)
        self.internal_check = QCheckBox(t["objects.internal"])
        self.internal_check.setToolTip(t["objects.internal_tooltip"])
        self.internal_check.toggled.connect(lambda _checked: self.refresh())
        options.addWidget(self.internal_check)
        # Mono's liveness calculation finds no templates or reflection objects
        self.internal_check.setVisible(not mono)
        options.addStretch()
        self.refresh_button = QPushButton(t["objects.refresh"])
        set_button_icon(self.refresh_button, "refresh")
        self.refresh_button.setToolTip(t["objects.refresh_tooltip"])
        self.refresh_button.clicked.connect(self.refresh)
        options.addWidget(self.refresh_button)
        layout.addLayout(options)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels([t[column] for column in _COLUMNS])
        self.tree.headerItem().setToolTip(_COUNT, t["objects.column_count_tooltip"])
        self.tree.headerItem().setToolTip(_SINGLETON, t["objects.singletons_tooltip"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(_NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in ((_COUNT, 90), (_PACKAGE, 220), (_SINGLETON, 130)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, width)
        header.setStretchLastSection(False)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemDoubleClicked.connect(lambda item, _column: self._inspect_item(item))
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)
        self.inspect_button = QPushButton(t["objects.inspect"])
        self.inspect_button.setObjectName("btn_primary")
        set_button_icon(self.inspect_button, "manage_search")
        self.inspect_button.setToolTip(t["objects.inspect_tooltip"])
        self.inspect_button.clicked.connect(lambda: self._inspect_item(self.tree.currentItem()))
        actions.addWidget(self.inspect_button)
        layout.addLayout(actions)
        self._update_buttons()

    # --- Requests ------------------------------------------------------------------
    def _current_worker(self):
        """The scripts tab's worker, its answers connected; None when detached."""
        worker = self.panel.worker()
        if worker is not None and worker is not self._worker:
            self._worker = worker
            worker.classes_browsed.connect(lambda request, answer: self._on_classes(
                worker, request, answer))
            worker.browse_failed.connect(lambda request, message: self._on_classes_failed(
                worker, request, message))
            worker.instances_browsed.connect(lambda request, answer: self._on_instances(
                worker, request, answer))
            worker.instances_failed.connect(lambda request, message: self._on_instances_failed(
                worker, request, message))
            self._pending = None
            self._loading.clear()
        return worker

    def refresh(self):
        """Read the object table anew (unless an answer is still due)."""
        if self._pending is not None:
            return
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["objects.detached"])
            return
        self._sent_words = filter_words(self.filter_input.text())
        self._pending = worker.browse_classes(" ".join(self._sent_words),
                                              self.internal_check.isChecked())
        self.status_label.setText(self.translations["objects.reading"])

    def _filter_changed(self, _text):
        # Class names at once; the objects' names when typing paused
        self._fill()
        self._filter_timer.start()

    def _filter_typed(self):
        if filter_words(self.filter_input.text()) != self._answered_words:
            if self._pending is not None:
                self._filter_timer.start()  # ask once the answer due is in
            else:
                self.refresh()

    def _load_instances(self, item):
        worker = self._current_worker()
        entry = item.data(0, _CLASS)
        if worker is None or entry is None:
            return
        request = worker.browse_instances(entry["klass"], item.data(0, _WORDS) or "",
                                          self.internal_check.isChecked())
        self._loading[request] = entry["klass"]

    # --- Answers -------------------------------------------------------------------
    def _on_classes(self, worker, request, answer):
        if worker is not self._worker or request != self._pending:
            return
        self._pending = None
        self._classes = (answer or {}).get("classes", [])
        self._objects = (answer or {}).get("objects", 0)
        self._stats = (answer or {}).get("stats")
        self._truncated = bool((answer or {}).get("truncated"))
        self._answered_words = self._sent_words
        self._fill_packages()
        self._fill()

    def _on_classes_failed(self, worker, request, message):
        if worker is not self._worker or request != self._pending:
            return
        self._pending = None
        self.status_label.setText(self.translations["objects.failed"].format(
            message=message.splitlines()[0] if message else message))

    def _on_instances(self, worker, request, answer):
        if worker is not self._worker:
            return
        klass = self._loading.pop(request, None)
        item = self._items.get(klass)
        if item is None:
            return
        item.takeChildren()
        class_name = (item.data(0, _CLASS) or {}).get("name", "?")
        for entry in (answer or {}).get("instances", []):
            child = QTreeWidgetItem([object_text(entry, class_name), "", "", ""])
            child.setData(0, _OBJECT, entry)
            if entry.get("outer"):
                child.setToolTip(_NAME, self.translations["objects.outer"].format(
                    outer=entry["outer"], name=entry["name"]))
            item.addChild(child)
        hidden = (answer or {}).get("count", 0) - item.childCount()
        if hidden > 0:
            more = QTreeWidgetItem([self.translations["objects.more"].format(count=hidden)])
            more.setFlags(Qt.ItemFlag.ItemIsEnabled)
            more.setForeground(_NAME, theme_qcolor("muted"))
            item.addChild(more)
        if item.childCount() == 0:
            none = QTreeWidgetItem([self.translations["objects.none_left"]])
            none.setFlags(Qt.ItemFlag.ItemIsEnabled)
            none.setForeground(_NAME, theme_qcolor("muted"))
            item.addChild(none)

    def _on_instances_failed(self, worker, request, message):
        if worker is not self._worker:
            return
        klass = self._loading.pop(request, None)
        item = self._items.get(klass)
        if item is None:
            return
        item.takeChildren()
        failed = QTreeWidgetItem([self.translations["objects.failed"].format(
            message=message.splitlines()[0] if message else message)])
        failed.setFlags(Qt.ItemFlag.ItemIsEnabled)
        failed.setForeground(_NAME, theme_qcolor("danger"))
        item.addChild(failed)
        # Read again when opened again
        item.setData(0, _ASKED, False)

    # --- Tree ----------------------------------------------------------------------
    def _fill_packages(self):
        """The packages of the classes read, with their numbers of objects."""
        t = self.translations
        counts = {}
        for entry in self._classes:
            counts[entry.get("package", "")] = counts.get(entry.get("package", ""), 0) \
                + entry["count"]
        game = sum(count for name, count in counts.items() if not self.is_framework(name))
        current = self.package_combo.currentData()
        self.package_combo.blockSignals(True)
        try:
            self.package_combo.clear()
            self.package_combo.addItem(t["objects.all_packages"].format(count=self._objects),
                                       ALL_PACKAGES)
            self.package_combo.addItem(t["objects.game_packages"].format(count=game),
                                       GAME_PACKAGES)
            for name in sorted(counts, key=str.lower):
                self.package_combo.addItem(f"{name or '?'} ({counts[name]})", name)
            index = self.package_combo.findData(current)
            self.package_combo.setCurrentIndex(max(index, 0))
        finally:
            self.package_combo.blockSignals(False)

    def _package_shown(self, package):
        wanted = self.package_combo.currentData()
        if wanted in (None, ALL_PACKAGES):
            return True
        if wanted == GAME_PACKAGES:
            return not self.is_framework(package)
        return package == wanted

    def shown_classes(self):
        """``[(class entry, object filter)]`` the tree shows: the filter its
        objects are listed with ("" for all of them)."""
        words = filter_words(self.filter_input.text())
        # The agent's counts are for the filter it was asked with
        counted = words == self._answered_words
        singletons = self.singleton_check.isChecked()
        shown = []
        for entry in self._classes:
            if not self._package_shown(entry.get("package", "")):
                continue
            if singletons and not is_singleton(entry, self.is_framework):
                continue
            if not words or name_matches(entry, words):
                shown.append((entry, ""))
            elif counted and entry.get("matched", 0) > 0:
                shown.append((entry, " ".join(words)))
        return shown

    def _fill(self):
        """Show the classes read last, as the package, filter and checkbox choose."""
        t = self.translations
        expanded = {klass for klass, item in self._items.items() if item.isExpanded()}
        selected = self._selected_key()
        self.tree.clear()
        self._items = {}
        self._loading.clear()
        shown = self.shown_classes()
        found_by_objects = [entry["klass"] for entry, words in shown if words]
        if len(found_by_objects) <= AUTO_EXPAND:
            expanded.update(found_by_objects)
        objects = 0
        reselect = None
        for entry, words in shown:
            count = entry.get("matched", 0) if words else entry["count"]
            objects += count
            role = entry.get("role")
            singleton = is_singleton(entry, self.is_framework)
            item = QTreeWidgetItem([
                entry["name"],
                f"{count} / {entry['count']}" if words else str(count),
                entry.get("package", ""),
                (role or t["objects.single_game_object"]) if singleton else ""])
            item.setData(0, _CLASS, entry)
            item.setData(0, _WORDS, words)
            item.setTextAlignment(_COUNT, Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(_NAME, entry.get("cppName") or entry["name"])
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            self.tree.addTopLevelItem(item)
            self._items[entry["klass"]] = item
            if selected == ("class", entry["klass"]):
                reselect = item
        for klass in expanded:
            item = self._items.get(klass)
            if item is not None:
                item.setExpanded(True)
        if reselect is not None:
            self.tree.setCurrentItem(reselect)
        if self._pending is None:
            status = t["objects.status"].format(classes=len(shown), objects=objects)
            if self._stats:
                status += " " + t["objects.stopped"].format(ms=self._stats.get("stoppedMs", 0))
            if self._truncated:
                status += " " + t["objects.truncated"]
            self.status_label.setText(status)
        self._update_buttons()

    def _expanded(self, item):
        entry = item.data(0, _CLASS)
        if entry is None or item.data(0, _ASKED):
            return
        item.setData(0, _ASKED, True)
        item.takeChildren()
        loading = QTreeWidgetItem([self.translations["objects.loading"]])
        loading.setFlags(Qt.ItemFlag.ItemIsEnabled)
        loading.setForeground(_NAME, theme_qcolor("muted"))
        item.addChild(loading)
        self._load_instances(item)

    def _selected_key(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        entry = item.data(0, _CLASS)
        return None if entry is None else ("class", entry["klass"])

    def selected_object(self):
        """The object entry ({address, name, outer}) selected, or None."""
        item = self.tree.currentItem()
        return None if item is None else item.data(0, _OBJECT)

    def _update_buttons(self):
        self.inspect_button.setEnabled(self.selected_object() is not None)

    def _inspect_item(self, item):
        """Open the object of ``item`` in the object inspector."""
        if item is None:
            return
        entry = item.data(0, _OBJECT)
        inspections = self.panel.inspections
        if entry is None or inspections is None or self.panel.worker() is not self._worker:
            return
        inspections.inspect({"address": entry["address"]}, name=entry["name"],
                            instance_name=entry["name"] or None)

    def _tree_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        t = self.translations
        menu = QMenu(self)
        entry = item.data(0, _OBJECT)
        klass = item.data(0, _CLASS)
        clipboard = QGuiApplication.clipboard()
        if entry is not None:
            menu.addAction(t["objects.inspect"], lambda: self._inspect_item(item))
            menu.addAction(t["objects.copy_address"],
                           lambda: clipboard.setText(entry["address"]))
            menu.addAction(t["objects.copy_name"], lambda: clipboard.setText(entry["name"]))
        elif klass is not None:
            menu.addAction(t["objects.copy_class"], lambda: clipboard.setText(klass["name"]))
            menu.addAction(t["objects.copy_class_address"],
                           lambda: clipboard.setText(klass["klass"]))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(position))
