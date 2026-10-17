"""Hook ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from hermes.db.base import Base


class Hook(Base):  # pylint: disable=too-few-public-methods
    """A saved write hook; the write site is stored as module + offset.

    Absolute addresses change on every start (ASLR), the offset of an
    instruction inside its module does not as long as the binary is unchanged.
    ``instruction_bytes`` guards against patching a changed binary.
    ``callers`` holds the learned caller signatures, each a list of
    ``caller_depth`` ``[module, offset]`` return addresses.
    """

    __tablename__ = "hooks"

    id = Column(Integer, primary_key=True)
    process_id = Column(
        Integer, ForeignKey("processes.id", ondelete="CASCADE"), nullable=False
    )
    label = Column(String, nullable=False, default="")
    module = Column(String, nullable=False)              # e.g. "game" or "libfoo.so"
    module_offset = Column(Integer, nullable=False)      # write instruction offset
    instruction_bytes = Column(String, nullable=False)   # hex, e.g. "0137"
    instruction_text = Column(String, nullable=False, default="")
    value_type = Column(String, nullable=False)          # int32, int64, float, double
    transform_kind = Column(String, nullable=False)
    transform_params = Column(JSON, nullable=False)      # ValueTransform.form_values()
    enabled = Column(Boolean, nullable=False, default=True)
    caller_depth = Column(Integer, nullable=False, default=1)
    callers = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    process = relationship("Process", back_populates="hooks")
