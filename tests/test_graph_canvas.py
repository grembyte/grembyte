"""What the graph windows share (see grembyte.ui.widgets.graph_canvas)."""

from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import


def test_edges_never_run_through_a_column(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QPointF  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.graph_canvas import (
        COLUMN_GAP,
        NODE_HEIGHT,
        NODE_WIDTH,
        edge_route,
    )

    step = NODE_WIDTH + COLUMN_GAP
    column = QPointF(-step, 0)
    # One column left: right side to left side
    forward = edge_route(QPointF(-2 * step, 0), column)
    assert forward.direction == 1 and forward.tip == QPointF(-step, NODE_HEIGHT / 2)
    # The same column: a loop into the gap left of it
    loop = edge_route(QPointF(-step, 200), column)
    assert loop.direction == 1 and loop.tip == QPointF(-step, NODE_HEIGHT / 2)
    box = loop.path.boundingRect()
    assert box.right() <= -step and box.left() >= -step - COLUMN_GAP / 2
    # Backwards: left side to the held node's right side, pointing left
    back = edge_route(column, QPointF(-2 * step, 0))
    assert back.direction == -1 and back.tip == QPointF(-2 * step + NODE_WIDTH, NODE_HEIGHT / 2)
    assert back.path.boundingRect().right() <= -step


def test_labels_stand_above_the_node_further_out(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QPointF  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.graph_canvas import NodeBox, captions

    positions = {"start": QPointF(0, 0), "left": QPointF(-380, 0), "left2": QPointF(-380, 70),
                 "far": QPointF(-760, 0), "right": QPointF(380, 0)}
    edges = [
        # Holders to the left: the field above the holder
        ("left", "start", "Player"), ("left", "start", "Owner"), ("left", "start", "Player"),
        ("far", "left", "Party"),
        # Called to the right: above the method called
        ("start", "right", "async"),
        # Backwards: above the node further out still
        ("start", "far", "Back"),
        # The same column: above the node it leads to; no label, no caption
        ("left", "left2", "Items"), ("left2", "start", ""),
    ]
    assert captions(edges, positions) == {
        "left": ["Player", "Owner"], "far": ["Party", "Back"], "right": ["async"],
        "left2": ["Items"]}
    box = NodeBox(None, "Party", "\"Hero\"", "")
    box.set_caption(["Player", "Owner"], box.brush().color())
    caption = box.childItems()[-1]
    assert caption.text() == "Player, Owner" and caption.toolTip() == "Player, Owner"
    # Above the box, in the gap to the row above
    assert caption.y() + caption.boundingRect().height() <= 0


def test_columns_follow_the_nodes_they_come_from(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.ui.widgets.graph_canvas import (
        COLUMN_GAP,
        NODE_WIDTH,
        column_positions,
    )

    # The start in the middle, two to its left, three to its right: each
    # column centred, the right one ordered by the nodes they come from
    columns = {"start": 0, "a": -1, "b": -1, "x": 1, "y": 1, "z": 2}
    leading = {"a": ["start"], "b": ["start"], "x": ["start"], "y": ["start"],
               "z": ["y"]}
    positions = column_positions(columns, lambda key: leading.get(key, []))
    step = NODE_WIDTH + COLUMN_GAP
    assert positions["start"].x() == 0 and positions["start"].y() == 0
    assert positions["a"].x() == positions["b"].x() == -step
    assert positions["x"].x() == step and positions["z"].x() == 2 * step
    assert positions["a"].y() == -positions["b"].y()
    assert positions["z"].y() == 0  # alone in its column
