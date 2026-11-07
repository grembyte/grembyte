"""Catalog of the classes and methods a game's managed runtime has loaded.

A short Frida session asks the engine adapter (see hermes/engines) for the
loaded assemblies and, assembly by assembly, for their classes and methods.
Reading a class may make the runtime load it, so framework assemblies (for
Mono: mscorlib, System, UnityEngine, ...) are only read when asked for: they
are large and rarely what one looks for.
"""

import re
import threading
from dataclasses import dataclass, field, replace

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs, messages
from hermes.core import sessions
from hermes.engines.registry import ENGINE_AGENT_SOURCE, resolve_engine
from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
from hermes.agent.source import load_agent_source
from hermes.features import catalog_cache, games
from hermes.features.assemblies import chosen_assemblies


# Also the call graph's agent (see hermes.features.call_graph)
AGENT_SOURCE = ENGINE_AGENT_SOURCE + load_agent_source(__file__)


@dataclass(frozen=True)
class CatalogMethod:
    """One method of the catalog."""

    assembly: str
    class_name: str
    name: str
    parameters: str
    return_type: str
    static: bool
    token: int
    # ``Namespace.Class:Method (int,single)`` as the runtime names it
    signature: str
    engine: str = "mono"
    argument_names: tuple = field(default=(), compare=False)

    @property
    def display_signature(self):
        """The method signature with parameter names, for UI display."""
        if not self.argument_names:
            return self.signature
        head, separator, parameters = self.signature.partition(" (")
        if not separator or not parameters.endswith(")"):
            return self.signature
        types = split_type_arguments(parameters[:-1])
        named = [
            f"{type_name} {self.argument_names[index] if index < len(self.argument_names) else f'arg{index}'}"
            for index, type_name in enumerate(types)
        ]
        return f"{head} ({','.join(named)})"

    def identity(self):
        """What finds the method in the running game (see HookSpec.managed)."""
        return {"engine": self.engine, "image": self.assembly, "file": None,
                "token": self.token, "method": self.signature}


def short_name(signature):
    """``Class:Method`` of ``Namespace.Class:Method (params)``."""
    class_name, _, method = signature.split(" (")[0].partition(":")
    return f"{class_name.rsplit('.', 1)[-1]}:{method}"


# ``Ns.Class`2`` at the end of a signature's class part: a generic class
# without type arguments, i.e. its definition.
_GENERIC_DEFINITION = re.compile(r"`(\d+)$")
# The type parameters Mono shows in a class name: ``Ns.Map<TKey,TValue>``
_TYPE_PARAMETERS = re.compile(r"<([^<>]*)>$")


def generic_parameters(method):
    """The type parameters of the generic class a method was defined in, else ().

    Such a method (``R3.ReactiveProperty`1:set_Value (T)``) has no code of
    its own, only its instances have (see with_type_arguments).
    """
    found = _GENERIC_DEFINITION.search(method.signature.split(":", 1)[0])
    if found is None:
        return ()
    named = _TYPE_PARAMETERS.search(method.class_name)
    if named is not None:
        return tuple(name.strip() for name in named.group(1).split(","))
    count = int(found.group(1))
    return ("T",) if count == 1 else tuple(f"T{index}" for index in range(1, count + 1))


def split_type_arguments(text):
    """``int, Ns.Map`2<int,string>`` -> ``["int", "Ns.Map`2<int,string>"]``."""
    arguments, depth, current = [], 0, ""
    for char in text:
        depth += {"<": 1, ">": -1}.get(char, 0)
        if char == "," and depth == 0:
            arguments.append(current.strip())
            current = ""
        else:
            current += char
    arguments.append(current.strip())
    return [argument for argument in arguments if argument]


def with_type_arguments(method, arguments):
    """The method of a generic class's instance, e.g. ``ReactiveProperty`1<int>``.

    The signature keeps the definition's parameters (``(T)``); the agent
    binds the class to ``arguments`` (see resolveMethod in the Mono agent).
    """
    head, _, rest = method.signature.partition(":")
    listed = ", ".join(arguments)
    class_name, count = _TYPE_PARAMETERS.subn(f"<{listed}>", method.class_name)
    return replace(method, signature=f"{head}<{listed}>:{rest}",
                   class_name=class_name if count else f"{method.class_name}<{listed}>")


def catalog_method_from_identity(identity):
    """A CatalogMethod for a method known by its identity (e.g. of a hook).

    The return type and whether it is static are not part of the identity;
    the method monitor learns them when it starts.
    """
    signature = identity["method"]
    head, _, parameters = signature.partition(" ")
    class_name, _, name = head.partition(":")
    return CatalogMethod(identity["image"], class_name, name, parameters or "()", "?", False,
                         identity["token"], signature, identity.get("engine") or "mono")


def catalog_methods(assembly, classes, engine="mono"):
    """Turn the agent's ``classes`` answer for ``assembly`` into CatalogMethods."""
    result = []
    for class_name, methods in classes:
        for entry in methods:
            name, parameters, return_type, static, token, signature, *metadata = entry
            # Mono and IL2CPP add parameter names after the trace flags; older
            # cache entries and other engines have no such field.
            argument_names = (
                tuple(metadata[-1])
                if engine in ("mono", "il2cpp") and len(metadata) >= 3
                and isinstance(metadata[-1], list) else ()
            )
            result.append(CatalogMethod(
                assembly, class_name, name, parameters, return_type, bool(static), token,
                signature, engine, argument_names,
            ))
    return result


class CatalogWorker(QThread):
    """Read the catalog of a process.

    Emits ``assemblies_found`` with all loaded assemblies first, then
    ``assembly_loaded`` for each one it reads: those named in ``assemblies``,
    or else the loaded ones of ``selection`` (the user's assembly selection,
    see hermes.features.assemblies), or else every assembly that is not part
    of the framework. ``engine`` is the adapter (see hermes/engines),
    detected if not given.

    An assembly read before in the same build comes from the catalog cache
    (see hermes.features.catalog_cache), announced by ``assembly_cached``
    before its ``assembly_loaded``; ``fresh`` reads every one anew.
    """

    assemblies_found = Signal(object)
    # assembly name, its methods (CatalogMethod)
    assembly_loaded = Signal(str, object)
    # assembly name: its methods come from the cache
    assembly_cached = Signal(str)
    # assembly name, error message
    assembly_failed = Signal(str, str)
    catalog_failed = Signal(str)

    def __init__(self, pid, assemblies=None, parent=None, engine=None, selection=(),
                 fresh=False):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        super().__init__(parent)
        self.pid = pid
        self.assemblies = assemblies
        self.selection = frozenset(selection)
        self.fresh = fresh
        self.engine = resolve_engine(pid, engine)
        self._detached = threading.Event()

    @sessions.tracked(sessions.CATALOG)
    def run(self):
        session = None
        script = None
        try:
            session = attach_session(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", lambda _reason, _crash: self._detached.set())
            script = session.create_script(AGENT_SOURCE)
            script.load()
            api = script.exports_sync
            found = api.assemblies()
            games.remember(self.pid, self.engine.id, found)
            self.assemblies_found.emit(found)
            names = self.assemblies
            if names is None:
                names = chosen_assemblies([a["name"] for a in found], self.selection,
                                          self.engine)
            mvids = {a["name"]: a.get("mvid") for a in found}
            for name in names:
                if self.isInterruptionRequested() or self._detached.is_set():
                    break
                try:
                    classes = self._classes(api, name, mvids.get(name))
                    self.assembly_loaded.emit(
                        name, catalog_methods(name, classes, self.engine.id))
                except frida.RPCException as error:
                    self.assembly_failed.emit(name, messages.describe(error))
        except FRIDA_ERRORS + (frida.RPCException, OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.catalog_failed.emit(messages.describe(error))
        finally:
            if script is not None:
                try:
                    script.unload()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)
            if session is not None:
                try:
                    session.detach()
                except CLEANUP_ERRORS as error:
                    logs.cleanup_failed(error)

    def _classes(self, api, name, mvid):
        """The agent's ``classes`` answer for ``name``, from the cache if it has it."""
        classes, cached = catalog_cache.read_classes(
            self.engine.id, name, mvid, lambda: api.classes(name), self.fresh)
        if cached:
            self.assembly_cached.emit(name)
        return classes


class CachedCatalogWorker(QThread):
    """Read the catalog of a game that is not running from the catalog cache.

    ``game`` is a KnownGame (see hermes.features.games): the builds it loaded
    when it was read last. The signals are CatalogWorker's; an assembly whose
    build is not in the cache (never read, or pruned) gives
    ``assembly_failed`` with ``NOT_CACHED``.
    """

    assemblies_found = Signal(object)
    assembly_loaded = Signal(str, object)
    assembly_cached = Signal(str)
    assembly_failed = Signal(str, str)
    catalog_failed = Signal(str)

    NOT_CACHED = "not cached"

    def __init__(self, game, assemblies=None, parent=None, engine=None):
        super().__init__(parent)
        self.game = game
        self.assemblies = assemblies
        self.engine = engine

    def run(self):
        found = self.game.assemblies
        self.assemblies_found.emit(found)
        names = self.assemblies
        if names is None:
            names = chosen_assemblies([a["name"] for a in found], (), self.engine)
        mvids = self.game.mvids()
        for name in names:
            if self.isInterruptionRequested():
                break
            classes = catalog_cache.load(self.engine.id, name, mvids.get(name))
            if classes is None:
                self.assembly_failed.emit(name, self.NOT_CACHED)
                continue
            self.assembly_cached.emit(name)
            self.assembly_loaded.emit(name, catalog_methods(name, classes, self.engine.id))

