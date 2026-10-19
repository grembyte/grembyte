"""Persistence of the user's trace filters."""

from sqlalchemy import select

from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import TraceFilterRecord
from hermes.features.trace_filters import TraceFilter


class TraceFilterRepository:
    """Create, list, change and delete the user's trace filters."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_filters(self):
        """Return the user's filters (TraceFilter), sorted by name."""
        with self._sessions() as session:
            return [
                TraceFilter.from_json(record.name, record.definition, filter_id=record.id,
                                      description=record.description)
                for record in session.scalars(
                    select(TraceFilterRecord).order_by(TraceFilterRecord.name))
            ]

    def save_filter(self, trace_filter):
        """Create or update (by ``filter_id``) a filter and return its id.

        Raises ValueError when another filter already has the name.
        """
        with self._sessions() as session:
            duplicate = session.scalars(select(TraceFilterRecord).where(
                TraceFilterRecord.name == trace_filter.name,
                TraceFilterRecord.id != trace_filter.filter_id)).first()
            if duplicate is not None:
                raise ValueError(f"A filter named {trace_filter.name!r} already exists")
            record = (None if trace_filter.filter_id is None
                      else session.get(TraceFilterRecord, trace_filter.filter_id))
            if record is None:
                record = TraceFilterRecord()
                session.add(record)
            record.name = trace_filter.name
            record.description = trace_filter.description
            record.definition = trace_filter.to_json()
            session.commit()
            return record.id

    def delete_filter(self, filter_id):
        """Delete a filter."""
        with self._sessions() as session:
            record = session.get(TraceFilterRecord, filter_id)
            if record is not None:
                session.delete(record)
            session.commit()
