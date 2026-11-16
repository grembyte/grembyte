"""What the graph windows share: the view, the node boxes, the column layout
and how edges and their labels are drawn.

The reference graph (grembyte.ui.widgets.reference_graph) and the call graph
(grembyte.ui.widgets.call_graph) build on it. A graph stands in columns: the
node a window starts from in column 0, each step away from it a column
further out, to the left (negative columns) or to the right. Edges run from
the node on the left to the one on the right of a step, and around columns
where they go back or stay in one. An edge's label (the field, how a method
is called) stands above the node of the edge further out (see caption_key),
not on the line: where many lines run close, a label on them could not be
told apart.
"""

from dataclasses import dataclass

from PySide6.QtCore import QPointF, Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtGui import (  # pylint: disable=import-error,no-name-in-module
    QBrush,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from grembyte.ui.widgets.icons import theme_qcolor

# Layout of a graph, in scene pixels
NODE_WIDTH = 230
NODE_HEIGHT = 46
COLUMN_GAP = 150
ROW_GAP = 30
ZOOM_STEP = 1.15
# Text of a node line at most, in characters
MAX_LINE = 34
# Where a node's caption (the labels of its edges) stands above it
CAPTION_TOP = -20


def short_class(name):
    """A class name without its namespace (generic arguments kept)."""
    head, bracket, rest = (name or "?").partition("<")
    return head.rsplit(".", 1)[-1] + bracket + rest


def clip(text, limit=MAX_LINE):
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def column_positions(columns, leading, in_order=False):
    """``{key: QPointF}`` of the nodes' top left corners.

    ``columns`` is ``{key: column}`` (0 for the node the graph starts from,
    negative to its left); ``leading(key)`` the keys of the nodes one step
    nearer to column 0 that lead to ``key``. In a column the nodes follow
    the order of the nodes they come from, which keeps edges from crossing,
    then by key, or with ``in_order`` in the order of ``columns``; the
    columns are centred on the start node's row.
    """
    order = {key: number for number, key in enumerate(columns)}
    by_column = {}
    for key, column in columns.items():
        by_column.setdefault(column, []).append(key)
    row_of = {}
    positions = {}
    for column in sorted(by_column, key=lambda number: (abs(number), number)):
        keys = by_column[column]
        if column != 0:
            nearer = column - (1 if column > 0 else -1)

            def place(key, nearer=nearer):
                rows = [row_of[other] for other in leading(key)
                        if columns.get(other) == nearer and other in row_of]
                return (min(rows) if rows else 0.0, order[key] if in_order else key)
            keys.sort(key=place)
        middle = (len(keys) - 1) / 2
        for row, key in enumerate(keys):
            row_of[key] = row
            positions[key] = QPointF(column * (NODE_WIDTH + COLUMN_GAP),
                                     (row - middle) * (NODE_HEIGHT + ROW_GAP))
    return positions


@dataclass
class EdgeRoute:
    """How an edge is drawn: its line, the arrow's tip and which way it points
    (1: right, -1: left)."""

    path: QPainterPath
    tip: QPointF
    direction: int


def edge_route(start, end):
    """The EdgeRoute from one node to another (their top left corners).

    Usually the edge leads one column to the right: it runs from the first
    node's right side to the other's left side.
    It can also stay in the same column (an array and an object both holding
    the target, the array also holding the object) or lead back to the left:
    those edges never cross the nodes of a column. The same column's loop out
    to the left, into the gap before the column; one running backwards goes
    from the first node's left side to the other's right side.
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
        return EdgeRoute(path, tip, 1)
    else:
        source, tip, direction = left_mid(start), right_mid(end), -1
    middle = (source.x() + tip.x()) / 2
    path = QPainterPath(source)
    path.cubicTo(QPointF(middle, source.y()), QPointF(middle, tip.y()), tip)
    return EdgeRoute(path, tip, direction)


def caption_key(start, end, positions):
    """Of the edge from node ``start`` to ``end`` (keys of ``positions``,
    their top left corners), the node its label stands above: the one
    further from the node the graph starts from (in column 0), which a
    graph reaches by that edge; of two in one column the one it leads to."""
    return start if abs(positions[start].x()) > abs(positions[end].x()) else end


def captions(edges, positions):
    """``{node key: [label, …]}`` of ``edges`` [(start, end, label)]: each
    label above its edge's caption_key, empty ones and repeats left out."""
    found = {}
    for start, end, label in edges:
        texts = found.setdefault(caption_key(start, end, positions), [])
        if label and label not in texts:
            texts.append(label)
    return {key: texts for key, texts in found.items() if texts}


def left_mid(corner):
    """The middle of the left side of the node with top left corner ``corner``."""
    return QPointF(corner.x(), corner.y() + NODE_HEIGHT / 2)


def right_mid(corner):
    """The middle of the right side of the node with top left corner ``corner``."""
    return QPointF(corner.x() + NODE_WIDTH, corner.y() + NODE_HEIGHT / 2)


def draw_edge(scene, start, end, color, marked):
    """An arrow from the node with top left corner ``start`` to the one at
    ``end`` in ``color`` (a QColor), drawn wider and above the others when
    ``marked``. Its label is the caption of a node (NodeBox.set_caption)."""
    route = edge_route(start, end)
    line = QGraphicsPathItem(route.path)
    line.setPen(QPen(color, 2 if marked else 1.2))
    # Marked ways above the others where lines cross
    line.setZValue(-1 if marked else -2)
    scene.addItem(line)
    tip, back = route.tip, -9 * route.direction
    arrow = QGraphicsPolygonItem(QPolygonF([
        tip, QPointF(tip.x() + back, tip.y() - 5), QPointF(tip.x() + back, tip.y() + 5)]))
    arrow.setBrush(QBrush(color))
    arrow.setPen(QPen(color))
    scene.addItem(arrow)


class NodeBox(QGraphicsRectItem):
    """One node of a graph: a box with two lines of text.

    ``node`` is what the graph keeps of it; ``expand_left`` and
    ``expand_right`` put a "+" on that side (there is more to unfold that
    way); ``more`` a count in the bottom right corner (more than is shown).
    Subclasses say how its border looks (``border``) and call
    ``set_highlight(None)`` once their own attributes are set.
    """

    def __init__(self, node, first, second, tooltip, expand_left=False, expand_right=False,
                 more=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(0, 0, NODE_WIDTH, NODE_HEIGHT)
        self.node = node
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setToolTip(tooltip)
        self.setBrush(QBrush(theme_qcolor("surface")))
        title = QGraphicsSimpleTextItem(clip(first), self)
        font = QFont(title.font())
        font.setBold(True)
        title.setFont(font)
        title.setBrush(QBrush(theme_qcolor("text")))
        title.setPos(10, 5)
        detail = QGraphicsSimpleTextItem(clip(second), self)
        detail.setBrush(QBrush(theme_qcolor("muted")))
        detail.setPos(10, 25)
        for wanted, left in ((expand_left, -14), (expand_right, NODE_WIDTH + 5)):
            if wanted:
                marker = QGraphicsSimpleTextItem("+", self)
                marker.setFont(font)
                marker.setBrush(QBrush(theme_qcolor("accent")))
                marker.setPos(left, NODE_HEIGHT / 2 - 10)
        if more:
            # In the bottom right corner (an address is short there), clear
            # of the arrows coming in on the left
            count = QGraphicsSimpleTextItem(f"+{more}", self)
            count.setBrush(QBrush(theme_qcolor("muted")))
            count.setPos(NODE_WIDTH - 8 - count.boundingRect().width(), 25)
        # The subclass's border needs its own attributes: it calls set_highlight

    def set_caption(self, labels, color):
        """Show ``labels`` (of the edges leading here, see captions) above
        the box in ``color`` (a QColor), clipped to its width."""
        text = ", ".join(labels)
        caption = QGraphicsSimpleTextItem(clip(text), self)
        caption.setBrush(QBrush(color))
        caption.setToolTip(text)
        caption.setPos(4, CAPTION_TOP)

    def border(self, mark):
        """``(QColor, width, Qt.PenStyle)`` of the border, for ``mark``: the
        name of the theme colour of a marked way it lies on, or None."""
        if mark is not None:
            return theme_qcolor(mark), 2.5, Qt.PenStyle.SolidLine
        return theme_qcolor("border"), 1.2, Qt.PenStyle.SolidLine

    def set_highlight(self, mark):
        """Draw the node as part of a marked way (``mark``, see ``border``) or,
        with None, as usual."""
        color, width, style = self.border(mark)
        pen = QPen(color, width)
        pen.setStyle(style)
        self.setPen(pen)


class GraphView(QGraphicsView):
    """The scene's view: drag to move, Ctrl+wheel to zoom."""

    # the NodeBox double-clicked
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
        """The NodeBox at ``position`` (viewport coordinates), or None."""
        item = self.itemAt(position)
        while item is not None and not isinstance(item, NodeBox):
            item = item.parentItem()
        return item

    def mouseDoubleClickEvent(self, event):  # pylint: disable=invalid-name
        item = self.node_at(event.position().toPoint())
        if item is None:
            super().mouseDoubleClickEvent(event)
            return
        self.node_activated.emit(item)
        event.accept()

    def fit(self):
        """Zoom so that the whole graph fits."""
        self.fitInView(self.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
