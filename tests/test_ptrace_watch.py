"""Tests for the ptrace-backed watch backend."""

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes.core.ptrace_watch import build_dr7, decode_access


@pytest.mark.parametrize(
    ("size", "reads", "expected"),
    [
        (1, False, 0x10001),
        (2, False, 0x50001),
        (4, False, 0xD0001),
        (8, False, 0x90001),
        (4, True, 0xF0001),  # read/write condition
    ],
)
def test_build_dr7_encodes_length_and_condition(size, reads, expected):
    assert build_dr7(size, reads) == expected


def test_build_dr7_rejects_unsupported_size():
    with pytest.raises(ValueError, match="Unsupported watch size"):
        build_dr7(3, False)


def _ending_with(instruction, padding=60):
    # NOP padding before the instruction feeds the sync-vote heuristic.
    code = b"\x90" * padding + instruction
    return code, len(code)


def test_decode_access_tells_reads_from_writes():
    # mov [rbx+0x10], eax  -> write only
    code, pc = _ending_with(b"\x89\x43\x10")
    assert decode_access(code, pc, 64, False) == (False, True)

    # mov eax, [rbx+0x10]  -> read only (read watch active)
    code, pc = _ending_with(b"\x8b\x43\x10")
    assert decode_access(code, pc, 64, True) == (True, False)

    # add [rbx+0x10], eax  -> read and write
    code, pc = _ending_with(b"\x01\x43\x10")
    assert decode_access(code, pc, 64, True) == (True, True)


def test_decode_access_write_watch_always_reports_write():
    # A read instruction seen through a write watch still counts as a write,
    # so a real write is never hidden.
    code, pc = _ending_with(b"\x8b\x43\x10")
    assert decode_access(code, pc, 64, False) == (True, True)


def test_decode_access_handles_32_bit_code():
    code, pc = _ending_with(b"\x89\x43\x10")
    assert decode_access(code, pc, 32, False) == (False, True)


def test_decode_access_defaults_to_write_when_undecodable():
    code, pc = _ending_with(b"", padding=60)  # only NOPs, no memory operand
    assert decode_access(code, pc, 64, False) == (False, True)


# ---------------------------------------------------------------------------
# End-to-end test against a real process.
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
    gold += 7;
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
def test_ptrace_watch_sees_writes_and_leaves_target_running(tmp_path):
    # pylint: disable=import-outside-toplevel
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from hermes.core.ptrace_watch import PtraceWriteWatchWorker

    source = tmp_path / "wtarget.c"
    source.write_text(_TARGET_SOURCE)
    binary = tmp_path / "wtarget"
    subprocess.run(["gcc", "-O2", str(source), "-o", str(binary)], check=True)

    app = QApplication.instance() or QApplication([])
    target = subprocess.Popen(
        [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        pid, address = target.stdout.readline().split()
        hits, ready, failed = [], [], []
        worker = PtraceWriteWatchWorker(int(pid), int(address, 16), 4, reads=True)
        worker.hits_ready.connect(hits.extend)
        worker.monitor_ready.connect(lambda count, _d: ready.append(count))
        worker.monitor_failed.connect(failed.append)
        worker.start()

        def pump(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)

        pump(1.0)
        assert ready and not failed, (ready, failed)

        target.stdin.write("x\n")
        target.stdin.flush()
        assert target.stdout.readline().strip() == "107"
        pump(0.6)

        worker.requestInterruption()
        assert worker.wait(3000)

        # The target must keep running normally after the watch detaches.
        target.stdin.write("y\n")
        target.stdin.flush()
        assert target.stdout.readline().strip() == "114"

        assert any(hit["write"] for hit in hits), hits
        assert all(hit["value"] == "6b 00 00 00" for hit in hits)  # 0x6b == 107
    finally:
        target.kill()
        target.wait()
