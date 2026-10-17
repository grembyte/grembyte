"""Create database"""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from hermes.db.base import Base

DB_PATH = Path(__file__).parent.parent.parent / "data" / "hermes.db"

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SESSION_LOCAL = sessionmaker(bind=engine)

def init_db(db_engine=engine):
    """Create all tables defined in the ORM models (idempotent)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    from hermes.db import models  # pylint: disable=C0415, W0611
    Base.metadata.create_all(db_engine)
    add_missing_columns(db_engine)


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
