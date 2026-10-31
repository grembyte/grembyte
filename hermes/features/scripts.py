"""Scripts: JavaScript that calls game code by time, e.g. a cheat every minute.

A script is a function body (like the ``code`` of a method script, see
``hermes.hooks.transforms``) that runs on the game thread, where game code
may be called (``engine.onMainThread``; Unreal: the next time it enters
ProcessEvent, Mono: mono_runtime_invoke on Unity's main thread). When it
runs is its ``mode``:

``"once"``    some ``seconds`` after it is started (each time hermes attaches)
``"repeat"``  every ``seconds``; the next run is timed when one has ended
``"manual"``  only when asked (``run_now``)

The body gets one ``ctx`` object:

    ctx.find("ClassName")   a live object of that class or a subclass
                            (Unreal, Unity), e.g. ctx.find("AlabamaCheatManager");
                            Unreal: searched for once (tens of ms on the
                            game thread), then only checked while it lives
    ctx.find("ClassName", "ObjectName")   the one of that name, e.g.
                            ctx.find("UnityEngine.GameObject", "Player(Clone)")
                            or ctx.find("BP_Hero_C", "BP_Hero_C_1"); Unreal
                            also by the name in its outer (ctx.outerName),
                            e.g. "AttributeStatsWidget.PointsAvailableTracker".
                            Class default objects and templates (a widget of
                            a Widget Blueprint's tree) are never found.
    ctx.forEach("ClassName", obj => ...)   calls the function for every object
                            ctx.find could give (until it returns false) and
                            gives how many; Unreal: reads every object anew
    ctx.outerName(obj)      Unreal: the object's name after its outer's,
                            "AttributeStatsWidget.PointsAvailableTracker" (a
                            widget's outer is the widget showing it)
    ctx.call("Namespace.Class.Method", ...args)   call a static game method
                     (also "Namespace.Class:Method", as the catalog names it)
    ctx.log(...)     output shown in the script log
    ctx.state        per-script object that persists between runs
    ctx.runs         how often it ran before
    ctx.scriptId

Objects are the wrappers method hooks get (``.call(name, ...args)``,
``.field(name)``, fields and properties as properties, methods as functions,
e.g. ``player.GetComponentByName("CharacterStats")``, ``.name``,
``.className``); a value
the body returns is shown as its result, an object with a ``target`` the
object inspector opens (``inspect``, ``evaluate`` and ``full_text`` work as
the method monitor's). Example (Avowed)::

    const cheats = ctx.find("AlabamaCheatManager");
    cheats.call("GiveHealth", 50);

Scripts live in the method monitor's agent (``hermes.features.monitor``),
in a Frida session of their own.
"""

import itertools
import queue
import threading
from dataclasses import dataclass

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS
from hermes.features.monitor import build_agent_source


MODES = ("once", "repeat", "manual")
# A repeating script runs at most this often
MIN_REPEAT_SECONDS = 0.1


@dataclass
class ScriptSpec:
    """The code of a script and when it runs (see the module's docstring)."""

    source: str
    mode: str = "manual"
    seconds: float = 0.0
    enabled: bool = True
    label: str = ""

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if self.seconds < 0:
            raise ValueError("seconds must not be negative")
        if self.mode == "repeat" and self.seconds < MIN_REPEAT_SECONDS:
            raise ValueError(f"a repeating script waits at least {MIN_REPEAT_SECONDS} s")

    def to_agent(self, script_id):
        return {
            "id": script_id,
            "source": self.source,
            "mode": self.mode,
            "seconds": self.seconds,
            "enabled": self.enabled,
        }


class ScriptWorker(QThread):
    """Keep a Frida session open and run the scripts of one process.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    script_added = pyqtSignal(int)
    script_updated = pyqtSignal(int)
    script_removed = pyqtSignal(int)
    # script id, error text (compiling it, or a command failed)
    script_failed = pyqtSignal(int, str)
    # script id, {time, runs, result, target?, logs, error}
    script_ran = pyqtSignal(int, dict)
    # As MethodMonitorWorker's, for the object inspector (see hermes.ui.widgets.inspection)
    inspected = pyqtSignal(int, dict)
    inspect_failed = pyqtSignal(int, str)
    evaluated = pyqtSignal(int, list)
    evaluate_failed = pyqtSignal(int, str)
    methods_listed = pyqtSignal(int, list)
    methods_failed = pyqtSignal(int, str)
    call_described = pyqtSignal(int, dict)
    called = pyqtSignal(int, dict)
    call_failed = pyqtSignal(int, str)
    text_fetched = pyqtSignal(int, object)
    text_failed = pyqtSignal(int, str)
    agent_log = pyqtSignal(str, str)
    worker_failed = pyqtSignal(str)

    _ids = itertools.count(1)
    _requests = itertools.count(1)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None

    def add_script(self, spec):
        """Queue a ScriptSpec and return its script id."""
        script_id = next(self._ids)
        self._commands.put(("add", script_id, spec))
        return script_id

    def update_script(self, script_id, spec):
        self._commands.put(("update", script_id, spec))

    def remove_script(self, script_id):
        self._commands.put(("remove", script_id, None))

    def run_now(self, script_id):
        self._commands.put(("run", script_id, None))

    def inspect(self, target):
        """Read an object (a result's ``target``); expect inspected or inspect_failed."""
        request_id = next(self._requests)
        self._commands.put(("inspect", request_id, target))
        return request_id

    def evaluate(self, target, names):
        """Call the named getters of an object on the game's main thread."""
        request_id = next(self._requests)
        self._commands.put(("evaluate", request_id, (target, list(names))))
        return request_id

    def list_methods(self, target):
        """List the instance methods of an object (see MethodMonitorWorker)."""
        request_id = next(self._requests)
        self._commands.put(("methods", request_id, target))
        return request_id

    def describe_method(self, method):
        """Read the parameters of an instance method (see MethodMonitorWorker)."""
        request_id = next(self._requests)
        self._commands.put(("describe_method", request_id, method))
        return request_id

    def call_on(self, target, method, arguments):
        """Call an instance method on an object (see MethodMonitorWorker)."""
        request_id = next(self._requests)
        self._commands.put(("call_on", request_id, (target, method, list(arguments))))
        return request_id

    def full_text(self, text_id):
        """Fetch the whole text of a long string value (its ``fullText``)."""
        request_id = next(self._requests)
        self._commands.put(("text", request_id, text_id))
        return request_id

    @sessions.tracked(sessions.SCRIPTS)
    def run(self):
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", self._on_detached)
            script = session.create_script(build_agent_source())
            script.on("message", self._on_message)
            script.set_log_handler(self._on_log)
            script.load()
            if not self._ready.wait(5):
                raise RuntimeError("Timed out while loading the script agent")
            while (not self.isInterruptionRequested() and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    command = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._execute(script.exports_sync, *command)
            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise RuntimeError(f"Target process detached: {self._detach_reason}")
        except FRIDA_ERRORS + (OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(str(error))
        finally:
            if script is not None:
                try:
                    script.exports_sync.dispose()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
                try:
                    script.unload()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)

    def _execute(self, api, action, script_id, argument):
        try:
            if action == "add":
                api.add_script(argument.to_agent(script_id))
                self.script_added.emit(script_id)
            elif action == "update":
                api.update_script(script_id, argument.to_agent(script_id))
                self.script_updated.emit(script_id)
            elif action == "remove":
                if api.remove_script(script_id):
                    self.script_removed.emit(script_id)
            elif action == "run":
                api.run_script_now(script_id)
            elif action in self._REQUESTS:
                self._answer(api, action, script_id, argument)
                return
        except frida.RPCException as error:
            self.script_failed.emit(script_id, str(error))

    # request action -> (agent call, answer signal, failure signal)
    _REQUESTS = {
        "inspect": ("inspect", "inspected", "inspect_failed"),
        "evaluate": ("evaluate", "evaluated", "evaluate_failed"),
        "methods": ("list_methods", "methods_listed", "methods_failed"),
        "describe_method": ("describe_method", "call_described", "call_failed"),
        "call_on": ("call_on", "called", "call_failed"),
        "text": ("full_text", "text_fetched", "text_failed"),
    }

    def _answer(self, api, action, request_id, argument):
        call, answered, failed = self._REQUESTS[action]
        # Requests with several arguments queue them as a tuple
        arguments = argument if isinstance(argument, tuple) else (argument,)
        try:
            getattr(self, answered).emit(request_id, getattr(api, call)(*arguments))
        except frida.RPCException as error:
            getattr(self, failed).emit(request_id, str(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = message.get("stack", "Script agent failed")
            self._ready.set()
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready.set()
        elif kind == "script-run":
            self.script_ran.emit(payload.get("scriptId", 0), payload)

    def _on_log(self, level, text):
        logs.agent_log("script agent", level, text)
        self.agent_log.emit(level, text)

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason
