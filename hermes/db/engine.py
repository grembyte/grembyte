"""Create database"""

import os
import shutil
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base

# Where the database lived before it moved to the user's data directory.
LEGACY_DB_PATH = Path(__file__).parent.parent.parent / "data" / "hermes.db"


def default_db_path():
    """Return ``$HERMES_DB`` or ``$XDG_DATA_HOME/hermes-hook/hermes.db``.

    ``XDG_DATA_HOME`` defaults to ``~/.local/share``.
    """
    override = os.environ.get("HERMES_DB")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "hermes-hook" / "hermes.db"


DB_PATH = default_db_path()

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SESSION_LOCAL = sessionmaker(bind=engine)

def init_db(db_engine=engine):
    """Create all tables defined in the ORM models (idempotent).

    For the default database this also creates its directory and copies a
    database from the old location in the repository on first use.
    """
    if db_engine is engine:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        adopt_legacy_db(DB_PATH)
    from hermes.db import models  # pylint: disable=C0415, W0611
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


def add_missing_columns(db_engine=engine):
    """Add nullable columns that models gained to existing tables.

    create_all() only creates missing tables. Nullable columns can be added
    without touching existing rows; anything else needs a real migration.
    """
    inspector = inspect(db_engine)
    with db_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not column.nullable:
                    continue
                column_type = column.type.compile(dialect=db_engine.dialect)
                connection.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'
                ))
