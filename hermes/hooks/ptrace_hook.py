"""A ptrace-backed value hook, as an alternative to the Frida hook engine.

This is "Option A": it does not inject code or patch the game. It sets a
hardware *data* watchpoint on the target address (reusing
:class:`hermes.core.ptrace_engine.PtraceWatchpointEngine`); the write traps the
thread *after* it stored the value, and the worker then reads the value back,
runs the hook's :class:`~hermes.hooks.transforms.ValueTransform` on it (in Qt's
JavaScript engine, the same ``ctx`` the Frida agent uses), and writes the
result back before resuming the thread. Because nothing is injected it works on
Wine/Proton targets.

Compared with the Frida backend this is address-anchored, not
instruction-anchored, which has consequences worth knowing:

* Shared code (``add_gold(&player)`` / ``add_gold(&enemy)``) is told apart for
  free, because only the chosen address is watched -- no caller learning.
* A hook needs a concrete address: a fixed address, or a pointer chain this
  worker resolves. Pointer targets are re-resolved periodically and the
  watchpoint is moved if the object moved.
* The value is corrected just *after* the store, so there is a one-instruction
  window where the game's own value is in memory.
* The CPU offers four data watchpoints, so at most four distinct addresses can
  be hooked at once.
* Training/JIT signatures, caller learning and debug events (Frida-only) are
  not provided.

Only x86-64 targets are supported.
"""

import itertools
import queue
import threading
import time

from PyQt6.QtCore import pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import sessions
from hermes.core.memory import read_memory, write_process_bytes
from hermes.core.pointer import resolve_chain
from hermes.core.ptrace_engine import PtraceWatchpointEngine, WATCHPOINT_SLOTS
from hermes.core.value_types import VALUE_TYPES
from hermes.hooks.sandbox import TransformSandbox
from hermes.platform import ProcessMemory


class PtraceHookWorker(PtraceWatchpointEngine):
    """Manage value hooks in one process using ptrace data watchpoints.

    The public methods mirror :class:`hermes.hooks.engine.HookWorker` and are
    safe to call from the GUI thread: each enqueues a command the worker thread
    runs. Results come back through the signals. The training, caller and debug
    signals exist for interface parity but this backend never emits them.
    """

    session_ready = pyqtSignal(str)
    hook_installed = pyqtSignal(int, dict)
    hook_training = pyqtSignal(int, dict)
    hook_failed = pyqtSignal(int, str)
    hook_removed = pyqtSignal(int)
    hook_updated = pyqtSignal(int)
    hook_events = pyqtSignal(list)
    transform_error = pyqtSignal(int, str)
    callers_learned = pyqtSignal(int, list)
    callers_ambiguous = pyqtSignal(int)
    callers_dropped = pyqtSignal(int, int)
    # Frida-only; kept for interface parity
    site_method = pyqtSignal(list, str, object)
    managed_failed = pyqtSignal(int, str)
    hook_debug = pyqtSignal(list, dict)
    agent_log = pyqtSignal(str, str)
    agent_warning = pyqtSignal(str)
    worker_failed = pyqtSignal(str)
    worker_stopped = pyqtSignal()

    # Pointer targets can move; re-resolve them this often.
    RERESOLVE_INTERVAL = 1.0

    def __init__(self, pid, parent=None):
        super().__init__(pid, parent)
        self.access_stub = None  # Frida-only; kept for interface parity
        self._commands = queue.Queue()
        self._ids = itertools.count(1)
        self._hooks = {}          # hook id -> hook state
        self._addresses = {}      # address -> {"slot", "value_type", "size", "last"}
        self._sandbox = None
        self._dirty = False

    # -- public API (GUI thread) -------------------------------------------

    def add_hook(self, spec):
        """Queue a hook and return its id; expect hook_installed or hook_failed."""
        hook_id = next(self._ids)
        self._commands.put(("add", hook_id, spec))
        return hook_id

    def remove_hook(self, hook_id):
        self._commands.put(("remove", hook_id, None))

    def set_transform(self, hook_id, transform):
        self._commands.put(("transform", hook_id, transform))

    def set_enabled(self, hook_id, enabled):
        self._commands.put(("enabled", hook_id, bool(enabled)))

    def set_trainings_paused(self, paused, timeout=2.0):
        """No training in this backend; satisfy the HookWorker interface."""
        done = threading.Event()
        self._commands.put(("training-paused", 0, done))
        return True if timeout is None else done.wait(timeout)

    def set_debug(self, hook_id, enabled):
        """Debug events are Frida-only; accepted and ignored."""
        self._commands.put(("debug", hook_id, bool(enabled)))

    # -- worker thread ------------------------------------------------------

    @sessions.tracked(sessions.PTRACE_HOOKS)
    def run(self):
        """Attach, service hook commands and watchpoint traps, then detach."""
        try:
            self._sandbox = TransformSandbox()
            self._load_mappings()
            self._attach_all()
            if not self._armed:
                raise OSError(1, "No thread could be attached for hooking.")
            self.session_ready.emit("x86-64")
            self._loop()
        except OSError as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(self._explain(error))
        finally:
            self._detach_all()
            self.worker_stopped.emit()

    def _loop(self):
        last_resolve = time.monotonic()
        while not self.isInterruptionRequested():
            worked = self._drain_commands()
            if time.monotonic() - last_resolve >= self.RERESOLVE_INTERVAL:
                self._reresolve_pointers()
                last_resolve = time.monotonic()
            if self._dirty:
                self._reprogram()
                self._dirty = False
            if not self._pump() and not worked:
                time.sleep(0.002)
        if self._target_gone:
            self.worker_failed.emit("The target process exited.")

    def _drain_commands(self):
        worked = False
        for _ in range(32):  # bounded so traps still get serviced
            try:
                action, hook_id, argument = self._commands.get_nowait()
            except queue.Empty:
                break
            worked = True
            self._dispatch_command(action, hook_id, argument)
        return worked

    def _dispatch_command(self, action, hook_id, argument):
        if action == "add":
            self._add_hook(hook_id, argument)
        elif action == "remove":
            self._remove_hook(hook_id)
        elif action == "transform":
            self._set_transform(hook_id, argument)
        elif action == "enabled":
            self._set_enabled(hook_id, argument)
        elif action == "training-paused":
            argument.set()  # an Event; no trainings to pause

    # -- hook management ----------------------------------------------------

    def _add_hook(self, hook_id, spec):
        if spec.value_type not in VALUE_TYPES:
            self.hook_failed.emit(hook_id, f"Unsupported value type: {spec.value_type}")
            return
        address = self._resolve_address(spec)
        if address is None:
            self.hook_failed.emit(
                hook_id,
                "Could not resolve the target address. The ptrace hook backend "
                "needs a fixed address or a resolvable pointer.",
            )
            return
        slot_info = self._claim_address(address, spec.value_type)
        if slot_info is None:
            self.hook_failed.emit(
                hook_id,
                f"No free hardware watchpoint (the CPU allows {WATCHPOINT_SLOTS}).",
            )
            return
        agent = spec.transform.to_agent()
        self._hooks[hook_id] = {
            "address": address,
            "pointer": spec.pointer,
            "value_type": spec.value_type,
            "source": agent["source"],
            "params": agent.get("params", {}),
            "enabled": spec.enabled,
        }
        self._dirty = True
        self.hook_installed.emit(
            hook_id, {"site": self._site(address), "address": hex(address)}
        )

    def _remove_hook(self, hook_id):
        hook = self._hooks.pop(hook_id, None)
        if hook is None:
            return
        self._release_address(hook["address"])
        self._dirty = True
        self.hook_removed.emit(hook_id)

    def _set_transform(self, hook_id, transform):
        hook = self._hooks.get(hook_id)
        if hook is None:
            return
        agent = transform.to_agent()
        hook["source"] = agent["source"]
        hook["params"] = agent.get("params", {})
        self.hook_updated.emit(hook_id)

    def _set_enabled(self, hook_id, enabled):
        hook = self._hooks.get(hook_id)
        if hook is None:
            return
        hook["enabled"] = enabled
        self.hook_updated.emit(hook_id)

    # -- address / slot bookkeeping ----------------------------------------

    def _claim_address(self, address, value_type):
        info = self._addresses.get(address)
        if info is not None:
            return info
        used = {entry["slot"] for entry in self._addresses.values()}
        free = next((slot for slot in range(WATCHPOINT_SLOTS) if slot not in used), None)
        if free is None:
            return None
        size = VALUE_TYPES[value_type].size
        info = {"slot": free, "value_type": value_type, "size": size,
                "last": self._read_value(address, value_type, size)}
        self._addresses[address] = info
        self._slots[free] = (address, size, False)
        return info

    def _release_address(self, address):
        if any(hook["address"] == address for hook in self._hooks.values()):
            return  # another hook still watches it
        info = self._addresses.pop(address, None)
        if info is not None:
            self._slots.pop(info["slot"], None)

    def _reresolve_pointers(self):
        """Follow pointer targets again and move watchpoints that moved."""
        moved = False
        memory = None
        try:
            for hook in self._hooks.values():
                if hook["pointer"] is None:
                    continue
                if memory is None:
                    memory = ProcessMemory(self.pid)
                new_address = resolve_chain(memory, self._bases, hook["pointer"])
                if new_address is None or new_address == hook["address"]:
                    continue
                old_address = hook["address"]
                info = self._claim_address(new_address, hook["value_type"])
                if info is None:
                    continue  # no free slot; keep watching the old address
                hook["address"] = new_address
                self._release_address(old_address)
                moved = True
        finally:
            if memory is not None:
                memory.close()
        if moved:
            self._dirty = True

    # -- trap handling ------------------------------------------------------

    def _handle_trap(self, tid, slot, pc, bits):
        address = self._slots.get(slot, (None,))[0]
        info = self._addresses.get(address)
        if info is None:
            return
        value_type = info["value_type"]
        size = info["size"]
        new_value = self._read_value(address, value_type, size)
        if new_value is None:
            return
        old_value = info["last"]
        value = new_value
        for hook_id, hook in list(self._hooks.items()):
            if hook["address"] != address or not hook["enabled"]:
                continue
            result = self._sandbox.run(
                hook["source"], hook["params"], old_value, value, value_type
            )
            if result.error:
                self.transform_error.emit(hook_id, result.error)
                continue
            if not result.kept and result.value is not None:
                value = result.value
        if value != new_value:
            self._write_value(address, value, value_type)
        info["last"] = value

    # -- value I/O ----------------------------------------------------------

    def _read_value(self, address, value_type, size):
        data, readable = read_memory(self.pid, address, size)
        if not all(readable):
            return None
        return VALUE_TYPES[value_type].unpack(bytes(data))

    def _write_value(self, address, value, value_type):
        try:
            write_process_bytes(self.pid, address, VALUE_TYPES[value_type].pack(value))
        except OSError:
            pass

    def _resolve_address(self, spec):
        if spec.address is not None:
            return spec.address
        if spec.pointer is not None:
            memory = ProcessMemory(self.pid)
            try:
                return resolve_chain(memory, self._bases, spec.pointer)
            finally:
                memory.close()
        return None
