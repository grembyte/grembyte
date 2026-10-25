"""Call graph: which methods a C# method calls, and which call it.

Read from the methods' IL code (ECMA-335 partition III), not from running
them: the engine adapter hands out a method's code (``methodBodies``), the
calls in it (``call``, ``callvirt``, ``newobj``, ``ldftn`` ...) name their
target by a metadata token, and the adapter says which method a token names
(``resolveTokens``). What a method calls shows in the order of its code.

Who calls a method needs the calls of every method of the assemblies looked
at: an index per assembly (``build_call_index``), kept in the catalog cache
like the classes (see hermes.features.catalog_cache). A call into another
assembly names its target by that assembly's token, so the index notes the
builds it was made with and is made anew when they differ
(``read_call_index``): the same build of a library may meet another
UnityEngine in another game.

The C# compiler moves the body of an ``async`` method or an iterator into
the ``MoveNext`` method of a class it generates (``Outer.<Load>d__0``); the
graph shows such a method under the method it came from (``async_owner``).
Calls of virtual methods and interfaces name the method they were compiled
against, not the override that runs.
"""

import itertools
import queue
import re
import struct
import threading
from dataclasses import dataclass

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.core.frida_attach import attach_session
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS
from hermes.engines.registry import resolve_engine
from hermes.features import catalog_cache, games
from hermes.features.assemblies import chosen_assemblies
from hermes.features.catalog import (
    AGENT_SOURCE,
    CatalogMethod,
    catalog_method_from_identity,
    catalog_methods,
)

# --- IL code ----------------------------------------------------------------

# Opcodes that name a method, and how the graph shows them
CALL_OPCODES = {0x28: "call", 0x6F: "callvirt", 0x73: "newobj", 0x27: "jmp",
                0xFE06: "ldftn", 0xFE07: "ldvirtftn"}
# Operand sizes of the one-byte opcodes; all others have none.
_OPERANDS = {
    **dict.fromkeys((0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x1F, 0xDE, *range(0x2B, 0x38)), 1),
    **dict.fromkeys((0x20, 0x22, 0x27, 0x28, 0x29, 0x6F, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75,
                     0x79, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F, 0x80, 0x81, 0x8C, 0x8D, 0x8F, 0xA3,
                     0xA4, 0xA5, 0xC2, 0xC6, 0xD0, 0xDD, *range(0x38, 0x45)), 4),
    0x21: 8, 0x23: 8,
}
# ... and of the two-byte ones (0xFE xx)
_OPERANDS_FE = {0x06: 4, 0x07: 4, 0x09: 2, 0x0A: 2, 0x0B: 2, 0x0C: 2, 0x0D: 2, 0x0E: 2,
                0x12: 1, 0x15: 4, 0x16: 4, 0x19: 1, 0x1C: 4}
_SWITCH = 0x45
# Metadata tables a call's token may point into: MethodDef, MemberRef, MethodSpec
METHOD_DEF, MEMBER_REF, METHOD_SPEC = 0x06, 0x0A, 0x2B


def calls_in(code):
    """``(opcode name, token)`` of the calls in IL ``code``, in the order of the code.

    Stops at code it cannot decode (its calls so far stay).
    """
    calls = []
    at, end = 0, len(code)
    while at < end:
        opcode = code[at]
        at += 1
        if opcode == 0xFE:
            if at >= end:
                break
            opcode = 0xFE00 | code[at]
            at += 1
            size = _OPERANDS_FE.get(opcode & 0xFF, 0)
        elif opcode == _SWITCH:
            if at + 4 > end:
                break
            size = 4 + 4 * struct.unpack_from("<I", code, at)[0]
        else:
            size = _OPERANDS.get(opcode, 0)
        if at + size > end:
            break
        if opcode in CALL_OPCODES:
            token = struct.unpack_from("<I", code, at)[0]
            if token >> 24 in (METHOD_DEF, MEMBER_REF, METHOD_SPEC):
                calls.append((CALL_OPCODES[opcode], token))
        at += size
    return calls


def split_bodies(buffer):
    """``{token: code}`` of a ``methodBodies`` answer (records: token, size, code)."""
    bodies, at = {}, 0
    while at + 8 <= len(buffer):
        token, size = struct.unpack_from("<II", buffer, at)
        bodies[token] = bytes(buffer[at + 8:at + 8 + size])
        at += 8 + size
    return bodies


# --- Index ------------------------------------------------------------------

# Methods per request to the agent
CHUNK = 1000


def method_tokens(classes):
    """The tokens of the methods of a catalog ``classes`` answer."""
    return [entry[4] for _class_name, methods in classes for entry in methods]


def build_call_index(assembly, classes, read_bodies, resolve, progress=None):
    """The calls of every method of ``assembly`` (``classes``: its catalog answer).

    ``read_bodies(tokens)`` returns ``{token: IL code}``, ``resolve(tokens)``
    ``[assembly, token, signature]`` (or None) for each token in the code;
    ``progress(done, total)`` hears how far it is. The index is JSON:
    ``{"targets": [[assembly, token, signature], ...],
    "calls": [[caller token, [[opcode name, target index], ...]], ...]}``.
    """
    tokens = method_tokens(classes)
    own = {entry[4]: entry[5] for _class_name, methods in classes for entry in methods}
    found = []
    for start in range(0, len(tokens), CHUNK):
        if progress is not None:
            progress(start, len(tokens))
        chunk = tokens[start:start + CHUNK]
        bodies = read_bodies(chunk)
        found.extend((token, calls_in(bodies.get(token, b""))) for token in chunk)
    # What each token names: the assembly's own methods are known, others asked for.
    named = {token: [assembly, token, signature] for token, signature in own.items()}
    unknown = sorted({token for _caller, calls in found for _op, token in calls} - named.keys())
    for start in range(0, len(unknown), CHUNK):
        chunk = unknown[start:start + CHUNK]
        named.update(zip(chunk, resolve(chunk)))
    targets, index_of = [], {}
    calls = []
    for caller, found_calls in found:
        listed = []
        for opcode, token in found_calls:
            target = named.get(token)
            if target is None:
                continue
            key = tuple(target)
            if key not in index_of:
                index_of[key] = len(targets)
                targets.append(list(target))
            listed.append([opcode, index_of[key]])
        if listed:
            calls.append([caller, listed])
    if progress is not None:
        progress(len(tokens), len(tokens))
    return {"targets": targets, "calls": calls}


def read_call_index(engine, assembly, mvid, mvids, build):
    """The call index of ``assembly`` (its ``mvid``) in a process whose
    assemblies have ``mvids`` ({name: mvid}).

    From the catalog cache if it was made with the same builds of the
    assemblies it refers to, else ``build()`` makes it (and it is kept).
    """
    def made():
        index = build()
        referred = {target[0] for target in index["targets"]} - {assembly}
        index["references"] = {name: mvids.get(name) for name in sorted(referred)}
        return index

    index, cached = catalog_cache.read_cached(engine, assembly, mvid, made,
                                              kind=catalog_cache.CALLS)
    if cached and any(mvids.get(name) != referred
                      for name, referred in index.get("references", {}).items()):
        index = catalog_cache.read_cached(engine, assembly, mvid, made, fresh=True,
                                          kind=catalog_cache.CALLS)[0]
    return index


@dataclass(frozen=True)
class Call:
    """One call in the graph: how (``opcode``) and the method (a CatalogMethod)."""

    opcode: str
    method: CatalogMethod


# ``Outer.<Load>d__0``: the class the compiler made of an async method or iterator
_STATE_MACHINE = re.compile(r"^(?P<outer>.+)\.<(?P<name>[^>]+)>d__\d+$")


def async_owner(method):
    """``(class name, method name)`` of the async method or iterator whose body
    ``method`` (a ``MoveNext``) is, else None."""
    if method.name != "MoveNext":
        return None
    found = _STATE_MACHINE.match(method.class_name)
    return None if found is None else (found["outer"], found["name"])


def state_machines(method, methods):
    """The ``MoveNext`` methods among ``methods`` that hold the body of ``method``."""
    return [other for other in methods if other.assembly == method.assembly
            and async_owner(other) == (method.class_name, method.name)]


def callees_of(index, method, lookup, engine="mono"):
    """The Calls ``method`` makes, by the index of its assembly.

    ``lookup(assembly, token)`` is the CatalogMethod of a method if known.
    """
    for caller, listed in index["calls"]:
        if caller == method.token:
            return [Call(opcode, _method_of(index["targets"][target], lookup, engine))
                    for opcode, target in listed]
    return []


def callers_in(index, method, methods_by_token):
    """The Calls of the methods in ``index`` that call ``method``.

    ``methods_by_token``: the CatalogMethods of the index's assembly.
    """
    targets = {position for position, (target_assembly, token, _signature)
               in enumerate(index["targets"])
               if target_assembly == method.assembly and token == method.token}
    found = []
    for caller, listed in index["calls"]:
        opcodes = [opcode for opcode, target in listed if target in targets]
        if opcodes and caller in methods_by_token:
            found.append(Call(opcodes[0], methods_by_token[caller]))
    return found


def _method_of(target, lookup, engine):
    target_assembly, token, signature = target
    known = lookup(target_assembly, token)
    # A generic instance has its definition's token but another signature.
    if known is not None and known.signature == signature:
        return known
    return catalog_method_from_identity({"engine": engine, "image": target_assembly,
                                         "token": token, "method": signature})


# --- Worker -----------------------------------------------------------------

class NotCached(Exception):
    """What a question needs is neither in the catalog cache nor readable:
    the game is not running (offline, see CallGraphWorker)."""


class _OfflineApi:
    """Stands in for the agent of a game that is not running: it knows the
    game's assemblies (a KnownGame, see hermes.features.games), nothing else."""

    def __init__(self, game):
        self.game = game

    def assemblies(self):
        return self.game.assemblies

    def classes(self, name):
        raise NotCached(name)

    def method_bodies(self, name, _tokens):
        raise NotCached(name)

    def resolve_tokens(self, name, _tokens):
        raise NotCached(name)


class CallGraphWorker(QThread):
    """Keep a Frida session open and answer call graph questions about a process.

    The public methods are safe to call from the GUI thread and return a
    request id; answers come back through the signals. ``selection`` (the
    user's assembly selection, see hermes.features.assemblies) decides which
    assemblies are searched for callers.

    With ``game`` (a KnownGame) instead of a running process the answers come
    from the catalog cache alone; a question it cannot answer fails with
    ``NOT_CACHED``, and callers are searched in the assemblies indexed before.
    """

    # request id, method (CatalogMethod), its Calls, the Calls of its body
    # in a compiler-made MoveNext (async methods, iterators)
    callees_found = pyqtSignal(int, object, list, list)
    # request id, method, the Calls of the methods that call it, the
    # assemblies not searched (offline: no call index in the cache)
    callers_found = pyqtSignal(int, object, list, list)
    # request id, message (NOT_CACHED: offline, not in the cache)
    request_failed = pyqtSignal(int, str)
    # assemblies indexed so far, of how many, the one at hand
    progress = pyqtSignal(int, int, str)
    worker_failed = pyqtSignal(str)

    NOT_CACHED = "not cached"

    _requests = itertools.count(1)

    def __init__(self, pid, parent=None, engine=None, selection=(), game=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.pid = pid
        self.game = game
        self.engine = resolve_engine(pid, engine) if game is None else engine
        self.selection = frozenset(selection)
        self._commands = queue.Queue()
        self._detached = threading.Event()
        # assembly -> (mvid, classes, {token: CatalogMethod}, call index or None)
        self._known = {}
        self._mvids = None

    def callees(self, method):
        """What ``method`` (a CatalogMethod) calls; expect callees_found."""
        return self._ask("callees", method)

    def callers(self, method):
        """Which methods call ``method``; expect callers_found (and progress)."""
        return self._ask("callers", method)

    def _ask(self, action, method):
        request_id = next(self._requests)
        self._commands.put((action, request_id, method))
        return request_id

    @sessions.tracked(sessions.CATALOG)
    def run(self):
        if self.game is not None:
            self._serve(_OfflineApi(self.game))
            return
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", lambda _reason, _crash: self._detached.set())
            script = session.create_script(AGENT_SOURCE)
            script.load()
            self._serve(script.exports_sync)
        except FRIDA_ERRORS + (frida.RPCException, OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.worker_failed.emit(str(error))
        finally:
            for close in (script and script.unload, session and session.detach):
                if close:
                    try:
                        close()
                    except CLEANUP_ERRORS as error:
                        logs.cleanup_failed(error)

    def _serve(self, api):
        while not self.isInterruptionRequested() and not self._detached.is_set():
            try:
                action, request_id, method = self._commands.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if action == "callees":
                    self._answer_callees(api, request_id, method)
                else:
                    self._answer_callers(api, request_id, method)
            except NotCached:
                self.request_failed.emit(request_id, self.NOT_CACHED)
            except frida.RPCException as error:
                self.request_failed.emit(request_id, str(error).split("\n", 1)[0])

    # --- Answers ---------------------------------------------------------------
    def _answer_callees(self, api, request_id, method):
        _mvid, classes, methods = self._assembly(api, method.assembly)
        machines = state_machines(method, methods.values())
        index = self._stored_index(api, method.assembly)
        if index is None:
            # Not indexed yet: read just these methods instead of the assembly.
            wanted = {method.token} | {machine.token for machine in machines}
            index = build_call_index(
                method.assembly,
                [[class_name, [entry for entry in entries if entry[4] in wanted]]
                 for class_name, entries in classes],
                lambda tokens: split_bodies(api.method_bodies(method.assembly, tokens)),
                lambda tokens: api.resolve_tokens(method.assembly, tokens))
        body = [call for machine in machines
                for call in callees_of(index, machine, self._lookup, self.engine.id)]
        self.callees_found.emit(request_id, method,
                                callees_of(index, method, self._lookup, self.engine.id), body)

    def _answer_callers(self, api, request_id, method):
        names = chosen_assemblies(list(self._assembly_mvids(api)), self.selection, self.engine)
        if method.assembly not in names:
            names.append(method.assembly)
        found, missing = [], []
        for done, name in enumerate(names):
            self.progress.emit(done, len(names), name)
            if self.isInterruptionRequested():
                return
            try:
                found.extend(callers_in(self._index(api, name), method,
                                        self._assembly(api, name)[2]))
            except NotCached:
                missing.append(name)
        self.progress.emit(len(names), len(names), "")
        self.callers_found.emit(request_id, method, found, missing)

    # --- What is known of the assemblies ----------------------------------------
    def _assembly_mvids(self, api):
        if self._mvids is None:
            found = api.assemblies()
            if self.game is None:
                games.remember(self.pid, self.engine.id, found)
            self._mvids = {a["name"]: a.get("mvid") for a in found}
        return self._mvids

    def _assembly(self, api, name):
        """(mvid, classes, {token: CatalogMethod}) of assembly ``name``."""
        if name not in self._known:
            mvid = self._assembly_mvids(api).get(name)
            classes = catalog_cache.read_classes(self.engine.id, name, mvid,
                                                 lambda: api.classes(name))[0]
            methods = {method.token: method
                       for method in catalog_methods(name, classes, self.engine.id)}
            self._known[name] = (mvid, classes, methods, None)
        return self._known[name][:3]

    def _lookup(self, assembly, token):
        """The CatalogMethod of a method of an assembly read already, else None."""
        known = self._known.get(assembly)
        return None if known is None else known[2].get(token)

    def _stored_index(self, api, name):
        """The call index of ``name`` if made already (and still fitting), else None."""
        if self._known[name][3] is None:
            try:
                self._index(api, name, build=False)
            except NotCached:
                return None
        return self._known[name][3]

    def _index(self, api, name, build=True):
        """The call index of assembly ``name`` (see build_call_index); without
        ``build`` only one from the cache (else NotCached)."""
        mvid, classes, methods = self._assembly(api, name)
        index = self._known[name][3]
        if index is None:
            def make():
                if not build:
                    raise NotCached(name)
                return build_call_index(
                    name, classes,
                    lambda tokens: split_bodies(api.method_bodies(name, tokens)),
                    lambda tokens: api.resolve_tokens(name, tokens))
            index = read_call_index(self.engine.id, name, mvid, self._assembly_mvids(api), make)
            self._known[name] = (mvid, classes, methods, index)
        return index
