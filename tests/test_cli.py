"""grembyte-cli: starting a game and installing what is saved for it."""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from grembyte import cli
from tests.test_hooks import requires_target


def test_split_arguments_takes_options_before_the_command():
    assert cli.split_arguments(["--delay", "2", "/game", "--delay", "9"]) == (
        2.0, False, ["/game", "--delay", "9"])
    assert cli.split_arguments(["--delay=0.5", "--", "-game"]) == (0.5, False, ["-game"])
    assert cli.split_arguments(["-h"]) == (cli.DEFAULT_DELAY_S, True, [])
    # Whatever is typed, the command is kept and the delay stays sane.
    assert cli.split_arguments(["--delay", "soon", "/game"]) == (
        cli.DEFAULT_DELAY_S, False, ["/game"])
    assert cli.split_arguments(["--delay", "-3", "/game"]) == (
        cli.DEFAULT_DELAY_S, False, ["/game"])


def test_clean_environment_drops_steams_loader_settings():
    environ = {"LD_PRELOAD": "gameoverlayrenderer.so", "LD_LIBRARY_PATH": "/runtime",
               "SteamAppId": "123", "HOME": "/home/me"}
    assert cli.needs_clean_environment(environ)
    assert not cli.needs_clean_environment({"LD_PRELOAD": "", "HOME": "/home/me"})
    assert cli.clean_environment(environ, 42) == {
        "SteamAppId": "123", "HOME": "/home/me", cli.CHILD_ENV: "42"}


def test_exit_code_follows_the_shell():
    assert cli.exit_code(7 << 8) == 7
    assert cli.exit_code(15) == 128 + 15  # killed by SIGTERM


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_launched_processes_are_found_by_their_environment():
    pid = cli.start_command(["sh", "-c", "sleep 30 & wait"])
    try:
        deadline = time.monotonic() + 5
        found = []
        while time.monotonic() < deadline and len(found) < 2:
            found = cli.launched_pids(os.getpid())
            time.sleep(0.05)
        # The shell and the sleep it started, which could outlive it
        assert pid in found and len(found) == 2
        assert set(found) <= {pid, *cli.descendant_pids(os.getpid())}
    finally:
        subprocess.run(["pkill", "-P", str(pid)], check=False)
        os.kill(pid, 15)
        os.waitpid(pid, 0)


def test_anti_cheat_is_found_up_to_the_game_folder(tmp_path):
    game = tmp_path / "steamapps" / "common" / "Game"
    binaries = game / "Binaries" / "Win64"
    binaries.mkdir(parents=True)
    proc = tmp_path / "proc" / "1"
    proc.mkdir(parents=True)
    os.symlink(binaries, proc / "cwd")
    assert cli.anti_cheat(1, proc_root=tmp_path / "proc") is None
    (game / "EasyAntiCheat").mkdir()
    assert cli.anti_cheat(1, proc_root=tmp_path / "proc") == str(game / "EasyAntiCheat")
    # Not beyond the game's own folder
    shutil.rmtree(game / "EasyAntiCheat")
    (tmp_path / "steamapps" / "common" / "BattlEye").mkdir()
    assert cli.anti_cheat(1, proc_root=tmp_path / "proc") is None


def test_without_a_command_it_shows_how_to_use_it(capsys):
    assert cli.main([]) == 2
    assert "%command%" in capsys.readouterr().err


def test_the_window_finds_the_grembyte_cli_of_a_game(tmp_path):
    from grembyte.core import cli_link  # pylint: disable=import-outside-toplevel

    proc = tmp_path / "proc"
    folder = tmp_path / "run"
    folder.mkdir()

    def process(pid, cmdline, environ=""):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
        (proc / str(pid) / "environ").write_bytes(environ.replace(" ", "\0").encode())

    process(10, "/usr/bin/python3 -m grembyte.cli --delay 5")
    process(20, "/venv/bin/python /venv/bin/grembyte-cli /game")
    process(11, "/game", f"{cli_link.LAUNCHER_ENV}=10 HOME=/home/me")
    process(12, "/launcher", f"{cli_link.LAUNCHER_ENV}=10")
    process(21, "/other", f"{cli_link.LAUNCHER_ENV}=20")
    cli_link.write_state({"pid": 10, "game_pid": 11, "state": "attached"}, folder)
    cli_link.write_state({"pid": 20, "game_pid": None, "state": "searching"}, folder)
    # Left by a grembyte-cli that was killed; its pid now runs something else
    process(30, "/usr/bin/bash")
    cli_link.write_state({"pid": 30, "game_pid": 31, "state": "attached"}, folder)

    def find(pid):
        state = cli_link.find_cli(pid, folder, proc)
        return state and state["pid"]

    assert find(11) == 10
    # Another process of a launch whose CLI found its game, and a CLI that is gone
    assert find(12) is None and find(31) is None
    # Before its CLI has found the game
    assert find(21) == 20
    assert set(cli_link.cli_games(folder, proc)) == {11}


# --- integration: grembyte-cli starts a game with a saved hook -------------

_GAME_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

typedef struct { int32_t gold; } Character;
Character g_player = { 0 };

volatile int g_events;

/* g_events gives the hook room for its jump after the write. */
__attribute__((noinline)) void add_gold(Character *c, int32_t amount) {
    c->gold += amount;
    g_events++;
}

int main(int argc, char **argv) {
    /* Earns 1 gold every 20 ms; the saved hook makes it 100. */
    int goal = argc > 1 ? atoi(argv[1]) : 1000;
    for (int i = 0; i < 1500; i++) {
        add_gold(&g_player, 1);
        if (g_player.gold >= goal) {
            printf("rich %d\n", g_player.gold);
            return 3;
        }
        usleep(20000);
    }
    printf("poor %d\n", g_player.gold);
    return 4;
}
"""


def _save_profile(database, binary):
    # pylint: disable=import-outside-toplevel
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from grembyte.db.engine import init_db
    from grembyte.db.repository.hooks import HookRepository
    from grembyte.hooks.transforms import MultiplyTransform

    listing = subprocess.run(["nm", str(binary)], check=True, capture_output=True,
                             text=True).stdout
    offset = next(int(line.split()[0], 16) for line in listing.splitlines()
                  if line.split()[-1] == "add_gold")
    engine = create_engine(f"sqlite:///{database}")
    init_db(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    # Named as the process list names a native program: its name and path
    process_id = repository.get_or_create_process(binary.name, os.path.realpath(binary))
    with open(binary, "rb") as program:
        program.seek(offset)
        code = program.read(2).hex()
    assert code == "0137", "add_gold should start with add [rdi], esi"
    repository.add_hook(process_id, label="Gold", module=binary.name, module_offset=offset,
                        instruction_bytes=code, instruction_text="add dword ptr [rdi], esi",
                        value_type="int32", transform=MultiplyTransform(100))
    engine.dispose()


@requires_target
def test_grembyte_cli_installs_the_saved_hooks_of_the_game_it_starts(tmp_path):
    binary = _build_game(tmp_path)
    database = tmp_path / "grembyte.db"
    _save_profile(database, binary)

    env = dict(_cli_env(tmp_path, database),
               # As Steam sets it: grembyte-cli executes itself anew without it.
               LD_LIBRARY_PATH=str(tmp_path / "steam-runtime"))
    finished = subprocess.run(
        [sys.executable, "-m", "grembyte.cli", "--delay", "0.2", str(binary)],
        env=env, capture_output=True, text=True, timeout=60, check=False,
        cwd=Path(__file__).resolve().parents[1])
    assert finished.returncode == 3, finished.stderr
    assert finished.stdout.startswith("rich "), finished.stdout
    assert 'Hook "Gold" is active.' in finished.stderr
    assert "Attaching to game" in finished.stderr


def _build_game(tmp_path):
    (tmp_path / "game.c").write_text(_GAME_SOURCE)
    binary = tmp_path / "game"
    subprocess.run(["gcc", "-O2", "-fPIE", "-pie", str(tmp_path / "game.c"), "-o", str(binary)],
                   check=True)
    return binary


def _cli_env(tmp_path, database):
    return dict(os.environ, GREMBYTE_DB=str(database), GREMBYTE_LOG_DIR=str(tmp_path / "logs"),
                GREMBYTE_LANGUAGE="en", XDG_RUNTIME_DIR=str(tmp_path / "run"))


@requires_target
def test_the_window_takes_the_game_over_and_gives_it_back(tmp_path):
    from grembyte.core import cli_link  # pylint: disable=import-outside-toplevel
    from grembyte.platform import frida_agents  # pylint: disable=import-outside-toplevel

    binary = _build_game(tmp_path)
    database = tmp_path / "grembyte.db"
    _save_profile(database, binary)
    folder = tmp_path / "run" / "grembyte"
    lines = []
    with subprocess.Popen(
            [sys.executable, "-m", "grembyte.cli", "--delay", "0.2", str(binary), "999999999"],
            env=_cli_env(tmp_path, database), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True,
            cwd=Path(__file__).resolve().parents[1]) as launch:
        reader = threading.Thread(target=lambda: lines.extend(launch.stderr), daemon=True)
        reader.start()

        def wait_for(text, count=1):
            deadline = time.monotonic() + 30
            while sum(text in line for line in lines) < count:
                assert time.monotonic() < deadline and launch.poll() is None, "".join(lines)
                time.sleep(0.05)

        try:
            wait_for('Hook "Gold" is active.')
            (state,) = cli_link.running_clis(folder)
            game_pid = state["game_pid"]
            assert state["state"] == cli_link.STATE_ATTACHED
            assert cli_link.find_cli(game_pid, folder) == state
            assert set(cli_link.cli_games(folder)) == {game_pid}

            claim = cli_link.claim(state, game_pid, folder=folder)
            # Its agent is gone: the window may attach on its own.
            assert not frida_agents(game_pid)
            assert cli_link.find_cli(game_pid, folder)["state"] == cli_link.STATE_RELEASED

            claim.release()
            wait_for('Hook "Gold" is active.', count=2)
            assert cli_link.find_cli(game_pid, folder)["state"] == cli_link.STATE_ATTACHED
        finally:
            launch.terminate()  # passed on to the game, as Steam ends it
            code = launch.wait(timeout=30)
            reader.join(timeout=5)
    assert code == 128 + 15, "".join(lines)
    # grembyte-cli cleans up after itself.
    assert not list(folder.iterdir())


def test_scripts_that_run_by_time_or_by_a_hotkey_are_started():
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel

    rows = [SimpleNamespace(mode=mode, hotkey=hotkey) for mode, hotkey in (
        ("manual", None), ("repeat", None), ("once", None), ("manual", "Ctrl+G"))]
    assert [(row.mode, row.hotkey) for row in cli.cli_scripts(rows)] == [
        ("repeat", None), ("once", None), ("manual", "Ctrl+G")]


def test_grembyte_cli_runs_manual_scripts_by_their_hotkeys(monkeypatch, capsys):
    # pylint: disable=import-outside-toplevel,protected-access
    import itertools
    from types import SimpleNamespace

    from PySide6.QtCore import QObject, Signal  # pylint: disable=import-error,no-name-in-module

    from grembyte.core import messages
    from grembyte.features import scripts
    from grembyte.platform import keyboard as keyboard_module
    from grembyte.ui.localization import load_translations
    from tests.test_hotkeys import FakeKeyboard

    messages.use(load_translations("en"))

    class Signals(QObject):  # pylint: disable=too-few-public-methods
        worker_failed = Signal(str)
        script_added = Signal(int)
        script_failed = Signal(int, str)
        script_ran = Signal(int, object)

    class FakeWorker:
        ids = itertools.count(1)

        def __init__(self, _pid, language="en"):
            self.signals = Signals()
            for name in ("worker_failed", "script_added", "script_failed", "script_ran"):
                setattr(self, name, getattr(self.signals, name))
            self.runs = []

        def start(self):
            pass

        def add_script(self, _spec):
            return next(self.ids)

        def run_now(self, script_id, args=None):
            self.runs.append((script_id, args))

        def requestInterruption(self):  # pylint: disable=invalid-name
            pass

        def wait(self, _ms):
            pass

        def isRunning(self):  # pylint: disable=invalid-name
            return False

    keyboard = FakeKeyboard(focused=4242)
    monkeypatch.setattr(scripts, "ScriptWorker", FakeWorker)
    monkeypatch.setattr(keyboard_module, "open_reader", lambda: keyboard)

    def row(row_id, label, mode, hotkey=None, enabled=True):
        return SimpleNamespace(id=row_id, label=label, source="return 1;", mode=mode,
                               seconds=5.0, enabled=enabled, params=None, hotkey=hotkey)

    rows = [row(1, "Gold", "manual", "Ctrl+G"), row(2, "Heal", "manual"),
            row(3, "Paused", "manual", "F9", enabled=False), row(4, "Tick", "repeat")]
    repository = SimpleNamespace(list_scripts=lambda _process_id: rows)
    game = cli.AttachedGame(4242, "PillarsOfEternity", 1, None, repository)
    game._start("scripts")
    try:
        worker = game.workers["scripts"]
        assert game.hotkeys is not None and game.hotkeyed == {1: "Ctrl+G"}
        game.hotkeys.poll()
        keyboard.down = {"ctrl", "G"}
        game.hotkeys.poll()
        assert worker.runs == [(1, None)]
        worker.script_ran.emit(1, {"result": "Gold +1000", "error": None})
        worker.script_added.emit(1)
    finally:
        game.stop()
    assert keyboard.closed
    output = capsys.readouterr().err
    assert 'Ctrl+G: running script "Gold".' in output
    assert 'Script "Gold": Gold +1000' in output
    assert 'Script "Gold" runs on Ctrl+G.' in output
