"""Static fields of C# classes as pointer chain bases (Mono).

Mono keeps the static fields of each class in a block of its own on the
heap, so no module holds them and a pointer scan would have to find its way
there through the runtime's own tables. Asked for the blocks of the classes
in the assemblies the user marked, the scan can start chains there instead
(see hermes.core.pointer, ``static_areas``). The blocks stay put while the
game runs; after a restart they are looked up again, for the classes the
saved chains name (``StaticBasesWorker``).
"""

from dataclasses import dataclass

import frida
from PySide6.QtCore import QThread, Signal  # pylint: disable=import-error,no-name-in-module

from hermes.attach.frida_attach import CLEANUP_ERRORS, FRIDA_ERRORS, attach_session
from hermes.core import logs, messages, sessions
from hermes.core.pointer import managed_base, remember_managed_bases
from hermes.features.catalog import AGENT_SOURCE


@dataclass(frozen=True)
class StaticArea:
    """The static data of one class: where it is and which fields it holds."""

    assembly: str
    class_name: str
    address: int
    size: int
    # ((field name, offset), ...)
    fields: tuple = ()

    @property
    def name(self):
        """Its chain base name (see hermes.core.pointer.managed_base)."""
        return managed_base(self.assembly, self.class_name)

    def field_at(self, offset):
        """The name of the field at ``offset``, or None."""
        return next((name for name, at in self.fields if at == offset), None)


def read_static_areas(pid, requests, owner=None):
    """Return the StaticAreas of ``requests`` ``{assembly: classes or None for all}``.

    A short Frida session; ``owner`` is the worker it runs for (so the
    session can be cut). The bases found are remembered for the process.
    Raises the errors of attaching and of the agent.
    """
    session = None
    script = None
    try:
        session = attach_session(pid)
        if owner is not None:
            sessions.SESSIONS.set_frida_session(owner, session)
        script = session.create_script(AGENT_SOURCE)
        script.load()
        answer = script.exports_sync.static_areas(
            [[assembly, None if classes is None else sorted(classes)]
             for assembly, classes in requests.items()])
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
    areas = [StaticArea(assembly, class_name, int(address, 16), int(size),
                        tuple((field, int(offset)) for field, offset in fields))
             for assembly, class_name, address, size, fields in answer]
    remember_managed_bases(pid, {area.name: area.address for area in areas})
    return areas


# What attaching to the game and the agent may raise
READ_ERRORS = FRIDA_ERRORS + (frida.RPCException, OSError, RuntimeError, ValueError)


class StaticBasesWorker(QThread):
    """Look up the managed chain bases saved chains need, after a (re)start.

    ``requests`` as for ``read_static_areas``; ``found`` carries how many
    bases were found, ``failed`` the reason none could be.
    """

    found = Signal(int)
    failed = Signal(str)

    def __init__(self, pid, requests, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.requests = requests

    @sessions.tracked(sessions.CATALOG)
    def run(self):
        try:
            areas = read_static_areas(self.pid, self.requests, owner=self)
        except READ_ERRORS as error:
            if not self.isInterruptionRequested():
                self.failed.emit(messages.describe(error))
            return
        if not self.isInterruptionRequested():
            self.found.emit(len(areas))
