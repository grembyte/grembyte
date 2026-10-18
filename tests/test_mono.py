"""C# methods of Mono JIT code, against a stand-in Mono runtime.

The target links ``libmonosgen-2.0.so``, which exports the part of Mono's
embedding API the agents use and contains a tiny "JIT": it emits two C#
methods into anonymous memory when the game first calls them or when
``mono_compile_method`` asks for them. ``FAKE_JIT_CONSTANT`` chooses how the
JIT encodes a constant before the gold write (``long``: movabs, 10 bytes;
``short``: mov, 5 bytes), so the write moves within its method between runs
like it can in real JIT code. ``FAKE_JIT_UPDATED`` gives the methods another
signature, as if the game had been updated.
"""

import os
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

typedef struct Class Class;
typedef struct { const char *name; int kind; int byref; Class *klass; } Type;
typedef struct { const char *name; const char *file; } Image;
struct Class { Type type; Image *image; int is_enum; Type *base; int is_valuetype; };
typedef struct { Class *klass; } Object;  /* a MonoObject starts with its vtable */

typedef struct {
    const char *name;
    const char *signature;
    uint32_t token;
    Class *klass;
    const char *short_name;
    uint32_t flags;
    Type *returns;
    int param_count;
    Type *params[8];
    const char *param_names[8];
    void *native;  /* "compiled" ahead: a C function */
    uint8_t *code;
    size_t size;
} Method;

#define ADD_GOLD 0x06000001u
#define TAKE 0x06000002u
#define ROLL_DAMAGE 0x06000004u
#define STATIC 0x10u

/* Assembly-CSharp: Game.Inventory, Game.Loot, Game.Combat and the enum
   Game.DamageType; mscorlib: System.Object. */
static Image game = { "Assembly-CSharp", "/game/Assembly-CSharp.dll" };
static Image corlib = { "mscorlib", "/game/mscorlib.dll" };
static Class classes[5];
static Type t_void = { "System.Void", 0x01 }, t_int = { "System.Int32", 0x08 },
    t_int_ref = { "System.Int32&", 0x08, 1 }, t_float = { "System.Single", 0x0c },
    t_string = { "System.String", 0x0e }, t_bool = { "System.Boolean", 0x02 },
    t_damage = { "Game.DamageType", 0x11, 0, &classes[4] };
static Class classes[5] = {
    { { "Game.Inventory", 0x12 }, &game },
    { { "Game.Loot", 0x12 }, &game },
    { { "System.Object", 0x1c }, &corlib },
    { { "Game.Combat", 0x12 }, &game },
    { { "Game.DamageType", 0x11 }, &game, 1, &t_int, 1 },
};

/* float RollDamage(int min, float scale, string name, bool crit,
                    DamageType type, out int roll), an instance method */
__attribute__((noipa)) float roll_damage(Object *self, int32_t min, float scale, void *name,
                                         int32_t crit, int32_t type, int32_t *roll) {
    (void)self; (void)name; (void)type;
    *roll = 42;
    return crit ? min * scale * 2 : min * scale;
}

static Method methods[] = {
    { "Game.Inventory:AddGold", "Game.Inventory:AddGold (int&,int)", ADD_GOLD,
      &classes[0], "AddGold", STATIC, &t_void, 2, { &t_int_ref, &t_int }, { "gold", "amount" } },
    { "Game.Loot:Take", "Game.Loot:Take (int&,int)", TAKE,
      &classes[1], "Take", STATIC, &t_void, 2, { &t_int_ref, &t_int }, { "gold", "amount" } },
    { "System.Object:ToString", "System.Object:ToString ()", 0x06000003u,
      &classes[2], "ToString", 0, &t_string },
    { "Game.Combat:RollDamage",
      "Game.Combat:RollDamage (int,single,string,bool,Game.DamageType,int&)", ROLL_DAMAGE,
      &classes[3], "RollDamage", 0, &t_float, 6,
      { &t_int, &t_float, &t_string, &t_bool, &t_damage, &t_int_ref },
      { "min", "scale", "name", "crit", "type", "roll" }, (void *)roll_damage },
};
#define COUNT(array) (sizeof array / sizeof *array)
static int root_domain, script_domain, thread;
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
    if (m->native) {
        m->code = m->native;
        m->size = 16;
        return m->code;
    }
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

/* Like Mono releasing its JIT code while the game exits */
void fake_unmap(void) { munmap(region, 0x1000); }

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
    return strcmp(name, game.file) == 0 ? &game : NULL;
}
const char *mono_image_get_name(Image *img) { return img->name; }
const char *mono_image_get_filename(Image *img) { return img->file; }
void *mono_get_method(void *img, uint32_t token, void *k) { (void)img; (void)k; return by_token(token); }
void *mono_method_get_class(Method *m) { return m->klass; }
void *mono_class_get_image(Class *k) { return k->image; }
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
    for (size_t i = 0; i < COUNT(methods); i++)
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

/* Catalog */
void mono_assembly_foreach(void (*callback)(void *, void *), void *data) {
    callback(&game, data);  /* an assembly stands for its image here */
    callback(&corlib, data);
}
void *mono_assembly_get_image(void *assembly) { return assembly; }
int mono_image_get_table_rows(Image *img, int table) {
    int rows = 1;  /* <Module> */
    for (size_t i = 0; table == 2 && i < COUNT(classes); i++)
        rows += classes[i].image == img;
    return rows;
}
void *mono_class_get(Image *img, uint32_t token) {
    uint32_t row = 1;
    for (size_t i = 0; i < COUNT(classes); i++)
        if (classes[i].image == img && ++row == (token & 0xffffff))
            return &classes[i];
    return NULL;
}
void *mono_class_get_type(Class *k) { return &k->type; }
char *mono_type_get_name(Type *type) { return strdup(type->name); }
void *mono_class_get_methods(Class *k, void **iterator) {
    size_t start = *iterator ? (size_t)((Method *)*iterator - methods) + 1 : 0;
    for (size_t i = start; i < COUNT(methods); i++)
        if (methods[i].klass == k)
            return *iterator = &methods[i];
    return NULL;
}
const char *mono_method_get_name(Method *m) { return m->short_name; }
uint32_t mono_method_get_flags(Method *m, uint32_t *implementation) {
    (void)implementation;
    return m->flags;
}
void *mono_method_signature(Method *m) { return m; }
void *mono_signature_get_return_type(Method *m) { return m->returns; }

/* Signatures and objects (method monitor) */
uint32_t mono_signature_get_param_count(Method *m) { return (uint32_t)m->param_count; }
void *mono_signature_get_params(Method *m, void **iterator) {
    int next = *iterator ? (int)((Type **)*iterator - m->params) + 1 : 0;
    if (next >= m->param_count)
        return NULL;
    *iterator = &m->params[next];
    return m->params[next];
}
void mono_method_get_param_names(Method *m, const char **names) {
    for (int i = 0; i < m->param_count; i++)
        names[i] = m->param_names[i];
}
int mono_signature_is_instance(Method *m) { return !(m->flags & STATIC); }
int mono_type_get_type(Type *type) { return type->kind; }
int mono_type_is_byref(Type *type) { return type->byref; }
void *mono_class_from_mono_type(Type *type) { return type->klass; }
int mono_class_is_enum(Class *k) { return k->is_enum; }
void *mono_class_enum_basetype(Class *k) { return k->base; }
int mono_class_is_valuetype(Class *k) { return k->is_valuetype; }
int mono_class_value_size(Class *k, uint32_t *align) { (void)k; (void)align; return 4; }
void *mono_object_get_class(Object *object) { return object->klass; }

/* The game's side: a Game.Combat object and a C# string */
void *fake_combat(void) {
    Object *object = malloc(sizeof *object);
    object->klass = &classes[3];
    return object;
}
void *fake_string(const char *ascii) {
    int32_t length = (int32_t)strlen(ascii);
    uint8_t *string = calloc(1, 0x14 + 2 * (size_t)length);
    memcpy(string + 0x10, &length, 4);
    for (int32_t i = 0; i < length; i++)
        string[0x14 + 2 * i] = (uint8_t)ascii[i];
    return string;
}
"""

_TARGET_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void fake_attach(void);
void fake_unmap(void);
void *fake_code(uint32_t token);
void *fake_combat(void);
void *fake_string(const char *ascii);
typedef float (*roll_fn)(void *self, int32_t min, float scale, void *name, int32_t crit,
                         int32_t type, int32_t *roll);

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
        if (strcmp(cmd, "quit") == 0) {
            fake_unmap();
            exit(0);
        }
        if (strcmp(cmd, "add") == 0)
            ((change_fn)fake_code(0x06000001u))(&g_game->player->gold, value);
        else if (strcmp(cmd, "loot") == 0)
            ((change_fn)fake_code(0x06000002u))(&g_game->player->gold, value);
        else if (strcmp(cmd, "roll") == 0) {
            int32_t roll = -1;
            float damage = ((roll_fn)fake_code(0x06000004u))(
                fake_combat(), value, 1.5f, fake_string("Fireball"), 1, 2, &roll);
            printf("%d %d\n", (int)damage, roll);
            fflush(stdout);
            continue;
        }
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
         "-o", str(tmp_path / "libmonosgen-2.0.so")],
        check=True,
    )
    binary = tmp_path / "target"
    subprocess.run(
        ["gcc", "-O1", "-fPIE", "-pie", str(tmp_path / "target.c"), "-o", str(binary),
         f"-L{tmp_path}", "-lmonosgen-2.0", f"-Wl,-rpath,{tmp_path}"],
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


@requires_target
def test_catalog_lists_game_methods_and_framework_on_request(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.core.mono import has_mono_runtime
    from hermes.core.mono_catalog import CatalogMethod, CatalogWorker

    target = _MonoTarget(_build(tmp_path))
    try:
        assert has_mono_runtime(target.pid)
        assert not has_mono_runtime(os.getpid())

        def read(assemblies=None):
            found, loaded, failed = [], {}, []
            worker = CatalogWorker(target.pid, assemblies)
            worker.assemblies_found.connect(found.extend)
            worker.assembly_loaded.connect(loaded.__setitem__)
            worker.catalog_failed.connect(failed.append)
            worker.start()
            _wait_for(qt_app, worker.isFinished)
            qt_app.processEvents()
            assert not failed
            return found, loaded

        found, loaded = read()
        assert [(a["name"], a["classes"]) for a in found] == [
            ("Assembly-CSharp", 4), ("mscorlib", 1)]
        # Framework assemblies are only read on request.
        assert loaded == {"Assembly-CSharp": [
            CatalogMethod("Assembly-CSharp", "Game.Inventory", "AddGold", "(int&,int)",
                          "System.Void", True, 0x06000001, "Game.Inventory:AddGold (int&,int)"),
            CatalogMethod("Assembly-CSharp", "Game.Loot", "Take", "(int&,int)",
                          "System.Void", True, 0x06000002, "Game.Loot:Take (int&,int)"),
            CatalogMethod("Assembly-CSharp", "Game.Combat", "RollDamage",
                          "(int,single,string,bool,Game.DamageType,int&)", "System.Single",
                          False, 0x06000004,
                          "Game.Combat:RollDamage (int,single,string,bool,Game.DamageType,int&)"),
        ]}
        assert loaded["Assembly-CSharp"][0].signature == "Game.Inventory:AddGold (int&,int)"
        _found, loaded = read(["mscorlib"])
        assert [m.signature for m in loaded["mscorlib"]] == ["System.Object:ToString ()"]
    finally:
        target.close()


@requires_target
def test_catalog_dialog_searches_methods(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.ui.widgets.catalog import CatalogDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = CatalogDialog(translations, target.pid)
    try:
        _wait_for(qt_app, lambda: dialog._worker is None)
        assert dialog.model.rowCount() == 3
        assert "3 von 3 Methoden" in dialog.status_label.text()

        dialog.search_input.setText("loot TAKE")
        dialog._apply_filter()
        assert dialog.model.rowCount() == 1
        dialog.view.selectRow(0)
        assert dialog.selected_method().signature == "Game.Loot:Take (int&,int)"
        assert "statisch" in dialog.details_label.text()

        dialog.search_input.setText("tostring")
        dialog._apply_filter()
        assert dialog.model.rowCount() == 0
        dialog.framework_check.setChecked(True)
        _wait_for(qt_app, lambda: dialog._worker is None)
        assert dialog.model.rowCount() == 1
        assert dialog.model.method_at(0).assembly == "mscorlib"
    finally:
        dialog.close()
        target.close()


@requires_target
def test_method_monitor_logs_arguments_caller_and_result(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.core.method_monitor import MethodMonitorWorker

    target = _MonoTarget(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    added, failed, calls = {}, {}, []
    worker.monitor_added.connect(added.__setitem__)
    worker.monitor_failed.connect(failed.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))
    identity = {"image": "Assembly-CSharp", "file": None, "token": 0x06000004,
                "method": "Game.Combat:RollDamage (int,single,string,bool,Game.DamageType,int&)"}
    try:
        worker.start()
        plain = worker.add_monitor(identity)
        _wait_for(qt_app, lambda: plain in added or plain in failed)
        assert not failed
        assert added[plain]["params"] == [
            "System.Int32 min", "System.Single scale", "System.String name",
            "System.Boolean crit", "Game.DamageType type", "System.Int32& roll"]
        assert added[plain]["undecoded"] == 0

        # crit doubles: 5 * 1.5 * 2
        assert target.send("roll", 5) == (15, 42)
        _wait_for(qt_app, lambda: calls)
        call = calls[0]
        assert call["self"].startswith("Game.Combat@0x")
        args = dict(call["args"])
        assert {key: args[key] for key in ("min", "scale", "name", "crit", "type")} == {
            "min": "5", "scale": "1.5", "name": '"Fireball"', "crit": "true",
            "type": "2 (Game.DamageType)"}
        # An out argument is shown before the method wrote it.
        assert args["roll"].startswith("&0x") and args["roll"].endswith("→ -1")
        assert call["caller"].startswith("target+0x")
        assert "result" not in call

        worker.remove_monitor(plain)
        with_result = worker.add_monitor(identity, returns=True)
        _wait_for(qt_app, lambda: with_result in added)
        calls.clear()
        assert target.send("roll", 2) == (6, 42)
        _wait_for(qt_app, lambda: calls)
        assert [call["result"] for call in calls] == ["6"]
        assert [call["monitorId"] for call in calls] == [with_result]
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_method_monitor_lets_the_game_free_its_code_and_exit(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.core.method_monitor import MethodMonitorWorker

    target = _MonoTarget(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    added, ended = {}, []
    worker.monitor_added.connect(added.__setitem__)
    worker.monitor_ended.connect(ended.append)
    try:
        worker.start()
        monitor_id = worker.add_monitor(
            {"image": "Assembly-CSharp", "file": None, "token": 0x06000001,
             "method": "Game.Inventory:AddGold (int&,int)"}, returns=True)
        _wait_for(qt_app, lambda: monitor_id in added)
        assert target.send("add", 1) == (101, 0)

        # Restoring the code after it was unmapped aborted the game on exit.
        target.process.stdin.write("quit 0\n")
        target.process.stdin.flush()
        assert target.process.wait(5) == 0
        _wait_for(qt_app, lambda: ended)
        assert ended == [monitor_id]
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
