"""Categories of writable memory for the pointer scan (grembyte/core/memory_regions.py)."""

import os
import subprocess
import sys

from grembyte.core import memory_regions
from grembyte.core.memory_regions import (
    ENGINE,
    GAME,
    HEAP,
    MAIN,
    STACK,
    SYSTEM,
    UNKNOWN,
    Region,
    classify,
    module_categories,
    selection,
    stack_ranges,
    summarize,
)
from grembyte.platform import Mapping, read_mappings
from grembyte.platform.linux import thread_stack_pointers
from tests.test_hooks import linux_only


def _module(start, end, path, writable=True, name=None):
    return Mapping(start, end, "rw-p" if writable else "r-xp", path,
                   name or os.path.basename(path.replace("\\", "/")), writable)


def _anonymous(start, end, permissions="rw-p", path=""):
    return Mapping(start, end, permissions, path, None, permissions.startswith("rw"))


_PROTON = "/home/me/.steam/steam/steamapps/common"
_PROTON_WINE = "/home/me/.steam/steam/steamapps/common/Proton 9.0/files/lib/wine/x86_64-windows"


def test_modules_of_a_proton_game_are_sorted_by_who_wrote_them():
    game = f"{_PROTON}/Baldurs Gate Enhanced Edition"
    mappings = [
        _module(0x140000000, 0x140001000, f"{game}/Baldur.exe", writable=False),
        _module(0x140001000, 0x140002000, f"{game}/Baldur.exe"),
        _module(0x180000000, 0x180001000, f"{game}/plugins/lua51.dll"),
        _module(0x181000000, 0x181001000, f"{game}/steam_api64.dll"),
        _module(0x182000000, 0x182001000, f"{game}/fmod.dll"),
        _module(0x6ffff0000000, 0x6ffff0001000, f"{_PROTON_WINE}/ntdll.dll"),
        _module(0x6ffff1000000, 0x6ffff1001000,
                "/home/me/.steam/steam/steamapps/compatdata/228280/pfx/drive_c/windows/"
                "system32/vcruntime140.dll"),
        _module(0x7f0000000000, 0x7f0000001000, "/usr/lib64/libc.so.6"),
        _module(0x7f1000000000, 0x7f1000001000,
                "/home/me/.local/share/Steam/ubuntu12_64/gameoverlayrenderer.so"),
    ]

    assert module_categories(mappings, "Baldur.exe") == {
        "Baldur.exe": MAIN,
        "lua51.dll": GAME,
        "steam_api64.dll": ENGINE,
        "fmod.dll": ENGINE,
        "ntdll.dll": SYSTEM,
        "vcruntime140.dll": SYSTEM,
        "libc.so.6": SYSTEM,
        "gameoverlayrenderer.so": UNKNOWN,
    }


def test_engine_runtimes_count_as_engine_wherever_they_lie():
    game = "/games/PillarsOfEternity"
    mappings = [
        _module(0x400000, 0x401000, f"{game}/PillarsOfEternity"),
        _module(0x500000, 0x501000, f"{game}/PillarsOfEternity_Data/Mono/x86_64/libmono.so"),
        _module(0x600000, 0x601000, f"{game}/UnityPlayer.so"),
        _module(0x700000, 0x701000, f"{game}/GameAssembly.so"),
        _module(0x800000, 0x801000, f"{game}/Engine/Binaries/UE4-CoreUObject-Win64-Shipping.dll"),
    ]

    categories = module_categories(mappings, "PillarsOfEternity")

    assert categories["PillarsOfEternity"] == MAIN
    assert {categories[name] for name in categories if name != "PillarsOfEternity"} == {ENGINE}


def test_windows_paths_and_names_ignore_case():
    mappings = [
        _module(0x140000000, 0x140001000, "C:\\Games\\Avowed\\Avowed.exe"),
        _module(0x180000000, 0x180001000, "C:\\Games\\Avowed\\bin\\Oodle.dll"),
        _module(0x7ff800000000, 0x7ff800001000, "C:\\WINDOWS\\System32\\KERNEL32.DLL"),
        _module(0x7ff900000000, 0x7ff900001000, "D:\\Tools\\overlay.dll"),
    ]

    assert module_categories(mappings, "avowed.exe") == {
        "Avowed.exe": MAIN, "Oodle.dll": GAME, "KERNEL32.DLL": SYSTEM, "overlay.dll": UNKNOWN,
    }


def test_without_a_main_module_nothing_is_a_game_module():
    mappings = [_module(0x400000, 0x401000, "/games/x/game"),
                _module(0x500000, 0x501000, "/games/x/libgame.so")]

    assert set(module_categories(mappings, None).values()) == {UNKNOWN}


def test_stacks_are_the_memory_around_each_stack_pointer():
    # Linux merges neighbouring anonymous mappings, stacks and heap alike
    merged = _anonymous(0x7f0000000000, 0x7f0004000000)
    mappings = [merged, _anonymous(0x7ffe00000000, 0x7ffe00100000, path="[stack]")]
    pointer = 0x7f0001000000

    assert stack_ranges(mappings, [pointer, pointer + 0x100, 0x1234]) == [
        (pointer - memory_regions.STACK_BELOW, pointer + 0x100 + memory_regions.STACK_ABOVE),
        (0x7ffe00000000, 0x7ffe00100000),
    ]
    # ... and never reach beyond their mapping
    assert stack_ranges([merged], [merged.end - 8]) == [
        (merged.end - 8 - memory_regions.STACK_BELOW, merged.end)]


def test_windows_stacks_lie_right_above_their_guard_page():
    mappings = [
        _anonymous(0x1000000, 0x1001000, "---p"),   # guard page
        _anonymous(0x1001000, 0x1010000),           # committed stack
        _anonymous(0x2000000, 0x2100000),           # heap
        _anonymous(0x2100000, 0x2200000, "---p"),   # its reserved rest
    ]

    assert stack_ranges(mappings, None) == [(0x1001000, 0x1010000)]


def test_classify_splits_the_heap_around_stacks():
    mappings = [
        _module(0x400000, 0x401000, "/games/x/game", writable=False),
        _module(0x401000, 0x402000, "/games/x/game"),
        _anonymous(0x1000000, 0x1400000),
        _module(0x7f0000000000, 0x7f0000001000, "/usr/lib64/libc.so.6"),
    ]
    pointer = 0x1200000

    regions = classify(mappings, "game", [pointer])

    low = pointer - memory_regions.STACK_BELOW
    high = pointer + memory_regions.STACK_ABOVE
    assert regions == [
        Region(0x401000, 0x402000, MAIN, "game"),
        Region(0x1000000, low, HEAP),
        Region(low, high, STACK),
        Region(high, 0x1400000, HEAP),
        Region(0x7f0000000000, 0x7f0000001000, SYSTEM, "libc.so.6"),
    ]
    summary = summarize(regions)
    assert summary[STACK].size == high - low
    assert summary[HEAP].size == 0x400000 - (high - low)
    assert summary[MAIN].modules == ["game"] and summary[SYSTEM].modules == ["libc.so.6"]

    ranges, modules = selection(regions, {MAIN, HEAP})
    assert ranges == [(0x401000, 0x402000), (0x1000000, low), (high, 0x1400000)]
    assert modules == {"game"}
    # the heap is always read
    assert selection(regions, {MAIN})[0] == ranges
    assert selection(regions, {MAIN, STACK, SYSTEM})[0] == [
        (0x401000, 0x402000), (0x1000000, 0x1400000), (0x7f0000000000, 0x7f0000001000)]


_WAITING_THREADS = """
import threading, sys
stop = threading.Event()
for _ in range(3):
    threading.Thread(target=stop.wait, daemon=True).start()
print("ready", flush=True)
sys.stdin.readline()
"""


@linux_only
def test_thread_stack_pointers_lie_on_the_threads_stacks():
    # A process of its own: the test process has threads that come and go
    child = subprocess.Popen([sys.executable, "-c", _WAITING_THREADS],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        pointers = thread_stack_pointers(child.pid)
        stacks = stack_ranges(read_mappings(child.pid), pointers)
    finally:
        child.communicate("\n")

    assert len(pointers) == 4  # the main thread and the three waiting ones
    assert all(any(start <= pointer < end for start, end in stacks) for pointer in pointers)


@linux_only
def test_thread_stack_pointers_of_a_fake_process(tmp_path):
    tasks = tmp_path / "7" / "task"
    for tid, text in (("7", "running\n"), ("8", "202 0x1 0x2 0x0 0x0 0x0 0x0 0x7ffd1000 0x401000\n"),
                      ("9", "-1 0x7ffd2000 0x401000\n")):
        (tasks / tid).mkdir(parents=True)
        (tasks / tid / "syscall").write_text(text)

    assert sorted(thread_stack_pointers(7, tmp_path)) == [0x7ffd1000, 0x7ffd2000]
    assert not thread_stack_pointers(8, tmp_path)
