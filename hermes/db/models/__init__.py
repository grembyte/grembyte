"""Database model package; importing registers the models with Base.metadata."""

# Import all ORM models so SQLAlchemy's Base.metadata is populated
# before create_all() is called in init_db().
from hermes.db.models.address import Address  # noqa: F401
from hermes.db.models.hook import Hook  # noqa: F401
from hermes.db.models.pointer_scan import PointerScan  # noqa: F401
from hermes.db.models.process import Process  # noqa: F401
from hermes.db.models.template import HookTemplate  # noqa: F401
