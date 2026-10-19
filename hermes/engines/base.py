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

Catalog (``CATALOG``): ``assemblies()``, ``classes(assembly)``.

Trace (``TRACE``): ``methods(assembly)`` with ECMA-335 flags,
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
``pauseThreads``) are in platform.py.
"""

import os

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

    def detect(self, pid, proc_root="/proc"):
        """Return whether the process has loaded this engine's runtime."""
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
ENGINE_PRELUDE_SOURCE = r"""
const engineAdapters = [];

function registerEngine(adapter) {
  engineAdapters.push(adapter);
}
"""

# After the adapters' sources: the object the features use.
ENGINE_FACADE_SOURCE = r"""
const engine = (() => {
  // ECMA-335 element types; the same for every .NET runtime.
  const TYPE = {
    VOID: 0x01, BOOLEAN: 0x02, CHAR: 0x03, I1: 0x04, U1: 0x05, I2: 0x06, U2: 0x07,
    I4: 0x08, U4: 0x09, I8: 0x0a, U8: 0x0b, R4: 0x0c, R8: 0x0d, STRING: 0x0e,
    VALUETYPE: 0x11, GENERICINST: 0x15, I: 0x18, U: 0x19
  };
  // Answers of the naming calls while no runtime is known (see base.py).
  const UNKNOWN = { locate: undefined, name: null, describe: null, identify: null };
  let active = null;

  function refresh() {
    if (active !== null)
      return true;
    for (const adapter of engineAdapters) {
      if (adapter.refresh()) {
        active = adapter;
        return true;
      }
    }
    return false;
  }

  function current() {
    if (!refresh())
      throw new Error('no supported engine runtime in the process');
    return active;
  }

  // Look once while the agent loads: exception handlers may name code
  // before any feature calls refresh().
  refresh();
  return new Proxy({}, {
    get(_target, key) {
      if (key === 'refresh')
        return refresh;
      if (key === 'TYPE')
        return TYPE;
      if (key === 'id')
        return active === null ? null : active.id;
      // Naming runs in exception handlers: it never searches for a runtime.
      if (Object.prototype.hasOwnProperty.call(UNKNOWN, key))
        return active === null ? () => UNKNOWN[key] : active[key];
      const adapter = current();
      const value = adapter[key];
      if (value === undefined)
        throw new Error(`the ${adapter.id} engine adapter has no ${String(key)}`);
      return value;
    }
  });
})();
"""
