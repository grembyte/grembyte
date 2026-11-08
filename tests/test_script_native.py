"""Scripts in a native game: the game thread without an engine, and ctx.native
(see hermes/features/script_native.js)."""

import subprocess

from hermes.features.scripts import ScriptSpec

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
    import json

    from hermes.features.scripts import ScriptWorker

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
    from hermes.core import messages
    from hermes.features.scripts import ScriptWorker

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
