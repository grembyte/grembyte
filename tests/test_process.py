"""Tests for reading process information from /proc."""

import os
import subprocess

from hermes.platform.linux import is_wine_process, process_start_time, program_bits
from tests.test_hooks import linux_only


def _write_stat(root, pid, name, state, start_time):
    fields = [state] + ["0"] * 18 + [str(start_time), "0"]
    (root / str(pid)).mkdir()
    (root / str(pid) / "stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")


def test_reads_start_time_after_names_with_spaces_and_parentheses(tmp_path):
    _write_stat(tmp_path, 42, "my game) (x", "S", 123456)

    assert process_start_time(42, tmp_path) == 123456


def test_zombies_and_missing_processes_count_as_exited(tmp_path):
    _write_stat(tmp_path, 42, "game", "Z", 1)

    assert process_start_time(42, tmp_path) is None
    assert process_start_time(43, tmp_path) is None


@linux_only
def test_running_and_exited_real_process():
    assert process_start_time(os.getpid()) is not None

    child = subprocess.Popen(["sleep", "10"])
    try:
        start_time = process_start_time(child.pid)
        assert start_time is not None
        assert process_start_time(child.pid) == start_time
    finally:
        child.kill()
        child.wait()
    assert process_start_time(child.pid) is None


def _link_exe(root, pid, target):
    (root / str(pid)).mkdir()
    (root / str(pid) / "exe").symlink_to(target)


@linux_only
def test_detects_wine_and_proton_loaders(tmp_path):
    proton = "/steam/GE-Proton11-7/files/lib/wine"
    _link_exe(tmp_path, 1, f"{proton}/i386-unix/wine-preloader")
    _link_exe(tmp_path, 2, f"{proton}/x86_64-unix/wine64-preloader")
    _link_exe(tmp_path, 3, "/usr/bin/wine64")
    _link_exe(tmp_path, 4, "/games/PillarsOfEternity")
    _link_exe(tmp_path, 5, "/usr/bin/winecfg")

    assert is_wine_process(1, tmp_path)
    assert is_wine_process(2, tmp_path)
    assert is_wine_process(3, tmp_path)
    assert not is_wine_process(4, tmp_path)
    assert not is_wine_process(5, tmp_path)
    assert not is_wine_process(6, tmp_path)
    assert not is_wine_process(os.getpid())


@linux_only
def test_reads_the_bitness_of_the_program(tmp_path):
    for pid, header in ((1, b"\x7fELF\x01"), (2, b"\x7fELF\x02"), (3, b"MZ\x90\x00\x03")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "exe").write_bytes(header + b"\0" * 59)

    assert program_bits(1, tmp_path) == 32
    assert program_bits(2, tmp_path) == 64
    assert program_bits(3, tmp_path) is None
    assert program_bits(4, tmp_path) is None
    assert program_bits(os.getpid()) == 64
