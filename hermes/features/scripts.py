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
    ctx.gas          Unreal games with the Gameplay Ability System: the
                     player's attributes. ctx.gas.get("Might") or
                     ctx.gas.get("AttributeSetCore.Might"): {base, current}
                     (null without a player); ctx.gas.setBase(name, value)
                     sets the base value through the game's own code, which
                     applies the active effects to it; ctx.gas.attributes()
                     lists the sets (see ScriptWorker.gas_attributes).
                     A set numbered anew on every load
                     ("AttributeSet_Health_2147445233") is also found
                     without the number ("AttributeSet_Health.Health");
                     ctx.gas.findSet(/^AttributeSet_Health/) gives the
                     name of the first set matching, or null
    ctx.findClass("ClassName")   Unreal: the class itself, e.g. for a
                     TSubclassOf parameter: ctx.findClass("ItFo_Potion_Health_03")
    ctx.args         the values of the script's parameters by name (see below)
    ctx.lang         the app language ("de" or "en")
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

A script may have parameters (``ScriptParam``): typed in when it is run by
hand, their defaults when it runs by time. A parameter is text, a number, a
bool or a choice; a choice's ``options`` is a function body like the
script's that returns the choices (asked for when they are to be shown, on
the game thread): values or ``{label, value}``, values being strings,
numbers or bools. Example (Gothic 1 Remake)::

    return ctx.call("CheatModeHelper:GetAllItems", "").map(item => ({
        label: String(item.m_Name), value: item.className}));

and the script gives the chosen item (``ctx.findClass(ctx.args.item)``).

Scripts live in the method monitor's agent (``hermes.features.monitor``),
in a Frida session of their own.
"""

import itertools
import json
import queue
import re
import threading
from dataclasses import dataclass, field

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs, messages
from hermes.core import sessions
from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
from hermes.features.monitor import build_agent_source


MODES = ("once", "repeat", "manual")
# A repeating script runs at most this often
MIN_REPEAT_SECONDS = 0.1
# What a parameter's value is
PARAM_KINDS = ("text", "number", "bool", "choice")
_PARAM_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TRUE = {"true", "1", "ja", "yes", "wahr"}
_FALSE = {"false", "0", "nein", "no", "falsch", ""}


@dataclass(frozen=True)
class ScriptParam:
    """A parameter of a script: ``ctx.args.<name>``.

    ``default`` is the value as typed in (a choice's: its value as text, or
    empty for none); ``options`` the function body that gives a choice's
    choices (see the module's docstring).
    """

    name: str
    label: str = ""
    kind: str = "text"
    default: str = ""
    options: str = ""

    def __post_init__(self):
        if not _PARAM_NAME.match(self.name):
            raise messages.LocalizedValueError("error.scripts.param_name", name=self.name)
        if self.kind not in PARAM_KINDS:
            raise messages.LocalizedValueError("error.scripts.param_kind",
                                              kinds=", ".join(PARAM_KINDS))
        if self.kind == "choice" and not self.options.strip():
            raise messages.LocalizedValueError("error.scripts.param_options", name=self.name)
        self.value(self.default)  # a default that is no value of its kind

    def value(self, text):
        """The value of ``text`` typed in for this parameter (not a choice's:
        its value is the chosen option's); raises LocalizedValueError."""
        if self.kind == "number":
            cleaned = text.strip().replace(",", ".")
            if cleaned == "":
                return 0
            try:
                return int(cleaned)
            except ValueError:
                pass
            try:
                return float(cleaned)
            except ValueError:
                raise messages.LocalizedValueError("error.scripts.param_number",
                                                  name=self.name, text=text) from None
        if self.kind == "bool":
            word = text.strip().lower()
            if word in _TRUE:
                return True
            if word in _FALSE:
                return False
            raise messages.LocalizedValueError("error.scripts.param_bool",
                                              name=self.name, text=text)
        if self.kind == "choice":
            return text if text != "" else None
        return text

    def to_json(self):
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "default": self.default, "options": self.options}

    @classmethod
    def from_json(cls, data):
        return cls(str(data["name"]), str(data.get("label", "")), str(data.get("kind", "text")),
                   str(data.get("default", "")), str(data.get("options", "")))


def params_to_json(params):
    """The parameters as the database keeps them: JSON text, or None without any."""
    return json.dumps([param.to_json() for param in params]) if params else None


def params_from_json(text):
    """The parameters of a saved script (see params_to_json)."""
    return tuple(ScriptParam.from_json(data) for data in json.loads(text)) if text else ()


def choice_matches(value, text):
    """Whether a choice's option ``value`` is the one saved as ``text``."""
    if isinstance(value, bool):
        return str(value).lower() == text.lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) == text
    return str(value) == text


@dataclass
class ScriptSpec:
    """The code of a script and when it runs (see the module's docstring)."""

    source: str
    mode: str = "manual"
    seconds: float = 0.0
    enabled: bool = True
    label: str = ""
    params: tuple = field(default_factory=tuple)

    def __post_init__(self):
        if self.mode not in MODES:
            raise messages.LocalizedValueError("error.scripts.mode", modes=", ".join(MODES))
        if self.seconds < 0:
            raise messages.LocalizedValueError("error.scripts.negative_seconds")
        if self.mode == "repeat" and self.seconds < MIN_REPEAT_SECONDS:
            raise messages.LocalizedValueError("error.scripts.repeat_minimum",
                                              seconds=MIN_REPEAT_SECONDS)
        self.params = tuple(self.params)
        names = [param.name for param in self.params]
        for name in names:
            if names.count(name) > 1:
                raise messages.LocalizedValueError("error.scripts.param_twice", name=name)

    def default_args(self):
        """``ctx.args`` of a run without asking: each parameter's default."""
        return {param.name: param.value(param.default) for param in self.params}

    def to_agent(self, script_id):
        return {
            "id": script_id,
            "source": self.source,
            "mode": self.mode,
            "seconds": self.seconds,
            "enabled": self.enabled,
            "args": self.default_args(),
            "options": {param.name: param.options for param in self.params
                        if param.kind == "choice"},
        }


class ScriptWorker(QThread):
    """Keep a Frida session open and run the scripts of one process.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    script_added = Signal(int)
    script_updated = Signal(int)
    script_removed = Signal(int)
    # script id, error text (compiling it, or a command failed)
    script_failed = Signal(int, str)
    # script id, {time, runs, result, target?, logs, error}
    script_ran = Signal(int, object)
    # As MethodMonitorWorker's, for the object inspector (see hermes.ui.widgets.inspection)
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
    # request id, the player's GAS attributes (see gas_attributes) or None
    gas_listed = Signal(int, object)
    gas_failed = Signal(int, str)
    # request id, {base, current} after setting a base value
    gas_set = Signal(int, object)
    gas_set_failed = Signal(int, str)
    # request id, a choice parameter's options [{label, value}] (see script_options)
    options_listed = Signal(int, object)
    options_failed = Signal(int, str)
    agent_log = Signal(str, str)
    worker_failed = Signal(str)

    _ids = itertools.count(1)
    _requests = itertools.count(1)

    def __init__(self, pid, parent=None, language="en"):
        super().__init__(parent)
        self.pid = pid
        self.language = language
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

    def run_now(self, script_id, args=None):
        """Run a script now; ``args`` are its ``ctx.args`` (else the defaults)."""
        self._commands.put(("run", script_id, args))

    def script_options(self, script_id, name):
        """The options of the choice parameter ``name``, from its code on the
        game thread; expect options_listed or options_failed."""
        request_id = next(self._requests)
        self._commands.put(("options", request_id, (script_id, name)))
        return request_id

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

    def gas_attributes(self):
        """Read the player's GAS attribute sets (Unreal); expect gas_listed or gas_failed.

        The answer is ``{owner, setter, component, sets: [{name, className,
        attributes: [{name, base, current}]}]}``, or None without a player.
        """
        request_id = next(self._requests)
        self._commands.put(("gas_attributes", request_id, ()))
        return request_id

    def gas_set_base(self, name, value):
        """Set the base value of the player's attribute ``name`` ("Set.Attribute")
        through the game; expect gas_set or gas_set_failed."""
        request_id = next(self._requests)
        self._commands.put(("gas_set_base", request_id, (name, float(value))))
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
            script = session.create_script(build_agent_source(self.language))
            script.on("message", self._on_message)
            script.set_log_handler(self._on_log)
            script.load()
            if not self._ready.wait(5):
                raise messages.LocalizedRuntimeError("error.scripts.load_timeout")
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
                api.run_script_now(script_id, argument)
            elif action in self._REQUESTS:
                self._answer(api, action, script_id, argument)
                return
        except frida.RPCException as error:
            self.script_failed.emit(script_id, messages.describe(error))

    # request action -> (agent call, answer signal, failure signal)
    _REQUESTS = {
        "inspect": ("inspect", "inspected", "inspect_failed"),
        "evaluate": ("evaluate", "evaluated", "evaluate_failed"),
        "methods": ("list_methods", "methods_listed", "methods_failed"),
        "describe_method": ("describe_method", "call_described", "call_failed"),
        "call_on": ("call_on", "called", "call_failed"),
        "text": ("full_text", "text_fetched", "text_failed"),
        "gas_attributes": ("gas_attributes", "gas_listed", "gas_failed"),
        "gas_set_base": ("gas_set_base", "gas_set", "gas_set_failed"),
        "options": ("script_options", "options_listed", "options_failed"),
    }

    def _answer(self, api, action, request_id, argument):
        call, answered, failed = self._REQUESTS[action]
        # Requests with several arguments queue them as a tuple
        arguments = argument if isinstance(argument, tuple) else (argument,)
        try:
            getattr(self, answered).emit(request_id, getattr(api, call)(*arguments))
        except frida.RPCException as error:
            getattr(self, failed).emit(request_id, messages.describe(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = messages.describe(
                message.get("stack", messages.text("error.scripts.agent_failed")))
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
