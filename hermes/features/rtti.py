"""C++ class names from run-time type information, without symbols.

Polymorphic C++ classes carry their name for ``typeid`` and
``dynamic_cast``, also in stripped builds. Through it the module's
virtual function tables (vtables) and the virtual methods in them get names,
``CGameSprite::vftable`` and ``CGameSprite::vf12`` (slot 12).

MSVC (PE): a type descriptor holds the decorated name (``.?AVCGameSprite@@``)
16 bytes (x64) or 8 bytes (x86) after its start. A complete object locator
points to it (x64: as an RVA, signature 1, with its own RVA as a check;
x86: as an address, signature 0), and the slot in front of each vtable points
to the locator.

Itanium (GCC, Clang; ELF): a typeinfo object holds a pointer to the mangled
type name (``11CGameSprite``) after its own vtable pointer. Each vtable starts
with the offset to the top of the object and a pointer to the typeinfo; the
virtual methods follow. In a position-independent module these pointers are
relative relocations (``Image.relocations``).

Only the primary vtable (offset 0) names methods; secondary vtables (multiple
inheritance) name the table only. A method in several vtables keeps the name
of the class with the shortest vtable, its most basic class; one found in
different slots (a shared stub such as ``_purecall``, or identical code
folded together) gets none.
"""

import re
import struct

# Slots a vtable has at most
MAX_SLOTS = 1024
_MSVC_NAME = re.compile(rb"\.\?A[VU][\x21-\x7e]{1,4096}?@@\0")
_ITANIUM_NAME = re.compile(r"[\w$]{2,}")


def rtti_names(image):
    """address -> name of the vtables and virtual methods ``image`` describes."""
    try:
        if image.format == "pe":
            tables = _msvc_vtables(image)
        else:
            tables = _itanium_vtables(image)
    except (struct.error, ValueError, IndexError):
        return {}
    return _name_tables(image, tables)


def _name_tables(image, tables):
    """Names for [(vtable address, class, offset, [method addresses])]."""
    names = {}
    owners = {}  # method -> (slots of the owner's vtable, class, slot)
    shared = set()
    for address, name, offset, methods in tables:
        names[address] = f"{name}::vftable" if offset == 0 else \
            f"{name}::vftable@0x{offset:x}"
        if offset != 0:
            continue
        for slot, method in enumerate(methods):
            known = owners.get(method)
            if known is not None and known[2] != slot:
                shared.add(method)
            if known is None or len(methods) < known[0]:
                owners[method] = (len(methods), name, slot)
    for method, (_count, name, slot) in owners.items():
        if method not in shared and method not in image.names:
            names[method] = f"{name}::vf{slot}"
    return names


def _executable(image, address):
    section = image.section_at(address)
    return section is not None and section.executable


def _data_sections(image):
    return [section for section in image.sections if not section.executable and section.raw_size]


def _words(image, section, width):
    """(address, value) of the aligned words in a section's file data."""
    data = image.section_bytes(section)
    start = (-section.address) % width
    fmt = "<Q" if width == 8 else "<I"
    usable = start + (len(data) - start) // width * width
    for index, (value,) in enumerate(struct.iter_unpack(fmt, data[start:usable])):
        yield section.address + start + index * width, value


def _methods(image, pointer_at, first):
    methods = []
    width = 8 if image.bits == 64 else 4
    for slot in range(MAX_SLOTS):
        target = pointer_at(first + slot * width)
        if target is None or not _executable(image, target):
            break
        methods.append(target)
    return methods


# --- MSVC -----------------------------------------------------------------------

def msvc_class_name(decorated):
    """``.?AVInner@Outer@@`` -> ``Outer::Inner``; templates as ``vector<…>``."""
    body = decorated[4:]
    if body.endswith("@@"):
        body = body[:-2]
    if body.startswith("?$"):
        return body[2:].split("@", 1)[0] + "<…>"
    parts = []
    for part in body.split("@"):
        if not part:
            break
        parts.append("`anonymous namespace'" if part.startswith("?A") else part)
    return "::".join(reversed(parts)) or decorated


def _msvc_vtables(image):
    wide = image.bits == 64
    width = 8 if wide else 4
    sections = _data_sections(image)
    descriptors = {}  # type descriptor (RVA on x64, address on x86) -> class
    for section in sections:
        data = image.section_bytes(section)
        for match in _MSVC_NAME.finditer(data):
            descriptor = section.address + match.start() - 2 * width
            decorated = match.group(0)[:-1].decode("ascii")
            descriptors[descriptor - image.base if wide else descriptor] = \
                msvc_class_name(decorated)
    if not descriptors:
        return []
    locators = {}  # locator address -> (class, offset)
    for section in sections:
        for address, value in _words(image, section, 4):
            if value not in descriptors:
                continue
            locator = address - 12
            header = image.read(locator, 24 if wide else 16)
            if len(header) < (24 if wide else 16):
                continue
            signature, offset = struct.unpack_from("<Ii", header, 0)
            if wide:
                if signature != 1 or struct.unpack_from("<I", header, 20)[0] != \
                        locator - image.base:
                    continue
            elif signature != 0:
                continue
            locators[locator] = (descriptors[value], offset)
    if not locators:
        return []

    def pointer_at(address):
        raw = image.read(address, width)
        return int.from_bytes(raw, "little") if len(raw) == width else None

    tables = []
    for section in sections:
        for address, value in _words(image, section, width):
            found = locators.get(value)
            if found is None:
                continue
            first = address + width
            methods = _methods(image, pointer_at, first)
            if methods:
                tables.append((first, found[0], found[1], methods))
    return tables


# --- Itanium ----------------------------------------------------------------------

def itanium_class_name(mangled):
    """``11CGameSprite`` -> ``CGameSprite``, ``N5Outer5InnerE`` -> ``Outer::Inner``;
    template arguments as ``<…>``. None for anything else."""
    position = 0
    nested = mangled.startswith("N")
    if nested:
        position = 1
    parts = []
    while position < len(mangled):
        character = mangled[position]
        if character.isdigit():
            digits = re.match(r"\d+", mangled[position:]).group(0)
            position += len(digits)
            length = int(digits)
            if length == 0 or position + length > len(mangled):
                return None
            parts.append(mangled[position:position + length])
            position += length
        elif mangled.startswith("St", position):
            parts.append("std")
            position += 2
        elif character == "I" and parts:
            end = _template_end(mangled, position + 1)
            if end is None:
                return None
            parts[-1] += "<…>"
            position = end
        elif character == "E" and nested and position == len(mangled) - 1:
            position += 1
            nested = False
        else:
            return None
        if not nested and parts != ["std"] and position < len(mangled) \
                and mangled[position] != "I":
            return None
    if nested or not parts:
        return None
    return "::".join(parts)


def _template_end(mangled, position):
    """The position after the ``E`` closing template arguments starting at ``position``."""
    depth = 1
    while position < len(mangled):
        character = mangled[position]
        if character.isdigit():
            digits = re.match(r"\d+", mangled[position:]).group(0)
            position += len(digits) + int(digits)
            continue
        if character == "L":  # a literal: L <type> <number> E
            end = mangled.find("E", position)
            if end < 0:
                return None
            position = end + 1
            continue
        if character in "IN":
            depth += 1
        elif character == "E":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    return None


def _itanium_vtables(image):
    width = 8 if image.bits == 64 else 4
    relocated = dict(image.relocations)
    absolute = image.base >= 0x10000 or not relocated

    def pointer_at(address):
        if address in relocated:
            return relocated[address]
        if not absolute:
            return None
        raw = image.read(address, width)
        return int.from_bytes(raw, "little") if len(raw) == width else None

    sections = _data_sections(image)
    slots = list(relocated.items())
    if absolute:
        for section in sections:
            slots.extend(_words(image, section, width))
    type_names = {}  # typeinfo address -> class
    for where, target in slots:
        if target == 0 or not image.contains(target) or _executable(image, target):
            continue
        text = _c_string(image, target)
        if text is None or not _ITANIUM_NAME.fullmatch(text):
            continue
        name = itanium_class_name(text)
        if name is not None:
            type_names[where - width] = name
    if not type_names:
        return []
    tables = []
    for where, target in slots:
        name = type_names.get(target)
        if name is None:
            continue
        raw = image.read(where - width, width)
        if len(raw) != width:
            continue
        offset = -int.from_bytes(raw, "little", signed=True)
        if offset < 0 or offset > 0x100000:
            continue
        first = where + width
        methods = _methods(image, pointer_at, first)
        if methods:
            tables.append((first, name, offset, methods))
    return tables


def _c_string(image, address, limit=512):
    raw = image.read(address, limit)
    end = raw.find(b"\0")
    if end <= 0:
        return None
    try:
        return raw[:end].decode("ascii")
    except UnicodeDecodeError:
        return None
