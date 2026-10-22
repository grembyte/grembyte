"""Persistence of the user's general settings."""

from sqlalchemy import select

from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import SettingRecord


class SettingsRepository:
    """Read and write the values of hermes.core.settings."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def load(self):
        """{key: value} of the stored settings; settings never changed are missing."""
        with self._sessions() as session:
            return {record.key: record.value for record in session.scalars(select(SettingRecord))}

    def save(self, values):
        """Store {key: value}."""
        with self._sessions() as session:
            for key, value in values.items():
                record = session.get(SettingRecord, key)
                if record is None:
                    session.add(SettingRecord(key=key, value=value))
                else:
                    record.value = value
            session.commit()
