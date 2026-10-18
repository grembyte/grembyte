"""Hook template ORM model."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Integer, String, Text

from hermes.db.base import Base


def _now():
    return datetime.now(timezone.utc)


class HookTemplate(Base):  # pylint: disable=too-few-public-methods
    """A reusable manipulation (JavaScript function and its parameters).

    Templates are not tied to a process. Hooks keep a copy of the template
    they were created from. Ids are never reused (AUTOINCREMENT), so a hook
    whose template was deleted cannot be mistaken for one of a new template.
    """

    __tablename__ = "hook_templates"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=False, default="")
    source = Column(Text, nullable=False)                 # JavaScript function
    parameters = Column(JSON, nullable=False)             # TransformParameter.to_json()
    seed_key = Column(String, nullable=True, unique=True)  # set for example templates
    kind = Column(String, nullable=True)                  # "method", else a value hook
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
