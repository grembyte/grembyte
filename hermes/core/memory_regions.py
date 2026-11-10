"""Categories of a process' writable memory, for the pointer scan.

A pointer chain starts in the static data of a module, so modules are sorted
by who wrote them:

``main``
    The program itself (``main_module``).
``game``
    Other modules in the game's folder: its own libraries and plugins.
``engine``
    Runtimes of game engines and middleware (Unity, Mono, IL2CPP, Unreal's
    modular builds, PhysX, FMOD, Wwise, Steam ...), wherever they lie.
    Matched by name before ``game``, since games ship them in their folder.
``system``
    The operating system, Wine and the drivers: libraries under /usr and
    /lib, Wine's and Proton's own DLLs, Windows' system folder.
``unknown``
    Modules none of the rules fits (Steam's overlay in the home folder, ...).

All other writable memory lies between the steps of a chain:

``heap``
    Memory the program allocated: [heap] and anonymous mappings. Nearly every
    chain goes through it.
``stack``
    Thread stacks. Their contents change all the time; pointers there are
    rarely part of a stable chain. On Linux (Wine included) these are
    [stack] and the memory around each thread's stack pointer
    (``thread_stack_pointers``): ``STACK_BELOW`` under it, ``STACK_ABOVE``
    over it, within its mapping. Not whole mappings, since the kernel merges
    neighbouring anonymous mappings, several threads' stacks and heap alike,
    into one. On Windows a stack is the committed memory right above its
    guard page.
"""

import ntpath
import os
import re
from dataclasses import dataclass, field

MAIN = "main"
GAME = "game"
ENGINE = "engine"
SYSTEM = "system"
UNKNOWN = "unknown"
HEAP = "heap"
STACK = "stack"

MODULE_CATEGORIES = (MAIN, GAME, ENGINE, SYSTEM, UNKNOWN)
DYNAMIC_CATEGORIES = (HEAP, STACK)
CATEGORIES = MODULE_CATEGORIES + DYNAMIC_CATEGORIES
# What a scan uses unless chosen otherwise.
DEFAULT_CATEGORIES = frozenset((MAIN, GAME, ENGINE, HEAP))
# Without the heap hardly any chain has more than one step.
REQUIRED_CATEGORIES = frozenset((HEAP,))

# Stack memory around a thread's stack pointer: below it lies nothing in
# use, above it the frames of the calls the thread is in.
STACK_BELOW = 64 * 1024
STACK_ABOVE = 1024 * 1024

_ENGINE_NAMES = re.compile(
    r"^("
    # Unity: player, scripting runtimes, IL2CPP and its libraries
    r"unityplayer|gameassembly|baselib|libil2cpp|libmain|lib_burst_generated"
    r"|libmono(bdwgc|sgen)?(-2\.0)?|mono(-2\.0-(bdwgc|sgen))?|monoposixhelper"
    # Unreal's modular builds load the engine as one library per module
    r"|(lib)?(ue4|ue5|unrealeditor)-.*"
    # Middleware shipped with games
    r"|physx.*|nvcloth.*|apex.*|fmod.*|libfmod.*|aksoundengine|wwise.*|bink.*|libbink.*"
    r"|steam_api(64)?|libsteam_api|steamclient(64)?|eossdk-.*|libeossdk-.*"
    r"|galaxy(64)?|discord_game_sdk|libdiscord_game_sdk|amd_ags_x64|nvngx_.*"
    r"|sl\.interposer|ffx_.*"
    r")(\.(dll|so)(\.[\d.]+)?)?$",
    re.IGNORECASE)

# Folders of the system and of Wine. Under Wine a builtin DLL is mapped from
# Wine's lib/wine/x86_64-windows (or Proton's files/lib/...), a DLL of the
# prefix from drive_c/windows.
_SYSTEM_PATHS = re.compile(
    r"^/(usr|lib|lib32|lib64|opt|nix|snap|run/host)/"
    r"|/lib(32|64)?/wine/|/files/lib(32|64)?/|/dist/lib(32|64)?/"
    r"|/drive_c/windows/"
    r"|^[a-z]:\\windows\\",
    re.IGNORECASE)


@dataclass(frozen=True)
class Region:
    """Writable memory of one category; ``module`` for module memory."""

    start: int
    end: int
    category: str
    module: str | None = None


@dataclass
class RegionSummary:
    """The writable memory of one category: its size and the modules in it."""

    size: int = 0
    modules: list = field(default_factory=list)


def _module_paths(mappings):
    """``{module: path}`` from the first mapping of each module that names its file."""
    paths = {}
    for mapping in mappings:
        if mapping.module is not None and mapping.path and mapping.module not in paths:
            paths[mapping.module] = mapping.path
    return paths


def _normalized(path):
    return path.lower().replace("\\", "/")


def _folder(path):
    """Folder of ``path``, Windows paths included, normalized, with a trailing slash."""
    flavour = ntpath if "\\" in path else os.path
    return _normalized(flavour.dirname(path)).rstrip("/") + "/"


def module_categories(mappings, main_module):
    """Return ``{module name: category}`` for the modules in ``mappings``."""
    paths = _module_paths(mappings)
    main_name = main_module.lower() if main_module else None
    main_path = next((path for module, path in paths.items() if module.lower() == main_name),
                     None)
    game_folder = _folder(main_path) if main_path else None
    categories = {}
    for mapping in mappings:
        module = mapping.module
        if module is None or module in categories:
            continue
        path = paths.get(module, "")
        if module.lower() == main_name:
            category = MAIN
        elif _ENGINE_NAMES.match(module):
            category = ENGINE
        elif path and _SYSTEM_PATHS.search(path):
            category = SYSTEM
        elif game_folder and path and _normalized(path).startswith(game_folder):
            category = GAME
        else:
            category = UNKNOWN
        categories[module] = category
    return categories


def stack_ranges(mappings, stack_pointers):
    """Return the merged ``[(start, end)]`` of thread stacks.

    ``stack_pointers`` None means the system gives none (Windows): then a
    stack is anonymous writable memory right above an inaccessible page.
    """
    ordered = sorted(mappings, key=lambda mapping: mapping.start)
    dynamic = [mapping for mapping in ordered if mapping.writable and mapping.module is None]
    ranges = [(mapping.start, mapping.end) for mapping in dynamic if mapping.path == "[stack]"]
    if stack_pointers is None:
        below = None
        for mapping in ordered:
            if (mapping.writable and mapping.module is None and not mapping.path
                    and below is not None and below.end == mapping.start
                    and below.module is None and below.permissions.startswith("---")):
                ranges.append((mapping.start, mapping.end))
            below = mapping
    else:
        for pointer in stack_pointers:
            mapping = next((m for m in dynamic if m.start <= pointer < m.end), None)
            if mapping is not None:
                ranges.append((max(mapping.start, pointer - STACK_BELOW),
                               min(mapping.end, pointer + STACK_ABOVE)))
    return _merged(ranges)


def _merged(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _without(start, end, holes):
    """Parts of [start, end) outside the sorted, merged ``holes``."""
    parts = []
    for low, high in holes:
        if high <= start or low >= end:
            continue
        if low > start:
            parts.append((start, low))
        start = max(start, high)
    if start < end:
        parts.append((start, end))
    return parts


def classify(mappings, main_module, stack_pointers):
    """Return the writable memory as sorted ``[Region]``, each of one category."""
    modules = module_categories(mappings, main_module)
    stacks = stack_ranges(mappings, stack_pointers)
    regions = []
    for mapping in sorted(mappings, key=lambda m: m.start):
        if not mapping.writable:
            continue
        if mapping.module is not None:
            regions.append(Region(mapping.start, mapping.end, modules[mapping.module],
                                  mapping.module))
            continue
        for start, end in _without(mapping.start, mapping.end, stacks):
            regions.append(Region(start, end, HEAP))
        for low, high in stacks:
            low, high = max(low, mapping.start), min(high, mapping.end)
            if low < high:
                regions.append(Region(low, high, STACK))
    regions.sort(key=lambda region: region.start)
    return regions


def summarize(regions):
    """Return ``{category: RegionSummary}`` of ``regions`` (from ``classify``)."""
    summary = {category: RegionSummary() for category in CATEGORIES}
    seen = set()
    for region in regions:
        summary[region.category].size += region.end - region.start
        if region.module is not None and region.module not in seen:
            seen.add(region.module)
            summary[region.category].modules.append(region.module)
    for entry in summary.values():
        entry.modules.sort(key=str.lower)
    return summary


def selection(regions, categories):
    """Return ``(ranges to read, modules chains may start in)`` for ``categories``.

    The ranges are sorted ``[(start, end)]``, neighbours merged; the heap is
    always among them (``REQUIRED_CATEGORIES``).
    """
    categories = set(categories) | REQUIRED_CATEGORIES
    chosen = [region for region in regions if region.category in categories]
    modules = {region.module for region in chosen if region.module is not None}
    return _merged((region.start, region.end) for region in chosen), modules
