"""The games whose assemblies Grembyte read: which builds each one loads.

The catalog cache (see grembyte.features.catalog_cache) keeps what was read of
an assembly build, but not which game loads which builds: every Unity game
has an ``Assembly-CSharp``. So whenever a feature asks a game for its loaded
assemblies, the answer is noted per game in ``cache/games/<name>-<hash>.json``
(``remember``); the offline catalog shows a game from it without the game
running (``known_games``).
"""

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from grembyte.core import logs
from grembyte.features.catalog_cache import cache_dir

FORMAT = 1

# The program of each process, worked out once (pid -> path)
_programs = {}
_programs_lock = threading.Lock()


@dataclass(frozen=True)
class KnownGame:
    """A game as last read: its program, engine and loaded assemblies.

    ``assemblies``: ``[{name, mvid, file, classes}]`` as the engine adapter's
    ``assemblies()`` answered; ``updated``: when (seconds since the epoch).
    """

    name: str
    path: str
    engine: str
    updated: float
    assemblies: list = field(default_factory=list, compare=False)

    def mvids(self):
        """``{assembly name: mvid}`` of the game's assemblies."""
        return {assembly["name"]: assembly.get("mvid") for assembly in self.assemblies}


def games_dir(directory=None):
    return Path(directory) if directory is not None else cache_dir() / "games"


def program_of(pid):
    """The path of the program ``pid`` runs (under Wine: the Windows program), else None."""
    with _programs_lock:
        if pid in _programs:
            return _programs[pid]
    # pylint: disable=import-outside-toplevel
    from grembyte.core.activity import describe_process

    try:
        details = describe_process(pid)
    except (OSError, RuntimeError) as error:
        logs.log.warning("program of process %s unknown: %s", pid, error)
        details = {}
    path = details.get("program") or details.get("exe")
    with _programs_lock:
        _programs[pid] = path
    return path


def _file_name(path):
    stem = re.sub(r"[^\w.-]", "_", Path(path.replace("\\", "/")).stem).strip(".") or "game"
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}.json"


def remember(pid, engine, assemblies, directory=None):
    """Note that the game ``pid`` runs has loaded ``assemblies`` (see KnownGame)."""
    path = program_of(pid)
    if not path:
        return
    target = games_dir(directory) / _file_name(path)
    partial = target.with_name(target.name + ".part")
    data = {"format": FORMAT, "name": Path(path.replace("\\", "/")).name, "path": path,
            "engine": engine, "updated": time.time(),
            "assemblies": [{key: assembly.get(key) for key in ("name", "mvid", "file", "classes")}
                           for assembly in assemblies]}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text(json.dumps(data), encoding="utf-8")
        os.replace(partial, target)
    except OSError as error:
        logs.log.warning("game list %s not written: %s", target, error)


def known_games(directory=None):
    """The KnownGames, the one read last first."""
    games = []
    for path in games_dir(directory).glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logs.log.warning("game list %s unreadable: %s", path, error)
            continue
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            continue
        games.append(KnownGame(data.get("name", "?"), data.get("path", ""),
                               data.get("engine", ""), data.get("updated", 0),
                               data.get("assemblies", [])))
    return sorted(games, key=lambda game: game.updated, reverse=True)
