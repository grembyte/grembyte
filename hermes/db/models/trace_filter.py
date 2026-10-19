"""Trace filter ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Integer, String

from hermes.db.base import Base


def _now():
    return datetime.now(timezone.utc)


class TraceFilterRecord(Base):  # pylint: disable=too-few-public-methods
    """A user's filter for the trace (see hermes.features.trace_filters).

    Not tied to a process: a filter like "gold" is useful in many games.
    """

    __tablename__ = "trace_filters"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=False, default="")
    definition = Column(JSON, nullable=False)  # TraceFilter.to_json()
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
