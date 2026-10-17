"""Address list ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String

from hermes.db.base import Base


class Address(Base):  # pylint: disable=too-few-public-methods
    """An entry of the address list of a program, e.g. "Gold".

    ``pointer`` (``PointerChain.to_json()``) finds the value again after the
    program was restarted; ``address`` is the last address it resolved to.
    """

    __tablename__ = "addresses"

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    label = Column(String, nullable=False, default="")
    value_type = Column(String, nullable=False)      # int32, int64, float, double
    address = Column(Integer, nullable=False)
    pointer = Column(JSON, nullable=True)
    frozen = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
