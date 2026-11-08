"""The code explorer: strings, references, functions and patterns of a module's file
(see hermes/features/code_explorer.py)."""

import shutil
import subprocess

import pytest

from hermes.core import messages
from hermes.features.code_explorer import (
    CALL,
    DATA,
    POINTER,
    CodeIndex,
    load_image,
    parse_location,
)
from hermes.features.pseudocode import render as render_pseudocode

from tests.test_hooks import fixture_qt_app  # pylint: disable=unused-import


# A game's code in small: a message used by one function, called by two others,
# and a table of pointers to strings (like a list of command names)
_SOURCE = r"""
#include <stdio.h>

int counter;
const char *const commands[] = { "hermes_cmd_heal", "hermes_cmd_kill" };

__attribute__((noinline)) int use_message(int value) {
    puts("Hermes marker: available only in debug mode.");
    counter += value;
    return counter;
}

__attribute__((noinline)) int first_caller(int value) { return use_message(value) + 1; }
__attribute__((noinline)) int second_caller(int value) { return use_message(value * 3) - 2; }

float scale;

__attribute__((noinline)) void set_scale(float value) { scale = value * 2.0f; }

int main(int argc, char **argv) {
    (void)argv;
    puts(commands[argc & 1]);
    set_scale((float)argc * 1.5f);
    return first_caller(argc) + second_caller(argc);
}
"""

requires_gcc = pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
requires_mingw = pytest.mark.skipif(shutil.which("x86_64-w64-mingw32-gcc") is None,
                                    reason="needs x86_64-w64-mingw32-gcc")


def _compile(tmp_path, compiler, output, *flags):
    source = tmp_path / "game.c"
    source.write_text(_SOURCE)
    binary = tmp_path / output
    subprocess.run([compiler, "-O1", *flags, str(source), "-o", str(binary)], check=True)
    return CodeIndex(load_image(str(binary)))


def _string(index, text):
    found = [address for address, string, _kind in index.strings if string == text]
    assert len(found) == 1, (text, found)
    return found[0]


def _check_index(index, symbols, rip=True):
    """What both formats must give for _SOURCE."""
    message = _string(index, "Hermes marker: available only in debug mode.")
    refs = index.references(message)
    assert [ref.kind for ref in refs] == [DATA]
    use = index.function_at(refs[0].address)
    assert use is not None
    use = index.owner(use)
    if symbols:
        assert index.name(use.start) == "use_message"
        assert index.where(refs[0].address).startswith("use_message")
    # Called by both callers (the compiler may also inline nothing else)
    callers = {index.owner(index.function_at(ref.address)).start
               for ref in index.references(use.start) if ref.kind == CALL}
    assert len(callers) == 2
    # The command names are reached through a table of pointers
    heal = _string(index, "hermes_cmd_heal")
    assert POINTER in {ref.kind for ref in index.references(heal)}

    # The function view names the string the instruction loads, and the
    # imported function it calls (through a PLT stub or an import slot)
    lines = index.disassemble(use)
    assert any(line.comment == '"Hermes marker: available only in debug mode."'
               for line in lines)
    assert any(line.comment.split("!")[-1] == "puts" for line in lines), \
        [line.comment for line in lines]
    # A pattern for the function is unique, its RIP-relative parts wildcards
    pattern = index.pattern(use.start)
    assert pattern.matches == 1
    assert index.count_matches(pattern.text.split()) == 1
    loading = next(line for line in lines if line.target == message)
    assert "??" in index.pattern(loading.address).text.split()
    script = index.script(loading.address)
    # RIP-relative (position-independent) or an absolute address in the instruction
    assert "n.scan(" in script
    assert ("n.rip(at, " if rip else "readU32()") in script
    assert '"Hermes marker' in script
    assert "n.fn(at" in index.script(use.start)
    # The pseudocode names the string and the import, the argument by the convention
    pseudocode = render_pseudocode(index, use)
    assert 'puts("Hermes marker: available only in debug mode.");' in pseudocode
    assert ("(int32_t a1 /* rdi */)" if index.image.format == "elf"
            else "(int32_t a1 /* rcx */)") in pseudocode

    # Signatures guessed from the code, by the platform's calling convention
    linux = index.image.format == "elf"
    signature = index.signature(use.start)
    assert signature.convention == ("sysv" if linux else "win64")
    assert [(p.register, p.type) for p in signature.parameters] == \
        [("rdi" if linux else "rcx", "int")]
    assert (signature.returns, signature.returns_known) == ("int", True)  # callers add 1
    assert 'n.fn(at, "int", ["int"])' in index.script(use.start)
    if symbols:
        scale = parse_location("set_scale", index.image)
        signature = index.signature(scale)
        assert [(p.register, p.type) for p in signature.parameters] == [("xmm0", "float")]
        assert signature.returns == "void"
        assert signature.text("set_scale") == "void set_scale(float xmm0)"
    assert index.signature(use.start + 1) is None  # no function start
    return use


@requires_gcc
def test_an_elf_module_gives_strings_references_functions_and_patterns(tmp_path):
    index = _compile(tmp_path, "gcc", "game", "-fPIE", "-pie")
    image = index.image
    assert (image.format, image.bits, image.base) == ("elf", 64, 0)  # position-independent
    use = _check_index(index, symbols=True)

    assert parse_location("use_message", image) == use.start
    assert parse_location(f"game+0x{use.start:x}", image, module_name="game") == use.start
    assert parse_location(f"0x{use.start:x}", image) == use.start
    # A runtime address of the module loaded at 0x555555554000
    assert parse_location(f"0x{0x555555554000 + use.start:x}", image,
                          module_base=0x555555554000) == use.start
    assert parse_location("no_such_symbol", image) is None


@requires_gcc
def test_a_module_linked_at_a_fixed_address_finds_absolute_pointers(tmp_path):
    index = _compile(tmp_path, "gcc", "game_fixed", "-fno-pie", "-no-pie")
    assert index.image.base >= 0x400000
    # The message's address is in the instruction itself
    _check_index(index, symbols=True, rip=False)


@requires_mingw
def test_a_pe_module_reads_its_exception_directory(tmp_path):
    index = _compile(tmp_path, "x86_64-w64-mingw32-gcc", "game.exe")
    image = index.image
    assert (image.format, image.bits) == ("pe", 64)
    assert image.base == 0x140000000
    # Functions come from .pdata, which MinGW writes for every function
    assert len(image.functions) > 10
    use = _check_index(index, symbols=False)
    assert use.bounded


def test_other_files_are_refused(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("no program")
    with pytest.raises(messages.LocalizedValueError) as raised:
        load_image(str(path))
    assert "notes.txt" in str(raised.value)


@requires_gcc
def test_the_window_leads_from_a_string_to_its_function_and_back(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel,protected-access
    import time

    from PySide6.QtGui import QGuiApplication  # pylint: disable=import-error,no-name-in-module

    from hermes.ui.localization import load_translations
    from hermes.ui.widgets.code_explorer import CodeExplorerDialog, ModuleChoice

    source = tmp_path / "game.c"
    source.write_text(_SOURCE)
    binary = tmp_path / "game"
    subprocess.run(["gcc", "-O1", "-fPIE", "-pie", str(source), "-o", str(binary)], check=True)
    translations = load_translations("en")
    dialog = CodeExplorerDialog(translations)  # no game attached
    try:
        assert dialog.status_label.text() == translations["code.no_process"]
        assert not dialog.monitor_button.isEnabled()
        dialog._load(ModuleChoice("game", str(binary), None))
        deadline = time.monotonic() + 10
        while dialog.index is None and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.01)
        assert dialog.index is not None, dialog.status_label.text()

        # A named global: its module offset, where a pointer chain would start
        dialog.goto_input.setText("counter")
        dialog._goto()
        counter = parse_location("counter", dialog.index.image)
        assert f"(counter, game+0x{counter - dialog.index.image.base:x})" in \
            dialog.refs_label.text()

        dialog.string_filter.setText("marker debug")
        assert dialog.string_model.rowCount() == 1
        dialog._string_activated(dialog.string_model.index(0, 0))
        assert dialog.ref_model.rowCount() == 1
        dialog._reference_activated(dialog.ref_model.index(0, 0))
        assert dialog.function_label.text().startswith("use_message:")
        # The pseudocode is rendered when its tab is shown, at the selected instruction
        loading = dialog._selected_address()
        assert dialog.pseudocode_view.toPlainText() == ""
        dialog.function_tabs.setCurrentWidget(dialog.pseudocode_page)
        assert "int32_t use_message(" in dialog.pseudocode_view.toPlainText()
        assert "puts(" in dialog.pseudocode_view.textCursor().block().text()
        # Finding a text moves to it, and selects its instruction in the assembler
        dialog.find_input.setText("return")
        assert "return" in dialog.pseudocode_view.textCursor().block().text()
        returned = dialog._selected_line()
        assert returned.address > loading
        dialog.find_input.setText("no such text")
        assert dialog.find_status.text() == "Not found"
        dialog.find_input.clear()
        dialog.function_tabs.setCurrentWidget(dialog.line_view)
        dialog._show_function(loading, loading, remember=False)
        selected = dialog._selected_address()
        assert dialog.index.where(selected).startswith("use_message+")

        dialog._copy_pattern()
        assert "??" in QGuiApplication.clipboard().text()
        assert "unique" in dialog.status_label.text()
        # Without a process there is no runtime address to monitor
        assert not dialog.monitor_button.isEnabled()

        dialog._show_callers()
        assert dialog.ref_model.rowCount() == 2
        dialog._reference_activated(dialog.ref_model.index(0, 0))
        assert dialog.function_label.text().split(":")[0] in ("first_caller", "second_caller")
        dialog._back()
        assert dialog.function_label.text().startswith("use_message:")

        dialog.goto_input.setText("main")
        dialog._goto()
        assert dialog.function_label.text().startswith("main:")
    finally:
        dialog.close()
