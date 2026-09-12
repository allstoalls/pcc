/* pcc/py_runtime/src/py_substrate.c
 *
 * Low-level memory-access primitives used by pcc-Python ports of
 * runtime modules. The intent is to let pcc-Python author
 * C-struct-equivalent layouts without growing the pcc frontend's
 * syntax for raw pointer deref / pointer arithmetic today.
 *
 * Each helper is a one-liner that cc inlines and pcc can emit
 * directly. See docs/plans/python-frontend-plan.md Phase 4a for
 * the design motivation.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "py_internal.h"
#include "../include/py_runtime.h"

int32_t py_class_attr_cache_epoch = 0;
/* Classes that ever had __del__ installed; never decremented.  Mirrors the
 * pcc_class_del_defined_count global in the py_class.py port. */
int32_t pcc_class_del_defined_count = 0;
PyObject *py_inst_field_cache_cls0 = NULL;
PyObject *py_inst_field_cache_cls1 = NULL;
PyObject *py_inst_field_cache_cls2 = NULL;
PyObject *py_inst_field_cache_cls3 = NULL;
const char *py_inst_field_cache_name0 = NULL;
const char *py_inst_field_cache_name1 = NULL;
const char *py_inst_field_cache_name2 = NULL;
const char *py_inst_field_cache_name3 = NULL;
int32_t py_inst_field_cache_idx0 = -1;
int32_t py_inst_field_cache_idx1 = -1;
int32_t py_inst_field_cache_idx2 = -1;
int32_t py_inst_field_cache_idx3 = -1;
int32_t py_inst_field_cache_epoch0 = -1;
int32_t py_inst_field_cache_epoch1 = -1;
int32_t py_inst_field_cache_epoch2 = -1;
int32_t py_inst_field_cache_epoch3 = -1;

void *py_mem_alloc(size_t bytes) {
    return malloc(bytes);
}

void py_mem_free(void *p) {
    free(p);
}

void *py_mem_zero(void *p, size_t bytes) {
    if (p != NULL) memset(p, 0, bytes);
    return p;
}

void *py_mem_copy(void *dst, const void *src, size_t bytes) {
    if (dst != NULL && src != NULL) memmove(dst, src, bytes);
    return dst;
}

int64_t py_mem_load_i64(const void *p, int64_t offset) {
    int64_t v;
    memcpy(&v, (const char *)p + offset, sizeof v);
    return v;
}

int32_t py_mem_load_i32(const void *p, int64_t offset) {
    int32_t v;
    memcpy(&v, (const char *)p + offset, sizeof v);
    return v;
}

int8_t py_mem_load_i8(const void *p, int64_t offset) {
    int8_t v;
    memcpy(&v, (const char *)p + offset, sizeof v);
    return v;
}

void *py_mem_load_ptr(const void *p, int64_t offset) {
    void *v;
    memcpy(&v, (const char *)p + offset, sizeof v);
    return v;
}

void py_mem_store_i64(void *p, int64_t offset, int64_t v) {
    memcpy((char *)p + offset, &v, sizeof v);
}

void py_mem_store_i32(void *p, int64_t offset, int32_t v) {
    memcpy((char *)p + offset, &v, sizeof v);
}

void py_mem_store_i8(void *p, int64_t offset, int8_t v) {
    memcpy((char *)p + offset, &v, sizeof v);
}

void py_mem_store_ptr(void *p, int64_t offset, void *v) {
    memcpy((char *)p + offset, &v, sizeof v);
}

void *py_mem_ptr_add(void *p, int64_t offset) {
    return (char *)p + offset;
}

int32_t py_mem_ptr_is_tagged_int(const void *p) {
    return ((uintptr_t)p & 1u) == 1u ? 1 : 0;
}

void *py_mem_null_ptr(void) {
    return (void *)0;
}

/* Thread-local current-exception slot. Host for the return-code
 * exception model — see py_exc_tls.c (for the C-level consumers)
 * and pcc/py_runtime/py/py_exc_tls.py (for the Python port). */
static _Thread_local void *g_tls_current_exc = (void *)0;

void *py_tls_exc_get(void) {
    return g_tls_current_exc;
}

void py_tls_exc_set(void *exc) {
    g_tls_current_exc = exc;
}

/* Immortal singleton storage. Hosted here so py_obj.c (the
 * refcount/dispatch layer) can be replaced by pcc-Python without
 * losing these exported symbols. The cc-C runtime and pcc-Python
 * port both refer to the same objects through these declarations.
 *
 * PY_TYPE_NONE=0, PY_TYPE_BOOL=1, PY_FLAG_IMMORTAL=0x1. */
static PyObjectHeader py_none_storage = {
    .refcount = 1,
    .type_tag = PY_TYPE_NONE,
    .flags    = PY_FLAG_IMMORTAL,
};
static PyObjectHeader py_notimplemented_storage = {
    .refcount = 1,
    .type_tag = PY_TYPE_NONE,
    .flags    = PY_FLAG_IMMORTAL,
};
static PyObjectHeader py_true_storage = {
    .refcount = 1,
    .type_tag = PY_TYPE_BOOL,
    .flags    = PY_FLAG_IMMORTAL,
};
static PyObjectHeader py_false_storage = {
    .refcount = 1,
    .type_tag = PY_TYPE_BOOL,
    .flags    = PY_FLAG_IMMORTAL,
};
PyObject *const py_None  = (PyObject *)&py_none_storage;
PyObject *const py_NotImplemented = (PyObject *)&py_notimplemented_storage;
PyObject *const py_True  = (PyObject *)&py_true_storage;
PyObject *const py_False = (PyObject *)&py_false_storage;

/* Accessors for immortal runtime singletons. Kept for the C runtime
 * path; pcc-Python ports now read the exported globals directly with
 * pcc.unsafe global intrinsics. */
void *py_subs_none(void) {
    return (void *)py_None;
}

void *py_subs_true(void) {
    return (void *)py_True;
}

void *py_subs_false(void) {
    return (void *)py_False;
}

/* Built-in exception class tables. Originally lived in
 * py_exc_table.c, but we host them here in substrate because
 * py_exc_table.o gets replaced by the Python port in pcc-py
 * archives — if the tables stayed there, the substrate accessors
 * below would have nothing to read. Substrate is the always-C
 * bottom of the runtime, so the tables live here safely. */
const char *const PY_EXC_BUILTIN_NAMES[PY_EXC_N_BUILTIN] = {
    "BaseException",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "RuntimeError",
    "StopIteration",
    "ZeroDivisionError",
    "NameError",
    "NotImplementedError",
    "ArithmeticError",
    "LookupError",
    "OSError",
    "OverflowError",
    "AssertionError",
    "StopAsyncIteration",
    "ReferenceError",
    "MemoryError",
    "ImportError",
    "ModuleNotFoundError",
    "Warning",
    "UserWarning",
    "DeprecationWarning",
    "PendingDeprecationWarning",
    "SyntaxWarning",
    "RuntimeWarning",
    "FutureWarning",
    "ImportWarning",
    "UnicodeWarning",
    "BytesWarning",
    "EncodingWarning",
    "ResourceWarning",
};

const int32_t PY_EXC_PARENT[PY_EXC_N_BUILTIN] = {
    [PY_EXC_BASE]              = -1,
    [PY_EXC_EXCEPTION]         = PY_EXC_BASE,
    [PY_EXC_VALUEERROR]        = PY_EXC_EXCEPTION,
    [PY_EXC_TYPEERROR]         = PY_EXC_EXCEPTION,
    [PY_EXC_LOOKUPERROR]       = PY_EXC_EXCEPTION,
    [PY_EXC_KEYERROR]          = PY_EXC_LOOKUPERROR,
    [PY_EXC_INDEXERROR]        = PY_EXC_LOOKUPERROR,
    [PY_EXC_ATTRIBUTEERROR]    = PY_EXC_EXCEPTION,
    [PY_EXC_RUNTIMEERROR]      = PY_EXC_EXCEPTION,
    [PY_EXC_STOPITERATION]     = PY_EXC_EXCEPTION,
    [PY_EXC_ARITHMETICERROR]   = PY_EXC_EXCEPTION,
    [PY_EXC_ZERODIVISIONERROR] = PY_EXC_ARITHMETICERROR,
    [PY_EXC_OVERFLOWERROR]     = PY_EXC_ARITHMETICERROR,
    [PY_EXC_NAMEERROR]         = PY_EXC_EXCEPTION,
    [PY_EXC_NOTIMPLEMENTEDERROR] = PY_EXC_RUNTIMEERROR,
    [PY_EXC_OSERROR]           = PY_EXC_EXCEPTION,
    [PY_EXC_ASSERTIONERROR]    = PY_EXC_EXCEPTION,
    [PY_EXC_STOPASYNCITERATION] = PY_EXC_EXCEPTION,
    [PY_EXC_REFERENCEERROR]    = PY_EXC_EXCEPTION,
    [PY_EXC_MEMORYERROR]       = PY_EXC_EXCEPTION,
    [PY_EXC_IMPORTERROR]       = PY_EXC_EXCEPTION,
    [PY_EXC_MODULENOTFOUNDERROR] = PY_EXC_IMPORTERROR,
    [PY_EXC_WARNING]           = PY_EXC_EXCEPTION,
    [PY_EXC_USERWARNING]       = PY_EXC_WARNING,
    [PY_EXC_DEPRECATIONWARNING] = PY_EXC_WARNING,
    [PY_EXC_PENDINGDEPRECATIONWARNING] = PY_EXC_WARNING,
    [PY_EXC_SYNTAXWARNING]     = PY_EXC_WARNING,
    [PY_EXC_RUNTIMEWARNING]    = PY_EXC_WARNING,
    [PY_EXC_FUTUREWARNING]     = PY_EXC_WARNING,
    [PY_EXC_IMPORTWARNING]     = PY_EXC_WARNING,
    [PY_EXC_UNICODEWARNING]    = PY_EXC_WARNING,
    [PY_EXC_BYTESWARNING]      = PY_EXC_WARNING,
    [PY_EXC_ENCODINGWARNING]   = PY_EXC_WARNING,
    [PY_EXC_RESOURCEWARNING]   = PY_EXC_WARNING,
};

/* Per-tag class cache. Populated lazily on first access by whichever
 * implementation of py_exc_builtin_class is linked (C or pcc-Python).
 *
 * Exported so pcc-Python ports can access the same storage through
 * pcc.unsafe.global_addr instead of calling substrate helper functions. */
PyClassObject *py_exc_classes[PY_EXC_N_BUILTIN] = {0};


const char *py_subs_exc_name(int32_t tag) {
    if (tag < 0 || tag >= PY_EXC_N_BUILTIN) return (const char *)0;
    return PY_EXC_BUILTIN_NAMES[tag];
}

int32_t py_subs_exc_parent(int32_t tag) {
    if (tag < 0 || tag >= PY_EXC_N_BUILTIN) return -1;
    return PY_EXC_PARENT[tag];
}

int32_t py_subs_exc_n_builtin(void) {
    return PY_EXC_N_BUILTIN;
}

void *py_subs_exc_cache_get(int32_t tag) {
    if (tag < 0 || tag >= PY_EXC_N_BUILTIN) return (void *)0;
    return (void *)py_exc_classes[tag];
}

void py_subs_exc_cache_set(int32_t tag, void *cls) {
    if (tag < 0 || tag >= PY_EXC_N_BUILTIN) return;
    py_exc_classes[tag] = (PyClassObject *)cls;
}

void **py_subs_exc_cache_slot(int32_t tag) {
    if (tag < 0 || tag >= PY_EXC_N_BUILTIN) return (void **)0;
    return (void **)&py_exc_classes[tag];
}

/* py_set_dummy tombstone sentinel. Must have a stable address that
 * doesn't collide with real PyObject* heap allocations or tagged
 * ints. Lives here so py_set.c can be ported to pcc-Python without
 * losing the sentinel. */
static char py_set_dummy_storage = 0;
PyObject *const py_set_dummy = (PyObject *)&py_set_dummy_storage;

void *py_subs_set_dummy(void) {
    return (void *)py_set_dummy;
}

int32_t py_mem_ptr_eq(const void *a, const void *b) {
    return a == b ? 1 : 0;
}

int32_t py_mem_ptr_is_null(const void *p) {
    return p == (void *)0 ? 1 : 0;
}

/* ---- OS + stdio substrate helpers (for py_os.py / py_print.py) ------ */
#include <unistd.h>

const char *py_subs_getenv(const char *name) {
    if (name == (const char *)0) return (const char *)0;
    return getenv(name);
}

int32_t py_subs_setenv(const char *name, const char *value) {
    if (name == (const char *)0 || value == (const char *)0) return -1;
    return setenv(name, value, 1);
}

int32_t py_subs_unsetenv(const char *name) {
    if (name == (const char *)0) return -1;
    return unsetenv(name);
}

int32_t py_subs_path_exists(const char *path) {
    if (path == (const char *)0) return 0;
    return access(path, F_OK) == 0 ? 1 : 0;
}

int64_t py_subs_cstr_len(const char *s) {
    if (s == (const char *)0) return 0;
    return (int64_t)strlen(s);
}

int8_t py_subs_cstr_at(const char *s, int64_t i) {
    if (s == (const char *)0) return 0;
    return (int8_t)s[i];
}

void *py_subs_realloc(void *p, size_t bytes) {
    return realloc(p, bytes);
}

int64_t py_subs_write_fd(int32_t fd, const void *buf, int64_t n) {
    if (buf == (const void *)0 || n <= 0) return 0;
    ssize_t wrote = write((int)fd, buf, (size_t)n);
    return wrote > 0 ? (int64_t)wrote : 0;
}

int32_t py_subs_strcmp(const char *a, const char *b) {
    if (a == (const char *)0 || b == (const char *)0) return -1;
    return (int32_t)strcmp(a, b);
}

/* User-class tag allocator. Lives here so swapping py_class.c for
 * py_class.py preserves the counter across versions. Exported for
 * pcc.unsafe.global_addr access from the Python port. */
int32_t py_next_user_tag = PY_TYPE_USER_CLASS_START;

int32_t py_subs_alloc_user_tag(void) {
    int32_t tag = py_next_user_tag;
    py_next_user_tag = py_next_user_tag + 1;
    return tag;
}

/* Lazy "object" root class. Allocated on first call and cached.
 * Refcount is immortal-flagged so py_decref ignores it. */
PyClassObject *py_object_root_cache = (PyClassObject *)0;

void *py_subs_object_root(void) {
    if (py_object_root_cache != (PyClassObject *)0) return (void *)py_object_root_cache;
    PyClassObject **mro =
        (PyClassObject **)malloc(sizeof(PyClassObject *));
    if (mro == (PyClassObject **)0) return (void *)0;
    /* The cache is a raw process-global pointer, so this immortal root must
     * keep a stable address rather than enter the relocating heap. */
    PyClassObject *r = (PyClassObject *)calloc(1, sizeof(PyClassObject));
    if (r == (PyClassObject *)0) {
        free(mro);
        return (void *)0;
    }
    r->h.refcount = 1;
    r->h.type_tag = PY_TYPE_CLASS;
    r->h.flags = PY_FLAG_IMMORTAL | PY_FLAG_GC_MALLOC_ALLOC;
    if (pcc_gc_pointer_register((PyObject *)r) < 0) {
        free(r);
        free(mro);
        return (void *)0;
    }
    r->name       = "object";
    r->n_bases    = 0;
    r->bases      = (PyClassObject **)0;
    r->n_mro      = 1;
    r->mro        = mro;
    r->mro[0]     = r;
    r->n_fields   = 0;
    r->field_names = (const char **)0;
    r->n_methods  = 0;
    r->methods    = (PyClassMethod *)0;
    r->instance_size = (int32_t)sizeof(PyInstanceObject);
    r->type_tag_alloc = PY_TYPE_INSTANCE;
    py_object_root_cache = r;
    return (void *)r;
}
