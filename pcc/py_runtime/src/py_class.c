/* pcc/py_runtime/src/py_class.c
 *
 * Class + instance runtime for Phase 3.
 *
 * Layouts live in py_internal.h. This file implements:
 *   - py_class_new / py_class_add_method / py_class_lookup
 *   - py_instance_new / py_instance_get_field / py_instance_set_field
 *   - py_instance_getattr / py_instance_setattr
 *   - py_isinstance
 *   - py_super_lookup
 *   - c3_linearize (PEP 3119 / CPython typeobject.c:mro_implementation)
 *   - py_class_dealloc / py_instance_dealloc
 *
 * Design notes:
 *
 *   * A class is itself a PyObject carrying PY_TYPE_CLASS. Refcount is
 *     1 (it is expected to live for the process lifetime because the
 *     codegen emits it as a module-init side effect and stores a pointer
 *     in a global). Freeing a class is still supported for completeness.
 *
 *   * Instances carry either PY_TYPE_INSTANCE or a class-specific tag at
 *     PY_TYPE_USER_CLASS_START or above. Tags between PY_TYPE_USER and that
 *     boundary are descriptor layouts and must not enter py_instance_*.
 *
 *   * Method dispatch is a semantic linear walk over methods[] of
 *     mro[0..n_mro-1]. A direct-mapped side cache was measured on the
 *     self-host workload and denied because it did not materially reduce
 *     wall time or retired instructions.
 *
 *   * C3 linearization follows the exact "merge" algorithm from PEP 3119.
 *     The implementation allocates scratch buffers with malloc and frees
 *     them on the way out; no global state. A bad MRO returns -1; the
 *     caller is expected to surface that as a Python TypeError.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

extern int32_t py_class_attr_cache_epoch;
extern int32_t pcc_class_del_defined_count;
extern PyObject *py_inst_field_cache_cls0;
extern PyObject *py_inst_field_cache_cls1;
extern PyObject *py_inst_field_cache_cls2;
extern PyObject *py_inst_field_cache_cls3;
extern const char *py_inst_field_cache_name0;
extern const char *py_inst_field_cache_name1;
extern const char *py_inst_field_cache_name2;
extern const char *py_inst_field_cache_name3;
extern int32_t py_inst_field_cache_idx0;
extern int32_t py_inst_field_cache_idx1;
extern int32_t py_inst_field_cache_idx2;
extern int32_t py_inst_field_cache_idx3;
extern int32_t py_inst_field_cache_epoch0;
extern int32_t py_inst_field_cache_epoch1;
extern int32_t py_inst_field_cache_epoch2;
extern int32_t py_inst_field_cache_epoch3;

static int32_t class_attr_cache_epoch_load(void) {
    return __atomic_load_n(&py_class_attr_cache_epoch, __ATOMIC_ACQUIRE);
}

/* ---- Forward decls (for dispatch from py_obj.c) ----------------------- */

/* py_obj.c already dispatches on type_tag via a switch; we hook
 * PY_TYPE_CLASS / PY_TYPE_INSTANCE / allocated user-class tags through the
 * "generic" path. Exposing dedicated deallocators here lets us centralize
 * the cleanup. */

static PyClassObject *object_root(void);

#define PY_CLASS_FLAG_SLOTS_ONLY 2
/* Bit 2: this class subclasses the builtin ``dict``. The frontend drops the
 * ``dict`` base from the native MRO (dict is not a user PyClassObject), so
 * dict-inherited item storage / methods (__setitem__ / __getitem__ / get /
 * __missing__ / keys / values / items / __len__ / __contains__ / iteration)
 * must be provided as a fallback. When this flag is set, those operations are
 * routed to a backing dict lazily materialized in the instance's hidden
 * dynamic-attribute (__dict__) slot. See py_dict_subclass_* in py_protocol.c. */
#define PY_CLASS_FLAG_DICT_SUBCLASS 4

/* ---- Helpers ---------------------------------------------------------- */

static int pointer_can_have_header(void *ptr) {
    return pcc_gc_pointer_is_managed((PyObject *)ptr) != 0;
}

static int class_pointer_is_class(PyClassObject *cls) {
    if (!pointer_can_have_header((void *)cls)) return 0;
    return py_header((PyObject *)cls)->type_tag == PY_TYPE_CLASS;
}

/* class_pointer_is_class without the provenance probe, for a class pointer that
 * came out of an already-validated instance's cls slot.  The caller has just
 * proved the instance is a managed object whose tag says instance;
 * py_class_new-produced classes are the only thing py_instance_new stores
 * there, the slot is a borrowed uncounted class pointer, and classes carry
 * PY_FLAG_IMMORTAL -- so this header read is safe on the strength of the
 * instance being valid.  Probing again cost a second lock plus hash probe on
 * the hottest path in the runtime: every attribute access and method dispatch
 * runs instance_pointer_is_instance, which paid the probe once for the instance
 * and once more for its class.  Mirrors the pcc-Python port. */
static int class_of_validated_instance_is_class(PyClassObject *cls) {
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    if (cls == NULL) return 0;
    return py_header((PyObject *)cls)->type_tag == PY_TYPE_CLASS;
}

static int instance_pointer_is_instance(PyInstanceObject *inst) {
    if (!pointer_can_have_header((void *)inst)) return 0;
    int32_t tag = py_header((PyObject *)inst)->type_tag;
    if (tag != PY_TYPE_INSTANCE && tag < PY_TYPE_USER_CLASS_START) return 0;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if (cls == NULL) return 0;
    return class_of_validated_instance_is_class(cls);
}

static PyObject *class_require_result(
    PyObject *result,
    const char *helper_name,
    const char *message
) {
    if (result == NULL) {
        py_runtime_error_if_unset(helper_name, message);
    }
    return result;
}

static void class_note_borrowed_metadata_slot_store(
    PyClassObject *cls,
    PyObject **slot,
    PyObject *value
);

static PyObject *class_call_binary_method(
    PyObject *func,
    PyObject *self,
    PyObject *arg
) {
    if (func == NULL) return NULL;
    if (pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(2);
        if (args == NULL) {
            return class_require_result(
                NULL,
                "py_tuple_new",
                "class callback argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        py_tuple_set_item(args, 1, arg);
        PyObject *out = py_func_call(func, args);
        class_require_result(
            out,
            "class callback",
            "class callback returned NULL without setting an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*BinaryMethod)(PyObject *, PyObject *);
    BinaryMethod meth = (BinaryMethod)(uintptr_t)func;
    return class_require_result(
        meth(self, arg),
        "class callback",
        "class callback returned NULL without setting an exception"
    );
}

static PyObject *class_call_ternary_method(
    PyObject *func,
    PyObject *self,
    PyObject *arg0,
    PyObject *arg1
) {
    if (func == NULL) return NULL;
    if (pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(3);
        if (args == NULL) {
            return class_require_result(
                NULL,
                "py_tuple_new",
                "class callback argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        py_tuple_set_item(args, 1, arg0);
        py_tuple_set_item(args, 2, arg1);
        PyObject *out = py_func_call(func, args);
        class_require_result(
            out,
            "class callback",
            "class callback returned NULL without setting an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*TernaryMethod)(PyObject *, PyObject *, PyObject *);
    TernaryMethod meth = (TernaryMethod)(uintptr_t)func;
    return class_require_result(
        meth(self, arg0, arg1),
        "class callback",
        "class callback returned NULL without setting an exception"
    );
}

static PyObject *class_call_unary_callable(PyObject *func, PyObject *arg) {
    if (func == NULL) return NULL;
    PyObject *args = py_tuple_new(1);
    if (args == NULL) {
        return class_require_result(
            NULL,
            "py_tuple_new",
            "class callback argument tuple allocation failed"
        );
    }
    py_tuple_set_item(args, 0, arg);
    PyObject *out = py_obj_call(func, args, py_None);
    class_require_result(
        out,
        "class callback",
        "class callback returned NULL without setting an exception"
    );
    py_decref(args);
    return out;
}

static PyObject *class_call_binary_callable(
    PyObject *func,
    PyObject *arg0,
    PyObject *arg1
) {
    if (func == NULL) return NULL;
    PyObject *args = py_tuple_new(2);
    if (args == NULL) {
        return class_require_result(
            NULL,
            "py_tuple_new",
            "class callback argument tuple allocation failed"
        );
    }
    py_tuple_set_item(args, 0, arg0);
    py_tuple_set_item(args, 1, arg1);
    PyObject *out = py_obj_call(func, args, py_None);
    class_require_result(
        out,
        "class callback",
        "class callback returned NULL without setting an exception"
    );
    py_decref(args);
    return out;
}

static void class_note_borrowed_metadata_store(
    PyClassObject *cls,
    PyObject *value
) {
    class_note_borrowed_metadata_slot_store(cls, NULL, value);
}

static void class_note_borrowed_metadata_slot_store(
    PyClassObject *cls,
    PyObject **slot,
    PyObject *value
) {
    if (!class_pointer_is_class(cls)) return;
    int64_t backend = pcc_gc_backend();
    if (
        backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR
        || backend == PCC_GC_KIND_CONCURRENT_MARK_SWEEP
        || backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    ) {
        pcc_gc_note_store();
    }
    pcc_gc_note_slot_write_barrier((PyObject *)cls, slot, value);
}

/* Safe names[] duplicator. We don't strdup — we keep a shallow copy of
 * the input pointer array. Field names are usually string literals in
 * the emitted module's read-only segment, so we can borrow. */
static const char **copy_name_array(const char **src, int32_t n) {
    if (n <= 0) return NULL;
    const char **out = (const char **)malloc(sizeof(char *) * (size_t)n);
    if (!out) return NULL;
    for (int32_t i = 0; i < n; i++) out[i] = src[i];
    return out;
}

static PyClassObject **copy_class_array(PyClassObject **src, int32_t n) {
    if (n <= 0) return NULL;
    PyClassObject **out = (PyClassObject **)malloc(sizeof(PyClassObject *) * (size_t)n);
    if (!out) return NULL;
    for (int32_t i = 0; i < n; i++) {
        out[i] = (PyClassObject *)pcc_gc_note_relocation_read(
            (PyObject *)src[i]
        );
    }
    return out;
}

/* ---- C3 linearization ------------------------------------------------- */

/* A "candidate head" algorithm implementation:
 *
 *   L[C] = C + merge(L[B1], L[B2], ..., L[Bn], [B1, B2, ..., Bn])
 *
 * where merge() iteratively pulls the head of the first sequence whose
 * head does not appear in the tail of any other sequence, and removes
 * that head from every sequence. Failure is detected when no candidate
 * is eligible.
 *
 * Our inputs are pre-existing MROs (each base's mro[]); we combine them
 * into a single owned MRO array including the "self" placeholder.
 * py_class_new is the sole caller and prepends the new class on top of
 * what c3_linearize returns — so this function only returns the merged
 * tail.
 *
 * Returns 0 on success; writes an owned malloc'd array to *out_mro and
 * the length to *out_n. Returns -1 on merge failure; in that case
 * *out_mro is NULL and *out_n = -1. Caller frees *out_mro. */

/* Internal mutable-sequence state: one per input list. */
typedef struct {
    PyClassObject **items;   /* NOT owned */
    int32_t         head;    /* index of next unconsumed element */
    int32_t         len;
} MergeSeq;

/* Is `cand` present in seq[head+1 .. len-1]? */
static int in_tail(const MergeSeq *seq, PyClassObject *cand) {
    cand = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cand);
    for (int32_t i = seq->head + 1; i < seq->len; i++) {
        PyClassObject *entry = (PyClassObject *)pcc_gc_note_relocation_read(
            (PyObject *)seq->items[i]
        );
        if (entry == cand) return 1;
    }
    return 0;
}

/* Consume the head of every sequence that starts with `cand`. */
static void consume_head(MergeSeq *seqs, int32_t nseqs, PyClassObject *cand) {
    cand = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cand);
    for (int32_t i = 0; i < nseqs; i++) {
        if (seqs[i].head < seqs[i].len) {
            PyClassObject *entry = (PyClassObject *)pcc_gc_note_relocation_read(
                (PyObject *)seqs[i].items[seqs[i].head]
            );
            if (entry != cand) continue;
            seqs[i].head++;
        }
    }
}

/* Pick the first eligible candidate (a head not found in any tail). */
static PyClassObject *pick_candidate(MergeSeq *seqs, int32_t nseqs) {
    for (int32_t i = 0; i < nseqs; i++) {
        if (seqs[i].head >= seqs[i].len) continue;  /* exhausted */
        PyClassObject *cand = (PyClassObject *)pcc_gc_note_relocation_read(
            (PyObject *)seqs[i].items[seqs[i].head]
        );
        int ok = 1;
        for (int32_t j = 0; j < nseqs; j++) {
            if (i == j) continue;
            if (in_tail(&seqs[j], cand)) { ok = 0; break; }
        }
        if (ok) return cand;
    }
    return NULL;   /* no candidate — MRO inconsistency */
}

int c3_linearize(PyClassObject **bases, int32_t n_bases,
                 PyClassObject ***out_mro, int32_t *out_n) {
    *out_mro = NULL;
    *out_n   = -1;

    /* Each base contributes its own mro[] PLUS the `bases` tail contributes
     * one more sequence (just the list of direct bases in order). So the
     * total sequence count is (n_bases + 1). Special case: zero bases ⇒
     * the resulting tail is empty (py_class_new will add the object
     * root). */
    if (n_bases == 0) {
        *out_mro = NULL;
        *out_n = 0;
        return 0;
    }

    int32_t nseqs = n_bases + 1;
    MergeSeq *seqs = (MergeSeq *)calloc((size_t)nseqs, sizeof(MergeSeq));
    if (!seqs) return -1;

    for (int32_t i = 0; i < n_bases; i++) {
        PyClassObject *b = bases[i];
        seqs[i].items = b->mro;
        seqs[i].head  = 0;
        seqs[i].len   = b->n_mro;
    }
    /* Last sequence: the bases themselves. */
    seqs[n_bases].items = bases;
    seqs[n_bases].head  = 0;
    seqs[n_bases].len   = n_bases;

    /* Upper bound on output length — sum of all input lengths. */
    int32_t cap = 0;
    for (int32_t i = 0; i < nseqs; i++) cap += seqs[i].len;

    PyClassObject **acc = (PyClassObject **)malloc(sizeof(PyClassObject *) * (size_t)cap);
    if (!acc) { free(seqs); return -1; }
    int32_t acc_len = 0;

    for (;;) {
        /* Are all sequences exhausted? */
        int any_remaining = 0;
        for (int32_t i = 0; i < nseqs; i++) {
            if (seqs[i].head < seqs[i].len) { any_remaining = 1; break; }
        }
        if (!any_remaining) break;

        PyClassObject *cand = pick_candidate(seqs, nseqs);
        if (cand == NULL) {
            free(acc);
            free(seqs);
            return -1;          /* inconsistent MRO */
        }
        acc[acc_len++] = cand;
        consume_head(seqs, nseqs, cand);
    }

    free(seqs);
    *out_mro = acc;
    *out_n = acc_len;
    return 0;
}

/* ---- py_class_new ----------------------------------------------------- */

/* Type-tag allocator. Starts after descriptor-reserved user tags and
 * monotonically increments.
 * Using a single allocator keeps tags unique across modules. In a future
 * phase this can be per-module. */
static int32_t g_next_user_tag = PY_TYPE_USER_CLASS_START;

PyClassObject *py_class_new(const char *name,
                            PyClassObject **bases, int32_t n_bases,
                            const char **field_names, int32_t n_fields) {
    PyClassObject *c = (PyClassObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyClassObject), PY_TYPE_CLASS, 0);
    if (!c) return NULL;
    memset((char *)c + sizeof(PyObjectHeader), 0,
           sizeof(PyClassObject) - sizeof(PyObjectHeader));
    /* Class/type objects are process-lifetime and are referenced by every
     * instance via a *borrowed* (uncounted) class pointer. Mark them
     * PY_FLAG_IMMORTAL so a stray over-release cannot free a live class out
     * from under its instances (which zeroes the field table and breaks all
     * subsequent attribute lookups). Mirrors py_class.py::py_class_new. */
    c->h.flags |= PY_FLAG_IMMORTAL;

    c->name       = name;
    c->n_bases    = n_bases;
    c->bases      = copy_class_array(bases, n_bases);
    if (n_bases > 0 && c->bases == NULL) {
        pcc_gc_free_object_memory((PyObject *)c);
        return NULL;
    }
    if (n_bases > 0) {
        (void)pcc_gc_backend4_zpage_register_owner_payload_span(
            (PyObject *)c,
            c->bases,
            n_bases * (int64_t)sizeof(PyClassObject *)
        );
    }
    c->n_fields   = n_fields;
    c->field_names = copy_name_array(field_names, n_fields);
    c->n_methods  = 0;
    c->methods    = NULL;
    c->type_tag_alloc = g_next_user_tag++;

    /* Instance size = header + class pointer + n_fields slots plus one
     * hidden dynamic-attribute dict slot.
     *
     * We compute using the concrete struct layout so we don't rely on
     * implementation-defined flexible-array sizeof behavior. */
    size_t inst_size = sizeof(PyInstanceObject) + sizeof(PyObject *) * (size_t)((n_fields > 0 ? n_fields : 0) + 1);
    /* Cap at INT32_MAX; nobody should need more, and this keeps the
     * field fit in int32_t. */
    if (inst_size > (size_t)INT32_MAX) inst_size = INT32_MAX;
    c->instance_size = (int32_t)inst_size;

    /* MRO: self + c3(linearize(bases)). Fallback when there are no bases
     * and this class is not the root itself: tail is [object_root]. */
    PyClassObject **tail = NULL;
    int32_t tail_len = 0;
    if (c3_linearize(c->bases, n_bases, &tail, &tail_len) != 0) {
        /* Inconsistent MRO. Clean up and return NULL. */
        free(c->bases);
        free((void *)c->field_names);
        pcc_gc_free_object_memory((PyObject *)c);
        return NULL;
    }

    /* Decide whether to append a root "object" class. Root is appended
     * only when the class has zero bases AND is not itself the root. */
    PyClassObject *root = object_root();
    int append_root = (n_bases == 0 && c != root);

    int32_t mro_len = 1 + tail_len + (append_root ? 1 : 0);
    c->mro = (PyClassObject **)malloc(sizeof(PyClassObject *) * (size_t)mro_len);
    if (!c->mro) {
        free(tail);
        free(c->bases);
        free((void *)c->field_names);
        pcc_gc_free_object_memory((PyObject *)c);
        return NULL;
    }
    c->mro[0] = c;
    for (int32_t i = 0; i < tail_len; i++) c->mro[1 + i] = tail[i];
    if (append_root) c->mro[mro_len - 1] = root;
    c->n_mro = mro_len;
    (void)pcc_gc_backend4_zpage_register_owner_payload_span(
        (PyObject *)c,
        c->mro,
        mro_len * (int64_t)sizeof(PyClassObject *)
    );

    c->del_method = py_class_lookup(c, "__del__");

    free(tail);
    pcc_gc_publish_initialized((PyObject *)c);
    return c;
}

void py_class_mark_slots_only(PyClassObject *cls) {
    if (!cls) return;
    cls->h.flags |= PY_CLASS_FLAG_SLOTS_ONLY;
}

void py_class_mark_dict_subclass(PyClassObject *cls) {
    if (!cls) return;
    cls->h.flags |= PY_CLASS_FLAG_DICT_SUBCLASS;
}

void py_class_set_metaclass(PyClassObject *cls, PyClassObject *metaclass) {
    if (!class_pointer_is_class(cls)) return;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    if (metaclass != NULL) {
        if (!class_pointer_is_class(metaclass)) return;
        metaclass = (PyClassObject *)pcc_gc_note_relocation_read(
            (PyObject *)metaclass
        );
    }
    cls->metaclass = metaclass;
    class_note_borrowed_metadata_slot_store(
        cls,
        (PyObject **)&cls->metaclass,
        (PyObject *)metaclass
    );
}

/* Lazily constructed root "object" class. It has no bases, no fields, no
 * methods. We only use it to anchor isinstance(x, object) and to appear
 * at the end of every class's MRO. Refcount is immortal-ish. */
static PyClassObject *object_root(void) {
    static PyClassObject *root = NULL;
    if (root != NULL) return root;
    PyClassObject **mro =
        (PyClassObject **)malloc(sizeof(PyClassObject *));
    if (mro == NULL) return NULL;
    /* `root` is cached in a process-global raw pointer, not a relocation-
     * updated GC root slot.  Keep its address stable while still publishing
     * exact managed-pointer provenance. */
    PyClassObject *r = (PyClassObject *)calloc(1, sizeof(PyClassObject));
    if (!r) { free(mro); return NULL; }
    r->h.refcount = 1;
    r->h.type_tag = PY_TYPE_CLASS;
    r->h.flags = PY_FLAG_IMMORTAL | PY_FLAG_GC_MALLOC_ALLOC;
    if (pcc_gc_pointer_register((PyObject *)r) < 0) {
        free(r);
        free(mro);
        return NULL;
    }
    r->name       = "object";
    r->n_bases    = 0;
    r->bases      = NULL;
    r->n_mro      = 1;
    r->mro        = mro;
    r->mro[0]     = r;
    r->n_fields   = 0;
    r->field_names = NULL;
    r->n_methods  = 0;
    r->methods    = NULL;
    r->instance_size = sizeof(PyInstanceObject) + sizeof(PyObject *);
    r->type_tag_alloc = PY_TYPE_INSTANCE;
    root = r;
    return root;
}

/* ---- Method table ----------------------------------------------------- */

/* `name` is a process-lifetime immutable borrowed C string.  Concurrent
 * readers may share an immutable table, but mutation of this realloc-backed
 * table is externally serialized and must not race with lookup. */
void py_class_add_method(PyClassObject *cls, const char *name, PyObject *func) {
    if (!class_pointer_is_class(cls) || !name) return;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    int32_t new_n = cls->n_methods + 1;
    PyClassMethod *old_methods = cls->methods;
    PyClassMethod *newarr = (PyClassMethod *)realloc(
        old_methods, sizeof(PyClassMethod) * (size_t)new_n);
    if (!newarr) return;   /* best-effort: drop on OOM */
    newarr[cls->n_methods].name = name;
    newarr[cls->n_methods].func = func;
    cls->methods = newarr;
    cls->n_methods = new_n;
    int64_t payload_offset = -1;
    if (old_methods != NULL && old_methods != newarr) {
        payload_offset = pcc_gc_backend4_zpage_retarget_owner_payload_span(
            (PyObject *)cls,
            old_methods,
            newarr,
            new_n * (int64_t)sizeof(PyClassMethod)
        );
    }
    if (payload_offset < 0) {
        if (old_methods != NULL && old_methods != newarr) {
            (void)pcc_gc_backend4_zpage_unregister_owner_payload_span(
                (PyObject *)cls,
                old_methods
            );
        }
        (void)pcc_gc_backend4_zpage_register_owner_payload_span(
            (PyObject *)cls,
            cls->methods,
            new_n * (int64_t)sizeof(PyClassMethod)
        );
    }
    class_note_borrowed_metadata_slot_store(
        cls,
        &cls->methods[cls->n_methods - 1].func,
        func
    );
    if (strcmp(name, "__del__") == 0) {
        __atomic_add_fetch(&pcc_class_del_defined_count, 1, __ATOMIC_RELEASE);
        cls->del_method = func;
        class_note_borrowed_metadata_slot_store(cls, &cls->del_method, func);
    }
    /* A newly installed data descriptor can shadow an instance field whose
     * location is already cached. Publish the completed mutation first. */
    __atomic_add_fetch(&py_class_attr_cache_epoch, 1, __ATOMIC_RELEASE);
}

/* Walk MRO and return the first method with the matching name. */
PyObject *py_class_lookup(PyClassObject *cls, const char *name) {
    if (!class_pointer_is_class(cls) || !name) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    if (strcmp(name, "__name__") == 0) {
        const char *cls_name = cls->name ? cls->name : "";
        return py_str_new(cls_name, (int64_t)strlen(cls_name));
    }
    if (strcmp(name, "__mro__") == 0) {
        PyObject *t = py_tuple_new(cls->n_mro);
        if (!t) return NULL;
        for (int32_t i = 0; i < cls->n_mro; i++) {
            PyClassObject *entry = (PyClassObject *)pcc_gc_load_ptr(
                (PyObject *)cls,
                (PyObject **)&cls->mro[i]
            );
            py_tuple_set_item(t, i, (PyObject *)entry);
        }
        return t;
    }
    if (strcmp(name, "__base__") == 0) {
        if (cls->n_bases <= 0 || cls->bases == NULL) return py_None;
        return pcc_gc_load_ptr(
            (PyObject *)cls,
            (PyObject **)&cls->bases[0]
        );
    }
    for (int32_t i = 0; i < cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)cls,
            (PyObject **)&cls->mro[i]
        );
        if (!m) continue;
        for (int32_t j = 0; j < m->n_methods; j++) {
            const char *method_name = m->methods[j].name;
            if (method_name && (method_name == name || strcmp(method_name, name) == 0)) {
                PyObject *func = pcc_gc_note_relocation_read(m->methods[j].func);
                if (func != m->methods[j].func) {
                    m->methods[j].func = func;
                }
                return func;
            }
        }
    }
    return NULL;
}

/* ---- Instance allocation --------------------------------------------- */

PyObject *py_instance_new(PyClassObject *cls) {
    if (!class_pointer_is_class(cls)) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    /* Allocate header + cls pointer + n_fields owned slots, plus one
     * hidden dynamic-attribute dict slot at fields[n_fields]. */
    size_t n_slots = (size_t)(cls->n_fields > 0 ? cls->n_fields : 0) + 1;
    size_t size = sizeof(PyInstanceObject) + sizeof(PyObject *) * n_slots;
    PyInstanceObject *inst = (PyInstanceObject *)pcc_gc_alloc(
        (int64_t)size, cls->type_tag_alloc, 0);
    if (!inst) return NULL;
    memset((char *)inst + sizeof(PyObjectHeader), 0,
           size - sizeof(PyObjectHeader));
    inst->cls        = cls;
    /* field slots are zero-initialized by calloc — which is the all-NULL
     * sentinel we need. */
    py_gc_track((PyObject *)inst);
    return (PyObject *)inst;
}

PyObject *py_valuebox_new(PyClassObject *cls) {
    if (!class_pointer_is_class(cls)) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    size_t n_slots = (size_t)(cls->n_fields > 0 ? cls->n_fields : 0) + 1;
    size_t size = sizeof(PyValueBoxObject) + sizeof(PyObject *) * n_slots;
    PyValueBoxObject *box = (PyValueBoxObject *)pcc_gc_alloc(
        (int64_t)size, PY_TYPE_VALUEBOX, 0
    );
    if (!box) return NULL;
    memset((char *)box + sizeof(PyObjectHeader), 0, size - sizeof(PyObjectHeader));
    box->cls        = cls;
    py_gc_track((PyObject *)box);
    return (PyObject *)box;
}

PyObject *py_instance_get_field(PyInstanceObject *inst, int32_t idx) {
    if (!instance_pointer_is_instance(inst) || idx < 0) return NULL;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if (idx >= cls->n_fields) return NULL;
    PyObject *v = pcc_gc_load_ptr((PyObject *)inst, &inst->fields[idx]);
    if (v) py_incref(v);
    return v;
}

PyObject *py_valuebox_get_field(PyValueBoxObject *box, int32_t idx) {
    return py_instance_get_field((PyInstanceObject *)box, idx);
}

void py_instance_set_field(PyInstanceObject *inst, int32_t idx, PyObject *value) {
    if (!instance_pointer_is_instance(inst) || idx < 0) return;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if (idx >= cls->n_fields) return;
    pcc_gc_store_ptr((PyObject *)inst, &inst->fields[idx], value);
}

void py_valuebox_set_field(PyValueBoxObject *box, int32_t idx, PyObject *value) {
    py_instance_set_field((PyInstanceObject *)box, idx, value);
}

/* Look up a field index by name on the instance's class (including
 * inherited fields — we walk MRO field_names to support simple
 * single-inheritance). Returns -1 if not found. */
static int32_t lookup_field_index(PyClassObject *cls, const char *name) {
    if (!class_pointer_is_class(cls) || !name) return -1;
    /* Field slot indices are per-class (the codegen allocates slots on
     * the most-derived class). In Phase 3 we only consult the leaf
     * class's field_names; inherited fields with identical names can
     * still live in the leaf's own slot table because the codegen
     * merges the declaration sets. */
    for (int32_t i = 0; i < cls->n_fields; i++) {
        const char *field_name = cls->field_names[i];
        if (field_name && (field_name == name || strcmp(field_name, name) == 0)) {
            return i;
        }
    }
    return -1;
}

static int32_t field_cache_slot(PyClassObject *cls, const char *name) {
    uintptr_t key = ((uintptr_t)cls >> 4) ^ ((uintptr_t)name >> 4);
    return (int32_t)(key & 3u);
}

static int32_t field_cache_lookup(PyClassObject *cls, const char *name) {
    int32_t epoch = class_attr_cache_epoch_load();
    if (py_inst_field_cache_epoch0 == epoch
        && py_inst_field_cache_cls0 == (PyObject *)cls
        && py_inst_field_cache_name0 == name) {
        return (int32_t)py_inst_field_cache_idx0;
    }
    if (py_inst_field_cache_epoch1 == epoch
        && py_inst_field_cache_cls1 == (PyObject *)cls
        && py_inst_field_cache_name1 == name) {
        return (int32_t)py_inst_field_cache_idx1;
    }
    if (py_inst_field_cache_epoch2 == epoch
        && py_inst_field_cache_cls2 == (PyObject *)cls
        && py_inst_field_cache_name2 == name) {
        return (int32_t)py_inst_field_cache_idx2;
    }
    if (py_inst_field_cache_epoch3 == epoch
        && py_inst_field_cache_cls3 == (PyObject *)cls
        && py_inst_field_cache_name3 == name) {
        return (int32_t)py_inst_field_cache_idx3;
    }
    return -1;
}

static void field_cache_store(PyClassObject *cls, const char *name, int32_t idx) {
    int32_t epoch = class_attr_cache_epoch_load();
    switch (field_cache_slot(cls, name)) {
        case 0:
            py_inst_field_cache_cls0 = (PyObject *)cls;
            py_inst_field_cache_name0 = name;
            py_inst_field_cache_idx0 = idx;
            py_inst_field_cache_epoch0 = epoch;
            return;
        case 1:
            py_inst_field_cache_cls1 = (PyObject *)cls;
            py_inst_field_cache_name1 = name;
            py_inst_field_cache_idx1 = idx;
            py_inst_field_cache_epoch1 = epoch;
            return;
        case 2:
            py_inst_field_cache_cls2 = (PyObject *)cls;
            py_inst_field_cache_name2 = name;
            py_inst_field_cache_idx2 = idx;
            py_inst_field_cache_epoch2 = epoch;
            return;
        default:
            py_inst_field_cache_cls3 = (PyObject *)cls;
            py_inst_field_cache_name3 = name;
            py_inst_field_cache_idx3 = idx;
            py_inst_field_cache_epoch3 = epoch;
            return;
    }
}

static PyObject **dynamic_attr_slot(PyInstanceObject *inst) {
    if (!instance_pointer_is_instance(inst)) return NULL;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if ((cls->h.flags & PY_CLASS_FLAG_SLOTS_ONLY) != 0) return NULL;
    int32_t n_fields = cls->n_fields;
    if (n_fields < 0) n_fields = 0;
    return &inst->fields[n_fields];
}

PyObject *py_instance_vars(PyInstanceObject *inst) {
    if (!instance_pointer_is_instance(inst)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "vars() argument has no __dict__"));
        return NULL;
    }
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if (!class_pointer_is_class(cls)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "vars() argument has no __dict__"));
        return NULL;
    }
    PyObject *out = py_dict_new();
    if (out == NULL) return NULL;
    int32_t n_fields = cls->n_fields;
    if (n_fields < 0) n_fields = 0;
    for (int32_t i = 0; i < n_fields; i++) {
        const char *field_name = cls->field_names != NULL ? cls->field_names[i] : NULL;
        if (field_name == NULL) continue;
        PyObject *value = pcc_gc_load_ptr((PyObject *)inst, &inst->fields[i]);
        if (value == NULL) continue;
        PyObject *key = py_str_new(field_name, (int64_t)strlen(field_name));
        if (key == NULL) {
            py_decref(out);
            return NULL;
        }
        py_dict_set(out, key, value);
        py_decref(key);
    }
    PyObject **dyn_slot = dynamic_attr_slot(inst);
    PyObject *dyn_obj = dyn_slot
        ? pcc_gc_load_ptr((PyObject *)inst, dyn_slot)
        : NULL;
    if (dyn_obj != NULL) {
        py_dict_update(out, dyn_obj);
    }
    return out;
}

PyObject *py_obj_vars(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "vars() argument has no __dict__"));
        return NULL;
    }
    int32_t tag = py_header(o)->type_tag;
    if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
        return py_instance_vars((PyInstanceObject *)o);
    }
    PyObject *dict = py_obj_getattr_default(o, "__dict__");
    if (dict != NULL) return dict;
    py_clear_exception();
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "vars() argument has no __dict__"));
    return NULL;
}

static PyObject *class_attr_lookup_in_mro(PyClassObject *cls, const char *name) {
    if (!class_pointer_is_class(cls) || !name) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    if (key == NULL) return NULL;
    for (int32_t i = 0; i < cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)cls,
            (PyObject **)&cls->mro[i]
        );
        if (!m) continue;
        PyObject *attrs = py_class_attrs_dict(m, 0);
        if (attrs == NULL) continue;
        PyObject *value = py_dict_get(attrs, key);
        if (value != NULL) {
            py_decref(key);
            return value;
        }
    }
    py_decref(key);
    return NULL;
}

static PyClassObject *descriptor_instance_class(PyObject *descriptor) {
    if (descriptor == NULL || PY_IS_TAGGED_INT(descriptor)) return NULL;
    PyInstanceObject *desc_inst = (PyInstanceObject *)descriptor;
    if (!instance_pointer_is_instance(desc_inst)) return NULL;
    return (PyClassObject *)pcc_gc_load_ptr(
        descriptor,
        (PyObject **)&desc_inst->cls
    );
}

static PyObject *descriptor_method(PyObject *descriptor, const char *name) {
    PyClassObject *desc_cls = descriptor_instance_class(descriptor);
    if (desc_cls == NULL) return NULL;
    return py_class_lookup(desc_cls, name);
}

static int descriptor_is_data(PyObject *descriptor) {
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        return 1;
    }
    return descriptor_method(descriptor, "__set__") != NULL
        || descriptor_method(descriptor, "__delete__") != NULL;
}

static PyObject *descriptor_call_get(
    PyObject *descriptor,
    PyObject *obj,
    PyClassObject *owner
) {
    if (descriptor != NULL && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_STATICMETHOD) {
        PyObject *func = pcc_gc_load_ptr(
            descriptor, &((PyStaticMethodObject *)descriptor)->func
        );
        py_incref(func);
        return func;
    }
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        PyPropertyObject *prop = (PyPropertyObject *)descriptor;
        PyObject *fget = pcc_gc_load_ptr(descriptor, &prop->fget);
        if (fget == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_ATTRIBUTEERROR, "unreadable attribute"));
            return NULL;
        }
        if (obj == NULL || obj == py_None) {
            py_incref(descriptor);
            return descriptor;
        }
        (void)owner;
        return class_call_unary_callable(fget, obj);
    }
    PyObject *get_method = descriptor_method(descriptor, "__get__");
    if (get_method == NULL) return NULL;
    return class_call_ternary_method(
        get_method,
        descriptor,
        obj != NULL ? obj : py_None,
        (PyObject *)owner
    );
}

static int64_t descriptor_call_set(
    PyObject *descriptor,
    PyObject *obj,
    PyObject *value
) {
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        PyPropertyObject *prop = (PyPropertyObject *)descriptor;
        PyObject *fset = pcc_gc_load_ptr(descriptor, &prop->fset);
        if (fset == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_ATTRIBUTEERROR, "can't set attribute"));
            return -1;
        }
        PyObject *out = class_call_binary_callable(fset, obj, value);
        if (out == NULL) return -1;
        py_decref(out);
        return 0;
    }
    PyObject *set_method = descriptor_method(descriptor, "__set__");
    if (set_method == NULL) return -1;
    PyObject *out = class_call_ternary_method(
        set_method,
        descriptor,
        obj,
        value
    );
    if (out == NULL) return -1;
    py_decref(out);
    return 0;
}

static int64_t descriptor_call_delete(PyObject *descriptor, PyObject *obj) {
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        PyPropertyObject *prop = (PyPropertyObject *)descriptor;
        PyObject *fdel = pcc_gc_load_ptr(descriptor, &prop->fdel);
        if (fdel == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_ATTRIBUTEERROR, "can't delete attribute"));
            return -1;
        }
        PyObject *out = class_call_unary_callable(fdel, obj);
        if (out == NULL) return -1;
        py_decref(out);
        return 0;
    }
    PyObject *delete_method = descriptor_method(descriptor, "__delete__");
    if (delete_method == NULL) return -1;
    PyObject *out = class_call_binary_method(delete_method, descriptor, obj);
    if (out == NULL) return -1;
    py_decref(out);
    return 0;
}

PyObject *py_instance_getattr_default(PyInstanceObject *inst, const char *name) {
    if (!instance_pointer_is_instance(inst) || !name) return NULL;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if (strcmp(name, "__class__") == 0) {
        PyObject *cls_obj = (PyObject *)cls;
        py_incref(cls_obj);
        return cls_obj;
    }
    if (strcmp(name, "__dict__") == 0) {
        PyObject **dyn_slot = dynamic_attr_slot(inst);
        if (!dyn_slot) return NULL;
        PyObject *dyn_obj = pcc_gc_load_ptr((PyObject *)inst, dyn_slot);
        if (!dyn_obj) {
            PyObject *dyn = py_dict_new();
            if (!dyn) return NULL;
            pcc_gc_store_ptr((PyObject *)inst, dyn_slot, dyn);
            py_decref(dyn);
            dyn_obj = pcc_gc_load_ptr((PyObject *)inst, dyn_slot);
            if (!dyn_obj) return NULL;
        }
        py_incref(dyn_obj);
        return dyn_obj;
    }
    int32_t cached_idx = field_cache_lookup(cls, name);
    if (cached_idx >= 0) {
        PyObject *v = pcc_gc_load_ptr((PyObject *)inst, &inst->fields[cached_idx]);
        if (v) py_incref(v);
        return v;
    }
    PyObject *class_attr = class_attr_lookup_in_mro(cls, name);
    if (class_attr != NULL && descriptor_is_data(class_attr)) {
        PyObject *out = descriptor_call_get(class_attr, (PyObject *)inst, cls);
        py_decref(class_attr);
        if (out != NULL || py_err_occurred()) return out;
    }
    int32_t idx = lookup_field_index(cls, name);
    if (idx >= 0) {
        field_cache_store(cls, name, idx);
        /* Return a new reference so callers can uniformly py_decref. */
        PyObject *v = pcc_gc_load_ptr((PyObject *)inst, &inst->fields[idx]);
        if (v) py_incref(v);
        return v;
    }
    PyObject **dyn_slot = dynamic_attr_slot(inst);
    PyObject *dyn_obj = dyn_slot
        ? pcc_gc_load_ptr((PyObject *)inst, dyn_slot)
        : NULL;
    if (dyn_obj) {
        PyObject *key = py_str_new(name, (int64_t)strlen(name));
        PyObject *v = py_dict_get(dyn_obj, key);
        py_decref(key);
        if (v) return v;
    }
    if (class_attr != NULL) {
        if (!PY_IS_TAGGED_INT(class_attr) && py_type_of(class_attr) == PY_TYPE_FUNC) {
            PyObject *bound = py_instance_bind_method(
                class_attr,
                (PyObject *)inst,
                name
            );
            py_decref(class_attr);
            return bound;
        }
        PyObject *out = descriptor_call_get(class_attr, (PyObject *)inst, cls);
        if (out != NULL || py_err_occurred()) {
            py_decref(class_attr);
            return out;
        }
        return class_attr;
    }
    /* Method fallthrough — returns a borrowed ref. The codegen is aware
     * that class-lookup results don't need refcounting. */
    PyObject *method = py_class_lookup(cls, name);
    if (method != NULL) {
        return py_instance_bind_method(method, (PyObject *)inst, name);
    }

    /* dict-subclass inherited method fallback (get / keys / values / items /
     * update / setdefault / pop / ...). User methods/attrs above win; this only
     * fires for names the user class did not define. Returns a bound native
     * callable, or NULL (no such dict method — fall through to __getattr__). */
    if ((cls->h.flags & PY_CLASS_FLAG_DICT_SUBCLASS) != 0) {
        PyObject *dm = py_dict_subclass_getattr((PyObject *)inst, name);
        if (dm != NULL || py_err_occurred()) return dm;
    }

    PyObject *getattr_method = py_class_lookup(cls, "__getattr__");
    if (getattr_method == NULL) return NULL;
    PyObject *name_obj = py_str_new(name, (int64_t)strlen(name));
    if (name_obj == NULL) return NULL;
    PyObject *out = class_call_binary_method(
        getattr_method, (PyObject *)inst, name_obj
    );
    py_decref(name_obj);
    return out;
}

PyObject *py_instance_getattr(PyInstanceObject *inst, const char *name) {
    if (!instance_pointer_is_instance(inst) || !name) return NULL;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    PyObject *getattribute_method = py_class_lookup(cls, "__getattribute__");
    if (getattribute_method != NULL) {
        PyObject *name_obj = py_str_new(name, (int64_t)strlen(name));
        if (name_obj == NULL) return NULL;
        PyObject *out = class_call_binary_method(
            getattribute_method, (PyObject *)inst, name_obj
        );
        if (out != NULL) {
            py_decref(name_obj);
            return out;
        }
        if (py_err_occurred()) {
            PyObject *cur = py_current_exception();
            PyClassObject *attr_cls =
                py_exc_builtin_class(PY_EXC_ATTRIBUTEERROR);
            if (attr_cls != NULL &&
                py_exc_matches(cur, (PyObject *)attr_cls)) {
                PyObject *getattr_method = py_class_lookup(cls, "__getattr__");
                if (getattr_method != NULL) {
                    py_clear_exception();
                    PyObject *fallback = class_call_binary_method(
                        getattr_method, (PyObject *)inst, name_obj
                    );
                    py_decref(name_obj);
                    return fallback;
                }
            }
            py_decref(name_obj);
            return NULL;
        }
        py_decref(name_obj);
        return NULL;
    }
    return py_instance_getattr_default(inst, name);
}

int64_t py_instance_setattr(PyInstanceObject *inst, const char *name, PyObject *value) {
    if (!instance_pointer_is_instance(inst) || !name) return -1;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    PyObject *class_attr = class_attr_lookup_in_mro(cls, name);
    if (class_attr != NULL) {
        PyObject *set_method = descriptor_method(class_attr, "__set__");
        if (set_method != NULL) {
            int64_t rc = descriptor_call_set(class_attr, (PyObject *)inst, value);
            py_decref(class_attr);
            return rc;
        }
        py_decref(class_attr);
    }
    int32_t idx = lookup_field_index(cls, name);
    if (idx >= 0) {
        py_instance_set_field(inst, idx, value);
        return 0;
    }
    PyObject **dyn_slot = dynamic_attr_slot(inst);
    if (!dyn_slot || value == NULL) return -1;
    PyObject *dyn_obj = pcc_gc_load_ptr((PyObject *)inst, dyn_slot);
    if (!dyn_obj) {
        PyObject *dyn = py_dict_new();
        if (!dyn) return -1;
        pcc_gc_store_ptr((PyObject *)inst, dyn_slot, dyn);
        py_decref(dyn);
        dyn_obj = pcc_gc_load_ptr((PyObject *)inst, dyn_slot);
        if (!dyn_obj) return -1;
    }
    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    py_dict_set(dyn_obj, key, value);
    py_decref(key);
    return 0;
}

int64_t py_instance_delattr(PyInstanceObject *inst, const char *name) {
    if (!instance_pointer_is_instance(inst) || !name) return -1;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    PyObject *class_attr = class_attr_lookup_in_mro(cls, name);
    if (class_attr != NULL) {
        PyObject *delete_method = descriptor_method(class_attr, "__delete__");
        if (delete_method != NULL) {
            int64_t rc = descriptor_call_delete(class_attr, (PyObject *)inst);
            py_decref(class_attr);
            return rc;
        }
        py_decref(class_attr);
    }
    int32_t idx = lookup_field_index(cls, name);
    if (idx >= 0) {
        if (!pcc_gc_load_ptr((PyObject *)inst, &inst->fields[idx])) return -1;
        py_instance_set_field(inst, idx, NULL);
        return 0;
    }
    PyObject **dyn_slot = dynamic_attr_slot(inst);
    PyObject *dyn_obj = dyn_slot
        ? pcc_gc_load_ptr((PyObject *)inst, dyn_slot)
        : NULL;
    if (!dyn_obj) return -1;
    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    int64_t rc = py_dict_del(dyn_obj, key);
    py_decref(key);
    return rc;
}

static PyInstanceObject *dataclass_copy_instance(PyObject *obj,
                                                 PyClassObject **cls_out) {
    if (!pointer_can_have_header((void *)obj)) return NULL;
    int32_t tag = py_header(obj)->type_tag;
    if (tag != PY_TYPE_INSTANCE && tag < PY_TYPE_USER_CLASS_START) return NULL;

    PyInstanceObject *src = (PyInstanceObject *)obj;
    if (!instance_pointer_is_instance(src)) return NULL;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)src,
        (PyObject **)&src->cls
    );
    if (cls_out) *cls_out = cls;

    PyInstanceObject *dst = (PyInstanceObject *)py_instance_new(cls);
    if (!dst) return NULL;

    for (int32_t i = 0; i < cls->n_fields; i++) {
        PyObject *v = pcc_gc_load_ptr((PyObject *)src, &src->fields[i]);
        if (!v) continue;
        pcc_gc_store_ptr((PyObject *)dst, &dst->fields[i], v);
    }
    PyObject **src_dyn = dynamic_attr_slot(src);
    PyObject **dst_dyn = dynamic_attr_slot(dst);
    PyObject *src_dyn_obj = src_dyn
        ? pcc_gc_load_ptr((PyObject *)src, src_dyn)
        : NULL;
    if (src_dyn_obj && dst_dyn) {
        pcc_gc_store_ptr((PyObject *)dst, dst_dyn, src_dyn_obj);
    }
    return dst;
}

PyObject *py_dataclass_replace(PyObject *obj, int64_t n_overrides,
                               const char **names, PyObject **values) {
    PyClassObject *cls = NULL;
    PyInstanceObject *dst = dataclass_copy_instance(obj, &cls);
    if (!dst || !cls) return NULL;

    for (int64_t i = 0; i < n_overrides; i++) {
        const char *name = names ? names[i] : NULL;
        PyObject *value = values ? values[i] : NULL;
        int32_t idx = lookup_field_index(cls, name);
        if (idx < 0) {
            py_decref((PyObject *)dst);
            return NULL;
        }
        py_instance_set_field(dst, idx, value);
    }

    return (PyObject *)dst;
}

PyObject *py_dataclass_replace_from_dict(PyObject *obj, PyObject *overrides) {
    PyClassObject *cls = NULL;
    PyInstanceObject *dst = dataclass_copy_instance(obj, &cls);
    if (!dst || !cls) return NULL;

    if (!pointer_can_have_header((void *)overrides) ||
        py_header(overrides)->type_tag != PY_TYPE_DICT) {
        py_decref((PyObject *)dst);
        return NULL;
    }

    PyDictObject *d = (PyDictObject *)overrides;
    for (int64_t i = 0; i < d->entries_used; i++) {
        DictEntry *e = &d->entries[i];
        if (!e->key) continue;
        const char *name = py_str_utf8(e->key);
        int32_t idx = lookup_field_index(cls, name);
        if (idx < 0) {
            py_decref((PyObject *)dst);
            return NULL;
        }
        py_instance_set_field(dst, idx, e->value);
    }

    return (PyObject *)dst;
}

/* ---- isinstance ------------------------------------------------------- */

int64_t py_isinstance(PyObject *obj, PyClassObject *cls) {
    if (!obj || !class_pointer_is_class(cls)) return 0;
    if (!pointer_can_have_header((void *)obj)) return 0;
    obj = pcc_gc_note_relocation_read(obj);
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    int32_t tag = py_header(obj)->type_tag;
    if (tag == PY_TYPE_EXC) {
        /* Builtin exception objects match via their exc_class MRO, not the
         * instance->cls path below (which is only for PY_TYPE_INSTANCE/user).
         * Without this isinstance(ValueError('x'), ValueError) was False. */
        return py_exc_matches(obj, (PyObject *)cls) ? 1 : 0;
    }
    if (tag != PY_TYPE_INSTANCE && tag < PY_TYPE_USER_CLASS_START) return 0;
    PyInstanceObject *inst = (PyInstanceObject *)obj;
    if (!instance_pointer_is_instance(inst)) return 0;
    PyClassObject *c = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst,
        (PyObject **)&inst->cls
    );
    if (c == cls) return 1;
    for (int32_t i = 0; i < c->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)c,
            (PyObject **)&c->mro[i]
        );
        if (m == cls) return 1;
    }
    return 0;
}

/* ---- super() lookup --------------------------------------------------- */

/* Find `from_cls` in `start_cls->mro`, then look for `name` in every
 * method table AFTER that entry. This is the standard Python super()
 * behavior per PEP 3135. */
PyObject *py_super_lookup(PyClassObject *start_cls,
                          PyClassObject *from_cls,
                          const char *name) {
    if (!class_pointer_is_class(start_cls) ||
        !class_pointer_is_class(from_cls) ||
        !name) return NULL;
    start_cls = (PyClassObject *)pcc_gc_note_relocation_read(
        (PyObject *)start_cls
    );
    from_cls = (PyClassObject *)pcc_gc_note_relocation_read(
        (PyObject *)from_cls
    );

    int32_t start = -1;
    for (int32_t i = 0; i < start_cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)start_cls,
            (PyObject **)&start_cls->mro[i]
        );
        if (m == from_cls) { start = i; break; }
    }
    if (start < 0) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "super(type, obj): obj must be an instance or subtype of type"
        ));
        return NULL;
    }

    for (int32_t i = start + 1; i < start_cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)start_cls,
            (PyObject **)&start_cls->mro[i]
        );
        if (!m) continue;
        for (int32_t j = 0; j < m->n_methods; j++) {
            const char *method_name = m->methods[j].name;
            if (method_name && (method_name == name || strcmp(method_name, name) == 0)) {
                PyObject *func = pcc_gc_note_relocation_read(m->methods[j].func);
                if (func != m->methods[j].func) {
                    m->methods[j].func = func;
                }
                return func;
            }
        }
    }
    py_raise_owned(py_exc_new(
        PY_EXC_ATTRIBUTEERROR,
        "super object has no attribute"
    ));
    return NULL;
}

/* ---- Deallocators ---------------------------------------------------- */

void py_class_dealloc(PyObject *o) {
    if (!o) return;
    PyClassObject *c = (PyClassObject *)o;
    py_class_attrs_dispose(c);
    free(c->bases);
    free(c->mro);
    free(c->methods);
    free((void *)c->field_names);
    pcc_gc_free_object_memory(o);
}

static void descriptor_release_slot(PyObject *owner, PyObject **slot) {
    PyObject *value = pcc_gc_load_ptr(owner, slot);
    *slot = NULL;
    if (value != NULL) py_decref(value);
}

void py_descriptor_dealloc(PyObject *o) {
    if (!o) return;
    int32_t tag = py_header(o)->type_tag;
    if (tag == PY_TYPE_PROPERTY) {
        PyPropertyObject *property = (PyPropertyObject *)o;
        descriptor_release_slot(o, &property->fget);
        descriptor_release_slot(o, &property->fset);
        descriptor_release_slot(o, &property->fdel);
    } else if (tag == PY_TYPE_CLASSMETHOD) {
        PyClassMethodObject *method = (PyClassMethodObject *)o;
        descriptor_release_slot(o, &method->func);
    } else if (tag == PY_TYPE_STATICMETHOD) {
        PyStaticMethodObject *method = (PyStaticMethodObject *)o;
        descriptor_release_slot(o, &method->func);
    }
    pcc_gc_free_object_memory(o);
}

void py_instance_dealloc(PyObject *o) {
    if (!o) return;
    PyInstanceObject *inst = (PyInstanceObject *)o;
    py_weakref_invalidate(o);
    py_user_del_dispatch(o);
    if (py_header(o)->refcount > 0) {
        py_gc_track(o);
        int64_t backend = pcc_gc_backend();
        int metadata_valid = 1;
        if (
            backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
            || backend == PCC_GC_KIND_COLORED_RELOCATING
        ) {
            metadata_valid = pcc_gc_pointer_is_managed(o) != 0
                && pcc_gc_object_index_find(o) != NULL;
        }
        PCC_RT_TRIPWIRE(
            metadata_valid,
            "resurrected instance lost GC metadata before DEALLOCATING clear"
        );
        if (!metadata_valid) return;
        py_header_flags_and(py_header(o), ~PY_FLAG_GC_DEALLOCATING);
        return;
    }
    if (instance_pointer_is_instance(inst)) {
        PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
            o,
            (PyObject **)&inst->cls
        );
        for (int32_t i = 0; i < cls->n_fields; i++) {
            PyObject *v = pcc_gc_load_ptr(o, &inst->fields[i]);
            /* Null the slot before decref so a finalizer that re-enters
             * py_instance_dealloc (its __del__ arg-tuple holds self and drops
             * self to 0 on free) reads NULL and does not double-release this
             * field. Mirrors py_class_dealloc. */
            if (v) { inst->fields[i] = NULL; py_decref(v); }
        }
        PyObject **dyn_slot = dynamic_attr_slot(inst);
        if (dyn_slot) {
            PyObject *dyn = pcc_gc_load_ptr(o, dyn_slot);
            if (dyn) { *dyn_slot = NULL; py_decref(dyn); }
        }
    }
    int delayed_zpage_note = (
        pcc_gc_backend() == PCC_GC_KIND_COLORED_RELOCATING
        && (py_header_flags_load(py_header(o)) & PY_FLAG_GC_ZPAGE_ALLOC) != 0
    );
    if (!delayed_zpage_note) {
        pcc_gc_note_object_freeing(o);
        py_gc_untrack(o);
        pcc_gc_free_object_memory(o);
    } else {
        py_gc_untrack(o);
        pcc_gc_free_object_memory(o);
        if (pcc_gc_pointer_is_managed(o) != 0) {
            pcc_gc_note_object_freeing(o);
        }
    }
}
