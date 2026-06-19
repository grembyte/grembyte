"""Example Address Model"""

from sqlalchemy import Column, Integer, String, Float, ForeignKey
from hermes.db.engine import Base

class Address(Base):
    """Address table"""
    __tablename__ = "addresses"

    id         = Column(Integer, primary_key=True)
    game_id    = Column(Integer, ForeignKey("games.id"))
    label      = Column(String, nullable=False)   # z.B. "Gold"
    address    = Column(String, nullable=False)   # Hex-Adresse
    value_type = Column(String, default="int32")  # int32, float, ...
    frozen     = Column(Integer, default=0)        # Wert einfrieren?
    last_value = Column(Float, nullable=True)
