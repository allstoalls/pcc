"""GC4 tracking-index elision does not erase ownership of live leaf objects."""
import subprocess
from pathlib import Path
import pytest

PROGRAM = r'''
#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern int64_t pcc_refcount_load(void *);
extern int64_t pcc_gc_unmanaged_refcount_ops;
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1]))) return 1;
    char data[256]; memset(data, 'x', sizeof(data));
    PyObject *value = py_str_new(data, sizeof(data));
    PyObject *slot = NULL;
    int64_t initial = pcc_refcount_load(value);
    pcc_gc_store_root(&slot, value);
    if (pcc_refcount_load(value) != initial + 1) return 2;
    pcc_gc_store_root(&slot, value);
    if (pcc_refcount_load(value) != initial + 1) return 3;
    pcc_gc_store_root(&slot, NULL);
    if (pcc_refcount_load(value) != initial) return 4;
    PyObject *set = py_set_new();
    pcc_gc_pin(set);
    py_set_add(set, value);
    if (pcc_refcount_load(value) != initial + 1) return 5;
    if (py_set_remove(set, value) != 0) return 6;
    if (pcc_refcount_load(value) != initial) return 7;
    pcc_gc_unpin(set);
    py_decref(set);
    py_decref(value);
    if (atoi(argv[1]) == 4) {
        void *raw = malloc(32);
        int64_t invalid_before = pcc_gc_unmanaged_refcount_ops;
        slot = (PyObject *)raw;
        pcc_gc_store_root(&slot, NULL);
        if (slot != NULL || pcc_gc_unmanaged_refcount_ops != invalid_before) return 8;
        free(raw);
    }
    puts("LEAF_ROOT_OK");
    return 0;
}
'''

@pytest.mark.parametrize("runtime_fixture", ["pcc_py_runtime_archive", "c_runtime_archive"])
def test_leaf_root_self_store_and_clear(tmp_path, request, runtime_fixture):
    archive = request.getfixturevalue(runtime_fixture)
    source = tmp_path / "leaf_root.c"
    source.write_text(PROGRAM)
    binary = tmp_path / "leaf_root"
    include = Path(__file__).resolve().parents[2] / "pcc/py_runtime/include"
    build = subprocess.run(["clang", "-I", str(include), str(source), str(archive),
                            "-pthread", "-o", str(binary)], capture_output=True,
                           text=True, timeout=30)
    assert build.returncode == 0, build.stderr
    for backend in (0, 4):
        ran = subprocess.run([str(binary), str(backend)], capture_output=True,
                             text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend} exit={ran.returncode}: {ran.stdout}{ran.stderr}"
        assert ran.stdout.strip() == "LEAF_ROOT_OK"
