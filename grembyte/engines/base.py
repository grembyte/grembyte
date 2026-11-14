"""What an engine adapter provides: the seam between grembyte's features and a
game engine's runtime.

grembyte's managed-code features (catalog, trace, method monitor, method hooks,
and naming of code addresses in write watches and hooks) do not talk to Mono
directly. They use the ``engine`` object of the Frida agent, which forwards to
the adapter whose runtime the game has loaded: Mono (grembyte/engines/mono),
IL2CPP (grembyte/engines/il2cpp) and Unreal Engine (grembyte/engines/unreal).

Python side: a subclass of :class:`EngineAdapter` says how to recognize the
engine from outside (``detect``), what it can do (``capabilities``) and which
of its code units are framework rather than game (``is_framework_assembly``).
It is listed in ``registry.ENGINES``.

Agent side: ``agent_source`` defines the adapter object and passes it to
``registerEngine``. The features call ``engine.<name>``; the contract below is
what they use. Its vocabulary follows the ECMA-335 model of .NET (assemblies,
classes, methods with metadata tokens and flags, element types), which Mono
and IL2CPP share; another kind of engine maps its own units onto it (Unreal:
packages as assemblies, UFunctions as methods, see grembyte/engines/unreal).
An adapter only needs what its ``capabilities`` promise; the facade answers
any other name with an error naming the adapter.

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
keys them by), ``classes(assembly)`` (with the methods' ECMA-335 flags). For the
call graph (``CALL_GRAPH``): ``methodBodies(assembly, tokens)`` (their IL code, see
grembyte.features.call_graph) and ``resolveTokens(assembly, tokens)`` (the
methods tokens in that code name); Unreal, without IL, has
``functionCalls(assembly, tokens)`` instead (the calls in Blueprint bytecode).

Trace (``TRACE``): ``methodsByToken(assembly, tokens)`` with ECMA-335 flags,
``checkAll(methods, progress)`` (which cannot be prepared safely),
``compileAll(methods, progress)``, ``describeAll(addresses)``,
``cancelBatches()``, ``batchesRunning()``, and ``releaseCode()``, which undoes
what ``compileAll`` changed in the game to make code hookable (once the trace
has removed its hooks).

Method monitor (``MONITOR``): ``prepare(identity)`` (code and signature),
the object model (``TYPE`` element types, ``classOf``, ``layout``,
``properties``), ``onRuntimeThread``, ``drainMain`` and ``unityName`` (the
name of a Unity object; Unreal: the UObject's name). Arguments are read where
the platform's calling convention puts them, unless ``prepare`` answers
``frame: true``: then ``callFrame(info, context)`` says on entry where each
one is (``{address}`` or ``{value}``) and where the result will be, and
types with ``engine`` set are read by ``readValue(type, address)`` (Unreal).
``layout`` may give ``headerSize``, where struct field offsets start
(Mono: after an object header). ``prepare`` answering ``returnPointerFirst``
(IL2CPP) says that a struct result's hidden pointer comes before ``this``,
``selfStruct`` (a class) that ``this`` points at the data of such a struct.

Method hooks (``METHOD_HOOKS``) also need ``writeReference``; with
``frame: true`` also ``valueJs(type, address)`` and ``writeValue(type, address,
value)`` for the types with ``engine`` set, and ``callFrame`` answers ``own``
(whether the arguments lie in the function's own frame or in the caller's
variables). Calling methods (``CALL``), and ``ctx.call`` or ``.call()`` in a
hook, need ``objectClassName``, ``classChain``, ``newString``,
``onMainThread``, ``findClass``, ``findMethod``, ``signatureOf``, ``invoke``,
``virtualMethod`` and ``getProperty``. A ``signatureOf`` answering ``parms:
true`` (Unreal) takes the arguments in one block at the parameters' offsets
(``parmsSize``, the result at ``returnOffset``): ``invoke(method, object,
block)``, and ``writeParameter(type, address, value)`` stores those of types
with ``engine`` set. ``findObject(className, name)`` gives scripts a live
object of a class, with ``name`` (or null) the one of that name (``ctx.find``),
``findObjects(className, name)`` all of them (``ctx.forEach``); Unreal's
``outerName(object)`` names an object in its outer, a name it also finds.
The object browser (``OBJECT_BROWSER``) lists the classes with live objects,
``browseClasses({filter, internal})``, and the objects of one class,
``browseInstances(klass, {filter, internal, limit})`` (see the Unreal adapter).
The reference graph (``OBJECT_REFERENCES``) names the locations that hold a
pointer to an object: ``locateReferences(locations, {maxObjects})`` answers
for each ``{location, kind, object, klass, className, name, field}``, ``kind``
'field', 'element' (of an array), 'static' (a class's static field, no
``object``) or 'unknown' (see the Mono adapter).
The object inspector lists what it can call with ``methods(klass)``: ``[{name,
method, static, owner}]``, the class's own methods first; a ``properties``
entry's ``setter`` (Mono) is the property's set_ method or null.

The platform helpers an adapter may use (``spawnThread``, ``watchUnmaps``,
``pauseThreads``) are in grembyte/agent/platform.js.
"""

import ntpath
import os
import sys

from grembyte.agent.source import load_agent_source
from grembyte.platform import read_mappings

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
CALL_GRAPH = "call_graph"
CALL = "call"
# Instance methods of the catalog called on a live object of their class
# (``findObject``); Mono only finds Unity objects, so it is offered for Unreal
CALL_FOUND = "call_found"
# The player's Gameplay Ability System attributes (see the Unreal adapter)
GAS = "gas"
# The object browser: every live object by its class (``browseClasses``,
# ``browseInstances``): Unreal's object table, Mono's liveness calculation
OBJECT_BROWSER = "object_browser"
# The reference graph: which objects hold an object (``locateReferences``)
OBJECT_REFERENCES = "object_references"


class EngineAdapter:
    """One game engine runtime grembyte knows. Subclasses fill the attributes."""

    id = ""
    display_name = ""
    capabilities = frozenset()
    # The adapter object of the agent; it calls registerEngine(...).
    agent_source = ""
    # Code units of the runtime and its frameworks rather than of the game
    framework_prefixes = ()
    # Whether the native trace and the native monitor are safe in its games:
    # they rewrite machine code the runtime may have to recognise. Where not,
    # they ask before they start.
    native_tools = True

    def detect(self, pid, proc_root="/proc"):
        """Return whether the process has loaded this engine's runtime."""
        if sys.platform == "win32":
            try:
                return any(self.is_runtime_path(mapping.path)
                           for mapping in read_mappings(pid) if mapping.module is not None)
            except OSError:
                return False
        try:
            with open(f"{proc_root}/{pid}/maps", encoding="utf-8", errors="replace") as maps:
                return any(
                    self.is_runtime_path(line.split(maxsplit=5)[-1].strip())
                    for line in maps
                    if "/" in line
                )
        except OSError:
            return False

    def is_runtime_path(self, path):
        """Whether the mapped file at ``path`` is the engine's runtime."""
        name = ntpath.basename(path) if sys.platform == "win32" else os.path.basename(path)
        return self.is_runtime_file(name)

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
