"""Retaining stores preserve ownership on self-assignment; take still consumes."""

from pathlib import Path
import subprocess


def test_refcount_store_self_assignment_preserves_distinct_owners(tmp_path: Path, pcc_runtime_archive):
    archive = pcc_runtime_archive
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "self_assignment.c"
    source.write_text('''#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
static int64_t refs(PyObject *value) { return *(int64_t *)value; }
int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 1;
    PyObject *value = py_list_new(0);
    PyObject *root = NULL;
    PyObject *container = py_list_new(0);
    if (!value || !container) return 2;
    pcc_gc_store_root(&root, value);
    py_list_append(container, value);
    if (refs(value) != 3) return 3;
    for (int i = 0; i < 1000; i++) {
        pcc_gc_store_root(&root, value);
        py_list_set(container, 0, value);
        if (refs(value) != 3) return 4;
    }
    py_incref(value);
    pcc_gc_store_root_take(&root, value);
    if (refs(value) != 3) return 5;
    pcc_gc_store_root(&root, NULL);
    pcc_gc_store_root(&root, NULL);
    if (refs(value) != 2) return 6;
    py_decref(container);
    if (refs(value) != 1) return 7;
    py_decref(value);
    pcc_gc_store_root(&root, py_None);
    pcc_gc_store_root(&root, py_None);
    if (root != py_None) return 8;
    pcc_gc_store_root(&root, NULL);
    puts("self-assignment-ownership-ok");
    return 0;
}
''')
    executable = tmp_path / "self_assignment"
    built = subprocess.run([
        "clang", "-std=c11", "-I" + str(root / "pcc/runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable),
    ], capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "self-assignment-ownership-ok"
