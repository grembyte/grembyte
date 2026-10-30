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
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

typedef struct Class Class;
typedef struct { const char *name; int kind; int byref; Class *klass; } Type;
typedef struct { const char *name; const char *file; const char *guid; } Image;
typedef struct { const char *name; Type *type; uint32_t offset; uint32_t flags; } Field;
struct Class {
    Type type;
    Image *image;
    int is_enum;
    Type *base;
    int is_valuetype;
    Class *parent;
    int field_count;
    Field fields[6];
    int rank;
    Class *element;
    int element_size;
    uint32_t type_flags;
    int instance;  /* a generic class bound to type arguments: no typedef row */
};
/* Like Mono: an object starts with its vtable, the vtable with its class. */
typedef struct { Class *klass; } VTable;
typedef struct { VTable *vtable; void *sync; } Object;

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
    const uint8_t *il;
    uint32_t il_size;
    int open;  /* of a generic class's definition: Mono cannot compile it */
} Method;

#define ADD_GOLD 0x06000001u
#define TAKE 0x06000002u
#define ROLL_DAMAGE 0x06000004u
#define CALC_DAMAGE 0x06000005u
#define SET_VALUE 0x0600000eu
#define STATIC 0x10u

/* Assembly-CSharp: Game.Inventory, Game.Loot, Game.Combat (a Unity
   component), the enum Game.DamageType, Game.DamageInfo and Game.Loop;
   mscorlib: System.Object, System.Int32, System.Int32[],
   UnityEngine.Component[] and System.InvalidOperationException;
   UnityEngine.CoreModule: UnityEngine.Object, .Component and .GameObject;
   R3.ReactiveProperty`1 (in Assembly-CSharp) and its instance for int. */
static Image game = { "Assembly-CSharp", "/game/Assembly-CSharp.dll",
                      "6F9619FF-8B86-D011-B42D-00C04FC964FF" };
static Image corlib = { "mscorlib", "/game/mscorlib.dll",
                        "0B5A6C1E-3F2D-4E7A-9C8B-1D2E3F4A5B6C" };
static Image unity = { "UnityEngine.CoreModule", "/game/UnityEngine.CoreModule.dll", NULL };
static Class classes[17];
static Type t_void = { "System.Void", 0x01 }, t_int = { "System.Int32", 0x08 },
    t_int_ref = { "System.Int32&", 0x08, 1 }, t_float = { "System.Single", 0x0c },
    t_string = { "System.String", 0x0e }, t_bool = { "System.Boolean", 0x02 },
    t_damage = { "Game.DamageType", 0x11, 0, &classes[4] },
    t_combat = { "Game.Combat", 0x12, 0, &classes[3] },
    t_info_ref = { "Game.DamageInfo&", 0x12, 1, &classes[6] },
    t_ints = { "System.Int32[]", 0x1d, 0, &classes[7] },
    t_intptr = { "System.IntPtr", 0x18 },
    t_game_object = { "UnityEngine.GameObject", 0x12, 0, &classes[10] },
    t_type = { "System.Type", 0x12 };
static Type *type_params[] = { &t_type };
static Class classes[17] = {
    { .type = { "Game.Inventory", 0x12 }, .image = &game },
    { .type = { "Game.Loot", 0x12 }, .image = &game },
    { .type = { "System.Object", 0x1c }, .image = &corlib },
    { .type = { "Game.Combat", 0x12 }, .image = &game, .parent = &classes[9],
      .field_count = 2,
      .fields = { { "Health", &t_int, 0x20 }, { "Instances", &t_int, 0, 0x10 } } },
    { .type = { "Game.DamageType", 0x11 }, .image = &game, .is_enum = 1, .base = &t_int,
      .is_valuetype = 1 },
    { .type = { "System.Int32", 0x08 }, .image = &corlib, .is_valuetype = 1 },
    { .type = { "Game.DamageInfo", 0x12 }, .image = &game, .parent = &classes[2],
      .field_count = 5,
      .fields = { { "Damage", &t_float, 0x10 }, { "Type", &t_damage, 0x14 },
                  { "Name", &t_string, 0x18 }, { "Owner", &t_combat, 0x20 },
                  { "Rolls", &t_ints, 0x28 } } },
    { .type = { "System.Int32[]", 0x1d }, .image = &corlib, .parent = &classes[2],
      .rank = 1, .element = &classes[5], .element_size = 4 },
    { .type = { "UnityEngine.Object", 0x12 }, .image = &unity, .parent = &classes[2],
      .field_count = 1, .fields = { { "m_CachedPtr", &t_intptr, 0x10 } } },
    { .type = { "UnityEngine.Component", 0x12 }, .image = &unity, .parent = &classes[8] },
    { .type = { "UnityEngine.GameObject", 0x12 }, .image = &unity, .parent = &classes[8] },
    { .type = { "UnityEngine.Component[]", 0x1d }, .image = &corlib, .parent = &classes[2],
      .rank = 1, .element = &classes[9], .element_size = 8 },
    { .type = { "System.InvalidOperationException", 0x12 }, .image = &corlib,
      .parent = &classes[2], .field_count = 1, .fields = { { "_message", &t_string, 0x10 } } },
    { .type = { "Game.Loop", 0x12 }, .image = &game, .parent = &classes[2] },
    /* [ComImport]: Mono hangs on Linux compiling code that uses it */
    { .type = { "UnityEngine.KnownFolders", 0x12 }, .image = &unity, .parent = &classes[2],
      .type_flags = 0x1000 },
    { .type = { "R3.ReactiveProperty<T>", 0x12 }, .image = &game, .parent = &classes[2] },
    { .type = { "R3.ReactiveProperty<int>", 0x15, 0, &classes[16] }, .image = &game,
      .parent = &classes[2], .instance = 1 },
};
static VTable vt_combat = { &classes[3] }, vt_info = { &classes[6] }, vt_ints = { &classes[7] },
    vt_game_object = { &classes[10] }, vt_components = { &classes[11] },
    vt_exception = { &classes[12] };
/* Unity objects: the name lives on Unity's native side, in no field. */
typedef struct GameObject GameObject;
typedef struct { Object header; void *cached_ptr; const char *name; int32_t health;
                 GameObject *game_object; } Combat;
struct GameObject { Object header; void *cached_ptr; const char *name; Combat *component; };
typedef struct { Object header; void *bounds; uintptr_t length; void *data[1]; } ComponentArray;
typedef struct { Object header; void *message; } Exception;
static int type_object_component, type_object_combat, type_object_game_object,
    type_object_other;
/* Every Combat and GameObject the game made, for FindObjectsOfType */
static Combat *live_combats[64];
static GameObject *live_game_objects[64];
static int live_count;
typedef struct { Object header; void *bounds; uintptr_t length; void *data[64]; } ObjectArray;
typedef struct { Object header; float damage; int32_t type; void *name; Combat *owner;
                 void *rolls; } DamageInfo;
typedef struct { Object header; void *bounds; uintptr_t length; int32_t data[3]; } IntArray;
void *fake_string(const char *ascii);

/* Methods mono_runtime_invoke calls: (this, params, exception) */
typedef void *(*invoke_fn)(void *self, void **params, void **exception);

static void *box_int(int32_t value) {
    uint8_t *box = calloc(1, sizeof(Object) + 8);  /* boxed value types: header, value */
    memcpy(box + sizeof(Object), &value, 4);
    return box;
}

/* string UnityEngine.Object.get_name(); Combat and GameObject keep the
   name at the same place */
static void *unity_get_name(Combat *self, void **params, void **exception) {
    (void)params; (void)exception;
    return fake_string(self->name);
}
/* GameObject UnityEngine.Component.get_gameObject() */
static void *component_get_game_object(Combat *self, void **params, void **exception) {
    (void)params; (void)exception;
    return self->game_object;
}
/* Component[] UnityEngine.GameObject.GetComponents(Type type) */
static void *game_object_get_components(GameObject *self, void **params, void **exception) {
    (void)exception;
    ComponentArray *array = calloc(1, sizeof *array);
    array->header.vtable = &vt_components;
    if (params[0] == &type_object_component) {
        array->length = 1;
        array->data[0] = self->component;
    }
    return array;
}
/* static Object[] UnityEngine.Object.FindObjectsOfType(Type type) */
static void *object_find_objects_of_type(void *self, void **params, void **exception) {
    (void)self; (void)exception;
    ObjectArray *array = calloc(1, sizeof *array);
    array->header.vtable = &vt_components;
    for (int i = 0; i < live_count; i++) {
        if (params[0] == &type_object_combat)
            array->data[array->length++] = live_combats[i];
        else if (params[0] == &type_object_game_object)
            array->data[array->length++] = live_game_objects[i];
    }
    return array;
}
/* bool Game.Combat.IsPartyMember { get; } */
static void *combat_is_party_member(Combat *self, void **params, void **exception) {
    (void)params; (void)exception;
    return box_int(strcmp(self->name, "Miranda") == 0);
}
/* int Game.Combat.Broken { get; } throws */
static void *combat_broken(Combat *self, void **params, void **exception) {
    (void)self; (void)params;
    Exception *error = calloc(1, sizeof *error);
    error->header.vtable = &vt_exception;
    error->message = fake_string("broken getter");
    *exception = error;
    return NULL;
}
/* static int Game.Combat.Count { get; } */
static void *combat_count(void *self, void **params, void **exception) {
    (void)self; (void)params; (void)exception;
    return box_int(7);
}
/* static void Game.Loop.Update(): what Unity runs every frame */
static void *loop_update(void *self, void **params, void **exception) {
    (void)self; (void)params; (void)exception;
    return NULL;
}

/* void CalcDamage(Combat other, out DamageInfo info), an instance method */
__attribute__((noipa)) void calc_damage(Combat *self, Combat *other, DamageInfo **info) {
    (void)other;
    DamageInfo *result = calloc(1, sizeof *result);
    IntArray *rolls = calloc(1, sizeof *rolls);
    rolls->header.vtable = &vt_ints;
    rolls->length = 3;
    for (int i = 0; i < 3; i++)
        rolls->data[i] = 3 + i;
    result->header.vtable = &vt_info;
    result->damage = 12.5f;
    result->type = 2;
    result->name = fake_string("Slash");
    result->owner = self;
    result->rolls = rolls;
    *info = result;
}

/* float RollDamage(int min, float scale, string name, bool crit,
                    DamageType type, out int roll), an instance method */
__attribute__((noipa)) float roll_damage(Object *self, int32_t min, float scale, void *name,
                                         int32_t crit, int32_t type, int32_t *roll) {
    (void)self; (void)name; (void)type;
    *roll = 42;
    return crit ? min * scale * 2 : min * scale;
}

/* void R3.ReactiveProperty<int>.set_Value(int value) */
static int32_t reactive_value;
__attribute__((noipa)) void reactive_set_value(void *self, int32_t value) {
    (void)self;
    reactive_value = value;
}

/* IL: get_Broken creates a COM object (newobj KnownFolders::.ctor), Update
   only does harmless things (a switch, ldstr, call AddGold) */
static const uint8_t broken_il[] = { 0x73, 0x0d, 0x00, 0x00, 0x06, 0x26, 0x16, 0x2a };
static const uint8_t update_il[] = {
    0x16, 0x45, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x72, 0x01, 0x00, 0x00, 0x70, 0x26, 0x28, 0x01, 0x00, 0x00, 0x06, 0x2a };
/* IL code for the call graph: Loot.Take calls Inventory.AddGold (ldarg.0;
   ldarg.1; call 0x06000001; ret), like its JIT code does. */
static const uint8_t take_il[] = { 0x02, 0x03, 0x28, 0x01, 0x00, 0x00, 0x06, 0x2a };
static const uint8_t add_gold_il[] = { 0x2a };
static Method methods[] = {
    { "Game.Inventory:AddGold", "Game.Inventory:AddGold (int&,int)", ADD_GOLD,
      &classes[0], "AddGold", STATIC, &t_void, 2, { &t_int_ref, &t_int }, { "gold", "amount" },
      .il = add_gold_il, .il_size = sizeof add_gold_il },
    { "Game.Loot:Take", "Game.Loot:Take (int&,int)", TAKE,
      &classes[1], "Take", STATIC, &t_void, 2, { &t_int_ref, &t_int }, { "gold", "amount" },
      .il = take_il, .il_size = sizeof take_il },
    { "System.Object:ToString", "System.Object:ToString ()", 0x06000003u,
      &classes[2], "ToString", 0, &t_string },
    { "Game.Combat:RollDamage",
      "Game.Combat:RollDamage (int,single,string,bool,Game.DamageType,int&)", ROLL_DAMAGE,
      &classes[3], "RollDamage", 0, &t_float, 6,
      { &t_int, &t_float, &t_string, &t_bool, &t_damage, &t_int_ref },
      { "min", "scale", "name", "crit", "type", "roll" }, (void *)roll_damage },
    { "Game.Combat:CalcDamage", "Game.Combat:CalcDamage (Game.Combat,Game.DamageInfo&)",
      CALC_DAMAGE, &classes[3], "CalcDamage", 0, &t_void, 2, { &t_combat, &t_info_ref },
      { "other", "info" }, (void *)calc_damage },
    { "UnityEngine.Object:get_name", "UnityEngine.Object:get_name ()", 0x06000006u,
      &classes[8], "get_name", 0, &t_string, 0, { 0 }, { 0 }, (void *)unity_get_name },
    { "Game.Combat:get_IsPartyMember", "Game.Combat:get_IsPartyMember ()", 0x06000007u,
      &classes[3], "get_IsPartyMember", 0, &t_bool, 0, { 0 }, { 0 },
      (void *)combat_is_party_member },
    { "Game.Combat:get_Broken", "Game.Combat:get_Broken ()", 0x06000008u,
      &classes[3], "get_Broken", 0, &t_int, 0, { 0 }, { 0 }, (void *)combat_broken,
      .il = broken_il, .il_size = sizeof broken_il },
    { "Game.Combat:get_Count", "Game.Combat:get_Count ()", 0x06000009u,
      &classes[3], "get_Count", STATIC, &t_int, 0, { 0 }, { 0 }, (void *)combat_count },
    { "UnityEngine.Component:get_gameObject", "UnityEngine.Component:get_gameObject ()",
      0x0600000au, &classes[9], "get_gameObject", 0, &t_game_object, 0, { 0 }, { 0 },
      (void *)component_get_game_object },
    { "UnityEngine.GameObject:GetComponents", "UnityEngine.GameObject:GetComponents (System.Type)",
      0x0600000bu, &classes[10], "GetComponents", 0, &t_void, 1, { &t_type }, { "type" },
      (void *)game_object_get_components },
    { "Game.Loop:Update", "Game.Loop:Update ()", 0x0600000cu, &classes[13], "Update", STATIC,
      &t_void, 0, { 0 }, { 0 }, (void *)loop_update, .il = update_il, .il_size = sizeof update_il },
    { "UnityEngine.KnownFolders:.ctor", "UnityEngine.KnownFolders:.ctor ()", 0x0600000du,
      &classes[14], ".ctor", 0, &t_void },
    /* The token finds the definition; only the instance has code. */
    { "R3.ReactiveProperty`1:set_Value", "R3.ReactiveProperty`1:set_Value (T)", SET_VALUE,
      &classes[15], "set_Value", 0, &t_void, 1, { &t_int }, { "value" }, .open = 1 },
    { "R3.ReactiveProperty`1<int>:set_Value", "R3.ReactiveProperty`1<int>:set_Value (int)",
      SET_VALUE, &classes[16], "set_Value", 0, &t_void, 1, { &t_int }, { "value" },
      (void *)reactive_set_value },
    { "UnityEngine.Object:FindObjectsOfType",
      "UnityEngine.Object:FindObjectsOfType (System.Type)", 0x0600000fu, &classes[8],
      "FindObjectsOfType", STATIC, &t_void, 1, { &t_type }, { "type" },
      (void *)object_find_objects_of_type },
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
    if (m->open)
        return NULL;
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

/* The game's side: its thread runs scripts in the child domain; it is
   Unity's main thread, the only one where get_name works. */
static pthread_t main_thread;
static int exception_object;
void fake_attach(void) {
    current_domain = &script_domain;
    main_thread = pthread_self();
}
void *mono_class_get_method_from_name(Class *k, const char *name, int count) {
    for (size_t i = 0; i < COUNT(methods); i++)
        if (methods[i].klass == k && strcmp(methods[i].short_name, name) == 0
            && methods[i].param_count == count)
            return &methods[i];
    return NULL;
}
void *mono_runtime_invoke(Method *m, void *object, void **params, void **exception) {
    if (!pthread_equal(pthread_self(), main_thread)) {
        *exception = &exception_object;  /* UnityException: main thread only */
        return NULL;
    }
    return ((invoke_fn)m->native)(object, params, exception);
}
void *mono_object_get_virtual_method(void *object, Method *m) { (void)object; return m; }
void *mono_type_get_object(void *domain, Type *type) {
    (void)domain;
    if (type == &classes[3].type)
        return &type_object_combat;
    if (type == &classes[10].type)
        return &type_object_game_object;
    return type == &classes[9].type ? (void *)&type_object_component : &type_object_other;
}
void *mono_class_from_name(Image *img, const char *ns, const char *name) {
    char full[128];
    snprintf(full, sizeof full, "%s.%s", ns, name);
    for (size_t i = 0; i < COUNT(classes); i++)
        if (classes[i].image == img && strcmp(classes[i].type.name, full) == 0)
            return &classes[i];
    return NULL;
}
/* The game's main loop runs a script method (Unity: every frame) */
void fake_tick(void) {
    void *exception = NULL;
    mono_runtime_invoke(by_token(0x0600000cu), NULL, NULL, &exception);
}
void *fake_code(uint32_t token) { return compile(by_token(token)); }
/* The game sets a ReactiveProperty<int> */
void fake_set_value(int32_t value) {
    for (size_t i = 0; i < COUNT(methods); i++)
        if (methods[i].token == SET_VALUE && !methods[i].open)
            ((void (*)(void *, int32_t))compile(&methods[i]))(NULL, value);
}

void *mono_get_root_domain(void) { return &root_domain; }
void *mono_domain_get(void) { return current_domain; }
void *mono_domain_get_by_id(int id) {
    /* Like an unloaded domain: a gap before the scripts' domain */
    return id == 0 ? (void *)&root_domain : id == 2 ? (void *)&script_domain : NULL;
}
void *mono_thread_attach(void *domain) { current_domain = domain; return &thread; }
void mono_thread_detach(void *attached) { (void)attached; current_domain = NULL; }

void *mono_image_loaded(const char *name) {
    return strcmp(name, game.file) == 0 ? &game : NULL;
}
const char *mono_image_get_name(Image *img) { return img->name; }
const char *mono_image_get_filename(Image *img) { return img->file; }
const char *mono_image_get_guid(Image *img) { return img->guid; }
void *mono_get_method(void *img, uint32_t token, void *k) { (void)img; (void)k; return by_token(token); }
void *mono_method_get_class(Method *m) { return m->klass; }
void *mono_class_get_image(Class *k) { return k->image; }
uint32_t mono_method_get_token(void *m) { return ((Method *)m)->token; }

void *mono_compile_method(void *m) {
    /* Mono only compiles for threads it knows, in the scripts' domain. */
    if (current_domain != &script_domain)
        return NULL;
    if (getenv("FAKE_JIT_SLOW"))
        usleep(atoi(getenv("FAKE_JIT_SLOW")) * 1000);  /* milliseconds */
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
    callback(&unity, data);
}
void *mono_assembly_get_image(void *assembly) { return assembly; }
int mono_image_get_table_rows(Image *img, int table) {
    int rows = 1;  /* <Module> */
    for (size_t i = 0; table == 2 && i < COUNT(classes); i++)
        rows += classes[i].image == img && !classes[i].instance;
    return rows;
}
void *mono_class_get(Image *img, uint32_t token) {
    uint32_t row = 1;
    for (size_t i = 0; i < COUNT(classes); i++)
        if (classes[i].image == img && !classes[i].instance && ++row == (token & 0xffffff))
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

/* Generic classes: only ReactiveProperty`1 bound to System.Int32. Like
   Unity's MonoBleedingEdge: no mono_class_bind_generic_parameters. */
void *mono_get_corlib(void) { return &corlib; }
void *mono_reflection_type_from_name(char *name, Image *img) {
    return img == &game && strcmp(name, "R3.ReactiveProperty`1[[System.Int32, mscorlib]]") == 0
        ? &classes[16].type : NULL;
}
void *mono_class_get_nested_types(Class *k, void **iterator) {
    (void)k; (void)iterator;
    return NULL;
}
const char *mono_class_get_name(Class *k) { return k->type.name; }
uint32_t mono_method_get_flags(Method *m, uint32_t *implementation) {
    (void)implementation;
    return m->flags;
}
void *mono_method_signature(Method *m) { return m; }
void *mono_method_get_header(Method *m) { return m->il ? m : NULL; }
const uint8_t *mono_method_header_get_code(Method *m, uint32_t *size, uint32_t *max_stack) {
    *size = m->il_size;
    *max_stack = 8;
    return m->il;
}
void mono_metadata_free_mh(void *header) { (void)header; }
uint32_t mono_class_get_flags(Class *k) { return k->type_flags; }
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
void *mono_object_get_class(Object *object) { return object->vtable->klass; }
void *mono_class_get_fields(Class *k, void **iterator) {
    int next = *iterator ? (int)((Field *)*iterator - k->fields) + 1 : 0;
    if (next >= k->field_count)
        return NULL;
    return *iterator = &k->fields[next];
}
const char *mono_field_get_name(Field *field) { return field->name; }
void *mono_field_get_type(Field *field) { return field->type; }
uint32_t mono_field_get_offset(Field *field) { return field->offset; }
uint32_t mono_field_get_flags(Field *field) { return field->flags; }
void *mono_class_get_parent(Class *k) { return k->parent; }
int mono_class_get_rank(Class *k) { return k->rank; }
void *mono_class_get_element_class(Class *k) { return k->element; }
int mono_array_element_size(Class *k) { return k->element_size; }

/* The game's side: a Game.Combat object and a C# string */
void *fake_combat(const char *name) {
    Combat *combat = calloc(1, sizeof *combat);
    GameObject *game_object = calloc(1, sizeof *game_object);
    combat->header.vtable = &vt_combat;
    combat->health = 100;
    combat->name = name;
    combat->game_object = game_object;
    game_object->header.vtable = &vt_game_object;
    game_object->name = name;
    game_object->component = combat;
    if (live_count < 64) {
        live_combats[live_count] = combat;
        live_game_objects[live_count++] = game_object;
    }
    return combat;
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
#include <pthread.h>

void fake_attach(void);
void fake_unmap(void);
void *fake_code(uint32_t token);
void *fake_combat(const char *name);
void *fake_string(const char *ascii);
void fake_tick(void);
void fake_set_value(int32_t value);
typedef float (*roll_fn)(void *self, int32_t min, float scale, void *name, int32_t crit,
                         int32_t type, int32_t *roll);
typedef void (*calc_fn)(void *self, void *other, void **info);

static void *roll_in_background(void *arg) {
    int32_t roll;
    (void)arg;
    ((roll_fn)fake_code(0x06000004u))(fake_combat("Ghost"), 1, 1.5f, fake_string("Bolt"), 0, 0,
                                      &roll);
    return NULL;
}

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
        if (strcmp(cmd, "tick") == 0)
            fake_tick();
        else if (strcmp(cmd, "set") == 0)
            fake_set_value(value);
        else if (strcmp(cmd, "add") == 0)
            ((change_fn)fake_code(0x06000001u))(&g_game->player->gold, value);
        else if (strcmp(cmd, "loot") == 0)
            ((change_fn)fake_code(0x06000002u))(&g_game->player->gold, value);
        else if (strcmp(cmd, "calc") == 0) {
            void *info = NULL;
            ((calc_fn)fake_code(0x06000005u))(fake_combat("Miranda"), fake_combat("Young Wolf"),
                                              &info);
            printf("%d %d\n", (int)(*(float *)((char *)info + 0x10) * 2),
                   *(int32_t *)((char *)info + 0x14));
            fflush(stdout);
            continue;
        }
        else if (strcmp(cmd, "roll") == 0) {
            int32_t roll = -1;
            float damage = ((roll_fn)fake_code(0x06000004u))(
                fake_combat("Hero"), value, 1.5f, fake_string("Fireball"), 1, 2, &roll);
            printf("%d %d\n", (int)damage, roll);
            fflush(stdout);
            continue;
        }
        else if (strcmp(cmd, "bgroll") == 0) {
            /* The same call on a thread other than Unity's main thread */
            pthread_t thread;
            pthread_create(&thread, NULL, roll_in_background, NULL);
            pthread_join(thread, NULL);
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
    def __init__(self, binary, constant="long", updated=False, slow_ms=0):
        # pylint: disable=super-init-not-called,import-outside-toplevel
        import os

        env = {**os.environ, "FAKE_JIT_CONSTANT": constant}
        if updated:
            env["FAKE_JIT_UPDATED"] = "1"
        if slow_ms:
            env["FAKE_JIT_SLOW"] = str(slow_ms)
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
            "engine": "mono",
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
    # pylint: disable=import-outside-toplevel,unused-argument,too-many-locals
    from hermes.engines.registry import detect_engine
    from hermes.features.catalog import CatalogMethod, CatalogWorker

    target = _MonoTarget(_build(tmp_path))
    try:
        assert detect_engine(target.pid).id == "mono"
        assert detect_engine(os.getpid()) is None

        cached = []

        def read(assemblies=None, fresh=False):
            found, loaded, failed = [], {}, []
            cached.clear()
            worker = CatalogWorker(target.pid, assemblies, fresh=fresh)
            worker.assemblies_found.connect(found.extend)
            worker.assembly_loaded.connect(loaded.__setitem__)
            worker.assembly_cached.connect(cached.append)
            worker.catalog_failed.connect(failed.append)
            worker.start()
            _wait_for(qt_app, worker.isFinished)
            qt_app.processEvents()
            assert not failed
            return found, loaded

        found, loaded = read()
        assert [(a["name"], a["classes"], a["mvid"]) for a in found] == [
            ("Assembly-CSharp", 7, "6F9619FF-8B86-D011-B42D-00C04FC964FF"),
            ("mscorlib", 5, "0B5A6C1E-3F2D-4E7A-9C8B-1D2E3F4A5B6C"),
            ("UnityEngine.CoreModule", 4, None)]
        assert not cached
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
            CatalogMethod("Assembly-CSharp", "Game.Combat", "CalcDamage",
                          "(Game.Combat,Game.DamageInfo&)", "System.Void", False, 0x06000005,
                          "Game.Combat:CalcDamage (Game.Combat,Game.DamageInfo&)"),
            CatalogMethod("Assembly-CSharp", "Game.Combat", "get_IsPartyMember", "()",
                          "System.Boolean", False, 0x06000007, "Game.Combat:get_IsPartyMember ()"),
            CatalogMethod("Assembly-CSharp", "Game.Combat", "get_Broken", "()", "System.Int32",
                          False, 0x06000008, "Game.Combat:get_Broken ()"),
            CatalogMethod("Assembly-CSharp", "Game.Combat", "get_Count", "()", "System.Int32",
                          True, 0x06000009, "Game.Combat:get_Count ()"),
            CatalogMethod("Assembly-CSharp", "Game.Loop", "Update", "()", "System.Void", True,
                          0x0600000c, "Game.Loop:Update ()"),
            # The definition of a generic class, not its instance
            CatalogMethod("Assembly-CSharp", "R3.ReactiveProperty<T>", "set_Value", "(T)",
                          "System.Void", False, 0x0600000e, "R3.ReactiveProperty`1:set_Value (T)"),
        ]}
        assert loaded["Assembly-CSharp"][0].signature == "Game.Inventory:AddGold (int&,int)"
        _found, loaded = read(["mscorlib"])
        assert [m.signature for m in loaded["mscorlib"]] == ["System.Object:ToString ()"]

        # Read again: the same builds come from the cache, unless fresh.
        first = loaded
        _found, loaded = read(["Assembly-CSharp", "mscorlib", "UnityEngine.CoreModule"])
        assert cached == ["Assembly-CSharp", "mscorlib"]
        assert loaded["mscorlib"] == first["mscorlib"]
        _found, again = read()
        assert again["Assembly-CSharp"] == loaded["Assembly-CSharp"]
        read(fresh=True)
        assert not cached
    finally:
        target.close()


@requires_target
def test_call_graph_reads_calls_from_il_code(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.call_graph import CallGraphWorker
    from hermes.features.catalog import CatalogMethod

    target = _MonoTarget(_build(tmp_path))
    worker = CallGraphWorker(target.pid)
    callees, callers, failed = [], [], []
    worker.callees_found.connect(lambda _id, _method, calls, body: callees.append((calls, body)))
    worker.callers_found.connect(lambda _id, _method, calls, _missing: callers.append(calls))
    worker.request_failed.connect(lambda _id, message: failed.append(message))
    worker.worker_failed.connect(failed.append)
    take = CatalogMethod("Assembly-CSharp", "Game.Loot", "Take", "(int&,int)", "System.Void",
                         True, 0x06000002, "Game.Loot:Take (int&,int)")
    add_gold = CatalogMethod("Assembly-CSharp", "Game.Inventory", "AddGold", "(int&,int)",
                             "System.Void", True, 0x06000001, "Game.Inventory:AddGold (int&,int)")
    try:
        worker.start()
        worker.callees(take)
        _wait_for(qt_app, lambda: callees or failed)
        assert not failed
        assert [(call.opcode, call.method) for call in callees[0][0]] == [("call", add_gold)]
        worker.callers(add_gold)
        _wait_for(qt_app, lambda: callers or failed)
        assert not failed
        assert [call.method.name for call in callers[0]] == ["Take", "Update"]
        # The index is in the cache now.
        assert list((tmp_path / "catalog-cache" / "mono" / "Assembly-CSharp").glob("*.calls.json.gz"))
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_catalog_dialog_searches_methods(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.ui.widgets.catalog import CatalogDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = CatalogDialog(translations, target.pid)
    try:
        _wait_for(qt_app, lambda: dialog._worker is None)
        assert dialog.model.rowCount() == 9
        assert "9 von 9 Methoden" in dialog.status_label.text()
        assert "Zwischenspeicher" not in dialog.status_label.text()
        dialog.reload()
        _wait_for(qt_app, lambda: dialog._worker is None)
        assert dialog.model.rowCount() == 9
        assert "1 aus dem Zwischenspeicher" in dialog.status_label.text()
        dialog.reload_button.click()
        _wait_for(qt_app, lambda: dialog._worker is None)
        assert "Zwischenspeicher" not in dialog.status_label.text()

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
    from hermes.features.monitor import MethodMonitorWorker

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
        # Game.Combat is a Unity component: Unity knows its name.
        assert call["self"]["text"].startswith('Game.Combat "Hero"@0x')
        args = {arg["name"]: arg["text"] for arg in call["args"]}
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
        assert [call["result"]["text"] for call in calls] == ["6"]
        assert [call["monitorId"] for call in calls] == [with_result]
        # What the method left in the out argument
        assert calls[0]["args"][5]["after"] == {"text": "42"}
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


_SET_VALUE = {"image": "Assembly-CSharp", "file": None, "token": 0x0600000e,
              "method": "R3.ReactiveProperty`1:set_Value (T)"}


def test_methods_of_generic_classes_need_type_arguments():
    # pylint: disable=import-outside-toplevel
    from hermes.features.catalog import (
        catalog_method_from_identity,
        generic_parameters,
        split_type_arguments,
        with_type_arguments,
    )

    definition = catalog_method_from_identity(_SET_VALUE)
    assert generic_parameters(definition) == ("T",)
    assert generic_parameters(definition.__class__(
        "A", "Game.Map<TKey,TValue>", "Get", "(TKey)", "TValue", False, 1,
        "Game.Map`2:Get (TKey)")) == ("TKey", "TValue")
    assert split_type_arguments("int, Game.Map`2<int, string>,") == [
        "int", "Game.Map`2<int, string>"]
    instance = with_type_arguments(definition, ["int"])
    assert instance.signature == "R3.ReactiveProperty`1<int>:set_Value (T)"
    assert generic_parameters(instance) == ()
    assert generic_parameters(catalog_method_from_identity(
        {**_SET_VALUE, "method": "R3.ReactiveProperty`1<int>:set_Value (int)"})) == ()


@requires_target
def test_method_monitor_binds_generic_classes_to_type_arguments(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.catalog import catalog_method_from_identity, with_type_arguments
    from hermes.features.monitor import MethodMonitorWorker

    definition = catalog_method_from_identity(_SET_VALUE)
    target = _MonoTarget(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    added, failed, calls = {}, {}, []
    worker.monitor_added.connect(added.__setitem__)
    worker.monitor_failed.connect(failed.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))
    try:
        worker.start()
        # The definition has no code of its own.
        open_id = worker.add_monitor(definition.identity())
        _wait_for(qt_app, lambda: open_id in failed)
        assert "generic class" in failed[open_id]

        # Type arguments given for a catalog entry
        chosen = worker.add_monitor(with_type_arguments(definition, ["int"]).identity())
        _wait_for(qt_app, lambda: chosen in added or chosen in failed)
        assert chosen not in failed
        assert added[chosen]["params"] == ["System.Int32 value"]
        target.send("set", 45)
        _wait_for(qt_app, lambda: calls)
        assert [arg["text"] for arg in calls[0]["args"]] == ["45"]
        worker.remove_monitor(chosen)

        # The name a write watch or a hook knows the instance by
        named = worker.add_monitor(
            {**_SET_VALUE, "method": "R3.ReactiveProperty`1<int>:set_Value (int)"})
        _wait_for(qt_app, lambda: named in added or named in failed)
        assert named not in failed

        unknown = worker.add_monitor(with_type_arguments(definition, ["Game.Nothing"]).identity())
        _wait_for(qt_app, lambda: unknown in failed)
        assert "Game.Nothing not found" in failed[unknown]
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_method_monitor_lets_the_game_free_its_code_and_exit(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker

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


_CALC_DAMAGE = {"image": "Assembly-CSharp", "file": None, "token": 0x06000005,
                "method": "Game.Combat:CalcDamage (Game.Combat,Game.DamageInfo&)"}


@requires_target
def test_method_monitor_records_objects_and_inspects_them(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel,too-many-locals
    from hermes.features.monitor import MethodMonitorWorker

    target = _MonoTarget(_build(tmp_path))
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
        monitor_id = worker.add_monitor(_CALC_DAMAGE, returns=True, objects=True)
        _wait_for(qt_app, lambda: monitor_id in added)
        assert target.send("calc", 0) == (25, 2)
        _wait_for(qt_app, lambda: calls)
        call = calls[0]

        def fields_of(snapshot):
            return [(f["name"], f["static"], f["value"]["text"]) for f in snapshot["fields"]]

        combat_fields = [("m_CachedPtr", False, "0x0"), ("Health", False, "100"),
                         ("Instances", True, "")]
        assert call["self"]["text"].startswith('Game.Combat "Miranda"@0x')
        assert call["self"]["snapshot"]["name"] == "Game.Combat"
        assert fields_of(call["self"]["snapshot"]) == combat_fields
        other, info = call["args"]
        assert other["text"].startswith('Game.Combat "Young Wolf"@0x')
        assert fields_of(other["snapshot"]) == combat_fields
        # The out argument is empty when the method starts, filled afterwards.
        assert info["text"].startswith("&0x") and info["text"].endswith("→ null")
        assert "snapshot" not in info
        after = info["after"]
        assert after["text"].startswith("Game.DamageInfo@0x")
        recorded = fields_of(after["snapshot"])
        assert recorded[:3] == [("Damage", False, "12.5"), ("Type", False, "2 (Game.DamageType)"),
                                ("Name", False, '"Slash"')]
        assert recorded[3][2].startswith('Game.Combat "Miranda"@0x')
        assert recorded[4][2].startswith("System.Int32[3]@0x")
        # Objects in recorded fields can be opened: they keep their target.
        assert after["snapshot"]["fields"][3]["value"]["target"] == call["self"]["target"]

        contents = inspect(after["target"])
        assert contents["name"] == "Game.DamageInfo"
        fields = {field["name"]: field for field in contents["fields"]}
        assert list(fields) == ["Damage", "Type", "Name", "Owner", "Rolls"]
        assert fields["Damage"]["value"]["text"] == "12.5"
        assert fields["Damage"]["type"] == "System.Single" and fields["Damage"]["offset"] == 0x10
        # Where each value is, and its value type for the address list
        base = int(after["target"]["address"], 16)
        assert int(fields["Damage"]["address"], 16) == base + 0x10
        assert [fields[name]["valueType"] for name in fields] == [
            "float", "int32", None, None, None]  # the enum counts as its base type

        rolls = inspect(fields["Rolls"]["value"]["target"])
        assert rolls["length"] == 3
        assert [(f["name"], f["value"]["text"]) for f in rolls["fields"]] == [
            ("[0]", "3"), ("[1]", "4"), ("[2]", "5")]
        first, second = (int(f["address"], 16) for f in rolls["fields"][:2])
        assert second - first == 4 and rolls["fields"][0]["valueType"] == "int32"
        assert first == int(rolls["address"], 16) + rolls["fields"][0]["offset"]
        owner = inspect(fields["Owner"]["value"]["target"])
        assert [(f["name"], f["static"], f["value"]["text"]) for f in owner["fields"]] == [
            ("m_CachedPtr", False, "0x0"), ("Health", False, "100"), ("Instances", True, "")]
        assert owner["fields"][2]["address"] is None  # static

        # An address that holds no object is refused instead of read.
        assert "no object" in inspect({"address": "0x10"})
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_monitor_window_opens_objects_in_the_inspector(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.catalog import CatalogMethod
    from hermes.ui.widgets.method_monitor import MethodMonitorDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = MethodMonitorDialog(translations, target.pid)
    try:
        dialog.add_method(CatalogMethod(
            "Assembly-CSharp", "Game.Combat", "CalcDamage", "(Game.Combat,Game.DamageInfo&)",
            "System.Void", False, 0x06000005, _CALC_DAMAGE["method"]), returns=True, objects=False)
        _wait_for(qt_app, lambda: dialog.methods_table.item(0, 5).text() == "Aktiv")
        target.send("calc", 0)
        _wait_for(qt_app, lambda: dialog.log.rowCount() == 1)
        assert "info=&0x" in dialog.log.data(dialog.log.index(0, 4))
        assert "⇒ Game.DamageInfo@0x" in dialog.log.data(dialog.log.index(0, 4))

        dialog.log_view.selectRow(0)
        labels = [dialog.details.item(row, 0).text() for row in range(dialog.details.rowCount())]
        assert labels == ["this", "other", "info", "info ⇒"]
        # Objects get a link to their fields; without recorded objects the
        # dialog shows the current ones.
        links = [dialog.details.cellWidget(row, 2) for row in range(4)]
        assert [link is not None for link in links] == [True, True, False, True]
        assert "Objekt-Details" in links[3].text()
        links[3].linkActivated.emit("#")
        assert len(dialog.inspectors) == 1
        inspector = dialog.inspectors[0]
        _wait_for(qt_app, lambda: inspector.table.rowCount() == 5)
        assert inspector.title_label.text().startswith("Game.DamageInfo @ 0x")
        assert inspector.windowTitle() == "Objekt-Inspektor - info (Game.DamageInfo) (1)"
        assert inspector.table.item(0, 2).text() == "12.5"
        assert inspector.live_check.isVisibleTo(inspector)

        # Owner and Rolls are objects: "öffnen" shows each in a new dialog;
        # the string Name shows in the text viewer.
        opens = [inspector.table.cellWidget(row, 4) is not None for row in range(5)]
        assert opens == [False, False, True, True, True]
        assert "Text …" in inspector.table.cellWidget(2, 4).text()
        inspector.table.cellWidget(3, 4).linkActivated.emit("#")
        assert len(dialog.inspectors) == 2
        owner = dialog.inspectors[1]
        _wait_for(qt_app, lambda: owner.title_label.text().startswith("Game.Combat"))
        assert owner.windowTitle() == "Objekt-Inspektor - Owner (Game.Combat) (2)"
        assert owner.table.item(2, 2).text() == "(statisch)"
        assert inspector.title_label.text().startswith("Game.DamageInfo")
        owner.close()
        _wait_for(qt_app, lambda: len(dialog.inspectors) == 1)

        # Clearing the log clears the values of the selected call as well.
        dialog.log.clear()
        assert dialog.details.rowCount() == 0
    finally:
        dialog.close()
        target.close()


@requires_target
def test_method_monitor_asks_unity_for_names_only_on_its_main_thread(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker

    target = _MonoTarget(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    added, calls = {}, []
    worker.monitor_added.connect(added.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))
    try:
        worker.start()
        monitor_id = worker.add_monitor(
            {"image": "Assembly-CSharp", "file": None, "token": 0x06000004,
             "method": "Game.Combat:RollDamage (int,single,string,bool,Game.DamageType,int&)"})
        _wait_for(qt_app, lambda: monitor_id in added)
        for command in ("bgroll", "roll", "bgroll"):
            target.send(command, 1)
        _wait_for(qt_app, lambda: len(calls) == 3)
        selves = [call["self"]["text"] for call in calls]
        # Unity refuses get_name on other threads: the address only.
        assert selves[0].startswith("Game.Combat@0x")
        assert selves[1].startswith('Game.Combat "Hero"@0x')
        assert selves[2].startswith("Game.Combat@0x")
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


def test_inspector_puts_number_fields_on_the_address_list(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.object_inspector import ObjectInspectorDialog

    contents = {"name": "Game.CharacterStats", "address": "0x7f55e9339000", "fields": [
        {"name": "BaseMight", "type": "System.Int32", "offset": 0x244, "static": False,
         "value": {"text": "12"}, "address": "0x7f55e9339244", "valueType": "int32"},
        {"name": "Owner", "type": "Game.Combat", "offset": 0x20, "static": False,
         "value": {"text": "Game.Combat@0x2000", "target": {"address": "0x2000"}},
         "address": "0x7f55e9339020", "valueType": None},
        {"name": "Cap", "type": "System.Int32", "offset": 0, "static": True,
         "value": {"text": ""}, "address": None, "valueType": None},
    ]}
    dialog = ObjectInspectorDialog(translations, lambda *_: None, contents=contents,
                                   captured="16:58:56.791")
    requested = []
    dialog.address_requested.connect(requested.append)
    try:
        assert not dialog.add_address_button.isEnabled()
        dialog.table.selectRow(0)
        assert dialog.add_address_button.isEnabled()
        dialog.add_address_button.click()
        assert requested == [{"address": 0x7F55E9339244, "value_type": "int32",
                              "label": "CharacterStats.BaseMight"}]
        assert "CharacterStats.BaseMight" in dialog.status_label.text()
        dialog.table.selectRow(1)  # a reference, no number
        assert not dialog.add_address_button.isEnabled()
        assert dialog.address_entry(2) is None  # static

        array = ObjectInspectorDialog(translations, lambda *_: None, captured="16:58:56.791",
                                      contents={"name": "System.Int32[]", "address": "0x3000",
                                                "length": 1, "fields": [
            {"name": "[0]", "type": "System.Int32", "offset": 0x20, "static": False,
             "value": {"text": "3"}, "address": "0x3020", "valueType": "int32"}]})
        assert array.address_entry(0)["label"] == "Int32[0]"
        array.close()
    finally:
        dialog.close()


def test_inspector_links_string_fields_to_the_text_viewer(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.object_inspector import ObjectInspectorDialog

    shown = []
    contents = {"name": "Kingmaker.SaveInfo", "address": "0x1000", "fields": [
        {"name": "Name", "type": "System.String", "offset": 0x10, "static": False,
         "value": {"text": '"Quicksave"', "string": True}},
        {"name": "Json", "type": "System.String", "offset": 0x18, "static": False,
         "value": {"text": '"{\\"m_Statistics…', "string": True, "fullText": 7,
                   "length": 5000}},
        {"name": "Empty", "type": "System.String", "offset": 0x20, "static": False,
         "value": {"text": "null"}},
        {"name": "Default", "type": "System.String", "offset": 0, "static": True,
         "value": {"text": ""}},
        {"name": "Count", "type": "System.Int32", "offset": 0x28, "static": False,
         "value": {"text": "3"}},
    ], "properties": [{"name": "Title", "type": "System.String", "static": False}]}
    dialog = ObjectInspectorDialog(
        translations, lambda *_: None, contents=contents, captured="07:58:56.016",
        show_text=lambda value, name, time: shown.append((value.get("fullText"), name, time)))
    try:
        # Every string field gets the link, short ones too; null and static do not.
        links = [dialog.table.cellWidget(row, 4) for row in range(5)]
        assert [link is not None for link in links] == [True, True, False, False, False]
        assert links[0].text().count("Text …") == 1
        links[0].linkActivated.emit("#")
        links[1].linkActivated.emit("#")
        assert shown == [(None, "Name", "07:58:56.016"), (7, "Json", "07:58:56.016")]

        dialog.show_properties([{"name": "Title", "value": {"text": '"A"', "string": True}}])
        dialog.properties_table.cellWidget(0, 3).linkActivated.emit("#")
        assert shown[-1] == (None, "Title", "07:58:56.016")
    finally:
        dialog.close()

    # Without a viewer (show_text) there is no link.
    plain = ObjectInspectorDialog(translations, lambda *_: None, contents=contents,
                                  captured="07:58:56.016")
    assert plain.table.cellWidget(0, 4) is None
    plain.close()


def test_inspector_shows_recorded_fields_without_reading_again(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.object_inspector import ObjectInspectorDialog

    opened = []
    contents = {"name": "Game.DamageInfo", "address": "0x1000", "fields": [
        {"name": "Damage", "type": "System.Single", "offset": 0x10, "static": False,
         "value": {"text": "12.5"}},
        {"name": "Owner", "type": "Game.Combat", "offset": 0x20, "static": False,
         "value": {"text": "Game.Combat@0x2000", "target": {"address": "0x2000"}}},
    ]}
    dialog = ObjectInspectorDialog(
        translations, lambda target, origin, name: opened.append((target, name)),
        contents=contents, captured="09:47:10.738", name="damage")
    try:
        assert "beim Aufruf um 09:47:10.738" in dialog.title_label.text()
        assert not dialog.live_check.isVisibleTo(dialog)
        assert dialog.table.cellWidget(0, 4) is None
        dialog.table.cellWidget(1, 4).linkActivated.emit("#")
        assert dialog.windowTitle() == "Objekt-Inspektor - damage (Game.DamageInfo) (1)"
        assert opened == [({"address": "0x2000"}, "Owner")]
        # A failed recording shows its reason.
        failed = ObjectInspectorDialog(translations, lambda *_: None, contents={"error": "gone"},
                                       captured="09:47:10.738")
        assert "gone" in failed.status_label.text()
        failed.close()
    finally:
        dialog.close()


@requires_target
def test_properties_are_evaluated_on_the_main_thread(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel,too-many-locals
    from hermes.features.monitor import MethodMonitorWorker

    target = _MonoTarget(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    added, calls, answers, failures = {}, [], {}, {}
    worker.monitor_added.connect(added.__setitem__)
    worker.calls.connect(lambda events, _dropped, _counts: calls.extend(events))
    worker.inspected.connect(answers.__setitem__)
    worker.evaluated.connect(answers.__setitem__)
    worker.inspect_failed.connect(failures.__setitem__)
    worker.evaluate_failed.connect(failures.__setitem__)

    def answer(request, then=None):
        if then is not None:
            time.sleep(0.2)  # the agent waits for the main thread now
            target.send(then, 0)
        _wait_for(qt_app, lambda: request in answers or request in failures, timeout=6)
        return answers.get(request, failures.get(request))

    try:
        worker.start()
        monitor_id = worker.add_monitor(_CALC_DAMAGE)
        _wait_for(qt_app, lambda: monitor_id in added)
        target.send("calc", 0)
        _wait_for(qt_app, lambda: calls)
        miranda = calls[0]["self"]["target"]

        properties = answer(worker.inspect(miranda))["properties"]
        assert [(p["name"], p["type"], p["static"]) for p in properties] == [
            ("IsPartyMember", "System.Boolean", False), ("Broken", "System.Int32", False),
            ("Count", "System.Int32", True), ("gameObject", "UnityEngine.GameObject", False),
            ("name", "System.String", False)]

        # The getters run when the main thread runs game code (here: "tick").
        values = answer(worker.evaluate(miranda, [p["name"] for p in properties]), then="tick")
        values = {value["name"]: value["value"] for value in values}
        assert values["IsPartyMember"] == {"text": "true"}
        assert values["Broken"]["error"]
        assert values["Broken"]["text"] == 'System.InvalidOperationException: "broken getter"'
        assert values["Count"] == {"text": "7"}
        assert values["name"] == {"text": '"Miranda"', "string": True}
        game_object = values["gameObject"]
        assert game_object["text"].startswith('UnityEngine.GameObject "Miranda"@0x')

        # A GameObject lists its components; a monitored call on the main
        # thread runs waiting work as well.
        values = answer(worker.evaluate(game_object["target"], ["GetComponents()"]), then="calc")
        components = values[0]["value"]
        assert components["text"].startswith("UnityEngine.Component[1]@0x")
        elements = answer(worker.inspect(components["target"]))["fields"]
        assert elements[0]["value"]["target"] == miranda

        # Without the main thread running game code the request gives up.
        message = answer(worker.evaluate(miranda, ["Count"]))
        assert "main thread" in message
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_static_methods_are_called_on_the_main_thread(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker

    count = {"image": "Assembly-CSharp", "file": None, "token": 0x06000009,
             "method": "Game.Combat:get_Count ()"}
    target = _MonoTarget(_build(tmp_path))
    worker = MethodMonitorWorker(target.pid)
    answers, failures = {}, {}
    worker.call_described.connect(answers.__setitem__)
    worker.called.connect(answers.__setitem__)
    worker.call_failed.connect(failures.__setitem__)

    def answer(request, then=None):
        if then is not None:
            time.sleep(0.2)  # the agent waits for the main thread now
            target.send(then, 0)
        _wait_for(qt_app, lambda: request in answers or request in failures, timeout=6)
        return answers.get(request, failures.get(request))

    try:
        worker.start()
        described = answer(worker.describe_call(count))
        assert described["params"] == [] and described["returns"]["name"] == "System.Int32"
        assert answer(worker.call(count, []), then="tick") == {"result": {"text": "7"}}
        # Instance methods need an object: not callable from the dialog.
        assert "no static method" in answer(worker.describe_call(_CALC_DAMAGE))
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_inspector_evaluates_properties(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.catalog import CatalogMethod
    from hermes.ui.widgets.method_monitor import MethodMonitorDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = MethodMonitorDialog(translations, target.pid)
    try:
        dialog.add_method(CatalogMethod(
            "Assembly-CSharp", "Game.Combat", "CalcDamage", "(Game.Combat,Game.DamageInfo&)",
            "System.Void", False, 0x06000005, _CALC_DAMAGE["method"]))
        _wait_for(qt_app, lambda: dialog.methods_table.item(0, 5).text() == "Aktiv")
        target.send("calc", 0)
        _wait_for(qt_app, lambda: dialog.log.rowCount() == 1)
        dialog.log_view.selectRow(0)
        dialog.details.cellWidget(0, 2).linkActivated.emit("#")
        inspector = dialog.inspectors[0]
        _wait_for(qt_app, lambda: inspector.properties_table.rowCount() == 5)
        assert inspector.properties_table.item(0, 0).text() == "IsPartyMember"
        assert inspector.properties_table.cellWidget(0, 3).text().endswith("auswerten</a>")

        inspector.properties_table.cellWidget(0, 3).linkActivated.emit("#")
        time.sleep(0.2)
        target.send("tick", 0)
        _wait_for(qt_app, lambda: inspector.properties_table.item(0, 2).text() == "true")

        inspector.evaluate_button.click()
        time.sleep(0.2)
        target.send("tick", 0)
        _wait_for(qt_app, lambda: inspector.properties_table.item(2, 2).text() == "7")
        # An object value can be opened in a dialog of its own.
        assert inspector.properties_table.item(3, 2).text().startswith("UnityEngine.GameObject")
        inspector.properties_table.cellWidget(3, 3).linkActivated.emit("#")
        assert len(dialog.inspectors) == 2
        _wait_for(qt_app, lambda: dialog.inspectors[1].properties_table.rowCount() == 2)
        assert dialog.inspectors[1].windowTitle() == \
            "Objekt-Inspektor - gameObject (UnityEngine.GameObject) (2)"
        assert dialog.inspectors[1].properties_table.item(0, 0).text() == "GetComponents()"
    finally:
        dialog.close()
        target.close()


@requires_target
def test_monitor_options_default_to_what_the_method_has(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.catalog import CatalogMethod
    from hermes.ui.widgets.method_monitor import MethodMonitorDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = MethodMonitorDialog(translations, target.pid)
    try:
        # An instance method with an out argument: both options.
        dialog.add_method(CatalogMethod(
            "Assembly-CSharp", "Game.Combat", "CalcDamage", "(Game.Combat,Game.DamageInfo&)",
            "System.Void", False, 0x06000005, _CALC_DAMAGE["method"]))
        # A static method with a ref int: a result to show, but no objects.
        dialog.add_method(CatalogMethod(
            "Assembly-CSharp", "Game.Inventory", "AddGold", "(int&,int)", "System.Void", True,
            0x06000001, "Game.Inventory:AddGold (int&,int)"))
        # Neither: Game.Loop:Update returns nothing and has no arguments.
        dialog.add_method(CatalogMethod(
            "Assembly-CSharp", "Game.Loop", "Update", "()", "System.Void", True,
            0x0600000c, "Game.Loop:Update ()"))
        _wait_for(qt_app, lambda: all(dialog.methods_table.item(row, 5).text() == "Aktiv"
                                      for row in range(3)))
        states = sorted(dialog._monitors.values(), key=lambda state: state["method"].token)
        options = [(state["returns_check"].isChecked(), state["returns_check"].isEnabled(),
                    state["objects_check"].isChecked(), state["objects_check"].isEnabled())
                   for state in states]
        assert options == [(True, True, False, False),   # AddGold
                           (True, True, True, True),     # CalcDamage
                           (False, False, False, False)]  # Update

        # Recorded accordingly: the out argument afterwards, objects' fields.
        target.send("calc", 0)
        _wait_for(qt_app, lambda: dialog.log.rowCount() == 1)
        info = dialog.log.calls[0]["args"][1]
        assert info["after"]["text"].startswith("Game.DamageInfo@0x")
        assert "snapshot" in info["after"]
        # Switching an option off still works and sticks.
        states[1]["objects_check"].setChecked(False)
        _wait_for(qt_app, lambda: dialog.methods_table.item(1, 5).text() == "Aktiv")
        assert not states[1]["objects_check"].isChecked()
    finally:
        dialog.close()
        target.close()


_ROLL_DAMAGE = {"engine": "mono", "image": "Assembly-CSharp", "file": None, "token": 0x06000004,
                "method": "Game.Combat:RollDamage (int,single,string,bool,Game.DamageType,int&)"}


def _method_transform(source, **params):
    # pylint: disable=import-outside-toplevel
    from hermes.hooks.transforms import TemplateTransform, TransformDefinition, TransformParameter

    definition = TransformDefinition(
        "test", source, tuple(TransformParameter(name, default=value)
                              for name, value in params.items()), kind="method")
    return TemplateTransform(definition, **params)


@requires_target
def test_method_hook_replaces_the_result(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.method_hooks import MethodHookSpec, MethodHookWorker

    target = _MonoTarget(_build(tmp_path))
    worker = MethodHookWorker(target.pid)
    installed, failed, errors, events, debug = {}, {}, {}, [], []
    worker.hook_installed.connect(installed.__setitem__)
    worker.hook_failed.connect(failed.__setitem__)
    worker.transform_error.connect(errors.__setitem__)
    worker.hook_events.connect(events.extend)
    worker.hook_debug.connect(lambda entries, _dropped: debug.extend(entries))
    script = """function (ctx) {
      const self = ctx.this;
      ctx.log(self.name, self.get("IsPartyMember"), self.field("Health"),
              self.component("Combat").className, ctx.args.name, ctx.args.min, ctx.refs.roll);
      // Fields and properties also read directly; unknown names stay undefined.
      ctx.log(self.IsPartyMember, self.Health, self.gameObject.name, self.Nothing);
      if (self.get("IsPartyMember"))
        return;  // keep it
      return ctx.result * ctx.params.factor * ctx.call("Game.Combat.get_Count");
    }"""
    try:
        worker.start()
        hook_id = worker.add_hook(MethodHookSpec(
            _ROLL_DAMAGE, _method_transform(script, factor=10), label="Damage"))
        _wait_for(qt_app, lambda: hook_id in installed or hook_id in failed)
        assert not failed, failed
        worker.set_debug(hook_id, True)
        time.sleep(0.2)

        # 5 * 1.5 * 2 (crit) = 15, times 10 times Count (7)
        assert target.send("roll", 5) == (1050, 42)
        _wait_for(qt_app, lambda: debug and events)
        assert not errors
        assert debug[0]["logs"] == ['Hero false 100 Game.Combat Fireball 5 42',
                                    'false 100 Hero undefined']
        assert (debug[0]["old"], debug[0]["result"], debug[0]["kept"]) == ("15", "1050", False)
        assert events[0]["hookId"] == hook_id and events[0]["count"] == 1

        # Paused, the method works as without the hook.
        worker.set_enabled(hook_id, False)
        time.sleep(0.2)
        assert target.send("roll", 5) == (15, 42)

        # A failing script keeps the result and reports the error once.
        worker.set_enabled(hook_id, True)
        worker.set_transform(hook_id, _method_transform(
            'function (ctx) { return ctx.call("Game.Nowhere.Method"); }'))
        time.sleep(0.2)
        assert target.send("roll", 5) == (15, 42)
        _wait_for(qt_app, lambda: hook_id in errors)
        assert "Game.Nowhere not found" in errors[hook_id]

        # Named as the catalog names methods, too: Class:Method, also with
        # its parameters; 3 times Count (7).
        worker.set_transform(hook_id, _method_transform(
            'function (ctx) { return ctx.call("Game.Combat:get_Count") '
            '+ ctx.call("Game.Combat:get_Count ()") + ctx.call("Game.Combat.get_Count"); }'))
        time.sleep(0.2)
        assert target.send("roll", 5) == (21, 42)

        # Unity objects by class and by their name: Health 100, plus 10 for
        # the GameObject, plus 1 for nothing found under another name.
        worker.set_transform(hook_id, _method_transform(
            'function (ctx) { return ctx.find("Game.Combat", "Hero").Health'
            ' + (ctx.find("UnityEngine.GameObject", "Hero").name === "Hero" ? 10 : 0)'
            ' + (ctx.find("Game.Combat", "Nobody") === null ? 1 : 0); }'))
        time.sleep(0.2)
        assert target.send("roll", 5) == (111, 42)

        removed = []
        worker.hook_removed.connect(removed.append)
        worker.remove_hook(hook_id)
        _wait_for(qt_app, lambda: removed)
        assert target.send("roll", 5) == (15, 42)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


def test_hook_dialog_offers_method_templates_for_methods(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "", "", "", method="Health:DoDamage (single)")
    names = [dialog.template_combo.itemText(i) for i in range(dialog.template_combo.count())]
    assert names == ["Result × factor", "Method script"]
    assert not hasattr(dialog, "caller_depth_input")
    dialog.label_input.setText("Schaden")
    dialog._accept()  # pylint: disable=protected-access
    assert dialog.transform.params == {"factor": 2} and dialog.label == "Schaden"


def test_method_script_with_a_syntax_error_is_refused(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from PyQt6.QtWidgets import QPlainTextEdit  # pylint: disable=import-error,no-name-in-module
    from hermes.hooks.syntax import checker
    from hermes.ui.widgets.hooks import HookDialog

    if not checker().available:
        return  # needs Frida's system session
    dialog = HookDialog(translations, "", "", "", method="Health:DoDamage (single)")
    dialog.template_combo.setCurrentIndex(1)  # the method script
    editor = dialog.forms.currentWidget().findChild(QPlainTextEdit)
    editor.setPlainText("const a = 1;\nreturn a * ;")
    dialog._accept()
    assert dialog.transform is None
    assert "Syntaxfehler in Zeile 2" in dialog.error_label.text()

    editor.setPlainText("return ctx.result * 2;")
    dialog._accept()
    assert dialog.transform.params["code"] == "return ctx.result * 2;"


@requires_target
def test_method_hooks_in_the_panel_are_saved_and_restored(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access,too-many-locals
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.hooks import HookRepository
    from hermes.features.method_hooks import MethodHookSpec
    from hermes.ui.widgets.hooks import HookPanel

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    binary = _build(tmp_path)
    process_id = repository.get_or_create_process("target", str(binary))
    status = lambda panel: panel.table.item(0, 4).text()  # pylint: disable=unnecessary-lambda-assignment

    target, panel = _MonoTarget(binary), HookPanel(translations, repository=repository)
    try:
        panel.set_process(target.pid, process_id)
        panel.add_method_hook(MethodHookSpec(
            _ROLL_DAMAGE, _method_transform("function (ctx) { return ctx.result * ctx.params.factor; }",
                                            factor=3), label="Schaden"))
        _wait_for(qt_app, lambda: status(panel) == "Aktiv")
        assert panel.table.item(0, 1).text() == "C#-Methode · nach Aufruf"
        assert panel.table.item(0, 2).text() == "Combat:RollDamage"
        assert target.send("roll", 5) == (45, 42)
        _wait_for(qt_app, lambda: panel.table.item(0, 5).text() == "1")
        assert panel.table.item(0, 6).text() == "15 ⇒ 45"
        saved = repository.list_method_hooks(process_id)
        assert [(hook.label, hook.managed["method"], hook.transform_params) for hook in saved] == [
            ("Schaden", _ROLL_DAMAGE["method"], {"factor": 3})]

        # Paused it does nothing, and stays paused after a restart.
        panel.table.selectRow(0)
        panel._toggle_selected()
        _wait_for(qt_app, lambda: status(panel) == "Pausiert")
        time.sleep(0.2)
        assert target.send("roll", 5) == (15, 42)
        panel._toggle_selected()
        _wait_for(qt_app, lambda: status(panel) == "Aktiv")
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()

    # After a restart the saved hook is installed right away.
    target, panel = _MonoTarget(binary), HookPanel(translations, repository=repository)
    try:
        panel.set_process(target.pid, process_id)
        _wait_for(qt_app, lambda: panel.table.rowCount() == 1 and status(panel) == "Aktiv")
        assert target.send("roll", 2) == (18, 42)
        panel.table.selectRow(0)
        panel._remove_selected()
        _wait_for(qt_app, lambda: panel.table.rowCount() == 0)
        assert repository.list_method_hooks(process_id) == []
        assert target.send("roll", 2) == (6, 42)
    finally:
        panel.shutdown(timeout_ms=None)
        target.close()
        engine.dispose()


@requires_target
def test_monitor_offers_to_hook_a_method(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel
    from hermes.features.catalog import CatalogMethod
    from hermes.ui.widgets.method_monitor import MethodMonitorDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = MethodMonitorDialog(translations, target.pid)
    requested = []
    dialog.hook_requested.connect(requested.append)
    method = CatalogMethod("Assembly-CSharp", "Game.Combat", "RollDamage",
                           "(int,single,string,bool,Game.DamageType,int&)", "System.Single",
                           False, 0x06000004, _ROLL_DAMAGE["method"])
    try:
        dialog.add_method(method)
        assert not dialog.hook_button.isEnabled()
        dialog.methods_table.selectRow(0)
        assert dialog.hook_button.isEnabled()
        dialog.hook_button.click()
        assert requested == [method]
    finally:
        dialog.close()
        target.close()


@requires_target
def test_method_hook_before_the_call_changes_arguments_and_fields(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.method_hooks import MethodHookSpec, MethodHookWorker

    target = _MonoTarget(_build(tmp_path))
    worker = MethodHookWorker(target.pid)
    installed, failed, errors, events, debug = {}, {}, {}, [], []
    worker.hook_installed.connect(installed.__setitem__)
    worker.hook_failed.connect(failed.__setitem__)
    worker.transform_error.connect(errors.__setitem__)
    worker.hook_events.connect(events.extend)
    worker.hook_debug.connect(lambda entries, _dropped: debug.extend(entries))
    before = """function (ctx) {
      ctx.args.min = 10;          // int in a register
      ctx.args.scale = 3;         // float in xmm1
      ctx.args.crit = false;      // bool
      ctx.args.roll = 7;          // ref int: the caller's variable
      ctx.this.Health = 5;        // a field of the instance
      return 1000;                // ignored before the call
    }"""
    after = """function (ctx) {
      ctx.log(ctx.phase, ctx.args.min, ctx.args.scale, ctx.args.crit, ctx.this.Health);
    }"""
    try:
        worker.start()
        first = worker.add_hook(MethodHookSpec(_ROLL_DAMAGE, _method_transform(before),
                                               phase="before"))
        second = worker.add_hook(MethodHookSpec(_ROLL_DAMAGE, _method_transform(after)))
        _wait_for(qt_app, lambda: {first, second} <= set(installed) | set(failed))
        assert not failed, failed
        for hook_id in (first, second):
            worker.set_debug(hook_id, True)
        time.sleep(0.2)

        # 10 * 3 without crit; RollDamage itself writes roll = 42 afterwards.
        assert target.send("roll", 5) == (30, 42)
        _wait_for(qt_app, lambda: len(debug) == 2 and events)
        assert not errors
        changed = next(entry for entry in debug if entry["hookId"] == first)
        # Fields are noted when set, arguments once the script is done.
        assert changed["result"] == "Combat.Health=5, min=10, scale=3, crit=false, roll=7"
        assert changed["old"] == "Combat.Health=100, min=5, scale=1.5, crit=true, roll=-1"
        assert not changed["kept"]
        logged = next(entry for entry in debug if entry["hookId"] == second)
        assert logged["logs"] == ["after 10 3 false 5"]

        # A typo in a field name is reported; the call goes on unchanged.
        worker.set_transform(first, _method_transform(
            "function (ctx) { ctx.this.Helth = 5; }"))
        time.sleep(0.2)
        assert target.send("roll", 5) == (15, 42)
        _wait_for(qt_app, lambda: first in errors)
        assert "Game.Combat has no field or property Helth to set" in errors[first]

        # Switched to after the call, the same hook can replace the result.
        worker.set_transform(first, _method_transform(
            "function (ctx) { return ctx.phase === 'after' ? ctx.result * 2 : undefined; }"))
        worker.set_phase(first, "after")
        time.sleep(0.2)
        assert target.send("roll", 5) == (30, 42)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


def test_hook_dialog_chooses_the_phase_of_method_hooks(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.ui.widgets.hooks import HookDialog

    dialog = HookDialog(translations, "", "", "", method="Health:DoDamage (single)")
    assert dialog.phase_combo.currentData() == "after"
    assert [dialog.phase_combo.itemText(i) for i in range(2)] == [
        "Nach dem Aufruf: Ergebnis ändern", "Vor dem Aufruf: Argumente und Felder ändern"]
    dialog.phase_combo.setCurrentIndex(1)
    dialog._accept()
    assert dialog.phase == "before"

    editing = HookDialog(translations, "", "", "", transform=dialog.transform,
                         method="Health:DoDamage (single)", phase="before")
    assert editing.phase_combo.currentData() == "before"
    # Value hooks have no phase.
    assert not hasattr(HookDialog(translations, "0x1000", "int32", "game+0x10"), "phase_combo")


@requires_target
def test_panel_saves_the_phase_of_method_hooks(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from hermes.db import models  # noqa: F401  pylint: disable=unused-import
    from hermes.db.base import Base
    from hermes.db.repository.hooks import HookRepository
    from hermes.features.method_hooks import MethodHookSpec
    from hermes.ui.widgets.hooks import HookPanel

    engine = create_engine(f"sqlite:///{tmp_path / 'hermes.db'}")
    Base.metadata.create_all(engine)
    repository = HookRepository(sessionmaker(bind=engine))
    binary = _build(tmp_path)
    process_id = repository.get_or_create_process("target", str(binary))
    script = "function (ctx) { ctx.args.min = 1; }"
    try:
        for restart in (False, True):
            target, panel = _MonoTarget(binary), HookPanel(translations, repository=repository)
            try:
                panel.set_process(target.pid, process_id)
                if not restart:
                    panel.add_method_hook(MethodHookSpec(
                        _ROLL_DAMAGE, _method_transform(script), label="min", phase="before"))
                _wait_for(qt_app, lambda panel=panel: panel.table.rowCount() == 1
                          and panel.table.item(0, 4).text() == "Aktiv")
                assert panel.table.item(0, 1).text() == "C#-Methode · vor Aufruf"
                # min 1: 1 * 1.5 * 2
                assert target.send("roll", 5) == (3, 42)
            finally:
                panel.shutdown(timeout_ms=None)
                target.close()
        assert [hook.phase for hook in repository.list_method_hooks(process_id)] == ["before"]
    finally:
        engine.dispose()


def test_hook_context_menu_offers_what_fits_the_hook(qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument,protected-access
    from hermes.core.pointer import PointerChain
    from hermes.hooks.engine import HookSpec
    from hermes.features.method_hooks import MethodHookSpec
    from hermes.ui.widgets.hooks import HookPanel

    panel = HookPanel(translations)
    panel._pid = 1234  # a process, but no worker is needed for the menu
    multiply = MultiplyTransform(2)
    panel._hooks = {
        1: {"spec": HookSpec(0x1000, "int32", multiply, trap_pc=0x10)},
        2: {"spec": HookSpec(None, "int32", multiply, write_pc=0x10,
                             pointer=PointerChain("game", 0x10, (0x8,)))},
        3: {"spec": HookSpec(None, "int32", multiply, write_pc=0x10)},
        4: {"kind": "method", "spec": MethodHookSpec(_ROLL_DAMAGE, _method_transform("x => 1"))},
    }
    watched, monitored = [], []
    panel.watch_requested.connect(lambda spec, reads: watched.append((spec, reads)))
    panel.monitor_requested.connect(monitored.append)

    def actions(hook_id):
        menu = panel.context_menu(hook_id)
        return [(a.text(), a.isEnabled()) for a in menu.actions() if not a.isSeparator()], menu

    for hook_id in (1, 2):
        entries, menu = actions(hook_id)
        assert entries == [("Adress-Überwachung (Schreibzugriffe)", True),
                           ("Adress-Überwachung (alle Zugriffe)", True),
                           ("Methoden-Überwachung", False)]
        menu.actions()[1].trigger()
        assert watched[-1] == (panel._hooks[hook_id]["spec"], True)
    # A hook for every address its code writes watches the one it wrote last.
    entries, _menu = actions(3)
    assert entries == [("Adress-Überwachung (Schreibzugriffe) · noch kein Schreibzugriff", False),
                       ("Adress-Überwachung (alle Zugriffe) · noch kein Schreibzugriff", False),
                       ("Methoden-Überwachung", False)]
    panel._hooks[3]["count"] = 0
    panel._hooks[3]["status"] = "active"
    panel._worker = worker = object()
    panel._on_events(worker, [{"hookId": 3, "count": 1,
                             "last": {"old": 1, "new": 2, "result": 4, "address": "0x2000"}}])
    entries, menu = actions(3)
    assert entries[0] == ("Adress-Überwachung (Schreibzugriffe) · zuletzt 0x2000", True)
    menu.actions()[0].trigger()
    spec, reads = watched[-1]
    assert (spec.address, spec.pointer, spec.write_pc, reads) == (0x2000, None, 0x10, False)
    assert panel._hooks[3]["spec"].address is None
    entries, menu = actions(4)
    assert [enabled for _text, enabled in entries] == [False, False, True]
    menu.actions()[-1].trigger()
    assert monitored == [_ROLL_DAMAGE]


def test_catalog_method_from_a_hook_identity():
    # pylint: disable=import-outside-toplevel
    from hermes.features.catalog import catalog_method_from_identity

    method = catalog_method_from_identity(_ROLL_DAMAGE)
    assert (method.assembly, method.class_name, method.name, method.parameters) == (
        "Assembly-CSharp", "Game.Combat", "RollDamage",
        "(int,single,string,bool,Game.DamageType,int&)")
    assert method.identity() == _ROLL_DAMAGE


@requires_target
def test_mono_trace_finds_what_a_scenario_calls(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.catalog import CatalogMethod
    from hermes.features.trace import NEW, SAME, TraceWorker, analyze, recording_calls

    target = _MonoTarget(_build(tmp_path))
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
        # The generic class's definition has no code to trace.
        assert planned[0] == {"methods": 8, "skipped": {"abstract": 0, "native": 0, "generic": 1},
                              "assemblies": ["Assembly-CSharp"]}

        worker.trace([])
        _wait_for(qt_app, lambda: started or failed)
        assert not failed
        methods, info = started[0]
        assert (info["notCompiled"], info["failed"]) == (0, 0)
        # get_Broken uses a [ComImport] class: compiling it would hang Mono.
        assert info["skipped"]["com"] == 1
        assert sorted(method.name for method in methods) == [
            "AddGold", "CalcDamage", "RollDamage", "Take", "Update", "get_Count",
            "get_IsPartyMember"]
        index = {method.name: i for i, method in enumerate(methods)}

        # The game's frame loop is noise; looting is what we look for.
        baseline = record("baseline", ("tick", 0), ("tick", 0), ("tick", 0))
        assert {int(key): count for key, count in baseline["counts"].items()} == {
            index["Update"]: 3}
        assert baseline["events"] == []
        scenario = record("scenario", ("tick", 0), ("loot", 5), ("tick", 0))
        rows = analyze(scenario, baseline)
        assert [(methods[row.method].name, row.verdict, row.calls) for row in rows] == [
            ("Take", NEW, 1), ("AddGold", NEW, 1), ("Update", SAME, 2)]
        # The sequence leaves out the noise; Take's JIT code calls AddGold.
        calls = recording_calls(scenario)
        assert [methods[call.method].name for call in calls] == ["Take", "AddGold"]
        assert calls[0].caller.startswith("target+0x")
        assert calls[1].caller.startswith("Game.Loot:Take+0x")
        assert calls[0].time <= calls[1].time
        assert calls[0].thread == target.pid

        # Without a baseline everything the scenario called is new.
        assert [row.verdict for row in analyze(scenario)] == [NEW, NEW, NEW]

        # The methods to choose from: the catalog cache has them now.
        found = []
        worker.candidates_found.connect(lambda methods, skipped: found.append((methods, skipped)))
        worker.candidates()
        _wait_for(qt_app, lambda: found or failed)
        assert not failed
        methods, skipped = found[0]
        assert len(methods) == 8 and skipped["generic"] == 1
        assert all(isinstance(method, CatalogMethod) for method in methods)
        assert (tmp_path / "catalog-cache" / "mono" / "Assembly-CSharp").is_dir()
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


def test_trace_analysis_compares_rates_with_the_baseline():
    # pylint: disable=import-outside-toplevel
    from hermes.features.trace import MORE, NEW, SAME, analyze

    baseline = {"phase": "baseline", "duration": 2000, "counts": {"0": 20, "1": 20, "2": 1}}
    # one second: method 0 as usual, 1 five times as often, 2 only two more calls
    scenario = {"phase": "scenario", "duration": 1000, "counts": {"0": 11, "1": 50, "2": 3, "3": 4},
                "events": [[3, 1, 10, "a"], [3, 1, 20, "b"]]}
    rows = analyze(scenario, baseline)
    assert [(row.method, row.verdict) for row in rows] == [(3, NEW), (1, MORE), (2, SAME), (0, SAME)]
    assert rows[0].first == 0 and rows[0].baseline_rate == 0
    assert rows[1].factor == 5
    assert [row.notable for row in rows] == [True, True, False, False]


@requires_target
def test_trace_window_shows_what_the_scenario_called(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.ui.widgets.trace import TraceDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = TraceDialog(translations, target.pid)
    monitored, hooked = [], []
    dialog.monitor_requested.connect(monitored.append)
    dialog.hook_requested.connect(hooked.append)

    def record(button, *commands):
        button.click()
        _wait_for(qt_app, lambda: dialog.recording_label.text()
                  != translations["trace.recording_starting"])
        for command, value in commands:
            target.send(command, value)
        button.click()
        _wait_for(qt_app, lambda: dialog._state == 2)  # ready again

    try:
        assert not dialog.baseline_button.isEnabled()
        dialog.filter_input.setText("game")
        dialog.trace_button.click()
        _wait_for(qt_app, lambda: dialog.methods or "Fehler" in dialog.status_label.text())
        assert "7 Methoden werden beobachtet (1 übersprungen" in dialog.status_label.text()
        assert dialog.trace_button.text() == translations["trace.stop"]

        record(dialog.baseline_button, ("tick", 0), ("tick", 0))
        assert "Grundrauschen: 1 Methoden" in dialog.status_label.text()
        record(dialog.scenario_button, ("tick", 0), ("add", 2), ("tick", 0))
        assert "davon 1 auffällig" in dialog.status_label.text()
        # Update ran like in the baseline: hidden unless asked for
        assert [dialog.row_model.data(dialog.row_model.index(row, 2))
                for row in range(dialog.row_model.rowCount())] == ["Inventory:AddGold"]
        assert dialog.row_model.data(dialog.row_model.index(0, 6)) == "neu"
        dialog.notable_check.setChecked(False)
        assert dialog.row_model.rowCount() == 2
        assert dialog.sequence_model.rowCount() == 1
        assert dialog.sequence_model.data(dialog.sequence_model.index(0, 4)).startswith("target+0x")

        dialog.sequence_view.selectRow(0)
        assert dialog.selection_label.text() == "Game.Inventory:AddGold (int&,int)"
        dialog.monitor_button.click()
        dialog.hook_button.click()
        assert [method.signature for method in monitored + hooked] == [
            "Game.Inventory:AddGold (int&,int)"] * 2

        dialog.trace_button.click()
        _wait_for(qt_app, lambda: dialog.trace_button.text() == translations["trace.start"]
                  and dialog.trace_button.isEnabled())
        assert dialog.status_label.text() == translations["trace.stopped"]
        assert target.send("add", 1) == (103, 0)
    finally:
        dialog.close()
        target.close()


@requires_target
def test_trace_dialog_traces_a_preselection(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,unused-argument
    from PyQt6.QtCore import Qt  # pylint: disable=import-error,no-name-in-module
    from hermes.ui.widgets.trace import TraceDialog

    target = _MonoTarget(_build(tmp_path))
    dialog = TraceDialog(translations, target.pid)
    try:
        _wait_for(qt_app, lambda: dialog.filter_panel.candidates is not None)
        panel = dialog.filter_panel
        total = len(panel.candidates)
        # The progress bar gave way to the count of the choice.
        assert dialog.loading_bar.isHidden() and not dialog.choice_label.isHidden()
        assert dialog.choice_label.text() == translations["trace.filters_count"].format(
            count=total, total=total)
        # Under Wine the dialog refuses more than its limit (here: one method)
        dialog.limit = 1
        dialog.trace_button.click()
        assert dialog.status_label.text() == translations["trace.wine_limit"].format(
            count=total, limit=1)
        assert dialog._state == 0 and not dialog.methods  # idle, nothing hooked

        money = next(row for row in range(panel.list.count())
                     if panel.list.item(row).text().startswith("Geld"))
        panel.list.item(money).setCheckState(Qt.CheckState.Checked)
        assert dialog.choice_label.text().startswith(
            translations["trace.filters_count"].format(count=1, total=total))
        dialog.trace_button.click()
        _wait_for(qt_app, lambda: dialog.methods or "Fehler" in dialog.status_label.text())
        assert [method.signature for method in dialog.methods] == [
            "Game.Inventory:AddGold (int&,int)"]
        assert not panel.isEnabled()  # no changes while tracing
    finally:
        dialog.close()
        target.close()


@requires_target
def test_mono_trace_preparation_can_be_cancelled(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.trace import TraceWorker

    # Compiling takes 0.3 s per method here: 2.4 s for all eight.
    target = _MonoTarget(_build(tmp_path), slow_ms=300)
    worker = TraceWorker(target.pid)
    progress, started, failed = [], [], []
    worker.progress.connect(
        lambda step, done, total, current: progress.append((step, done, total, current)))
    worker.started.connect(lambda methods, info: started.append(methods))
    worker.command_failed.connect(lambda action, message: failed.append((action, message)))
    try:
        worker.start()
        worker.trace([])
        _wait_for(qt_app, lambda: any(step == "compile" for step, *_rest in progress))
        assert ("compile", 0, 7, "Game.Inventory:AddGold (int&,int)") in progress
        # Before: reading the methods to choose from
        assert progress[:2] == [("catalog", 0, 1, "Assembly-CSharp"), ("catalog", 1, 1, "")]
        began = time.monotonic()
        worker.cancel()
        _wait_for(qt_app, lambda: failed)
        assert failed == [("start", "Error: cancelled")]
        assert time.monotonic() - began < 1.5
        assert not started
        # The game was not disturbed.
        assert target.send("add", 1) == (101, 0)
    finally:
        worker.requestInterruption()
        assert worker.wait(5000)
        target.close()


@requires_target
def test_sessions_list_workers_and_cut_one_that_hangs(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.trace import TraceWorker
    from hermes.core.sessions import SESSIONS, TRACE

    target = _MonoTarget(_build(tmp_path), slow_ms=300)
    worker = TraceWorker(target.pid)
    # As if the agent did not answer: stopping alone does not end the trace.
    worker.cancel = lambda: None
    progress = []
    worker.progress.connect(lambda step, *_rest: progress.append(step))
    try:
        worker.start()
        _wait_for(qt_app, lambda: any(s.frida_session for s in SESSIONS.sessions(target.pid)))
        [session] = SESSIONS.sessions(target.pid)
        assert (session.kind, session.worker) == (TRACE, worker)

        worker.trace([])  # compiling takes 2.4 s here
        _wait_for(qt_app, lambda: "compile" in progress)
        SESSIONS.stop(session)
        assert session.stopping is not None and not session.stuck()
        assert not worker.wait(300)
        session.stopping -= 10  # as if asked long ago
        assert session.stuck()

        began = time.monotonic()
        assert SESSIONS.disconnect(session)
        assert worker.wait(5000)
        assert time.monotonic() - began < 2
        _wait_for(qt_app, lambda: not SESSIONS.sessions(target.pid))
        # The batch thread finished its C code: the game lives on.
        time.sleep(2.5)
        assert target.send("add", 1) == (101, 0)
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_agent_answers_while_mono_works_on_a_thread_of_its_own(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel
    from hermes.features.monitor import MethodMonitorWorker
    from hermes.features.trace import TraceWorker

    # Compiling a method takes 2 s; all sessions share the agent's thread.
    target = _MonoTarget(_build(tmp_path), slow_ms=2000)
    monitor = MethodMonitorWorker(target.pid)
    trace = TraceWorker(target.pid)
    added, planned = [], []
    monitor.monitor_added.connect(lambda monitor_id, _info: added.append(time.monotonic()))
    trace.planned.connect(lambda plan: planned.append(time.monotonic()))
    try:
        monitor.start()
        trace.start()
        _wait_for(qt_app, lambda: trace._script is not None)  # pylint: disable=protected-access
        began = time.monotonic()
        monitor.add_monitor({"image": "Assembly-CSharp", "file": None, "token": 0x06000002,
                             "method": "Game.Loot:Take (int&,int)"})
        time.sleep(0.3)  # compiling now
        trace.plan([])
        _wait_for(qt_app, lambda: planned and added, timeout=10)
        # The trace was answered while the monitor still waited for Mono.
        assert planned[0] - began < 1.5 < added[0] - began
    finally:
        for worker in (monitor, trace):
            worker.requestInterruption()
            worker.wait(5000)
        target.close()


@requires_target
def test_hook_panel_logs_scripts_and_acts_on_several_hooks(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.features.method_hooks import MethodHookSpec
    from hermes.ui.widgets.hooks import HookPanel

    target, panel = _MonoTarget(_build(tmp_path)), HookPanel(translations)
    status = lambda row: panel.table.item(row, 4).text()  # pylint: disable=unnecessary-lambda-assignment
    try:
        panel.set_process(target.pid)
        logger = panel.add_method_hook(MethodHookSpec(_ROLL_DAMAGE, _method_transform(
            "function (ctx) { ctx.log('rolled', ctx.result); }"), label="Logger"))
        doubler = panel.add_method_hook(MethodHookSpec(_ROLL_DAMAGE, _method_transform(
            "function (ctx) { return ctx.result * 2; }"), label="Doppelt"))
        _wait_for(qt_app, lambda: status(0) == status(1) == "Aktiv")

        # What a script logs shows below the table without debug mode.
        assert target.send("roll", 5) == (30, 42)
        _wait_for(qt_app, lambda: f"#{logger} Logger: rolled 15" in panel.log_view.toPlainText())
        assert panel._debug_window is None

        # Both selected: Edit is for one hook only; debug, pause and remove act on both.
        panel.table.selectAll()
        assert panel._selected_hook_ids() == [logger, doubler]
        assert not panel.btn_edit.isEnabled() and panel.btn_debug.isEnabled()
        panel.btn_debug.click()
        assert all(panel._hooks[hook_id]["debug"] for hook_id in (logger, doubler))
        assert panel.btn_debug.isChecked() and panel._debug_window.isVisible()
        panel.btn_debug.click()
        assert not any(panel._hooks[hook_id]["debug"] for hook_id in (logger, doubler))

        panel.btn_toggle.click()
        _wait_for(qt_app, lambda: status(0) == status(1) == "Pausiert")
        assert panel.btn_toggle.text() == translations["hooks.resume"]
        time.sleep(0.2)
        assert target.send("roll", 5) == (15, 42)
        panel.btn_toggle.click()
        _wait_for(qt_app, lambda: status(0) == status(1) == "Aktiv")

        # A script's error goes to the log as well.
        panel.table.selectRow(1)
        assert panel.btn_edit.isEnabled()
        panel._method_worker.set_transform(doubler, _method_transform(
            "function (ctx) { return ctx.nothing.here; }"))
        time.sleep(0.2)
        target.send("roll", 5)
        _wait_for(qt_app, lambda: f"#{doubler} Doppelt: Fehler:" in panel.log_view.toPlainText())

        panel.table.selectAll()
        panel.btn_remove.click()
        _wait_for(qt_app, lambda: panel.table.rowCount() == 0)
        assert target.send("roll", 5) == (15, 42)
    finally:
        if panel._debug_window is not None:
            panel._debug_window.close()
        panel.shutdown(timeout_ms=None)
        target.close()
