"""The engine adapters hermes knows, and the agent source built from them.

To add an engine, write an adapter (see base.py) and list it in ``ENGINES``;
the order is the order in which the agent looks for their runtimes.
"""

from hermes.engines.base import ENGINE_FACADE_SOURCE, ENGINE_PRELUDE_SOURCE
from hermes.engines.mono.adapter import MonoAdapter
from hermes.engines.platform import PLATFORM_AGENT_SOURCE

ENGINES = (MonoAdapter(),)

# Agents that work with managed code start with this: the platform helpers,
# every adapter, and ``engine``.
ENGINE_AGENT_SOURCE = (PLATFORM_AGENT_SOURCE + ENGINE_PRELUDE_SOURCE
                       + "".join(adapter.agent_source for adapter in ENGINES)
                       + ENGINE_FACADE_SOURCE)


def detect_engine(pid, proc_root="/proc"):
    """Return the adapter of the engine runtime ``pid`` has loaded, or None."""
    return next((adapter for adapter in ENGINES if adapter.detect(pid, proc_root)), None)


def engine_by_id(engine_id):
    """Return the adapter with ``engine_id`` (identities without one are Mono's)."""
    engine_id = engine_id or "mono"
    return next((adapter for adapter in ENGINES if adapter.id == engine_id), None)


def resolve_engine(pid, engine=None):
    """Return ``engine``, else the adapter detected in ``pid``, else the first one.

    The runtime may load only after hermes attached; the agent finds it then
    by itself, so a feature can start before detection succeeds.
    """
    return engine or detect_engine(pid) or ENGINES[0]
