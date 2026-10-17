"""Tests for the pointer scan."""

import subprocess

import pytest

from hermes.core.pointer import (
    Mapping,
    PointerChain,
    PointerMap,
    ProcessMemory,
    chain_for_static_address,
    filter_chains,
    module_bases,
    read_mappings,
    resolve_chain,
    static_regions,
)
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
int32_t g_score = 7;

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
        }
        printf("%p\n", (void *)&g_game->world->player->gold);
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

    def new_level(self, allocations):
        self.process.stdin.write(f"level {allocations}\n")
        self.process.stdin.flush()
        return int(self.process.stdout.readline(), 16)

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
    mappings = [
        Mapping(0x1000, 0x2000, "r--p", "/opt/game/game"),
        Mapping(0x2000, 0x3000, "r-xp", "/opt/game/game"),
        Mapping(0x3000, 0x4000, "rw-p", "/opt/game/game"),
        Mapping(0x4000, 0x6000, "rw-p", ""),          # .bss
        Mapping(0x8000, 0x9000, "rw-p", ""),          # heap-like, not adjacent
        Mapping(0xA000, 0xB000, "rw-p", "[heap]"),
    ]

    assert static_regions(mappings) == [(0x3000, 0x4000, "game"), (0x4000, 0x6000, "game")]
    assert module_bases(mappings) == {"game": 0x1000}
    assert chain_for_static_address(mappings, 0x4010) == PointerChain("game", 0x3010)
    assert chain_for_static_address(mappings, 0x8010) is None


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
        window.close()
