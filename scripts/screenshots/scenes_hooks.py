"""Hooks: write watch, hook dialog, hook list, debug window, templates, pointer scan.

The story follows the test game (test_app/dummy_game.c): the gold of the
player lives behind ``g_game → player → gold``; a write watch finds the
instructions of ``add_gold`` and ``remove_gold``, a hook multiplies the gains.
No worker runs: the widgets get the data their workers would send.
"""

from datetime import datetime

from PySide6.QtWidgets import QHeaderView  # pylint: disable=import-error,no-name-in-module

from hermes.core.pointer import PointerChain
from hermes.db.repository.addresses import SavedPointerScan
from hermes.hooks.engine import HookSpec
from hermes.hooks.transforms import TemplateTransform, TransformDefinition
from hermes.platform import Mapping
from hermes.ui.widgets import pointer_scan
from hermes.ui.widgets.hooks import HookDialog
from hermes.ui.widgets.templates import TemplateDialog
from hermes.ui.widgets.write_watch import WriteWatchDialog
from studio import DEMO_NAME, DEMO_PID, scene, settle

# pylint: disable=protected-access

# Same gold and chain as the main window scene.
GOLD = 0x5615_3A2C_41F8
MANA = 0x5615_3A2C_420C
EXPERIENCE = 0x5615_3A2C_4210
GOLD_CHAIN = PointerChain(DEMO_NAME, 0x4040, (0x28, 0x0))
MANA_CHAIN = PointerChain(DEMO_NAME, 0x4040, (0x28, 0x14))
EXPERIENCE_CHAIN = PointerChain(DEMO_NAME, 0x4040, (0x28, 0x18))
# Where the game's code is loaded.
MODULE_BASE = 0x5615_38E5_9000

# Write instructions of the test game: offset, length, disassembly.
ADD_GOLD = (0x1A3C, 2, "add dword ptr [rax], edi")
REMOVE_GOLD = (0x1A5C, 2, "sub dword ptr [rax], edi")
SHOW_GOLD = (0x1B07, 2, "mov edx, dword ptr [rax]")
CHANGE_MANA = (0x1A88, 5, "movss dword ptr [rax + 0x14], xmm0")
ADD_EXPERIENCE = (0x1AC4, 5, "movsd qword ptr [rax + 0x18], xmm0")

# Moments of the demo session, in ms since the epoch.
_START = datetime(2026, 11, 7, 14, 3, 12).timestamp() * 1000


def _moment(seconds):
    return int(_START + seconds * 1000)


def _site(offset):
    return f"{DEMO_NAME}+{offset:#x}"


def _own_window(studio):
    """A main window of its own, shown as attached to the demo program.

    The hooks scenes fill the hook panel; the shared ``studio.window`` stays
    as it is for the scenes of the other modules.
    """
    # pylint: disable=import-outside-toplevel
    from hermes.ui.main_window import MainWindow

    window = studio.keep(MainWindow(studio.language))
    window.show()
    settle()
    translations = window.translations
    text = translations["app.process_display_details"].format(
        name=DEMO_NAME, pid=DEMO_PID, details="64-bit")
    window.lbl_status.setText(translations["app.process_attached"].format(process=text))
    window._current_proc = translations["app.process_display"].format(
        name=DEMO_NAME, pid=DEMO_PID)
    window.lbl_proc_info.setText(f" {window._current_proc}")
    window._proc_icon_action.setVisible(True)
    return window


def _template(window, name):
    """The saved template called ``name`` (the examples are seeded)."""
    return next(t for t in window.hook_panel.templates() if t.name == name)


# ----------------------------------------------------------------------
# Write watch
# ----------------------------------------------------------------------
def _watch_hit(instruction, count, seconds, read, write, value):
    offset, length, _text = instruction
    trap = offset + length
    return {
        "pc": f"{MODULE_BASE + trap:#x}", "site": _site(trap), "count": count,
        "read": read, "write": write, "value": value, "threadId": 48213,
        "time": _moment(seconds),
    }


@scene("write-watch", size=(980, 300),
       description="Reads and writes of the gold, grouped by instruction")
def write_watch(studio):
    """The access watch on the gold after buying and finding treasure a few times."""
    window = studio.show_attached()
    dialog = studio.keep(WriteWatchDialog(
        f"0x{GOLD:X}", 4, studio.translations, window, reads=True))
    dialog.set_monitoring(4)
    dialog.add_hits([
        _watch_hit(ADD_GOLD, 4, 41.2, True, True, "46 05 00 00"),
        _watch_hit(REMOVE_GOLD, 1, 31.5, True, True, "84 03 00 00"),
        _watch_hit(SHOW_GOLD, 5, 41.2, True, False, "46 05 00 00"),
    ])
    dialog.results_table.selectRow(0)
    return dialog


# ----------------------------------------------------------------------
# Hook dialog
# ----------------------------------------------------------------------
@scene("hook-dialog", size=(560, 350),
       description="Creating a hook: the Multiplier template, factor 10, increases only")
def hook_dialog(studio):
    """The dialog that opens on "Create hook …" for the add_gold instruction."""
    window = studio.show_attached()
    offset, length, _text = ADD_GOLD
    dialog = studio.keep(HookDialog(
        studio.translations, f"0x{GOLD:X}", "int32", _site(offset + length),
        label="Gold", templates=window.hook_panel.templates(), pointer=GOLD_CHAIN,
        parent=window))
    index = dialog.template_combo.findText("Multiplier")
    dialog.template_combo.setCurrentIndex(index)
    editors = dialog.forms.currentWidget()._editors
    editors["factor"][1].setText("10")
    direction = editors["direction"][1]
    direction.setCurrentIndex(direction.findData("increase"))
    dialog.label_input.setText("Gold ×10")
    return dialog


# ----------------------------------------------------------------------
# Hooks tab and debug window
# ----------------------------------------------------------------------
class _StubHookWorker:
    """Stands in for the HookWorker: hands out ids, attaches to nothing."""

    access_stub = None
    fast_code = None

    def __init__(self):
        self._next_id = 0

    def add_hook(self, _spec):
        """Return the id of a new hook."""
        self._next_id += 1
        return self._next_id

    @staticmethod
    def isRunning():  # pylint: disable=invalid-name
        """The panel takes a running worker as its own."""
        return True

    def __getattr__(self, name):
        # requestInterruption, wait, set_debug, set_enabled, …: nothing to do.
        return lambda *args, **kwargs: None


_XP_SOURCE = """function (ctx) {
  if (ctx.delta <= 0)
    return;
  ctx.log(`XP +${ctx.delta.toFixed(1)} → +${(ctx.delta * 2).toFixed(1)}`);
  return ctx.old + ctx.delta * 2;
}"""


def _install(panel, spec, instruction, trampoline):
    """Add ``spec`` and report it installed at ``instruction`` as the agent would."""
    offset, length, text = instruction
    hook_id = panel.add_hook(spec, _site(offset), saved_id=0)
    address = MODULE_BASE + offset
    panel._on_installed(panel._worker, hook_id, {
        "writeInstruction": {
            "address": f"{address:#x}", "site": _site(offset), "module": DEMO_NAME,
            "moduleOffset": f"{offset:#x}", "text": text, "size": length,
        },
        "trampoline": f"{trampoline:#x}", "resumePc": f"{address + length:#x}",
        "patchSize": 5,
    })
    return hook_id


def _hooks(window):
    """Three hooks of the test game: gold ×10, a mana floor (paused), XP ×2 with a log."""
    panel = window.hook_panel
    panel._worker = _StubHookWorker()
    multiplier = _template(window, "Multiplier")
    clamp = _template(window, "Clamp")
    experience = TransformDefinition(
        name="Double XP gains", source=_XP_SOURCE,
        description="Doubles every gain and logs it")
    gold = _install(panel, HookSpec(
        GOLD, "int32", TemplateTransform(multiplier, factor=10, direction="increase"),
        module=DEMO_NAME, module_offset=ADD_GOLD[0], pointer=GOLD_CHAIN, label="Gold ×10",
    ), ADD_GOLD, 0x7F3B_1D40_0000)
    mana = _install(panel, HookSpec(
        MANA, "float", TemplateTransform(clamp, minimum=40, maximum=None),
        module=DEMO_NAME, module_offset=CHANGE_MANA[0], pointer=MANA_CHAIN,
        label="Mana at least 40", enabled=False,
    ), CHANGE_MANA, 0x7F3B_1D40_1000)
    xp = _install(panel, HookSpec(
        EXPERIENCE, "double", TemplateTransform(experience),
        module=DEMO_NAME, module_offset=ADD_EXPERIENCE[0], pointer=EXPERIENCE_CHAIN,
        label="Experience ×2",
    ), ADD_EXPERIENCE, 0x7F3B_1D40_2000)
    panel._on_events(panel._worker, [
        {"hookId": gold, "count": 4, "last": {"old": 1100, "new": 1125, "result": 1350}},
        {"hookId": mana, "count": 14, "last": {"old": 41.04, "new": 33.54, "result": 40}},
        {"hookId": xp, "count": 3, "last": {"old": 387.8, "new": 400.2, "result": 412.6}},
    ])
    return panel, {"gold": gold, "mana": mana, "xp": xp}


_XP_GAINS = ((61.4, 12.7), (88.0, 10.3), (97.5, 12.4))


@scene("hooks-tab", size=(1500, 600),
       description="The Hooks tab with three hooks of the test game and their log")
def hooks_tab(studio):
    """The main window with the Hooks tab: active, paused and logging hooks."""
    window = _own_window(studio)
    panel, ids = _hooks(window)
    panel._on_hook_logs(panel._worker, [
        {"hookId": ids["xp"], "time": _moment(seconds),
         "text": f"XP +{gain:.1f} → +{gain * 2:.1f}"}
        for seconds, gain in _XP_GAINS
    ], {})
    window.bottom_tabs.setCurrentWidget(panel)
    # The write site with its instruction gets the room, the manipulation
    # is cut instead (its tooltip has it all).
    header = panel.table.horizontalHeader()
    header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
    panel.table.setColumnWidth(3, 250)
    panel.table.resizeRowsToContents()
    panel.table.selectRow(0)
    window.lbl_status.setText(studio.translations["hooks.status_created"].format(
        address=f"0x{GOLD:X}", transform=panel._hooks[ids["gold"]]["spec"].transform.describe()))
    # All the height for the hooks and their log; the shared setting stays.
    window._set_scan_area_visible(False)
    window._settings.setValue("scan_area_visible", True)
    settle()
    return window


def _debug_events(ids):
    """What the agent reports for the gold and experience hooks in debug mode."""
    gold = f"{GOLD:#x}"
    caller = _site(0x2B6E)  # on_gold_add, the button handler
    events = []
    # Between the second and third treasure the player spent 120 gold
    # (remove_gold, which the hook leaves alone).
    for seconds, old, found in ((21.6, 520, 23), (29.1, 750, 27), (34.8, 900, 20),
                                (41.2, 1100, 25)):
        events.append({"hookId": ids["gold"], "time": _moment(seconds), "threadId": 48213,
                       "address": gold, "caller": caller,
                       "old": old, "new": old + found, "result": old + found * 10})
    experience = 341.8
    for seconds, gain in _XP_GAINS:
        events.append({"hookId": ids["xp"], "time": _moment(seconds + 0.4),
                       "threadId": 48213, "address": f"{EXPERIENCE:#x}",
                       "caller": _site(0x2C31),  # on_fight
                       "old": round(experience, 1), "new": round(experience + gain, 1),
                       "result": round(experience + 2 * gain, 1),
                       "logs": [f"XP +{gain:.1f} → +{gain * 2:.1f}"]})
        experience += 2 * gain
    events.sort(key=lambda event: event["time"])
    return events


@scene("hook-debug", size=(1240, 600),
       description="The debug window logging every call of the hooks")
def hook_debug(studio):
    """Debug mode for the gold and experience hooks after a few treasures and fights."""
    window = _own_window(studio)
    panel, ids = _hooks(window)
    for key in ("gold", "xp"):
        panel._set_debug_flag(ids[key], True)
    debug = panel.debug_window()
    debug.add_events(_debug_events(ids))
    debug.table.selectRow(debug.table.rowCount() - 1)
    # Room for all lines of the selected call's details.
    debug.table.parentWidget().setSizes([380, 200])
    studio.keep(debug)
    return debug


# ----------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------
@scene("hook-templates", size=(1060, 700),
       description="The hook templates with the Multiplier selected")
def hook_templates(studio):
    """The template editor with the example Multiplier."""
    window = studio.show_attached()
    dialog = studio.keep(TemplateDialog(studio.translations, window._hook_repository, window))
    names = [dialog.template_list.item(row).text()
             for row in range(dialog.template_list.count())]
    dialog.template_list.setCurrentRow(names.index("Multiplier"))
    return dialog


# ----------------------------------------------------------------------
# Pointer scan
# ----------------------------------------------------------------------
class _PointerRepository:
    """Holds the candidates of the gold after two filter rounds."""

    # In the order a scan saves them (shortest first); the dialog sorts them
    # by their score.
    CHAINS = [
        PointerChain(DEMO_NAME, 0x4400, (0x7E8, 0x0)),
        GOLD_CHAIN,
        PointerChain(DEMO_NAME, 0x40A0, (0x7E8, 0x28, 0x0)),
        PointerChain(DEMO_NAME, 0x4058, (0x0, 0x28, 0x0)),
        PointerChain(DEMO_NAME, 0x4130, (0x10, 0x5B0, 0x28, 0x0)),
        PointerChain(DEMO_NAME, 0x4130, (0x14, 0x5B0, 0x28, 0x0)),
    ]

    @classmethod
    def get_pointer_scan(cls, _entry_id):
        """The saved candidates of an address-list entry."""
        return SavedPointerScan(list(cls.CHAINS), 5, 0x1000, 2)

    @staticmethod
    def save_pointer_scan(_entry_id, _scan):
        """Nothing is saved."""


def _module(start, size, path, permissions="rw-p"):
    name = path.rsplit("/", 1)[-1]
    return Mapping(start, start + size, permissions, path, name, permissions.startswith("rw"))


def _anonymous(start, size, path=""):
    return Mapping(start, start + size, "rw-p", path, None, True)


_GAME_FOLDER = "/home/player/Games/dummy_game"
_DEMO_MAPPINGS = [
    _module(MODULE_BASE, 0x4000, f"{_GAME_FOLDER}/{DEMO_NAME}", "r-xp"),
    _module(MODULE_BASE + 0x4000, 0x2000, f"{_GAME_FOLDER}/{DEMO_NAME}"),
    _module(0x5615_3900_0000, 0x18000, f"{_GAME_FOLDER}/libsave.so", "r-xp"),
    _module(0x5615_3901_8000, 0x1000, f"{_GAME_FOLDER}/libsave.so"),
    _anonymous(0x5615_3A2C_0000, 0x21000, "[heap]"),
    _anonymous(0x7F3A_0000_0000, 0x4000000),
    _anonymous(0x7F3A_1C00_0000, 0x1000000),
    _module(0x7F3A_2000_0000, 0x400000, "/usr/lib64/libgtk-3.so.0", "r-xp"),
    _module(0x7F3A_2040_0000, 0x9000, "/usr/lib64/libgtk-3.so.0"),
    _module(0x7F3A_2100_0000, 0x120000, "/usr/lib64/libglib-2.0.so.0", "r-xp"),
    _module(0x7F3A_2112_0000, 0x3000, "/usr/lib64/libglib-2.0.so.0"),
    _module(0x7F3A_2200_0000, 0x1D0000, "/usr/lib64/libc.so.6", "r-xp"),
    _module(0x7F3A_221D_0000, 0x15000, "/usr/lib64/libc.so.6"),
    _anonymous(0x7FFD_4E00_0000, 0x21000, "[stack]"),
]


@scene("pointer-scan", size=(860, 720),
       description="Pointer scan for the gold: candidates left after two restarts")
def pointer_scan_dialog(studio):
    """The pointer scan of the gold after filtering twice; the stable chain is selected."""
    window = studio.show_attached()
    # Read no process: the demo program does not run. Its memory, as the
    # memory groups show it:
    pointer_scan.main_module = lambda _pid: DEMO_NAME
    pointer_scan.read_mappings = lambda _pid: _DEMO_MAPPINGS
    pointer_scan.thread_stack_pointers = lambda _pid: [0x7F3A_1C7F_E000]
    pointer_scan.pointer_size = lambda _pid: 8
    entry = {"id": 1, "label": "Gold", "address": GOLD}
    dialog = studio.keep(pointer_scan.PointerScanDialog(
        studio.translations, _PointerRepository(), 48213, entry, window,
        address_source=lambda: GOLD))
    dialog._show_candidates(studio.translations["pointer.filtered"].format(
        before=1843, count=len(_PointerRepository.CHAINS), rounds=2))
    dialog.table.selectRow(0)
    return dialog
