"""Trace: which C# methods a scenario in a game runs (e.g. Unity with Mono).

It works through the engine adapter of the game (see hermes/engines). While
it adds or removes its hooks, the game's threads are paused where the
platform needs it (Wine, see ``pauseThreads``).

The trace hooks many methods at once, e.g. all methods of the game's
assemblies, to find the one worth monitoring or hooking. It chooses them by
the catalog (see hermes.features.catalog_cache), so once the assemblies are
cached, listing and filtering them needs no reading in the game:

1. ``start`` compiles the chosen methods (like the method monitor does) and
   puts a Frida interceptor on each. The interceptors call a small C
   function (a Frida CModule) rather than JavaScript: it only counts the call
   and, while a scenario is recorded, notes method, thread, return address
   and time in a buffer. That keeps thousands of hooks affordable.
2. ``begin("baseline")`` … ``finish()`` records the noise: what the game
   calls anyway while one does nothing (Update methods, UI, AI …).
3. ``begin("scenario")`` … ``finish()`` records while one does the thing of
   interest in the game. Calls of methods seen in the baseline are only
   counted, not written into the call sequence.

``analyze`` then keeps the methods that are new in the scenario or ran
clearly more often than in the baseline, in the order they were first
called. Only the entry of a method is hooked, so Frida leaves return
addresses alone: exceptions pass through traced methods as usual.

Like the method monitor, the trace notices when Mono frees traced code
(Unity does so on exit) and removes all its interceptors first.
"""

import queue
import re
import threading
from dataclasses import dataclass

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import activity, logs, messages, settings
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE, resolve_engine
from hermes.features import catalog_cache, games
from hermes.features.assemblies import chosen_assemblies
from hermes.features.catalog import CatalogMethod
from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
from hermes.agent.source import load_agent_source


# Calls of the scenario written into the sequence by default (setting
# trace_sequence); later ones are only counted.
EVENT_CAPACITY = 100_000

# A method ran "clearly more often" in the scenario than in the baseline when
# it ran this many times as often as expected, and at least MIN_EXTRA_CALLS
# times more.
MORE_FACTOR = 3.0
MIN_EXTRA_CALLS = 3

NEW, MORE, SAME = "new", "more", "same"

# Tokens per message choosing the methods: a message to Frida's gadget under
# Wine must stay below 128 KiB.
CHOOSE_PART = 4000

# ECMA-335 method flags, as the catalog has them
METHOD_ABSTRACT = 0x0400
METHOD_PINVOKE = 0x2000
IMPL_CODE_TYPE = 0x0003  # native or runtime (delegates) instead of IL
IMPL_INTERNAL_CALL = 0x1000
# ``Ns.List`1:Add``: a method of a generic class's definition
_GENERIC_CLASS = re.compile(r"`\d+:")

_AGENT_TEMPLATE = ENGINE_AGENT_SOURCE + load_agent_source(__file__)


def build_agent_source(event_capacity=EVENT_CAPACITY):
    return _AGENT_TEMPLATE.replace("EVENT_CAPACITY_VALUE", str(event_capacity))


def trace_methods(rows, engine="mono"):
    """CatalogMethods of the agent's ``start`` answer, in index order."""
    return [
        CatalogMethod(assembly, class_name, name, parameters, return_type, bool(static), token,
                      signature, engine)
        for assembly, class_name, name, parameters, return_type, static, token, signature in rows
    ]


def selection_parts(chosen):
    """``[[assembly, [token, ...]], ...]`` parts of ``[[assembly, token], ...]``.

    Each part holds at most CHOOSE_PART tokens; the agent's ``choose`` takes them.
    """
    parts, part, size = [], {}, 0
    for assembly, token in chosen:
        if size == CHOOSE_PART:
            parts.append([[name, tokens] for name, tokens in part.items()])
            part, size = {}, 0
        part.setdefault(assembly, []).append(token)
        size += 1
    if part:
        parts.append([[name, tokens] for name, tokens in part.items()])
    return parts


def untraceable(class_name, name, signature, flags, impl_flags):
    """Why a method cannot be traced (``"abstract"``, ``"native"``, ``"generic"``), else None."""
    if flags & METHOD_ABSTRACT:
        return "abstract"
    if flags & METHOD_PINVOKE or impl_flags & (IMPL_CODE_TYPE | IMPL_INTERNAL_CALL):
        return "native"
    # Open generics have no code of their own: List`1, Method<T>; Mono names
    # the class of a definition 'Ns.List<T>', its methods 'Ns.List`1:Add (T)'.
    head = signature.split(" (", 1)[0]
    if ("`" in class_name or _GENERIC_CLASS.search(head)
            or head[head.rfind(":") + 1:] != name):
        return "generic"
    return None


def select_methods(classes_of, words, engine="mono"):
    """The methods of ``classes_of`` matching ``words`` that a trace can hook.

    ``classes_of`` maps assemblies to their catalog ``classes`` answer (with
    the flags). Returns the CatalogMethods and how many matching methods
    cannot be traced: ``{abstract, native, generic}``.
    """
    words = [word.lower() for word in words]
    chosen = []
    skipped = {"abstract": 0, "native": 0, "generic": 0}
    for assembly, classes in classes_of.items():
        for class_name, methods in classes:
            for entry in methods:
                (name, parameters, return_type, static, token, signature, flags, impl_flags,
                 *names) = entry
                text = f"{class_name}:{name} {assembly}".lower()
                if not all(word in text for word in words):
                    continue
                reason = untraceable(class_name, name, signature, flags, impl_flags)
                if reason is not None:
                    skipped[reason] += 1
                else:
                    # Mono adds parameter names; older cache entries have none
                    chosen.append(CatalogMethod(assembly, class_name, name, parameters,
                                                return_type, bool(static), token, signature,
                                                engine, tuple(names[0]) if names else ()))
    return chosen, skipped


@dataclass(frozen=True)
class TraceCall:
    """One call of the scenario."""

    method: int
    thread: int
    # microseconds since the scenario began
    time: int
    # where it was called from: ``Namespace.Class:Method+0x1a`` or ``module+0x…``
    caller: str


@dataclass(frozen=True)
class TraceRow:
    """A method the scenario called, compared with the baseline."""

    method: int
    calls: int
    # calls per second in the scenario, and in the baseline (None: no baseline)
    rate: float
    baseline_rate: float | None
    verdict: str
    # position of its first call in the sequence (None: not in the sequence)
    first: int | None

    @property
    def notable(self):
        return self.verdict in (NEW, MORE)

    @property
    def factor(self):
        """How many times as often as in the baseline, or None."""
        if not self.baseline_rate:
            return None
        return self.rate / self.baseline_rate


def recorded_summary(result):
    """What a ``finish`` answer holds, in numbers, for the activity log."""
    counts = result.get("counts", {})
    summary = {"phase": result.get("phase"), "duration_ms": result.get("duration"),
               "functions": len(counts), "calls": sum(counts.values()),
               "events": len(result.get("events", [])), "dropped": result.get("dropped", 0)}
    for key in ("lost", "saturated", "waiting"):
        if result.get(key) is not None:
            summary[key] = result[key]
    return summary


def recording_calls(result):
    """TraceCalls of a ``finish`` answer of the scenario."""
    return [TraceCall(method, thread, time, caller or "")
            for method, thread, time, caller in result.get("events", [])]


def _counts(result):
    return {int(key): value for key, value in result.get("counts", {}).items()}


def analyze(scenario, baseline=None):
    """TraceRows of the methods ``scenario`` called (``finish`` answers).

    Without a baseline every method counts as new. Rows of methods in the
    call sequence come first, in the order they were first called; the
    others (only counted, since the baseline had them) follow, the most
    increased first.
    """
    seconds = max(scenario.get("duration", 0), 1) / 1000
    base_counts = _counts(baseline) if baseline else {}
    base_seconds = max(baseline.get("duration", 0), 1) / 1000 if baseline else None
    first = {}
    for position, (method, *_rest) in enumerate(scenario.get("events", [])):
        first.setdefault(method, position)
    rows = []
    for method, calls in _counts(scenario).items():
        base_calls = base_counts.get(method, 0)
        baseline_rate = None if baseline is None else base_calls / base_seconds
        if not base_calls:
            verdict = NEW
        else:
            expected = baseline_rate * seconds
            verdict = (MORE if calls >= MORE_FACTOR * expected
                       and calls - expected >= MIN_EXTRA_CALLS else SAME)
        rows.append(TraceRow(method, calls, calls / seconds, baseline_rate, verdict,
                             first.get(method)))
    rows.sort(key=lambda row: (
        row.first is None,
        row.first if row.first is not None else 0,
        {NEW: 0, MORE: 1, SAME: 2}[row.verdict],
        -(row.factor or 0),
        -row.calls,
    ))
    return rows


def calls_filter(text):
    """A test of call counts for ``text``, or None to keep every count.

    Alternatives are separated by commas: ``3`` exactly, ``3-10`` a range,
    ``>5``, ``>=5``, ``<5``, ``<=5`` bounds, ``%3`` multiples. A scenario
    repeated three times shows its functions with 3, 6, 9 … calls: ``%3``.
    Raises ValueError for text that is none of these.
    """
    tests = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if part.startswith("%"):
            divisor = int(part[1:])
            if divisor <= 0:
                raise ValueError(part)
            tests.append(lambda calls, d=divisor: calls % d == 0)
        elif part.startswith((">=", "<=")):
            bound = int(part[2:])
            tests.append((lambda calls, b=bound: calls >= b) if part[0] == ">"
                         else (lambda calls, b=bound: calls <= b))
        elif part.startswith((">", "<")):
            bound = int(part[1:])
            tests.append((lambda calls, b=bound: calls > b) if part[0] == ">"
                         else (lambda calls, b=bound: calls < b))
        elif "-" in part:
            low, high = (int(value) for value in part.split("-", 1))
            tests.append(lambda calls, lo=low, hi=high: lo <= calls <= hi)
        else:
            exact = int(part)
            tests.append(lambda calls, e=exact: calls == e)
    if not tests:
        return None
    return lambda calls: any(test(calls) for test in tests)


class AgentWorker(QThread):
    """Keep a Frida session with one agent open and run commands on it.

    The base of the traces: the commands queue up from the GUI thread and run
    one after another as the agent's RPCs on the worker's thread (see
    ``_execute``); the agent's messages turn into signals (see ``_on_payload``).
    Subclasses decorate ``run`` with ``sessions.tracked`` and call
    ``_run_agent``.
    """

    # step, done, total, item at hand (e.g. the method Mono compiles)
    progress = Signal(str, int, int, str)
    # recording of the phase ("baseline", "scenario") has begun
    began = Signal(str)
    # {phase, calls, methods, events} while recording
    live = Signal(object)
    # a ``finish`` answer: {phase, duration, counts, events, dropped}
    recorded = Signal(object)
    stopped = Signal()
    # a command failed: what was asked, message
    command_failed = Signal(str, str)
    worker_failed = Signal(str)

    # What failed to load, for the log
    agent_name = "trace"
    # Its error messages: error.<agent_key>.load_timeout and .agent_failed
    agent_key = "trace"

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._agent_error = None
        self._detach_reason = None
        self._script = None
        self._session = None

    def agent_source(self):
        raise NotImplementedError

    def begin(self, phase):
        """Record ``"baseline"`` or ``"scenario"`` until ``finish``."""
        self._commands.put(("begin", phase))

    def finish(self):
        self._commands.put(("finish", None))

    def stop(self):
        """Remove the trace; the session stays open for another one."""
        self._commands.put(("stop", None))

    def cancel(self):
        """Stop preparing a trace as soon as possible.

        A message rather than a command: the worker is busy with the start
        until then.
        """
        script = self._script
        if script is not None:
            try:
                script.post({"type": "cancel"})
            except CLEANUP_ERRORS as error:
                logs.cleanup_failed(error)

    def requestInterruption(self):  # pylint: disable=invalid-name
        self.cancel()
        super().requestInterruption()

    def _run_agent(self):
        session = None
        try:
            session = attach_session(self.pid)
            self._session = session
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", self._on_detached)
            self._load_script(session)
            while (not self.isInterruptionRequested() and self._detach_reason is None
                   and self._agent_error is None):
                try:
                    action, argument = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                if self._script is None:
                    break  # a fresh agent failed to load
                self._execute(self._script.exports_sync, action, argument)
            if self._agent_error and not self.isInterruptionRequested():
                raise RuntimeError(self._agent_error)
            if self._detach_reason and not self.isInterruptionRequested():
                raise messages.LocalizedRuntimeError("error.agent.detached",
                                                     reason=self._detach_reason)
        except FRIDA_ERRORS + (OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(messages.describe(error))
        finally:
            self._unload_script()
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)

    def _load_script(self, session):
        self._ready.clear()
        script = session.create_script(self.agent_source())
        script.on("message", self._on_message)
        script.load()
        self._script = script
        if not self._ready.wait(5):
            raise messages.LocalizedRuntimeError(f"error.{self.agent_key}.load_timeout")

    def _unload_script(self):
        script = self._script
        self._script = None
        if script is None:
            return
        try:
            script.exports_sync.dispose()
        except CLEANUP_ERRORS as error:
            logs.cleanup_failed(error)
        try:
            script.unload()
        except CLEANUP_ERRORS as error:
            logs.cleanup_failed(error)

    def _execute(self, api, action, argument):
        try:
            self._run_command(api, action, argument)
        except frida.RPCException as error:
            # Without the agent's stack trace
            self.command_failed.emit(action, messages.describe(error))

    def _run_command(self, api, action, argument):
        if action == "begin":
            api.begin(argument)
            self.began.emit(argument)
        elif action == "finish":
            result = api.finish()
            activity.record("trace_recorded", **recorded_summary(result))
            self.recorded.emit(result)
        elif action == "stop":
            api.stop()
            self.stopped.emit()

    def _on_message(self, message, _data):
        if message.get("type") == "error":
            self._agent_error = messages.describe(
                message.get("stack", messages.text(f"error.{self.agent_key}.agent_failed")))
            self._ready.set()
            return
        payload = message.get("payload", {})
        kind = payload.get("kind")
        if kind == "ready":
            self._ready.set()
        elif kind == "progress":
            self.progress.emit(payload.get("step", ""), payload.get("done", 0),
                               payload.get("total", 0), payload.get("current", ""))
        elif kind == "live":
            self.live.emit(payload)
        elif kind == "agent-error":
            logs.log.error("%s agent: %s", self.agent_name,
                           messages.describe(payload.get("error", "")))
        else:
            self._on_payload(kind, payload)

    def _on_payload(self, kind, payload):
        """A message of the agent the base does not know."""

    def _on_detached(self, reason, _crash):
        self._detach_reason = reason


class TraceWorker(AgentWorker):
    """Keep a Frida session open for a trace; see the module documentation.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # {methods, skipped: {abstract, native, generic}, assemblies}
    planned = Signal(object)
    # every method a trace could hook (CatalogMethods), {abstract, native, generic}
    candidates_found = Signal(object, object)
    # CatalogMethods in index order, {skipped: {abstract, native, generic, com},
    # notCompiled, failed}
    started = Signal(object, object)
    # the game freed traced code (e.g. while exiting); the trace was removed
    ended = Signal()

    agent_name = "Mono trace"
    agent_key = "mono_trace"

    def __init__(self, pid, parent=None, engine=None, selection=()):
        super().__init__(pid, parent)
        self.engine = resolve_engine(pid, engine)
        # The user's assembly selection (see hermes.features.assemblies)
        self.selection = frozenset(selection)

    def set_selection(self, selection):
        """Trace only these assemblies from now on; none means all of the game."""
        self.selection = frozenset(selection)

    def agent_source(self):
        return build_agent_source(settings.get("trace_sequence"))

    def plan(self, words):
        """Count the methods a trace of ``words`` would hook; expect planned."""
        self._commands.put(("plan", list(words)))

    def trace(self, words):
        """Hook the methods of the game's assemblies matching ``words``."""
        self._commands.put(("start", list(words)))

    def candidates(self):
        """List every method a trace could hook; expect candidates_found."""
        self._commands.put(("candidates", []))

    def trace_selection(self, methods, words=()):
        """Hook exactly ``methods`` (CatalogMethods, e.g. from candidates_found).

        ``words`` (the free-text filter they were chosen with) only make the
        skipped methods count like ``trace`` counts them.
        """
        self._commands.put(("start", {"methods": [[m.assembly, m.token] for m in methods],
                                      "words": list(words)}))

    @sessions.tracked(sessions.TRACE)
    def run(self):
        self._run_agent()

    def _select(self, api, words):
        """The assemblies a trace covers, their traceable methods matching
        ``words`` and the count of the others (see select_methods).

        Assemblies come from the catalog cache, else from the agent.
        """
        found = api.assemblies()
        games.remember(self.pid, self.engine.id, found)
        mvids = {a["name"]: a.get("mvid") for a in found}
        names = chosen_assemblies(list(mvids), self.selection, self.engine)
        classes_of = {}
        for done, name in enumerate(names):
            self.progress.emit("catalog", done, len(names), name)
            classes_of[name] = catalog_cache.read_classes(
                self.engine.id, name, mvids[name], lambda name=name: api.classes(name))[0]
        self.progress.emit("catalog", len(names), len(names), "")
        return (names, *select_methods(classes_of, words, self.engine.id))

    def _run_command(self, api, action, argument):
        if action == "plan":
            names, methods, skipped = self._select(api, argument)
            self.planned.emit({"methods": len(methods), "skipped": skipped,
                               "assemblies": names})
        elif action == "candidates":
            _names, methods, skipped = self._select(api, argument)
            self.candidates_found.emit(methods, skipped)
        elif action == "start":
            # words, or {"methods": [[assembly, token], ...], "words": [...]}
            words = argument["words"] if isinstance(argument, dict) else argument
            _names, methods, skipped = self._select(api, words)
            chosen = (argument["methods"] if isinstance(argument, dict)
                      else [[method.assembly, method.token] for method in methods])
            for part in selection_parts(chosen):
                api.choose(part)
            answer = api.start({"skipped": skipped})
            self.started.emit(trace_methods(answer.pop("methods"), self.engine.id), answer)
        else:
            super()._run_command(api, action, argument)

    def _on_payload(self, kind, payload):
        if kind == "trace-ended":
            self.ended.emit()
