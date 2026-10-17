"""Tests for reading process information from /proc."""

import os
import subprocess

from hermes.core.process import process_start_time


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
