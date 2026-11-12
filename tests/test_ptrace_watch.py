"""Tests for the ptrace-backed watch backend."""

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from grembyte.features.ptrace_watch import build_dr7, decode_access


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
    from PySide6.QtWidgets import QApplication  # pylint: disable=no-name-in-module

    from grembyte.features.ptrace_watch import PtraceWriteWatchWorker

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


# Threads come and go all the time, and the process counts the SIGUSR1s it
# gets: neither may be lost or left stopped while the watch attaches, runs
# and detaches.
_BUSY_SOURCE = r"""
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <unistd.h>
volatile int gold = 100;
volatile sig_atomic_t signals;
static void on_signal(int sig) { (void) sig; signals++; }
static void *short_lived(void *unused) { (void) unused; gold += 0; return NULL; }
static void *spawner(void *unused) {
  (void) unused;
  for (;;) {
    pthread_t thread;
    if (pthread_create(&thread, NULL, short_lived, NULL) == 0)
      pthread_join(thread, NULL);
    usleep(200);
  }
  return NULL;
}
int main(void) {
  pthread_t thread;
  signal(SIGUSR1, on_signal);
  pthread_create(&thread, NULL, spawner, NULL);
  printf("%d %p\n", (int)getpid(), (void*)&gold);
  fflush(stdout);
  char line[64];
  while (fgets(line, sizeof line, stdin)) {
    printf("%d\n", (int)signals);
    fflush(stdout);
  }
  return 0;
}
"""


def _state(pid):
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rindex(")") + 2]


@requires_ptrace_target
def test_ptrace_watch_keeps_signals_and_new_threads_of_the_target(tmp_path):
    # pylint: disable=import-outside-toplevel
    import signal

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=no-name-in-module

    from grembyte.features.ptrace_watch import PtraceWriteWatchWorker

    source = tmp_path / "busy.c"
    source.write_text(_BUSY_SOURCE)
    binary = tmp_path / "busy"
    subprocess.run(["gcc", "-O2", "-pthread", str(source), "-o", str(binary)], check=True)
    app = QApplication.instance() or QApplication([])
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
            app.processEvents()
            time.sleep(0.02)  # apart enough that the kernel does not merge them

    try:
        pid, address = target.stdout.readline().split()
        for _round in range(3):
            ready, failed = [], []
            worker = PtraceWriteWatchWorker(int(pid), int(address, 16), 4)
            worker.monitor_ready.connect(lambda count, _d: ready.append(count))
            worker.monitor_failed.connect(failed.append)
            worker.start()
            signal_for(0.5)
            assert ready and not failed, (ready, failed)
            worker.requestInterruption()
            signal_for(0.1)
            assert worker.wait(10000)
            signal_for(0.1)

        time.sleep(0.2)
        assert _state(target.pid) != "T"
        target.stdin.write("count\n")
        target.stdin.flush()
        assert int(target.stdout.readline()) == sent
    finally:
        target.kill()
        target.wait()
