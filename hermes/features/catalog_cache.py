"""What Hermes read of an assembly, kept on disk per assembly build.

Reading an assembly's classes takes a while and may make the runtime load
them (see hermes.features.catalog); so does reading its code for the call
graph (see hermes.features.call_graph). What they find only changes with the
assembly, so it is kept under the assembly's module version id (MVID): the
compiler gives each build a new one, so an updated or modded assembly is
read again. Assemblies the runtime names no MVID for are not cached.

The files lie in ``cache/<engine>/<assembly>/<mvid>.json.gz`` (``CLASSES``)
and ``<mvid>.calls.json.gz`` (``CALLS``) next to the database; deleting them
only costs the next reading.
"""

import gzip
import json
import os
import re
from pathlib import Path

from PyQt6.QtCore import QStandardPaths  # pylint: disable=import-error,no-name-in-module

from hermes.core import logs

CLASSES, CALLS = "classes", "calls"

# Raise when what is stored changes its shape; older files are then ignored.
# classes 2: the methods' flags, for the trace
FORMATS = {CLASSES: 2, CALLS: 1}

# Characters kept in a file or directory name; others become "_".
_UNSAFE = re.compile(r"[^\w.-]")


def cache_dir():
    """``$HERMES_CACHE_DIR`` or ``hermes-hook/cache`` in the user's data directory."""
    if os.environ.get("HERMES_CACHE_DIR"):
        return Path(os.environ["HERMES_CACHE_DIR"])
    data = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return Path(data) / "hermes-hook" / "cache"


def _safe(name):
    """``name`` usable as one file name that stays in its directory.

    Leading and trailing dots go too: ``..`` leaves the directory, and
    Windows drops dots at the end of a name.
    """
    return _UNSAFE.sub("_", name).strip(".") or "_"


def _path(engine, assembly, mvid, directory, kind=CLASSES):
    base = Path(directory) if directory is not None else cache_dir()
    suffix = "" if kind == CLASSES else f".{kind}"
    return base / _safe(engine) / _safe(assembly) / f"{_safe(mvid)}{suffix}.json.gz"


def load(engine, assembly, mvid, directory=None, kind=CLASSES):
    """What is stored of ``kind`` for this build of ``assembly``, else None."""
    if not mvid:
        return None
    path = _path(engine, assembly, mvid, directory, kind)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            stored = json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as error:
        logs.log.warning("catalog cache %s unreadable: %s", path, error)
        return None
    if (not isinstance(stored, dict) or stored.get("format") != FORMATS[kind]
            or stored.get("assembly") != assembly or stored.get("mvid") != mvid):
        return None
    return stored.get(kind)


def store(engine, assembly, mvid, data, directory=None, kind=CLASSES):
    """Keep ``data`` of ``kind`` for this build of ``assembly``; drop other builds'."""
    if not mvid:
        return
    path = _path(engine, assembly, mvid, directory, kind)
    partial = path.with_name(path.name + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(partial, "wt", encoding="utf-8") as file:
            json.dump({"format": FORMATS[kind], "assembly": assembly, "mvid": mvid,
                       kind: data}, file, separators=(",", ":"))
        os.replace(partial, path)
        build = f"{_safe(mvid)}."
        for other in path.parent.glob("*.json.gz"):
            if not other.name.startswith(build):
                other.unlink()
    except OSError as error:
        logs.log.warning("catalog cache %s not written: %s", path, error)


def read_cached(engine, assembly, mvid, read, fresh=False, kind=CLASSES):
    """What is known of ``kind`` for ``assembly`` and whether it came from the cache.

    Not cached (or ``fresh``): ``read()`` finds it out (e.g. asks the agent),
    and the answer is stored for the next time.
    """
    if not fresh:
        data = load(engine, assembly, mvid, kind=kind)
        if data is not None:
            return data, True
    data = read()
    store(engine, assembly, mvid, data, kind=kind)
    return data, False


def read_classes(engine, assembly, mvid, read, fresh=False):
    """The ``classes`` answer for ``assembly`` and whether it came from the cache."""
    return read_cached(engine, assembly, mvid, read, fresh)
