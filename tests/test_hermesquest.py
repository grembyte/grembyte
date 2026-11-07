"""Hermes against a build of HermesQuest (test_app/unity), the real Unity Mono runtime.

The build is not in the repository (see test_app/unity/README.md); without
it, or with one older than what a test needs, the tests are skipped. The
game runs without a window (``-batchmode -nographics``).
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_hooks import _wait_for, fixture_qt_app  # pylint: disable=unused-import

_BUILD = Path(__file__).resolve().parent.parent / "test_app" / "unity" / "Build"
_GAME = (_BUILD / "Windows" / "HermesQuest.exe" if sys.platform == "win32"
         else _BUILD / "Linux" / "HermesQuest.x86_64")

requires_build = pytest.mark.skipif(not _GAME.exists(), reason=f"no HermesQuest build at {_GAME}")

# The token and Mono's name of a method once Assembly-CSharp is loaded, null
# while it is not, 'missing' if the class or method is.
_FIND_METHOD = r"""
let mono;
const f = (name, ret, args) => new NativeFunction(mono.getExportByName(name), ret, args);
rpc.exports = {
  find(namespace, className, name, params) {
    // Unity loads Mono a moment after the start
    mono = Process.enumerateModules().find(m => m.findExportByName('mono_get_root_domain') !== null);
    if (mono === undefined)
      return null;
    const image = f('mono_image_loaded', 'pointer', ['pointer'])(Memory.allocUtf8String('Assembly-CSharp'));
    if (image.isNull())
      return null;
    const thread = f('mono_thread_attach', 'pointer', ['pointer'])(f('mono_get_root_domain', 'pointer', [])());
    try {
      const klass = f('mono_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer'])(
        image, Memory.allocUtf8String(namespace), Memory.allocUtf8String(className));
      if (klass.isNull())
        return 'missing';
      const method = f('mono_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int'])(
        klass, Memory.allocUtf8String(name), params);
      if (method.isNull())
        return 'missing';
      return [f('mono_method_get_token', 'uint32', ['pointer'])(method),
              f('mono_method_full_name', 'pointer', ['pointer', 'int'])(method, 1).readUtf8String()];
    } finally {
      f('mono_thread_detach', 'void', ['pointer'])(thread);
    }
  }
};
"""


class _Game:
    def __init__(self, log):
        self.log = log
        self.process = subprocess.Popen(  # pylint: disable=consider-using-with
            [str(_GAME), "-batchmode", "-nographics", "-logFile", str(log), "-seed", "1"])
        self.pid = self.process.pid

    def identity(self, namespace, class_name, name, params):
        """The identity of a method of Assembly-CSharp; skips if the build lacks it."""
        import frida  # pylint: disable=import-outside-toplevel

        session = frida.attach(self.pid)
        try:
            script = session.create_script(_FIND_METHOD)
            script.load()
            deadline = time.monotonic() + 30
            found = None
            while found is None and time.monotonic() < deadline:
                found = script.exports_sync.find(namespace, class_name, name, params)
                time.sleep(0.2)
            script.unload()
        finally:
            session.detach()
        if found is None:
            raise AssertionError("HermesQuest did not load Assembly-CSharp")
        if found == "missing":
            pytest.skip(f"the HermesQuest build has no {namespace}.{class_name}:{name}; rebuild it")
        token, method = found
        return {"image": "Assembly-CSharp", "file": None, "token": token, "method": method}

    def alive(self):
        return self.process.poll() is None

    def close(self):
        self.process.kill()
        self.process.wait()


@requires_build
@pytest.mark.parametrize("returns", [False, True])
def test_monitor_leaves_exceptions_thrown_through_the_method_to_the_game(tmp_path, qt_app, returns):
    # Rumors.Tick catches what Rumors.Parse throws for a garbled line, every
    # second or two. A hook that put its own return address on the stack (as
    # Frida's onEnter and onLeave do) hid Tick from Mono's unwinder: the
    # exception went unhandled and Unity ended the game.
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker

    game = _Game(tmp_path / "Player.log")
    worker = MethodMonitorWorker(game.pid)
    added, failed, calls = {}, {}, []
    worker.monitor_added.connect(added.__setitem__)
    worker.monitor_failed.connect(failed.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))
    try:
        identity = game.identity("HermesQuest", "Rumors", "Parse", 1)
        worker.start()
        monitor_id = worker.add_monitor(identity, returns=returns)
        _wait_for(qt_app, lambda: monitor_id in added or monitor_id in failed, timeout=30)
        assert not failed
        assert added[monitor_id]["viaStack"] is False

        # Five rumors, two of them garbled; then another round
        _wait_for(qt_app, lambda: len(calls) >= (6 if returns else 10) or not game.alive(),
                  timeout=30)
        assert game.alive()
        lines = [call["args"][0]["text"] for call in calls]
        if returns:
            # A call that threw has no result and is not shown.
            assert {call["result"]["text"] for call in calls} == {"3", "1", "12"}
            assert not any(line.startswith(('"seven', '"?')) for line in lines)
        else:
            assert any(line.startswith('"seven:') for line in lines)
            assert any(line.startswith('"?:') for line in lines)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        game.close()
    log = game.log.read_text(encoding="utf-8", errors="replace")
    assert "UnhandledExceptionEventArgs" not in log
