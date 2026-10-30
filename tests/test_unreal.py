"""Unreal Engine reflection, against a stand-in Unreal game.

The target is named like a shipping build (``fakegame-Linux-Shipping``) and
keeps in its data what a real game has: an FNamePool and a GUObjectArray
with the objects of /Script/CoreUObject the adapter measures its layout on,
a native class with UFunctions (exec thunks), and a Blueprint class whose
function runs through a stand-in script interpreter (``ProcessInternal``).
Its layout is not Avowed's (FField with a 16-byte owner, 24-byte object
items), so the adapter has to find the offsets rather than assume them.

Commands: ``tick`` calls Hero:Tick, ``loot N`` calls BP_Hero_C:Loot through
its Func, whose script calls Hero:AddGold. Each answers with the gold and
whether Loot's Func is the interpreter again.
"""

import subprocess

from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    fixture_qt_app,
    requires_target,
)


_FAKE_GAME_SOURCE = r"""
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct Object Object;
typedef void (*native_fn)(Object *context, void *stack, void *result);

typedef struct { uint32_t name, number; uint64_t id, cast_flags; } FieldClass;
typedef struct Field {
    void *vtable;
    FieldClass *klass;
    void *owner;
    uint64_t owner_is_object;
    struct Field *next;
    uint32_t name, number;
    uint32_t flags, pad;
    int32_t array_dim, element_size;
    uint64_t property_flags;
    uint16_t rep_index;
    uint8_t condition, pad2;
    int32_t offset;
    uint32_t rep_notify[2];
    void *links[4];
    void *extra[2];
} Field;
_Static_assert(offsetof(Field, extra) == 0x78, "FProperty size");

struct Object {
    void *vtable;
    uint32_t flags;
    int32_t index;
    Object *klass;
    uint32_t name, number;
    Object *outer;
    Object *next;
    void *base_chain;
    int64_t base_count;
    Object *super;
    Object *children;
    Field *child_properties;
    int32_t properties_size, min_alignment;
    uint8_t *script;
    int32_t script_num, script_max;
    char pad[0xb0 - 0x70];
    uint32_t function_flags;
    uint8_t num_parms, pad1;
    uint16_t parms_size;
    uint16_t return_value_offset, rpc_id, rpc_response_id, pad2;
    void *first_property_to_init;
    void *event_graph_function;
    int64_t event_graph_call_offset;
    native_fn func;
};
_Static_assert(offsetof(Object, func) == 0xd8, "UFunction::Func");

/* FNamePool: a lock (a pthread_rwlock_t on Linux), CurrentBlock,
   CurrentByteCursor, Blocks */
struct {
    char lock[56];
    uint32_t current_block, cursor;
    uint8_t *blocks[8192];
} g_name_pool;

typedef struct { Object *object; int32_t flags, cluster, serial, pad; } Item;
/* FUObjectArray: GC indexes, then FChunkedFixedUObjectArray */
struct {
    int32_t first_gc, last_non_gc, max_not_gc, open;
    Item **objects;
    Item *preallocated;
    int32_t max, num, max_chunks, num_chunks;
} g_uobject_array;

#define FUNC_EVENT 0x800
#define FUNC_NATIVE 0x400
#define FUNC_STATIC 0x2000
#define FUNC_PUBLIC 0x20000
#define FUNC_BLUEPRINT_EVENT 0x8000000
#define CPF_PARM 0x80
#define CPF_RETURN_PARM 0x400
#define FILLERS 5000

static uint32_t name(const char *text) {
    size_t length = strlen(text);
    uint8_t *entry = g_name_pool.blocks[0] + g_name_pool.cursor;
    uint16_t header = (uint16_t)(length << 6);
    memcpy(entry, &header, 2);
    memcpy(entry + 2, text, length);
    uint32_t index = g_name_pool.cursor / 2;
    g_name_pool.cursor += (uint32_t)((2 + length + 1) & ~(size_t)1);
    return index;
}

static Object *object(Object *klass, const char *text, Object *outer) {
    Object *created = calloc(1, sizeof(Object));
    int32_t index = g_uobject_array.num++;
    created->vtable = (void *)0x1234;
    created->flags = 0x41;
    created->index = index;
    created->klass = klass;
    created->name = name(text);
    created->outer = outer;
    g_uobject_array.objects[index / 65536][index % 65536].object = created;
    return created;
}

static FieldClass *field_class(const char *text) {
    FieldClass *created = calloc(1, sizeof(FieldClass));
    created->name = name(text);
    return created;
}

static Field *property(Object *owner, FieldClass *klass, const char *text, int32_t size,
                       int32_t offset, uint64_t flags, void *extra) {
    Field *created = calloc(1, sizeof(Field));
    created->vtable = (void *)0x5678;
    created->klass = klass;
    created->owner = owner;
    created->owner_is_object = 1;
    created->name = name(text);
    created->flags = 0x45;
    created->array_dim = 1;
    created->element_size = size;
    created->property_flags = flags;
    created->offset = offset;
    created->extra[0] = extra;
    Field **last = &owner->child_properties;
    while (*last != NULL)
        last = &(*last)->next;
    *last = created;
    return created;
}

static void add_function(Object *klass, Object *function) {
    Object **last = &klass->children;
    while (*last != NULL)
        last = &(*last)->next;
    *last = function;
}

static Object *g_add_gold, *g_tick, *g_loot;
static volatile int32_t g_gold = 100;
static volatile int32_t g_ticks;

static void invoke(Object *function, Object *self, void *parms) {
    native_fn code = *(native_fn volatile *)&function->func;
    code(self, parms, NULL);
}

__attribute__((noinline)) void exec_AddGold(Object *context, void *stack, void *result) {
    (void)context;
    (void)result;
    g_gold += *(volatile int32_t *)stack;
}

__attribute__((noinline)) void exec_Tick(Object *context, void *stack, void *result) {
    (void)context;
    (void)stack;
    (void)result;
    g_ticks = g_ticks + 1;
}

/* The script interpreter: Loot's script adds gold. */
__attribute__((noinline)) void ProcessInternal(Object *context, void *stack, void *result) {
    (void)result;
    if (context != NULL)
        invoke(g_add_gold, context, stack);
}

static Object *function(Object *klass, Object *outer, const char *text, uint32_t flags,
                        native_fn code, uint8_t parms, uint16_t size, uint16_t ret) {
    Object *created = object(klass, text, outer);
    created->function_flags = flags;
    created->num_parms = parms;
    created->parms_size = size;
    created->properties_size = size;
    created->min_alignment = 4;
    created->return_value_offset = ret;
    created->func = code;
    add_function(outer, created);
    return created;
}

int main(void) {
    g_name_pool.blocks[0] = calloc(1, 0x20000);
    name("None");
    name("ByteProperty");
    g_uobject_array.max = 0x210000;
    g_uobject_array.max_chunks = 0x21;
    g_uobject_array.num_chunks = 1;
    g_uobject_array.objects = calloc(0x21, sizeof(Item *));
    g_uobject_array.objects[0] = calloc(65536, sizeof(Item));

    Object *core = object(NULL, "/Script/CoreUObject", NULL);
    Object *class_class = object(NULL, "Class", core);
    class_class->klass = class_class;
    Object *object_class = object(class_class, "Object", core);
    Object *field_class_object = object(class_class, "Field", core);
    Object *struct_class = object(class_class, "Struct", core);
    Object *function_class = object(class_class, "Function", core);
    Object *script_struct = object(class_class, "ScriptStruct", core);
    Object *package_class = object(class_class, "Package", core);
    Object *enum_class = object(class_class, "Enum", core);
    core->klass = package_class;
    field_class_object->super = object_class;
    struct_class->super = field_class_object;
    class_class->super = struct_class;
    function_class->super = struct_class;
    script_struct->super = struct_class;
    package_class->super = object_class;
    enum_class->super = field_class_object;

    FieldClass *double_property = field_class("DoubleProperty");
    FieldClass *int_property = field_class("IntProperty");
    FieldClass *float_property = field_class("FloatProperty");
    FieldClass *struct_property = field_class("StructProperty");
    FieldClass *object_property = field_class("ObjectProperty");

    Object *vector = object(script_struct, "Vector", core);
    vector->properties_size = 0x18;
    property(vector, double_property, "X", 8, 0, 0, NULL);
    property(vector, double_property, "Y", 8, 8, 0, NULL);
    property(vector, double_property, "Z", 8, 16, 0, NULL);
    Object *quat = object(script_struct, "Quat", core);
    quat->properties_size = 0x20;
    Object *transform = object(script_struct, "Transform", core);
    transform->properties_size = 0x60;
    property(transform, struct_property, "Rotation", 0x20, 0, 0, quat);
    property(transform, struct_property, "Translation", 0x18, 0x20, 0, vector);

    Object *ubergraph = function(function_class, object_class, "ExecuteUbergraph",
                                 FUNC_EVENT | FUNC_PUBLIC | FUNC_BLUEPRINT_EVENT,
                                 ProcessInternal, 1, 4, 0xffff);
    property(ubergraph, int_property, "EntryPoint", 4, 0, CPF_PARM, NULL);

    Object *engine = object(package_class, "/Script/Engine", NULL);
    Object *actor = object(class_class, "Actor", engine);
    actor->super = object_class;
    Object *generated_class = object(class_class, "BlueprintGeneratedClass", engine);
    generated_class->super = class_class;

    Object *game = object(package_class, "/Script/FakeGame", NULL);
    Object *hero = object(class_class, "Hero", game);
    hero->super = actor;
    g_add_gold = function(function_class, hero, "AddGold",
                          FUNC_NATIVE | FUNC_STATIC | FUNC_PUBLIC, exec_AddGold, 2, 8, 4);
    property(g_add_gold, int_property, "Amount", 4, 0, CPF_PARM, NULL);
    property(g_add_gold, int_property, "ReturnValue", 4, 4, CPF_PARM | CPF_RETURN_PARM, NULL);
    g_tick = function(function_class, hero, "Tick", FUNC_NATIVE | FUNC_PUBLIC, exec_Tick,
                      1, 4, 0xffff);
    property(g_tick, float_property, "DeltaSeconds", 4, 0, CPF_PARM, NULL);
    /* A Blueprint event the native class declares: no script, nothing to hook */
    Object *on_looted = function(function_class, hero, "OnLooted",
                                 FUNC_EVENT | FUNC_PUBLIC | FUNC_BLUEPRINT_EVENT,
                                 ProcessInternal, 1, 8, 0xffff);
    property(on_looted, object_property, "By", 8, 0, CPF_PARM, hero);

    Object *blueprint = object(package_class, "/Game/Blueprints/BP_Hero", NULL);
    Object *bp_hero = object(generated_class, "BP_Hero_C", blueprint);
    bp_hero->super = hero;
    g_loot = function(function_class, bp_hero, "Loot", FUNC_PUBLIC, ProcessInternal,
                      2, 12, 0xffff);
    static uint8_t script[] = {0x1c, 0x53};
    g_loot->script = script;
    g_loot->script_num = g_loot->script_max = sizeof(script);
    property(g_loot, object_property, "Target", 8, 0, CPF_PARM, hero);
    property(g_loot, int_property, "Amount", 4, 8, CPF_PARM, NULL);

    Object *level = object(package_class, "/Game/Maps/Start", NULL);
    Object *player = object(bp_hero, "BP_Hero_C_0", level);
    for (int i = 0; i < FILLERS; i++)
        object(object_class, "Filler", game);

    printf("%d %p\n", getpid(), (void *)&g_gold);
    fflush(stdout);
    char cmd[16];
    int value;
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "tick") == 0) {
            float delta = 0.016f;
            invoke(g_tick, player, &delta);
        } else if (strcmp(cmd, "loot") == 0) {
            int32_t parms[3] = {0, 0, value};
            invoke(g_loot, player, &parms[2]);
        }
        printf("%d %d\n", g_gold, *(native_fn volatile *)&g_loot->func == ProcessInternal);
        fflush(stdout);
    }
    return 0;
}
"""


def _build(tmp_path):
    (tmp_path / "fake_game.c").write_text(_FAKE_GAME_SOURCE)
    binary = tmp_path / "fakegame-Linux-Shipping"
    subprocess.run(["gcc", "-O0", "-fPIE", "-pie", str(tmp_path / "fake_game.c"),
                    "-o", str(binary)], check=True)
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
def test_catalog_lists_packages_classes_and_ufunctions(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.engines.registry import detect_engine
    from hermes.features.catalog import CatalogWorker

    target = _Target(_build(tmp_path))
    try:
        engine = detect_engine(target.pid)
        assert engine.id == "unreal"

        worker = CatalogWorker(target.pid)
        loaded = {}
        worker.assembly_loaded.connect(loaded.__setitem__)
        found = _run(qt_app, worker, worker.assemblies_found)[0][0]
        # Packages with functions; Blueprints grouped by their first folder
        assert [(a["name"], a["classes"]) for a in found] == [
            ("/Script/CoreUObject", 8), ("/Script/FakeGame", 1), ("/Game/Blueprints", 1)]
        assert all(a["mvid"] for a in found)
        assert engine.is_framework_assembly("/Script/CoreUObject")
        # The framework is left out unless asked for.
        assert sorted(loaded) == ["/Game/Blueprints", "/Script/FakeGame"]
        hero = {method.name: method for method in loaded["/Script/FakeGame"]}
        assert [(m.class_name, m.name, m.parameters, m.return_type, m.static, m.signature)
                for m in hero.values()] == [
            ("Hero", "AddGold", "(int32)", "int32", True, "Hero:AddGold (int32)"),
            ("Hero", "Tick", "(float)", "void", False, "Hero:Tick (float)"),
            ("Hero", "OnLooted", "(AHero*)", "void", False, "Hero:OnLooted (AHero*)"),
        ]
        assert all(method.engine == "unreal" for method in hero.values())
        [loot] = loaded["/Game/Blueprints"]
        assert loot.signature == "BP_Hero_C:Loot (AHero*,int32)"
        # Tokens hash the names: the same in every run of the game.
        assert len({m.token for m in hero.values()} | {loot.token}) == 4
    finally:
        target.close()


@requires_target
def test_trace_hooks_native_and_blueprint_functions(tmp_path, qt_app):
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
        assert target.send("loot", 0) == (100, 1)
        worker.start()
        worker.plan([])
        _wait_for(qt_app, lambda: planned or failed)
        assert not failed
        # OnLooted has no script: nothing to hook.
        assert planned[0] == {"methods": 3, "skipped": {"abstract": 1, "native": 0, "generic": 0},
                              "assemblies": ["/Script/FakeGame", "/Game/Blueprints"]}

        worker.trace([])
        _wait_for(qt_app, lambda: started or failed)
        assert not failed
        methods, info = started[0]
        assert (info["notCompiled"], info["failed"]) == (0, 0)
        assert sorted(method.name for method in methods) == ["AddGold", "Loot", "Tick"]
        # The Blueprint function runs through a stub of its own now.
        assert target.send("loot", 0)[1] == 0

        baseline = record("baseline", ("tick", 0), ("tick", 0), ("tick", 0))
        index = {method.name: i for i, method in enumerate(methods)}
        assert {int(key): count for key, count in baseline["counts"].items()} == {
            index["Tick"]: 3}
        scenario = record("scenario", ("tick", 0), ("loot", 5), ("tick", 0))
        rows = analyze(scenario, baseline)
        assert [(methods[row.method].name, row.verdict, row.calls) for row in rows] == [
            ("Loot", NEW, 1), ("AddGold", NEW, 1), ("Tick", SAME, 2)]
        calls = recording_calls(scenario)
        assert [methods[call.method].name for call in calls] == ["Loot", "AddGold"]
        assert all(call.caller.startswith("fakegame-Linux-Shipping+0x") for call in calls)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
    try:
        # Stopping gave the Blueprint function its interpreter back.
        assert target.send("loot", 1) == (106, 1)
    finally:
        target.close()
