"""A completed result can move to the caller without constructing an exception."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("runtime_kind", ["c", "py"])
def test_completed_result_handoff_preserves_owners_and_pending_errors(tmp_path, request, runtime_kind):
    archive = request.getfixturevalue("c_runtime_archive" if runtime_kind == "c" else "pcc_py_runtime_archive")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "take_completed.c"
    source.write_text('''#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
extern PyObject *py_gen_take_completed(PyObject *gen);
static PyObject *ordinary_resume(PyObject *gen, PyObject *frame) {
    return py_gen_finish(gen, frame);
}
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    PyObject *value = py_list_new(0);
    PyObject *item = py_int_from_i64(42);
    py_list_append(value, item);
    py_decref(item);
    PyObject *roots[2] = {py_gen_completed(value), NULL};
    int32_t map[1] = {2};
    pcc_gc_frame_enter(map, roots);
    py_decref(value);
    py_gc_collect();
    PyObject *error = py_exc_new(2, "pending");
    py_raise(error);
    py_decref(error);
    if (py_gen_take_completed(roots[0]) != NULL) return 2;
    if (py_gen_is_done(roots[0]) || !py_current_exception()) return 3;
    py_clear_exception();
    /* Borrowed result stays owned by the completed generator until capture. */
    value = py_gen_take_completed(roots[0]);
    if (!value || py_current_exception()) return 4;
    pcc_gc_store_root(&roots[1], value);
    if (!py_gen_is_done(roots[0])) return 5;
    if (py_gen_take_completed(roots[0]) != NULL || py_current_exception()) return 6;
    pcc_gc_store_root(&roots[0], NULL);
    py_gc_collect();
    if (py_list_len(roots[1]) != 1) return 7;
    item = py_list_get(roots[1], 0);
    int overflow = 0;
    if (py_int_to_i64(item, &overflow) != 42 || overflow) return 8;
    py_decref(item);
    pcc_gc_store_root(&roots[1], NULL);
    roots[0] = py_gen_new((void *)ordinary_resume, py_None);
    if (py_gen_take_completed(roots[0]) != NULL || py_current_exception()) return 9;
    if (py_gen_next(roots[0]) != NULL || !py_current_exception()) return 10;
    py_clear_exception();
    pcc_gc_store_root(&roots[0], NULL);
    roots[0] = py_gen_completed(py_None);
    if (py_gen_take_completed(roots[0]) != py_None || py_current_exception()) return 11;
    pcc_gc_store_root(&roots[0], NULL);
    pcc_gc_frame_leave(roots);
    puts("completed-result-handoff-ok");
    return 0;
}
''')
    executable = tmp_path / "take_completed"
    compiled = subprocess.run(["clang", "-I" + str(root / "pcc/py_runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable)],
        capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "completed-result-handoff-ok"
