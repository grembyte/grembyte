"""The hooks saved in the database, as specs to install them again.

The hook panel installs a program's saved hooks whenever it is attached,
grembyte-cli whenever it starts the game; both build the specs here.
"""

from grembyte.core.pointer import PointerChain
from grembyte.db.repository.hooks import hook_transform
from grembyte.features.method_hooks import MethodHookSpec
from grembyte.hooks.engine import HookSpec


def persistable_callers(callers):
    """Keep the caller signatures whose frames all lie inside a module."""
    return [
        [tuple(frame) for frame in frames]
        for frames in callers
        if all(frame is not None for frame in frames)
    ]


def spec_from_saved(saved):
    """Build the HookSpec that reinstalls a hook saved in the database.

    A hook on JIT code (no module) is found again by its C# method if it
    has one (Mono games), else trained again via its signature.
    """
    jit = not saved.module
    return HookSpec(
        None,
        saved.value_type,
        hook_transform(saved),
        module=None if jit else saved.module,
        module_offset=None if jit else saved.module_offset,
        signature=saved.signature if jit else None,
        managed=saved.managed if jit else None,
        breakpoint=bool(saved.breakpoint),
        instruction_bytes=saved.instruction_bytes,
        enabled=saved.enabled,
        caller_depth=saved.caller_depth,
        callers=persistable_callers(saved.callers),
        pointer=None if saved.pointer is None else PointerChain.from_json(saved.pointer),
        label=saved.label,
    )


def method_spec_from_saved(saved):
    """Build the MethodHookSpec that reinstalls a saved method hook."""
    return MethodHookSpec(saved.managed, hook_transform(saved, kind="method"),
                          enabled=saved.enabled, label=saved.label,
                          phase=saved.phase or "after")
