"""grembyte-cli: start a game with the hooks and scripts saved for it.

Used as a Steam launch option, in front of ``%command%``::

    /path/to/grembyte-cli %command%

It starts the command, finds the game among the processes the command
starts and installs the hooks, method hooks and scripts the window saved for
that program (see ``grembyte.db.models.Process``). It needs no window: the
game is set up once in Grembyte, then played through the launch option.
Manual scripts with a hotkey run when it is pressed while the game has the
focus (grembyte.core.hotkeys); other manual scripts are left out.

Finding the game
    Every process of the launch carries ``GREMBYTE_LAUNCHER=<pid>`` in its
    environment, so it is found even when a launcher in between has exited
    and it was handed to Steam's reaper. The descendants of grembyte-cli
    count too. Each is named as the process list names it
    (``grembyte.core.programs.resolve_display``); the first one with saved
    hooks or scripts is the game.

ptrace
    With ``kernel.yama.ptrace_scope`` at 1 a process may only ptrace its
    descendants. Frida injects into a process of the same architecture from
    the calling process itself, so attaching works while the game is a
    descendant of grembyte-cli. A game whose parent chain was cut (a launcher
    that exits) is no descendant: the attach then fails as from the window.

Steam's environment
    Steam starts the launch option with ``LD_PRELOAD`` (its overlay) and,
    for native games, ``LD_LIBRARY_PATH`` (its runtime). The game gets them
    unchanged; grembyte-cli itself executes itself anew without them once
    the game runs (``CHILD_ENV`` names the game's pid), so its Qt and Frida
    do not load Steam's libraries. The game stays its child across the exec.

Anti-cheat
    A game that ships EasyAntiCheat or BattlEye is never attached; it runs
    without Grembyte.

The window
    Grembyte's window can take the game over (``grembyte.core.cli_link``):
    grembyte-cli then removes its hooks and waits, and attaches again once
    the window has let go of the game.

Whatever goes wrong in Grembyte, the game runs on; grembyte-cli waits for
the command and exits with its exit code.
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from grembyte.core import cli_link, programs
from grembyte.core.cli_link import LAUNCHER_ENV
# Set when grembyte-cli executed itself anew: the pid of the started command
CHILD_ENV = "GREMBYTE_CLI_CHILD"
# Steam's settings for the game that grembyte-cli's own process does without
STEAM_LOADER_VARIABLES = ("LD_PRELOAD", "LD_LIBRARY_PATH")
# Files and folders in a game's directory that show an anti-cheat
ANTI_CHEAT_MARKERS = ("easyanticheat", "easyanticheat_eos", "battleye",
                      "beservice.exe", "beservice_x64.exe", "start_protected_game.exe")

# How often the processes of the launch are looked through
POLL_S = 1.0
# How long to wait after the game was found before attaching (its engine loads)
DEFAULT_DELAY_S = 5.0
# A worker that failed is started again this often, this long after
RETRIES = 3
RETRY_S = 10.0
# After a window let go of the game: wait this long before attaching again,
# and at most this long for its Frida agent to unload
RESUME_DELAY_S = 1.0
AGENT_WAIT_S = 10.0


def _translations():
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.localization import load_translations
    return load_translations()


def build_parser(translations):
    """The parser that prints grembyte-cli's help (split_arguments reads them)."""
    def help_text(key):
        return translations[key].replace("%", "%%")  # argparse formats help with %

    parser = argparse.ArgumentParser(
        prog="grembyte-cli", description=translations["cli.description"],
        epilog=translations["cli.epilog"],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_S, metavar="SECONDS",
                        help=help_text("cli.option_delay"))
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help=help_text("cli.option_command"))
    return parser


def say(key, **params):
    """Print a message of grembyte-cli in the user's language, and log it."""
    # pylint: disable=import-outside-toplevel
    from grembyte.core import logs, messages
    text = messages.text(key, **params)
    print(f"grembyte-cli: {text}", file=sys.stderr, flush=True)
    logs.log.info("cli: %s", text)


# ----------------------------------------------------------------------
# Starting the command
# ----------------------------------------------------------------------
def start_command(command, environ=None):
    """Start ``command`` with the launch marked in its environment; returns its pid."""
    import subprocess  # pylint: disable=import-outside-toplevel
    env = dict(os.environ if environ is None else environ)
    env[LAUNCHER_ENV] = str(os.getpid())
    env.pop(CHILD_ENV, None)
    return subprocess.Popen(command, env=env).pid  # pylint: disable=consider-using-with


def needs_clean_environment(environ):
    """Whether Steam's loader settings are in ``environ``."""
    return any(environ.get(name) for name in STEAM_LOADER_VARIABLES)


def clean_environment(environ, child_pid):
    """``environ`` without Steam's loader settings, naming the started command."""
    env = {name: value for name, value in environ.items()
           if name not in STEAM_LOADER_VARIABLES}
    env[CHILD_ENV] = str(child_pid)
    return env


def exit_code(status):
    """The shell's exit code of a wait status: 128 + the signal of a killed process."""
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


# ----------------------------------------------------------------------
# Finding the game
# ----------------------------------------------------------------------
def launched_pids(launcher_pid, proc_root="/proc"):
    """The pids of the processes whose environment marks them as started by
    ``launcher_pid`` (see LAUNCHER_ENV), oldest first."""
    marker = f"{LAUNCHER_ENV}={launcher_pid}".encode()
    found = []
    for entry in os.scandir(proc_root):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"{entry.path}/environ", "rb") as environ:
                variables = environ.read().split(b"\0")
        except OSError:
            continue
        if marker in variables:
            found.append(int(entry.name))
    return sorted(found)


def descendant_pids(pid):
    import psutil  # pylint: disable=import-outside-toplevel
    try:
        return [child.pid for child in psutil.Process(pid).children(recursive=True)]
    except psutil.Error:
        return []


def anti_cheat(pid, proc_root="/proc"):
    """The anti-cheat file or folder found in the game's directory, or None.

    Looks from the working directory of the game (Proton starts it in the
    game's folder) and, for a native game, the folder of its program up to
    the folder below ``steamapps/common``.
    """
    folders = []
    for link in ("cwd", "exe"):
        try:
            path = Path(os.readlink(f"{proc_root}/{pid}/{link}"))
        except OSError:
            continue
        folders.append(path if link == "cwd" else path.parent)
    seen = set()
    for folder in folders:
        for current in (folder, *folder.parents):
            if current in seen or current.parent == current:
                break
            seen.add(current)
            try:
                names = {entry.lower(): entry for entry in os.listdir(current)}
            except OSError:
                break
            for marker in ANTI_CHEAT_MARKERS:
                if marker in names:
                    return str(current / names[marker])
            if current.parent.name.lower() == "common":
                break
    return None


# ----------------------------------------------------------------------
# Running with Qt
# ----------------------------------------------------------------------
def _setup():
    """Logs, database and settings as the window sets them up."""
    # pylint: disable=import-outside-toplevel
    from grembyte.core import activity, logs, messages, settings
    from grembyte.db.engine import init_db
    from grembyte.db.repository.settings import SettingsRepository
    from grembyte.ui.localization import load_translations
    try:
        logs.setup()
    except OSError as error:
        print(f"grembyte-cli: {error}", file=sys.stderr)
    logs.install_exception_hooks()
    init_db()
    settings.load(SettingsRepository().load())
    messages.use(load_translations(settings.language()))
    if settings.get("activity_log"):
        try:
            activity.setup()
        except OSError as error:
            logs.log.error("cannot write the activity log: %s", error)


def run(child_pid, delay):
    """Watch the launch of ``child_pid`` until it ends; returns its exit code."""
    # pylint: disable=import-outside-toplevel
    from PySide6.QtCore import QCoreApplication, QTimer  # pylint: disable=no-name-in-module
    from grembyte.core import logs

    try:
        _setup()
    except Exception as error:  # pylint: disable=broad-exception-caught
        # The game runs on without Grembyte.
        print(f"grembyte-cli: {error}", file=sys.stderr, flush=True)
        return wait_for(child_pid)

    app = QCoreApplication.instance() or QCoreApplication(["grembyte-cli"])
    launch = Launch(child_pid, delay)
    link = WindowLink(launch, app)
    launch.publish()

    def forward(signum, _frame):
        # Steam ends a game by signalling the launch option: pass it on and
        # wait for the game to exit.
        try:
            os.kill(child_pid, signum)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGHUP, forward)
    # Ctrl+C in a terminal reaches the game as well; it decides.
    signal.signal(signal.SIGINT, lambda _signum, _frame: None)

    timer = QTimer(app)
    timer.setInterval(int(POLL_S * 1000))
    timer.timeout.connect(lambda: launch.poll() or app.quit())
    timer.start()
    QTimer.singleShot(0, lambda: launch.poll() or app.quit())
    app.exec()
    timer.stop()
    link.close()
    hanging = launch.stop()
    code = launch.exit_code if launch.exit_code is not None else wait_for(child_pid)
    logs.log.info("grembyte-cli exits (code %s)", code)
    if hanging:
        # A worker hangs in a call to the game: leave without waiting for it.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)  # pylint: disable=protected-access
    return code


def wait_for(child_pid):
    """Wait for the started command; its exit code (0 if it is no child)."""
    while True:
        try:
            _pid, status = os.waitpid(child_pid, 0)
        except InterruptedError:
            continue
        except ChildProcessError:
            return 0
        return exit_code(status)


class Launch:
    """The game of one launch: found, attached, and its workers."""

    def __init__(self, child_pid, delay):
        # pylint: disable=import-outside-toplevel
        from grembyte.db.repository.hooks import HookRepository
        from grembyte.db.repository.scripts import ScriptRepository
        self.child_pid = child_pid
        self.delay = delay
        self.exit_code = None
        self.hook_repository = HookRepository()
        self.script_repository = ScriptRepository()
        self.target = None
        self.released = False
        self._seen = set()
        self._attach_at = None
        self._agents_deadline = None
        self._attached = None

    def publish(self):
        """Write what this grembyte-cli does where the window finds it (cli_link)."""
        state = {"pid": os.getpid(), "game_pid": None, "name": None,
                 "state": cli_link.STATE_SEARCHING}
        if self.target is not None:
            if self.released:
                current = cli_link.STATE_RELEASED
            elif self._attached is not None:
                current = cli_link.STATE_ATTACHED
            else:
                current = cli_link.STATE_WAITING
            state.update(game_pid=self.target[0], name=self.target[1], state=current)
        try:
            cli_link.write_state(state)
        except OSError as error:
            from grembyte.core import logs  # pylint: disable=import-outside-toplevel
            logs.log.warning("grembyte-cli: cannot write its state: %s", error)

    def release(self):
        """A window takes the game over: remove the hooks and leave the game
        alone until resume(). Whether the game is free."""
        self.released = True
        hanging = self.stop()
        self._attached = None
        if self.target is not None:
            say("cli.released", name=self.target[1], pid=self.target[0])
            if not hanging:
                _wait_agents_unloaded(self.target[0], AGENT_WAIT_S)
        self.publish()
        return not hanging

    def resume(self):
        """The window let go of the game: attach again once its agent is gone."""
        self.released = False
        if self.target is not None:
            say("cli.resumed", name=self.target[1], pid=self.target[0])
            now = time.monotonic()
            self._attach_at = now + RESUME_DELAY_S
            self._agents_deadline = now + AGENT_WAIT_S
        self.publish()

    def poll(self):
        """One look at the launch; False once the started command has exited."""
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            self.exit_code = 0
            return False
        if pid:
            self.exit_code = exit_code(status)
            return False
        try:
            self._poll_game()
        except Exception as error:  # pylint: disable=broad-exception-caught
            from grembyte.core import logs  # pylint: disable=import-outside-toplevel
            logs.log.exception("grembyte-cli: %s", error)
        return True

    def _poll_game(self):
        if self.target is not None:
            pid = self.target[0]
            if not _alive(pid):
                say("cli.game_ended", name=self.target[1], pid=pid)
                self.stop()
                self.target = None
                self._attached = None
                self.publish()
                return
            if self.released:
                return
            if self._attached is None and time.monotonic() >= self._attach_at:
                if self._agents_unloaded(pid):
                    self._attach()
            elif self._attached is not None:
                self._attached.retry_failed()
            return
        self._find_game()

    def _agents_unloaded(self, pid):
        """Whether the Frida agent of a window that let go is gone (or waited for long enough).

        A new agent next to a foreign one is refused (attach_alone).
        """
        if self._agents_deadline is None:
            return True
        if time.monotonic() >= self._agents_deadline or not _foreign_agents(pid):
            self._agents_deadline = None
            return True
        return False

    def _find_game(self):
        launcher = os.getpid()
        for pid in dict.fromkeys(launched_pids(launcher) + descendant_pids(launcher)):
            program = programs.program_of(pid)
            # By program too: a process may execute another one (a script or
            # wine becoming the game) and keep its pid.
            if program is None or (pid, program) in self._seen:
                continue
            self._seen.add((pid, program))
            name, path = program
            process_id = self.hook_repository.find_process(name, path)
            # The window notes every program it attached to; only one with
            # something saved is a game to set up.
            if process_id is None or not self._has_profile(process_id):
                from grembyte.core import logs  # pylint: disable=import-outside-toplevel
                logs.log.info("grembyte-cli: no saved profile for %s (%s, pid %s)",
                              name, path, pid)
                continue
            marker = anti_cheat(pid)
            if marker is not None:
                say("cli.anti_cheat", name=name, marker=marker)
                continue
            say("cli.game_found", name=name, pid=pid, seconds=f"{self.delay:g}")
            self.target = (pid, name, path, process_id)
            self._attach_at = time.monotonic() + self.delay
            self.publish()
            return

    def _has_profile(self, process_id):
        return bool(self.hook_repository.list_hooks(process_id)
                    or self.hook_repository.list_method_hooks(process_id)
                    or cli_scripts(self.script_repository.list_scripts(process_id)))

    def _attach(self):
        pid, name, _path, process_id = self.target
        self._attached = AttachedGame(pid, name, process_id, self.hook_repository,
                                      self.script_repository)
        self._attached.start()
        self.publish()

    def stop(self):
        """Stop the workers; True if one of them did not end."""
        if self._attached is None:
            return False
        return self._attached.stop()


def cli_scripts(rows):
    """The saved scripts grembyte-cli runs: by time, or manual ones by their
    hotkey; a manual one without needs the window to run it."""
    return [row for row in rows if row.mode != "manual" or row.hotkey]


def _alive(pid):
    import psutil  # pylint: disable=import-outside-toplevel
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _foreign_agents(pid):
    """The Frida agents loaded in ``pid`` while grembyte-cli is detached from it.

    Not for a Wine game: its gadget stays loaded and is shared by everyone.
    """
    # pylint: disable=import-outside-toplevel
    from grembyte.platform import frida_agents, is_wine_process
    if is_wine_process(pid):
        return set()
    return frida_agents(pid) or set()


def _wait_agents_unloaded(pid, timeout):
    """Wait until grembyte-cli's own agent left ``pid``, so the window may attach."""
    deadline = time.monotonic() + timeout
    while _foreign_agents(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


class WindowLink:
    """The socket through which the window takes the game over (see cli_link).

    Each connection that sent ``claim`` holds the game; the last one to close
    gives it back.
    """

    def __init__(self, launch, parent):
        # pylint: disable=import-outside-toplevel,no-name-in-module
        from PySide6.QtNetwork import QLocalServer
        from grembyte.core import logs
        self.launch = launch
        self.claims = []
        self.path = cli_link.socket_path(os.getpid())
        self.server = QLocalServer(parent)
        QLocalServer.removeServer(self.path)
        if not self.server.listen(self.path):
            logs.log.warning("grembyte-cli: cannot listen on %s: %s", self.path,
                             self.server.errorString())
        self.server.newConnection.connect(self._on_connection)

    def _on_connection(self):
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.readyRead.connect(lambda c=connection: self._on_read(c))
            connection.disconnected.connect(lambda c=connection: self._on_disconnected(c))

    def _on_read(self, connection):
        while connection.canReadLine():
            if bytes(connection.readLine()) != cli_link.CLAIM or connection in self.claims:
                continue
            first = not self.claims
            self.claims.append(connection)
            released = self.launch.release() if first else True
            target = self.launch.target
            answer = {"released": released, "game_pid": target[0] if target else None}
            connection.write(json.dumps(answer).encode() + b"\n")
            connection.flush()

    def _on_disconnected(self, connection):
        if connection in self.claims:
            self.claims.remove(connection)
            if not self.claims:
                self.launch.resume()
        connection.deleteLater()

    def close(self):
        self.server.close()
        cli_link.remove_files(os.getpid())


class AttachedGame:
    """The workers that run the saved hooks, method hooks and scripts of a game."""

    # pylint: disable=too-many-instance-attributes

    def __init__(self, pid, name, process_id, hook_repository, script_repository):
        self.pid = pid
        self.name = name
        self.process_id = process_id
        self.hook_repository = hook_repository
        self.script_repository = script_repository
        # kind -> worker; kind -> how often it was started; kind -> when to retry
        self.workers = {}
        self.starts = {}
        self.retry_at = {}
        # hook id -> saved row, per kind
        self.saved = {"hooks": {}, "method_hooks": {}, "scripts": {}}
        self.backend = None
        # script id -> hotkey of the manual scripts the scripts worker runs
        self.hotkeyed = {}
        self.hotkeys = None

    def start(self):
        # pylint: disable=import-outside-toplevel
        from grembyte.core import messages
        try:
            self.backend = self._prepare()
        except Exception as error:  # pylint: disable=broad-exception-caught
            say("cli.attach_failed", name=self.name, message=messages.describe(error))
            return
        scripts = self.script_repository.list_scripts(self.process_id)
        counts = {"hooks": len(self.hook_repository.list_hooks(self.process_id)),
                  "method_hooks": len(self.hook_repository.list_method_hooks(self.process_id)),
                  "scripts": len(cli_scripts(scripts))}
        say("cli.attaching", name=self.name, pid=self.pid, **counts)
        manual = len(scripts) - counts["scripts"]
        if manual:
            say("cli.scripts_manual", count=manual)
        for kind, count in counts.items():
            if count:
                self._start(kind)

    def _prepare(self):
        """Fetch the Wine gadget if needed; the hook backend to use."""
        # pylint: disable=import-outside-toplevel
        from grembyte.attach.backends import BACKEND_FRIDA, BACKEND_PTRACE
        from grembyte.attach.wine_gadget import ensure_gadget
        from grembyte.core import settings
        from grembyte.platform import is_wine_process
        from grembyte.platform.wine import wine_bits
        if not is_wine_process(self.pid):
            return BACKEND_FRIDA
        bits = wine_bits(self.pid)
        if bits in (32, 64):
            ensure_gadget(bits, download=bool(settings.get("wine_gadget_download")))
        # Frida's hook engine only handles x64 code (see MainWindow._auto_select_backend).
        return BACKEND_PTRACE if bits == 32 else BACKEND_FRIDA

    def _start(self, kind):
        # pylint: disable=import-outside-toplevel
        from grembyte.core import messages, settings
        from grembyte.features.method_hooks import MethodHookWorker
        from grembyte.features.scripts import ScriptWorker, spec_from_saved as script_spec
        from grembyte.hooks.engine import create_hook_worker
        from grembyte.hooks.saved import method_spec_from_saved, spec_from_saved

        self.starts[kind] = self.starts.get(kind, 0) + 1
        self.saved[kind] = {}
        if kind == "scripts":
            self.hotkeyed = {}
        if kind == "hooks":
            worker = create_hook_worker(self.backend, self.pid)
            rows = self.hook_repository.list_hooks(self.process_id)
            build = spec_from_saved
        elif kind == "method_hooks":
            worker = MethodHookWorker(self.pid)
            rows = self.hook_repository.list_method_hooks(self.process_id)
            build = method_spec_from_saved
        else:
            worker = ScriptWorker(self.pid, language=settings.language() or "en")
            rows = cli_scripts(self.script_repository.list_scripts(self.process_id))
            build = script_spec
        self._connect(kind, worker)
        self.workers[kind] = worker
        worker.start()
        for saved in rows:
            try:
                spec = build(saved)
            except (TypeError, ValueError, KeyError) as error:
                say("cli.restore_failed", label=saved.label or saved.id,
                    message=messages.describe(error))
                continue
            item_id = worker.add_script(spec) if kind == "scripts" else worker.add_hook(spec)
            self.saved[kind][item_id] = saved
            if kind == "scripts" and spec.hotkey and spec.enabled:
                self.hotkeyed[item_id] = spec.hotkey
        if kind == "scripts":
            self._listen_for_hotkeys()

    def _listen_for_hotkeys(self):
        """Run the manual scripts by their hotkeys (see grembyte.core.hotkeys)."""
        # pylint: disable=import-outside-toplevel
        from grembyte.core.hotkeys import HotkeyListener
        from grembyte.platform.keyboard import open_reader
        if not self.hotkeyed and self.hotkeys is None:
            return
        if self.hotkeys is None:
            try:
                reader = open_reader()
            except OSError as error:
                reader = None
                say("cli.hotkeys_unavailable", message=error)
            self.hotkeys = HotkeyListener({self.pid}, reader)
            self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.set_hotkeys(sorted(set(self.hotkeyed.values())))

    def _on_hotkey(self, hotkey):
        worker = self.workers.get("scripts")
        if worker is None:
            return
        for item_id, wanted in self.hotkeyed.items():
            if wanted == hotkey:
                say("cli.hotkey_pressed", hotkey=hotkey, label=self._label("scripts", item_id))
                worker.run_now(item_id)

    def _label(self, kind, item_id):
        saved = self.saved[kind].get(item_id)
        return (saved.label or f"#{saved.id}") if saved is not None else f"#{item_id}"

    def _connect(self, kind, worker):
        # pylint: disable=import-outside-toplevel
        from grembyte.core import logs

        def mine(callback):
            # Signals of a worker that was replaced are dropped.
            def call(*args):
                if self.workers.get(kind) is worker:
                    callback(*args)
            return call

        worker.worker_failed.connect(mine(lambda message: self._on_worker_failed(kind, message)))
        if kind == "scripts":
            worker.script_added.connect(mine(self._on_script_added))
            worker.script_failed.connect(mine(lambda item_id, message: say(
                "cli.script_failed", label=self._label(kind, item_id), message=message)))
            worker.script_ran.connect(mine(self._on_script_ran))
            return
        worker.hook_installed.connect(mine(lambda item_id, _info: say(
            "cli.hook_installed", label=self._label(kind, item_id))))
        worker.hook_failed.connect(mine(lambda item_id, message: say(
            "cli.hook_failed", label=self._label(kind, item_id), message=message)))
        worker.transform_error.connect(mine(lambda item_id, message: logs.log.warning(
            "grembyte-cli: hook %s: %s", self._label(kind, item_id), message)))
        worker.agent_warning.connect(mine(lambda message: logs.log.warning(
            "grembyte-cli: %s", message)))
        if kind == "hooks":
            worker.hook_training.connect(mine(lambda item_id, state: (
                say("cli.hook_training", label=self._label(kind, item_id)) if not state
                else None)))
            worker.callers_learned.connect(mine(self._on_callers_learned))
            worker.site_method.connect(mine(self._on_site_method))

    def _on_worker_failed(self, kind, message):
        # pylint: disable=import-outside-toplevel
        from grembyte.core import logs, messages
        if not _alive(self.pid):
            logs.log.info("grembyte-cli: %s ended with the game: %s", kind, message)
            return
        title = messages.text(f"sessions.kind_{kind}")
        say("cli.worker_failed", kind=title, message=message)
        if self.starts.get(kind, 0) <= RETRIES:
            self.retry_at[kind] = time.monotonic() + RETRY_S
            say("cli.retry", kind=title, seconds=f"{RETRY_S:g}")

    def retry_failed(self):
        now = time.monotonic()
        for kind, when in list(self.retry_at.items()):
            worker = self.workers.get(kind)
            if now < when or (worker is not None and worker.isRunning()):
                continue
            del self.retry_at[kind]
            self._start(kind)

    def _on_script_added(self, item_id):
        saved = self.saved["scripts"].get(item_id)
        if saved is None:
            return
        label = self._label("scripts", item_id)
        if not saved.enabled:
            say("cli.script_paused", label=label)
        elif saved.mode == "manual":
            say("cli.script_hotkey", label=label, hotkey=self.hotkeyed.get(item_id, ""))
        else:
            say(f"cli.script_{saved.mode}", label=label, seconds=f"{saved.seconds:g}")

    def _on_script_ran(self, item_id, result):
        if result.get("error"):
            say("cli.script_failed", label=self._label("scripts", item_id),
                message=result["error"])
        elif item_id in self.hotkeyed and result.get("result"):
            say("cli.script_result", label=self._label("scripts", item_id),
                result=result["result"])

    def _on_callers_learned(self, item_id, callers):
        # As the hook panel does: a saved hook keeps the callers it learned.
        # pylint: disable=import-outside-toplevel
        from grembyte.hooks.saved import persistable_callers
        saved = self.saved["hooks"].get(item_id)
        if saved is None:
            return
        callers = persistable_callers(callers)
        if callers and callers != persistable_callers(saved.callers or []):
            self.hook_repository.update_hook(saved.id, callers=callers)

    def _on_site_method(self, item_ids, _method, managed):
        for item_id in item_ids:
            saved = self.saved["hooks"].get(item_id)
            if saved is not None and managed and saved.managed != managed:
                self.hook_repository.update_hook(saved.id, managed=managed)

    def stop(self):
        """Ask the workers to remove their hooks and end; True if one hangs."""
        workers = list(self.workers.values())
        self.workers = {}
        self.retry_at = {}
        if self.hotkeys is not None:
            self.hotkeys.close()
            self.hotkeys = None
        for worker in workers:
            worker.requestInterruption()
        deadline = time.monotonic() + 3.0
        for worker in workers:
            worker.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        return any(worker.isRunning() for worker in workers)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def split_arguments(argv):
    """``(delay, wants_help, command)`` of grembyte-cli's arguments.

    Its options come first; the command starts at the first other word (or
    after ``--``). Nothing here may fail: the game must start whatever was
    typed in front of it. A delay that is no number keeps the default.
    """
    delay, wants_help = DEFAULT_DELAY_S, False
    words = list(argv)
    while words:
        word = words[0]
        if word == "--":
            words.pop(0)
            break
        if word in ("-h", "--help"):
            wants_help = True
            words.pop(0)
        elif word == "--delay" and len(words) >= 2:
            delay = _seconds(words[1], delay)
            del words[:2]
        elif word.startswith("--delay="):
            delay = _seconds(word.partition("=")[2], delay)
            words.pop(0)
        else:
            break
    return delay, wants_help, words


def _seconds(text, default):
    try:
        value = float(text)
    except ValueError:
        return default
    return value if value >= 0 else default


def main(argv=None):
    delay, wants_help, command = split_arguments(sys.argv[1:] if argv is None else argv)
    child = os.environ.get(CHILD_ENV)
    if child is not None:
        # Executed anew, the game runs already.
        return run(int(child), delay)
    if command:
        # The game first: whatever goes wrong in Grembyte later, it runs.
        try:
            child_pid = start_command(command)
        except OSError as error:
            print(f"grembyte-cli: {_translations()['cli.start_failed'].format(error=error)}",
                  file=sys.stderr)
            return 127
    if wants_help or not command:
        translations = _translations()
        parser = build_parser(translations)
        if wants_help:
            parser.print_help()
        else:
            parser.print_usage(sys.stderr)
            print(f"grembyte-cli: {translations['cli.no_command']}", file=sys.stderr)
        if not command:
            return 0 if wants_help else 2
    if needs_clean_environment(os.environ):
        own = [sys.executable, "-m", "grembyte.cli", "--delay", str(delay)]
        try:
            os.execve(sys.executable, own, clean_environment(os.environ, child_pid))
        except OSError as error:
            print(f"grembyte-cli: {error}", file=sys.stderr)
    return run(child_pid, delay)


if __name__ == "__main__":
    raise SystemExit(main())
