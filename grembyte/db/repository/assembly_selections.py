"""Persistence of the assemblies chosen for the catalog and the trace."""

from sqlalchemy import delete, select

from grembyte.db.engine import SESSION_LOCAL
from grembyte.db.models import AssemblySelectionRecord


class AssemblySelectionRepository:
    """Read and replace the assembly selection of a program."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_assemblies(self, process_id):
        """Return the names of the selected assemblies, sorted."""
        with self._sessions() as session:
            return list(session.scalars(
                select(AssemblySelectionRecord.name)
                .where(AssemblySelectionRecord.process_id == process_id)
                .order_by(AssemblySelectionRecord.name)))

    def save_assemblies(self, process_id, names):
        """Replace the selection with ``names``; none means all assemblies."""
        with self._sessions() as session:
            session.execute(delete(AssemblySelectionRecord).where(
                AssemblySelectionRecord.process_id == process_id))
            session.add_all(AssemblySelectionRecord(process_id=process_id, name=name)
                            for name in sorted(set(names)))
            session.commit()
