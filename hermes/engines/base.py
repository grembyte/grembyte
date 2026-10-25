"""What an engine adapter provides: the seam between hermes' features and a
game engine's runtime.

hermes' managed-code features (catalog, trace, method monitor, method hooks,
and naming of code addresses in write watches and hooks) do not talk to Mono
directly. They use the ``engine`` object of the Frida agent, which forwards to
the adapter whose runtime the game has loaded. Mono is the first adapter
(hermes/engines/mono); IL2CPP or Unreal would be further ones.

Python side: a subclass of :class:`EngineAdapter` says how to recognize the
engine from outside (``detect``), what it can do (``capabilities``) and which
of its code units are framework rather than game (``is_framework_assembly``).
It is listed in ``registry.ENGINES``.

Agent side: ``agent_source`` defines the adapter object and passes it to
``registerEngine``. The features call ``engine.<name>``; the contract below is
what they use. Its vocabulary follows the ECMA-335 model of .NET (assemblies,
classes, methods with metadata tokens and flags, element types), which Mono
and IL2CPP share; another kind of engine maps its own units onto it.

Every adapter object has:

``id``
    Its registry id (``'mono'``).
``refresh()``
    Look for the runtime (at most every few seconds) and return whether it
    is loaded. Call it from the agent's own thread.

Naming (``NAMING``), safe in exception handlers on game threads:

``locate(address)`` / ``name(address)`` / ``describe(address)``
    Find the method containing JIT code and name it; ``locate`` returns
    ``undefined`` while that cannot be answered yet, ``null`` for no method.
``identify(address)`` / ``compile(identity)``
    What finds the method again after a restart (``{engine, image, file,
    token, method, offset}``), and (a promise of) ``{start, size}`` of its
    code for such an identity.

Catalog (``CATALOG``): ``assemblies()`` (with the MVID the catalog cache
keys them by), ``classes(assembly)`` (with the methods' ECMA-335 flags).

Trace (``TRACE``): ``methodsByToken(assembly, tokens)`` with ECMA-335 flags,
``checkAll(methods, progress)`` (which cannot be prepared safely),
``compileAll(methods, progress)``, ``describeAll(addresses)``,
``cancelBatches()``, ``batchesRunning()``.

Method monitor and method hooks (``MONITOR``): ``prepare(identity)`` (code
and signature), the object model (``TYPE`` element types, ``classOf``,
``objectClassName``, ``layout``, ``classChain``, ``newString``,
``writeReference``), running code (``onRuntimeThread``, ``onMainThread``,
``drainMain``, ``findClass``, ``findMethod``, ``signatureOf``, ``invoke``,
``virtualMethod``, ``properties``, ``getProperty``) and ``unityName`` for
Unity games.

The platform helpers an adapter may use (``spawnThread``, ``watchUnmaps``,
``pauseThreads``) are in platform.js.
"""

import ntpath
import os
import sys

from hermes.core.agent_source import load_agent_source
from hermes.platform import read_mappings

# Libraries many games ship that are not the game itself (input, tweening,
# store and platform SDKs, consoles, serializers). Adapters add them to their
# framework assemblies, so the catalog and the trace leave them out by default.
MIDDLEWARE_PREFIXES = (
    "Rewired", "Steamworks", "com.rlabrecque.steamworks", "Facepunch.Steamworks",
    "GalaxyCSharp", "DOTween", "DG.Tweening", "LeanTween", "HeathenEngineering",
    "IngameDebugConsole", "GooglePlayGames", "Newtonsoft.", "Sirenix.", "Photon",
    "PlayFab", "Firebase.", "Facebook.", "InControl", "Cinemachine", "FMOD", "AK.Wwise",
    "Zenject", "UniRx", "UniTask", "Mirror",
)

# Capabilities (what the UI offers for a game with that engine)
NAMING = "naming"
CATALOG = "catalog"
TRACE = "trace"
MONITOR = "monitor"
METHOD_HOOKS = "method_hooks"


class EngineAdapter:
    """One game engine runtime hermes knows. Subclasses fill the attributes."""

    id = ""
    display_name = ""
    capabilities = frozenset()
    # The adapter object of the agent; it calls registerEngine(...).
    agent_source = ""
    # Code units of the runtime and its frameworks rather than of the game
    framework_prefixes = ()
    # Whether the native trace and the native monitor are safe in its games:
    # they rewrite machine code the runtime may have to recognise.
    native_tools = True

    def detect(self, pid, proc_root="/proc"):
        """Return whether the process has loaded this engine's runtime."""
        if sys.platform == "win32":
            try:
                return any(self.is_runtime_file(ntpath.basename(mapping.path))
                           for mapping in read_mappings(pid) if mapping.module is not None)
            except OSError:
                return False
        try:
            with open(f"{proc_root}/{pid}/maps", encoding="utf-8", errors="replace") as maps:
                return any(
                    self.is_runtime_file(os.path.basename(line.split(maxsplit=5)[-1].strip()))
                    for line in maps
                    if "/" in line
                )
        except OSError:
            return False

    def is_runtime_file(self, name):
        """Whether a mapped file of that name is the engine's runtime."""
        raise NotImplementedError

    def is_framework_assembly(self, name):
        """Whether a code unit belongs to the runtime or framework, not the game."""
        return name.startswith(self.framework_prefixes)

    def supports(self, capability):
        """Whether the adapter offers ``capability``."""
        return capability in self.capabilities


# Before the adapters' sources: where they register.
ENGINE_PRELUDE_SOURCE = load_agent_source(__file__, "engine_prelude.js")

# After the adapters' sources: the object the features use.
ENGINE_FACADE_SOURCE = load_agent_source(__file__, "engine_facade.js")
