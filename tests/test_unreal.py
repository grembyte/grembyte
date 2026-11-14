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
Loot's Func is the interpreter again. Hero:MoveBy (FVector, out int32) is
only called by grembyte; ``moved 0`` answers with the sum of its last offset
and how often it ran.
"""

import subprocess

import pytest

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

typedef struct {
#ifdef FATEKEEPER_LAYOUT
    void *vtable;  /* newer engines: FFieldClass is polymorphic, its name after the vtable */
#endif
    uint32_t name, number;
    uint64_t id, cast_flags;
} FieldClass;
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
/* FText: TextData (the object and its reference controller) and flags; the
   text history keeps its display string behind its vtable and reference count */
typedef struct { void *vtable; int32_t refs, weak_refs; String display; } TextHistory;
typedef struct { void *data; void *controller; uint32_t flags, pad; } Text;
/* A localized text's history: its id, source string and display string, a
   shared pointer to an FString that MakeShared put behind its controller */
typedef struct { void *vtable; int32_t refs, weak_refs; String string; } SharedString;
typedef struct {
    void *vtable;
    int32_t refs, weak_refs;
    const uint16_t *text_namespace, *key;
    uint64_t hash;
    String source;
    String *display;
    SharedString *display_controller;
} LocalizedHistory;
_Static_assert(offsetof(LocalizedHistory, display) == 0x38, "FTextHistory_Base::LocalizedString");
/* A string table's text: its history shares an FStringTableReferenceData
   (weak self-reference, TableId, Key, ...) with a weak pointer to the
   FStringTableEntry (its table, then the source string); each behind its
   controller as MakeShared puts them */
typedef struct {
    void *vtable;
    int32_t refs, weak_refs;
    void *owner, *owner_controller;
    String source;
    uint64_t display_id[2];
} TableEntry;
typedef struct {
    void *vtable;
    int32_t refs, weak_refs;
    void *self, *self_controller;
    uint32_t table_id, table_number;
    const uint16_t *key;
    uint64_t hash, revision;
    void *entry, *entry_controller;
} TableReference;
typedef struct {
    void *vtable;
    int32_t refs, weak_refs;
    void *reference, *reference_controller;
} TableHistory;
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

#ifdef FATEKEEPER_LAYOUT
/* Newer engines (Fatekeeper): the 64-bit FlagsAndRefCount before the object;
   no PreAllocatedObjects, and the counts before the maxima */
typedef struct { int64_t flags; Object *object; int32_t serial, cluster; } Item;
#else
typedef struct { Object *object; int32_t flags, cluster, serial, pad; } Item;
#endif
/* FUObjectArray: GC indexes, then FChunkedFixedUObjectArray */
struct {
    int32_t first_gc, last_non_gc, max_not_gc, open;
    Item **objects;
#ifdef FATEKEEPER_LAYOUT
    int32_t num, max, num_chunks, max_chunks;
#else
    Item *preallocated;
    int32_t max, num, max_chunks, num_chunks;
#endif
} g_uobject_array;

#define FUNC_EVENT 0x800
#define FUNC_NATIVE 0x400
#define FUNC_STATIC 0x2000
#define FUNC_PUBLIC 0x20000
#define FUNC_BLUEPRINT_EVENT 0x8000000
#define CPF_PARM 0x80
#define CPF_OUT_PARM 0x100
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
#ifdef FATEKEEPER_LAYOUT
    created->vtable = (void *)0x9abc;
#endif
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

static Object *g_add_gold, *g_tick, *g_loot, *g_rename, *g_move_by;
static volatile int32_t g_moved, g_moves;
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

/* The call of a UFunction's Func the way Unreal's code makes it, call [reg + Func] */
#define CALL_FUNC(function, self, stack, result) \
    __asm__ volatile("call *0xd8(%%rax)" \
                     : "+a"(function), "+D"(self), "+S"(stack), "+d"(result) \
                     : \
                     : "rcx", "r8", "r9", "r10", "r11", "memory", "cc", \
                       "xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7", \
                       "xmm8", "xmm9", "xmm10", "xmm11", "xmm12", "xmm13", "xmm14", "xmm15")

#ifdef OUT_OF_LINE_INVOKE
/* UFunction::Invoke, as some games keep it out of ProcessEvent */
__attribute__((noinline)) static void UFunction_Invoke(Object *function, Object *self,
                                                       void *stack, void *result) {
    CALL_FUNC(function, self, stack, result);
}
#endif

/* UObject::ProcessEvent: a frame of the function's own over its parameters,
   and the call of its Func */
__attribute__((noinline)) void UObject_ProcessEvent(Object *self, Object *function,
                                                    void *parms) {
    Frame frame = {g_frame_vtable, 0, function, self, function->script, parms};
    void *result = function->return_value_offset == 0xffff ? NULL
        : (char *)parms + function->return_value_offset;
    void *stack = &frame;
    count_event();
#ifdef OUT_OF_LINE_INVOKE
    UFunction_Invoke(function, self, stack, result);
#else
    CALL_FUNC(function, self, stack, result);
#endif
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

/* MoveBy(FVector Offset, out int32 Distance): the sum of the offset's values */
__attribute__((noinline)) void exec_MoveBy(Object *context, Frame *stack, void *result) {
    struct { double x, y, z; int32_t distance; } *parms = (void *)stack->locals;
    (void)context;
    (void)result;
    g_moved = (int32_t)(parms->x + parms->y + parms->z);
    g_moves = g_moves + 1;
    parms->distance = g_moved;
}

/* The Gameplay Ability System: FGameplayAttribute names an attribute by
   its name, a TFieldPath (the FProperty, a weak pointer to its class, the
   path: the property's name) and its class. */
typedef struct {
    String attribute_name;
    Field *resolved_field;
    int32_t owner_index, owner_serial;
    uint32_t *path;
    int32_t path_num, path_max;
    Object *attribute_owner;
} GameplayAttribute;
_Static_assert(sizeof(GameplayAttribute) == 56, "FGameplayAttribute");

static Object *g_hero_set, *g_component;
static volatile int32_t g_set_calls;

static int name_is(uint32_t index, const String *text) {
    /* Whether FName ``index`` reads as the FString ``text`` */
    const uint8_t *entry = g_name_pool.blocks[0] + index * 2;
    uint16_t header;
    memcpy(&header, entry, 2);
    int length = header >> 6;
    if (text->num != length + 1)
        return 0;
    for (int i = 0; i < length; i++)
        if (text->data[i] != entry[2 + i])
            return 0;
    return 1;
}

/* The game's setter of an attribute's base value (Avowed's
   SetFloatAttributeBaseFromAbilitySystemComponent): GAS applies the hero's
   x1.5 effect to the current value. It finds the attribute only by a
   complete FGameplayAttribute. */
__attribute__((noinline)) void exec_SetFloatAttributeBase(Object *context, Frame *stack,
                                                          void *result) {
    struct { Object *component; GameplayAttribute attribute; float value; uint8_t found; }
        *parms = (void *)stack->locals;
    const GameplayAttribute *attribute = &parms->attribute;
    const Field *field = attribute->resolved_field;
    (void)context;
    (void)result;
    g_set_calls = g_set_calls + 1;
    parms->found = 0;
    if (parms->component != g_component || field == NULL || attribute->owner_index < 0)
        return;
    const Item *owner = &g_uobject_array.objects[0][attribute->owner_index];
    if (owner->object != field->owner || owner->serial != attribute->owner_serial
        || attribute->attribute_owner != field->owner || attribute->path_num != 1
        || attribute->path[0] != field->name || !name_is(field->name, &attribute->attribute_name))
        return;
    float *data = (float *)((char *)g_hero_set + field->offset + 8);
    data[0] = parms->value;
    data[1] = parms->value * 1.5f;
    parms->found = 1;
}

/* AngelscriptGAS' setter (Gothic 1 Remake): AngelscriptAttributeSet's
   TrySetAttributeBaseValue(FName, float), called on the set; it finds the
   attribute by the name of its property. */
__attribute__((noinline)) void exec_TrySetAttributeBaseValue(Object *context, Frame *stack,
                                                             void *result) {
    struct { uint32_t name, number; float value; } *parms = (void *)stack->locals;
    g_set_calls = g_set_calls + 1;
    *(uint8_t *)result = 0;
    if (context != g_hero_set)
        return;
    for (Field *field = context->klass->child_properties; field != NULL; field = field->next) {
        if (field->name != parms->name || field->number != parms->number)
            continue;
        float *data = (float *)((char *)context + field->offset + 8);
        data[0] = parms->value;
        data[1] = parms->value * 1.5f;
        *(uint8_t *)result = 1;
        return;
    }
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
    FieldClass *text_property = field_class("TextProperty");
    property(hero, text_property, "Title", 0x18, 0x78, 0, NULL);
    property(hero, text_property, "Motto", 0x18, 0x90, 0, NULL);
    property(hero, text_property, "Lore", 0x18, 0xa8, 0, NULL);
    property(hero, text_property, "Epitaph", 0x18, 0xc0, 0, NULL);
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
    g_move_by = function(function_class, hero, "MoveBy", FUNC_NATIVE | FUNC_STATIC | FUNC_PUBLIC,
                         exec_MoveBy, 2, 28, 0xffff);
    property(g_move_by, struct_property, "Offset", 0x18, 0, CPF_PARM, vector);
    property(g_move_by, int_property, "Distance", 4, 0x18, CPF_PARM | CPF_OUT_PARM, NULL);

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
    static uint16_t title[] = {'A', 't', 't', 'r', 'i', 'b', 'u', 't', 'e', 0};
    static TextHistory title_history = {(void *)0x9abc, 1, 1, {title, 10, 10}};
    *(Text *)((char *)player + 0x78) = (Text){&title_history, NULL, 0, 0};
    /* Motto: localized, shown as "Mut" */
    static uint16_t courage[] = {'C', 'o', 'u', 'r', 'a', 'g', 'e', 0};
    static uint16_t mut[] = {'M', 'u', 't', 0};
    static uint16_t motto_key[] = {'M', 'O', 'T', 'T', 'O', 0};
    static SharedString motto_display = {(void *)0x9abd, 1, 1, {mut, 4, 4}};
    static LocalizedHistory motto_history = {
        (void *)0x9abe, 1, 1, motto_key + 5, motto_key, 0x1234, {courage, 8, 8},
        &motto_display.string, &motto_display};
    *(Text *)((char *)player + 0x90) = (Text){&motto_history, NULL, 0, 0};
    /* Lore: from a string table */
    static uint16_t legend[] = {'L', 'e', 'g', 'e', 'n', 'd', 'e', 0};
    static uint16_t lore_key[] = {'L', 'O', 'R', 'E', 0};
    static TableEntry lore_entry = {(void *)0x9abf, 1, 2, NULL, NULL, {legend, 8, 8}, {7, 0}};
    static TableReference lore_reference = {(void *)0x9ac0, 1, 2, NULL, NULL, 0x30c0, 0,
                                            lore_key, 0x5678, 2, NULL, NULL};
    lore_reference.self = &lore_reference.self;
    lore_reference.self_controller = &lore_reference;
    lore_reference.entry = &lore_entry.owner;
    lore_reference.entry_controller = &lore_entry;
    static TableHistory lore_history = {(void *)0x9ac1, 1, 1, NULL, NULL};
    lore_history.reference = &lore_reference.self;
    lore_history.reference_controller = &lore_reference;
    *(Text *)((char *)player + 0xa8) = (Text){&lore_history, NULL, 0, 0};
    /* Epitaph: empty (FText::GetEmpty), its id the empty string */
    static uint16_t nothing[] = {0};
    static LocalizedHistory empty_history = {
        (void *)0x9ac2, 30000, 1, nothing, nothing, 0, {NULL, 0, 0}, NULL, NULL};
    *(Text *)((char *)player + 0xc0) = (Text){&empty_history, NULL, 0, 0};
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
    /* A template of that name, in the Blueprint's class: never found */
    Object *rival_template = object(bp_hero, "BP_Hero_C_1", bp_hero);
    *(int32_t *)((char *)rival_template + 0x70) = 99;
    /* A hero in the player's WidgetTree, as a widget is in the one of the
       widget showing it: named "BP_Hero_C_0.Sidekick" in its outer */
    Object *widget_tree_class = object(class_class, "WidgetTree", engine);
    widget_tree_class->super = object_class;
    Object *tree = object(widget_tree_class, "WidgetTree_7", player);
    Object *sidekick = object(bp_hero, "Sidekick", tree);
    *(int32_t *)((char *)sidekick + 0x70) = 12;
    /* A second hero: scripts find it by its name */
    Object *rival = object(bp_hero, "BP_Hero_C_1", level);
    *(int32_t *)((char *)rival + 0x70) = 30;
    /* The Gameplay Ability System: the player's attribute set and ability
       system component (their outer: the player's pawn), the PlayerController
       naming the pawn, FGameplayAttributeData (vtable, BaseValue,
       CurrentValue), and the setter in a library class. */
    Object *abilities = object(package_class, "/Script/GameplayAbilities", NULL);
    Object *attribute_data = object(script_struct, "GameplayAttributeData", abilities);
    attribute_data->properties_size = 16;
    property(attribute_data, float_property, "BaseValue", 4, 8, 0, NULL);
    property(attribute_data, float_property, "CurrentValue", 4, 12, 0, NULL);
    Object *gameplay_attribute = object(script_struct, "GameplayAttribute", abilities);
    gameplay_attribute->properties_size = 56;
    property(gameplay_attribute, str_property, "AttributeName", 16, 0, 0, NULL);
    property(gameplay_attribute, field_class("FieldPathProperty"), "Attribute", 32, 16, 0, NULL);
    property(gameplay_attribute, object_property, "AttributeOwner", 8, 48, 0, struct_class);
    Object *attribute_set = object(class_class, "AttributeSet", abilities);
    attribute_set->super = object_class;
    Object *component_class = object(class_class, "AbilitySystemComponent", abilities);
    component_class->super = object_class;
    Object *hero_set_class = object(class_class, "HeroAttributeSet", game);
#ifdef ANGELSCRIPT_GAS
    Object *script_set = object(class_class, "AngelscriptAttributeSet",
                                object(package_class, "/Script/AngelscriptGAS", NULL));
    script_set->super = attribute_set;
    Object *try_set = function(function_class, script_set, "TrySetAttributeBaseValue",
                               FUNC_NATIVE | FUNC_PUBLIC, exec_TrySetAttributeBaseValue,
                               3, 16, 12);
    property(try_set, field_class("NameProperty"), "AttributeName", 8, 0, CPF_PARM, NULL);
    property(try_set, float_property, "NewBaseValue", 4, 8, CPF_PARM, NULL);
    property(try_set, bool_property, "ReturnValue", 1, 12, CPF_PARM | CPF_RETURN_PARM,
             (void *)0xff010001ULL);
    hero_set_class->super = script_set;
#else
    hero_set_class->super = attribute_set;
#endif
    property(hero_set_class, struct_property, "Might", 16, 0x30, 0, attribute_data);
    property(hero_set_class, struct_property, "DamageOutgoingMult", 16, 0x40, 0, attribute_data);
    property(hero_set_class, float_property, "NotAnAttribute", 4, 0x50, 0, NULL);
    /* Something has a weak pointer to the class: it has a serial number */
    g_uobject_array.objects[0][hero_set_class->index].serial = 77;
    Object *library = object(class_class, "HeroAbilityLibrary", abilities);
    library->super = object_class;
    object(library, "Default__HeroAbilityLibrary", abilities);
#ifndef ANGELSCRIPT_GAS
    Object *set_base = function(function_class, library,
                                "SetFloatAttributeBaseFromAbilitySystemComponent",
                                FUNC_NATIVE | FUNC_STATIC | FUNC_PUBLIC,
                                exec_SetFloatAttributeBase, 4, 72, 0xffff);
    property(set_base, object_property, "AbilitySystemComponent", 8, 0, CPF_PARM,
             component_class);
    property(set_base, struct_property, "Attribute", 56, 8, CPF_PARM, gameplay_attribute);
    property(set_base, float_property, "Value", 4, 64, CPF_PARM, NULL);
    property(set_base, bool_property, "bSuccessfullyFoundAttribute", 1, 68,
             CPF_PARM | CPF_OUT_PARM, (void *)0xff010001ULL);
#endif
    Object *controller_class = object(class_class, "PlayerController", engine);
    controller_class->super = actor;
    property(controller_class, object_property, "Pawn", 8, 0x70, 0, hero);
    Object *controller = object(controller_class, "PlayerController_0", level);
    *(Object **)((char *)controller + 0x70) = player;
    /* Numbered anew on every load, as in Gothic 1 Remake */
    g_hero_set = object(hero_set_class, "HeroAttributeSet_2147445233", player);
    float *might = (float *)((char *)g_hero_set + 0x38);
    might[0] = 4;
    might[1] = 5;
    float *damage = (float *)((char *)g_hero_set + 0x48);
    damage[0] = 1;
    damage[1] = 1.5f;
    g_component = object(component_class, "AbilitySystemComponent", player);
    /* The rival's set: not the player's */
    object(hero_set_class, "HeroAttributeSet", rival);
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
        } else if (strcmp(cmd, "attribute") == 0) {
            /* Base and current value (in tenths) of Might (0) or DamageOutgoingMult (1) */
            const float *data = (float *)((char *)g_hero_set + (value ? 0x48 : 0x38));
            printf("%d %d\n", (int)(data[0] * 10 + 0.5f), (int)(data[1] * 10 + 0.5f));
            fflush(stdout);
            continue;
        } else if (strcmp(cmd, "moved") == 0) {
            printf("%d %d\n", g_moved, g_moves);
            fflush(stdout);
            continue;
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


def _build(tmp_path, *defines):
    (tmp_path / "fake_game.c").write_text(_FAKE_GAME_SOURCE)
    # Where a packaged game keeps it; detection looks for Binaries/<Platform>/.
    binary = tmp_path / "FakeGame" / "Binaries" / "Linux" / "fakegame-Linux-Shipping"
    binary.parent.mkdir(parents=True)
    subprocess.run(["gcc", "-O0", "-mno-red-zone", "-fPIE", "-pie",
                    *(f"-D{define}" for define in defines), str(tmp_path / "fake_game.c"),
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
    from grembyte.engines.registry import detect_engine
    from grembyte.features.catalog import CatalogWorker

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
            ("/Script/CoreUObject", 8), ("/Script/FakeGame", 2), ("/Game/Blueprints", 1),
            ("/Script/GameplayAbilities", 3)]
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
            ("Hero", "MoveBy", "(FVector,int32&)", "void", True, "Hero:MoveBy (FVector,int32&)"),
        ]
        assert all(method.engine == "unreal" for method in hero.values())
        [loot] = loaded["/Game/Blueprints"]
        assert loot.signature == "BP_Hero_C:Loot (AHero*,int32)"
        # Tokens hash the names: the same in every run of the game.
        assert len({m.token for m in hero.values()} | {loot.token}) == 6
    finally:
        target.close()


@requires_target
def test_trace_hooks_native_and_blueprint_functions(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.trace import NEW, SAME, TraceWorker, analyze, recording_calls

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
        assert planned[0] == {"methods": 5, "skipped": {"abstract": 1, "native": 0, "generic": 0},
                              "assemblies": ["/Script/FakeGame", "/Game/Blueprints"]}

        worker.trace([])
        _wait_for(qt_app, lambda: started or failed)
        assert not failed
        methods, info = started[0]
        assert (info["notCompiled"], info["failed"]) == (0, 0)
        assert sorted(method.name for method in methods) == [
            "AddGold", "Loot", "MoveBy", "Rename", "Tick"]
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
    from grembyte.features.catalog import CatalogWorker

    worker = CatalogWorker(pid)
    loaded = {}
    worker.assembly_loaded.connect(loaded.__setitem__)
    _run(qt_app, worker, worker.assemblies_found)
    return {method.signature: method for methods in loaded.values() for method in methods}


@requires_target
def test_method_monitor_reads_arguments_from_the_call_frame(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.monitor import MethodMonitorWorker

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
def test_the_call_dialog_calls_ufunctions_static_and_on_found_objects(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.method_call import parse_argument
    from grembyte.features.monitor import MethodMonitorWorker

    target = _Target(_build(tmp_path))
    methods = _catalog(qt_app, target.pid)
    worker = MethodMonitorWorker(target.pid)
    described, called, failed = {}, {}, {}
    worker.call_described.connect(described.__setitem__)
    worker.called.connect(called.__setitem__)
    worker.call_failed.connect(failed.__setitem__)

    def describe(signature, instance=False):
        request = worker.describe_call(methods[signature].identity(), instance)
        _wait_for(qt_app, lambda: request in described or request in failed)
        assert request not in failed, failed[request]
        return described[request]["params"]

    def answer(request):
        # Calls run when the game thread next enters ProcessEvent.
        _wait_for(qt_app, lambda: target.send("tick", 0)
                  and (request in called or request in failed))
        return called.get(request) or failed[request]

    try:
        worker.start()
        # A struct of numbers comes with its fields; the output needs no input.
        offset, distance = describe("Hero:MoveBy (FVector,int32&)")
        assert [(field["name"], field["offset"]) for field in offset["type"]["fields"]] == [
            ("X", 0), ("Y", 8), ("Z", 16)]
        assert offset["type"]["structSize"] == 0x18 and not offset.get("out")
        assert distance["out"] is True
        move_by = methods["Hero:MoveBy (FVector,int32&)"].identity()
        result = answer(worker.call(move_by, [parse_argument(offset["type"], "Z=3; X=1,5"),
                                              None]))
        assert result == {"result": None, "outputs": [
            {"name": "Distance", "value": {"text": "4"}}]}
        assert target.send("moved", 0) == (4, 1)

        # An instance function runs on a live object of its class: the first
        # found, or the one named or at an address.
        rename = methods["Hero:Rename (FString,bool)"].identity()
        name, loud = describe("Hero:Rename (FString,bool)", instance=True)
        assert (name["type"]["engine"], loud["type"]["engine"]) == ("string", "bool")
        result = answer(worker.call_found(rename, "Hero", "", ["Ann", False]))
        assert result["result"] is None
        assert result["object"]["text"].startswith('ABP_Hero_C "BP_Hero_C_0"@0x')
        assert target.send("renamed", 0) == (4, 0)
        result = answer(worker.call_found(rename, "Hero", "BP_Hero_C_1", ["Bobby", True]))
        assert result["object"]["text"].startswith('ABP_Hero_C "BP_Hero_C_1"@0x')
        assert target.send("renamed", 0) == (6, 1)
        address = result["object"]["target"]["address"]
        result = answer(worker.call_found(rename, "Hero", address, ["Al", True]))
        assert result["object"]["target"]["address"] == address
        assert target.send("renamed", 0) == (3, 1)

        # Wrong objects are refused before anything runs.
        assert "no live Hero named Nobody" in answer(
            worker.call_found(rename, "Hero", "Nobody", ["X", True]))
        assert "no Hero at 0x10" in answer(worker.call_found(rename, "Hero", "0x10", ["X", True]))
        request = worker.describe_call(rename)
        _wait_for(qt_app, lambda: request in failed)
        assert "no static method" in failed[request]
        assert target.send("renamed", 0) == (3, 1)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_scripts_call_unreal_functions_through_process_event(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.core import messages
    from grembyte.features.scripts import ScriptParam, ScriptSpec, ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid, language="de")
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
            ctx.log(hero);
            ctx.log(hero.name, hero.Health, hero.Title);
            ctx.log("hero = " + hero);
            hero.call("Rename", "Bob", true);
            return ctx.call("Hero.AddGold", 7);
        """))
        _wait_for(qt_app, lambda: calls in added or calls in failed)
        assert not failed
        run = run_on_game_thread(calls)
        assert run["error"] is None, run["error"]
        logged_object = run["logs"][0]
        assert logged_object["text"].startswith('ABP_Hero_C "BP_Hero_C_0"@0x')
        assert logged_object["targets"] == [{
            "address": logged_object["text"].partition("@")[2]}]
        assert run["logs"][1] == "BP_Hero_C_0 75 Attribute"
        assert run["logs"][2] == f"hero = {logged_object['text']}"
        assert run["result"] == "107"
        assert target.send("renamed", 0) == (4, 1)  # "Bob" and its terminating 0

        language_script = worker.add_script(ScriptSpec("return ctx.lang;"))
        _wait_for(qt_app, lambda: language_script in added)
        run = run_on_game_thread(language_script)
        assert run["error"] is None, run["error"]
        assert run["result"] == '"de"'

        # An object it returns comes with a target the inspector reads.
        inspected = []
        worker.inspected.connect(lambda _request, contents: inspected.append(contents))
        hero = worker.add_script(ScriptSpec('return ctx.find("BP_Hero_C");'))
        _wait_for(qt_app, lambda: hero in added)
        run = run_on_game_thread(hero)
        assert run["result"].startswith('ABP_Hero_C "BP_Hero_C_0"@0x')
        worker.inspect(run["target"])
        _wait_for(qt_app, lambda: inspected)
        assert inspected[0]["instanceName"] == "BP_Hero_C_0"
        assert [(field["name"], field["value"]["text"]) for field in inspected[0]["fields"]
                if field["name"] == "Health"] == [("Health", "75")]

        # TArray, TMap and TSet open in the inspector: an entry per row, a
        # TMap's named by its key; numbers have an address for the address list.
        values = {field["name"]: field["value"] for field in inspected[0]["fields"]}
        assert values["Focus"]["text"] == "2 (Dexterity)"  # an enum by the name of its value
        # An FText by its display string: generated, localized (not its source
        # string); from a string table, by its entry's string; an empty one
        assert values["Title"]["text"] == '"Attribute"'
        assert values["Motto"]["text"] == '"Mut"'
        assert values["Lore"]["text"] == '"Legende"'
        assert values["Epitaph"]["text"] == '""'
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
            assert run["result"] == "1030"  # not the template's 99

        # Every hero, named in its outer; such a name finds it, too.
        every = worker.add_script(ScriptSpec("""
            const names = [];
            const count = ctx.forEach("BP_Hero_C", hero => {
                names.push(`${ctx.outerName(hero)}=${hero.Health}`);
            });
            ctx.log(count, names.join(" "));
            let first = 0;
            ctx.forEach("BP_Hero_C", () => { first++; return false; });
            return ctx.find("BP_Hero_C", "BP_Hero_C_0.Sidekick").Health + 100 * first;
        """))
        _wait_for(qt_app, lambda: every in added)
        run = run_on_game_thread(every)
        assert run["error"] is None, run["error"]
        assert run["logs"] == ["3 /Game/Maps/Start.BP_Hero_C_0=75 BP_Hero_C_0.Sidekick=12"
                               " /Game/Maps/Start.BP_Hero_C_1=30"]
        assert run["result"] == "112"

        # Parameters: ctx.args holds the defaults, or what a run by hand
        # passes; a choice's options come from its code on the game thread.
        # ctx.findClass gives a class itself (for TSubclassOf parameters).
        chosen = worker.add_script(ScriptSpec(
            'const hero = ctx.find("BP_Hero_C").className;'
            ' return `${ctx.args.hero}+${ctx.args.n}:${ctx.findClass("BP_Hero_C").name}'
            ':${hero}=${ctx.findClass(hero).name}`;',
            params=(ScriptParam("hero", kind="choice", default="75", options="""
                        const heroes = [];
                        ctx.forEach("BP_Hero_C", hero => {
                            heroes.push({ label: ctx.outerName(hero), value: hero.Health });
                        });
                        return heroes;"""),
                    ScriptParam("n", kind="number", default="4"))))
        _wait_for(qt_app, lambda: chosen in added)
        first = run_on_game_thread(chosen)
        assert first["error"] is None, first["error"]
        assert first["result"] == '"75+4:BP_Hero_C:ABP_Hero_C=BP_Hero_C"'
        listed, refused = [], []
        worker.options_listed.connect(lambda _request, options: listed.append(options))
        worker.options_failed.connect(lambda _request, message: refused.append(message))
        worker.script_options(chosen, "hero")
        _wait_for(qt_app, lambda: target.send("tick", 0) and (listed or refused))
        assert not refused, refused
        assert listed[0] == [{"label": "/Game/Maps/Start.BP_Hero_C_0", "value": 75},
                             {"label": "BP_Hero_C_0.Sidekick", "value": 12},
                             {"label": "/Game/Maps/Start.BP_Hero_C_1", "value": 30}]
        runs.clear()
        worker.run_now(chosen, {"hero": 30, "n": 1})
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0][1]["result"] == '"30+1:BP_Hero_C:ABP_Hero_C=BP_Hero_C"'
        worker.script_options(chosen, "n")  # no choice
        _wait_for(qt_app, lambda: refused)
        assert "no choice parameter n" in messages.describe(refused[0])

        # Unknown functions and classes are reported as the script's error.
        typo = worker.add_script(ScriptSpec('ctx.find("BP_Hero_C").call("Jump");'))
        _wait_for(qt_app, lambda: typo in added)
        run = run_on_game_thread(typo)
        assert "has no function Jump with 0 argument(s)" in messages.describe(run["error"])

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


@requires_target
def test_process_event_is_found_when_it_calls_an_out_of_line_invoke(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # As in Gothic 1 Remake: ProcessEvent calls UFunction::Invoke, which
    # makes the call [reg + Func].
    target = _Target(_build(tmp_path, "OUT_OF_LINE_INVOKE"))
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append((script_id, run)))
    try:
        worker.start()
        calls = worker.add_script(ScriptSpec('return ctx.call("Hero.AddGold", 7);'))
        _wait_for(qt_app, lambda: calls in added or calls in failed)
        assert not failed
        worker.run_now(calls)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0][1]["error"] is None, runs[0][1]["error"]
        assert runs[0][1]["result"] == "107"
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
@pytest.mark.parametrize("defines", [(), ("FATEKEEPER_LAYOUT",)],
                         ids=["common-layout", "fatekeeper-layout"])
def test_gas_attributes_are_read_and_set_through_the_game(tmp_path, qt_app, defines):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker
    from grembyte.ui.widgets.gas_attributes import hold_spec

    # The layout of most games and the one of newer engines (Fatekeeper): the
    # object not first in its item of the object table (the weak pointer in
    # the FGameplayAttribute needs the item's serial), FFieldClass with a vtable.
    target = _Target(_build(tmp_path, *defines))
    worker = ScriptWorker(target.pid)
    listed, values, failed = [], [], []
    worker.gas_listed.connect(lambda _request, state: listed.append(state))
    worker.gas_failed.connect(lambda _request, message: failed.append(message))
    worker.gas_set.connect(lambda _request, answer: values.append(answer))
    worker.gas_set_failed.connect(lambda _request, message: failed.append(message))
    added, runs = [], []
    worker.script_added.connect(added.append)
    worker.script_ran.connect(lambda script_id, run: runs.append((script_id, run)))

    def set_base(name, value):
        values.clear()
        failed.clear()
        worker.gas_set_base(name, value)
        # On the game thread: when it next enters ProcessEvent
        _wait_for(qt_app, lambda: target.send("tick", 0) and (values or failed))

    try:
        worker.start()
        # The player's set (not the rival's), its attributes (not other
        # fields), the component and the game's setter, found by its parameters
        worker.gas_attributes()
        _wait_for(qt_app, lambda: listed or failed)
        assert not failed, failed
        state = listed[0]
        assert state["owner"] == "BP_Hero_C_0"
        assert state["ownerClass"] == "ABP_Hero_C"
        assert int(state["ownerAddress"], 16) != 0
        assert state["setter"] == ("HeroAbilityLibrary:"
                                   "SetFloatAttributeBaseFromAbilitySystemComponent")
        assert state["component"]
        assert state["sets"] == [{
            "name": "HeroAttributeSet_2147445233", "className": "UHeroAttributeSet",
            "attributes": [{"name": "Might", "base": 4, "current": 5},
                           {"name": "DamageOutgoingMult", "base": 1, "current": 1.5}]}]

        # Set through the game, with an FGameplayAttribute as the game makes
        # one: the game applies its effects to the current value. The set is
        # found without its object's number, too.
        set_base("HeroAttributeSet.DamageOutgoingMult", 3)
        assert not failed, failed
        set_base("HeroAttributeSet_2147445233.DamageOutgoingMult", 3)
        assert not failed, failed
        assert values == [{"base": 3, "current": 4.5}]
        assert target.send("attribute", 1) == (30, 45)
        set_base("Nope", 1)
        assert "the player has no attribute Nope" in failed[0]

        # ctx.gas in scripts: the hold script of the GAS window sets the value
        # again only when the game changed it.
        hold = worker.add_script(hold_spec("Might", 9))
        _wait_for(qt_app, lambda: hold in added)
        for expected in ('"Might = 9"', ""):
            runs.clear()
            worker.run_now(hold)
            _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
            assert runs[0][1]["error"] is None, runs[0][1]["error"]
            assert runs[0][1]["result"] == expected
        assert target.send("attribute", 0) == (90, 135)

        # ctx.gas.findSet: the set's name now, by a regular expression
        found = worker.add_script(ScriptSpec(
            'const set = ctx.gas.findSet("^HeroAttribute");\n'
            'return [set, ctx.gas.findSet(/^Nope/), ctx.gas.get(`${set}.Might`).base,\n'
            '        ctx.gas.get("HeroAttributeSet.Might").base].join(" ");'))
        _wait_for(qt_app, lambda: found in added)
        runs.clear()
        worker.run_now(found)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0][1]["error"] is None, runs[0][1]["error"]
        assert runs[0][1]["result"] == '"HeroAttributeSet_2147445233  9 9"'
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_gas_base_values_are_set_through_angelscript_attribute_sets(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptWorker

    # As in Gothic 1 Remake: no library setter; the set's own
    # TrySetAttributeBaseValue(FName, float) sets it.
    target = _Target(_build(tmp_path, "ANGELSCRIPT_GAS"))
    worker = ScriptWorker(target.pid)
    listed, values, failed = [], [], []
    worker.gas_listed.connect(lambda _request, state: listed.append(state))
    worker.gas_failed.connect(lambda _request, message: failed.append(message))
    worker.gas_set.connect(lambda _request, answer: values.append(answer))
    worker.gas_set_failed.connect(lambda _request, message: failed.append(message))

    def set_base(name, value):
        values.clear()
        failed.clear()
        worker.gas_set_base(name, value)
        _wait_for(qt_app, lambda: target.send("tick", 0) and (values or failed))

    try:
        worker.start()
        worker.gas_attributes()
        _wait_for(qt_app, lambda: listed or failed)
        assert not failed, failed
        assert listed[0]["setter"] == "AngelscriptAttributeSet:TrySetAttributeBaseValue"
        set_base("HeroAttributeSet.DamageOutgoingMult", 3)
        assert not failed, failed
        assert values == [{"base": 3, "current": 4.5}]
        assert target.send("attribute", 1) == (30, 45)
        set_base("Might", 7)
        assert values == [{"base": 7, "current": 10.5}]
        assert target.send("attribute", 0) == (70, 105)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_object_browser_lists_live_objects_by_class(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    answers, failed = [], []
    worker.classes_browsed.connect(lambda _request, answer: answers.append(answer))
    worker.browse_failed.connect(lambda _request, message: failed.append(message))
    worker.instances_browsed.connect(lambda _request, answer: answers.append(answer))
    worker.instances_failed.connect(lambda _request, message: failed.append(message))

    def ask(request, *args):
        answers.clear()
        failed.clear()
        getattr(worker, request)(*args)
        _wait_for(qt_app, lambda: answers or failed)
        assert not failed, failed
        return answers[0]

    try:
        worker.start()
        # By exact class, with the catalog's package groups; no class default
        # objects, templates (the rival's in its Blueprint class) or
        # reflection objects (packages, classes, functions, structs)
        listed = ask("browse_classes")
        classes = {entry["name"]: entry for entry in listed["classes"]}
        assert {name: (entry["count"], entry["package"], entry["role"])
                for name, entry in classes.items()} == {
            "AbilitySystemComponent": (1, "/Script/GameplayAbilities", None),
            "BP_Hero_C": (3, "/Game/Blueprints", None),
            "HeroAttributeSet": (2, "/Script/FakeGame", None),
            "Object": (5000, "/Script/CoreUObject", None),
            "PlayerController": (1, "/Script/Engine", "PlayerController"),
            "WidgetTree": (1, "/Script/Engine", None)}
        assert listed["objects"] == 5008
        assert classes["BP_Hero_C"]["cppName"] == "ABP_Hero_C"
        hero = classes["BP_Hero_C"]["klass"]

        # A class's objects, named in their outer
        instances = ask("browse_instances", hero)
        assert [(entry["name"], entry["outer"]) for entry in instances["instances"]] == [
            ("BP_Hero_C_0", "/Game/Maps/Start"), ("BP_Hero_C_1", "/Game/Maps/Start"),
            ("Sidekick", "WidgetTree_7")]
        assert all(int(entry["address"], 16) for entry in instances["instances"])
        assert (instances["count"], instances["truncated"]) == (3, False)

        # The filter: how many objects of each class are named so, and those
        listed = ask("browse_classes", "SIDE kick")
        assert {entry["name"]: entry["matched"] for entry in listed["classes"]
                if entry["matched"]} == {"BP_Hero_C": 1}
        instances = ask("browse_instances", hero, "side")
        assert [entry["name"] for entry in instances["instances"]] == ["Sidekick"]

        # Templates and reflection on request
        listed = ask("browse_classes", "default", True)
        matched = {entry["name"]: entry["matched"] for entry in listed["classes"]
                   if entry["matched"]}
        assert matched["Object"] == 1 and matched["Hero"] == 1
        assert {"Class", "Package", "Function", "ScriptStruct"} <= {
            entry["name"] for entry in listed["classes"]}
        instances = ask("browse_instances", hero, "", True)
        assert instances["count"] == 4  # with the rival's template
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
