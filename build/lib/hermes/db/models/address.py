"""Address ORM model."""

from sqlalchemy import Column, Integer, String, Float

from hermes.db.base import Base


class Address(Base):  # pylint: disable=too-few-public-methods
    """A memory address entry tracked by the scanner."""

    __tablename__ = "addresses"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, nullable=True)       # optional grouping by game/process
    label = Column(String, nullable=False)         # e.g. "Gold"
    address = Column(String, nullable=False)       # hex address string
    value_type = Column(String, default="int32")   # int32, int64, float, double
    frozen = Column(Integer, default=0)            # freeze value (1 = yes, 0 = no)
    last_value = Column(Float, nullable=True)
