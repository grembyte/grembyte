"""Create database"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = Path(__file__).parent.parent.parent / "data" / "hermes.db"

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SESSION_LOCAL = sessionmaker(bind=engine)

class Base(DeclarativeBase): # pylint: disable=R0903
    """Base class"""

def init_db():
    """Create all tables defined in the ORM models (idempotent)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    from hermes.db import models  # pylint: disable=C0415, W0611
    Base.metadata.create_all(engine)
