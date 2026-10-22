"""Process ORM model."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from hermes.db.base import Base


class Process(Base):  # pylint: disable=too-few-public-methods
    """A known target program, identified by name and path rather than PID."""

    __tablename__ = "processes"
    __table_args__ = (UniqueConstraint("name", "path"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)          # e.g. "game" or "main.py"
    path = Column(String, nullable=False)          # executable (or script) path
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    hooks = relationship("Hook", back_populates="process", cascade="all, delete-orphan")
    method_hooks = relationship(
        "MethodHook", back_populates="process", cascade="all, delete-orphan"
    )
    favorites = relationship(
        "FavoriteRecord", back_populates="process", cascade="all, delete-orphan"
    )
    assembly_selections = relationship(
        "AssemblySelectionRecord", back_populates="process", cascade="all, delete-orphan"
    )
