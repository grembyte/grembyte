"""Method hook ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from hermes.db.base import Base


class MethodHook(Base):  # pylint: disable=too-few-public-methods
    """A saved hook on a C# method of a Mono game.

    ``managed`` identifies the method like ``Hook.managed`` (assembly,
    metadata token, signature); it stays valid while the game is unchanged.
    The manipulation is a copy of a method hook template, as for ``Hook``.
    """

    __tablename__ = "method_hooks"

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    label = Column(String, nullable=False, default="")
    managed = Column(JSON, nullable=False)
    template_id = Column(Integer, nullable=True)
    transform_name = Column(String, nullable=False)
    transform_source = Column(Text, nullable=False)
    transform_parameters = Column(JSON, nullable=False)
    transform_params = Column(JSON, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    phase = Column(String, nullable=True)  # "before" the call, else after it
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    process = relationship("Process", back_populates="method_hooks")
