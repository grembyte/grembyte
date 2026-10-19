"""The Mono engine adapter (Unity games that use Mono, and other Mono games)."""

import re

from hermes.engines import base
from hermes.engines.mono.agent import MONO_AGENT_SOURCE

# Mono runtime libraries; libmono-native.so (System.Native) is none. Under
# Wine/Proton the runtime is a DLL (MonoPosixHelper.dll is none either).
_RUNTIME_NAMES = re.compile(
    r"^libmono(bdwgc|sgen)?(-2\.0)?\.so|^mono(-2\.0-(bdwgc|sgen))?\.dll$",
    re.IGNORECASE)


class MonoAdapter(base.EngineAdapter):
    """Mono's embedding API: JIT code, assemblies loaded at run time."""

    id = "mono"
    display_name = "Mono"
    capabilities = frozenset({base.NAMING, base.CATALOG, base.TRACE, base.MONITOR,
                              base.METHOD_HOOKS})
    agent_source = MONO_AGENT_SOURCE
    framework_prefixes = (
        "mscorlib", "netstandard", "System", "Microsoft.", "Mono.", "UnityEngine", "Unity.",
        "UnityScript", "Boo.", "I18N", "Accessibility", "Novell.", "SMDiagnostics",
    ) + base.MIDDLEWARE_PREFIXES

    def is_runtime_file(self, name):
        return _RUNTIME_NAMES.match(name) is not None
