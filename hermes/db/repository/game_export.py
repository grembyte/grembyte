"""Export of the entries saved for a game as JSON.

A game is all processes of one name: the same game is saved once per path,
e.g. under two Proton versions. The export holds per process its address
list, hooks, method hooks and favorites, and the templates its hooks were
created from. The candidate chains of pointer scans are only exported when
asked for: they can be large.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import PurePath

from sqlalchemy import func, select

from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import (
    Address,
    FavoriteRecord,
    Hook,
    HookTemplate,
    MethodHook,
    PointerScan,
    Process,
)

EXPORT_FORMAT = "hermes-hook-export"
EXPORT_VERSION = 1
LINUX, WINDOWS = "Linux", "Windows"
# Characters a file name must not hold on Windows or Linux
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# Entries counted for the selection, by key of the export
_COUNTED = (
    ("addresses", Address),
    ("hooks", Hook),
    ("method_hooks", MethodHook),
    ("favorites", FavoriteRecord),
)


@dataclass
class ExportableGame:
    """A game name with the processes saved under it and what they hold."""

    name: str
    paths: list
    counts: dict = field(default_factory=dict)

    @property
    def platform(self):
        return game_platform(self.paths)

    @property
    def entries(self):
        """Entries exported always (pointer scans not counted)."""
        return sum(count for key, count in self.counts.items() if key != "pointer_scans")


def game_platform(paths):
    """``Windows`` if a process runs under Wine/Proton or on Windows, else ``Linux``.

    The path of a Wine process is Wine's loader (``wine64-preloader``),
    not the game's .exe; a path with a drive or backslashes was saved by
    Hermes on Windows. The name alone tells nothing: a Linux program may be
    called "game.exe".
    """
    for path in paths:
        if _WINDOWS_PATH.match(path) or "\\" in path:
            return WINDOWS
        name = PurePath(path).name
        if name in {"wine", "wine64"} or (name.startswith("wine") and name.endswith("-preloader")):
            return WINDOWS
    return LINUX


def export_file_name(platform, name, day=None):
    """``hermes-hook_<platform>_<process name>_<yyyy-mm-dd>.json``, safe as a file name."""
    day = day or date.today()
    safe = _UNSAFE.sub("_", name).strip(" .") or "game"
    return f"hermes-hook_{platform}_{safe}_{day.isoformat()}.json"


def _row(record, skip=("process_id",)):
    """The columns of ``record`` as JSON values."""
    values = {}
    for column in record.__table__.columns:
        if column.key in skip:
            continue
        value = getattr(record, column.key)
        if isinstance(value, datetime):
            value = value.isoformat()
        values[column.key] = value
    return values


class GameExportRepository:
    """Read what is saved per game and build its export."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_games(self):
        """Games with at least one exported entry, by name (case-insensitive)."""
        with self._sessions() as session:
            processes = list(session.scalars(select(Process).order_by(Process.id)))
            counts = {}
            for key, model in _COUNTED:
                counts[key] = dict(session.execute(
                    select(model.process_id, func.count()).group_by(model.process_id)).all())
            counts["pointer_scans"] = dict(session.execute(
                select(Address.process_id, func.count())
                .join(PointerScan, PointerScan.address_id == Address.id)
                .group_by(Address.process_id)).all())
        games = {}
        for process in processes:
            game = games.setdefault(process.name, ExportableGame(process.name, []))
            game.paths.append(process.path)
            for key, by_process in counts.items():
                game.counts[key] = game.counts.get(key, 0) + by_process.get(process.id, 0)
        return sorted((game for game in games.values() if game.entries > 0),
                      key=lambda game: game.name.lower())

    def export_game(self, name, pointer_scans=False):
        """The export of the game ``name`` as a JSON-ready dict."""
        with self._sessions() as session:
            processes = list(session.scalars(
                select(Process).where(Process.name == name).order_by(Process.id)))
            exported = []
            template_ids = set()
            for process in processes:
                entry = self._export_process(session, process, pointer_scans)
                if not any(entry[key] for key, _model in _COUNTED):
                    continue
                exported.append(entry)
                template_ids.update(
                    hook["template_id"] for hook in entry["hooks"] + entry["method_hooks"]
                    if hook["template_id"] is not None)
            templates = [
                _row(template, skip=("seed_key",)) for template in session.scalars(
                    select(HookTemplate).where(HookTemplate.id.in_(template_ids))
                    .order_by(HookTemplate.id))
            ] if template_ids else []
        return {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "game": name,
            "platform": game_platform([process["path"] for process in exported]),
            "pointer_scans": pointer_scans,
            "processes": exported,
            "templates": templates,
        }

    @staticmethod
    def _export_process(session, process, pointer_scans):
        def rows(model):
            return [_row(record) for record in session.scalars(
                select(model).where(model.process_id == process.id).order_by(model.id))]

        addresses = rows(Address)
        if pointer_scans and addresses:
            scans = {scan.address_id: scan for scan in session.scalars(
                select(PointerScan).where(
                    PointerScan.address_id.in_([address["id"] for address in addresses])))}
            for address in addresses:
                scan = scans.get(address["id"])
                address["pointer_scan"] = None if scan is None else _row(
                    scan, skip=("id", "address_id"))
        return {
            "name": process.name,
            "path": process.path,
            "addresses": addresses,
            "hooks": rows(Hook),
            "method_hooks": rows(MethodHook),
            "favorites": rows(FavoriteRecord),
        }

    def write_export(self, name, path, pointer_scans=False):
        """Write the export of the game ``name`` to ``path``; returns the export."""
        export = self.export_game(name, pointer_scans)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(export, file, ensure_ascii=False, indent=2)
            file.write("\n")
        return export
