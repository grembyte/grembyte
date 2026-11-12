"""Setting ORM model."""

from sqlalchemy import JSON, Column, String

from grembyte.db.base import Base


class SettingRecord(Base):  # pylint: disable=too-few-public-methods
    """The value of one of the user's general settings (see grembyte.core.settings)."""

    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=True)
