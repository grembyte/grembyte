"""Tests for running the Windows injection helper in a game's wineserver."""

import os

import pytest

from grembyte.attach import wine_helper
from grembyte.attach.wine_helper import (
    HelperError,
    WineProcess,
    helper_environment,
    match_process,
    parse_list,
    read_environ,
    server_socket,
    sibling_start_times,
    wine_loader,
)
from tests.test_hooks import linux_only

# Wine and Proton run on Linux only.
pytestmark = linux_only


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _fake_process(root, pid, exe, environ=b""):
    (root / str(pid)).mkdir()
    (root / str(pid) / "exe").symlink_to(exe)
    (root / str(pid) / "environ").write_bytes(environ)


def test_helpers_are_found_in_the_build_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMBYTE_INJECT_DIR", str(tmp_path))
    _executable(tmp_path / "grembyte-inject-x86_64.exe")
    _executable(tmp_path / "grembyte-inject-x86.exe")
    assert wine_helper.helper_path(64) == tmp_path / "grembyte-inject-x86_64.exe"
    assert wine_helper.helper_path(32) == tmp_path / "grembyte-inject-x86.exe"
    monkeypatch.delenv("GREMBYTE_INJECT_DIR")
    assert wine_helper.helper_dir() == wine_helper.BUILD_DIR
    assert wine_helper.BUILD_DIR.parts[-2:] == ("build", "inject")


def test_a_missing_helper_is_reported_with_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GREMBYTE_INJECT_DIR", str(tmp_path))
    with pytest.raises(HelperError, match="build.sh"):
        wine_helper.helper_path(64, build=False)


def test_the_build_uses_grembytes_python_and_absolute_paths(tmp_path, monkeypatch):
    # pylint: disable=import-outside-toplevel
    import subprocess
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GREMBYTE_INJECT_DIR", "helpers")
    assert wine_helper.helper_dir() == tmp_path.resolve() / "helpers"
    calls = []
    monkeypatch.setattr(wine_helper, "_build_tried", False)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(kwargs)
                        or subprocess.CompletedProcess(command, 0, "", ""))
    assert wine_helper.build_helpers("helpers")
    env = calls[0]["env"]
    assert env["PYTHON"] == sys.executable
    assert env["OUT_DIR"] == str(tmp_path.resolve() / "helpers")


def test_build_sh_builds_both_helpers(tmp_path):
    # pylint: disable=import-outside-toplevel
    import shutil
    import subprocess

    if not (shutil.which("x86_64-w64-mingw32-gcc") and shutil.which("i686-w64-mingw32-gcc")):
        pytest.skip("needs MinGW (mingw64-gcc, mingw32-gcc)")
    subprocess.run(["sh", str(wine_helper.HELPER_DIR / "build.sh")], check=True,
                   env={**os.environ, "OUT_DIR": str(tmp_path)}, capture_output=True)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "grembyte-inject-x86.exe", "grembyte-inject-x86_64.exe"]
    assert (tmp_path / "grembyte-inject-x86_64.exe").read_bytes()[:2] == b"MZ"


def test_parses_list_output_with_crlf_and_skips_noise():
    output = ("48\t1792596163981\tservices.exe\r\n"
              "wine: some warning\r\n"
              "420\t1792596168917\tPing.exe\r\n")

    assert parse_list(output) == [
        WineProcess(48, 1792596163981, "services.exe"),
        WineProcess(420, 1792596168917, "Ping.exe"),
    ]


def test_matches_game_by_name_then_closest_creation_time():
    processes = [
        WineProcess(420, 1000, "ping.exe"),
        WineProcess(836, 50_000, "PING.EXE"),
        WineProcess(964, 90_045, "ping.exe"),
        WineProcess(12, 90_000, "start.exe"),
    ]

    assert match_process(processes, "ping.exe", 90_000).pid == 964
    assert match_process(processes, "ping.exe", 49_000).pid == 836
    assert match_process(processes[3:], "start.exe", None).pid == 12
    with pytest.raises(HelperError):
        match_process(processes, "game.exe", 0)


def test_same_named_processes_match_in_the_order_they_started():
    # Gothic 1 Remake: the bootstrap and the game it starts share a name, and
    # Wine registered both 0.8 s after their Unix start.
    processes = [WineProcess(320, 86_554, "G1R-Win64-Shipping.exe"),
                 WineProcess(340, 86_724, "G1R-Win64-Shipping.exe")]
    siblings = [85_920, 85_790]

    assert match_process(processes, "g1r-win64-shipping.exe", 85_920,
                         siblings_ms=siblings).pid == 340
    assert match_process(processes, "g1r-win64-shipping.exe", 85_790,
                         siblings_ms=siblings).pid == 320
    # Without the siblings, closeness alone picks the bootstrap.
    assert match_process(processes, "g1r-win64-shipping.exe", 85_920).pid == 320


@linux_only
def test_finds_the_starts_of_same_named_processes_in_the_prefix(tmp_path):
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    ticks = os.sysconf("SC_CLK_TCK")
    programs = {
        41: (r"S:\Gothic\G1R-Win64-Shipping.exe", "/pfx", 5),
        42: (r"S:\Gothic\G1R\Binaries\Win64\G1R-Win64-Shipping.exe", "/pfx", 7),
        43: (r"S:\Gothic\G1R-Win64-Shipping.exe", "/other", 9),  # another prefix
        44: (r"S:\Gothic\Launcher.exe", "/pfx", 11),
    }
    for pid, (program, prefix, start) in programs.items():
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "cmdline").write_bytes(program.encode() + b"\0G1R\0")
        (tmp_path / str(pid) / "environ").write_bytes(f"WINEPREFIX={prefix}\0".encode())
        fields = ["S"] + ["0"] * 18 + [str(start * ticks), "0"]
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (x) {' '.join(fields)}\n")

    starts = sibling_start_times(42, "g1r-win64-shipping.exe", proc_root=str(tmp_path))
    assert sorted(starts) == [1_005_000, 1_007_000]


def test_reads_architecture_and_tells_same_named_programs_apart():
    processes = parse_list("40\t90000\tping.exe\t32\r\n52\t90003\tping.exe\t64\r\n"
                           "60\t1\tsvc.exe\t0\r\n")

    assert processes[0] == WineProcess(40, 90000, "ping.exe", 32)
    assert match_process(processes, "ping.exe", 90000, bits=64).pid == 52
    assert match_process(processes, "ping.exe", 90003, bits=32).pid == 40
    # Unknown architecture (0) never rules a process out.
    assert match_process(processes, "svc.exe", 1, bits=64).pid == 60


def test_environment_copies_prefix_and_sync_but_not_wine_internals(tmp_path):
    _fake_process(tmp_path, 7, "/x/wine64-preloader",
                  b"WINEPREFIX=/games/pfx\0WINEFSYNC=1\0WINEESYNC=1\0"
                  b"WINESERVERSOCKET=16\0WINEPRELOADRESERVE=140000000-14000a000\0"
                  b"WINEDLLPATH=/container/only\0LD_LIBRARY_PATH=/container\0")
    base = {"PATH": "/usr/bin", "WINEPREFIX": "/mine", "WINEDEBUG": "+all"}

    env = helper_environment(read_environ(7, tmp_path), base)

    assert env == {"PATH": "/usr/bin", "WINEPREFIX": "/games/pfx", "WINEFSYNC": "1",
                   "WINEESYNC": "1", "WINEDEBUG": "-all"}


def test_default_prefix_is_home_wine():
    env = helper_environment({"HOME": "/home/player"}, {})

    assert env["WINEPREFIX"] == "/home/player/.wine"


def test_server_socket_is_named_after_prefix_device_and_inode(tmp_path):
    info = os.stat(tmp_path)

    socket = server_socket(tmp_path, uid=1000)

    assert str(socket) == (
        f"/tmp/.wine-1000/server-{info.st_dev:x}-{info.st_ino:x}/socket")


def test_finds_proton_wine_above_the_preloader(tmp_path):
    files = tmp_path / "Proton" / "files"
    wine = _executable(files / "bin" / "wine")
    preloader = _executable(files / "lib" / "wine" / "x86_64-unix" / "wine64-preloader")
    _fake_process(tmp_path, 7, preloader)

    assert wine_loader(7, {}, tmp_path) == wine


def test_finds_system_wine_beside_the_preloader(tmp_path):
    wine = _executable(tmp_path / "usr" / "bin" / "wine")
    preloader = _executable(tmp_path / "usr" / "bin" / "wine64-preloader")
    _fake_process(tmp_path, 7, preloader)

    assert wine_loader(7, {}, tmp_path) == wine


def test_prefers_the_game_wineloader(tmp_path):
    loader = _executable(tmp_path / "custom" / "wine")
    _executable(tmp_path / "usr" / "bin" / "wine")
    preloader = _executable(tmp_path / "usr" / "bin" / "wine64-preloader")
    _fake_process(tmp_path, 7, preloader)

    assert wine_loader(7, {"WINELOADER": str(loader)}, tmp_path) == loader


def test_no_wine_found_raises(tmp_path):
    preloader = _executable(tmp_path / "a" / "b" / "c" / "d" / "e" / "wine64-preloader")
    _fake_process(tmp_path, 7, preloader)

    with pytest.raises(HelperError):
        wine_loader(7, {}, tmp_path)
