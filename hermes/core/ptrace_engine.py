"""Shared ptrace machinery for hardware-watchpoint tools.

This is the injection-free core both the ptrace watch backend and the ptrace
hook backend build on. It drives the Linux ``ptrace`` API the way gdb does:
attach to every thread of a target, program the CPU debug registers (DR0-DR3
hold up to four watched addresses, DR7 their enable/length/condition bits, DR6
reports which one fired), resume the threads, and dispatch the SIGTRAPs that
accesses raise. Nothing is injected, so it works on targets Frida cannot
instrument, most notably Wine/Proton processes.

ptrace requires that every call for a tracee come from the one OS thread that
attached it, so a subclass runs its whole lifecycle inside a ``QThread``'s
``run``. The tracer must be 64-bit; the register layout here is x86-64's.
"""

import ctypes
import os
import signal
import time

from capstone import (  # pylint: disable=import-error
    CS_ARCH_X86,
    CS_MODE_32,
    CS_MODE_64,
    CS_AC_READ,
    CS_AC_WRITE,
    CS_OP_MEM,
    Cs,
)
from PyQt6.QtCore import QThread  # pylint: disable=import-error,no-name-in-module

from hermes.core.pointer import module_bases
from hermes.platform import read_mappings


# ptrace request numbers (see <sys/ptrace.h>).
PTRACE_CONT = 7
PTRACE_DETACH = 17
PTRACE_PEEKUSER = 3
PTRACE_POKEUSER = 6
PTRACE_ATTACH = 16
PTRACE_SETOPTIONS = 0x4200
PTRACE_GETEVENTMSG = 0x4201
PTRACE_O_TRACECLONE = 0x00000008
PTRACE_EVENT_CLONE = 3

# x86-64 syscall number for tgkill(2); the debug-register layout below is
# x86-64's too, so this core is x86-64 only.
SYS_TGKILL = 234

# Offsets into ``struct user`` on x86-64: the general registers come first,
# the eight debug registers sit at u_debugreg[0..7].
USER_REGS_RIP = 128
USER_REGS_CS = 136
DEBUGREG_BASE = 848
DR_ADDRESS = [DEBUGREG_BASE + slot * 8 for slot in range(4)]  # DR0..DR3
DR6 = DEBUGREG_BASE + 6 * 8
DR7 = DEBUGREG_BASE + 7 * 8
WATCHPOINT_SLOTS = 4

# waitpid flag that lets a tracer reap thread (clone) children.
_WALL = 0x40000000

# x86 segment selector a thread runs under: 0x33 for 64-bit code, 0x23 for
# 32-bit (compat) code such as a 32-bit Wine process under a 64-bit kernel.
_CS_64BIT = 0x33

MAX_INSTRUCTION_SIZE = 15
# x86 decoding self-synchronizes: count how many earlier decode chains land on
# a candidate's start to tell the real instruction from a spurious decode.
SYNC_WINDOW = 48

_LENGTH_BITS = {1: 0b00, 2: 0b01, 4: 0b11, 8: 0b10}

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.ptrace.restype = ctypes.c_long
_libc.ptrace.argtypes = [
    ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
]


def _ptrace(request, pid, addr, data):
    """Call ``ptrace`` and raise OSError on failure."""
    ctypes.set_errno(0)
    result = _libc.ptrace(request, pid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    err = ctypes.get_errno()
    if result == -1 and err != 0:
        raise OSError(err, os.strerror(err))
    return result


def _peek_user(pid, offset):
    """Read one word from the tracee's ``struct user`` as an unsigned value."""
    return _ptrace(PTRACE_PEEKUSER, pid, offset, 0) & 0xFFFFFFFFFFFFFFFF


def combined_dr7(slots):
    """Return the DR7 value that arms every slot in ``slots``.

    ``slots`` maps a slot index (0-3) to ``(size, reads)``. ``reads`` picks the
    condition: x86 offers write-only and read/write, but no read-only, so a
    read watch also traps writes.
    """
    value = 0
    for slot, (size, reads) in slots.items():
        length_bits = _LENGTH_BITS.get(size)
        if length_bits is None:
            raise ValueError(f"Unsupported watch size: {size}")
        condition = 0b11 if reads else 0b01  # 11 = read/write, 01 = write
        value |= 1 << (slot * 2)  # local enable for this slot
        value |= (condition | (length_bits << 2)) << (16 + slot * 4)
    return value


def build_dr7(size, reads):
    """DR7 for a single watchpoint in slot 0 (kept for the watch backend)."""
    return combined_dr7({0: (size, reads)})


def _memory_operands(instruction):
    return [op for op in instruction.operands if op.type == CS_OP_MEM]


def _votes(engine, code, start):
    """Count decode chains ending before ``start`` that land exactly on it."""
    votes = 0
    for offset in range(max(0, start - SYNC_WINDOW), start):
        cursor = offset
        while cursor < start:
            chunk = next(engine.disasm(code[cursor:start], cursor, count=1), None)
            if chunk is None or chunk.size == 0:
                break
            cursor += chunk.size
        if cursor == start:
            votes += 1
    return votes


def decode_access(code, pc_offset, bits, reads):
    """Decide whether the instruction ending at ``pc_offset`` reads/writes.

    ``code`` holds bytes leading up to (not including) the trap PC, with the PC
    at index ``pc_offset``. A data breakpoint is a trap, so the PC is the
    instruction *after* the access. Returns ``(read, write)``; an undecodable
    access is reported as a write, which never hides a real write.
    """
    engine = Cs(CS_ARCH_X86, CS_MODE_64 if bits == 64 else CS_MODE_32)
    engine.detail = True
    best = None  # (read, write) of the best-scoring candidate
    best_votes = -1
    for start in range(max(0, pc_offset - MAX_INSTRUCTION_SIZE), pc_offset):
        instruction = next(engine.disasm(code[start:pc_offset], start, count=1), None)
        if instruction is None or start + instruction.size != pc_offset:
            continue
        operands = _memory_operands(instruction)
        if not operands:
            continue
        votes = _votes(engine, code, start)
        if votes > best_votes:
            best_votes = votes
            best = (
                any(op.access & CS_AC_READ for op in operands),
                any(op.access & CS_AC_WRITE for op in operands),
            )
    if best is None:
        return (bool(reads), True)
    read, write = best
    if not reads:
        write = True  # a write watch only traps on writes
    return (read, write)


class PtraceWatchpointEngine(QThread):
    """Attach to a process and dispatch hardware-watchpoint traps.

    Subclasses fill :attr:`_slots` (``{slot: (address, size, reads)}``, up to
    :data:`WATCHPOINT_SLOTS` entries) and implement :meth:`_handle_trap`. The
    whole lifecycle must run inside :meth:`run`, because ptrace binds a tracee
    to the thread that attached it.
    """

    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._armed = set()
        self._slots = {}
        self._target_gone = False
        self._mappings = []
        self._bases = {}

    # -- introspection ------------------------------------------------------

    def _load_mappings(self):
        self._mappings = read_mappings(self.pid)
        self._bases = module_bases(self._mappings)

    def _site(self, pc):
        """Return ``module+offset`` for ``pc``, or its hex address."""
        for mapping in self._mappings:
            if mapping.start <= pc < mapping.end and mapping.module is not None:
                return f"{mapping.module}+{pc - self._bases[mapping.module]:#x}"
        return hex(pc)

    @staticmethod
    def _pc_bits(tid):
        """Return ``(pc, bits)`` of a stopped thread (bits: 32 or 64)."""
        pc = _peek_user(tid, USER_REGS_RIP)
        segment = _peek_user(tid, USER_REGS_CS)
        bits = 64 if segment == _CS_64BIT else 32
        if bits == 32:
            pc &= 0xFFFFFFFF
        return pc, bits

    @staticmethod
    def _explain(error):
        if error.errno == 1:  # EPERM
            return (
                "ptrace was denied. Set /proc/sys/kernel/yama/ptrace_scope to 0, "
                "or run with CAP_SYS_PTRACE. Also stop any other debugger "
                "(including a running Frida session) attached to the process."
            )
        if error.errno == 3:  # ESRCH
            return "The target thread or process is gone."
        return str(error)

    # -- attaching / arming -------------------------------------------------

    def _attach_all(self):
        """Attach every current thread and arm it. Returns warnings."""
        warnings = []
        for _ in range(2):  # a second pass catches threads born while attaching
            for tid in self._list_threads():
                if tid in self._armed:
                    continue
                try:
                    self._attach_thread(tid)
                    self._armed.add(tid)
                except OSError as error:
                    warnings.append(f"Thread {tid}: {self._explain(error)}")
        return warnings

    def _list_threads(self):
        try:
            return [int(entry) for entry in os.listdir(f"/proc/{self.pid}/task")]
        except OSError:
            return []

    def _attach_thread(self, tid):
        _ptrace(PTRACE_ATTACH, tid, 0, 0)
        os.waitpid(tid, _WALL)  # consume the stop ATTACH raises
        _ptrace(PTRACE_SETOPTIONS, tid, 0, PTRACE_O_TRACECLONE)
        self._arm(tid)
        _ptrace(PTRACE_CONT, tid, 0, 0)

    def _arm(self, tid):
        """Write the current slot set into one stopped thread's registers."""
        dr7 = combined_dr7({slot: (size, reads)
                            for slot, (_addr, size, reads) in self._slots.items()})
        for slot, (address, _size, _reads) in self._slots.items():
            _ptrace(PTRACE_POKEUSER, tid, DR_ADDRESS[slot], address)
        _ptrace(PTRACE_POKEUSER, tid, DR6, 0)
        _ptrace(PTRACE_POKEUSER, tid, DR7, dr7)

    def _reprogram(self):
        """Re-arm every thread after the slot set changed.

        POKEUSER needs a stopped thread, so each thread is halted with SIGSTOP,
        its registers rewritten, and resumed. A watchpoint SIGTRAP that arrives
        meanwhile is acknowledged (DR6 cleared) but not acted on; one missed
        correction during a reprogram is harmless.
        """
        targets = list(self._armed)
        for tid in targets:
            self._tgkill(tid, signal.SIGSTOP)
        stopped = set()
        deadline = time.monotonic() + 2.0
        while set(targets) - stopped and time.monotonic() < deadline:
            try:
                tid, status = os.waitpid(-1, os.WNOHANG | _WALL)
            except ChildProcessError:
                break
            if tid == 0:
                time.sleep(0.001)
                continue
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                self._armed.discard(tid)
                stopped.discard(tid)
                if tid in targets:
                    targets.remove(tid)
                continue
            if os.WIFSTOPPED(status):
                if os.WSTOPSIG(status) == signal.SIGTRAP:
                    try:
                        _ptrace(PTRACE_POKEUSER, tid, DR6, 0)
                    except OSError:
                        pass
                stopped.add(tid)
        for tid in stopped:
            try:
                self._arm(tid)
                _ptrace(PTRACE_CONT, tid, 0, 0)
            except OSError:
                self._armed.discard(tid)

    # -- event loop ---------------------------------------------------------

    def _pump(self):
        """Handle one ptrace event. Returns False when none was pending."""
        try:
            tid, status = os.waitpid(-1, os.WNOHANG | _WALL)
        except ChildProcessError:
            self.requestInterruption()
            return False
        if tid == 0:
            return False
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            self._armed.discard(tid)
            if tid == self.pid:
                self._target_gone = True
                self.requestInterruption()
            return True
        if not os.WIFSTOPPED(status):
            return True

        if status >> 16 == PTRACE_EVENT_CLONE:
            self._on_clone(tid)
            return True

        stop_signal = os.WSTOPSIG(status)
        deliver = 0
        if stop_signal == signal.SIGTRAP:
            deliver = self._dispatch_trap(tid) or 0
        elif stop_signal != signal.SIGSTOP:
            deliver = stop_signal  # pass the target's own signals through
        try:
            _ptrace(PTRACE_CONT, tid, 0, deliver)
        except OSError:
            self._armed.discard(tid)
        return True

    def _dispatch_trap(self, tid):
        """Handle a SIGTRAP stop; return a signal to deliver, or None."""
        try:
            dr6 = _peek_user(tid, DR6)
        except OSError:
            return
        fired = [slot for slot in self._slots if dr6 & (1 << slot)]
        if not fired:
            return
        try:
            _ptrace(PTRACE_POKEUSER, tid, DR6, 0)  # acknowledge the hit
            pc, bits = self._pc_bits(tid)
        except OSError:
            return
        for slot in fired:
            self._handle_trap(tid, slot, pc, bits)

    def _on_clone(self, tid):
        try:
            new_tid = _ptrace(PTRACE_GETEVENTMSG, tid, 0, 0)
            os.waitpid(new_tid, _WALL)  # the new thread is stopped at birth
            _ptrace(PTRACE_SETOPTIONS, new_tid, 0, PTRACE_O_TRACECLONE)
            self._arm(new_tid)
            _ptrace(PTRACE_CONT, new_tid, 0, 0)
            self._armed.add(new_tid)
        except (OSError, ChildProcessError):
            pass
        try:
            _ptrace(PTRACE_CONT, tid, 0, 0)
        except OSError:
            self._armed.discard(tid)

    def _handle_trap(self, tid, slot, pc, bits):
        """Act on a watchpoint hit; implemented by subclasses."""
        raise NotImplementedError

    # -- detaching ----------------------------------------------------------

    def _detach_all(self):
        """Stop every armed thread, clear its watchpoints, and detach cleanly.

        PTRACE_DETACH only works on a stopped thread, and detaching without
        clearing DR7 would leave an armed watchpoint with no tracer, so the
        next access raises SIGTRAP and kills the target. So each thread is
        halted with SIGSTOP first; detaching with signal 0 discards that stop
        (and any pending watchpoint SIGTRAP) and resumes the thread normally.
        """
        pending = set(self._armed)
        for tid in pending:
            self._tgkill(tid, signal.SIGSTOP)
        deadline = time.monotonic() + 2.0
        while pending and time.monotonic() < deadline:
            try:
                tid, status = os.waitpid(-1, os.WNOHANG | _WALL)
            except ChildProcessError:
                break
            if tid == 0:
                time.sleep(0.001)
                continue
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                pending.discard(tid)
                continue
            if os.WIFSTOPPED(status):
                self._disarm_and_detach(tid)
                pending.discard(tid)
        for tid in pending:  # never reported a stop; best effort
            self._disarm_and_detach(tid)
        self._armed.clear()

    def _tgkill(self, tid, sig):
        """Send ``sig`` to one thread of the target (tgkill(tgid, tid, sig))."""
        _libc.syscall(SYS_TGKILL, self.pid, tid, int(sig))

    @staticmethod
    def _disarm_and_detach(tid):
        for offset in (DR7, DR6):
            try:
                _ptrace(PTRACE_POKEUSER, tid, offset, 0)
            except OSError:
                pass
        try:
            _ptrace(PTRACE_DETACH, tid, 0, 0)
        except OSError:
            pass
