"""Game script ORM model."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from grembyte.db.base import Base


class GameScript(Base):  # pylint: disable=too-few-public-methods
    """A saved script of a process: code that calls game code by time.

    ``mode`` and ``seconds`` say when it runs (see grembyte.features.scripts);
    ``params`` are its parameters (JSON, see params_to_json), ``last_args``
    the values they were last run with (JSON, by name).
    """

    __tablename__ = "game_scripts"

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    label = Column(String, nullable=False, default="")
    source = Column(Text, nullable=False)
    mode = Column(String, nullable=False, default="manual")
    seconds = Column(Float, nullable=False, default=0.0)
    enabled = Column(Boolean, nullable=False, default=True)
    params = Column(Text, nullable=True)
    last_args = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    process = relationship("Process", back_populates="scripts")
