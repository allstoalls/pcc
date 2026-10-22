/* Deterministic sweep-boundary regressions on the real tracing runtime.
 * This fixture injects only the pending CANDIDATE verdict on fully initialized
 * graphs under STW and the graph lock. It does not reproduce a worker race or
 * the Darwin typed-loop failure. Every mode runs in a fresh process.
 */
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

enum { WHITE = 8, PINNED = 64, CANDIDATE = 1024, FRESH = 16384, DEAD = 524288 };
enum { KEEPER = 0, CLASS = 1, CALLBACK = 2, WEAK_FIRST = 3, WEAK_SECOND = 4,
       TEMP_FIRST = 5, TEMP_SECOND = 6, INTERIOR = 7, GC_CALLBACK = 8, ROOT_COUNT = 9 };

extern int32_t pcc_gc_mark_active;
extern int32_t pcc_gc_explicit_collect_active;
extern int32_t pcc_gc_trace_extension_roots_pending;
extern int64_t pcc_gc_tracing_finish_claim_epoch;
extern int64_t pcc_gc_debt_threshold_override;
extern int64_t pcc_gc_debt_bytes;
extern int64_t pcc_gc_gray_count_load_acquire(void);
extern int64_t pcc_gc_object_is_known_no_lock(PyObject *);
extern int64_t pcc_gc_sweep_owed(void);
extern void pcc_gc_begin_explicit_tracing_collect(void);
extern void pcc_gc_end_explicit_tracing_collect(void);
extern int64_t pcc_gc_collect_tracing(void);
extern int64_t pcc_gc_tracing_sweep_unreachable(int64_t);
extern void pcc_gc_cms_stop_worker(void);
extern PyClassObject *py_class_new(const char *, PyClassObject **, int32_t,
                                  const char **, int32_t);
extern void py_class_add_method(PyClassObject *, const char *, PyObject *);
extern PyObject *py_instance_new(PyClassObject *);
extern void py_instance_set_field(PyObject *, int32_t, PyObject *);
extern PyObject *py_instance_get_field(PyObject *, int32_t);

static PyObject *roots[ROOT_COUNT];
static const int32_t root_map[1] = {ROOT_COUNT};
static PyObject *watched[2]; /* address diagnostics, never roots or owners */
static int mode;
static int held_world;
static int phase;
static int finalizers;
static int weak_callbacks;
static int gc_callbacks;
static int callback_error;

static PyObject *weak_callback(PyObject *captures, PyObject *args) {
    (void)captures;
    (void)args;
    weak_callbacks++;
    return py_int_from_i64(0);
}

static PyObject *start_callback(PyObject *captures, PyObject *args) {
    (void)captures;
    (void)args;
    gc_callbacks++;
    if (gc_callbacks == 1) py_list_append(roots[KEEPER], watched[0]);
    return py_int_from_i64(0);
}

static PyObject *token_del(PyObject *self) {
    if (self != watched[0] && self != watched[1]) callback_error = 1;
    finalizers++;
    /* Every callback sees the cycle intact, including the later finalizer in
     * a batch where an earlier callback has already resurrected the graph. */
    PyObject *peer = py_instance_get_field(self, 0);
    if (peer == 0 || (peer != watched[0] && peer != watched[1])) callback_error = 2;
    if (peer != 0) py_decref(peer);
    if (mode == 3 || (mode == 4 && finalizers == 1)) {
        py_list_append(roots[KEEPER], self);
    }
    /* The public entry guard must still prevent nested collection. */
    if (pcc_gc_collect(0) != 0) callback_error = 3;
    return py_int_from_i64(0);
}

static int phase_clean(void) {
    return pcc_gc_mark_active == 0
        && pcc_gc_explicit_collect_active == 0
        && pcc_gc_trace_extension_roots_pending == 0
        && pcc_gc_tracing_finish_claim_epoch == 0
        && pcc_gc_gray_count_load_acquire() == 0;
}

static int debt_safe(void) {
    return pcc_gc_debt_threshold_override == 1099511627776LL
        && pcc_gc_debt_bytes < pcc_gc_debt_threshold_override;
}

static int initialize(void) {
    phase = 1;
    int backend = pcc_gc_backend();
    if (backend < 1 || backend > 4) return 101;
    if (sizeof(void *) != 8 || sizeof(PyObjectHeader) != 16) return 102;
    if (pcc_threads_enabled() != 1) return 103;
    if (pcc_refcount_strategy() != PCC_REFCOUNT_STRATEGY_ATOMIC) return 104;
    if (pcc_current_thread_id() <= 0 || !debt_safe()) return 105;
    if (pcc_stop_the_world() != 0) return 106;
    held_world = 1;
    if (pcc_thread_owns_stopped_world() != 1 || pcc_thread_no_park_depth() != 0) return 107;
    (void)pcc_gc_collect(0);
    if (!phase_clean() || pcc_gc_sweep_owed() != 0) return 108;
    pcc_gc_frame_enter(root_map, roots);
    return 0;
}

static int install_root(int slot, PyObject *value) {
    if (value == 0) return 0;
    pcc_gc_store_root(&roots[slot], value);
    py_decref(value);
    return 1;
}

static int build_graph(void) {
    phase = 2;
    static const char *fields[1] = {"peer"};
    PyClassObject *cls = py_class_new("SweepVerdictToken", 0, 0, fields, 1);
    if (!install_root(CLASS, (PyObject *)cls)) return 120;
    py_class_add_method((PyClassObject *)roots[CLASS], "__del__",
                        (PyObject *)(uintptr_t)token_del);
    if (!install_root(KEEPER, py_list_new(1))) return 121;
    if (!install_root(INTERIOR, py_list_new(1))) return 122;
    if (!install_root(CALLBACK, py_func_new((void *)weak_callback, 0))) return 123;
    if (!install_root(TEMP_FIRST, py_instance_new((PyClassObject *)roots[CLASS]))) return 124;
    int count = mode == 4 || mode == 7 ? 2 : 1;
    if (count == 2 && !install_root(TEMP_SECOND, py_instance_new((PyClassObject *)roots[CLASS]))) return 125;
    int index;
    for (index = 0; index < count; index++) {
        PyObject *self = roots[TEMP_FIRST + index];
        PyObject *peer = mode == 4 ? roots[TEMP_FIRST + 1 - index] : self;
        py_instance_set_field(self, 0, peer);
        if (!install_root(WEAK_FIRST + index, py_weakref_new(self, roots[CALLBACK]))) return 126;
    }
    if (mode == 1) {
        py_list_append(roots[INTERIOR], roots[TEMP_FIRST]);
        py_list_append(roots[KEEPER], roots[INTERIOR]);
    }
    if (mode == 5) {
        if (!install_root(GC_CALLBACK, py_func_new((void *)start_callback, 0))) return 127;
        py_gc_callbacks_append(roots[GC_CALLBACK]);
    }
    for (index = 0; index < count; index++) {
        watched[index] = roots[TEMP_FIRST + index];
        pcc_gc_store_root(&roots[TEMP_FIRST + index], 0);
    }
    pcc_gc_store_root(&roots[INTERIOR], 0);
    if (!phase_clean() || !debt_safe()) return 128;
    return 0;
}

static int inject_candidates(int count) {
    phase = 3;
    pcc_py_gc_minor_graph_lock();
    int index;
    for (index = 0; index < count; index++) {
        PyObject *obj = watched[index];
        if (pcc_gc_object_is_known_no_lock(obj) != 1) {
            pcc_py_gc_minor_graph_unlock();
            return 130;
        }
        PyObjectHeader *header = (PyObjectHeader *)obj;
        if ((header->flags & (PINNED | DEAD)) != 0 || header->refcount != (mode == 1 ? 2 : 1)) {
            pcc_py_gc_minor_graph_unlock();
            return 131;
        }
        header->flags = (header->flags & ~FRESH) | CANDIDATE;
    }
    pcc_py_gc_minor_graph_unlock();
    return pcc_gc_sweep_owed() == 1 && finalizers == 0 ? 0 : 132;
}

static int weak_matches(int index, int live) {
    PyObject *target = py_weakref_call(roots[WEAK_FIRST + index]);
    int same = target == (live ? watched[index] : py_None);
    if (target != 0) py_decref(target);
    return same;
}

static int inspect_live(int index) {
    PyObject *obj = watched[index];
    if (pcc_gc_object_is_known_no_lock(obj) != 1) return 0;
    if ((((PyObjectHeader *)obj)->flags & CANDIDATE) != 0) return 0;
    PyObject *peer = py_instance_get_field(obj, 0);
    int same = peer == watched[mode == 4 ? 1 - index : index];
    if (peer != 0) py_decref(peer);
    return same && weak_matches(index, 1);
}

static int run_mode(void) {
    phase = 4;
    int count = mode == 4 || mode == 7 ? 2 : 1;
    int64_t collected;
    if (mode == 6) {
        int before = ((PyObjectHeader *)watched[0])->flags;
        if (pcc_gc_tracing_sweep_unreachable(0) != 0
            || pcc_gc_tracing_sweep_unreachable(-1) != 0) return 140;
        if (finalizers != 0 || weak_callbacks != 0
            || ((PyObjectHeader *)watched[0])->flags != before) return 141;
        /* Leave the no-work check in a safe live state before process exit. */
        py_list_append(roots[KEEPER], watched[0]);
    }
    if (mode == 5) {
        collected = pcc_gc_collect(0);
        /* The public full collection can relocate the now-live graph on
         * GC3/4. Resolve the diagnostic address before checking it. */
        watched[0] = pcc_gc_note_relocation_read(watched[0]);
    } else {
        pcc_gc_begin_explicit_tracing_collect();
        collected = mode == 7 ? pcc_gc_tracing_sweep_unreachable(1) : pcc_gc_collect_tracing();
        pcc_gc_end_explicit_tracing_collect();
    }
    /* Address-index snapshots precede any allocation after reclamation. */
    int known_first = (int)pcc_gc_object_is_known_no_lock(watched[0]);
    int known_second = count == 2 ? (int)pcc_gc_object_is_known_no_lock(watched[1]) : 0;
    if (callback_error != 0) return 142;
    if (mode == 1 || mode == 5 || mode == 6) {
        if (finalizers != 0 || weak_callbacks != 0 || collected != 0 || known_first != 1) return 143;
        if (!inspect_live(0)) return 144;
        if (mode == 5 && gc_callbacks != 2) return 145;
    } else if (mode == 2) {
        if (finalizers != 1 || weak_callbacks != 1 || collected != 1 || known_first != 0) return 146;
        if (!weak_matches(0, 0)) return 147;
        pcc_gc_begin_explicit_tracing_collect();
        if (pcc_gc_collect_tracing() != 0) return 148;
        pcc_gc_end_explicit_tracing_collect();
        if (finalizers != 1 || weak_callbacks != 1) return 149;
    } else if (mode == 3 || mode == 4) {
        if (finalizers != count || weak_callbacks != 0 || collected != 0 || known_first != 1) return 150;
        if (!inspect_live(0)) return 151;
        if (count == 2 && (known_second != 1 || !inspect_live(1))) return 152;
    } else if (mode == 7) {
        if (finalizers != 2 || weak_callbacks != 1 || collected != 1 || known_first + known_second != 1) return 153;
        if (!weak_matches(0, known_first) || !weak_matches(1, known_second)) return 154;
        pcc_gc_begin_explicit_tracing_collect();
        int64_t next = pcc_gc_tracing_sweep_unreachable(1);
        pcc_gc_end_explicit_tracing_collect();
        int known_after = (int)pcc_gc_object_is_known_no_lock(watched[0])
                          + (int)pcc_gc_object_is_known_no_lock(watched[1]);
        if (next != 1 || known_after != 0 || finalizers != 2 || weak_callbacks != 2) return 155;
        if (!weak_matches(0, 0) || !weak_matches(1, 0)) return 156;
    }
    printf("sweep-verdict:ok backend=%d mode=%d finalizers=%d weak_callbacks=%d gc_callbacks=%d\n",
           pcc_gc_backend(), mode, finalizers, weak_callbacks, gc_callbacks);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2 || argv[1][0] < '1' || argv[1][0] > '7' || argv[1][1] != 0) return 100;
    mode = argv[1][0] - '0';
    int status = initialize();
    if (status == 0) status = build_graph();
    if (status == 0) status = inject_candidates(mode == 4 || mode == 7 ? 2 : 1);
    if (status == 0) status = run_mode();
    if (status != 0) printf("sweep-verdict:blocked status=%d phase=%d mode=%d\n", status, phase, mode);
    if (held_world != 0) {
        if (pcc_resume_world() != 0 && status == 0) status = 157;
        pcc_gc_cms_stop_worker();
    }
    return status;
}
