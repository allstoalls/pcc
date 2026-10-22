
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
extern int64_t baseline_probe(int64_t, int64_t);
extern int64_t candidate_probe(int64_t, int64_t);
extern int64_t pcc_refcount_load(void *);

static int64_t invoke(int arm, int64_t first, int64_t second) {
    if (arm == 0) return baseline_probe(first, second);
    return candidate_probe(first, second);
}

int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    if (pcc_gc_backend() != atoi(argv[1])) return 16;
    PyObject *roots[4] = {NULL, NULL, NULL, NULL};
    int32_t map[1] = {4};
    pcc_gc_frame_enter(map, roots);
    int64_t root_count = pcc_gc_frame_root_slot_count();
    int64_t owners[2] = {0, 0};

    for (int mode = 0; mode < 2; ++mode) {
        for (int arm = 0; arm < 2; ++arm) {
            py_clear_exception();
            if (invoke(arm, 0, 0) != 17 || py_err_occurred()) return 2;
            int64_t first = mode == 0 ? -1 : 0;
            int64_t second = mode == 0 ? 0 : -1;
            if (invoke(arm, first, second) != -mode - 1) return 3;
            pcc_gc_store_root(&roots[0], py_current_exception());
            if (!roots[0]) return 4;
            if (!py_exc_matches(roots[0], (PyObject *)py_exc_builtin_class(PY_EXC_RUNTIMEERROR))) return 5;
            if (py_exc_traceback_len(roots[0]) != mode) return 6;
            owners[arm] = pcc_refcount_load(roots[0]);
            pcc_gc_store_root_take(&roots[arm + 1], py_exc_traceback_format_exc(roots[0]));
            if (!roots[arm + 1]) return 7;
            py_clear_exception();
            pcc_gc_store_root(&roots[0], NULL);
            py_gc_collect();
            if (pcc_gc_frame_root_slot_count() != root_count) return 8;
        }
        /* Exact message and source-frame order, including no helper frame. */
        if (!py_str_eq(roots[1], roots[2])) return 9;
        /* Preserve the existing borrowed py_raise ownership convention. */
        if (owners[0] != owners[1]) return 10;
        pcc_gc_store_root(&roots[1], NULL);
        pcc_gc_store_root(&roots[2], NULL);
    }

    pcc_gc_store_root_take(&roots[0], py_exc_new(PY_EXC_VALUEERROR, "original"));
    py_exc_append_frame_source(roots[0], "original_owner", "original.py", "raise original", 23);
    py_raise(roots[0]);
    for (int arm = 0; arm < 2; ++arm) {
        if (invoke(arm, 0, 0) != 17 || py_current_exception() != roots[0]) return 11;
        if (invoke(arm, -1, 0) != -1 || py_current_exception() != roots[0]) return 12;
        if (invoke(arm, 0, -1) != -2 || py_current_exception() != roots[0]) return 13;
        if (py_exc_traceback_len(roots[0]) != 1) return 14;
        if (pcc_gc_frame_root_slot_count() != root_count) return 15;
    }
    py_clear_exception();
    pcc_gc_store_root(&roots[0], NULL);
    py_gc_collect();
    pcc_gc_frame_leave(roots);
    puts("slot-status-runtime-equal");
    return 0;
}
