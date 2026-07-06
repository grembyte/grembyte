# Import all ORM models so SQLAlchemy's Base.metadata is populated
# before create_all() is called in init_db().
from hermes.db.models.address import Address  # noqa: F401
