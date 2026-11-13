"""The link between the window and a running grembyte-cli.

A grembyte-cli publishes what it does in a small state file and listens on a
Unix socket, both in Grembyte's runtime folder and named after its pid::

    $XDG_RUNTIME_DIR/grembyte/cli-<pid>.json   {"pid", "game_pid", "name", "state"}
    $XDG_RUNTIME_DIR/grembyte/cli-<pid>.sock

The window finds the grembyte-cli that runs a game from the state files (and,
before it has found its game, from ``GREMBYTE_LAUNCHER`` in the game's
environment). To take the game over it connects and sends ``claim``: the CLI
removes its hooks, stops its workers and answers once the game is free. The
connection is the window's claim. While it is open the CLI leaves the game
alone; once it closes (the window detached, was closed or crashed) the CLI
attaches again.
"""

import getpass
import json
import os
import socket
import tempfile

# The environment variable that marks the processes of one launch
LAUNCHER_ENV = "GREMBYTE_LAUNCHER"
CLAIM = b"claim\n"
# How long the window waits for the CLI to free the game
CLAIM_TIMEOUT_S = 20.0

STATE_SEARCHING = "searching"
STATE_WAITING = "waiting"
STATE_ATTACHED = "attached"
STATE_RELEASED = "released"


def runtime_dir():
    """Grembyte's folder for files that live as long as a session (created 0700)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    folder = (os.path.join(base, "grembyte") if base
              else os.path.join(tempfile.gettempdir(), f"grembyte-{getpass.getuser()}"))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder


def state_path(cli_pid, folder=None):
    return os.path.join(folder or runtime_dir(), f"cli-{cli_pid}.json")


def socket_path(cli_pid, folder=None):
    return os.path.join(folder or runtime_dir(), f"cli-{cli_pid}.sock")


def write_state(state, folder=None):
    """Publish the state of this grembyte-cli (atomically: readers never see half)."""
    path = state_path(state["pid"], folder)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file)
    os.replace(temporary, path)


def remove_files(cli_pid, folder=None):
    for path in (state_path(cli_pid, folder), socket_path(cli_pid, folder)):
        try:
            os.remove(path)
        except OSError:
            pass


def is_cli_process(pid, proc_root="/proc"):
    """Whether ``pid`` runs grembyte-cli (the script or ``python -m grembyte.cli``)."""
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as file:
            words = file.read().decode(errors="replace").split("\0")
    except OSError:
        return False
    return any(word == "grembyte.cli" or os.path.basename(word) == "grembyte-cli"
               for word in words[:4])


def running_clis(folder=None, proc_root="/proc"):
    """The states of the grembyte-clis that run; a file a killed one left is skipped."""
    try:
        folder = folder or runtime_dir()
        names = os.listdir(folder)
    except OSError:
        return []
    states = []
    for name in names:
        if not (name.startswith("cli-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as file:
                state = json.load(file)
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and is_cli_process(state.get("pid"), proc_root):
            states.append(state)
    return states


def cli_games(folder=None, proc_root="/proc"):
    """``{game pid: state}`` of the games grembyte-clis run."""
    return {state["game_pid"]: state for state in running_clis(folder, proc_root)
            if state.get("game_pid")}


def launcher_of(pid, proc_root="/proc"):
    """The pid of the grembyte-cli whose launch started ``pid``, or None."""
    prefix = f"{LAUNCHER_ENV}=".encode()
    try:
        with open(f"{proc_root}/{pid}/environ", "rb") as file:
            variables = file.read().split(b"\0")
    except OSError:
        return None
    for variable in variables:
        if variable.startswith(prefix):
            try:
                return int(variable[len(prefix):])
            except ValueError:
                return None
    return None


def find_cli(pid, folder=None, proc_root="/proc"):
    """The state of the grembyte-cli that runs (or will attach to) ``pid``, or None.

    A process of a launch whose CLI found another game is none of its business.
    """
    states = running_clis(folder, proc_root)
    for state in states:
        if state.get("game_pid") == pid:
            return state
    launcher = launcher_of(pid, proc_root)
    for state in states:
        if state.get("pid") == launcher and not state.get("game_pid"):
            return state
    return None


class CliClaim:
    """The window's hold on a game taken over from a grembyte-cli.

    The CLI attaches again once this is released or the window's process ends.
    """

    def __init__(self, connection, cli_pid, game_pid):
        self._connection = connection
        self.cli_pid = cli_pid
        self.game_pid = game_pid

    def release(self):
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None


def claim(state, game_pid, folder=None, timeout=CLAIM_TIMEOUT_S):
    """Ask the grembyte-cli of ``state`` to free ``game_pid``; returns a CliClaim.

    Blocks until the CLI has removed its hooks; raises LocalizedOSError if it
    does not answer or cannot free the game.
    """
    # Not at the top: grembyte-cli imports this module before the game runs.
    from grembyte.core.messages import LocalizedOSError  # pylint: disable=import-outside-toplevel
    cli_pid = state["pid"]
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(socket_path(cli_pid, folder))
        connection.sendall(CLAIM)
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            reply += chunk
        answer = json.loads(reply or b"{}")
    except (OSError, ValueError) as error:
        connection.close()
        raise LocalizedOSError("error.cli.no_answer", pid=cli_pid, error=error) from error
    if not answer.get("released"):
        connection.close()
        raise LocalizedOSError("error.cli.not_released", pid=cli_pid)
    connection.settimeout(None)
    return CliClaim(connection, cli_pid, game_pid)
