"""Tests for the value types shared by scanner, address list, freezing and hooks."""

import os
import re
import subprocess
import time

import pytest

from hermes.core.value_types import AUTO_VALUE_TYPES, VALUE_TYPES, value_type
from hermes.hooks.engine import HookSpec, build_agent_source
from hermes.hooks.transforms import MultiplyTransform
from tests.test_hooks import _wait_for, requires_target


@pytest.mark.parametrize(
    ("name", "size", "limits"),
    [
        ("int8", 1, (-128, 127)),
        ("uint8", 1, (0, 255)),
        ("int16", 2, (-32768, 32767)),
        ("uint16", 2, (0, 65535)),
        ("uint32", 4, (0, 2**32 - 1)),
        ("uint64", 8, (0, 2**64 - 1)),
        ("double", 8, None),
    ],
)
def test_value_type_sizes_and_limits(name, size, limits):
    kind = value_type(name)

    assert (kind.size, kind.dtype.itemsize, kind.limits) == (size, size, limits)


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [("uint8", "0xff", 255), ("int16", "-2", -2), ("uint64", str(2**64 - 1), 2**64 - 1),
     ("float", "0.5", 0.5)],
)
def test_parse_accepts_values_in_range(name, text, expected):
    assert value_type(name).parse(text) == expected


@pytest.mark.parametrize(
    ("name", "text"),
    [("uint8", "256"), ("uint8", "-1"), ("int8", "128"), ("uint32", "-1"), ("int16", "x")],
)
def test_parse_rejects_out_of_range_values(name, text):
    with pytest.raises(ValueError, match=f"Invalid {name} value"):
        value_type(name).parse(text)


def test_unknown_value_type_is_rejected():
    with pytest.raises(ValueError, match="Unsupported data type"):
        value_type("string")


def test_auto_scan_types_are_known():
    assert set(AUTO_VALUE_TYPES) <= set(VALUE_TYPES)


def test_hook_agent_supports_every_value_type():
    source = build_agent_source()
    block = source[source.index("const VALUE_IO"):source.index("};", source.index("const VALUE_IO"))]

    assert set(re.findall(r"^  (\w+): \{", block, re.MULTILINE)) == set(VALUE_TYPES)


# --- integration: hooks on 1- and 2-byte values -------------------------------

_SMALL_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int16_t g_small = 100;
uint8_t g_byte = 10;

__attribute__((noinline)) void add_small(int16_t amount) { g_small += amount; }
__attribute__((noinline)) void add_byte(uint8_t amount) { g_byte += amount; }

int main(void) {
    char cmd[16];
    int value;
    printf("%d %p %p\n", getpid(), (void *)&g_small, (void *)&g_byte);
    fflush(stdout);
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "small") == 0) add_small((int16_t)value);
        else if (strcmp(cmd, "byte") == 0) add_byte((uint8_t)value);
        printf("%d %d\n", g_small, g_byte);
        fflush(stdout);
    }
    return 0;
}
"""


class _SmallTarget:
    def __init__(self, binary):
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        pid, small, byte = self.process.stdout.readline().split()
        self.pid = int(pid)
        self.addresses = {"small": int(small, 16), "byte": int(byte, 16)}

    def send(self, command, value):
        self.process.stdin.write(f"{command} {value}\n")
        self.process.stdin.flush()
        small, byte = self.process.stdout.readline().split()
        return int(small), int(byte)

    def close(self):
        self.process.kill()
        self.process.wait()


def _trap_pc(target, command, size):
    """Watch the command's variable with the real write-watch agent."""
    import frida  # pylint: disable=import-outside-toplevel
    from hermes.core.write_watch import _build_agent_source  # pylint: disable=import-outside-toplevel

    hits = []
    session = frida.attach(target.pid)
    script = session.create_script(_build_agent_source(target.addresses[command], size))
    script.on(
        "message",
        lambda message, _data: hits.extend(message.get("payload", {}).get("hits", [])),
    )
    script.load()
    try:
        target.send(command, 1)
        deadline = time.monotonic() + 2
        while not hits and time.monotonic() < deadline:
            time.sleep(0.02)
        assert hits, f"write watch saw no write for {command}"
        return int(hits[0]["pc"], 16)
    finally:
        script.exports_sync.stop()
        script.unload()
        session.detach()


@pytest.fixture(name="qt_app", scope="module")
def fixture_qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # pylint: disable=import-error,no-name-in-module,import-outside-toplevel

    return QApplication.instance() or QApplication([])


@requires_target
def test_hooks_rewrite_small_values(tmp_path, qt_app):
    from hermes.hooks.engine import HookWorker  # pylint: disable=import-outside-toplevel

    source = tmp_path / "small.c"
    source.write_text(_SMALL_TARGET_SOURCE)
    binary = tmp_path / "small"
    subprocess.run(["gcc", "-O2", str(source), "-o", str(binary)], check=True)
    target = _SmallTarget(binary)
    worker = HookWorker(target.pid)
    installed, failed = {}, {}
    worker.hook_installed.connect(lambda hook_id, info: installed.update({hook_id: info}))
    worker.hook_failed.connect(lambda hook_id, message: failed.update({hook_id: message}))
    try:
        trap_pcs = {
            "small": _trap_pc(target, "small", 2),
            "byte": _trap_pc(target, "byte", 1),
        }
        assert target.send("small", 0) == (101, 11)

        worker.start()
        ids = {
            worker.add_hook(HookSpec(
                target.addresses["small"], "int16", MultiplyTransform(10),
                trap_pc=trap_pcs["small"],
            )),
            worker.add_hook(HookSpec(
                target.addresses["byte"], "uint8", MultiplyTransform(100),
                trap_pc=trap_pcs["byte"],
            )),
        }
        _wait_for(qt_app, lambda: ids <= set(installed) | set(failed))
        assert not failed

        assert target.send("small", 5) == (151, 11)
        # 11 + 3 * 100 does not fit into a uint8 and is clamped to 255.
        assert target.send("byte", 3) == (151, 255)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
