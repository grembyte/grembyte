"""Native trace: which functions of a game's machine code a scenario runs.

The native counterpart of the Mono trace (trace.py), for games without
metadata: C++, own engines, Unreal, Godot, Source. Instead of hooking
methods it follows the game's threads with Frida's Stalker, which recompiles
the code they run and so sees every call, wherever it goes. It works like
Cheat Engine's "code filter":

1. ``inventory`` lists the threads and modules. ``start`` follows the chosen
   threads; the modules not chosen (system libraries, drivers) run natively,
   only the calls into them are seen. That keeps the game playable.
2. ``begin("baseline")`` … ``finish()`` counts the calls of every function
   while nothing happens in the game, ``begin("scenario")`` … ``finish()``
   while one does the thing of interest.

The answers have the shape of the Mono trace's, so ``analyze`` compares them
the same way: functions are numbered as the agent first sees them, the
numbers stay the same for the whole trace. A CModule counts the calls: its
Stalker ``transform`` puts a callout before every call instruction of the
followed code, which works out the target from the registers and counts it
in the game's thread. The sequence therefore holds the first call of every
function (not seen in the baseline) per thread in the exact order, with the
call instruction as the caller.

The table of functions holds ``FUNCTION_CAPACITY`` of them; calls of
functions beyond it are lost, the answers count them in ``lost``.
"""

from dataclasses import dataclass

from PySide6.QtCore import Signal  # pylint: disable=import-error,no-name-in-module

import sys
import threading

import frida

from grembyte.core import activity, messages, sessions, settings
from grembyte.platform import is_wine_process, read_mappings
from grembyte.features.trace import AgentWorker
from grembyte.agent.platform import PLATFORM_AGENT_SOURCE
from grembyte.agent.source import load_agent_source


# Functions the CModule's table can hold (a power of two; 17 bytes each).
FUNCTION_CAPACITY = 1 << 19
# (function, thread) pairs it notes first calls of at least (a power of two;
# 12 bytes each); twice the sequence's capacity when that is larger
FIRST_CAPACITY = 1 << 19
# First calls written into the sequence by default (setting trace_sequence);
# later ones are only counted.
EVENT_CAPACITY = 100_000
# The agent's answer when a trace needs a fresh agent (FRESH_AGENT in the agent).
FRESH_AGENT = "needs a fresh agent"
# How long ``finish`` may look up function names.
SYMBOL_BUDGET_MS = 3000
# How long the agent measures the threads' load under Wine (seconds), as
# thread_activity does on Linux.
WINE_LOAD_SECONDS = 0.5

_AGENT_TEMPLATE = PLATFORM_AGENT_SOURCE + (
    load_agent_source(__file__)
    .replace("FUNCTION_CAPACITY_VALUE", str(FUNCTION_CAPACITY))
    .replace("SYMBOL_BUDGET_VALUE", str(SYMBOL_BUDGET_MS))
)


def first_capacity(event_capacity):
    """Entries of the table of first calls for a sequence of ``event_capacity``."""
    capacity = FIRST_CAPACITY
    while capacity < 2 * event_capacity:
        capacity *= 2
    return capacity


def build_agent_source(event_capacity=EVENT_CAPACITY):
    return (_AGENT_TEMPLATE
            .replace("FIRST_CAPACITY_VALUE", str(first_capacity(event_capacity)))
            .replace("EVENT_CAPACITY_VALUE", str(event_capacity)))


@dataclass
class NativeFunction:
    """A function the trace saw called: an address, by module where it has one."""

    address: int
    # "" for code outside any module (e.g. generated at run time)
    module: str
    offset: int | None
    # an export or symbol of exactly that address, or None
    symbol: str | None = None

    @property
    def location(self):
        """``module+0x…``, or the bare address."""
        if not self.module:
            return f"0x{self.address:x}"
        return f"{self.module}+0x{self.offset:x}"

    @property
    def label(self):
        """The symbol, or where it is."""
        return self.symbol or self.location

    @property
    def signature(self):
        """Everything known, for tooltips and the selection."""
        if self.symbol:
            return f"{self.symbol}  ({self.location}, 0x{self.address:x})"
        return f"{self.location}  (0x{self.address:x})"


def native_functions(rows):
    """NativeFunctions of the ``functions`` of a ``finish`` answer."""
    return [NativeFunction(int(address, 16), module, offset)
            for address, module, offset in rows]


def apply_names(functions, result):
    """Add the functions and names of a ``finish`` answer to ``functions``."""
    first = result.get("firstFunction", len(functions))
    if first != len(functions):
        raise ValueError(f"functions out of step: {first} instead of {len(functions)}")
    functions.extend(native_functions(result.get("functions", [])))
    for number, name in result.get("names", {}).items():
        functions[int(number)].symbol = name


@dataclass(frozen=True)
class TraceThread:
    """A thread of the game, to follow or not."""

    id: int
    name: str
    # share of a CPU it used just now (0–1), None if unknown
    load: float | None = None
    # whether it ran just now at all, None if unknown
    active: bool | None = None


@dataclass(frozen=True)
class TraceModule:
    """A module of the game; the game's own ones are followed by default."""

    name: str
    path: str
    base: int
    size: int
    game: bool


def foreign_code(mappings, followed):
    """[base, size] of the executable files mapped outside the ``followed`` TraceModules.

    Under Wine Frida knows only the PE modules: Wine's Unix side (ntdll.so,
    win32u.so, winex11.so, libc, the GL driver) is no module to exclude, so
    Stalker would recompile every system call down into the driver, Wine's
    syscall dispatcher included. Even one followed thread froze Baldur's Gate
    under Proton. Anonymous code stays followed: Stalker's own lies there.
    """
    spans = [(module.base, module.base + module.size) for module in followed]
    return [[hex(mapping.start), mapping.end - mapping.start] for mapping in mappings
            if "x" in mapping.permissions and mapping.path.startswith("/")
            and not any(mapping.start < end and start < mapping.end for start, end in spans)]


def _thread_times(pid, tid, proc_root="/proc"):
    """(CPU time in clock ticks, context switches) of a thread, or None."""
    task = f"{proc_root}/{pid}/task/{tid}"
    try:
        with open(f"{task}/stat", encoding="utf-8") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        with open(f"{task}/status", encoding="utf-8") as status:
            # voluntary_ctxt_switches and nonvoluntary_ctxt_switches
            switches = sum(int(line.split(":", 1)[1]) for line in status
                           if "ctxt_switches:" in line)
    except (OSError, IndexError, ValueError):
        return None
    # fields[0] is the state (field 3): utime and stime are fields 14 and 15
    return int(fields[11]) + int(fields[12]), switches


def thread_activity(pid, tids, seconds=0.5, proc_root="/proc", sleep=None):
    """{tid: (share of a CPU, whether it ran)} over ``seconds``, for the tids /proc knows.

    A thread that only wakes up briefly uses less than a clock tick: whether
    it ran shows in its context switches (on Windows in its CPU cycles).
    """
    # pylint: disable=import-outside-toplevel
    import os
    import time

    sleep = sleep or time.sleep
    if sys.platform == "win32":
        from grembyte.platform.windows import thread_times
        # (CPU seconds, CPU cycles)
        sample = thread_times
    else:
        ticks_per_second = os.sysconf("SC_CLK_TCK")  # pylint: disable=no-member

        def sample(tid):
            # (CPU seconds, context switches)
            times = _thread_times(pid, tid, proc_root)
            return None if times is None else (times[0] / ticks_per_second, times[1])

    before = {tid: sample(tid) for tid in tids}
    sleep(seconds)
    activity = {}
    for tid, start in before.items():
        end = sample(tid)
        if start is not None and end is not None:
            busy, woke = end[0] - start[0], end[1] - start[1]
            activity[tid] = (busy / seconds, busy > 0 or woke > 0)
    return activity


class NativeTraceWorker(AgentWorker):
    """Keep a Frida session open for a native trace; see the module documentation.

    The public methods are safe to call from the GUI thread; results come
    back through the signals.
    """

    # TraceThreads (the busiest first), TraceModules, platform
    inventory_found = Signal(object, object, str)
    # {threads, failed, modules}
    started = Signal(object)

    agent_name = "native trace"
    agent_key = "native_trace"

    def __init__(self, pid, parent=None):
        super().__init__(pid, parent)
        # Under Wine the agent sees Windows thread ids, which /proc does not
        # know: it measures their load itself.
        self._wine = is_wine_process(pid)
        # TraceModules of the last inventory
        self._modules = []
        # Agents of earlier traces: their Stalker keeps its exclusions, and
        # unloading one waits for threads still blocked in its code.
        self._retired = []
        self._traced = False
        # The agent runs on Windows (natively or under Wine), from the inventory.
        self._windows = False

    def agent_source(self):
        return build_agent_source(settings.get("trace_sequence"))

    def inventory(self):
        """List the threads and modules; expect inventory_found."""
        self._commands.put(("inventory", None))

    def trace(self, threads, modules):
        """Follow the thread ids ``threads`` in the modules named ``modules``."""
        self._commands.put(("start", {"threads": list(threads), "modules": list(modules)}))

    @sessions.tracked(sessions.NATIVE_TRACE)
    def run(self):
        self._run_agent()

    def _run_command(self, api, action, argument):
        if action == "inventory":
            if self._wine:
                # The agent measures with GetThreadTimes; whether a thread only
                # woke up briefly (less than a tick) cannot be told there.
                answer = api.inventory(WINE_LOAD_SECONDS)
                loads = {thread["id"]: (thread["load"], thread["load"] > 0)
                         for thread in answer["threads"] if thread.get("load") is not None}
            else:
                answer = api.inventory()
                ids = [thread["id"] for thread in answer["threads"]]
                loads = thread_activity(self.pid, ids)
            threads = [TraceThread(thread["id"], thread["name"],
                                   *loads.get(thread["id"], (None, None)))
                       for thread in answer["threads"]]
            threads.sort(key=lambda thread: -(thread.load or 0))
            modules = [TraceModule(module["name"], module["path"], int(module["base"], 16),
                                   module["size"], module["game"])
                       for module in answer["modules"]]
            self._modules = modules
            self._windows = answer["platform"] == "windows"
            self.inventory_found.emit(threads, modules, answer["platform"])
        elif action == "start":
            # Stalker keeps what it excluded: on Linux a new trace gets a fresh
            # agent. Under Windows one agent, and so one Stalker, serves every
            # trace (two froze a game); it asks for a fresh one only when a
            # module to follow was excluded before.
            if self._traced and not self._windows:
                api = self._fresh_agent()
            self._traced = True
            if self._wine:
                followed = [module for module in self._modules
                            if module.name in argument["modules"]]
                argument = {**argument,
                            "ranges": foreign_code(read_mappings(self.pid), followed)}
            activity.record("native_trace_start", pid=self.pid, threads=argument["threads"],
                            modules=argument["modules"],
                            native_ranges=len(argument.get("ranges", [])))
            try:
                answer = api.start(argument)
            except frida.RPCException as error:
                if FRESH_AGENT not in str(error):
                    raise
                answer = self._fresh_agent().start(argument)
            activity.record("native_trace_started", pid=self.pid, **answer)
            # Where the threads are once Stalker has taken them over (or not)
            timer = threading.Timer(1.0, activity.record_threads, (self.pid, "1 s after start"))
            timer.daemon = True
            timer.start()
            self.started.emit(answer)
        elif action == "stop":
            answer = api.stop()
            staying = answer.get("staying", 0) if isinstance(answer, dict) else 0
            if staying:
                # Windows: blocked in a call since; they leave once it returns.
                activity.record("native_trace_staying", pid=self.pid, threads=staying)
            self.stopped.emit()
            activity.record_threads(self.pid, "trace stopped")
        else:
            super()._run_command(api, action, argument)

    def _fresh_agent(self):
        """Keep the agent loaded (its Stalker may still hold threads) and load another."""
        self._retired.append(self._script)
        self._script = None
        self._load_script(self._session)
        return self._script.exports_sync

    def _unload_script(self):
        super()._unload_script()
        while self._retired:
            self._script = self._retired.pop()
            super()._unload_script()
