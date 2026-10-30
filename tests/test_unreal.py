"""Unreal Engine reflection, against a stand-in Unreal game.

The target is named like a shipping build (``fakegame-Linux-Shipping``) and
keeps in its data what a real game has: an FNamePool and a GUObjectArray
with the objects of /Script/CoreUObject the adapter measures its layout on,
a native class with UFunctions (exec thunks), and a Blueprint class whose
function runs through a stand-in script interpreter (``ProcessInternal``).
Its layout is not Avowed's (FField with a 16-byte owner, 24-byte object
items), so the adapter has to find the offsets rather than assume them.

Calls go through Func with an FFrame, as in Unreal: ``tick`` calls
Hero:Tick and ``rename N`` Hero:Rename (FString, bool) like ProcessEvent does
(the frame's Locals hold the parameters); ``loot N`` calls BP_Hero_C:Loot that
way, whose script calls Hero:AddGold with its local Amount (the caller's
frame, Code at an EX_LocalVariable). Each answers with the gold and whether
Loot's Func is the interpreter again.
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
typedef struct Frame Frame;
typedef void (*native_fn)(Object *context, Frame *stack, void *result);
/* FFrame: FOutputDevice (vtable, flags), Node, Object, Code, Locals */
struct Frame {
    void *vtable;
    uint64_t flags;
    Object *node;
    Object *object;
    uint8_t *code;
    uint8_t *locals;
};
typedef struct { uint16_t *data; int32_t num, max; } String;

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
    int32_t layout[6];  /* FMapProperty's MapLayout after KeyProp and ValueProp */
} Field;
_Static_assert(offsetof(Field, extra) == 0x78, "FProperty size");

/* TSet's elements, a TSparseArray: slots, the TBitArray of those in use
   (inline storage, secondary, NumBits, MaxBits), FirstFreeIndex,
   NumFreeIndices; then the hash */
typedef struct {
    void *data;
    int32_t num, max;
    uint32_t bits[4];
    void *secondary;
    int32_t num_bits, max_bits;
    int32_t first_free, num_free;
    uint8_t hash[24];
} SparseSet;
_Static_assert(offsetof(SparseSet, num_bits) == 40, "TBitArray::NumBits");
_Static_assert(offsetof(SparseSet, num_free) == 52, "TSparseArray::NumFreeIndices");
typedef struct { int32_t key; float value; int32_t hash_next, hash_index; } StatSlot;
typedef struct { int32_t value; int32_t hash_next, hash_index; } TagSlot;
typedef struct { void *data; int32_t num, max; } Array;
/* UEnum::Names: TPair<FName, int64> */
typedef struct { uint32_t name, number; int64_t value; } EnumName;

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

/* Room for a hero's properties behind the UObject */
#define OBJECT_SIZE (sizeof(Object) + 0x200)

static Object *object(Object *klass, const char *text, Object *outer) {
    Object *created = calloc(1, OBJECT_SIZE);
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

/* The property of a container's elements (or keys, values): in no class */
static Field *inner(FieldClass *klass, const char *text, int32_t size, void *extra) {
    Field *created = calloc(1, sizeof(Field));
    created->vtable = (void *)0x5678;
    created->klass = klass;
    created->name = name(text);
    created->flags = 0x45;
    created->array_dim = 1;
    created->element_size = size;
    created->extra[0] = extra;
    return created;
}

static void add_function(Object *klass, Object *function) {
    Object **last = &klass->children;
    while (*last != NULL)
        last = &(*last)->next;
    *last = function;
}

#define EX_LOCAL_VARIABLE 0x00
#define EX_END_FUNCTION_PARMS 0x16

static Object *g_add_gold, *g_tick, *g_loot, *g_rename;
static volatile int32_t g_gold = 100;
static volatile int32_t g_ticks;
static volatile int32_t g_loud;

static volatile int32_t g_name_length;
static volatile int32_t g_events;

/* Every FFrame has FFrame's vtable. */
static void *g_frame_vtable[4];

__attribute__((noinline)) static void count_event(void) {
    g_events = g_events + 1;
}

/* UObject::ProcessEvent: a frame of the function's own over its parameters,
   and the call of its Func the way Unreal's code makes it, call [reg + Func] */
__attribute__((noinline)) void UObject_ProcessEvent(Object *self, Object *function,
                                                    void *parms) {
    Frame frame = {g_frame_vtable, 0, function, self, function->script, parms};
    void *result = function->return_value_offset == 0xffff ? NULL
        : (char *)parms + function->return_value_offset;
    void *stack = &frame;
    count_event();
    __asm__ volatile("call *0xd8(%%rax)"
                     : "+a"(function), "+D"(self), "+S"(stack), "+d"(result)
                     :
                     : "rcx", "r8", "r9", "r10", "r11", "memory", "cc",
                       "xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7",
                       "xmm8", "xmm9", "xmm10", "xmm11", "xmm12", "xmm13", "xmm14", "xmm15");
}

/* Other virtual functions of UObject, before and after ProcessEvent */
__attribute__((noinline)) void UObject_BeginDestroy(Object *self) {
    self->flags |= 0x8000;
}

__attribute__((noinline)) void UObject_PostLoad(Object *self) {
    self->flags |= 0x10000;
}

static void *g_object_vtable[] = {
    (void *)UObject_BeginDestroy, (void *)UObject_PostLoad, (void *)UObject_ProcessEvent,
    (void *)UObject_PostLoad, NULL
};

__attribute__((noinline)) void exec_AddGold(Object *context, Frame *stack, void *result) {
    int32_t amount;
    (void)context;
    if (stack->node == g_add_gold) {
        amount = *(int32_t *)stack->locals;
    } else {
        /* Called from bytecode: evaluate the argument, a local variable */
        Field *local;
        memcpy(&local, stack->code + 1, sizeof(local));
        amount = *(int32_t *)(stack->locals + local->offset);
        stack->code += 1 + sizeof(local) + 1;
    }
    g_gold += amount;
    if (result != NULL)
        *(int32_t *)result = g_gold;
}

__attribute__((noinline)) void exec_Tick(Object *context, Frame *stack, void *result) {
    (void)context;
    (void)stack;
    (void)result;
    g_ticks = g_ticks + 1;
}

__attribute__((noinline)) void exec_Rename(Object *context, Frame *stack, void *result) {
    (void)context;
    (void)result;
    g_loud = stack->locals[16];
    g_name_length = ((String *)stack->locals)->num;
}

/* The script interpreter: Loot's script calls AddGold(Amount), then
   broadcasts a delegate that ticks the hero (through ProcessEvent). */
__attribute__((noinline)) void ProcessInternal(Object *context, Frame *stack, void *result) {
    int32_t total = 0;
    (void)result;
    if (context != NULL && stack->node == g_loot) {
        native_fn code = *(native_fn volatile *)&g_add_gold->func;
        code(context, stack, &total);
        float delta = 0;
        UObject_ProcessEvent(context, g_tick, &delta);
    }
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
    Object *default_object = object(object_class, "Default__Object", core);
    default_object->vtable = g_object_vtable;
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
    FieldClass *str_property = field_class("StrProperty");
    FieldClass *bool_property = field_class("BoolProperty");

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
    /* A field of heroes, in memory an Object leaves unused */
    property(hero, int_property, "Health", 4, 0x70, 0, NULL);
    /* An enum known by its names: UEnum::Names after UField and CppType,
       UHT's _MAX last */
    Object *attribute = object(enum_class, "EPrimaryAttribute", game);
    static EnumName attribute_names[3];
    attribute_names[0] = (EnumName){name("EPrimaryAttribute::Might"), 0, 0};
    attribute_names[1] = (EnumName){name("EPrimaryAttribute::Dexterity"), 0, 2};
    attribute_names[2] = (EnumName){name("EPrimaryAttribute::EPrimaryAttribute_MAX"), 0, 3};
    *(Array *)((char *)attribute + 0x40) = (Array){attribute_names, 3, 3};
    Field *focus = property(hero, field_class("EnumProperty"), "Focus", 1, 0x74, 0, NULL);
    focus->extra[0] = inner(field_class("ByteProperty"), "UnderlyingType", 1, NULL);
    focus->extra[1] = attribute;
    /* Containers, behind the UObject: TArray<int32>, TArray<FVector>,
       TMap<int32, float> with UE5's MapLayout (ValueOffset, HashNextIdOffset,
       HashIndexOffset, Size, Alignment, sparse array Size), one without a
       layout (computed then), TSet<int32> with its SetLayout */
    FieldClass *array_property = field_class("ArrayProperty");
    FieldClass *map_property = field_class("MapProperty");
    FieldClass *set_property = field_class("SetProperty");
    Field *scores = property(hero, array_property, "Scores", 16, 0x100, 0, NULL);
    scores->extra[0] = inner(int_property, "Scores", 4, NULL);
    Field *path = property(hero, array_property, "Path", 16, 0x110, 0, NULL);
    path->extra[0] = inner(struct_property, "Path", 0x18, vector);
    static const int32_t stat_layout[6] = {4, 8, 12, 16, 4, 16};
    Field *stats = property(hero, map_property, "Stats", 80, 0x120, 0, NULL);
    stats->extra[0] = inner(int_property, "Stats_Key", 4, NULL);
    stats->extra[1] = inner(float_property, "Stats", 4, NULL);
    memcpy(stats->layout, stat_layout, sizeof stat_layout);
    Field *weights = property(hero, map_property, "Weights", 80, 0x170, 0, NULL);
    weights->extra[0] = inner(int_property, "Weights_Key", 4, NULL);
    weights->extra[1] = inner(float_property, "Weights", 4, NULL);
    static const int32_t tag_layout[5] = {4, 8, 12, 4, 12};
    Field *tags = property(hero, set_property, "Tags", 80, 0x1c0, 0, NULL);
    tags->extra[0] = inner(int_property, "Tags", 4, NULL);
    memcpy((char *)tags->extra + 8, tag_layout, sizeof tag_layout);
    g_add_gold = function(function_class, hero, "AddGold",
                          FUNC_NATIVE | FUNC_STATIC | FUNC_PUBLIC, exec_AddGold, 2, 8, 4);
    property(g_add_gold, int_property, "Amount", 4, 0, CPF_PARM, NULL);
    property(g_add_gold, int_property, "ReturnValue", 4, 4, CPF_PARM | CPF_RETURN_PARM, NULL);
    object(hero, "Default__Hero", game);
    g_tick = function(function_class, hero, "Tick", FUNC_NATIVE | FUNC_PUBLIC, exec_Tick,
                      1, 4, 0xffff);
    property(g_tick, float_property, "DeltaSeconds", 4, 0, CPF_PARM, NULL);
    g_rename = function(function_class, hero, "Rename", FUNC_NATIVE | FUNC_PUBLIC, exec_Rename,
                        2, 24, 0xffff);
    property(g_rename, str_property, "NewName", 16, 0, CPF_PARM, NULL);
    /* FBoolProperty of a native bool: FieldSize 1, ByteOffset 0, ByteMask 1,
       FieldMask 0xff */
    property(g_rename, bool_property, "bLoud", 1, 16, CPF_PARM, (void *)0xff010001ULL);
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
    property(g_loot, object_property, "Target", 8, 0, CPF_PARM, hero);
    Field *amount = property(g_loot, int_property, "Amount", 4, 8, CPF_PARM, NULL);
    /* AddGold(Amount): the argument, then the end of the arguments */
    static uint8_t script[1 + sizeof(Field *) + 1];
    script[0] = EX_LOCAL_VARIABLE;
    memcpy(script + 1, &amount, sizeof(amount));
    script[1 + sizeof(Field *)] = EX_END_FUNCTION_PARMS;
    g_loot->script = script;
    g_loot->script_num = g_loot->script_max = sizeof(script);

    Object *level = object(package_class, "/Game/Maps/Start", NULL);
    Object *player = object(bp_hero, "BP_Hero_C_0", level);
    *(int32_t *)((char *)player + 0x70) = 75;
    *((uint8_t *)player + 0x74) = 2;
    static int32_t score_data[3] = {7, 8, 9};
    static double path_data[6] = {1, 2, 3, 4, 5, 6};
    *(Array *)((char *)player + 0x100) = (Array){score_data, 3, 3};
    *(Array *)((char *)player + 0x110) = (Array){path_data, 2, 2};
    /* Stats: slot 1 was freed, so 2 of 3 slots are in use */
    static StatSlot stat_slots[4] = {{1, 1.5f, -1, 0}, {0, 0, -1, -1}, {3, 2.5f, -1, 1}};
    SparseSet *stat_set = (SparseSet *)((char *)player + 0x120);
    stat_set->data = stat_slots;
    stat_set->num = 3;
    stat_set->max = 4;
    stat_set->bits[0] = 0x5;
    stat_set->num_bits = 3;
    stat_set->max_bits = 128;
    stat_set->first_free = 1;
    stat_set->num_free = 1;
    static StatSlot weight_slots[1] = {{5, 0.5f, -1, 0}};
    SparseSet *weight_set = (SparseSet *)((char *)player + 0x170);
    weight_set->data = weight_slots;
    weight_set->num = weight_set->max = 1;
    weight_set->bits[0] = 0x1;
    weight_set->num_bits = 1;
    weight_set->max_bits = 128;
    weight_set->first_free = -1;
    static TagSlot tag_slots[2] = {{10, -1, 0}, {20, -1, 1}};
    SparseSet *tag_set = (SparseSet *)((char *)player + 0x1c0);
    tag_set->data = tag_slots;
    tag_set->num = tag_set->max = 2;
    tag_set->bits[0] = 0x3;
    tag_set->num_bits = 2;
    tag_set->max_bits = 128;
    tag_set->first_free = -1;
    /* A second hero: scripts find it by its name */
    Object *rival = object(bp_hero, "BP_Hero_C_1", level);
    *(int32_t *)((char *)rival + 0x70) = 30;
    for (int i = 0; i < FILLERS; i++)
        object(object_class, "Filler", game);

    printf("%d %p\n", getpid(), (void *)&g_gold);
    fflush(stdout);
    char cmd[16];
    int value;
    while (scanf("%15s %d", cmd, &value) == 2) {
        if (strcmp(cmd, "tick") == 0) {
            float delta = 0.016f;
            UObject_ProcessEvent(player, g_tick, &delta);
        } else if (strcmp(cmd, "loot") == 0) {
            struct { Object *target; int32_t amount; } parms = {player, value};
            UObject_ProcessEvent(player, g_loot, &parms);
        } else if (strcmp(cmd, "rename") == 0) {
            char text[16];
            uint16_t wide[16];
            int length = snprintf(text, sizeof(text), "Hero %d", value);
            for (int i = 0; i <= length; i++)
                wide[i] = (uint16_t)text[i];
            struct { String name; uint8_t loud; } parms = {{wide, length + 1, length + 1}, 1};
            UObject_ProcessEvent(player, g_rename, &parms);
        } else if (strcmp(cmd, "renamed") == 0) {
            printf("%d %d\n", g_name_length, g_loud);
            fflush(stdout);
            continue;
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
    subprocess.run(["gcc", "-O0", "-mno-red-zone", "-fPIE", "-pie", str(tmp_path / "fake_game.c"),
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
            ("Hero", "Rename", "(FString,bool)", "void", False, "Hero:Rename (FString,bool)"),
            ("Hero", "OnLooted", "(AHero*)", "void", False, "Hero:OnLooted (AHero*)"),
        ]
        assert all(method.engine == "unreal" for method in hero.values())
        [loot] = loaded["/Game/Blueprints"]
        assert loot.signature == "BP_Hero_C:Loot (AHero*,int32)"
        # Tokens hash the names: the same in every run of the game.
        assert len({m.token for m in hero.values()} | {loot.token}) == 5
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
        assert planned[0] == {"methods": 4, "skipped": {"abstract": 1, "native": 0, "generic": 0},
                              "assemblies": ["/Script/FakeGame", "/Game/Blueprints"]}

        worker.trace([])
        _wait_for(qt_app, lambda: started or failed)
        assert not failed
        methods, info = started[0]
        assert (info["notCompiled"], info["failed"]) == (0, 0)
        assert sorted(method.name for method in methods) == ["AddGold", "Loot", "Rename", "Tick"]
        # The Blueprint function runs through a stub of its own now.
        assert target.send("loot", 0)[1] == 0

        baseline = record("baseline", ("tick", 0), ("tick", 0), ("tick", 0))
        index = {method.name: i for i, method in enumerate(methods)}
        assert {int(key): count for key, count in baseline["counts"].items()} == {
            index["Tick"]: 3}
        scenario = record("scenario", ("tick", 0), ("loot", 5), ("tick", 0))
        rows = analyze(scenario, baseline)
        assert [(methods[row.method].name, row.verdict, row.calls) for row in rows] == [
            ("Loot", NEW, 1), ("AddGold", NEW, 1), ("Tick", SAME, 3)]
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


def _catalog(qt_app, pid):
    """{signature: CatalogMethod} of the game's packages."""
    # pylint: disable=import-outside-toplevel
    from hermes.features.catalog import CatalogWorker

    worker = CatalogWorker(pid)
    loaded = {}
    worker.assembly_loaded.connect(loaded.__setitem__)
    _run(qt_app, worker, worker.assemblies_found)
    return {method.signature: method for methods in loaded.values() for method in methods}


@requires_target
def test_method_monitor_reads_arguments_from_the_call_frame(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker

    target = _Target(_build(tmp_path))
    methods = _catalog(qt_app, target.pid)
    worker = MethodMonitorWorker(target.pid)
    added, failed, calls = {}, {}, []
    worker.monitor_added.connect(added.__setitem__)
    worker.monitor_failed.connect(failed.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))

    def monitor(signature, **options):
        monitor_id = worker.add_monitor(methods[signature].identity(), **options)
        _wait_for(qt_app, lambda: monitor_id in added or monitor_id in failed)
        assert not failed
        return monitor_id

    def one_call(monitor_id, command, value):
        calls.clear()
        answer = target.send(command, value)

        def mine():
            return [call for call in calls if call["monitorId"] == monitor_id]

        _wait_for(qt_app, mine)
        assert len(mine()) == 1
        return answer, mine()[0]

    try:
        worker.start()
        # Called from Loot's bytecode: the argument is Loot's local Amount.
        add_gold = monitor("Hero:AddGold (int32)", returns=True)
        assert added[add_gold]["params"] == ["int32 Amount"]
        assert (added[add_gold]["returns"], added[add_gold]["instance"]) == ("int32", False)
        answer, call = one_call(add_gold, "loot", 5)
        assert answer == (105, 1)
        assert call["self"] is None
        assert [(arg["name"], arg["text"]) for arg in call["args"]] == [("Amount", "5")]
        assert call["result"] == {"text": "105"}
        # The Blueprint function whose bytecode called it
        assert call["caller"] == "BP_Hero_C:Loot+0x0"
        worker.remove_monitor(add_gold)

        # A Blueprint function, through ProcessEvent: its parameters, the
        # object it runs on (with its name), and a snapshot of its fields
        loot = monitor("BP_Hero_C:Loot (AHero*,int32)", objects=True)
        answer, call = one_call(loot, "loot", 2)
        assert answer == (107, 0)
        assert call["self"]["text"].startswith('ABP_Hero_C "BP_Hero_C_0"@0x')
        target_arg, amount = call["args"]
        assert target_arg["text"] == call["self"]["text"]
        assert amount["text"] == "2"
        # Native code called it (no script frame above).
        assert call["caller"].startswith("fakegame-Linux-Shipping+0x")
        health = target_arg["snapshot"]["fields"][0]
        assert (health["name"], health["type"], health["value"], health["valueType"]) == (
            "Health", "int32", {"text": "75"}, "int32")
        worker.remove_monitor(loot)
        # Removing the monitor gives Loot its interpreter back (the worker
        # does it in a moment).
        _wait_for(qt_app, lambda: target.send("loot", 0) == (107, 1))

        # Tick through ProcessEvent while Loot's script runs: Loot is found
        # further up the stack, at the end of its bytecode.
        tick = monitor("Hero:Tick (float)")
        _answer, call = one_call(tick, "loot", 0)
        assert call["caller"] == "BP_Hero_C:Loot+0xa"
        _answer, call = one_call(tick, "tick", 0)
        assert call["caller"].startswith("fakegame-Linux-Shipping+0x")
        worker.remove_monitor(tick)

        rename = monitor("Hero:Rename (FString,bool)")
        _answer, call = one_call(rename, "rename", 7)
        assert [(arg["name"], arg["text"]) for arg in call["args"]] == [
            ("NewName", '"Hero 7"'), ("bLoud", "true")]
        worker.remove_monitor(rename)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_scripts_call_unreal_functions_through_process_event(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.scripts import ScriptSpec, ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append((script_id, run)))

    def run_on_game_thread(script_id):
        # Scripts run when the game thread next enters ProcessEvent.
        runs.clear()
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0][0] == script_id
        return runs[0][1]

    try:
        worker.start()
        # A static function runs on the class default object; its result is
        # read from the parameter block. An instance function gets an FString
        # and a bool.
        calls = worker.add_script(ScriptSpec("""
            const hero = ctx.find("BP_Hero_C");
            ctx.log(hero.name, hero.Health);
            hero.call("Rename", "Bob", true);
            return ctx.call("Hero.AddGold", 7);
        """))
        _wait_for(qt_app, lambda: calls in added or calls in failed)
        assert not failed
        run = run_on_game_thread(calls)
        assert run["error"] is None, run["error"]
        assert run["logs"] == ["BP_Hero_C_0 75"]
        assert run["result"] == "107"
        assert target.send("renamed", 0) == (4, 1)  # "Bob" and its terminating 0

        # An object it returns comes with a target the inspector reads.
        inspected = []
        worker.inspected.connect(lambda _request, contents: inspected.append(contents))
        hero = worker.add_script(ScriptSpec('return ctx.find("BP_Hero_C");'))
        _wait_for(qt_app, lambda: hero in added)
        run = run_on_game_thread(hero)
        assert run["result"].startswith('ABP_Hero_C "BP_Hero_C_0"@0x')
        worker.inspect(run["target"])
        _wait_for(qt_app, lambda: inspected)
        assert [(field["name"], field["value"]["text"]) for field in inspected[0]["fields"]
                if field["name"] == "Health"] == [("Health", "75")]

        # TArray, TMap and TSet open in the inspector: an entry per row, a
        # TMap's named by its key; numbers have an address for the address list.
        values = {field["name"]: field["value"] for field in inspected[0]["fields"]}
        assert values["Focus"]["text"] == "2 (Dexterity)"  # an enum by the name of its value
        assert values["Scores"]["text"] == "TArray<int32>[3] {7, 8, 9}"
        assert values["Stats"]["text"] == "TMap<int32, float>[2]"
        assert values["Tags"]["text"] == "TSet<int32>[2]"

        def entries(name):
            inspected.clear()
            worker.inspect(values[name]["target"])
            _wait_for(qt_app, lambda: inspected)
            assert "error" not in inspected[0], inspected[0]
            return inspected[0]

        scores = entries("Scores")
        assert (scores["name"], scores["length"]) == ("TArray<int32>", 3)
        assert [(field["name"], field["value"]["text"], field["valueType"])
                for field in scores["fields"]] == [
            ("[0]", "7", "int32"), ("[1]", "8", "int32"), ("[2]", "9", "int32")]
        assert int(scores["fields"][1]["address"], 16) == int(scores["fields"][0]["address"], 16) + 4
        path = entries("Path")["fields"]
        assert path[1]["value"]["text"] == "struct FVector" and path[1]["value"]["target"]
        stats = entries("Stats")
        assert stats["length"] == 2  # the freed slot is skipped
        assert [(field["name"], field["value"]["text"], field["valueType"])
                for field in stats["fields"]] == [("1", "1.5", "float"), ("3", "2.5", "float")]
        # Without a layout in the property it is computed from the sizes.
        assert [(field["name"], field["value"]["text"])
                for field in entries("Weights")["fields"]] == [("5", "0.5")]
        assert [field["value"]["text"] for field in entries("Tags")["fields"]] == ["10", "20"]

        # The inspector lists the object's UFunctions (not the static ones)
        # and calls one on it, through ProcessEvent on the game thread.
        listed, called = [], []
        worker.methods_listed.connect(lambda _request, methods: listed.append(methods))
        worker.called.connect(lambda _request, answer: called.append(answer))
        worker.list_methods(run["target"])
        _wait_for(qt_app, lambda: listed)
        names = [method["name"] for method in listed[0]]
        assert names[0] == "Loot" and "Rename" in names and "AddGold" not in names
        rename = next(method for method in listed[0] if method["name"] == "Rename")
        worker.call_on(run["target"], rename["method"], ["Ann", True])
        _wait_for(qt_app, lambda: target.send("tick", 0) and called)
        assert called[0] == {"result": None}
        assert target.send("renamed", 0) == (4, 1)  # "Ann" and its terminating 0

        # A name picks one of several objects of a class; found again from
        # the cache in the second run.
        named = worker.add_script(ScriptSpec(
            'return ctx.find("BP_Hero_C", "BP_Hero_C_1").Health'
            ' + (ctx.find("BP_Hero_C", "Nobody") === null ? 1000 : 0);'))
        _wait_for(qt_app, lambda: named in added)
        for _ in range(2):
            run = run_on_game_thread(named)
            assert run["error"] is None, run["error"]
            assert run["result"] == "1030"

        # Unknown functions and classes are reported as the script's error.
        typo = worker.add_script(ScriptSpec('ctx.find("BP_Hero_C").call("Jump");'))
        _wait_for(qt_app, lambda: typo in added)
        run = run_on_game_thread(typo)
        assert "has no function Jump with 0 argument(s)" in run["error"]

        # A repeating script runs again and again while the game runs.
        runs.clear()
        repeating = worker.add_script(ScriptSpec(
            "ctx.state.n = (ctx.state.n || 0) + 1; return ctx.state.n;", "repeat", 0.1))
        _wait_for(qt_app, lambda: target.send("tick", 0)
                  and len([r for r in runs if r[0] == repeating]) >= 3)
        assert [r[1]["result"] for r in runs if r[0] == repeating][:3] == ["1", "2", "3"]
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
