# pylint: disable=too-many-lines
"""C-like pseudocode for a function of the code explorer.

A reading aid next to the disassembly, not a decompiler: every instruction
becomes at most one C statement, in address order, and keeps its address as
a comment. What it adds over the disassembly:

- the function's head from the guessed Signature, its arguments named
  ``a1``, ``a2`` … until their register is overwritten;
- registers keep their names (``eax``, ``rdi``, ``xmm0``), so a line reads
  like the instruction next to it;
- globals, strings, functions and read-only constants by name or value
  (``g_game``, ``"clicked"``, ``100.0f``) instead of ``[rip + 0x2a31]``;
- calls with their arguments (the assignments to argument registers right
  before the call are folded in) and the result register the caller reads;
- conditions from the instruction that set the flags (``cmp``, ``test``,
  ``comiss``, arithmetic), with unsigned comparisons cast as such;
- tail calls (a ``jmp`` to another function) as a call and a return;
- prologue and epilogue (saving registers, the stack frame) left out;
- a value used once right after it is computed folded into that use
  (``rax = *(uint64_t *)(g_game + 0x28)`` instead of two statements);
- memory reached from a global noted as a pointer chain, the way the pointer
  scan writes it: ``g_game → 0x28 → 0x0``.

What it leaves alone appears as ``asm("…")``. There are no loops or
``if``/``else`` blocks: branches are ``if (…) goto loc_…``.

``render(index, function)`` takes a ``CodeIndex`` (hermes.features.code_explorer)
and one of its Functions.
"""

import math
import os
import struct
from dataclasses import dataclass
from typing import Any

from capstone import CS_AC_WRITE  # pylint: disable=import-error
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_OP_REG  # pylint: disable=import-error

# The column address comments start at
COMMENT_COLUMN = 44
# Statements looked back over for the arguments of a call
FOLD_WINDOW = 16
# Instructions looked ahead to see that a register's value is not read again
LIVENESS_WINDOW = 64


def _families():
    """{register name: (family, width in bytes)} of the general and vector registers."""
    result = {}
    groups = [("rax", "eax", "ax", "al", "ah"), ("rbx", "ebx", "bx", "bl", "bh"),
              ("rcx", "ecx", "cx", "cl", "ch"), ("rdx", "edx", "dx", "dl", "dh"),
              ("rsi", "esi", "si", "sil"), ("rdi", "edi", "di", "dil"),
              ("rbp", "ebp", "bp", "bpl"), ("rsp", "esp", "sp", "spl")]
    groups += [(f"r{n}", f"r{n}d", f"r{n}w", f"r{n}b") for n in range(8, 16)]
    for names in groups:
        widths = (8, 4, 2, 1, 1)
        for position, name in enumerate(names):
            result[name] = (names[0], widths[position])
    for number in range(32):
        for kind in ("xmm", "ymm", "zmm"):
            result[f"{kind}{number}"] = (f"xmm{number}", 16)
    return result


_FAMILIES = _families()
# {family: {width: register name}}: eax for rax at 4 bytes
_BY_WIDTH = {}
for _name, (_family, _width) in _FAMILIES.items():
    _BY_WIDTH.setdefault(_family, {}).setdefault(_width, _name)

# Signature types (Frida's) as C types
_C_TYPES = {"uint8": "uint8_t", "int16": "int16_t", "int": "int32_t", "int64": "int64_t",
            "pointer": "void *", "float": "float", "double": "double", "void": "void"}
_TYPE_WIDTHS = {"uint8": 1, "int16": 2, "int": 4}
_SIGNED = {1: "int8_t", 2: "int16_t", 4: "int32_t", 8: "int64_t", 16: "__m128"}
_UNSIGNED = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t", 16: "__m128"}
# A plain memory access: bytes are mostly flags, 8 bytes mostly pointers
_PLAIN = {1: "uint8_t", 2: "int16_t", 4: "int32_t", 8: "uint64_t", 16: "__m128"}

_ARGUMENTS = {
    "sysv": (("rdi", "rsi", "rdx", "rcx", "r8", "r9"), tuple(f"xmm{n}" for n in range(8))),
    "win64": (("rcx", "rdx", "r8", "r9"), tuple(f"xmm{n}" for n in range(4))),
}
# Registers a call may change
_VOLATILE = {
    "sysv": frozenset({"rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"}
                      | {f"xmm{n}" for n in range(16)}),
    "win64": frozenset({"rax", "rcx", "rdx", "r8", "r9", "r10", "r11"}
                       | {f"xmm{n}" for n in range(6)}),
    "x86": frozenset({"rax", "rcx", "rdx"}),
}
# Registers a function saves for its caller: pushing them is no argument
_SAVED = {
    "sysv": frozenset({"rbx", "rbp", "r12", "r13", "r14", "r15"}),
    "win64": frozenset({"rbx", "rbp", "rsi", "rdi", "r12", "r13", "r14", "r15"}),
    "x86": frozenset({"rbx", "rbp", "rsi", "rdi"}),
}

# Instructions shown as nothing
_SKIP = frozenset({"nop", "endbr64", "endbr32", "int3", "leave", "fnop", "pause"})
# Instructions that leave the flags as they are (besides jcc, setcc and cmovcc)
_KEEP_FLAGS = frozenset({
    "mov", "movabs", "movzx", "movsx", "movsxd", "lea", "push", "pop", "nop", "endbr64",
    "endbr32", "int3", "leave", "cdq", "cdqe", "cqo", "cwde", "cbw", "cwd", "not", "xchg",
    "movss", "movsd", "movaps", "movapd", "movups", "movupd", "movd", "movq", "movdqa",
    "movdqu", "addss", "addsd", "subss", "subsd", "mulss", "mulsd", "divss", "divsd",
    "sqrtss", "sqrtsd", "minss", "minsd", "maxss", "maxsd", "cvtsi2ss", "cvtsi2sd",
    "cvtss2sd", "cvtsd2ss", "cvttss2si", "cvttsd2si", "cvtss2si", "cvtsd2si", "xorps",
    "xorpd", "pxor", "andps", "andpd", "andnps", "andnpd", "orps", "orpd", "unpcklps",
    "unpcklpd", "shufps", "cmpltss", "cmpless", "cmpltsd", "cmplesd", "jmp",
})
_SSE_MOVES = frozenset({"movss", "movsd", "movaps", "movapd", "movups", "movupd", "movdqa",
                        "movdqu"})
# Instructions that only write their first operand: a register named there is not read
_PURE = frozenset({"mov", "movabs", "movzx", "movsx", "movsxd", "lea", "movd", "movq",
                   "cvtsi2ss", "cvtsi2sd", "cvtss2sd", "cvtsd2ss", "cvttss2si", "cvttsd2si",
                   "cvtss2si", "cvtsd2si", "sqrtss", "sqrtsd"}) | _SSE_MOVES
_SSE_OPERATORS = {"add": "+", "sub": "-", "mul": "*", "div": "/"}
_INTEGER_OPERATORS = {"add": "+", "sub": "-", "imul": "*", "and": "&", "or": "|", "xor": "^",
                      "shl": "<<", "sal": "<<", "sar": ">>"}
# Condition codes by their other names
_CONDITION_ALIASES = {"z": "e", "nz": "ne", "nbe": "a", "nb": "ae", "nc": "ae", "nae": "b",
                      "c": "b", "na": "be", "nle": "g", "nl": "ge", "nge": "l", "ng": "le",
                      "pe": "p", "po": "np"}
_SIGNED_CONDITIONS = {"e": "==", "ne": "!=", "g": ">", "ge": ">=", "l": "<", "le": "<="}
_UNSIGNED_CONDITIONS = {"a": ">", "ae": ">=", "b": "<", "be": "<="}
# After test x, x (or and/or/xor): the conditions as comparisons of x with 0
_ZERO_CONDITIONS = {"e": "== 0", "ne": "!= 0", "s": "< 0", "ns": ">= 0", "l": "< 0",
                    "ge": ">= 0", "g": "> 0", "le": "<= 0", "a": "!= 0", "be": "== 0"}


def render(index, function):
    """The pseudocode of ``function`` (a Function of ``index``), as text."""
    return _Renderer(index, function).render()


@dataclass
class _Line:  # pylint: disable=too-many-instance-attributes
    """A statement of the body, or a label."""

    text: str
    address: int = None
    label: bool = False
    # A full assignment ``register = expression``: the register's family …
    defines: str = None
    expression: str = None
    # … and the families the expression reads
    uses: frozenset = frozenset()
    writes: frozenset = frozenset()  # families the statement changes
    target: str = None  # the register a full assignment writes, as named
    # The memory read and written: globals by address, other memory as None
    reads_memory: frozenset = frozenset()
    writes_memory: frozenset = frozenset()
    # A plain statement an argument assignment may be moved past
    movable: bool = False
    # Pointer chains to the memory it reaches: (root, offset, …)
    notes: tuple = ()


@dataclass
class _Flags:
    """What the last flag-setting instruction compared."""

    kind: str  # "cmp", "test", "float", "zero" (and/or/xor), "result" (other arithmetic)
    instruction: Any  # a Capstone instruction
    families: frozenset  # registers it read: writing them makes the flags stale
    stale: bool = False
    notes: tuple = ()  # pointer chains of the memory it compared


def _number(value, hexadecimal=False):
    if not hexadecimal and -256 < value < 256:
        return str(value)
    return f"-0x{-value:x}" if value < 0 else f"0x{value:x}"


def _float_literal(raw, single):
    """The shortest C literal of a float (4 bytes) or double (8 bytes)."""
    value = struct.unpack("<f" if single else "<d", raw)[0]
    if math.isnan(value):
        return "NAN"
    if math.isinf(value):
        return "INFINITY" if value > 0 else "-INFINITY"
    text = repr(value)
    if single:
        for digits in range(1, 10):
            text = repr(float(f"{value:.{digits}g}"))
            if struct.pack("<f", float(text)) == raw:
                break
    if "e" not in text and "." not in text:
        text += ".0"
    return text + ("f" if single else "")


def _mnemonic(instruction):
    """The instruction's name without the prefixes that do not change what it does."""
    mnemonic = instruction.mnemonic
    for prefix in ("notrack ", "bnd "):
        if mnemonic.startswith(prefix):
            mnemonic = mnemonic[len(prefix):]
    return mnemonic


def _zeroing(instruction):
    """Whether the instruction zeroes a register it names twice (xor eax, eax),
    which reads nothing."""
    operands = instruction.operands
    return _mnemonic(instruction).lstrip("v") in ("xor", "sub", "pxor", "xorps", "xorpd") and \
        len(operands) >= 2 and all(operand.type == X86_OP_REG for operand in operands) and \
        len({operand.reg for operand in operands}) == 1


def _overlaps(reads, writes):
    """Whether memory written (globals by address, other memory as None) may
    be memory read: two different globals are apart, other memory may be anything."""
    if not reads or not writes:
        return False
    return None in reads or None in writes or bool(reads & writes)


def _atom(expression):
    """``expression`` as an operand of another one: in parentheses unless it is
    a name, a number or a dereference of one (``*(uint64_t *)(g_game + 0x28)``)."""
    if " " not in expression:
        return expression
    rest = expression
    if rest.startswith("*("):
        close = rest.find(")")
        rest = rest[close + 1:] if close > 0 else rest
        if " " not in rest:
            return expression
    if rest.startswith("("):
        depth = 0
        for position, character in enumerate(rest):
            depth += {"(": 1, ")": -1}.get(character, 0)
            if depth == 0:
                if position == len(rest) - 1:
                    return expression
                break
    return f"({expression})"


def _chain_texts(notes):
    """Pointer chains as text, ``g_game → 0x28 → 0x0``, without one that only
    leads part of the way of another."""
    unique = list(dict.fromkeys(notes))
    longest = [chain for chain in unique
               if not any(other != chain and other[:len(chain)] == chain for other in unique)]
    return [" → ".join([chain[0]] + [_number(offset, hexadecimal=True) if offset >= 0
                                     else f"-0x{-offset:x}" for offset in chain[1:]])
            for chain in longest]


def _condition_code(mnemonic):
    for prefix in ("cmov", "set", "j"):
        if mnemonic.startswith(prefix):
            code = mnemonic[len(prefix):]
            return _CONDITION_ALIASES.get(code, code)
    return None


def _is_sse(mnemonic):
    name = mnemonic[1:] if mnemonic.startswith("v") else mnemonic
    return name.endswith(("ss", "sd", "ps", "pd")) or name.startswith(("cvt", "ucomis", "comis"))


class _Renderer:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Renders one function; see the module's docstring."""

    def __init__(self, index, function):
        self.index = index
        self.image = index.image
        self.function = function
        self.instructions = list(index.instructions(function.start, function.end))
        self.owner = index.owner(function)
        self.wide = self.image.bits == 64
        if not self.wide:
            self.convention = "x86"
        else:
            self.convention = "win64" if self.image.format == "pe" else "sysv"
        # The owner's signature tells the return type, also in a chained part
        self.returns = index.signature(self.owner.start)
        self.signature = self.returns if self.owner is function else None
        self.parameters = {}  # family -> a1 …, while the register still holds the argument
        if self.signature is not None:
            for number, parameter in enumerate(self.signature.parameters, 1):
                self.parameters[parameter.register] = f"a{number}"
        self.imports = set(self.image.imports.values())
        self.lines = []
        self.flags = None
        self.targets = set()
        self.module = os.path.basename(self.image.path)
        # Assignments whose value may still be folded into its one use: family -> _Line
        self.pending = {}
        # Registers holding a pointer read along a chain from a global: family -> chain
        self.chains = {}
        # The instruction being translated: what it folds in, notes and emits
        self.current = None
        self.position = 0
        self.substitutes = {}
        self.substituted = set()
        self.notes = []
        self.emitted = []

    # --- the text --------------------------------------------------------------
    def render(self):
        head = self._head()
        if not self.instructions:
            return "\n".join(head + ["{", "}"]) + "\n"
        self._find_targets()
        for position, instruction in enumerate(self.instructions):
            if instruction.address in self.targets:
                self.lines.append(_Line(f"loc_{instruction.address:x}:", label=True))
                self.pending.clear()  # other paths lead here
                self.chains.clear()
            self.position = position
            self._translate(instruction)
        if self.lines and self.lines[-1].text == "return;" and \
                (len(self.lines) < 2 or not self.lines[-2].label):
            self.lines.pop()  # a void function's last return says nothing
        body = []
        for line in self.lines:
            if line.label:
                body.append(line.text)
                continue
            text = "    " + line.text
            if line.address is not None:
                text = text.ljust(COMMENT_COLUMN - 1) + f" // 0x{line.address:x}"
                chains = _chain_texts(line.notes)
                if chains:
                    text += "  " + ", ".join(chains)
            body.append(text)
        return "\n".join(head + ["{"] + body + ["}"]) + "\n"

    def _head(self):
        index = self.index
        name = index.name(self.function.start)
        lines = ["// Guessed from the machine code: registers keep their names, "
                 "a1, a2 … are the arguments.",
                 "// After each line: the address of its instruction (code, not data), and",
                 "// for memory reached from a global its pointer chain, as the pointer scan",
                 "// writes it."]
        if self.owner is not self.function:
            lines.append(f"// A part of {index.name(self.owner.start)}, "
                         "whose code lies elsewhere.")
        signature = self.signature
        if signature is None:
            if not self.wide:
                lines.append("// 32-bit code passes its arguments on the stack.")
            lines.append(f"{name}(...)")
            return lines
        arguments = []
        for parameter in signature.parameters:
            c_type = _C_TYPES.get(parameter.type, "int32_t")
            separator = "" if c_type.endswith("*") else " "
            note = parameter.register if parameter.used else f"{parameter.register}, unused"
            arguments.append(f"{c_type}{separator}{self.parameters[parameter.register]}"
                             f" /* {note} */")
        if signature.more_possible:
            arguments.append("...")
        if not signature.returns_known:
            lines.append("// The return type is guessed: no caller in the module uses it.")
        returns = _C_TYPES.get(signature.returns, "int32_t")
        separator = "" if returns.endswith("*") else " "
        lines.append(f"{returns}{separator}{name}({', '.join(arguments) or 'void'})")
        return lines

    def _find_targets(self):
        function = self.function
        for instruction in self.instructions:
            if _mnemonic(instruction).startswith(("j", "loop")) and instruction.operands and \
                    instruction.operands[0].type == X86_OP_IMM:
                target = instruction.operands[0].imm
                if function.start <= target < function.end:
                    self.targets.add(target)

    def _emit(self, text, instruction, defines=None, expression=None, uses=None,
              target=None):
        # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        mnemonic = _mnemonic(instruction)
        operands = instruction.operands
        memory = frozenset(self._global_target(instruction, operand)
                           for operand in operands if operand.type == X86_OP_MEM)
        written = frozenset({self._global_target(instruction, operands[0])}) \
            if operands and operands[0].type == X86_OP_MEM else frozenset()
        writes = set()
        for register_id in instruction.regs_access()[1]:
            family = self._family(instruction, register_id)
            if family is not None:
                writes.add(family)
        if uses is None:
            uses = self._reads(instruction)
        reads_memory = memory if mnemonic != "lea" else frozenset()
        notes = list(self.notes)
        if instruction is self.current:
            # A folded-in value reads what its assignment read
            for family in self.substituted:
                folded = self.substitutes[family]
                uses = (uses - {family}) | folded.uses
                reads_memory |= folded.reads_memory
                notes += folded.notes
        movable = mnemonic not in ("call", "ret", "pop") and \
            not mnemonic.startswith(("j", "loop")) and \
            not text.startswith(("asm(", "if (", "return"))
        line = _Line(text, instruction.address, defines=defines, expression=expression,
                     uses=frozenset(uses), writes=frozenset(writes), target=target,
                     reads_memory=reads_memory, writes_memory=written, movable=movable,
                     notes=tuple(notes))
        self.lines.append(line)
        self.emitted.append(line)

    # --- registers ----------------------------------------------------------------
    @staticmethod
    def _family(instruction, register_id):
        found = _FAMILIES.get(instruction.reg_name(register_id))
        return found[0] if found else None

    def _register(self, instruction, register_id):
        """A register as read: the value folded into this use, its argument
        name while it holds the argument, else its name."""
        name = instruction.reg_name(register_id)
        found = _FAMILIES.get(name)
        if found is None:
            return name
        if instruction is self.current and found[0] in self.substitutes:
            self.substituted.add(found[0])
            return _atom(self.substitutes[found[0]].expression)
        if found[0] in self.parameters:
            return self.parameters[found[0]]
        return name

    def _sized(self, family, type_):
        """``family``'s register at the width of a signature type (rdi, int -> edi)."""
        if family in self.parameters:
            return self.parameters[family]
        if family.startswith("xmm"):
            return family
        widths = _BY_WIDTH.get(family, {})
        return widths.get(_TYPE_WIDTHS.get(type_, 8), family)

    def _reads(self, instruction):
        families = set()
        for register_id in instruction.regs_access()[0]:
            family = self._family(instruction, register_id)
            if family is not None:
                families.add(family)
        return frozenset(families)

    # --- operands -------------------------------------------------------------------
    def _global_target(self, instruction, operand):
        """The address a memory operand names directly (RIP-relative or absolute), or None."""
        memory = operand.mem
        if memory.index:
            return None
        if memory.base:
            if instruction.reg_name(memory.base) != "rip":
                return None
            return instruction.address + instruction.size + memory.disp
        if not self.index.absolute_addresses:
            return None
        target = memory.disp & ((1 << self.image.bits) - 1)
        return target if self.image.contains(target) else None

    def _address_of(self, target):
        """A global's address as C: a string, a function, &name, or the number."""
        index = self.index
        text = index.string_at(target)
        if text is not None:
            return index.describe(target)
        section = self.image.section_at(target)
        if section is not None and not section.writable and not section.executable and \
                self.image.read(target, 1) == b"\0":
            return '""'  # an empty string, often the end of another one
        name = self.image.names.get(target) or self.image.imports.get(target)
        if name is not None:
            return name if section is not None and section.executable else f"&{name}"
        if index.function_starting(target) is not None:
            return index.name(target)
        return f"0x{target:x}"

    def _global_value(self, target, size, c_type):
        """A global's value as C: its name, or a read-only constant; else None."""
        # pylint: disable=too-many-return-statements
        image = self.image
        if target in image.names:
            return image.names[target]
        if target in image.imports:
            return image.imports[target].rsplit("!", 1)[-1]
        section = image.section_at(target)
        if section is None or section.writable or section.executable:
            return None
        raw = image.read(target, size)
        if len(raw) != size:
            return None
        if c_type in ("float", "double") and size in (4, 8):
            return _float_literal(raw, size == 4)
        if size in (1, 2, 4) and c_type in _SIGNED.values():
            return _number(int.from_bytes(raw, "little", signed=True))
        if size in (1, 2, 4) and c_type in _UNSIGNED.values():
            return _number(int.from_bytes(raw, "little"))
        found = self.index.describe(target)
        if found.startswith("→ "):
            return found[2:]
        return None

    def _address_text(self, instruction, operand, decimal=False, width=None):
        """The address of a memory operand: ``rax + 0x28``, ``rbx + rcx * 4``.
        ``width`` names the registers at that width (lea eax, [rdx + rdx * 4]
        computes edx + edx * 4)."""
        memory = operand.mem
        parts = []

        def register(register_id):
            name = self._register(instruction, register_id)
            found = _FAMILIES.get(name)
            if width is None or found is None:
                return name
            return _BY_WIDTH[found[0]].get(width, name)

        if memory.segment:
            parts.append(instruction.reg_name(memory.segment))
        if memory.base:
            parts.append(register(memory.base))
        if memory.index:
            index = register(memory.index)
            parts.append(f"{index} * {memory.scale}" if memory.scale != 1 else index)
        text = " + ".join(parts)
        if memory.disp or not parts:
            displacement = abs(memory.disp) if parts else memory.disp
            number = _number(displacement, hexadecimal=not decimal)
            if not parts:
                text = number
            else:
                text += (" - " if memory.disp < 0 else " + ") + number
        return text

    def _memory(self, instruction, operand, c_type):
        target = self._global_target(instruction, operand)
        if target is not None:
            value = self._global_value(target, operand.size, c_type)
            if value is not None:
                return value
            return f"*({c_type} *)0x{target:x}"
        address = self._address_text(instruction, operand)
        if " " in address:
            address = f"({address})"
        return f"*({c_type} *){address}"

    def _memory_type(self, instruction, operand, signedness=None):
        mnemonic = _mnemonic(instruction)
        if _is_sse(mnemonic) and operand.size in (4, 8) and \
                not mnemonic.lstrip("v").startswith(("cvtsi2", "movd", "movq")):
            return "float" if operand.size == 4 else "double"
        table = {"signed": _SIGNED, "unsigned": _UNSIGNED}.get(signedness, _PLAIN)
        return table.get(operand.size, "uint64_t")

    def _operand(self, instruction, operand, signedness=None, hexadecimal=False,
                 address_ok=False):
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        """An operand as C. ``signedness`` ("signed", "unsigned") picks the
        types of memory and casts registers to unsigned ones."""
        if operand.type == X86_OP_REG:
            name = self._register(instruction, operand.reg)
            if signedness == "unsigned" and not name.startswith("xmm"):
                return f"({_UNSIGNED.get(operand.size, 'uint64_t')}){name}"
            return name
        if operand.type == X86_OP_IMM:
            value = operand.imm
            if address_ok and self.index.absolute_addresses and value >= 0x10000 and \
                    self.image.contains(value):
                return self._address_of(value)
            if signedness == "unsigned" and value < 0:
                value &= (1 << (8 * (operand.size or 8))) - 1
            return _number(value, hexadecimal)
        if operand.type == X86_OP_MEM:
            return self._memory(instruction, operand,
                                self._memory_type(instruction, operand, signedness))
        return instruction.op_str

    def _assign(self, instruction, destination, expression, overwrite=True):
        """``destination = expression;``. Overwriting a register ends its
        argument name, so the destination is named by the register itself."""
        if destination.type == X86_OP_REG:
            family = self._family(instruction, destination.reg)
            name = instruction.reg_name(destination.reg) if overwrite else \
                self._register(instruction, destination.reg)
            self._emit(f"{name} = {expression};", instruction,
                       defines=family if overwrite else None, expression=expression,
                       uses=self._reads(instruction), target=name)
        else:
            target = self._operand(instruction, destination)
            self._emit(f"{target} = {expression};", instruction)

    def _asm(self, instruction):
        text = f"{instruction.mnemonic} {instruction.op_str}".strip()
        self._emit(f'asm("{text}");', instruction)

    # --- conditions -----------------------------------------------------------------
    def _condition(self, code):  # pylint: disable=too-many-branches
        flags = self.flags
        if flags is None:
            return "/* flags */"
        instruction = flags.instruction
        source = f"{instruction.mnemonic} {instruction.op_str}".strip()
        if flags.stale:
            return f"/* flags of {source} */"
        operands = instruction.operands
        text = None
        if flags.kind == "cmp" and len(operands) == 2:
            if code in _SIGNED_CONDITIONS:
                signed = "signed" if code not in ("e", "ne") else None
                left = self._operand(instruction, operands[0], signed)
                right = self._operand(instruction, operands[1], signed)
                text = f"{left} {_SIGNED_CONDITIONS[code]} {right}"
            elif code in _UNSIGNED_CONDITIONS:
                left = self._operand(instruction, operands[0], "unsigned")
                right = self._operand(instruction, operands[1], "unsigned")
                text = f"{left} {_UNSIGNED_CONDITIONS[code]} {right}"
            elif code in ("s", "ns"):
                left = self._operand(instruction, operands[0], "signed")
                right = self._operand(instruction, operands[1], "signed")
                text = f"{left} - {right} {'<' if code == 's' else '>='} 0"
        elif flags.kind in ("test", "zero") and code in _ZERO_CONDITIONS:
            left = self._operand(instruction, operands[0], "signed", hexadecimal=True)
            if flags.kind == "test" and len(operands) == 2 and \
                    (operands[1].type != X86_OP_REG or operands[0].type != X86_OP_REG or
                     operands[0].reg != operands[1].reg):
                right = self._operand(instruction, operands[1], hexadecimal=True)
                left = f"({left} & {right})"
            text = f"{left} {_ZERO_CONDITIONS[code]}"
        elif flags.kind == "result" and code in ("e", "ne", "s", "ns"):
            left = self._operand(instruction, operands[0], "signed")
            text = f"{left} {_ZERO_CONDITIONS[code]}"
        elif flags.kind == "float" and len(operands) == 2:
            left = self._operand(instruction, operands[0])
            right = self._operand(instruction, operands[1])
            relations = {"a": ">", "ae": ">=", "b": "<", "be": "<=", "e": "==", "ne": "!="}
            if code in relations:
                text = f"{left} {relations[code]} {right}"
            elif code in ("p", "np"):
                text = f"{'' if code == 'p' else '!'}isunordered({left}, {right})"
        if text is None:
            return f"/* flags of {source} */"
        self.notes.extend(flags.notes)
        return text

    def _set_flags(self, instruction):
        """The flags after ``instruction``."""
        mnemonic = _mnemonic(instruction)
        name = mnemonic[1:] if mnemonic.startswith("v") else mnemonic
        if mnemonic in _KEEP_FLAGS or name in _KEEP_FLAGS or \
                mnemonic.startswith(("j", "set", "cmov", "vmov")):
            return
        kind = None
        if name == "cmp":
            kind = "cmp"
        elif name == "test":
            kind = "test"
        elif name in ("comiss", "ucomiss", "comisd", "ucomisd"):
            kind = "float"
        elif name in ("and", "or", "xor"):
            kind = "zero"
        elif name in ("add", "sub", "inc", "dec", "neg", "shl", "sal", "sar", "shr"):
            kind = "result"
        self.flags = None if kind is None else _Flags(kind, instruction,
                                                      self._reads(instruction),
                                                      notes=tuple(self.notes))

    # --- calls ----------------------------------------------------------------------
    def _stub(self, target):
        """The import a stub at ``target`` jumps to (a PLT entry, a thunk), or None."""
        for instruction in self.index.instructions(target, target + 16, limit=3):
            if instruction.mnemonic in ("endbr64", "endbr32", "nop"):
                continue
            if _mnemonic(instruction) == "jmp" and instruction.operands and \
                    instruction.operands[0].type == X86_OP_MEM:
                slot = self._global_target(instruction, instruction.operands[0])
                if slot in self.image.imports:
                    return self.image.imports[slot].rsplit("!", 1)[-1]
            break
        return None

    def _callee(self, instruction, operand):
        """(name as C, Signature or None) of what a call or jump goes to."""
        # pylint: disable=too-many-return-statements
        index = self.index
        if operand.type == X86_OP_IMM:
            target = operand.imm
            name = self.image.names.get(target)
            if name is not None and name in self.imports:
                return name.rsplit("!", 1)[-1], None
            if index.function_starting(target) is not None or name is not None:
                return index.name(target), index.signature(target)
            stub = self._stub(target)
            if stub is not None:
                return stub, None
            return index.where(target) or f"sub_{target:x}", None
        if operand.type == X86_OP_MEM:
            target = self._global_target(instruction, operand)
            if target is not None:
                if target in self.image.imports:
                    return self.image.imports[target].rsplit("!", 1)[-1], None
                if target in self.image.names:
                    return self.image.names[target], None
            return f"(*{self._operand(instruction, operand)})", None
        return f"(*{self._operand(instruction, operand)})", None

    def _fold(self, candidates):
        """({family: expression}, {family: register}) of the assignments to
        ``candidates`` before a call: those taken out of the body to become
        its arguments, and those that stay (something in between needs the
        register, or changes what it reads). Compilers mix other work into
        setting up the arguments (keeping the last result: ``lbl_gold = rax``);
        an assignment is moved past such a statement if neither changes
        what the other reads."""
        folded, kept = {}, {}
        taken = []
        used, written = set(), set()  # by the statements after the one looked at
        memory_written = set()
        start = max(0, len(self.lines) - FOLD_WINDOW)
        for position in range(len(self.lines) - 1, start - 1, -1):
            line = self.lines[position]
            if line.label or not line.movable:
                break
            if line.defines in candidates and line.defines not in written and \
                    line.defines not in used and not line.uses & written and \
                    not _overlaps(line.reads_memory, memory_written):
                folded[line.defines] = line.expression
                taken.append(position)
            else:
                memory_written |= line.writes_memory
                if line.defines in candidates and line.defines not in written and \
                        line.defines not in used:
                    kept[line.defines] = line.target
            used |= line.uses
            written |= line.writes | ({line.defines} if line.defines else set())
        for position in taken:
            self.notes.extend(self.lines[position].notes)
            del self.lines[position]
        return folded, kept

    def _arguments(self, signature):  # pylint: disable=too-many-locals
        """The arguments of a call, as C: the callee's guessed parameters and
        the argument registers the caller sets right before the call (a
        function that only passes an argument on does not show it in its
        signature). Registers no statement sets are named by themselves."""
        if self.convention == "x86":
            return "..."
        integers, vectors = _ARGUMENTS[self.convention]
        folded, kept = self._fold(set(integers) | set(vectors))
        for family, register in kept.items():
            folded.setdefault(family, register)
        types = {parameter.register: parameter.type
                 for parameter in signature.parameters} if signature is not None else {}
        present = set(folded) | set(types)

        def value(register):
            return folded.get(register) or self._sized(register, types.get(register, "pointer"))

        texts = []
        if self.convention == "sysv":
            for registers in (integers, vectors):
                count = max((registers.index(r) + 1 for r in present if r in registers),
                            default=0)
                texts += [value(register) for register in registers[:count]]
            full = integers[-1] in present
        else:
            count = max((max(integers.index(r) if r in integers else -1,
                             vectors.index(r) if r in vectors else -1) + 1 for r in present),
                        default=0)
            for position in range(count):
                integer, vector = integers[position], vectors[position]
                floating = vector in folded or (vector in types and integer not in folded)
                texts.append(value(vector if floating else integer))
            full = count == len(integers)
        if (signature.more_possible if signature is not None else full):
            texts.append("...")  # more on the stack
        return ", ".join(texts)

    def _call(self, instruction):
        name, signature = self._callee(instruction, instruction.operands[0])
        text = f"{name}({self._arguments(signature)})"
        result = self.index.call_result(instruction.address) if self.wide else None
        if result is not None:
            family, width, _type = result
            register = family if family.startswith("xmm") else \
                _BY_WIDTH.get(family, {}).get(width, family)
            self._emit(f"{register} = {text};", instruction)
        else:
            self._emit(f"{text};", instruction)

    def _return_value(self):
        signature = self.returns
        if signature is None:
            return "return eax;" if not self.wide else "return;"
        if signature.returns == "void":
            return "return;"
        family = "xmm0" if signature.returns in ("float", "double") else "rax"
        return f"return {self._sized(family, signature.returns)};"

    def _tail_call(self, instruction, operand):
        name, signature = self._callee(instruction, operand)
        text = f"{name}({self._arguments(signature)})"
        own = self.returns.returns if self.returns is not None else "void"
        if own != "void" and (signature is None or signature.returns != "void"):
            self._emit(f"return {text};", instruction)
        else:
            self._emit(f"{text};", instruction)
            self._emit("return;", instruction)
            self.lines[-1].address = None

    # --- instructions ---------------------------------------------------------------
    def _translate(self, instruction):
        read_ids, write_ids = instruction.regs_access()
        reads = {self._family(instruction, r) for r in read_ids} - {None}
        writes = {self._family(instruction, r) for r in write_ids} - {None}
        mnemonic = _mnemonic(instruction)
        zeroing = _zeroing(instruction)
        if zeroing:
            reads = set()
        self.current = instruction
        self.notes = self._chain_notes(instruction)
        self.substitutes = self._substitutions(instruction, reads)
        self.substituted = set()
        self.emitted = []
        self._statement(instruction, zeroing)
        # The assignments folded into this instruction's statement go
        for family in self.substituted:
            folded = self.substitutes[family]
            self.lines = [line for line in self.lines if line is not folded]
        self._update_pending(instruction, reads, writes)
        self._update_chains(instruction, writes)
        self.current = None
        self.substitutes = {}
        # Bookkeeping: arguments overwritten, flags changed or made stale
        overwritten = writes - reads
        if mnemonic == "call":
            overwritten |= _VOLATILE[self.convention]
        for family in overwritten:
            self.parameters.pop(family, None)
        flags = self.flags
        self._set_flags(instruction)
        if self.flags is flags and flags is not None and writes & flags.families:
            flags.stale = True

    # --- folding a value into its one use -----------------------------------------
    def _substitutions(self, instruction, reads):
        """{family: _Line} of the pending assignments this instruction may fold
        in: it names the register once, as a value it does not also change,
        and nothing reads the register afterwards."""
        if not self.pending:
            return {}
        mnemonic = _mnemonic(instruction)
        name = mnemonic[1:] if mnemonic.startswith("v") else mnemonic
        if mnemonic in ("cmp", "test", "call", "ret", "push", "pop") or \
                name in ("comiss", "ucomiss", "comisd", "ucomisd") or \
                mnemonic.startswith(("j", "set", "cmov", "loop")):
            return {}
        result = {}
        for family in reads:
            line = self.pending.get(family)
            if line is not None and self._occurrences(instruction, family) == 1 and \
                    self._dead_after(family):
                result[family] = line
        return result

    def _occurrences(self, instruction, family):
        """How often the instruction names ``family`` as a value it reads
        (many if it also changes it: add eax, … cannot take eax's value)."""
        operands = instruction.operands
        pure = _mnemonic(instruction).lstrip("v") in _PURE
        count = 0
        for position, operand in enumerate(operands):
            if operand.type == X86_OP_REG and self._family(instruction, operand.reg) == family:
                if position == 0 and not pure:
                    return 2
                if position > 0 or not pure:
                    count += 1
            elif operand.type == X86_OP_MEM:
                for register_id in (operand.mem.base, operand.mem.index):
                    if register_id and self._family(instruction, register_id) == family:
                        count += 1
        return count

    def _overwrites(self, instruction, family):
        """Whether the instruction replaces all of ``family``'s value."""
        operands = instruction.operands
        name = _mnemonic(instruction).lstrip("v")
        for register_id in instruction.regs_access()[1]:
            found = _FAMILIES.get(instruction.reg_name(register_id))
            if found is None or found[0] != family:
                continue
            if not family.startswith("xmm"):
                return found[1] >= 4  # 32 bits clear the upper half
            if name in ("pxor", "xorps", "xorpd") and len(operands) >= 2 and \
                    all(o.type == X86_OP_REG and o.reg == operands[0].reg for o in operands):
                return True
            return name in _SSE_MOVES and len(operands) == 2 and \
                (operands[1].type == X86_OP_MEM or name in ("movaps", "movapd", "movups",
                                                            "movupd", "movdqa", "movdqu"))
        return False

    def _dead_after(self, family):  # pylint: disable=too-many-return-statements
        """Whether ``family``'s value is not read after the current
        instruction (straight-line code only: a branch or label ends the look)."""
        if self._overwrites(self.current, family):
            return True
        following = self.instructions[self.position + 1:self.position + 1 + LIVENESS_WINDOW]
        for instruction in following:
            if instruction.address in self.targets:
                return False
            mnemonic = _mnemonic(instruction)
            if not _zeroing(instruction) and family in self._reads(instruction):
                return False
            if mnemonic == "call":
                integers, vectors = _ARGUMENTS.get(self.convention, ((), ()))
                if family in integers or family in vectors:
                    return False
                if family in _VOLATILE[self.convention]:
                    return True
                continue
            if mnemonic in ("ret", "retf"):
                returns = self.returns.returns if self.returns is not None else "int"
                if family == "rax":
                    return returns in ("void", "float", "double")
                return family != "xmm0" or returns not in ("float", "double")
            if mnemonic.startswith(("j", "loop")):
                return False
            if self._overwrites(instruction, family):
                return True
        return False

    def _update_pending(self, instruction, reads, writes):
        """Keep the assignments that may still be folded into a later use."""
        mnemonic = _mnemonic(instruction)
        if mnemonic in ("call", "ret", "retf") or mnemonic.startswith(("j", "loop")) or \
                any(line.text.startswith("asm(") for line in self.emitted):
            self.pending.clear()
        else:
            memory_written = {self._global_target(instruction, operand)
                              for operand in instruction.operands
                              if operand.type == X86_OP_MEM and operand.access & CS_AC_WRITE}
            if mnemonic == "push":
                memory_written.add(None)
            for family, line in list(self.pending.items()):
                if family in reads or family in writes or line.uses & writes or \
                        _overlaps(line.reads_memory, frozenset(memory_written)):
                    del self.pending[family]
        for line in self.emitted:
            if self._foldable(line):
                self.pending[line.defines] = line

    def _foldable(self, line):
        """Whether a statement assigns a whole register a value that may be
        folded into a later use (an 8- or 16-bit write keeps the rest)."""
        found = _FAMILIES.get(line.target or "")
        if not line.defines or not line.movable or line.expression is None or found is None:
            return False
        if found[1] < 4 and not line.defines.startswith("xmm"):
            return False
        return any(kept is line for kept in self.lines)  # not taken as an argument

    # --- pointer chains -----------------------------------------------------------------
    def _chain(self, instruction, operand):
        """(root, offset, …) of a memory operand reached from a global, or None."""
        memory = operand.mem
        if memory.index or not memory.base:
            return None
        chain = self.chains.get(self._family(instruction, memory.base))
        return chain + (memory.disp,) if chain is not None else None

    def _chain_notes(self, instruction):
        notes = []
        for operand in instruction.operands:
            if operand.type == X86_OP_MEM:
                chain = self._chain(instruction, operand)
                if chain is not None:
                    notes.append(chain)
        return notes

    def _update_chains(self, instruction, writes):
        """The registers holding a pointer read along a chain from a global
        after ``instruction``: a load of a global or through a chain starts or
        extends one, a copy takes it along, any other write ends it."""
        before = dict(self.chains)
        mnemonic = _mnemonic(instruction)
        for family in writes:
            self.chains.pop(family, None)
        if mnemonic == "call":
            for family in _VOLATILE[self.convention]:
                self.chains.pop(family, None)
        operands = instruction.operands
        pointer = 8 if self.wide else 4
        if mnemonic != "mov" or len(operands) != 2 or operands[0].type != X86_OP_REG or \
                operands[0].size != pointer:
            return
        destination = self._family(instruction, operands[0].reg)
        source = operands[1]
        chain = None
        if source.type == X86_OP_MEM:
            target = self._global_target(instruction, source)
            if target is not None:
                if target not in self.image.imports:
                    chain = (self._root(target),)
            else:
                saved, self.chains = self.chains, before
                chain = self._chain(instruction, source)
                self.chains = saved
        elif source.type == X86_OP_REG:
            chain = before.get(self._family(instruction, source.reg))
        if chain is not None and destination is not None:
            self.chains[destination] = chain

    def _root(self, target):
        """A global as the start of a pointer chain: its name, else module+offset."""
        return self.image.names.get(target) or f"{self.module}+0x{target - self.image.base:x}"

    def _frame(self, instruction):
        """Whether the instruction only keeps registers or builds the stack frame."""
        mnemonic = _mnemonic(instruction)
        operands = instruction.operands
        if mnemonic in ("push", "pop") and operands and operands[0].type == X86_OP_REG:
            return self._family(instruction, operands[0].reg) in _SAVED[self.convention]
        if len(operands) == 2 and operands[0].type == X86_OP_REG and \
                self._family(instruction, operands[0].reg) in ("rsp", "rbp"):
            if mnemonic in ("sub", "add", "and") and operands[1].type == X86_OP_IMM:
                return True
            if mnemonic in ("mov", "lea"):
                source = operands[1]
                register = source.reg if source.type == X86_OP_REG else \
                    source.mem.base if source.type == X86_OP_MEM else 0
                return bool(register) and \
                    self._family(instruction, register) in ("rsp", "rbp")
        return False

    def _statement(self, instruction, zeroing):
        # pylint: disable=too-many-return-statements,too-many-branches
        mnemonic = _mnemonic(instruction)
        operands = instruction.operands
        if mnemonic in _SKIP or (mnemonic.startswith("nop")) or self._frame(instruction):
            return
        if zeroing:
            self._assign(instruction, operands[0], "0")
            return
        if mnemonic in ("cmp", "test") or mnemonic.lstrip("v") in (
                "comiss", "ucomiss", "comisd", "ucomisd"):
            return
        if mnemonic == "call" and operands:
            self._call(instruction)
            return
        if mnemonic in ("ret", "retf"):
            self._emit(self._return_value(), instruction)
            return
        if mnemonic.startswith("j") or mnemonic.startswith("loop"):
            self._jump(instruction)
            return
        if self._integer(instruction) or self._sse(instruction):
            return
        self._asm(instruction)

    def _jump(self, instruction):  # pylint: disable=too-many-branches
        mnemonic = _mnemonic(instruction)
        operands = instruction.operands
        if not operands:
            self._asm(instruction)
            return
        operand = operands[0]
        if mnemonic == "jmp":
            if operand.type == X86_OP_IMM:
                target = operand.imm
                if self.function.start <= target < self.function.end:
                    self._emit(f"goto loc_{target:x};", instruction)
                elif self.index.function_starting(target) is not None or \
                        target in self.image.names or self._stub(target) is not None:
                    self._tail_call(instruction, operand)
                else:
                    where = self.index.where(target)
                    self._emit(f"goto 0x{target:x};" + (f"  /* {where} */" if where else ""),
                               instruction)
            elif operand.type == X86_OP_MEM and \
                    self._global_target(instruction, operand) in self.image.imports:
                self._tail_call(instruction, operand)
            else:
                self._emit(f"goto *{self._operand(instruction, operand)};", instruction)
            return
        if mnemonic in ("jecxz", "jrcxz"):
            condition = f"{mnemonic[1:-1]} == 0"
        elif mnemonic.startswith("loop"):
            self._asm(instruction)
            return
        else:
            condition = self._condition(_condition_code(mnemonic))
        if operand.type != X86_OP_IMM:
            self._asm(instruction)
            return
        target = operand.imm
        if self.function.start <= target < self.function.end:
            label = f"loc_{target:x}"
        else:
            label = self.index.where(target) or f"0x{target:x}"
        self._emit(f"if ({condition}) goto {label};", instruction)

    def _integer(self, instruction):
        """General-purpose instructions; False if not one of them."""
        # pylint: disable=too-many-branches,too-many-locals,too-many-statements
        mnemonic = _mnemonic(instruction)
        operands = instruction.operands
        count = len(operands)
        operand = self._operand
        if mnemonic in ("mov", "movabs") and count == 2:
            self._assign(instruction, operands[0],
                         operand(instruction, operands[1], address_ok=True))
        elif mnemonic in ("movzx", "movsx", "movsxd") and count == 2:
            signedness = "unsigned" if mnemonic == "movzx" else "signed"
            source = operands[1]
            if source.type == X86_OP_REG:
                table = _UNSIGNED if signedness == "unsigned" else _SIGNED
                text = f"({table[source.size]}){self._register(instruction, source.reg)}"
            else:
                text = operand(instruction, source, signedness)
            self._assign(instruction, operands[0], text)
        elif mnemonic == "lea" and count == 2:
            source = operands[1]
            target = self._global_target(instruction, source)
            if target is not None:
                text = self._address_of(target)
            else:
                narrow = operands[0].size < 8
                text = self._address_text(instruction, source, decimal=narrow,
                                          width=operands[0].size if narrow else None)
            self._assign(instruction, operands[0], text)
        elif mnemonic in _INTEGER_OPERATORS and count == 2:
            hexadecimal = mnemonic in ("and", "or", "xor")
            self._emit(f"{operand(instruction, operands[0])} {_INTEGER_OPERATORS[mnemonic]}= "
                       f"{operand(instruction, operands[1], hexadecimal=hexadecimal)};",
                       instruction)
        elif mnemonic == "imul" and count == 3:
            self._assign(instruction, operands[0], f"{operand(instruction, operands[1])} * "
                         f"{operand(instruction, operands[2])}")
        elif mnemonic in ("shl", "sal", "sar", "shr") and count == 1:
            self._emit(f"{operand(instruction, operands[0])} "
                       f"{'>>' if mnemonic in ('sar', 'shr') else '<<'}= 1;", instruction)
        elif mnemonic == "shr" and count == 2:
            target = operands[0]
            if target.type == X86_OP_REG:
                name = self._register(instruction, target.reg)
                self._emit(f"{name} = ({_UNSIGNED[target.size]}){name} >> "
                           f"{operand(instruction, operands[1])};", instruction)
            else:
                self._emit(f"{operand(instruction, target, 'unsigned')} >>= "
                           f"{operand(instruction, operands[1])};", instruction)
        elif mnemonic in ("inc", "dec") and count == 1:
            self._emit(f"{operand(instruction, operands[0])}"
                       f"{'++' if mnemonic == 'inc' else '--'};", instruction)
        elif mnemonic in ("neg", "not") and count == 1:
            text = operand(instruction, operands[0])
            self._emit(f"{text} = {'-' if mnemonic == 'neg' else '~'}{text};", instruction)
        elif mnemonic.startswith("set") and count == 1:
            self._assign(instruction, operands[0],
                         self._condition(_condition_code(mnemonic)), overwrite=False)
        elif mnemonic.startswith("cmov") and count == 2:
            condition = self._condition(_condition_code(mnemonic))
            self._emit(f"if ({condition}) {self._register(instruction, operands[0].reg)} = "
                       f"{operand(instruction, operands[1])};", instruction)
        elif mnemonic in ("cdqe", "cwde", "cbw"):
            source, target, c_type = {"cdqe": ("eax", "rax", "int32_t"),
                                      "cwde": ("ax", "eax", "int16_t"),
                                      "cbw": ("al", "ax", "int8_t")}[mnemonic]
            self._emit(f"{target} = ({c_type}){self._family_name(source)};", instruction)
        elif mnemonic in ("cdq", "cqo", "cwd"):
            source, target = {"cdq": ("eax", "edx"), "cqo": ("rax", "rdx"),
                              "cwd": ("ax", "dx")}[mnemonic]
            self._emit(f"{target} = {self._family_name(source)} < 0 ? -1 : 0;", instruction)
        elif mnemonic == "push" and count == 1:
            self._emit(f"push({operand(instruction, operands[0], address_ok=True)});",
                       instruction)
        elif mnemonic == "pop" and count == 1:
            self._assign(instruction, operands[0], "pop()")
        else:
            return False
        return True

    def _family_name(self, register):
        """``register``, or its argument name while it holds the argument."""
        family = _FAMILIES[register][0]
        return self.parameters.get(family, register)

    def _sse(self, instruction):
        """Scalar SSE and AVX instructions; False if not one of them."""
        # pylint: disable=too-many-return-statements,too-many-branches
        mnemonic = _mnemonic(instruction)
        operands = instruction.operands
        vex = mnemonic.startswith("v")
        name = mnemonic[1:] if vex else mnemonic
        # AVX names three operands, the first one only written: vaddss xmm0, xmm1, xmm2
        sources = operands[1:] if vex and len(operands) == 3 else operands
        operand = self._operand
        if name in _SSE_MOVES and len(operands) in (2, 3):
            self._assign(instruction, operands[0], operand(instruction, operands[-1]))
        elif name in ("movd", "movq") and len(operands) == 2:
            if all(o.type == X86_OP_REG for o in operands):
                return False
            self._assign(instruction, operands[0], operand(instruction, operands[1]))
        elif name[:3] in _SSE_OPERATORS and name[3:] in ("ss", "sd") and len(sources) == 2:
            symbol = _SSE_OPERATORS[name[:3]]
            left, right = (operand(instruction, o) for o in sources)
            if vex and len(operands) == 3:
                self._assign(instruction, operands[0], f"{left} {symbol} {right}")
            else:
                self._emit(f"{left} {symbol}= {right};", instruction)
        elif name[:3] in ("min", "max") and name[3:] in ("ss", "sd") and len(sources) == 2:
            function = "f" + name[:3] + ("f" if name.endswith("ss") else "")
            left, right = (operand(instruction, o) for o in sources)
            self._assign(instruction, operands[0], f"{function}({left}, {right})")
        elif name in ("sqrtss", "sqrtsd") and sources:
            function = "sqrtf" if name == "sqrtss" else "sqrt"
            self._assign(instruction, operands[0],
                         f"{function}({operand(instruction, sources[-1])})")
        elif name in ("cvtsi2ss", "cvtsi2sd", "cvtss2sd", "cvtsd2ss") and sources:
            c_type = "float" if name.endswith("ss") else "double"
            source = sources[-1]
            text = operand(instruction, source, "signed") if source.type == X86_OP_MEM \
                else operand(instruction, source)
            self._assign(instruction, operands[0], f"({c_type}){text}")
        elif name in ("cvttss2si", "cvttsd2si") and len(operands) == 2:
            c_type = _SIGNED.get(operands[0].size, "int32_t")
            self._assign(instruction, operands[0],
                         f"({c_type}){operand(instruction, operands[1])}")
        elif name in ("cvtss2si", "cvtsd2si") and len(operands) == 2:
            function = "lrintf" if name == "cvtss2si" else "lrint"
            self._assign(instruction, operands[0],
                         f"{function}({operand(instruction, operands[1])})")
        else:
            return False
        return True
