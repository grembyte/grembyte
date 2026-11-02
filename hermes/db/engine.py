"""Create database"""

import logging
import os
import shutil
import sqlite3
from pathlib import Path

from PyQt6.QtCore import QStandardPaths  # pylint: disable=import-error,no-name-in-module
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from hermes.db.base import Base

log = logging.getLogger("hermes.db")

# Where the database lived before it moved to the user's data directory.
LEGACY_DB_PATH = Path(__file__).parent.parent.parent / "data" / "hermes.db"


def default_db_path():
    """Return ``$HERMES_DB`` or ``hermes-hook/hermes.db`` in the user's data directory.

    That is ``$XDG_DATA_HOME`` or ``~/.local/share`` on Linux,
    ``%LOCALAPPDATA%`` on Windows.
    """
    override = os.environ.get("HERMES_DB")
    if override:
        return Path(override).expanduser()
    data_home = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.GenericDataLocation)
    return Path(data_home) / "hermes-hook" / "hermes.db"


DB_PATH = default_db_path()

@event.listens_for(Engine, "connect")
def _enable_foreign_keys(dbapi_connection, _connection_record):
    """SQLite ignores foreign keys, and with them ``ondelete``, unless a connection asks."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SESSION_LOCAL = sessionmaker(bind=engine)

_PENDING_CHANGES = "_hermes_db_pending_changes"


@event.listens_for(Session, "before_flush")
def _collect_database_changes(session, _flush_context, _instances):
    """Remember inserted, updated and deleted rows until the transaction commits."""
    changes = session.info.setdefault(_PENDING_CHANGES, {})
    new_objects = {id(instance) for instance in session.new}
    for instance in session.new:
        changes[id(instance)] = {
            "table": inspect(instance).mapper.local_table.name,
            "operation": "created",
            "fields": set(),
        }
    for instance in session.dirty:
        if id(instance) in new_objects or instance in session.deleted:
            continue
        state = inspect(instance)
        fields = {
            attribute.key for attribute in state.mapper.column_attrs
            if state.attrs[attribute.key].history.has_changes()
        }
        if not fields:
            continue
        change = changes.get(id(instance))
        if change is not None and change["operation"] == "created":
            continue
        if change is None:
            change = {
                "table": state.mapper.local_table.name,
                "operation": "updated",
                "fields": set(),
            }
            changes[id(instance)] = change
        change["fields"].update(fields)
    for instance in session.deleted:
        changes[id(instance)] = {
            "table": inspect(instance).mapper.local_table.name,
            "operation": "deleted",
            "fields": set(),
        }


@event.listens_for(Session, "do_orm_execute")
def _collect_bulk_deletes(orm_execute_state):
    """Remember rows removed by ORM-enabled bulk DELETE statements."""
    if not orm_execute_state.is_delete:
        return None
    result = orm_execute_state.invoke_statement()
    count = getattr(result, "rowcount", 0)
    if count > 0:
        table = orm_execute_state.statement.table.name
        changes = orm_execute_state.session.info.setdefault(_PENDING_CHANGES, {})
        key = ("bulk-delete", table)
        change = changes.get(key)
        if change is None:
            changes[key] = {
                "table": table,
                "operation": "bulk-deleted",
                "count": count,
            }
        else:
            change["count"] += count
    return result


@event.listens_for(Session, "after_commit")
def _log_database_changes(session):
    """Log database changes only after their transaction succeeds."""
    changes = session.info.pop(_PENDING_CHANGES, {})
    for change in changes.values():
        table = change["table"]
        if change["operation"] == "created":
            log.info("database record created: %s", table)
        elif change["operation"] == "updated":
            fields = ", ".join(sorted(change["fields"]))
            log.info("database record updated: %s (fields: %s)", table, fields)
        elif change["operation"] == "deleted":
            log.info("database record deleted: %s", table)
        else:
            log.info("database records deleted: %s (count: %d)", table, change["count"])


@event.listens_for(Session, "after_rollback")
def _discard_database_changes(session):
    """Do not report flushed rows from a transaction that was rolled back."""
    session.info.pop(_PENDING_CHANGES, None)


def init_db(db_engine=engine):
    """Create all tables defined in the ORM models (idempotent).

    For the default database this also creates its directory and copies a
    database from the old location in the repository on first use.
    """
    if db_engine is engine:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        adopt_legacy_db(DB_PATH)
    from hermes.db import models  # pylint: disable=C0415, W0611
    set_aside_incompatible_tables(db_engine)
    Base.metadata.create_all(db_engine)
    add_missing_columns(db_engine)


def adopt_legacy_db(path, legacy_path=LEGACY_DB_PATH):
    """Copy ``legacy_path`` to ``path`` if only the old database exists.

    The old file is kept, so an older version of Hermes still finds it.
    Returns whether a database was copied.
    """
    if path.exists() or not legacy_path.is_file() or path.resolve() == legacy_path.resolve():
        return False
    shutil.copy2(legacy_path, path)
    return True


def _affinity(declared):
    """SQLite's type affinity of a declared column type (rules of section 3.1)."""
    declared = declared.upper()
    if "INT" in declared:
        return "INTEGER"
    if any(word in declared for word in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(word in declared for word in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _placeholder(column):
    """SQL default for a required column added to a table with rows, or None.

    The model's own default, else an empty text for a name or path. Without
    either a placeholder would make the old rows look valid but unusable.
    """
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return None
    return "''" if _affinity(str(column.type)) == "TEXT" else None


def _incompatibility(table, existing):
    """Why the existing columns of ``table`` cannot serve its model, or None."""
    for column in table.columns:
        if column.name not in existing:
            if not column.nullable and _placeholder(column) is None:
                return f"required column {column.name} is missing"
            continue
        # A number stored in a text column comes back as text, and vice versa.
        kinds = {_affinity(str(column.type)), _affinity(str(existing[column.name]["type"]))}
        if "TEXT" in kinds and kinds & {"INTEGER", "REAL"}:
            return f"column {column.name} has another type"
    for name, column in existing.items():
        if name not in table.columns and not column["nullable"] and column.get("default") is None:
            return f"column {name} is required but no longer used"
    return None


def set_aside_incompatible_tables(db_engine=engine):
    """Rename tables of an older schema the models cannot use; returns {old: new name}.

    add_missing_columns() adds what a table lacks where that keeps its rows
    usable. A table with a column it cannot fill, a required column the
    models no longer know or a column of another type (all of which happened
    with databases of the first versions) would fail or garble every insert.
    It is kept as ``<name>_old`` and create_all() makes the table anew.
    """
    inspector = inspect(db_engine)
    names = set(inspector.get_table_names())
    renamed = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in names:
            continue
        existing = {column["name"]: column for column in inspector.get_columns(table.name)}
        reason = _incompatibility(table, existing)
        if reason is None:
            continue
        new_name, number = f"{table.name}_old", 2
        while new_name in names:
            new_name, number = f"{table.name}_old{number}", number + 1
        names.add(new_name)
        renamed[table.name] = new_name
        log.warning("Table %s has an older schema (%s); kept as %s", table.name, reason, new_name)
    if renamed:
        # References from other tables keep naming the table, not its old copy:
        # SQLite rewrites them unless both pragmas say otherwise. foreign_keys
        # cannot change inside a transaction.
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("PRAGMA foreign_keys=OFF"))
            connection.execute(text("PRAGMA legacy_alter_table=ON"))
            try:
                for old, new in renamed.items():
                    connection.execute(text(f'ALTER TABLE "{old}" RENAME TO "{new}"'))
            finally:
                connection.execute(text("PRAGMA legacy_alter_table=OFF"))
                connection.execute(text("PRAGMA foreign_keys=ON"))
    return renamed


def add_missing_columns(db_engine=engine):
    """Add columns that models gained to existing tables.

    create_all() only creates missing tables. Nullable columns are added
    empty, required ones with a placeholder for the rows there are (see
    _placeholder); set_aside_incompatible_tables() has dealt with the rest.
    """
    inspector = inspect(db_engine)
    with db_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                column_type = column.type.compile(dialect=db_engine.dialect)
                required = ""
                if not column.nullable:
                    placeholder = _placeholder(column)
                    if placeholder is None:
                        continue
                    required = f" NOT NULL DEFAULT {placeholder}"
                connection.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{required}'
                ))


def database_size(db_engine=engine):
    """Bytes of the database file and how many of them are free pages."""
    with db_engine.connect() as connection:
        page_size = connection.execute(text("PRAGMA page_size")).scalar()
        pages = connection.execute(text("PRAGMA page_count")).scalar()
        free = connection.execute(text("PRAGMA freelist_count")).scalar()
    return page_size * pages, page_size * free


def vacuum_db(db_engine=engine):
    """Rebuild the database without its free pages; returns the sizes before and after.

    SQLite keeps the space of deleted rows (e.g. large pointer scans) in the
    file. VACUUM cannot run inside a transaction, hence the autocommit
    connection; it fails while another connection writes.
    """
    before, _free = database_size(db_engine)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM"))
    after, _free = database_size(db_engine)
    return before, after
