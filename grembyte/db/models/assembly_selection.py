"""Assembly selection ORM model."""

from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from grembyte.db.base import Base


class AssemblySelectionRecord(Base):  # pylint: disable=too-few-public-methods
    """An assembly the catalog and the trace of a program are limited to.

    See grembyte.features.assemblies; a program without rows uses all assemblies.
    """

    __tablename__ = "assembly_selections"
    __table_args__ = (UniqueConstraint("process_id", "name"),)

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String, nullable=False)

    process = relationship("Process", back_populates="assembly_selections")
