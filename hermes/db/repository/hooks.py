"""Persistence of known processes and their saved hooks."""

from sqlalchemy import select

from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import Hook, Process


class HookRepository:
    """Store hooks per program so they can be reinstalled after a restart."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def get_or_create_process(self, name, path):
        """Return the id of the process entry for ``name`` and ``path``."""
        with self._sessions() as session:
            process = session.scalars(
                select(Process).where(Process.name == name, Process.path == path)
            ).first()
            if process is None:
                process = Process(name=name, path=path)
                session.add(process)
                session.commit()
            return process.id

    def list_hooks(self, process_id):
        """Return the saved hooks of a process in creation order."""
        with self._sessions() as session:
            return list(session.scalars(
                select(Hook).where(Hook.process_id == process_id).order_by(Hook.id)
            ))

    def add_hook(self, process_id, *, label, module, module_offset,
                 instruction_bytes, instruction_text, value_type, transform,
                 enabled=True, caller_depth=1, callers=()):
        # pylint: disable=too-many-arguments
        """Save a hook and return its id."""
        with self._sessions() as session:
            hook = Hook(
                process_id=process_id,
                label=label,
                module=module,
                module_offset=module_offset,
                instruction_bytes=instruction_bytes,
                instruction_text=instruction_text,
                value_type=value_type,
                transform_kind=transform.kind,
                transform_params=transform.form_values(),
                enabled=enabled,
                caller_depth=caller_depth,
                callers=[list(map(list, frames)) for frames in callers],
            )
            session.add(hook)
            session.commit()
            return hook.id

    def update_hook(self, hook_id, *, label=None, transform=None, enabled=None,
                    callers=None):
        """Change the given fields of a saved hook; ``None`` keeps a field."""
        with self._sessions() as session:
            hook = session.get(Hook, hook_id)
            if hook is None:
                return
            if label is not None:
                hook.label = label
            if transform is not None:
                hook.transform_kind = transform.kind
                hook.transform_params = transform.form_values()
            if enabled is not None:
                hook.enabled = enabled
            if callers is not None:
                hook.callers = [list(map(list, frames)) for frames in callers]
            session.commit()

    def delete_hook(self, hook_id):
        """Delete a saved hook."""
        with self._sessions() as session:
            hook = session.get(Hook, hook_id)
            if hook is not None:
                session.delete(hook)
                session.commit()
