"""Scripts in a native game: the game thread without an engine, and ctx.native
(see grembyte/features/script_native.js)."""

import json
import subprocess

from grembyte.features.scripts import ScriptSpec

from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    fixture_qt_app,
    requires_target,
)


# A game of its own engine: its main thread polls glfw's events each frame; a
# second thread polls SDL's, which comes first in the agent's list. The agent
# must take the main thread's. Exported (-rdynamic) like a library's.
_TARGET_SOURCE = r"""
#include <pthread.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

struct Menu { int state; int page; };
struct Menu menu = { 5, 5 };
int frame_calls_on_main;

/* Bytes found by ctx.native.scan: one unique run, and a prefix twice */
__attribute__((used)) const unsigned char marker_a[] =
    { 0x5a, 0xc3, 0x11, 0x7e, 0xa1, 0x42, 0x93, 0x0f, 0x66 };
__attribute__((used)) const unsigned char marker_b[] =
    { 0x5a, 0xc3, 0x11, 0x7e, 0xb7, 0x21, 0x84, 0x3c, 0x55 };

__attribute__((noinline)) void open_page(int page, unsigned char replace) {
    menu.page = replace ? -page : page;
}

__attribute__((noinline)) int glfwPollEvents(void) { return frame_calls_on_main++; }
__attribute__((noinline)) int SDL_PollEvent(void *event) { return event != 0; }

static void pause_ms(int ms) {
    struct timespec wait = { 0, ms * 1000000L };
    nanosleep(&wait, 0);
}

static void *other_thread(void *unused) {
    for (;;) {
        SDL_PollEvent(unused);
        pause_ms(2);
    }
    return 0;
}

int main(void) {
    pthread_t thread;
    pthread_create(&thread, 0, other_thread, 0);
    printf("%d %p\n", getpid(), (void *)marker_a);
    fflush(stdout);
    for (;;) {
        glfwPollEvents();
        pause_ms(5);
    }
}
"""


def _build(tmp_path):
    (tmp_path / "game.c").write_text(_TARGET_SOURCE)
    binary = tmp_path / "game"
    subprocess.run(["gcc", "-O1", "-rdynamic", "-pthread", str(tmp_path / "game.c"),
                    "-o", str(binary)], check=True)
    return binary


# Runs on the game thread: where it ran, what the scans and the call give
_SCRIPT = r"""
const n = ctx.native;
const game = n.module();
const errors = [];
for (const attempt of [() => n.scan("5a c3 11 7e"), () => n.scan("01 02 03 04 05 06 07 08 09"),
                       () => n.scan("zz 11"), () => n.module("no_such_module.so")])
  try { attempt(); errors.push(null); } catch (e) { errors.push(String(e.message)); }
const code = Memory.alloc(16);
code.writeByteArray([0x48, 0x8d, 0x05, 0x10, 0x00, 0x00, 0x00]);  // lea rax, [rip+0x10]
const menu = game.getSymbolByName("menu");
n.fn(game.getSymbolByName("open_page"), "void", ["int", "uint8"])(11, 0);
return JSON.stringify({
  thread: Process.getCurrentThreadId(),
  module: game.name,
  marker: n.scan("5A C3 11 7E A1 ?? 93 ? 66").toString(),
  all: n.scanAll("5ac3117e").length,
  rip: n.rip(code, 3, 7).sub(code).toInt32(),
  page: menu.add(4).readS32(),
  errors
});
"""


@requires_target
def test_native_script_runs_on_the_main_thread_and_finds_code_by_bytes(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    runs, failed = [], []
    worker.script_ran.connect(lambda _id, run: runs.append(run))
    worker.script_failed.connect(lambda _id, message: failed.append(message))
    worker.worker_failed.connect(failed.append)
    try:
        worker.start()
        script_id = worker.add_script(ScriptSpec(source=_SCRIPT, mode="manual"))
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: runs or failed, timeout=10)
        assert not failed
        run = runs[0]
        assert run["error"] is None, run["error"]
        # glfwPollEvents on the main thread, not SDL_PollEvent on the other
        assert run["gameThread"] == {"name": "glfwPollEvents", "thread": target.pid,
                                     "main": True}
        result = json.loads(json.loads(run["result"]))
        assert result["thread"] == target.pid
        assert result["module"] == "game"
        assert int(result["marker"], 16) == target.address
        assert result["all"] == 2
        assert result["rip"] == 7 + 0x10
        assert result["page"] == 11
        ambiguous, missing, bad_pattern, no_module = result["errors"]
        assert "error.native.pattern_ambiguous" in ambiguous and '"count":2' in ambiguous
        assert "error.native.pattern_missing" in missing
        assert "error.native.pattern" in bad_pattern
        assert "error.native.no_module" in no_module

        # Reported once only
        runs.clear()
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: runs or failed, timeout=10)
        assert runs[0]["error"] is None and runs[0].get("gameThread") is None
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_a_native_game_without_frame_functions_says_so(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.core import messages
    from grembyte.features.scripts import ScriptWorker

    source = tmp_path / "plain.c"
    source.write_text(r"""
#include <stdio.h>
#include <unistd.h>
int main(void) { printf("%d 0\n", getpid()); fflush(stdout); for (;;) pause(); }
""")
    binary = tmp_path / "plain"
    subprocess.run(["gcc", str(source), "-o", str(binary)], check=True)
    target = _Target(binary)
    worker = ScriptWorker(target.pid, language="en")
    runs, failed = [], []
    worker.script_ran.connect(lambda _id, run: runs.append(run))
    worker.worker_failed.connect(failed.append)
    try:
        worker.start()
        script_id = worker.add_script(ScriptSpec(source="return 1;", mode="manual"))
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: runs or failed, timeout=10)
        assert not failed
        # libc exports none of the frame functions: an error, not a hang
        assert "no frame function" in messages.describe(runs[0]["error"])
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


def _menu_address(binary, marker):
    """Where the global ``menu`` lies in the running game: as far from
    ``marker_a`` (whose address the game prints) as in the file."""
    symbols = subprocess.run(["nm", str(binary)], check=True, capture_output=True,
                             text=True).stdout
    found = {line.split()[-1]: int(line.split()[0], 16) for line in symbols.splitlines()
             if line.split()[-1] in ("menu", "marker_a")}
    return marker + found["menu"] - found["marker_a"]


@requires_target
def test_a_script_calls_game_code_that_a_write_watch_traps(tmp_path, qt_app):
    """A hardware watchpoint on what the called game code writes: the watch
    takes the trap and the call runs to its end (Frida's default would abort
    it with "breakpoint triggered")."""
    # pylint: disable=import-outside-toplevel
    import frida

    from grembyte.features.scripts import ScriptWorker
    from grembyte.features.write_watch import _build_agent_source

    binary = _build(tmp_path)
    target = _Target(binary)
    page = _menu_address(binary, target.address) + 4
    worker = ScriptWorker(target.pid)
    runs, failed, hits = [], [], []
    worker.script_ran.connect(lambda _id, run: runs.append(run))
    worker.script_failed.connect(lambda _id, message: failed.append(message))
    worker.worker_failed.connect(failed.append)
    session = None
    try:
        session = frida.attach(target.pid)
        watch = session.create_script(_build_agent_source(page, 4))
        watch.on("message", lambda message, _data: hits.extend(
            message.get("payload", {}).get("hits", [])
            if message.get("type") == "send" else []))
        watch.load()
        worker.start()
        script_id = worker.add_script(ScriptSpec(source=r"""
const game = ctx.native.module();
ctx.native.fn(game.getSymbolByName("open_page"), "void", ["int", "uint8"])(7, 0);
return game.getSymbolByName("menu").add(4).readS32();
""", mode="manual"))
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: runs or failed, timeout=10)
        assert not failed
        assert runs[0]["error"] is None, runs[0]["error"]
        assert runs[0]["result"] == "7"
        _wait_for(qt_app, lambda: hits, timeout=5)  # the watch saw open_page's write
        watch.exports_sync.stop()
        watch.unload()
    finally:
        if session is not None:
            session.detach()
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_a_script_reads_and_writes_what_an_access_watch_traps(tmp_path, qt_app):
    """A hardware watchpoint on reads and writes of what the script itself
    reads and writes: the accesses happen, no "breakpoint triggered"."""
    # pylint: disable=import-outside-toplevel
    import frida

    from grembyte.features.scripts import ScriptWorker
    from grembyte.features.write_watch import _build_agent_source

    binary = _build(tmp_path)
    target = _Target(binary)
    page = _menu_address(binary, target.address) + 4
    worker = ScriptWorker(target.pid)
    runs, failed = [], []
    worker.script_ran.connect(lambda _id, run: runs.append(run))
    worker.script_failed.connect(lambda _id, message: failed.append(message))
    worker.worker_failed.connect(failed.append)
    session = None
    try:
        session = frida.attach(target.pid)
        watch = session.create_script(_build_agent_source(page, 4, reads=True))
        watch.load()
        worker.start()
        script_id = worker.add_script(ScriptSpec(source=r"""
const page = ctx.native.module().getSymbolByName("menu").add(4);
page.writeS32(42);
const first = page.readS32();
page.writeByteArray([0x2b, 0x00, 0x00, 0x00]);
const bytes = Array.from(new Uint8Array(page.readByteArray(4)));
page.writeU32(page.readU32() + 1);
return JSON.stringify({ first, bytes, last: page.readS32() });
""", mode="manual"))
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: runs or failed, timeout=10)
        assert not failed
        assert runs[0]["error"] is None, runs[0]["error"]
        assert json.loads(json.loads(runs[0]["result"])) == \
            {"first": 42, "bytes": [0x2b, 0, 0, 0], "last": 44}
        watch.exports_sync.stop()
        watch.unload()
    finally:
        if session is not None:
            session.detach()
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
