"""The engine tools: processes, catalog, trace, monitor, inspector, calls and GAS.

The windows here start workers that attach to the game when they open. The
scenes build them with idle workers instead (``_idle_workers``: the worker
classes are swapped for subclasses whose thread never starts, only while
the window is built) and feed them the answers those workers would send.
"""

import itertools
import random
from contextlib import contextmanager
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal  # pylint: disable=import-error,no-name-in-module
from PySide6.QtWidgets import QSplitter  # pylint: disable=import-error,no-name-in-module

from hermes.db.repository.assembly_selections import AssemblySelectionRepository
from hermes.db.repository.favorites import FavoriteRepository
from hermes.db.repository.hooks import HookRepository
from hermes.engines.registry import engine_by_id
from hermes.features.call_graph import Call
from hermes.features.catalog import CatalogMethod
from hermes.features.favorites import CATALOG, NATIVE_TRACE, TRACE
from hermes.features.favorites import favorite_from_function, favorite_from_method
from hermes.features.method_call import R4
from hermes.features.native_trace import NativeFunction
from hermes.ui.widgets import assemblies as assemblies_widget
from hermes.ui.widgets import call_graph as call_graph_widget
from hermes.ui.widgets import catalog as catalog_widget
from hermes.ui.widgets import method_call as method_call_widget
from hermes.ui.widgets import method_monitor as method_monitor_widget
from hermes.ui.widgets import process_list as process_list_widget
from hermes.ui.widgets import trace as trace_widget
from hermes.ui.widgets.gas_attributes import hold_spec
from hermes.ui.widgets.object_inspector import ObjectInspectorDialog
from studio import scene, settle

# pylint: disable=protected-access  # the scenes answer for the workers

MONO = engine_by_id("mono")
UNREAL = engine_by_id("unreal")

# The games, as the process list shows them (Proton: the Windows program)
_STEAM = r"Z:\home\user\.local\share\Steam\steamapps\common"
# Pillars of Eternity runs as a native Linux build
_LINUX_STEAM = "/home/user/.local/share/Steam/steamapps/common"
_POE_NAME = "PillarsOfEternity"
_POE_PATH = _LINUX_STEAM + "/Pillars of Eternity/PillarsOfEternity"
_POE_PID = 51874
_POE_DETAILS = "64-bit, Unity Mono"
_AVOWED_NAME = "Avowed-Win64-Shipping.exe"
_AVOWED_PATH = _STEAM + r"\Avowed\Alabama\Binaries\Win64\Avowed-Win64-Shipping.exe"
_AVOWED_PID = 60317
_AVOWED_DETAILS = "64-bit, Unreal Engine"
_MANAGED = _LINUX_STEAM + "/Pillars of Eternity/PillarsOfEternity_Data/Managed"

# The evening the screenshots were taken: call times of the monitor
_EVENING = datetime(2026, 11, 7, 21, 14, 5).timestamp() * 1000
# The game's main thread: on Linux its id is the process id
_MAIN_THREAD = _POE_PID


@contextmanager
def _idle_workers(*places):
    """Swap ``(module, name)`` worker classes for ones whose thread never starts.

    Only while the window is built: its worker is made then, and the
    classes are back for whatever renders next.
    """
    saved = [(module, name, getattr(module, name)) for module, name in places]
    for module, name, worker_class in saved:
        setattr(module, name, type(f"Idle{worker_class.__name__}", (worker_class,),
                                   {"start": lambda self, *_args: None}))
    try:
        yield
    finally:
        for module, name, worker_class in saved:
            setattr(module, name, worker_class)


def _method(class_name, name, parameters="()", returns="System.Void", static=False,
            assembly="Assembly-CSharp", names=()):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    token = 0x06000000 + next(_TOKENS)
    return CatalogMethod(assembly, class_name, name, parameters, returns, static, token,
                         f"{class_name}:{name} {parameters}", "mono", tuple(names))


_TOKENS = itertools.count(0x2a10, 7)

# --- Pillars of Eternity: the classes the shots look at ----------------------------

_PLAYER_INVENTORY = [
    _method("PlayerInventory", ".ctor"),
    _method("PlayerInventory", "Awake"),
    _method("PlayerInventory", "Start"),
    _method("PlayerInventory", "OnDestroy"),
    _method("PlayerInventory", "Restored"),
    _method("PlayerInventory", "get_Instance", returns="PlayerInventory", static=True),
    _method("PlayerInventory", "get_currencyTotalValue", returns="System.Single"),
    _method("PlayerInventory", "set_currencyTotalValue", "(single)", names=("value",)),
    _method("PlayerInventory", "AddCurrency", "(int)", names=("amount",)),
    _method("PlayerInventory", "RemoveCurrency", "(int)", "System.Boolean", names=("amount",)),
    _method("PlayerInventory", "CanAfford", "(int)", "System.Boolean", names=("cost",)),
    _method("PlayerInventory", "GetCurrencyString", returns="System.String"),
    _method("PlayerInventory", "AddItem", "(Item,int,int,bool)", "System.Int32",
            names=("item", "quantity", "slot", "playSound")),
    _method("PlayerInventory", "RemoveItem", "(Item,int)", "System.Int32",
            names=("item", "quantity")),
    _method("PlayerInventory", "HasItem", "(string)", "System.Boolean", names=("itemName",)),
    _method("PlayerInventory", "ItemCount", "(string)", "System.Int32", names=("itemName",)),
    _method("PlayerInventory", "TransferToStash", "(InventoryItem)", "System.Boolean",
            names=("item",)),
    _method("PlayerInventory", "SortInventory"),
    _method("PlayerInventory", "get_MaxItems", returns="System.Int32"),
    _method("PlayerInventory", "OnLoad"),
    _method("PlayerInventory", "<SortInventory>m__1A4", "(InventoryItem,InventoryItem)",
            "System.Int32", static=True),
]
_BY_NAME = {f"{m.class_name}:{m.name}": m for m in _PLAYER_INVENTORY}

_STORE = [
    _method("UIStoreStorePage", "FinalizeTransaction"),
    _method("UIStoreStorePage", "OnConfirmClicked", "(UnityEngine.GameObject)", names=("go",)),
    _method("UIStoreStorePage", "RefreshTotals"),
    _method("UIStoreStorePage", "Update"),
    _method("UIStoreStorePage", "CancelTransaction"),
    _method("StoreInventory", "RemoveItem", "(Item,int)", "System.Int32",
            names=("item", "quantity")),
    _method("StoreInventory", "GetBuyPrice", "(Item)", "System.Int32", names=("item",)),
    _method("Inventory", "AddItem", "(Item,int,int,bool)", "System.Int32",
            names=("item", "quantity", "slot", "playSound")),
    _method("Inventory", "get_ItemList",
            returns="System.Collections.Generic.List`1<InventoryItem>"),
    _method("UIInventoryGoldCounter", "Update"),
    _method("UIInventoryGoldCounter", "OnCurrencyChanged", "(single)", names=("total",)),
    _method("GlobalAudioPlayer", "Play", "(UIAudioList/UIAudioType)", static=True,
            names=("type",)),
    _method("Health", "DoDamage", "(DamageInfo,UnityEngine.GameObject)", "System.Single",
            names=("damage", "attacker")),
    _method("DamagePacket", "RollDamage", "(CharacterStats,single)", "System.Single",
            names=("roller", "mult")),
]
_BY_NAME.update({f"{m.class_name}:{m.name}": m for m in _STORE})

# Further classes, so the catalog is as big as a game's
_CLASSES = (
    "AIController", "AttackBase", "AttackMelee", "AttackRanged", "CameraControl",
    "CharacterStats", "Consumable", "Container", "ConversationManager", "DamageInfo",
    "Equipment", "Equippable", "Faction", "FogOfWar", "GameInput", "GameState",
    "GenericAbility", "GenericSpell", "GlobalVariables", "Item", "Loot", "Mover",
    "PartyMemberAI", "Persistence", "PersistenceManager", "QuestManager", "Reputation",
    "SaveGameInfo", "StatusEffect", "Stronghold", "UICharacterSheetManager",
    "UIInventoryGridItem", "UIInventoryManager", "Weapon",
)
_MEMBERS = (
    ("Awake", "()", "System.Void"), ("Start", "()", "System.Void"),
    ("Update", "()", "System.Void"), ("OnDestroy", "()", "System.Void"),
    ("OnEnable", "()", "System.Void"), ("OnDisable", "()", "System.Void"),
    ("Restored", "()", "System.Void"), ("get_IsPartyMember", "()", "System.Boolean"),
    ("get_Owner", "()", "UnityEngine.GameObject"), ("Refresh", "()", "System.Void"),
    ("Apply", "(CharacterStats)", "System.Void"), ("GetValue", "(int)", "System.Single"),
    ("SetValue", "(int,single)", "System.Void"), ("Reset", "()", "System.Void"),
    ("Load", "(string)", "System.Boolean"), ("Save", "()", "System.Void"),
    ("Tick", "(single)", "System.Void"), ("HandleEvent", "(GameEventArgs)", "System.Void"),
)


def _game_methods():
    """Every method of Assembly-CSharp the catalog lists."""
    pick = random.Random(7)
    methods = list(_STORE) + list(_PLAYER_INVENTORY)
    for class_name in _CLASSES:
        for name, parameters, returns in pick.sample(_MEMBERS, pick.randint(9, 16)):
            methods.append(_method(class_name, name, parameters, returns))
    return methods


_POE_ASSEMBLIES = (
    ("Assembly-CSharp", 4218), ("Assembly-CSharp-firstpass", 611),
    ("Assembly-UnityScript-firstpass", 14), ("ICSharpCode.SharpZipLib", 118),
    ("Mono.Security", 161), ("mscorlib", 1836), ("Polenter.SharpSerializer", 94),
    ("System", 818), ("System.Core", 307), ("System.Xml", 1121), ("UnityEngine", 412),
    ("UnityEngine.UI", 168),
)


def _assemblies():
    return [{"name": name, "classes": classes, "file": f"{_MANAGED}/{name}.dll"}
            for name, classes in _POE_ASSEMBLIES]


def _poe_process():
    """The database id of the game, with its favorites (made once per run)."""
    process_id = HookRepository().get_or_create_process(_POE_NAME, _POE_PATH)
    repository = FavoriteRepository()
    if repository.list_favorites(process_id):
        return process_id
    for method, source, note in (
            (_BY_NAME["PlayerInventory:AddCurrency"], TRACE,
             "Every gold change: shop, loot, quest rewards"),
            (_BY_NAME["UIStoreStorePage:FinalizeTransaction"], CATALOG,
             "Runs once per purchase"),
            (_BY_NAME["Health:DoDamage"], CATALOG, "Returns the damage dealt; this = victim"),
            (_BY_NAME["DamagePacket:RollDamage"], TRACE, "Damage before DR, hookable"),
            (_BY_NAME["PlayerInventory:get_Instance"], CATALOG, ""),
    ):
        favorite_id = repository.add_favorite(process_id, favorite_from_method(method, source))
        if note:
            repository.set_note(favorite_id, note)
    native = NativeFunction(0xDC3A10, _POE_NAME, 0x9C3A10)
    favorite_id = repository.add_favorite(process_id,
                                          favorite_from_function(native, NATIVE_TRACE))
    repository.set_note(favorite_id, "Writes the gold float (movss)")
    return process_id


def _favorites(studio):
    """The main window's favorites, showing the game's."""
    store = studio.window._favorite_store
    store.set_process(_poe_process())
    return store


# --- Process list --------------------------------------------------------------------

class _Process:  # pylint: disable=too-few-public-methods
    """What psutil.process_iter yields, as far as the dialog asks."""

    def __init__(self, pid, name, exe, cmdline, cwd="/"):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        self.info = {"pid": pid, "name": name, "exe": exe, "cmdline": cmdline, "cwd": cwd}


_PROTON = "/home/user/.local/share/Steam/steamapps/common/Proton - Experimental/files/bin"


def _wine(pid, thread_name, program, *args):
    return _Process(pid, thread_name, f"{_PROTON}/wine64-preloader", [program, *args])


_PROCESSES = [
    _Process(1, "systemd", "/usr/lib/systemd/systemd", ["/usr/lib/systemd/systemd"]),
    _Process(1873, "pipewire", "/usr/bin/pipewire", ["/usr/bin/pipewire"]),
    _Process(2011, "kwin_wayland", "/usr/bin/kwin_wayland", ["/usr/bin/kwin_wayland"]),
    _Process(2088, "plasmashell", "/usr/bin/plasmashell", ["/usr/bin/plasmashell"]),
    _Process(2604, "konsole", "/usr/bin/konsole", ["/usr/bin/konsole"]),
    _Process(3120, "firefox", "/usr/lib64/firefox/firefox", ["/usr/lib64/firefox/firefox"]),
    _Process(4471, "steam", "/home/user/.local/share/Steam/ubuntu12_32/steam",
             ["/home/user/.local/share/Steam/ubuntu12_32/steam"]),
    _Process(4630, "steamwebhelper",
             "/home/user/.local/share/Steam/ubuntu12_64/steamwebhelper",
             ["/home/user/.local/share/Steam/ubuntu12_64/steamwebhelper"]),
    _Process(48213, "dummy_game.exe", "/home/user/hermes-hook/build/dummy_game.exe",
             ["./dummy_game.exe"], "/home/user/hermes-hook/build"),
    _Process(51790, "wineserver", f"{_PROTON}/wineserver", [f"{_PROTON}/wineserver"]),
    _wine(51802, "services.exe", r"C:\windows\system32\services.exe"),
    _wine(51811, "explorer.exe", r"C:\windows\system32\explorer.exe", "/desktop"),
    _Process(_POE_PID, _POE_NAME, _POE_PATH, [_POE_PATH], _LINUX_STEAM + "/Pillars of Eternity"),
    _wine(60288, "Avowed.exe", _STEAM + r"\Avowed\Avowed.exe"),
    _wine(_AVOWED_PID, "GameThread", _AVOWED_PATH),
    _wine(63954, "VRising.exe", _STEAM + r"\VRising\VRising.exe"),
    _wine(64102, "VRisingServer.e", _STEAM + r"\VRising\VRising_Server\VRisingServer.exe",
          "-batchMode", "-nographics"),
    _Process(66530, "python3", "/usr/bin/python3.14", ["python3", "main.py"],
             "/home/user/hermes-hook"),
]


@scene("process-list", size=(980, 600),
       description="Choosing the game to attach to, Proton games by their Windows program")
def process_list(studio):
    """The process dialog with a few Proton games, the test game and the desktop."""
    original = process_list_widget.psutil.process_iter
    process_list_widget.psutil.process_iter = lambda _attributes: iter(_PROCESSES)
    try:
        dialog = studio.keep(process_list_widget.ProcessSelectDialog(
            studio.window, studio.translations))
    finally:
        process_list_widget.psutil.process_iter = original
    header = dialog.list.horizontalHeader()
    header.resizeSection(0, 80)
    header.resizeSection(1, 220)
    row = next(row for row in range(dialog.list.rowCount())
               if dialog.list.item(row, 1).text() == _POE_NAME)
    dialog.list.selectRow(row)
    return dialog


# --- Catalog -----------------------------------------------------------------------------

@scene("catalog", size=(1180, 720),
       description="The C# methods of a Unity Mono game, searched for a class")
def catalog(studio):
    """The catalog of Pillars of Eternity, narrowed to PlayerInventory."""
    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)
    favorites = _favorites(studio)
    with _idle_workers((catalog_widget, "CatalogWorker")):
        dialog = studio.keep(catalog_widget.CatalogDialog(
            studio.translations, _POE_PID, parent=studio.window, engine=MONO,
            favorites=favorites))
    found = _assemblies()
    dialog._on_assemblies_found(found)
    methods = _game_methods()
    dialog._on_assembly_loaded("Assembly-CSharp", methods)
    firstpass = [_method(class_name, name, parameters, returns,
                         assembly="Assembly-CSharp-firstpass")
                 for class_name in ("UIPanel", "UILabel", "UISprite", "UIButton", "UIGrid",
                                    "UITweener", "UICamera", "UIRoot", "UIWidget", "UITable")
                 for name, parameters, returns in _MEMBERS[:12]]
    dialog._on_assembly_loaded("Assembly-CSharp-firstpass", firstpass)
    dialog._on_assembly_loaded("Assembly-UnityScript-firstpass", [])
    dialog._on_assembly_loaded("ICSharpCode.SharpZipLib", [])
    dialog._on_assembly_loaded("Polenter.SharpSerializer", [])
    for assembly in ("Assembly-CSharp", "Assembly-CSharp-firstpass"):
        dialog._cached.add(assembly)
    dialog._on_worker_finished(dialog._worker)
    dialog.search_input.setText("PlayerInventory")
    dialog._filter_timer.stop()
    dialog._apply_filter()
    header = dialog.view.horizontalHeader()
    header.resizeSection(catalog_widget._RETURN, 230)
    header.resizeSection(catalog_widget._ASSEMBLY, 150)
    row = next(row for row, method in enumerate(dialog.model.visible)
               if method.name == "AddCurrency")
    dialog.view.selectRow(row)
    dialog.view.setFocus()
    return dialog


# --- Trace -------------------------------------------------------------------------------

# A purchase in a shop: (time in microseconds, method, caller) in the order called;
# methods the baseline called too are only counted.
_PURCHASE = (
    (1_204_118, "PlayerInventory:CanAfford", "UIStoreStorePage:OnConfirmClicked+0x4e"),
    (1_204_160, "StoreInventory:RemoveItem", "UIStoreStorePage:FinalizeTransaction+0x2b8"),
    (1_204_391, "Inventory:AddItem", "UIStoreStorePage:FinalizeTransaction+0x31a"),
    (1_204_655, "PlayerInventory:RemoveCurrency", "UIStoreStorePage:FinalizeTransaction+0x5f0"),
    (1_204_662, "PlayerInventory:AddCurrency", "PlayerInventory:RemoveCurrency+0x3a"),
    (1_204_671, "PlayerInventory:set_currencyTotalValue", "PlayerInventory:AddCurrency+0x41"),
    (1_204_690, "UIInventoryGoldCounter:OnCurrencyChanged",
     "PlayerInventory:set_currencyTotalValue+0x57"),
)
# Calls in the scenario (4 s) and in the baseline (10 s)
_SCENARIO_COUNTS = {
    "UIInventoryGoldCounter:Update": 240,
    "PlayerInventory:CanAfford": 1, "PlayerInventory:get_currencyTotalValue": 44,
    "StoreInventory:RemoveItem": 1, "Inventory:AddItem": 1, "Inventory:get_ItemList": 82,
    "PlayerInventory:RemoveCurrency": 1, "PlayerInventory:AddCurrency": 1,
    "PlayerInventory:set_currencyTotalValue": 1, "UIInventoryGoldCounter:OnCurrencyChanged": 1,
    "StoreInventory:GetBuyPrice": 12,
}
_BASELINE_COUNTS = {
    "UIInventoryGoldCounter:Update": 600,
    "PlayerInventory:get_currencyTotalValue": 20, "Inventory:get_ItemList": 50,
    "StoreInventory:GetBuyPrice": 31,
}


@scene("trace", size=(1200, 1000),
       description="Mono trace: the methods a purchase called, compared with a baseline")
def trace(studio):
    """A trace of Pillars of Eternity after a baseline and the scenario "pay gold"."""
    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)
    favorites = _favorites(studio)
    with _idle_workers((trace_widget, "TraceWorker")):
        dialog = studio.keep(trace_widget.TraceDialog(
            studio.translations, _POE_PID, parent=studio.window, engine=MONO,
            favorites=favorites))
    candidates = _game_methods()
    dialog._on_candidates(candidates, {})
    panel = dialog.filter_panel
    for row in range(panel.list.count()):
        item = panel.list.item(row)
        if item.data(Qt.ItemDataRole.UserRole).builtin in ("money", "inventory"):
            item.setCheckState(Qt.CheckState.Checked)
    chosen = panel.selection([])
    index = {f"{m.class_name}:{m.name}": i for i, m in enumerate(chosen)}
    for name in list(_SCENARIO_COUNTS) + [name for _time, name, _caller in _PURCHASE]:
        if name not in index:
            raise LookupError(f"the money and inventory filters do not choose {name}")
    dialog._on_started(chosen, {"skipped": {"abstract": 3, "generic": 2}})
    dialog._on_recorded({"phase": "baseline", "duration": 10_000, "events": [],
                         "counts": {str(index[name]): calls
                                    for name, calls in _BASELINE_COUNTS.items()}})
    dialog._on_recorded({
        "phase": "scenario", "duration": 4_000,
        "counts": {str(index[name]): calls for name, calls in _SCENARIO_COUNTS.items()},
        "events": [[index[name], _MAIN_THREAD, time, caller]
                   for time, name, caller in _PURCHASE],
    })
    header = dialog.rows_view.horizontalHeader()
    header.resizeSection(trace_widget._R_BASELINE, 150)
    sequence = dialog.sequence_view.horizontalHeader()
    sequence.resizeSection(trace_widget._S_METHOD, 360)
    row = next(row for row, item in enumerate(dialog.row_model.items)
               if chosen[item.method].name == "AddCurrency")
    dialog.rows_view.selectRow(row)
    dialog.rows_view.scrollToTop()
    dialog.rows_view.setFocus()
    dialog.findChild(QSplitter).setSizes([520, 300])
    return dialog


# --- Method monitor -----------------------------------------------------------------------

def _object(type_name, name, address):
    text = f'{type_name} "{name}"@{address}' if name else f"{type_name}@{address}"
    return {"text": text, "target": {"address": address}}


_EDER = "Companion_Eder(Clone)_0"
_PLAYER = "Player_New_Game(Clone)_0"


def _monitor_calls(ids):
    """The calls of a fight after a purchase: (monitor, seconds, this, args, result, caller)."""
    inventory = _object("PlayerInventory", _PLAYER, "0x2a41c7f3e40")
    store = _object("UIStoreStorePage", "StorePage", "0x2a41d0218a0")
    xaurip = _object("UnityEngine.GameObject", "CRE_Xaurip_Skirmisher(Clone)_3", "0x2a42a1c5b80")
    eder = _object("Health", _EDER, "0x2a41c80a1e0")
    enemy = _object("Health", "CRE_Xaurip_Skirmisher(Clone)_3", "0x2a42a1c7f00")
    calls = [
        (ids["store"], 0.000, store, [], None, "UIStoreStorePage:OnConfirmClicked+0x4e"),
        (ids["gold"], 0.012, inventory, [("amount", "-250")], None,
         "PlayerInventory:RemoveCurrency+0x3a"),
        (ids["damage"], 41.207, eder, [
            ("damage", _object("DamageInfo", None, "0x2a43b0e1d20")), ("attacker", xaurip)],
         "12.5", "AttackMelee:OnImpact+0x1c4"),
        (ids["damage"], 41.982, enemy, [
            ("damage", _object("DamageInfo", None, "0x2a43b0e2a80")),
            ("attacker", _object("UnityEngine.GameObject", _EDER, "0x2a41c809f00"))],
         "18.75", "AttackMelee:OnImpact+0x1c4"),
        (ids["damage"], 43.316, eder, [
            ("damage", _object("DamageInfo", None, "0x2a43b0e3c40")), ("attacker", xaurip)],
         "9.0", "AttackMelee:OnImpact+0x1c4"),
        (ids["damage"], 44.105, enemy, [
            ("damage", _object("DamageInfo", None, "0x2a43b0e4e00")),
            ("attacker", _object("UnityEngine.GameObject", _PLAYER, "0x2a41c7f1a00"))],
         "23.25", "AttackRanged:OnImpact+0x2a8"),
        (ids["damage"], 45.660, enemy, [
            ("damage", _object("DamageInfo", None, "0x2a43b0e5fc0")),
            ("attacker", _object("UnityEngine.GameObject", _EDER, "0x2a41c809f00"))],
         "0.0", "AttackMelee:OnImpact+0x1c4"),
        (ids["gold"], 47.918, inventory, [("amount", "75")], None, "Loot:TakeAll+0x9d"),
        (ids["gold"], 47.921, inventory, [("amount", "12")], None, "Loot:TakeAll+0x9d"),
    ]
    result = []
    for monitor_id, seconds, this, args, value, caller in calls:
        call = {"monitorId": monitor_id, "time": _EVENING + seconds * 1000,
                "threadId": _MAIN_THREAD, "self": this, "caller": caller,
                "args": [dict(arg, name=name) if isinstance(arg, dict)
                         else {"name": name, "text": arg} for name, arg in args]}
        if value is not None:
            call["result"] = {"text": value}
        result.append(call)
    return result


@scene("method-monitor", size=(1480, 860),
       description="Method monitor: the calls of C# methods with this, arguments and result")
def method_monitor(studio):
    """Three monitored methods of Pillars of Eternity after a purchase and a fight."""
    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)
    with _idle_workers((method_monitor_widget, "MethodMonitorWorker")):
        dialog = studio.keep(method_monitor_widget.MethodMonitorDialog(
            studio.translations, _POE_PID, parent=studio.window))
    ids = {}
    for key, name, info in (
            ("gold", "PlayerInventory:AddCurrency", {
                "address": "0x2a43f6b1a40", "params": ["System.Int32 amount"],
                "returns": "System.Void", "recordsResults": False, "hasResults": False,
                "recordsObjects": False, "hasObjects": True}),
            ("store", "UIStoreStorePage:FinalizeTransaction", {
                "address": "0x2a43f7c0e10", "params": [], "returns": "System.Void",
                "recordsResults": False, "hasResults": False, "recordsObjects": False,
                "hasObjects": True}),
            ("damage", "Health:DoDamage", {
                "address": "0x2a43f80d5c0",
                "params": ["DamageInfo damage", "UnityEngine.GameObject attacker"],
                "returns": "System.Single", "recordsResults": True, "hasResults": True,
                "recordsObjects": False, "hasObjects": True})):
        before = set(dialog._monitors)
        dialog.add_method(_BY_NAME[name])
        (ids[key],) = set(dialog._monitors) - before
        dialog._on_added(ids[key], info)
    calls = _monitor_calls(ids)
    counts = {}
    for call in calls:
        counts[call["monitorId"]] = counts.get(call["monitorId"], 0) + 1
    dialog._on_calls(calls, {}, counts)
    header = dialog.log_view.horizontalHeader()
    for column, width in ((method_monitor_widget._C_METHOD, 270),
                          (method_monitor_widget._C_SELF, 330),
                          (method_monitor_widget._C_RESULT, 80),
                          (method_monitor_widget._C_CALLER, 300)):
        header.resizeSection(column, width)
    dialog.methods_table.selectRow(2)
    dialog.findChild(QSplitter).setSizes([200, 600])
    row = next(row for row, call in enumerate(dialog.log.calls)
               if call["monitorId"] == ids["damage"] and call["result"]["text"] == "23.25")
    dialog.log_view.selectRow(row)
    dialog.log_view.setFocus()
    return dialog


# --- Object inspector -----------------------------------------------------------------------

def _field(name, type_name, offset, text, value_type=None, address=None, target=None,
           static=False, storage=None):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    value = {"text": text}
    if target is not None:
        value["target"] = {"address": target}
    return {"name": name, "type": type_name, "offset": offset, "static": static,
            "storage": storage, "value": value, "address": address, "valueType": value_type}


_HEALTH = 0x2A41C80A1E0


def _health_contents():
    """Eder's Health component, as the agent reads it."""

    def at(offset):
        return hex(_HEALTH + offset)

    return {
        "name": "Health", "instanceName": _EDER, "address": hex(_HEALTH),
        "fields": [
            _field("m_CachedPtr", "System.IntPtr", 0x10, "0x2a3f1b2c600", None, at(0x10)),
            _field("m_stats", "CharacterStats", 0x18,
                   f'CharacterStats "{_EDER}"@0x2a41c80b4c0', None, at(0x18),
                   "0x2a41c80b4c0"),
            _field("OnDamaged", "Health/DamageEvent", 0x20, "Health/DamageEvent@0x2a43e019a80",
                   None, at(0x20), "0x2a43e019a80"),
            _field("OnDeath", "Health/DeathEvent", 0x28, "null", None, at(0x28)),
            _field("m_lastAttacker", "UnityEngine.GameObject", 0x30,
                   'UnityEngine.GameObject "CRE_Xaurip_Skirmisher(Clone)_3"@0x2a42a1c5b80',
                   None, at(0x30), "0x2a42a1c5b80"),
            _field("m_currentHealth", "System.Single", 0x38, "212.5", "float", at(0x38)),
            _field("m_currentStamina", "System.Single", 0x3C, "41.25", "float", at(0x3C)),
            _field("m_staminaRechargeDelay", "System.Single", 0x40, "3", "float", at(0x40)),
            _field("m_damageTakenThisFrame", "System.Single", 0x44, "0", "float", at(0x44)),
            _field("m_deathCount", "System.Int32", 0x48, "0", "int32", at(0x48)),
            _field("m_isDead", "System.Boolean", 0x4C, "false", None, at(0x4C)),
            _field("m_unconscious", "System.Boolean", 0x4D, "false", None, at(0x4D)),
            _field("CanBeTargeted", "System.Boolean", 0x4E, "true", None, at(0x4E)),
            _field("Targetable", "System.Boolean", 0x4F, "true", None, at(0x4F)),
            _field("s_activeHealth", "System.Collections.Generic.List`1<Health>", 0x0,
                   "System.Collections.Generic.List`1<Health>[14]@0x2a3f9d10c40", None,
                   "0x2a3f8e2a010", "0x2a3f9d10c40", static=True, storage="data"),
            _field("DEATH_THRESHOLD", "System.Single", 0x0, "", static=True, storage="const"),
        ],
        "properties": [
            {"name": "CurrentHealth", "type": "System.Single", "setter": "0x2a43f80e010"},
            {"name": "CurrentStamina", "type": "System.Single", "setter": "0x2a43f80e2a0"},
            {"name": "MaxHealth", "type": "System.Single"},
            {"name": "MaxStamina", "type": "System.Single"},
            {"name": "HealthPercentage", "type": "System.Single"},
            {"name": "Dead", "type": "System.Boolean"},
            {"name": "Unconscious", "type": "System.Boolean"},
            {"name": "name", "type": "System.String", "setter": "0x2a43f612a30"},
            {"name": "gameObject", "type": "UnityEngine.GameObject"},
            {"name": "enabled", "type": "System.Boolean", "setter": "0x2a43f6130c0"},
        ],
    }


_HEALTH_METHODS = (
    ("DoDamage", "DamageInfo damage, UnityEngine.GameObject attacker", "System.Single"),
    ("ApplyHealthChange", "System.Single amount, UnityEngine.GameObject source",
     "System.Void"),
    ("ApplyStaminaChange", "System.Single amount, UnityEngine.GameObject source",
     "System.Void"),
    ("Kill", "", "System.Void"),
    ("Resurrect", "", "System.Void"),
    ("FullHeal", "", "System.Void"),
    ("IsAlly", "UnityEngine.GameObject other", "System.Boolean"),
    ("OnRestored", "", "System.Void"),
    ("get_name", "", "System.String"),
    ("GetComponent", "System.Type type", "UnityEngine.Component"),
)


@scene("object-inspector", size=(1080, 1120),
       description="Object inspector: the fields, getters and methods of a C# object")
def object_inspector(studio):
    """Eder's Health component, opened from a call of Health:DoDamage."""
    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)

    def nothing(*_args):
        return None

    dialog = studio.keep(ObjectInspectorDialog(
        studio.translations, nothing, target={"address": hex(_HEALTH)}, request=nothing,
        evaluate=nothing, name="this", parent=studio.window, show_text=nothing,
        list_methods=nothing, call_method=nothing))
    dialog.show_contents(_health_contents())
    dialog.show_properties([
        {"name": name, "value": value} for name, value in (
            ("CurrentHealth", {"text": "212.5"}), ("CurrentStamina", {"text": "41.25"}),
            ("MaxHealth", {"text": "236"}), ("MaxStamina", {"text": "88"}),
            ("HealthPercentage", {"text": "0.9004237"}), ("Dead", {"text": "false"}),
            ("name", {"text": f'"{_EDER}"', "string": True}))])
    dialog.methods_section.set_open(True, remember=False)
    dialog.show_methods([
        {"name": name, "method": hex(0x2A43F80D5C0 + 0x2A0 * index), "owner": "Health",
         "params": params, "returns": returns}
        for index, (name, params, returns) in enumerate(_HEALTH_METHODS)])
    dialog.splitter.setSizes([560, 250, 230])
    dialog.table.selectRow(5)
    dialog.table.setFocus()
    return dialog


# --- Call graph -------------------------------------------------------------------------------

def _framework(class_name, name, parameters, returns, assembly, static=False):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    return _method(class_name, name, parameters, returns, static, assembly)


@scene("call-graph", size=(1100, 720),
       description="Call graph: what a C# method calls and which methods call it")
def call_graph(studio):
    """What UIStoreStorePage:FinalizeTransaction calls, and its callers."""
    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)
    finalize = _BY_NAME["UIStoreStorePage:FinalizeTransaction"]
    with _idle_workers((call_graph_widget, "CallGraphWorker")):
        dialog = studio.keep(call_graph_widget.CallGraphDialog(
            studio.translations, _POE_PID, finalize, parent=studio.window, engine=MONO))
    callees = [
        Call("call", _BY_NAME["PlayerInventory:get_Instance"]),
        Call("callvirt", _BY_NAME["PlayerInventory:CanAfford"]),
        Call("callvirt", _BY_NAME["StoreInventory:GetBuyPrice"]),
        Call("callvirt", _BY_NAME["StoreInventory:RemoveItem"]),
        Call("callvirt", _BY_NAME["Inventory:AddItem"]),
        Call("callvirt", _BY_NAME["PlayerInventory:RemoveCurrency"]),
        Call("call", _BY_NAME["UIStoreStorePage:RefreshTotals"]),
        Call("call", _BY_NAME["GlobalAudioPlayer:Play"]),
        Call("call", _framework("UnityEngine.Object", "op_Implicit", "(UnityEngine.Object)",
                                "System.Boolean", "UnityEngine", static=True)),
        Call("callvirt", _framework("System.Collections.Generic.List`1<StoreItem>", "Clear",
                                    "()", "System.Void", "mscorlib")),
        Call("call", _framework("System.String", "Format", "(string,object)",
                                "System.String", "mscorlib", static=True)),
        Call("newobj", _framework("StoreTransactionEvent", ".ctor", "(Item,int)",
                                  "System.Void", "Assembly-CSharp")),
    ]
    dialog._on_callees(dialog._callee_request, finalize, callees, [])
    dialog.search_callers()
    dialog._on_callers(dialog._caller_request, finalize, [
        Call("call", _BY_NAME["UIStoreStorePage:OnConfirmClicked"]),
        Call("call", _method("UIStoreStorePage", "<Start>m__2A3", "(UnityEngine.GameObject)",
                                 static=True)),
        Call("call", _method("UIStoreStorePage", "OnKeyConfirm", "(KeyControl)",
                             "System.Boolean")),
    ], [])
    dialog.findChild(QSplitter).setSizes([480, 240])
    dialog.callees_table.selectRow(5)
    dialog.callees_table.setFocus()
    return dialog


# --- Method call (Unreal) -------------------------------------------------------------------

_GIVE_HEALTH = CatalogMethod("/Script/Alabama", "AlabamaCheatManager", "GiveHealth", "(float)",
                             "void", False, 0x2D41C, "AlabamaCheatManager:GiveHealth (float)",
                             "unreal", ("Amount",))


@scene("method-call", size=(760, 400),
       description="Calling a UFunction of an Unreal game on a live object, with an argument")
def method_call(studio):
    """AlabamaCheatManager:GiveHealth in Avowed, called once with 50."""
    studio.show_attached(_AVOWED_NAME, _AVOWED_PID, _AVOWED_DETAILS)
    with _idle_workers((method_call_widget, "MethodMonitorWorker")):
        dialog = studio.keep(method_call_widget.MethodCallDialog(
            studio.translations, _AVOWED_PID, parent=studio.window))
    dialog.set_method(_GIVE_HEALTH)
    dialog._on_described(dialog._describe_request, {
        "params": [{"name": "Amount", "type": {"kind": R4, "byref": False, "name": "float"}}],
        "returns": {"kind": 0x01, "byref": False, "name": "void"}})
    dialog._editors[0].setText("50")
    before = set(dialog._calls)
    dialog.call()
    (request,) = set(dialog._calls) - before
    dialog._on_called(request, {"result": None, "object": {
        "text": 'BP_AlabamaCheatManager_C "BP_AlabamaCheatManager_C_2147482190"@0x2c6f1e40a00'}})
    return dialog


# --- GAS attributes -------------------------------------------------------------------------

class _GasWorker(QObject):
    """Answers for the window from the scene: the scripts worker's GAS signals."""

    gas_listed = Signal(int, object)
    gas_failed = Signal(int, str)
    gas_set = Signal(int, object)
    gas_set_failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self._requests = itertools.count(1)
        self.last = None

    def gas_attributes(self):
        self.last = next(self._requests)
        return self.last

    def gas_set_base(self, _name, _value):
        return next(self._requests)


class _ScriptPanel:
    """Stands in for the scripts tab: its worker and one hold script."""

    def __init__(self, worker, held):
        self._worker = worker
        self._held = held

    def worker(self):
        return self._worker

    def scripts_labelled(self, prefix):
        return {number: spec for number, spec in enumerate(self._held, 1)
                if spec.label.startswith(prefix)}


def _attributes(*rows):
    return [{"name": name, "base": base, "current": current} for name, base, current in rows]


_GAS_STATE = {
    "owner": "BPC_AlabamaPlayer_C_2147479871",
    "setter": "AlabamaAbilitySystemBlueprintLibrary:SetFloatAttributeBaseFromAbilitySystemComponent",
    "component": True,
    "sets": [
        {"name": "AttributeSetCore", "className": "UAlabamaAttributeSetCore",
         "attributes": _attributes(
             ("Might", 4, 5), ("Constitution", 3, 3), ("Dexterity", 2, 2),
             ("Perception", 1, 2), ("Intellect", 0, 0), ("Resolve", 2, 2),
             ("Health", 312, 312), ("MaxHealth", 312, 346.5), ("Stamina", 140, 140),
             ("MaxStamina", 140, 140), ("Essence", 96, 96),
             ("DamageOutgoingMult", 3, 4.199999809), ("CritChance", 0.05, 0.1),
             ("MoveSpeedMult", 1, 1))},
        {"name": "AttributeSetElemental", "className": "UAlabamaAttributeSetElemental",
         "attributes": _attributes(("FireResist", 0, 0.15), ("FrostResist", 0, 0),
                                   ("ShockResist", 0, 0))},
        {"name": "AttributeSetAttack", "className": "UAlabamaAttributeSetAttack",
         "attributes": _attributes(("AttackSpeedMult", 1, 1.1), ("StaggerMult", 1, 1))},
    ],
}


@scene("gas-attributes", size=(900, 760),
       description="GAS attributes of an Unreal game: base and current values, held values")
def gas_attributes(studio):
    """The player's attribute sets in Avowed; the damage multiplier is held at 3."""
    # pylint: disable=import-outside-toplevel
    from hermes.ui.widgets.gas_attributes import GasAttributesDialog

    studio.show_attached(_AVOWED_NAME, _AVOWED_PID, _AVOWED_DETAILS)
    worker = _GasWorker()
    panel = _ScriptPanel(worker, [hold_spec("AttributeSetCore.DamageOutgoingMult", 3)])
    dialog = studio.keep(GasAttributesDialog(studio.translations, panel, parent=studio.window))
    worker.gas_listed.emit(worker.last, _GAS_STATE)
    row = next(row for row, (_set, attribute) in enumerate(dialog.rows())
               if attribute["name"] == "DamageOutgoingMult")
    dialog.table.selectRow(row)
    dialog.table.setFocus()
    return dialog


# --- Assemblies -----------------------------------------------------------------------------

@scene("assemblies", size=(980, 580),
       description="The assemblies the catalog and the trace read: the game's are ticked")
def assemblies(studio):
    """Pillars of Eternity's assemblies with its own two selected."""
    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)
    repository = AssemblySelectionRepository()
    process_id = _poe_process()
    repository.save_assemblies(process_id, ["Assembly-CSharp", "Assembly-CSharp-firstpass"])
    store = assemblies_widget.AssemblySelectionStore(repository, studio.window)
    store.set_process(process_id)
    with _idle_workers((assemblies_widget, "CatalogWorker")):
        dialog = studio.keep(assemblies_widget.AssemblyDialog(
            studio.translations, _POE_PID, store, parent=studio.window, engine=MONO))
    dialog._on_assemblies_found(_assemblies())
    dialog._on_worker_finished(dialog._worker)
    return dialog


# --- Favorites ------------------------------------------------------------------------------

@scene("favorites", size=(1440, 420),
       description="Favorite methods and functions of a game, with notes")
def favorites(studio):
    """The favorites saved for Pillars of Eternity."""
    # pylint: disable=import-outside-toplevel
    from hermes.ui.widgets.favorites import FavoritesDialog

    studio.show_attached(_POE_NAME, _POE_PID, _POE_DETAILS)
    dialog = studio.keep(FavoritesDialog(studio.translations, _favorites(studio),
                                         parent=studio.window))
    dialog.set_attached(True)
    dialog.table.selectRow(0)
    dialog.table.setFocus()
    settle()
    return dialog
