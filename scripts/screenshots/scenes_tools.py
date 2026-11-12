"""The tool windows: scripts, native trace and monitor, memory viewer, sessions,
log, settings, theme and export.

No scene attaches: the workers these windows start are replaced by stubs
while the window is built, and the tables are filled through the slots the
workers would signal, with data in the format they emit.
"""

import json
import struct
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

from grembyte import __version__

from studio import DEMO_NAME, DEMO_PID, scene, settle

# The test game as Linux maps it: the program, then its heap with the gold.
_GAME_BASE = 0x5615_3A1F_0000
_HEAP_START = 0x5615_3A2A_0000
_HEAP_END = 0x5615_3A2E_1000
_GOLD = 0x5615_3A2C_41F8
# The object that holds the pointer to the gold's struct (chain +0x4040 → 0x28 → 0x0)
_HOLDER = _GOLD - 0x78
_MAPS = (
    f"{_GAME_BASE:x}-{_GAME_BASE + 0x1000:x} r--p 00000000 08:01 1 /opt/games/dummy/{DEMO_NAME}",
    f"{_GAME_BASE + 0x1000:x}-{_GAME_BASE + 0x3000:x} r-xp 00001000 08:01 1 "
    f"/opt/games/dummy/{DEMO_NAME}",
    f"{_GAME_BASE + 0x3000:x}-{_GAME_BASE + 0x5000:x} rw-p 00003000 08:01 1 "
    f"/opt/games/dummy/{DEMO_NAME}",
    f"{_HEAP_START:x}-{_HEAP_END:x} rw-p 00000000 00:00 0 [heap]",
)

# The Unreal game of the scripts
_AVOWED = "Avowed-Win64-Shipping.exe"
_AVOWED_PID = 61407


def _today_at(hour, minute, second, millisecond=0):
    """Milliseconds since the epoch of a time today, as the agents send them."""
    moment = datetime.now().replace(hour=hour, minute=minute, second=second,
                                    microsecond=millisecond * 1000)
    return int(moment.timestamp() * 1000)


class _Patched:
    """Replace module attributes while a window is built, then put them back."""

    def __init__(self, module, **replacements):
        self.module = module
        self.replacements = replacements
        self.saved = {}

    def __enter__(self):
        for name, value in self.replacements.items():
            self.saved[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self

    def __exit__(self, *_exc):
        for name, value in self.saved.items():
            setattr(self.module, name, value)


# --------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------
class _ScriptWorkerStub(QObject):
    """What ScriptPanel and ScriptRunDialog use of a ScriptWorker; attaches nowhere."""

    options_listed = Signal(object, object)
    options_failed = Signal(object, str)

    def __init__(self):
        super().__init__()
        self._next = 0

    def isRunning(self):  # pylint: disable=invalid-name
        return True

    def requestInterruption(self):  # pylint: disable=invalid-name
        """Nothing runs."""

    def wait(self, _milliseconds=None):
        return True

    def add_script(self, _spec):
        self._next += 1
        return self._next

    def update_script(self, _script_id, _spec):
        """Nothing runs."""

    def remove_script(self, _script_id):
        """Nothing runs."""

    def run_now(self, _script_id, _args=None):
        """Nothing runs."""

    def script_options(self, _script_id, _name):
        self._next += 1
        return self._next


_HEAL_SOURCE = """\
// Avowed: heal the player by 50
const cheats = ctx.find("AlabamaCheatManager");
cheats.call("GiveHealth", 50);
ctx.log("healed by 50");
"""

_DAMAGE_SOURCE = """\
// Twice the damage, also after a level-up resets it
const damage = ctx.gas.get("DamageOutgoingMult");
if (damage && damage.base !== 2) ctx.gas.setBase("DamageOutgoingMult", 2);
return ctx.gas.get("DamageOutgoingMult").current;
"""

_ITEM_SOURCE = """\
const cheats = ctx.find("AlabamaCheatManager");
for (let i = 0; i < ctx.args.count; i++)
    cheats.call("GiveItem", ctx.findClass(ctx.args.item));
if (ctx.args.notify) ctx.log(`added ${ctx.args.count} × ${ctx.args.item}`);
return `Added ${ctx.args.count} items`;
"""

_ITEM_OPTIONS = [
    {"label": "Healing Potion", "value": "BP_Consumable_HealthPotion_T1_C"},
    {"label": "Greater Healing Potion", "value": "BP_Consumable_HealthPotion_T2_C"},
    {"label": "Essence Potion", "value": "BP_Consumable_EssencePotion_T1_C"},
    {"label": "Lockpick", "value": "BP_Misc_Lockpick_C"},
]


def _item_spec():
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptParam, ScriptSpec

    return ScriptSpec(_ITEM_SOURCE, "manual", 0.0, True, "Give item", (
        ScriptParam("item", "Item", "choice", "BP_Consumable_HealthPotion_T1_C",
                    "return ctx.call(\"AlabamaItemLibrary:GetAllItems\")"
                    ".map(item => ({label: item.DisplayName, value: item.className}));"),
        ScriptParam("count", "Count", "number", "5"),
        ScriptParam("notify", "Note it in the log", "bool", "true"),
    ))


@scene("scripts-tab", size=(1280, 800),
       description="Scripts tab with a manual, a repeating and a parameterized script")
def scripts_tab(studio):
    """Three Avowed scripts, some runs in the log, the manual heal selected."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.features.scripts import ScriptSpec

    window = studio.show_attached(_AVOWED, pid=_AVOWED_PID, details="64-bit, Unreal Engine")
    panel = window.script_panel
    panel.set_process(_AVOWED_PID)
    worker = _ScriptWorkerStub()
    panel._worker = worker
    heal = panel.add_script(ScriptSpec(_HEAL_SOURCE, "manual", 0.0, True, "Restore health"))
    damage = panel.add_script(ScriptSpec(_DAMAGE_SOURCE, "repeat", 10.0, True, "Double damage"))
    item = panel.add_script(_item_spec())
    for script_id in (heal, damage, item):
        panel._on_added(worker, script_id)

    panel._on_ran(worker, damage, {"runs": 1, "time": _today_at(21, 3, 12, 418),
                                   "logs": [], "result": "2"})
    panel._on_ran(worker, heal, {"runs": 1, "time": _today_at(21, 4, 55, 102),
                                 "logs": ["healed by 50"], "result": None})
    panel._on_ran(worker, item, {"runs": 1, "time": _today_at(21, 6, 20, 731),
                                 "logs": ["added 5 × BP_Consumable_HealthPotion_T1_C"],
                                 "result": "Added 5 items"})
    panel._on_ran(worker, heal, {"runs": 2, "time": _today_at(21, 9, 41, 560),
                                 "logs": ["healed by 50"], "result": None})
    panel._on_ran(worker, damage, {"runs": 41, "time": _today_at(21, 10, 2, 7),
                                   "logs": [], "result": "2"})
    panel._on_ran(worker, heal, {"runs": 3, "time": _today_at(21, 11, 18, 244),
                                 "logs": ["healed by 50"], "result": None})
    panel.status_label.setText("")
    window.bottom_tabs.setCurrentWidget(panel)
    panel.table.selectRow(0)
    # The scan area folded away, as the button in the tab bar does
    window._set_scan_area_visible(False)
    panel.table.parentWidget().setSizes([260, 420])
    settle()
    return window


@scene("script-params", size=(640, 230),
       description="Parameters asked for before a script runs by hand")
def script_params(studio):
    """The run dialog of "Give item": a choice from the game, a number, a yes/no."""
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets.script_params import ScriptRunDialog

    worker = studio.keep(_ScriptWorkerStub())
    # The options came from the game when the dialog was opened before.
    dialog = ScriptRunDialog(studio.translations, _item_spec(), worker=worker, script_id=3,
                             options_cache={"item": _ITEM_OPTIONS},
                             last={"item": "BP_Consumable_HealthPotion_T2_C", "count": 3,
                                   "notify": True})
    dialog._inputs["item"].lineEdit().setCursorPosition(0)  # pylint: disable=protected-access
    return studio.keep(dialog)


# --------------------------------------------------------------------------
# Native trace and monitor
# --------------------------------------------------------------------------
class _AgentWorkerStub(QObject):
    """The signals of the native trace's and monitor's workers; attaches nowhere."""

    # NativeTraceWorker
    inventory_found = Signal(object, object, object)
    began = Signal(str)
    live = Signal(object)
    recorded = Signal(object)
    stopped = Signal()
    command_failed = Signal(str, str)
    worker_failed = Signal(str)
    # NativeMonitorWorker
    added = Signal(int, object)
    add_failed = Signal(int, str)
    removed = Signal(int)
    calls = Signal(object, object)

    platform = "linux"
    arch = "x64"

    def __init__(self, *_args, **_kwargs):
        super().__init__()

    # The ``started`` signal of a QThread, with the agent's answer
    started = Signal(object)

    def start(self):
        """Nothing is attached."""

    def isRunning(self):  # pylint: disable=invalid-name
        return False

    def requestInterruption(self):  # pylint: disable=invalid-name
        """Nothing runs."""

    def command(self, *_args, **_kwargs):
        """A command for the agent: there is none."""

    inventory = trace = begin = finish = stop = add = remove = command


def _offset(offset):
    return f"{_GAME_BASE + offset:#x}"


# (offset in the game, symbol) of the functions the trace saw
_FUNCTIONS = (
    (0x1820, "game_tick"),
    (0x2a10, "render_frame"),
    (0x2c40, "draw_text"),
    (0x1a40, "buy_item"),
    (0x1b10, "spend_gold"),
    (0x2230, "update_gold_label"),
    (0x2f80, "play_sound"),
    (0x1c70, "add_to_inventory"),
)


def _trace_results():
    """The ``finish`` answers of a baseline and of buying three times."""
    functions = [[_offset(offset), DEMO_NAME, offset] for offset, _name in _FUNCTIONS]
    names = {str(number): name for number, (_offset_, name) in enumerate(_FUNCTIONS)}
    main = DEMO_PID
    baseline = {"phase": "baseline", "duration": 5000, "firstFunction": 0,
                "functions": functions, "names": names,
                "counts": {"0": 300, "1": 300, "2": 600}, "events": [], "dropped": 0}
    events = []
    time_us = 812_400
    for _buy in range(3):
        for number, caller in ((3, 0x18d4), (4, 0x1a9c), (5, 0x1b5e), (7, 0x1ab8),
                               (6, 0x1ac6)):
            events.append([number, main, time_us, f"{DEMO_NAME}+{caller:#x}"])
            time_us += 37 + number * 11
        time_us += 1_100_000
    scenario = {"phase": "scenario", "duration": 4000, "firstFunction": len(functions),
                "functions": [], "names": {},
                "counts": {"0": 240, "1": 240, "2": 1450, "3": 3, "4": 3, "5": 3, "6": 3,
                           "7": 3},
                "events": events, "dropped": 0}
    return baseline, scenario


@scene("native-trace", size=(1180, 820),
       description="Native trace: functions a scenario called that the baseline did not")
def native_trace(studio):
    """Baseline while idle, scenario while buying three times; buy_item selected."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.features.native_trace import TraceModule, TraceThread
    from grembyte.ui.widgets import native_trace as module

    with _Patched(module, NativeTraceWorker=_AgentWorkerStub,
                  is_wine_process=lambda _pid: False):
        dialog = module.NativeTraceDialog(studio.translations, DEMO_PID)
    studio.keep(dialog)
    threads = [TraceThread(DEMO_PID, DEMO_NAME, 0.41, True),
               TraceThread(DEMO_PID + 3, "render", 0.23, True),
               TraceThread(DEMO_PID + 4, "audio", 0.02, True),
               TraceThread(DEMO_PID + 7, "gmain", 0.0, False),
               TraceThread(DEMO_PID + 8, "pool-spawner", 0.0, False)]
    path = f"/opt/games/dummy/{DEMO_NAME}"
    modules = [TraceModule(DEMO_NAME, path, _GAME_BASE, 0x5000, True),
               TraceModule("libSDL2-2.0.so.0", "/usr/lib64/libSDL2-2.0.so.0",
                           0x7F3B_1D20_0000, 0x1A0000, False),
               TraceModule("libc.so.6", "/usr/lib64/libc.so.6", 0x7F3B_1C80_0000,
                           0x1F0000, False),
               TraceModule("libm.so.6", "/usr/lib64/libm.so.6", 0x7F3B_1CA0_0000,
                           0xD8000, False)]
    dialog._on_inventory(threads, modules, "linux")
    dialog.followed_modules = {DEMO_NAME}
    dialog._on_started({"threads": 3, "modules": 1, "failed": 0})
    baseline, scenario = _trace_results()
    dialog._on_recorded(baseline)
    dialog._on_recorded(scenario)
    dialog.rows_view.selectRow(0)
    settle()
    return dialog


def _value(number, where=None, text=None):
    value = {"value": f"{number & 0xFFFF_FFFF_FFFF_FFFF:#x}"}
    if where:
        value["where"] = where
    if text:
        value["text"] = text
    return value


def _monitor_calls():
    """Three purchases: buy_item, and spend_gold taking 50 gold each time."""
    player = _GOLD
    calls = []
    gold = 1500
    for number, second in enumerate((12, 14, 17)):
        start = _today_at(21, 20, second, 104 + number * 211)
        calls.append({"monitorId": 2, "time": start, "thread": DEMO_PID,
                      "args": [_value(_HOLDER), _value(3), _value(50), _value(0)],
                      "result": _value(1),
                      "caller": _offset(0x18d4), "callerWhere": f"{DEMO_NAME}+0x18d4",
                      "callerSymbol": "handle_input+0x54", "changes": []})
        before, after = gold.to_bytes(4, "little").hex(), (gold - 50).to_bytes(4, "little").hex()
        calls.append({"monitorId": 1, "time": start + 1, "thread": DEMO_PID,
                      "args": [_value(player, "[heap]+0x241f8"), _value(50),
                               _value(_GAME_BASE + 0x3120, f"{DEMO_NAME}+0x3120",
                                      text="Healing Potion"),
                               _value(0)],
                      "result": _value(gold - 50),
                      "caller": _offset(0x1a9c), "callerWhere": f"{DEMO_NAME}+0x1a9c",
                      "callerSymbol": "buy_item+0x5c",
                      "changes": [{"arg": 0, "offset": 0, "address": f"{player:#x}",
                                   "before": before, "after": after}]})
        gold -= 50
    return calls


class _SignatureWorkerStub(QObject):
    """Stands in for the monitor's SignatureWorker: the scene sets the signatures."""

    found = Signal(int, object)
    finished = Signal()

    def __init__(self, *_args, **_kwargs):
        super().__init__()

    def start(self):
        self.finished.emit()

    def isRunning(self):  # pylint: disable=invalid-name
        return False

    def wait(self, _milliseconds=None):
        return True


@scene("native-monitor", size=(1180, 820),
       description="Native function monitor: calls with arguments, result, caller and changes")
def native_monitor(studio):
    """spend_gold and buy_item monitored while buying three times; the last call selected."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.features.native_trace import NativeFunction
    from grembyte.ui.widgets import native_monitor as module

    from grembyte.features.code_explorer import Parameter, Signature

    with _Patched(module, NativeMonitorWorker=_AgentWorkerStub,
                  is_wine_process=lambda _pid: False):
        dialog = module.NativeMonitorDialog(studio.translations, DEMO_PID)
    studio.keep(dialog)
    # The demo game has no file to guess from: its signatures as the code explorer gives them
    signatures = {
        1: Signature("sysv", (Parameter("rdi", "pointer"), Parameter("rsi", "int"),
                              Parameter("rdx", "pointer")), "int"),
        2: Signature("sysv", (Parameter("rdi", "pointer"), Parameter("rsi", "int"),
                              Parameter("rdx", "int")), "uint8"),
    }
    with _Patched(module, SignatureWorker=_SignatureWorkerStub):
        for monitor_id, (offset, name) in ((1, (0x1b10, "spend_gold")),
                                           (2, (0x1a40, "buy_item"))):
            dialog.add_function(NativeFunction(_GAME_BASE + offset, DEMO_NAME, offset, name))
            dialog._on_added(monitor_id, {"address": _offset(offset), "kind": "function",
                                          "where": f"{DEMO_NAME}+{offset:#x}", "symbol": name})
            dialog._on_signature(monitor_id, signatures[monitor_id])
    dialog._on_calls(_monitor_calls(), {1: {"calls": 3, "skipped": 0},
                                        2: {"calls": 3, "skipped": 0}})
    dialog.functions_table.selectRow(0)
    dialog.log_view.selectRow(dialog.log.rowCount() - 1)
    dialog.changes_table.selectRow(0)
    # Room for both monitored functions, and for the changed bytes' columns
    dialog.functions_table.parentWidget().parentWidget().setSizes([170, 300, 250])
    dialog.changes_table.parentWidget().parentWidget().setSizes([400, 700])
    settle()
    return dialog


# --------------------------------------------------------------------------
# Code explorer
# --------------------------------------------------------------------------
_DUMMY_BINARY = None


def _dummy_game_binary():
    """test_app/dummy_game.c built as the tutorial builds it, once per run.

    Without position-independent code, as GCC on Fedora builds it, so the
    addresses match the example page (docs/examples/dummy-game-code-explorer.md).
    Needs gcc and the GTK 3 development files.
    """
    global _DUMMY_BINARY  # pylint: disable=global-statement
    if _DUMMY_BINARY is None:
        root = Path(__file__).resolve().parents[2]
        folder = Path(tempfile.mkdtemp(prefix="grembyte-dummy-game-"))
        binary = folder / DEMO_NAME
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gtk+-3.0"], check=True,
                               capture_output=True, text=True).stdout.split()
        subprocess.run(["gcc", "-O2", "-fno-pie", "-no-pie",
                        str(root / "test_app" / "dummy_game.c"), "-o", str(binary), *flags],
                       check=True)
        _DUMMY_BINARY = binary
    return _DUMMY_BINARY


@scene("code-explorer", size=(1280, 820),
       description="Code explorer: a button's text, the handler it is connected to, its calls")
def code_explorer(studio):
    """The test game: "treasure" among the strings, on_treasure open with its
    call of add_gold selected, and the instruction that hands on_treasure to GTK."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.features.code_explorer import CodeIndex, load_image
    from grembyte.ui.widgets import code_explorer as module

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

    names = {name: address for address, name in index.image.names.items()}
    dialog.string_filter.setText("treasure")
    dialog.string_view.selectRow(0)
    handler = names["on_treasure"]
    call = next(line.address for line in index.disassemble(index.function_starting(handler))
                if line.comment == "add_gold")
    dialog._show_function(handler, call)
    dialog._show_references(handler)
    dialog.ref_view.selectRow(0)
    settle()
    return dialog


@scene("code-explorer-pseudocode", size=(1280, 820),
       description="Code explorer: refresh_ui as pseudocode, with pointer chains")
def code_explorer_pseudocode(studio):
    """refresh_ui of the test game in the pseudocode tab, at the line that
    reads the gold through g_game → 0x28 → 0x0."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.features.code_explorer import CodeIndex, load_image
    from grembyte.ui.widgets import code_explorer as module

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
    # At its final size, so the selected line can be put in the middle
    dialog.resize(1280, 820)
    dialog.show()
    settle()

    names = {name: address for address, name in index.image.names.items()}
    dialog.tabs.setCurrentIndex(1)  # the functions
    dialog.function_filter.setText("refresh")
    refresh_ui = names["refresh_ui"]
    row = dialog.function_model.row_of(refresh_ui)
    if row is not None:
        dialog.function_view.selectRow(row)
    # The call of snprintf for the gold
    call = next(line.address for line in index.disassemble(index.function_starting(refresh_ui))
                if line.comment == "snprintf")
    dialog._show_function(refresh_ui, call)
    dialog._show_callers()
    dialog.ref_view.selectRow(0)
    dialog.function_tabs.setCurrentWidget(dialog.pseudocode_page)
    # More room for the code than for the callers
    dialog.ref_view.parentWidget().parentWidget().setSizes([170, 610])
    settle()
    dialog.pseudocode_view.centerCursor()
    settle()
    return dialog


# --------------------------------------------------------------------------
# Memory viewer
# --------------------------------------------------------------------------
def _heap_bytes():
    """The heap of the test game around the gold, as (start, bytes)."""
    start = _HOLDER - 0x40
    memory = bytearray(0x200)

    def put(address, data):
        memory[address - start:address - start + len(data)] = data

    # malloc chunk headers, a vtable and the holder whose +0x28 points to the gold's struct
    put(_HOLDER - 0x08, struct.pack("<Q", 0x71))
    put(_HOLDER, struct.pack("<Q", _GAME_BASE + 0x3D88))
    put(_HOLDER + 0x08, b"Imoen\0\0\0")
    put(_HOLDER + 0x10, struct.pack("<IIII", 7, 18, 16, 12))
    put(_HOLDER + 0x20, struct.pack("<I", 2))
    put(_HOLDER + 0x28, struct.pack("<Q", _GOLD))
    put(_HOLDER + 0x30, struct.pack("<Q", _HOLDER + 0x140))
    put(_HOLDER + 0x48, struct.pack("<Q", 0x31))
    put(_HOLDER + 0x50, b"Tavern\0\0\0\0\0\0\0\0\0\0")
    put(_HOLDER + 0x68, struct.pack("<Q", 0x41))
    # Character of test_app/dummy_game.c: gold, hp, strength, dexterity,
    # intelligence, mana (+0x14), experience (+0x18)
    put(_GOLD, struct.pack("<iiiii", 1350, 100, 14, 11, 9))
    put(_GOLD + 0x14, struct.pack("<f", 20.13))
    put(_GOLD + 0x18, struct.pack("<d", 412.6))
    put(_GOLD + 0x28, struct.pack("<Q", 0x51))
    put(_GOLD + 0x30, b"Healing Potion\0\0")
    put(_GOLD + 0x40, struct.pack("<IIQ", 3, 50, _GAME_BASE + 0x3120))
    put(_GOLD + 0x78, struct.pack("<Q", 0x20F81))
    return start, bytes(memory)


def _fake_read_memory(_pid, address, size):
    """Bytes of the demo heap; the rest of the heap reads as zeros."""
    start, memory = _heap_bytes()
    data = bytearray(size)
    readable = bytearray(size)
    for index in range(size):
        where = address + index
        if _HEAP_START <= where < _HEAP_END:
            readable[index] = 1
            if start <= where < start + len(memory):
                data[index] = memory[where - start]
    return bytes(data), readable


@scene("memory-viewer", size=(860, 640),
       description="Memory viewer at the gold of the test game, its values decoded")
def memory_viewer(studio):
    """The heap around the gold (1350 as int32), the gold's first byte selected."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.platform.linux import parse_maps
    from grembyte.ui.widgets import memory_view as module

    patch = _Patched(module, read_memory=_fake_read_memory,
                     read_mappings=lambda _pid: parse_maps(_MAPS))
    patch.__enter__()  # the viewer reads on every refresh: kept while it lives
    dialog = module.MemoryViewerDialog(studio.translations, DEMO_PID)
    dialog.destroyed.connect(lambda _object=None: patch.__exit__())
    studio.keep(dialog)
    dialog.resize(860, 640)
    dialog.show()
    settle()
    # Some rows before the gold for context; its first byte selected
    row_start = _HOLDER - _HOLDER % 16
    dialog.go_to(row_start)
    column = (_GOLD - row_start) % 16
    row = (_GOLD - row_start) // 16
    dialog.view.setCurrentIndex(dialog.model.index(row, column))
    dialog.address_input.setText(f"0x{_GOLD:X}")
    settle()
    return dialog


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
class _Session(SimpleNamespace):
    """What the sessions window reads of a session of the registry."""

    def stuck(self, _now):
        return False


class _Registry:
    """The session registry with demo sessions; stopping does nothing."""

    def __init__(self, demo_sessions):
        self._sessions = demo_sessions

    def sessions(self, pid=None):
        return [s for s in self._sessions if pid is None or s.pid == pid]

    def stop(self, _session):
        """Nothing runs."""

    def disconnect(self, _session):
        """Nothing runs."""

    def stop_all(self):
        """Nothing runs."""


@scene("sessions", size=(760, 380),
       description="Sessions window with Frida and ptrace sessions on the test game")
def sessions(studio):
    """Hooks and a native monitor over Frida, a write watch over ptrace."""
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets import sessions as module

    now = time.monotonic()
    registry = _Registry([
        _Session(kind="hooks", pid=DEMO_PID, started=now - 1124, stopping=None,
                 detaching=False, frida_session=object()),
        _Session(kind="native_monitor", pid=DEMO_PID, started=now - 318, stopping=None,
                 detaching=False, frida_session=object()),
        _Session(kind="ptrace_watch", pid=DEMO_PID, started=now - 47, stopping=None,
                 detaching=False, frida_session=None),
    ])
    # The window refreshes every second: the demo registry stays while it lives.
    patch = _Patched(module, SESSIONS=registry, frida_agents=lambda _pid: 1,
                     frida_gadgets=lambda _pid: 0)
    patch.__enter__()
    dialog = module.SessionsDialog(studio.translations, lambda: DEMO_PID)
    dialog.destroyed.connect(lambda _object=None: patch.__exit__())
    studio.keep(dialog)
    dialog.table.selectRow(0)
    return dialog


# --------------------------------------------------------------------------
# Log
# --------------------------------------------------------------------------
def _activity_entries():
    """A short run: attach, hooks, a native trace and a monitor."""
    run = "3f9c2a71"
    day = datetime.now().strftime("%Y-%m-%d")
    entries = [
        ("21:02:41.118", "app_started", {
            "pid": 30412, "grembyte": __version__, "python": "3.13.7", "frida": "17.24.3",
            "qt": "6.9.2", "pyside": "6.9.2", "system": "Fedora Linux 44",
            "kernel": "6.17.4-300.fc44.x86_64", "machine": "x86_64", "session": "wayland",
            "desktop": "KDE", "argv": []}),
        ("21:02:47.902", "process_selected", {
            "name": DEMO_NAME, "changed": True, "pid": DEMO_PID,
            "exe": f"/opt/games/dummy/{DEMO_NAME}", "wine": False, "loader_bits": 64,
            "bits": 64, "frida_agents": 0}),
        ("21:02:48.015", "session_started", {
            "session": 1, "kind": "hooks", "worker": "HookWorker", "pid": DEMO_PID}),
        ("21:02:48.391", "attached", {
            "pid": DEMO_PID, "session": 1, "kind": "hooks", "via": "frida.attach",
            "seconds": 0.376}),
        ("21:02:48.522", "agent_loaded", {
            "agent": "hooks#1", "pid": DEMO_PID, "session": 1, "size": 48211,
            "seconds": 0.131}),
        ("21:03:05.640", "click", {
            "window": "Grembyte", "widget": "QPushButton", "text": "First scan"}),
        ("21:03:19.207", "click", {
            "window": "Grembyte", "widget": "QPushButton", "text": "Next scan"}),
        ("21:03:31.774", "rpc", {
            "agent": "hooks#1", "method": "readValues", "repeated": 24, "ms": 41.9,
            "max_ms": 3.2, "within_s": 12.0}),
        ("21:04:02.113", "window_shown", {"window": "Native trace",
                                          "widget": "NativeTraceDialog"}),
        ("21:04:02.250", "session_started", {
            "session": 2, "kind": "native_trace", "worker": "NativeTraceWorker",
            "pid": DEMO_PID}),
        ("21:04:02.598", "attached", {
            "pid": DEMO_PID, "session": 2, "kind": "native_trace", "via": "frida.attach",
            "seconds": 0.348}),
        ("21:04:09.431", "native_trace_start", {"pid": DEMO_PID, "threads": 3, "modules": 1}),
        ("21:04:09.802", "native_trace_started", {
            "pid": DEMO_PID, "threads": 3, "modules": 1, "failed": 0}),
        ("21:04:21.006", "trace_recorded", {
            "phase": "baseline", "duration_ms": 5000, "functions": 3, "calls": 1200,
            "events": 0, "dropped": 0}),
        ("21:04:33.470", "trace_recorded", {
            "phase": "scenario", "duration_ms": 4000, "functions": 8, "calls": 1945,
            "events": 20, "dropped": 0}),
        ("21:04:40.911", "click", {
            "window": "Native trace", "widget": "QPushButton", "text": "Monitor function"}),
        ("21:04:41.037", "session_started", {
            "session": 3, "kind": "native_monitor", "worker": "NativeMonitorWorker",
            "pid": DEMO_PID}),
        ("21:04:41.402", "attached", {
            "pid": DEMO_PID, "session": 3, "kind": "native_monitor", "via": "frida.attach",
            "seconds": 0.365}),
        ("21:05:12.288", "click", {
            "window": "Native trace", "widget": "QPushButton", "text": "Stop following"}),
        ("21:05:12.904", "session_ended", {
            "session": 2, "kind": "native_trace", "pid": DEMO_PID, "seconds": 70.654}),
        ("21:05:13.017", "window_hidden", {"window": "Native trace",
                                           "widget": "NativeTraceDialog"}),
        ("21:05:20.455", "click", {
            "window": "Native function monitor", "widget": "QPushButton",
            "text": "Show in memory viewer"}),
        ("21:05:20.583", "window_shown", {"window": "Memory viewer",
                                          "widget": "MemoryViewerDialog"}),
        ("21:05:34.120", "rpc", {
            "agent": "native_monitor#3", "method": "add", "ms": 12.4}),
        ("21:05:41.706", "watch_backend", {"backend": "ptrace", "chosen": "auto"}),
        ("21:05:41.733", "session_started", {
            "session": 4, "kind": "ptrace_watch", "worker": "PtraceWriteWatchWorker",
            "pid": DEMO_PID}),
    ]
    started = None
    for clock, event, fields in entries:
        moment = datetime.fromisoformat(f"{day}T{clock}")
        started = started or moment
        yield {"t": moment.isoformat(timespec="milliseconds"),
               "s": round((moment - started).total_seconds(), 3), "run": run,
               "event": event, **fields}


@scene("log-activity", size=(1000, 600),
       description="Log window on the activity log: what Grembyte did, in order")
def log_activity(studio):
    """The activity of a short run on the test game."""
    # pylint: disable=import-outside-toplevel
    from grembyte.core import activity
    from grembyte.ui.widgets.log_view import LogDialog

    path = activity.activity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n"
                            for entry in _activity_entries()), encoding="utf-8")
    with _Patched(activity, enabled=lambda: True):
        dialog = LogDialog(studio.translations)
        dialog.source_combo.setCurrentIndex(1)
    # Where it is on a real system, not in the studio's temporary folder
    dialog.path_label.setText(studio.translations["log.path"].format(
        path="~/.local/state/grembyte/activity.jsonl"))
    dialog.text.moveCursor(dialog.text.textCursor().MoveOperation.Start)
    return studio.keep(dialog)


# --------------------------------------------------------------------------
# Settings, theme, export
# --------------------------------------------------------------------------
@scene("settings", description="Settings window")
def settings_window(studio):
    """The general settings with their defaults."""
    # pylint: disable=import-outside-toplevel
    from grembyte.db.repository.settings import SettingsRepository
    from grembyte.ui.widgets.settings_dialog import SettingsDialog

    dialog = SettingsDialog(studio.translations, SettingsRepository())
    dialog.adjustSize()
    return studio.keep(dialog)


@scene("theme-editor", size=(560, 680), description="Color theme editor")
def theme_editor(studio):
    """The current theme's colors, each a button that opens a color picker."""
    # pylint: disable=import-outside-toplevel,protected-access
    from grembyte.ui.widgets.theme_dialog import ThemeDialog

    dialog = ThemeDialog(studio.translations, studio.window._theme_manager)
    return studio.keep(dialog)


class _ExportRepository:  # pylint: disable=too-few-public-methods
    """The games the export lists; writing is not needed for the picture."""

    def __init__(self, games):
        self.games = games

    def list_games(self):
        return self.games


@scene("game-export", size=(940, 400), description="Export of a game's saved entries")
def game_export(studio):
    """Three games with saved entries; the test game selected."""
    # pylint: disable=import-outside-toplevel
    from grembyte.db.repository.game_export import ExportableGame
    from grembyte.ui.widgets.game_export import GameExportDialog

    proton = ("/home/user/.steam/steam/steamapps/common/Proton - Experimental/files/lib/"
              "wine/x86_64-unix/wine64-preloader")
    games = [
        ExportableGame(_AVOWED, [proton], {
            "addresses": 2, "hooks": 0, "method_hooks": 3, "favorites": 7, "scripts": 3,
            "pointer_scans": 0}),
        ExportableGame(DEMO_NAME, [f"/opt/games/dummy/{DEMO_NAME}"], {
            "addresses": 4, "hooks": 1, "method_hooks": 0, "favorites": 2, "scripts": 0,
            "pointer_scans": 1}),
        ExportableGame("PillarsOfEternity", [
            "/home/user/.steam/steam/steamapps/common/Pillars of Eternity/PillarsOfEternity"], {
            "addresses": 3, "hooks": 0, "method_hooks": 2, "favorites": 5, "scripts": 1,
            "pointer_scans": 0}),
    ]
    dialog = GameExportDialog(studio.translations, _ExportRepository(games), current=DEMO_NAME)
    dialog.folder.setText("/home/user/Documents/grembyte")
    dialog.pointer_scans.setChecked(True)
    return studio.keep(dialog)
