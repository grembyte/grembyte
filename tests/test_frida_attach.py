"""Grembyte refuses to attach next to another Frida host (frida_attach.py)."""

import subprocess
import sys
import time

import frida
import pytest

from grembyte.attach import frida_attach
from grembyte.core import messages
from grembyte.platform.linux import frida_agents
from tests.test_hooks import linux_only, requires_target

_MAPS = """\
55d0c0000000-55d0c0001000 r-xp 00000000 fd:01 1234 /usr/bin/game
7f0000000000-7f0000100000 r-xp 00000000 00:01 78589 /memfd:frida-agent-64.so (deleted)
7f0000100000-7f0000110000 rw-p 00100000 00:01 78589 /memfd:frida-agent-64.so (deleted)
7f0000200000-7f0000300000 r-xp 00000000 00:01 91191 /memfd:frida-agent-64.so (deleted)
7f0000400000-7f0000500000 r-xp 00000000 fd:01 4321 /opt/game/libfrida-gadget.so
7f0000600000-7f0000601000 rw-p 00000000 00:00 0
"""

# Four threads call ``work`` all the time, as a game calls Mono's functions.
_TARGET_SOURCE = r"""
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>
__attribute__((noinline)) int work(int x) { int s = 0; for (int i = 0; i < x; i++) s += i * x; return s; }
int (*volatile call)(int) = work;
static void *spin(void *arg) { (void)arg; for (;;) call(50); return 0; }
int main(void) {
    pthread_t threads[4];
    for (int i = 0; i < 4; i++)
        pthread_create(&threads[i], 0, spin, 0);
    printf("%d\n", getpid());
    fflush(stdout);
    for (;;)
        pause();
}
"""


@linux_only
def test_each_frida_runtime_counts_once(tmp_path):
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "maps").write_text(_MAPS)
    # Two injected agents (two memfds, one mapped twice) and a gadget
    assert frida_agents(42, proc_root=tmp_path) == {
        ("00:01", "78589"), ("00:01", "91191"), ("fd:01", "4321")}
    (tmp_path / "42" / "maps").write_text(_MAPS.splitlines()[0] + "\n")
    assert frida_agents(42, proc_root=tmp_path) == set()
    assert frida_agents(43, proc_root=tmp_path) is None


@pytest.fixture(name="busy_target")
def fixture_busy_target(tmp_path):
    (tmp_path / "busy.c").write_text(_TARGET_SOURCE)
    binary = tmp_path / "busy"
    subprocess.run(["gcc", "-O1", "-pthread", str(tmp_path / "busy.c"), "-o", str(binary)],
                   check=True)
    with subprocess.Popen([str(binary)], stdout=subprocess.PIPE, text=True) as process:
        pid = int(process.stdout.readline())
        yield pid
        process.kill()


def _until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@linux_only
@requires_target
def test_a_second_frida_host_is_refused_its_own_sessions_are_not(busy_target):
    pid = busy_target
    # Sessions of this process share its agent: a second one is fine
    first = frida_attach.attach_session(pid)
    second = frida_attach.attach_session(pid)
    assert len(frida_agents(pid)) == 1
    second.detach()
    first.detach()
    # Right after detaching, its agent may still be there
    frida_attach.attach_session(pid).detach()
    assert _until(lambda: not frida_agents(pid))
    # A session opened past attach_session shares the agent too
    raw = frida.attach(pid)
    frida_attach.attach_session(pid).detach()
    raw.detach()
    # Its agent unloaded, another host's arrives: not taken for its own
    assert _until(lambda: not frida_agents(pid))

    # Another host process attached (another grembyte, another Frida tool)
    other = subprocess.Popen(
        [sys.executable, "-c",
         f"import frida, sys; s = frida.attach({pid}); print('attached', flush=True); "
         "sys.stdin.readline(); s.detach()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "attached"
        with pytest.raises(messages.LocalizedRuntimeError) as refused:
            frida_attach.attach_session(pid)
        assert refused.value.key == "error.attach.foreign_frida"
    finally:
        other.communicate("\n", timeout=10)
    # Once it has left (and this process' agent unloaded), attaching works again
    assert _until(lambda: not frida_agents(pid))
    frida_attach.attach_session(pid).detach()
