"""Code explorer: strings, cross references and functions of a native module.

It reads the module's file (a PE image, also under Wine, or an ELF file), not
the game's memory, so it cannot disturb the game. The way into unknown code
it supports is: a string the game shows or logs -> the instructions that
load it -> the function around them -> who calls that function. A byte
pattern for an instruction or function (``CodeIndex.pattern``) finds it
again after an update in a native script (``ctx.native.scan``, see
hermes/features/script_native.js).

``load_image(path)``
    The module's sections, functions and names (``Image``). PE: the
    exception directory's functions (x64), chained parts belonging to their
    function, the exports, and the imports by their slot in the import
    address table; matching local PDB names through DbgHelp on Windows.
    ELF: the functions and objects of the symbol tables, and
    the imports by their GOT slot. x86 and x86-64 only.
``CodeIndex(image)``
    Disassembles the code sections once (Capstone, linear sweep), notes
    every address an instruction refers to (RIP-relative operands, branch
    targets, absolute addresses) and the pointers in the data; also the
    module's strings (ASCII and UTF-16) and functions only known as call
    targets.

Addresses are virtual addresses as the file states them (a PE's preferred
image base, 0x140000000 for most 64-bit games; an ELF's link addresses, 0
for a position-independent one). In the process a module may be loaded
elsewhere: ``runtime = address - image.base + module_base``.
"""

import bisect
import os
import re
import stat
import struct
from dataclasses import dataclass, field

from capstone import CS_ARCH_X86, CS_MODE_32, CS_MODE_64, Cs  # pylint: disable=import-error

from hermes.core import messages
from hermes.platform import is_wine_process, main_module, read_mappings
from hermes.platform.symbols import load_pe_symbols

# Kinds of references
CALL, JUMP, DATA, POINTER = "call", "jump", "data", "pointer"
_KINDS = (CALL, JUMP, DATA, POINTER)
# Characters a string has at least
MIN_STRING = 5
# Instructions the function view shows at most
MAX_LINES = 6000
# A function only known as a call target ends at the next one, at most this far
MAX_UNBOUNDED_FUNCTION = 0x4000
# Bytes a pattern has at most, and bytes that are no wildcard at least
MAX_PATTERN = 96
MIN_PATTERN_FIXED = 8
# Characters of a string a comment shows
COMMENT_STRING = 80
# Below this image base numbers in instructions are taken for constants, not
# addresses (a position-independent ELF is linked at 0)
_ABSOLUTE_FLOOR = 0x10000

_HEX = re.compile(r"0x[0-9a-f]+")
_RIP = re.compile(r"rip ([+-]) (0x[0-9a-f]+|\d+)")
_ASCII = re.compile(rb"[\x20-\x7e\t]{%d,}" % MIN_STRING)
_UTF16 = re.compile(rb"(?:[\x20-\x7e\t]\x00){%d,}" % MIN_STRING)
_BRANCHES = frozenset({"call", "jmp", "jecxz", "jrcxz", "loop", "loope", "loopne"})
# Sections whose contents are no pointers the game reads (PE, ELF)
_NO_POINTERS = (".pdata", ".reloc", ".rsrc", ".rela", ".rel.", ".dynsym", ".dynstr", ".symtab",
                ".strtab", ".eh_frame", ".gnu", ".note", ".dynamic", ".hash", ".debug")


@dataclass(frozen=True)
class Section:
    """A section of the module as loaded: where it lies and what it holds."""

    name: str
    address: int
    size: int
    offset: int
    raw_size: int
    executable: bool
    writable: bool

    @property
    def end(self):
        return self.address + self.size


@dataclass(frozen=True)
class Function:
    """Code from ``start`` to ``end``; ``parent``: the function a chained part
    (PE) belongs to, else None; ``bounded``: its end is known (not just the
    next function's start)."""

    start: int
    end: int
    parent: int | None = None
    bounded: bool = True


@dataclass(frozen=True)
class Reference:
    """An instruction (or a pointer in the data) at ``address`` referring to an address."""

    address: int
    kind: str


@dataclass(frozen=True)
class Line:
    """An instruction of the function view."""

    address: int
    raw: bytes
    text: str
    # The address it refers to, or None; and what lies there
    target: int | None = None
    comment: str = ""


@dataclass(frozen=True)
class Pattern:
    """A byte pattern starting at ``address``: hex bytes and ?? (ctx.native.scan)."""

    address: int
    text: str
    length: int
    # How often it is found in the module: 1 is what a script needs
    matches: int


@dataclass(frozen=True)
class Parameter:
    """An argument of a function, as guessed from its code (see CodeIndex.signature)."""

    # Where it arrives: rdi, rcx, xmm0, …
    register: str
    # Frida type: uint8 (also bool), int16, int, int64, pointer, float, double
    type: str
    # Read by the function; an argument it ignores is implied by a later one
    used: bool = True


@dataclass(frozen=True)
class Signature:
    """A function's arguments and return type, as guessed from the machine code."""

    convention: str  # "sysv" (Linux x86-64) or "win64" (Windows, Wine)
    parameters: tuple
    returns: str  # a Frida type, or "void"
    # Decided by what callers do with the result (else from the function's own code)
    returns_known: bool = True
    # The last argument register is used: more arguments may follow on the stack
    more_possible: bool = False

    @property
    def argument_types(self):
        """The argument types for ctx.native.fn (NativeFunction)."""
        return [parameter.type for parameter in self.parameters]

    def text(self, name=""):
        """``void add_gold(int rdi)``; arguments the function ignores are marked."""
        arguments = ", ".join(f"{parameter.type} {parameter.register}"
                              + ("" if parameter.used else " (unused)")
                              for parameter in self.parameters)
        if self.more_possible:
            arguments += ", …" if arguments else "…"
        returns = self.returns if self.returns_known else f"{self.returns}?"
        return f"{returns} {name}({arguments})" if name else f"{returns} ({arguments})"


@dataclass
class Image:
    """A module's file: its sections, functions and names."""

    path: str
    format: str  # "pe" or "elf"
    bits: int
    base: int
    data: bytes
    sections: list = field(default_factory=list)
    functions: list = field(default_factory=list)  # Functions, sorted by start
    names: dict = field(default_factory=dict)  # address -> export or symbol
    # (where, target) of pointers the file keeps as relocations only (ELF)
    relocations: list = field(default_factory=list)
    # The slots imported functions are called through: address -> name
    imports: dict = field(default_factory=dict)

    @property
    def end(self):
        return max((section.end for section in self.sections), default=self.base)

    def contains(self, address):
        return self.base <= address < self.end

    def section_at(self, address):
        for section in self.sections:
            if section.address <= address < section.end:
                return section
        return None

    def read(self, address, size):
        """The file's bytes at ``address`` (fewer at the end of a section's data)."""
        section = self.section_at(address)
        if section is None:
            return b""
        start = address - section.address
        available = max(0, min(size, section.raw_size - start))
        return self.data[section.offset + start:section.offset + start + available]

    def section_bytes(self, section):
        return self.data[section.offset:section.offset + min(section.size, section.raw_size)]


# --- Reading the file -----------------------------------------------------------

def load_image(path, symbol_path=None):
    """Read a PE or ELF file, optionally searching a local PDB folder on Windows.

    Raises LocalizedValueError for other formats.
    """
    with open(path, "rb") as file:
        data = file.read()
    if data[:2] == b"MZ":
        return _load_pe(path, data, symbol_path)
    if data[:4] == b"\x7fELF":
        return _load_elf(path, data)
    raise messages.LocalizedValueError("error.code.format", path=path)


def _unpack(fmt, data, offset):
    try:
        return struct.unpack_from(fmt, data, offset)
    except struct.error:
        raise messages.LocalizedValueError("error.code.truncated") from None


def _load_pe(path, data, symbol_path=None):
    # pylint: disable=too-many-locals
    header = _unpack("<I", data, 0x3c)[0]
    if data[header:header + 4] != b"PE\0\0":
        raise messages.LocalizedValueError("error.code.format", path=path)
    machine, count, _time, _symbols, _symbol_count, optional_size, _flags = _unpack(
        "<HHIIIHH", data, header + 4)
    if machine not in (0x8664, 0x14c):
        raise messages.LocalizedValueError("error.code.machine", machine=f"0x{machine:x}")
    optional = header + 24
    magic = _unpack("<H", data, optional)[0]
    wide = magic == 0x20b
    base = _unpack("<Q" if wide else "<I", data, optional + (24 if wide else 28))[0]
    directories = optional + (112 if wide else 96)
    directory_count = _unpack("<I", data, directories - 4)[0]

    def directory(index):
        if index >= directory_count:
            return 0, 0
        return _unpack("<II", data, directories + 8 * index)

    image = Image(path, "pe", 64 if wide else 32, base, data)
    table = optional + optional_size
    for index in range(count):
        entry = table + 40 * index
        raw_name = data[entry:entry + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        virtual_size, rva, raw_size, raw_offset = _unpack("<IIII", data, entry + 8)
        characteristics = _unpack("<I", data, entry + 36)[0]
        image.sections.append(Section(
            raw_name, base + rva, virtual_size or raw_size, raw_offset, raw_size,
            bool(characteristics & 0x20000000 or characteristics & 0x20),
            bool(characteristics & 0x80000000)))
    _pe_exports(image, directory(0))
    _pe_imports(image, directory(1))
    if wide:
        _pe_functions(image, directory(3))
    _pe_symbols(image, symbol_path)
    return image


def _pe_symbols(image, symbol_path=None):
    names = load_pe_symbols(image.path, image.base, image.end - image.base, symbol_path)
    for address, name in names.items():
        image.names.setdefault(address, name)


def _pe_offset(image, rva):
    section = image.section_at(image.base + rva)
    if section is None:
        return None
    return section.offset + (image.base + rva - section.address)


def _pe_exports(image, entry):
    rva, size = entry
    start = _pe_offset(image, rva) if rva else None
    if start is None:
        return
    data = image.data
    try:
        _flags, _time, _major, _minor, _name, _first, function_count, name_count, functions, \
            names, ordinals = struct.unpack_from("<IIHHIIIIIII", data, start)
        for index in range(name_count):
            name_offset = _pe_offset(image, struct.unpack_from("<I", data,
                                                               _pe_offset(image, names) + 4 * index)[0])
            ordinal = struct.unpack_from("<H", data, _pe_offset(image, ordinals) + 2 * index)[0]
            if ordinal >= function_count or name_offset is None:
                continue
            target = struct.unpack_from("<I", data, _pe_offset(image, functions) + 4 * ordinal)[0]
            if rva <= target < rva + size:
                continue  # forwarded to another module
            name = data[name_offset:data.index(b"\0", name_offset)].decode("ascii", "replace")
            image.names[image.base + target] = name
    except (struct.error, TypeError, ValueError):
        return  # a damaged export directory: no names


def _pe_imports(image, entry):
    """The import directory: the name each import address table slot gets."""
    rva, _size = entry
    start = _pe_offset(image, rva) if rva else None
    if start is None:
        return
    data = image.data
    wide = image.bits == 64
    width = 8 if wide else 4
    ordinal_flag = 1 << (63 if wide else 31)
    try:
        for descriptor in range(start, len(data) - 19, 20):
            lookup, _time, _chain, name_rva, first_thunk = struct.unpack_from("<IIIII", data,
                                                                            descriptor)
            if first_thunk == 0:
                break
            name_offset = _pe_offset(image, name_rva)
            module = (data[name_offset:data.index(b"\0", name_offset)].decode("ascii", "replace")
                      if name_offset is not None else "?")
            thunks = _pe_offset(image, lookup or first_thunk)
            if thunks is None:
                continue
            for index in range(0x10000):
                value = int.from_bytes(data[thunks + index * width:thunks + (index + 1) * width],
                                       "little")
                if value == 0:
                    break
                if value & ordinal_flag:
                    name = f"#{value & 0xffff}"
                else:
                    hint = _pe_offset(image, value & 0x7fffffff)
                    if hint is None:
                        continue
                    name = data[hint + 2:data.index(b"\0", hint + 2)].decode("ascii", "replace")
                image.imports[image.base + first_thunk + index * width] = f"{module}!{name}"
    except (struct.error, ValueError):
        return  # a damaged import directory: the names found so far


def _pe_functions(image, entry):
    """The exception directory: RUNTIME_FUNCTIONs (begin, end, unwind info)."""
    rva, size = entry
    start = _pe_offset(image, rva) if rva else None
    if start is None:
        return
    data = image.data
    entries = []
    for offset in range(start, start + size - 11, 12):
        begin, end, unwind = struct.unpack_from("<III", data, offset)
        if begin == 0 and end == 0:
            break
        entries.append((begin, end, unwind))

    def parent_of(unwind, depth=0):
        # A chained part's unwind info ends with the RUNTIME_FUNCTION it continues
        offset = _pe_offset(image, unwind & ~1)
        if offset is None or depth > 32 or offset + 4 > len(data):
            return None
        flags = data[offset] >> 3
        if not flags & 0x4:
            return None
        codes = data[offset + 2]
        chained = offset + 4 + ((codes + 1) & ~1) * 2
        try:
            begin, _end, next_unwind = struct.unpack_from("<III", data, chained)
        except struct.error:
            return None
        return parent_of(next_unwind, depth + 1) or begin

    functions = []
    for begin, end, unwind in entries:
        parent = parent_of(unwind)
        functions.append(Function(image.base + begin, image.base + end,
                                  image.base + parent if parent else None))
    functions.sort(key=lambda function: function.start)
    image.functions = functions


def _load_elf(path, data):
    # pylint: disable=too-many-locals
    wide = data[4] == 2
    if data[5] != 1:
        raise messages.LocalizedValueError("error.code.machine", machine="big endian")
    machine = _unpack("<H", data, 18)[0]
    if machine not in (62, 3):
        raise messages.LocalizedValueError("error.code.machine", machine=str(machine))
    if wide:
        shoff, = _unpack("<Q", data, 40)
        shentsize, shnum, shstrndx = _unpack("<HHH", data, 58)
        section_format = "<IIQQQQIIQQ"
    else:
        shoff, = _unpack("<I", data, 32)
        shentsize, shnum, shstrndx = _unpack("<HHH", data, 46)
        section_format = "<IIIIIIIIII"
    headers = [_unpack(section_format, data, shoff + shentsize * index) for index in range(shnum)]
    if not headers:
        raise messages.LocalizedValueError("error.code.no_sections", path=path)
    names_offset = headers[shstrndx][4] if shstrndx < len(headers) else 0

    def name_of(offset):
        start = names_offset + offset
        end = data.find(b"\0", start)
        return data[start:end].decode("ascii", "replace") if end >= 0 else ""

    image = Image(path, "elf", 64 if wide else 32, 0, data)
    loaded = []
    for header in headers:
        name, kind, flags, address, offset, size = header[:6]
        if flags & 0x2 and size:  # SHF_ALLOC
            loaded.append(Section(name_of(name), address, size, offset,
                                  0 if kind == 8 else size,  # SHT_NOBITS: .bss
                                  bool(flags & 0x4), bool(flags & 0x1)))
    image.sections = loaded
    image.base = min((section.address for section in loaded), default=0) & ~0xfff
    _elf_symbols(image, headers, wide)
    image.relocations = _elf_relocations(image, headers, wide)
    _elf_imports(image, headers, wide)
    return image


def _elf_symbols(image, headers, wide):
    data = image.data
    functions = {}
    for header in headers:
        kind, link, offset, size = header[1], header[6], header[4], header[5]
        if kind not in (2, 11):  # SHT_SYMTAB, SHT_DYNSYM
            continue
        strings = headers[link][4] if link < len(headers) else 0
        entry_size = 24 if wide else 16
        for start in range(offset, offset + size - entry_size + 1, entry_size):
            if wide:
                name, info, _other, shndx, value, length = struct.unpack_from("<IBBHQQ", data, start)
            else:
                name, value, length, info, _other, shndx = struct.unpack_from("<IIIBBH", data, start)
            symbol_type = info & 0xf
            if shndx == 0 or value == 0 or symbol_type not in (1, 2):  # OBJECT, FUNC
                continue
            end = data.find(b"\0", strings + name)
            text = data[strings + name:end].decode("ascii", "replace") if name else ""
            if text and value not in image.names:
                image.names[value] = text
            if symbol_type == 2 and length:
                functions[value] = Function(value, value + length)
    image.functions = sorted(functions.values(), key=lambda function: function.start)


def _elf_imports(image, headers, wide):
    """Name the GOT slots the PLT jumps through (JUMP_SLOT, GLOB_DAT)."""
    if not wide:
        return
    data = image.data
    for header in headers:
        kind, offset, size, link = header[1], header[4], header[5], header[6]
        if kind != 4 or link >= len(headers):  # SHT_RELA with its symbol table
            continue
        symbols = headers[link]
        if symbols[1] != 11:  # SHT_DYNSYM
            continue
        strings = headers[symbols[6]][4] if symbols[6] < len(headers) else 0
        for start in range(offset, offset + size - 23, 24):
            where, info, _addend = struct.unpack_from("<QQq", data, start)
            if info & 0xffffffff not in (6, 7):  # R_X86_64_GLOB_DAT, R_X86_64_JUMP_SLOT
                continue
            entry = symbols[4] + 24 * (info >> 32)
            try:
                name = struct.unpack_from("<I", data, entry)[0]
            except struct.error:
                continue
            end = data.find(b"\0", strings + name)
            if name and end > 0:
                image.imports[where] = data[strings + name:end].decode("ascii", "replace")


def _elf_relocations(image, headers, wide):
    """(where, target) of the relative relocations: the pointers of a
    position-independent module, which its file holds as relocations only."""
    if not wide:
        return []  # 32-bit x86 keeps the addend in the data (REL), found there
    data = image.data
    pointers = []
    for header in headers:
        kind, offset, size = header[1], header[4], header[5]
        if kind != 4:  # SHT_RELA
            continue
        for start in range(offset, offset + size - 23, 24):
            where, info, addend = struct.unpack_from("<QQq", data, start)
            if info & 0xffffffff == 8:  # R_X86_64_RELATIVE
                pointers.append((where, addend))
    return pointers


# --- The index --------------------------------------------------------------------

def _operand_target(address, size, mnemonic, operands, absolute_ok):
    """(address, kind) an instruction refers to, or None."""
    if "0x" not in operands and "rip" not in operands:
        return None
    rip = _RIP.search(operands)
    if rip is not None:
        displacement = int(rip.group(2), 0)
        target = address + size + (displacement if rip.group(1) == "+" else -displacement)
        return target, DATA
    if mnemonic in _BRANCHES or (mnemonic.startswith("j") and mnemonic != "jmp"):
        if operands.startswith("0x"):
            return int(operands, 16), CALL if mnemonic == "call" else JUMP
        return None
    if not absolute_ok:
        return None
    for match in _HEX.finditer(operands):
        return int(match.group(0), 16), DATA
    return None


# --- Calling conventions (x86-64) ---------------------------------------------------

def _family(names):
    """{register name: (family, width in bytes)} for the names of one register."""
    family = names[0]
    widths = (8, 4, 2, 1, 1)
    return {name: (family, widths[index]) for index, name in enumerate(names) if name}


_GENERAL = {}
for _names in (("rdi", "edi", "di", "dil"), ("rsi", "esi", "si", "sil"),
               ("rdx", "edx", "dx", "dl", "dh"), ("rcx", "ecx", "cx", "cl", "ch"),
               ("r8", "r8d", "r8w", "r8b"), ("r9", "r9d", "r9w", "r9b"),
               ("rax", "eax", "ax", "al", "ah")):
    _GENERAL.update(_family(_names))
_VECTOR = {f"{kind}{number}": (f"xmm{number}", 16)
           for number in range(8) for kind in ("xmm", "ymm", "zmm")}
_CONVENTIONS = {
    "sysv": (("rdi", "rsi", "rdx", "rcx", "r8", "r9"), tuple(f"xmm{n}" for n in range(8))),
    "win64": (("rcx", "rdx", "r8", "r9"), tuple(f"xmm{n}" for n in range(4))),
}
# Registers a call may change (so after a call they hold no argument any more)
_VOLATILE = {"rdi", "rsi", "rdx", "rcx", "r8", "r9", "rax"} | {f"xmm{n}" for n in range(8)}
# Instructions that zero a register they name twice, without reading it
_ZEROING = {"xor", "sub", "pxor", "xorps", "xorpd", "vxorps", "vxorpd", "vpxor"}
# Instructions after a call that end the look at what the caller does with the result
_FLOW = {"call", "ret", "jmp"}
# Instructions a caller looks at a result with at most
_RESULT_WINDOW = 8
# Callers looked at
_CALLERS_CHECKED = 16
# Instructions of a called helper looked at to see which registers it keeps
_HELPER_SIZE = 48
# What a script skeleton passes for an argument of each type
_EXAMPLE_ARGUMENTS = {"uint8": "1", "int16": "1", "int": "1", "pointer": "object",
                      "float": "1.5", "double": "1.5"}


def _register(instruction, register_id):
    name = instruction.reg_name(register_id)
    return _GENERAL.get(name) or _VECTOR.get(name)


def _vector_type(mnemonic):
    """float or double, from an SSE instruction's name: a conversion reads
    the type before the 2 (cvtsd2ss reads a double), others the one they end in."""
    source = mnemonic.split("2", 1)[0] if "2" in mnemonic else mnemonic
    if source.endswith(("ss", "ps")):
        return "float"
    return "double"


def _integer_type(width):
    """The Frida type of an integer argument read ``width`` bytes wide: all
    64 bits are a pointer in game code far more often than an int64 (and a
    script passes pointers as NativePointers)."""
    return {1: "uint8", 2: "int16", 4: "int"}.get(width, "pointer")


class CodeIndex:
    """Strings, references and functions of an Image (see the module's docstring).

    Building takes seconds for a large game; ``progress(fraction)`` is called
    now and then and may return True to cancel (raises InterruptedError).
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, image, progress=None):
        self.image = image
        self.mode = CS_MODE_64 if image.bits == 64 else CS_MODE_32
        # Numbers in instructions may be addresses (not in a position-independent ELF)
        self.absolute_addresses = image.base >= _ABSOLUTE_FLOOR
        self._refs = {}
        self._call_targets = set()
        self._clobbers = {}  # call target -> register families it may change
        self._signatures = {}  # function start -> Signature or None
        self.strings = []  # (address, text, "ascii" or "utf16"), by address
        self._string_at = {}
        self._report = progress or (lambda _fraction: False)
        self._index_code()
        self._name_stubs()
        self._index_pointers()
        self._index_strings()
        self._complete_functions()
        self._starts = [function.start for function in self.functions]

    # --- building ---------------------------------------------------------------
    def _check(self, fraction):
        if self._report(fraction):
            raise InterruptedError

    def _add(self, target, source, kind):
        refs = self._refs.get(target)
        if refs is None:
            self._refs[target] = refs = []
        refs.append((source << 2) | _KINDS.index(kind))

    def _index_code(self):
        sections = [section for section in self.image.sections
                    if section.executable and section.raw_size]
        total = sum(section.raw_size for section in sections) or 1
        done = 0
        disassembler = Cs(CS_ARCH_X86, self.mode)
        disassembler.skipdata = True
        image = self.image
        for section in sections:
            code = image.section_bytes(section)
            next_check = 0
            for address, size, mnemonic, operands in disassembler.disasm_lite(code, section.address):
                if address >= next_check:
                    self._check(0.8 * (done + address - section.address) / total)
                    next_check = address + 0x40000
                found = _operand_target(address, size, mnemonic, operands, self.absolute_addresses)
                if found is None:
                    continue
                target, kind = found
                if not image.contains(target):
                    continue
                self._add(target, address, kind)
                if kind == CALL:
                    self._call_targets.add(target)
            done += section.raw_size

    def _name_stubs(self):
        """Give the stubs calls go through to imports the import's name: an
        ELF PLT entry or a PE thunk, a jump through an import slot."""
        image = self.image
        if not image.imports:
            return
        disassembler = Cs(CS_ARCH_X86, self.mode)
        for target in self._call_targets:
            if target in image.names:
                continue
            code = image.read(target, 16)
            for address, size, mnemonic, operands in disassembler.disasm_lite(code, target, 3):
                if mnemonic in ("endbr64", "endbr32", "nop"):
                    continue
                if mnemonic in ("jmp", "bnd jmp"):
                    found = _operand_target(address, size, "mov", operands, self.absolute_addresses)
                    if found is not None and found[0] in image.imports:
                        image.names[target] = image.imports[found[0]]
                break

    def _index_pointers(self):
        image = self.image
        self._check(0.8)
        width = 8 if image.bits == 64 else 4
        fmt = "<Q" if width == 8 else "<I"
        if self.absolute_addresses:
            for section in image.sections:
                if section.executable or not section.raw_size or \
                        section.name.startswith(_NO_POINTERS):
                    continue
                content = image.section_bytes(section)
                content = content[:len(content) - len(content) % width]
                low, high = image.base, image.end
                for index, (value,) in enumerate(struct.iter_unpack(fmt, content)):
                    if low <= value < high:
                        self._add(value, section.address + index * width, POINTER)
        for where, target in image.relocations:
            if image.contains(target):
                self._add(target, where, POINTER)

    def _index_strings(self):
        image = self.image
        self._check(0.9)
        found = []
        for section in image.sections:
            if section.executable or not section.raw_size or section.name in (".reloc", ".pdata"):
                continue
            content = image.section_bytes(section)
            for match in _ASCII.finditer(content):
                found.append((section.address + match.start(),
                              match.group(0).decode("ascii"), "ascii"))
            for match in _UTF16.finditer(content):
                found.append((section.address + match.start(),
                              match.group(0).decode("utf-16-le"), "utf16"))
        found.sort()
        found = self._split_at_references(found)
        self.strings = found
        self._string_at = {address: text for address, text, _kind in found}

    def _split_at_references(self, strings):
        """Split strings where an instruction or pointer refers into them: a
        constant before a string may end in a printable byte (1.5f ends in
        0x3f, "?"), which the scan joins to the string the code refers to."""
        targets = sorted(self._refs)
        result = []
        for address, text, kind in strings:
            width = 2 if kind == "utf16" else 1
            end = address + len(text) * width
            position = bisect.bisect_right(targets, address)
            cuts = []
            while position < len(targets) and targets[position] < end:
                offset = targets[position] - address
                if offset % width == 0:
                    cuts.append(offset // width)
                position += 1
            if not cuts:
                result.append((address, text, kind))
                continue
            starts = [0] + cuts
            for index, start in enumerate(starts):
                stop = starts[index + 1] if index + 1 < len(starts) else len(text)
                piece = text[start:stop]
                # A short leftover counts only where something refers to it
                if len(piece) >= MIN_STRING or index > 0:
                    result.append((address + start * width, piece, kind))
        return result

    def _complete_functions(self):
        """Add call targets no table names as functions, ending at the next start."""
        image = self.image
        known = image.functions
        starts = {function.start for function in known}
        for function in known:
            if function.parent is not None:
                starts.add(function.parent)
        extra = sorted(target for target in self._call_targets if target not in starts)
        boundaries = sorted(starts | set(extra))
        added = []
        for target in extra:
            section = image.section_at(target)
            if section is None or not section.executable:
                continue
            position = bisect.bisect_right(boundaries, target)
            limit = boundaries[position] if position < len(boundaries) else section.end
            added.append(Function(target, min(limit, section.end,
                                              target + MAX_UNBOUNDED_FUNCTION), bounded=False))
        self.functions = sorted(known + added, key=lambda function: function.start)

    # --- questions -----------------------------------------------------------------
    @property
    def reference_count(self):
        return sum(len(refs) for refs in self._refs.values())

    def references(self, target):
        """The References to ``target``, by address."""
        return sorted((Reference(code >> 2, _KINDS[code & 3])
                       for code in self._refs.get(target, ())), key=lambda ref: ref.address)

    def function_at(self, address):
        """The Function containing ``address`` (a chained part: the part), or None."""
        position = bisect.bisect_right(self._starts, address) - 1
        while position >= 0:
            function = self.functions[position]
            if function.start <= address < function.end:
                return function
            if function.bounded or address - function.start > MAX_UNBOUNDED_FUNCTION:
                break
            position -= 1
        return None

    def function_starting(self, address):
        position = bisect.bisect_left(self._starts, address)
        if position < len(self._starts) and self._starts[position] == address:
            return self.functions[position]
        return None

    def owner(self, function):
        """The function a chained part belongs to, else the function itself."""
        if function.parent is None:
            return function
        return self.function_starting(function.parent) or function

    def name(self, address):
        """The export or symbol at ``address``, else sub_<address>."""
        return self.image.names.get(address) or f"sub_{address:x}"

    def where(self, address):
        """``name`` or ``name+0x…`` of the function around ``address``, or ""."""
        function = self.function_at(address)
        if function is None:
            return ""
        owner = self.owner(function)
        offset = address - owner.start
        name = self.name(owner.start)
        return name if offset == 0 else f"{name}+0x{offset:x}"

    def string_at(self, address):
        return self._string_at.get(address)

    def describe(self, address):
        """What lies at ``address``, for a comment: a string, a function, a
        pointer to one of them, or ""."""
        text = self.string_at(address)
        if text is not None:
            return _quote(text)
        if address in self.image.names or self.function_starting(address) is not None:
            return self.name(address)
        section = self.image.section_at(address)
        if section is not None and section.executable:
            return self.where(address)
        if address in self.image.imports:
            return self.image.imports[address]
        width = 8 if self.image.bits == 64 else 4
        raw = self.image.read(address, width)
        if len(raw) == width:
            value = int.from_bytes(raw, "little")
            if value != address and self.image.contains(value):
                text = self.string_at(value)
                if text is not None:
                    return f"→ {_quote(text)}"
                if value in self.image.names or self.function_starting(value) is not None:
                    return f"→ {self.name(value)}"
        return self.image.names.get(address, "")

    def disassemble(self, function):
        """The Lines of ``function`` (at most MAX_LINES)."""
        image = self.image
        code = image.read(function.start, function.end - function.start)
        disassembler = Cs(CS_ARCH_X86, self.mode)
        disassembler.skipdata = True
        lines = []
        for address, size, mnemonic, operands in disassembler.disasm_lite(code, function.start):
            raw = code[address - function.start:address - function.start + size]
            found = _operand_target(address, size, mnemonic, operands, self.absolute_addresses)
            target = found[0] if found and image.contains(found[0]) else None
            lines.append(Line(address, raw, f"{mnemonic} {operands}".strip(), target,
                              self.describe(target) if target is not None else ""))
            if len(lines) >= MAX_LINES:
                break
        return lines

    # --- patterns ----------------------------------------------------------------
    def pattern(self, address):
        """The shortest unique Pattern starting at ``address`` (instruction by
        instruction, MAX_PATTERN bytes at most): addresses, branch targets and
        RIP-relative displacements, which an update moves, are wildcards."""
        image = self.image
        disassembler = Cs(CS_ARCH_X86, self.mode)
        disassembler.detail = True
        tokens = []
        matches = 0
        code = image.read(address, MAX_PATTERN + 16)
        for instruction in disassembler.disasm(code, address):
            tokens.extend(self._instruction_tokens(instruction))
            fixed = sum(token != "??" for token in tokens)
            if fixed >= MIN_PATTERN_FIXED:
                matches = self.count_matches(tokens, limit=2)
                if matches == 1:
                    break
            if len(tokens) >= MAX_PATTERN:
                break
        if not tokens:
            raise messages.LocalizedValueError("error.code.no_code", address=f"0x{address:x}")
        if matches == 0 or matches > 1:
            matches = self.count_matches(tokens, limit=100)
        return Pattern(address, " ".join(tokens), len(tokens), matches)

    def _instruction_tokens(self, instruction):
        raw = bytes(instruction.bytes)
        wild = set()
        relative = instruction.mnemonic in _BRANCHES or instruction.mnemonic.startswith("j")
        if instruction.imm_size and instruction.imm_offset:
            immediate = int.from_bytes(raw[instruction.imm_offset:instruction.imm_offset
                                           + instruction.imm_size], "little")
            if (relative and instruction.imm_size >= 4) or \
                    (self.absolute_addresses and instruction.imm_size >= 4
                     and self.image.contains(immediate)):
                wild.update(range(instruction.imm_offset,
                                  instruction.imm_offset + instruction.imm_size))
        if instruction.disp_size == 4 and instruction.disp_offset:
            displacement = int.from_bytes(raw[instruction.disp_offset:instruction.disp_offset + 4],
                                          "little")
            if "rip" in instruction.op_str or (self.absolute_addresses
                                               and self.image.contains(displacement)):
                wild.update(range(instruction.disp_offset, instruction.disp_offset + 4))
        return ["??" if index in wild else f"{byte:02X}" for index, byte in enumerate(raw)]

    def count_matches(self, tokens, limit=2):
        """How often the pattern is found in the module's sections (up to ``limit``)."""
        expression = re.compile(b"".join(b"." if token == "??" else re.escape(bytes([int(token, 16)]))
                                         for token in tokens), re.DOTALL)
        count = 0
        for section in self.image.sections:
            if not section.raw_size:
                continue
            for _match in expression.finditer(self.image.section_bytes(section)):
                count += 1
                if count >= limit:
                    return count
        return count

    def absolute_operand(self, address):
        """(offset, size) of an address in the module that the instruction at
        ``address`` holds as a displacement or immediate, or None (a module
        loaded at a fixed address: 32-bit games, ELF without PIE)."""
        if not self.absolute_addresses:
            return None
        disassembler = Cs(CS_ARCH_X86, self.mode)
        disassembler.detail = True
        for instruction in disassembler.disasm(self.image.read(address, 16), address, 1):
            if instruction.mnemonic in _BRANCHES or instruction.mnemonic.startswith("j"):
                return None
            raw = bytes(instruction.bytes)
            for offset, size in ((instruction.disp_offset, instruction.disp_size),
                                 (instruction.imm_offset, instruction.imm_size)):
                if offset and size in (4, 8) and "rip" not in instruction.op_str and \
                        self.image.contains(int.from_bytes(raw[offset:offset + size], "little")):
                    return offset, size
        return None

    def rip_operand(self, address):
        """(displacement offset, length) of a RIP-relative operand of the
        instruction at ``address`` (for ctx.native.rip), or None."""
        disassembler = Cs(CS_ARCH_X86, self.mode)
        disassembler.detail = True
        for instruction in disassembler.disasm(self.image.read(address, 16), address, 1):
            if "rip" in instruction.op_str and instruction.disp_size == 4:
                return instruction.disp_offset, instruction.size
        return None

    # --- signatures ------------------------------------------------------------
    def signature(self, address):
        """The Signature of the function starting at ``address``, or None
        (not a function start, or 32-bit code, which passes arguments on the stack).

        A guess from the code: an argument register the function reads before
        it writes it is an argument (its width the type, its use as an
        address a pointer, an SSE instruction a float or double); whether
        callers read rax or xmm0 right after the call tells the return type.
        The code is read in address order, not along its branches, and
        arguments a function only passes on unread (a tail call) are not
        seen.
        """
        if address in self._signatures:
            return self._signatures[address]
        self._signatures[address] = signature = self._guess_signature(address)
        return signature

    def _guess_signature(self, address):
        function = self.function_starting(address)
        if function is None or self.image.bits != 64:
            return None
        convention = "win64" if self.image.format == "pe" else "sysv"
        integer_registers, vector_registers = _CONVENTIONS[convention]
        reads = self._registers_read_first(function)
        integers = {register: reads[register] for register in integer_registers
                    if register in reads}
        vectors = {register: reads[register] for register in vector_registers
                   if register in reads}
        parameters = []
        if convention == "sysv":
            count = max((integer_registers.index(r) + 1 for r in integers), default=0)
            for register in integer_registers[:count]:
                parameters.append(self._parameter(register, integers.get(register)))
            count = max((vector_registers.index(r) + 1 for r in vectors), default=0)
            for register in vector_registers[:count]:
                parameters.append(self._parameter(register, vectors.get(register)))
            more = integer_registers[-1] in integers or vector_registers[-1] in vectors
        else:
            positions = [integer_registers.index(r) for r in integers] + \
                        [vector_registers.index(r) for r in vectors]
            count = max(positions, default=-1) + 1
            for position in range(count):
                integer, vector = integer_registers[position], vector_registers[position]
                if vector in vectors and integer not in integers:
                    parameters.append(self._parameter(vector, vectors[vector]))
                else:
                    parameters.append(self._parameter(integer, integers.get(integer)))
            more = count == len(integer_registers)
        returns, known = self._return_type(function)
        return Signature(convention, tuple(parameters), returns, known, more)

    @staticmethod
    def _parameter(register, read):
        if read is None:
            return Parameter(register, "int", used=False)
        if register.startswith("xmm"):
            return Parameter(register, read["vector"] or "double")
        return Parameter(register, _integer_type(read["width"]))

    def instructions(self, start, end, limit=MAX_LINES):
        """Capstone instructions with details from ``start`` to ``end``."""
        disassembler = Cs(CS_ARCH_X86, self.mode)
        disassembler.detail = True
        code = self.image.read(start, end - start)
        for count, instruction in enumerate(disassembler.disasm(code, start)):
            if count >= limit:
                break
            yield instruction

    def _registers_read_first(self, function):
        """{register family: {width, vector}} of the registers the function
        reads before writing them, in address order."""
        from capstone.x86 import X86_OP_REG  # pylint: disable=import-error,import-outside-toplevel
        written = set()
        reads = {}
        for instruction in self.instructions(function.start, function.end):
            mnemonic = instruction.mnemonic
            operands = instruction.operands
            read_ids, write_ids = instruction.regs_access()
            if mnemonic in _ZEROING and len(operands) == 2 and \
                    operands[0].type == X86_OP_REG and operands[1].type == X86_OP_REG and \
                    operands[0].reg == operands[1].reg:
                read_ids = []
            # lea computes with the whole register but keeps only its
            # destination's width: lea eax, [rsi + 1] reads an int
            narrow = operands[0].size if mnemonic == "lea" and operands else None
            for register_id in read_ids:
                found = _register(instruction, register_id)
                if found is None or found[0] in written:
                    continue
                family, width = found
                if narrow is not None:
                    width = min(width, narrow)
                entry = reads.setdefault(family, {"width": 0, "vector": None})
                entry["width"] = max(entry["width"], width)
                if family.startswith("xmm") and entry["vector"] is None and \
                        not mnemonic.startswith(("movd", "movq")):
                    entry["vector"] = _vector_type(mnemonic)
            for register_id in write_ids:
                found = _register(instruction, register_id)
                if found is not None:
                    written.add(found[0])
            if mnemonic == "call":
                written |= self._call_clobbers(instruction)
        return reads

    def _call_clobbers(self, instruction):
        """The register families a call may change: those a small helper
        without calls of its own writes (e.g. __chkstk, which a function with
        a large stack frame calls before it reads its arguments), else every
        volatile one."""
        target = _operand_target(instruction.address, instruction.size, "call",
                                 instruction.op_str, False)
        if target is None:
            return _VOLATILE
        cached = self._clobbers.get(target[0])
        if cached is not None:
            return cached
        result = set()
        section = self.image.section_at(target[0])
        if section is None or not section.executable:
            result = _VOLATILE
        else:
            for count, callee in enumerate(self.instructions(target[0], target[0] + 0x200)):
                if callee.mnemonic in ("call", "jmp") or count >= _HELPER_SIZE:
                    result = _VOLATILE
                    break
                for register_id in callee.regs_access()[1]:
                    found = _register(callee, register_id)
                    if found is not None:
                        result.add(found[0])
                if callee.mnemonic == "ret":
                    break
        self._clobbers[target[0]] = frozenset(result)
        return self._clobbers[target[0]]

    def _return_type(self, function, depth=0):
        """(type, decided by callers) of what the function returns."""
        callers = [ref.address for ref in self.references(function.start) if ref.kind == CALL]
        if callers:
            results = [self.call_result(caller) for caller in callers[:_CALLERS_CHECKED]]
            results = [result for result in results if result is not None]
            if not results:
                return "void", True
            return max(results, key=lambda result: (result[0] == "xmm0", result[1]))[2], True
        # Called only through pointers. Ending in a jump to another function
        # (a tail call), it returns what that one returns
        if depth < 3:
            for instruction in self.instructions(function.start, function.end):
                target = _operand_target(instruction.address, instruction.size,
                                         instruction.mnemonic, instruction.op_str, False)
                if instruction.mnemonic == "jmp" and target is not None and \
                        not function.start <= target[0] < function.end:
                    callee = self.function_starting(target[0])
                    if callee is not None:
                        return self._return_type(callee, depth + 1)[0], False
        # Which of rax and xmm0 does the function set last before it returns?
        # (A float set early, e.g. for a call, is no return value.)
        last = returned = None
        for instruction in self.instructions(function.start, function.end):
            if instruction.mnemonic == "call":
                last = None  # what a call leaves there is the callee's
                continue
            for register_id in instruction.regs_access()[1]:
                found = _register(instruction, register_id)
                if found is not None and found[0] in ("rax", "xmm0"):
                    last = found[0]
            if instruction.mnemonic == "ret" and last is not None:
                returned = last
        if returned is None:
            return "void", False
        return ("double" if returned == "xmm0" else "int"), False

    def call_result(self, call_address):
        """(family, width, type) of the result the caller at ``call_address``
        reads right after the call, or None if it ignores it."""
        from capstone.x86 import X86_OP_MEM  # pylint: disable=import-error,import-outside-toplevel
        instructions = self.instructions(call_address, call_address + 16 * _RESULT_WINDOW,
                                         limit=_RESULT_WINDOW + 1)
        written = set()
        for position, instruction in enumerate(instructions):
            if position == 0:
                continue  # the call itself
            mnemonic = instruction.mnemonic
            read_ids, write_ids = instruction.regs_access()
            operands = instruction.operands
            if mnemonic in _ZEROING and len(operands) == 2 and operands[0].type == operands[1].type \
                    and operands[0].type != X86_OP_MEM and operands[0].reg == operands[1].reg:
                read_ids = []
            for register_id in read_ids:
                found = _register(instruction, register_id)
                if found is None or found[0] not in ("rax", "xmm0") or found[0] in written:
                    continue
                family, width = found
                if family == "xmm0":
                    return family, width, _vector_type(mnemonic)
                return family, width, _integer_type(width)
            for register_id in write_ids:
                found = _register(instruction, register_id)
                if found is not None:
                    written.add(found[0])
            if mnemonic in _FLOW or mnemonic.startswith("j") or {"rax", "xmm0"} <= written:
                return None
        return None

    def script(self, address, module=None):
        """A native script that finds ``address`` by its bytes (see ctx.native):
        a function to call, or an instruction whose RIP-relative operand it
        resolves. ``module``: the module's name if it is not the game's executable."""
        pattern = self.pattern(address)
        where = self.where(address) or f"0x{address:x}"
        module_text = f"{module} " if module else ""
        module_arg = f', "{module}"' if module else ""
        lines = [f"// {module_text}{where}: found by its bytes, not by a fixed address",
                 "const n = ctx.native;",
                 f'const at = n.scan("{pattern.text}"{module_arg});']
        if pattern.matches != 1:
            lines.insert(1, f"// Careful: the pattern is found {pattern.matches} times; "
                            "scan() throws until it is made unique")
        function = self.function_starting(address)
        operand = None if function is not None else self.rip_operand(address)
        absolute = None if function is not None or operand else self.absolute_operand(address)
        comment = ""
        if operand or absolute:
            line = next((line for line in self.disassemble(Function(address, address + 16))
                         if line.address == address), None)
            comment = f"  // {line.comment}" if line and line.comment else ""
        signature = self.signature(address) if function is not None else None
        if signature is not None:
            types = ", ".join(f'"{kind}"' for kind in signature.argument_types)
            example = ", ".join(_EXAMPLE_ARGUMENTS.get(kind, "0")
                                for kind in signature.argument_types)
            call = "const result = " if signature.returns != "void" else ""
            lines += [f"// Guessed from the code, check it in the disassembly: "
                      f"{signature.text(self.name(address))}",
                      f'const fn = n.fn(at, "{signature.returns}", [{types}]);',
                      f"// {call}fn({example});",
                      "return String(at);"]
        elif function is not None:
            lines += ["// Check its parameters in the disassembly before calling it, e.g.",
                      '// const result = n.fn(at, "int", ["pointer", "int"])(object, 1);',
                      "return String(at);"]
        elif operand is not None:
            lines += [f"const target = n.rip(at, {operand[0]}, {operand[1]});{comment}",
                      "return String(target);"]
        elif absolute is not None:
            # The file's address; a module loaded elsewhere (relocated) holds its own
            read = "readU32()" if absolute[1] == 4 else "readU64()"
            lines += [f"const target = ptr(at.add({absolute[0]}).{read});{comment}",
                      "return String(target);"]
        else:
            lines.append("return String(at);")
        return "\n".join(lines) + "\n"


# Indexes kept while hermes runs (the window, the monitor): path -> (size, mtime, CodeIndex)
_CACHE = {}
_CACHE_SIZE = 3


@dataclass(frozen=True)
class ModuleChoice:
    """A module of the process the explorer can open: its file and where it is loaded."""

    name: str
    path: str
    base: int | None
    program: bool = False


def process_modules(pid):
    """The ModuleChoices of process ``pid``: the program first, then the
    modules in its folder tree, then the rest by name."""
    if pid is None:
        return []
    try:
        mappings = read_mappings(pid)
    except OSError:
        return []
    bases = {}
    images = {}
    for mapping in mappings:
        path = mapping.path
        if not path or path.startswith("["):
            continue
        if path not in images:
            # Under Wine the PE images are file mappings without a module name;
            # fonts, caches and shared memory are mapped files too
            images[path] = _is_image(path)
        if images[path]:
            bases[path] = min(bases.get(path, mapping.start), mapping.start)
    program_path = _program_path(pid, bases)
    folder = os.path.dirname(program_path).lower() if program_path else None
    choices = [ModuleChoice(os.path.basename(path), path, base, path == program_path)
               for path, base in bases.items()]

    def order(choice):
        nearby = folder is not None and choice.path.lower().startswith(folder)
        return (not choice.program, not nearby, choice.name.lower())

    return sorted(choices, key=order)


def _is_image(path):
    """Whether ``path`` is a PE or ELF file (devices such as /dev/dri/… are
    mapped too, and reading them may block)."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False
        with open(path, "rb") as file:
            magic = file.read(4)
    except OSError:
        return False
    return magic[:2] == b"MZ" or magic == b"\x7fELF"


def _program_path(pid, paths):
    if is_wine_process(pid):
        try:
            from hermes.platform.wine import wine_program  # pylint: disable=import-outside-toplevel
            path = wine_program(pid)[0]
        except (ImportError, OSError):
            path = None
        if path in paths:
            return path
        return next((path for path in paths if path.lower().endswith(".exe")), None)
    name = main_module(pid)
    return next((path for path in paths if os.path.basename(path) == name), None)


def cached_index(path):
    """A CodeIndex built before for the unchanged file at ``path``, or None."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    entry = _CACHE.get(path)
    if entry and entry[0] == info.st_size and entry[1] == info.st_mtime:
        return entry[2]
    return None


def remember_index(path, index):
    """Keep ``index`` for the file at ``path`` (see cached_index)."""
    try:
        info = os.stat(path)
    except OSError:
        return
    _CACHE.pop(path, None)
    _CACHE[path] = (info.st_size, info.st_mtime, index)
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.pop(next(iter(_CACHE)))


def index_for(path, progress=None):
    """The CodeIndex of the file at ``path``: kept, else built (seconds) and kept."""
    index = cached_index(path)
    if index is None:
        index = CodeIndex(load_image(path), progress=progress)
        remember_index(path, index)
    return index


def module_at(pid, address):
    """(ModuleChoice, file address) of the module of process ``pid`` that
    ``address`` lies in, or (None, None)."""
    best = None
    for choice in process_modules(pid):
        if choice.base is not None and choice.base <= address and \
                (best is None or choice.base > best.base):
            best = choice
    if best is None:
        return None, None
    index = index_for(best.path)
    file_address = address - best.base + index.image.base
    if not index.image.contains(file_address):
        return None, None
    return best, file_address


def _quote(text):
    shown = text if len(text) <= COMMENT_STRING else text[:COMMENT_STRING - 1] + "…"
    return '"' + shown.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def parse_location(text, image, module_base=None, module_name=None):
    """The file address of ``text``: 0x… (a file address, or a runtime address
    in the module if ``module_base`` is known), module+0x…, or an export or
    symbol name. None if it is none of them."""
    text = text.strip()
    if not text:
        return None
    match = re.fullmatch(r"(.+?)\s*\+\s*(0x[0-9a-fA-F]+|\d+)", text)
    if match and not match.group(1).lower().startswith("0x"):
        if module_name and match.group(1).lower() != module_name.lower():
            name_address = _name_address(image, match.group(1))
            return None if name_address is None else name_address + int(match.group(2), 0)
        return image.base + int(match.group(2), 0)
    if re.fullmatch(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{6,}", text):
        value = int(text, 16)
        if image.contains(value):
            return value
        if module_base is not None:
            address = value - module_base + image.base
            if image.contains(address):
                return address
        return None
    return _name_address(image, text)


def _name_address(image, name):
    for address, known in image.names.items():
        if known == name:
            return address
    return None
