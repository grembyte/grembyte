"""Tests for the pointer scan."""

import builtins
import io
import struct
import subprocess

import pytest

from hermes.core import pointer as pointer_module
from hermes.core.pointer import (
    PointerChain,
    PointerMap,
    chain_for_static_address,
    filter_chains,
    module_bases,
    resolve_chain,
    static_regions,
)
from hermes.platform import Mapping, ProcessMemory, main_module, read_mappings
from hermes.platform import linux
from hermes.platform.linux import parse_maps
from tests.test_hooks import requires_target


_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct { int32_t hp; int32_t gold; } Player;
typedef struct { char pad[0x40]; Player *player; } World;
typedef struct { char pad[0x18]; World *world; } Game;
Game *g_game;
Player *g_enemy;
int32_t g_score = 7;
volatile int g_events;

/* Shared code: changes the gold of the player and of the enemy. g_events keeps
   the function long enough for the 5-byte jump of a hook. */
__attribute__((noinline)) void add_gold(Player *p, int32_t amount) {
    p->gold += amount;
    g_events++;
}

int main(void) {
    /* A different number of allocations per run moves the objects around. */
    srand(getpid());
    int count = 1 + rand() % 64;
    for (int i = 0; i < count; i++)
        memset(malloc(16 + rand() % 4096), 0, 16);
    g_game = calloc(1, sizeof(Game));
    g_game->world = calloc(1, sizeof(World));
    g_game->world->player = calloc(1, sizeof(Player));
    g_game->world->player->gold = 1234;
    g_enemy = calloc(1, sizeof(Player));
    g_enemy->gold = 50;
    printf("%d %p %p\n", getpid(), (void *)&g_game->world->player->gold, (void *)&g_score);
    fflush(stdout);
    char cmd[16];
    int value;
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "level") == 0) {  /* a new player object, like loading a level */
            Player *old = g_game->world->player;
            for (int i = 0; i < value; i++)
                malloc(64);
            g_game->world->player = calloc(1, sizeof(Player));
            g_game->world->player->gold = old->gold;
            free(old);
        } else if (strcmp(cmd, "add") == 0) {
            add_gold(g_game->world->player, value);
        } else if (strcmp(cmd, "enemy") == 0) {
            add_gold(g_enemy, value);
        }
        printf("%p %d %d\n", (void *)&g_game->world->player->gold,
               g_game->world->player->gold, g_enemy->gold);
        fflush(stdout);
    }
    return 0;
}
"""


class _Target:
    def __init__(self, binary):
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        pid, gold, score = self.process.stdout.readline().split()
        self.pid = int(pid)
        self.gold = int(gold, 16)
        self.score = int(score, 16)

    def send(self, command, value):
        """Run a command; return (address of the player's gold, gold, enemy gold)."""
        self.process.stdin.write(f"{command} {value}\n")
        self.process.stdin.flush()
        address, gold, enemy = self.process.stdout.readline().split()
        return int(address, 16), int(gold), int(enemy)

    def new_level(self, allocations):
        return self.send("level", allocations)[0]

    def close(self):
        self.process.kill()
        self.process.wait()


@pytest.fixture(name="binary", scope="module")
def fixture_binary(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pointer")
    source = directory / "target.c"
    source.write_text(_TARGET_SOURCE)
    binary = directory / "target"
    subprocess.run(["gcc", "-O1", "-fPIE", "-pie", str(source), "-o", str(binary)],
                   check=True)
    return binary


def _gold_chain(binary):
    """The chain the program builds: g_game -> world (+0x18) -> player (+0x40) -> gold (+4)."""
    nm = subprocess.run(["nm", str(binary)], check=True, capture_output=True, text=True)
    # A PIE binary is linked at 0, so the symbol value is the offset in the module.
    g_game = next(int(line.split()[0], 16) for line in nm.stdout.splitlines()
                  if line.endswith(" g_game"))
    return PointerChain("target", g_game, (0x18, 0x40, 0x4))


def test_chain_text_and_json():
    chain = PointerChain("game", 0x2A1F0, (0x18, 0x40, 0x10))

    assert chain.text() == "game+0x2a1f0 → 0x18 → 0x40 → 0x10"
    assert PointerChain.from_json(chain.to_json()) == chain
    assert PointerChain("libc.so.6", 0x10).text() == "libc.so.6+0x10"


def test_static_regions_include_bss_behind_a_module():
    mappings = parse_maps([
        "1000-2000 r--p 00000000 08:01 1 /opt/game/game",
        "2000-3000 r-xp 00001000 08:01 1 /opt/game/game",
        "3000-4000 rw-p 00002000 08:01 1 /opt/game/game",
        "4000-6000 rw-p 00000000 00:00 0",            # .bss
        "8000-9000 rw-p 00000000 00:00 0",            # heap-like, not adjacent
        "a000-b000 rw-p 00000000 00:00 0 [heap]",
    ])

    assert static_regions(mappings) == [(0x3000, 0x4000, "game"), (0x4000, 0x6000, "game")]
    assert module_bases(mappings) == {"game": 0x1000}
    assert chain_for_static_address(mappings, 0x4010) == PointerChain("game", 0x3010)
    assert chain_for_static_address(mappings, 0x8010) is None


def test_memory_behind_mapped_files_that_are_no_modules_is_not_static():
    # Unity maps the GPU device right in front of anonymous (managed) heap.
    mappings = parse_maps([
        "1000-2000 r-xp 00000000 08:01 1 /opt/game/game",
        "2000-3000 rw-p 00001000 08:01 1 /opt/game/game",
        "10000-11000 rw-s 00000000 00:05 2 /dev/dri/renderD128",
        "11000-20000 rw-p 00000000 00:00 0",
        "20000-21000 r--p 00000000 08:01 3 /opt/game/Data/level0",
        "21000-22000 rw-p 00000000 00:00 0",
    ])

    assert static_regions(mappings) == [(0x2000, 0x3000, "game")]
    assert module_bases(mappings) == {"game": 0x1000}
    assert chain_for_static_address(mappings, 0x11010) is None
    assert chain_for_static_address(mappings, 0x21010) is None


@requires_target
def test_static_address_survives_a_restart(binary):
    first = _Target(binary)
    try:
        chain = chain_for_static_address(read_mappings(first.pid), first.score)
        assert chain is not None and chain.module == "target" and chain.offsets == ()
    finally:
        first.close()

    second = _Target(binary)
    try:
        bases = module_bases(read_mappings(second.pid))
        with ProcessMemory(second.pid) as memory:
            assert resolve_chain(memory, bases, chain) == second.score
    finally:
        second.close()


@requires_target
def test_pointer_scan_finds_chain_and_filters_after_restart(binary):
    first = _Target(binary)
    try:
        expected = _gold_chain(binary)
        pointer_map = PointerMap.build(first.pid)
        levels = []
        chains = pointer_map.find_chains(first.gold, max_depth=4, max_offset=0x100,
                                         progress=levels.append)

        assert expected in chains
        assert levels[0] == 1
        assert all(len(chain.offsets) <= 4 for chain in chains)
        assert [len(c.offsets) for c in chains] == sorted(len(c.offsets) for c in chains)
        bases = module_bases(read_mappings(first.pid))
        with ProcessMemory(first.pid) as memory:
            assert all(resolve_chain(memory, bases, chain) == first.gold for chain in chains)
            # The chain follows the game when it replaces the player object.
            moved = first.new_level(10)
            assert moved != first.gold
            assert resolve_chain(memory, bases, expected) == moved
    finally:
        first.close()

    second = _Target(binary)
    try:
        remaining = filter_chains(second.pid, chains, second.gold)
        assert expected in remaining
        assert len(remaining) <= len(chains)
    finally:
        second.close()


@requires_target
def test_scan_limits(binary):
    target = _Target(binary)
    try:
        pointer_map = PointerMap.build(target.pid)
        # The game chain needs three levels.
        shallow = pointer_map.find_chains(target.gold, max_depth=2, max_offset=0x100)
        assert _gold_chain(binary) not in shallow
        assert all(len(chain.offsets) <= 2 for chain in shallow)
        # Gold is 4 bytes into the player object, so no pointer reaches it with offset < 4.
        assert pointer_map.find_chains(target.gold, max_depth=4, max_offset=0x3) == []
        assert len(pointer_map.find_chains(target.gold, max_depth=4, max_offset=0x1000,
                                           max_results=1)) == 1
    finally:
        target.close()


@requires_target
def test_result_limit_drops_the_longest_chains(binary):
    target = _Target(binary)
    try:
        pointer_map = PointerMap.build(target.pid)
        everything = pointer_map.find_chains(target.gold, max_depth=4, max_offset=0x100)
        shorter = [chain for chain in everything if len(chain.offsets) <= 3]
        assert len(shorter) < len(everything), "the test needs chains of depth 4"

        limited = pointer_map.find_chains(target.gold, max_depth=4, max_offset=0x100,
                                          max_results=len(shorter))

        assert limited == shorter
        assert _gold_chain(binary) in limited
    finally:
        target.close()


@requires_target
def test_chains_can_be_limited_to_the_main_module(binary):
    target = _Target(binary)
    try:
        assert main_module(target.pid) == "target"
        pointer_map = PointerMap.build(target.pid)
        everything = pointer_map.find_chains(target.gold, max_depth=4, max_offset=0x100)
        own = pointer_map.find_chains(target.gold, max_depth=4, max_offset=0x100,
                                      base_modules={"target"})

        assert own == [chain for chain in everything if chain.module == "target"]
        assert _gold_chain(binary) in own
    finally:
        target.close()


def test_pointer_map_ignores_unaligned_values_by_default(monkeypatch):
    mappings = [Mapping(0x1000, 0x1020, "rw-p", writable=True)]
    memory = struct.pack("<4Q", 0x1008, 0x1004, 0x2000, 0)

    def fake_open(path, mode="r", **_kwargs):
        assert path == "/proc/123/mem" and mode == "rb"
        image = io.BytesIO(b"\0" * 0x1000 + memory)
        return image

    monkeypatch.setattr(pointer_module, "read_mappings", lambda _pid: mappings)
    monkeypatch.setattr(pointer_module, "pointer_size", lambda _pid: 8)
    monkeypatch.setattr(builtins, "open", fake_open)
    # The fake is /proc/<pid>/mem: use the Linux backend on every system.
    monkeypatch.setattr(pointer_module, "ProcessMemory", linux.ProcessMemory)

    assert PointerMap.build(123).values.tolist() == [0x1008]
    assert PointerMap.build(123, aligned_values=False).values.tolist() == [0x1004, 0x1008]


def test_pointer_map_of_a_32_bit_program_reads_4_byte_pointers(monkeypatch):
    mappings = [Mapping(0x1000, 0x1020, "rw-p", writable=True)]
    memory = struct.pack("<8I", 0x1008, 0x1004, 0x2000, 0, 0x1010, 0, 0, 0)

    def fake_open(path, mode="r", **_kwargs):
        assert path == "/proc/123/mem" and mode == "rb"
        return io.BytesIO(b"\0" * 0x1000 + memory)

    monkeypatch.setattr(pointer_module, "read_mappings", lambda _pid: mappings)
    monkeypatch.setattr(pointer_module, "pointer_size", lambda _pid: 4)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(pointer_module, "ProcessMemory", linux.ProcessMemory)

    pointer_map = PointerMap.build(123)
    assert pointer_map.values.tolist() == [0x1004, 0x1008, 0x1010]
    assert sorted(pointer_map.locations.tolist()) == [0x1000, 0x1004, 0x1010]


def test_pointer_size_follows_the_program_and_is_kept_per_start(monkeypatch):
    calls = []
    monkeypatch.setattr(pointer_module, "_pointer_sizes", {})
    monkeypatch.setattr(pointer_module, "process_start_time", lambda pid: 77)
    monkeypatch.setattr(pointer_module, "is_wine_process", lambda pid: False)
    monkeypatch.setattr(pointer_module, "program_bits", lambda pid: calls.append(pid) or 32)

    assert pointer_module.pointer_size(5) == 4
    assert pointer_module.pointer_size(5) == 4
    assert calls == [5]
    assert pointer_module.pointer_size(None) == 8

    class Memory:  # pylint: disable=too-few-public-methods
        pid = 5

        @staticmethod
        def read_pointer(address, size):
            return {(0x100, 4): 0x2000}.get((address, size))

    chain = PointerChain("game", 0x100, (0x8,))
    assert resolve_chain(Memory(), {"game": 0}, chain) == 0x2008


@requires_target
def test_search_stops_at_its_memory_limit(binary):
    target = _Target(binary)
    try:
        pointer_map = PointerMap.build(target.pid)
        everything = pointer_map.find_chains(target.gold, max_depth=5, max_offset=0x1000)
        assert not pointer_map.truncated

        limited = pointer_map.find_chains(target.gold, max_depth=5, max_offset=0x1000,
                                          max_edges=50)

        assert pointer_map.truncated
        complete = pointer_map.complete_depth
        assert set(limited) < set(everything)
        assert {chain for chain in everything if len(chain.offsets) <= complete} <= set(limited)
        # The search goes level by level, so the limit costs the longest chains.
        assert max(len(chain.offsets) for chain in limited) < 5
    finally:
        target.close()


# --- UI: pointer scan dialog and saved address list ---------------------------

@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    # pylint: disable=import-outside-toplevel
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


@pytest.fixture(name="sessions")
def fixture_sessions(tmp_path):
    # pylint: disable=import-outside-toplevel
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _wait(app, predicate, timeout=10):
    # pylint: disable=import-outside-toplevel
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.01)


@requires_target
def test_pointer_scan_dialog_scans_filters_and_picks(binary, qt_app, sessions):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.pointer_scan import PointerScanDialog

    translations = load_translations("de")
    repository = AddressRepository(sessions)
    process_id = HookRepository(sessions).get_or_create_process("target", str(binary))
    expected = _gold_chain(binary)

    first = _Target(binary)
    try:
        entry_id = repository.add_address(process_id, label="Gold", value_type="int32",
                                          address=first.gold)
        entry = {"id": entry_id, "label": "Gold", "address": first.gold, "type": "int32"}
        dialog = PointerScanDialog(translations, repository, first.pid, entry)
        assert not dialog.btn_filter.isEnabled()
        dialog.depth_input.setValue(4)
        dialog.offset_input.setText("0x100")
        dialog.start_scan()
        _wait(qt_app, lambda: dialog._worker is None)
        assert expected in dialog.candidates()
        assert "gespeichert" in dialog.status_label.text()
        scan = repository.get_pointer_scan(entry_id)
        assert expected in scan.chains and scan.rounds == 0 and scan.max_offset == 0x100
    finally:
        first.close()

    second = _Target(binary)
    try:
        entry["address"] = 0
        dialog = PointerScanDialog(translations, repository, second.pid, entry)
        assert "gespeicherte Kandidaten" in dialog.status_label.text()
        assert dialog.btn_filter.isEnabled()
        dialog.address_input.setText(hex(second.gold))
        dialog.start_filter()
        _wait(qt_app, lambda: dialog._worker is None)
        assert expected in dialog.candidates()
        assert repository.get_pointer_scan(entry_id).rounds == 1

        from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
        row = next(r for r in range(dialog.table.rowCount())
                   if dialog.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == expected)
        dialog.table.selectRow(row)
        assert dialog.btn_use.isEnabled()
        dialog._accept()
        assert dialog.chain == expected
    finally:
        second.close()


def _delete_window(window):
    """Close and delete a MainWindow now.

    Left to the garbage collector, its children would run their Python
    ``destroyed`` handlers in the middle of a collection, which can crash the
    interpreter in a later test.
    """
    from PyQt6 import sip  # pylint: disable=import-error,no-name-in-module,import-outside-toplevel

    window.close()
    sip.delete(window)


def test_main_window_table_headers_are_centered(qt_app, sessions, monkeypatch):
    # pylint: disable=import-outside-toplevel
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui import main_window

    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))
    window = main_window.MainWindow("de")
    expected = (main_window.Qt.AlignmentFlag.AlignHCenter
                | main_window.Qt.AlignmentFlag.AlignVCenter)
    try:
        qt_app.processEvents()
        assert window.addr_table.horizontalHeader().defaultAlignment() == expected
        assert window.result_table.horizontalHeader().defaultAlignment() == expected
    finally:
        _delete_window(window)


@requires_target
def test_address_list_survives_restart(binary, qt_app, sessions, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui import main_window

    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))
    window = main_window.MainWindow("de")
    window._refresh_timer.stop()
    process_id = HookRepository(sessions).get_or_create_process("target", str(binary))
    expected = _gold_chain(binary)

    def attach(target):
        window._current_pid = target.pid
        window._process_id = process_id
        window._load_saved_addresses()
        window._refresh_timer.stop()

    def refresh():
        window._refresh_memory_values()
        _wait(qt_app, lambda: window._refresh_worker is None)

    first = _Target(binary)
    try:
        attach(first)
        assert window.addr_table.rowCount() == 0
        for address in (first.score, first.gold):
            window._add_tracked_address({
                "address": f"0x{address:X}", "address_value": address,
                "type": "int32", "value": "?",
            })
        score_meta, gold_meta = window._address_meta(0), window._address_meta(1)
        # The global variable is found as module + offset right away.
        assert score_meta["pointer"] is not None and score_meta["pointer"].offsets == ()
        assert gold_meta["pointer"] is None
        assert window.addr_table.item(1, 5).text() == "—"
        window.addr_table.item(1, 0).setText("Gold")
        window._set_pointer(1, expected)
        assert window.addr_table.item(1, 5).text() == expected.text()
        refresh()
        assert window.addr_table.item(0, 3).text() == "7"
        assert window.addr_table.item(1, 3).text() == "1234"
    finally:
        first.close()

    second = _Target(binary)
    try:
        attach(second)
        assert window.addr_table.rowCount() == 2
        assert window.addr_table.item(1, 0).text() == "Gold"
        assert window.addr_table.item(0, 1).text() == f"0x{second.score:X}"
        assert window.addr_table.item(1, 1).text() == f"0x{second.gold:X}"
        refresh()
        assert window.addr_table.item(1, 3).text() == "1234"

        # A new level replaces the player object; the next refresh follows it.
        moved = second.new_level(10)
        refresh()
        assert window.addr_table.item(1, 1).text() == f"0x{moved:X}"
        saved = AddressRepository(sessions).list_addresses(process_id)
        assert saved[1].address == moved

        window._set_pointer(1, None)
        window.addr_table.selectRow(0)
        window._remove_address()
        saved = AddressRepository(sessions).list_addresses(process_id)
        assert [entry.label for entry in saved] == ["Gold"] and saved[0].pointer is None
    finally:
        second.close()
        _delete_window(window)



def _add_gold_write_offset(binary):
    """Module offset of the instruction in add_gold that writes the gold."""
    dump = subprocess.run(["objdump", "-d", "--no-show-raw-insn", str(binary)],
                          check=True, capture_output=True, text=True).stdout
    body = dump.split("<add_gold>:", 1)[1].split("\n\n", 1)[0]
    line = next(line for line in body.splitlines() if "add" in line and "(%rdi)" in line)
    return int(line.split(":")[0], 16)


def test_hook_dialog_with_pointer_target(qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.hooks import HookDialog

    chain = PointerChain("game", 0x3070, (0x18, 0x40, 0x4))
    dialog = HookDialog(load_translations("de"), "0x1000", "int32", "game+0x10", pointer=chain)

    assert not hasattr(dialog, "caller_depth_input")
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.caller_depth == 0


@requires_target
def test_pointer_hook_survives_restart_and_ignores_enemy(binary, qt_app, sessions):
    # pylint: disable=import-outside-toplevel,protected-access,too-many-locals
    from hermes.db.repository.hooks import HookRepository
    from hermes.hooks.engine import HookSpec
    from hermes.hooks.transforms import MultiplyTransform
    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.hooks import HookPanel

    translations = load_translations("de")
    repository = HookRepository(sessions)
    process_id = repository.get_or_create_process("target", str(binary))
    chain = _gold_chain(binary)
    status = lambda panel: panel.table.item(0, 4).text()  # pylint: disable=unnecessary-lambda-assignment

    first = _Target(binary)
    panel = HookPanel(translations, repository=repository)
    try:
        panel.set_process(first.pid, process_id)
        panel.add_hook(HookSpec(
            None, "int32", MultiplyTransform(3),
            module="target", module_offset=_add_gold_write_offset(binary),
            pointer=chain, caller_depth=0, label="Gold",
        ), "target+?")
        _wait(qt_app, lambda: status(panel) != "Wird installiert …")
        assert status(panel) == "Aktiv"
        assert panel.table.item(0, 1).text() == "↳ " + chain.text()

        assert first.send("add", 10)[1:] == (1234 + 30, 50)
        # Same code, other object: the pointer target tells them apart.
        assert first.send("enemy", 5)[1:] == (1264, 55)
        # The game allocates a new player object; the hook follows the chain.
        first.new_level(10)
        assert first.send("add", 10)[1:] == (1294, 55)
        [saved] = repository.list_hooks(process_id)
        assert PointerChain.from_json(saved.pointer) == chain
    finally:
        panel.shutdown(timeout_ms=None)
        first.close()

    second = _Target(binary)
    panel = HookPanel(translations, repository=repository)
    try:
        panel.set_process(second.pid, process_id)
        _wait(qt_app, lambda: status(panel) != "Wird installiert …")
        assert status(panel) == "Aktiv"
        assert second.send("add", 10)[1:] == (1264, 50)
        assert second.send("enemy", 5)[1:] == (1264, 55)
    finally:
        panel.shutdown(timeout_ms=None)
        second.close()


@requires_target
def test_pointer_dialog_recognizes_static_address(binary, qt_app, sessions):
    # pylint: disable=import-outside-toplevel,protected-access,unused-argument
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.pointer_scan import PointerScanDialog

    translations = load_translations("de")
    repository = AddressRepository(sessions)
    process_id = HookRepository(sessions).get_or_create_process("target", str(binary))
    target = _Target(binary)
    try:
        def dialog_for(address):
            entry_id = repository.add_address(process_id, label="x", value_type="int32",
                                              address=address)
            entry = {"id": entry_id, "label": "x", "address": address, "type": "int32"}
            return PointerScanDialog(translations, repository, target.pid, entry)

        static = dialog_for(target.score)
        assert static.static_label.isVisibleTo(static)
        assert "target+0x" in static.static_label.text()
        static._use_static()
        assert static.chain == PointerChain("target", static._static_chain.base_offset)
        assert static.chain.offsets == ()

        heap = dialog_for(target.gold)
        assert not heap.static_label.isVisibleTo(heap)
        assert not heap.btn_static.isVisibleTo(heap)
    finally:
        target.close()


@requires_target
def test_pointer_dialog_stays_open_while_scanning(binary, qt_app, sessions, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui import main_window

    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))
    window = main_window.MainWindow("de")
    target = _Target(binary)
    try:
        window._current_pid = target.pid
        window._process_id = HookRepository(sessions).get_or_create_process("target", "t")
        window._refresh_timer.stop()
        window._add_tracked_address({"address": hex(target.gold), "address_value": target.gold,
                                     "type": "int32", "value": "1234"})
        window._open_pointer_scan(0)
        entry_id = window._address_meta(0)["id"]
        dialog = window._pointer_dialogs[entry_id]
        assert not dialog.isModal() and dialog.isVisible()

        # The scanner can be used meanwhile; its selection fills the address.
        dialog.take_scan_address()
        assert "zuerst ein Ergebnis" in dialog.status_label.text()
        moved = target.new_level(10)
        window._on_scan_results([{"address": moved, "type": "int32", "value": 1234}])
        window.result_table.selectRow(0)
        dialog.take_scan_address()
        assert dialog.address_input.text() == f"0x{moved:X}"

        window._open_pointer_scan(0)
        assert window._pointer_dialogs[entry_id] is dialog

        dialog._choose(_gold_chain(binary))
        qt_app.processEvents()
        assert window._address_meta(0)["pointer"] == _gold_chain(binary)
        assert window.addr_table.item(0, 1).text() == f"0x{moved:X}"
        assert entry_id not in window._pointer_dialogs
    finally:
        target.close()
        _delete_window(window)


@requires_target
def test_pointer_dialog_survives_a_restart_of_the_program(binary, qt_app, sessions,
                                                         monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access,too-many-locals
    from PyQt6.QtWidgets import QDialog  # pylint: disable=import-error,no-name-in-module

    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui import main_window

    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))

    def select(pid, name):
        """Answer the next "Select process" with this process."""
        class FakeSelection:  # pylint: disable=too-few-public-methods
            selected_pid, selected_name, selected_path = pid, name, f"/opt/{name}"

            def __init__(self, *_args):
                pass

            @staticmethod
            def exec():
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(main_window, "ProcessSelectDialog", FakeSelection)
        window._open_process_dialog()

    window = main_window.MainWindow("de")
    first = _Target(binary)
    second = None
    try:
        select(first.pid, "target")
        window._refresh_timer.stop()
        window._add_tracked_address({"address": hex(first.gold), "address_value": first.gold,
                                     "type": "int32", "value": "1234"})
        window._open_pointer_scan(0)
        entry_id = window._address_meta(0)["id"]
        dialog = window._pointer_dialogs[entry_id]
        dialog.depth_input.setValue(4)
        dialog.offset_input.setText("0x100")
        dialog.start_scan()
        dialog.wait_for_worker()
        _wait(qt_app, lambda: dialog._worker is None)
        assert _gold_chain(binary) in dialog.candidates()

        # The game is closed while a scan is running: the dialog stays open.
        from hermes.ui.widgets import pointer_scan
        monkeypatch.setattr(pointer_scan.QMessageBox, "question",
                            lambda *_args: pointer_scan.QMessageBox.StandardButton.Yes)
        monkeypatch.setattr(main_window.QMessageBox, "question",
                            lambda *_args: main_window.QMessageBox.StandardButton.Yes)
        dialog.start_scan()
        first.close()
        assert not window._check_process_alive()
        assert dialog.isVisible() and window._pointer_dialogs[entry_id] is dialog
        assert "beendet" in dialog.status_label.text()
        dialog.wait_for_worker()
        _wait(qt_app, lambda: dialog._worker is None)
        assert not dialog.btn_scan.isEnabled() and not dialog.btn_filter.isEnabled()

        # Restarted and attached again: filter with the new address.
        second = _Target(binary)
        select(second.pid, "target")
        assert dialog.pid == second.pid and dialog.btn_filter.isEnabled()
        # All candidates go to the address list, each with its pointer.
        candidates = dialog.candidates()
        dialog.adopt_candidates()
        assert window.addr_table.rowCount() == 1 + len(candidates)
        adopted = {window._address_meta(row)["pointer"]: window.addr_table.item(row, 1).text()
                   for row in range(1, window.addr_table.rowCount())}
        assert adopted[_gold_chain(binary)] == f"0x{second.gold:X}"
        assert str(len(candidates)) in dialog.status_label.text()
        dialog.adopt_candidates()  # nothing twice
        assert window.addr_table.rowCount() == 1 + len(candidates)
        # Several selected entries are removed at once.
        window.addr_table.clearSelection()
        for row in range(1, window.addr_table.rowCount()):
            window.addr_table.selectionModel().select(
                window.addr_table.model().index(row, 0),
                window.addr_table.selectionModel().SelectionFlag.Select
                | window.addr_table.selectionModel().SelectionFlag.Rows,
            )
        window._remove_address()
        assert window.addr_table.rowCount() == 1
        assert len(AddressRepository(sessions).list_addresses(window._process_id)) == 1

        dialog.address_input.setText(f"0x{second.gold:X}")
        dialog.start_filter()
        dialog.wait_for_worker()
        _wait(qt_app, lambda: dialog._worker is None)
        assert _gold_chain(binary) in dialog.candidates()

        # The chain is taken over by the reloaded address-list entry.
        dialog._choose(_gold_chain(binary))
        qt_app.processEvents()
        assert window._address_meta(0)["pointer"] == _gold_chain(binary)
        assert window.addr_table.item(0, 1).text() == f"0x{second.gold:X}"
        window._open_pointer_scan(0)
        dialog = window._pointer_dialogs[entry_id]

        # Removing the entry removes its candidates and closes its dialog, so
        # they are not saved again for an entry that is gone.
        asked = []
        monkeypatch.setattr(main_window.QMessageBox, "question",
                            lambda *args: asked.append(args[2])
                            or main_window.QMessageBox.StandardButton.Yes)
        window.addr_table.selectRow(0)
        window._remove_address()
        assert str(len(dialog.candidates())) in asked[0]
        assert not dialog.isVisible() and not window._pointer_dialogs
        assert AddressRepository(sessions).get_pointer_scan(entry_id) is None
        window._add_tracked_address({"address": hex(second.gold), "address_value": second.gold,
                                     "type": "int32", "value": "1234"})
        window._open_pointer_scan(0)
        dialog = next(iter(window._pointer_dialogs.values()))

        # Another program: its address list has no such entry.
        select(second.pid + 1_000_000, "other")
        assert not dialog.isVisible() and not window._pointer_dialogs
    finally:
        first.close()
        if second is not None:
            second.close()
        _delete_window(window)


@requires_target
def test_scan_of_the_previous_program_is_cleared_on_a_switch(binary, qt_app, sessions,
                                                             monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access
    from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module
    from PyQt6.QtWidgets import QDialog  # pylint: disable=import-error,no-name-in-module

    from hermes.db.repository.addresses import AddressRepository
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui import main_window

    monkeypatch.setattr(main_window, "HookRepository", lambda: HookRepository(sessions))
    monkeypatch.setattr(main_window, "AddressRepository", lambda: AddressRepository(sessions))

    class InstantScan(QThread):
        """Finds one address at once; its results are still queued on the switch."""

        results_ready = pyqtSignal(object)
        scan_failed = pyqtSignal(str)
        scan_cancelled = pyqtSignal()
        progress_changed = pyqtSignal(int)

        def __init__(self, *_args, parent=None, **_kwargs):
            super().__init__(parent)

        def run(self):
            self.results_ready.emit([{"address": 0x1000, "type": "int32", "value": 7}])

    monkeypatch.setattr(main_window, "ScanWorker", InstantScan)

    def select(pid, name):
        class FakeSelection:  # pylint: disable=too-few-public-methods
            selected_pid, selected_name, selected_path = pid, name, f"/opt/{name}"

            def __init__(self, *_args):
                pass

            @staticmethod
            def exec():
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(main_window, "ProcessSelectDialog", FakeSelection)
        window._open_process_dialog()

    window = main_window.MainWindow("de")
    first, second = _Target(binary), _Target(binary)
    try:
        select(first.pid, "first")
        window._refresh_timer.stop()
        window._start_scan_worker(main_window.ScanRequest(first.pid, "int32", "exact", "7"))
        _wait(qt_app, lambda: window._scan_worker is None)
        assert window.result_table.rowCount() == 1 and window._scan_results

        window._start_scan_worker(main_window.ScanRequest(first.pid, "int32", "exact", "7"))
        window._scan_worker.wait()  # finished, its results not yet delivered
        select(second.pid, "second")
        _wait(qt_app, lambda: window._scan_worker is None)
        assert window.result_table.rowCount() == 0 and not window._scan_results
        assert window.btn_first_scan.isEnabled() and not window.btn_next_scan.isEnabled()
    finally:
        first.close()
        second.close()
        _delete_window(window)


def test_worker_finishing_after_its_dialog_was_deleted_is_harmless(qt_app, monkeypatch):
    # pylint: disable=import-outside-toplevel,protected-access
    import threading

    from hermes.ui import workers
    from hermes.ui.widgets import pointer_scan

    release = threading.Event()

    class SlowWorker(pointer_scan.PointerScanWorker):
        """Ignores cancellation until released, like a long search step."""

        def _scan(self):
            release.wait(10)
            self.progress.emit("searching", 1)
            return [PointerChain("target", 0x10, (0x8,))]

    class Repository:  # pylint: disable=too-few-public-methods
        @staticmethod
        def get_pointer_scan(_entry_id):
            return None

    monkeypatch.setattr(pointer_scan, "PointerScanWorker", SlowWorker)
    monkeypatch.setattr(pointer_scan, "STOP_TIMEOUT_MS", 0)
    translations = __import__("hermes.ui.localization", fromlist=["x"]).load_translations("de")
    dialog = pointer_scan.PointerScanDialog(
        translations, Repository(), 1, {"id": 1, "label": "Gold", "address": 0x1000}
    )
    dialog.setAttribute(pointer_scan.Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.show()
    dialog.start_scan()
    worker = dialog._worker
    dialog.reject()
    from PyQt6.QtCore import QCoreApplication, QEvent  # pylint: disable=import-error,no-name-in-module
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert pointer_scan.sip.isdeleted(dialog)

    # The worker finishes now; its callbacks must not touch the deleted dialog.
    release.set()
    worker.wait(5000)
    _wait(qt_app, lambda: worker not in workers.running_workers())
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
