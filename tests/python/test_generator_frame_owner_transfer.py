"""Frame stores may consume an owned local, but must retain borrowed values."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("runtime_kind", ["c", "py"])
def test_frame_store_transfers_only_owned_sources(tmp_path, request, runtime_kind):
    archive = request.getfixturevalue("c_runtime_archive" if runtime_kind == "c" else "pcc_py_runtime_archive")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "frame_owners.c"
    source.write_text('''#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
extern void py_list_set_from_owned_root(PyObject *, int64_t, void *, void *);
extern int64_t pcc_gc_try_store_ptr_take(PyObject *, PyObject **, PyObject *);
extern int32_t py_gc_tracked_count;
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    int32_t baseline = py_gc_tracked_count;
    PyObject *roots[3] = {py_gen_frame_new(1), py_list_new(0), NULL};
    int32_t map[1] = {3};
    pcc_gc_frame_enter(map, roots);
    PyObject *number = py_int_from_i64(42);
    py_list_append(roots[1], number);
    py_decref(number);
    unsigned char owned = 1;
    py_list_set_from_owned_root(roots[0], 5, &roots[1], &owned);
    if (owned != 1) return 2; /* failed stores cannot consume the source */
    py_list_set_from_owned_root(roots[0], 0, &roots[1], &owned);
    if (owned != (atoi(argv[1]) != 0)) return 3;
    if (owned) pcc_gc_store_root(&roots[1], NULL);
    else roots[1] = NULL; /* its reference now belongs to the frame */
    py_gc_collect();
    roots[1] = py_list_get(roots[0], 0);
    if (py_list_len(roots[1]) != 1) return 8;
    number = py_list_get(roots[1], 0);
    int overflow = 0;
    if (py_int_to_i64(number, &overflow) != 42 || overflow) return 9;
    py_decref(number);
    owned = 1;
    /* Same-value stores must consume the extra source owner too. */
    py_list_set_from_owned_root(roots[0], 0, &roots[1], &owned);
    if (owned) pcc_gc_store_root(&roots[1], NULL);
    else roots[1] = NULL;
    py_gc_collect();
    roots[1] = py_list_new(0);
    PyObject *borrowed = roots[1];
    owned = 0;
    py_list_set_from_owned_root(roots[0], 0, &borrowed, &owned);
    if (owned != 0) return 4;
    pcc_gc_store_root(&roots[1], NULL);
    py_gc_collect();
    roots[1] = py_list_get(roots[0], 0);
    if (py_list_len(roots[1]) != 0) return 5;
    /* The primitive's unsupported path leaves both owner and slot alone. */
    py_incref(roots[1]);
    int64_t moved = pcc_gc_try_store_ptr_take(NULL, &roots[2], roots[1]);
    if (moved != (atoi(argv[1]) == 0)) return 6;
    if (!moved) {
        if (roots[2] != NULL) return 7;
        py_decref(roots[1]);
    }
    pcc_gc_store_root(&roots[2], NULL);
    pcc_gc_store_root(&roots[1], NULL);
    pcc_gc_store_root(&roots[0], NULL);
    pcc_gc_frame_leave(roots);
    py_gc_collect();
    if (atoi(argv[1]) == 0 && py_gc_tracked_count != baseline) return 10;
    puts("frame-owner-transfer-ok");
    return 0;
}
''')
    executable = tmp_path / "frame_owners"
    built = subprocess.run(["clang", "-I" + str(root / "pcc/py_runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable)],
        capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "frame-owner-transfer-ok"
