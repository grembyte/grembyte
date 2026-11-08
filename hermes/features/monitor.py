"""Method monitor: log the calls of methods (C# methods of Mono games, UFunctions
of Unreal games).

It works through the engine adapter of the game (see hermes/engines).

For Unreal games the adapter hooks a UFunction's Func and finds the arguments
in its call frame rather than in registers (``frame``, see
hermes/engines/unreal/agent.py): in the parameters ProcessEvent passes, or,
when Blueprint code calls a native function, in the bytecode of the call
(variables and constants; other arguments show as ``?``). The result is read
where the caller wants it written. The caller is the Blueprint function and
bytecode offset the call came from (``WBP_HUD_C:Tick+0x35``) when a script
runs above it, else the native return address. Objects show their UObject name, and the
inspector their UProperties. Calls go through ProcessEvent on the game
thread; an instance UFunction of the catalog runs on a live object of its
class (``call_found``), and outputs (``out``) are read from the parameter
block afterwards.

Mono compiles the method (see ``engine.prepare``), and a Frida interceptor on
its native code reports every call with its arguments, the calling method
and, if asked for, the return value.

Arguments are read where Mono's calling convention for the game's platform
puts them:

- x86-64 Linux (System V): ``this`` and integer-like values in rdi, rsi, rdx,
  rcx, r8, r9, floating point values in xmm0-xmm7, the rest on the stack. A
  struct larger than 16 bytes is returned through a hidden pointer argument.
  Where structs are passed by value depends on their fields, so arguments
  after a struct are not decoded.
- x64 Windows (Wine): rcx, rdx, r8, r9 or xmm0-xmm3 by position, the rest on
  the stack above 32 bytes of shadow space. Every argument, a struct too,
  takes one slot; a struct of other than 1, 2, 4 or 8 bytes is returned
  through a hidden pointer.
- x86 Windows (32-bit Wine): everything on the stack, ``this`` first; 64-bit
  values take two slots, a struct its size rounded to 4 bytes. Every struct
  result goes through a hidden pointer. A float result is in the x87 register
  st0 and a 64-bit one in edx:eax; Frida reads neither, so they are not
  shown.

The hidden pointer comes after ``this``, or after a first reference argument.

When the game frees the memory of monitored code (Unity does so on exit,
when Mono releases its JIT code), the monitor is removed first: Frida would
otherwise write the original bytes back into unmapped memory when it is
unloaded, and abort the game. The agent watches unmapping for that
(``watchUnmaps``: ``munmap``, or ntdll's calls under Wine) and reports
``monitor-ended``.

The return value needs Frida to replace the return address of every call
with its own code. Mono cannot walk the stack through that code, so an
exception passing through the method can crash the game; hence it is
optional. With it, ``ref`` and ``out`` arguments are read again after the
call (``after``): an ``out`` argument only holds something then.

Arguments, ``this`` and results that are GameObjects or components show
their Unity name (``Health "Young Wolf"@0x…``): the monitor asks Unity for it
while the game thread is in the call. Names seen that way are also shown for
the same objects in snapshots and the inspector.

Values are ``{text, target?, snapshot?}``. ``target`` is an object, array or
struct the object inspector can open (``inspect``), ``snapshot`` its
contents at the time of the call (like ``inspect`` returns them, or
``{error}``) when the monitor records ``objects``. A string (not null) has
``string`` set; one longer than ``text`` shows has ``fullText`` (the id to
fetch it whole with ``full_text``; the agent keeps the texts of recent
calls) and ``length``.

Properties (parameterless getters, and a GameObject's components) are game
code that Unity only allows on its main thread. ``evaluate`` queues the call
until the main thread runs a script method (see ``engine.onMainThread``). The inspector
shows the current fields; a short-lived object may be collected by then.

Static methods can be called there too (``call``, see
hermes/features/method_call.py for the arguments).
"""

import itertools
import json
import queue
import threading

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs, messages
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE
from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
from hermes.agent.source import load_agent_source


_AGENT_SOURCE = (ENGINE_AGENT_SOURCE + load_agent_source(__file__, "script_native.js")
                 + load_agent_source(__file__))


def build_agent_source(language="en"):
    return f"const HERMES_LANGUAGE = {json.dumps(language)};\n" + _AGENT_SOURCE


class MethodMonitorWorker(QThread):
    """Keep a Frida session open and log the calls of monitored C# methods.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # monitor id, {address, instance, params, returns, undecoded}
    monitor_added = Signal(int, object)
    # monitor id: the game freed the method's code (e.g. while exiting)
    monitor_ended = Signal(int)
    # request id, {name, address, fields: [{name, type, offset, static, value}],
    # properties: [{name, type, static, setter}], length?}
    inspected = Signal(int, object)
    inspect_failed = Signal(int, str)
    # request id, [{name, value}] of evaluated properties
    evaluated = Signal(int, object)
    evaluate_failed = Signal(int, str)
    # request id, [{name, method, owner, params, returns}] of an object's instance methods
    methods_listed = Signal(int, object)
    methods_failed = Signal(int, str)
    # request id, {params: [{name, type}], returns} of a static or instance method
    call_described = Signal(int, object)
    # request id, {result: value or None (void), outputs?: [{name, value}],
    # object?: value} or {exception: text}
    called = Signal(int, object)
    call_failed = Signal(int, str)
    # request id, the whole text of a long string value (None: no longer kept)
    text_fetched = Signal(int, object)
    text_failed = Signal(int, str)
    monitor_failed = Signal(int, str)
    # calls, dropped calls per monitor id, all calls per monitor id
    calls = Signal(object, object, object)
    worker_failed = Signal(str)

    _ids = itertools.count(1)
    _requests = itertools.count(1)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None

    def add_monitor(self, identity, returns=False, objects=False):
        """Queue a method (``CatalogMethod.identity()``) and return its monitor id.

        With ``returns`` the return value and what ``ref``/``out`` arguments
        hold afterwards are logged too; with ``objects`` the fields of object
        arguments (``snapshot``). ``None`` enables an option if the method
        has a result or objects; monitor_added tells (``recordsResults``,
        ``recordsObjects``, ``hasResults``, ``hasObjects``).
        """
        monitor_id = next(self._ids)
        self._commands.put(("add", monitor_id,
                            {"identity": identity, "returns": returns, "objects": objects}))
        return monitor_id

    def inspect(self, target):
        """Read an object (a value's ``target``); expect inspected or inspect_failed."""
        request_id = next(self._requests)
        self._commands.put(("inspect", request_id, target))
        return request_id

    def evaluate(self, target, names):
        """Call the named getters of an object on the game's main thread.

        Expect evaluated or evaluate_failed. Getters are game code: they may
        have side effects.
        """
        request_id = next(self._requests)
        self._commands.put(("evaluate", request_id, (target, list(names))))
        return request_id

    def describe_call(self, identity, instance=False):
        """Read the parameters of a static method, or with ``instance`` of an
        instance method (see ``call_found``); expect call_described or call_failed."""
        request_id = next(self._requests)
        self._commands.put(("describe", request_id, (identity, instance)))
        return request_id

    def call(self, identity, arguments):
        """Call a static method on the game's main thread; expect called or call_failed.

        ``arguments`` as ``parse_argument`` (hermes/features/method_call.py)
        returns them. Game code: it changes the game like the game itself would.
        """
        request_id = next(self._requests)
        self._commands.put(("call", request_id, (identity, list(arguments))))
        return request_id

    def call_found(self, identity, class_name, object_text, arguments):
        """Call an instance method on a live object of ``class_name``, on the
        game's main thread: the first one found if ``object_text`` is empty,
        else the one of that name or at that address (0x…). Expect called
        (with ``object``, the object it ran on) or call_failed. As ``call``: game code.
        """
        request_id = next(self._requests)
        self._commands.put(("call_found", request_id,
                            (identity, class_name, object_text, list(arguments))))
        return request_id

    def list_methods(self, target):
        """List the instance methods of an object; expect methods_listed or methods_failed."""
        request_id = next(self._requests)
        self._commands.put(("methods", request_id, target))
        return request_id

    def describe_method(self, method):
        """Read the parameters of an instance method (a ``list_methods`` entry's
        ``method``); expect call_described or call_failed."""
        request_id = next(self._requests)
        self._commands.put(("describe_method", request_id, method))
        return request_id

    def call_on(self, target, method, arguments):
        """Call an instance method on an object, on the game's main thread;
        expect called or call_failed. As ``call``: game code."""
        request_id = next(self._requests)
        self._commands.put(("call_on", request_id, (target, method, list(arguments))))
        return request_id

    def full_text(self, text_id):
        """Fetch the whole text of a long string value (its ``fullText``).

        Expect text_fetched or text_failed. The agent keeps the texts of
        recent calls only.
        """
        request_id = next(self._requests)
        self._commands.put(("text", request_id, text_id))
        return request_id

    def remove_monitor(self, monitor_id):
        self._commands.put(("remove", monitor_id, None))

    @sessions.tracked(sessions.MONITOR)
    def run(self):
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", self._on_detached)
            script = session.create_script(build_agent_source())
            script.on("message", self._on_message)
            script.load()
            if not self._ready.wait(5):
                raise messages.LocalizedRuntimeError("error.method_monitor.load_timeout")
            while (not self.isInterruptionRequested() and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    action, monitor_id, argument = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._execute(script.exports_sync, action, monitor_id, argument)
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

    def _execute(self, api, action, monitor_id, argument):
        try:
            if action == "add":
                info = api.add_monitor({"id": monitor_id, **argument})
                if info.get("viaStack"):
                    logs.log.warning("method monitor: returns of %s not probed, an exception "
                                     "thrown through it may end the game",
                                     argument["identity"].get("method"))
                self.monitor_added.emit(monitor_id, info)
            elif action == "remove":
                api.remove_monitor(monitor_id)
            elif action == "inspect":
                try:
                    self.inspected.emit(monitor_id, api.inspect(argument))
                except frida.RPCException as error:
                    self.inspect_failed.emit(monitor_id, messages.describe(error))
            elif action == "evaluate":
                try:
                    self.evaluated.emit(monitor_id, api.evaluate(*argument))
                except frida.RPCException as error:
                    self.evaluate_failed.emit(monitor_id, messages.describe(error))
            elif action == "describe":
                try:
                    self.call_described.emit(monitor_id, api.describe_call(*argument))
                except frida.RPCException as error:
                    self.call_failed.emit(monitor_id, messages.describe(error))
            elif action == "call":
                try:
                    self.called.emit(monitor_id, api.call_static(*argument))
                except frida.RPCException as error:
                    self.call_failed.emit(monitor_id, messages.describe(error))
            elif action == "call_found":
                try:
                    self.called.emit(monitor_id, api.call_found(*argument))
                except frida.RPCException as error:
                    self.call_failed.emit(monitor_id, messages.describe(error))
            elif action == "methods":
                try:
                    self.methods_listed.emit(monitor_id, api.list_methods(argument))
                except frida.RPCException as error:
                    self.methods_failed.emit(monitor_id, messages.describe(error))
            elif action == "describe_method":
                try:
                    self.call_described.emit(monitor_id, api.describe_method(argument))
                except frida.RPCException as error:
                    self.call_failed.emit(monitor_id, messages.describe(error))
            elif action == "call_on":
                try:
                    self.called.emit(monitor_id, api.call_on(*argument))
                except frida.RPCException as error:
                    self.call_failed.emit(monitor_id, messages.describe(error))
            elif action == "text":
                try:
                    self.text_fetched.emit(monitor_id, api.full_text(argument))
                except frida.RPCException as error:
                    self.text_failed.emit(monitor_id, messages.describe(error))
        except frida.RPCException as error:
            self.monitor_failed.emit(monitor_id, messages.describe(error))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = messages.describe(
                message.get("stack", messages.text("error.method_monitor.agent_failed")))
            self._ready.set()
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready.set()
        elif kind == "calls":
            self.calls.emit(
                payload.get("events", []),
                {int(key): value for key, value in payload.get("dropped", {}).items()},
                {int(key): value for key, value in payload.get("counts", {}).items()},
            )
        elif kind == "monitor-ended":
            self.monitor_ended.emit(payload.get("monitorId", 0))
        elif kind == "agent-error":
            logs.log.error("method monitor agent: %s", messages.describe(payload.get("error", "")))
        elif kind == "monitor-error":
            self.monitor_failed.emit(payload.get("monitorId", 0),
                                     messages.describe(payload.get("error", "")))

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason
