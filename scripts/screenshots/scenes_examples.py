"""The worked examples: the code explorer, the native monitor and a native
script on the tutorial's test game (docs/examples/dummy-game-code-explorer.md).

The code explorer scenes index the real test game, built as the tutorial
builds it (see scenes_tools._dummy_game_binary); the monitor and the script
get calls and runs in the format their workers emit, with the addresses of
that build.
"""

import struct

from studio import DEMO_NAME, DEMO_PID, scene, settle

from scenes_tools import (
    _AgentWorkerStub,
    _dummy_game_binary,
    _Patched,
    _ScriptWorkerStub,
    _SignatureWorkerStub,
    _today_at,
)


def _explorer(studio, size=(1280, 820)):
    """The code explorer on the test game, indexed and shown at its final
    size (so a selected line can be scrolled to the middle); and its index."""
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.code_explorer import CodeIndex, load_image
    from hermes.ui.widgets import code_explorer as module

    path = _dummy_game_binary()
    dialog = module.CodeExplorerDialog(studio.translations)
    studio.keep(dialog)
    index = CodeIndex(load_image(str(path)))
    choice = module.ModuleChoice(DEMO_NAME, str(path), index.image.base, program=True)
    dialog.modules = [choice]
    dialog.module_combo.clear()
    dialog.module_combo.addItem(choice.name, choice)
    dialog.module = choice
    dialog._on_built(index)
    dialog.resize(*size)
    dialog.show()
    settle()
    names = {name: address for address, name in index.image.names.items()}
    return dialog, index, names


@scene("example-code-button", size=(1280, 820),
       description="Example: the button's text, and where on_activate hands on_treasure to GTK")
def example_code_button(studio):
    """"treasure" among the strings, on_activate open at the line loading on_treasure."""
    # pylint: disable=protected-access
    dialog, index, names = _explorer(studio)
    dialog.string_filter.setText("treasure")
    dialog.string_view.selectRow(0)  # its references: the one in on_activate
    dialog.ref_view.selectRow(0)
    handler = names["on_treasure"]
    function = index.owner(index.function_at(index.references(handler)[0].address))
    line = next(line.address for line in index.disassemble(function) if line.target == handler)
    dialog._show_function(line, line)
    settle()
    dialog.line_view.scrollTo(dialog.line_view.currentIndex(),
                              dialog.line_view.ScrollHint.PositionAtCenter)
    settle()
    return dialog


@scene("example-code-add-gold", size=(1280, 820),
       description="Example: add_gold with its guessed signature and its two callers")
def example_code_add_gold(studio):
    """add_gold open, its callers listed above it."""
    # pylint: disable=protected-access
    dialog, _index, names = _explorer(studio)
    dialog.tabs.setCurrentIndex(1)  # the functions
    dialog.function_filter.setText("gold")
    add_gold = names["add_gold"]
    row = dialog.function_model.row_of(add_gold)
    if row is not None:
        dialog.function_view.selectRow(row)
    dialog._show_function(add_gold)
    dialog._show_callers()
    dialog.ref_view.selectRow(0)
    settle()
    return dialog


def _float_register(number, kind="float"):
    """The low 8 bytes of an xmm register holding ``number``; the rest as a game leaves it."""
    raw = struct.pack("<f", number) + b"\x00\x00\x80\x3f" if kind == "float" \
        else struct.pack("<d", number)
    return raw.hex()


def _value(number, where=None):
    value = {"value": f"{number & 0xFFFF_FFFF_FFFF_FFFF:#x}"}
    if where:
        value["where"] = where
    return value


# The player of the test game on the heap, and what the registers hold beside the arguments
_PLAYER = 0x1B6F_2C40
_LEFTOVERS = [_value(0x1B6F_2C40, "[heap]+0x2c40"), _value(0x7F3B_1D41_2A80), _value(0)]


def _example_calls():
    """Find treasure twice and Gold +100 once (add_gold), the mana regenerating
    and a spell (change_mana): with the return addresses of the test game's build."""
    calls = []

    def add_gold(second, amount, caller, symbol, millisecond=120):
        calls.append({"monitorId": 1, "time": _today_at(18, 42, second, millisecond),
                      "thread": DEMO_PID,
                      "args": [_value(amount)] + _LEFTOVERS, "result": _value(_PLAYER),
                      "vectors": [_float_register(0.13)] + ["0000000000000000"] * 7,
                      "vectorResult": _float_register(0.13),
                      "caller": f"{caller:#x}", "callerWhere": f"{DEMO_NAME}+{caller - 0x400000:#x}",
                      "callerSymbol": symbol, "changes": []})

    def change_mana(second, amount, caller, symbol, millisecond=640):
        calls.append({"monitorId": 2, "time": _today_at(18, 42, second, millisecond),
                      "thread": DEMO_PID,
                      "args": [_value(0x1B6F_2C40, "[heap]+0x2c40"), _value(0),
                               _value(0x7F3B_1D41_2A80), _value(1)],
                      "result": _value(_PLAYER),
                      "vectors": [_float_register(amount)] + ["0000000000000000"] * 7,
                      "vectorResult": _float_register(amount),
                      "caller": f"{caller:#x}", "callerWhere": f"{DEMO_NAME}+{caller - 0x400000:#x}",
                      "callerSymbol": symbol, "changes": []})

    change_mana(5, 0.13, 0x400E31, "on_regen_tick+0x31")
    add_gold(6, 23, 0x400C6E, "on_treasure+0x2e")
    change_mana(6, 0.13, 0x400E31, "on_regen_tick+0x31")
    add_gold(7, 100, 0x400D0B, "on_gold_add+0xb")
    change_mana(7, -7.5, 0x400C31, "on_cast_spell+0x31", millisecond=388)
    change_mana(7, 0.13, 0x400E31, "on_regen_tick+0x31")
    add_gold(8, 28, 0x400C6E, "on_treasure+0x2e")
    return calls


@scene("example-monitor", size=(1180, 820),
       description="Example: add_gold and change_mana monitored with their guessed signatures")
def example_monitor(studio):
    """The calls of Find treasure, Gold +100, Cast spell and the mana regenerating."""
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.code_explorer import Parameter, Signature
    from hermes.features.native_trace import NativeFunction
    from hermes.ui.widgets import native_monitor as module

    with _Patched(module, NativeMonitorWorker=_AgentWorkerStub,
                  is_wine_process=lambda _pid: False):
        dialog = module.NativeMonitorDialog(studio.translations, DEMO_PID)
    studio.keep(dialog)
    dialog._worker.platform, dialog._worker.arch = "linux", "x64"
    signatures = {1: Signature("sysv", (Parameter("rdi", "int"),), "void"),
                  2: Signature("sysv", (Parameter("xmm0", "float"),), "void")}
    with _Patched(module, SignatureWorker=_SignatureWorkerStub):
        for monitor_id, (address, name) in ((1, (0x4006D0, "add_gold")),
                                            (2, (0x4006F0, "change_mana"))):
            offset = address - 0x400000
            dialog.add_function(NativeFunction(address, DEMO_NAME, offset, name))
            dialog._on_added(monitor_id, {"address": f"{address:#x}", "kind": "function",
                                          "where": f"{DEMO_NAME}+{offset:#x}", "symbol": name})
            dialog._on_signature(monitor_id, signatures[monitor_id])
    dialog._on_calls(_example_calls(), {1: {"calls": 3, "skipped": 0},
                                        2: {"calls": 4, "skipped": 0}})
    dialog.functions_table.selectRow(0)
    dialog.log_view.selectRow(dialog.log.rowCount() - 1)
    dialog.functions_table.parentWidget().parentWidget().setSizes([170, 330, 250])
    settle()
    return dialog


_GOLD_SOURCE = """\
// Test game: 500 gold through the game's own functions
const n = ctx.native;
// add_gold(int amount): mov rax, [g_game] / mov rax, [rax+0x28] / add [rax], edi
const addGoldAt = n.scan("48 8B 05 ?? ?? ?? ?? 48 8B 40 28 01 38");
const addGold = n.fn(addGoldAt, "void", ["int"]);
// refresh_ui(): shows the new values in the window
const refreshUi = n.fn(n.scan("53 BA ?? ?? ?? ?? BE 00 01 00 00 48 81 EC 00 01 00 00"), "void", []);
addGold(500);
refreshUi();
// Read the gold back: g_game is what add_gold's first instruction loads
const gGame = n.rip(addGoldAt, 3, 7);
return `Gold: ${gGame.readPointer().add(0x28).readPointer().readS32()}`;
"""


@scene("example-script", size=(1280, 600),
       description="Example: the native script giving 500 gold, and its log")
def example_script(studio):
    """The gold script in the Scripts tab, run twice."""
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.scripts import ScriptSpec

    window = studio.show_attached()
    panel = window.script_panel
    panel.set_process(DEMO_PID)
    worker = _ScriptWorkerStub()
    panel._worker = worker
    gold = panel.add_script(ScriptSpec(_GOLD_SOURCE, "manual", 0.0, True, "500 gold"))
    panel._on_added(worker, gold)
    thread = {"name": "g_main_context_iteration", "thread": DEMO_PID, "main": True}
    panel._on_ran(worker, gold, {"runs": 1, "time": _today_at(18, 44, 3, 517), "logs": [],
                                 "result": '"Gold: 600"', "gameThread": thread})
    panel._on_ran(worker, gold, {"runs": 2, "time": _today_at(18, 44, 9, 82), "logs": [],
                                 "result": '"Gold: 1100"'})
    panel.status_label.setText("")
    window.bottom_tabs.setCurrentWidget(panel)
    panel.table.selectRow(0)
    window._set_scan_area_visible(False)
    panel.table.parentWidget().setSizes([170, 260])
    settle()
    return window
