"""Create database"""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = Path(__file__).parent.parent.parent / "data" / "hermes.db"

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SESSION_LOCAL = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    """Base class"""
    pass

def init_db():
    """Initialize database"""
    from hermes.db import models  # alle Models importieren
    Base.metadata.create_all(engine)
