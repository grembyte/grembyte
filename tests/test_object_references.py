"""The reference graph (grembyte.features.object_references and its window)."""

from types import SimpleNamespace

import numpy as np

from grembyte.core import settings
from grembyte.core.memory_regions import HEAP, MAIN, STACK as STACK_REGION, Region
from grembyte.features.object_references import (
    MODULE,
    OBJECT,
    STACK,
    STATIC,
    UNKNOWN,
    ReferenceGraph,
    ReferenceIndex,
)
from grembyte.ui.localization import load_translations

from tests.test_scripts import _FakeScriptWorker, fixture_qt_app  # pylint: disable=unused-import

TARGET = 0x10100
LOADER = 0x10200


class _PointerMap:
    """Stands in for PointerMap: values sorted, their locations."""

    def __init__(self, pairs):
        pairs = sorted(pairs, key=lambda pair: pair[0])
        self.values = np.array([value for value, _location in pairs], dtype=np.uint64)
        self.locations = np.array([location for _value, location in pairs], dtype=np.uint64)
        self.mappings = [SimpleNamespace(module="game.so", start=0x1000, end=0x2000,
                                         writable=True)]

    def __len__(self):
        return len(self.values)


def _index():
    # The target is held in the module's data, twice on the heap and on a
    # stack; something else elsewhere on the heap.
    pointer_map = _PointerMap([(TARGET, 0x10208), (TARGET, 0x1008), (TARGET, 0x30010),
                               (TARGET, 0x10308), (0x10500, 0x10400), (LOADER, 0x10600)])
    regions = [Region(0x1000, 0x2000, MAIN, "game.so"), Region(0x10000, 0x20000, HEAP),
               Region(0x30000, 0x31000, STACK_REGION)]
    return ReferenceIndex(pointer_map, regions)


def test_index_finds_and_places_the_locations_holding_an_address():
    index = _index()
    assert index.referrers(TARGET) == ([0x1008, 0x10208, 0x10308, 0x30010], 4)
    assert index.referrers(TARGET, limit=2) == ([0x1008, 0x10208], 4)
    assert index.referrers(0x4242) == ([], 0)
    assert index.place(0x1008) == (MODULE, "game.so+0x8")
    assert index.place(0x10208) == (None, None)
    assert index.place(0x30010) == (STACK, None)
    assert index.place(0x500) == (None, None)  # below every region
    assert index.place(0x2500) == (None, None)  # between regions


def _loader_entry(location=0x10208):
    return {"location": hex(location), "kind": "field", "object": hex(LOADER),
            "klass": "0x99", "className": "Game.Loader", "name": None, "field": "manager"}


def test_graph_records_holders_and_finds_the_way_to_a_root():
    graph = ReferenceGraph.for_object(TARGET, "Game.GameManager")
    target = graph.target
    loader = graph.node_for(target, 0x10208, entry=_loader_entry())
    unknown = graph.node_for(target, 0x10308, entry={"kind": "unknown"})
    graph.node_for(target, 0x10388, entry={"kind": "unknown"})
    graph.add_holders(target, [(loader, "manager"), (unknown, "")], 3)
    assert graph.nodes[target].expanded and graph.nodes[target].total == 3
    assert loader.kind == OBJECT and loader.can_expand and loader.target() == {"address": "0x10200"}
    # The unknown locations of one object share a node
    assert unknown.kind == UNKNOWN and unknown.locations == [0x10308, 0x10388]
    assert graph.path_to_root() is None
    assert graph.next_to_expand() == loader.key

    # The loader is held by a static field: the way from there
    boot = graph.node_for(loader.key, 0x7000, entry={
        "location": "0x7000", "kind": "static", "klass": "0x98", "className": "Game.Boot",
        "field": "Loader"})
    graph.add_holders(loader.key, [(boot, "Loader"), (loader, "self")], 1)
    assert boot.kind == STATIC and boot.is_root and boot.target() is None
    assert graph.depths() == {target: 0, loader.key: 1, unknown.key: 1, boot.key: 2}
    assert graph.depths((UNKNOWN,)) == {target: 0, loader.key: 1, boot.key: 2}
    assert graph.hidden_locations(target, (UNKNOWN,)) == 2
    assert graph.hidden_locations(target, ()) == 0
    assert graph.path_to_root() == [boot.key, loader.key, target]
    assert graph.next_to_expand() is None
    # Holding itself draws no edge; the same edge is kept once
    graph.add_holders(loader.key, [(boot, "Loader")], 1)
    assert [(edge.holder, edge.held) for edge in graph.edges] == [
        (loader.key, target), (unknown.key, target), (boot.key, loader.key)]


def test_roots_and_objects_come_first_when_holders_are_many():
    # As the GameManager in Vampire Survivors: held 1349 times, mostly from
    # native memory at lower addresses than the objects holding it
    graph = ReferenceGraph.for_object(TARGET, "Game.GameManager")
    target = graph.target
    holders = [(graph.node_for(target, location, entry={"kind": "unknown"}), "")
               for location in range(0x1000, 0x1000 + 8 * 150, 8)]
    holders.append((graph.node_for(target, 0x10208, entry=_loader_entry()), "manager"))
    holders.append((graph.node_for(target, 0x7000, entry={
        "kind": "static", "klass": "0x98", "className": "Game.Boot", "field": "Manager"}), ""))
    graph.add_holders(target, holders, 1349, limit=2)
    assert {edge.holder for edge in graph.edges} == {"static:Game.Boot.Manager",
                                                     f"object:{LOADER:x}"}
    # The one left out is forgotten; shown counts the locations kept
    assert f"unknown:{target}" not in graph.nodes
    assert (graph.nodes[target].shown, graph.nodes[target].total) == (2, 1349)
    # With room for it, the unknown memory is one node for all 150
    graph = ReferenceGraph.for_object(TARGET)
    holders = [(graph.node_for(graph.target, location, entry={"kind": "unknown"}), "")
               for location in range(0x1000, 0x1000 + 8 * 150, 8)]
    graph.add_holders(graph.target, holders, 150)
    assert len(graph.nodes[f"unknown:{graph.target}"].locations) == 150
    assert graph.nodes[graph.target].shown == 150


def test_layout_puts_holders_left_of_what_they_hold(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from grembyte.ui.widgets.reference_graph import layout_positions, short_class

    graph = ReferenceGraph.for_object(TARGET, "Game.GameManager")
    first = graph.node_for(graph.target, 0x10208, entry=_loader_entry())
    second = graph.node_for(graph.target, 0x1008, place=(MODULE, "game.so+0x8"))
    graph.add_holders(graph.target, [(first, "manager"), (second, "")], 2)
    positions = layout_positions(graph)
    assert positions[graph.target].x() == 0
    assert positions[first.key].x() == positions[second.key].x() < 0
    assert positions[first.key].y() != positions[second.key].y()
    assert short_class("Game.Systems.Singleton<Game.GameManager>") == \
        "Singleton<Game.GameManager>"


def test_edges_never_run_through_a_column(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtCore import QPointF  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.reference_graph import (
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

    from grembyte.ui.widgets.reference_graph import free_place

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


class _FakeIndexWorker:
    """Stands in for ReferenceIndexWorker: the test hands the index over."""

    def __init__(self, pid, main_module=None, parent=None):
        # pylint: disable=import-outside-toplevel,unused-argument
        from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

        class Signals(QObject):  # pylint: disable=too-few-public-methods
            progress = Signal(int)
            ready = Signal(object)
            failed = Signal(str)
            finished = Signal()

        self.signals = Signals()
        self.progress = self.signals.progress
        self.ready = self.signals.ready
        self.failed = self.signals.failed
        self.finished = self.signals.finished
        self.pid = pid

    def start(self):
        pass

    def requestInterruption(self):  # pylint: disable=invalid-name
        pass

    def isRunning(self):  # pylint: disable=invalid-name
        return False

    def wait(self, _timeout=None):
        return True


def test_window_unfolds_holders_and_marks_the_way_to_a_root(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import reference_graph, scripts

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(reference_graph, "ReferenceIndexWorker", _FakeIndexWorker)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = reference_graph.ReferenceGraphDialog(
        translations, panel, 4242, TARGET, "Game.GameManager", framework=("mscorlib",))
    try:
        assert dialog.windowTitle() == "Verweise auf GameManager 0x10100"
        assert dialog.status_label.text() == "Lese den Speicher des Spiels …"
        assert not dialog.path_button.isEnabled()
        dialog._index_worker.ready.emit(_index())
        dialog._index_worker.finished.emit()

        # The roots are placed at once, the heap locations named by the agent
        worker = panel._worker
        assert worker.requests[-1] == ("resolve_references", [0x10208, 0x10308], ("mscorlib",))
        worker.references_resolved.emit(len(worker.requests), [
            _loader_entry(), {"location": "0x10308", "kind": "unknown"}])
        kinds = sorted(node.kind for node in dialog.graph.nodes.values())
        assert kinds == [MODULE, OBJECT, OBJECT, STACK, UNKNOWN]
        assert dialog.status_label.text() == "4 von 4 Stellen, die das Objekt halten."
        # Other memory only counts, unless the settings show it (at once)
        assert len(dialog._items) == 4
        assert dialog._items[dialog.graph.target].toolTip().endswith(
            "1 in anderem Speicher, ausgeblendet (Einstellungen → Objekt-Browser)")
        settings.apply({"reference_other_memory": True})
        assert len(dialog._items) == 5
        settings.apply({"reference_other_memory": False})
        assert len(dialog._items) == 4

        # The module's data is a root: the way is one step
        dialog.find_root()
        assert dialog._path == ["module:game.so+0x8", dialog.graph.target]
        assert dialog.status_label.text() == "Weg zur Wurzel gefunden, Länge 1."

        # A double click unfolds an object; once unfolded it opens in the inspector
        loader = dialog._items[f"object:{LOADER:x}"]
        dialog._activated(loader)
        assert worker.requests[-1] == ("resolve_references", [0x10600], ("mscorlib",))
        worker.references_resolved.emit(len(worker.requests), [
            {"location": "0x10600", "kind": "static", "klass": "0x98",
             "className": "Game.Boot", "field": "Loader"}])
        assert dialog.graph.nodes["static:Game.Boot.Loader"].is_root
        opened = []
        monkeypatch.setattr(panel.inspections, "inspect",
                            lambda target, name="", instance_name=None: opened.append(
                                (target, name, instance_name)))
        dialog._activated(dialog._items[f"object:{LOADER:x}"])
        assert opened == [({"address": "0x10200"}, "Loader", None)]

        # Many locations are named in batches, the holders shown once all are back
        monkeypatch.setattr(reference_graph, "RESOLVE_BATCH", 1)
        boot = dialog.graph.nodes["static:Game.Boot.Loader"]
        dialog.graph.nodes[dialog.graph.target].expanded = False
        dialog.graph.edges.clear()
        dialog.expand(dialog.graph.target)
        first, second = worker.requests[-2:]
        assert (first[1], second[1]) == ([0x10208], [0x10308])
        worker.references_resolved.emit(len(worker.requests) - 1, [_loader_entry()])
        assert not dialog.graph.edges
        worker.references_resolved.emit(len(worker.requests), [{"kind": "unknown"}])
        assert len(dialog.graph.holders_of(dialog.graph.target)) == 4
        assert boot.key in dialog.graph.nodes

        # Reading again starts from the object alone
        dialog.read_memory()
        assert list(dialog.graph.nodes) == [dialog.graph.target]
    finally:
        settings.apply({"reference_other_memory": False})
        dialog.close()
        panel.set_process(None)


def test_inspector_offers_references_only_where_a_window_opens_them(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PySide6.QtWidgets import QWidget  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets.reference_graph import find_reference_opener

    class Main(QWidget):
        """Stands in for the main window."""

        def __init__(self, can):
            super().__init__()
            self.can = can
            self.opened = []

        def can_open_reference_graph(self):
            return self.can

        def open_reference_graph(self, address, class_name="", name=None):
            self.opened.append((address, class_name, name))

    main = Main(True)
    child = QWidget(main)
    opener = find_reference_opener(child)
    opener(0x10, "Game.Loader", None)
    assert main.opened == [(0x10, "Game.Loader", None)]
    main.can = False
    assert find_reference_opener(child) is None
    assert find_reference_opener(QWidget()) is None
