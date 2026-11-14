"""Window of the reference graph: which objects hold an object, up to the roots."""

from dataclasses import dataclass
from datetime import datetime

import shiboken6  # pylint: disable=import-error,no-name-in-module
from PySide6.QtCore import (  # pylint: disable=import-error,no-name-in-module
    QPointF,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QBrush,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
)

from grembyte import __version__
from grembyte.core import settings
from grembyte.core.programs import program_of
from grembyte.features.object_references import (
    BY_USER,
    FILTERED,
    HOLDERS,
    MAX_PATH_STEPS,
    MODULE,
    OBJECT,
    STACK,
    STATIC,
    TARGETS,
    UNKNOWN,
    ReferenceGraph,
    ReferenceIndexWorker,
    static_fields,
)
from grembyte.ui.export_folder import remember, suggested_path
from grembyte.ui.localization import language_code
from grembyte.ui.widgets import graph_export
from grembyte.ui.widgets.busy import BusyDots
from grembyte.ui.widgets.docking import PinButton
from grembyte.ui.widgets.icons import set_button_icon, theme_color, theme_qcolor
from grembyte.ui.widgets.labels import DocsHint
from grembyte.ui.workers import connect_while_alive, keep_until_finished

# Layout of the graph, in scene pixels
NODE_WIDTH = 230
NODE_HEIGHT = 46
COLUMN_GAP = 150
ROW_GAP = 22
ZOOM_STEP = 1.15
# Locations named per request to the agent: the Wine gadget drops messages
# to the agent above 128 KiB (see docs/development/implementation-notes.md)
RESOLVE_BATCH = 2000
# Text of a node line at most, in characters
MAX_LINE = 34
# How a node or edge is marked: on the way to a root found by the search, or
# on the way from a node to the object the user asked to highlight; the
# theme colour of each
ROOT_PATH, MARKED_PATH = "root", "marked"
_MARK_COLORS = {ROOT_PATH: "orange", MARKED_PATH: "highlight"}
# The icons of the two ways: the holders stand left of the object, what it
# points to right of it
_DIRECTION_ICONS = {HOLDERS: "arrow_back", TARGETS: "arrow_forward"}


def find_reference_opener(widget):
    """The main window's ``open_reference_graph(address, class_name, name)``
    for ``widget``, or None where the game's engine cannot name the holders
    (asked when the window opens)."""
    node = widget
    while node is not None:
        opener = getattr(node, "open_reference_graph", None)
        if callable(opener):
            can = getattr(node, "can_open_reference_graph", None)
            return opener if can is None or can() else None
        node = node.parentWidget() if hasattr(node, "parentWidget") else None
    return None


def short_class(name):
    """A class name without its namespace (generic arguments kept)."""
    head, bracket, rest = (name or "?").partition("<")
    return head.rsplit(".", 1)[-1] + bracket + rest


def _clip(text, limit=MAX_LINE):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def node_lines(node, translations):
    """The two lines a node shows: what it is, and which one."""
    t = translations
    if node.kind == OBJECT:
        second = f'"{node.name}"' if node.name else f"0x{node.address:x}"
        return short_class(node.class_name), second
    if node.kind == STATIC:
        return t["references.static"], f"{short_class(node.class_name)}.{node.name}"
    if node.kind == MODULE:
        return t["references.module"], node.name or ""
    count = t["references.locations"].format(count=len(node.locations))
    if node.kind == STACK:
        return t["references.stack"], count
    return t["references.unknown"], count


# Texts of a node's tooltip per way of the graph: what a double click does,
# that it leads nowhere, how many of the locations are shown
_TOOLTIP_KEYS = {
    HOLDERS: ("references.expand_hint", "references.no_holders", "references.holders_count"),
    TARGETS: ("references.targets_hint", "references.no_targets", "references.targets_count"),
}


def node_tooltip(node, translations, hidden=0, hidden_user=0, hidden_filter=0,
                 direction=HOLDERS):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """The tooltip of a node: its details and what a double click does;
    ``hidden`` of the locations it leads to are not shown (other memory),
    ``hidden_user`` because the user hid them, ``hidden_filter`` because
    only singletons are shown. ``direction`` the way the graph grows."""
    t = translations
    expand_hint, none_found, count = _TOOLTIP_KEYS[direction]
    lines = []
    if node.kind == OBJECT:
        lines.append(node.class_name or "?")
        if node.name:
            lines.append(f'"{node.name}"')
        lines.append(f"0x{node.address:x}")
        if not node.expanded:
            lines.append(t[expand_hint])
        elif node.total == 0:
            lines.append(t[none_found])
            lines.append(t["references.collapse_hint"])
        else:
            lines.append(t[count].format(
                shown=node.shown - hidden - hidden_user - hidden_filter, total=node.total))
            if hidden:
                lines.append(t["references.hidden_other"].format(count=hidden))
            if hidden_user:
                lines.append(t["references.hidden_user"].format(count=hidden_user))
            if hidden_filter:
                lines.append(t["references.hidden_filter"].format(count=hidden_filter))
            lines.append(t["references.collapse_hint"])
    else:
        # The other way a static field is not a holder: its class's object reaches it
        lines.append(t["references.static_target_tooltip" if node.kind == STATIC
                       and direction == TARGETS else f"references.{node.kind}_tooltip"])
        lines.extend(f"0x{location:x}" for location in node.locations[:5])
        if len(node.locations) > 5:
            lines.append("…")
        if node.kind in (STATIC, MODULE):
            lines.append(f"0x{node.address:x}")
    return "\n".join(lines)


def layout_positions(graph, hidden=()):
    """``{key: QPointF}`` of the nodes' top left corners: the target on the
    right, each step to its holders a column further left (the other way,
    TARGETS: on the left, what it points to further right); in a column the
    nodes follow the order of the nodes they lead from, which keeps edges
    from crossing. Nodes of the kinds ``hidden`` are left out."""
    depth = graph.depths(hidden)
    side = -1 if graph.direction == HOLDERS else 1
    columns = {}
    for key, steps in depth.items():
        columns.setdefault(steps, []).append(key)
    row_of = {graph.target: 0.0}
    positions = {}
    for steps in sorted(columns):
        keys = columns[steps]
        if steps > 0:
            def place(key, steps=steps):
                rows = [row_of[other] for other in graph.leading(key)
                        if depth.get(other) == steps - 1]
                return (min(rows) if rows else 0.0, key)
            keys.sort(key=place)
        middle = (len(keys) - 1) / 2
        for row, key in enumerate(keys):
            row_of[key] = row
            positions[key] = QPointF(side * steps * (NODE_WIDTH + COLUMN_GAP),
                                     (row - middle) * (NODE_HEIGHT + ROW_GAP))
    return positions


@dataclass
class EdgeRoute:
    """How an edge is drawn: its line, the arrow's tip and which way it points
    (1: right, -1: left), and where its label goes (``align``: which side of
    the label ``label`` is: "center", "right" or "left")."""

    path: QPainterPath
    tip: QPointF
    direction: int
    label: QPointF
    align: str


def edge_route(start, end):
    """The EdgeRoute from a holder to the node it holds (their top left corners).

    Holders usually stand one column left of what they hold: the edge runs
    from the holder's right side to the held node's left side, labelled in
    the gap between. A holder can also stand in the same column (an array
    and an object both holding the target, the array also holding the
    object) or further right: those edges never cross the nodes of a column.
    The same column's loop out to the left, into the gap before the column,
    labelled at its outermost point; one running backwards goes from the
    holder's left side to the held node's right side.
    """
    if end.x() - start.x() > NODE_WIDTH / 2:
        source, tip, direction = right_mid(start), left_mid(end), 1
    elif abs(end.x() - start.x()) <= NODE_WIDTH / 2:
        source, tip = left_mid(start), left_mid(end)
        # Further out the further apart, never past a third of the gap
        bulge = min(COLUMN_GAP / 3, 18 + abs(tip.y() - source.y()) * 0.1)
        # A cubic curve reaches 3/4 of the way to its control points
        out = source.x() - bulge * 4 / 3
        path = QPainterPath(source)
        path.cubicTo(QPointF(out, source.y()), QPointF(out, tip.y()), tip)
        middle = path.pointAtPercent(0.5)
        return EdgeRoute(path, tip, 1, QPointF(middle.x() - 4, middle.y()), "right")
    else:
        source, tip, direction = left_mid(start), right_mid(end), -1
    middle = (source.x() + tip.x()) / 2
    path = QPainterPath(source)
    path.cubicTo(QPointF(middle, source.y()), QPointF(middle, tip.y()), tip)
    if direction == 1 or abs(tip.x() - source.x()) <= COLUMN_GAP + 1:
        point = path.pointAtPercent(0.5)
        return EdgeRoute(path, tip, direction, QPointF(point.x(), point.y() - 10), "center")
    # Backwards over several columns: labelled in the gap beside the held node
    return EdgeRoute(path, tip, direction, QPointF(tip.x() + 12, tip.y() - 12), "left")


# How often a label moves off the others before it stays where it is
MAX_LABEL_MOVES = 6


def free_place(rect, taken):
    """``rect`` moved up or down, by its height a step, as little as it
    takes to overlap none of ``taken``; where it was if no place is near."""
    step = rect.height() + 1
    for move in range(MAX_LABEL_MOVES + 1):
        for sign in (1, -1) if move else (1,):
            candidate = rect.translated(0, sign * move * step)
            if not any(candidate.intersects(other) for other in taken):
                return candidate
    return rect


def left_mid(corner):
    """The middle of the left side of the node with top left corner ``corner``."""
    return QPointF(corner.x(), corner.y() + NODE_HEIGHT / 2)


def right_mid(corner):
    return QPointF(corner.x() + NODE_WIDTH, corner.y() + NODE_HEIGHT / 2)


class NodeItem(QGraphicsRectItem):
    """One node of the graph: a box with two lines of text."""

    def __init__(self, node, translations, target=False, hidden=0, hidden_user=0,
                 hidden_filter=0, direction=HOLDERS):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        """``hidden`` of the locations holding it are not shown (other
        memory), ``hidden_user`` because the user hid them, ``hidden_filter``
        because only singletons are shown."""
        super().__init__(0, 0, NODE_WIDTH, NODE_HEIGHT)
        self.node = node
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setToolTip(node_tooltip(node, translations, hidden, hidden_user, hidden_filter,
                                     direction))
        self.setBrush(QBrush(theme_qcolor("surface")))
        self._target = target
        self.set_highlight(None)
        hidden += hidden_user + hidden_filter
        first, second = node_lines(node, translations)
        title = QGraphicsSimpleTextItem(_clip(first), self)
        font = QFont(title.font())
        font.setBold(True)
        title.setFont(font)
        title.setBrush(QBrush(theme_qcolor("text")))
        title.setPos(10, 5)
        detail = QGraphicsSimpleTextItem(_clip(second), self)
        detail.setBrush(QBrush(theme_qcolor("muted")))
        detail.setPos(10, 25)
        if node.can_expand:
            # A handle on the left: there are holders to unfold
            marker = QGraphicsSimpleTextItem("+", self)
            marker.setFont(font)
            marker.setBrush(QBrush(theme_qcolor("accent")))
            marker.setPos(-14, NODE_HEIGHT / 2 - 10)
        elif node.kind == OBJECT and node.total > node.shown - hidden:
            # In the bottom right corner (an address is short there), clear
            # of the arrows coming in on the left
            more = QGraphicsSimpleTextItem(f"+{node.total - node.shown + hidden}", self)
            more.setBrush(QBrush(theme_qcolor("muted")))
            more.setPos(NODE_WIDTH - 8 - more.boundingRect().width(), 25)

    def set_highlight(self, mark):
        """Draw the node as part of a marked way (``mark``: ROOT_PATH or
        MARKED_PATH) or, with None, as usual."""
        node = self.node
        if mark is not None:
            color, width = theme_qcolor(_MARK_COLORS[mark]), 2.5
        elif self._target:
            color, width = theme_qcolor("accent"), 2.5
        elif node.is_root:
            color, width = theme_qcolor("success"), 2
        else:
            color, width = theme_qcolor("border"), 1.2
        pen = QPen(color, width)
        if node.kind == UNKNOWN:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)


class GraphView(QGraphicsView):
    """The scene's view: drag to move, Ctrl+wheel to zoom."""

    # the NodeItem double-clicked
    node_activated = Signal(object)

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(theme_qcolor("background")))

    def wheelEvent(self, event):  # pylint: disable=invalid-name
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def node_at(self, position):
        """The NodeItem at ``position`` (viewport coordinates), or None."""
        item = self.itemAt(position)
        while item is not None and not isinstance(item, NodeItem):
            item = item.parentItem()
        return item

    def mouseDoubleClickEvent(self, event):  # pylint: disable=invalid-name
        item = self.node_at(event.position().toPoint())
        if item is None:
            super().mouseDoubleClickEvent(event)
            return
        self.node_activated.emit(item)
        event.accept()


class ReferenceGraphDialog(QDialog):
    """Show which objects hold an object, and which hold those, as a graph.

    ``panel`` is the scripts tab (grembyte.ui.widgets.scripts.ScriptPanel):
    its worker names the holders (``resolve_references``), its inspections
    open an object in the object inspector. The game's memory is read once
    (grembyte.features.object_references.ReferenceIndex) when the window
    opens and again on "Read again"; a double click unfolds a node's holders.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, translations, panel, pid, target, class_name="", name=None,
                 framework=(), parent=None, is_framework=lambda _name: False):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        """``target`` the object's address (int); ``framework`` the engine
        adapter's framework_prefixes (see ScriptWorker.resolve_references);
        ``is_framework(package)`` whether a package belongs to the engine
        (for "only singletons", as in the object browser)."""
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        self.panel = panel
        self.pid = pid
        self.framework = tuple(framework)
        self.is_framework = is_framework
        # The ``klass`` of the singleton classes, once listed; the request asking
        self._singletons = None
        self._classes_request = None
        # One graph per way, what the user hid shared (see _new_graphs)
        self._graphs = {}
        self.graph = None
        self._new_graphs(target, class_name, name)
        self.index = None
        self._index_worker = None
        self._worker = None
        self._relays = []  # the connections to the worker's answers
        # request id -> (way, node key, the locations asked about)
        self._pending = {}
        # (way, node key) -> {entries: {location: answer}, waiting: batches due, done}
        self._expansions = {}
        self._items = {}
        # Unfolding nodes one after another until a root is reached
        self._searching = False
        self._search_steps = 0
        self._path = None
        # Per way, the node whose way to the object is highlighted (MARKED_PATH)
        self._marks = {}
        self.setWindowTitle(translations["references.title"].format(
            name=f"{short_class(class_name)} 0x{target:x}" if class_name else f"0x{target:x}"))
        self.resize(1100, 700)
        self._build_ui()
        self._redraw()
        settings.subscribe(self._settings_changed)
        self.destroyed.connect(self._forget_settings)
        self.read_memory()

    def _new_graphs(self, address, class_name="", name=None, klass=None):
        """The graphs of both ways, from the object alone; what the user hid
        (and the classes shown) stays, for both ways alike."""
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        previous = self.graph
        self._graphs = {direction: ReferenceGraph.for_object(address, class_name, name, klass,
                                                             direction)
                        for direction in (HOLDERS, TARGETS)}
        holders, targets = self._graphs[HOLDERS], self._graphs[TARGETS]
        if previous is not None:
            holders.hidden_keys = previous.hidden_keys
            holders.hidden_classes = previous.hidden_classes
            holders.only_classes = previous.only_classes
        targets.hidden_keys = holders.hidden_keys
        targets.hidden_classes = holders.hidden_classes
        targets.only_classes = holders.only_classes
        self.graph = self._graphs[self.graph.direction if self.graph is not None else HOLDERS]

    @property
    def _marked(self):
        return self._marks.get(self.graph.direction)

    def _settings_changed(self, changed):
        if "reference_other_memory" in changed:
            self._redraw()

    def _forget_settings(self, _object=None):
        settings.unsubscribe(self._settings_changed)

    def _build_ui(self):
        # The widgets get the window as parent at once: added to a row layout not
        # yet attached, PySide kept them as Python's own and deleted them a second
        # time at exit, which crashed Grembyte (a QCheckBox, seen in crash.log).
        t = self.translations
        layout = QVBoxLayout(self)
        layout.addWidget(DocsHint(t, t["references.hint"], "guide/reference-graph/"))
        bar = QHBoxLayout()
        self.export_button = QPushButton(t["references.export"], self)
        set_button_icon(self.export_button, "download")
        self.export_button.setToolTip(t["references.export_tooltip"])
        self.export_button.clicked.connect(self.export_html)
        bar.addWidget(self.export_button)
        # The way the graph grows: to what holds the object, or to what it points to
        self.direction_group = QButtonGroup(self)
        self.direction_buttons = {}
        for direction in (HOLDERS, TARGETS):
            button = QPushButton(t[f"references.direction_{direction}"], self)
            button.setToolTip(t[f"references.direction_{direction}_tooltip"])
            button.setCheckable(True)
            button.setChecked(direction == self.graph.direction)
            button.clicked.connect(lambda _checked, way=direction: self.set_direction(way))
            self.direction_group.addButton(button)
            self.direction_buttons[direction] = button
            bar.addWidget(button)
        self._show_direction()
        self.path_button = QPushButton(t["references.find_root"], self)
        set_button_icon(self.path_button, "account_tree")
        self.path_button.setToolTip(t["references.find_root_tooltip"])
        self.path_button.clicked.connect(self.find_root)
        bar.addWidget(self.path_button)
        bar.addStretch()
        self.singletons_check = QCheckBox(t["references.only_singletons"], self)
        self.singletons_check.setToolTip(t["references.only_singletons_tooltip"])
        self.singletons_check.toggled.connect(self._only_singletons)
        self.refresh_button = QPushButton(t["references.refresh"], self)
        set_button_icon(self.refresh_button, "refresh")
        self.refresh_button.setToolTip(t["references.refresh_tooltip"])
        self.refresh_button.clicked.connect(self.read_memory)
        bar.addWidget(self.singletons_check)
        bar.addWidget(self.refresh_button)
        self.pin_button = PinButton(t, parent=self)
        bar.addWidget(self.pin_button)
        layout.addLayout(bar)

        self.scene = QGraphicsScene(self)
        self.scene.selectionChanged.connect(self._update_buttons)
        self.view = GraphView(self.scene, self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.node_activated.connect(self._activated)
        layout.addWidget(self.view, 1)

        bottom = QHBoxLayout()
        # While the memory is read or answers of the agent are due
        self.busy = BusyDots(self)
        bottom.addWidget(self.busy)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        layout.addLayout(bottom)
        self._update_buttons()

    # --- Reading the memory --------------------------------------------------------
    def read_memory(self):
        """Read the game's memory anew; the graph starts again from the target."""
        if self._index_worker is not None:
            return
        target = self.graph.nodes[self.graph.target]
        # What the user hid stays hidden; the highlight is drawn again once
        # its node is unfolded again
        self._new_graphs(target.address, target.class_name, target.name, target.klass)
        # The singletons listed anew with the memory (see _on_index)
        self._singletons = None
        self._classes_request = None
        self._pending.clear()
        self._expansions.clear()
        self._stop_search()
        self.index = None
        self._redraw()
        worker = ReferenceIndexWorker(self.pid)
        worker.progress.connect(self._show_reading)
        worker.ready.connect(self._on_index)
        worker.failed.connect(self._on_index_failed)
        worker.finished.connect(self._index_read)
        self._index_worker = worker
        keep_until_finished(worker)
        self.status_label.setText(self.translations["references.reading"])
        self._update_buttons()
        worker.start()

    def _show_reading(self, percent):
        # A bound method: not called once the window is gone (see inspection.py)
        if self._index_worker is not None:
            self.status_label.setText(self.translations["references.reading_percent"].format(
                percent=percent))

    def _index_read(self):
        # A bound method: not called once the window is gone (see inspection.py)
        self._index_worker = None
        self._update_buttons()

    def _on_index(self, index):
        self.index = index
        self.status_label.setText(self.translations["references.ready"].format(
            pointers=len(index.pointer_map)))
        if self.singletons_check.isChecked():
            # The target is unfolded once the singletons are known
            self._list_classes()
            return
        self.expand(self.graph.target)

    def _on_index_failed(self, message):
        self.status_label.setText(message)
        self._update_buttons()

    # --- Unfolding nodes -----------------------------------------------------------
    def _current_worker(self):
        """The scripts tab's worker, its answers connected; None when detached."""
        worker = self.panel.worker()
        if worker is not None and worker is not self._worker:
            self._worker = worker
            for relay in self._relays:
                relay.deleteLater()
            # The worker outlives this window: connected so that its answers
            # stop reaching it once it is deleted
            self._relays = [
                connect_while_alive(signal, self, lambda request, answer, handler=handler:
                                    handler(worker, request, answer))
                for signal, handler in ((worker.references_resolved, self._on_resolved),
                                        (worker.references_failed, self._on_failed),
                                        (worker.classes_browsed, self._on_classes),
                                        (worker.browse_failed, self._on_classes_failed))]
            self._classes_request = None
            self._pending.clear()
            self._expansions.clear()
            self._update_busy()
        return worker

    def expand(self, key):
        """Look up what node ``key`` (an object not unfolded yet) leads to:
        what holds it, or (TARGETS) what it points to."""
        graph = self.graph
        node = graph.nodes.get(key)
        if (node is None or not node.can_expand or self.index is None
                or (graph.direction, key) in self._expansions):
            return
        if graph.direction == HOLDERS:
            self._expand_holders(graph, node)
        else:
            self._expand_targets(graph, node)

    def _expand_holders(self, graph, node):
        key = node.key
        locations, total = self.index.referrers(node.address)
        roots, heap = [], []
        for location in locations:
            place = self.index.place(location)
            if place[0] is None:
                heap.append(location)
            else:
                roots.append((graph.node_for(key, location, place=place), ""))
        if not heap:
            self._finish(graph, key, roots, total)
            return

        def named(entries):
            holders = [(graph.node_for(key, location, entry=entries[location]),
                        entries[location].get("field") or "") for location in heap]
            self._finish(graph, key, roots + holders, total)
        self._resolve(graph, key, heap, named)

    def _expand_targets(self, graph, node):
        """The objects ``node`` points to: the pointers lying in its memory
        (its size from the agent) to the start of an object, labelled with
        the field holding them, and those in the static fields of its class
        (Mono), each through that field's node."""
        key, start = node.key, node.address
        if node.size is None or node.statics is None:
            def sized(entries):
                entry = entries.get(start) or {}
                if entry.get("object") and int(entry["object"], 16) == start and entry.get("size"):
                    node.size = int(entry["size"])
                    node.statics = static_fields(entry) or []
                    self._expand_targets(graph, node)
                else:
                    self.status_label.setText(self.translations["references.not_an_object"])
                    self._finish(graph, key, [], 0, quiet=True)
            self._resolve(graph, key, [start], sized)
            return
        pairs, total = self.index.pointers_in(start, start + node.size)
        # The static fields holding a pointer: [(field, location, class name, value)]
        statics = []
        for field_name, location, class_name in node.statics:
            held, _count = self.index.pointers_in(location, location + 1)
            if held and held[0][1]:
                statics.append((field_name, location, class_name, held[0][1]))
        total += len(statics)
        values = list(dict.fromkeys([value for _location, value in pairs
                                     if value and value != start]
                                    + [value for *_rest, value in statics]))
        if not values:
            self._finish(graph, key, [], total)
            return

        def is_object(entry, value):
            return entry.get("object") and int(entry["object"], 16) == value

        def named(entries):
            targets = []
            for location, value in pairs:
                entry = entries.get(value) or {}
                if not is_object(entry, value):
                    continue  # not the start of an object
                place = entries.get(location) or {}
                label = (place.get("field") if place.get("object")
                         and int(place["object"], 16) == start
                         else None) or f"+0x{location - start:x}"
                targets.append((graph.node_for(key, value, entry=entry), label))
            for field_name, location, class_name, value in statics:
                entry = entries.get(value) or {}
                if is_object(entry, value):
                    target = graph.node_for(key, value, entry=entry)
                    targets.append((graph.add_static_target(
                        key, field_name, location, class_name, target), ""))
            self._finish(graph, key, targets, total)
        self._resolve(graph, key,
                      list(dict.fromkeys([location for location, _value in pairs] + values)),
                      named)

    def _resolve(self, graph, key, locations, done):
        """Have the agent name ``locations`` (in batches); ``done({location:
        answer})`` once all are back."""
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["references.detached"])
            self._stop_search()
            return
        batches = [locations[at:at + RESOLVE_BATCH] for at in range(0, len(locations),
                                                                  RESOLVE_BATCH)]
        self._expansions[(graph.direction, key)] = {"entries": {}, "waiting": len(batches),
                                                    "done": done}
        for batch in batches:
            request = worker.resolve_references(batch, self.framework)
            self._pending[request] = (graph.direction, key, batch)
        self.status_label.setText(self.translations["references.resolving"].format(
            count=len(locations)))
        self._update_buttons()

    def _on_resolved(self, worker, request, answer):
        if worker is not self._worker or request not in self._pending:
            return
        direction, key, batch = self._pending.pop(request)
        self._update_busy()
        expansion = self._expansions.get((direction, key))
        if expansion is None:
            return  # another batch of it failed
        for location, entry in zip(batch, answer):
            expansion["entries"][location] = entry
        expansion["waiting"] -= 1
        if expansion["waiting"] == 0:
            del self._expansions[(direction, key)]
            expansion["done"](expansion["entries"])

    def _on_failed(self, worker, request, message):
        if worker is not self._worker or request not in self._pending:
            return
        direction, key, _batch = self._pending.pop(request)
        self._update_busy()
        if self._expansions.pop((direction, key), None) is None:
            return
        self.status_label.setText(message)
        self._stop_search()
        self._update_buttons()

    def _finish(self, graph, key, linked, total, quiet=False):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        graph.add_holders(key, linked, total)
        node = graph.nodes[key]
        t = self.translations
        holders = graph.direction == HOLDERS
        if quiet:
            pass
        elif total == 0:
            self.status_label.setText(t["references.none_found" if holders
                                        else "references.none_found_targets"])
        else:
            self.status_label.setText(t["references.found" if holders
                                        else "references.found_targets"].format(
                shown=node.shown, total=total))
        if graph is not self.graph:
            return
        if self._searching:
            self._continue_search()
        self._redraw()

    # --- The two ways ----------------------------------------------------------------
    def set_direction(self, direction):
        """Show the graph growing to what holds the object (HOLDERS) or to
        what it points to (TARGETS); a double click unfolds either way."""
        self.direction_buttons[direction].setChecked(True)
        if direction == self.graph.direction:
            return
        self._stop_search()
        self.graph = self._graphs[direction]
        self._show_direction()
        self._items = {}  # centred on the object again
        target = self.graph.nodes[self.graph.target]
        waiting = self.singletons_check.isChecked() and self._singletons is None
        if target.can_expand and self.index is not None and not waiting:
            self.expand(target.key)
        self._redraw()

    def _show_direction(self):
        """The way shown as the primary button, the other as a plain one."""
        for direction, button in self.direction_buttons.items():
            button.setObjectName("btn_primary" if direction == self.graph.direction else "")
            set_button_icon(button, _DIRECTION_ICONS[direction])
            button.style().unpolish(button)
            button.style().polish(button)

    # --- Only singletons -----------------------------------------------------------
    def _only_singletons(self, on):
        if not on:
            self._show_classes(None)
            self._redraw()
        elif self._singletons is not None:
            self._show_classes(self._singletons)
            self._redraw()
        else:
            self._list_classes()

    def _show_classes(self, classes):
        for graph in self._graphs.values():
            graph.only_classes = classes

    def _list_classes(self):
        """Ask for the classes with live objects to know the singletons."""
        if self._classes_request is not None:
            return
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["references.detached"])
            return
        self._classes_request = worker.browse_classes("", False)
        self.status_label.setText(self.translations["references.reading_classes"])
        self._update_busy()

    def _on_classes(self, worker, request, answer):
        if worker is not self._worker or request != self._classes_request:
            return
        self._classes_request = None
        # Not at the top: the object browser imports this module
        from grembyte.ui.widgets.object_browser import is_singleton  # pylint: disable=import-outside-toplevel
        self._singletons = {entry["klass"] for entry in answer.get("classes", [])
                            if is_singleton(entry, self.is_framework)}
        self.status_label.setText(self.translations["references.singletons"].format(
            count=len(self._singletons)))
        if self.singletons_check.isChecked():
            self._show_classes(self._singletons)
        target = self.graph.nodes[self.graph.target]
        if target.can_expand and self.index is not None:
            self.expand(target.key)
        self._redraw()

    def _on_classes_failed(self, worker, request, message):
        if worker is not self._worker or request != self._classes_request:
            return
        self._classes_request = None
        self._update_busy()
        self.status_label.setText(self.translations["references.classes_failed"].format(
            message=message.splitlines()[0] if message else message))
        target = self.graph.nodes[self.graph.target]
        if target.can_expand and self.index is not None:
            self.expand(target.key)

    # --- The way to a root -------------------------------------------------------
    def find_root(self):
        """Unfold the nearest nodes until one is a root, and mark the way there."""
        if self.index is None:
            return
        self._searching = True
        self._search_steps = 0
        self._continue_search()
        self._update_buttons()

    def _continue_search(self):
        path = self.graph.path_to_root()
        key = None if path is not None else self.graph.next_to_expand()
        if path is not None or key is None or self._search_steps >= MAX_PATH_STEPS:
            self._searching = False
            self._path = path
            t = self.translations
            if path is not None:
                self.status_label.setText(t["references.root_found"].format(
                    steps=len(path) - 1))
            else:
                self.status_label.setText(t["references.no_root"])
            self._redraw()
            return
        self._search_steps += 1
        self.expand(key)

    def _stop_search(self):
        self._searching = False
        self._path = None
        self._update_busy()

    # --- Drawing -------------------------------------------------------------------
    def _redraw(self):
        if not getattr(shiboken6, "isValid")(self):
            return
        self._update_busy()
        selected = self._selected()
        selected_key = None if selected is None else selected.key
        first = not self._items
        self.scene.clear()
        self._items = {}
        # Other memory only if the settings say so
        hidden = () if settings.get("reference_other_memory") else (UNKNOWN,)
        positions = layout_positions(self.graph, hidden)
        # The ways drawn anew: around what the user hid since
        if self._path is not None:
            self._path = self.graph.path_to_root()
        marked = (None if self._marked is None or self._marked not in self.graph.nodes
                  else self.graph.path_from(self._marked))
        direction = self.graph.direction
        marks = {key: ROOT_PATH for key in self._path or ()}
        marks.update((key, MARKED_PATH) for key in marked or ())
        for key, position in positions.items():
            item = NodeItem(self.graph.nodes[key], self.translations,
                            target=key == self.graph.target,
                            hidden=self.graph.hidden_locations(key, hidden),
                            hidden_user=self.graph.hidden_locations(key, hidden, BY_USER),
                            hidden_filter=self.graph.hidden_locations(key, hidden, FILTERED),
                            direction=direction)
            item.setPos(position)
            item.set_highlight(marks.get(key))
            self.scene.addItem(item)
            self._items[key] = item
            if key == selected_key:
                item.setSelected(True)
        labels = {}
        for edge in self.graph.edges:
            if edge.holder in positions and edge.held in positions:
                labels.setdefault((edge.holder, edge.held), []).append(edge.label)
        edge_marks = {pair: ROOT_PATH for pair in self.graph.path_edges(self._path)}
        edge_marks.update((pair, MARKED_PATH) for pair in self.graph.path_edges(marked))
        # The labels placed so far; the edges of the marked ways first
        taken = []
        order = sorted(labels.items(), key=lambda pair: pair[0] not in edge_marks)
        for (holder, held), names in order:
            self._draw_edge(positions[holder], positions[held],
                            ", ".join(name for name in names if name),
                            edge_marks.get((holder, held)), taken)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        if first:
            self.view.centerOn(self._items[self.graph.target])
        self._update_buttons()

    def _draw_edge(self, start, end, label, mark, taken):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        """An arrow from the holder (top left corner ``start``) to the held
        node (``end``), its label where ``edge_route`` puts it, or moved up
        or down off the labels already ``taken`` (their QRectFs); ``mark``
        as in NodeItem.set_highlight."""
        route = edge_route(start, end)
        color = theme_qcolor("muted" if mark is None else _MARK_COLORS[mark])
        line = QGraphicsPathItem(route.path)
        line.setPen(QPen(color, 1.2 if mark is None else 2))
        # Marked ways above the others where lines cross
        line.setZValue(-2 if mark is None else -1)
        self.scene.addItem(line)
        tip, back = route.tip, -9 * route.direction
        arrow = QGraphicsPolygonItem(QPolygonF([
            tip, QPointF(tip.x() + back, tip.y() - 5), QPointF(tip.x() + back, tip.y() + 5)]))
        arrow.setBrush(QBrush(color))
        arrow.setPen(QPen(color))
        self.scene.addItem(arrow)
        if label:
            text = QGraphicsSimpleTextItem(_clip(label, 26))
            text.setBrush(QBrush(theme_qcolor("text")))
            text.setToolTip(label)
            width = text.boundingRect().width()
            height = text.boundingRect().height()
            point = route.label
            left = {"center": point.x() - width / 2, "right": point.x() - width}.get(
                route.align, point.x())
            rect = free_place(QRectF(left - 3, point.y() - height / 2 - 1, width + 6, height + 2),
                              taken)
            taken.append(rect)
            text.setPos(rect.x() + 3, rect.y() + 1)
            box = QGraphicsRectItem(rect)
            box.setBrush(QBrush(theme_qcolor("background")))
            box.setPen(QPen(Qt.PenStyle.NoPen))
            self.scene.addItem(box)
            self.scene.addItem(text)

    def fit(self):
        """Zoom so that the whole graph fits the window."""
        self.view.fitInView(self.scene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # --- Actions -------------------------------------------------------------------
    def _selected(self):
        """The Node selected, or None."""
        if not hasattr(self, "scene"):
            return None
        for item in self.scene.selectedItems():
            if isinstance(item, NodeItem):
                return item.node
        return None

    def _activated(self, item):
        """A double click: unfold an object the way shown, or fold it in again
        (the inspector opens from the context menu)."""
        node = item.node
        if node.can_expand:
            self.expand(node.key)
        elif node.kind == OBJECT:
            self.collapse(node.key)

    def collapse(self, key):
        """Fold node ``key`` in again: what unfolding it showed goes, with every
        node only reached through it (an unfolding still due is left alone)."""
        graph = self.graph
        node = graph.nodes.get(key)
        if (node is None or node.kind != OBJECT or not node.expanded
                or (graph.direction, key) in self._expansions):
            return
        self._stop_search()
        graph.collapse(key)
        self._redraw()

    def inspect(self, node):
        """Open an object node in the object inspector."""
        if node is None or node.target() is None:
            return
        inspections = self.panel.inspections
        if inspections is None:
            return
        inspections.inspect(node.target(), name=short_class(node.class_name),
                            instance_name=node.name or None)

    def _menu(self, position):
        item = self.view.node_at(position)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        self.fill_menu(menu, None if item is None else item.node)
        menu.exec(self.view.viewport().mapToGlobal(position))

    def fill_menu(self, menu, node):
        """The context menu: the actions on ``node`` (None: the background),
        then those on the graph."""
        t = self.translations
        if node is not None:
            self._node_actions(menu, node)
            menu.addSeparator()
        menu.addAction(t["references.fit"], self.fit).setToolTip(t["references.fit_tooltip"])
        if self._marked is not None:
            menu.addAction(t["references.highlight_clear"], lambda: self.highlight(None))
        hidden = self.graph.hidden_by_user()
        if hidden:
            menu.addAction(t["references.show_hidden"].format(count=hidden),
                           self.show_hidden).setToolTip(t["references.show_hidden_tooltip"])
        menu.addAction(t["references.export"], self.export_html).setToolTip(
            t["references.export_tooltip"])

    def _node_actions(self, menu, node):
        t = self.translations
        clipboard = QGuiApplication.clipboard()
        if node.can_expand:
            expand = ("references.expand" if self.graph.direction == HOLDERS
                      else "references.expand_targets")
            menu.addAction(t[expand], lambda: self.expand(node.key))
        elif node.kind == OBJECT and node.expanded:
            menu.addAction(t["references.collapse"], lambda: self.collapse(node.key))
        if node.target() is not None:
            menu.addAction(t["references.inspect"], lambda: self.inspect(node)).setToolTip(
                t["references.inspect_tooltip"])
        menu.addAction(t["references.copy_address"],
                       lambda: clipboard.setText(f"0x{node.address:x}"))
        if node.kind in (STATIC, MODULE):
            text = node.name if node.kind == MODULE else f"{node.class_name}.{node.name}"
            menu.addAction(t["references.copy_name"], lambda: clipboard.setText(text))
        if node.key == self.graph.target:
            return
        menu.addSeparator()
        highlight = ("references.highlight_path" if self.graph.direction == HOLDERS
                     else "references.highlight_path_targets")
        menu.addAction(t[highlight], lambda: self.highlight(node.key))
        menu.addAction(t["references.hide_node"], lambda: self.hide_node(node.key))
        if node.kind == OBJECT and node.class_name:
            menu.addAction(t["references.hide_class"].format(cls=short_class(node.class_name)),
                           lambda: self.hide_class(node.class_name))

    # --- Hiding and highlighting ----------------------------------------------------
    def highlight(self, key):
        """Highlight the way from node ``key`` to the object (MARKED_PATH);
        None clears it."""
        self._marks[self.graph.direction] = key
        if key is not None and self.graph.path_from(key) is None:
            self.status_label.setText(self.translations["references.no_path"])
        self._redraw()

    def hide_node(self, key):
        """Leave node ``key`` out of the graph, and the way to a root out of it."""
        self.graph.hidden_keys.add(key)
        self._hidden_changed()

    def hide_class(self, class_name):
        """Leave every object of class ``class_name`` out of the graph."""
        self.graph.hidden_classes.add(class_name)
        self._hidden_changed()

    def show_hidden(self):
        """Show what the user hid again."""
        self.graph.hidden_keys.clear()
        self.graph.hidden_classes.clear()
        self._hidden_changed()

    def _hidden_changed(self):
        # A highlight through what is hidden now shows again with it
        self._redraw()

    # --- Export --------------------------------------------------------------------
    def export_html(self):
        """Save the graph as it is drawn now as an HTML page."""
        t = self.translations
        target = self.graph.nodes[self.graph.target]
        name = target.name or short_class(target.class_name) or "object"
        stamp = datetime.now()
        file_name = "-".join(
            "".join(char if char.isalnum() or char in "._-" else "_" for char in part).strip("_")
            for part in ("references", name, f"{target.address:x}",
                         stamp.strftime("%Y%m%d-%H%M%S"))) + ".html"
        path, _filter = QFileDialog.getSaveFileName(
            self, t["references.export"], suggested_path(file_name), t["inspector.export_filter"])
        if not path:
            return
        if not path.lower().endswith((".html", ".htm")):
            path += ".html"
        remember(path)
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(self.render_html(stamp))
        except OSError as error:
            self.status_label.setText(t["references.export_failed"].format(error=error))
            return
        self.status_label.setText(t["inspector.exported"].format(path=path))

    def render_html(self, stamp=None):
        """The HTML page of the graph as it is drawn now (see graph_export)."""
        t = self.translations
        target = self.graph.nodes[self.graph.target]
        facts = [(t["inspector.export_address"], f"0x{target.address:x}")]
        program = program_of(self.pid) if self.pid is not None else None
        if program is not None:
            facts.append((t["inspector.export_program"],
                          t["app.process_display"].format(name=program[0], pid=self.pid)))
        facts.append((t["inspector.export_time"],
                      (stamp or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")))
        colors = {key: theme_color(key) for key in ("background", "text", "muted")}
        return graph_export.render_html(
            self.scene, self.windowTitle(), facts, colors,
            t["inspector.export_footer"].format(version=__version__),
            language_code(settings.language()))

    def _update_buttons(self):
        if not hasattr(self, "path_button"):
            return
        ready = self.index is not None
        # The way to a root only where the graph grows to the holders
        self.path_button.setVisible(self.graph.direction == HOLDERS)
        self.path_button.setEnabled(ready and not self._searching)
        self.refresh_button.setEnabled(self._index_worker is None)
        self._update_busy()

    def _update_busy(self):
        """The dots while the memory is read, the agent names objects or lists the
        classes, or a root is sought."""
        if hasattr(self, "busy"):
            self.busy.set_busy(self._index_worker is not None or bool(self._pending)
                               or self._classes_request is not None or self._searching)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._forget_settings()
        if self._index_worker is not None:
            self._index_worker.requestInterruption()
        super().closeEvent(event)
