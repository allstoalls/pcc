/* pcc/py_runtime/src/py_dict.c
 *
 * Hash table with split probe table + insertion-ordered entries array.
 *
 * Design (compact-dict, PEP 468 / PEP 509):
 *
 *   indices : int64_t[capacity]
 *       Open-addressing probe slots. Each holds an index into entries[],
 *       or PY_DICT_EMPTY (-1) or PY_DICT_TOMBSTONE (-2).
 *
 *   entries : DictEntry[capacity]
 *       Append-only insertion order. Iteration walks this directly, so
 *       dict preserves insertion order. Deletion sets key=value=NULL and
 *       leaves a hole; rehash compacts. capacity * 2/3 is the soft cap.
 *
 *   entries_used is the high-water mark into entries[]. New inserts land
 *   at entries_used and then bump it. `size` counts only live entries.
 *
 * Invariants:
 *   - capacity is a power of 2 (lets us do `& (capacity - 1)` for modulo).
 *   - For every live entry at entries[j], exactly one cell in indices[]
 *     holds the value j; all other indices cells are EMPTY or TOMBSTONE.
 *   - For every dead entry (key == NULL), there may exist TOMBSTONE cells
 *     in indices[] pointing at it until the next rehash.
 *   - 0 <= size <= entries_used <= capacity.
 *
 * Probing: we do the classic CPython-style perturbation that mixes the
 * high bits back in, yielding a decent random-ish probe sequence without
 * paying for a full second hash:
 *     perturb = hash;
 *     j = hash & mask;
 *     while (!match) {
 *         perturb >>= 5;
 *         j = (j * 5 + perturb + 1) & mask;
 *     }
 *
 * Hash/equality:
 *   - key hash via py_obj_hash(k).
 *   - key equality via py_obj_eq(a, b).
 *   We cache `hash` in the entry to avoid recomputing on rehash.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <stdint.h>

#define PY_DICT_INITIAL_CAPACITY  8   /* must be power of 2 */

/* ---- Forward decls ---------------------------------------------------- */
static int  py_dict_rehash(PyDictObject *d, int64_t new_capacity);
static int  py_dict_maybe_grow(PyDictObject *d);

static int py_dict_pointer_can_have_header(PyObject *obj) {
    return pcc_gc_pointer_is_managed(obj) != 0;
}

static int py_object_is_dict(PyObject *obj) {
    if (!py_dict_pointer_can_have_header(obj)) return 0;
    return py_header(obj)->type_tag == PY_TYPE_DICT;
}

static int py_dict_prepare_moving_root(PyObject **slot, void **out_handle) {
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
    *out_handle = handle;
    return 0;
}

static PyObject *py_dict_reload_moving_root(
    PyObject **slot,
    void *handle
) {
    if (slot == NULL) return NULL;
    if (handle != NULL) *slot = pcc_gc_load_ptr(NULL, slot);
    return *slot;
}

static void py_dict_finish_moving_root(void *handle) {
    if (handle != NULL) pcc_gc_scheduler_root_unregister_handle(handle);
}

static PyObject *py_dict_entry_key(PyDictObject *d, DictEntry *e) {
    if (e->key == NULL) return NULL;
    if (__atomic_load_n(&pcc_gc_read_barrier_enabled, __ATOMIC_ACQUIRE) == 0) {
        return e->key;
    }
    return pcc_gc_load_ptr((PyObject *)d, &e->key);
}

static PyObject *py_dict_entry_value(PyDictObject *d, DictEntry *e) {
    if (e->value == NULL) return NULL;
    if (__atomic_load_n(&pcc_gc_read_barrier_enabled, __ATOMIC_ACQUIRE) == 0) {
        return e->value;
    }
    return pcc_gc_load_ptr((PyObject *)d, &e->value);
}

/* ---- Allocation ------------------------------------------------------- */

static int py_dict_alloc_tables(PyDictObject *d, int64_t capacity) {
    int64_t *indices = (int64_t *)malloc((size_t)capacity * sizeof(int64_t));
    if (indices == NULL) return -1;
    DictEntry *entries = (DictEntry *)malloc((size_t)capacity * sizeof(DictEntry));
    if (entries == NULL) {
        free(indices);
        return -1;
    }
    for (int64_t i = 0; i < capacity; i++) indices[i] = PY_DICT_EMPTY;
    /* entries[] is only read for 0..entries_used, so no need to init. */
    d->indices      = indices;
    d->entries      = entries;
    d->capacity     = capacity;
    d->size         = 0;
    d->entries_used = 0;
    (void)pcc_gc_backend4_zpage_register_owner_payload_span(
        (PyObject *)d,
        d->entries,
        capacity * (int64_t)sizeof(DictEntry)
    );
    return 0;
}

static PyObject *py_dict_new_with_capacity(int64_t capacity) {
    PyDictObject *d = (PyDictObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyDictObject), PY_TYPE_DICT, 0
    );
    if (d == NULL) return NULL;
    d->indices      = NULL;
    d->entries      = NULL;
    d->capacity     = 0;
    d->size         = 0;
    d->entries_used = 0;
    if (py_dict_alloc_tables(d, capacity) != 0) {
        py_decref((PyObject *)d);
        return NULL;
    }
    py_gc_track((PyObject *)d);
    pcc_gc_publish_initialized((PyObject *)d);
    return (PyObject *)d;
}

PyObject *py_dict_new(void) {
    return py_dict_new_with_capacity(PY_DICT_INITIAL_CAPACITY);
}

PyObject *py_dict_new_presized(int64_t expected_items) {
    if (expected_items > INT64_C(0x10000000000000)) return NULL;
    int64_t capacity = 4;
    while (expected_items > capacity * 2 / 3) capacity *= 2;
    return py_dict_new_with_capacity(capacity);
}

/* ---- Probing ---------------------------------------------------------- */

/* Publish a fresh (key, value) pair plus its index and size under one graph
 * lock.  Key and value each need their own store plan: a plan commits exactly
 * one slot.  Both plans are initialized before the lock and finished after it,
 * so an incref/decref finalizer can only observe the fully published table. */
static int py_dict_insert_rooted_slot(
    PyObject **dict_storage,
    void *dict_handle,
    PyObject **key_storage,
    void *key_handle,
    PyObject **value_storage,
    void *value_handle,
    int64_t *indices,
    DictEntry *entries,
    int64_t capacity,
    int64_t entries_used,
    int64_t slot,
    int64_t hash
) {
    PyObject *dict = py_dict_reload_moving_root(dict_storage, dict_handle);
    (void)py_dict_reload_moving_root(key_storage, key_handle);
    (void)py_dict_reload_moving_root(value_storage, value_handle);
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan key_plan;
    PccGcStoreRootPlan value_plan;
    pcc_gc_store_ptr_plan_init(&key_plan, dict, backend);
    pcc_gc_store_ptr_plan_init(&value_plan, dict, backend);
    pcc_gc_root_slot_lock();
    dict = py_dict_reload_moving_root(dict_storage, dict_handle);
    PyObject *key = py_dict_reload_moving_root(key_storage, key_handle);
    PyObject *value = py_dict_reload_moving_root(value_storage, value_handle);
    int committed = 0;
    if (py_object_is_dict(dict)) {
        PyDictObject *d = (PyDictObject *)dict;
        if (
            d->indices == indices && d->entries == entries
            && d->capacity == capacity && d->entries_used == entries_used
            && slot >= 0 && slot < capacity
            && entries_used >= 0 && entries_used < capacity
            && (indices[slot] == PY_DICT_EMPTY
                || indices[slot] == PY_DICT_TOMBSTONE)
        ) {
            int64_t ei = entries_used;
            DictEntry *entry = &entries[ei];
            entry->hash  = hash;
            entry->key   = NULL;
            entry->value = NULL;
            int key_ok = pcc_gc_store_ptr_plan_commit_locked(
                &key_plan, dict, &entry->key, key
            ) != 0;
            int value_ok = pcc_gc_store_ptr_plan_commit_locked(
                &value_plan, dict, &entry->value, value
            ) != 0;
            if (key_ok && value_ok) {
                d->entries_used = ei + 1;
                indices[slot] = ei;
                d->size++;
                committed = 1;
            } else {
                /* The entry was never indexed, so it is unreachable; the
                 * plans' finish still balances any partial store. */
                entry->hash = 0;
            }
        }
    }
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&key_plan);
    pcc_gc_store_ptr_plan_finish(&value_plan);
    if (committed) {
        dict = py_dict_reload_moving_root(dict_storage, dict_handle);
        if (py_object_is_dict(dict)) {
            (void)py_dict_maybe_grow((PyDictObject *)dict);
        }
    }
    return committed;
}

/* Replace the value of an existing entry.  `dict[k] = v` keeps the original
 * stored key object, so the key slot is never written here.  The displaced
 * value's release runs in plan finish, after the lock is dropped. */
static int py_dict_replace_value_rooted_slot(
    PyObject **dict_storage,
    void *dict_handle,
    PyObject **value_storage,
    void *value_handle,
    int64_t *indices,
    DictEntry *entries,
    int64_t capacity,
    int64_t slot,
    int64_t ix,
    int64_t hash
) {
    PyObject *dict = py_dict_reload_moving_root(dict_storage, dict_handle);
    (void)py_dict_reload_moving_root(value_storage, value_handle);
    PccGcStoreRootPlan plan;
    pcc_gc_store_ptr_plan_init(&plan, dict, pcc_gc_backend());
    pcc_gc_root_slot_lock();
    dict = py_dict_reload_moving_root(dict_storage, dict_handle);
    PyObject *value = py_dict_reload_moving_root(value_storage, value_handle);
    int committed = 0;
    if (py_object_is_dict(dict)) {
        PyDictObject *d = (PyDictObject *)dict;
        if (
            d->indices == indices && d->entries == entries
            && d->capacity == capacity
            && slot >= 0 && slot < capacity && indices[slot] == ix
            && ix >= 0 && ix < d->entries_used
            && entries[ix].hash == hash
            && entries[ix].key != NULL
        ) {
            DictEntry *entry = &entries[ix];
            committed = pcc_gc_store_ptr_plan_commit_locked(
                &plan, dict, &entry->value, value
            ) != 0;
        }
    }
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&plan);
    return committed;
}

/* Detach an entry: key, value, index tombstone and size all publish under one
 * graph lock, and both releases run in plan finish after unlock.  The legacy
 * path decref'd key and value first, so a finalizer re-entering the dict could
 * observe a freed key behind a still-live index. */
static int py_dict_del_rooted_slot(
    PyObject **dict_storage,
    void *dict_handle,
    int64_t *indices,
    DictEntry *entries,
    int64_t capacity,
    int64_t slot,
    int64_t ix
) {
    PyObject *dict = py_dict_reload_moving_root(dict_storage, dict_handle);
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan key_plan;
    PccGcStoreRootPlan value_plan;
    pcc_gc_store_ptr_plan_init(&key_plan, dict, backend);
    pcc_gc_store_ptr_plan_init(&value_plan, dict, backend);
    pcc_gc_root_slot_lock();
    dict = py_dict_reload_moving_root(dict_storage, dict_handle);
    int committed = 0;
    if (py_object_is_dict(dict)) {
        PyDictObject *d = (PyDictObject *)dict;
        if (
            d->indices == indices && d->entries == entries
            && d->capacity == capacity
            && slot >= 0 && slot < capacity && indices[slot] == ix
            && ix >= 0 && ix < d->entries_used
            && entries[ix].key != NULL
        ) {
            DictEntry *entry = &entries[ix];
            int key_ok = pcc_gc_store_ptr_plan_commit_locked(
                &key_plan, dict, &entry->key, NULL
            ) != 0;
            int value_ok = pcc_gc_store_ptr_plan_commit_locked(
                &value_plan, dict, &entry->value, NULL
            ) != 0;
            if (key_ok && value_ok) {
                indices[slot] = PY_DICT_TOMBSTONE;
                d->size--;
                committed = 1;
            }
        }
    }
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&key_plan);
    pcc_gc_store_ptr_plan_finish(&value_plan);
    return committed;
}

/* mode 0: get, returning an owned value.  mode 1: delete.  mode 2: set —
 * fresh insert or value replacement.  Modes 1 and 2 return NULL and
 * report through *out_status. */
static PyObject *py_dict_rooted_op(
    PyObject *dict,
    PyObject *key,
    PyObject *value,
    int mode,
    int *out_status
) {
    if (out_status != NULL) *out_status = 0;
    PyObject *dict_storage = dict;
    PyObject *key_storage = key;
    PyObject *value_storage = value;
    void *dict_handle = NULL;
    void *key_handle = NULL;
    void *value_handle = NULL;
    if (py_dict_prepare_moving_root(&dict_storage, &dict_handle) != 0) {
        return NULL;
    }
    if (py_dict_prepare_moving_root(&key_storage, &key_handle) != 0) {
        py_dict_finish_moving_root(dict_handle);
        return NULL;
    }
    if (py_dict_prepare_moving_root(&value_storage, &value_handle) != 0) {
        py_dict_finish_moving_root(key_handle);
        py_dict_finish_moving_root(dict_handle);
        return NULL;
    }

    key = py_dict_reload_moving_root(&key_storage, key_handle);
    int64_t hash = py_obj_hash(key);
    dict = py_dict_reload_moving_root(&dict_storage, dict_handle);
    key = py_dict_reload_moving_root(&key_storage, key_handle);
    if (py_err_occurred()) {
        /* value_handle is already registered by this point, so a raising
         * __hash__ must release all three or it leaks a scheduler root and
         * keeps the value alive under GC3/GC4. */
        py_dict_finish_moving_root(value_handle);
        py_dict_finish_moving_root(key_handle);
        py_dict_finish_moving_root(dict_handle);
        return NULL;
    }

    int restarts = 0;
restart:
    if (restarts++ >= 16) {
        py_dict_finish_moving_root(value_handle);
        py_dict_finish_moving_root(key_handle);
        py_dict_finish_moving_root(dict_handle);
        return NULL;
    }
    dict = py_dict_reload_moving_root(&dict_storage, dict_handle);
    key = py_dict_reload_moving_root(&key_storage, key_handle);
    if (!py_object_is_dict(dict)) {
        py_dict_finish_moving_root(value_handle);
        py_dict_finish_moving_root(key_handle);
        py_dict_finish_moving_root(dict_handle);
        return NULL;
    }
    PyDictObject *d = (PyDictObject *)dict;
    if (d->capacity <= 0 || d->indices == NULL || d->entries == NULL) {
        py_dict_finish_moving_root(value_handle);
        py_dict_finish_moving_root(key_handle);
        py_dict_finish_moving_root(dict_handle);
        return NULL;
    }
    int64_t capacity = d->capacity;
    int64_t *indices = d->indices;
    DictEntry *entries = d->entries;
    int64_t entries_used = d->entries_used;
    int64_t mask = capacity - 1;
    uint64_t perturb = (uint64_t)hash;
    int64_t j = (int64_t)((uint64_t)hash & (uint64_t)mask);
    int64_t probes = 0;
    /* See py_dict.py: capacity * 2 is not a sufficient probe budget. */
    int64_t limit = capacity + 16;
    int64_t first_tombstone = -1;
    int64_t insert_slot = -1;
    while (probes < limit) {
        int64_t ix = indices[j];
        if (ix == PY_DICT_EMPTY) {
            insert_slot = (first_tombstone >= 0) ? first_tombstone : j;
            break;
        }
        if (ix == PY_DICT_TOMBSTONE) {
            if (first_tombstone < 0) first_tombstone = j;
        } else if (ix >= 0 && ix < d->entries_used) {
            DictEntry *entry = &entries[ix];
            PyObject *entry_key = py_dict_entry_key(d, entry);
            if (entry_key != NULL && entry->hash == hash) {
                int equal = 0;
                int callback = 0;
                if (entry_key == key) equal = 1;
                else if (PY_IS_TAGGED_INT(entry_key) && PY_IS_TAGGED_INT(key)) {
                    equal = 0;
                } else if (
                    !PY_IS_TAGGED_INT(entry_key)
                    && !PY_IS_TAGGED_INT(key)
                    && py_header(entry_key)->type_tag == PY_TYPE_STR
                    && py_header(key)->type_tag == PY_TYPE_STR
                ) {
                    equal = py_str_eq(entry_key, key) != 0;
                } else {
                    callback = 1;
                    py_incref(entry_key);
                    PyObject *candidate_storage = entry_key;
                    void *candidate_handle = NULL;
                    if (py_dict_prepare_moving_root(
                            &candidate_storage, &candidate_handle
                        ) != 0) {
                        py_decref(entry_key);
                        break;
                    }
                    PyObject *before_dict = dict;
                    equal = py_obj_eq(candidate_storage, key) != 0;
                    dict = py_dict_reload_moving_root(
                        &dict_storage, dict_handle
                    );
                    key = py_dict_reload_moving_root(
                        &key_storage, key_handle
                    );
                    PyObject *candidate = py_dict_reload_moving_root(
                        &candidate_storage, candidate_handle
                    );
                    py_dict_finish_moving_root(candidate_handle);
                    int stable = dict == before_dict
                        && py_object_is_dict(dict)
                        && ((PyDictObject *)dict)->capacity == capacity
                        && ((PyDictObject *)dict)->indices == indices
                        && ((PyDictObject *)dict)->entries == entries
                        && j < capacity
                        && indices[j] == ix
                        && ix >= 0
                        && ix < ((PyDictObject *)dict)->entries_used
                        && py_dict_entry_key(
                            (PyDictObject *)dict, &entries[ix]
                        ) == candidate;
                    py_decref(candidate);
                    if (py_err_occurred()) {
                        /* A raising __eq__ leaves py_obj_eq returning 0.
                         * Treating that as "not equal" would keep probing and,
                         * in set mode, insert -- mutating the dict even though
                         * the statement raises.  Abort without committing. */
                        py_dict_finish_moving_root(value_handle);
                        py_dict_finish_moving_root(key_handle);
                        py_dict_finish_moving_root(dict_handle);
                        return NULL;
                    }
                    if (!stable) goto restart;
                }
                (void)callback;
                if (equal) {
                    if (mode == 1) {
                        int removed = py_dict_del_rooted_slot(
                            &dict_storage,
                            dict_handle,
                            indices,
                            entries,
                            capacity,
                            j,
                            ix
                        );
                        if (!removed) goto restart;
                        if (out_status != NULL) *out_status = 1;
                        py_dict_finish_moving_root(value_handle);
                        py_dict_finish_moving_root(key_handle);
                        py_dict_finish_moving_root(dict_handle);
                        return NULL;
                    }
                    if (mode == 2) {
                        int replaced = py_dict_replace_value_rooted_slot(
                            &dict_storage,
                            dict_handle,
                            &value_storage,
                            value_handle,
                            indices,
                            entries,
                            capacity,
                            j,
                            ix,
                            hash
                        );
                        if (!replaced) goto restart;
                        if (out_status != NULL) *out_status = 1;
                        py_dict_finish_moving_root(value_handle);
                        py_dict_finish_moving_root(key_handle);
                        py_dict_finish_moving_root(dict_handle);
                        return NULL;
                    }
                    PyObject *found = py_dict_entry_value(
                        (PyDictObject *)dict, &entries[ix]
                    );
                    if (found != NULL) py_incref(found);
                    py_dict_finish_moving_root(value_handle);
                    py_dict_finish_moving_root(key_handle);
                    py_dict_finish_moving_root(dict_handle);
                    return found;
                }
            }
        }
        perturb >>= 5;
        j = (int64_t)(((uint64_t)j * 5u + perturb + 1u) & (uint64_t)mask);
        probes++;
    }
    if (mode == 2) {
        int64_t target = insert_slot >= 0 ? insert_slot : first_tombstone;
        if (target >= 0) {
            int inserted = py_dict_insert_rooted_slot(
                &dict_storage,
                dict_handle,
                &key_storage,
                key_handle,
                &value_storage,
                value_handle,
                indices,
                entries,
                capacity,
                entries_used,
                target,
                hash
            );
            if (!inserted) goto restart;
            if (out_status != NULL) *out_status = 1;
        }
    }
    py_dict_finish_moving_root(value_handle);
    py_dict_finish_moving_root(key_handle);
    py_dict_finish_moving_root(dict_handle);
    return NULL;
}

/* Rebuild indices[] and compact entries[] into a new capacity. `new_capacity`
 * must be a power of 2 and large enough to hold all live entries at load
 * factor < 2/3. */
static int64_t py_dict_rehash_find_empty_slot(
    int64_t *indices,
    int64_t capacity,
    int64_t hash
) {
    int64_t mask = capacity - 1;
    uint64_t perturb = (uint64_t)hash;
    int64_t slot = (int64_t)((uint64_t)hash & (uint64_t)mask);
    int64_t probes = 0;
    while (probes < capacity + 16) {
        if (indices[slot] == PY_DICT_EMPTY) return slot;
        perturb >>= 5;
        slot = (int64_t)(
            ((uint64_t)slot * 5u + perturb + 1u) & (uint64_t)mask
        );
        probes++;
    }
    return -1;
}

static int py_dict_rehash_refcount_fast(
    PyDictObject *d,
    int64_t new_capacity
) {
    DictEntry *old_entries = d->entries;
    int64_t *old_indices = d->indices;
    int64_t old_entries_used = d->entries_used;
    DictEntry *new_entries = (DictEntry *)calloc(
        (size_t)new_capacity, sizeof(DictEntry)
    );
    int64_t *new_indices = (int64_t *)malloc(
        (size_t)new_capacity * sizeof(int64_t)
    );
    if (new_entries == NULL || new_indices == NULL) {
        free(new_entries);
        free(new_indices);
        return -1;
    }
    for (int64_t i = 0; i < new_capacity; i++) {
        new_indices[i] = PY_DICT_EMPTY;
    }
    int64_t new_entries_used = 0;
    for (int64_t i = 0; i < old_entries_used; i++) {
        DictEntry *old_entry = &old_entries[i];
        if (old_entry->key == NULL) continue;
        int64_t slot = py_dict_rehash_find_empty_slot(
            new_indices, new_capacity, old_entry->hash
        );
        if (slot < 0) {
            free(new_entries);
            free(new_indices);
            return -1;
        }
        new_entries[new_entries_used] = *old_entry;
        new_indices[slot] = new_entries_used;
        new_entries_used++;
    }
    d->indices = new_indices;
    d->entries = new_entries;
    d->capacity = new_capacity;
    d->size = new_entries_used;
    d->entries_used = new_entries_used;
    free(old_indices);
    free(old_entries);
    return 0;
}

static int py_dict_rehash(PyDictObject *d, int64_t new_capacity) {
    if (d == NULL || new_capacity <= 0) return -1;
    int64_t initial_backend = pcc_gc_backend();
    if (initial_backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        return py_dict_rehash_refcount_fast(d, new_capacity);
    }
    PyObject *owner_slot = (PyObject *)d;
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
        d = (PyDictObject *)owner_slot;
        DictEntry *old_entries = d->entries;
        int64_t *old_indices = d->indices;
        int64_t old_capacity = d->capacity;
        int64_t old_entries_used = d->entries_used;
        int64_t old_size = d->size;
        pcc_gc_root_slot_unlock();
        if (
            old_entries == NULL
            || old_indices == NULL
            || old_capacity <= 0
            || old_entries_used < 0
            || old_entries_used > old_capacity
            || new_capacity < old_capacity
            || old_size < 0
            || old_size > new_capacity
            || old_entries_used > INT64_MAX / 4
        ) break;

        DictEntry *new_entries = (DictEntry *)calloc(
            (size_t)new_capacity, sizeof(DictEntry)
        );
        int64_t *new_indices = (int64_t *)malloc(
            (size_t)new_capacity * sizeof(int64_t)
        );
        PyObject ***slot_pairs = NULL;
        if (old_entries_used > 0) {
            slot_pairs = (PyObject ***)calloc(
                (size_t)old_entries_used * 4u,
                sizeof(PyObject **)
            );
        }
        if (
            new_entries == NULL
            || new_indices == NULL
            || (old_entries_used > 0 && slot_pairs == NULL)
        ) {
            free(slot_pairs);
            free(new_entries);
            free(new_indices);
            break;
        }
        for (int64_t i = 0; i < new_capacity; i++) {
            new_indices[i] = PY_DICT_EMPTY;
        }

        pcc_gc_root_slot_lock();
        if (pcc_gc_backend() != initial_backend) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_entries);
            free(new_indices);
            break;
        }
        if (owner_handle != NULL) {
            owner_slot = pcc_gc_load_ptr(NULL, &owner_slot);
        }
        d = (PyDictObject *)owner_slot;
        if (
            d->entries != old_entries
            || d->indices != old_indices
            || d->capacity != old_capacity
            || d->entries_used != old_entries_used
            || d->size != old_size
        ) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_entries);
            free(new_indices);
            continue;
        }

        int64_t new_entries_used = 0;
        int64_t new_size = 0;
        int64_t pair_count = 0;
        int copy_valid = 1;
        for (int64_t i = 0; i < old_entries_used; i++) {
            DictEntry *old_entry = &old_entries[i];
            PyObject *entry_key = py_dict_entry_key(d, old_entry);
            if (entry_key == NULL) continue;
            PyObject *entry_value = py_dict_entry_value(d, old_entry);
            int64_t slot = py_dict_rehash_find_empty_slot(
                new_indices, new_capacity, old_entry->hash
            );
            if (slot < 0) {
                copy_valid = 0;
                break;
            }
            DictEntry *new_entry = &new_entries[new_entries_used];
            new_entry->hash = old_entry->hash;
            new_entry->key = entry_key;
            new_entry->value = entry_value;
            new_indices[slot] = new_entries_used;
            slot_pairs[pair_count * 2] = &old_entry->key;
            slot_pairs[pair_count * 2 + 1] = &new_entry->key;
            pair_count++;
            slot_pairs[pair_count * 2] = &old_entry->value;
            slot_pairs[pair_count * 2 + 1] = &new_entry->value;
            pair_count++;
            new_entries_used++;
            new_size++;
        }
        int64_t retargeted = 0;
        if (copy_valid) {
            retargeted = pcc_gc_backend4_retarget_mutator_payload_locked(
                (PyObject *)d,
                old_entries,
                old_capacity * (int64_t)sizeof(DictEntry),
                new_entries,
                new_capacity * (int64_t)sizeof(DictEntry),
                slot_pairs,
                pair_count
            );
        }
        if (!copy_valid || retargeted == 0) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_entries);
            free(new_indices);
            break;
        }
        for (int64_t i = 0; i < pair_count; i++) {
            PyObject **new_slot = slot_pairs[i * 2 + 1];
            pcc_gc_note_slot_write_barrier(
                (PyObject *)d, new_slot, *new_slot
            );
        }
        d->indices = new_indices;
        d->entries = new_entries;
        d->capacity = new_capacity;
        d->size = new_size;
        d->entries_used = new_entries_used;
        if (retargeted == 2) {
            (void)pcc_gc_backend4_zpage_register_owner_payload_span(
                (PyObject *)d,
                new_entries,
                new_capacity * (int64_t)sizeof(DictEntry)
            );
        }
        pcc_gc_root_slot_unlock();

        free(old_indices);
        free(old_entries);
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

/* Decide the next capacity when we need to grow (or shrink). CPython uses
 * `capacity <<= (size > 50000 ? 1 : 2)`; we keep it simple — double when
 * we exceed 2/3 fill, hold otherwise. */
static int py_dict_maybe_grow(PyDictObject *d) {
    /* entries_used counts both live and tombstoned slots; once we're past
     * 2/3 fill of capacity, rehash. Choose new capacity = capacity * 2 if
     * the table is actually dense with live entries, else keep the same
     * capacity (just compact the tombstones). */
    int64_t threshold = (d->capacity * 2) / 3;
    if (d->entries_used <= threshold) return 0;

    int64_t new_cap = d->capacity;
    if (d->size > threshold / 2) {
        /* Most of the fill is real data → grow. */
        new_cap = d->capacity * 2;
    }
    return py_dict_rehash(d, new_cap);
}

/* ---- Public API ------------------------------------------------------- */

void py_dict_set(PyObject *dict, PyObject *key, PyObject *value) {
    if (!py_object_is_dict(dict) || key == NULL) return;
    (void)py_dict_rooted_op(dict, key, value, 2, NULL);
}

PyObject *py_dict_get(PyObject *dict, PyObject *key) {
    if (!py_object_is_dict(dict) || key == NULL) return NULL;
    return py_dict_rooted_op(dict, key, NULL, 0, NULL);
}

PyObject *py_dict_get_default(PyObject *dict, PyObject *key, PyObject *def) {
    PyObject *v = py_dict_get(dict, key);
    if (v != NULL) return v;
    if (py_err_occurred()) return NULL;
    py_incref(def);
    return def;
}

/* d[key] subscript: like py_dict_get but raises KeyError (carrying the key,
 * like CPython) when the key is absent, so a surrounding try/except can catch
 * it. py_dict_get stays non-raising for dict.get()/setdefault()/etc. */
PyObject *py_dict_getitem(PyObject *dict, PyObject *key) {
    PyObject *v = py_dict_get(dict, key);
    if (v == NULL) {
        if (py_err_occurred()) return NULL;
        PyObject *exc = py_exc_new_with_value(PY_EXC_KEYERROR, key);
        py_raise(exc);
        if (exc) py_decref(exc);
        return NULL;
    }
    return v;
}

/* dict.fromkeys(iterable, value): new dict with each element of iterable as a
 * key mapped to value (caller passes None when omitted). Iterator protocol;
 * clears a terminal StopIteration like the sorted() iterator path. */
PyObject *py_dict_fromkeys(PyObject *iterable, PyObject *value) {
    PyObject *d = py_dict_new();
    if (d == NULL) return NULL;
    PyObject *it = py_obj_iter(iterable);
    if (it == NULL) {
        py_runtime_error_if_unset(
            "py_obj_iter",
            "dict.fromkeys could not create an iterator"
        );
        py_decref(d);
        return NULL;
    }
    for (;;) {
        PyObject *k = py_obj_next(it);
        if (k == NULL) {
            if (py_err_occurred()) {
                PyObject *cur = py_current_exception();
                PyObject *stop = py_exc_builtin_class(PY_EXC_STOPITERATION);
                if (py_exc_matches(cur, stop)) {
                    py_clear_exception();
                    break;
                }
            } else {
                py_runtime_error_if_unset(
                    "py_obj_next",
                    "dict.fromkeys iterator returned NULL without an exception"
                );
            }
            py_decref(it);
            py_decref(d);
            return NULL;
        }
        py_dict_set(d, k, value);
        if (py_err_occurred()) {
            py_decref(k);
            py_decref(it);
            py_decref(d);
            return NULL;
        }
        py_decref(k);
    }
    py_decref(it);
    return d;
}

PyObject *py_dict_pop(PyObject *dict, PyObject *key) {
    PyObject *v = py_dict_get(dict, key);
    if (v == NULL) {
        if (py_err_occurred()) return NULL;
        PyObject *exc = py_exc_new_with_value(PY_EXC_KEYERROR, key);
        py_raise(exc);
        if (exc) py_decref(exc);
        return NULL;
    }
    (void)py_dict_del(dict, key);
    return v;
}

/* dict.popitem(): remove and return the LAST-inserted (key, value) pair as a
 * 2-tuple (dicts are insertion-ordered). Raises KeyError when empty. The tuple
 * increfs key/value (py_tuple_set_item), so py_dict_del's decref leaves them
 * owned by the returned tuple. */
PyObject *py_dict_popitem(PyObject *dict) {
    if (!py_object_is_dict(dict)) return NULL;
    PyDictObject *d = (PyDictObject *)dict;
    for (int64_t i = d->entries_used - 1; i >= 0; i--) {
        DictEntry *e = &d->entries[i];
        PyObject *key = py_dict_entry_key(d, e);
        if (key == NULL) continue;           /* dead slot */
        PyObject *val = py_dict_entry_value(d, e);
        PyObject *tup = py_tuple_new(2);
        if (tup == NULL) return NULL;
        py_tuple_set_item(tup, 0, key);
        py_tuple_set_item(tup, 1, val);
        (void)py_dict_del(dict, key);
        return tup;
    }
    /* Empty dict: CPython raises KeyError('popitem(): dictionary is empty');
     * we raise a bare KeyError (consistent with the pcc-Python port tier,
     * which has no cstr message helper here). Rare error path. */
    PyObject *exc = py_exc_new_with_value(PY_EXC_KEYERROR, NULL);
    py_raise(exc);
    if (exc) py_decref(exc);
    return NULL;
}

int64_t py_dict_contains(PyObject *dict, PyObject *key) {
    PyObject *value = py_dict_get(dict, key);
    if (value == NULL) return 0;
    py_decref(value);
    return 1;
}

int64_t py_dict_del(PyObject *dict, PyObject *key) {
    if (!py_object_is_dict(dict) || key == NULL) return -1;
    int status = 0;
    (void)py_dict_rooted_op(dict, key, NULL, 1, &status);
    return status ? 0 : -1;
}

void py_dict_clear(PyObject *dict) {
    if (!py_object_is_dict(dict)) return;
    PyDictObject *d = (PyDictObject *)dict;
    for (int64_t i = 0; i < d->entries_used; i++) {
        DictEntry *e = &d->entries[i];
        PyObject *key = py_dict_entry_key(d, e);
        if (key == NULL) continue;
        py_decref(key);
        py_decref(py_dict_entry_value(d, e));
        e->hash = 0;
        e->key = NULL;
        e->value = NULL;
    }
    for (int64_t i = 0; i < d->capacity; i++) {
        d->indices[i] = PY_DICT_EMPTY;
    }
    d->size = 0;
    d->entries_used = 0;
}

int64_t py_dict_len(PyObject *dict) {
    if (!py_object_is_dict(dict)) return 0;
    return ((PyDictObject *)dict)->size;
}

int64_t py_dict_entries_used(PyObject *dict) {
    if (!py_object_is_dict(dict)) return 0;
    return ((PyDictObject *)dict)->entries_used;
}

PyObject *py_dict_entry_key_at(PyObject *dict, int64_t i) {
    if (!py_object_is_dict(dict) || i < 0) return NULL;
    PyDictObject *d = (PyDictObject *)dict;
    if (i >= d->entries_used) return NULL;
    PyObject *key = py_dict_entry_key(d, &d->entries[i]);
    if (key != NULL) py_incref(key);
    return key;
}

PyObject *py_dict_entry_value_at(PyObject *dict, int64_t i) {
    if (!py_object_is_dict(dict) || i < 0) return NULL;
    PyDictObject *d = (PyDictObject *)dict;
    if (i >= d->entries_used) return NULL;
    DictEntry *e = &d->entries[i];
    PyObject *key = py_dict_entry_key(d, e);
    if (key == NULL) return NULL;
    PyObject *value = py_dict_entry_value(d, e);
    if (value != NULL) py_incref(value);
    return value;
}

PyObject *py_dict_keys(PyObject *dict) {
    if (!py_object_is_dict(dict)) return NULL;
    PyDictObject *d = (PyDictObject *)dict;
    PyObject *out = py_list_new(d->size > 0 ? d->size : 4);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < d->entries_used; i++) {
        DictEntry *e = &d->entries[i];
        PyObject *key = py_dict_entry_key(d, e);
        if (key == NULL) continue;
        py_list_append(out, key);
    }
    return out;
}

PyObject *py_dict_values(PyObject *dict) {
    if (!py_object_is_dict(dict)) return NULL;
    PyDictObject *d = (PyDictObject *)dict;
    PyObject *out = py_list_new(d->size > 0 ? d->size : 4);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < d->entries_used; i++) {
        DictEntry *e = &d->entries[i];
        PyObject *key = py_dict_entry_key(d, e);
        if (key == NULL) continue;
        py_list_append(out, py_dict_entry_value(d, e));
    }
    return out;
}

PyObject *py_dict_items(PyObject *dict) {
    if (!py_object_is_dict(dict)) return NULL;
    PyDictObject *d = (PyDictObject *)dict;
    PyObject *out = py_list_new(d->size > 0 ? d->size : 4);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < d->entries_used; i++) {
        DictEntry *e = &d->entries[i];
        PyObject *key = py_dict_entry_key(d, e);
        if (key == NULL) continue;
        PyObject *value = py_dict_entry_value(d, e);
        /* Build a 2-tuple (key, value) for each live entry. */
        PyObject *pair = py_tuple_new(2);
        if (pair == NULL) {
            py_decref(out);
            return NULL;
        }
        py_tuple_set_item(pair, 0, key);
        py_tuple_set_item(pair, 1, value);
        py_list_append(out, pair);
        py_decref(pair);  /* list took its own ref */
    }
    return out;
}

static int py_dict_update_hold(PyObject **slots, void **handles,
                               int index, PyObject *value) {
    slots[index] = value;
    if (py_dict_prepare_moving_root(&slots[index], &handles[index]) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR,
                                 "dict.update: cannot root temporary"));
        return 0;
    }
    return 1;
}

static PyObject *py_dict_update_load(PyObject **slots, void **handles, int index) {
    return py_dict_reload_moving_root(&slots[index], handles[index]);
}

static void py_dict_update_drop(PyObject **slots, void **handles, int index) {
    PyObject *value = py_dict_update_load(slots, handles, index);
    py_dict_finish_moving_root(handles[index]);
    handles[index] = NULL;
    slots[index] = NULL;
    if (index >= 2) py_decref(value);
}

static void py_dict_update_protocol(PyObject *dst, PyObject *src) {
    /* Destination/source are borrowed; the remaining slots own temporaries.
     * Mirrors _dict_update_protocol in the pcc-Python runtime. */
    PyObject *slots[7] = {NULL};
    void *handles[7] = {NULL};
    int ok = py_dict_update_hold(slots, handles, 0, dst);
    if (ok) ok = py_dict_update_hold(slots, handles, 1, src);
    int mapping = 0;
    if (ok) {
        PyObject *method = py_obj_getattr(py_dict_update_load(slots, handles, 1), "keys");
        if (method == NULL) {
            if (py_err_occurred()) {
                if (py_exc_matches(py_current_exception(), py_exc_builtin_class(PY_EXC_ATTRIBUTEERROR)))
                    py_clear_exception();
                else ok = 0;
            }
        } else {
            mapping = 1;
            ok = py_dict_update_hold(slots, handles, 3, method);
        }
    }
    if (ok && mapping) {
        PyObject *args = py_tuple_new(0);
        ok = py_dict_update_hold(slots, handles, 4, args);
        if (args == NULL) ok = 0;
        if (ok) {
            PyObject *keys = py_obj_call(py_dict_update_load(slots, handles, 3),
                                        py_dict_update_load(slots, handles, 4), NULL);
            ok = py_dict_update_hold(slots, handles, 5, keys);
            if (keys == NULL) ok = 0;
        }
    }
    if (ok) {
        PyObject *source = py_dict_update_load(slots, handles, mapping ? 5 : 1);
        PyObject *iterator = py_obj_iter(source);
        ok = py_dict_update_hold(slots, handles, 2, iterator);
        if (iterator == NULL) ok = 0;
    }
    py_dict_update_drop(slots, handles, 5);
    py_dict_update_drop(slots, handles, 4);
    py_dict_update_drop(slots, handles, 3);
    while (ok) {
        PyObject *item = py_obj_next(py_dict_update_load(slots, handles, 2));
        if (item == NULL) {
            if (py_err_occurred()) {
                if (py_exc_matches(py_current_exception(), py_exc_builtin_class(PY_EXC_STOPITERATION)))
                    py_clear_exception();
            } else {
                py_runtime_error_if_unset("py_obj_next", "dict.update iterator returned NULL");
            }
            ok = 0;
        } else {
            ok = py_dict_update_hold(slots, handles, 3, item);
            if (ok && !mapping) {
                PyObject *pair = py_list_new(2);
                ok = py_dict_update_hold(slots, handles, 4, pair);
                if (pair == NULL) ok = 0;
                if (ok) {
                    py_list_extend(py_dict_update_load(slots, handles, 4),
                                   py_dict_update_load(slots, handles, 3));
                    if (py_err_occurred()) ok = 0;
                    else if (py_list_len(py_dict_update_load(slots, handles, 4)) != 2) {
                        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR,
                            "dictionary update sequence element must have length 2"));
                        ok = 0;
                    }
                }
            }
            if (ok) {
                PyObject *key = py_dict_update_load(slots, handles, 3);
                if (mapping) py_incref(key);
                else key = py_list_get(py_dict_update_load(slots, handles, 4), 0);
                ok = py_dict_update_hold(slots, handles, 5, key);
            }
            if (ok) {
                PyObject *value = mapping
                    ? py_obj_getitem(py_dict_update_load(slots, handles, 1),
                                     py_dict_update_load(slots, handles, 5))
                    : py_list_get(py_dict_update_load(slots, handles, 4), 1);
                ok = py_dict_update_hold(slots, handles, 6, value);
                if (value == NULL) ok = 0;
            }
            if (ok) {
                py_dict_set(py_dict_update_load(slots, handles, 0),
                            py_dict_update_load(slots, handles, 5),
                            py_dict_update_load(slots, handles, 6));
                if (py_err_occurred()) ok = 0;
            }
            for (int index = 6; index >= 3; index--)
                py_dict_update_drop(slots, handles, index);
        }
    }
    for (int index = 6; index >= 0; index--)
        py_dict_update_drop(slots, handles, index);
}

void py_dict_update(PyObject *dst, PyObject *src) {
    if (!py_object_is_dict(dst)) return;
    if (!py_object_is_dict(src)) {
        py_dict_update_protocol(dst, src);
        return;
    }
    PyObject *dst_storage = dst;
    PyObject *src_storage = src;
    void *dst_handle = NULL;
    void *src_handle = NULL;
    if (py_dict_prepare_moving_root(&dst_storage, &dst_handle) != 0) return;
    if (py_dict_prepare_moving_root(&src_storage, &src_handle) != 0) {
        py_dict_finish_moving_root(dst_handle);
        return;
    }
    src = py_dict_reload_moving_root(&src_storage, src_handle);
    if (!py_object_is_dict(src)) {
        py_dict_finish_moving_root(src_handle);
        py_dict_finish_moving_root(dst_handle);
        return;
    }

    /* Snapshot the source before invoking destination hash/equality callbacks.
     * py_dict_set runs user code, which may relocate either dict or mutate the
     * source; caching the source PyDictObject and entries_used across those
     * calls would leave later iterations reading a stale owner/table.  Mirrors
     * py_set_update.  The snapshot holds key and value alternately. */
    PyDictObject *source_dict = (PyDictObject *)src;
    PyObject *snapshot_storage = py_list_new(
        source_dict->size > 0 ? source_dict->size * 2 : 4
    );
    if (snapshot_storage == NULL) {
        py_dict_finish_moving_root(src_handle);
        py_dict_finish_moving_root(dst_handle);
        return;
    }
    void *snapshot_handle = NULL;
    if (py_dict_prepare_moving_root(
            &snapshot_storage, &snapshot_handle
        ) != 0) {
        py_decref(snapshot_storage);
        py_dict_finish_moving_root(src_handle);
        py_dict_finish_moving_root(dst_handle);
        return;
    }

    src = py_dict_reload_moving_root(&src_storage, src_handle);
    source_dict = (PyDictObject *)src;
    int64_t source_used = source_dict->entries_used;
    for (int64_t i = 0; i < source_used; i++) {
        src = py_dict_reload_moving_root(&src_storage, src_handle);
        if (!py_object_is_dict(src)) break;
        source_dict = (PyDictObject *)src;
        if (i >= source_dict->entries_used) break;
        DictEntry *entry = &source_dict->entries[i];
        PyObject *key = py_dict_entry_key(source_dict, entry);
        if (key == NULL) continue;
        PyObject *value = py_dict_entry_value(source_dict, entry);
        PyObject *snapshot = py_dict_reload_moving_root(
            &snapshot_storage, snapshot_handle
        );
        py_list_append(snapshot, key);
        if (py_err_occurred()) break;
        snapshot = py_dict_reload_moving_root(
            &snapshot_storage, snapshot_handle
        );
        py_list_append(snapshot, value);
        if (py_err_occurred()) break;
    }

    PyObject *snapshot = py_dict_reload_moving_root(
        &snapshot_storage, snapshot_handle
    );
    int64_t snapshot_len = py_list_len(snapshot);
    for (int64_t i = 0; i + 1 < snapshot_len && !py_err_occurred(); i += 2) {
        snapshot = py_dict_reload_moving_root(
            &snapshot_storage, snapshot_handle
        );
        PyObject *key_storage = py_list_get(snapshot, i);
        if (key_storage == NULL) break;
        snapshot = py_dict_reload_moving_root(
            &snapshot_storage, snapshot_handle
        );
        PyObject *value_storage = py_list_get(snapshot, i + 1);
        if (value_storage == NULL) {
            py_decref(key_storage);
            break;
        }
        void *key_handle = NULL;
        void *value_handle = NULL;
        if (py_dict_prepare_moving_root(&key_storage, &key_handle) != 0) {
            py_decref(key_storage);
            py_decref(value_storage);
            break;
        }
        if (py_dict_prepare_moving_root(&value_storage, &value_handle) != 0) {
            py_dict_finish_moving_root(key_handle);
            py_decref(key_storage);
            py_decref(value_storage);
            break;
        }
        dst = py_dict_reload_moving_root(&dst_storage, dst_handle);
        py_dict_set(dst, key_storage, value_storage);
        key_storage = py_dict_reload_moving_root(&key_storage, key_handle);
        value_storage = py_dict_reload_moving_root(
            &value_storage, value_handle
        );
        py_dict_finish_moving_root(value_handle);
        py_dict_finish_moving_root(key_handle);
        py_decref(key_storage);
        py_decref(value_storage);
    }

    snapshot = py_dict_reload_moving_root(
        &snapshot_storage, snapshot_handle
    );
    py_dict_finish_moving_root(snapshot_handle);
    py_decref(snapshot);
    py_dict_finish_moving_root(src_handle);
    py_dict_finish_moving_root(dst_handle);
}
