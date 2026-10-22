"""Favorite ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from hermes.db.base import Base


class FavoriteRecord(Base):  # pylint: disable=too-few-public-methods
    """A C# method or native function the user marked (see hermes.features.favorites)."""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("process_id", "key"),)

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(String, nullable=False)  # "method" or "native"
    key = Column(String, nullable=False)
    label = Column(String, nullable=False, default="")
    location = Column(String, nullable=False, default="")
    signature = Column(String, nullable=False, default="")
    data = Column(JSON, nullable=False)
    note = Column(String, nullable=False, default="")
    source = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    process = relationship("Process", back_populates="favorites")
