"""Persistence of known processes, their saved hooks and hook templates."""

from sqlalchemy import select

from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import Hook, HookTemplate, MethodHook, Process
from hermes.hooks.transforms import (
    TemplateTransform,
    TransformDefinition,
    as_template_transform,
)


def _template_definition(template):
    return TransformDefinition.from_json(
        template.name,
        template.source,
        template.parameters,
        description=template.description,
        template_id=template.id,
        kind=template.kind or "value",
    )


def hook_transform(hook, kind="value"):
    """Return the manipulation copied into a saved hook (or method hook)."""
    definition = TransformDefinition.from_json(
        hook.transform_name,
        hook.transform_source,
        hook.transform_parameters,
        template_id=hook.template_id,
        kind=kind,
    )
    return TemplateTransform(definition, **hook.transform_params)


def _store_transform(hook, transform):
    transform = as_template_transform(transform)
    definition = transform.definition
    hook.template_id = definition.template_id
    hook.transform_name = definition.name
    hook.transform_source = definition.source
    hook.transform_parameters = definition.parameters_json()
    hook.transform_params = transform.form_values()


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
                 enabled=True, caller_depth=1, callers=(), pointer=None, signature=None,
                 managed=None):
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
                enabled=enabled,
                caller_depth=caller_depth,
                callers=[list(map(list, frames)) for frames in callers],
                pointer=None if pointer is None else pointer.to_json(),
                signature=signature,
                managed=managed,
            )
            _store_transform(hook, transform)
            session.add(hook)
            session.commit()
            return hook.id

    def update_hook(self, hook_id, *, label=None, transform=None, enabled=None,
                    callers=None, managed=None):
        # pylint: disable=too-many-arguments
        """Change the given fields of a saved hook; ``None`` keeps a field."""
        with self._sessions() as session:
            hook = session.get(Hook, hook_id)
            if hook is None:
                return
            if label is not None:
                hook.label = label
            if transform is not None:
                _store_transform(hook, transform)
            if enabled is not None:
                hook.enabled = enabled
            if callers is not None:
                hook.callers = [list(map(list, frames)) for frames in callers]
            if managed is not None:
                hook.managed = managed
            session.commit()

    # ------------------------------------------------------------------
    # Method hooks
    # ------------------------------------------------------------------
    def list_method_hooks(self, process_id):
        """Return the saved method hooks of a process in creation order."""
        with self._sessions() as session:
            return list(session.scalars(
                select(MethodHook).where(MethodHook.process_id == process_id)
                .order_by(MethodHook.id)
            ))

    def add_method_hook(self, process_id, *, label, managed, transform, enabled=True,
                        phase="after"):
        # pylint: disable=too-many-arguments
        """Save a method hook and return its id."""
        with self._sessions() as session:
            hook = MethodHook(process_id=process_id, label=label, managed=managed,
                              enabled=enabled, phase=phase)
            _store_transform(hook, transform)
            session.add(hook)
            session.commit()
            return hook.id

    def update_method_hook(self, hook_id, *, label=None, transform=None, enabled=None,
                           phase=None):
        # pylint: disable=too-many-arguments
        """Change the given fields of a saved method hook; ``None`` keeps a field."""
        with self._sessions() as session:
            hook = session.get(MethodHook, hook_id)
            if hook is None:
                return
            if label is not None:
                hook.label = label
            if transform is not None:
                _store_transform(hook, transform)
            if enabled is not None:
                hook.enabled = enabled
            if phase is not None:
                hook.phase = phase
            session.commit()

    def delete_method_hook(self, hook_id):
        """Delete a saved method hook."""
        with self._sessions() as session:
            hook = session.get(MethodHook, hook_id)
            if hook is not None:
                session.delete(hook)
                session.commit()

    def delete_hook(self, hook_id):
        """Delete a saved hook."""
        with self._sessions() as session:
            hook = session.get(Hook, hook_id)
            if hook is not None:
                session.delete(hook)
                session.commit()

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    def list_templates(self):
        """Return all hook templates as TransformDefinitions, sorted by name."""
        with self._sessions() as session:
            return [
                _template_definition(template)
                for template in session.scalars(
                    select(HookTemplate).order_by(HookTemplate.name)
                )
            ]

    def save_template(self, definition, template_id=None):
        """Create or update a template and return its id.

        Raises ValueError when another template already has the name.
        """
        with self._sessions() as session:
            duplicate = session.scalars(
                select(HookTemplate).where(
                    HookTemplate.name == definition.name, HookTemplate.id != template_id
                )
            ).first()
            if duplicate is not None:
                raise ValueError(f"A template named {definition.name!r} already exists")
            template = None if template_id is None else session.get(HookTemplate, template_id)
            if template is None:
                template = HookTemplate()
                session.add(template)
            template.name = definition.name
            template.description = definition.description
            template.source = definition.source
            template.parameters = definition.parameters_json()
            template.kind = definition.kind
            session.commit()
            return template.id

    def delete_template(self, template_id):
        """Delete a template; hooks created from it keep their copy."""
        with self._sessions() as session:
            template = session.get(HookTemplate, template_id)
            if template is not None:
                session.delete(template)
            session.commit()

    def ensure_templates(self, examples):
        """Create the example templates ``{seed_key: definition}`` that are missing.

        An example counts as existing when a template with its seed key or its
        name is present.
        """
        with self._sessions() as session:
            existing = session.execute(select(HookTemplate.seed_key, HookTemplate.name)).all()
            seed_keys = {seed_key for seed_key, _name in existing}
            names = {name for _seed_key, name in existing}
            for seed_key, definition in examples.items():
                if seed_key in seed_keys or definition.name in names:
                    continue
                session.add(HookTemplate(
                    name=definition.name,
                    description=definition.description,
                    source=definition.source,
                    parameters=definition.parameters_json(),
                    seed_key=seed_key,
                    kind=definition.kind,
                ))
            session.commit()
