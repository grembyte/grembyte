"""Caller learning of hooks: stale return addresses on the stack are not callers."""

import subprocess

from grembyte.hooks.engine import HookSpec
from grembyte.hooks.transforms import MultiplyTransform

from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _trap_pcs,
    _wait_for,
    fixture_qt_app,
    requires_target,
)


# Like a game without frame pointers: noise() descends deep into libc and
# warmup() calls add_gold() one level deeper; then add_gold() runs at the
# same stack depth with a large uninitialised local, which still holds
# libc's return addresses and warmup's return from add_gold() when it writes
# the gold. Only earn() called it.
_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

typedef struct { int32_t gold; int32_t hp; } Character;
Character g_player = { 100, 50 };
Character g_enemy = { 999, 80 };
Character g_dummy = { 0, 0 };
volatile int g_events;
volatile char g_sink;

__attribute__((noinline)) void noise(int value) {
    char text[96];
    snprintf(text, sizeof text, "%d %f %s %x", value, value * 1.5, "noise", value);
    g_sink = text[0];
}

__attribute__((noinline)) void add_gold(Character *c, int32_t amount) {
    volatile char scratch[3000];  /* more than the slots scanned (2 KB) */
    scratch[0] = 1;
    c->gold += amount;
    g_sink = scratch[0];
}

__attribute__((noinline)) void warmup(void) {
    volatile char pad[256];
    pad[0] = 0;
    add_gold(&g_dummy, pad[0]);
    g_events++;  /* a call, not a jump */
}

__attribute__((noinline)) void remove_gold(int32_t amount) { g_player.gold -= amount; }
__attribute__((noinline)) void earn(int32_t amount) {
    noise(amount);
    warmup();
    add_gold(&g_player, amount);
    g_events++;
}

int main(void) {
    char cmd[16];
    int value;
    printf("%d %p\n", getpid(), (void *)&g_player.gold);
    fflush(stdout);
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "add") == 0) earn(value);
        else if (strcmp(cmd, "sub") == 0) remove_gold(value);
        printf("%d %d\n", g_player.gold, g_enemy.gold);
        fflush(stdout);
    }
    return 0;
}
"""


def _build(tmp_path):
    (tmp_path / "target.c").write_text(_TARGET_SOURCE)
    binary = tmp_path / "target"
    subprocess.run(["gcc", "-O2", "-fomit-frame-pointer", "-fPIE", "-pie",
                    str(tmp_path / "target.c"), "-o", str(binary)], check=True)
    return binary


def _symbol_offset(binary, name):
    listing = subprocess.run(["nm", str(binary)], check=True, capture_output=True,
                             text=True).stdout
    return next(int(line.split()[0], 16) for line in listing.splitlines()
                if line.split()[-1] == name)


@requires_target
def test_stale_return_addresses_are_not_learned_and_old_ones_dropped(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel,too-many-locals
    from grembyte.hooks.engine import HookWorker

    binary = _build(tmp_path)
    earn = _symbol_offset(binary, "earn")
    target = _Target(binary)
    worker = HookWorker(target.pid)
    installed, learned, dropped, failed, ambiguous = {}, {}, {}, {}, []
    worker.hook_installed.connect(installed.__setitem__)
    worker.callers_learned.connect(learned.__setitem__)
    worker.callers_dropped.connect(dropped.__setitem__)
    worker.hook_failed.connect(failed.__setitem__)
    worker.callers_ambiguous.connect(ambiguous.append)
    try:
        trap_pcs = _trap_pcs(target)
        worker.start()
        first = worker.add_hook(HookSpec(target.address, "int32", MultiplyTransform(2),
                                         trap_pc=trap_pcs["add"], caller_depth=1))
        _wait_for(qt_app, lambda: first in installed or failed)
        assert not failed
        assert target.send("add", 5) == (110, 999)  # 100 + 5 * 2
        _wait_for(qt_app, lambda: first in learned)
        # The caller is earn(), right after its call of add_gold(): not libc,
        # and not warmup(), whose call of add_gold() returned before.
        [[(module, offset)]] = learned[first]
        assert module == "target"
        assert earn < offset < earn + 0x40
        assert not ambiguous

        # Restored after a restart, with a stale frame learned before the check
        site = installed[first]["writeInstruction"]
        worker.remove_hook(first)
        libc = next(name for name in _modules(target.pid) if name.startswith("libc.so"))
        second = worker.add_hook(HookSpec(
            None, "int32", MultiplyTransform(2), module=site["module"],
            module_offset=int(site["moduleOffset"], 16), caller_depth=1,
            callers=[[(module, offset)], [(libc, 0x401d3)]]))
        _wait_for(qt_app, lambda: second in installed or failed)
        assert not failed
        assert dropped[second] == 1
        assert learned[second] == [[[module, offset]]]
        assert target.send("add", 1) == (112, 999)  # the real caller still applies
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


def _modules(pid):
    with open(f"/proc/{pid}/maps", encoding="utf-8") as maps:
        return {line.split()[-1].rsplit("/", 1)[-1] for line in maps if "/" in line}
