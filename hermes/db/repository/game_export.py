"""Export of the entries saved for a game as JSON, and their import.

A game is all processes of one name: the same game is saved once per path,
e.g. under two Proton versions. The export holds per process its address
list, hooks, method hooks and favorites, and the templates its hooks were
created from. The candidate chains of pointer scans are only exported when
asked for: they can be large.

An import puts all entries of a file into one saved process, whatever path
they were exported from. It either replaces what the process holds or mixes
the file's entries in, leaving out those it holds already (``merge``). A
template of the file is linked to a local one of the same name and code,
else added (under a new name if the name is taken).

``delete_entries`` removes chosen kinds of entries of a saved process, or
the process itself with everything saved for it.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import PurePath

from sqlalchemy import DateTime, delete, func, select

from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import (
    Address,
    AssemblySelectionRecord,
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
# Kinds of entries ``delete_entries`` can delete
DELETABLE = ("addresses", "pointer_scans", "hooks", "method_hooks", "favorites")


@dataclass
class SavedProcess:
    """A saved process (name and path) and the number of its entries."""

    process_id: int
    name: str
    path: str
    counts: dict = field(default_factory=dict)

    @property
    def platform(self):
        return game_platform([self.path])


@dataclass
class ImportResult:
    """What an import added and left out, by key of the export."""

    added: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    templates: int = 0


class ImportFormatError(ValueError):
    """The file is no export of hermes-hook, or of a newer version."""


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

    def list_processes(self):
        """All saved processes with the number of their entries, by name, oldest first."""
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
        saved = [
            SavedProcess(process.id, process.name, process.path,
                         {key: by_process.get(process.id, 0)
                          for key, by_process in counts.items()})
            for process in processes
        ]
        return sorted(saved, key=lambda process: (process.name.lower(), process.process_id))

    def list_games(self):
        """Games with at least one exported entry, by name (case-insensitive)."""
        games = {}
        for process in self.list_processes():
            game = games.setdefault(process.name, ExportableGame(process.name, []))
            game.paths.append(process.path)
            for key, count in process.counts.items():
                game.counts[key] = game.counts.get(key, 0) + count
        return [game for game in games.values() if game.entries > 0]

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

    def delete_entries(self, process_id, kinds, remove_process=False):
        """Delete the entries of the ``kinds`` (keys of the export) of a saved process.

        Deleting addresses deletes their pointer scans as well.
        ``remove_process`` deletes the process with all its entries and its
        assembly selection. Returns {kind: number deleted}.
        """
        kinds = set(DELETABLE) if remove_process else set(kinds)
        if "addresses" in kinds:
            kinds.add("pointer_scans")
        deleted = {}
        with self._sessions() as session:
            addresses = select(Address.id).where(Address.process_id == process_id)
            if "pointer_scans" in kinds:
                deleted["pointer_scans"] = session.execute(
                    delete(PointerScan).where(PointerScan.address_id.in_(addresses))).rowcount
            for key, model in _COUNTED:
                if key in kinds:
                    deleted[key] = session.execute(
                        delete(model).where(model.process_id == process_id)).rowcount
            if remove_process:
                session.execute(delete(AssemblySelectionRecord).where(
                    AssemblySelectionRecord.process_id == process_id))
                deleted["process"] = session.execute(
                    delete(Process).where(Process.id == process_id)).rowcount
            session.commit()
        return deleted

    def import_export(self, export, process_id, replace=False):
        """Put the entries of ``export`` into the saved process ``process_id``."""
        return import_into(self._sessions, export, process_id, replace)

    def write_export(self, name, path, pointer_scans=False):
        """Write the export of the game ``name`` to ``path``; returns the export."""
        export = self.export_game(name, pointer_scans)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(export, file, ensure_ascii=False, indent=2)
            file.write("\n")
        return export


def read_export(path):
    """The export in the file ``path``; ImportFormatError if it is none."""
    try:
        with open(path, encoding="utf-8") as file:
            export = json.load(file)
    except json.JSONDecodeError as error:
        raise ImportFormatError(str(error)) from error
    if not isinstance(export, dict) or export.get("format") != EXPORT_FORMAT:
        raise ImportFormatError("not an export of hermes-hook")
    if not isinstance(export.get("version"), int) or export["version"] > EXPORT_VERSION:
        raise ImportFormatError(f"export version {export.get('version')} is not supported")
    if not isinstance(export.get("processes"), list):
        raise ImportFormatError("the export holds no processes")
    return export


def export_counts(export):
    """The number of entries of an export, by key (pointer scans included)."""
    counts = {key: 0 for key, _model in _COUNTED}
    counts["pointer_scans"] = 0
    for process in export.get("processes", []):
        for key, _model in _COUNTED:
            counts[key] += len(process.get(key) or [])
        counts["pointer_scans"] += sum(
            1 for address in process.get("addresses") or [] if address.get("pointer_scan"))
    return counts


def _stable(value):
    return json.dumps(value, sort_keys=True)


# What makes two entries the same when an import mixes them in. The address
# of an entry with a pointer is only where the pointer led last time.
def _address_key(row):
    where = _stable(row.get("pointer")) if row.get("pointer") else row.get("address")
    return (row.get("label"), row.get("value_type"), where)


def _hook_key(row):
    return (row.get("label"), row.get("module"), row.get("module_offset"),
            row.get("instruction_bytes"), _stable(row.get("managed")),
            _stable(row.get("signature")))


def _method_hook_key(row):
    return (row.get("label"), _stable(row.get("managed")), row.get("phase"))


def _favorite_key(row):
    return row.get("key")


_IMPORTED = (
    ("addresses", Address, _address_key),
    ("hooks", Hook, _hook_key),
    ("method_hooks", MethodHook, _method_hook_key),
    ("favorites", FavoriteRecord, _favorite_key),
)


def _record(model, row, **values):
    """A new ``model`` from the exported ``row``: its known columns, no id."""
    for column in model.__table__.columns:
        key = column.key
        if key in ("id", "process_id", "address_id") or key in values or key not in row:
            continue
        value = row[key]
        if value is not None and isinstance(column.type, DateTime):
            try:
                value = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
        values[key] = value
    return model(**values)


class _Templates:
    """Templates of the file, linked to local ones or added when a hook uses them."""

    def __init__(self, session, rows):
        self.session = session
        self.rows = {row.get("id"): row for row in rows if isinstance(row, dict)}
        self.ids = {}
        self.added = 0

    def local_id(self, exported_id):
        """The local id of the exported template ``exported_id`` (None if unknown)."""
        if exported_id in self.ids:
            return self.ids[exported_id]
        row = self.rows.get(exported_id)
        local = None if row is None else self._find_or_add(row)
        self.ids[exported_id] = local
        return local

    def _find_or_add(self, row):
        name = row.get("name") or "Import"
        existing = self.session.scalars(select(HookTemplate).where(HookTemplate.name == name)).first()
        if existing is None:
            return self._add(row, name)
        if (existing.source == row.get("source") and existing.parameters == row.get("parameters")
                and existing.kind == row.get("kind")):
            return existing.id
        number = 1
        while True:
            candidate = f"{name} (Import)" if number == 1 else f"{name} (Import {number})"
            taken = self.session.scalars(
                select(HookTemplate).where(HookTemplate.name == candidate)).first()
            if taken is None:
                return self._add(row, candidate)
            if (taken.source == row.get("source") and taken.parameters == row.get("parameters")
                    and taken.kind == row.get("kind")):
                return taken.id
            number += 1

    def _add(self, row, name):
        template = _record(HookTemplate, row, name=name, seed_key=None)
        self.session.add(template)
        self.session.flush()
        self.added += 1
        return template.id


def import_into(session_factory, export, process_id, replace=False):
    """Put the entries of ``export`` into the saved process ``process_id``.

    ``replace`` deletes its address list (with pointer scans), hooks, method
    hooks and favorites first; else entries it holds already are left out.
    Returns an ImportResult.
    """
    result = ImportResult({key: 0 for key, *_rest in _IMPORTED},
                          {key: 0 for key, *_rest in _IMPORTED})
    result.added["pointer_scans"] = 0
    with session_factory() as session:
        if session.get(Process, process_id) is None:
            raise ValueError(f"no saved process with id {process_id}")
        if replace:
            addresses = select(Address.id).where(Address.process_id == process_id)
            session.execute(delete(PointerScan).where(PointerScan.address_id.in_(addresses)))
            for _key, model, _identify in _IMPORTED:
                session.execute(delete(model).where(model.process_id == process_id))
        known = {}
        for key, model, identify in _IMPORTED:
            known[key] = {identify(_row(record)) for record in session.scalars(
                select(model).where(model.process_id == process_id))}
        templates = _Templates(session, export.get("templates") or [])

        for process in export["processes"]:
            for key, model, identify in _IMPORTED:
                for row in process.get(key) or []:
                    identity = identify(row)
                    if identity in known[key]:
                        result.skipped[key] += 1
                        continue
                    known[key].add(identity)
                    values = {"process_id": process_id}
                    if "template_id" in row:
                        values["template_id"] = templates.local_id(row["template_id"])
                    record = _record(model, row, **values)
                    session.add(record)
                    result.added[key] += 1
                    scan = row.get("pointer_scan") if model is Address else None
                    if scan:
                        session.flush()
                        session.add(_record(PointerScan, scan, address_id=record.id))
                        result.added["pointer_scans"] += 1
        result.templates = templates.added
        session.commit()
    return result
