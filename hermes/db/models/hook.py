"""Hook ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from hermes.db.base import Base


class Hook(Base):  # pylint: disable=too-few-public-methods
    """A saved write hook; the write site is stored as module + offset.

    Absolute addresses change on every start (ASLR), the offset of an
    instruction inside its module does not as long as the binary is unchanged.
    ``instruction_bytes`` guards against patching a changed binary.

    A write instruction in JIT code has no module (``module`` is ""); such a
    hook is found again by training: ``signature`` holds the code bytes around
    the instruction (``{"before": hex, "after": hex}``) and ``pointer`` the
    target the agent watches for a write from matching code.
    In Mono games ``managed`` also names the C# method of such a write
    site (see ``HookSpec.managed``); the hook is then installed right away
    after a restart, training is the fallback.
    ``callers`` holds the learned caller signatures, each a list of
    ``caller_depth`` ``[module, offset]`` return addresses.
    With ``breakpoint`` the hook uses a hardware breakpoint instead of a
    patched jump (see ``HookSpec.breakpoint``); NULL in older rows means no.

    The manipulation is a copy of the template it was created from
    (``template_id``), so editing or deleting the template does not change
    the hook. ``template_id`` is no foreign key: it is kept after the template
    is deleted so the hook dialog can tell the user.
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
    value_type = Column(String, nullable=False)          # see hermes.core.value_types
    template_id = Column(Integer, nullable=True)
    transform_name = Column(String, nullable=False)
    transform_source = Column(Text, nullable=False)      # copy of the template's function
    transform_parameters = Column(JSON, nullable=False)  # copy of its parameter definitions
    transform_params = Column(JSON, nullable=False)      # parameter values of this hook
    enabled = Column(Boolean, nullable=False, default=True)
    caller_depth = Column(Integer, nullable=False, default=1)
    callers = Column(JSON, nullable=False, default=list)
    pointer = Column(JSON, nullable=True)                # PointerChain.to_json() of the target
    signature = Column(JSON, nullable=True)              # code around a JIT write instruction
    managed = Column(JSON, nullable=True)                # C# method of a JIT write instruction
    breakpoint = Column(Boolean, nullable=True)          # hardware breakpoint, no code patch
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    process = relationship("Process", back_populates="hooks")
