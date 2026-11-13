"""Persistence of the scripts of known processes."""

from sqlalchemy import select

from grembyte.db.engine import SESSION_LOCAL
from grembyte.db.models import GameScript


class ScriptRepository:
    """Create, list, change and delete the saved scripts of a process."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_scripts(self, process_id):
        """Return the saved scripts of a process in creation order."""
        with self._sessions() as session:
            return list(session.scalars(
                select(GameScript).where(GameScript.process_id == process_id)
                .order_by(GameScript.id)
            ))

    def add_script(self, process_id, *, label, source, mode, seconds, enabled=True,
                   params=None, hotkey=None):
        # pylint: disable=too-many-arguments
        """Save a script and return its id."""
        with self._sessions() as session:
            script = GameScript(process_id=process_id, label=label, source=source, mode=mode,
                                seconds=seconds, enabled=enabled, params=params,
                                hotkey=hotkey or None)
            session.add(script)
            session.commit()
            return script.id

    def update_script(self, script_id, *, label=None, source=None, mode=None, seconds=None,
                      enabled=None, params=None, last_args=None, hotkey=None):
        # pylint: disable=too-many-arguments
        """Change the given fields of a saved script; ``None`` keeps a field
        (``params=""`` removes the parameters, ``hotkey=""`` the hotkey)."""
        with self._sessions() as session:
            script = session.get(GameScript, script_id)
            if script is None:
                return
            for name, value in (("label", label), ("source", source), ("mode", mode),
                                ("seconds", seconds), ("enabled", enabled)):
                if value is not None:
                    setattr(script, name, value)
            for name, value in (("params", params), ("last_args", last_args),
                                ("hotkey", hotkey)):
                if value is not None:
                    setattr(script, name, value or None)
            session.commit()

    def delete_script(self, script_id):
        """Delete a saved script."""
        with self._sessions() as session:
            script = session.get(GameScript, script_id)
            if script is not None:
                session.delete(script)
                session.commit()
