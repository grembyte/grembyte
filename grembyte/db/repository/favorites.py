"""Persistence of the user's favorite methods and functions."""

from sqlalchemy import select

from grembyte.db.engine import SESSION_LOCAL
from grembyte.db.models import FavoriteRecord
from grembyte.features.favorites import Favorite


def _favorite(record):
    return Favorite(record.kind, record.key, record.label, record.location, record.signature,
                    dict(record.data), note=record.note, source=record.source,
                    favorite_id=record.id, created_at=record.created_at)


class FavoriteRepository:
    """Save, list, annotate and delete the favorites of a program."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_favorites(self, process_id):
        """Return the favorites (Favorite) of a process, oldest first."""
        with self._sessions() as session:
            return [_favorite(record) for record in session.scalars(
                select(FavoriteRecord).where(FavoriteRecord.process_id == process_id)
                .order_by(FavoriteRecord.id))]

    def add_favorite(self, process_id, favorite):
        """Save a favorite and return its id; one already saved under its key keeps its note."""
        with self._sessions() as session:
            record = session.scalars(select(FavoriteRecord).where(
                FavoriteRecord.process_id == process_id,
                FavoriteRecord.key == favorite.key)).first()
            if record is None:
                record = FavoriteRecord(process_id=process_id, key=favorite.key,
                                        note=favorite.note)
                session.add(record)
            record.kind = favorite.kind
            record.label = favorite.label
            record.location = favorite.location
            record.signature = favorite.signature
            record.data = favorite.data
            record.source = favorite.source
            session.commit()
            return record.id

    def set_note(self, favorite_id, note):
        """Change the note of a favorite."""
        with self._sessions() as session:
            record = session.get(FavoriteRecord, favorite_id)
            if record is not None:
                record.note = note
            session.commit()

    def delete_favorite(self, process_id, key):
        """Delete the favorite saved under ``key``."""
        with self._sessions() as session:
            for record in session.scalars(select(FavoriteRecord).where(
                    FavoriteRecord.process_id == process_id, FavoriteRecord.key == key)):
                session.delete(record)
            session.commit()
