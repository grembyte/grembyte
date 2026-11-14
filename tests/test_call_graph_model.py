"""The call graph window's model (see grembyte.features.call_graph_model)."""

from grembyte.features.call_graph_model import (
    CALLEES,
    CALLERS,
    CallGraph,
    call_label,
    method_key,
)
from grembyte.features.catalog import CatalogMethod


def _method(class_name, name, token, assembly="Assembly-CSharp"):
    return CatalogMethod(assembly, class_name, name, "()", "System.Void", False, token,
                         f"{class_name}:{name} ()")


START = _method("Game.Store", "Buy", 1)
PAY = _method("Game.Purse", "Pay", 2)
REFRESH = _method("Game.Store", "Refresh", 3)
ADD = _method("System.Collections.Generic.List`1", "Add", 4, "mscorlib")
CLICK = _method("Game.StoreUi", "OnClick", 5)
LOAD = _method("Game.Store.<Load>d__3", "MoveNext", 6)


def _graph():
    graph = CallGraph.for_method(START)
    calls = [(graph.node_for(PAY), ""), (graph.node_for(REFRESH), ""),
             (graph.node_for(ADD, framework=True), ""), (graph.node_for(PAY), "newobj")]
    graph.add_calls(graph.center, CALLEES, calls, limit=100)
    graph.add_calls(graph.center, CALLERS, [(graph.node_for(CLICK), "")], limit=100)
    return graph


def test_callees_stand_right_callers_left():
    graph = _graph()
    columns = graph.columns()
    assert columns[method_key(PAY)] == columns[method_key(REFRESH)] == 1
    assert columns[method_key(CLICK)] == -1
    # Framework methods only when asked for, but counted
    assert method_key(ADD) not in columns
    assert graph.hidden_count(graph.center, CALLEES) == 1
    graph.show_framework = True
    assert graph.columns()[method_key(ADD)] == 1
    assert graph.hidden_count(graph.center, CALLEES) == 0
    assert graph.side(method_key(PAY)) == CALLEES
    assert graph.side(method_key(CLICK)) == CALLERS and graph.side(graph.center) is None
    # One edge per method called, its labels joined
    labels = {edge.callee: edge.label for edge in graph.edges if edge.caller == graph.center}
    assert labels[method_key(PAY)] == "newobj" and labels[method_key(REFRESH)] == ""
    assert graph.nodes[graph.center].total == {CALLERS: 1, CALLEES: 3}


def test_the_limit_keeps_the_games_methods_first():
    graph = CallGraph.for_method(START)
    calls = [(graph.node_for(ADD, framework=True), ""), (graph.node_for(PAY), ""),
             (graph.node_for(REFRESH), "")]
    graph.add_calls(graph.center, CALLEES, calls, limit=2)
    assert set(graph.nodes) == {graph.center, method_key(PAY), method_key(REFRESH)}
    assert graph.nodes[graph.center].shown[CALLEES] == 2
    assert graph.hidden_count(graph.center, CALLEES) == 1


def test_folding_in_takes_back_one_unfolding():
    graph = _graph()
    pay = method_key(PAY)
    # Pay also calls Refresh: found twice, by two unfoldings
    graph.add_calls(pay, CALLEES, [(graph.node_for(REFRESH), ""),
                                   (graph.node_for(LOAD), "")], limit=100)
    assert graph.columns()[method_key(LOAD)] == 2
    removed = graph.collapse(pay, CALLEES)
    assert removed == [method_key(LOAD)]
    # Refresh stays: the centre's unfolding found it too
    assert method_key(REFRESH) in graph.nodes
    assert not graph.nodes[pay].unfolded[CALLEES]
    graph.collapse(graph.center, CALLEES)
    assert set(graph.nodes) == {graph.center, method_key(CLICK)}


def test_hiding_and_the_way_to_the_centre():
    graph = _graph()
    pay = method_key(PAY)
    graph.add_calls(pay, CALLEES, [(graph.node_for(LOAD), "")], limit=100)
    path = graph.path_from(method_key(LOAD))
    assert path == [method_key(LOAD), pay, graph.center]
    assert graph.path_edges(path) == {(graph.center, pay), (pay, method_key(LOAD))}
    graph.hidden_classes.add("Game.Purse")
    assert pay not in graph.columns() and method_key(LOAD) not in graph.columns()
    assert graph.hidden_by_user() == 1
    # A MoveNext shows as its async method
    load = graph.nodes[method_key(LOAD)]
    assert (load.class_name, load.name) == ("Game.Store", "Load (async)")
    assert call_label("call") == "" and call_label("newobj", True) == "newobj · async"
