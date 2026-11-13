"""Persistence of the notes the user keeps per saved process (see GameNote)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from grembyte.db.engine import SESSION_LOCAL
from grembyte.db.models import GameNote

# Put between a note's text and a text merged into it by an import
MERGE_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class Note:
    """A saved note: its id, title, Markdown text and when it last changed."""

    note_id: int
    title: str
    body: str
    updated_at: datetime | None = None


def _note(record):
    return Note(record.id, record.title, record.body, record.updated_at)


def merged_text(existing, imported):
    """Append ``imported`` unless it is already a complete merged section.

    Returns None when nothing changes. Nothing of ``existing`` is dropped.
    """
    imported = (imported or "").strip()
    existing = existing or ""
    if not imported or imported in (section.strip() for section in existing.split(
            MERGE_SEPARATOR)):
        return None
    if not existing.strip():
        return imported
    return existing.rstrip() + MERGE_SEPARATOR + imported


class NoteRepository:
    """Read and save the notes of a process."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_notes(self, process_id):
        """The notes (Note) of a process, oldest first."""
        with self._sessions() as session:
            return [_note(record) for record in session.scalars(
                select(GameNote).where(GameNote.process_id == process_id)
                .order_by(GameNote.id))]

    def first_note(self, process_id):
        """The oldest note of a process (the one the notes window shows), or None."""
        notes = self.list_notes(process_id)
        return notes[0] if notes else None

    def save_first_note(self, process_id, body):
        """Save ``body`` as the oldest note, or remove it when it is empty."""
        with self._sessions() as session:
            record = session.scalars(select(GameNote).where(
                GameNote.process_id == process_id).order_by(GameNote.id)).first()
            if not body.strip():
                if record is not None:
                    session.delete(record)
                    session.commit()
                return None
            if record is None:
                record = GameNote(process_id=process_id, title="", body=body)
                session.add(record)
            elif record.body != body:
                record.body = body
            session.commit()
            return _note(record)
