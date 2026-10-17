"""Pointer scan ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer

from hermes.db.base import Base


def _now():
    return datetime.now(timezone.utc)


class PointerScan(Base):  # pylint: disable=too-few-public-methods
    """Candidate pointer chains of an address-list entry.

    Kept between restarts of the program so the candidates can be filtered
    with the address the value has in each new run.
    """

    __tablename__ = "pointer_scans"

    id = Column(Integer, primary_key=True)
    address_id = Column(
        Integer, ForeignKey("addresses.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    max_depth = Column(Integer, nullable=False)
    max_offset = Column(Integer, nullable=False)
    chains = Column(JSON, nullable=False)             # [PointerChain.to_json()]
    rounds = Column(Integer, nullable=False, default=0)  # times filtered after a restart
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
