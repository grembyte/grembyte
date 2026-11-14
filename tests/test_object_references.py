"""The reference graph (grembyte.features.object_references and its window)."""

from types import SimpleNamespace

import numpy as np

from grembyte.core import settings
from grembyte.core.memory_regions import HEAP, MAIN, STACK as STACK_REGION, Region
from grembyte.features.object_references import (
    BY_USER,
    FILTERED,
    HOLDERS,
    MODULE,
    OBJECT,
    STACK,
    STATIC,
    TARGETS,
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


def _graph_with_two_ways():
    """The target held by the module's data (a root) and by the loader, which a
    static field holds: two ways to a root."""
    graph = ReferenceGraph.for_object(TARGET, "Game.GameManager")
    target = graph.target
    module = graph.node_for(target, 0x1008, place=(MODULE, "game.so+0x8"))
    loader = graph.node_for(target, 0x10208, entry=_loader_entry())
    graph.add_holders(target, [(module, ""), (loader, "manager")], 2)
    boot = graph.node_for(loader.key, 0x7000, entry={
        "location": "0x7000", "kind": "static", "klass": "0x98", "className": "Game.Boot",
        "field": "Loader"})
    graph.add_holders(loader.key, [(boot, "Loader")], 1)
    return graph, module, loader, boot


def test_hidden_objects_and_classes_are_left_out_and_gone_around():
    graph, module, loader, boot = _graph_with_two_ways()
    target = graph.target
    assert graph.path_from(boot.key) == [boot.key, loader.key, target]
    assert graph.path_from(module.key) == [module.key, target]
    assert graph.hidden_by_user() == 0

    # One object hidden: it and what only it leads to are left out
    graph.hidden_keys.add(loader.key)
    assert graph.depths() == {target: 0, module.key: 1}
    assert graph.hidden_locations(target, (), BY_USER) == 1
    assert graph.hidden_locations(target, ()) == 0
    assert graph.path_from(boot.key) is None
    assert graph.hidden_by_user() == 1
    # The way to a root goes around it
    graph.hidden_keys.add(module.key)
    assert graph.path_to_root() is None
    graph.hidden_keys.clear()
    graph.hidden_keys.add(module.key)
    assert graph.path_to_root() == [boot.key, loader.key, target]

    # A whole class; the target is never hidden, whatever its class
    graph.hidden_keys.clear()
    graph.hidden_classes.update({"Game.Loader", "Game.GameManager"})
    assert set(graph.depths()) == {target, module.key}
    assert not graph.is_hidden(target)
    assert graph.hidden_by_user() == 1

    # Only the objects of some classes (singletons): roots stay, and the
    # holders of those classes come first when holders are many
    graph.hidden_classes.clear()
    graph.only_classes = {"0x77"}
    assert graph.hidden_reason(loader.key) == FILTERED
    assert graph.hidden_by_user() == 0
    assert graph.hidden_locations(target, (), FILTERED) == 1
    assert set(graph.depths()) == {target, module.key}
    other = graph.node_for(boot.key, 0x10708, entry={**_loader_entry(0x10708),
                                                     "object": "0x10700", "klass": "0x76"})
    single = graph.node_for(boot.key, 0x10808, entry={**_loader_entry(0x10808),
                                                      "object": "0x10800", "klass": "0x77"})
    graph.nodes[target].expanded = False
    graph.add_holders(target, [(other, "a"), (single, "b")], 2, limit=1)
    assert [edge.holder for edge in graph.holders_of(target)][-1] == single.key
    graph.only_classes = None
    assert graph.path_from(boot.key) == [boot.key, loader.key, target]


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


def _index_with_fields():
    """As _index, and the target's own fields: one points to the loader,
    one into other memory."""
    pointer_map = _PointerMap([(TARGET, 0x10208), (LOADER, 0x10108), (0x10900, 0x10110),
                               (0x10500, 0x10400)])
    return ReferenceIndex(pointer_map, [Region(0x10000, 0x20000, HEAP)])


def test_index_lists_the_pointers_inside_an_object():
    index = _index_with_fields()
    assert index.pointers_in(TARGET, TARGET + 0x20) == (
        [(0x10108, LOADER), (0x10110, 0x10900)], 2)
    assert index.pointers_in(TARGET, TARGET + 0x20, limit=1) == ([(0x10108, LOADER)], 2)
    assert index.pointers_in(0x10110, 0x10111) == ([(0x10110, 0x10900)], 1)
    assert index.pointers_in(0x5000, 0x6000) == ([], 0)


def test_the_other_way_leads_from_an_object_to_what_it_points_to():
    graph = ReferenceGraph.for_object(TARGET, "Game.GameManager", direction=TARGETS)
    target = graph.target
    loader = graph.node_for(target, LOADER, entry={**_loader_entry(), "size": 0x40})
    assert loader.size == 0x40
    graph.add_holders(target, [(loader, "loader")], 2)
    boot = graph.node_for(loader.key, 0x10700, entry={
        **_loader_entry(0x10700), "object": "0x10700", "className": "Game.Boot"})
    graph.add_holders(loader.key, [(boot, "boot")], 1)
    # The edges run from the object to what it points to
    assert [(edge.holder, edge.held, edge.label) for edge in graph.edges] == [
        (target, loader.key, "loader"), (loader.key, boot.key, "boot")]
    assert graph.depths() == {target: 0, loader.key: 1, boot.key: 2}
    assert graph.leading(boot.key) == [loader.key]
    path = graph.path_from(boot.key)
    assert path == [boot.key, loader.key, target]
    assert graph.path_edges(path) == [(loader.key, boot.key), (target, loader.key)]
    # No roots this way
    assert graph.path_to_root() is None


def _menu_texts(dialog, node=None):
    """The texts of the window's context menu on ``node`` (None: the background)."""
    # pylint: disable=import-outside-toplevel
    from PySide6.QtWidgets import QMenu  # pylint: disable=import-error,no-name-in-module

    menu = QMenu()
    dialog.fill_menu(menu, node)
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def test_window_hides_highlights_and_exports_the_graph(qt_app, monkeypatch, tmp_path):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access,too-many-locals
    import sys

    import shiboken6  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtCore import QCoreApplication, QEvent  # pylint: disable=import-error,no-name-in-module
    from PySide6.QtWidgets import QFileDialog  # pylint: disable=import-error,no-name-in-module

    from grembyte.ui.widgets import reference_graph, scripts
    from grembyte.ui.widgets.icons import theme_color

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(reference_graph, "ReferenceIndexWorker", _FakeIndexWorker)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = reference_graph.ReferenceGraphDialog(
        translations, panel, 4242, TARGET, "Game.GameManager")
    try:
        graph, module, loader, boot = _graph_with_two_ways()
        graph.nodes[graph.target].name = "<Manager>"
        dialog.graph = dialog._graphs[graph.direction] = graph
        dialog.index = _index()
        dialog._redraw()
        assert set(dialog._items) == {graph.target, module.key, loader.key, boot.key}
        # Fit, show hidden, export: in the context menu, not the tool bar
        assert _menu_texts(dialog) == ["Einpassen", "Als HTML exportieren"]

        # The way from the static field to the object, in the theme's highlight colour
        dialog.highlight(boot.key)
        highlight = theme_color("highlight")
        for key in (boot.key, loader.key):
            assert dialog._items[key].pen().color().name() == highlight
        assert dialog._items[module.key].pen().color().name() != highlight
        lines = [item for item in dialog.scene.items()
                 if isinstance(item, reference_graph.QGraphicsPathItem)]
        assert sum(item.pen().color().name() == highlight for item in lines) == 2

        # Hiding the loader: gone with the static field only it leads to, and
        # the highlight through it with them, until they are shown again
        dialog.hide_node(loader.key)
        assert set(dialog._items) == {graph.target, module.key}
        assert all(item.pen().color().name() != highlight for item in dialog._items.values())
        assert dialog._items[graph.target].toolTip().endswith("1 von dir ausgeblendet")
        assert "Ausgeblendete zeigen (1)" in _menu_texts(dialog)
        dialog.show_hidden()
        assert len(dialog._items) == 4
        assert dialog._items[boot.key].pen().color().name() == highlight
        dialog.highlight(None)
        assert all(item.pen().color().name() != highlight for item in dialog._items.values())
        dialog.hide_class("Game.Loader")
        assert set(dialog._items) == {graph.target, module.key}
        # Hidden stays hidden when the memory is read again
        dialog.read_memory()
        assert dialog.graph.hidden_classes == {"Game.Loader"}
        dialog.graph = dialog._graphs[graph.direction] = graph
        dialog.show_hidden()

        # Exported as drawn: an SVG of the boxes, the game's names escaped
        dialog.highlight(boot.key)
        path = tmp_path / "graph"
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            lambda *args, **kwargs: (str(path), ""))
        dialog.export_html()
        page = (tmp_path / "graph.html").read_text(encoding="utf-8")
        assert dialog.status_label.text() == f"Gespeichert unter {path}.html."
        assert page.startswith("<!DOCTYPE html>") and "<script" not in page
        assert "<svg " in page and page.count("<rect ") >= 4
        assert "&quot;&lt;Manager&gt;&quot;" in page and "<Manager>" not in page
        assert f'stroke="{highlight}"' in page
        assert ">Boot.Loader<" in page
        assert "<title>Ein statisches Feld einer Klasse" in page  # its tooltip

        # Only singletons: the classes are listed once, then the other
        # objects are left out (roots stay) and counted in the tooltip
        dialog.highlight(None)
        dialog.singletons_check.setChecked(True)
        worker = panel._worker
        assert worker.requests[-1] == ("browse_classes", "", False)
        assert dialog.status_label.text() == "Lese die Klassen …"
        worker.classes_browsed.emit(len(worker.requests), {"objects": 9, "classes": [
            {"klass": "0x99", "name": "Loader", "package": "Game", "count": 2},
            {"klass": "0x55", "name": "Boot", "package": "Game", "count": 1}]})
        assert dialog.status_label.text() == "1 Singleton-Klassen."
        assert set(dialog._items) == {graph.target, module.key}
        assert dialog._items[graph.target].toolTip().endswith("1 keine Singletons, ausgeblendet")
        dialog.singletons_check.setChecked(False)
        assert len(dialog._items) == 4
        requests = len(worker.requests)
        dialog.singletons_check.setChecked(True)  # known: not asked again
        assert len(worker.requests) == requests and len(dialog._items) == 2
        dialog.singletons_check.setChecked(False)

        # Closed (and deleted) while the agent still names locations: the
        # answer reaches it no more
        dialog.expand(graph.target)
        graph.nodes[graph.target].expanded = False
        dialog.expand(graph.target)
        worker = panel._worker
        dialog.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(dialog)
        worker.references_resolved.emit(len(worker.requests), [_loader_entry()])
        worker.references_failed.emit(len(worker.requests), "gone")
        qt_app.processEvents()
        assert not errors, errors
    finally:
        if shiboken6.isValid(dialog):
            dialog.close()
        panel.set_process(None)


def test_window_switches_to_what_the_object_points_to(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import reference_graph, scripts
    from grembyte.ui.widgets.icons import theme_color

    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(reference_graph, "ReferenceIndexWorker", _FakeIndexWorker)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = reference_graph.ReferenceGraphDialog(
        translations, panel, 4242, TARGET, "Game.GameManager")
    try:
        dialog._index_worker.ready.emit(_index_with_fields())
        dialog._index_worker.finished.emit()
        worker = panel._worker
        holders = dialog.graph
        assert holders.direction == HOLDERS and worker.requests[-1][1] == [0x10208]
        assert not dialog.path_button.isHidden()

        # The other way: the object's size first, then its pointers named in one go
        dialog.set_direction(TARGETS)
        assert dialog.direction_buttons[TARGETS].isChecked()
        assert dialog.path_button.isHidden()
        assert worker.requests[-1][1] == [TARGET]
        worker.references_resolved.emit(len(worker.requests), [
            {"location": hex(TARGET), "kind": "field", "object": hex(TARGET), "size": 0x20,
             "klass": "0x98", "className": "Game.GameManager", "field": None}])
        assert worker.requests[-1][1] == [0x10108, 0x10110, LOADER, 0x10900]
        own = {"kind": "field", "object": hex(TARGET), "klass": "0x98",
               "className": "Game.GameManager", "size": 0x20}
        worker.references_resolved.emit(len(worker.requests), [
            {**own, "field": "loader"}, {**own, "field": "cache"},
            {**_loader_entry(LOADER), "field": "+0x0", "size": 0x40}, {"kind": "unknown"}])
        targets = dialog.graph
        loader = f"object:{LOADER:x}"
        assert targets.direction == TARGETS
        assert [(edge.holder, edge.held, edge.label) for edge in targets.edges] == [
            (targets.target, loader, "loader")]
        assert dialog.status_label.text() == "1 von 2 Zeigern im Objekt führen zu Objekten."
        # What it points to stands right of it
        assert dialog._items[loader].pos().x() > dialog._items[targets.target].pos().x()
        assert "Doppelklick: suchen, worauf es zeigt" in dialog._items[loader].toolTip()
        assert {"Suchen, worauf es zeigt", "Weg vom Objekt hervorheben"} <= set(
            _menu_texts(dialog, targets.nodes[loader]))
        dialog.highlight(loader)
        assert dialog._items[loader].pen().color().name() == theme_color("highlight")

        # A double click unfolds it the same way; its size is known already
        dialog._activated(dialog._items[loader])
        assert worker.requests[-1][1] == [0x10208, TARGET]
        # Back: the holders as they were, not asked again
        requests = len(worker.requests)
        dialog.set_direction(HOLDERS)
        assert dialog.graph is holders and len(worker.requests) == requests
    finally:
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
