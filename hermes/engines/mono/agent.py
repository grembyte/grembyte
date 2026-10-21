"""C# method names for JIT code of Mono games (e.g. Unity games using Mono).

Mono compiles C# methods into anonymous memory, so a code address there has
no module + offset. The Mono runtime knows which method it belongs to. The
agent snippet in agent.js asks it through Mono's embedding API, which the runtime
library (``libmonobdwgc-2.0.so``, ``libmonosgen-2.0.so``, ``libmono.so``;
under Wine/Proton ``mono-2.0-bdwgc.dll``, ``mono-2.0-sgen.dll``, ``mono.dll``)
exports.

``MONO_AGENT_SOURCE`` is the Mono engine adapter of the agent: it defines
``mono`` and registers it (see hermes/engines/base.py for the contract the
features use through ``engine``; ``onMonoThread`` is exported there as
``onRuntimeThread``). ``mono`` has:

``mono.refresh()``
    Look for the Mono runtime (again, at most every two seconds) and return
    whether it is loaded. Call it from the agent's own thread.
``mono.locate(address)``
    Find the JIT method containing ``address`` and remember it. Returns the
    location, ``null`` (no JIT code) or ``undefined`` (unknown: no Mono
    runtime, or this thread is no Mono thread; ask again later). Only call it
    on a thread that runs managed code: Mono's lock-free JIT table expects a
    thread it knows, and the thread's domain is the one its code lives in.
    That makes it usable in an exception handler for a trap in JIT code.
``mono.name(address)``
    ``Namespace.Class:Method+0x1a`` for an address ``locate`` found, from
    any thread, else ``null``. It never searches the JIT table itself.
``mono.describe(address)``
    ``locate`` and ``name`` in one, for threads that run managed code.
``mono.identify(address)``
    What finds the code again after a restart, for an address ``locate``
    found: ``{image, file, token, method, offset}`` (assembly name and path,
    metadata token, ``Namespace.Class:Method (params)``, offset in its code),
    else ``null``. Token and offset stay valid while the game is unchanged.
``mono.assemblies()`` / ``mono.classes(assembly)``
    The catalog: the loaded assemblies (``{name, file, classes}``) and the
    classes of one with their methods (see ``classes`` below).
``mono.methods(assembly)`` / ``mono.compileAll(methods, progress)``
    For the trace: the methods of an assembly with their flags, and (a
    promise of) the code of many methods at once (``{start, size}`` or null
    each); ``mono.checkAll(methods)`` tells which use COM interop, whose
    compilation hangs on Linux. ``mono.describeAll(addresses)`` names many addresses like
    ``describe`` does. Both run C on a thread of Mono's own (a batch) and
    keep the agent's thread free; ``mono.cancelBatches()`` stops them.
``mono.prepare(identity)`` / ``mono.objectClassName(object)``
    For the method monitor: ``compile`` plus the method's signature, and
    the class name of a managed object.
``mono.classOf(object)`` / ``mono.layout(klass)``
    For inspecting objects: the class of an object (null for an address
    that holds none) and the fields of a class (cached).
``mono.unityName(object)``
    The name of a GameObject or component, by calling Unity's
    ``UnityEngine.Object.get_name``; only on the game's main thread.
``mono.onMainThread(work)`` / ``mono.drainMain()``
    Run game code on Unity's main thread, as soon as it runs a script
    method (a promise); ``drainMain`` runs queued work if called there.
``mono.findClass(name)`` / ``mono.findMethod(klass, name, count, static)``
    Look up game code by name, to call it with ``mono.invoke(method, this,
    params)`` (main thread); ``mono.signatureOf(method)`` describes it.
``mono.properties(klass)`` / ``mono.getProperty(object, property, isStruct)``
    The parameterless getters of a class (and a GameObject's components),
    and calling one on the main thread: ``{result, exception}``.
``mono.compile(identity)``
    Look up the method of an ``identify`` result, let Mono compile it (or
    return the code it has) and return (a promise of) ``{start, size}`` of
    its code. Rejects if the assembly is not loaded or the method has changed.

``onMonoThread``, ``compile``, ``prepare``, ``assemblies``, ``classes`` and
``methods`` return promises: the work runs on a thread Mono knows while the
agent's thread stays free (see ``onMonoThread``).

The agent's own thread is deliberately not attached to Mono: an attached
thread is stopped by every garbage collection, also while it holds the
agent's lock, which a game thread trapped in an exception handler may wait
for. ``compile`` runs on a short-lived thread of its own instead, which
Mono forgets again when it ends.
"""

from hermes.core.agent_source import load_agent_source


MONO_AGENT_SOURCE = load_agent_source(__file__)

