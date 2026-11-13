"""Game note ORM model."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from grembyte.db.base import Base


def _now():
    return datetime.now(timezone.utc)


class GameNote(Base):  # pylint: disable=too-few-public-methods
    """A note (Markdown) the user keeps on a saved process.

    The notes window shows one note per process (the oldest); the table holds
    several already, each with a ``title``, so that more can be offered later.
    """

    __tablename__ = "game_notes"

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    title = Column(String, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    process = relationship("Process", back_populates="notes")
