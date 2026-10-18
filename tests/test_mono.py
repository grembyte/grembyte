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

typedef struct Class Class;
typedef struct { const char *name; int kind; int byref; Class *klass; } Type;
typedef struct { const char *name; const char *file; } Image;
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
} Method;

#define ADD_GOLD 0x06000001u
#define TAKE 0x06000002u
#define ROLL_DAMAGE 0x06000004u
#define CALC_DAMAGE 0x06000005u
#define STATIC 0x10u

/* Assembly-CSharp: Game.Inventory, Game.Loot, Game.Combat (a Unity
   component), the enum Game.DamageType, Game.DamageInfo and Game.Loop;
   mscorlib: System.Object, System.Int32, System.Int32[],
   UnityEngine.Component[] and System.InvalidOperationException;
   UnityEngine.CoreModule: UnityEngine.Object, .Component and .GameObject. */
static Image game = { "Assembly-CSharp", "/game/Assembly-CSharp.dll" };
static Image corlib = { "mscorlib", "/game/mscorlib.dll" };
static Image unity = { "UnityEngine.CoreModule", "/game/UnityEngine.CoreModule.dll" };
static Class classes[14];
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
static Class classes[14] = {
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
static int type_object_component, type_object_other;
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
    { "Game.Combat:CalcDamage", "Game.Combat:CalcDamage (Game.Combat,Game.DamageInfo&)",
      CALC_DAMAGE, &classes[3], "CalcDamage", 0, &t_void, 2, { &t_combat, &t_info_ref },
      { "other", "info" }, (void *)calc_damage },
    { "UnityEngine.Object:get_name", "UnityEngine.Object:get_name ()", 0x06000006u,
      &classes[8], "get_name", 0, &t_string, 0, { 0 }, { 0 }, (void *)unity_get_name },
    { "Game.Combat:get_IsPartyMember", "Game.Combat:get_IsPartyMember ()", 0x06000007u,
      &classes[3], "get_IsPartyMember", 0, &t_bool, 0, { 0 }, { 0 },
      (void *)combat_is_party_member },
    { "Game.Combat:get_Broken", "Game.Combat:get_Broken ()", 0x06000008u,
      &classes[3], "get_Broken", 0, &t_int, 0, { 0 }, { 0 }, (void *)combat_broken },
    { "Game.Combat:get_Count", "Game.Combat:get_Count ()", 0x06000009u,
      &classes[3], "get_Count", STATIC, &t_int, 0, { 0 }, { 0 }, (void *)combat_count },
    { "UnityEngine.Component:get_gameObject", "UnityEngine.Component:get_gameObject ()",
      0x0600000au, &classes[9], "get_gameObject", 0, &t_game_object, 0, { 0 }, { 0 },
      (void *)component_get_game_object },
    { "UnityEngine.GameObject:GetComponents", "UnityEngine.GameObject:GetComponents (System.Type)",
      0x0600000bu, &classes[10], "GetComponents", 0, &t_void, 1, { &t_type }, { "type" },
      (void *)game_object_get_components },
    { "Game.Loop:Update", "Game.Loop:Update ()", 0x0600000cu, &classes[13], "Update", STATIC,
      &t_void, 0, { 0 }, { 0 }, (void *)loop_update },
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
    callback(&unity, data);
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
            ("Assembly-CSharp", 6), ("mscorlib", 5), ("UnityEngine.CoreModule", 3)]
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
        assert dialog.model.rowCount() == 8
        assert "8 von 8 Methoden" in dialog.status_label.text()

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


_CALC_DAMAGE = {"image": "Assembly-CSharp", "file": None, "token": 0x06000005,
                "method": "Game.Combat:CalcDamage (Game.Combat,Game.DamageInfo&)"}


@requires_target
def test_method_monitor_records_objects_and_inspects_them(tmp_path, qt_app):
    # pylint: disable=import-outside-toplevel,too-many-locals
    from hermes.core.method_monitor import MethodMonitorWorker

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

        rolls = inspect(fields["Rolls"]["value"]["target"])
        assert rolls["length"] == 3
        assert [(f["name"], f["value"]["text"]) for f in rolls["fields"]] == [
            ("[0]", "3"), ("[1]", "4"), ("[2]", "5")]
        owner = inspect(fields["Owner"]["value"]["target"])
        assert [(f["name"], f["static"], f["value"]["text"]) for f in owner["fields"]] == [
            ("m_CachedPtr", False, "0x0"), ("Health", False, "100"), ("Instances", True, "")]

        # An address that holds no object is refused instead of read.
        assert "no object" in inspect({"address": "0x10"})
    finally:
        worker.requestInterruption()
        worker.wait(5000)
        target.close()


@requires_target
def test_monitor_window_opens_objects_in_the_inspector(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.core.mono_catalog import CatalogMethod
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

        # Owner and Rolls are objects: "öffnen" shows each in a new dialog.
        opens = [inspector.table.cellWidget(row, 4) is not None for row in range(5)]
        assert opens == [False, False, False, True, True]
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
    from hermes.core.method_monitor import MethodMonitorWorker

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
    from hermes.core.method_monitor import MethodMonitorWorker

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
        assert values["name"] == {"text": '"Miranda"'}
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
def test_inspector_evaluates_properties(tmp_path, qt_app, translations):
    # pylint: disable=import-outside-toplevel,protected-access
    from hermes.core.mono_catalog import CatalogMethod
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
    from hermes.core.mono_catalog import CatalogMethod
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
