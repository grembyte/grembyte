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
                     (also "Namespace.Class:Method", as the catalog names it)
    ctx.find("ClassName")   a live object of that class or a subclass (Unreal,
                     Unity), e.g. ctx.find("AlabamaCheatManager").call("GiveHealth", 50)
    ctx.find("ClassName", "ObjectName")   the one of that name, e.g.
                     ctx.find("UnityEngine.GameObject", "Player(Clone)")
    ctx.forEach("ClassName", obj => ...)   every object ctx.find could give
    ctx.outerName(obj)   Unreal: "AttributeStatsWidget.PointsAvailableTracker",
                     a name ctx.find takes (see hermes.features.scripts)
    ctx.log(...)     output shown in the hook debug window
    ctx.method, ctx.hookId, ctx.threadId

Values are JavaScript numbers, booleans and strings; objects (and structs)
are wrappers with ``.name`` (Unity name of a GameObject or component; Unreal:
the UObject's name), ``.className`` (Unreal: e.g. ``AAlabama_PlayerController_C``),
``.className``, ``.address``, ``.field(name)``, ``.get(property)``,
``.call(method, ...args)`` and ``.component(typeName)``. Fields and
properties can also be read and assigned directly (``roller.IsPartyMember``,
``damage.m_damageMult = 0.5``; assigning a property calls its setter) unless
the wrapper has a member of that name. Calling game code
is only allowed on Unity's main thread, where most game logic runs.

Unreal: the hook sees a UFunction where the monitor does (Blueprint code,
ProcessEvent) and gets its arguments from the call frame. Called from
Blueprint bytecode, arguments that are no simple variable or constant are
missing from ``ctx.args``, and only ref arguments can be changed (the others
are the caller's own variables). Weak object pointers read as objects, TArrays
as arrays, FString and FName as strings; only numbers, bools and object
fields can be assigned. ``.call()`` runs a UFunction through ProcessEvent
(on the game thread, where hooks run); arguments may be numbers, bools,
objects, structs, strings (FString) and FName texts the game knows. Hooks
on functions the call reaches do not run for it (Frida skips hooks while one
runs).

Hooks live in the method monitor's agent (``hermes.features.monitor``)
and are found again after a restart by their method (``HookSpec.managed``).
Hooks after the call need Frida to swap the return address of each call, so
an exception passing through the hooked method can crash the game; hooks
before the call do not.
"""

import itertools
import queue
import threading
from dataclasses import dataclass

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs, messages
from hermes.core import sessions
from hermes.features.monitor import build_agent_source
from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
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

    _requests = itertools.count(1)

    session_ready = Signal(str)
    hook_installed = Signal(int, object)
    hook_failed = Signal(int, str)
    hook_removed = Signal(int)
    hook_updated = Signal(int)
    # [{hookId, count, last: {old, result, kept, error}}]
    hook_events = Signal(object)
    # ctx.log lines: {hookId, time, text, targets?}; lines dropped per hook id
    hook_logs = Signal(object, object)
    inspected = Signal(int, object)
    inspect_failed = Signal(int, str)
    evaluated = Signal(int, object)
    evaluate_failed = Signal(int, str)
    methods_listed = Signal(int, object)
    methods_failed = Signal(int, str)
    call_described = Signal(int, object)
    called = Signal(int, object)
    call_failed = Signal(int, str)
    text_fetched = Signal(int, object)
    text_failed = Signal(int, str)
    transform_error = Signal(int, str)
    hook_debug = Signal(object, object)
    # hook id: the game freed the method's code (e.g. while exiting)
    hook_ended = Signal(int)
    agent_log = Signal(str, str)
    agent_warning = Signal(str)
    worker_failed = Signal(str)

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

    def inspect(self, target):
        """Read an object for the Inspector; expect inspected or inspect_failed."""
        request_id = next(self._requests)
        self._commands.put(("inspect", request_id, target))
        return request_id

    def evaluate(self, target, names):
        """Evaluate the Inspector's properties on the game's main thread."""
        request_id = next(self._requests)
        self._commands.put(("evaluate", request_id, (target, list(names))))
        return request_id

    def list_methods(self, target):
        """List the Inspector's callable instance methods."""
        request_id = next(self._requests)
        self._commands.put(("methods", request_id, target))
        return request_id

    def describe_method(self, method):
        """Read the parameters of an Inspector method entry."""
        request_id = next(self._requests)
        self._commands.put(("describe_method", request_id, method))
        return request_id

    def call_on(self, target, method, arguments):
        """Call an Inspector method on the game's main thread."""
        request_id = next(self._requests)
        self._commands.put(("call_on", request_id, (target, method, list(arguments))))
        return request_id

    def full_text(self, text_id):
        """Fetch the whole text of a long string shown in the Inspector."""
        request_id = next(self._requests)
        self._commands.put(("text", request_id, text_id))
        return request_id

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
                raise messages.LocalizedRuntimeError("error.method_hooks.load_timeout")
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
                raise messages.LocalizedRuntimeError("error.agent.detached",
                                                     reason=self._detach_reason)
        except FRIDA_ERRORS + (OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(messages.describe(error))
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
                if info.get("viaStack"):
                    logs.log.warning("method hook: returns of %s not probed, an exception "
                                     "thrown through it may end the game", argument.label)
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
            elif action == "inspect":
                try:
                    self.inspected.emit(hook_id, api.inspect(argument))
                except frida.RPCException as error:
                    self.inspect_failed.emit(hook_id, messages.describe(error))
            elif action == "evaluate":
                try:
                    self.evaluated.emit(hook_id, api.evaluate(*argument))
                except frida.RPCException as error:
                    self.evaluate_failed.emit(hook_id, messages.describe(error))
            elif action == "methods":
                try:
                    self.methods_listed.emit(hook_id, api.list_methods(argument))
                except frida.RPCException as error:
                    self.methods_failed.emit(hook_id, messages.describe(error))
            elif action == "describe_method":
                try:
                    self.call_described.emit(hook_id, api.describe_method(argument))
                except frida.RPCException as error:
                    self.call_failed.emit(hook_id, messages.describe(error))
            elif action == "call_on":
                try:
                    self.called.emit(hook_id, api.call_on(*argument))
                except frida.RPCException as error:
                    self.call_failed.emit(hook_id, messages.describe(error))
            elif action == "text":
                try:
                    self.text_fetched.emit(hook_id, api.full_text(argument))
                except frida.RPCException as error:
                    self.text_failed.emit(hook_id, messages.describe(error))
        except frida.RPCException as error:
            self.hook_failed.emit(hook_id, messages.describe(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = messages.describe(
                message.get("stack", messages.text("error.method_hooks.agent_failed")))
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
        elif kind == "hook-logs":
            self.hook_logs.emit(
                payload.get("lines", []),
                {int(hook_id): count for hook_id, count in payload.get("dropped", {}).items()},
            )
        elif kind == "transform-error":
            self.transform_error.emit(payload.get("hookId", 0),
                                      messages.describe(payload.get("error", "")))
        elif kind == "hook-ended":
            self.hook_ended.emit(payload.get("hookId", 0))
        elif kind == "agent-error":
            self.agent_warning.emit(messages.describe(payload.get("error", "")))

    def _on_log(self, level, text):
        logs.agent_log("method hook agent", level, text)
        self.agent_log.emit(level, text)

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason
