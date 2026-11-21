"""What the code fields offer while typing: the fixed members of ``ctx``, of
object wrappers and of Frida's API, and the JavaScript keywords.

A heuristic, not a JavaScript parser: it looks at the expression before the
dot (``ctx.``, ``ctx.find("…").``, ``p.add(0x10).``, a variable assigned from
one of them) and offers what that is known to have. Where it cannot tell –
a field of a game object, the result of a game method – it offers nothing
rather than something wrong. The game's own classes and fields are not
offered yet (see ROADMAP).

The members of ``ctx`` are those of the editor's context (scripts, write or
method hooks),
limited to the attached game's engine: ``ctx.isPlaying()`` only in Unreal
games, ``ctx.static`` only in Unity ones. ``engine`` is an adapter id
("mono", "il2cpp", "unreal"), ``NATIVE`` for a game without an adapter, or
None while the engine is not known (then everything is offered).
"""

import dataclasses
import re

SCRIPT = "script"
HOOK = "hook"  # a write or a method hook
VALUE_HOOK = "value_hook"
METHOD_HOOK = "method_hook"
NATIVE = "native"

_UNITY = ("mono", "il2cpp")
_ENGINES = ("mono", "il2cpp", "unreal")
_UNREAL = ("unreal",)
_ENUMS = ("unreal", "mono")


@dataclasses.dataclass(frozen=True)
class Completion:
    """One entry of the popup.

    ``params`` is None for a value (inserted as the name), else the
    parameter list shown after the name (a method: ``name()`` is inserted).
    ``description`` is a translation key, formatted with ``values``.
    ``engines`` limits the entry to these engine adapters (None: every game).
    """

    name: str
    description: str
    params: str | None = None
    engines: tuple | None = None
    values: tuple = ()
    # Matched anywhere in the name, not only at its start (class names)
    anywhere: bool = False

    @property
    def label(self):
        """The name as the popup shows it, with the parameters of a method."""
        return self.name if self.params is None else f"{self.name}({self.params})"

    def available(self, engine):
        """Whether the entry works in a game with ``engine`` (see the module)."""
        return engine is None or self.engines is None or engine in self.engines


def _value(name, engines=None):
    return Completion(name, f"completion.{{}}.{name}", None, engines)


def _method(name, params="", engines=None, key=None, values=()):
    return Completion(name, f"completion.{{}}.{key or name}", params, engines, tuple(values))


def _group(group, entries):
    return tuple(dataclasses.replace(entry, description=entry.description.format(group))
                 for entry in entries)


# ctx in the Scripts tab (scriptContext in grembyte/features/monitor.js)
SCRIPT_CONTEXT = _group("ctx", (
    _method("find", "className[, objectName]", _ENGINES),
    _method("findClass", "className", _UNREAL),
    _method("forEach", "className, callback", _ENGINES),
    _method("object", "address", _ENGINES),
    _method("isPlaying", "", _UNREAL),
    _method("outerName", "object", _UNREAL),
    _method("enum", "enumName", _ENUMS),
    _method("enumName", "enumName, value", _ENUMS),
    _method("call", "method, ...args", _ENGINES),
    _method("static", "field", _UNITY),
    _value("gas", _UNREAL),
    _value("native"),
    _value("args"),
    _value("lang"),
    _method("log", "...items"),
    _value("state"),
    _value("runs"),
    _value("scriptId"),
))

# ctx of write hooks (grembyte/hooks/engine.js)
VALUE_HOOK_CONTEXT = _group("hook", (
    _value("old"),
    _value("new"),
    _value("delta"),
    _value("params"),
    _value("state"),
    _value("valueType"),
    _value("address"),
    _value("writePc"),
    _value("threadId"),
    _value("hookId"),
    _method("log", "...items"),
))

# ctx of method hooks (hookBefore/hookLeave in grembyte/features/monitor.js)
METHOD_HOOK_CONTEXT = tuple(entry for entry in VALUE_HOOK_CONTEXT if entry.name in {
    "params", "state", "threadId", "hookId", "log"}) + _group("hook", (
    _value("phase"),
    _value("args"),
    _value("result"),
    _value("refs"),
    _value("this"),
    _value("caller"),
    _value("method"),
)) + tuple(entry for entry in SCRIPT_CONTEXT if entry.name in {
    "call", "static", "find", "forEach", "object", "outerName", "isPlaying", "enum",
    "enumName"})

# Both, for code whose kind of hook is not known
HOOK_CONTEXT = VALUE_HOOK_CONTEXT + tuple(
    entry for entry in METHOD_HOOK_CONTEXT if entry not in VALUE_HOOK_CONTEXT)

# What every object wrapper has besides the game's fields and methods
WRAPPER = _group("wrapper", (
    _value("name"),
    _value("className"),
    _value("address"),
    _method("field", "name"),
    _method("get", "name"),
    _method("call", "name, ...args"),
    _method("component", "className", _UNITY),
))

NATIVE_CONTEXT = _group("native", (
    _method("module", "[name]"),
    _method("scan", "pattern[, module]"),
    _method("scanAll", "pattern[, module]"),
    _method("rip", "address, dispOffset, length"),
    _method("fn", "address, returnType, argTypes[, abi]"),
))

GAS = _group("gas", (
    _method("attributes"),
    _method("get", "name"),
    _method("setBase", "name, value"),
    _method("findSet", "regex"),
))

_INTEGERS = (("S8", 8), ("U8", 8), ("S16", 16), ("U16", 16), ("S32", 32), ("U32", 32),
             ("S64", 64), ("U64", 64))
# Frida's NativePointer
POINTER = _group("pointer", (
    _method("add", "offset"),
    _method("sub", "offset"),
    _method("readPointer"),
    _method("writePointer", "value"),
    *(_method(f"read{suffix}", "", key="read_signed" if suffix[0] == "S" else "read_unsigned",
              values=(("bits", bits),)) for suffix, bits in _INTEGERS),
    *(_method(f"write{suffix}", "value",
              key="write_signed" if suffix[0] == "S" else "write_unsigned",
              values=(("bits", bits),)) for suffix, bits in _INTEGERS),
    _method("readFloat"),
    _method("readDouble"),
    _method("writeFloat", "value"),
    _method("writeDouble", "value"),
    _method("readUtf8String", "[length]"),
    _method("readUtf16String", "[length]"),
    _method("writeUtf8String", "text"),
    _method("writeUtf16String", "text"),
    _method("readByteArray", "length"),
    _method("writeByteArray", "bytes"),
    _method("isNull"),
    _method("equals", "other"),
    _method("compare", "other"),
    _method("and", "mask"),
    _method("toInt32"),
    _method("toString", "[radix]"),
))

# A Frida Module (ctx.native.module(), Process.mainModule …)
MODULE = _group("module", (
    _value("name"),
    _value("base"),
    _value("size"),
    _value("path"),
    _method("findExportByName", "name"),
    _method("getExportByName", "name"),
    _method("findSymbolByName", "name"),
    _method("enumerateExports"),
    _method("enumerateImports"),
))

MEMORY = _group("memory", (
    _method("alloc", "size"),
    _method("allocUtf8String", "text"),
    _method("allocUtf16String", "text"),
    _method("copy", "destination, source, length"),
    _method("protect", "address, size, protection"),
    _method("scanSync", "address, size, pattern"),
))

PROCESS = _group("process", (
    _value("id"),
    _value("arch"),
    _value("platform"),
    _value("pointerSize"),
    _value("mainModule"),
    _method("getModuleByName", "name"),
    _method("findModuleByName", "name"),
    _method("enumerateModules"),
    _method("findRangeByAddress", "address"),
))

MODULE_API = _group("module_api", (
    _method("load", "path"),
    _method("findGlobalExportByName", "name"),
    _method("getGlobalExportByName", "name"),
))

# JavaScript's own values: what a game's texts, numbers and Unreal arrays are
# in a script (an FString, System.String, int32 or TArray reads as one)
STRING = _group("string", (
    _value("length"),
    _method("startsWith", "search"),
    _method("endsWith", "search"),
    _method("includes", "search"),
    _method("indexOf", "search"),
    _method("slice", "start[, end]"),
    _method("substring", "start[, end]"),
    _method("toLowerCase"),
    _method("toUpperCase"),
    _method("trim"),
    _method("split", "separator"),
    _method("replace", "pattern, replacement"),
    _method("padStart", "length[, fill]"),
    _method("match", "regex"),
))

NUMBER = _group("number", (
    _method("toFixed", "digits"),
    _method("toPrecision", "digits"),
    _method("toString", "[radix]"),
))

ARRAY = _group("array", (
    _value("length"),
    _method("find", "callback"),
    _method("filter", "callback"),
    _method("map", "callback"),
    _method("forEach", "callback"),
    _method("some", "callback"),
    _method("every", "callback"),
    _method("findIndex", "callback"),
    _method("indexOf", "value"),
    _method("includes", "value"),
    _method("at", "index"),
    _method("slice", "start[, end]"),
    _method("sort", "[compare]"),
    _method("join", "[separator]"),
))

# Names that stand on their own: Frida's globals and ctx
GLOBALS = _group("global", (
    _value("ctx"),
    _method("ptr", "value"),
    _value("NULL"),
    _value("NativePointer"),
    _value("NativeFunction"),
    _value("Memory"),
    _value("Process"),
    _value("Module"),
    _method("hexdump", "target[, options]"),
    _value("Int64"),
    _value("UInt64"),
    _value("console"),
))

# The JavaScript keywords and literals a script uses (the highlighter's list
# without the words of TypeScript and modules, which a script cannot use)
KEYWORDS = tuple(Completion(word, "completion.keyword") for word in sorted({
    "async", "await", "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "extends", "false", "finally", "for", "function",
    "if", "in", "instanceof", "let", "new", "null", "of", "return", "super", "switch",
    "this", "throw", "true", "try", "typeof", "undefined", "var", "void", "while", "yield",
}))

# Words a variable cannot be named
_RESERVED = frozenset(entry.name for entry in KEYWORDS) | {"ctx"}

# What an expression is, and what follows its dot
_POINTER, _WRAPPER, _MODULE, _CLASS = "pointer", "wrapper", "module", "class"
_STRING, _NUMBER, _ARRAY = "string", "number", "array"
_MEMBERS = {
    _POINTER: POINTER, _WRAPPER: WRAPPER, _MODULE: MODULE, "native": NATIVE_CONTEXT,
    _STRING: STRING, _NUMBER: NUMBER,
    "gas": GAS, "memory": MEMORY, "process": PROCESS, "module_api": MODULE_API,
}
# NativePointer methods that give a NativePointer again
_POINTER_METHODS = frozenset({"add", "sub", "and", "or", "xor", "readPointer"})
# Names that are known on their own: (name, called) -> kind
_ROOTS = {
    ("ctx", False): "ctx", ("ptr", True): _POINTER, ("NULL", False): _POINTER,
    ("Memory", False): "memory", ("Process", False): "process", ("Module", False): "module_api",
}
# What a step gives: (kind of the object, member, called) -> kind
_STEPS = {
    ("ctx", "find", True): _WRAPPER,
    ("ctx", "object", True): _WRAPPER,
    ("ctx", "findClass", True): _WRAPPER,
    ("ctx", "this", False): _WRAPPER,
    ("ctx", "native", False): "native",
    ("ctx", "gas", False): "gas",
    ("native", "scan", True): _POINTER,
    ("native", "rip", True): _POINTER,
    ("native", "module", True): _MODULE,
    (_MODULE, "base", False): _POINTER,
    ("process", "mainModule", False): _MODULE,
    ("process", "getModuleByName", True): _MODULE,
    ("process", "findModuleByName", True): _MODULE,
    ("module_api", "load", True): _MODULE,
    ("module_api", "findGlobalExportByName", True): _POINTER,
    ("module_api", "getGlobalExportByName", True): _POINTER,
    (_WRAPPER, "component", True): _WRAPPER,
    # a wrapper's address; nothing else offered here has one
    (_WRAPPER, "address", False): _POINTER,
    (None, "address", False): _POINTER,
    **{(_POINTER, name, True): _POINTER for name in _POINTER_METHODS},
}
_PREFIX = re.compile(r"[\w$]*$")
# Steps and variables an expression is followed through (a guard against loops)
_MAX_DEPTH = 32


def _skip_back(text, end, opening, closing):
    """The index of the bracket ``opening`` matching the ``closing`` one at
    ``end - 1``, or -1. Brackets inside strings are not told apart."""
    depth = 0
    for position in range(end - 1, -1, -1):
        if text[position] == closing:
            depth += 1
        elif text[position] == opening:
            depth -= 1
            if depth == 0:
                return position
    return -1


def receiver(text):
    """The member chain ``text`` ends with: ``ctx.find("A").address`` for
    ``x = ctx.find("A").address``; "" if it does not end with one."""
    end = len(text.rstrip())
    position = end
    while position > 0:
        character = text[position - 1]
        if character in ")]":
            start = _skip_back(text, position, "([" [")]".index(character)], character)
            if start < 0:
                return ""
            position = start
            continue
        match = re.search(r"[\w$]+$", text[:position])
        if match is None:
            break
        position = match.start()
        before = text[:position].rstrip()
        if before.endswith("?."):
            position = len(before) - 2
        elif before.endswith("."):
            position = len(before) - 1
        else:
            # ``new NativePointer(…)`` is one expression
            if before.endswith("new") and not re.search(r"[\w$]new$", before):
                position = len(before) - 3
            break
    return text[position:end].strip()


def _split_last(chain):
    """``(object, member, called, arguments)`` of the chain's last step:
    ``("p", "add", True, "4")`` for ``p.add(4)``; object "" for a chain of
    one name, arguments None for a step without a call."""
    chain = chain.strip()
    arguments = None
    if chain.endswith(")"):
        start = _skip_back(chain, len(chain), "(", ")")
        if start < 0:
            return None
        arguments = chain[start + 1:-1]
        chain = chain[:start].rstrip()
    elif chain.endswith("]"):
        return None  # an element of an array or a computed member
    match = re.search(r"(\?\.|\.)?\s*([\w$]+)$", chain)
    if match is None:
        return None
    rest = chain[:match.start()].rstrip()
    called = arguments is not None
    if match.group(1):
        return (rest, match.group(2), called, arguments) if rest else None
    return None if rest else ("", match.group(2), called, arguments)


# The members of a game class (see ClassMembers.from_agent) while the agent is
# asked for them
PENDING = object()
# What the game answered for a tooltip when it has no such class or field
MISSING = object()
# Types whose values are no wrappers: numbers, texts, enums of Unreal
_PLAIN_TYPES = frozenset({
    "bool", "boolean", "choice", "byte", "sbyte", "char", "short", "ushort", "int", "uint", "long",
    "ulong", "single", "float", "double", "decimal", "string", "object", "void",
    "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
    "intptr", "uintptr", "fstring", "fname", "ftext", "?",
})
_STRING_ARGUMENT = re.compile(r"""\s*(["'`])([^"'`$]+)\1\s*(?:,|$)""")


# The type ctx.args.<name> has for a script parameter of each kind (see
# ScriptParam): a JavaScript text, number or boolean; a choice's value is
# whatever its options give
SCRIPT_PARAM_TYPES = {"text": "string", "number": "number", "bool": "boolean",
                      "choice": "choice"}


def class_type(type_name):
    """The class a value of ``type_name`` is a wrapper of, as the agent looks
    it up, or None: numbers, texts, enums of Unreal, arrays and generic types
    are not followed."""
    name = (type_name or "").strip().rstrip("&*").strip()
    if not name or "<" in name or "[" in name or "`" in name:
        return None
    short = name.rsplit(".", 1)[-1] if name.startswith("System.") else name
    if short.lower() in _PLAIN_TYPES or name.lower() in _PLAIN_TYPES:
        return None
    if name.startswith("E") and name[1:2].isupper() and "." not in name:
        return None  # an Unreal enum (EPrimaryAttribute)
    return name


@dataclasses.dataclass(frozen=True)
class ClassMembers:
    """What a game class has: fields and properties (name -> type) and
    methods (name -> [(parameters, return type)], one per overload)."""

    name: str
    fields: dict
    properties: dict
    methods: dict
    statics: frozenset = frozenset()

    @classmethod
    def from_agent(cls, members):
        """From the agent's ``classMembers`` answer (grembyte/features/monitor.js)."""
        fields, properties, methods, statics = {}, {}, {}, set()
        for field in members.get("fields", ()):
            if field["name"].startswith("<"):
                continue  # compiler-made, e.g. a property's backing field
            fields.setdefault(field["name"], field["type"])
            if field.get("static"):
                statics.add(field["name"])
        for prop in members.get("properties", ()):
            properties.setdefault(prop["name"], prop["type"])
        for method in members.get("methods", ()):
            name = method["name"]
            if name.startswith(("<", ".", "op_")):
                continue
            params = ", ".join(f"{kind} {param}".strip() for kind, param in method["params"])
            overloads = methods.setdefault(name, [])
            if (params, method["returns"]) not in overloads:
                overloads.append((params, method["returns"]))
        return cls(members.get("name") or "", fields, properties, methods, frozenset(statics))

    def member_type(self, name, called):
        """The type of field or property ``name`` (``called`` False) or the
        return type of method ``name``; None if the class has no such member,
        or overloads return different types."""
        if not called:
            return self.fields.get(name, self.properties.get(name))
        returns = {result for _params, result in self.methods.get(name, ())}
        return returns.pop() if len(returns) == 1 else None

    def entries(self):
        """The members as Completions: fields and properties, then methods."""
        values = [
            Completion(name, "completion.member.static" if name in self.statics
                       else "completion.member.field", values=(("type", kind),))
            for name, kind in self.fields.items()]
        values += [Completion(name, "completion.member.property", values=(("type", kind),))
                   for name, kind in self.properties.items() if name not in self.fields]
        values.sort(key=lambda entry: entry.name.lower())
        methods = [Completion(name, "completion.member.method", params, values=(("type", result),))
                   for name, overloads in self.methods.items()
                   for params, result in overloads]
        methods.sort(key=lambda entry: entry.name.lower())
        return tuple(values + methods)


@dataclasses.dataclass
class Scope:
    """What the completion knows beyond the fixed names.

    ``lookup(type_name)`` gives ClassMembers, None or PENDING (see
    grembyte.ui.widgets.member_lookup); ``args`` are a method hook's
    parameters (name -> type) and ``this`` its class. ``pending`` is set when
    an answer of the lookup is still missing.
    """

    lookup: object = None
    args: dict = dataclasses.field(default_factory=dict)
    this: str | None = None
    pending: bool = False

    def members(self, type_name):
        """The ClassMembers of ``type_name``, or None."""
        name = class_type(type_name)
        if name is None or self.lookup is None:
            return None
        found = self.lookup(name)
        if found is PENDING:
            self.pending = True
            return None
        return found

    def class_names(self):
        """The names of the classes ctx.find finds, () while not known."""
        return self._list("class_names")

    def static_fields(self):
        """``[name, type]`` of the static fields ctx.static reads, () while
        not known."""
        return self._list("static_fields")

    def static_methods(self):
        """``[name, parameters, return type]`` of the static methods ctx.call
        calls, () while not known."""
        return self._list("static_methods")

    def answer(self, kind, name):
        """What the lookup's ``kind`` (class_info, static_value) gives for
        ``name``: a dict, MISSING, PENDING, or None without a game."""
        ask = getattr(self.lookup, kind, None)
        return None if ask is None else ask(name)

    def _list(self, kind):
        ask = getattr(self.lookup, kind, None)
        found = None if ask is None else ask()
        if found is PENDING:
            self.pending = True
            return ()
        return found or ()


# The name a ctx.find("…", ctx.forEach("…", ctx.static("…" or ctx.call("…" is
# typed with: the function, the quote and the text typed so far in the string
_STRING_ARGUMENT_AT_END = re.compile(
    r"""\bctx\s*\.\s*(find|forEach|static|call)\s*\(\s*(["'`])([^"'`\\$]*)$""")
MAX_NAMES = 200


def string_argument(line):
    """``(typed, quote, function)`` if ``line`` ends inside the first
    argument of ctx.find, ctx.forEach, ctx.static or ctx.call (a class, a
    static field's or a static method's name), else None."""
    match = _STRING_ARGUMENT_AT_END.search(line)
    return None if match is None else (match.group(3), match.group(2), match.group(1))


_STRING_ARGUMENT_IN_LINE = re.compile(
    r"""\bctx\s*\.\s*(find|forEach|static|call)\s*\(\s*(["'`])([^"'`\\$]*)\2""")


def string_at(line, column):
    """``(function, text)`` of the first argument of ctx.find, ctx.forEach or
    ctx.static on ``line`` whose string (quotes included) holds ``column``,
    else None."""
    for match in _STRING_ARGUMENT_IN_LINE.finditer(line):
        if match.start(3) - 1 <= column <= match.end(3):
            return match.group(1), match.group(3)
    return None


def tooltip(function, text, scope, translations, engine=None):
    """The tooltip of the string ``text`` given to ctx.find, ctx.forEach
    (the class: its base classes, in Unreal games its live objects) or
    ctx.static (the field's type and value now); None for none. While the
    game is asked it says so, and ``scope.pending`` is set."""
    allowed = _UNITY if function == "static" else _ENGINES
    if not text.strip() or (engine is not None and engine not in allowed):
        return None
    if function == "call":
        return _call_tooltip(text, scope, translations)
    kind = "static_value" if function == "static" else "class_info"
    found = scope.answer(kind, text)
    if found is PENDING:
        scope.pending = True
        return translations["completion.tooltip_reading"]
    if function == "static":
        return _static_tooltip(text, found, scope, translations)
    if found is MISSING:
        return translations["completion.tooltip_no_class"].format(name=text)
    if found is None:
        return None
    lines = [translations["completion.tooltip_class"].format(name=found["name"])]
    if found.get("parents"):
        parents = " → ".join(found["parents"]) + (" → …" if found.get("more") else "")
        lines.append(translations["completion.tooltip_parents"].format(parents=parents))
    if found.get("objects") is not None:
        lines.append(translations["completion.tooltip_objects"].format(count=found["objects"]))
    return "\n".join(lines)


def static_method_overloads(text, scope):
    """``[(name, parameters, return type), ...]`` of the static method
    ``text`` names as ctx.call takes it (``Ns.Class:Method``, also with a
    dot or a parameter list after it), from the list of static methods;
    case does not matter, as in the game."""
    name = re.sub(r"\s*\(.*\)\s*$", "", text.strip())
    if ":" not in name:
        owner, dot, method = name.rpartition(".")
        name = f"{owner}:{method}" if dot else name
    wanted = name.lower()
    return [tuple(entry) for entry in scope.static_methods() if entry[0].lower() == wanted]


def _call_tooltip(text, scope, translations):
    """The tooltip of the method named in ctx.call("…"): its overloads with
    parameters and return type; None while nothing is known (no game)."""
    overloads = static_method_overloads(text, scope)
    if scope.pending:
        return translations["completion.tooltip_reading"]
    if overloads:
        return "\n".join(f"{name}({params}) → {returns}" for name, params, returns in overloads)
    if not scope.static_methods():
        return None
    return translations["completion.tooltip_no_static_method"].format(name=text)


def _static_tooltip(text, found, scope, translations):
    if found is MISSING:
        return translations["completion.tooltip_no_static"].format(name=text)
    if found is None:
        # No game: the type, if the list of static fields is known
        known = dict(scope.static_fields())
        return (translations["completion.tooltip_static_type"].format(name=text, type=known[text])
                if text in known else None)
    value = (found.get("value") or {}).get("text") or "—"
    return translations["completion.tooltip_static"].format(
        name=text, type=found.get("type", "?"), value=value)


def matches(entry, typed):
    """Whether ``entry`` fits what was typed of its name: its start, or for
    class names any part of it (``Player`` finds ``BP_PlayerCharacter_C``)."""
    name, wanted = entry.name.lower(), typed.lower()
    return name.startswith(wanted) or (entry.anywhere and wanted in name)


def _name_entries(typed, function, scope):
    """The class names (ctx.find, ctx.forEach) or static fields (ctx.static)
    containing ``typed``, those starting with it first, at most MAX_NAMES."""
    if function == "call":
        return _method_entries(typed, scope)
    if function == "static":
        named = [(name, type_name) for name, type_name in scope.static_fields()]
    else:
        named = [(name, None) for name in scope.class_names()]
    wanted = typed.lower()
    found = [entry for entry in named if wanted in entry[0].lower()]
    found.sort(key=lambda entry: (not entry[0].lower().startswith(wanted), entry[0].lower()))
    return tuple(
        Completion(name, "completion.class", anywhere=True) if type_name is None
        else Completion(name, "completion.member.static", values=(("type", type_name),),
                        anywhere=True)
        for name, type_name in found[:MAX_NAMES])


def _method_entries(typed, scope):
    """The static methods (ctx.call) whose names contain ``typed``, those
    starting with it first, at most MAX_NAMES; one entry per overload."""
    wanted = typed.lower()
    found = [entry for entry in scope.static_methods() if wanted in entry[0].lower()]
    found.sort(key=lambda entry: (not entry[0].lower().startswith(wanted), entry[0].lower()))
    return tuple(Completion(name, "completion.static_method", params, values=(("type", returns),),
                            anywhere=True)
                 for name, params, returns in found[:MAX_NAMES])


def method_parameters(signature):
    """``(class, {name: type})`` of a method signature with parameter names,
    as the hook dialog shows it: ``Game.Combat:RollDamage (CharacterStats
    roller,single scale)`` or with full types (``System.Int32 min``). A
    parameter without a name is left out."""
    head, separator, rest = (signature or "").partition(" (")
    owner = head.partition(":")[0].strip() or None
    if not separator:
        return owner, {}
    depth, part, parts = 0, "", []
    for character in rest[:rest.rfind(")")] if ")" in rest else rest:
        depth += {"<": 1, ">": -1}.get(character, 0)
        if character == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += character
    parts.append(part)
    params = {}
    for entry in parts:
        kind, _space, name = entry.strip().rpartition(" ")
        if kind and re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            params[name] = kind.strip()
    return owner, params


def _class_kind(type_name):
    name = class_type(type_name)
    return None if name is None else (_CLASS, name)


_STRING_TYPES = frozenset({"string", "system.string", "fstring", "fname", "ftext"})
_NUMBER_TYPES = frozenset({
    "number", "byte", "sbyte", "short", "ushort", "int", "uint", "long", "ulong", "single", "float",
    "double", "decimal", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32",
    "uint64", "system.byte", "system.sbyte", "system.int16", "system.uint16", "system.int32",
    "system.uint32", "system.int64", "system.uint64", "system.single", "system.double",
    "system.decimal"})
_POINTER_TYPES = frozenset({"intptr", "uintptr", "system.intptr", "system.uintptr"})
# Unreal's containers that read as JavaScript arrays (see engine.valueJs)
_ARRAY_TYPE = re.compile(r"(?:TArray|TSet)<(.+)>")


def value_kind(type_name):
    """What a value of ``type_name`` is in a script: a wrapper of a class
    (``("class", name)``), "string", "number", "pointer", an Unreal array of
    elements (``("array", element type)``), or None (bool, enum, unknown)."""
    name = (type_name or "").strip().rstrip("&").strip()
    lower = name.lower()
    if lower in _STRING_TYPES:
        return _STRING
    if lower in _NUMBER_TYPES:
        return _NUMBER
    if lower in _POINTER_TYPES:
        return _POINTER
    array = _ARRAY_TYPE.fullmatch(name)
    if array is not None:
        return (_ARRAY, array.group(1).strip())
    return _class_kind(name)


# What a step on a JavaScript value gives: (kind, member) of a call -> kind;
# "element" for an array's element, "same" for the array itself
_VALUE_STEPS = {
    _STRING: {**dict.fromkeys(("slice", "substring", "toLowerCase", "toUpperCase", "trim",
                               "replace", "padStart"), _STRING),
              "indexOf": _NUMBER, "split": (_ARRAY, "System.String")},
    _NUMBER: dict.fromkeys(("toFixed", "toPrecision", "toString"), _STRING),
    _ARRAY: {**dict.fromkeys(("find", "at"), "element"),
             **dict.fromkeys(("filter", "slice", "sort"), "same"),
             "findIndex": _NUMBER, "indexOf": _NUMBER, "join": _STRING},
}
# Array methods whose callback gets an element first: arr.find(u => u.…)
_CALLBACK_METHODS = ("find", "filter", "map", "forEach", "some", "every", "findIndex",
                     "findLast", "findLastIndex", "flatMap")


def _value_step(kind, member, called):
    """The kind a member of a JavaScript value gives (see _VALUE_STEPS)."""
    base = kind[0] if isinstance(kind, tuple) else kind
    if member == "length" and not called and base in (_STRING, _ARRAY):
        return _NUMBER
    result = _VALUE_STEPS.get(base, {}).get(member) if called else None
    if result == "element":
        return value_kind(kind[1])
    if result == "same":
        return kind
    return result


def classify(chain, source="", depth=0, scope=None):
    """What the expression ``chain`` gives, as far as known: "ctx",
    "pointer", "wrapper", "module", "native", "gas", "memory", "process",
    "module_api" (Frida's ``Module``), "args" (a method hook's ctx.args),
    ``("class", name)`` for a wrapper of a known class, or None. ``source``
    is the code before it, where a variable's assignment is looked up."""
    if depth > _MAX_DEPTH:
        return None
    scope = scope or Scope()
    chain = chain.strip()
    if chain.startswith("new "):
        return _POINTER if re.fullmatch(r"new\s+NativePointer\s*\(.*\)", chain, re.S) else None
    if chain.endswith("]"):
        # arr[0]: an element of an Unreal array
        start = _skip_back(chain, len(chain), "[", "]")
        owner = chain[:start].rstrip().removesuffix("?.") if start > 0 else ""
        kind = classify(owner, source, depth + 1, scope) if owner else None
        return value_kind(kind[1]) if isinstance(kind, tuple) and kind[0] == _ARRAY else None
    parts = _split_last(chain)
    if parts is None:
        return None
    owner, member, called, arguments = parts
    if owner == "":
        if (member, called) in _ROOTS:
            return _ROOTS[member, called]
        return None if called else _variable(member, source, depth, scope)
    kind = classify(owner, source, depth + 1, scope)
    if kind == "ctx":
        return _ctx_step(member, called, arguments, scope)
    if kind == "args":
        return None if called else value_kind(scope.args.get(member))
    if isinstance(kind, tuple) and kind[0] == _CLASS:
        return _class_step(kind[1], member, called, arguments, scope)
    if kind in (_STRING, _NUMBER) or (isinstance(kind, tuple) and kind[0] == _ARRAY):
        return _value_step(kind, member, called)
    return _STEPS.get((kind, member, called))


def _ctx_step(member, called, arguments, scope):
    if member in ("find", "findClass") and called:
        # ctx.find("BP_PlayerCharacter_C"): a wrapper of that class
        named = _STRING_ARGUMENT.match(arguments or "")
        return _class_kind(named.group(2)) if named else _WRAPPER
    if member == "this" and not called:
        return _class_kind(scope.this) if scope.this else _WRAPPER
    if member == "static" and called:
        # ctx.static("Ns.Class.Field"): an object of the field's type
        named = _STRING_ARGUMENT.match(arguments or "")
        if named is None:
            return None
        owner, _dot, field = named.group(2).replace(":", ".").rpartition(".")
        members = scope.members(owner) if owner else None
        return None if members is None else value_kind(members.member_type(field, False))
    if member == "call" and called:
        # ctx.call("Ns.Class:Method"): what the method returns
        named = _STRING_ARGUMENT.match(arguments or "")
        if named is None:
            return None
        name = re.sub(r"\s*\(.*\)\s*$", "", named.group(2))
        split = name.rfind(":") if ":" in name else name.rfind(".")
        members = scope.members(name[:split]) if split > 0 else None
        return None if members is None else value_kind(
            members.member_type(name[split + 1:], True))
    if member == "args" and not called and scope.args:
        return "args"
    return _STEPS.get(("ctx", member, called))


def _class_step(class_name, member, called, arguments, scope):
    if member == "address" and not called:
        return _POINTER
    if member == "component" and called:
        named = _STRING_ARGUMENT.match(arguments or "")
        return _class_kind(named.group(2)) if named else _WRAPPER
    if member in ("field", "get", "call") and called:
        named = _STRING_ARGUMENT.match(arguments or "")
        if named is None:
            return None
        member, called = named.group(2), member == "call"
    members = scope.members(class_name)
    return None if members is None else value_kind(members.member_type(member, called))


def _variable(name, source, depth, scope):
    """What the variable ``name`` holds, from its last ``const``/``let``/``var``
    before the cursor, or as the element of an array it walks: a callback's
    first parameter (``arr.find(u => …)``) or ``for (const x of arr)``; None
    if it is not known."""
    escaped = re.escape(name)
    candidates = []
    for match in re.finditer(rf"\b(?:const|let|var)\s+{escaped}\s*=\s*([^;\n]+)", source):
        candidates.append((match.start(), "value", match))
    for match in re.finditer(
            rf"(?:\?\.|\.)\s*(?:{'|'.join(_CALLBACK_METHODS)})\s*\(\s*"
            rf"(?:\(\s*{escaped}\s*(?:,[^)]*)?\)|{escaped})\s*=>", source):
        candidates.append((match.start(), "callback", match))
    for match in re.finditer(
            rf"\bfor\s*\(\s*(?:const|let|var)\s+{escaped}\s+of\s+([^)\n]+)\)", source):
        candidates.append((match.start(), "of", match))
    if not candidates:
        return None
    start, how, match = max(candidates, key=lambda candidate: candidate[0])
    if how == "value":
        value = re.sub(r"//.*$", "", match.group(1)).strip()
        return classify(value, source[:start], depth + 1, scope)
    walked = (receiver(source[:start].rstrip()) if how == "callback"
              else match.group(1).strip())
    kind = classify(walked, source[:start], depth + 1, scope) if walked else None
    return value_kind(kind[1]) if isinstance(kind, tuple) and kind[0] == _ARRAY else None


_DECLARED = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|\bfunction\s+([A-Za-z_$][\w$]*)"
    r"|(?:\(\s*|,\s*)([A-Za-z_$][\w$]*)\s*(?=[,)][^;{]*=>)"
    r"|\b([A-Za-z_$][\w$]*)\s*=>")


def declared_names(source):
    """The names ``source`` declares (variables, functions, parameters of
    arrow functions), in order, each once; scopes are not told apart."""
    names = []
    for match in _DECLARED.finditer(source):
        name = next(group for group in match.groups() if group)
        if name not in names and name not in _RESERVED:
            names.append(name)
    return names


def _in_code(line, state):
    """Whether the end of ``line`` is code, not a string or a comment; ``state``
    is the line's starting state as the JavaScript highlighter keeps it."""
    # pylint: disable=import-outside-toplevel
    from grembyte.ui.widgets.code_editor import highlight_javascript_tokens

    tokens = highlight_javascript_tokens(line + " ", state)
    return tokens[len(line)] not in ("string", "comment")


_CONTEXTS = {SCRIPT: SCRIPT_CONTEXT, HOOK: HOOK_CONTEXT, VALUE_HOOK: VALUE_HOOK_CONTEXT,
             METHOD_HOOK: METHOD_HOOK_CONTEXT}


def hook_context(kind):
    """The completion context of a hook of ``kind`` ("value", "method"; else
    either)."""
    return {"value": VALUE_HOOK, "method": METHOD_HOOK}.get(kind, HOOK)


def completions(source, line, state=0, context=SCRIPT, engine=None, scope=None):
    """``(prefix, entries)``: what to offer at the end of ``source`` (all code
    before the cursor), whose last line ``line`` starts in highlighter state
    ``state``. ``prefix`` is the part of the name typed already; entries is
    empty where nothing is known. ``scope`` (a Scope) adds the game's
    classes and a method hook's arguments; its ``pending`` tells whether
    members are still being read."""
    scope = scope or Scope()
    scope.pending = False
    argument = string_argument(line)
    if argument is not None and not _in_code(line[:len(line) - len(argument[0]) - 1], state):
        argument = None  # the ctx.find is in a comment or a string itself
    if argument is not None:
        typed, _quote, function = argument
        allowed = (_UNITY if function == "static" else _ENGINES)
        if engine is not None and engine not in allowed:
            return typed, ()
        return typed, _name_entries(typed, function, scope)
    prefix = _PREFIX.search(line)[0]  # always matches, maybe empty
    if not _in_code(line, state):
        return prefix, ()
    before = source[:len(source) - len(prefix)]
    stripped = before.rstrip()
    if stripped.endswith("."):
        if stripped.endswith("..."):
            return prefix, ()
        owner = stripped[:-2] if stripped.endswith("?.") else stripped[:-1]
        if owner and owner[-1].isdigit() and re.search(r"(?<![\w$])\d+$", owner):
            return prefix, ()  # a number: 1.5
        chain = receiver(owner.rstrip())
        if not chain:
            return prefix, ()
        kind = classify(chain, before, scope=scope)
        if kind == "ctx":
            members = _CONTEXTS[context]
        elif kind == "args":
            members = tuple(Completion(name, "completion.member.argument",
                                       values=(("type", type_name),))
                            for name, type_name in scope.args.items())
        elif isinstance(kind, tuple) and kind[0] == _ARRAY:
            members = ARRAY
        elif isinstance(kind, tuple):
            found = scope.members(kind[1])
            members = (found.entries() if found is not None else ()) + WRAPPER
        else:
            members = _MEMBERS.get(kind, ())
        return prefix, tuple(entry for entry in members if entry.available(engine))
    if not expression_starts(line[:len(line) - len(prefix)]):
        return prefix, ()
    globals_ = {entry.name for entry in GLOBALS}
    variables = tuple(_variable_entry(name, before, scope)
                      for name in declared_names(before) if name not in globals_)
    return prefix, variables + GLOBALS + KEYWORDS


def _variable_entry(name, source, scope):
    """The entry of a variable of the script, with what it holds if known
    (as its tooltip says, see variable_tooltip)."""
    kind = _variable(name, source, 0, scope)
    if kind is None:
        return Completion(name, "completion.variable")
    return Completion(name, "completion.variable_typed", values=(("kind", kind),))


# Before a name that starts an expression or a statement: an operator or an
# opening bracket, or a keyword that takes one (return ctx…, typeof ptr…)
_EXPRESSION_START_CHARACTERS = frozenset("=(,;{}[!?:&|+-*/<>%~^")
_EXPRESSION_START_WORD = re.compile(
    r"(?<![\w$.])(?:return|typeof|new|await|case|else|in|of|do|throw|yield|void|delete|"
    r"instanceof)$")


def expression_starts(text):
    """Whether a new expression or statement can start after ``text`` (the
    line before a name): at the start of the line, after an operator, an
    opening bracket or a keyword like ``return``; not after a closed
    bracket, a string, a number or another name, where only an operator or a
    dot can follow."""
    text = text.rstrip()
    if not text:
        return True
    return text[-1] in _EXPRESSION_START_CHARACTERS or bool(_EXPRESSION_START_WORD.search(text))


def description(entry, translations):
    """The entry's translated description; a variable's ``kind`` is named as
    its tooltip names it."""
    values = dict(entry.values)
    if "kind" in values:
        values["type"] = _kind_text(values.pop("kind"), translations)
    return translations[entry.description].format(**values)


# --- Signature while typing ---------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Signature:
    """One way to call the method whose arguments are being typed: its name,
    parameters (texts) and return type (None if not known); ``entry`` is the
    fixed Completion it stems from, for its description, else None."""

    name: str
    params: tuple
    returns: str | None = None
    entry: Completion | None = None


def split_params(text):
    """The parameters of a parameter list as the completion shows it:
    ``className[, objectName]`` is ``("className", "[objectName]")``,
    ``System.Int32 min, Dictionary<A, B> table`` keeps the generic's comma."""
    text = (text or "").replace("[, ", ", [")
    params, depth, part = [], 0, ""
    for character in text:
        depth += {"<": 1, "(": 1, "[": 1, ">": -1, ")": -1, "]": -1}.get(character, 0)
        if character == "," and depth == 0:
            params.append(part.strip())
            part = ""
        else:
            part += character
    if part.strip():
        params.append(part.strip())
    return tuple(params)


def _open_call(source):
    """``(position of its "(", arguments before the cursor)`` of the innermost
    call whose parentheses are open at the end of ``source``, or None (also
    inside a comment). Strings, comments and other brackets are skipped."""
    stack = []  # [bracket, position, commas]
    position, end = 0, len(source)
    while position < end:
        character = source[position]
        if character in "'\"`":
            closing = source.find(character, position + 1)
            while closing > 0 and source[closing - 1] == "\\":
                closing = source.find(character, closing + 1)
            if closing < 0:
                break  # the cursor is inside this string: an argument being typed
            position = closing + 1
            continue
        if source.startswith("//", position):
            newline = source.find("\n", position)
            if newline < 0:
                return None
            position = newline + 1
            continue
        if source.startswith("/*", position):
            closing = source.find("*/", position + 2)
            if closing < 0:
                return None
            position = closing + 2
            continue
        if character in "([{":
            stack.append([character, position, 0])
        elif character in ")]}":
            if stack:
                stack.pop()
        elif character == "," and stack:
            stack[-1][2] += 1
        position += 1
    for bracket, opened, commas in reversed(stack):
        if bracket == "(":
            return opened, commas
    return None


def signature_help(source, context=SCRIPT, engine=None, scope=None):
    """``(signatures, index)`` of the call whose arguments are typed at the
    end of ``source``: the ways to call it (Signature, one per overload) and
    the number of the argument at the cursor; None where no known method is
    called. ``scope.pending`` tells whether the game is still asked."""
    scope = scope or Scope()
    scope.pending = False
    found = _open_call(source)
    if found is None:
        return None
    opened, index = found
    chain = receiver(source[:opened].rstrip())
    parts = _split_last(chain) if chain else None
    if parts is None or parts[2]:
        return None  # not a name before "(", or a call's result called
    owner, member, _called, _arguments = parts
    signatures = None
    if member == "call" and index > 0 and classify(owner, source[:opened], scope=scope) == "ctx":
        signatures = _call_signatures(source[opened + 1:], scope)
    if not signatures:
        signatures = _signatures(owner, member, source[:opened], context, engine, scope)
    return (tuple(signatures), index) if signatures else None


def _call_signatures(arguments, scope):
    """The overloads of the static method ctx.call names first in
    ``arguments`` (the text after its "("), as Signatures of ctx.call: the
    name, then the method's parameters; [] if not known."""
    named = _STRING_ARGUMENT.match(arguments)
    if named is None:
        return []
    return [Signature("ctx.call", (f'"{name}"',) + split_params(params), returns)
            for name, params, returns in static_method_overloads(named.group(2), scope)]


def _signatures(owner, member, source, context, engine, scope):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def fixed(entries):
        return [Signature(entry.name, split_params(entry.params), None, entry)
                for entry in entries
                if entry.name == member and entry.params is not None and entry.available(engine)]

    if owner == "":
        return fixed(GLOBALS)
    kind = classify(owner, source, scope=scope)
    if kind == "ctx":
        return fixed(_CONTEXTS[context])
    if isinstance(kind, tuple) and kind[0] == _ARRAY:
        return fixed(ARRAY)
    if isinstance(kind, tuple):
        members = scope.members(kind[1])
        overloads = members.methods.get(member, ()) if members is not None else ()
        if overloads:
            return [Signature(member, split_params(params), returns)
                    for params, returns in overloads]
        return fixed(WRAPPER)
    return fixed(_MEMBERS.get(kind, ()))


# --- Tooltips of the script's variables ---------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


def identifier_at(line, column):
    """``(name, start)`` of the name on ``line`` that holds ``column``, or None."""
    for match in _IDENTIFIER.finditer(line):
        if match.start() <= column < match.end():
            return match.group(), match.start()
    return None


def _kind_text(kind, translations):
    """What a kind of value is, as a tooltip names it."""
    if isinstance(kind, tuple):
        if kind[0] == _ARRAY:
            return translations["completion.kind_array"].format(element=kind[1])
        return kind[1]
    key = {_STRING: "string", _NUMBER: "number", _POINTER: "pointer", _MODULE: "module",
           _WRAPPER: "object", "ctx": "ctx", "native": "native", "gas": "gas"}.get(kind)
    return translations[f"completion.kind_{key}" if key else "completion.kind_unknown"]


def variable_tooltip(name, source, scope, translations):
    """The tooltip of the script's variable ``name``: what it holds (a game
    class, an array of …, a text, a number, a Frida pointer …), as far as the
    completion can tell from ``source`` (the code up to the end of the line
    it is shown for); None if ``source`` declares no such name. While the
    game is asked it says so, and ``scope.pending`` is set."""
    if name not in declared_names(source):
        return None
    kind = _variable(name, source, 0, scope)
    if scope.pending:
        return translations["completion.tooltip_reading"]
    return translations["completion.tooltip_variable"].format(
        name=name, type=_kind_text(kind, translations))


# --- Ctrl+click: what can be inspected ----------------------------------------------
#
# A string in ctx.find("…") or ctx.static("…"), or a variable holding a
# lookup, opens the object it gives in the object inspector. That runs code
# in the game, so only lookups qualify: ctx.find and ctx.static with string
# arguments, then fields and properties read from them (a property runs its
# getter, as the inspector's does) – no method calls, no ctx.args or ctx.state,
# nothing the script computes. A variable assigned more than once is left out:
# which value it holds at a line is not known.

_LOOKUP_CALL = re.compile(
    r"""\bctx\s*\.\s*(find|static)\s*\(\s*(["'`])([^"'`\\$]*)\2"""
    r"""(?:\s*,\s*(["'`])([^"'`\\$]*)\4)?\s*\)""")
_LOOKUP_STEP = re.compile(
    r"""\s*\??\.\s*(?:(?:field|get)\s*\(\s*(["'`])([^"'`\\$]+)\1\s*\)|([A-Za-z_$][\w$]*)(?!\s*\())"""
    r"""|\s*\[\s*(\d+)\s*\]""")
_MAX_LINK_DEPTH = 8


@dataclasses.dataclass(frozen=True)
class Link:
    """A place of the code that Ctrl+click inspects: columns ``start`` to
    ``end`` of its line, the ``expression`` the game evaluates and the
    ``name`` the inspector is titled with."""

    start: int
    end: int
    expression: str
    name: str


def _lookup_allowed(function, engine):
    allowed = _UNITY if function == "static" else _ENGINES
    return engine is None or engine in allowed


def inspect_link(line, column, token, source, engine=None):
    """The Link at ``column`` of ``line`` (``token``: its highlighter token,
    "string" or "text"), or None; ``source`` is the code up to the end of the
    line, where variables are looked up. See the comment above."""
    if token == "string":
        for match in _LOOKUP_CALL.finditer(line):
            function = match.group(1)
            for quote, text in ((2, 3), (4, 5)):
                if match.group(quote) is None:
                    continue
                start, end = match.start(quote), match.end(text) + 1
                if start <= column < end and _lookup_allowed(function, engine):
                    return Link(start, end, match.group(0),
                                match.group(5) or match.group(3))
        return None
    if token != "text":
        return None
    named = identifier_at(line, column)
    if named is None or line[:named[1]].rstrip().endswith("."):
        return None
    name, start = named
    found = _lookup_expression(name, source, 0)
    if found is None or not all(_lookup_allowed(function, engine) for function in found[1]):
        return None
    return Link(start, start + len(name), found[0], name)


def _lookup_expression(name, source, depth):
    """``(expression, functions)`` of the lookup the variable ``name`` holds,
    with the variables it uses put in, and the ctx functions it calls; None
    if it holds anything else (see the comment above)."""
    if depth > _MAX_LINK_DEPTH or name in ("ctx", "this"):
        return None
    escaped = re.escape(name)
    declarations = list(re.finditer(rf"\b(?:const|let|var)\s+{escaped}\s*=\s*([^;\n]+)", source))
    assignments = re.findall(rf"(?<![\w$.]){escaped}\s*(?:[-+*/%&|^]|\*\*|<<|>>>?|\?\?)?=(?!=)",
                             source)
    if len(declarations) != 1 or len(assignments) != 1:
        return None
    return _lookup_chain(declarations[0].group(1).strip(), source, depth)


def _lookup_chain(text, source, depth):
    call = _LOOKUP_CALL.match(text)
    if call is not None:
        expression, functions, rest = call.group(0), [call.group(1)], text[call.end():]
    else:
        head = re.match(r"[A-Za-z_$][\w$]*", text)
        if head is None:
            return None
        found = _lookup_expression(head.group(), source, depth + 1)
        if found is None:
            return None
        expression, functions, rest = f"({found[0]})", list(found[1]), text[head.end():]
    while rest.strip():
        step = _LOOKUP_STEP.match(rest)
        if step is None:
            return None
        expression += step.group(0).strip()
        rest = rest[step.end():]
    return expression, functions
