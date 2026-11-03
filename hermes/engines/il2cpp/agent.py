"""C# methods of IL2CPP games (Unity games built with IL2CPP).

IL2CPP compiles a game's C# ahead of time into one native library,
``GameAssembly.dll`` (``GameAssembly.so`` on Linux), which exports the
runtime's API (``il2cpp_*``). Unlike Mono there is no JIT: every method's
code already lies in that module, and its ``MethodInfo`` starts with the
pointer to it. Nor is there any IL left, and no table from code back to
methods.

``IL2CPP_AGENT_SOURCE`` is the IL2CPP engine adapter of the agent: it
defines ``il2cpp`` and registers it (see hermes/engines/base.py for the
contract). It offers naming, the catalog, the trace, the method monitor and
method hooks:

Catalog
    The images of the domain (named without ``.dll``, as Mono names
    assemblies) and their classes and methods, with ECMA-335 flags and
    signatures written as Mono writes them (``Ns.Class:Method (int,single)``,
    ``Ns.List`1:Add (T)``). Their MVID stands for the build of GameAssembly
    (PE time stamp and size), which holds every assembly's code.
Naming
    A table of every method's code start, built on the agent's thread once
    the runtime is found (under a second for some 230,000 methods); until
    then ``locate`` answers ``undefined``. Where the unwind data of Windows
    x64 names the function holding an address (``RtlLookupFunctionEntry``),
    the address belongs to a method only if that function starts at one;
    functions without unwind data (leaves) and Linux fall back to the method
    starting closest before it. Abstract methods are left out: their
    pointer is a stub they all share.
Trace
    ``compileAll`` only hands out the code pointers. Methods often share
    code (identical functions folded by the linker, shared generic code);
    their names say ``[shared with N]`` and the trace counts them under the
    first.

Method monitor and object inspector
    Signatures, fields and properties as Mono gives them. A method is a C
    function ``(this, arguments..., MethodInfo*)``; a struct result's
    hidden pointer comes first (``returnPointerFirst``). Objects start with
    their class, whose first field is its image; static data is found
    through the class's pointer to itself (see ``staticData``). Work that
    may set classes up runs on a thread that attaches to IL2CPP and
    detaches again (``onRuntimeThread``); getters and Unity names run on
    the main thread, reached through ``il2cpp_runtime_invoke`` as with Mono.
    Struct methods get a pointer to the struct's data as ``this``
    (``selfStruct``), and so does ``il2cpp_runtime_invoke`` for them.
Method hooks
    References are written through IL2CPP's write barrier, strings made with
    ``il2cpp_string_new``; scripts find classes, methods and Unity objects
    by name (``FindObjectsOfType(Type)``, or ``(Type, bool)`` where code
    stripping left only that, as in V Rising).

Reading a class's methods makes IL2CPP set it up, which allocates from
its own heap and not from the garbage collector; this works from the
agent's thread, which IL2CPP does not know.
"""

from hermes.core.agent_source import load_agent_source


IL2CPP_AGENT_SOURCE = load_agent_source(__file__)
