"""Tests for the ptrace hook backend (Option A: value rewrite at a watchpoint)."""

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from grembyte.attach.ptrace_engine import WATCHPOINT_SLOTS, build_dr7, combined_dr7


def test_combined_dr7_matches_single_slot_for_slot_zero():
    assert combined_dr7({0: (4, False)}) == build_dr7(4, False)


def test_combined_dr7_arms_each_slot_independently():
    two = combined_dr7({0: (4, False), 2: (1, True)})
    assert two == combined_dr7({0: (4, False)}) | combined_dr7({2: (1, True)})
    # Enable bits for slots 0 and 2, nothing for 1 and 3.
    assert two & 0b1 and two & (1 << 4)
    assert not two & (1 << 2) and not two & (1 << 6)


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-outside-toplevel,import-error,no-name-in-module

    return QApplication.instance() or QApplication([])


def test_claim_address_uses_four_slots_then_refuses(qt_app):
    # pylint: disable=unused-argument,protected-access
    from grembyte.hooks.ptrace_hook import PtraceHookWorker  # pylint: disable=import-outside-toplevel

    worker = PtraceHookWorker(os.getpid())
    worker._read_value = lambda *args: 0  # avoid touching real memory

    slots = [worker._claim_address(0x1000 + 4 * i, "int32")["slot"]
             for i in range(WATCHPOINT_SLOTS)]
    assert sorted(slots) == list(range(WATCHPOINT_SLOTS))  # four distinct slots
    assert worker._claim_address(0x9000, "int32") is None  # no slot left
    # A second hook on an already-watched address reuses its slot.
    assert worker._claim_address(0x1000, "int32")["slot"] == slots[0]


def test_unaligned_addresses_take_no_watchpoint(qt_app):
    # pylint: disable=unused-argument,protected-access,import-outside-toplevel
    from grembyte.attach.ptrace_engine import watch_alignment_error
    from grembyte.hooks.ptrace_hook import PtraceHookWorker

    # The kernel refuses them; a thread whose registers failed stayed stopped.
    assert watch_alignment_error(0x1002, 4) and watch_alignment_error(0x1004, 8)
    assert watch_alignment_error(0x1004, 4) is None and watch_alignment_error(0x1003, 1) is None
    worker = PtraceHookWorker(os.getpid())
    worker._read_value = lambda *args: 0
    assert worker._claim_address(0x1002, "int32") is None and not worker._slots


# ---------------------------------------------------------------------------
# End-to-end against a real process.
# ---------------------------------------------------------------------------

_TARGET_SOURCE = """
#include <stdio.h>
#include <unistd.h>
volatile int gold = 100;
int main(void) {
  printf("%d %p\\n", (int)getpid(), (void*)&gold);
  fflush(stdout);
  char line[64];
  while (fgets(line, sizeof line, stdin)) {
    int delta = 0;
    sscanf(line, "%d", &delta);
    gold += delta;
    printf("%d\\n", gold);
    fflush(stdout);
  }
  return 0;
}
"""


def _ptrace_allowed():
    try:
        return Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip() == "0"
    except OSError:
        return True


requires_ptrace_target = pytest.mark.skipif(
    shutil.which("gcc") is None
    or not _ptrace_allowed()
    or platform.machine() != "x86_64",
    reason="needs gcc, x86-64, and ptrace_scope=0 to attach to a test process",
)


@requires_ptrace_target
def test_ptrace_hook_freezes_multiplies_and_removes(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.hooks.engine import HookSpec
    from grembyte.hooks.ptrace_hook import PtraceHookWorker
    from grembyte.hooks.transforms import FreezeTransform, MultiplyTransform

    source = tmp_path / "htarget.c"
    source.write_text(_TARGET_SOURCE)
    binary = tmp_path / "htarget"
    subprocess.run(["gcc", "-O2", str(source), "-o", str(binary)], check=True)

    target = subprocess.Popen(
        [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        pid, address = target.stdout.readline().split()
        pid, address = int(pid), int(address, 16)
        installed, failed, errors, ready = {}, {}, {}, []
        worker = PtraceHookWorker(pid)
        worker.hook_installed.connect(lambda i, info: installed.update({i: info}))
        worker.hook_failed.connect(lambda i, message: failed.update({i: message}))
        worker.transform_error.connect(lambda i, message: errors.update({i: message}))
        worker.session_ready.connect(ready.append)
        worker.start()

        def pump(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.01)

        def send(delta):
            target.stdin.write(f"{delta}\n")
            target.stdin.flush()
            return int(target.stdout.readline())

        pump(0.8)
        assert ready == ["x86-64"], ready

        hook_id = worker.add_hook(
            HookSpec(address, "int32", FreezeTransform(100), trap_pc=address)
        )
        pump(0.5)
        assert hook_id in installed and not failed

        # Freeze: every write is forced back to 100.
        assert [send(5), send(5), send(5)] == [100, 100, 100]

        # Multiply increases tenfold.
        worker.set_transform(hook_id, MultiplyTransform(10))
        pump(0.3)
        base = send(0)
        assert send(5) == base + 50

        # Removing the hook lets the value change freely again.
        worker.remove_hook(hook_id)
        pump(0.4)
        base = send(0)
        assert send(5) == base + 5

        worker.requestInterruption()
        assert worker.wait(3000)
        # The target keeps running after the hook detaches.
        assert send(1) == base + 6
        assert not errors
    finally:
        target.kill()
        target.wait()


@requires_ptrace_target
def test_ptrace_hooks_changing_keep_signals_and_new_threads(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    import signal

    from grembyte.hooks.engine import HookSpec
    from grembyte.hooks.ptrace_hook import PtraceHookWorker
    from grembyte.hooks.transforms import MultiplyTransform
    from tests.test_ptrace_watch import _BUSY_SOURCE, _state

    source = tmp_path / "busy.c"
    source.write_text(_BUSY_SOURCE)
    binary = tmp_path / "busy"
    subprocess.run(["gcc", "-O2", "-pthread", str(source), "-o", str(binary)], check=True)
    target = subprocess.Popen(
        [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    sent = 0

    def signal_for(seconds):
        nonlocal sent
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            os.kill(target.pid, signal.SIGUSR1)
            sent += 1
            qt_app.processEvents()
            time.sleep(0.02)

    try:
        pid, address = target.stdout.readline().split()
        pid, address = int(pid), int(address, 16)
        installed, ready = {}, []
        worker = PtraceHookWorker(pid)
        worker.hook_installed.connect(lambda i, info: installed.update({i: info}))
        worker.session_ready.connect(ready.append)
        worker.start()
        signal_for(0.5)
        assert ready
        # Each change halts and re-arms every thread (_reprogram).
        for _round in range(4):
            hook_id = worker.add_hook(
                HookSpec(address, "int32", MultiplyTransform(2), trap_pc=address))
            signal_for(0.2)
            assert hook_id in installed
            worker.remove_hook(hook_id)
            signal_for(0.2)
        worker.requestInterruption()
        signal_for(0.1)
        assert worker.wait(10000)

        time.sleep(0.2)
        assert _state(target.pid) != "T"
        target.stdin.write("count\n")
        target.stdin.flush()
        assert int(target.stdout.readline()) == sent
    finally:
        target.kill()
        target.wait()
