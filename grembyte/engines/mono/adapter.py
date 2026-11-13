"""The Mono engine adapter (Unity games that use Mono, and other Mono games)."""

import re

from grembyte.engines import base
from grembyte.engines.mono.agent import MONO_AGENT_SOURCE

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
                              base.METHOD_HOOKS, base.CALL_GRAPH, base.CALL,
                              base.OBJECT_BROWSER, base.OBJECT_REFERENCES})
    agent_source = MONO_AGENT_SOURCE
    # The native trace ended Shadowrun Returns within seconds (Linux). Likely cause:
    # Stalker recompiles the JIT code too (it lies in no module to exclude),
    # and Mono, finding no method for a faulting address there, takes the
    # fault for a crash instead of throwing a NullReferenceException. The
    # native tools ask before they start (see MainWindow._confirm_native_tools).
    native_tools = False
    framework_prefixes = (
        "mscorlib", "netstandard", "System", "Microsoft.", "Mono.", "UnityEngine", "Unity.",
        "UnityScript", "Boo.", "I18N", "Accessibility", "Novell.", "SMDiagnostics",
    ) + base.MIDDLEWARE_PREFIXES

    def is_runtime_file(self, name):
        return _RUNTIME_NAMES.match(name) is not None
