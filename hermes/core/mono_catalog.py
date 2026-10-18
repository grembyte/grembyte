"""Catalog of the C# classes and methods a Mono game has loaded.

A short Frida session asks the Mono runtime for the loaded assemblies and,
assembly by assembly, for their classes and methods. Reading a class makes
Mono load it, so framework assemblies (mscorlib, System, UnityEngine, ...)
are only read when asked for: they are large and rarely what one looks for.
"""

import threading
from dataclasses import dataclass

import frida
from PyQt6.QtCore import QThread, pyqtSignal  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs
from hermes.core import sessions
from hermes.core.mono import MONO_AGENT_SOURCE
from hermes.core.process import WINE_FRIDA_ERROR, is_wine_process
from hermes.core.write_watch import CLEANUP_ERRORS, FRIDA_ERRORS


FRAMEWORK_PREFIXES = (
    "mscorlib", "netstandard", "System", "Microsoft.", "Mono.", "UnityEngine", "Unity.",
    "UnityScript", "Boo.", "I18N", "Accessibility", "Novell.", "SMDiagnostics",
)

_AGENT_SOURCE = MONO_AGENT_SOURCE + r"""
rpc.exports = {
  assemblies() {
    return mono.assemblies();
  },
  classes(assembly) {
    return mono.classes(assembly);
  }
};
"""


def is_framework_assembly(name):
    """Whether an assembly belongs to .NET, Mono or Unity rather than the game."""
    return name.startswith(FRAMEWORK_PREFIXES)


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
    # ``Namespace.Class:Method (int,single)`` as Mono names it
    signature: str

    def identity(self):
        """What finds the method in the running game (see HookSpec.managed)."""
        return {"image": self.assembly, "file": None, "token": self.token,
                "method": self.signature}


def catalog_method_from_identity(identity):
    """A CatalogMethod for a method known by its identity (e.g. of a hook).

    The return type and whether it is static are not part of the identity;
    the method monitor learns them when it starts.
    """
    signature = identity["method"]
    head, _, parameters = signature.partition(" ")
    class_name, _, name = head.partition(":")
    return CatalogMethod(identity["image"], class_name, name, parameters or "()", "?", False,
                         identity["token"], signature)


def catalog_methods(assembly, classes):
    """Turn the agent's ``classes`` answer for ``assembly`` into CatalogMethods."""
    return [
        CatalogMethod(assembly, class_name, name, parameters, return_type, bool(static), token,
                      signature)
        for class_name, methods in classes
        for name, parameters, return_type, static, token, signature in methods
    ]


class CatalogWorker(QThread):
    """Read the catalog of a process.

    Emits ``assemblies_found`` with all loaded assemblies first, then
    ``assembly_loaded`` for each one it reads: those named in ``assemblies``,
    or else every assembly that is not part of the framework.
    """

    assemblies_found = pyqtSignal(list)
    # assembly name, its methods (CatalogMethod)
    assembly_loaded = pyqtSignal(str, list)
    # assembly name, error message
    assembly_failed = pyqtSignal(str, str)
    catalog_failed = pyqtSignal(str)

    def __init__(self, pid, assemblies=None, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.assemblies = assemblies
        self._detached = threading.Event()

    @sessions.tracked(sessions.CATALOG)
    def run(self):
        session = None
        script = None
        try:
            if is_wine_process(self.pid):
                raise RuntimeError(WINE_FRIDA_ERROR)
            session = frida.attach(self.pid)
            sessions.SESSIONS.set_frida_session(self, session)
            session.on("detached", lambda _reason, _crash: self._detached.set())
            script = session.create_script(_AGENT_SOURCE)
            script.load()
            api = script.exports_sync
            found = api.assemblies()
            self.assemblies_found.emit(found)
            names = self.assemblies
            if names is None:
                names = [a["name"] for a in found if not is_framework_assembly(a["name"])]
            for name in names:
                if self.isInterruptionRequested() or self._detached.is_set():
                    break
                try:
                    self.assembly_loaded.emit(name, catalog_methods(name, api.classes(name)))
                except frida.RPCException as error:
                    self.assembly_failed.emit(name, str(error))
        except FRIDA_ERRORS + (frida.RPCException, OSError, RuntimeError) as error:
            if not self.isInterruptionRequested():
                self.catalog_failed.emit(str(error))
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
