"""Tests for writing frozen address-list values back to a process."""

import shutil
import subprocess
import time

import pytest

from hermes.core import freeze
from hermes.core.freeze import FreezeWorker, FrozenValue
from hermes.core.pointer import PointerChain
from hermes.core.value_types import value_type


class FakeMemory:
    """Seekable, writable memory image addressed by virtual address."""

    def __init__(self, base_address, size, pointers=None):
        self.base_address = base_address
        self.contents = bytearray(size)
        self.position = 0
        self.pointers = pointers or {}

    def write(self, address, data):
        if address < self.base_address:
            raise OSError("bad address")
        offset = address - self.base_address
        self.contents[offset:offset + len(data)] = data

    def read_pointer(self, address, _size=8):
        return self.pointers.get(address)

    def read(self, address, size):
        offset = address - self.base_address
        return bytes(self.contents[offset:offset + size])


def test_write_once_writes_fixed_and_pointer_entries(monkeypatch):
    memory = FakeMemory(0x1000, 0x40, pointers={0x5000 + 0x10: 0x1020})
    monkeypatch.setattr(freeze, "read_mappings", lambda _pid: [])
    monkeypatch.setattr(freeze, "module_bases", lambda _mappings: {"game": 0x5000})
    worker = FreezeWorker(123)
    worker.set_entry(1, FrozenValue(value_type("int16").pack(-2), address=0x1004))
    worker.set_entry(2, FrozenValue(
        value_type("uint8").pack(200), pointer=PointerChain("game", 0x10, (0x8,)),
    ))

    worker.write_once(memory)

    assert memory.read(0x1004, 2) == value_type("int16").pack(-2)
    assert memory.read(0x1028, 1) == bytes([200])


def test_write_once_skips_unresolved_pointers_and_removed_entries(monkeypatch):
    memory = FakeMemory(0x1000, 0x10)
    monkeypatch.setattr(freeze, "read_mappings", lambda _pid: [])
    monkeypatch.setattr(freeze, "module_bases", lambda _mappings: {})
    worker = FreezeWorker(123)
    worker.set_entry(1, FrozenValue(b"\x01", pointer=PointerChain("game", 0x10, ())))
    worker.set_entry(2, FrozenValue(b"\x02", address=0x1000))
    worker.remove_entry(2)

    worker.write_once(memory)

    assert memory.contents == bytearray(0x10)


def test_failing_entry_is_reported_once():
    memory = FakeMemory(0x1000, 0x10)
    worker = FreezeWorker(123)
    messages = []
    worker.write_failed.connect(messages.append)
    worker.set_entry(1, FrozenValue(b"\x01", address=0x10))

    worker.write_once(memory)
    worker.write_once(memory)
    assert len(messages) == 1 and "0x10" in messages[0]

    # Changing the entry reports again.
    worker.set_entry(1, FrozenValue(b"\x02", address=0x10))
    worker.write_once(memory)
    assert len(messages) == 2


def test_run_reports_a_missing_process(monkeypatch):
    def fail(*_args, **_kwargs):
        raise FileNotFoundError("no such process")

    monkeypatch.setattr(freeze, "ProcessMemory", fail)
    worker = FreezeWorker(123)
    messages = []
    worker.write_failed.connect(messages.append)

    worker.run()

    assert messages == ["no such process"]


_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

volatile int32_t g_gold = 100;

int main(void) {
    int value;
    printf("%d %p\n", getpid(), (void *)&g_gold);
    fflush(stdout);
    while (scanf("%d", &value) == 1) {
        g_gold += value;
        printf("%d\n", g_gold);
        fflush(stdout);
    }
    return 0;
}
"""


@pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
def test_frozen_value_survives_writes_of_a_real_process(tmp_path):
    source = tmp_path / "target.c"
    source.write_text(_TARGET_SOURCE)
    binary = tmp_path / "target"
    subprocess.run(["gcc", "-O2", str(source), "-o", str(binary)], check=True)
    process = subprocess.Popen(
        [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    worker = FreezeWorker(0)
    try:
        pid, address = process.stdout.readline().split()
        worker.pid = int(pid)
        worker.set_entry(1, FrozenValue(value_type("int32").pack(5000), address=int(address, 16)))
        worker.start()
        time.sleep(0.3)

        process.stdin.write("1\n")
        process.stdin.flush()
        # The frozen 5000 was in place when the game added 1.
        assert process.stdout.readline().strip() == "5001"
        time.sleep(0.3)
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            memory.seek(int(address, 16))
            assert value_type("int32").unpack(memory.read(4)) == 5000
    finally:
        worker.requestInterruption()
        worker.wait(2000)
        process.kill()
        process.wait()
