"""IL2CPP games, against a stand-in GameAssembly.so.

The stand-in exports the part of IL2CPP's API the adapter uses, over two
images: ``mscorlib.dll`` (framework) and ``Assembly-CSharp.dll`` with
``Game.Hero``. Its methods are real functions in the library, as IL2CPP
compiles them: ``Loot`` calls ``AddGold``; ``Tick`` and ``Idle`` share one
function, as identical functions the linker folds do; ``Attack`` is
abstract and points at a stub like IL2CPP's; ``Game.Bag<T>`` is a generic
class definition. ``Rename`` and ``Place`` are instance methods of the
player object (``Place`` returns a struct through a hidden pointer). A
program linked against the library calls them on commands (``tick``,
``loot N``, ``rename N``, ``place N``) and answers with the gold.
"""

import json
import subprocess
import time

from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    fixture_qt_app,
    requires_target,
)


_GAME_ASSEMBLY_SOURCE = r"""
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct Class Class;
typedef struct Image { const char *name; Class **classes; size_t count; } Image;
typedef struct Type { const char *name; int kind, byref; Class *klass; } Type;
typedef struct Field { const char *name; Type *type; Class *parent; int offset, flags; } Field;
typedef struct Method Method;
/* Laid out like Il2CppClass where the adapter looks: the image first, the
   field ``klass`` pointing at the class itself, then the fields and, seven
   pointers later, the static data. */
struct Class {
    Image *image;
    const char *name;
    Type type;
    Class *element_class;
    Class *klass;
    Field *fields;
    void *unused[6];
    void *static_fields;
    Class *parent;
    int generic, valuetype, size;
    Method **methods;
    size_t count, field_count;
};
struct Method {
    void *pointer;  /* first, as in IL2CPP's MethodInfo */
    const char *name;
    Class *klass;
    uint32_t token, flags, iflags;
    uint32_t param_count;
    Type *params[2];
    const char *param_names[2];
    Type *returns;
};
typedef struct Assembly { Image *image; } Assembly;
typedef struct String { Class *klass; void *monitor; int32_t length; uint16_t chars[16]; } String;
typedef struct HeroObject { Class *klass; void *monitor; int32_t gold; String *name; } HeroObject;
typedef struct Vec { double x, y, z; } Vec;

static Class hero, bag, string_class, object_class, vec_class;
static Type t_void = {"System.Void", 0x01}, t_int = {"System.Int32", 0x08};
static Type t_float = {"System.Single", 0x0c}, t_string = {"System.String", 0x0e};
static Type t_param = {"T", 0x13}, t_vec = {"Game.Vec", 0x11, 0, &vec_class};
static Type t_hero = {"Game.Hero", 0x12, 0, &hero};

int g_gold;
static int g_instances = 7;
static String names[2];
static HeroObject player;

__attribute__((noinline)) void Hero_AddGold(int amount) { g_gold += amount; }
__attribute__((noinline)) void Hero_Tick(float delta) { (void)delta; __asm__ volatile(""); }
__attribute__((noinline)) void Hero_Loot(int amount) {
    __asm__ volatile("");
    Hero_AddGold(amount);
    __asm__ volatile("");
}
__attribute__((noinline)) void Unresolved(void) { abort(); }
__attribute__((noinline)) void Bag_Add(void *item) { (void)item; __asm__ volatile(""); }
__attribute__((noinline)) int String_get_Length(String *s) { return s->length; }
__attribute__((noinline)) void Hero_Rename(HeroObject *self, String *name, void *method) {
    (void)method;
    self->name = name;
}
/* Returns a struct through a hidden pointer, which comes before ``self`` */
__attribute__((noinline)) Vec Hero_Place(HeroObject *self, int steps, void *method) {
    (void)method;
    Vec v = {self->gold, steps, 0};
    return v;
}

#define STATIC 0x10
#define PUBLIC 0x06
#define ABSTRACT 0x400
static Method m_add = {Hero_AddGold, "AddGold", &hero, 0x06000001, PUBLIC | STATIC, 0, 1,
                       {&t_int}, {"amount"}, &t_void};
static Method m_tick = {Hero_Tick, "Tick", &hero, 0x06000002, PUBLIC, 0, 1,
                        {&t_float}, {"delta"}, &t_void};
static Method m_idle = {Hero_Tick, "Idle", &hero, 0x06000003, PUBLIC, 0, 1,
                        {&t_float}, {"delta"}, &t_void};
static Method m_loot = {Hero_Loot, "Loot", &hero, 0x06000004, PUBLIC | STATIC, 0, 1,
                        {&t_int}, {"amount"}, &t_void};
static Method m_attack = {Unresolved, "Attack", &hero, 0x06000005, PUBLIC | ABSTRACT, 0, 0,
                          {0}, {0}, &t_void};
static Method m_rename = {Hero_Rename, "Rename", &hero, 0x06000007, PUBLIC, 0, 1,
                          {&t_string}, {"name"}, &t_void};
static Method m_place = {Hero_Place, "Place", &hero, 0x06000008, PUBLIC, 0, 1,
                         {&t_int}, {"steps"}, &t_vec};
static Method m_bag_add = {Bag_Add, "Add", &bag, 0x06000006, PUBLIC, 0, 1,
                           {&t_param}, {"item"}, &t_void};
static Method m_length = {String_get_Length, "get_Length", &string_class, 0x06000001, PUBLIC,
                          0, 0, {0}, {0}, &t_int};
static Method *hero_methods[] = {&m_add, &m_tick, &m_idle, &m_loot, &m_attack, &m_rename,
                                 &m_place};
static Method *bag_methods[] = {&m_bag_add};
static Method *string_methods[] = {&m_length};
static Field hero_fields[] = {
    {"Gold", &t_int, &hero, 0x10, 0x06},
    {"Name", &t_string, &hero, 0x18, 0x06},
    {"Instances", &t_int, &hero, 0, 0x16},
};
static Field string_fields[] = {{"m_stringLength", &t_int, &string_class, 0x10, 0x01}};
static Image game, corlib;
static Class object_class = {.image = &corlib, .name = "Object",
    .type = {"System.Object", 0x1c, 0, &object_class}, .element_class = &object_class,
    .klass = &object_class, .size = 16};
static Class hero = {.image = &game, .name = "Hero", .type = {"Game.Hero", 0x12, 0, &hero},
    .element_class = &hero, .klass = &hero, .fields = hero_fields,
    .static_fields = &g_instances, .parent = &object_class, .size = 0x20,
    .methods = hero_methods, .count = 7, .field_count = 3};
static Class bag = {.image = &game, .name = "Bag", .type = {"Game.Bag<T>", 0x12, 0, &bag},
    .element_class = &bag, .klass = &bag, .parent = &object_class, .generic = 1,
    .methods = bag_methods, .count = 1};
static Class vec_class = {.image = &game, .name = "Vec", .type = {"Game.Vec", 0x11, 0, &vec_class},
    .element_class = &vec_class, .klass = &vec_class, .valuetype = 1, .size = 16 + 24};
static Class string_class = {.image = &corlib, .name = "String",
    .type = {"System.String", 0x0e, 0, &string_class}, .element_class = &string_class,
    .klass = &string_class, .fields = string_fields, .parent = &object_class,
    .methods = string_methods, .count = 1, .field_count = 1};
static Class *game_classes[] = {&hero, &bag, &vec_class};
static Class *corlib_classes[] = {&object_class, &string_class};
static Image game = {"Assembly-CSharp.dll", game_classes, 3};
static Image corlib = {"mscorlib.dll", corlib_classes, 2};
static Assembly a_corlib = {&corlib}, a_game = {&game};
static Assembly *assemblies[] = {&a_corlib, &a_game};
static int domain, thread;

static void make_name(String *s, const char *text) {
    s->klass = &string_class;
    s->length = (int32_t)strlen(text);
    for (int i = 0; i < s->length; i++)
        s->chars[i] = (uint16_t)text[i];
}

void Game_Rename(int which) {
    if (player.klass == NULL) {
        player.klass = &hero;
        player.gold = 30;
        make_name(&names[0], "Miranda");
        make_name(&names[1], "Young Wolf");
    }
    Hero_Rename(&player, &names[which], &m_rename);
}
int Game_Place(int steps) {
    Game_Rename(0);
    return (int)Hero_Place(&player, steps, &m_place).y;
}

void *il2cpp_get_corlib(void) { return &corlib; }
void *il2cpp_domain_get(void) { return &domain; }
Assembly **il2cpp_domain_get_assemblies(void *d, size_t *size) { (void)d; *size = 2; return assemblies; }
Image *il2cpp_assembly_get_image(Assembly *a) { return a->image; }
const char *il2cpp_image_get_name(Image *i) { return i->name; }
size_t il2cpp_image_get_class_count(Image *i) { return i->count; }
Class *il2cpp_image_get_class(Image *i, size_t n) { return n < i->count ? i->classes[n] : NULL; }
Type *il2cpp_class_get_type(Class *k) { return &k->type; }
int il2cpp_class_is_generic(Class *k) { return k->generic; }
Image *il2cpp_class_get_image(Class *k) { return k->image; }
Method *il2cpp_class_get_methods(Class *k, void **iter) {
    size_t next = (size_t)*iter;
    if (next >= k->count)
        return NULL;
    *iter = (void *)(next + 1);
    return k->methods[next];
}
char *il2cpp_type_get_name(Type *t) { return strdup(t->name); }
void il2cpp_free(void *p) { free(p); }
const char *il2cpp_method_get_name(Method *m) { return m->name; }
Class *il2cpp_method_get_class(Method *m) { return m->klass; }
uint32_t il2cpp_method_get_token(Method *m) { return m->token; }
uint32_t il2cpp_method_get_flags(Method *m, uint32_t *iflags) {
    if (iflags)
        *iflags = m->iflags;
    return m->flags;
}
uint32_t il2cpp_method_get_param_count(Method *m) { return m->param_count; }
Type *il2cpp_method_get_param(Method *m, uint32_t i) { return m->params[i]; }
const char *il2cpp_method_get_param_name(Method *m, uint32_t i) { return m->param_names[i]; }
Type *il2cpp_method_get_return_type(Method *m) { return m->returns; }

/* What the method monitor and the object inspector use */
void *il2cpp_thread_attach(void *d) { (void)d; return &thread; }
void il2cpp_thread_detach(void *t) { (void)t; }
int il2cpp_type_get_type(Type *t) { return t->kind; }
int il2cpp_type_is_byref(Type *t) { return t->byref; }
Class *il2cpp_class_from_type(Type *t) { return t->klass; }
int il2cpp_class_is_enum(Class *k) { (void)k; return 0; }
Type *il2cpp_class_enum_basetype(Class *k) { (void)k; return NULL; }
int il2cpp_class_is_valuetype(Class *k) { return k->valuetype; }
int il2cpp_class_value_size(Class *k, uint32_t *align) { (void)align; return k->size - 16; }
int il2cpp_method_is_instance(Method *m) { return !(m->flags & STATIC); }
Class *il2cpp_class_get_parent(Class *k) { return k->parent; }
Field *il2cpp_class_get_fields(Class *k, void **iter) {
    size_t next = (size_t)*iter;
    if (next >= k->field_count)
        return NULL;
    *iter = (void *)(next + 1);
    return &k->fields[next];
}
const char *il2cpp_field_get_name(Field *f) { return f->name; }
Type *il2cpp_field_get_type(Field *f) { return f->type; }
size_t il2cpp_field_get_offset(Field *f) { return (size_t)f->offset; }
int il2cpp_field_get_flags(Field *f) { return f->flags; }
int il2cpp_class_get_rank(Class *k) { (void)k; return 0; }
Class *il2cpp_class_get_element_class(Class *k) { return k->element_class; }
int il2cpp_class_array_element_size(Class *k) { (void)k; return 0; }
Method *il2cpp_class_get_method_from_name(Class *k, const char *name, int count) {
    for (size_t i = 0; i < k->count; i++)
        if (strcmp(k->methods[i]->name, name) == 0 && (int)k->methods[i]->param_count == count)
            return k->methods[i];
    return NULL;
}
Method *il2cpp_object_get_virtual_method(void *o, Method *m) { (void)o; return m; }
void *il2cpp_runtime_invoke(Method *m, void *obj, void **params, void **exc) {
    (void)params;
    *exc = NULL;
    return ((void *(*)(void *, Method *))m->pointer)(obj, m);
}
"""

_GAME_SOURCE = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>

extern int g_gold;
void Hero_Tick(float delta);
void Hero_Loot(int amount);
void Game_Rename(int which);
int Game_Place(int steps);

int main(void) {
    printf("%d %p\n", getpid(), (void *)&g_gold);
    fflush(stdout);
    char cmd[32];
    int value;
    while (scanf("%31s %d", cmd, &value) == 2) {
        int answer = 0;
        if (strcmp(cmd, "tick") == 0)
            Hero_Tick(0.5f);
        else if (strcmp(cmd, "loot") == 0)
            Hero_Loot(value);
        else if (strcmp(cmd, "rename") == 0)
            Game_Rename(value);
        else if (strcmp(cmd, "place") == 0)
            answer = Game_Place(value);
        printf("%d %d\n", g_gold, answer);
        fflush(stdout);
    }
    return 0;
}
"""


def _build(tmp_path):
    (tmp_path / "game_assembly.c").write_text(_GAME_ASSEMBLY_SOURCE)
    (tmp_path / "game.c").write_text(_GAME_SOURCE)
    library = tmp_path / "GameAssembly.so"
    subprocess.run(["gcc", "-O0", "-shared", "-fPIC", str(tmp_path / "game_assembly.c"),
                    "-o", str(library)], check=True)
    binary = tmp_path / "fakegame"
    subprocess.run(["gcc", "-O0", str(tmp_path / "game.c"), str(library),
                    "-Wl,-rpath,$ORIGIN", "-o", str(binary)], check=True)
    return binary


def _run(qt_app, worker, signal):
    found, failed = [], []
    signal.connect(lambda *args: found.append(args))
    worker.catalog_failed.connect(failed.append)
    worker.start()
    _wait_for(qt_app, worker.isFinished)
    qt_app.processEvents()
    assert not failed
    return found


@requires_target
def test_catalog_lists_images_classes_and_methods(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.engines.registry import detect_engine
    from hermes.features.catalog import CatalogWorker

    target = _Target(_build(tmp_path))
    try:
        engine = detect_engine(target.pid)
        assert engine.id == "il2cpp"

        worker = CatalogWorker(target.pid)
        loaded = {}
        worker.assembly_loaded.connect(loaded.__setitem__)
        found = _run(qt_app, worker, worker.assemblies_found)[0][0]
        # Named without .dll, as Mono names assemblies; the build of
        # GameAssembly stands for their MVID.
        assert [(a["name"], a["file"], a["classes"]) for a in found] == [
            ("mscorlib", "mscorlib.dll", 2), ("Assembly-CSharp", "Assembly-CSharp.dll", 3)]
        assert found[0]["mvid"] and found[0]["mvid"] != found[1]["mvid"]
        # The framework is left out unless asked for.
        assert sorted(loaded) == ["Assembly-CSharp"]
        methods = {m.signature: m for m in loaded["Assembly-CSharp"]}
        assert sorted(methods) == [
            "Game.Bag`1:Add (T)", "Game.Hero:AddGold (int)", "Game.Hero:Attack ()",
            "Game.Hero:Idle (single)", "Game.Hero:Loot (int)", "Game.Hero:Place (int)",
            "Game.Hero:Rename (string)", "Game.Hero:Tick (single)"]
        add = methods["Game.Hero:AddGold (int)"]
        assert (add.class_name, add.name, add.parameters, add.return_type, add.static,
                add.token, add.engine, add.argument_names) == (
            "Game.Hero", "AddGold", "(int)", "System.Void", True, 0x06000001, "il2cpp",
            ("amount",))
        assert methods["Game.Bag`1:Add (T)"].class_name == "Game.Bag<T>"
    finally:
        target.close()


@requires_target
def test_trace_counts_methods_and_names_their_callers(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.trace import NEW, SAME, TraceWorker, analyze, recording_calls

    target = _Target(_build(tmp_path))
    worker = TraceWorker(target.pid)
    planned, started, began, recorded, failed = [], [], [], [], []
    worker.planned.connect(planned.append)
    worker.started.connect(lambda methods, info: started.append((methods, info)))
    worker.began.connect(began.append)
    worker.recorded.connect(recorded.append)
    worker.command_failed.connect(lambda action, message: failed.append((action, message)))
    worker.worker_failed.connect(lambda message: failed.append(("worker", message)))

    def record(phase, *commands):
        worker.begin(phase)
        _wait_for(qt_app, lambda: phase in began or failed)
        began.clear()
        for command, value in commands:
            target.send(command, value)
        count = len(recorded)
        worker.finish()
        _wait_for(qt_app, lambda: len(recorded) > count or failed)
        assert not failed
        return recorded[-1]

    try:
        worker.start()
        worker.plan([])
        _wait_for(qt_app, lambda: planned or failed)
        assert not failed
        # Attack is abstract, Bag`1:Add belongs to a generic definition.
        assert planned[0] == {"methods": 6, "skipped": {"abstract": 1, "native": 0, "generic": 1},
                              "assemblies": ["Assembly-CSharp"]}

        worker.trace([])
        _wait_for(qt_app, lambda: started or failed)
        assert not failed
        methods, info = started[0]
        assert (info["notCompiled"], info["failed"]) == (0, 0)
        # Tick and Idle share their code: hooked once, under the first.
        assert sorted(method.name for method in methods) == [
            "AddGold", "Loot", "Place", "Rename", "Tick"]

        baseline = record("baseline", ("tick", 0), ("tick", 0))
        index = {method.name: i for i, method in enumerate(methods)}
        assert {int(key): count for key, count in baseline["counts"].items()} == {
            index["Tick"]: 2}
        scenario = record("scenario", ("tick", 0), ("loot", 5), ("tick", 0))
        rows = analyze(scenario, baseline)
        assert [(methods[row.method].name, row.verdict, row.calls) for row in rows] == [
            ("Loot", NEW, 1), ("AddGold", NEW, 1), ("Tick", SAME, 2)]
        calls = {methods[call.method].name: call.caller for call in recording_calls(scenario)}
        # Code in GameAssembly is named as the C# method it belongs to.
        assert calls["AddGold"].startswith("Game.Hero:Loot+0x")
        assert calls["Loot"].startswith("fakegame+0x")
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


_NAMING_AGENT = r"""
rpc.exports = {
  ready() {
    return engine.refresh() && engine.locate(ptr(0)) !== undefined;
  },
  names() {
    const library = Process.getModuleByName('GameAssembly.so');
    const at = name => library.getExportByName(name);
    const located = engine.locate(at('Hero_Loot').add(4));
    return {
      loot: engine.describe(at('Hero_Loot').add(4)),
      tick: engine.describe(at('Hero_Tick')),
      // The abstract method's stub is no method's code.
      stub: engine.describe(at('Unresolved')),
      outside: engine.describe(Process.getModuleByName('fakegame').base),
      identity: located ? engine.identify(at('Hero_Loot').add(4)) : null
    };
  },
  async compile(identity) {
    const code = await engine.compile(identity);
    return [code.start.toString(), code.size];
  },
  address: name => Process.getModuleByName('GameAssembly.so').getExportByName(name).toString()
};
"""


@requires_target
def test_code_addresses_are_named_and_found_again(tmp_path):
    # pylint: disable=import-outside-toplevel
    import frida

    from hermes.engines.registry import ENGINE_AGENT_SOURCE

    target = _Target(_build(tmp_path))
    session = frida.attach(target.pid)
    try:
        script = session.create_script(ENGINE_AGENT_SOURCE + _NAMING_AGENT)
        script.load()
        api = script.exports_sync
        # The table is built on the agent's thread soon after the runtime was found.
        deadline = time.monotonic() + 5
        while not api.ready():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        names = api.names()
        assert names["loot"] == "Game.Hero:Loot+0x4"
        assert names["tick"] == "Game.Hero:Tick+0x0 [shared with 1]"
        assert names["stub"] is None and names["outside"] is None
        identity = names["identity"]
        assert identity == {"engine": "il2cpp", "image": "Assembly-CSharp",
                            "file": "Assembly-CSharp.dll", "token": 0x06000004,
                            "method": "Game.Hero:Loot (int)", "offset": 4}
        start, size = api.compile(identity)
        assert start == api.address("Hero_Loot") and size > 4
        # A method that changed (another signature) is not taken for the old one.
        changed = json.loads(json.dumps(identity))
        changed["method"] = "Game.Hero:Loot (long)"
        try:
            api.compile(changed)
        except frida.core.RPCException as error:
            assert "method_changed" in str(error) or "no longer" in str(error)
        else:
            raise AssertionError("compile accepted a changed method")
        script.unload()
    finally:
        session.detach()
        target.close()


_RENAME = {"engine": "il2cpp", "image": "Assembly-CSharp", "file": None, "token": 0x06000007,
           "method": "Game.Hero:Rename (string)"}
_PLACE = {"engine": "il2cpp", "image": "Assembly-CSharp", "file": None, "token": 0x06000008,
          "method": "Game.Hero:Place (int)"}


@requires_target
def test_method_monitor_logs_arguments_and_inspects_objects(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker

    target = _Target(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    added, calls, inspected, failed = {}, [], {}, {}
    worker.monitor_added.connect(added.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))
    worker.inspected.connect(inspected.__setitem__)
    worker.inspect_failed.connect(failed.__setitem__)

    def inspect(value_target):
        request = worker.inspect(value_target)
        _wait_for(qt_app, lambda: request in inspected or request in failed)
        return inspected.get(request) or failed[request]

    try:
        worker.start()
        rename = worker.add_monitor(_RENAME, returns=False, objects=True)
        place = worker.add_monitor(_PLACE, returns=True, objects=False)
        _wait_for(qt_app, lambda: rename in added and place in added)
        assert (added[rename]["instance"], added[rename]["params"]) == (
            True, ["System.String name"])
        target.send("rename", 1)
        _wait_for(qt_app, lambda: calls)
        call = calls[0]
        assert call["monitorId"] == rename
        assert call["self"]["text"].startswith("Game.Hero@0x")
        assert call["args"][0]["text"] == '"Young Wolf"'
        # Game_Rename is no method: its code is named module+offset.
        assert call["caller"].startswith("GameAssembly.so+0x")

        calls.clear()
        # The struct result's pointer comes first: self and steps are still found.
        assert target.send("place", 4) == (0, 4)
        _wait_for(qt_app, lambda: any(c["monitorId"] == place for c in calls))
        call = next(c for c in calls if c["monitorId"] == place)
        assert call["self"]["text"].startswith("Game.Hero@0x")
        assert call["args"][0]["text"] == "4"
        assert call["result"]["text"] == "struct Game.Vec"

        contents = inspect(call["self"]["target"])
        assert contents["name"] == "Game.Hero"
        fields = {field["name"]: field for field in contents["fields"]}
        assert [(name, f["static"], f["value"]["text"]) for name, f in fields.items()] == [
            ("Gold", False, "30"), ("Name", False, '"Miranda"'), ("Instances", True, "7")]
        # The static field lies in the class's static data, found by its layout.
        assert fields["Instances"]["address"] is not None
        assert fields["Gold"]["valueType"] == "int32"
        # An address that holds no object is refused instead of read.
        assert "no object" in inspect({"address": "0x10"})
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
