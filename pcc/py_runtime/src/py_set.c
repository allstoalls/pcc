/* pcc/py_runtime/src/py_set.c
 *
 * Open-addressing hash set of PyObject* (unordered, unique).
 *
 * Layout (see py_internal.h):
 *
 *   SetEntry { int64_t hash; PyObject *key; }
 *
 *   PySetObject {
 *       header; size; capacity; fill; entries[];
 *   }
 *
 *   entries[j].key == NULL              -> never-used slot ("empty")
 *   entries[j].key == py_set_dummy      -> tombstone (deleted)
 *   otherwise                           -> live entry
 *
 *   size = live count
 *   fill = live + tombstones (drives rehash at 2/3 load factor)
 *
 * Probing matches py_dict.c (CPython-style perturbation):
 *   perturb = hash; j = hash & mask;
 *   next:   perturb >>= 5;  j = (j*5 + perturb + 1) & mask.
 *
 * Hash/equality: py_obj_hash + py_obj_eq from py_obj_ops.c.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>

#define PY_SET_INITIAL_CAPACITY  8  /* must be power of 2 */

/* Tombstone sentinel moved to py_substrate.c so py_set.c can be
 * replaced by a pcc-Python port (py_set.py) without losing the
 * sentinel symbol. The const pointer value is accessed via
 * py_subs_set_dummy() from the Python port, and imported via
 * the extern declaration in py_internal.h for C callers. */

/* ---- Forward decls ----------------------------------------------------- */
static int py_set_rehash(PySetObject *s, int64_t new_capacity);
static int py_set_maybe_grow(PySetObject *s);
int64_t py_set_contains(PyObject *set, PyObject *item);

static PyObject *py_set_entry_key(PySetObject *s, SetEntry *e) {
    PyObject *raw = e->key;
    if (raw == NULL || raw == py_set_dummy) return raw;
    return pcc_gc_load_ptr((PyObject *)s, &e->key);
}

static int py_set_prepare_moving_root(PyObject **slot, void **out_handle) {
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

static PyObject *py_set_reload_moving_root(PyObject **slot, void *handle) {
    if (slot == NULL) return NULL;
    if (handle != NULL) *slot = pcc_gc_load_ptr(NULL, slot);
    return *slot;
}

static void py_set_finish_moving_root(void *handle) {
    if (handle != NULL) pcc_gc_scheduler_root_unregister_handle(handle);
}

/* ---- Allocation -------------------------------------------------------- */

static int py_set_alloc_entries(PySetObject *s, int64_t capacity) {
    SetEntry *entries = (SetEntry *)malloc((size_t)capacity * sizeof(SetEntry));
    if (entries == NULL) return -1;
    for (int64_t i = 0; i < capacity; i++) {
        entries[i].hash = 0;
        entries[i].key  = NULL;
    }
    s->entries  = entries;
    s->capacity = capacity;
    s->size     = 0;
    s->fill     = 0;
    (void)pcc_gc_backend4_zpage_register_owner_payload_span(
        (PyObject *)s,
        s->entries,
        capacity * (int64_t)sizeof(SetEntry)
    );
    return 0;
}

PyObject *py_set_new(void) {
    PySetObject *s = (PySetObject *)pcc_gc_alloc(
        (int64_t)sizeof(PySetObject), PY_TYPE_SET, 0
    );
    if (s == NULL) return NULL;
    s->entries    = NULL;
    s->capacity   = 0;
    s->size       = 0;
    s->fill       = 0;
    if (py_set_alloc_entries(s, PY_SET_INITIAL_CAPACITY) != 0) {
        py_decref((PyObject *)s);
        return NULL;
    }
    py_gc_track((PyObject *)s);
    pcc_gc_publish_initialized((PyObject *)s);
    return (PyObject *)s;
}

/* ---- Probing ----------------------------------------------------------- */

/* Locate the slot for `key`. On return:
 *   *out_slot        = slot index the key lives at (if found) or a usable
 *                      insert target (first tombstone seen, else the
 *                      terminal empty slot).
 *   *out_found       = 1 if the key is live, 0 otherwise.
 */
static void py_set_lookup(PySetObject *s, int64_t hash, PyObject *key,
                          int64_t *out_slot, int *out_found) {
    int64_t mask = s->capacity - 1;
    uint64_t perturb = (uint64_t)hash;
    int64_t j = (int64_t)((uint64_t)hash & (uint64_t)mask);
    int64_t first_tombstone = -1;
    int64_t probes = 0;
    /* See py_set.py: capacity * 2 is not a sufficient probe budget. */
    int64_t limit = s->capacity + 16;

    while (probes < limit) {
        SetEntry *e = &s->entries[j];
        PyObject *entry_key = py_set_entry_key(s, e);
        if (entry_key == NULL) {
            *out_slot = (first_tombstone >= 0) ? first_tombstone : j;
            *out_found = 0;
            return;
        }
        if (entry_key == py_set_dummy) {
            if (first_tombstone < 0) first_tombstone = j;
        } else if (e->hash == hash && (entry_key == key || py_obj_eq(entry_key, key))) {
            *out_slot = j;
            *out_found = 1;
            return;
        }
        perturb >>= 5;
        j = (int64_t)(((uint64_t)j * 5u + perturb + 1u) & (uint64_t)mask);
        probes++;
    }

    *out_slot = (first_tombstone >= 0) ? first_tombstone : 0;
    *out_found = 0;
}

static int py_set_remove_rooted_slot(
    PyObject **set_storage,
    void *set_handle,
    SetEntry *entries,
    int64_t capacity,
    int64_t slot
) {
    PyObject *set = py_set_reload_moving_root(set_storage, set_handle);
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan plan;
    pcc_gc_store_ptr_plan_init(&plan, set, backend);
    pcc_gc_root_slot_lock();
    set = py_set_reload_moving_root(set_storage, set_handle);
    int committed = 0;
    if (
        set != NULL && !PY_IS_TAGGED_INT(set)
        && py_header(set)->type_tag == PY_TYPE_SET
    ) {
        PySetObject *s = (PySetObject *)set;
        if (
            s->entries == entries && s->capacity == capacity
            && slot >= 0 && slot < capacity
        ) {
            SetEntry *entry = &entries[slot];
            PyObject *key = py_set_entry_key(s, entry);
            if (key != NULL && key != py_set_dummy) {
                /* The tombstone is a sentinel, not a reference: the ordinary
                 * commit increfed it, and incref prepare begins with the
                 * provenance probe, so every discard paid
                 * pcc_gc_granule_is_object_start to learn that py_set_dummy is
                 * not an object.  The sentinel path still releases the key. */
                committed = pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
                    &plan, set, &entry->key, py_set_dummy, py_set_dummy
                ) != 0;
                if (committed) s->size--;
            }
        }
    }
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&plan);
    return committed;
}

static int py_set_add_rooted_slot(
    PyObject **set_storage,
    void *set_handle,
    PyObject **item_storage,
    void *item_handle,
    SetEntry *entries,
    int64_t capacity,
    int64_t slot,
    int64_t hash
) {
    PyObject *set = py_set_reload_moving_root(set_storage, set_handle);
    PyObject *item = py_set_reload_moving_root(item_storage, item_handle);
    int64_t backend = pcc_gc_backend();
    PccGcStoreRootPlan plan;
    pcc_gc_store_ptr_plan_init(&plan, set, backend);
    pcc_gc_root_slot_lock();
    set = py_set_reload_moving_root(set_storage, set_handle);
    item = py_set_reload_moving_root(item_storage, item_handle);
    int committed = 0;
    if (
        set != NULL && !PY_IS_TAGGED_INT(set)
        && py_header(set)->type_tag == PY_TYPE_SET
    ) {
        PySetObject *s = (PySetObject *)set;
        if (
            s->entries == entries && s->capacity == capacity
            && slot >= 0 && slot < capacity
        ) {
            SetEntry *entry = &entries[slot];
            PyObject *old = py_set_entry_key(s, entry);
            if (old == NULL || old == py_set_dummy) {
                int was_tombstone = old == py_set_dummy;
                /* A real element may land on a tombstone; releasing the
                 * sentinel is the other half of the same class. */
                committed = pcc_gc_store_ptr_plan_commit_sentinel_aware_locked(
                    &plan, set, &entry->key, item, py_set_dummy
                ) != 0;
                if (committed) {
                    entry->hash = hash;
                    s->size++;
                    if (!was_tombstone) s->fill++;
                }
            }
        }
    }
    pcc_gc_root_slot_unlock();
    pcc_gc_store_ptr_plan_finish(&plan);
    if (committed) {
        set = py_set_reload_moving_root(set_storage, set_handle);
        (void)py_set_maybe_grow((PySetObject *)set);
    }
    return committed;
}

static int64_t py_set_lookup_rooted(
    PyObject *set,
    PyObject *item,
    int mode
) {
    PyObject *set_storage = set;
    PyObject *item_storage = item;
    void *set_handle = NULL;
    void *item_handle = NULL;
    if (py_set_prepare_moving_root(&set_storage, &set_handle) != 0) return 0;
    if (py_set_prepare_moving_root(&item_storage, &item_handle) != 0) {
        py_set_finish_moving_root(set_handle);
        return 0;
    }
    item = py_set_reload_moving_root(&item_storage, item_handle);
    int64_t hash = py_obj_hash(item);
    set = py_set_reload_moving_root(&set_storage, set_handle);
    item = py_set_reload_moving_root(&item_storage, item_handle);
    if (py_err_occurred()) {
        py_set_finish_moving_root(item_handle);
        py_set_finish_moving_root(set_handle);
        return 0;
    }

    int attempts = 0;
restart:
    if (attempts++ >= 16) {
        py_set_finish_moving_root(item_handle);
        py_set_finish_moving_root(set_handle);
        return 0;
    }
    set = py_set_reload_moving_root(&set_storage, set_handle);
    item = py_set_reload_moving_root(&item_storage, item_handle);
    if (
        set == NULL || PY_IS_TAGGED_INT(set)
        || py_header(set)->type_tag != PY_TYPE_SET
    ) {
        py_set_finish_moving_root(item_handle);
        py_set_finish_moving_root(set_handle);
        return 0;
    }
    PySetObject *s = (PySetObject *)set;
    int64_t capacity = s->capacity;
    SetEntry *entries = s->entries;
    if (capacity <= 0 || entries == NULL) {
        py_set_finish_moving_root(item_handle);
        py_set_finish_moving_root(set_handle);
        return 0;
    }
    int64_t mask = capacity - 1;
    uint64_t perturb = (uint64_t)hash;
    int64_t j = (int64_t)((uint64_t)hash & (uint64_t)mask);
    int64_t first_tombstone = -1;
    int64_t probes = 0;
    while (probes < capacity + 16) {
        SetEntry *entry = &entries[j];
        PyObject *entry_key = py_set_entry_key(s, entry);
        if (entry_key == NULL) {
            if (mode == 2) {
                int64_t target = first_tombstone >= 0
                    ? first_tombstone : j;
                int added = py_set_add_rooted_slot(
                    &set_storage,
                    set_handle,
                    &item_storage,
                    item_handle,
                    entries,
                    capacity,
                    target,
                    hash
                );
                py_set_finish_moving_root(item_handle);
                py_set_finish_moving_root(set_handle);
                return added ? 1 : 0;
            }
            break;
        }
        if (entry_key == py_set_dummy && first_tombstone < 0) {
            first_tombstone = j;
        }
        if (
            entry_key != py_set_dummy
            && entry->hash == hash
        ) {
            if (entry_key == item) {
                int removed = 1;
                if (mode == 1) {
                    removed = py_set_remove_rooted_slot(
                        &set_storage, set_handle, entries, capacity, j
                    );
                }
                py_set_finish_moving_root(item_handle);
                py_set_finish_moving_root(set_handle);
                return removed ? 1 : 0;
            }
            if (!(PY_IS_TAGGED_INT(entry_key) && PY_IS_TAGGED_INT(item))) {
                py_incref(entry_key);
                PyObject *candidate_storage = entry_key;
                void *candidate_handle = NULL;
                if (py_set_prepare_moving_root(
                        &candidate_storage, &candidate_handle
                    ) != 0) {
                    py_decref(entry_key);
                    break;
                }
                PyObject *before_set = set;
                int equal = py_obj_eq(candidate_storage, item) != 0;
                set = py_set_reload_moving_root(&set_storage, set_handle);
                item = py_set_reload_moving_root(&item_storage, item_handle);
                PyObject *candidate = py_set_reload_moving_root(
                    &candidate_storage, candidate_handle
                );
                py_set_finish_moving_root(candidate_handle);
                int stable = set == before_set
                    && set != NULL
                    && !PY_IS_TAGGED_INT(set)
                    && py_header(set)->type_tag == PY_TYPE_SET
                    && ((PySetObject *)set)->capacity == capacity
                    && ((PySetObject *)set)->entries == entries
                    && j < capacity
                    && py_set_entry_key(
                        (PySetObject *)set, &entries[j]
                    ) == candidate;
                py_decref(candidate);
                if (py_err_occurred()) {
                    py_set_finish_moving_root(item_handle);
                    py_set_finish_moving_root(set_handle);
                    return 0;
                }
                if (!stable) goto restart;
                if (equal) {
                    int removed = 1;
                    if (mode == 1) {
                        removed = py_set_remove_rooted_slot(
                            &set_storage, set_handle, entries, capacity, j
                        );
                    }
                    py_set_finish_moving_root(item_handle);
                    py_set_finish_moving_root(set_handle);
                    return removed ? 1 : 0;
                }
            }
        }
        perturb >>= 5;
        j = (int64_t)(((uint64_t)j * 5u + perturb + 1u) & (uint64_t)mask);
        probes++;
    }
    if (mode == 2 && first_tombstone >= 0) {
        int added = py_set_add_rooted_slot(
            &set_storage,
            set_handle,
            &item_storage,
            item_handle,
            entries,
            capacity,
            first_tombstone,
            hash
        );
        py_set_finish_moving_root(item_handle);
        py_set_finish_moving_root(set_handle);
        return added ? 1 : 0;
    }
    py_set_finish_moving_root(item_handle);
    py_set_finish_moving_root(set_handle);
    return 0;
}

/* Rebuild the entries[] array at `new_capacity`. Moves refs (no
 * incref/decref) from old to new. */
static int64_t py_set_rehash_find_empty_slot(
    SetEntry *entries,
    int64_t capacity,
    int64_t hash
) {
    int64_t mask = capacity - 1;
    uint64_t perturb = (uint64_t)hash;
    int64_t slot = (int64_t)((uint64_t)hash & (uint64_t)mask);
    int64_t probes = 0;
    while (probes < capacity + 16) {
        if (entries[slot].key == NULL) return slot;
        perturb >>= 5;
        slot = (int64_t)(
            ((uint64_t)slot * 5u + perturb + 1u) & (uint64_t)mask
        );
        probes++;
    }
    return -1;
}

static int py_set_rehash_refcount_fast(
    PySetObject *s,
    int64_t new_capacity
) {
    SetEntry *old_entries = s->entries;
    int64_t old_capacity = s->capacity;
    SetEntry *new_entries = (SetEntry *)calloc(
        (size_t)new_capacity, sizeof(SetEntry)
    );
    if (new_entries == NULL) return -1;
    int64_t new_size = 0;
    for (int64_t i = 0; i < old_capacity; i++) {
        PyObject *key = old_entries[i].key;
        if (key == NULL || key == py_set_dummy) continue;
        int64_t slot = py_set_rehash_find_empty_slot(
            new_entries, new_capacity, old_entries[i].hash
        );
        if (slot < 0) {
            free(new_entries);
            return -1;
        }
        new_entries[slot] = old_entries[i];
        new_size++;
    }
    s->entries = new_entries;
    s->capacity = new_capacity;
    s->size = new_size;
    s->fill = new_size;
    free(old_entries);
    return 0;
}

static int py_set_rehash(PySetObject *s, int64_t new_capacity) {
    if (s == NULL || new_capacity <= 0) return -1;
    int64_t initial_backend = pcc_gc_backend();
    if (initial_backend == PCC_GC_KIND_REFCOUNT_CYCLE) {
        return py_set_rehash_refcount_fast(s, new_capacity);
    }
    PyObject *owner_slot = (PyObject *)s;
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
        s = (PySetObject *)owner_slot;
        SetEntry *old_entries = s->entries;
        int64_t old_capacity = s->capacity;
        int64_t old_size = s->size;
        int64_t old_fill = s->fill;
        pcc_gc_root_slot_unlock();
        if (
            old_entries == NULL
            || old_capacity <= 0
            || new_capacity < old_capacity
            || old_size < 0
            || old_size > new_capacity
            || old_fill < old_size
            || old_fill > old_capacity
            || old_capacity > INT64_MAX / 2
        ) break;

        SetEntry *new_entries = (SetEntry *)calloc(
            (size_t)new_capacity, sizeof(SetEntry)
        );
        PyObject ***slot_pairs = (PyObject ***)calloc(
            (size_t)old_capacity * 2u,
            sizeof(PyObject **)
        );
        if (new_entries == NULL || slot_pairs == NULL) {
            free(slot_pairs);
            free(new_entries);
            break;
        }

        pcc_gc_root_slot_lock();
        if (pcc_gc_backend() != initial_backend) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_entries);
            break;
        }
        if (owner_handle != NULL) {
            owner_slot = pcc_gc_load_ptr(NULL, &owner_slot);
        }
        s = (PySetObject *)owner_slot;
        if (
            s->entries != old_entries
            || s->capacity != old_capacity
            || s->size != old_size
            || s->fill != old_fill
        ) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_entries);
            continue;
        }

        int64_t new_size = 0;
        int64_t pair_count = 0;
        int copy_valid = 1;
        for (int64_t i = 0; i < old_capacity; i++) {
            PyObject *key = py_set_entry_key(s, &old_entries[i]);
            if (key == NULL || key == py_set_dummy) continue;
            int64_t slot = py_set_rehash_find_empty_slot(
                new_entries, new_capacity, old_entries[i].hash
            );
            if (slot < 0) {
                copy_valid = 0;
                break;
            }
            new_entries[slot].hash = old_entries[i].hash;
            new_entries[slot].key = key;
            slot_pairs[pair_count * 2] = &old_entries[i].key;
            slot_pairs[pair_count * 2 + 1] = &new_entries[slot].key;
            pair_count++;
            new_size++;
        }
        int64_t retargeted = 0;
        if (copy_valid) {
            retargeted = pcc_gc_backend4_retarget_mutator_payload_locked(
                (PyObject *)s,
                old_entries,
                old_capacity * (int64_t)sizeof(SetEntry),
                new_entries,
                new_capacity * (int64_t)sizeof(SetEntry),
                slot_pairs,
                pair_count
            );
        }
        if (!copy_valid || retargeted == 0) {
            pcc_gc_root_slot_unlock();
            free(slot_pairs);
            free(new_entries);
            break;
        }
        for (int64_t i = 0; i < pair_count; i++) {
            PyObject **new_slot = slot_pairs[i * 2 + 1];
            pcc_gc_note_slot_write_barrier(
                (PyObject *)s, new_slot, *new_slot
            );
        }
        s->entries = new_entries;
        s->capacity = new_capacity;
        s->size = new_size;
        s->fill = new_size;
        if (retargeted == 2) {
            (void)pcc_gc_backend4_zpage_register_owner_payload_span(
                (PyObject *)s,
                new_entries,
                new_capacity * (int64_t)sizeof(SetEntry)
            );
        }
        pcc_gc_root_slot_unlock();

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

static int py_set_maybe_grow(PySetObject *s) {
    int64_t threshold = (s->capacity * 2) / 3;
    if (s->fill <= threshold) return 0;
    int64_t new_cap = s->capacity;
    if (s->size > threshold / 2) {
        new_cap = s->capacity * 2;
    }
    return py_set_rehash(s, new_cap);
}

/* ---- Public API -------------------------------------------------------- */

void py_set_add(PyObject *set, PyObject *item) {
    if (set == NULL || item == NULL) return;
    (void)py_set_lookup_rooted(set, item, 2);
}

void py_set_update(PyObject *dst, PyObject *src) {
    if (dst == NULL || src == NULL) return;
    PyObject *dst_storage = dst;
    PyObject *src_storage = src;
    void *dst_handle = NULL;
    void *src_handle = NULL;
    if (py_set_prepare_moving_root(&dst_storage, &dst_handle) != 0) return;
    if (py_set_prepare_moving_root(&src_storage, &src_handle) != 0) {
        py_set_finish_moving_root(dst_handle);
        return;
    }
    src = py_set_reload_moving_root(&src_storage, src_handle);
    if (
        PY_IS_TAGGED_INT(src)
        || py_header(src)->type_tag != PY_TYPE_SET
    ) {
        py_set_finish_moving_root(src_handle);
        py_set_finish_moving_root(dst_handle);
        return;
    }

    /* Snapshot the source before invoking destination hash/equality callbacks.
     * A callback may relocate either set or mutate the source; walking the raw
     * source entries across py_set_add would otherwise retain stale payload
     * addresses and would not match Python's consume-the-input-first behavior. */
    PySetObject *source_set = (PySetObject *)src;
    PyObject *snapshot_storage = py_list_new(
        source_set->size > 0 ? source_set->size : 4
    );
    if (snapshot_storage == NULL) {
        py_set_finish_moving_root(src_handle);
        py_set_finish_moving_root(dst_handle);
        return;
    }
    void *snapshot_handle = NULL;
    if (py_set_prepare_moving_root(
            &snapshot_storage, &snapshot_handle
        ) != 0) {
        py_decref(snapshot_storage);
        py_set_finish_moving_root(src_handle);
        py_set_finish_moving_root(dst_handle);
        return;
    }
    src = py_set_reload_moving_root(&src_storage, src_handle);
    source_set = (PySetObject *)src;
    int64_t source_capacity = source_set->capacity;
    for (int64_t i = 0; i < source_capacity; i++) {
        src = py_set_reload_moving_root(&src_storage, src_handle);
        source_set = (PySetObject *)src;
        PyObject *key = py_set_entry_key(
            source_set, &source_set->entries[i]
        );
        if (key == NULL || key == py_set_dummy) continue;
        PyObject *snapshot = py_set_reload_moving_root(
            &snapshot_storage, snapshot_handle
        );
        py_list_append(snapshot, key);
        if (py_err_occurred()) break;
    }

    PyObject *snapshot = py_set_reload_moving_root(
        &snapshot_storage, snapshot_handle
    );
    int64_t snapshot_len = py_list_len(snapshot);
    for (int64_t i = 0; i < snapshot_len && !py_err_occurred(); i++) {
        snapshot = py_set_reload_moving_root(
            &snapshot_storage, snapshot_handle
        );
        PyObject *key_storage = py_list_get(snapshot, i);
        if (key_storage == NULL) break;
        void *key_handle = NULL;
        if (py_set_prepare_moving_root(&key_storage, &key_handle) != 0) {
            py_decref(key_storage);
            break;
        }
        dst = py_set_reload_moving_root(&dst_storage, dst_handle);
        py_set_add(dst, key_storage);
        key_storage = py_set_reload_moving_root(&key_storage, key_handle);
        py_set_finish_moving_root(key_handle);
        py_decref(key_storage);
    }
    snapshot = py_set_reload_moving_root(
        &snapshot_storage, snapshot_handle
    );
    py_set_finish_moving_root(snapshot_handle);
    py_decref(snapshot);
    py_set_finish_moving_root(src_handle);
    py_set_finish_moving_root(dst_handle);
}

PyObject *py_set_intersection(PyObject *a, PyObject *b) {
    PyObject *out = py_set_new();
    if (out == NULL) return NULL;
    if (a == NULL || b == NULL) return out;
    if (PY_IS_TAGGED_INT(a) || PY_IS_TAGGED_INT(b)) return out;
    if (py_header(a)->type_tag != PY_TYPE_SET) return out;
    if (py_header(b)->type_tag != PY_TYPE_SET) return out;
    PySetObject *sa = (PySetObject *)a;
    for (int64_t i = 0; i < sa->capacity; i++) {
        PyObject *k = py_set_entry_key(sa, &sa->entries[i]);
        if (k == NULL || k == py_set_dummy) continue;
        if (py_set_contains(b, k)) py_set_add(out, k);
    }
    return out;
}

PyObject *py_set_difference(PyObject *a, PyObject *b) {
    PyObject *out = py_set_new();
    if (out == NULL) return NULL;
    if (a == NULL) return out;
    if (PY_IS_TAGGED_INT(a)) return out;
    if (py_header(a)->type_tag != PY_TYPE_SET) return out;
    PySetObject *sa = (PySetObject *)a;
    int b_is_set = (
        b != NULL
        && !PY_IS_TAGGED_INT(b)
        && py_header(b)->type_tag == PY_TYPE_SET
    );
    for (int64_t i = 0; i < sa->capacity; i++) {
        PyObject *k = py_set_entry_key(sa, &sa->entries[i]);
        if (k == NULL || k == py_set_dummy) continue;
        if (!b_is_set || !py_set_contains(b, k)) py_set_add(out, k);
    }
    return out;
}

/* a ^ b: elements in exactly one of a, b ((a - b) | (b - a)). */
PyObject *py_set_symmetric_difference(PyObject *a, PyObject *b) {
    PyObject *out = py_set_new();
    if (out == NULL) return NULL;
    int a_is_set = (
        a != NULL && !PY_IS_TAGGED_INT(a)
        && py_header(a)->type_tag == PY_TYPE_SET
    );
    int b_is_set = (
        b != NULL && !PY_IS_TAGGED_INT(b)
        && py_header(b)->type_tag == PY_TYPE_SET
    );
    if (a_is_set) {
        PySetObject *sa = (PySetObject *)a;
        for (int64_t i = 0; i < sa->capacity; i++) {
            PyObject *k = py_set_entry_key(sa, &sa->entries[i]);
            if (k == NULL || k == py_set_dummy) continue;
            if (!b_is_set || !py_set_contains(b, k)) py_set_add(out, k);
        }
    }
    if (b_is_set) {
        PySetObject *sb = (PySetObject *)b;
        for (int64_t i = 0; i < sb->capacity; i++) {
            PyObject *k = py_set_entry_key(sb, &sb->entries[i]);
            if (k == NULL || k == py_set_dummy) continue;
            if (!a_is_set || !py_set_contains(a, k)) py_set_add(out, k);
        }
    }
    return out;
}

/* Drop every live key in `dst` (decref + tombstone) without freeing the
 * entries array, then re-add every live key of `result`. Preserves the
 * receiver object identity while replacing its contents. `result` is a
 * private set produced by one of the *_difference/intersection helpers. */
static void py_set_replace_contents(PyObject *dst, PyObject *result) {
    if (dst == NULL) return;
    if (PY_IS_TAGGED_INT(dst)) return;
    if (py_header(dst)->type_tag != PY_TYPE_SET) return;
    PySetObject *s = (PySetObject *)dst;
    for (int64_t i = 0; i < s->capacity; i++) {
        SetEntry *e = &s->entries[i];
        PyObject *k = py_set_entry_key(s, e);
        if (k == NULL || k == py_set_dummy) continue;
        py_decref(k);
        e->key = py_set_dummy;   /* tombstone; fill unchanged */
        s->size--;
    }
    /* py_set_update re-adds each live key of `result` (incref via
     * pcc_gc_store_ptr inside py_set_add). Tombstones from the clear above
     * are reused as insert targets and reclaimed on the next rehash. */
    py_set_update(dst, result);
}

void py_set_intersection_update(PyObject *dst, PyObject *other) {
    PyObject *result = py_set_intersection(dst, other);
    if (result == NULL) return;
    py_set_replace_contents(dst, result);
    py_decref(result);
}

void py_set_difference_update(PyObject *dst, PyObject *other) {
    PyObject *result = py_set_difference(dst, other);
    if (result == NULL) return;
    py_set_replace_contents(dst, result);
    py_decref(result);
}

void py_set_symmetric_difference_update(PyObject *dst, PyObject *other) {
    PyObject *result = py_set_symmetric_difference(dst, other);
    if (result == NULL) return;
    py_set_replace_contents(dst, result);
    py_decref(result);
}

int64_t py_set_issubset(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) return 0;
    if (PY_IS_TAGGED_INT(a) || PY_IS_TAGGED_INT(b)) return 0;
    if (py_header(a)->type_tag != PY_TYPE_SET) return 0;
    if (py_header(b)->type_tag != PY_TYPE_SET) return 0;
    PySetObject *sa = (PySetObject *)a;
    PySetObject *sb = (PySetObject *)b;
    if (sa->size > sb->size) return 0;
    for (int64_t i = 0; i < sa->capacity; i++) {
        PyObject *k = py_set_entry_key(sa, &sa->entries[i]);
        if (k == NULL || k == py_set_dummy) continue;
        if (!py_set_contains(b, k)) return 0;
    }
    return 1;
}

int64_t py_set_issuperset(PyObject *a, PyObject *b) {
    return py_set_issubset(b, a);
}

PyObject *py_set_items(PyObject *set) {
    if (set == NULL) return NULL;
    if (PY_IS_TAGGED_INT(set)) return NULL;
    if (py_header(set)->type_tag != PY_TYPE_SET) return NULL;
    PySetObject *s = (PySetObject *)set;
    PyObject *out = py_list_new(s->size > 0 ? s->size : 4);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < s->capacity; i++) {
        PyObject *k = py_set_entry_key(s, &s->entries[i]);
        if (k == NULL || k == py_set_dummy) continue;
        py_list_append(out, k);
    }
    return out;
}

PyObject *py_set_pop(PyObject *set) {
    if (set == NULL) return NULL;
    if (PY_IS_TAGGED_INT(set)) return NULL;
    if (py_header(set)->type_tag != PY_TYPE_SET) return NULL;
    PySetObject *s = (PySetObject *)set;
    if (s->size <= 0) {
        py_raise_owned(py_exc_new(PY_EXC_KEYERROR, "pop from an empty set"));
        return NULL;
    }
    for (int64_t i = 0; i < s->capacity; i++) {
        SetEntry *e = &s->entries[i];
        PyObject *k = py_set_entry_key(s, e);
        if (k == NULL || k == py_set_dummy) continue;
        e->key = py_set_dummy;   /* transfer set's owned ref to caller */
        s->size--;
        return k;
    }
    py_raise_owned(py_exc_new(PY_EXC_KEYERROR, "pop from an empty set"));
    return NULL;
}

int64_t py_set_contains(PyObject *set, PyObject *item) {
    if (set == NULL || item == NULL) return 0;
    return py_set_lookup_rooted(set, item, 0);
}

int64_t py_set_remove(PyObject *set, PyObject *item) {
    if (set == NULL || item == NULL) return -1;
    return py_set_lookup_rooted(set, item, 1) ? 0 : -1;
}

int64_t py_set_len(PyObject *set) {
    if (set == NULL) return 0;
    return ((PySetObject *)set)->size;
}
