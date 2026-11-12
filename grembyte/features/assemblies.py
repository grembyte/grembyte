"""The assemblies the catalog and the Mono trace read.

A game loads dozens of assemblies; most of the game logic usually sits in a
few (``Assembly-CSharp`` and the game's own libraries). The user can pick
them once per program (see grembyte.ui.widgets.assemblies); the selection is
saved and limits the catalog and the trace to them. Without a selection both
use every assembly that is not part of the framework, as before; the
catalog reads framework assemblies on request.
"""


def chosen_assemblies(names, selection, engine):
    """The loaded assemblies (``names``) to read, in their order.

    ``selection`` is the set of selected names, empty for none; then all
    assemblies that are not part of the framework of ``engine``.
    """
    if selection:
        return [name for name in names if name in selection]
    return [name for name in names if not engine.is_framework_assembly(name)]


def missing_assemblies(names, selection):
    """The selected assemblies the game has not loaded (now), sorted."""
    return sorted(set(selection) - set(names))
