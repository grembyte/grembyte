"""Method hooks: scripts that change what C# methods of a game do.

They work through the engine adapter of the game (see hermes/engines).

A method hook runs a JavaScript function (a ``"method"`` hook template, see
``hermes.hooks.transforms``) on the game thread that calls the hooked method,
at one of two points (``phase``):

``"after"``  when the method returns. A value the function returns replaces
             the method's result; ``undefined`` or ``null`` keep it.
``"before"`` when the method is called. The function may assign arguments
             (``ctx.args.amount = 5``, also ref/out arguments) and fields or
             properties of objects (``ctx.args.damage.m_damageMult *= 0.2``);
             the method then runs with them. Its return value is ignored.

The function gets one ``ctx`` object:

    ctx.phase        "before" or "after"
    ctx.args.<name>  the arguments as the method got them
    ctx.refs.<name>  what ref/out arguments hold after the call ("after")
    ctx.this         the instance (null for static methods)
    ctx.result       the method's result ("after"; undefined for void methods)
    ctx.params       the hook's parameters
    ctx.state        per-hook object that persists between calls
    ctx.call("Namespace.Class.Method", ...args)   call a static game method
    ctx.log(...)     output shown in the hook debug window
    ctx.method, ctx.hookId, ctx.threadId

Values are JavaScript numbers, booleans and strings; objects (and structs)
are wrappers with ``.name`` (Unity name of a GameObject or component),
``.className``, ``.address``, ``.field(name)``, ``.get(property)``,
``.call(method, ...args)`` and ``.component(typeName)``. Fields and
properties can also be read and assigned directly (``roller.IsPartyMember``,
``damage.m_damageMult = 0.5``; assigning a property calls its setter) unless
the wrapper has a member of that name. Calling game code
is only allowed on Unity's main thread, where most game logic runs.

Hooks live in the method monitor's agent (``hermes.features.monitor``)
and are found again after a restart by their method (``HookSpec.managed``).
Hooks after the call need Frida to swap the return address of each call, so
an exception passing through the hooked method can crash the game; hooks
before the call do not.
"""

import queue
import threading
from dataclasses import dataclass

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.features.monitor import build_agent_source
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS
from hermes.hooks.engine import HookWorker
from hermes.hooks.transforms import ValueTransform


PHASES = ("after", "before")


@dataclass
class MethodHookSpec:
    """A method (``identity``: assembly, token, signature) and its manipulation.

    ``phase`` is ``"after"`` (change the result) or ``"before"`` (change
    arguments and fields) the call.
    """

    identity: dict
    transform: ValueTransform
    enabled: bool = True
    label: str = ""
    phase: str = "after"

    def __post_init__(self):
        if not isinstance(self.transform, ValueTransform):
            raise ValueError("transform must be a ValueTransform instance")
        if self.phase not in PHASES:
            raise ValueError(f"phase must be one of {', '.join(PHASES)}")
        if not self.identity.get("method") or "token" not in self.identity:
            raise ValueError("a method hook needs the method's identity")

    @property
    def signature(self):
        """``Namespace.Class:Method (params)``."""
        return self.identity["method"]

    def to_agent(self, hook_id):
        return {
            "id": hook_id,
            "identity": self.identity,
            "transform": self.transform.to_agent(),
            "enabled": self.enabled,
            "phase": self.phase,
        }


class MethodHookWorker(QThread):
    """Keep a Frida session open and manage the method hooks of one process.

    Its signals match those of ``HookWorker`` that the hook panel uses, and
    hook ids come from the same counter, so both kinds share one table.
    """

    session_ready = pyqtSignal(str)
    hook_installed = pyqtSignal(int, dict)
    hook_failed = pyqtSignal(int, str)
    hook_removed = pyqtSignal(int)
    hook_updated = pyqtSignal(int)
    # [{hookId, count, last: {old, result, kept, error}}]
    hook_events = pyqtSignal(list)
    transform_error = pyqtSignal(int, str)
    hook_debug = pyqtSignal(list, dict)
    # hook id: the game freed the method's code (e.g. while exiting)
    hook_ended = pyqtSignal(int)
    agent_log = pyqtSignal(str, str)
    agent_warning = pyqtSignal(str)
    worker_failed = pyqtSignal(str)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None

    def add_hook(self, spec):
        """Queue a MethodHookSpec and return its hook id."""
        hook_id = next(HookWorker._ids)  # pylint: disable=protected-access
        self._commands.put(("add", hook_id, spec))
        return hook_id

    def remove_hook(self, hook_id):
        self._commands.put(("remove", hook_id, None))

    def set_transform(self, hook_id, transform):
        if not isinstance(transform, ValueTransform):
            raise ValueError("transform must be a ValueTransform instance")
        self._commands.put(("transform", hook_id, transform))

    def set_enabled(self, hook_id, enabled):
        self._commands.put(("enabled", hook_id, bool(enabled)))

    def set_debug(self, hook_id, enabled):
        self._commands.put(("debug", hook_id, bool(enabled)))

    def set_phase(self, hook_id, phase):
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {', '.join(PHASES)}")
        self._commands.put(("phase", hook_id, phase))

    @sessions.tracked(sessions.METHOD_HOOKS)
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
                raise RuntimeError("Timed out while loading the method hook agent")
            self.session_ready.emit("")
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

    def _execute(self, api, action, hook_id, argument):
        try:
            if action == "add":
                info = api.add_method_hook(argument.to_agent(hook_id))
                info["label"] = argument.label
                info["transformDescription"] = argument.transform.describe()
                self.hook_installed.emit(hook_id, info)
            elif action == "remove":
                if api.remove_method_hook(hook_id):
                    self.hook_removed.emit(hook_id)
            elif action == "transform":
                api.set_method_hook_transform(hook_id, argument.to_agent())
                self.hook_updated.emit(hook_id)
            elif action == "enabled":
                api.set_method_hook_enabled(hook_id, argument)
                self.hook_updated.emit(hook_id)
            elif action == "debug":
                api.set_method_hook_debug(hook_id, argument)
                self.hook_updated.emit(hook_id)
            elif action == "phase":
                api.set_method_hook_phase(hook_id, argument)
                self.hook_updated.emit(hook_id)
        except frida.RPCException as error:
            self.hook_failed.emit(hook_id, str(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = message.get("stack", "Method hook agent failed")
            self._ready.set()
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready.set()
        elif kind == "hook-events":
            self.hook_events.emit(payload.get("events", []))
        elif kind == "hook-debug":
            self.hook_debug.emit(
                payload.get("events", []),
                {int(key): count for key, count in payload.get("dropped", {}).items()},
            )
        elif kind == "transform-error":
            self.transform_error.emit(payload.get("hookId", 0), payload.get("error", ""))
        elif kind == "hook-ended":
            self.hook_ended.emit(payload.get("hookId", 0))
        elif kind == "agent-error":
            self.agent_warning.emit(payload.get("error", ""))

    def _on_log(self, level, text):
        logs.agent_log("method hook agent", level, text)
        self.agent_log.emit(level, text)

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason
