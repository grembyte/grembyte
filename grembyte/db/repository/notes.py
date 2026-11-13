"""Persistence of the notes the user keeps per saved process (see GameNote)."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from grembyte.db.engine import SESSION_LOCAL
from grembyte.db.models import GameNote

# Put between a note's text and a text merged into it by an import
MERGE_SEPARATOR = "\n\n---\n\n"
# The separator as it may look after editing; a rule the user wrote looks the same
_SECTION_BREAK = re.compile(r"\n[ \t]*\n {0,3}---[ \t]*\n(?:[ \t]*\n)+")
# The characters around a note's text that do not count: SQL's trim() takes
# the same, so that the notes counted (game_export) are the notes exported
BLANK = " \t\n\r"


@dataclass(frozen=True)
class Note:
    """A saved note: its id, title, Markdown text and when it last changed."""

    note_id: int
    title: str
    body: str
    updated_at: datetime | None = None


def _note(record):
    return Note(record.id, record.title, record.body, record.updated_at)


def _sections(text):
    return [section.strip(BLANK) for section in _SECTION_BREAK.split(text)]


def merged_text(existing, imported):
    """Append the sections of ``imported`` that ``existing`` does not hold.

    Sections are the parts between merge separators (or rules looking the
    same), so a note merged back from another computer only adds what is new
    there. Returns None when nothing changes. Nothing of ``existing`` is dropped.
    """
    imported = (imported or "").strip(BLANK)
    existing = existing or ""
    if not imported:
        return None
    if not existing.strip(BLANK):
        return imported
    known = set(_sections(existing))
    new = []
    for section in _sections(imported):
        if section and section not in known:
            known.add(section)
            new.append(section)
    if not new:
        return None
    return existing.rstrip(BLANK) + MERGE_SEPARATOR + MERGE_SEPARATOR.join(new)


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
        """Save ``body`` as the oldest note, or remove it when it is empty.

        An emptied note the process has further notes of (from an import) is
        kept empty, so that the window does not show the next one instead.
        """
        with self._sessions() as session:
            record = session.scalars(select(GameNote).where(
                GameNote.process_id == process_id).order_by(GameNote.id)).first()
            if not body.strip(BLANK):
                if record is None:
                    return None
                others = session.scalar(select(func.count()).select_from(GameNote).where(
                    GameNote.process_id == process_id, GameNote.id != record.id))
                if not others:
                    session.delete(record)
                    session.commit()
                    return None
                if record.body:
                    record.body = ""
                    session.commit()
                return None
            if record is None:
                record = GameNote(process_id=process_id, title="", body=body)
                session.add(record)
            elif record.body != body:
                record.body = body
            session.commit()
            return _note(record)
