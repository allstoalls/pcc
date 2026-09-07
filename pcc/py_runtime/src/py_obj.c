/* pcc/py_runtime/src/py_obj.c
 *
 * PyObject reference counting + dealloc dispatch.
 *
 * Phase 4c.11 split:
 *   - Immortal singletons (py_None/py_True/py_False) live in
 *     py_substrate.c so they remain exported when this module is
 *     replaced by the pcc-Python port.
 *   - Type-specific deallocators live in py_obj_dealloc.c so the
 *     dispatch here can be independently ported while the dealloc
 *     details (flexible-array-member free, child ref drop, etc.)
 *     stay C.
 */

#include "py_internal.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

extern void pcc_debug_note_alloc_size(void *ptr, int64_t size);
void pcc_gc_pin(PyObject *o);

static int pcc_obj_debug_runtime_enabled_cache = -1;

static void pcc_obj_runtime_log_event_code(
    int32_t category,
    int32_t event,
    int64_t value0,
    int64_t value1,
    const void *ptr
) {
    if (
        __atomic_load_n(&pcc_runtime_log_fast_state, __ATOMIC_RELAXED) != 0
    ) {
        pcc_runtime_log_event_code(category, event, value0, value1, ptr);
    }
}

static int pcc_obj_debug_runtime_enabled(void) {
    int cached = __atomic_load_n(
        &pcc_obj_debug_runtime_enabled_cache, __ATOMIC_ACQUIRE
    );
    if (cached < 0) {
        int value = getenv("PCC_DEBUG_RUNTIME") != NULL ? 1 : 0;
        int expected = -1;
        (void)__atomic_compare_exchange_n(
            &pcc_obj_debug_runtime_enabled_cache,
            &expected,
            value,
            0,
            __ATOMIC_ACQ_REL,
            __ATOMIC_ACQUIRE
        );
        cached = __atomic_load_n(
            &pcc_obj_debug_runtime_enabled_cache, __ATOMIC_ACQUIRE
        );
    }
    return cached;
}

static PyObject *pcc_gc_callbacks = NULL;
static int32_t pcc_gc_callbacks_firing = 0;

static PyObject *pcc_gc_ensure_callbacks(void) {
    if (pcc_gc_callbacks == NULL) {
        pcc_gc_callbacks = py_list_new(0);
        if (pcc_gc_callbacks != NULL) {
            pcc_gc_pin(pcc_gc_callbacks);
        }
    }
    return pcc_gc_callbacks;
}

PyObject *py_gc_callbacks_list(void) {
    PyObject *callbacks = pcc_gc_ensure_callbacks();
    if (callbacks != NULL) py_incref(callbacks);
    return callbacks;
}

void py_gc_callbacks_append(PyObject *callback) {
    PyObject *callbacks = pcc_gc_ensure_callbacks();
    if (callbacks == NULL) return;
    py_list_append(callbacks, callback);
}

static int pcc_gc_callback_eq(PyObject *a, PyObject *b) {
    if (a == b) return 1;
    if (a == NULL || b == NULL) return 0;
    if (PY_IS_TAGGED_INT(a) || PY_IS_TAGGED_INT(b)) return py_obj_eq(a, b);
    a = pcc_gc_note_relocation_read(a);
    b = pcc_gc_note_relocation_read(b);
    if (a == b) return 1;
    if (
        py_header(a)->type_tag == PY_TYPE_FUNC
        && py_header(b)->type_tag == PY_TYPE_FUNC
    ) {
        PyFuncObject *fa = (PyFuncObject *)a;
        PyFuncObject *fb = (PyFuncObject *)b;
        PyObject *a_captures = pcc_gc_load_ptr(a, &fa->captures);
        PyObject *b_captures = pcc_gc_load_ptr(b, &fb->captures);
        return fa->entry == fb->entry && py_obj_eq(a_captures, b_captures);
    }
    return py_obj_eq(a, b);
}

void py_gc_callbacks_remove(PyObject *callback) {
    PyObject *callbacks = pcc_gc_ensure_callbacks();
    if (callbacks == NULL) return;
    PyListObject *lst = (PyListObject *)callbacks;
    for (int64_t i = 0; i < lst->length; i++) {
        PyObject *existing = pcc_gc_load_ptr(callbacks, &lst->items[i]);
        int equal = pcc_gc_callback_eq(existing, callback);
        if (py_err_occurred()) return;
        if (equal) {
            PyObject *old = existing;
            if (i < lst->length - 1) {
                memmove(
                    &lst->items[i],
                    &lst->items[i + 1],
                    (size_t)(lst->length - i - 1) * sizeof(PyObject *)
                );
            }
            lst->length--;
            py_decref(old);
            return;
        }
    }
    py_list_remove(callbacks, callback);
}

static void pcc_gc_fire_callbacks(const char *phase) {
    if (pcc_gc_callbacks == NULL || pcc_gc_callbacks_firing != 0) return;
    int64_t n = py_list_len(pcc_gc_callbacks);
    if (n <= 0) return;

    pcc_gc_callbacks_firing++;
    PyObject *phase_obj = py_str_new(phase, (int64_t)strlen(phase));
    PyObject *info = py_dict_new();
    if (phase_obj == NULL || info == NULL) {
        if (phase_obj != NULL) py_decref(phase_obj);
        if (info != NULL) py_decref(info);
        pcc_gc_callbacks_firing--;
        return;
    }

    for (int64_t i = 0; i < n; i++) {
        PyObject *callback = py_list_get(pcc_gc_callbacks, i);
        if (callback == NULL) continue;
        PyObject *args = py_tuple_new(2);
        if (args != NULL) {
            py_tuple_set_item(args, 0, phase_obj);
            py_tuple_set_item(args, 1, info);
            PyObject *result = py_obj_call(callback, args, py_None);
            if (result != NULL) py_decref(result);
            py_decref(args);
        }
        py_decref(callback);
        py_clear_exception();
    }

    py_decref(info);
    py_decref(phase_obj);
    pcc_gc_callbacks_firing--;
}

static int py_pointer_can_have_header(PyObject *o) {
    return pcc_gc_pointer_is_managed(o) != 0;
}

static int py_gc_relocation_candidate(PyObject *o) {
    if (!py_pointer_can_have_header(o)) return 0;
    return (
        py_header_flags_load(py_header(o))
        & PY_FLAG_GC_RELOCATION_CANDIDATE
    ) != 0;
}

static int py_gc_backend4_should_check_slot(PyObject **slot) {
    if (pcc_gc_forwarding_population_load() > 0) return 1;
    if (pcc_gc_relocation_set_active_load() == 0) return 0;
    return pcc_gc_slot_is_runtime_root(slot) == 0;
}

/* Refcount transitions are split at the graph-lock boundary.  Prepare owns
 * canonicalization and the actual counter update; finish owns only logging
 * and the potentially reentrant deallocation tail.  Keeping every scalar
 * needed by finish here means it never has to resolve or decrement again. */
typedef struct {
    PyObject *obj;
    int32_t type_tag;
    int32_t flags;
    int64_t backend;
    int64_t new_refcount;
    int32_t did_update;
    int32_t underflow_before;
    int32_t debug_bad_tag;
    int32_t debug_bad;
    int32_t debug_check_deferred;
} PccRefcountPrepared;

typedef struct {
    PccRefcountPrepared new_prepared;
    PccRefcountPrepared old_prepared;
    int64_t backend;
    int32_t debug_runtime_enabled;
    int32_t state;
} PccGcStoreRootPlanImpl;

enum {
    PCC_GC_STORE_ROOT_PLAN_ATTEMPTED = 1,
    PCC_GC_STORE_ROOT_PLAN_PUBLISHED = 2,
    PCC_GC_STORE_ROOT_PLAN_FINISHED = 4,
};

_Static_assert(
    sizeof(PccRefcountPrepared) == 56,
    "PccRefcountPrepared strict mirror size drift"
);
_Static_assert(
    sizeof(PccGcRetainPlan) == sizeof(PccRefcountPrepared),
    "PccGcRetainPlan opaque storage size drift"
);
_Static_assert(
    _Alignof(PccGcRetainPlan) >= _Alignof(PccRefcountPrepared),
    "PccGcRetainPlan opaque storage alignment drift"
);
_Static_assert(
    offsetof(PccGcStoreRootPlanImpl, new_prepared) == 0,
    "PccGcStoreRootPlan NEW packet offset drift"
);
_Static_assert(
    offsetof(PccGcStoreRootPlanImpl, old_prepared) == 56,
    "PccGcStoreRootPlan OLD packet offset drift"
);
_Static_assert(
    offsetof(PccGcStoreRootPlanImpl, backend) == 112,
    "PccGcStoreRootPlan backend offset drift"
);
_Static_assert(
    offsetof(PccGcStoreRootPlanImpl, debug_runtime_enabled) == 120,
    "PccGcStoreRootPlan debug offset drift"
);
_Static_assert(
    offsetof(PccGcStoreRootPlanImpl, state) == 124,
    "PccGcStoreRootPlan state offset drift"
);
_Static_assert(
    sizeof(PccGcStoreRootPlanImpl) == sizeof(PccGcStoreRootPlan),
    "PccGcStoreRootPlan opaque storage size drift"
);
_Static_assert(
    _Alignof(PccGcStoreRootPlanImpl) <= _Alignof(PccGcStoreRootPlan),
    "PccGcStoreRootPlan opaque storage alignment drift"
);

static void pcc_incref_prepare(
    PyObject *o,
    int debug_runtime_mode,
    PccRefcountPrepared *prepared
);
static void pcc_incref_finish(const PccRefcountPrepared *prepared);
static void pcc_decref_prepare(
    PyObject *o,
    int debug_runtime_mode,
    PccRefcountPrepared *prepared
);
static void pcc_decref_finish(const PccRefcountPrepared *prepared);

static int py_type_tag_is_valid(int32_t tag) {
    return (
        tag == PY_TYPE_NONE || tag == PY_TYPE_BOOL || tag == PY_TYPE_INT
        || tag == PY_TYPE_FLOAT || tag == PY_TYPE_STR || tag == PY_TYPE_LIST
        || tag == PY_TYPE_DICT || tag == PY_TYPE_TUPLE || tag == PY_TYPE_SET
        || tag == PY_TYPE_FUNC || tag == PY_TYPE_CLASS || tag == PY_TYPE_INSTANCE
        || tag == PY_TYPE_EXC || tag == PY_TYPE_FILE || tag == PY_TYPE_ITER
        || tag == PY_TYPE_GEN || tag == PY_TYPE_COMPLEX || tag == PY_TYPE_BYTES
        || tag == PY_TYPE_BYTEARRAY || tag == PY_TYPE_MEMORYVIEW
        || tag == PY_TYPE_COROUTINE || tag == PY_TYPE_WEAKREF
        || tag == PY_TYPE_THREAD_LOCK || tag == PY_TYPE_THREAD_RLOCK
        || tag == PY_TYPE_THREAD_EVENT || tag == PY_TYPE_THREAD_CONDITION
        || tag == PY_TYPE_THREAD_SEMAPHORE || tag == PY_TYPE_THREAD
        || tag == PY_TYPE_TASK
        || tag == PY_TYPE_CONTINUATION
        || tag == PY_TYPE_VIRTUAL_THREAD
        || tag == PY_TYPE_VTHREAD_CHANNEL
        || tag == PY_TYPE_CPY_HANDLE
        || tag >= PY_TYPE_USER
    );
}

PyObject *py_bool_from_bit(int b) {
    return b ? py_True : py_False;
}

static int pcc_alloc_graph_leaf_tag(int32_t tag) {
    switch (tag) {
        case PY_TYPE_NONE:
        case PY_TYPE_BOOL:
        case PY_TYPE_INT:
        case PY_TYPE_FLOAT:
        case PY_TYPE_STR:
        case PY_TYPE_COMPLEX:
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_CPY_HANDLE:
            return 1;
        default:
            return 0;
    }
}

PyObject *pcc_gc_alloc(int64_t size, int32_t type_tag, int32_t flags) {
    if (size < (int64_t)sizeof(PyObjectHeader)) return NULL;
    pcc_thread_safepoint();
    pcc_gc_note_alloc(size);
    pcc_obj_runtime_log_event_code(1, 1, size, type_tag, NULL);
    int32_t stored_flags = flags;
    int64_t backend = pcc_gc_backend();
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && (
            type_tag == PY_TYPE_LIST
            || type_tag == PY_TYPE_TUPLE
            || type_tag == PY_TYPE_DICT
            || type_tag == PY_TYPE_SET
            || type_tag == PY_TYPE_PROPERTY
            || type_tag == PY_TYPE_CLASSMETHOD
            || type_tag == PY_TYPE_WEAKREF
            /* Every remaining tag pcc_gc_colored_relocate_copy_supported_tag
             * accepts (GC-P1-BACKEND4-RELOCATABLE-TAGS-LACK-FRESH-ALLOC):
             * without FRESH_ALLOC a mid-construction object can enter the
             * relocation set.  Each tag's constructor publishes on its
             * success path, mirroring the seven originals. */
            || type_tag == PY_TYPE_FUNC
            || type_tag == PY_TYPE_ITER
            || type_tag == PY_TYPE_GEN
            || type_tag == PY_TYPE_COROUTINE
            || type_tag == PY_TYPE_CONTINUATION
            || type_tag == PY_TYPE_TASK
            || type_tag == PY_TYPE_EXC
            || type_tag == PY_TYPE_CLASS
            || type_tag == PY_TYPE_STATICMETHOD
            || type_tag == PY_TYPE_MEMORYVIEW
        )
    ) stored_flags |= PY_FLAG_GC_FRESH_ALLOC;
    PyObjectHeader *h = NULL;
    if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        h = (PyObjectHeader *)pcc_gc_try_minor_alloc(size);
    } else if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
        if (!pcc_alloc_graph_leaf_tag(type_tag)) {
            h = (PyObjectHeader *)pcc_gc_backend4_try_zpage_alloc(size, flags);
            if (h != NULL) stored_flags |= PY_FLAG_GC_ZPAGE_ALLOC;
        } else {
            stored_flags &= ~PY_FLAG_GC_ZPAGE_ALLOC;
        }
    }
    if (h == NULL) {
        h = (PyObjectHeader *)calloc(1, (size_t)size);
        if (h != NULL) {
            if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
                stored_flags =
                    (stored_flags & ~PY_FLAG_GC_ZPAGE_ALLOC)
                    | PY_FLAG_GC_MALLOC_ALLOC;
            } else if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
                stored_flags =
                    (stored_flags & ~PY_FLAG_GC_MINOR_ARENA)
                    | PY_FLAG_GC_MALLOC_ALLOC;
            }
        }
    }
    if (h == NULL) return NULL;
    h->refcount = 1;
    h->type_tag = type_tag;
    h->flags = stored_flags;
    pcc_debug_note_alloc_size(h, size);
    /* Publish exact provenance before any GC path can observe the object.
     * Tracking may subsequently transfer ownership to the object index; graph
     * leaves and backend-0 objects deliberately remain in this exact set. */
    if (pcc_gc_pointer_register((PyObject *)h) < 0) {
        return NULL;
    }
    pcc_gc_note_object_allocated_sized((PyObject *)h, size);
    pcc_obj_runtime_log_event_code(1, 2, size, type_tag, h);
    return (PyObject *)h;
}

void pcc_gc_publish_initialized(PyObject *obj) {
    if (
        obj == NULL
        || PY_IS_TAGGED_INT(obj)
        || pcc_gc_backend() != PCC_GC_KIND_COLORED_RELOCATING
    ) return;
    pcc_gc_root_slot_lock();
    py_header_flags_and(py_header(obj), ~PY_FLAG_GC_FRESH_ALLOC);
    pcc_gc_root_slot_unlock();
}

PyObject *pcc_gc_retain(PyObject *o) {
    py_incref(o);
    return o;
}

void pcc_gc_release(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    int64_t backend = pcc_gc_backend();
    if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        if (py_gc_relocation_candidate(o)) {
            PyObject *resolved = pcc_gc_note_relocation_read(o);
            if (resolved != o) {
                py_decref(o);
                return;
            }
        }
    } else if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
        if (
            pcc_gc_forwarding_population_load() > 0
            && py_gc_relocation_candidate(o)
        ) {
            PyObject *resolved = pcc_gc_note_relocation_read(o);
            if (resolved == o && pcc_gc_object_is_known_no_lock(o) == 0) {
                return;
            }
            o = resolved;
        }
    }
    if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        PyObjectHeader *h = py_header(o);
        int32_t flags = py_header_flags_load(h);
        if (
            (flags & PY_FLAG_GC_MINOR_ARENA) != 0
            && (flags & PY_FLAG_GC_OLD) != 0
            && pcc_refcount_load(&h->refcount) <= 0
        ) {
            return;
        }
    }
    py_decref(o);
}

PyObject *pcc_gc_load_ptr(PyObject *owner, PyObject **slot) {
    (void)owner;
    if (slot == NULL) return NULL;
    PyObject *value = *slot;
    if (
        __atomic_load_n(&pcc_gc_read_barrier_enabled, __ATOMIC_ACQUIRE) == 0
    ) {
        return value;
    }
    int64_t backend = pcc_gc_backend();
    if (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        if (
            backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            && pcc_gc_forwarding_population_load() <= 0
        ) {
            return value;
        }
        if (
            backend == PCC_GC_KIND_COLORED_RELOCATING
            && !py_gc_backend4_should_check_slot(slot)
        ) {
            return value;
        }
        pcc_gc_note_load();
        int needs_resolve = 0;
        if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
            /* G-P0-LONGRUN: decide via pointer-value lookups, never a raw
             * header deref of a possibly-stale/unmapped slot value. */
            needs_resolve = (int)pcc_gc_backend4_slot_needs_resolve(value);
        } else if (py_gc_relocation_candidate(value)) {
            needs_resolve = 1;
        }
        if (needs_resolve) {
            PyObject *resolved = pcc_gc_note_relocation_read(value);
            if (resolved != value) {
                *slot = resolved;
                value = resolved;
            }
        }
    }
    return value;
}

PyObject *pcc_gc_load_borrowed_ptr(PyObject *owner, PyObject **slot) {
    (void)owner;
    if (slot == NULL) return NULL;
    PyObject *value = *slot;
    if (
        __atomic_load_n(&pcc_gc_read_barrier_enabled, __ATOMIC_ACQUIRE) == 0
    ) {
        return value;
    }
    int64_t backend = pcc_gc_backend();
    if (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        if (
            backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            && pcc_gc_forwarding_population_load() <= 0
        ) {
            return value;
        }
        if (
            backend == PCC_GC_KIND_COLORED_RELOCATING
            && !py_gc_backend4_should_check_slot(slot)
        ) {
            return value;
        }
        pcc_gc_note_load();
        int needs_resolve = 0;
        if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
            /* G-P0-LONGRUN: decide via pointer-value lookups, never a raw
             * header deref of a possibly-stale/unmapped slot value. */
            needs_resolve = (int)pcc_gc_backend4_slot_needs_resolve(value);
        } else if (py_gc_relocation_candidate(value)) {
            needs_resolve = 1;
        }
        if (needs_resolve) {
            PyObject *resolved = pcc_gc_note_relocation_read(value);
            if (resolved != value) {
                *slot = resolved;
                value = resolved;
            }
        }
    }
    return value;
}

PyObject *pcc_gc_resolve_owned_ptr(PyObject *value) {
    if (value == NULL || PY_IS_TAGGED_INT(value)) return value;
    int64_t backend = pcc_gc_backend();
    if (
        backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && backend != PCC_GC_KIND_COLORED_RELOCATING
    ) {
        return value;
    }
    if (pcc_gc_forwarding_population_load() <= 0) {
        return value;
    }
    if (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && !py_gc_relocation_candidate(value)
    ) {
        return value;
    }
    PyObject *resolved = pcc_gc_note_relocation_read(value);
    if (resolved != value) {
        return resolved;
    }
    return value;
}

static void pcc_gc_incref_fresh_native_instance(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    PyObjectHeader *h = py_header(o);
    if (
        h->type_tag != PY_TYPE_INSTANCE
        && (
            h->type_tag < PY_TYPE_USER_CLASS_START
            || h->type_tag > 500
        )
    ) {
        /* Fail safely if a future compiler caller widens the trusted lane
         * without supplying the corresponding provenance proof. */
        py_incref(o);
        return;
    }
    int64_t backend = pcc_gc_backend();
    int32_t flags = py_header_flags_load(h);
    if (
        (
            backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
            || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        )
        && flags == 0
    ) {
        return;
    }
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_forwarding_population_load() > 0
        && (flags & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
    ) {
        PyObject *resolved = pcc_gc_note_relocation_read(o);
        if (resolved != NULL && resolved != o) {
            o = resolved;
            h = py_header(o);
            flags = py_header_flags_load(h);
        }
    }
    if (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && (flags & PY_FLAG_GC_MINOR_ARENA) != 0
        && (flags & PY_FLAG_GC_OLD) != 0
        && pcc_refcount_load(&h->refcount) <= 0
    ) {
        return;
    }
    if (flags & PY_FLAG_IMMORTAL) return;
    int64_t new_refcount = pcc_refcount_incref(&h->refcount);
    pcc_obj_runtime_log_event_code(3, 1, new_refcount, h->type_tag, o);
}

int64_t pcc_gc_store_ptr_plan_commit_locked(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    PyObject **slot,
    PyObject *value
);
void pcc_gc_store_ptr_plan_finish(PccGcStoreRootPlan *plan);

void pcc_gc_store_ptr(PyObject *owner, PyObject **slot, PyObject *value) {
    if (slot == NULL) return;
    int64_t backend = pcc_gc_backend();
    if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_obj_runtime_log_event_code(2, 3, backend, 0, owner);
        PyObject *old = *slot;
        if (old == value) return;
        py_incref(value);
        *slot = value;
        py_decref(old);
        return;
    }
    PccGcStoreRootPlan plan;
    pcc_gc_store_ptr_plan_init(&plan, owner, backend);
    pcc_gc_root_slot_lock();
    (void)pcc_gc_store_ptr_plan_commit_locked(
        &plan, owner, slot, value
    );
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&plan);
}

void pcc_gc_store_ptr_fresh_native_instance(
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    (void)owner;
    if (slot == NULL) return;
    int64_t backend = pcc_gc_backend();
    if (
        backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        pcc_gc_note_store();
    }
    if (backend == PCC_GC_KIND_COLORED_RELOCATING) {
        if (
            pcc_gc_forwarding_population_load() > 0
            && py_gc_relocation_candidate(value)
        ) {
            value = pcc_gc_note_relocation_read(value);
        }
    } else if (backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) {
        if (
            pcc_gc_forwarding_population_load() > 0
            && py_gc_relocation_candidate(value)
        ) {
            value = pcc_gc_note_relocation_read(value);
        }
    }
    if (
        backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        pcc_gc_note_slot_write_barrier(owner, slot, value);
    }
    pcc_obj_runtime_log_event_code(2, 3, backend, 0, owner);
    PyObject *old = *slot;
    pcc_gc_incref_fresh_native_instance(value);
    *slot = value;
    py_decref(old);
}

static PccGcStoreRootPlanImpl *pcc_gc_store_root_plan_impl(
    PccGcStoreRootPlan *plan
) {
    return (PccGcStoreRootPlanImpl *)(void *)plan;
}

void pcc_gc_store_root_plan_init(
    PccGcStoreRootPlan *plan,
    int64_t backend
) {
    if (plan == NULL) return;
    memset(plan, 0, sizeof(*plan));
    PccGcStoreRootPlanImpl *impl = pcc_gc_store_root_plan_impl(plan);
    impl->backend = backend;
    /* Warm getenv/cache before an enclosing graph lock.  Commit passes only
     * this captured predicate to refcount prepare, so diagnostics cannot run
     * from the locked transaction. */
    impl->debug_runtime_enabled = pcc_obj_debug_runtime_enabled();
}

void pcc_gc_store_ptr_plan_init(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    int64_t backend
) {
    pcc_gc_store_root_plan_init(plan, backend);
    pcc_obj_runtime_log_event_code(2, 3, backend, 0, owner);
}

static int64_t pcc_gc_store_plan_commit_locked_impl(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    if (plan == NULL || slot == NULL) return 0;
    PccGcStoreRootPlanImpl *impl = pcc_gc_store_root_plan_impl(plan);
    if (impl->state != 0) return 0;
    impl->state = PCC_GC_STORE_ROOT_PLAN_ATTEMPTED;
    int64_t backend = impl->backend;
    if (
        backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        pcc_gc_note_store();
    }
    PyObject *canonical_value = value;
    if (
        (
            backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            || backend == PCC_GC_KIND_COLORED_RELOCATING
        )
        && pcc_gc_forwarding_population_load() > 0
        && py_gc_relocation_candidate(canonical_value)
    ) {
        canonical_value = pcc_gc_note_relocation_read(canonical_value);
    }
    pcc_incref_prepare(
        canonical_value,
        impl->debug_runtime_enabled,
        &impl->new_prepared
    );
    if (impl->new_prepared.debug_bad) {
        /* Preserve the public helper's debug-invalid contract: telemetry and
         * the deferred diagnostic run, but no slot publication or old-value
         * release occurs. */
        return 0;
    }
    if (
        backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        pcc_gc_note_slot_write_barrier(
            owner, slot, impl->new_prepared.obj
        );
    }
    PyObject *old = *slot;
    *slot = impl->new_prepared.obj;
    if (
        old != NULL
        && !PY_IS_TAGGED_INT(old)
        && backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_object_is_known_no_lock(old) == 0
    ) {
        old = NULL;
    }
    pcc_decref_prepare(
        old,
        impl->debug_runtime_enabled,
        &impl->old_prepared
    );
    impl->state |= PCC_GC_STORE_ROOT_PLAN_PUBLISHED;
    return 1;
}

int64_t pcc_gc_store_root_plan_commit_locked(
    PccGcStoreRootPlan *plan,
    PyObject **slot,
    PyObject *value
) {
    return pcc_gc_store_plan_commit_locked_impl(
        plan, NULL, slot, value
    );
}

int64_t pcc_gc_store_ptr_plan_commit_locked(
    PccGcStoreRootPlan *plan,
    PyObject *owner,
    PyObject **slot,
    PyObject *value
) {
    return pcc_gc_store_plan_commit_locked_impl(
        plan, owner, slot, value
    );
}

static void pcc_gc_store_plan_finish_impl(
    PccGcStoreRootPlan *plan,
    int emit_store_log
) {
    if (plan == NULL) return;
    PccGcStoreRootPlanImpl *impl = pcc_gc_store_root_plan_impl(plan);
    if (
        (impl->state & PCC_GC_STORE_ROOT_PLAN_ATTEMPTED) == 0
        || (impl->state & PCC_GC_STORE_ROOT_PLAN_FINISHED) != 0
    ) {
        return;
    }
    impl->state |= PCC_GC_STORE_ROOT_PLAN_FINISHED;
    /* Preserve the historical per-call telemetry sequence while keeping all
     * logger I/O, finalizers, weakref callbacks and frees outside the lock. */
    if (emit_store_log) {
        pcc_obj_runtime_log_event_code(2, 3, impl->backend, 0, NULL);
    }
    pcc_incref_finish(&impl->new_prepared);
    if ((impl->state & PCC_GC_STORE_ROOT_PLAN_PUBLISHED) == 0) return;
    pcc_decref_finish(&impl->old_prepared);
}

void pcc_gc_store_root_plan_finish(PccGcStoreRootPlan *plan) {
    pcc_gc_store_plan_finish_impl(plan, 1);
}

void pcc_gc_store_ptr_plan_finish(PccGcStoreRootPlan *plan) {
    pcc_gc_store_plan_finish_impl(plan, 0);
}

void pcc_gc_store_root(PyObject **slot, PyObject *value) {
    if (slot == NULL) return;
    int64_t backend = pcc_gc_backend();
    if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_obj_runtime_log_event_code(2, 3, backend, 0, NULL);
        PyObject *old = *slot;
        if (old == value) return;
        /* Skip refcount calls for values that cannot be refcounted: a tagged
         * immediate and NULL both make py_incref/py_decref return at once, so
         * the calls are pure overhead.  Codegen emits ~47000 store_root sites
         * and pcc_gc_store_root measured 17.5% of a list-append loop against
         * 5.6% for the append itself.  The slot store is unconditional; only
         * the no-op refcount calls are elided.  Mirrors py_obj.py. */
        if (!PY_IS_TAGGED_INT(value) && value != NULL) py_incref(value);
        *slot = value;
        if (!PY_IS_TAGGED_INT(old) && old != NULL) py_decref(old);
        return;
    }
    PccGcStoreRootPlan plan;
    pcc_gc_store_root_plan_init(&plan, backend);
    pcc_gc_root_slot_lock();
    (void)pcc_gc_store_root_plan_commit_locked(&plan, slot, value);
    pcc_gc_root_slot_unlock();
    pcc_gc_store_root_plan_finish(&plan);
}

void pcc_gc_store_root_take(PyObject **slot, PyObject *value) {
    /* Ownership-transferring root store; mirror of py_obj.py. */
    if (slot == NULL) return;
    int64_t backend = pcc_gc_backend();
    if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_obj_runtime_log_event_code(2, 3, backend, 0, NULL);
        PyObject *old = *slot;
        *slot = value;
        if (!PY_IS_TAGGED_INT(old) && old != NULL) py_decref(old);
        return;
    }
    pcc_gc_store_root(slot, value);
    PyObject *stored = pcc_gc_load_ptr(NULL, slot);
    if (!PY_IS_TAGGED_INT(stored) && stored != NULL) py_decref(stored);
}

int64_t pcc_gc_try_store_ptr_take(PyObject *owner, PyObject **slot, PyObject *value) {
    if (slot == NULL || pcc_gc_backend() != PCC_GC_KIND_REFCOUNT_CYCLE) return 0;
    pcc_obj_runtime_log_event_code(2, 3, PCC_GC_KIND_REFCOUNT_CYCLE, 0, owner);
    PyObject *old = *slot;
    *slot = value;
    /* Self-stores also consume the caller's additional reference. */
    if (!PY_IS_TAGGED_INT(old) && old != NULL) py_decref(old);
    return 1;
}

void pcc_gc_frame_enter(const void *frame_map, PyObject **slots) {
    pcc_gc_note_frame_enter(frame_map, slots);
}

void pcc_gc_frame_leave(PyObject **slots) {
    pcc_gc_note_frame_leave(slots);
}

void pcc_gc_frame_enter_lifo(const void *frame_map, PyObject **slots) {
    pcc_gc_note_frame_enter_lifo(frame_map, slots);
}

void pcc_gc_frame_leave_lifo(PyObject **slots) {
    pcc_gc_note_frame_leave_lifo(slots);
}

void pcc_gc_safepoint(void) {
    pcc_gc_note_safepoint();
    pcc_thread_safepoint();
    (void)pcc_gc_external_resource_poll();
}

int64_t pcc_gc_collect(int32_t reason) {
    (void)reason;
    int64_t backend = pcc_gc_backend();
    /* Reentrancy guard (mirror of the py_obj.py port): a gc.collect() invoked
     * from a finalizer running DURING an in-progress tracing collect must be a
     * no-op, else the reentrant mark/sweep corrupts the outer sweep's in-flight
     * state -> use-after-free segfault on #1/#2/#3/#4. #0 never sets the flag.
     * See gc-5backend-reentrant-collect-during-finalizer-no-libpython.md. */
    if (backend != PCC_GC_KIND_REFCOUNT_CYCLE
        && pcc_gc_explicit_collect_is_active()) {
        return 0;
    }
    pcc_obj_runtime_log_event_code(2, 1, reason, backend, NULL);
    pcc_gc_fire_callbacks("start");
    int64_t collected = 0;
    if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        /* G-P3: backend 0's explicit cycle collect is its only pause-like
         * window — time it so the pause telemetry covers all five
         * backends. */
        int64_t pause_t0 = pcc_runtime_monotonic_us();
        collected = py_gc_collect();
        pcc_gc_record_explicit_pause(
            pause_t0, pcc_runtime_monotonic_us());
    } else {
        int64_t stw = pcc_stop_the_world();
        while (stw != 0) {
            pcc_thread_safepoint();
            stw = pcc_stop_the_world();
        }
        pcc_gc_begin_explicit_tracing_collect();
        /* Sweep only when a mark cycle has actually finished.  The gate is
         * pcc_gc_sweep_owed(), not pcc_gc_has_tracing_sweep(): the latter
         * ignores mark_active, so candidates left over from a previous
         * unfinished sweep read true mid-mark and sweeping there frees live
         * objects (measured).
         *
         * The round bound is a LIVENESS backstop only, and it is not load
         * bearing for correctness: a step legitimately reports zero progress at
         * a phase boundary (measured 1, 0, 6 on backend 1), so breaking on the
         * first zero returns with work outstanding, while looping unbounded
         * would spin if the collector never converges.  Sweeping stays gated
         * either way. */
        int64_t idle_rounds = 0;
        for (;;) {
            if (pcc_gc_sweep_owed() != 0) {
                int64_t swept = pcc_gc_collect_tracing();
                collected += swept;
                if (swept == 0 && pcc_gc_sweep_owed() != 0) break;
                idle_rounds = 0;
                continue;
            }
            if (pcc_gc_step(1024) > 0) {
                idle_rounds = 0;
                continue;
            }
            idle_rounds++;
            if (idle_rounds >= 4) break;
        }
        pcc_gc_end_explicit_tracing_collect();
        (void)pcc_resume_world();
    }
    /* Driver release callbacks run only after the tracing world is resumed.
     * The registry's zero-ready fast path keeps ordinary collections cheap. */
    (void)pcc_gc_external_resource_poll();
    pcc_gc_fire_callbacks("stop");
    pcc_obj_runtime_log_event_code(2, 2, collected, backend, NULL);
    return collected;
}

void pcc_gc_pin(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    py_header_flags_or(py_header(o), PY_FLAG_GC_PINNED);
    pcc_gc_note_pin(1);
}

void pcc_gc_unpin(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    py_header_flags_and(py_header(o), ~PY_FLAG_GC_PINNED);
    pcc_gc_note_pin(-1);
}

/* Immortalize a process-lifetime singleton: py_incref/py_decref early-return
 * on PY_FLAG_IMMORTAL, so shared objects stop generating cross-thread
 * refcount cache-line traffic. Also pins the object so moving backends
 * (#3/#4) never relocate it. The object is never deallocated afterwards;
 * the caller owns the decision that its lifetime is the process. */
void pcc_gc_immortalize(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    pcc_gc_pin(o);
    py_header_flags_or(py_header(o), PY_FLAG_IMMORTAL);
}

extern void pcc_debug_bad_incref(void *o, int32_t tag);

static void pcc_refcount_prepared_reset(
    PccRefcountPrepared *prepared,
    PyObject *o
) {
    if (prepared == NULL) return;
    prepared->obj = o;
    prepared->type_tag = -1;
    prepared->flags = 0;
    prepared->backend = -1;
    prepared->new_refcount = 0;
    prepared->did_update = 0;
    prepared->underflow_before = 0;
    prepared->debug_bad_tag = -1;
    prepared->debug_bad = 0;
    prepared->debug_check_deferred = 0;
}

static void pcc_refcount_prepare_debug_bad(
    PccRefcountPrepared *prepared,
    int32_t bad_tag,
    int debug_runtime_mode
) {
    prepared->debug_bad_tag = bad_tag;
    if (debug_runtime_mode > 0) {
        prepared->debug_bad = 1;
    } else if (debug_runtime_mode < 0) {
        /* Public refcount calls can run inside an existing graph-lock owner.
         * Preserve the historical lazy getenv: valid values never query the
         * debug predicate, while invalid values defer that query to finish. */
        prepared->debug_check_deferred = 1;
    }
}

static void pcc_incref_prepare(
    PyObject *o,
    int debug_runtime_mode,
    PccRefcountPrepared *prepared
) {
    pcc_refcount_prepared_reset(prepared, o);
    if (prepared == NULL) return;
    if (o == NULL) return;
    if (PY_IS_TAGGED_INT(o)) return;  /* tagged ints carry no refcount */
    int64_t backend = pcc_gc_backend();
    if (!py_pointer_can_have_header(o)) {
        pcc_refcount_prepare_debug_bad(prepared, -2, debug_runtime_mode);
        return;
    }
    PyObjectHeader *h = py_header(o);
    if (
        (!py_type_tag_is_valid(h->type_tag) || h->type_tag > 500)
        && pcc_capi_is_cext_type_tag((int64_t)h->type_tag) == 0
    ) {
        pcc_refcount_prepare_debug_bad(
            prepared, h->type_tag, debug_runtime_mode
        );
        return;
    }
    int32_t flags = py_header_flags_load(h);
    if (
        (
            backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
            || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        )
        && flags == 0
    ) {
        return;
    }
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_forwarding_population_load() > 0
        && (flags & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
    ) {
        /* Count-on-NEW model (gc4 remap design): see py_decref. */
        PyObject *resolved = pcc_gc_note_relocation_read(o);
        if (resolved != NULL && resolved != o) {
            o = resolved;
            h = py_header(o);
            flags = py_header_flags_load(h);
        }
    }
    prepared->obj = o;
    prepared->type_tag = h->type_tag;
    prepared->flags = flags;
    prepared->backend = backend;
    if (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && (flags & PY_FLAG_GC_MINOR_ARENA) != 0
        && (flags & PY_FLAG_GC_OLD) != 0
        && pcc_refcount_load(&h->refcount) <= 0
    ) {
        return;
    }
    if (flags & PY_FLAG_IMMORTAL) return;
    prepared->new_refcount = pcc_refcount_incref(&h->refcount);
    prepared->did_update = 1;
}

static void pcc_incref_finish(const PccRefcountPrepared *prepared) {
    if (prepared == NULL) return;
    if (prepared->debug_check_deferred) {
        if (!pcc_obj_debug_runtime_enabled()) return;
        pcc_debug_bad_incref(prepared->obj, prepared->debug_bad_tag);
        return;
    }
    if (prepared->debug_bad) {
        pcc_debug_bad_incref(prepared->obj, prepared->debug_bad_tag);
        return;
    }
    if (!prepared->did_update) return;
    pcc_obj_runtime_log_event_code(
        3,
        1,
        prepared->new_refcount,
        prepared->type_tag,
        prepared->obj
    );
}

PyObject *pcc_gc_retain_plan_prepare_locked(
    PccGcRetainPlan *plan,
    PyObject *value
) {
    if (plan == NULL) return NULL;
    PccRefcountPrepared *prepared = (PccRefcountPrepared *)(void *)plan;
    pcc_incref_prepare(value, -1, prepared);
    return prepared->obj;
}

void pcc_gc_retain_plan_finish(PccGcRetainPlan *plan) {
    if (plan == NULL) return;
    PccRefcountPrepared *prepared = (PccRefcountPrepared *)(void *)plan;
    pcc_incref_finish(prepared);
    pcc_refcount_prepared_reset(prepared, NULL);
}

void py_incref(PyObject *o) {
    PccRefcountPrepared prepared;
    pcc_incref_prepare(o, -1, &prepared);
    pcc_incref_finish(&prepared);
}

typedef struct PccTrashNode {
    PyObject *obj;
    int32_t type_tag;
    struct PccTrashNode *next;
} PccTrashNode;

static _Thread_local int pcc_trash_dealloc_depth = 0;
static _Thread_local PccTrashNode *pcc_trash_head = NULL;
static _Thread_local PccTrashNode *pcc_trash_tail = NULL;

static int pcc_trash_should_defer(int32_t type_tag) {
    if (pcc_capi_is_cext_type_tag((int64_t)type_tag) != 0) return 0;
    /* These pointer-bearing container/instance types must DEFER dealloc when
     * already inside a dealloc (depth > 0) so a deep ownership cascade unwinds
     * iteratively via the trash queue instead of recursing. This mirrors the
     * pcc-Python port's _dealloc_should_defer exactly; previously every case
     * fell through to `default` (no per-case return), so these tags wrongly
     * returned the generic user-tag fallback (false) and were dealloc'd
     * recursively in cc mode — a divergence from the deferring port that
     * double-freed a trash node when a list/dict cascade re-entered the
     * drain. */
    switch (type_tag) {
        case PY_TYPE_LIST:
        case PY_TYPE_TUPLE:
        case PY_TYPE_DICT:
        case PY_TYPE_SET:
        case PY_TYPE_INSTANCE:
        case PY_TYPE_EXC:
        case PY_TYPE_ITER:
        case PY_TYPE_GEN:
        case PY_TYPE_COROUTINE:
        case PY_TYPE_CONTINUATION:
        case PY_TYPE_TASK:
        case PY_TYPE_VIRTUAL_THREAD:
        case PY_TYPE_VTHREAD_CHANNEL:
        case PY_TYPE_PROPERTY:
        case PY_TYPE_CLASSMETHOD:
        case PY_TYPE_STATICMETHOD:
            return 1;
        default:
            return type_tag >= PY_TYPE_USER_CLASS_START;
    }
}

static void pcc_dealloc_dispatch(PyObject *o, int32_t type_tag) {
    switch (type_tag) {
        case PY_TYPE_INT:      py_dealloc_int(o);      break;
        case PY_TYPE_FLOAT:    py_dealloc_float(o);    break;
        case PY_TYPE_STR:      py_dealloc_str(o);      break;
        case PY_TYPE_LIST:     py_dealloc_list(o);     break;
        case PY_TYPE_TUPLE:    py_dealloc_tuple(o);    break;
        case PY_TYPE_DICT:     py_dealloc_dict(o);     break;
        case PY_TYPE_SET:      py_dealloc_set(o);      break;
        case PY_TYPE_FUNC:     py_dealloc_func(o);     break;
        case PY_TYPE_CLASS:    py_class_dealloc(o);    break;
        case PY_TYPE_INSTANCE: py_instance_dealloc(o); break;
        case PY_TYPE_EXC:      py_dealloc_exc(o);      break;
        case PY_TYPE_ITER:     py_dealloc_iter(o);     break;
        case PY_TYPE_GEN:      py_dealloc_gen(o);      break;
        case PY_TYPE_COROUTINE: py_dealloc_coroutine(o); break;
        case PY_TYPE_CONTINUATION: py_dealloc_continuation(o); break;
        case PY_TYPE_MEMORYVIEW: py_dealloc_memoryview(o); break;
        case PY_TYPE_WEAKREF:   py_dealloc_weakref(o); break;
        case PY_TYPE_THREAD_LOCK: py_dealloc_thread_lock(o); break;
        case PY_TYPE_THREAD_RLOCK: py_dealloc_thread_rlock(o); break;
        case PY_TYPE_THREAD_EVENT: py_dealloc_thread_event(o); break;
        case PY_TYPE_THREAD_CONDITION: py_dealloc_thread_condition(o); break;
        case PY_TYPE_THREAD_SEMAPHORE: py_dealloc_thread_semaphore(o); break;
        case PY_TYPE_THREAD: py_dealloc_thread_thread(o); break;
        case PY_TYPE_TASK: py_dealloc_task(o); break;
        case PY_TYPE_VIRTUAL_THREAD: py_dealloc_virtual_thread(o); break;
        case PY_TYPE_VTHREAD_CHANNEL: py_dealloc_vthread_channel(o); break;
        case PY_TYPE_CPY_HANDLE: py_dealloc_cpy_handle(o); break;
        case PY_TYPE_PROPERTY:
        case PY_TYPE_CLASSMETHOD:
        case PY_TYPE_STATICMETHOD: py_descriptor_dealloc(o); break;
        default:
            if (pcc_capi_dealloc_cext_object(o, (int64_t)type_tag) != 0) {
                break;
            }
            if (type_tag >= PY_TYPE_USER_CLASS_START) {
                py_instance_dealloc(o);
            } else {
                py_dealloc_generic(o);
            }
            break;
    }
}

static int pcc_trash_enqueue(PyObject *o, int32_t type_tag) {
    PccTrashNode *node = (PccTrashNode *)malloc(sizeof(PccTrashNode));
    if (node == NULL) return 0;
    node->obj = o;
    node->type_tag = type_tag;
    node->next = NULL;
    if (pcc_trash_tail != NULL) {
        pcc_trash_tail->next = node;
    } else {
        pcc_trash_head = node;
    }
    pcc_trash_tail = node;
    return 1;
}

static void pcc_trash_drain(void) {
    while (pcc_trash_head != NULL) {
        PccTrashNode *node = pcc_trash_head;
        pcc_trash_head = node->next;
        if (pcc_trash_head == NULL) pcc_trash_tail = NULL;
        PyObject *obj = node->obj;
        int32_t type_tag = node->type_tag;
        free(node);
        pcc_dealloc_dispatch(obj, type_tag);
    }
}

static void pcc_decref_prepare(
    PyObject *o,
    int debug_runtime_mode,
    PccRefcountPrepared *prepared
) {
    pcc_refcount_prepared_reset(prepared, o);
    if (prepared == NULL) return;
    if (o == NULL) return;
    if (PY_IS_TAGGED_INT(o)) return;
    int64_t backend = pcc_gc_backend();
    if (!py_pointer_can_have_header(o)) {
        pcc_refcount_prepare_debug_bad(prepared, -2, debug_runtime_mode);
        return;
    }
    PyObjectHeader *h = py_header(o);
    if (
        (!py_type_tag_is_valid(h->type_tag) || h->type_tag > 500)
        && pcc_capi_is_cext_type_tag((int64_t)h->type_tag) == 0
    ) {
        pcc_refcount_prepare_debug_bad(
            prepared, h->type_tag, debug_runtime_mode
        );
        return;
    }
    int32_t flags = py_header_flags_load(h);
    if (
        (
            backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
            || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        )
        && flags == 0
    ) {
        return;
    }
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && pcc_gc_forwarding_population_load() > 0
        && (flags & PY_FLAG_GC_RELOCATION_CANDIDATE) != 0
    ) {
        /* Count-on-NEW model (gc4 remap design): after relocation the
         * outstanding refcount lives on the NEW copy; every count
         * operation through a stale pointer must resolve first. Old
         * copies are immortal shells, so an unresolvable stray decref
         * (forwarding already retired) is a no-op below. */
        PyObject *resolved = pcc_gc_note_relocation_read(o);
        if (resolved != NULL && resolved != o) {
            o = resolved;
            h = py_header(o);
            flags = py_header_flags_load(h);
        }
    }
    prepared->obj = o;
    prepared->type_tag = h->type_tag;
    prepared->flags = flags;
    prepared->backend = backend;
    if (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && (flags & PY_FLAG_GC_MINOR_ARENA) != 0
        && (flags & PY_FLAG_GC_OLD) != 0
        && pcc_refcount_load(&h->refcount) <= 0
    ) {
        return;
    }
    if (flags & PY_FLAG_IMMORTAL) return;
    if (
        backend == PCC_GC_KIND_COLORED_RELOCATING
        && (flags & (
            PY_FLAG_GC_RELOCATION_CANDIDATE
            | PY_FLAG_GC_RELOCATION_TARGET
            | PY_FLAG_GC_FORWARD_RETIRING
        )) != 0
        && pcc_gc_object_is_known(o) == 0
    ) {
        return;
    }
    if (pcc_refcount_load(&h->refcount) <= 0) {
        prepared->underflow_before = 1;
        return;
    }
    prepared->new_refcount = pcc_refcount_decref(&h->refcount);
    prepared->did_update = 1;
    if (prepared->new_refcount == 0) {
        /* Publish logical death before releasing a graph/root-slot lock.  No
         * selector or copy path may admit this object while finish runs. */
        py_header_flags_or(h, PY_FLAG_GC_DEALLOCATING);
    }
}

static void pcc_decref_finish(const PccRefcountPrepared *prepared) {
    if (prepared == NULL) return;
    if (prepared->debug_check_deferred) {
        if (!pcc_obj_debug_runtime_enabled()) return;
        pcc_debug_bad_incref(prepared->obj, prepared->debug_bad_tag);
        return;
    }
    if (prepared->debug_bad) {
        pcc_debug_bad_incref(prepared->obj, prepared->debug_bad_tag);
        return;
    }
    if (prepared->underflow_before) {
        PCC_RT_TRIPWIRE(
            0,
            "py_decref: refcount underflow (<=0 before decref of a live, non-immortal object)"
        );
        assert(0 && "py_decref: refcount underflow");
        return;
    }
    if (!prepared->did_update) return;
    PyObject *o = prepared->obj;
    int32_t type_tag = prepared->type_tag;
    int32_t flags = prepared->flags;
    int64_t new_refcount = prepared->new_refcount;
    PCC_RT_TRIPWIRE(
        new_refcount >= 0,
        "py_decref: refcount went negative after decref (double free / concurrent over-release)"
    );
    assert(new_refcount >= 0 && "py_decref: refcount underflow");
    if (new_refcount > 0) {
        /* Non-terminal finish is captured-scalar telemetry only. */
        pcc_obj_runtime_log_event_code(3, 2, new_refcount, type_tag, o);
        return;
    }
    int delay_zpage_freeing_note = (
        prepared->backend == PCC_GC_KIND_COLORED_RELOCATING
        && (flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0
    );
    int delay_instance_metadata = (
        type_tag == PY_TYPE_INSTANCE
        || type_tag >= PY_TYPE_USER_CLASS_START
    );
    pcc_obj_runtime_log_event_code(3, 2, new_refcount, type_tag, o);
    pcc_refcount_forget(&py_header(o)->refcount);
    pcc_obj_runtime_log_event_code(3, 3, 0, type_tag, o);

    py_weakref_invalidate(o);
    if (!delay_zpage_freeing_note && !delay_instance_metadata) {
        pcc_gc_note_object_freeing(o);
    }
    if (!delay_instance_metadata) py_gc_untrack(o);
    /* zpage-resident objects must dealloc IMMEDIATELY, but their zpage
     * accounting cannot be decremented before type-specific dealloc runs:
     * the last object on a page may recycle the span, and finalizers still
     * need to read the dying object's header/fields. */
    if (
        pcc_trash_dealloc_depth > 0
        && pcc_trash_should_defer(type_tag)
        && (flags & PY_FLAG_GC_ZPAGE_ALLOC) == 0
        && pcc_trash_enqueue(o, type_tag)
    ) {
        return;
    }

    pcc_trash_dealloc_depth++;
    pcc_dealloc_dispatch(o, type_tag);
    if (pcc_trash_dealloc_depth == 1) {
        pcc_trash_drain();
    }
    pcc_trash_dealloc_depth--;
    if (
        delay_zpage_freeing_note
        && !delay_instance_metadata
        && pcc_gc_pointer_is_managed(o) != 0
    ) {
        pcc_gc_note_object_freeing(o);
    }
}

void py_decref(PyObject *o) {
    PccRefcountPrepared prepared;
    pcc_decref_prepare(o, -1, &prepared);
    pcc_decref_finish(&prepared);
}
