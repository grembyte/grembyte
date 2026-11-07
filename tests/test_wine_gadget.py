"""Downloading Frida's Windows gadget: checked against GitHub's digest, written whole."""

import hashlib
import io
import json
import lzma
import struct

import pytest

from hermes.attach import wine_gadget
from hermes.attach.wine_gadget import GadgetError, ensure_gadget


def _pe(machine):
    header = bytearray(0x40)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    return bytes(header) + b"PE\0\0" + struct.pack("<H", machine) + bytes(64)


def _serve(monkeypatch, dll, digest="right"):
    """Answer the release and the download as GitHub would."""
    compressed = lzma.compress(dll)
    name = "frida-gadget-1.2.3-windows-x86_64.dll.xz"
    if digest == "right":
        digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
    release = {"assets": [{"name": name, "digest": digest}]}

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        url = getattr(request, "full_url", request)
        body = json.dumps(release).encode() if "api.github.com" in url else compressed
        return io.BytesIO(body)

    monkeypatch.setattr(wine_gadget.urllib.request, "urlopen", urlopen)


@pytest.fixture(autouse=True)
def _data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_a_checked_gadget_is_written_whole(monkeypatch, tmp_path):
    dll = _pe(0x8664)
    _serve(monkeypatch, dll)
    path = ensure_gadget(64, version="1.2.3")
    assert path.read_bytes() == dll
    assert not list((tmp_path / "hermes-hook" / "gadget").glob("*.part"))


def test_download_reports_progress(monkeypatch):
    _serve(monkeypatch, _pe(0x8664))
    progress = []
    ensure_gadget(64, version="1.2.3", progress=lambda received, total:
                  progress.append((received, total)))
    assert progress[0] == (0, -1)
    assert progress[-1][0] > 0
    assert all(total == -1 for _received, total in progress)


@pytest.mark.parametrize("allowed", [False, True])
def test_connect_uses_the_download_setting(monkeypatch, allowed):
    from hermes.core import settings

    calls = []
    monkeypatch.setattr(settings, "get", lambda _key: allowed)
    monkeypatch.setattr(wine_gadget, "gadget_port", lambda _pid: None)

    def load(_pid, download):
        calls.append(download)
        raise GadgetError("error.gadget.not_present", path="missing")

    monkeypatch.setattr(wine_gadget, "load_gadget", load)
    with pytest.raises(GadgetError):
        wine_gadget.connect(1234)
    assert calls == [allowed]


@pytest.mark.parametrize(
    ("allowed", "answer", "expected"),
    [
        (False, "no", (True, False)),
        (False, "yes", (True, True)),
        (True, "unused", (True, True)),
    ],
)
def test_preparing_a_missing_gadget_respects_consent(
        monkeypatch, tmp_path, allowed, answer, expected):
    from hermes.ui import main_window

    QMessageBox = main_window.QMessageBox

    class FakeMessageBox:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def question(*_args):
            button = QMessageBox.StandardButton.No
            if answer == "yes":
                button = QMessageBox.StandardButton.Yes
            return button

    class Harness:
        translations = {
            "gadget.download_title": "Download",
            "gadget.download_prompt": "Missing at {path}",
        }
        _current_pid = None

        def _download_wine_gadget(self, bits):
            downloads.append(bits)
            return True

    saved, applied, downloads = [], [], []

    class Repository:
        def save(self, values):
            saved.append(values)

    monkeypatch.setattr(main_window, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(main_window, "is_wine_process", lambda _pid: True)
    monkeypatch.setattr(main_window, "wine_bits", lambda _pid: 64)
    monkeypatch.setattr(main_window, "gadget_path", lambda _bits: tmp_path / "missing.dll")
    monkeypatch.setattr(main_window.app_settings, "get", lambda _key: allowed)
    monkeypatch.setattr(main_window.app_settings, "apply", applied.append)
    monkeypatch.setattr(main_window, "SettingsRepository", Repository)

    prepare = getattr(main_window.MainWindow, "_prepare_wine_gadget")
    result = prepare(Harness(), 1234)

    assert result == expected
    assert downloads == ([64] if expected[1] else [])
    assert saved == ([{"wine_gadget_download": True}] if not allowed and answer == "yes" else [])
    assert applied == saved


@pytest.mark.parametrize("current_bits, expected", [(64, [64]), (None, [32, 64])])
def test_gadget_check_offers_missing_compatible_architectures(
        monkeypatch, tmp_path, current_bits, expected):
    from hermes.ui import main_window

    class FakeMessageBox:
        StandardButton = main_window.QMessageBox.StandardButton

        @staticmethod
        def question(*_args):
            return FakeMessageBox.StandardButton.Yes

    class Harness:
        _current_pid = 1234 if current_bits else None
        translations = {
            "gadget.check_title": "Check",
            "gadget.check_prompt": "Missing: {paths}",
            "gadget.check_current": "Current",
        }

        def __init__(self):
            self.downloads = []
            self.restarts = 0
            self.status_updates = 0

        def _download_wine_gadget(self, bits):
            self.downloads.append(bits)
            return True

        def _restart_current_frida_workers(self):
            self.restarts += 1

        def _update_backend_label(self):
            self.status_updates += 1

    window = Harness()
    monkeypatch.setattr(main_window, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(
        main_window, "is_wine_process", lambda _pid: current_bits is not None)
    monkeypatch.setattr(main_window, "wine_bits", lambda _pid: current_bits)
    monkeypatch.setattr(
        main_window, "gadget_path",
        lambda bits: tmp_path / f"frida-gadget-current-{bits}.dll",
    )

    check = getattr(main_window.MainWindow, "_check_wine_gadgets")
    check(window)

    assert window.downloads == expected
    assert window.restarts == (1 if current_bits == 64 else 0)
    assert window.status_updates == 1


def test_gadget_check_is_inert_on_windows(monkeypatch):
    from types import SimpleNamespace
    from hermes.ui import main_window

    class Harness:
        _current_pid = None

    monkeypatch.setattr(main_window, "sys", SimpleNamespace(platform="win32"))
    check = getattr(main_window.MainWindow, "_check_wine_gadgets")
    check(Harness())


@pytest.mark.parametrize("present, expected", [(False, " (Update available)"), (True, "")])
def test_gadget_status_marks_a_missing_current_version(
        monkeypatch, tmp_path, present, expected):
    from hermes.ui import main_window

    path = tmp_path / "frida-gadget-current-64.dll"
    if present:
        path.touch()

    class Harness:
        _current_pid = 1234
        translations = {
            "status.gadget": "  •  Frida via Wine gadget",
            "status.gadget_update": " (Update available)",
        }

    monkeypatch.setattr(main_window, "is_wine_process", lambda _pid: True)
    monkeypatch.setattr(main_window, "gadget_port", lambda _pid: 27100)
    monkeypatch.setattr(main_window, "gadget_loader", lambda _pid: None)
    monkeypatch.setattr(main_window, "wine_bits", lambda _pid: 64)
    monkeypatch.setattr(main_window, "gadget_path", lambda _bits: path)

    gadget_note = getattr(main_window.MainWindow, "_gadget_note")
    assert gadget_note(Harness()).endswith(expected)


def test_a_gadget_that_does_not_match_its_digest_is_not_used(monkeypatch):
    _serve(monkeypatch, _pe(0x8664), digest="sha256:" + "0" * 64)
    with pytest.raises(GadgetError, match="SHA-256"):
        ensure_gadget(64, version="1.2.3")
    with pytest.raises(GadgetError, match="not present"):
        ensure_gadget(64, version="1.2.3", download=False)


def test_a_gadget_for_another_architecture_is_not_used(monkeypatch):
    _serve(monkeypatch, _pe(0x14C))
    with pytest.raises(GadgetError, match="no x86_64 DLL"):
        ensure_gadget(64, version="1.2.3")


def test_a_release_without_digests_still_checks_the_dll(monkeypatch):
    _serve(monkeypatch, _pe(0x8664), digest=None)
    assert ensure_gadget(64, version="1.2.3").exists()


# The ptrace loader on a native process: three threads call hot() all the
# time, another one makes threads, and the process counts the SIGUSR1s it
# gets. answer() stands in for LoadLibraryW; trap_site() is never called.
_BORROW_SOURCE = r"""
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <unistd.h>
volatile sig_atomic_t signals;
volatile int called, hot_calls;
static void on_signal(int sig) { (void) sig; signals++; }
__attribute__((noinline)) void hot(void) { hot_calls++; }
__attribute__((noinline)) long answer(void) { called++; return 42; }
__attribute__((noinline)) void trap_site(void) { __asm__ volatile("nop"); }
static void *worker(void *unused) {
  (void) unused;
  for (;;) { hot(); usleep(100); }
  return NULL;
}
static void *short_lived(void *unused) { (void) unused; return NULL; }
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
  for (int i = 0; i < 3; i++) pthread_create(&thread, NULL, worker, NULL);
  pthread_create(&thread, NULL, spawner, NULL);
  printf("%d %p %p %p\n", (int)getpid(), (void*)hot, (void*)answer, (void*)trap_site);
  fflush(stdout);
  char line[64];
  while (fgets(line, sizeof line, stdin)) {
    int before = hot_calls;
    usleep(50000);
    printf("%d %d %d\n", (int)signals, called, hot_calls > before);
    fflush(stdout);
  }
  return 0;
}
"""


def _can_trace():
    import platform  # pylint: disable=import-outside-toplevel
    import shutil  # pylint: disable=import-outside-toplevel
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", encoding="ascii") as scope:
            allowed = scope.read().strip() == "0"
    except OSError:
        allowed = True
    return allowed and shutil.which("gcc") and platform.machine() == "x86_64"


@pytest.mark.skipif(not _can_trace(), reason="needs gcc, x86-64 and ptrace_scope=0")
def test_a_borrowed_thread_calls_and_the_others_keep_their_signals(tmp_path):
    # pylint: disable=import-outside-toplevel,protected-access
    import os
    import signal
    import subprocess
    import threading
    import time

    source = tmp_path / "borrow.c"
    source.write_text(_BORROW_SOURCE)
    binary = tmp_path / "borrow"
    subprocess.run(["gcc", "-O1", "-pthread", str(source), "-o", str(binary)], check=True)
    target = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True)
    sent, stop = [0], threading.Event()

    def signal_all_the_time():
        while not stop.is_set():
            os.kill(target.pid, signal.SIGUSR1)
            sent[0] += 1
            time.sleep(0.01)  # apart enough that the kernel does not merge them

    pinger = threading.Thread(target=signal_all_the_time)
    memory = None
    try:
        pid, hot, answer, trap = (int(field, 0) for field in target.stdout.readline().split())
        memory = wine_gadget._Memory(pid)
        pinger.start()
        for _round in range(3):
            tracee = wine_gadget._Tracee(pid)
            tracee.attach()
            try:
                tracee.set_breakpoints(memory, [hot])
                tid, address = tracee.catch_breakpoint(5)
                assert address == hot
                # The other workers reach hot() and wait there; what else the
                # threads report is dealt with, as the loader does while it waits.
                deadline = time.monotonic() + 0.05
                while time.monotonic() < deadline:
                    event = tracee.next_event()
                    if event is None:
                        time.sleep(0.002)
                    else:
                        tracee.service(*event)
                tracee.clear_breakpoints(memory)
                wine_gadget._call_load_library(memory, tracee, tid, answer, trap, b"x\0", 5)
            finally:
                tracee.clear_breakpoints(memory)
                tracee.detach()
            time.sleep(0.1)
        stop.set()
        pinger.join()
        time.sleep(0.2)

        stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
        assert stat[stat.rindex(")") + 2] != "T"
        target.stdin.write("count\n")
        target.stdin.flush()
        signals, called, hot_runs = (int(field) for field in target.stdout.readline().split())
        assert (signals, called, hot_runs) == (sent[0], 3, 1)
    finally:
        stop.set()
        if pinger.is_alive():
            pinger.join()
        if memory is not None:
            memory.close()
        target.kill()
        target.wait()
