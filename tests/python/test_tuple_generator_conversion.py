"""tuple consumes iterators and preserves their exceptions and owned values."""
import os
import subprocess
from pathlib import Path

import pytest

PROGRAM = """import gc
class Value:
    def __init__(self, number):
        self.number = number
def values():
    yield Value(11)
    gc.collect()
    yield Value(22)
def broken():
    yield Value(33)
    raise ValueError("iterator failed")
def main():
    result = tuple(values())
    assert len(result) == 2
    assert result[0].number == 11
    assert result[1].number == 22
    assert tuple(x for x in [3, 4]) == (3, 4)
    assert tuple(x for x in []) == ()
    try:
        tuple(broken())
        raise RuntimeError("lost exception")
    except ValueError as error:
        assert str(error) == "iterator failed"
    gc.collect()
    print("TUPLE_ITERATOR_OK")
main()
"""

def test_tuple_generator_contents_and_error(tmp_path: Path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python
    source = tmp_path / "tuple_iterator.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "tuple_iterator"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout.strip() == "TUPLE_ITERATOR_OK"


# External C harness is a differential reference, not native C qualification.
RUNTIME_PROBE = r"""
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
extern int64_t pcc_refcount_load(void *);
extern int64_t pcc_gc_unmanaged_refcount_ops;
static int fail_after_yield;
static PyObject *resume(PyObject *gen, PyObject *frame) {
    int64_t state = py_gen_state(gen);
    if (state == 0) {
        py_gen_set_state(gen, 1);
        return py_list_get(frame, 0);
    }
    if (fail_after_yield) {
        py_gen_set_done(gen);
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "iterator failed"));
        return NULL;
    }
    return py_gen_finish(gen, py_None);
}
int main(int argc, char **argv) {
    if (argc != 2) return 1;
    int backend = atoi(argv[1]);
    if (pcc_gc_set_backend(backend)) return 1;
    PyObject *roots[4] = {NULL};
    int32_t map[] = {4};
    pcc_gc_frame_enter(map, roots);
    pcc_gc_store_root_take(&roots[0], py_list_new(0));
    pcc_gc_store_root_take(&roots[1], py_list_new(0));
    py_list_append(roots[1], roots[0]);
    int64_t owners = pcc_refcount_load(roots[0]);
    for (int mode = 0; mode < 2; ++mode) {
        fail_after_yield = mode;
        for (int repeat = 0; repeat < 4; ++repeat) {
            pcc_gc_store_root_take(&roots[2], py_gen_new((void *)resume, roots[1]));
            pcc_gc_store_root_take(&roots[3], py_tuple_from_splat(roots[2]));
            if (mode) {
                if (roots[3] != NULL || !py_exc_matches(py_current_exception(),
                        (PyObject *)py_exc_builtin_class(PY_EXC_VALUEERROR))) return 2;
                py_clear_exception();
            } else {
                if (py_err_occurred() || py_tuple_len(roots[3]) != 1) return 3;
                PyObject *item = py_tuple_get(roots[3], 0);
                if (item != roots[0]) return 4;
                py_decref(item);
            }
            pcc_gc_store_root(&roots[3], NULL);
            pcc_gc_store_root(&roots[2], NULL);
            py_gc_collect();
            if (backend == 0 && pcc_refcount_load(roots[0]) != owners) return 5;
        }
    }
    for (int i = 0; i < 4; ++i) pcc_gc_store_root(&roots[i], NULL);
    pcc_gc_frame_leave(roots);
    py_gc_collect();
    if (pcc_gc_unmanaged_refcount_ops != 0) return 6;
    puts("TUPLE_RUNTIME_OK");
    return 0;
}
"""

@pytest.mark.parametrize("runtime_fixture", ["pcc_py_runtime_archive", "c_runtime_archive"])
def test_tuple_iterator_runtime_mirrors(tmp_path, request, runtime_fixture):
    archive = request.getfixturevalue(runtime_fixture)
    source = tmp_path / "tuple_runtime.c"
    source.write_text(RUNTIME_PROBE)
    binary = tmp_path / "tuple_runtime"
    include = Path(__file__).resolve().parents[2] / "pcc/py_runtime/include"
    built = subprocess.run(["clang", "-I", str(include), str(source), str(archive),
                            "-pthread", "-o", str(binary)], capture_output=True,
                           text=True, timeout=30)
    assert built.returncode == 0, built.stderr
    for backend in range(5):
        ran = subprocess.run([str(binary), str(backend)], capture_output=True,
                             text=True, timeout=20)
        assert ran.returncode == 0, f"GC{backend} exit={ran.returncode}: {ran.stdout}{ran.stderr}"
        assert ran.stdout.strip() == "TUPLE_RUNTIME_OK"
