"""C# methods of Mono JIT code, against a stand-in Mono runtime.

The target links ``libmono-fake.so``, which exports the part of Mono's
embedding API the agents use and contains a tiny "JIT": it emits two C#
methods into anonymous memory when the game first calls them or when
``mono_compile_method`` asks for them. ``FAKE_JIT_CONSTANT`` chooses how the
JIT encodes a constant before the gold write (``long``: movabs, 10 bytes;
``short``: mov, 5 bytes), so the write moves within its method between runs
like it can in real JIT code. ``FAKE_JIT_UPDATED`` gives the methods another
signature, as if the game had been updated.
"""

import subprocess
import time

from hermes.core.pointer import PointerChain
from hermes.hooks.transforms import MultiplyTransform
from hermes.hooks.engine import HookSpec

from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    _watch_hits,
    fixture_qt_app,
    fixture_translations,
    requires_target,
)


_FAKE_MONO_SOURCE = r"""
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

typedef struct {
    const char *name;
    const char *signature;
    uint32_t token;
    uint8_t *code;
    size_t size;
} Method;

#define ADD_GOLD 0x06000001u
#define TAKE 0x06000002u

static Method methods[] = {
    { "Game.Inventory:AddGold", "Game.Inventory:AddGold (int&,int)", ADD_GOLD },
    { "Game.Loot:Take", "Game.Loot:Take (int&,int)", TAKE },
};
static int root_domain, script_domain, image, klass, thread;
static __thread void *current_domain;
static uint8_t *region;
static size_t used;

static Method *by_token(uint32_t token) {
    for (size_t i = 0; i < sizeof methods / sizeof *methods; i++)
        if (methods[i].token == token)
            return &methods[i];
    return NULL;
}

static uint8_t *compile(Method *m) {
    if (m->code)
        return m->code;
    if (!region) {
        region = mmap(NULL, 0x1000, PROT_READ | PROT_WRITE | PROT_EXEC,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        memset(region, 0xcc, 0x1000);
    }
    uint8_t *code = region + used;
    size_t n = 0;
    code[n++] = 0x55; code[n++] = 0x48; code[n++] = 0x89; code[n++] = 0xe5;
    if (m->token == ADD_GOLD) {
        const char *variant = getenv("FAKE_JIT_CONSTANT");
        if (variant && strcmp(variant, "short") == 0) {
            code[n++] = 0xb8;                               /* mov eax, imm32 */
            n += 4;
        } else {
            code[n++] = 0x48; code[n++] = 0xb8;             /* movabs rax, imm64 */
            n += 8;
        }
        code[n++] = 0x01; code[n++] = 0x37;                 /* add [rdi], esi */
        for (int i = 0; i < 4; i++)
            code[n++] = 0x90;
    } else {
        uint8_t *add = compile(by_token(ADD_GOLD));
        code[n++] = 0x48; code[n++] = 0xb8;                 /* movabs rax, add */
        memcpy(code + n, &add, 8); n += 8;
        code[n++] = 0xff; code[n++] = 0xd0;                 /* call rax */
    }
    code[n++] = 0x5d; code[n++] = 0xc3;
    m->code = code;
    m->size = n;
    used += (n + 0x3f) & ~(size_t)0x3f;
    return code;
}

__attribute__((constructor)) static void init(void) {
    if (getenv("FAKE_JIT_UPDATED"))
        for (size_t i = 0; i < sizeof methods / sizeof *methods; i++)
            methods[i].signature = "Game.Changed:Method ()";
}

/* The game's side: its thread runs scripts in the child domain. */
void fake_attach(void) { current_domain = &script_domain; }
void *fake_code(uint32_t token) { return compile(by_token(token)); }

void *mono_get_root_domain(void) { return &root_domain; }
void *mono_domain_get(void) { return current_domain; }
void mono_domain_foreach(void (*callback)(void *, void *), void *data) {
    callback(&root_domain, data);
    callback(&script_domain, data);
}
void *mono_thread_attach(void *domain) { current_domain = domain; return &thread; }
void mono_thread_detach(void *attached) { (void)attached; current_domain = NULL; }

void *mono_image_loaded(const char *name) {
    return strcmp(name, "/game/Assembly-CSharp.dll") == 0 ? &image : NULL;
}
const char *mono_image_get_name(void *img) { (void)img; return "Assembly-CSharp"; }
const char *mono_image_get_filename(void *img) { (void)img; return "/game/Assembly-CSharp.dll"; }
void *mono_get_method(void *img, uint32_t token, void *k) { (void)img; (void)k; return by_token(token); }
void *mono_method_get_class(void *m) { (void)m; return &klass; }
void *mono_class_get_image(void *k) { (void)k; return &image; }
uint32_t mono_method_get_token(void *m) { return ((Method *)m)->token; }

void *mono_compile_method(void *m) {
    /* Mono only compiles for threads it knows, in the scripts' domain. */
    if (current_domain != &script_domain)
        return NULL;
    return compile(m);
}

void *mono_jit_info_table_find(void *domain, void *address) {
    if (domain != &script_domain)
        return NULL;
    for (size_t i = 0; i < sizeof methods / sizeof *methods; i++)
        if (methods[i].code && (uint8_t *)address >= methods[i].code
            && (uint8_t *)address < methods[i].code + methods[i].size)
            return &methods[i];
    return NULL;
}
void *mono_jit_info_get_method(void *info) { return info; }
void *mono_jit_info_get_code_start(void *info) { return ((Method *)info)->code; }
int mono_jit_info_get_code_size(void *info) { return (int)((Method *)info)->size; }
char *mono_method_full_name(void *m, int signature) {
    return strdup(signature ? ((Method *)m)->signature : ((Method *)m)->name);
}
void mono_free(void *pointer) { free(pointer); }
"""

_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void fake_attach(void);
void *fake_code(uint32_t token);

typedef struct { int32_t hp; int32_t gold; } Player;
typedef struct { char pad[0x10]; Player *player; } Game;
Game *g_game;
typedef void (*change_fn)(int32_t *gold, int32_t amount);

int main(void) {
    fake_attach();
    g_game = calloc(1, sizeof(Game));
    g_game->player = calloc(1, sizeof(Player));
    g_game->player->gold = 100;
    printf("%d %p\n", getpid(), (void *)&g_game->player->gold);
    fflush(stdout);
    char cmd[16];
    int value;
    while (scanf("%15s %d", cmd, &value) == 2) {
        /* Calls compile a method the first time, like a JIT. */
        if (strcmp(cmd, "add") == 0)
            ((change_fn)fake_code(0x06000001u))(&g_game->player->gold, value);
        else if (strcmp(cmd, "loot") == 0)
            ((change_fn)fake_code(0x06000002u))(&g_game->player->gold, value);
        printf("%d 0\n", g_game->player->gold);
        fflush(stdout);
    }
    return 0;
}
"""


def _build(tmp_path):
    (tmp_path / "fake_mono.c").write_text(_FAKE_MONO_SOURCE)
    (tmp_path / "target.c").write_text(_TARGET_SOURCE)
    subprocess.run(
        ["gcc", "-shared", "-fPIC", str(tmp_path / "fake_mono.c"),
         "-o", str(tmp_path / "libmono-fake.so")],
        check=True,
    )
    binary = tmp_path / "target"
    subprocess.run(
        ["gcc", "-O1", "-fPIE", "-pie", str(tmp_path / "target.c"), "-o", str(binary),
         f"-L{tmp_path}", "-lmono-fake", f"-Wl,-rpath,{tmp_path}"],
        check=True,
    )
    return binary


class _MonoTarget(_Target):
    def __init__(self, binary, constant="long", updated=False):
        # pylint: disable=super-init-not-called,import-outside-toplevel
        import os

        env = {**os.environ, "FAKE_JIT_CONSTANT": constant}
        if updated:
            env["FAKE_JIT_UPDATED"] = "1"
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env
        )
        pid, address = self.process.stdout.readline().split()
        self.pid = int(pid)
        self.address = int(address, 16)


def _gold_chain(binary):
    """g_game -> player (+0x10) -> gold (+4)."""
    nm = subprocess.run(["nm", str(binary)], check=True, capture_output=True, text=True)
    g_game = next(int(line.split()[0], 16) for line in nm.stdout.splitlines()
                  if line.endswith(" g_game"))
    return PointerChain("target", g_game, (0x10, 0x4))


@requires_target
def test_write_watch_names_the_csharp_method(tmp_path):
    target = _MonoTarget(_build(tmp_path))
    try:
        result, hits = _watch_hits(target, "add", reads=False)
        assert result == (101, 0)
        # movabs (10 bytes) + add [rdi], esi (2 bytes) after the prologue
        assert [hit.get("method") for hit in hits] == ["Game.Inventory:AddGold+0x10"]
        # Native code has no method.
        assert not any(hit.get("method") for hit in _watch_hits(target, "add", True)[1]
                       if not hit["write"])
    finally:
        target.close()


@requires_target
def test_hook_names_its_site_and_callers(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.ui.widgets.hooks import HookPanel

    target = _MonoTarget(_build(tmp_path))
    panel = HookPanel(translations)
    debug = []
    try:
        _result, hits = _watch_hits(target, "add", reads=False)
        trap_pc = int(hits[0]["pc"], 16)
        panel.set_process(target.pid)
        panel.add_hook(
            HookSpec(target.address, "int32", MultiplyTransform(10), trap_pc=trap_pc),
            hits[0]["method"],
        )
        _wait_for(qt_app, lambda: panel.table.item(0, 4).text() == "Aktiv")
        worker = panel._worker
        hook_id = next(iter(panel._hooks))
        worker.hook_debug.connect(lambda events, _dropped: debug.extend(events))
        worker.set_debug(hook_id, True)
        time.sleep(0.2)

        assert target.send("loot", 1) == (101 + 10, 0)
        assert target.send("add", 1) == (111 + 10, 0)
        _wait_for(qt_app, lambda: len(debug) == 2)

        # The first write on a game thread names the site in the panel.
        assert panel.table.item(0, 2).text().startswith("Game.Inventory:AddGold+0xe")
        # JIT callers by their method, native ones by module + offset.
        assert debug[0]["caller"] == "Game.Loot:Take+0x10"
        assert debug[1]["caller"].startswith("target+0x")
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()


@requires_target
def test_hook_is_found_by_its_method_after_a_restart(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access,too-many-locals
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.hooks import HookRepository
    from hermes.ui.widgets.hooks import HookPanel

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    binary = _build(tmp_path)
    chain = _gold_chain(binary)
    process_id = repository.get_or_create_process("target", str(binary))
    status = lambda panel: panel.table.item(0, 4).text()  # pylint: disable=unnecessary-lambda-assignment

    def run(constant, updated=False):
        return _MonoTarget(binary, constant, updated), HookPanel(translations, repository=repository)

    # First run: the hook is created from a write watch hit and saved; its
    # first write tells the method, which is saved as well.
    target, panel = run("long")
    try:
        _result, hits = _watch_hits(target, "add", reads=False)
        panel.set_process(target.pid, process_id)
        panel.add_hook(
            HookSpec(target.address, "int32", MultiplyTransform(10),
                     trap_pc=int(hits[0]["pc"], 16), pointer=chain, label="Gold"),
            hits[0]["method"],
        )
        _wait_for(qt_app, lambda: status(panel) == "Aktiv")
        assert target.send("add", 1) == (101 + 10, 0)
        _wait_for(qt_app, lambda: repository.list_hooks(process_id)[0].managed is not None)
        managed = repository.list_hooks(process_id)[0].managed
        assert managed == {
            "image": "Assembly-CSharp", "file": "/game/Assembly-CSharp.dll",
            "token": 0x06000001, "method": "Game.Inventory:AddGold (int&,int)",
            "offset": 0xe,
        }
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()

    # Second run: the game has not run the method yet and its JIT encodes the
    # constant shorter. The hook is installed right away, without training.
    target, panel = run("short")
    try:
        panel.set_process(target.pid, process_id)
        assert panel.table.item(0, 2).text() == "Game.Inventory:AddGold+0xe"
        _wait_for(qt_app, lambda: status(panel) == "Aktiv")
        assert target.send("add", 1) == (100 + 10, 0)
        _wait_for(qt_app, lambda: panel.table.item(0, 2).text().startswith(
            "Game.Inventory:AddGold+0x9"))
        assert target.send("loot", 2) == (110 + 20, 0)
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()

    # After an update the method is gone: the hook trains instead.
    target, panel = run("short", updated=True)
    try:
        panel.set_process(target.pid, process_id)
        _wait_for(qt_app, lambda: status(panel) == "Training")
        assert "C#-Methode" in panel.status_label.text()
        assert target.send("add", 1) == (101, 0)
        _wait_for(qt_app, lambda: status(panel) == "Aktiv")
        assert target.send("add", 1) == (101 + 10, 0)
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()
        engine.dispose()
