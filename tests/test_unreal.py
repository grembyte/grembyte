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

import gzip
import json
import subprocess
import time

import pytest

from tests.test_activity import _events, fixture_activity_file  # pylint: disable=unused-import
from tests.test_hooks import (  # pylint: disable=unused-import
    _Target,
    _wait_for,
    fixture_qt_app,
    requires_target,
)


_FAKE_GAME_SOURCE = r"""
#include <ctype.h>
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

#ifdef LOWERCASE_NAMES
/* FNames ignore case and the pool keeps the spelling registered first: in
   Stellar Blade UStruct is "struct". These names are stored lowercase. */
static const char *const LOWERCASE[] = {
    "Class", "Struct", "Function", "ScriptStruct", "Package", "Enum", "Default__Object",
    "X", "Y", "Rotation", "ExecuteUbergraph", "Actor", "WidgetTree", "PlayerController",
    "Pawn", "AttributeSet", "AbilitySystemComponent", "GameplayAttribute", "BaseValue",
    "CurrentValue", "AttributeName", "Attribute", "AttributeOwner", "SoftObjectPath",
    "AssetPath", "PackageName", "AssetName", "SubPathString", NULL
};

static int lowercased(const char *text) {
    size_t length = strlen(text);
    if (length > 8 && strcmp(text + length - 8, "Property") == 0)
        return 1;  /* every property kind */
    for (int i = 0; LOWERCASE[i] != NULL; i++) {
        if (strcmp(text, LOWERCASE[i]) == 0)
            return 1;
    }
    return 0;
}
#endif

static uint32_t name(const char *text) {
    size_t length = strlen(text);
    uint8_t *entry = g_name_pool.blocks[0] + g_name_pool.cursor;
    uint16_t header = (uint16_t)(length << 6);
    memcpy(entry, &header, 2);
    memcpy(entry + 2, text, length);
#ifdef LOWERCASE_NAMES
    /* not None and ByteProperty, the engine's first entries */
    if (g_name_pool.cursor >= 20 && lowercased(text)) {
        for (size_t i = 0; i < length; i++)
            entry[2 + i] = (uint8_t)tolower(entry[2 + i]);
    }
#endif
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
#define EX_VIRTUAL_FUNCTION 0x1B
#define EX_FINAL_FUNCTION 0x1C
#define EX_INT_CONST 0x1D
#define EX_LOCAL_VIRTUAL_FUNCTION 0x45
#define EX_CALL_MATH 0x68

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

/* TSoftClassPtr (UE5.1+): a TPersistentObjectPtr, the weak pointer and
   FSoftObjectPath (FTopLevelAssetPath: the package's and the asset's FName,
   then the subobject's path) */
typedef struct { uint32_t package, package_number, asset, asset_number; String sub; } SoftPath;
typedef struct { int32_t index, serial; SoftPath path; } SoftPointer;
_Static_assert(sizeof(SoftPointer) == 0x28, "FSoftObjectPtr");
static SoftPointer g_equipped;
static char g_equipped_sub[64];
static volatile int32_t g_equips;
static Object *g_soft_class, *g_soft_sidekick;

/* Equip(TSoftClassPtr<Hero> Kind): keeps what it was given */
__attribute__((noinline)) void exec_Equip(Object *context, Frame *stack, void *result) {
    (void)context;
    (void)result;
    g_equipped = *(SoftPointer *)stack->locals;
    memset(g_equipped_sub, 0, sizeof g_equipped_sub);
    for (int i = 0; i < g_equipped.path.sub.num && i < 63; i++)
        g_equipped_sub[i] = (char)g_equipped.path.sub.data[i];
    g_equipped.path.sub.data = NULL;
    g_equips = g_equips + 1;
}

static void append_name(char *text, uint32_t index) {
    const uint8_t *entry = g_name_pool.blocks[0] + index * 2;
    uint16_t header;
    memcpy(&header, entry, 2);
    strncat(text, (const char *)entry + 2, header >> 6);
}

/* Whether the soft pointer Equip was given names ``target`` by its path */
static int names(Object *target) {
    Object *chain[8];
    int n = 0;
    for (Object *current = target; current != NULL && n < 8; current = current->outer)
        chain[n++] = current;
    const SoftPath *path = &g_equipped.path;
    if (path->package != chain[n - 1]->name || path->package_number != chain[n - 1]->number)
        return 0;
    if (path->asset != (n > 1 ? chain[n - 2]->name : 0))
        return 0;
    char sub[64] = "";
    for (int i = n - 3; i >= 0; i--) {
        append_name(sub, chain[i]->name);
        if (i > 0)
            strcat(sub, ".");
    }
    return strcmp(sub, g_equipped_sub) == 0;
}

/* Whether the player plays (ctx.isPlaying): GameplayStatics' IsGamePaused,
   and the actor the controller's camera shows */
static volatile int32_t g_paused;
static Object *volatile g_view_target;

__attribute__((noinline)) void exec_IsGamePaused(Object *context, Frame *stack, void *result) {
    (void)context;
    (void)stack;
    *(uint8_t *)result = (uint8_t)g_paused;
}

__attribute__((noinline)) void exec_GetViewTarget(Object *context, Frame *stack, void *result) {
    (void)context;
    (void)stack;
    *(Object **)result = g_view_target;
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
    FieldClass *name_property = field_class("NameProperty");
    Object *top_level_path = object(script_struct, "TopLevelAssetPath", core);
    top_level_path->properties_size = 16;
    property(top_level_path, name_property, "PackageName", 8, 0, 0, NULL);
    property(top_level_path, name_property, "AssetName", 8, 8, 0, NULL);
    Object *soft_path = object(script_struct, "SoftObjectPath", core);
    soft_path->properties_size = 32;
    property(soft_path, struct_property, "AssetPath", 16, 0, 0, top_level_path);
    property(soft_path, str_property, "SubPathString", 16, 16, 0, NULL);

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
    Object *equip = function(function_class, hero, "Equip", FUNC_NATIVE | FUNC_STATIC | FUNC_PUBLIC,
                             exec_Equip, 1, 0x28, 0xffff);
    Field *kind = property(equip, field_class("SoftClassProperty"), "Kind", 0x28, 0, CPF_PARM,
                           class_class);
    kind->extra[1] = hero;

    Object *blueprint = object(package_class, "/Game/Blueprints/BP_Hero", NULL);
    Object *bp_hero = object(generated_class, "BP_Hero_C", blueprint);
    bp_hero->super = hero;
#ifdef ANIM_BLUEPRINT
    /* The hero's Animation Blueprint (Lies of P: BP_CH_PC_Pino_C and
       ABP_CH_PC_Pino_C): a class named as the hero's with a C++ prefix */
    Object *anim_blueprint = object(package_class, "/Game/Blueprints/ABP_Hero", NULL);
    Object *abp_hero = object(generated_class, "ABP_Hero_C", anim_blueprint);
    abp_hero->super = object_class;
#endif
    /* Something has a weak pointer to the class: it has a serial number */
    g_uobject_array.objects[0][bp_hero->index].serial = 5;
    g_soft_class = bp_hero;
    g_loot = function(function_class, bp_hero, "Loot", FUNC_PUBLIC, ProcessInternal,
                      2, 12, 0xffff);
    property(g_loot, object_property, "Target", 8, 0, CPF_PARM, hero);
    Field *amount = property(g_loot, int_property, "Amount", 4, 8, CPF_PARM, NULL);
    /* AddGold(Amount): the argument, then the end of the arguments (where
       the interpreter's frame points when it calls AddGold). Then the calls
       the call graph finds: AddGold through EX_CallMath, Tick virtually on
       self, Rename through EX_FinalFunction; between them an int constant
       holding EX_FinalFunction's token, and a virtual call of a name no
       function has (Amount). */
    static uint8_t script[256];
    size_t at = 0;
    script[at++] = EX_LOCAL_VARIABLE;
    memcpy(script + at, &amount, sizeof(amount));
    at += sizeof(amount);
    script[at++] = EX_END_FUNCTION_PARMS;
    script[at++] = EX_CALL_MATH;
    memcpy(script + at, &g_add_gold, sizeof(g_add_gold));
    at += sizeof(g_add_gold);
    script[at++] = EX_END_FUNCTION_PARMS;
    uint32_t tick_name[3] = {g_tick->name, g_tick->name, 0};
    script[at++] = EX_LOCAL_VIRTUAL_FUNCTION;
    memcpy(script + at, tick_name, sizeof(tick_name));
    at += sizeof(tick_name);
    script[at++] = EX_END_FUNCTION_PARMS;
    static const uint8_t noise[] = {EX_INT_CONST, EX_FINAL_FUNCTION, 1, 2, 3};
    memcpy(script + at, noise, sizeof(noise));
    at += sizeof(noise);
    script[at++] = EX_FINAL_FUNCTION;
    memcpy(script + at, &g_rename, sizeof(g_rename));
    at += sizeof(g_rename);
    script[at++] = EX_END_FUNCTION_PARMS;
    uint32_t amount_name[3] = {amount->name, amount->name, 0};
    script[at++] = EX_VIRTUAL_FUNCTION;
    memcpy(script + at, amount_name, sizeof(amount_name));
    at += sizeof(amount_name);
    script[at++] = EX_END_FUNCTION_PARMS;
    g_loot->script = script;
    g_loot->script_num = g_loot->script_max = (int32_t)at;

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
    g_soft_sidekick = sidekick;
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
#ifdef NO_GAS
    /* A game without the Gameplay Ability System (Lies of P): no class of
       these names, its own stat classes instead */
    Object *attribute_set = object(class_class, "StatSet", abilities);
    attribute_set->super = object_class;
    Object *component_class = object(class_class, "StatComponent", abilities);
#else
    Object *attribute_set = object(class_class, "AttributeSet", abilities);
    attribute_set->super = object_class;
    Object *component_class = object(class_class, "AbilitySystemComponent", abilities);
#endif
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
    controller_class->properties_size = 0x78;
    Object *controller = object(controller_class, "PlayerController_0", level);
    *(Object **)((char *)controller + 0x70) = player;
    Object *view_target = function(function_class, controller_class, "GetViewTarget",
                                   FUNC_NATIVE | FUNC_PUBLIC, exec_GetViewTarget, 1, 8, 0);
    property(view_target, object_property, "ReturnValue", 8, 0, CPF_PARM | CPF_RETURN_PARM, actor);
    g_view_target = player;
    Object *statics = object(class_class, "GameplayStatics", engine);
    statics->super = object_class;
    object(statics, "Default__GameplayStatics", engine);
    Object *is_paused = function(function_class, statics, "IsGamePaused",
                                 FUNC_NATIVE | FUNC_STATIC | FUNC_PUBLIC, exec_IsGamePaused,
                                 2, 9, 8);
    property(is_paused, object_property, "WorldContextObject", 8, 0, CPF_PARM, object_class);
    property(is_paused, bool_property, "ReturnValue", 1, 8, CPF_PARM | CPF_RETURN_PARM,
             (void *)0xff010001ULL);
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
        } else if (strcmp(cmd, "pause") == 0) {
            g_paused = value;
        } else if (strcmp(cmd, "camera") == 0) {
            /* 1: on the player, 0: on a camera of its own (a menu, a cutscene) */
            g_view_target = value ? player : controller;
        } else if (strcmp(cmd, "equipped") == 0) {
            /* 0: whether the path names BP_Hero_C, the sidekick; 1: whether
               the weak pointer names BP_Hero_C, the calls */
            const Item *item = g_equipped.index < 0 ? NULL
                : &g_uobject_array.objects[0][g_equipped.index];
            if (value == 0)
                printf("%d %d\n", names(g_soft_class), names(g_soft_sidekick));
            else
                printf("%d %d\n", item != NULL && item->object == g_soft_class
                                      && item->serial == g_equipped.serial, g_equips);
            fflush(stdout);
            continue;
        } else if (strcmp(cmd, "renamed") == 0) {
            printf("%d %d\n", g_name_length, g_loud);
            fflush(stdout);
            continue;
        } else if (strcmp(cmd, "newclass") == 0) {
            /* A class loaded while the game runs: LateClass<value> */
            char text[32];
            snprintf(text, sizeof(text), "LateClass%d", value);
            object(class_class, text, game)->super = object_class;
        } else if (strcmp(cmd, "loop") == 0) {
            /* Classes whose supers run in a circle, as freed memory may read,
               and an object of one of them */
            Object *first = object(class_class, "LoopA", game);
            Object *second = object(class_class, "LoopB", game);
            first->super = second;
            second->super = first;
            object(first, "LoopA_0", game);
        } else if (strcmp(cmd, "dangling") == 0) {
            /* An item whose object is gone: its memory is no longer mapped */
            int32_t index = g_uobject_array.num++;
            g_uobject_array.objects[index / 65536][index % 65536].object = (Object *)(uintptr_t)0x1000;
        } else if (strcmp(cmd, "reclass") == 0) {
            /* The player's memory now holds an object of another class, as
               after the player was freed and the memory given anew */
            player->klass = actor;
        } else if (strcmp(cmd, "entry") == 0) {
            /* The first 8 bytes of ProcessEvent: hooked or not */
            const volatile int32_t *code = (const volatile int32_t *)(void *)UObject_ProcessEvent;
            printf("%d %d\n", code[0], code[1]);
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
@pytest.mark.parametrize("defines", [(), ("LOWERCASE_NAMES",)], ids=["default", "lowercase"])
def test_catalog_lists_packages_classes_and_ufunctions(tmp_path, qt_app, defines):
    # pylint: disable=import-outside-toplevel
    from grembyte.engines.registry import detect_engine
    from grembyte.features.catalog import CatalogWorker

    target = _Target(_build(tmp_path, *defines))
    try:
        engine = detect_engine(target.pid)
        assert engine.id == "unreal"

        worker = CatalogWorker(target.pid)
        loaded = {}
        worker.assembly_loaded.connect(loaded.__setitem__)
        found = _run(qt_app, worker, worker.assemblies_found)[0][0]
        # Packages with functions; Blueprints grouped by their first folder
        assert [(a["name"], a["classes"]) for a in found] == [
            ("/Script/CoreUObject", 8), ("/Script/Engine", 5), ("/Script/FakeGame", 2),
            ("/Game/Blueprints", 1), ("/Script/GameplayAbilities", 3)]
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
            ("Hero", "Equip", "(TSoftClassPtr<AHero>)", "void", True,
             "Hero:Equip (TSoftClassPtr<AHero>)"),
        ]
        assert all(method.engine == "unreal" for method in hero.values())
        [loot] = loaded["/Game/Blueprints"]
        assert loot.signature == "BP_Hero_C:Loot (AHero*,int32)"
        # Tokens hash the names: the same in every run of the game.
        assert len({m.token for m in hero.values()} | {loot.token}) == 7
    finally:
        target.close()


@requires_target
def test_call_graph_finds_the_calls_in_blueprint_bytecode(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.call_graph import CallGraphWorker
    from grembyte.features.catalog import CatalogWorker

    target = _Target(_build(tmp_path))
    worker = None
    try:
        catalog = CatalogWorker(target.pid)
        loaded = {}
        catalog.assembly_loaded.connect(loaded.__setitem__)
        _run(qt_app, catalog, catalog.assemblies_found)
        [loot] = loaded["/Game/Blueprints"]
        hero = {method.name: method for method in loaded["/Script/FakeGame"]}

        worker = CallGraphWorker(target.pid)
        callees, callers, failed = [], [], []
        worker.callees_found.connect(
            lambda _id, _method, calls, body: callees.append((calls, body)))
        worker.callers_found.connect(lambda _id, _method, calls, _missing: callers.append(calls))
        worker.request_failed.connect(lambda _id, message: failed.append(message))
        worker.worker_failed.connect(failed.append)
        worker.start()
        # In the order of the bytecode: AddGold (EX_CallMath), Tick virtually on
        # self (Hero's, BP_Hero_C's base), Rename (EX_FinalFunction); not the
        # int constant holding a call token, nor the virtual call of Amount
        worker.callees(loot)
        _wait_for(qt_app, lambda: callees or failed)
        assert not failed, failed
        calls, body = callees[0]
        assert [(call.opcode, call.method.signature) for call in calls] == [
            ("call", "Hero:AddGold (int32)"), ("virtual", "Hero:Tick (float)"),
            ("call", "Hero:Rename (FString,bool)")]
        # The same function as the catalog's (named by its signature until
        # its package is read)
        assert body == [] and (calls[0].method.assembly, calls[0].method.token) == (
            hero["AddGold"].assembly, hero["AddGold"].token)
        # A native function has no bytecode: it calls nothing seen
        callees.clear()
        worker.callees(hero["AddGold"])
        _wait_for(qt_app, lambda: callees or failed)
        assert callees[0] == ([], [])
        # Who calls AddGold: the Blueprint, found in the other package group
        worker.callers(hero["AddGold"])
        _wait_for(qt_app, lambda: callers or failed)
        assert not failed, failed
        assert [call.method for call in callers[0]] == [loot]
        assert list((tmp_path / "catalog-cache" / "unreal").rglob("*.calls.json.gz"))
    finally:
        if worker is not None:
            worker.requestInterruption()
            worker.wait(5000)
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
        assert planned[0] == {"methods": 6, "skipped": {"abstract": 1, "native": 0, "generic": 0},
                              "assemblies": ["/Script/FakeGame", "/Game/Blueprints"]}

        worker.trace([])
        _wait_for(qt_app, lambda: started or failed)
        assert not failed
        methods, info = started[0]
        assert (info["notCompiled"], info["failed"]) == (0, 0)
        assert sorted(method.name for method in methods) == [
            "AddGold", "Equip", "Loot", "MoveBy", "Rename", "Tick"]
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
        assert call["self"]["text"].startswith('BP_Hero_C "BP_Hero_C_0"@0x')
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
        assert result["object"]["text"].startswith('BP_Hero_C "BP_Hero_C_0"@0x')
        assert target.send("renamed", 0) == (4, 0)
        result = answer(worker.call_found(rename, "Hero", "BP_Hero_C_1", ["Bobby", True]))
        assert result["object"]["text"].startswith('BP_Hero_C "BP_Hero_C_1"@0x')
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
def test_class_members_follow_unreal_types(tmp_path, qt_app):
    # The completion asks by the C++ type a field shows (ABP_Hero_C*): the
    # class is found without the prefix and the pointer
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    answers, failed = [], []
    worker.members_read.connect(lambda _request, answer: answers.append(answer))
    worker.members_failed.connect(lambda _request, message: failed.append(message))

    def members(class_name):
        answers.clear()
        failed.clear()
        worker.class_members(class_name)
        _wait_for(qt_app, lambda: answers or failed)
        assert not failed
        return answers[0]

    try:
        worker.start()
        controller = members("PlayerController")
        pawn = next(field for field in controller["fields"] if field["name"] == "Pawn")
        assert pawn["type"].endswith("*")
        hero = members(pawn["type"])
        assert hero is not None and "Health" in {field["name"] for field in hero["fields"]}
        assert "Rename" in {method["name"] for method in hero["methods"]}
        assert members("ANothing*") is None
        # A struct (the element of a TArray<FVector>, a field's FVector)
        vector = members("FVector")
        assert vector is not None and vector["name"] == "FVector"
        assert {"X", "Y", "Z"} <= {field["name"] for field in vector["fields"]}

        # The classes ctx.find finds: those with live objects
        names = []
        worker.names_read.connect(lambda _request, answer: names.append(answer))
        worker.class_names()
        _wait_for(qt_app, lambda: names or failed)
        assert not failed
        assert {"BP_Hero_C", "PlayerController"} <= set(names[0])
        assert names[0] == sorted(names[0])

        # What a tooltip shows: base classes and the live objects with subclasses
        infos = []
        worker.info_read.connect(lambda _request, answer: infos.append(answer))
        worker.class_info("BP_Hero_C")
        worker.class_info("ANothing")
        _wait_for(qt_app, lambda: len(infos) == 2 or failed)
        assert not failed
        hero, nothing = infos
        assert hero["name"] == "BP_Hero_C" and hero["parents"]
        assert hero["objects"] >= 2  # BP_Hero_C_0 and BP_Hero_C_1
        assert nothing is None
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
@pytest.mark.parametrize("defines", [(), ("LOWERCASE_NAMES",)], ids=["default", "lowercase"])
def test_scripts_call_unreal_functions_through_process_event(tmp_path, qt_app, defines):
    # pylint: disable=import-outside-toplevel
    from grembyte.core import messages
    from grembyte.features.scripts import ScriptParam, ScriptSpec, ScriptWorker

    target = _Target(_build(tmp_path, *defines))
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
        assert logged_object["text"].startswith('BP_Hero_C "BP_Hero_C_0"@0x')
        assert logged_object["targets"] == [{
            "address": logged_object["text"].partition("@")[2]}]
        assert run["logs"][1] == "BP_Hero_C_0 75 Attribute"
        assert run["logs"][2] == f"hero = {logged_object['text']}"
        assert run["result"] == "107"
        assert target.send("renamed", 0) == (4, 1)  # "Bob" and its terminating 0

        # Arguments that do not suit their parameters stop the call instead of
        # reaching the game as other values (swapped: true as a number).
        def refusal(code):
            script_id = worker.add_script(ScriptSpec(code))
            _wait_for(qt_app, lambda: script_id in added)
            return run_on_game_thread(script_id)["error"]

        assert target.send("renamed", 0) == (4, 1)
        swapped = refusal('ctx.find("BP_Hero_C").call("Rename", true, "Bob");')
        assert 'error.monitor.argument_mismatch:{"name":"NewName","type":"FString",' \
               '"value":"true"}' in swapped
        assert "bLoud" in refusal('ctx.find("BP_Hero_C").Rename("Bob", 1);')
        assert "Amount" in refusal('return ctx.call("Hero.AddGold", "7");')
        assert target.send("renamed", 0) == (4, 1)  # nothing was called

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
        assert run["result"].startswith('BP_Hero_C "BP_Hero_C_0"@0x')
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

        # ctx.object: the object at an address (pointer, text or number) as
        # a wrapper, null for a null pointer, an error for what is none
        at = worker.add_script(ScriptSpec("""
            const hero = ctx.find("BP_Hero_C", "BP_Hero_C_1");
            const again = ctx.object(hero.address);
            const fromText = ctx.object(hero.address.toString());
            const errors = [];
            for (const wrong of [hero.address.add(8), "nowhere"]) {
                try { ctx.object(wrong); } catch (error) { errors.push(error.message); }
            }
            ctx.log(errors.length);
            return again.Health + fromText.Health + (ctx.object(0) === null ? 1000 : 0)
                + (ctx.object(again) === again ? 100 : 0);
        """))
        _wait_for(qt_app, lambda: at in added)
        run = run_on_game_thread(at)
        assert run["error"] is None, run["error"]
        assert run["logs"] == ["2"]
        assert run["result"] == "1160"

        # TSoftClassPtr arguments: a loaded class (weak pointer and path), a
        # subobject (its path: package, asset, the rest), null, no object
        def equip(argument):
            script = worker.add_script(ScriptSpec(f'ctx.call("Hero.Equip", {argument});'))
            _wait_for(qt_app, lambda: script in added)
            return run_on_game_thread(script)["error"]

        assert equip('ctx.findClass("BP_Hero_C")') is None
        assert target.send("equipped", 0) == (1, 0)
        assert target.send("equipped", 1) == (1, 1)
        assert equip('ctx.find("BP_Hero_C", "BP_Hero_C_0.Sidekick")') is None
        assert target.send("equipped", 0) == (0, 1)
        assert target.send("equipped", 1) == (0, 2)
        assert equip("null") is None
        assert target.send("equipped", 0) == (0, 0)
        assert "error.unreal.soft_pointer_object" in equip('"BP_Hero_C"')

        # ctx.isPlaying: a pawn, not paused, the camera on the pawn
        playing = worker.add_script(ScriptSpec("return ctx.isPlaying();"))
        _wait_for(qt_app, lambda: playing in added)
        assert run_on_game_thread(playing)["result"] == "true"
        target.send("pause", 1)
        assert run_on_game_thread(playing)["result"] == "false"
        target.send("pause", 0)
        target.send("camera", 0)
        assert run_on_game_thread(playing)["result"] == "false"
        target.send("camera", 1)
        assert run_on_game_thread(playing)["result"] == "true"

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
        assert first["result"] == '"75+4:BP_Hero_C:BP_Hero_C=BP_Hero_C"'
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
        assert runs[0][1]["result"] == '"30+1:BP_Hero_C:BP_Hero_C=BP_Hero_C"'
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
def test_the_game_thread_hook_stays_between_script_runs(tmp_path, qt_app, activity_file):
    # pylint: disable=import-outside-toplevel
    from grembyte.core import settings
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # Hooking and unhooking ProcessEvent for every run rewrote its first bytes
    # while the game thread ran them; RoboCop crashed at ProcessEvent+4. The
    # hook is set once and stays, and runs after the first use it.
    target = _Target(_build(tmp_path))
    original = target.send("entry", 0)
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append((script_id, run)))
    settings.apply({"script_timings": True})
    try:
        worker.start()
        calls = worker.add_script(ScriptSpec('return ctx.call("Hero.AddGold", 1);'))
        _wait_for(qt_app, lambda: calls in added or calls in failed)
        assert not failed
        hooked = None
        for expected in range(101, 106):
            if expected == 104:
                settings.apply({"script_timings": False})  # reaches the running agent
            runs.clear()
            worker.run_now(calls)
            _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
            assert runs[0][1]["error"] is None, runs[0][1]["error"]
            assert runs[0][1]["result"] == str(expected)
            # Ticks without waiting work pass the hook and leave it in place
            for _ in range(3):
                target.send("tick", 0)
                qt_app.processEvents()
            entry = target.send("entry", 0)
            assert entry != original
            assert hooked in (None, entry)  # not rewritten anew
            hooked = entry
    finally:
        settings.apply({"script_timings": False})
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
    # Setting the hook is in the activity log, step by step, once
    steps = [(entry["event"], entry.get("step")) for entry in _events(activity_file)
             if entry.get("pid") == target.pid and entry["event"].startswith("main_gate_")]
    assert steps == [("main_gate_preparing", "main_thread"), ("main_gate_preparing", "cmodule"),
                     ("main_gate_pausing", None), ("main_gate_attaching", None),
                     ("main_gate_attached", None)]
    # So is the prewarm, each step as it begins; the hook is set within it
    events = [(entry["event"], entry.get("step")) for entry in _events(activity_file)
              if entry.get("pid") == target.pid and (entry["event"] == "prewarm_step"
                                                     or entry["event"].startswith("main_gate_"))]
    assert [step for event, step in events if event == "prewarm_step"] == [
        "reflection", "process_event", "main_gate", "objects", "done"]
    assert events.index(("prewarm_step", "main_gate")) + 1 == events.index(
        ("main_gate_preparing", "main_thread"))
    assert events.index(("main_gate_attached", None)) + 1 == events.index(
        ("prewarm_step", "objects"))
    phases = [entry for entry in _events(activity_file) if entry.get("pid") == target.pid
              and entry["event"] == "script_phases"]
    # With the setting script_timings each run's timings. Right after
    # attaching the agent found the reflection and ProcessEvent, set the hook
    # and read the objects (script_prewarm); the first run only looks up its
    # class and function, later runs take all of it from the caches.
    # Switched off, runs 4 and 5 are not measured.
    [prewarm] = [entry for entry in _events(activity_file) if entry.get("pid") == target.pid
                 and entry["event"] == "script_prewarm"]
    for phase in ("name_pool", "object_array", "layout", "snapshot", "process_event",
                  "main_gate"):
        assert prewarm[f"{phase}_count"] == 1 and prewarm[f"{phase}_ms"] >= 0, phase
    assert not prewarm["failed"] and prewarm["name_reads_count"] > 0
    assert [entry["script_run"] for entry in phases] == [1, 2, 3]
    first = phases[0]
    assert first["find_class_count"] == 1 and first["signature_count"] == 1
    assert not any(f"{phase}_count" in first for phase in ("name_pool", "snapshot", "main_gate"))
    # Later runs only prepare what the first looked up, on the agent's thread
    assert all(not any(key.endswith("_count") for key in entry) for entry in phases[1:])
    assert "prepare_ms" not in first
    assert all(0 <= entry["prepare_ms"] <= entry["wait_ms"] for entry in phases[1:])
    assert all(entry["total_ms"] >= entry["game_thread_ms"] and not entry["failed"]
               and isinstance(entry["run"], str) for entry in phases)


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
@pytest.mark.parametrize("defines", [(), ("FATEKEEPER_LAYOUT",), ("LOWERCASE_NAMES",)],
                         ids=["common-layout", "fatekeeper-layout", "lowercase"])
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
        assert state["owner"] == "BP_Hero_C_0" and state["gas"] is True
        assert state["ownerClass"] == "BP_Hero_C"
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
def test_reference_graph_names_the_objects_holding_one(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.object_references import ReferenceIndex
    from grembyte.features.scripts import ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    answers, failed = [], []
    for answer in ("classes_browsed", "instances_browsed", "references_resolved"):
        getattr(worker, answer).connect(lambda _request, value: answers.append(value))
    for failure in ("browse_failed", "instances_failed", "references_failed"):
        getattr(worker, failure).connect(lambda _request, message: failed.append(message))

    def ask(request, *args):
        answers.clear()
        failed.clear()
        getattr(worker, request)(*args)
        _wait_for(qt_app, lambda: answers or failed)
        assert not failed, failed
        return answers[0]

    try:
        worker.start()
        classes = {entry["name"]: entry for entry in ask("browse_classes")["classes"]}
        player = next(entry for entry in
                      ask("browse_instances", classes["BP_Hero_C"]["klass"])["instances"]
                      if entry["name"] == "BP_Hero_C_0")
        index = ReferenceIndex.build(target.pid)
        locations, _total = index.referrers(int(player["address"], 16), limit=1000)
        heap = [location for location in locations if index.place(location)[0] is None]
        resolved = ask("resolve_references", heap)
        named = {(entry["kind"], entry.get("className"), entry.get("field"), entry.get("name"))
                 for entry in resolved}
        # A property of the controller, and the widget tree whose outer it is;
        # the object table's chunk is no object
        assert ("field", "APlayerController", "Pawn", "PlayerController_0") in named
        assert ("field", "UWidgetTree", "OuterPrivate", "WidgetTree_7") in named
        assert ("unknown", None, None, None) in named
        # The other way: the object's own address names it with its size, and
        # the pointers inside lead to its class (ClassPrivate)
        address = int(player["address"], 16)
        own = ask("resolve_references", [address])[0]
        assert int(own["object"], 16) == address and own["size"] >= 0x28
        pairs, _total = index.pointers_in(address, address + own["size"])
        assert int(classes["BP_Hero_C"]["klass"], 16) in {value for _location, value in pairs}
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
        assert classes["BP_Hero_C"]["cppName"] == "BP_Hero_C"
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


@requires_target
def test_scripts_keep_the_offsets_they_found_per_game_build(tmp_path, qt_app, activity_file,
                                                            monkeypatch):
    # pylint: disable=import-outside-toplevel,too-many-locals
    from grembyte.core import settings
    from grembyte.features import catalog_cache
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # The first script run searched the name pool and the object table for
    # seconds (RoboCop: 2.4 of 3.2 s). The offsets are kept per game build and
    # only checked the next time; a wrong one is searched again and replaced.
    cache = tmp_path / "cache"
    monkeypatch.setenv("GREMBYTE_CACHE_DIR", str(cache))
    target = _Target(_build(tmp_path))

    def first_run():
        worker = ScriptWorker(target.pid)
        added, failed, runs = [], {}, []
        worker.script_added.connect(added.append)
        worker.script_failed.connect(failed.__setitem__)
        worker.script_ran.connect(lambda script_id, run: runs.append(run))
        try:
            worker.start()
            calls = worker.add_script(ScriptSpec('return ctx.call("Hero.AddGold", 1);'))
            _wait_for(qt_app, lambda: calls in added or calls in failed)
            assert not failed
            worker.run_now(calls)
            _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
            assert runs[0]["error"] is None, runs[0]["error"]
        finally:
            worker.requestInterruption()
            worker.wait(5000)
        return [entry for entry in _events(activity_file) if entry.get("pid") == target.pid
                and entry["event"] == "script_phases"][-1]

    def stored():
        [path] = cache.glob("unreal/*/*.offsets.json.gz")
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return path, json.load(file)

    settings.apply({"script_timings": True, "script_prewarm": False})
    try:
        searched = first_run()
        assert searched["name_pool_count"] == 1 and searched["process_event_count"] == 1
        path, kept = stored()
        offsets = kept[catalog_cache.OFFSETS]
        assert set(offsets) == {"namePool", "objectArray", "processEvent"}

        checked = first_run()  # a fresh agent: only checks them
        assert checked["stored_offsets_count"] == 2
        assert not any(f"{phase}_count" in checked
                       for phase in ("data_ranges", "name_pool", "object_array", "process_event"))

        wrong = dict(offsets, namePool=offsets["namePool"] + 8)
        with gzip.open(path, "wt", encoding="utf-8") as file:
            json.dump(dict(kept, offsets=wrong), file)
        searched_again = first_run()
        assert searched_again["name_pool_count"] == 1
        assert "process_event_count" not in searched_again  # the stored one still fits
        assert stored()[1][catalog_cache.OFFSETS] == offsets
    finally:
        settings.apply({"script_timings": False, "script_prewarm": True})
        target.close()


@requires_target
def test_a_class_newer_than_the_snapshot_is_found(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # On the game thread a snapshot serves for minutes (reading one there
    # stalls the game); a class it does not have yet is looked for in a
    # fresh one.
    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append(run))
    try:
        worker.start()
        find = worker.add_script(ScriptSpec('return String(ctx.findClass("LateClass7"));'))
        _wait_for(qt_app, lambda: find in added or find in failed)
        assert not failed
        worker.run_now(find)  # read the snapshot; the class is not there yet
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert "LateClass7" in (runs[0]["error"] or "")
        time.sleep(1.1)  # the snapshot is older than a second
        target.send("newclass", 7)
        runs.clear()
        worker.run_now(find)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0]["error"] is None, runs[0]["error"]
        assert runs[0]["result"].startswith("\"UClass@")
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_a_super_chain_in_a_circle_ends(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # Read from memory freed since the snapshot (a level changed), a class's
    # supers may run in a circle; walking them must end (RoboCop froze).
    target = _Target(_build(tmp_path))
    target.send("loop", 0)
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append(run))
    try:
        worker.start()
        # A name no hero has: the search passes every object
        find = worker.add_script(ScriptSpec('return String(ctx.find("Hero", "Nobody"));'))
        _wait_for(qt_app, lambda: find in added or find in failed)
        assert not failed
        worker.run_now(find)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0]["error"] is None, runs[0]["error"]
        assert runs[0]["result"] == '"null"'
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_the_snapshot_skips_an_object_freed_while_it_is_read(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # The snapshot is read in C; an object freed meanwhile faults there and
    # is skipped, as the JavaScript reader skipped it, instead of ending the
    # game. The objects after it are still found.
    target = _Target(_build(tmp_path))
    target.send("dangling", 0)
    target.send("newclass", 3)  # an object after the dangling item
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append(run))
    try:
        worker.start()
        find = worker.add_script(ScriptSpec(
            'return String(ctx.find("Hero")) + " " + String(ctx.findClass("LateClass3"));'))
        _wait_for(qt_app, lambda: find in added or find in failed)
        assert not failed
        worker.run_now(find)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0]["error"] is None, runs[0]["error"]
        assert "Hero" in runs[0]["result"] and "UClass@" in runs[0]["result"]
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_an_object_of_another_class_now_is_not_found(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # The snapshot keeps each object's class as it was read. An object freed
    # since, whose memory now holds an object of another class, is passed
    # over: RoboCop's scripts got another object instead of the
    # PlayerController and read its Pawn from the wrong place.
    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    added, failed, runs = [], {}, []
    worker.script_added.connect(added.append)
    worker.script_failed.connect(failed.__setitem__)
    worker.script_ran.connect(lambda script_id, run: runs.append(run))
    try:
        worker.start()  # reads the snapshot right after attaching
        find = worker.add_script(ScriptSpec('return String(ctx.find("BP_Hero_C", "BP_Hero_C_0"));'))
        _wait_for(qt_app, lambda: find in added or find in failed)
        assert not failed
        target.send("reclass", 0)
        worker.run_now(find)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0]["error"] is None, runs[0]["error"]
        assert runs[0]["result"] == '"null"'
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_scripts_find_what_they_looked_up_before_the_game_thread_runs_them(
        tmp_path, qt_app, activity_file):
    # pylint: disable=import-outside-toplevel,too-many-locals
    from grembyte.core import settings
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    # A search on the game thread reads the object table: RoboCop stalled
    # 130-260 ms about once a minute (2026-11-20). The agent learns what a
    # script looks up and finds it on its own thread before the next run; a
    # saved script hands that in, so even its first run in a new session
    # does not search on the game thread. Missing objects are trusted
    # missing there, and ctx.forEach takes the snapshot read just before.
    source = ('let heroes = 0;'
              ' ctx.forEach("BP_Hero_C", () => { heroes++; });'
              ' return `${ctx.find("BP_Hero_C", "BP_Hero_C_1").Health}'
              ' ${ctx.find("BP_Hero_C", "Nobody")} ${heroes}`;')
    target = _Target(_build(tmp_path))
    settings.apply({"script_timings": True})
    learned = []

    def session(spec):
        worker = ScriptWorker(target.pid)
        added, failed, runs = [], {}, []
        worker.script_added.connect(added.append)
        worker.script_failed.connect(failed.__setitem__)
        worker.script_ran.connect(lambda script_id, run: runs.append(run))
        worker.lookups_learned.connect(lambda script_id, lookups: learned.append(lookups))
        try:
            worker.start()
            script = worker.add_script(spec)
            _wait_for(qt_app, lambda: script in added or script in failed)
            assert not failed
            results = []
            for _ in range(2):
                runs.clear()
                worker.run_now(script)
                _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
                assert runs[0]["error"] is None, runs[0]["error"]
                results.append(runs[0]["result"])
            return results
        finally:
            worker.requestInterruption()
            worker.wait(5000)

    def phases():
        return [entry for entry in _events(activity_file) if entry.get("pid") == target.pid
                and entry["event"] == "script_phases"]

    on_game_thread = ("game_thread_search_count", "game_thread_snapshot_count")
    try:
        first = session(ScriptSpec(source))
        # Learned once, after the first run; the second added nothing
        assert len(learned) == 1
        assert sorted(map(tuple, learned[0])) == [
            ("class", "BP_Hero_C"), ("object", "BP_Hero_C", "BP_Hero_C_1"),
            ("object", "BP_Hero_C", "Nobody"), ("objects", "BP_Hero_C")]
        assert first[0] == first[1] and first[0].endswith(' null 3"')
        one, two = phases()
        assert any(key in one for key in on_game_thread) and "prepare_ms" not in one
        assert not any(key in two for key in on_game_thread), two
        assert two["prepare_ms"] <= two["wait_ms"]

        # A new session with the saved lookups: no search on the game thread at all
        second = session(ScriptSpec(source, lookups=tuple(map(tuple, learned[0]))))
        assert second == first and len(learned) == 1
        for entry in phases()[2:]:
            assert not any(key in entry for key in on_game_thread), entry
            assert entry["prepare_ms"] >= 0
    finally:
        settings.apply({"script_timings": False})
        target.close()


@requires_target
def test_field_catalog_lists_the_uproperties_each_class_declares(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.catalog import FIELDS, CatalogWorker

    target = _Target(_build(tmp_path))
    try:
        worker = CatalogWorker(target.pid, kind=FIELDS)
        loaded = {}
        worker.assembly_loaded.connect(loaded.__setitem__)
        _run(qt_app, worker, worker.assemblies_found)
        fields = {f"{field.class_name}.{field.name}": field
                  for entries in loaded.values() for field in entries}
        # The packages read are the catalog's; the Blueprint declares no UProperty
        assert sorted(loaded) == ["/Game/Blueprints", "/Script/FakeGame"]
        assert not loaded["/Game/Blueprints"]
        assert [(key, field.type_name, field.offset) for key, field in sorted(fields.items())
                if key.startswith(("Hero.Health", "Hero.Focus", "Hero.Stats",
                                   "HeroAttributeSet.Might"))] == [
            ("Hero.Focus", "EPrimaryAttribute", 116), ("Hero.Health", "int32", 112),
            ("Hero.Stats", "TMap<int32, float>", 288),
            ("HeroAttributeSet.Might", "FGameplayAttributeData", 48)]
        assert len(fields) == 14
        assert all(field.engine == "unreal" and not field.static for field in fields.values())
    finally:
        target.close()


@requires_target
def test_scripts_name_the_values_of_unreal_enums(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    target = _Target(_build(tmp_path))
    worker = ScriptWorker(target.pid)
    added, runs = [], []
    worker.script_added.connect(added.append)
    worker.script_ran.connect(lambda script_id, run: runs.append((script_id, run)))

    def run(source):
        script_id = worker.add_script(ScriptSpec(source))
        _wait_for(qt_app, lambda: script_id in added)
        runs.clear()
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        return runs[0][1]

    try:
        worker.start()
        # The names without the enum's prefix and without UHT's _MAX
        result = run('const a = ctx.enum("EPrimaryAttribute");'
                     ' return [JSON.stringify(a), ctx.enumName("EPrimaryAttribute", 2),'
                     ' ctx.enumName("EPrimaryAttribute", 7)].join(" ");')
        assert result["error"] is None, result["error"]
        assert json.loads(result["result"]) == '{"Might":0,"Dexterity":2} Dexterity '
        # Found before the next run on the agent's thread, as classes are
        again = run('return ctx.enum("EPrimaryAttribute").Dexterity;')
        assert again["result"] == "2"
        missing = run('return ctx.enum("ENope");')
        assert "error.unreal.no_enum:{\"name\":\"ENope\"}" in missing["error"]
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_a_game_without_the_ability_system_still_names_its_player(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptWorker

    target = _Target(_build(tmp_path, "NO_GAS"))
    worker = ScriptWorker(target.pid)
    listed, failed = [], []
    worker.gas_listed.connect(lambda _request, state: listed.append(state))
    worker.gas_failed.connect(lambda _request, message: failed.append(message))
    try:
        worker.start()
        for _ in range(2):  # the player's parts found anew, then kept
            listed.clear()
            worker.gas_attributes()
            _wait_for(qt_app, lambda: listed or failed)
            assert not failed, failed
            state = listed[0]
            assert (state["gas"], state["owner"], state["ownerClass"], state["sets"],
                    state["component"], state["setter"]) == (
                False, "BP_Hero_C_0", "BP_Hero_C", [], False, None)
            assert int(state["ownerAddress"], 16) != 0
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
@pytest.mark.parametrize("defines", [(), ("ANIM_BLUEPRINT",)], ids=["alone", "anim-blueprint"])
def test_blueprint_classes_keep_their_own_names(tmp_path, qt_app, defines):
    # pylint: disable=import-outside-toplevel
    from grembyte.features.scripts import ScriptSpec, ScriptWorker

    target = _Target(_build(tmp_path, *defines))
    worker = ScriptWorker(target.pid)
    added, runs = [], []
    worker.script_added.connect(added.append)
    worker.script_ran.connect(lambda script_id, run: runs.append((script_id, run)))

    def run(source):
        script_id = worker.add_script(ScriptSpec(source))
        _wait_for(qt_app, lambda: script_id in added)
        runs.clear()
        worker.run_now(script_id)
        _wait_for(qt_app, lambda: target.send("tick", 0) and runs)
        assert runs[0][1]["error"] is None, runs[0][1]["error"]
        return json.loads(runs[0][1]["result"])

    try:
        worker.start()
        # The hero's Blueprint class as the game names it; a C++ class with
        # its prefix (AHero), found by either name
        assert run('const hero = ctx.find("BP_Hero_C");'
                   ' return [hero.className, ctx.findClass("AHero").name,'
                   ' ctx.findClass("Hero").name].join(" ");') == "BP_Hero_C Hero Hero"
        # The prefixed name Grembyte showed before: the hero's class, unless
        # a class of that very name exists (an Animation Blueprint)
        expected = "ABP_Hero_C" if defines else "BP_Hero_C"
        assert run('return ctx.findClass("ABP_Hero_C").name;') == expected
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()
