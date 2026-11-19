"""The watchdog of pauseThreads under Wine (grembyte/agent/platform.js).

A paused section that waits for a lock a suspended game thread holds would
wait for good: RoboCop froze removing a trace's hooks (2026-11-19), the agent
waiting for Frida's Interceptor lock with every game thread suspended. The
watchdog resumes the threads once the agent makes no progress for
PAUSE_LIMIT_MS. Here a test program's thread holds a critical section nearly
all the time, and the agent pauses the threads and then enters it.

Needs Proton's wine, MinGW and Frida's Windows gadget in Grembyte's cache;
skipped otherwise. The Wine prefix is kept in pytest's cache.
"""

import glob
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import frida
import pytest

from grembyte.agent.platform import PLATFORM_AGENT_SOURCE
from grembyte.attach.wine_gadget import gadget_path

PROTON_WINES = sorted(glob.glob(os.path.expanduser(
    "~/.local/share/Steam/steamapps/common/Proton*/files/bin/wine")))
MINGW = "x86_64-w64-mingw32-gcc"

PROGRAM = r"""
#include <windows.h>
#include <stdio.h>

__declspec(dllexport) CRITICAL_SECTION held_lock;

static DWORD WINAPI holder(LPVOID arg) {
  for (;;) {
    EnterCriticalSection(&held_lock);
    Sleep(50);
    LeaveCriticalSection(&held_lock);
    Sleep(1);
  }
  return 0;
}

int main(void) {
  InitializeCriticalSection(&held_lock);
  if (LoadLibraryA("frida-gadget.dll") == NULL)
    return 1;
  CreateThread(NULL, 0, holder, NULL, 0, NULL);
  for (;;)
    Sleep(1000);
}
"""

AGENT = PLATFORM_AGENT_SOURCE + r"""
const game = Process.getModuleByName('game.exe');
const kernel32 = Process.getModuleByName('kernel32.dll');
const exclusive = { scheduling: 'exclusive' };
const enter = new NativeFunction(kernel32.getExportByName('EnterCriticalSection'),
                                 'void', ['pointer'], exclusive);
const leave = new NativeFunction(kernel32.getExportByName('LeaveCriticalSection'),
                                 'void', ['pointer'], exclusive);
rpc.exports = {
  pauseOnly() {
    const began = Date.now();
    const paused = pauseThreads();
    resumeThreads(paused);
    return { ms: Date.now() - began, threads: paused.length };
  },
  pauseAndLock() {
    const lock = game.getExportByName('held_lock');
    const began = Date.now();
    const paused = pauseThreads();
    enter(lock);
    leave(lock);
    const ms = Date.now() - began;
    resumeThreads(paused);
    return { ms, threads: paused.length };
  }
};
"""

pytestmark = pytest.mark.skipif(
    not PROTON_WINES or shutil.which(MINGW) is None or not gadget_path(64).exists(),
    reason="needs Proton's wine, x86_64-w64-mingw32-gcc and the cached 64-bit gadget")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(function, timeout):
    """``function()``, or AssertionError if it does not return in ``timeout`` s."""
    result = {}

    def run():
        try:
            result["value"] = function()
        except (frida.RPCException, frida.InvalidOperationError) as error:
            result["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"no answer within {timeout} s: the game hangs"
    if "error" in result:
        raise result["error"]
    return result["value"]


@pytest.fixture(name="game_session")
def fixture_game_session(tmp_path, request):
    wine = PROTON_WINES[-1]
    prefix = Path(request.config.cache.mkdir("wineprefix-pause-watchdog"))
    env = {**os.environ, "WINEPREFIX": str(prefix), "WINEDEBUG": "-all"}
    (tmp_path / "game.c").write_text(PROGRAM)
    subprocess.run([MINGW, "-O1", "-o", str(tmp_path / "game.exe"), str(tmp_path / "game.c")],
                   check=True)
    (tmp_path / "frida-gadget.dll").write_bytes(gadget_path(64).read_bytes())
    port = _free_port()
    (tmp_path / "frida-gadget.config").write_text(
        '{"interaction": {"type": "listen", "address": "127.0.0.1", '
        f'"port": {port}, "on_load": "resume"}}}}')
    game = subprocess.Popen([wine, "game.exe"], cwd=tmp_path, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        device = frida.get_device_manager().add_remote_device(f"127.0.0.1:{port}")
        deadline = time.time() + 90  # a new prefix boots first
        while True:
            try:
                session = device.attach("Gadget")
                break
            except (frida.ServerNotRunningError, frida.TransportError, frida.TimedOutError):
                assert time.time() < deadline, "the gadget did not start"
                time.sleep(0.2)
        yield session
    finally:
        game.kill()
        subprocess.run([str(Path(wine).with_name("wineserver")), "-k"], env=env, check=False)


@pytest.mark.timeout(240)
def test_the_watchdog_resumes_threads_the_agent_waits_for(game_session):
    activity = []

    def on_message(message, _data):
        payload = message.get("payload")
        if isinstance(payload, dict) and "grembyte:activity" in payload:
            activity.append(payload["grembyte:activity"])

    script = game_session.create_script(AGENT)
    script.on("message", on_message)
    script.load()
    api = script.exports_sync

    quick = _call(api.pause_only, 30)
    assert quick["threads"] >= 2 and quick["ms"] < 1000
    assert not activity  # no lock: the agent resumes the threads itself

    # Without the watchdog this never returns (the holder stays suspended).
    locked = _call(api.pause_and_lock, 30)
    assert locked["ms"] >= 2500
    assert activity == ["threads_pause_timeout"]
    script.unload()  # ends the watchdog thread before its code goes
