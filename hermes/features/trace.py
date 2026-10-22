"""Trace: which C# methods a scenario in a game runs (e.g. Unity with Mono).

It works through the engine adapter of the game (see hermes/engines). While
it adds or removes its hooks, the game's threads are paused where the
platform needs it (Wine, see ``pauseThreads``).

The trace hooks many methods at once, e.g. all methods of the game's
assemblies, to find the one worth monitoring or hooking:

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
import threading
from dataclasses import dataclass

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import activity, logs, settings
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE, resolve_engine
from hermes.features.catalog import CatalogMethod
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS
from hermes.core.agent_source import load_agent_source


# Calls of the scenario written into the sequence by default (setting
# trace_sequence); later ones are only counted.
EVENT_CAPACITY = 100_000

# A method ran "clearly more often" in the scenario than in the baseline when
# it ran this many times as often as expected, and at least MIN_EXTRA_CALLS
# times more.
MORE_FACTOR = 3.0
MIN_EXTRA_CALLS = 3

NEW, MORE, SAME = "new", "more", "same"

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
    progress = pyqtSignal(str, int, int, str)
    # recording of the phase ("baseline", "scenario") has begun
    began = pyqtSignal(str)
    # {phase, calls, methods, events} while recording
    live = pyqtSignal(dict)
    # a ``finish`` answer: {phase, duration, counts, events, dropped}
    recorded = pyqtSignal(dict)
    stopped = pyqtSignal()
    # a command failed: what was asked, message
    command_failed = pyqtSignal(str, str)
    worker_failed = pyqtSignal(str)

    # What failed to load, for the error message
    agent_name = "trace"

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
                raise RuntimeError(f"Target process detached: {self._detach_reason}")
        except FRIDA_ERRORS + (OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(str(error))
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
            raise RuntimeError(f"Timed out while loading the {self.agent_name}")

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
            self.command_failed.emit(action, str(error).split("\n", 1)[0])

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
            self._agent_error = message.get("stack", f"{self.agent_name} agent failed")
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
            logs.log.error("%s agent: %s", self.agent_name, payload.get("error", ""))
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
    planned = pyqtSignal(dict)
    # every method a trace could hook (CatalogMethods), {abstract, native, generic}
    candidates_found = pyqtSignal(list, dict)
    # CatalogMethods in index order, {skipped: {abstract, native, generic, com},
    # notCompiled, failed}
    started = pyqtSignal(list, dict)
    # the game freed traced code (e.g. while exiting); the trace was removed
    ended = pyqtSignal()

    agent_name = "Mono trace"

    def __init__(self, pid, parent=None, engine=None):
        super().__init__(pid, parent)
        self.engine = resolve_engine(pid, engine)

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

    def _spec(self, api, argument):
        """The agent's spec: words, or {"methods": [[assembly, token], ...]}."""
        if isinstance(argument, dict):
            methods = argument["methods"]
            return {"assemblies": sorted({assembly for assembly, _token in methods}),
                    "words": [word.lower() for word in argument.get("words", [])],
                    "methods": methods}
        names = [a["name"] for a in api.assemblies()
                 if not self.engine.is_framework_assembly(a["name"])]
        return {"assemblies": names, "words": [word.lower() for word in argument]}

    def _run_command(self, api, action, argument):
        if action == "plan":
            spec = self._spec(api, argument)
            self.planned.emit({**api.plan(spec), "assemblies": spec["assemblies"]})
        elif action == "candidates":
            answer = api.candidates(self._spec(api, argument))
            self.candidates_found.emit(trace_methods(answer["methods"], self.engine.id),
                                       answer["skipped"])
        elif action == "start":
            answer = api.start(self._spec(api, argument))
            self.started.emit(trace_methods(answer.pop("methods"), self.engine.id), answer)
        else:
            super()._run_command(api, action, argument)

    def _on_payload(self, kind, payload):
        if kind == "trace-ended":
            self.ended.emit()
