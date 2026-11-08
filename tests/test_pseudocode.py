"""Pseudocode of the code explorer (see hermes/features/pseudocode.py).

Functions written in assembly (naked, Intel syntax) give exact instructions
whatever the compiler version; the C functions show how compiled code reads.
"""

import re
import shutil
import subprocess

import pytest

from hermes.features.code_explorer import CodeIndex, load_image, parse_location
from hermes.features.pseudocode import render

_SOURCE = r"""
#include <stdio.h>

int counter;
const char report_format[] = "value %d\n";
float scale;
void *game;

#define NAKED __attribute__((naked, noinline))

__attribute__((noinline)) void add_counter(int value) { counter += value; }
__attribute__((noinline)) void set_scale(float value) { scale = value * 1.5f; }

NAKED int signed_test(int value) { __asm__(
    "test edi, edi\n"
    "jle 1f\n"
    "mov eax, 1\n"
    "ret\n"
    "1: xor eax, eax\n"
    "ret\n"); }

NAKED int unsigned_compare(unsigned value) { __asm__(
    "xor eax, eax\n"
    "cmp edi, 9\n"
    "seta al\n"
    "ret\n"); }

// The register the comparison read changes before the jump
NAKED int stale_flags(int value) { __asm__(
    "cmp edi, 5\n"
    "mov edi, 7\n"
    "jg 1f\n"
    "mov eax, 1\n"
    "ret\n"
    "1: mov eax, 2\n"
    "ret\n"); }

NAKED int multiply(int value) { __asm__(
    "imul eax, edi, 12\n"
    "ret\n"); }

NAKED void shift(unsigned *value) { __asm__(
    "shr dword ptr [rdi], 3\n"
    "ret\n"); }

// A stack frame around a tail call
NAKED void tail(int value) { __asm__(
    "push rbx\n"
    "sub rsp, 16\n"
    "lea edi, [rdi + 20]\n"
    "add rsp, 16\n"
    "pop rbx\n"
    "jmp add_counter\n"); }

// An argument set before a store the call does not need
NAKED void report(int value) { __asm__(
    "push rbx\n"
    "mov ebx, edi\n"
    "lea rdi, [rip + report_format]\n"
    "mov esi, ebx\n"
    "mov dword ptr [rip + counter], ebx\n"
    "xor eax, eax\n"
    "call printf@PLT\n"
    "pop rbx\n"
    "ret\n"); }

NAKED int float_compare(float value) { __asm__(
    "xor eax, eax\n"
    "ucomiss xmm0, dword ptr [rip + scale]\n"
    "seta al\n"
    "ret\n"); }

// A pointer chain from a global, each value used once
NAKED int chain_read(void) { __asm__(
    "mov rax, qword ptr [rip + game]\n"
    "mov rax, qword ptr [rax + 40]\n"
    "mov eax, dword ptr [rax + 4]\n"
    "ret\n"); }

// The global's value is used twice
NAKED int chain_twice(void) { __asm__(
    "mov rax, qword ptr [rip + game]\n"
    "mov ecx, dword ptr [rax + 8]\n"
    "mov eax, dword ptr [rax + 12]\n"
    "add eax, ecx\n"
    "ret\n"); }

int main(int argc, char **argv) {
    (void)argv;
    unsigned u = (unsigned)argc;
    int r = signed_test(argc) + unsigned_compare(u) + stale_flags(argc) + multiply(argc);
    set_scale((float)argc);
    r += float_compare((float)argc);
    shift(&u);
    tail(argc);
    report(r);
    r += chain_read() + chain_twice();
    return r + (int)u + counter;
}
"""

requires_gcc = pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")


@pytest.fixture(name="index", scope="module")
def fixture_index(tmp_path_factory):
    if shutil.which("gcc") is None:
        pytest.skip("needs gcc")
    directory = tmp_path_factory.mktemp("pseudocode")
    source = directory / "game.c"
    source.write_text(_SOURCE)
    binary = directory / "game"
    subprocess.run(["gcc", "-O1", "-masm=intel", str(source), "-o", str(binary)], check=True)
    return CodeIndex(load_image(str(binary)))


def _render(index, name):
    return render(index, index.function_starting(parse_location(name, index.image)))


def _body(text):
    """The statements and labels, without the comments (address, pointer
    chains) and the gcc ud2 after a naked function."""
    lines = text.splitlines()
    body = lines[lines.index("{") + 1:lines.index("}")]
    body = [re.sub(r"\s*// 0x[0-9a-f]+.*$", "", line).strip() for line in body]
    return [line for line in body if line != 'asm("ud2");']


def _chains(text):
    """{statement: its pointer chain comment} of the statements that have one."""
    found = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(.*?)\s*// 0x[0-9a-f]+  (.+)", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def _head(text):
    return next(line for line in text.splitlines()
                if not line.startswith("//") and line != "{")


@requires_gcc
def test_the_head_names_the_arguments_by_the_guessed_signature(index):
    assert _head(_render(index, "multiply")) == "int32_t multiply(int32_t a1 /* rdi */)"
    assert _head(_render(index, "shift")) == "void shift(void *a1 /* rdi */)"
    assert _head(_render(index, "float_compare")) == \
        "int32_t float_compare(float a1 /* xmm0 */)"


@requires_gcc
def test_test_and_compare_give_signed_and_unsigned_conditions(index):
    body = _body(_render(index, "signed_test"))
    label = re.fullmatch(r"if \(a1 <= 0\) goto (loc_[0-9a-f]+);", body[0]).group(1)
    assert body[1:] == ["eax = 1;", "return eax;", f"{label}:", "eax = 0;", "return eax;"]
    assert _body(_render(index, "unsigned_compare")) == [
        "eax = 0;",
        "al = (uint32_t)a1 > 9;",
        "return eax;",
    ]
    assert "al = a1 > scale;" in _body(_render(index, "float_compare"))


@requires_gcc
def test_a_changed_register_leaves_the_condition_to_the_flags(index):
    body = _body(_render(index, "stale_flags"))
    assert body[0] == "edi = 7;"  # no longer the argument
    assert body[1].startswith("if (/* flags of cmp edi, 5 */) goto loc_")


@requires_gcc
def test_arithmetic_keeps_every_operand(index):
    assert _body(_render(index, "multiply")) == ["eax = a1 * 12;", "return eax;"]
    assert _body(_render(index, "shift"))[0] == "*(uint32_t *)a1 >>= 3;"


@requires_gcc
def test_a_tail_call_drops_the_frame_and_takes_its_argument(index):
    assert _body(_render(index, "tail")) == ["add_counter(a1 + 20);", "return;"]


@requires_gcc
def test_arguments_are_folded_past_statements_the_call_does_not_need(index):
    assert _body(_render(index, "report")) == [
        "ebx = a1;",
        "counter = ebx;",
        "eax = 0;",
        'printf("value %d", ebx);',
        "return;",
    ]


@requires_gcc
def test_compiled_code_reads_constants_and_calls_with_results(index):
    text = _render(index, "set_scale")
    assert "a1 *= 1.5f;" in _body(text)
    assert "scale = a1;" in _body(text)
    body = _body(_render(index, "main"))
    assert any(re.fullmatch(r"eax = signed_test\(a1\);", line) for line in body), body
    assert "add_counter" not in " ".join(body)  # behind the tail call in tail()
    assert any(line.startswith("tail(") for line in body), body
    assert _head(_render(index, "main")).startswith("int32_t main(")


@requires_gcc
def test_every_function_renders(index):
    for function in index.functions:
        text = render(index, function)
        assert text.endswith("}\n"), text


@requires_gcc
def test_a_value_used_once_is_folded_into_its_use(index):
    text = _render(index, "chain_read")
    statement = "eax = *(int32_t *)(*(uint64_t *)(game + 0x28) + 0x4);"
    assert _body(text) == [statement, "return eax;"]
    assert _chains(text) == {statement: "game → 0x28 → 0x4"}


@requires_gcc
def test_a_value_used_twice_stays_a_register(index):
    text = _render(index, "chain_twice")
    assert _body(text) == [
        "rax = game;",
        # Read once, but not folded into the add: the load into eax changes
        # rax, which its expression reads
        "ecx = *(int32_t *)(rax + 0x8);",
        "eax = *(int32_t *)(rax + 0xc);",
        "eax += ecx;",
        "return eax;",
    ]
    assert _chains(text) == {"ecx = *(int32_t *)(rax + 0x8);": "game → 0x8",
                             "eax = *(int32_t *)(rax + 0xc);": "game → 0xc"}


@requires_gcc
def test_the_head_explains_the_comments(index):
    assert "the address of its instruction (code, not data)" in _render(index, "multiply")
