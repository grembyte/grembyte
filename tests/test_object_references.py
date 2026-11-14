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
        # Reading the memory: the dots, and how far it got in the status line
        assert dialog.busy.is_busy()
        dialog._index_worker.progress.emit(42)
        assert dialog.status_label.text() == "Lese den Speicher des Spiels … 42 %"
        dialog._index_worker.ready.emit(_index())
        dialog._index_worker.finished.emit()

        # The roots are placed at once, the heap locations named by the agent
        worker = panel._worker
        assert worker.requests[-1] == ("resolve_references", [0x10208, 0x10308], ("mscorlib",))
        assert dialog.busy.is_busy()  # the agent names them
        worker.references_resolved.emit(len(worker.requests), [
            _loader_entry(), {"location": "0x10308", "kind": "unknown"}])
        assert not dialog.busy.is_busy()
        kinds = sorted(node.kind for node in dialog.graph.nodes.values())
        assert kinds == [MODULE, OBJECT, OBJECT, STACK, UNKNOWN]
        assert dialog.status_label.text() == "4 von 4 Stellen, die das Objekt halten."
        # Other memory only counts, unless the settings show it (at once)
        assert len(dialog._items) == 4
        assert dialog._items[dialog.graph.target].toolTip().endswith(
            "1 in anderem Speicher, ausgeblendet (Einstellungen → Graphen)\n"
            "Doppelklick: wieder einklappen")
        settings.apply({"reference_other_memory": True})
        assert len(dialog._items) == 5
        settings.apply({"reference_other_memory": False})
        assert len(dialog._items) == 4

        # The module's data is a root: the way is one step
        dialog.find_root()
        assert dialog._path == ["module:game.so+0x8", dialog.graph.target]
        assert dialog.status_label.text() == "Weg zur Wurzel gefunden, Länge 1."

        # A double click unfolds an object, a second one folds it in again
        loader_key = f"object:{LOADER:x}"
        boot_answer = [{"location": "0x10600", "kind": "static", "klass": "0x98",
                        "className": "Game.Boot", "field": "Loader"}]
        dialog._activated(dialog._items[loader_key])
        assert worker.requests[-1] == ("resolve_references", [0x10600], ("mscorlib",))
        worker.references_resolved.emit(len(worker.requests), boot_answer)
        assert dialog.graph.nodes["static:Game.Boot.Loader"].is_root
        assert "Einklappen" in _menu_texts(dialog, dialog.graph.nodes[loader_key])
        requests = len(worker.requests)
        dialog._activated(dialog._items[loader_key])
        assert len(worker.requests) == requests  # nothing asked
        assert "static:Game.Boot.Loader" not in dialog.graph.nodes
        assert "static:Game.Boot.Loader" not in dialog._items
        assert dialog.graph.nodes[loader_key].can_expand
        # What the target's own unfolding found stays
        assert len(dialog._items) == 4
        dialog._activated(dialog._items[loader_key])
        worker.references_resolved.emit(len(worker.requests), boot_answer)
        assert "static:Game.Boot.Loader" in dialog.graph.nodes
        # The inspector opens from the context menu
        opened = []
        monkeypatch.setattr(panel.inspections, "inspect",
                            lambda target, name="", instance_name=None: opened.append(
                                (target, name, instance_name)))
        dialog.inspect(dialog.graph.nodes[loader_key])
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
        assert dialog.busy.is_busy()  # until the last batch is back
        worker.references_resolved.emit(len(worker.requests), [{"kind": "unknown"}])
        assert not dialog.busy.is_busy()
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
    from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
        QFileDialog,
        QGraphicsPathItem,
    )

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
                 if isinstance(item, QGraphicsPathItem)]
        assert sum(item.pen().color().name() == highlight for item in lines) == 2

        # Hiding the loader: gone with the static field only it leads to, and
        # the highlight through it with them, until they are shown again
        dialog.hide_node(loader.key)
        assert set(dialog._items) == {graph.target, module.key}
        assert all(item.pen().color().name() != highlight for item in dialog._items.values())
        assert dialog._items[graph.target].toolTip().endswith(
            "1 von dir ausgeblendet\nDoppelklick: wieder einklappen")
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
        assert dialog._items[graph.target].toolTip().endswith(
            "1 keine Singletons, ausgeblendet\nDoppelklick: wieder einklappen")
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


def test_window_shows_what_the_static_fields_of_the_class_point_to(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import reference_graph, scripts

    # GameState keeps the player in a static field (Pillars of Eternity:
    # GameState.s_playerCharacter), not in the object; a second one is empty.
    player, data = 0x10c00, 0x10a00
    pointer_map = _PointerMap([(LOADER, 0x10108), (player, data)])
    index = ReferenceIndex(pointer_map, [Region(0x10000, 0x20000, HEAP)])
    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(reference_graph, "ReferenceIndexWorker", _FakeIndexWorker)
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = reference_graph.ReferenceGraphDialog(
        translations, panel, 4242, TARGET, "GameState")
    try:
        dialog._index_worker.ready.emit(index)
        dialog._index_worker.finished.emit()
        dialog.set_direction(TARGETS)
        worker = panel._worker  # nothing holds the object: asked first now
        assert worker.requests[-1][1] == [TARGET]
        own = {"location": hex(TARGET), "kind": "field", "object": hex(TARGET), "size": 0x20,
               "klass": "0x98", "className": "GameState", "field": None}
        worker.references_resolved.emit(len(worker.requests), [{**own, "statics": [
            ["s_playerCharacter", hex(data), "GameState"],
            ["m_stronghold", hex(data + 8), "GameState"]]}])
        # The field in the object and the static field's value, named in one go
        assert worker.requests[-1][1] == [0x10108, LOADER, player]
        worker.references_resolved.emit(len(worker.requests), [
            {**own, "field": "loader"},
            {**_loader_entry(LOADER), "size": 0x40},
            {"location": hex(player), "kind": "field", "object": hex(player), "size": 0x80,
             "klass": "0x9a", "className": "PlayerCharacter", "name": "Player", "field": None}])
        graph = dialog.graph
        static = "static:GameState.s_playerCharacter"
        # Drawn as the holders show it: the object, the static field's node, its object
        assert {(edge.holder, edge.held) for edge in graph.edges} == {
            (static, f"object:{player:x}"), (graph.target, static),
            (graph.target, f"object:{LOADER:x}")}
        assert graph.nodes[static].kind == STATIC
        assert dialog._items[static].toolTip().startswith(
            "Ein statisches Feld der Klasse des Objekts")
        assert graph.nodes[graph.target].total == 2
        assert "object:a" not in graph.nodes  # the empty static field leads nowhere
        assert (dialog._items[f"object:{player:x}"].pos().x()
                > dialog._items[static].pos().x() > dialog._items[graph.target].pos().x())
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


def test_folding_in_keeps_what_another_way_still_reaches():
    graph = ReferenceGraph.for_object(TARGET, "Game.GameManager", direction=TARGETS)
    target = graph.target

    def node(address, name):
        return graph.node_for(target, address, entry={
            "location": hex(address), "kind": "field", "object": hex(address),
            "className": name, "size": 0x20})

    first, second, shared, only = (node(0x20000, "A"), node(0x20100, "B"),
                                   node(0x20200, "Shared"), node(0x20300, "Only"))
    graph.add_holders(target, [(first, "a"), (second, "b")], 2)
    graph.add_holders(first.key, [(shared, "s"), (only, "o")], 2)
    graph.add_holders(second.key, [(shared, "s")], 1)
    removed = graph.collapse(first.key)
    # Only reached through the node folded in: gone; the shared one stays
    assert removed == [only.key]
    assert set(graph.nodes) == {target, first.key, second.key, shared.key}
    assert {(edge.holder, edge.held) for edge in graph.edges} == {
        (target, first.key), (target, second.key), (second.key, shared.key)}
    assert first.can_expand and first.total == 0
    # Folding in the object itself leaves it alone
    graph.collapse(target)
    assert list(graph.nodes) == [target] and not graph.edges


def test_the_window_shows_as_many_holders_as_the_setting_says(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from grembyte.ui.widgets import reference_graph, scripts

    holders = [0x20000 + 0x100 * number for number in range(12)]
    pointer_map = _PointerMap([(TARGET, location + 8) for location in holders])
    index = ReferenceIndex(pointer_map, [Region(0x10000, 0x30000, HEAP)])
    monkeypatch.setattr(scripts, "ScriptWorker", _FakeScriptWorker)
    monkeypatch.setattr(reference_graph, "ReferenceIndexWorker", _FakeIndexWorker)
    settings.apply({"graph_max_nodes": 10})
    translations = load_translations("de")
    panel = scripts.ScriptPanel(translations)
    panel.set_process(4242)
    dialog = reference_graph.ReferenceGraphDialog(translations, panel, 4242, TARGET, "X")
    try:
        dialog._index_worker.ready.emit(index)
        dialog._index_worker.finished.emit()
        worker = panel._worker
        worker.references_resolved.emit(len(worker.requests), [
            {"location": hex(location + 8), "kind": "field", "object": hex(location),
             "klass": "0x99", "className": "Game.Holder", "field": "target"}
            for location in holders])
        target = dialog.graph.nodes[dialog.graph.target]
        assert target.total == 12 and target.shown == 10
        assert len(dialog._items) == 11
    finally:
        settings.apply({"graph_max_nodes": 100})
        dialog.close()
        panel.set_process(None)
