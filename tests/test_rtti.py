"""Class names from C++ RTTI (see hermes/features/rtti.py)."""

import shutil
import struct
import subprocess

import pytest

from hermes.features.code_explorer import Image, Section, load_image
from hermes.features.rtti import itanium_class_name, msvc_class_name, rtti_names

_SOURCE = r"""
#include <cstdio>
namespace game {
struct Creature { virtual ~Creature() {} virtual int health() { return 1; } };
struct Sprite : Creature { int hp = 5; int health() override { return hp; }
                           virtual void walk() { puts("walk"); } };
template <typename T> struct Box { virtual T get() { return T(); } };
}
struct Other { virtual void other() { puts("o"); } };
struct Both : game::Sprite, Other { void other() override { puts("b"); } };
int main(int argc, char **) {
    game::Creature *c = argc > 1 ? (game::Creature *)new game::Sprite : new game::Creature;
    Other *o = new Both;
    o->other();
    game::Box<int> box;
    game::Box<int> *pointer = &box;
    return c->health() + pointer->get();
}
"""


@pytest.mark.parametrize("decorated, name", [
    (".?AVCGameSprite@@", "CGameSprite"),
    (".?AUInner@Outer@@", "Outer::Inner"),
    (".?AV?$vector@HV?$allocator@H@std@@@std@@", "vector<…>"),
    (".?AVHidden@?A0x1b2c@@", "`anonymous namespace'::Hidden"),
])
def test_msvc_names(decorated, name):
    assert msvc_class_name(decorated) == name


@pytest.mark.parametrize("mangled, name", [
    ("11CGameSprite", "CGameSprite"),
    ("N4game6SpriteE", "game::Sprite"),
    ("N4game3BoxIiEE", "game::Box<…>"),
    ("St9exception", "std::exception"),
    ("N4game3BoxILi3EEE", "game::Box<…>"),
    ("hello", None),
    ("3abcdef", None),
])
def test_itanium_names(mangled, name):
    assert itanium_class_name(mangled) == name


@pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")
@pytest.mark.parametrize("flags", [("-fPIE", "-pie"), ("-no-pie",)])
def test_a_stripped_elf_names_vtables_and_virtual_methods(tmp_path, flags):
    source = tmp_path / "game.cpp"
    source.write_text(_SOURCE)
    binary = tmp_path / "game"
    subprocess.run(["g++", "-O1", "-s", *flags, str(source), "-o", str(binary)], check=True)
    image = load_image(str(binary))
    names = set(image.names.values())
    assert {"game::Creature::vftable", "game::Sprite::vftable", "game::Box<…>::vftable",
            "Both::vftable", "Both::vftable@0x10"} <= names
    # Creature: two destructors, health; Sprite overrides health and adds walk
    assert {"game::Creature::vf2", "game::Sprite::vf2", "game::Sprite::vf3"} <= names
    for address, name in image.names.items():
        if name.endswith(("::vf2", "::vf3")):
            assert image.section_at(address).executable


def _msvc_image(wide):
    """.text at 0x1000 with three functions, .rdata at 0x2000 with a type
    descriptor, a complete object locator and the vtable after its slot."""
    base = 0x140000000 if wide else 0x400000
    width = 8 if wide else 4
    word = "<Q" if wide else "<I"
    text = bytes(0x40)
    rdata = bytearray(0x400)
    descriptor = 0x100
    rdata[descriptor + 2 * width:descriptor + 2 * width + 23] = b".?AVCGameSprite@Game@@\0"
    locator = 0x200
    if wide:
        struct.pack_into("<IiiIII", rdata, locator, 1, 0, 0, 0x2000 + descriptor, 0,
                         0x2000 + locator)
    else:
        struct.pack_into("<IiiII", rdata, locator, 0, 0, 0, base + 0x2000 + descriptor, 0)
    vtable = 0x300
    struct.pack_into(word, rdata, vtable - width, base + 0x2000 + locator)
    for slot, function in enumerate((0x1000, 0x1010, 0x1020)):
        struct.pack_into(word, rdata, vtable + slot * width, base + function)
    data = text + bytes(0x400 - len(text)) + bytes(rdata)
    image = Image("game.exe", "pe", 64 if wide else 32, base, data)
    image.sections = [Section(".text", base + 0x1000, 0x40, 0, 0x40, True, False),
                      Section(".rdata", base + 0x2000, 0x400, 0x400, 0x400, False, False)]
    return image, base


@pytest.mark.parametrize("wide", [True, False])
def test_msvc_rtti_names_the_vtable_and_its_methods(wide):
    image, base = _msvc_image(wide)
    image.names[base + 0x1010] = "exported"
    names = rtti_names(image)
    assert names[base + 0x2300] == "Game::CGameSprite::vftable"
    assert names[base + 0x1000] == "Game::CGameSprite::vf0"
    assert names[base + 0x1020] == "Game::CGameSprite::vf2"
    assert base + 0x1010 not in names  # an existing name stays


def test_a_method_in_different_slots_gets_no_name():
    image, base = _msvc_image(True)
    # A second vtable for the same class with the functions in another order
    data = bytearray(image.data)
    second = 0x400 + 0x380
    struct.pack_into("<QQQ", data, second - 8, base + 0x2200, base + 0x1020, base + 0x1000)
    image.data = bytes(data)
    names = rtti_names(image)
    assert base + 0x1000 not in names and base + 0x1020 not in names
    assert names[base + 0x1010] == "Game::CGameSprite::vf1"
