"""Frida-backed write hooks that rewrite values right after the game stores them.

The hook targets the instruction that writes the target address (found via a
write watch: the reported trap PC is the instruction *after* the write). The
agent replaces it with a jump into a trampoline (code cave)::

    before(regs)            snapshot the old value if the write hits the target
    <relocated write>
    after(regs)             hand old/new value to the ValueTransform, store result
    <rest of the relocated bytes>
    jmp back

This works independently of how the write is encoded (``add [mem], reg``,
``mov [mem], reg``, ``sub``, ...). Several hooks may share one write site;
they run in the order they were added. Only x86-64 targets are supported.

The jump takes 5 bytes. A tiny function (``mov [rcx+8], edx; ret``) may end
before that, with the next function right behind it. With the setting
``hook_short_jump`` the agent then writes a 2-byte ``jmp rel8`` over the
write instead, to int3 padding within 128 bytes that ends at an aligned
function start, and puts the jump to the trampoline there. Off by default:
padding is found by a heuristic.

A hook either targets one fixed address or, when no address is given (e.g. a
hook restored from module + offset after the game was restarted), whatever
address the hooked instruction writes on each call.

Shared code (``add_gold(&player)`` and ``add_gold(&enemy)``) is told apart by
the caller: a fixed-address hook learns the first ``caller_depth`` return
addresses on the stack of every write that hits its address. After a restart
the address-less hook only runs for writes with one of those learned callers.
Return addresses are found by scanning the stack for values that follow a
call instruction. Without frame pointers the stack also holds stale ones,
left by calls that returned long ago (e.g. into libc or the graphics
driver), in the local variables of the functions running now. A candidate
therefore only counts

- if its call leads to the function it returns from: a direct call's target
  (or where a PLT stub or thunk jumps) must lie in the same module (or both
  in generated code), at most ``MAX_FUNCTION_SIZE`` before the address in
  the function, and not before its start where a symbol tells it. An
  indirect call (``call rax``, virtual methods) cannot be followed; it counts
  within a module, and into generated code (JIT) from anywhere;
- and if it lies above the function's frame: where the function's start is
  known (symbol or direct call), its prologue (``push``, ``sub rsp``) tells
  how much stack it took below its return address at least. With a symbol
  the scan starts right there: ``CALLER_SCAN_SLOTS`` would not reach past
  a frame with kilobytes of locals.

Learned callers that fail the first check (learned before it existed) are
dropped when the hook is installed. The same caller signature can still
show up for other addresses; the agent reports that as ambiguous.

A hook with a pointer chain (``module + base_offset``, then dereference and
add each offset) resolves it on every write and only applies to writes of the
address it leads to. That works the same after a restart, follows objects the
game allocates anew and tells player and enemy apart in shared code.

A write instruction in JIT code has no module + offset that survives a
restart. A hook on it with a pointer target is restored by training instead:
it is saved with the bytes of its write instruction and of the code around
it (``signature``), and after a restart a hardware watchpoint on the target
waits for a write from matching code and installs the hook there.

In Mono games (Unity) the agent names JIT code by its C# method: the write
site (``method`` of the write instruction, or a later ``site-method``
message once a game thread ran it) and the callers in debug events. The
``site-method`` message also identifies the method (``managed``). A hook
saved with it is installed right away after a restart: Mono compiles the
method and the agent finds the write instruction in its code. Training is
the fallback when that fails (``managed-failed``).

Anti-tamper code in some games (e.g. Arxan in Dark Souls III) repairs
patched code: the original write is back and the hook stops without an
error. Once a second the agent compares every site with the bytes it wrote
and reports a change (``site_state``: reverted, changed, repatched,
restored). With the setting ``hook_repatch`` it patches a site whose
original code is back again; off by default, as it races the game's checks.

With the setting ``hook_profiling`` the agent measures every hooked write
and reports once a second per site (``hook_profile``, also in the log): how
often the site ran and how many writes all its hooks skipped, how long a
game thread waited from entering the trampoline until its JavaScript ran
(the agent runs one thread at a time, so other threads in a hook, its
timers and garbage collection make it wait) and how long that ran, in µs
on average and at most, per callback (two per write).

A hook with ``breakpoint`` leaves the game's code untouched: a hardware
breakpoint on the write instruction takes the old value and moves to the
next instruction, where the transform runs and it moves back. Patch checks
cannot see it, but it uses one of the three debug register slots the write
watch leaves (shared with trainings) and each write traps twice.

The trampoline calls C first (``FAST_SOURCE`` in engine.js, a Frida
CModule): it computes the written address, checks the hook's switch, its
fixed target and its learned callers (its own copy of the stack scan,
taught by the JavaScript scan what each return address it met means) and
runs the built-in
manipulations (``ValueTransform.to_agent()["native"]``: multiply, freeze,
clamp, also as copies in templates) itself. JavaScript only runs for what
C cannot decide (scripts, debug mode, caller learning, pointer targets,
unknown stack layouts). The agent's JavaScript runs one thread at a time;
a game thread descheduled there stalled every other hooked thread for a
scheduler slice and dropped frames.

In debug mode a hook reports every call (values, caller, ``ctx.log()``
output, errors with stack) and every write it skipped, with the reason.
At most ``MAX_DEBUG_EVENTS`` events are sent per 100 ms; the rest is counted.
"""

import itertools
import logging
import queue
import threading
from dataclasses import dataclass, field

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs, messages
from hermes.core import sessions
from hermes.core import settings
from hermes.engines.registry import ENGINE_AGENT_SOURCE
from hermes.core.pointer import PointerChain
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import (
    BACKEND_PTRACE,
    CLEANUP_ERRORS,
    FRIDA_ERRORS,
    watch_size_for_type,
)
from hermes.hooks.transforms import ValueTransform
from hermes.agent.source import load_agent_source


MAX_CALLER_DEPTH = 4

_profile_log = logging.getLogger("hermes.hook_profile")


_AGENT_SOURCE = load_agent_source(__file__)


@dataclass
class HookSpec:
    """Where to hook and how to transform the written value.

    Pass ``trap_pc`` (the PC reported by a write watch, i.e. the instruction
    after the write), ``write_pc`` (the write instruction itself) or
    ``module`` and ``module_offset`` (the write instruction relative to its
    module, which stays valid across restarts). ``instruction_bytes`` (hex)
    makes the agent refuse to patch a module whose code has changed.

    With ``filter_address`` the transform only runs when the hooked instruction
    actually writes ``address``; disable it only when the effective address
    cannot be computed reliably. Without an ``address`` the transform applies
    to every address the instruction writes, restricted to the learned
    ``callers`` if there are any.

    ``caller_depth`` is the number of return addresses that identify a caller
    (0 disables caller learning); ``callers`` holds the learned signatures as
    lists of ``(module, offset)`` frames.

    With a ``pointer`` (PointerChain) the hook targets whatever address the
    chain leads to at each write; caller learning is not needed then.

    Instead of a code location, a ``signature`` (``{"before", "after"}``: hex
    code bytes around the write instruction ``instruction_bytes``) together
    with a ``pointer`` lets the agent train: it installs the hook at the first
    write to the target from matching code.

    With ``breakpoint`` the write is hooked by a hardware breakpoint instead
    of a jump patched into the code (see the module docstring).

    ``managed`` (``{image, file, token, method, offset}`` from the agent's
    ``site-method`` message) names the C# method of a write site in Mono JIT
    code: the agent lets Mono compile it and looks for ``instruction_bytes``
    in its code, preferring the code around it given by ``signature``. If
    that fails it falls back to training with the ``signature``.
    """

    # pylint: disable=too-many-instance-attributes
    address: int | None
    value_type: str
    transform: ValueTransform
    trap_pc: int | None = None
    write_pc: int | None = None
    module: str | None = None
    module_offset: int | None = None
    instruction_bytes: str | None = None
    filter_address: bool = True
    enabled: bool = True
    caller_depth: int = 1
    callers: list = field(default_factory=list)
    pointer: PointerChain | None = None
    signature: dict | None = None
    managed: dict | None = None
    breakpoint: bool = False
    label: str = field(default="", compare=False)

    @property
    def training(self):
        """Whether the agent has to find the write instruction first."""
        return (self.signature is not None and self.trap_pc is None
                and self.write_pc is None and self.module is None)

    def __post_init__(self):
        watch_size_for_type(self.value_type)
        if not isinstance(self.transform, ValueTransform):
            raise ValueError("transform must be a ValueTransform instance")
        if self.training:
            if self.pointer is None or not self.instruction_bytes:
                raise ValueError("training needs a pointer and the instruction bytes")
        elif (self.trap_pc is None and self.write_pc is None and self.module is None
              and self.managed is None):
            raise ValueError(
                "HookSpec needs a trap_pc, a write_pc, a module, a method or a signature"
            )
        if (self.managed is not None and not self.instruction_bytes
                and self.trap_pc is None and self.write_pc is None):
            raise ValueError("a method location needs the instruction bytes")
        if (self.module is None) != (self.module_offset is None):
            raise ValueError("module and module_offset must be given together")
        if (self.address is None and self.write_pc is None and self.module is None
                and self.managed is None and not self.training):
            raise ValueError("a trap_pc can only be resolved with an address")
        if not 0 <= self.caller_depth <= MAX_CALLER_DEPTH:
            raise ValueError(f"caller_depth must be between 0 and {MAX_CALLER_DEPTH}")
        if any(len(frames) != self.caller_depth for frames in self.callers):
            raise ValueError("every caller signature needs caller_depth frames")

    def to_agent(self, hook_id):
        return {
            "id": hook_id,
            "address": None if self.address is None else hex(self.address),
            "valueType": self.value_type,
            "trapPc": None if self.trap_pc is None else hex(self.trap_pc),
            "writePc": None if self.write_pc is None else hex(self.write_pc),
            "module": self.module,
            "moduleOffset": self.module_offset,
            "instructionBytes": self.instruction_bytes,
            "filterAddress": self.filter_address,
            "enabled": self.enabled,
            "callerDepth": self.caller_depth,
            "callers": [[list(frame) for frame in frames] for frames in self.callers],
            "pointer": None if self.pointer is None else {
                "module": self.pointer.module,
                "baseOffset": self.pointer.base_offset,
                "offsets": list(self.pointer.offsets),
            },
            "signature": self.signature,
            "managed": self.managed,
            "breakpoint": self.breakpoint,
            "transform": self.transform.to_agent(),
        }


def build_agent_source():
    return ENGINE_AGENT_SOURCE + _AGENT_SOURCE


def create_hook_worker(backend, pid, parent=None) -> QThread:
    """Build the hook worker for ``backend`` (``frida`` or ``ptrace``).

    Both workers share the same signals and public methods, so the panel treats
    them alike. The ptrace backend (Option A) needs no injection and works on
    Wine/Proton, but only rewrites values at a data watchpoint; see
    :mod:`hermes.hooks.ptrace_hook` for what it does not cover.
    """
    if backend == BACKEND_PTRACE:
        from hermes.hooks.ptrace_hook import (  # pylint: disable=import-outside-toplevel
            PtraceHookWorker,
        )
        return PtraceHookWorker(pid, parent)
    return HookWorker(pid, parent)


class HookWorker(QThread):
    """Keep a Frida session open and manage value hooks inside one process.

    The public methods are safe to call from the GUI thread: they enqueue a
    command that the worker thread executes against the agent. Results come
    back through the signals.
    """

    session_ready = Signal(str)
    hook_installed = Signal(int, object)
    # hook id, training state: resolved (target found), waiting (for a free
    # watchpoint), seen and rejected writes; empty when the training starts
    hook_training = Signal(int, object)
    hook_failed = Signal(int, str)
    hook_removed = Signal(int)
    hook_updated = Signal(int)
    hook_events = Signal(object)
    transform_error = Signal(int, str)
    # hook id, learned caller signatures (frames are [module, offset] or None)
    callers_learned = Signal(int, object)
    callers_ambiguous = Signal(int)
    # hook id, number of learned callers dropped as stale return addresses
    callers_dropped = Signal(int, int)
    # hook ids sharing a write site in JIT code, its C# method (Mono games)
    # and what finds it again after a restart (HookSpec.managed)
    site_method = Signal(object, str, object)
    # hook id, why its C# method did not lead to the write site; it trains
    managed_failed = Signal(int, str)
    # hook ids sharing a write site, its state (reverted, changed, repatched,
    # restored), how often the game reverted it and why patching it again
    # failed ("" if it did not)
    site_state = Signal(object, str, int, str)
    # timings per write site, once a second with hook_profiling: dicts with
    # hookIds, calls, skipped, waitAvg, waitMax, runAvg, runMax (µs)
    hook_profile = Signal(object)
    # debug events of hooks in debug mode, events dropped per hook id
    hook_debug = Signal(object, object)
    # ctx.log lines of scripts (also outside debug mode): [{hookId, time, text}],
    # lines dropped per hook id (more than the agent sends at once)
    hook_logs = Signal(object, object)
    # console.log output of the agent and its transforms: level, text
    agent_log = Signal(str, str)
    agent_warning = Signal(str)
    worker_failed = Signal(str)
    worker_stopped = Signal()

    # Shared by all workers so ids stay unique when a worker is replaced.
    _ids = itertools.count(1)

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready_event = threading.Event()
        self._ready_payload = None
        # (start, end) of the agent's own memory-access code, once it is ready
        self.access_stub = None
        # (start, end) of the agent's C code, which reads and writes targets too
        self.fast_code = None
        self._agent_error = None
        self._detach_reason = None

    def add_hook(self, spec):
        """Queue a hook and return its id; expect hook_installed or hook_failed."""
        hook_id = next(self._ids)
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

    def set_trainings_paused(self, paused, timeout=2.0):
        """Pause or resume all trainings; wait until the agent did it.

        Trainings must rest while a write watch runs in the process.
        Returns False if the agent did not answer in time; with a
        ``timeout`` of None the command is only queued.
        """
        done = threading.Event()
        self._commands.put(("training-paused", 0, (bool(paused), done)))
        return True if timeout is None else done.wait(timeout)

    def set_debug(self, hook_id, enabled):
        """Report every call and skipped write of a hook via hook_debug."""
        self._commands.put(("debug", hook_id, bool(enabled)))

    @sessions.tracked(sessions.HOOKS)
    def run(self):
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", self._on_session_detached)
            script = session.create_script(build_agent_source())
            script.on("message", self._on_message)
            script.set_log_handler(self._on_log)
            script.load()

            if not self._ready_event.wait(5):
                raise messages.LocalizedRuntimeError("error.hooks.load_timeout")
            if self._agent_error:
                raise RuntimeError(self._agent_error)
            ready = self._ready_payload or {}
            if ready.get("accessStub"):
                start = int(ready["accessStub"], 16)
                self.access_stub = (start, start + ready.get("accessStubSize", 0x1000))
            if ready.get("fastCode"):
                start = int(ready["fastCode"], 16)
                self.fast_code = (start, start + ready.get("fastCodeSize", 0x1000))
            script.exports_sync.set_repatch(settings.get("hook_repatch"))
            script.exports_sync.set_profiling(settings.get("hook_profiling"))
            settings.subscribe(self._on_settings_changed)
            self.session_ready.emit(ready.get("arch", ""))

            while (not self.isInterruptionRequested()
                   and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    command = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._execute(script, *command)

            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise messages.LocalizedRuntimeError("error.agent.detached",
                                                     reason=self._detach_reason)
        except FRIDA_ERRORS + (ImportError, OSError, RuntimeError, ValueError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(messages.describe(error))
        finally:
            settings.unsubscribe(self._on_settings_changed)
            if script is not None:
                try:
                    disposed = script.exports_sync.dispose()
                    inside = disposed.get("threadsInside", 0) if isinstance(disposed, dict) else 0
                    if inside:
                        logs.log.warning("Unloading the hook agent with %d game thread(s) "
                                         "still counted in a trampoline", inside)
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
            self.worker_stopped.emit()

    def _execute(self, script, action, hook_id, argument):
        api = script.exports_sync
        try:
            if action == "add":
                payload = argument.to_agent(hook_id)
                payload["shortJump"] = settings.get("hook_short_jump")
                info = api.add_hook(payload)
                if info.get("training"):
                    self.hook_training.emit(hook_id, {})
                else:
                    info["label"] = argument.label
                    info["transformDescription"] = argument.transform.describe()
                    self.hook_installed.emit(hook_id, info)
            elif action == "remove":
                if api.remove_hook(hook_id):
                    self.hook_removed.emit(hook_id)
            elif action == "transform":
                api.set_transform(hook_id, argument.to_agent())
                self.hook_updated.emit(hook_id)
            elif action == "enabled":
                api.set_enabled(hook_id, argument)
                self.hook_updated.emit(hook_id)
            elif action == "training-paused":
                paused, done = argument
                try:
                    api.set_training_paused(paused)
                finally:
                    done.set()
            elif action == "debug":
                api.set_debug(hook_id, argument)
                self.hook_updated.emit(hook_id)
            elif action == "repatch":
                api.set_repatch(argument)
            elif action == "profiling":
                api.set_profiling(argument)
        except frida.RPCException as error:
            self.hook_failed.emit(hook_id, messages.describe(error))

    def _on_settings_changed(self, changed):
        if "hook_repatch" in changed:
            self._commands.put(("repatch", 0, changed["hook_repatch"]))
        if "hook_profiling" in changed:
            self._commands.put(("profiling", 0, changed["hook_profiling"]))

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = messages.describe(
                message.get("stack", messages.text("error.hooks.agent_failed")))
            self._ready_event.set()
            return

        if message.get("type") != "send":
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready_payload = payload
            self._ready_event.set()
        elif kind == "events":
            self.hook_events.emit(payload.get("events", []))
        elif kind == "training":
            for event in payload.get("events", []):
                self.hook_training.emit(event.get("hookId", 0), event)
        elif kind == "trained":
            self.hook_installed.emit(payload.get("hookId", 0), payload.get("info", {}))
        elif kind == "training-failed":
            self.hook_failed.emit(payload.get("hookId", 0),
                                  messages.describe(payload.get("error", "")))
        elif kind == "agent-error":
            self.agent_warning.emit(messages.describe(payload.get("error", "")))
        elif kind == "debug":
            self.hook_debug.emit(
                payload.get("events", []),
                {int(hook_id): count for hook_id, count in payload.get("dropped", {}).items()},
            )
        elif kind == "hook-logs":
            self.hook_logs.emit(
                payload.get("lines", []),
                {int(hook_id): count for hook_id, count in payload.get("dropped", {}).items()},
            )
        elif kind == "callers":
            self.callers_learned.emit(
                payload.get("hookId", 0), payload.get("callers", [])
            )
        elif kind == "site-method":
            self.site_method.emit(
                payload.get("hookIds", []), payload.get("method", ""), payload.get("managed")
            )
        elif kind == "managed-failed":
            self.managed_failed.emit(payload.get("hookId", 0),
                                     messages.describe(payload.get("error", "")))
        elif kind == "profile":
            sites = payload.get("sites", [])
            for site in sites:
                _profile_log.info(
                    "hooks %s: %d calls/s, %d skipped, %d in JS; wait avg %.1f max %.1f us; "
                    "run avg %.1f max %.1f us", site.get("hookIds"), site.get("calls", 0),
                    site.get("skipped", 0), site.get("js", 0), site.get("waitAvg", 0),
                    site.get("waitMax", 0), site.get("runAvg", 0), site.get("runMax", 0))
            self.hook_profile.emit(sites)
        elif kind == "site-state":
            self.site_state.emit(
                payload.get("hookIds", []), payload.get("state", ""),
                payload.get("reverts", 0), messages.describe(payload.get("error") or ""),
            )
        elif kind == "callers-ambiguous":
            self.callers_ambiguous.emit(payload.get("hookId", 0))
        elif kind == "callers-dropped":
            self.callers_dropped.emit(payload.get("hookId", 0), payload.get("count", 0))
        elif kind == "transform-error":
            self.transform_error.emit(
                payload.get("hookId", 0), messages.describe(payload.get("error", ""))
            )

    def _on_log(self, level, text):
        logs.agent_log("hook agent", level, text)
        self.agent_log.emit(level, text)

    def _on_session_detached(self, reason, _crash):
        self._detach_reason = reason
