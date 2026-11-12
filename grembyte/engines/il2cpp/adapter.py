"""The IL2CPP engine adapter (Unity games built with IL2CPP)."""

import re

from grembyte.engines import base
from grembyte.engines.il2cpp.agent import IL2CPP_AGENT_SOURCE
from grembyte.engines.mono.adapter import MonoAdapter

_RUNTIME_NAMES = re.compile(r"^GameAssembly\.(dll|so)$", re.IGNORECASE)


class Il2CppAdapter(base.EngineAdapter):
    """IL2CPP's exported API: ahead-of-time code in GameAssembly."""

    id = "il2cpp"
    display_name = "IL2CPP"
    capabilities = frozenset({base.NAMING, base.CATALOG, base.TRACE, base.MONITOR,
                              base.METHOD_HOOKS})
    agent_source = IL2CPP_AGENT_SOURCE
    # All code is machine code in GameAssembly, as with Unreal.
    native_tools = True
    # The same libraries as under Mono, plus what IL2CPP generates itself
    framework_prefixes = MonoAdapter.framework_prefixes + ("__Generated", "Il2Cpp")

    def is_runtime_file(self, name):
        return _RUNTIME_NAMES.match(name) is not None
