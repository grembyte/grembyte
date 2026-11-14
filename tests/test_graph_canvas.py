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
    # One column left: right side to left side, labelled in the gap between
    forward = edge_route(QPointF(-2 * step, 0), column)
    assert forward.direction == 1 and forward.tip == QPointF(-step, NODE_HEIGHT / 2)
    assert -2 * step + NODE_WIDTH < forward.label.x() < -step and forward.align == "center"
    # The same column: a loop into the gap left of it, labelled at its far end
    loop = edge_route(QPointF(-step, 200), column)
    assert loop.direction == 1 and loop.tip == QPointF(-step, NODE_HEIGHT / 2)
    box = loop.path.boundingRect()
    assert box.right() <= -step and box.left() >= -step - COLUMN_GAP / 2
    assert loop.align == "right" and loop.label.x() < box.left() + 1
    # Backwards: left side to the held node's right side, pointing left
    back = edge_route(column, QPointF(-2 * step, 0))
    assert back.direction == -1 and back.tip == QPointF(-2 * step + NODE_WIDTH, NODE_HEIGHT / 2)
    assert back.path.boundingRect().right() <= -step
    far = edge_route(QPointF(0, 0), QPointF(-3 * step, 0))
    assert far.align == "left" and far.label.x() > -3 * step + NODE_WIDTH


def test_labels_move_off_each_other(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QRectF  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.graph_canvas import free_place

    taken = [QRectF(0, 0, 50, 10)]
    assert free_place(QRectF(100, 0, 50, 10), taken) == QRectF(100, 0, 50, 10)
    # Down first, then up, by its height and a pixel
    assert free_place(QRectF(10, 2, 50, 10), taken) == QRectF(10, 13, 50, 10)
    # One step up still covers the first: two steps down
    taken.append(QRectF(0, 13, 50, 10))
    assert free_place(QRectF(10, 2, 50, 10), taken) == QRectF(10, 24, 50, 10)
    # Up when down is taken as far
    taken.append(QRectF(0, 24, 50, 10))
    assert free_place(QRectF(10, 2, 50, 10), taken) == QRectF(10, -20, 50, 10)


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
