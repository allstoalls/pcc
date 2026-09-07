/* pcc/py_runtime/src/py_list.c
 *
 * Growable array of PyObject* with owned references.
 *
 * Phase 2 scope (complete Section 3 `py_list_*` ABI):
 *   - new, append, get, set, len, concat, contains
 *   - slice, extend, insert, pop, remove, index, count, reverse
 *
 * Growth policy: double on overflow, starting at max(4, initial_capacity).
 * Refcount ownership:
 *   - The list owns one ref per stored item.
 *   - append/insert/set: INCREF the new item; DECREF any displaced item.
 *   - get: returns a new (incref'd) ref.
 *   - pop/remove: slot's existing ref is transferred to the caller (no
 *     re-incref); shifted-over slots keep their existing refs.
 *   - slice: new list owns fresh INCREFs to the sliced items.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>
#include <assert.h>

/* ---- Internals -------------------------------------------------------- */

static int list_prepare_moving_root(PyObject **slot, void **out_handle) {
    if (out_handle == NULL) return -1;
    *out_handle = NULL;
    if (slot == NULL || *slot == NULL || PY_IS_TAGGED_INT(*slot)) return 0;
    int64_t backend = pcc_gc_backend();
    if (
        backend != PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        && backend != PCC_GC_KIND_COLORED_RELOCATING
    ) return 0;
    void *handle = pcc_gc_scheduler_root_register_handle(slot);
    if (handle == NULL) return -1;
    *slot = pcc_gc_load_ptr(NULL, slot);
    if (*slot == NULL) {
        pcc_gc_scheduler_root_unregister_handle(handle);
        return -1;
    }
    *out_handle = handle;
    return 0;
}

static PyObject *list_reload_moving_root(PyObject **slot, void *handle) {
    if (slot == NULL) return NULL;
    if (handle != NULL) *slot = pcc_gc_load_ptr(NULL, slot);
    return *slot;
}

static void list_finish_moving_root(void *handle) {
    if (handle != NULL) pcc_gc_scheduler_root_unregister_handle(handle);
}

/* Reserve capacity for at least `want` items (total, not additional). */
static int grow_if_needed(PyListObject **owner, int64_t want) {
    if (owner == NULL || *owner == NULL) return -1;
    PyListObject *l = *owner;
    if (l->capacity >= want) return 0;
    int64_t initial_backend = pcc_gc_backend();
    if (initial_backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        int64_t cap = l->capacity > 0 ? l->capacity : 4;
        while (cap < want) {
            if (cap > INT64_MAX / 2) return -1;
            cap *= 2;
        }
        PyObject **new_items = (PyObject **)realloc(
            l->items, (size_t)cap * sizeof(PyObject *)
        );
        if (new_items == NULL) return -1;
        l->items = new_items;
        l->capacity = cap;
        return 0;
    }

    PyObject *owner_slot = (PyObject *)l;
    void *owner_handle = NULL;
    if (
        initial_backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || initial_backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        owner_handle = pcc_gc_scheduler_root_register_handle(&owner_slot);
        if (owner_handle == NULL) return -1;
    }

    for (int attempt = 0; attempt < 8; attempt++) {
        pcc_gc_root_slot_lock();
        if (pcc_gc_backend() != initial_backend) {
            pcc_gc_root_slot_unlock();
            break;
        }
        if (owner_handle != NULL) {
            owner_slot = pcc_gc_load_ptr(NULL, &owner_slot);
        }
        l = (PyListObject *)owner_slot;
        PyObject **old_items = l->items;
        int64_t old_capacity = l->capacity;
        int64_t old_length = l->length;
        pcc_gc_root_slot_unlock();
        if (old_capacity >= want) {
            *owner = l;
            if (owner_handle != NULL) {
                pcc_gc_scheduler_root_unregister_handle(owner_handle);
            }
            return 0;
        }
        if (
            old_items == NULL
            || old_capacity <= 0
            || old_length < 0
            || old_length > old_capacity
            || old_length > INT64_MAX / 2
        ) break;
        int64_t cap = old_capacity;
        while (cap < want) {
            if (cap > INT64_MAX / 2) {
                cap = -1;
                break;
            }
            cap *= 2;
        }
        if (cap <= 0) break;

        PyObject **new_items = (PyObject **)calloc(
            (size_t)cap, sizeof(PyObject *)
        );
        PyObject ***slot_pairs = NULL;
        if (old_length > 0) {
            slot_pairs = (PyObject ***)calloc(
                (size_t)old_length * 2u,
                sizeof(PyObject **)
            );
        }
        if (
            new_items == NULL
            || (old_length > 0 && slot_pairs == NULL)
        ) {
            free(slot_pairs);
            free(new_items);
            break;
        }

        pcc_gc_root_slot_lock();
        if (pcc_gc_backend() != initial_backend) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_items);
            break;
        }
        if (owner_handle != NULL) {
            owner_slot = pcc_gc_load_ptr(NULL, &owner_slot);
        }
        l = (PyListObject *)owner_slot;
        if (
            l->items != old_items
            || l->capacity != old_capacity
            || l->length != old_length
        ) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_items);
            continue;
        }
        for (int64_t i = 0; i < old_length; i++) {
            PyObject *item = pcc_gc_load_ptr((PyObject *)l, &old_items[i]);
            new_items[i] = item;
            slot_pairs[i * 2] = &old_items[i];
            slot_pairs[i * 2 + 1] = &new_items[i];
        }
        int64_t retargeted =
            pcc_gc_backend4_retarget_mutator_payload_locked(
                (PyObject *)l,
                old_items,
                old_capacity * (int64_t)sizeof(PyObject *),
                new_items,
                cap * (int64_t)sizeof(PyObject *),
                slot_pairs,
                old_length
            );
        if (retargeted == 0) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_items);
            break;
        }
        for (int64_t i = 0; i < old_length; i++) {
            pcc_gc_note_slot_write_barrier(
                (PyObject *)l, &new_items[i], new_items[i]
            );
        }
        l->items = new_items;
        l->capacity = cap;
        *owner = l;
        if (retargeted == 2) {
            (void)pcc_gc_backend4_zpage_register_owner_payload_span(
                (PyObject *)l,
                new_items,
                cap * (int64_t)sizeof(PyObject *)
            );
        }
        pcc_gc_root_slot_unlock();

        free(old_items);
        free(slot_pairs);
        if (owner_handle != NULL) {
            pcc_gc_scheduler_root_unregister_handle(owner_handle);
        }
        return 0;
    }
    if (owner_handle != NULL) {
        pcc_gc_scheduler_root_unregister_handle(owner_handle);
    }
    return -1;
}

/* Normalize a Python index.
 *   clip == 0 (wrap mode, for __getitem__/__setitem__):
 *     - Negative: add len. If still out-of-range, returns -1 (caller raises
 *       IndexError).
 *   clip == 1 (slice/insert mode):
 *     - Negative: add len, then saturate to 0.
 *     - Too large: saturate to len.
 */
static int64_t normalize_index(int64_t i, int64_t len, int clip) {
    if (clip) {
        if (i < 0) {
            i += len;
            if (i < 0) i = 0;
        }
        if (i > len) i = len;
        return i;
    }
    if (i < 0) i += len;
    if (i < 0 || i >= len) return -1;
    return i;
}

/* Return 1 if o is NULL or py_None (treated as "no argument"). */
static int is_none_or_null(PyObject *o) {
    return o == NULL || o == py_None;
}

static int normalize_slice(PyObject *lo, PyObject *hi, PyObject *step,
                           int64_t len, int64_t *lo_out,
                           int64_t *hi_out, int64_t *step_out) {
    int64_t step_v = 1;
    if (!is_none_or_null(step)) {
        step_v = py_obj_index_i64(step);
        if (py_err_occurred()) return -1;
        if (step_v == 0) return -1;
    }

    int64_t lo_v, hi_v;
    if (step_v > 0) {
        lo_v = is_none_or_null(lo) ? 0   : py_obj_index_i64(lo);
        if (py_err_occurred()) return -1;
        hi_v = is_none_or_null(hi) ? len : py_obj_index_i64(hi);
        if (py_err_occurred()) return -1;
    } else {
        lo_v = is_none_or_null(lo) ? len - 1 : py_obj_index_i64(lo);
        if (py_err_occurred()) return -1;
        hi_v = is_none_or_null(hi) ? -1      : py_obj_index_i64(hi);
        if (py_err_occurred()) return -1;
    }

    if (step_v > 0) {
        if (lo_v < 0) {
            lo_v += len;
            if (lo_v < 0) lo_v = 0;
        }
        if (lo_v > len) lo_v = len;
        if (hi_v < 0) {
            hi_v += len;
            if (hi_v < 0) hi_v = 0;
        }
        if (hi_v > len) hi_v = len;
    } else {
        if (lo_v < 0) {
            lo_v += len;
            if (lo_v < 0) lo_v = -1;
        }
        if (lo_v >= len) lo_v = len - 1;

        if (hi_v < 0) {
            if (is_none_or_null(hi)) {
                hi_v = -1;
            } else {
                hi_v += len;
                if (hi_v < 0) hi_v = -1;
            }
        }
        if (hi_v >= len) hi_v = len - 1;
    }

    *lo_out = lo_v;
    *hi_out = hi_v;
    *step_out = step_v;
    return 0;
}

static void normalize_slice_scalars(
    int lo_none,
    int hi_none,
    int64_t raw_lo,
    int64_t raw_hi,
    int64_t step,
    int64_t len,
    int64_t *lo_out,
    int64_t *hi_out
) {
    int64_t lo = lo_none ? (step > 0 ? 0 : len - 1) : raw_lo;
    int64_t hi = hi_none ? (step > 0 ? len : -1) : raw_hi;
    if (step > 0) {
        if (lo < 0) {
            lo += len;
            if (lo < 0) lo = 0;
        }
        if (lo > len) lo = len;
        if (hi < 0) {
            hi += len;
            if (hi < 0) hi = 0;
        }
        if (hi > len) hi = len;
    } else {
        if (lo < 0) {
            lo += len;
            if (lo < 0) lo = -1;
        }
        if (lo >= len) lo = len - 1;
        if (hi < 0) {
            if (hi_none) {
                hi = -1;
            } else {
                hi += len;
                if (hi < 0) hi = -1;
            }
        }
        if (hi >= len) hi = len - 1;
    }
    *lo_out = lo;
    *hi_out = hi;
}

static int64_t slice_count(int64_t lo, int64_t hi, int64_t step) {
    int64_t n = 0;
    if (step > 0) {
        for (int64_t i = lo; i < hi; i += step) n++;
    } else {
        for (int64_t i = lo; i > hi; i += step) {
            if (i < 0) break;
            n++;
        }
    }
    return n;
}

static int64_t seq_len(PyObject *seq) {
    if (seq == NULL) return -1;
    int32_t tag = py_type_of(seq);
    if (tag == PY_TYPE_LIST) return ((PyListObject *)seq)->length;
    if (tag == PY_TYPE_TUPLE) return ((PyTupleObject *)seq)->len;
    return -1;
}

static PyObject *seq_get_borrowed(PyObject *seq, int64_t i) {
    int32_t tag = py_type_of(seq);
    if (tag == PY_TYPE_LIST) {
        PyListObject *l = (PyListObject *)seq;
        return pcc_gc_load_ptr(seq, &l->items[i]);
    }
    if (tag == PY_TYPE_TUPLE) {
        PyTupleObject *t = (PyTupleObject *)seq;
        return pcc_gc_load_ptr(seq, &t->items[i]);
    }
    return NULL;
}

static inline int64_t list_int_to_i64_or_zero(PyObject *v) {
    if (v == NULL) return 0;
    if (PY_IS_TAGGED_INT(v)) return py_untag_int(v);
    if (py_header(v)->type_tag != PY_TYPE_INT) return 0;
    int overflow = 0;
    int64_t out = py_bigint_to_i64((const PyIntObject *)v, &overflow);
    return overflow ? 0 : out;
}

static void list_delete_index(PyListObject *l, int64_t idx) {
    PyObject *old = pcc_gc_load_ptr((PyObject *)l, &l->items[idx]);
    l->items[idx] = NULL;
    if (old != NULL) py_decref(old);
    if (idx < l->length - 1) {
        memmove(&l->items[idx],
                &l->items[idx + 1],
                (size_t)(l->length - idx - 1) * sizeof(PyObject *));
    }
    l->length--;
}

static int list_delete_range(PyListObject *l, int64_t lo, int64_t hi) {
    if (hi <= lo) return 0;
    for (int64_t i = lo; i < hi; i++) {
        PyObject *old = pcc_gc_load_ptr((PyObject *)l, &l->items[i]);
        l->items[i] = NULL;
        if (old != NULL) py_decref(old);
    }
    if (hi < l->length) {
        memmove(&l->items[lo],
                &l->items[hi],
                (size_t)(l->length - hi) * sizeof(PyObject *));
    }
    l->length -= hi - lo;
    return 0;
}

/* ---- Public API ------------------------------------------------------- */

PyObject *py_list_new(int64_t initial_capacity) {
    PyListObject *l = (PyListObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyListObject), PY_TYPE_LIST, 0
    );
    if (l == NULL) return NULL;
    l->length     = 0;
    l->items      = NULL;
    if (initial_capacity < 4) initial_capacity = 4;
    l->capacity = initial_capacity;
    l->items = (PyObject **)malloc((size_t)initial_capacity * sizeof(PyObject *));
    if (l->items == NULL) {
        py_decref((PyObject *)l);
        return NULL;
    }
    (void)pcc_gc_backend4_zpage_register_owner_payload_span(
        (PyObject *)l,
        l->items,
        initial_capacity * (int64_t)sizeof(PyObject *)
    );
    py_gc_track((PyObject *)l);
    pcc_gc_publish_initialized((PyObject *)l);
    return (PyObject *)l;
}

void py_list_append(PyObject *lst, PyObject *item) {
    if (lst == NULL) return;
    assert(!PY_IS_TAGGED_INT(lst));
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        if (grow_if_needed(&fast, fast->length + 1) != 0) {
            py_raise_owned(py_exc_new(
                PY_EXC_MEMORYERROR, "list append: out of memory"
            ));
            return;
        }
        fast->items[fast->length] = NULL;
        pcc_gc_store_ptr(
            (PyObject *)fast, &fast->items[fast->length], item
        );
        fast->length++;
        return;
    }
    PyObject *list_root = lst;
    PyObject *item_root = item;
    void *list_handle = NULL;
    void *item_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list append: out of memory"));
        return;
    }
    if (list_prepare_moving_root(&item_root, &item_handle) != 0) {
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list append: out of memory"));
        return;
    }
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    assert(l->h.type_tag == PY_TYPE_LIST);
    if (grow_if_needed(&l, l->length + 1) != 0) {
        list_finish_moving_root(item_handle);
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list append: out of memory"));
        return;
    }
    list_root = (PyObject *)l;
    int64_t commit_backend = pcc_gc_backend();
    PccGcStoreRootPlan store_plan;
    pcc_gc_store_ptr_plan_init(
        &store_plan, list_root, commit_backend
    );
    if (commit_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_root_slot_lock();
    }
    lst = list_reload_moving_root(&list_root, list_handle);
    l = (PyListObject *)lst;
    item = list_reload_moving_root(&item_root, item_handle);
    l->items[l->length] = NULL;
    int committed = pcc_gc_store_ptr_plan_commit_locked(
        &store_plan, lst, &l->items[l->length], item
    );
    if (committed) l->length++;
    if (commit_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_root_slot_unlock();
    }
    pcc_gc_store_ptr_plan_finish(&store_plan);
    list_finish_moving_root(item_handle);
    list_finish_moving_root(list_handle);
}

void py_list_append_fresh_native_instance(PyObject *lst, PyObject *item) {
    if (lst == NULL) return;
    assert(!PY_IS_TAGGED_INT(lst));
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        if (grow_if_needed(&fast, fast->length + 1) != 0) {
            py_raise_owned(py_exc_new(
                PY_EXC_MEMORYERROR, "list append: out of memory"
            ));
            return;
        }
        fast->items[fast->length] = NULL;
        pcc_gc_store_ptr_fresh_native_instance(
            (PyObject *)fast, &fast->items[fast->length], item
        );
        fast->length++;
        return;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list append: out of memory"));
        return;
    }
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    assert(l->h.type_tag == PY_TYPE_LIST);
    if (grow_if_needed(&l, l->length + 1) != 0) {
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list append: out of memory"));
        return;
    }
    list_root = (PyObject *)l;
    int64_t commit_backend = pcc_gc_backend();
    if (commit_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_root_slot_lock();
    }
    lst = list_reload_moving_root(&list_root, list_handle);
    l = (PyListObject *)lst;
    l->items[l->length] = NULL;
    pcc_gc_store_ptr_fresh_native_instance(
        lst,
        &l->items[l->length],
        item
    );
    l->length++;
    if (commit_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_root_slot_unlock();
    }
    list_finish_moving_root(list_handle);
}

PyObject *py_list_get(PyObject *lst, int64_t i) {
    if (lst == NULL) return NULL;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t fast_idx = normalize_index(i, fast->length, 0);
        if (fast_idx < 0) return NULL;
        PyObject *value = fast->items[fast_idx];
        py_incref(value);
        return value;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return NULL;
    PccGcRetainPlan retain_plan;
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    int64_t idx = normalize_index(i, l->length, 0);
    if (idx < 0) {
        /* Deliberately non-raising: internal callers pre-check bounds and
         * probe with NULL. The raising subscript path is py_list_getitem. */
        pcc_gc_root_slot_unlock();
        list_finish_moving_root(list_handle);
        return NULL;
    }
    PyObject *v = pcc_gc_load_ptr(lst, &l->items[idx]);
    v = pcc_gc_retain_plan_prepare_locked(&retain_plan, v);
    pcc_gc_root_slot_unlock();
    pcc_gc_retain_plan_finish(&retain_plan);
    list_finish_moving_root(list_handle);
    return v;
}

/* a[i] subscript: like py_list_get but raises IndexError on out-of-range so a
 * surrounding try/except can catch it. py_list_get stays non-raising for other
 * internal callers. Negative indices normalize like CPython. */
PyObject *py_list_getitem(PyObject *lst, int64_t i) {
    if (lst == NULL) return NULL;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t fast_idx = normalize_index(i, fast->length, 0);
        if (fast_idx < 0) {
            py_raise_owned(py_exc_new(
                PY_EXC_INDEXERROR, "list index out of range"
            ));
            return NULL;
        }
        PyObject *value = fast->items[fast_idx];
        py_incref(value);
        return value;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return NULL;
    PccGcRetainPlan retain_plan;
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    int64_t idx = normalize_index(i, l->length, 0);
    if (idx < 0) {
        pcc_gc_root_slot_unlock();
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "list index out of range"));
        return NULL;
    }
    PyObject *v = pcc_gc_load_ptr(lst, &l->items[idx]);
    v = pcc_gc_retain_plan_prepare_locked(&retain_plan, v);
    pcc_gc_root_slot_unlock();
    pcc_gc_retain_plan_finish(&retain_plan);
    list_finish_moving_root(list_handle);
    return v;
}

int64_t py_list_get_i64(PyObject *lst, int64_t i) {
    if (lst == NULL) return 0;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t fast_idx = normalize_index(i, fast->length, 0);
        if (fast_idx < 0) return 0;
        return list_int_to_i64_or_zero(fast->items[fast_idx]);
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return 0;
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    int64_t idx = normalize_index(i, l->length, 0);
    if (idx < 0) {
        pcc_gc_root_slot_unlock();
        list_finish_moving_root(list_handle);
        return 0;
    }
    PyObject *v = pcc_gc_load_ptr(lst, &l->items[idx]);
    int64_t result = list_int_to_i64_or_zero(v);
    pcc_gc_root_slot_unlock();
    list_finish_moving_root(list_handle);
    return result;
}

int64_t py_list_get_i64_nonnegative(PyObject *lst, int64_t i) {
    if (lst == NULL) return 0;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        if (i < 0 || i >= fast->length) return 0;
        return list_int_to_i64_or_zero(fast->items[i]);
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return 0;
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    if (i < 0 || i >= l->length) {
        pcc_gc_root_slot_unlock();
        list_finish_moving_root(list_handle);
        return 0;
    }
    PyObject *v = pcc_gc_load_ptr(lst, &l->items[i]);
    int64_t result = list_int_to_i64_or_zero(v);
    pcc_gc_root_slot_unlock();
    list_finish_moving_root(list_handle);
    return result;
}

static int64_t list_set_item_transaction(
    PyObject *lst,
    int64_t i,
    PyObject *item
) {
    if (lst == NULL) return -1;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t fast_idx = normalize_index(i, fast->length, 0);
        if (fast_idx < 0) return -1;
        pcc_gc_store_ptr(lst, &fast->items[fast_idx], item);
        return 0;
    }
    PyObject *list_root = lst;
    PyObject *item_root = item;
    void *list_handle = NULL;
    void *item_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return -1;
    if (list_prepare_moving_root(&item_root, &item_handle) != 0) {
        list_finish_moving_root(list_handle);
        return -1;
    }
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan store_plan;
    pcc_gc_store_ptr_plan_init(&store_plan, list_root, backend);
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    item = list_reload_moving_root(&item_root, item_handle);
    PyListObject *l = (PyListObject *)lst;
    int64_t idx = normalize_index(i, l->length, 0);
    if (idx < 0) {
        pcc_gc_root_slot_unlock();
        pcc_gc_store_ptr_plan_finish(&store_plan);
        list_finish_moving_root(item_handle);
        list_finish_moving_root(list_handle);
        return -1;
    }
    int64_t committed = pcc_gc_store_ptr_plan_commit_locked(
        &store_plan, lst, &l->items[idx], item
    );
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&store_plan);
    list_finish_moving_root(item_handle);
    list_finish_moving_root(list_handle);
    return committed ? 0 : -1;
}

void py_list_set(PyObject *lst, int64_t i, PyObject *item) {
    /* Internal non-raising setter: callers (sort/insert shifts, generator
     * frames) index within bounds by construction. User-visible subscript
     * stores go through py_list_setitem below. */
    (void)list_set_item_transaction(lst, i, item);
}

void py_list_set_from_owned_root(PyObject *lst, int64_t i, void *source_slot, void *owned_flag) {
    if (source_slot == NULL || owned_flag == NULL) return;
    PyObject **source = (PyObject **)source_slot;
    unsigned char *owned = (unsigned char *)owned_flag;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyObject *item = *source;
        if ((*owned & 1) && lst != NULL && py_type_of(lst) == PY_TYPE_LIST) {
            PyListObject *list = (PyListObject *)lst;
            int64_t index = normalize_index(i, list->length, 0);
            if (index >= 0 && pcc_gc_try_store_ptr_take(lst, &list->items[index], item)) {
                *owned = 0;
                return;
            }
        }
        py_list_set(lst, i, item);
        return;
    }
    py_list_set(lst, i, pcc_gc_load_ptr(NULL, source));
}

int64_t py_list_setitem(PyObject *lst, int64_t i, PyObject *item) {
    /* items[i] = v subscript store: like py_list_set but raises IndexError on
     * out-of-range so try/except can catch it (CPython: "list assignment
     * index out of range"). py_list_set stays non-raising for internal
     * callers. Negative indices normalize. Mirrored in py_list.py. */
    if (list_set_item_transaction(lst, i, item) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "list assignment index out of range"));
        return -1;
    }
    return 0;
}

int64_t py_list_len(PyObject *lst) {
    if (lst == NULL) return 0;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        return ((PyListObject *)lst)->length;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return 0;
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    int64_t length = ((PyListObject *)lst)->length;
    pcc_gc_root_slot_unlock();
    list_finish_moving_root(list_handle);
    return length;
}

static int list_append_snapshot_items(
    PyObject **out_slot,
    PyObject *source,
    int64_t source_length,
    int64_t repeat_count
) {
    if (out_slot == NULL || *out_slot == NULL || source == NULL) return -1;
    PyObject *source_root = source;
    void *out_handle = NULL;
    void *source_handle = NULL;
    if (list_prepare_moving_root(out_slot, &out_handle) != 0) return -1;
    if (list_prepare_moving_root(&source_root, &source_handle) != 0) {
        list_finish_moving_root(out_handle);
        return -1;
    }
    for (int64_t repeat = 0; repeat < repeat_count; repeat++) {
        for (int64_t i = 0; i < source_length; i++) {
            source = list_reload_moving_root(&source_root, source_handle);
            *out_slot = list_reload_moving_root(out_slot, out_handle);
            PyObject *value = py_list_get(source, i);
            if (value == NULL) {
                list_finish_moving_root(source_handle);
                list_finish_moving_root(out_handle);
                return -1;
            }
            py_list_append(*out_slot, value);
            py_decref(value);
            if (py_err_occurred()) {
                *out_slot = list_reload_moving_root(out_slot, out_handle);
                list_finish_moving_root(source_handle);
                list_finish_moving_root(out_handle);
                return -1;
            }
        }
    }
    *out_slot = list_reload_moving_root(out_slot, out_handle);
    list_finish_moving_root(source_handle);
    list_finish_moving_root(out_handle);
    return 0;
}

PyObject *py_list_concat(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) return NULL;
    PyListObject *la = (PyListObject *)a;
    PyListObject *lb = (PyListObject *)b;
    int64_t n = la->length + lb->length;
    PyObject *out = py_list_new(n > 0 ? n : 4);
    if (out == NULL) return NULL;
    if (pcc_gc_backend() != PCC_GC_KIND_REFCOUNT_CYCLE) {
        int64_t a_length = la->length;
        int64_t b_length = lb->length;
        if (
            list_append_snapshot_items(&out, a, a_length, 1) != 0
            || list_append_snapshot_items(&out, b, b_length, 1) != 0
        ) {
            py_decref(out);
            return NULL;
        }
        return out;
    }
    PyListObject *lo = (PyListObject *)out;
    for (int64_t i = 0; i < la->length; i++) {
        PyObject *v = pcc_gc_load_ptr(a, &la->items[i]);
        py_incref(v);
        lo->items[lo->length++] = v;
    }
    for (int64_t i = 0; i < lb->length; i++) {
        PyObject *v = pcc_gc_load_ptr(b, &lb->items[i]);
        py_incref(v);
        lo->items[lo->length++] = v;
    }
    return out;
}

/* ``[x] * n`` / ``n * [x]`` — returns a fresh list of length
 * ``src->length * count`` with each source slot copied ``count`` times
 * in order. Matches CPython's list-repeat semantics (elements share
 * refs with the source; incref'd once per copy). ``count <= 0`` yields
 * an empty list. */
PyObject *py_list_repeat(PyObject *src, int64_t count) {
    if (src == NULL) return NULL;
    PyListObject *ls = (PyListObject *)src;
    int64_t out_len = count > 0 ? ls->length * count : 0;
    PyObject *out = py_list_new(out_len > 0 ? out_len : 4);
    if (out == NULL) return NULL;
    if (pcc_gc_backend() != PCC_GC_KIND_REFCOUNT_CYCLE) {
        if (list_append_snapshot_items(
                &out, src, ls->length, count > 0 ? count : 0
            ) != 0) {
            py_decref(out);
            return NULL;
        }
        return out;
    }
    PyListObject *lo = (PyListObject *)out;
    for (int64_t k = 0; k < (count > 0 ? count : 0); k++) {
        for (int64_t i = 0; i < ls->length; i++) {
            PyObject *v = pcc_gc_load_ptr(src, &ls->items[i]);
            py_incref(v);
            lo->items[lo->length++] = v;
        }
    }
    return out;
}

/* list.copy() — shallow copy: a fresh list with the same elements (element
 * refs are shared with the source, incref'd once each). Matches CPython
 * ``list.copy()`` (equal contents, distinct identity). An empty source yields
 * a fresh empty list. */
PyObject *py_list_copy(PyObject *src) {
    if (src == NULL) return NULL;
    PyListObject *ls = (PyListObject *)src;
    assert(ls->h.type_tag == PY_TYPE_LIST);
    int64_t n = ls->length;
    PyObject *out = py_list_new(n > 0 ? n : 4);
    if (out == NULL) return NULL;
    if (pcc_gc_backend() != PCC_GC_KIND_REFCOUNT_CYCLE) {
        if (list_append_snapshot_items(&out, src, n, 1) != 0) {
            py_decref(out);
            return NULL;
        }
        return out;
    }
    PyListObject *lo = (PyListObject *)out;
    for (int64_t i = 0; i < n; i++) {
        PyObject *v = pcc_gc_load_ptr(src, &ls->items[i]);
        py_incref(v);
        lo->items[lo->length++] = v;
    }
    return out;
}

static int list_eq_at_callback(
    PyObject **list_root,
    void *list_handle,
    PyObject **query_root,
    void *query_handle,
    PyObject **candidate_root,
    int64_t index,
    int *out_in_range
) {
    if (out_in_range == NULL) return 0;
    *out_in_range = 0;
    PccGcRetainPlan retain_plan;
    pcc_gc_root_slot_lock();
    PyObject *lst = list_reload_moving_root(list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    if (index < 0 || index >= l->length) {
        pcc_gc_root_slot_unlock();
        return 0;
    }
    *candidate_root = pcc_gc_load_ptr(lst, &l->items[index]);
    *candidate_root = pcc_gc_retain_plan_prepare_locked(
        &retain_plan, *candidate_root
    );
    pcc_gc_root_slot_unlock();
    pcc_gc_retain_plan_finish(&retain_plan);
    *out_in_range = 1;

    PyObject *query = list_reload_moving_root(query_root, query_handle);
    int equal = py_obj_eq(*candidate_root, query);
    int had_error = py_err_occurred() != 0;

    pcc_gc_root_slot_lock();
    PyObject *candidate = pcc_gc_load_ptr(NULL, candidate_root);
    *candidate_root = NULL;
    pcc_gc_root_slot_unlock();
    py_decref(candidate);
    return had_error ? -1 : equal;
}

int64_t py_list_contains(PyObject *lst, PyObject *item) {
    if (lst == NULL) return 0;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        for (int64_t i = 0; i < fast->length; i++) {
            int equal = py_obj_eq(fast->items[i], item) != 0;
            if (py_err_occurred()) return 0;
            if (equal) return 1;
        }
        return 0;
    }
    PyObject *list_root = lst;
    PyObject *query_root = item;
    PyObject *candidate_root = NULL;
    void *list_handle = NULL;
    void *query_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return 0;
    if (list_prepare_moving_root(&query_root, &query_handle) != 0) {
        list_finish_moving_root(list_handle);
        return 0;
    }
    void *candidate_handle = pcc_gc_scheduler_root_register_handle(
        &candidate_root
    );
    if (candidate_handle == NULL) {
        list_finish_moving_root(query_handle);
        list_finish_moving_root(list_handle);
        return 0;
    }
    int64_t index = 0;
    for (;;) {
        int in_range = 0;
        int equal = list_eq_at_callback(
            &list_root,
            list_handle,
            &query_root,
            query_handle,
            &candidate_root,
            index,
            &in_range
        );
        if (equal < 0) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return 0;
        }
        if (!in_range || equal) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return equal ? 1 : 0;
        }
        index++;
    }
}

/* ---- Slice ------------------------------------------------------------ */

PyObject *py_list_slice(PyObject *lst, PyObject *lo, PyObject *hi, PyObject *step) {
    if (lst == NULL) return NULL;
    PyObject *source_root = lst;
    void *source_handle = NULL;
    if (list_prepare_moving_root(&source_root, &source_handle) != 0) {
        return NULL;
    }
    lst = list_reload_moving_root(&source_root, source_handle);
    PyListObject *l = (PyListObject *)lst;
    int64_t len = l->length;

    int64_t lo_v, hi_v, step_v;
    if (normalize_slice(lo, hi, step, len, &lo_v, &hi_v, &step_v) != 0) {
        list_finish_moving_root(source_handle);
        return NULL;
    }
    lst = list_reload_moving_root(&source_root, source_handle);
    l = (PyListObject *)lst;

    /* Walk and build. */
    PyObject *out = py_list_new(4);
    if (out == NULL) {
        list_finish_moving_root(source_handle);
        return NULL;
    }
    PyObject *out_root = out;
    void *out_handle = NULL;
    if (list_prepare_moving_root(&out_root, &out_handle) != 0) {
        py_decref(out);
        list_finish_moving_root(source_handle);
        return NULL;
    }

    if (step_v > 0) {
        for (int64_t i = lo_v; i < hi_v; i += step_v) {
            lst = list_reload_moving_root(&source_root, source_handle);
            l = (PyListObject *)lst;
            out = list_reload_moving_root(&out_root, out_handle);
            PyObject *v = pcc_gc_load_ptr(lst, &l->items[i]);
            py_list_append(out, v);
            if (py_err_occurred()) {
                out = list_reload_moving_root(&out_root, out_handle);
                list_finish_moving_root(out_handle);
                list_finish_moving_root(source_handle);
                py_decref(out);
                return NULL;
            }
        }
    } else {
        for (int64_t i = lo_v; i > hi_v; i += step_v) {
            if (i < 0 || i >= len) break;
            lst = list_reload_moving_root(&source_root, source_handle);
            l = (PyListObject *)lst;
            out = list_reload_moving_root(&out_root, out_handle);
            PyObject *v = pcc_gc_load_ptr(lst, &l->items[i]);
            py_list_append(out, v);
            if (py_err_occurred()) {
                out = list_reload_moving_root(&out_root, out_handle);
                list_finish_moving_root(out_handle);
                list_finish_moving_root(source_handle);
                py_decref(out);
                return NULL;
            }
        }
    }

    out = list_reload_moving_root(&out_root, out_handle);
    list_finish_moving_root(out_handle);
    list_finish_moving_root(source_handle);
    return out;
}

static PyObject *list_snapshot_sequence(PyObject *seq) {
    if (seq == NULL) return NULL;
    if (py_type_of(seq) == PY_TYPE_LIST) return py_list_copy(seq);
    if (py_type_of(seq) != PY_TYPE_TUPLE) return NULL;
    PyObject *source_root = seq;
    void *source_handle = NULL;
    if (list_prepare_moving_root(&source_root, &source_handle) != 0) return NULL;
    int64_t length = py_tuple_len(source_root);
    if (length < 0) {
        list_finish_moving_root(source_handle);
        return NULL;
    }
    PyObject *out = py_list_new(length > 0 ? length : 4);
    void *out_handle = NULL;
    if (out == NULL || list_prepare_moving_root(&out, &out_handle) != 0) {
        list_finish_moving_root(source_handle);
        if (out != NULL) py_decref(out);
        return NULL;
    }
    for (int64_t i = 0; i < length; i++) {
        source_root = list_reload_moving_root(&source_root, source_handle);
        out = list_reload_moving_root(&out, out_handle);
        PyObject *value = py_tuple_get(source_root, i);
        if (value == NULL) {
            list_finish_moving_root(out_handle);
            list_finish_moving_root(source_handle);
            py_decref(out);
            return NULL;
        }
        py_list_append(out, value);
        py_decref(value);
    }
    out = list_reload_moving_root(&out, out_handle);
    list_finish_moving_root(out_handle);
    list_finish_moving_root(source_handle);
    return out;
}

int64_t py_list_set_slice(PyObject *lst, PyObject *lo, PyObject *hi,
                          PyObject *step, PyObject *replacement) {
    if (lst == NULL || replacement == NULL) return -1;
    PyObject *list_root = lst;
    PyObject *replacement_root = replacement;
    PyObject *lo_root = lo;
    PyObject *hi_root = hi;
    PyObject *step_root = step;
    void *list_handle = NULL;
    void *replacement_handle = NULL;
    void *lo_handle = NULL;
    void *hi_handle = NULL;
    void *step_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return -1;
    if (list_prepare_moving_root(
            &replacement_root, &replacement_handle
        ) != 0) goto set_fail;
    if (list_prepare_moving_root(&lo_root, &lo_handle) != 0) goto set_fail;
    if (list_prepare_moving_root(&hi_root, &hi_handle) != 0) goto set_fail;
    if (list_prepare_moving_root(&step_root, &step_handle) != 0) goto set_fail;

    step_root = list_reload_moving_root(&step_root, step_handle);
    int step_none = is_none_or_null(step_root);
    int64_t step_v = 1;
    if (!step_none) {
        step_v = py_obj_index_i64(step_root);
        if (py_err_occurred() || step_v == 0) goto set_fail;
    }
    lo_root = list_reload_moving_root(&lo_root, lo_handle);
    int lo_none = is_none_or_null(lo_root);
    int64_t raw_lo = 0;
    if (!lo_none) {
        raw_lo = py_obj_index_i64(lo_root);
        if (py_err_occurred()) goto set_fail;
    }
    hi_root = list_reload_moving_root(&hi_root, hi_handle);
    int hi_none = is_none_or_null(hi_root);
    int64_t raw_hi = 0;
    if (!hi_none) {
        raw_hi = py_obj_index_i64(hi_root);
        if (py_err_occurred()) goto set_fail;
    }
    list_finish_moving_root(step_handle);
    step_handle = NULL;
    list_finish_moving_root(hi_handle);
    hi_handle = NULL;
    list_finish_moving_root(lo_handle);
    lo_handle = NULL;

    replacement = list_reload_moving_root(
        &replacement_root, replacement_handle
    );
    PyObject *snapshot = list_snapshot_sequence(replacement);
    if (snapshot == NULL) goto set_fail;
    list_finish_moving_root(replacement_handle);
    replacement_handle = NULL;
    PyObject *snapshot_root = snapshot;
    void *snapshot_handle = NULL;
    if (list_prepare_moving_root(&snapshot_root, &snapshot_handle) != 0) {
        py_decref(snapshot);
        goto set_fail;
    }
    int64_t repl_len = py_list_len(snapshot_root);
    int64_t backend = pcc_gc_backend();

    for (int attempt = 0; attempt < 8; attempt++) {
        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        PyListObject *old_list = (PyListObject *)lst;
        int64_t old_len = old_list->length;
        int64_t old_capacity = old_list->capacity;
        PyObject **old_items = old_list->items;
        pcc_gc_root_slot_unlock();
        int64_t lo_v = 0;
        int64_t hi_v = 0;
        normalize_slice_scalars(
            lo_none, hi_none, raw_lo, raw_hi, step_v, old_len, &lo_v, &hi_v
        );
        if (step_v == 1 && hi_v < lo_v) hi_v = lo_v;
        int64_t selected = step_v == 1
            ? (hi_v > lo_v ? hi_v - lo_v : 0)
            : slice_count(lo_v, hi_v, step_v);
        if (step_v != 1 && repl_len != selected) break;
        int64_t new_len = step_v == 1
            ? old_len - selected + repl_len
            : old_len;
        if (
            old_len < 0
            || new_len < 0
            || old_capacity < old_len
            || old_capacity <= 0
            || new_len > INT64_MAX / 2
        ) break;
        int64_t new_capacity = old_capacity;
        while (new_capacity < new_len) {
            if (new_capacity > INT64_MAX / 2) {
                new_capacity = -1;
                break;
            }
            new_capacity *= 2;
        }
        if (new_capacity <= 0) break;
        PyObject **new_items = (PyObject **)calloc(
            (size_t)new_capacity, sizeof(PyObject *)
        );
        int64_t *replacement_index = (int64_t *)malloc(
            (size_t)(old_len > 0 ? old_len : 1) * sizeof(int64_t)
        );
        if (new_items == NULL || replacement_index == NULL) {
            free(replacement_index);
            free(new_items);
            break;
        }
        for (int64_t i = 0; i < old_len; i++) replacement_index[i] = -1;
        if (step_v != 1) {
            int64_t idx = lo_v;
            for (int64_t i = 0; i < repl_len; i++) {
                if (idx >= 0 && idx < old_len) replacement_index[idx] = i;
                idx += step_v;
            }
        }

        if (backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
            int64_t built = 0;
            for (int64_t i = 0; i < new_len; i++) {
                PyObject *value = NULL;
                if (step_v == 1) {
                    if (i < lo_v) {
                        value = py_list_get(lst, i);
                    } else if (i < lo_v + repl_len) {
                        value = py_list_get(snapshot_root, i - lo_v);
                    } else {
                        value = py_list_get(
                            lst, hi_v + i - (lo_v + repl_len)
                        );
                    }
                } else if (replacement_index[i] >= 0) {
                    value = py_list_get(snapshot_root, replacement_index[i]);
                } else {
                    value = py_list_get(lst, i);
                }
                if (value == NULL) break;
                new_items[i] = value;
                built++;
            }
            if (built != new_len) {
                for (int64_t i = 0; i < built; i++) py_decref(new_items[i]);
                free(replacement_index);
                free(new_items);
                break;
            }
            old_list->items = new_items;
            old_list->capacity = new_capacity;
            old_list->length = new_len;
            for (int64_t i = 0; i < old_len; i++) {
                if (old_items[i] != NULL) py_decref(old_items[i]);
            }
            free(old_items);
            free(replacement_index);
            list_finish_moving_root(snapshot_handle);
            list_finish_moving_root(list_handle);
            py_decref(snapshot_root);
            return 0;
        }

        PccGcStoreRootPlan *old_plans = NULL;
        PccGcRetainPlan *new_plans = NULL;
        PyObject ***slot_pairs = NULL;
        if (old_len > 0) {
            old_plans = (PccGcStoreRootPlan *)calloc(
                (size_t)old_len, sizeof(PccGcStoreRootPlan)
            );
            slot_pairs = (PyObject ***)calloc(
                (size_t)old_len * 2u, sizeof(PyObject **)
            );
        }
        if (new_len > 0) {
            new_plans = (PccGcRetainPlan *)calloc(
                (size_t)new_len, sizeof(PccGcRetainPlan)
            );
        }
        if (
            (old_len > 0 && (old_plans == NULL || slot_pairs == NULL))
            || (new_len > 0 && new_plans == NULL)
        ) {
            free(slot_pairs);
            free(new_plans);
            free(old_plans);
            free(replacement_index);
            free(new_items);
            break;
        }
        for (int64_t i = 0; i < old_len; i++) {
            pcc_gc_store_ptr_plan_init(&old_plans[i], list_root, backend);
        }

        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        snapshot_root = list_reload_moving_root(
            &snapshot_root, snapshot_handle
        );
        old_list = (PyListObject *)lst;
        PyListObject *snapshot_list = (PyListObject *)snapshot_root;
        if (
            pcc_gc_backend() != backend
            || old_list->length != old_len
            || old_list->capacity != old_capacity
            || old_list->items != old_items
            || snapshot_list->length != repl_len
        ) {
            pcc_gc_root_slot_unlock();
            for (int64_t i = 0; i < old_len; i++) {
                pcc_gc_store_ptr_plan_finish(&old_plans[i]);
            }
            free(slot_pairs);
            free(new_plans);
            free(old_plans);
            free(replacement_index);
            free(new_items);
            continue;
        }
        int64_t pair_count = 0;
        for (int64_t i = 0; i < new_len; i++) {
            int64_t old_index = -1;
            if (step_v == 1) {
                if (i < lo_v) old_index = i;
                else if (i >= lo_v + repl_len) {
                    old_index = hi_v + i - (lo_v + repl_len);
                }
            } else if (replacement_index[i] < 0) {
                old_index = i;
            }
            if (old_index >= 0) {
                slot_pairs[pair_count * 2] = &old_items[old_index];
                slot_pairs[pair_count * 2 + 1] = &new_items[i];
                pair_count++;
            }
        }
        int64_t retargeted =
            pcc_gc_backend4_retarget_mutator_payload_locked(
                lst,
                old_items,
                old_capacity * (int64_t)sizeof(PyObject *),
                new_items,
                new_capacity * (int64_t)sizeof(PyObject *),
                slot_pairs,
                pair_count
            );
        PCC_RT_TRIPWIRE(
            retargeted != 0,
            "list set-slice payload retarget failed before publication"
        );
        if (retargeted == 0) {
            pcc_gc_root_slot_unlock();
            for (int64_t i = 0; i < old_len; i++) {
                pcc_gc_store_ptr_plan_finish(&old_plans[i]);
            }
            free(slot_pairs);
            free(new_plans);
            free(old_plans);
            free(replacement_index);
            free(new_items);
            break;
        }
        for (int64_t i = 0; i < new_len; i++) {
            PyObject *value = NULL;
            if (step_v == 1) {
                if (i < lo_v) {
                    value = pcc_gc_load_ptr(lst, &old_items[i]);
                } else if (i < lo_v + repl_len) {
                    value = pcc_gc_load_ptr(
                        snapshot_root, &snapshot_list->items[i - lo_v]
                    );
                } else {
                    int64_t old_index = hi_v + i - (lo_v + repl_len);
                    value = pcc_gc_load_ptr(lst, &old_items[old_index]);
                }
            } else if (replacement_index[i] >= 0) {
                value = pcc_gc_load_ptr(
                    snapshot_root,
                    &snapshot_list->items[replacement_index[i]]
                );
            } else {
                value = pcc_gc_load_ptr(lst, &old_items[i]);
            }
            new_items[i] = pcc_gc_retain_plan_prepare_locked(
                &new_plans[i], value
            );
            pcc_gc_note_slot_write_barrier(lst, &new_items[i], new_items[i]);
        }
        for (int64_t i = 0; i < old_len; i++) {
            int64_t committed = pcc_gc_store_ptr_plan_commit_locked(
                &old_plans[i], lst, &old_items[i], NULL
            );
            PCC_RT_TRIPWIRE(
                committed != 0,
                "list set-slice old ownership detach failed"
            );
            (void)committed;
        }
        old_list->items = new_items;
        old_list->capacity = new_capacity;
        old_list->length = new_len;
        if (retargeted == 2) {
            (void)pcc_gc_backend4_zpage_register_owner_payload_span(
                lst,
                new_items,
                new_capacity * (int64_t)sizeof(PyObject *)
            );
        }
        pcc_gc_root_slot_unlock();

        for (int64_t i = 0; i < new_len; i++) {
            pcc_gc_retain_plan_finish(&new_plans[i]);
        }
        for (int64_t i = 0; i < old_len; i++) {
            pcc_gc_store_ptr_plan_finish(&old_plans[i]);
        }
        free(old_items);
        free(slot_pairs);
        free(new_plans);
        free(old_plans);
        free(replacement_index);
        snapshot_root = list_reload_moving_root(
            &snapshot_root, snapshot_handle
        );
        list_finish_moving_root(snapshot_handle);
        list_finish_moving_root(list_handle);
        py_decref(snapshot_root);
        return 0;
    }

    snapshot_root = list_reload_moving_root(&snapshot_root, snapshot_handle);
    list_finish_moving_root(snapshot_handle);
    py_decref(snapshot_root);
set_fail:
    list_finish_moving_root(step_handle);
    list_finish_moving_root(hi_handle);
    list_finish_moving_root(lo_handle);
    list_finish_moving_root(replacement_handle);
    list_finish_moving_root(list_handle);
    return -1;
}

int64_t py_list_del_slice(PyObject *lst, PyObject *lo, PyObject *hi,
                          PyObject *step) {
    if (lst == NULL) return -1;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t lo_v, hi_v, step_v;
        if (normalize_slice(
                lo, hi, step, fast->length, &lo_v, &hi_v, &step_v
            ) != 0) return -1;
        if (step_v == 1) return list_delete_range(fast, lo_v, hi_v);
        int64_t count = slice_count(lo_v, hi_v, step_v);
        if (count <= 0) return 0;
        if (step_v > 0) {
            int64_t idx = lo_v + (count - 1) * step_v;
            for (int64_t n = 0; n < count; n++) {
                list_delete_index(fast, idx);
                idx -= step_v;
            }
        } else {
            int64_t idx = lo_v;
            for (int64_t n = 0; n < count; n++) {
                list_delete_index(fast, idx);
                idx += step_v;
            }
        }
        return 0;
    }

    PyObject *list_root = lst;
    PyObject *lo_root = lo;
    PyObject *hi_root = hi;
    PyObject *step_root = step;
    void *list_handle = NULL;
    void *lo_handle = NULL;
    void *hi_handle = NULL;
    void *step_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return -1;
    if (list_prepare_moving_root(&lo_root, &lo_handle) != 0) goto del_fail;
    if (list_prepare_moving_root(&hi_root, &hi_handle) != 0) goto del_fail;
    if (list_prepare_moving_root(&step_root, &step_handle) != 0) goto del_fail;

    step_root = list_reload_moving_root(&step_root, step_handle);
    int step_none = is_none_or_null(step_root);
    int64_t step_v = 1;
    if (!step_none) {
        step_v = py_obj_index_i64(step_root);
        if (py_err_occurred() || step_v == 0) goto del_fail;
    }
    lo_root = list_reload_moving_root(&lo_root, lo_handle);
    int lo_none = is_none_or_null(lo_root);
    int64_t raw_lo = 0;
    if (!lo_none) {
        raw_lo = py_obj_index_i64(lo_root);
        if (py_err_occurred()) goto del_fail;
    }
    hi_root = list_reload_moving_root(&hi_root, hi_handle);
    int hi_none = is_none_or_null(hi_root);
    int64_t raw_hi = 0;
    if (!hi_none) {
        raw_hi = py_obj_index_i64(hi_root);
        if (py_err_occurred()) goto del_fail;
    }
    list_finish_moving_root(step_handle);
    step_handle = NULL;
    list_finish_moving_root(hi_handle);
    hi_handle = NULL;
    list_finish_moving_root(lo_handle);
    lo_handle = NULL;

    int64_t backend = pcc_gc_backend();
    for (int attempt = 0; attempt < 8; attempt++) {
        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        PyListObject *snapshot = (PyListObject *)lst;
        int64_t length = snapshot->length;
        int64_t capacity = snapshot->capacity;
        pcc_gc_root_slot_unlock();
        int64_t lo_v = 0;
        int64_t hi_v = 0;
        normalize_slice_scalars(
            lo_none, hi_none, raw_lo, raw_hi, step_v, length, &lo_v, &hi_v
        );
        int64_t count = slice_count(lo_v, hi_v, step_v);
        if (count <= 0) {
            list_finish_moving_root(list_handle);
            return 0;
        }
        if (
            length <= 0
            || count > length
            || length > INT64_MAX / (2 * (int64_t)sizeof(PyObject **))
            || count > INT64_MAX / (int64_t)sizeof(PccGcStoreRootPlan)
        ) break;
        uint8_t *remove_mask = (uint8_t *)calloc((size_t)length, 1);
        PccGcStoreRootPlan *plans = (PccGcStoreRootPlan *)calloc(
            (size_t)count, sizeof(PccGcStoreRootPlan)
        );
        PyObject ***slot_pairs = (PyObject ***)calloc(
            (size_t)length * 2u, sizeof(PyObject **)
        );
        if (remove_mask == NULL || plans == NULL || slot_pairs == NULL) {
            free(slot_pairs);
            free(plans);
            free(remove_mask);
            break;
        }
        int64_t idx = lo_v;
        for (int64_t i = 0; i < count; i++) {
            if (idx >= 0 && idx < length) remove_mask[idx] = 1;
            idx += step_v;
        }
        for (int64_t i = 0; i < count; i++) {
            pcc_gc_store_ptr_plan_init(&plans[i], list_root, backend);
        }

        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        PyListObject *l = (PyListObject *)lst;
        if (
            pcc_gc_backend() != backend
            || l->length != length
            || l->capacity != capacity
        ) {
            pcc_gc_root_slot_unlock();
            for (int64_t i = 0; i < count; i++) {
                pcc_gc_store_ptr_plan_finish(&plans[i]);
            }
            free(slot_pairs);
            free(plans);
            free(remove_mask);
            continue;
        }
        PyObject **items = l->items;
        int64_t dst = 0;
        int64_t pair_count = 0;
        for (int64_t src = 0; src < length; src++) {
            if (remove_mask[src]) continue;
            if (dst != src) {
                slot_pairs[pair_count * 2] = &items[src];
                slot_pairs[pair_count * 2 + 1] = &items[dst];
                pair_count++;
            }
            dst++;
        }
        if (pcc_gc_backend4_retarget_mutator_payload_locked(
                lst,
                items,
                capacity * (int64_t)sizeof(PyObject *),
                items,
                capacity * (int64_t)sizeof(PyObject *),
                slot_pairs,
                pair_count
            ) == 0) {
            pcc_gc_root_slot_unlock();
            for (int64_t i = 0; i < count; i++) {
                pcc_gc_store_ptr_plan_finish(&plans[i]);
            }
            free(slot_pairs);
            free(plans);
            free(remove_mask);
            break;
        }
        int64_t plan_i = 0;
        for (int64_t src = 0; src < length; src++) {
            if (!remove_mask[src]) continue;
            int64_t committed = pcc_gc_store_ptr_plan_commit_locked(
                &plans[plan_i], lst, &items[src], NULL
            );
            PCC_RT_TRIPWIRE(
                committed != 0,
                "list delete split-store commit failed after mutation began"
            );
            (void)committed;
            plan_i++;
        }
        dst = 0;
        for (int64_t src = 0; src < length; src++) {
            if (remove_mask[src]) continue;
            if (dst != src) {
                PyObject *value = pcc_gc_load_ptr(lst, &items[src]);
                items[dst] = value;
                pcc_gc_note_slot_write_barrier(lst, &items[dst], value);
            }
            dst++;
        }
        for (int64_t i = dst; i < length; i++) items[i] = NULL;
        l->length = dst;
        pcc_gc_root_slot_unlock();

        for (int64_t i = 0; i < count; i++) {
            pcc_gc_store_ptr_plan_finish(&plans[i]);
        }
        free(slot_pairs);
        free(plans);
        free(remove_mask);
        list_finish_moving_root(list_handle);
        return 0;
    }

del_fail:
    list_finish_moving_root(step_handle);
    list_finish_moving_root(hi_handle);
    list_finish_moving_root(lo_handle);
    list_finish_moving_root(list_handle);
    return -1;
}

/* ---- Extend ----------------------------------------------------------- */

void py_list_extend(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) return;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)a;
        int32_t fast_tag = py_type_of(b);
        if (fast_tag == PY_TYPE_LIST) {
            PyListObject *source = (PyListObject *)b;
            int64_t source_length = source->length;
            if (grow_if_needed(&fast, fast->length + source_length) != 0) {
                py_raise_owned(py_exc_new(
                    PY_EXC_MEMORYERROR, "list extend: out of memory"
                ));
                return;
            }
            for (int64_t i = 0; i < source_length; i++) {
                PyObject *value = source->items[i];
                fast->items[fast->length] = NULL;
                pcc_gc_store_ptr(
                    (PyObject *)fast, &fast->items[fast->length], value
                );
                fast->length++;
            }
            return;
        }
        if (fast_tag == PY_TYPE_TUPLE) {
            PyTupleObject *source = (PyTupleObject *)b;
            int64_t source_length = source->len;
            if (grow_if_needed(&fast, fast->length + source_length) != 0) {
                py_raise_owned(py_exc_new(
                    PY_EXC_MEMORYERROR, "list extend: out of memory"
                ));
                return;
            }
            for (int64_t i = 0; i < source_length; i++) {
                PyObject *value = source->items[i];
                fast->items[fast->length] = NULL;
                pcc_gc_store_ptr(
                    (PyObject *)fast, &fast->items[fast->length], value
                );
                fast->length++;
            }
            return;
        }
        PyObject *iterator = py_obj_iter(b);
        if (iterator == NULL) return;
        for (;;) {
            PyObject *value = py_obj_next(iterator);
            if (value == NULL) {
                if (py_err_occurred()) {
                    PyObject *cur = py_current_exception();
                    PyObject *stop = (PyObject *)py_exc_builtin_class(
                        PY_EXC_STOPITERATION
                    );
                    if (py_exc_matches(cur, stop)) {
                        py_clear_exception();
                        break;
                    }
                }
                py_decref(iterator);
                return;
            }
            py_list_append((PyObject *)fast, value);
            py_decref(value);
        }
        py_decref(iterator);
        return;
    }

    PyObject *list_root = a;
    PyObject *source_root = b;
    void *list_handle = NULL;
    void *source_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list extend: out of memory"));
        return;
    }
    if (list_prepare_moving_root(&source_root, &source_handle) != 0) {
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list extend: out of memory"));
        return;
    }
    a = list_reload_moving_root(&list_root, list_handle);
    b = list_reload_moving_root(&source_root, source_handle);
    PyListObject *la = (PyListObject *)a;

    int32_t btag = py_type_of(b);
    if (btag == PY_TYPE_LIST) {
        PyListObject *lb = (PyListObject *)b;
        /* Guard against self-extend: snapshot length before iterating. */
        int64_t bl = lb->length;
        if (grow_if_needed(&la, la->length + bl) != 0) {
            list_finish_moving_root(source_handle);
            list_finish_moving_root(list_handle);
            py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list extend: out of memory"));
            return;
        }
        list_root = (PyObject *)la;
        for (int64_t i = 0; i < bl; i++) {
            PccGcStoreRootPlan store_plan;
            pcc_gc_store_ptr_plan_init(
                &store_plan, list_root, pcc_gc_backend()
            );
            pcc_gc_root_slot_lock();
            a = list_reload_moving_root(&list_root, list_handle);
            b = list_reload_moving_root(&source_root, source_handle);
            la = (PyListObject *)a;
            lb = (PyListObject *)b;
            PyObject *v = pcc_gc_load_ptr(b, &lb->items[i]);
            /* Match py_list_append's grown-slot store: NULL-init the fresh
             * capacity slot (py_list_new/grow leave items[] unzeroed, so the
             * slot holds garbage), then route through the collector barrier.
            * pcc_gc_store_ptr increfs v, so the prior manual incref is dropped
             * to keep the net accounting (+1 owned ref) identical. */
            la->items[la->length] = NULL;
            int committed = pcc_gc_store_ptr_plan_commit_locked(
                &store_plan, a, &la->items[la->length], v
            );
            if (committed) la->length++;
            pcc_gc_root_slot_unlock();
            pcc_gc_store_ptr_plan_finish(&store_plan);
        }
        list_finish_moving_root(source_handle);
        list_finish_moving_root(list_handle);
        return;
    }

    if (btag == PY_TYPE_TUPLE) {
        PyTupleObject *tb = (PyTupleObject *)b;
        int64_t bl = tb->len;
        if (grow_if_needed(&la, la->length + bl) != 0) {
            list_finish_moving_root(source_handle);
            list_finish_moving_root(list_handle);
            py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list extend: out of memory"));
            return;
        }
        list_root = (PyObject *)la;
        for (int64_t i = 0; i < bl; i++) {
            PccGcStoreRootPlan store_plan;
            pcc_gc_store_ptr_plan_init(
                &store_plan, list_root, pcc_gc_backend()
            );
            pcc_gc_root_slot_lock();
            a = list_reload_moving_root(&list_root, list_handle);
            b = list_reload_moving_root(&source_root, source_handle);
            la = (PyListObject *)a;
            tb = (PyTupleObject *)b;
            PyObject *v = pcc_gc_load_ptr(b, &tb->items[i]);
            /* Same grown-slot store idiom as the list branch / py_list_append:
             * NULL-init the fresh (unzeroed) capacity slot, then barrier-store.
             * pcc_gc_store_ptr increfs v, so the manual incref is dropped (net +1). */
            la->items[la->length] = NULL;
            int committed = pcc_gc_store_ptr_plan_commit_locked(
                &store_plan, a, &la->items[la->length], v
            );
            if (committed) la->length++;
            pcc_gc_root_slot_unlock();
            pcc_gc_store_ptr_plan_finish(&store_plan);
        }
        list_finish_moving_root(source_handle);
        list_finish_moving_root(list_handle);
        return;
    }

    b = list_reload_moving_root(&source_root, source_handle);
    PyObject *it = py_obj_iter(b);
    if (it == NULL) {
        list_finish_moving_root(source_handle);
        list_finish_moving_root(list_handle);
        return;
    }
    for (;;) {
        PyObject *item = py_obj_next(it);
        if (item == NULL) {
            if (py_err_occurred()) {
                PyObject *cur = py_current_exception();
                PyObject *stop = py_exc_builtin_class(PY_EXC_STOPITERATION);
                if (py_exc_matches(cur, stop)) {
                    py_clear_exception();
                    break;
                }
            }
            py_decref(it);
            list_finish_moving_root(source_handle);
            list_finish_moving_root(list_handle);
            return;
        }
        a = list_reload_moving_root(&list_root, list_handle);
        py_list_append(a, item);
        py_decref(item);
    }
    py_decref(it);
    list_finish_moving_root(source_handle);
    list_finish_moving_root(list_handle);
}

/* ---- Insert ----------------------------------------------------------- */

void py_list_insert(PyObject *lst, int64_t i, PyObject *item) {
    if (lst == NULL) return;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t idx = normalize_index(i, fast->length, 1);
        if (grow_if_needed(&fast, fast->length + 1) != 0) {
            py_raise_owned(py_exc_new(
                PY_EXC_MEMORYERROR, "list insert: out of memory"
            ));
            return;
        }
        if (idx < fast->length) {
            memmove(
                &fast->items[idx + 1],
                &fast->items[idx],
                (size_t)(fast->length - idx) * sizeof(PyObject *)
            );
        }
        fast->items[idx] = NULL;
        pcc_gc_store_ptr((PyObject *)fast, &fast->items[idx], item);
        fast->length++;
        return;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list insert: out of memory"));
        return;
    }
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;

    /* Python's list.insert clips: negative wraps, out-of-range saturates. */
    int64_t idx = normalize_index(i, l->length, 1);

    PyObject *item_root = item;
    void *item_handle = NULL;
    if (list_prepare_moving_root(&item_root, &item_handle) != 0) {
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list insert: out of memory"));
        return;
    }
    if (grow_if_needed(&l, l->length + 1) != 0) {
        list_finish_moving_root(item_handle);
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "list insert: out of memory"));
        return;
    }
    list_root = (PyObject *)l;
    int64_t commit_backend = pcc_gc_backend();
    PccGcStoreRootPlan store_plan;
    pcc_gc_store_ptr_plan_init(
        &store_plan, list_root, commit_backend
    );
    if (commit_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_root_slot_lock();
    }
    lst = list_reload_moving_root(&list_root, list_handle);
    l = (PyListObject *)lst;
    item = list_reload_moving_root(&item_root, item_handle);

    /* Shift tail [idx, length) right by one. */
    if (idx < l->length) {
        memmove(&l->items[idx + 1],
                &l->items[idx],
                (size_t)(l->length - idx) * sizeof(PyObject *));
    }
    l->items[idx] = NULL;
    int committed = pcc_gc_store_ptr_plan_commit_locked(
        &store_plan, lst, &l->items[idx], item
    );
    if (committed) l->length++;
    if (commit_backend != PCC_GC_KIND_REFCOUNT_CYCLE) {
        pcc_gc_root_slot_unlock();
    }
    pcc_gc_store_ptr_plan_finish(&store_plan);
    list_finish_moving_root(item_handle);
    list_finish_moving_root(list_handle);
}

/* ---- Pop -------------------------------------------------------------- */

PyObject *py_list_pop(PyObject *lst, int64_t i) {
    if (lst == NULL) return NULL;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        if (fast->length == 0) {
            py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "pop from empty list"));
            return NULL;
        }
        int64_t fast_idx = i == -1
            ? fast->length - 1
            : normalize_index(i, fast->length, 0);
        if (fast_idx < 0) {
            py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "pop index out of range"));
            return NULL;
        }
        PyObject *value = fast->items[fast_idx];
        if (fast_idx < fast->length - 1) {
            memmove(
                &fast->items[fast_idx],
                &fast->items[fast_idx + 1],
                (size_t)(fast->length - fast_idx - 1) * sizeof(PyObject *)
            );
        }
        fast->length--;
        return value;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return NULL;
    PyObject *result_root = NULL;
    void *result_handle = pcc_gc_scheduler_root_register_handle(&result_root);
    if (result_handle == NULL) {
        list_finish_moving_root(list_handle);
        return NULL;
    }
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    if (l->length == 0) {
        pcc_gc_root_slot_unlock();
        pcc_gc_scheduler_root_unregister_handle(result_handle);
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "pop from empty list"));
        return NULL;
    }
    int64_t idx = i == -1
        ? l->length - 1
        : normalize_index(i, l->length, 0);
    if (idx < 0) {
        pcc_gc_root_slot_unlock();
        pcc_gc_scheduler_root_unregister_handle(result_handle);
        list_finish_moving_root(list_handle);
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "pop index out of range"));
        return NULL;
    }
    result_root = pcc_gc_load_ptr(lst, &l->items[idx]);
    if (idx < l->length - 1) {
        memmove(
            &l->items[idx],
            &l->items[idx + 1],
            (size_t)(l->length - idx - 1) * sizeof(PyObject *)
        );
    }
    l->length--;
    pcc_gc_root_slot_unlock();
    list_finish_moving_root(list_handle);
    pcc_gc_scheduler_root_unregister_handle(result_handle);
    return result_root;
}

/* ---- Remove ----------------------------------------------------------- */

void py_list_remove(PyObject *lst, PyObject *item) {
    if (lst == NULL) return;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        for (int64_t i = 0; i < fast->length; i++) {
            PyObject *value = fast->items[i];
            int equal = py_obj_eq(value, item) != 0;
            if (py_err_occurred()) return;
            if (equal) {
                fast->items[i] = NULL;
                if (value != NULL) py_decref(value);
                if (i < fast->length - 1) {
                    memmove(
                        &fast->items[i],
                        &fast->items[i + 1],
                        (size_t)(fast->length - i - 1) * sizeof(PyObject *)
                    );
                }
                fast->length--;
                return;
            }
        }
        PyObject *exc = py_exc_new(
            PY_TYPE_EXC, "list.remove(x): x not in list"
        );
        py_raise(exc);
        if (exc) py_decref(exc);
        return;
    }
    PyObject *list_root = lst;
    PyObject *query_root = item;
    PyObject *candidate_root = NULL;
    void *list_handle = NULL;
    void *query_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return;
    if (list_prepare_moving_root(&query_root, &query_handle) != 0) {
        list_finish_moving_root(list_handle);
        return;
    }
    void *candidate_handle = pcc_gc_scheduler_root_register_handle(
        &candidate_root
    );
    if (candidate_handle == NULL) {
        list_finish_moving_root(query_handle);
        list_finish_moving_root(list_handle);
        return;
    }
    int64_t index = 0;
    for (;;) {
        int in_range = 0;
        int equal = list_eq_at_callback(
            &list_root, list_handle, &query_root, query_handle,
            &candidate_root, index, &in_range
        );
        if (equal < 0) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return;
        }
        if (!in_range) break;
        if (!equal) {
            index++;
            continue;
        }
        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        PyListObject *l = (PyListObject *)lst;
        if (index >= 0 && index < l->length) {
            candidate_root = pcc_gc_load_ptr(lst, &l->items[index]);
            l->items[index] = NULL;
            if (index < l->length - 1) {
                memmove(
                    &l->items[index],
                    &l->items[index + 1],
                    (size_t)(l->length - index - 1) * sizeof(PyObject *)
                );
            }
            l->length--;
        }
        pcc_gc_root_slot_unlock();
        list_finish_moving_root(query_handle);
        list_finish_moving_root(list_handle);
        pcc_gc_scheduler_root_unregister_handle(candidate_handle);
        if (candidate_root != NULL) py_decref(candidate_root);
        return;
    }
    pcc_gc_scheduler_root_unregister_handle(candidate_handle);
    list_finish_moving_root(query_handle);
    list_finish_moving_root(list_handle);
    PyObject *exc = py_exc_new(PY_TYPE_EXC, "list.remove(x): x not in list");
    py_raise(exc);
    /* py_raise incref's; drop our construction ref. */
    if (exc) py_decref(exc);
}

void py_list_clear(PyObject *lst) {
    if (lst == NULL) return;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t fast_length = fast->length;
        fast->length = 0;
        for (int64_t i = 0; i < fast_length; i++) {
            PyObject *value = fast->items[i];
            fast->items[i] = NULL;
            if (value != NULL) py_decref(value);
        }
        return;
    }

    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return;
    int64_t backend = pcc_gc_backend();
    for (int attempt = 0; attempt < 8; attempt++) {
        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        int64_t length = ((PyListObject *)lst)->length;
        pcc_gc_root_slot_unlock();
        if (length <= 0) {
            list_finish_moving_root(list_handle);
            return;
        }
        if (length > INT64_MAX / (int64_t)sizeof(PccGcStoreRootPlan)) break;
        PccGcStoreRootPlan *plans = (PccGcStoreRootPlan *)calloc(
            (size_t)length, sizeof(PccGcStoreRootPlan)
        );
        if (plans == NULL) break;
        for (int64_t i = 0; i < length; i++) {
            pcc_gc_store_ptr_plan_init(&plans[i], list_root, backend);
        }

        pcc_gc_root_slot_lock();
        if (pcc_gc_backend() != backend) {
            pcc_gc_root_slot_unlock();
            for (int64_t i = 0; i < length; i++) {
                pcc_gc_store_ptr_plan_finish(&plans[i]);
            }
            free(plans);
            break;
        }
        lst = list_reload_moving_root(&list_root, list_handle);
        PyListObject *l = (PyListObject *)lst;
        if (l->length != length) {
            pcc_gc_root_slot_unlock();
            for (int64_t i = 0; i < length; i++) {
                pcc_gc_store_ptr_plan_finish(&plans[i]);
            }
            free(plans);
            continue;
        }
        for (int64_t i = 0; i < length; i++) {
            int64_t committed = pcc_gc_store_ptr_plan_commit_locked(
                &plans[i], lst, &l->items[i], NULL
            );
            PCC_RT_TRIPWIRE(
                committed != 0,
                "list clear split-store commit failed after mutation began"
            );
            (void)committed;
        }
        l->length = 0;
        pcc_gc_root_slot_unlock();

        for (int64_t i = 0; i < length; i++) {
            pcc_gc_store_ptr_plan_finish(&plans[i]);
        }
        free(plans);
        list_finish_moving_root(list_handle);
        return;
    }
    list_finish_moving_root(list_handle);
}

void py_obj_clear(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj)) return;
    int32_t tag = py_type_of(obj);
    if (tag == PY_TYPE_LIST) {
        py_list_clear(obj);
    } else if (tag == PY_TYPE_DICT) {
        py_dict_clear(obj);
    }
}

/* ---- Index ------------------------------------------------------------ */

int64_t py_list_index(PyObject *lst, PyObject *item) {
    if (lst == NULL) return -1;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        for (int64_t i = 0; i < fast->length; i++) {
            int equal = py_obj_eq(fast->items[i], item) != 0;
            if (py_err_occurred()) return -1;
            if (equal) return i;
        }
        return -1;
    }
    PyObject *list_root = lst;
    PyObject *query_root = item;
    PyObject *candidate_root = NULL;
    void *list_handle = NULL;
    void *query_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return -1;
    if (list_prepare_moving_root(&query_root, &query_handle) != 0) {
        list_finish_moving_root(list_handle);
        return -1;
    }
    void *candidate_handle = pcc_gc_scheduler_root_register_handle(
        &candidate_root
    );
    if (candidate_handle == NULL) {
        list_finish_moving_root(query_handle);
        list_finish_moving_root(list_handle);
        return -1;
    }
    int64_t index = 0;
    for (;;) {
        int in_range = 0;
        int equal = list_eq_at_callback(
            &list_root, list_handle, &query_root, query_handle,
            &candidate_root, index, &in_range
        );
        if (equal < 0) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return -1;
        }
        if (!in_range || equal) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return equal ? index : -1;
        }
        index++;
    }
}

/* list.index(item, start, end): search the half-open slice [start, end) for
 * the first element equal to ``item``. ``start``/``end`` follow CPython slice
 * clamping: negative indices are offset by the length once, then both bounds
 * are clamped into [0, length]. When ``item`` is not found in the resolved
 * window, raise ValueError (and return -1), matching CPython list.index. This
 * is the range-aware variant of py_list_index; the frontend routes the 3-arg
 * (and 2-arg) form here and checks py_err_occurred() after the call. */
int64_t py_list_index_range(PyObject *lst, PyObject *item,
                            int64_t start, int64_t end) {
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        int64_t length = (lst == NULL) ? 0 : ((PyListObject *)lst)->length;
        if (start < 0) {
            start += length;
            if (start < 0) start = 0;
        } else if (start > length) {
            start = length;
        }
        if (end < 0) {
            end += length;
            if (end < 0) end = 0;
        } else if (end > length) {
            end = length;
        }
        if (lst != NULL) {
            PyListObject *fast = (PyListObject *)lst;
            for (int64_t i = start; i < end; i++) {
                int equal = py_obj_eq(fast->items[i], item) != 0;
                if (py_err_occurred()) return -1;
                if (equal) return i;
            }
        }
        PyObject *exc = py_exc_new(
            PY_EXC_VALUEERROR, "list.index(x): x not in list"
        );
        py_raise(exc);
        if (exc) py_decref(exc);
        return -1;
    }
    if (lst == NULL) return -1;
    PyObject *list_root = lst;
    PyObject *query_root = item;
    PyObject *candidate_root = NULL;
    void *list_handle = NULL;
    void *query_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return -1;
    if (list_prepare_moving_root(&query_root, &query_handle) != 0) {
        list_finish_moving_root(list_handle);
        return -1;
    }
    void *candidate_handle = pcc_gc_scheduler_root_register_handle(
        &candidate_root
    );
    if (candidate_handle == NULL) {
        list_finish_moving_root(query_handle);
        list_finish_moving_root(list_handle);
        return -1;
    }
    pcc_gc_root_slot_lock();
    lst = list_reload_moving_root(&list_root, list_handle);
    int64_t length = ((PyListObject *)lst)->length;
    pcc_gc_root_slot_unlock();
    if (start < 0) {
        start += length;
        if (start < 0) start = 0;
    } else if (start > length) {
        start = length;
    }
    if (end < 0) {
        end += length;
        if (end < 0) end = 0;
    } else if (end > length) {
        end = length;
    }
    for (int64_t i = start; i < end; i++) {
        int in_range = 0;
        int equal = list_eq_at_callback(
            &list_root, list_handle, &query_root, query_handle,
            &candidate_root, i, &in_range
        );
        if (equal < 0) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return -1;
        }
        if (equal) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return i;
        }
        if (!in_range) break;
    }
    pcc_gc_scheduler_root_unregister_handle(candidate_handle);
    list_finish_moving_root(query_handle);
    list_finish_moving_root(list_handle);
    PyObject *exc = py_exc_new(PY_EXC_VALUEERROR, "list.index(x): x not in list");
    py_raise(exc);
    if (exc) py_decref(exc);
    return -1;
}

int64_t py_list_count(PyObject *lst, PyObject *item) {
    if (lst == NULL) return 0;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t fast_count = 0;
        for (int64_t i = 0; i < fast->length; i++) {
            int equal = py_obj_eq(fast->items[i], item) != 0;
            if (py_err_occurred()) return 0;
            if (equal) fast_count++;
        }
        return fast_count;
    }
    PyObject *list_root = lst;
    PyObject *query_root = item;
    PyObject *candidate_root = NULL;
    void *list_handle = NULL;
    void *query_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return 0;
    if (list_prepare_moving_root(&query_root, &query_handle) != 0) {
        list_finish_moving_root(list_handle);
        return 0;
    }
    void *candidate_handle = pcc_gc_scheduler_root_register_handle(
        &candidate_root
    );
    if (candidate_handle == NULL) {
        list_finish_moving_root(query_handle);
        list_finish_moving_root(list_handle);
        return 0;
    }
    int64_t count = 0;
    int64_t index = 0;
    for (;;) {
        int in_range = 0;
        int equal = list_eq_at_callback(
            &list_root, list_handle, &query_root, query_handle,
            &candidate_root, index, &in_range
        );
        if (equal < 0) {
            pcc_gc_scheduler_root_unregister_handle(candidate_handle);
            list_finish_moving_root(query_handle);
            list_finish_moving_root(list_handle);
            return 0;
        }
        if (!in_range) break;
        if (equal) count++;
        index++;
    }
    pcc_gc_scheduler_root_unregister_handle(candidate_handle);
    list_finish_moving_root(query_handle);
    list_finish_moving_root(list_handle);
    return count;
}

void py_list_reverse(PyObject *lst) {
    if (lst == NULL) return;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        PyListObject *fast = (PyListObject *)lst;
        int64_t left_index = 0;
        int64_t right_index = fast->length - 1;
        while (left_index < right_index) {
            PyObject *left = fast->items[left_index];
            PyObject *right = fast->items[right_index];
            py_incref(left);
            py_incref(right);
            pcc_gc_store_ptr(
                (PyObject *)fast, &fast->items[left_index], right
            );
            pcc_gc_store_ptr(
                (PyObject *)fast, &fast->items[right_index], left
            );
            py_decref(left);
            py_decref(right);
            left_index++;
            right_index--;
        }
        return;
    }
    PyObject *list_root = lst;
    void *list_handle = NULL;
    if (list_prepare_moving_root(&list_root, &list_handle) != 0) return;
    lst = list_reload_moving_root(&list_root, list_handle);
    PyListObject *l = (PyListObject *)lst;
    int64_t i = 0;
    int64_t j = l->length - 1;
    PccGcRetainPlan left_plan;
    PccGcRetainPlan right_plan;
    while (i < j) {
        PccGcStoreRootPlan left_store_plan;
        PccGcStoreRootPlan right_store_plan;
        int64_t backend = pcc_gc_backend();
        pcc_gc_store_ptr_plan_init(
            &left_store_plan, list_root, backend
        );
        pcc_gc_store_ptr_plan_init(
            &right_store_plan, list_root, backend
        );
        pcc_gc_root_slot_lock();
        lst = list_reload_moving_root(&list_root, list_handle);
        l = (PyListObject *)lst;
        PyObject *left = pcc_gc_load_ptr(lst, &l->items[i]);
        PyObject *right = pcc_gc_load_ptr(lst, &l->items[j]);
        left = pcc_gc_retain_plan_prepare_locked(&left_plan, left);
        right = pcc_gc_retain_plan_prepare_locked(&right_plan, right);
        (void)pcc_gc_store_ptr_plan_commit_locked(
            &left_store_plan, lst, &l->items[i], right
        );
        (void)pcc_gc_store_ptr_plan_commit_locked(
            &right_store_plan, lst, &l->items[j], left
        );
        pcc_gc_root_slot_unlock();
        pcc_gc_store_ptr_plan_finish(&left_store_plan);
        pcc_gc_store_ptr_plan_finish(&right_store_plan);
        pcc_gc_retain_plan_finish(&left_plan);
        pcc_gc_retain_plan_finish(&right_plan);
        py_decref(left);
        py_decref(right);
        i++;
        j--;
    }
    list_finish_moving_root(list_handle);
}
