/* pcc/py_runtime/src/py_tuple.c
 *
 * Fixed-length immutable sequence of PyObject*.
 *
 * Phase 2 layout:
 *
 *   typedef struct {
 *       PyObjectHeader h;
 *       int64_t  len;
 *       PyObject *items[];   // flexible array of owned refs
 *   } PyTupleObject;
 *
 * The header and payload live in one contiguous allocation, which matches
 * the contract exactly (see docs/plans/python-frontend-interfaces.md §3).
 * Deallocation is centralized in py_obj.c's dispatch table, so all we do
 * here is allocate, set (during construction), get (with INCREF) and
 * return len.
 *
 * Reference-counting rules:
 *   - py_tuple_new(n) zeros items[]; caller populates via set_item.
 *   - py_tuple_set_item INCREFs the stored item. The contract says "does
 *     NOT INCREF because tuple owns" in the sense that the caller is NOT
 *     donating its own ref — we take a fresh one so the caller still owns
 *     theirs. This matches Phase-1 behavior; simpler than a steal-protocol
 *     and makes construction from borrowed refs safe.
 *   - py_tuple_get returns a fresh INCREF'd ref that the caller owns.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>

static int tuple_is_none_or_null(PyObject *o) {
    return o == NULL || o == py_None;
}

static int py_tuple_item_can_participate_in_cycle_with_depth(PyObject *o, int depth) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    if (depth < 0) depth = 0;
    if (depth > 64) return 0;

    int32_t tag = py_header(o)->type_tag;
    if (tag >= PY_TYPE_USER) return 1;
    switch (tag) {
        case PY_TYPE_LIST:
        case PY_TYPE_DICT:
        case PY_TYPE_SET:
        case PY_TYPE_FUNC:
        case PY_TYPE_CLASS:
        case PY_TYPE_INSTANCE:
        case PY_TYPE_EXC:
        case PY_TYPE_ITER:
        case PY_TYPE_GEN:
        case PY_TYPE_COROUTINE:
        case PY_TYPE_MEMORYVIEW:
        case PY_TYPE_WEAKREF:
            return 1;
        case PY_TYPE_TUPLE: {
            PyTupleObject *t = (PyTupleObject *)o;
            for (int64_t i = 0; i < t->len; i++) {
                PyObject *child = pcc_gc_load_ptr(o, &t->items[i]);
                if (py_tuple_item_can_participate_in_cycle_with_depth(child, depth + 1)) {
                    return 1;
                }
            }
            return 0;
        }
        default:
            return 0;
    }
}

static int py_tuple_item_can_participate_in_cycle(PyObject *o) {
    return py_tuple_item_can_participate_in_cycle_with_depth(o, 0);
}

PyObject *py_tuple_new(int64_t n) {
    if (n < 0) n = 0;
    /* One contiguous allocation: header + n * sizeof(PyObject*). */
    size_t bytes = sizeof(PyTupleObject) + (size_t)n * sizeof(PyObject *);
    PyTupleObject *t = (PyTupleObject *)pcc_gc_alloc(
        (int64_t)bytes, PY_TYPE_TUPLE, 0
    );
    if (t == NULL) return NULL;
    t->len        = n;
    if (n > 0) {
        memset(t->items, 0, (size_t)n * sizeof(PyObject *));
    } else {
        pcc_gc_publish_initialized((PyObject *)t);
    }
    return (PyObject *)t;
}

/* Build a new tuple from a pcc list's elements (returns a new ref).
 * Used by the dynamic-call lowering to normalize mixed ``f(a, *rest)``
 * argument lists to the tuple the native callable ABI requires. */
PyObject *py_tuple_from_list(PyObject *lst) {
    if (lst == NULL) return NULL;
    int64_t n = py_list_len(lst);
    PyObject *tup = py_tuple_new(n);
    if (tup == NULL) return NULL;
    for (int64_t i = 0; i < n; i++) {
        PyObject *elem = py_list_get(lst, i);  /* new ref */
        py_tuple_set_item(tup, i, elem);       /* increfs */
        py_decref(elem);
    }
    return tup;
}

/* Generic iterables need the iterator protocol, not len()/getitem(). */
static int tuple_collect_iterator(PyObject **slots) {
    pcc_gc_store_root_take(&slots[1], py_obj_iter(pcc_gc_load_ptr(NULL, &slots[0])));
    if (slots[1] == NULL) return 0;
    pcc_gc_store_root_take(&slots[2], py_list_new(0));
    if (slots[2] == NULL) goto no_memory;
    for (;;) {
        PyObject *item = py_obj_next(pcc_gc_load_ptr(NULL, &slots[1]));
        if (item == NULL) {
            if (py_err_occurred()) {
                if (!py_exc_matches(py_current_exception(),
                        (PyObject *)py_exc_builtin_class(PY_EXC_STOPITERATION))) return 0;
                py_clear_exception();
            }
            break;
        }
        pcc_gc_store_root_take(&slots[3], item);
        py_list_append(pcc_gc_load_ptr(NULL, &slots[2]), pcc_gc_load_ptr(NULL, &slots[3]));
        if (py_err_occurred()) return 0;
        pcc_gc_store_root(&slots[3], NULL);
    }
    int64_t n = py_list_len(pcc_gc_load_ptr(NULL, &slots[2]));
    pcc_gc_store_root_take(&slots[4], py_tuple_new(n));
    if (slots[4] == NULL) goto no_memory;
    for (int64_t i = 0; i < n; ++i) {
        pcc_gc_store_root_take(&slots[3], py_list_get(pcc_gc_load_ptr(NULL, &slots[2]), i));
        py_tuple_set_item(pcc_gc_load_ptr(NULL, &slots[4]), i, pcc_gc_load_ptr(NULL, &slots[3]));
        if (py_err_occurred()) return 0;
        pcc_gc_store_root(&slots[3], NULL);
    }
    return 1;
no_memory:
    py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "tuple: out of memory"));
    return 0;
}

static PyObject *tuple_from_iterable(PyObject *seq) {
    /* Root all owners across callbacks/collections; preserve errors on cleanup. */
    PyObject *slots[6] = {0};
    void *handles[6] = {0};
    for (int count = 0; count < 6; ++count) {
        handles[count] = pcc_gc_scheduler_root_register_handle(&slots[count]);
        if (handles[count] == NULL) {
            while (count > 0) pcc_gc_scheduler_root_unregister_handle(handles[--count]);
            py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "tuple: out of memory"));
            return NULL;
        }
    }
    pcc_gc_store_root(&slots[0], seq);
    int success = tuple_collect_iterator(slots);
    if (!success) {
        pcc_gc_store_root(&slots[5], py_current_exception());
        py_clear_exception();
    }
    for (int i = 0; i < 4; ++i) pcc_gc_store_root(&slots[i], NULL);
    PyObject *result = NULL;
    if (success) {
        result = pcc_gc_load_ptr(NULL, &slots[4]);
        py_incref(result);
    } else {
        py_raise(pcc_gc_load_ptr(NULL, &slots[5]));
    }
    pcc_gc_store_root(&slots[4], NULL);
    pcc_gc_store_root(&slots[5], NULL);
    for (int i = 0; i < 6; ++i) pcc_gc_scheduler_root_unregister_handle(handles[i]);
    return result;
}

PyObject *py_tuple_from_splat(PyObject *seq) {
    if (seq == NULL) return NULL;

    int32_t tag = py_type_of(seq);
    int64_t n = -1;
    if (tag == PY_TYPE_TUPLE) {
        n = py_tuple_len(seq);
    } else if (tag == PY_TYPE_LIST) {
        n = py_list_len(seq);
    } else {
        return tuple_from_iterable(seq);
    }
    if (n < 0) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "tuple() argument is not iterable"));
        return NULL;
    }

    PyObject *out = py_tuple_new(n);
    if (out == NULL) return NULL;

    if (tag == PY_TYPE_TUPLE) {
        PyTupleObject *src = (PyTupleObject *)seq;
        for (int64_t i = 0; i < n; i++) {
            PyObject *item = pcc_gc_load_ptr(seq, &src->items[i]);
            py_tuple_set_item(out, i, item);
        }
        return out;
    }
    if (tag == PY_TYPE_LIST) {
        PyListObject *src = (PyListObject *)seq;
        for (int64_t i = 0; i < n; i++) {
            PyObject *item = pcc_gc_load_ptr(seq, &src->items[i]);
            py_tuple_set_item(out, i, item);
        }
        return out;
    }

    return out;
}

void py_tuple_set_item(PyObject *tuple, int64_t i, PyObject *item) {
    /* Used during construction only. The slot is expected to be NULL —
     * we don't decref the old slot, only incref the new item so the
     * caller's ref is independent of the tuple's. */
    if (tuple == NULL) return;
    PyTupleObject *t = (PyTupleObject *)tuple;
    if (i < 0 || i >= t->len) return;
    if (pcc_gc_backend() == PCC_GC_KIND_REFCOUNT_CYCLE) {
        py_incref(item);
        t->items[i] = item;
    } else {
        pcc_gc_store_ptr(tuple, &t->items[i], item);
    }
    if (py_tuple_item_can_participate_in_cycle(item)) {
        int32_t flags = py_header(tuple)->flags;
        if ((flags & PY_FLAG_GC_TRACKED) == 0) {
            py_gc_track(tuple);
        }
    }
    /* Only colored relocation waits for complete payload publication.
     * Keep the store/ownership/tracking work above for every collector. */
    if (pcc_gc_backend() != PCC_GC_KIND_COLORED_RELOCATING) return;
    int complete = 1;
    for (int64_t slot_index = 0; slot_index < t->len; slot_index++) {
        if (pcc_gc_load_ptr(tuple, &t->items[slot_index]) == NULL) {
            complete = 0;
            break;
        }
    }
    if (complete) pcc_gc_publish_initialized(tuple);
}

PyObject *py_tuple_get(PyObject *tuple, int64_t i) {
    if (tuple == NULL) return NULL;
    PyTupleObject *t = (PyTupleObject *)tuple;
    /* Python allows negative indices. */
    if (i < 0) i += t->len;
    if (i < 0 || i >= t->len) {
        /* Non-raising: internal callers (tuple unpacking, capture/arg
         * extraction, weakref/re machinery) always pass in-range indices and
         * rely on the silent-NULL contract. User-level ``t[i]`` subscripts go
         * through py_tuple_getitem below, which raises IndexError. */
        return NULL;
    }
    PyObject *v = pcc_gc_load_ptr(tuple, &t->items[i]);
    py_incref(v);
    return v;
}

PyObject *py_tuple_get_known(PyObject *tuple, int64_t i) {
    return py_tuple_get(tuple, i);
}

/* t[i] subscript: like py_tuple_get but raises IndexError on out-of-range so a
 * surrounding try/except can catch it. py_tuple_get stays non-raising for the
 * many internal callers. Negative indices normalize like CPython. Mirrors the
 * py_list_get / py_list_getitem split in py_list.c. */
PyObject *py_tuple_getitem(PyObject *tuple, int64_t i) {
    if (tuple == NULL) return NULL;
    PyTupleObject *t = (PyTupleObject *)tuple;
    if (i < 0) i += t->len;
    if (i < 0 || i >= t->len) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "tuple index out of range"));
        return NULL;
    }
    PyObject *v = pcc_gc_load_ptr(tuple, &t->items[i]);
    py_incref(v);
    return v;
}

int64_t py_tuple_len(PyObject *tuple) {
    if (tuple == NULL) return 0;
    return ((PyTupleObject *)tuple)->len;
}

static int64_t tuple_slice_count(int64_t lo, int64_t hi, int64_t step) {
    int64_t count = 0;
    if (step > 0) {
        for (int64_t i = lo; i < hi; i += step) count++;
    } else {
        for (int64_t i = lo; i > hi; i += step) count++;
    }
    return count;
}

PyObject *py_tuple_slice(PyObject *tuple, PyObject *lo, PyObject *hi, PyObject *step) {
    if (tuple == NULL) return NULL;
    PyTupleObject *t = (PyTupleObject *)tuple;
    int64_t len = t->len;

    int64_t step_v = 1;
    if (!tuple_is_none_or_null(step)) {
        step_v = py_obj_index_i64(step);
        if (py_err_occurred()) return NULL;
        if (step_v == 0) {
            return NULL;
        }
    }

    int64_t lo_v, hi_v;
    if (step_v > 0) {
        lo_v = tuple_is_none_or_null(lo) ? 0   : py_obj_index_i64(lo);
        if (py_err_occurred()) return NULL;
        hi_v = tuple_is_none_or_null(hi) ? len : py_obj_index_i64(hi);
        if (py_err_occurred()) return NULL;
    } else {
        lo_v = tuple_is_none_or_null(lo) ? len - 1 : py_obj_index_i64(lo);
        if (py_err_occurred()) return NULL;
        hi_v = tuple_is_none_or_null(hi) ? -1      : py_obj_index_i64(hi);
        if (py_err_occurred()) return NULL;
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
            if (tuple_is_none_or_null(hi)) {
                hi_v = -1;
            } else {
                hi_v += len;
                if (hi_v < 0) hi_v = -1;
            }
        }
        if (hi_v >= len) hi_v = len - 1;
    }

    int64_t count = tuple_slice_count(lo_v, hi_v, step_v);
    PyObject *out = py_tuple_new(count);
    if (out == NULL) return NULL;

    int64_t j = 0;
    if (step_v > 0) {
        for (int64_t i = lo_v; i < hi_v; i += step_v) {
            PyObject *v = pcc_gc_load_ptr(tuple, &t->items[i]);
            py_tuple_set_item(out, j++, v);
        }
    } else {
        for (int64_t i = lo_v; i > hi_v; i += step_v) {
            if (i < 0 || i >= len) break;
            PyObject *v = pcc_gc_load_ptr(tuple, &t->items[i]);
            py_tuple_set_item(out, j++, v);
        }
    }
    return out;
}

PyObject *py_tuple_concat(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) return NULL;
    PyTupleObject *ta = (PyTupleObject *)a;
    PyTupleObject *tb = (PyTupleObject *)b;
    int64_t na = ta->len;
    int64_t nb = tb->len;
    PyObject *out = py_tuple_new(na + nb);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < na; i++) {
        PyObject *v = pcc_gc_load_ptr(a, &ta->items[i]);
        py_tuple_set_item(out, i, v);
    }
    for (int64_t j = 0; j < nb; j++) {
        PyObject *v = pcc_gc_load_ptr(b, &tb->items[j]);
        py_tuple_set_item(out, na + j, v);
    }
    return out;
}

PyObject *py_tuple_repeat(PyObject *tuple, int64_t count) {
    if (tuple == NULL) return NULL;
    PyTupleObject *t = (PyTupleObject *)tuple;
    int64_t n = t->len;
    int64_t repeats = count > 0 ? count : 0;
    PyObject *out = py_tuple_new(n * repeats);
    if (out == NULL) return NULL;
    int64_t dst = 0;
    for (int64_t k = 0; k < repeats; k++) {
        for (int64_t i = 0; i < n; i++) {
            PyObject *v = pcc_gc_load_ptr(tuple, &t->items[i]);
            py_tuple_set_item(out, dst++, v);
        }
    }
    return out;
}
