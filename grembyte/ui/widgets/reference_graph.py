"""Window of the reference graph: which objects hold an object, up to the roots."""

from dataclasses import dataclass

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
    QDialog,
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
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from grembyte.core import settings
from grembyte.features.object_references import (
    MAX_PATH_STEPS,
    MODULE,
    OBJECT,
    STACK,
    STATIC,
    UNKNOWN,
    ReferenceGraph,
    ReferenceIndexWorker,
)
from grembyte.ui.widgets.docking import PinButton
from grembyte.ui.widgets.icons import set_button_icon, theme_qcolor
from grembyte.ui.widgets.labels import DocsHint
from grembyte.ui.workers import keep_until_finished

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


def node_tooltip(node, translations, hidden=0):
    """The tooltip of a node: its details and what a double click does;
    ``hidden`` of its holders' locations are not shown (other memory)."""
    t = translations
    lines = []
    if node.kind == OBJECT:
        lines.append(node.class_name or "?")
        if node.name:
            lines.append(f'"{node.name}"')
        lines.append(f"0x{node.address:x}")
        if not node.expanded:
            lines.append(t["references.expand_hint"])
        elif node.total == 0:
            lines.append(t["references.no_holders"])
        else:
            lines.append(t["references.holders_count"].format(shown=node.shown - hidden,
                                                               total=node.total))
            if hidden:
                lines.append(t["references.hidden_other"].format(count=hidden))
    else:
        lines.append(t[f"references.{node.kind}_tooltip"])
        lines.extend(f"0x{location:x}" for location in node.locations[:5])
        if len(node.locations) > 5:
            lines.append("…")
        if node.kind in (STATIC, MODULE):
            lines.append(f"0x{node.address:x}")
    return "\n".join(lines)


def layout_positions(graph, hidden=()):
    """``{key: QPointF}`` of the nodes' top left corners: the target on the
    right, each step to its holders a column further left; in a column the
    nodes follow the order of what they hold, which keeps edges from crossing.
    Holders of the node kinds ``hidden`` are left out."""
    depth = graph.depths(hidden)
    columns = {}
    for key, steps in depth.items():
        columns.setdefault(steps, []).append(key)
    row_of = {graph.target: 0.0}
    positions = {}
    for steps in sorted(columns):
        keys = columns[steps]
        if steps > 0:
            def place(key, steps=steps):
                held = [row_of[edge.held] for edge in graph.edges
                        if edge.holder == key and depth.get(edge.held) == steps - 1]
                return (min(held) if held else 0.0, key)
            keys.sort(key=place)
        middle = (len(keys) - 1) / 2
        for row, key in enumerate(keys):
            row_of[key] = row
            positions[key] = QPointF(-steps * (NODE_WIDTH + COLUMN_GAP),
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

    def __init__(self, node, translations, target=False, hidden=0):
        """``hidden`` of the locations holding it are not shown (other memory)."""
        super().__init__(0, 0, NODE_WIDTH, NODE_HEIGHT)
        self.node = node
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setToolTip(node_tooltip(node, translations, hidden))
        self.setBrush(QBrush(theme_qcolor("surface")))
        self._target = target
        self.set_highlight(False)
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

    def set_highlight(self, on):
        """Draw the node as part of the way to a root (``on``) or as usual."""
        node = self.node
        if on:
            color, width = theme_qcolor("orange"), 2.5
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
                 framework=(), parent=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        """``target`` the object's address (int); ``framework`` the engine
        adapter's framework_prefixes (see ScriptWorker.resolve_references)."""
        super().__init__(parent)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.translations = translations
        self.panel = panel
        self.pid = pid
        self.framework = tuple(framework)
        self.graph = ReferenceGraph.for_object(target, class_name, name)
        self.index = None
        self._index_worker = None
        self._worker = None
        # request id -> (node key, the heap locations asked about)
        self._pending = {}
        # node key -> {holders: [(Node, label)] so far, total, waiting: batches due}
        self._expansions = {}
        self._items = {}
        # Unfolding nodes one after another until a root is reached
        self._searching = False
        self._search_steps = 0
        self._path = None
        self.setWindowTitle(translations["references.title"].format(
            name=f"{short_class(class_name)} 0x{target:x}" if class_name else f"0x{target:x}"))
        self.resize(1100, 700)
        self._build_ui()
        self._redraw()
        settings.subscribe(self._settings_changed)
        self.destroyed.connect(self._forget_settings)
        self.read_memory()

    def _settings_changed(self, changed):
        if "reference_other_memory" in changed:
            self._redraw()

    def _forget_settings(self, _object=None):
        settings.unsubscribe(self._settings_changed)

    def _build_ui(self):
        t = self.translations
        layout = QVBoxLayout(self)
        layout.addWidget(DocsHint(t, t["references.hint"], "guide/reference-graph/"))
        bar = QHBoxLayout()
        self.path_button = QPushButton(t["references.find_root"])
        set_button_icon(self.path_button, "account_tree")
        self.path_button.setToolTip(t["references.find_root_tooltip"])
        self.path_button.clicked.connect(self.find_root)
        bar.addWidget(self.path_button)
        self.inspect_button = QPushButton(t["references.inspect"])
        set_button_icon(self.inspect_button, "manage_search")
        self.inspect_button.setToolTip(t["references.inspect_tooltip"])
        self.inspect_button.clicked.connect(lambda: self.inspect(self._selected()))
        bar.addWidget(self.inspect_button)
        self.fit_button = QPushButton(t["references.fit"])
        set_button_icon(self.fit_button, "compress")
        self.fit_button.setToolTip(t["references.fit_tooltip"])
        self.fit_button.clicked.connect(self.fit)
        bar.addWidget(self.fit_button)
        bar.addStretch()
        self.refresh_button = QPushButton(t["references.refresh"])
        set_button_icon(self.refresh_button, "refresh")
        self.refresh_button.setToolTip(t["references.refresh_tooltip"])
        self.refresh_button.clicked.connect(self.read_memory)
        bar.addWidget(self.refresh_button)
        self.pin_button = PinButton(t)
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
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        bottom.addWidget(self.progress)
        layout.addLayout(bottom)
        self._update_buttons()

    # --- Reading the memory --------------------------------------------------------
    def read_memory(self):
        """Read the game's memory anew; the graph starts again from the target."""
        if self._index_worker is not None:
            return
        target = self.graph.nodes[self.graph.target]
        self.graph = ReferenceGraph.for_object(target.address, target.class_name, target.name,
                                               target.klass)
        self._pending.clear()
        self._expansions.clear()
        self._stop_search()
        self.index = None
        self._redraw()
        worker = ReferenceIndexWorker(self.pid)
        worker.progress.connect(self.progress.setValue)
        worker.ready.connect(self._on_index)
        worker.failed.connect(self._on_index_failed)
        worker.finished.connect(self._index_read)
        self._index_worker = worker
        keep_until_finished(worker)
        self.progress.setValue(0)
        self.progress.show()
        self.status_label.setText(self.translations["references.reading"])
        self._update_buttons()
        worker.start()

    def _index_read(self):
        # A bound method: not called once the window is gone (see inspection.py)
        self._index_worker = None
        self._update_buttons()

    def _on_index(self, index):
        self.index = index
        self.progress.hide()
        self.status_label.setText(self.translations["references.ready"].format(
            pointers=len(index.pointer_map)))
        self.expand(self.graph.target)

    def _on_index_failed(self, message):
        self.progress.hide()
        self.status_label.setText(message)
        self._update_buttons()

    # --- Unfolding nodes -----------------------------------------------------------
    def _current_worker(self):
        """The scripts tab's worker, its answers connected; None when detached."""
        worker = self.panel.worker()
        if worker is not None and worker is not self._worker:
            self._worker = worker
            worker.references_resolved.connect(lambda request, answer: self._on_resolved(
                worker, request, answer))
            worker.references_failed.connect(lambda request, message: self._on_failed(
                worker, request, message))
            self._pending.clear()
            self._expansions.clear()
        return worker

    def expand(self, key):
        """Look up what holds node ``key`` (an object not unfolded yet)."""
        node = self.graph.nodes.get(key)
        if node is None or not node.can_expand or self.index is None or key in self._expansions:
            return
        locations, total = self.index.referrers(node.address)
        roots, heap = [], []
        for location in locations:
            place = self.index.place(location)
            if place[0] is None:
                heap.append(location)
            else:
                roots.append((self.graph.node_for(key, location, place=place), ""))
        if not heap:
            self._finish(key, roots, total)
            return
        worker = self._current_worker()
        if worker is None:
            self.status_label.setText(self.translations["references.detached"])
            self._stop_search()
            return
        batches = [heap[start:start + RESOLVE_BATCH] for start in range(0, len(heap), RESOLVE_BATCH)]
        self._expansions[key] = {"holders": roots, "total": total, "waiting": len(batches)}
        for batch in batches:
            self._pending[worker.resolve_references(batch, self.framework)] = (key, batch)
        self.status_label.setText(self.translations["references.resolving"].format(
            count=len(heap)))
        self._update_buttons()

    def _on_resolved(self, worker, request, answer):
        if worker is not self._worker or request not in self._pending:
            return
        key, batch = self._pending.pop(request)
        expansion = self._expansions.get(key)
        if expansion is None:
            return  # another batch of it failed
        for location, entry in zip(batch, answer):
            holder = self.graph.node_for(key, location, entry=entry)
            expansion["holders"].append((holder, entry.get("field") or ""))
        expansion["waiting"] -= 1
        if expansion["waiting"] == 0:
            del self._expansions[key]
            self._finish(key, expansion["holders"], expansion["total"])

    def _on_failed(self, worker, request, message):
        if worker is not self._worker or request not in self._pending:
            return
        key, _batch = self._pending.pop(request)
        if self._expansions.pop(key, None) is None:
            return
        self.status_label.setText(message)
        self._stop_search()
        self._update_buttons()

    def _finish(self, key, holders, total):
        self.graph.add_holders(key, holders, total)
        node = self.graph.nodes[key]
        t = self.translations
        if total == 0:
            self.status_label.setText(t["references.none_found"])
        else:
            self.status_label.setText(t["references.found"].format(
                shown=node.shown, total=total))
        if self._searching:
            self._continue_search()
        self._redraw()

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

    # --- Drawing -------------------------------------------------------------------
    def _redraw(self):
        if not getattr(shiboken6, "isValid")(self):
            return
        selected = self._selected()
        selected_key = None if selected is None else selected.key
        first = not self._items
        self.scene.clear()
        self._items = {}
        # Other memory only if the settings say so
        hidden = () if settings.get("reference_other_memory") else (UNKNOWN,)
        positions = layout_positions(self.graph, hidden)
        on_path = set(self._path or ())
        for key, position in positions.items():
            item = NodeItem(self.graph.nodes[key], self.translations,
                            target=key == self.graph.target,
                            hidden=self.graph.hidden_locations(key, hidden))
            item.setPos(position)
            item.set_highlight(key in on_path)
            self.scene.addItem(item)
            self._items[key] = item
            if key == selected_key:
                item.setSelected(True)
        labels = {}
        for edge in self.graph.edges:
            if edge.holder in positions and edge.held in positions:
                labels.setdefault((edge.holder, edge.held), []).append(edge.label)
        path_edges = set(zip(self._path or (), (self._path or ())[1:]))
        # The labels placed so far; the edges of the way to a root first
        taken = []
        order = sorted(labels.items(), key=lambda pair: pair[0] not in path_edges)
        for (holder, held), names in order:
            self._draw_edge(positions[holder], positions[held],
                            ", ".join(name for name in names if name),
                            (holder, held) in path_edges, taken)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        if first:
            self.view.centerOn(self._items[self.graph.target])
        self._update_buttons()

    def _draw_edge(self, start, end, label, highlight, taken):
        """An arrow from the holder (top left corner ``start``) to the held
        node (``end``), its label where ``edge_route`` puts it, or moved up
        or down off the labels already ``taken`` (their QRectFs)."""
        route = edge_route(start, end)
        color = theme_qcolor("orange" if highlight else "muted")
        line = QGraphicsPathItem(route.path)
        line.setPen(QPen(color, 2 if highlight else 1.2))
        line.setZValue(-1)
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
        """A double click: unfold a node's holders, or inspect it once unfolded."""
        if item.node.can_expand:
            self.expand(item.node.key)
        else:
            self.inspect(item.node)

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
        if item is None:
            return
        node = item.node
        t = self.translations
        menu = QMenu(self)
        clipboard = QGuiApplication.clipboard()
        if node.can_expand:
            menu.addAction(t["references.expand"], lambda: self.expand(node.key))
        if node.target() is not None:
            menu.addAction(t["references.inspect"], lambda: self.inspect(node))
        menu.addAction(t["references.copy_address"],
                       lambda: clipboard.setText(f"0x{node.address:x}"))
        if node.kind in (STATIC, MODULE):
            text = node.name if node.kind == MODULE else f"{node.class_name}.{node.name}"
            menu.addAction(t["references.copy_name"], lambda: clipboard.setText(text))
        menu.exec(self.view.viewport().mapToGlobal(position))

    def _update_buttons(self):
        if not hasattr(self, "path_button"):
            return
        selected = self._selected()
        ready = self.index is not None
        self.path_button.setEnabled(ready and not self._searching)
        self.inspect_button.setEnabled(selected is not None and selected.target() is not None)
        self.refresh_button.setEnabled(self._index_worker is None)

    def closeEvent(self, event):  # pylint: disable=invalid-name
        self._forget_settings()
        if self._index_worker is not None:
            self._index_worker.requestInterruption()
        super().closeEvent(event)
