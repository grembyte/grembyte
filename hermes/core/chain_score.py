"""How promising a pointer chain is: points for what makes chains last.

A chain that starts in the game's own module, has few steps and small
offsets, and steps through aligned pointers to the start of objects is more
likely to survive a restart or an update than one through a system library
with large offsets into the middle of structures. The points are a guide for
sorting the candidates, not a test; filtering after a restart is.

Every pointer of a chain except the last lies at an aligned address (the
scan reads aligned words), which is the previous pointer value plus its
offset. So a pointer value is aligned exactly when the offset after it is a
multiple of the pointer size; the last offset leads to the value itself and
does not count. A scan at 4-byte steps (``location_step``) also finds
pointers at addresses that are not multiples of 8; there the points go to
chains whose offsets are, which is the best the chain itself tells.
"""

from dataclasses import dataclass

from hermes.core import memory_regions

# Points by the memory category the chain starts in.
START_POINTS = {
    memory_regions.MAIN: 40,
    memory_regions.ASSEMBLIES: 40,
    memory_regions.GAME: 30,
    memory_regions.ENGINE: 20,
    memory_regions.SYSTEM: 0,
    memory_regions.UNKNOWN: 0,
}
# Points by the number of steps (offsets); longer chains get LONG_POINTS.
LENGTH_POINTS = {1: 20, 2: 10, 3: 0, 4: -5, 5: -10}
LONG_POINTS = -15
# Points if every offset is at most the limit; the first limit that fits counts.
OFFSET_POINTS = ((0x100, 10), (0x400, 5))
# Points if every pointer of the chain points to an aligned address.
ALIGNED_POINTS = 10


@dataclass(frozen=True)
class Score:
    """The total and its parts, ``((reason, points, detail), ...)``.

    ``reason`` is "start" (detail: the category, None if unknown), "length"
    (the number of steps), "offsets" (the limit all offsets keep) or
    "aligned".
    """

    total: int
    parts: tuple


def score(chain, category, pointer_size=8):
    """Return the Score of ``chain`` starting in memory of ``category`` (or None)."""
    parts = []
    if category is not None:
        parts.append(("start", START_POINTS.get(category, 0), category))
    steps = len(chain.offsets)
    parts.append(("length", LENGTH_POINTS.get(steps, LONG_POINTS), steps))
    largest = max((abs(offset) for offset in chain.offsets), default=0)
    limit = next(((limit, points) for limit, points in OFFSET_POINTS if largest <= limit), None)
    if limit is not None:
        parts.append(("offsets", limit[1], limit[0]))
    if all(offset % pointer_size == 0 for offset in chain.offsets[:-1]):
        parts.append(("aligned", ALIGNED_POINTS, None))
    return Score(sum(points for _reason, points, _detail in parts), tuple(parts))


def ranked(chains, categories, pointer_size=8):
    """Return ``[(chain, Score)]``, best first; equal scores keep their order.

    ``categories`` maps module names to memory categories (modules missing
    from it get no start points).
    """
    scored = [(chain, score(chain, categories.get(chain.module), pointer_size))
              for chain in chains]
    return sorted(scored, key=lambda pair: -pair[1].total)
