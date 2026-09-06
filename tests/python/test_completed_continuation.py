"""Completed continuations keep the generator protocol and managed value owner."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("runtime_kind", ["c", "py"])
def test_completed_continuation_value_survives_every_collector(tmp_path, request, runtime_kind):
    archive = request.getfixturevalue("c_runtime_archive" if runtime_kind == "c" else "pcc_py_runtime_archive")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "completed.c"
    source.write_text('''#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
extern PyObject *py_gen_completed(PyObject *value);
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    PyObject *value = py_list_new(0);
    PyObject *item = py_int_from_i64(42);
    py_list_append(value, item);
    py_decref(item);
    PyObject *roots[1] = {py_gen_completed(value)};
    int32_t map[1] = {1};
    if (!roots[0]) return 2;
    pcc_gc_frame_enter(map, roots);
    py_decref(value);
    py_gc_collect();
    if (!py_gen_is_may_park(roots[0])) return 3;
    if (py_gen_next(roots[0]) != NULL) return 4;
    PyObject *error = py_current_exception();
    if (!error || !py_exc_matches(error, (PyObject *)py_exc_builtin_class(8))) return 5;
    PyObject *result = py_exc_get_message(error);
    if (py_list_len(result) != 1) return 6;
    item = py_list_get(result, 0);
    int overflow = 0;
    if (py_int_to_i64(item, &overflow) != 42 || overflow) return 7;
    py_decref(item);
    py_clear_exception();
    if (!py_gen_is_done(roots[0])) return 8;
    if (py_gen_next(roots[0]) != NULL || !py_current_exception()) return 9;
    py_clear_exception();
    pcc_gc_frame_leave(roots);
    py_decref(roots[0]);
    roots[0] = py_gen_completed(py_None);
    if (!roots[0] || py_gen_next(roots[0]) != NULL) return 10;
    if (!py_exc_matches(py_current_exception(), (PyObject *)py_exc_builtin_class(8))) return 11;
    py_clear_exception();
    py_decref(roots[0]);
    roots[0] = py_gen_completed(py_None);
    error = py_exc_new(2, "injected");
    if (py_gen_throw(roots[0], error) != NULL) return 12;
    py_decref(error);
    if (!py_exc_matches(py_current_exception(), (PyObject *)py_exc_builtin_class(2))) return 13;
    py_clear_exception();
    py_decref(roots[0]);
    puts("completed-continuation-ok");
    return 0;
}
''')
    executable = tmp_path / "completed"
    compiled = subprocess.run(["clang", "-I" + str(root / "pcc/py_runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable)],
        capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "completed-continuation-ok"
