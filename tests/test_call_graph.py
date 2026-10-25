"""The call graph from IL code (see hermes.features.call_graph), without a game."""

import struct

from hermes.features.call_graph import (
    Call,
    async_owner,
    build_call_index,
    callees_of,
    callers_in,
    calls_in,
    split_bodies,
    state_machines,
)
from hermes.features.catalog import CatalogMethod, catalog_methods


def _token(value):
    return struct.pack("<I", value)


# ldarg.0; call LoadBin; ldc.i4.s 5; switch (2 targets); callvirt (MemberRef);
# ldftn (lambda); newobj (MemberRef); ldstr; ldc.i4 0x28282828 (looks like calls);
# ldc.i8; call into a field token (not a method); ret
_CODE = (b"\x02" + b"\x28" + _token(0x06000002) + b"\x1f\x05"
         + b"\x45" + _token(2) + _token(0) + _token(4)
         + b"\x6f" + _token(0x0A000010) + b"\xfe\x06" + _token(0x06000003)
         + b"\x73" + _token(0x0A000011) + b"\x72" + _token(0x70000001)
         + b"\x20" + _token(0x28282828) + b"\x21" + b"\x28" * 8
         + b"\x28" + _token(0x04000001) + b"\x2a")

_LOADER = "Game.Loader"
_CLASSES = [
    [_LOADER, [
        ["Load", "()", "System.Void", False, 0x06000001, "Game.Loader:Load ()", 0, 0],
        ["LoadBin", "(string)", "System.Byte[]", False, 0x06000002,
         "Game.Loader:LoadBin (string)", 0, 0],
        ["<Load>b__0", "()", "System.Void", False, 0x06000003, "Game.Loader:<Load>b__0 ()", 0, 0],
    ]],
    [f"{_LOADER}.<Load>d__4", [
        ["MoveNext", "()", "System.Void", False, 0x06000004,
         "Game.Loader/<Load>d__4:MoveNext ()", 0, 0],
    ]],
]


def test_calls_are_found_in_the_order_of_the_code():
    assert calls_in(_CODE) == [("call", 0x06000002), ("callvirt", 0x0A000010),
                               ("ldftn", 0x06000003), ("newobj", 0x0A000011)]


def test_cut_or_odd_code_keeps_what_was_decoded():
    assert calls_in(_CODE[:6]) == [("call", 0x06000002)]
    assert calls_in(_CODE[:4]) == []
    assert calls_in(b"\xfe") == [] and calls_in(b"") == []


def test_method_bodies_answer_is_split_into_records():
    buffer = _token(0x06000001) + _token(2) + b"\x02\x2a" + _token(0x06000002) + _token(0)
    assert split_bodies(buffer) == {0x06000001: b"\x02\x2a", 0x06000002: b""}


def test_index_names_own_and_foreign_methods():
    asked = []

    def resolve(tokens):
        asked.append(tokens)
        return [["mscorlib", 0x06001234, "System.Text.Encoding:GetString (byte[])"], None]

    progress = []
    index = build_call_index("Assembly-CSharp", _CLASSES,
                             lambda tokens: {0x06000001: _CODE, 0x06000004: _CODE[:6]},
                             resolve, lambda done, total: progress.append((done, total)))

    # Only the tokens the assembly does not define are asked for, once.
    assert asked == [[0x0A000010, 0x0A000011]]
    assert progress == [(0, 4), (4, 4)]
    targets = index["targets"]
    calls = dict((caller, [(opcode, targets[target][2]) for opcode, target in listed])
                 for caller, listed in index["calls"])
    # The unresolvable newobj is left out.
    assert calls == {
        0x06000001: [("call", "Game.Loader:LoadBin (string)"),
                     ("callvirt", "System.Text.Encoding:GetString (byte[])"),
                     ("ldftn", "Game.Loader:<Load>b__0 ()")],
        0x06000004: [("call", "Game.Loader:LoadBin (string)")]}


def test_callees_callers_and_the_body_of_an_async_method():
    index = build_call_index("Assembly-CSharp", _CLASSES,
                             lambda tokens: {0x06000004: _CODE[:6]},
                             lambda tokens: [None] * len(tokens))
    methods = {m.token: m for m in catalog_methods("Assembly-CSharp", _CLASSES)}
    load, load_bin, move_next = methods[0x06000001], methods[0x06000002], methods[0x06000004]

    # Load's body is in <Load>d__4.MoveNext.
    assert async_owner(move_next) == (_LOADER, "Load")
    assert async_owner(load) is None
    assert state_machines(load, methods.values()) == [move_next]

    lookup = lambda assembly, token: methods.get(token)  # pylint: disable=unnecessary-lambda-assignment
    assert callees_of(index, load, lookup) == []
    assert callees_of(index, move_next, lookup) == [Call("call", load_bin)]
    assert callers_in(index, load_bin, methods) == [Call("call", move_next)]
    assert callers_in(index, load, methods) == []


def test_a_generic_instance_keeps_its_signature():
    index = {"targets": [["mscorlib", 0x0600000e, "System.Collections.Generic.List`1<int>:Add (int)"]],
             "calls": [[0x06000001, [["callvirt", 0]]]]}
    definition = CatalogMethod("mscorlib", "System.Collections.Generic.List<T>", "Add", "(T)",
                               "System.Void", False, 0x0600000e,
                               "System.Collections.Generic.List`1:Add (T)")
    caller = CatalogMethod("Assembly-CSharp", _LOADER, "Load", "()", "System.Void", False,
                           0x06000001, "Game.Loader:Load ()")
    [call] = callees_of(index, caller, lambda assembly, token: definition)
    assert call.method.signature == "System.Collections.Generic.List`1<int>:Add (int)"
    assert call.method.token == 0x0600000e


def test_dialog_navigates_and_searches_callers(monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access,too-many-locals
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module
    from hermes.engines.registry import engine_by_id
    from hermes.features.call_graph import CallGraphWorker
    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.call_graph import CallGraphDialog

    _app = QApplication.instance() or QApplication([])
    asked = []
    monkeypatch.setattr(CallGraphWorker, "start", lambda self: None)
    monkeypatch.setattr(CallGraphWorker, "callees",
                        lambda self, method: asked.append(("callees", method.name)) or len(asked))
    monkeypatch.setattr(CallGraphWorker, "callers",
                        lambda self, method: asked.append(("callers", method.name)) or len(asked))
    methods = {m.token: m for m in catalog_methods("Assembly-CSharp", _CLASSES)}
    load, load_bin, move_next = methods[0x06000001], methods[0x06000002], methods[0x06000004]

    dialog = CallGraphDialog(load_translations("de"), 4242, load, engine=engine_by_id("mono"))
    monitored = []
    dialog.monitor_requested.connect(monitored.append)
    try:
        assert asked == [("callees", "Load")]
        dialog._worker.callees_found.emit(1, load, [], [Call("call", load_bin)])
        assert dialog.callees_table.item(0, 0).text() == "call · async"
        assert dialog.callees_table.item(0, 1).text() == "Game.Loader:LoadBin (string)"
        assert "async-Teil" in dialog.status_label.text()

        # Callers: searched once, then for every method gone to.
        dialog.callers_button.click()
        dialog._worker.progress.emit(0, 2, "Assembly-CSharp")
        assert "Assembly-CSharp (0 von 2)" in dialog.callers_progress.format()
        dialog._worker.callers_found.emit(2, load, [Call("call", move_next)])
        assert dialog.callers_table.item(0, 1).text() == "Game.Loader:Load (async)"

        dialog.callees_table.setCurrentCell(0, 1)
        dialog.monitor_button.click()
        assert monitored == [load_bin]
        dialog.callees_table.cellDoubleClicked.emit(0, 1)
        assert dialog.method == load_bin and asked[-2:] == [("callees", "LoadBin"),
                                                            ("callers", "LoadBin")]
        # An answer for the method left behind is not shown.
        dialog._worker.callees_found.emit(1, load, [Call("call", load_bin)], [])
        assert dialog.callees_table.rowCount() == 0

        dialog.back_button.click()
        assert dialog.method == load and dialog.forward_button.isEnabled()
        dialog.forward_button.click()
        assert dialog.method == load_bin and not dialog.forward_button.isEnabled()
    finally:
        dialog.close()


def test_an_index_made_with_other_builds_is_made_anew():
    # pylint: disable=import-outside-toplevel
    from hermes.features.call_graph import read_call_index

    made = []

    def build():
        made.append(len(made))
        return {"targets": [["DOTween", 0x06000001, "DG.Tween:Kill ()"],
                            ["UnityEngine.CoreModule", 0x06000123, "UnityEngine.Object:Destroy ()"]],
                "calls": [[0x06000001, [["call", 1]]]]}

    game_a = {"DOTween": "dotween", "UnityEngine.CoreModule": "unity-2021"}
    index = read_call_index("mono", "DOTween", "dotween", game_a, build)
    # Its own assembly is not a reference.
    assert index["references"] == {"UnityEngine.CoreModule": "unity-2021"}
    read_call_index("mono", "DOTween", "dotween", game_a, build)
    assert made == [0]  # from the cache

    # The same DOTween in a game with another UnityEngine
    game_b = {"DOTween": "dotween", "UnityEngine.CoreModule": "unity-2022"}
    index = read_call_index("mono", "DOTween", "dotween", game_b, build)
    assert made == [0, 1]
    assert index["references"] == {"UnityEngine.CoreModule": "unity-2022"}
    read_call_index("mono", "DOTween", "dotween", game_b, build)
    assert made == [0, 1]
