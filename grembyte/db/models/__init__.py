"""Database model package; importing registers the models with Base.metadata."""

# Import all ORM models so SQLAlchemy's Base.metadata is populated
# before create_all() is called in init_db().
from grembyte.db.models.address import Address  # noqa: F401
from grembyte.db.models.assembly_selection import AssemblySelectionRecord  # noqa: F401
from grembyte.db.models.favorite import FavoriteRecord  # noqa: F401
from grembyte.db.models.game_script import GameScript  # noqa: F401
from grembyte.db.models.hook import Hook  # noqa: F401
from grembyte.db.models.method_hook import MethodHook  # noqa: F401
from grembyte.db.models.pointer_scan import PointerScan  # noqa: F401
from grembyte.db.models.process import Process  # noqa: F401
from grembyte.db.models.setting import SettingRecord  # noqa: F401
from grembyte.db.models.template import HookTemplate  # noqa: F401
from grembyte.db.models.trace_filter import TraceFilterRecord  # noqa: F401
