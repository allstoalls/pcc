"""A task may own its generator directly while preserving scheduler outcomes."""

from pathlib import Path
import subprocess
import re

import pytest


def test_direct_generator_spawn_omits_only_its_redundant_continuation(tmp_path, monkeypatch):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "direct_spawn_shape.py"
    source.write_text('''import pcc.virtual_thread as vt
def yielding():
    vt.yield_now()
    return 42
def ordinary():
    return 7
left = vt.spawn(yielding)
right = vt.spawn(ordinary)
vt.run(1, 32)
print(vt.result(left), vt.result(right))
''')
    counts = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("PCC_DIRECT_GENERATOR_TASKS", enabled)
        output = tmp_path / ("spawn_" + enabled + ".ll")
        compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                       libpython_mode="off", ir_scaffold_mode="on")
        counts.append(len(re.findall(r"call[^\n]*@py_continuation_new_typed\(", output.read_text())))
    assert counts == [2, 1], "ordinary callbacks still need their typed captured slots"


@pytest.mark.parametrize("runtime_kind", ["c", "py"])
def test_direct_generator_task_yield_result_cancel_and_failure(tmp_path, request, runtime_kind):
    archive = request.getfixturevalue("c_runtime_archive" if runtime_kind == "c" else "pcc_py_runtime_archive")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "direct_generator.c"
    source.write_text('''#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
static int calls = 0;
static PyObject *resume(PyObject *gen, PyObject *frame) {
    calls++;
    if (py_gen_state(gen) == 0) {
        py_gen_set_state(gen, 1);
        py_incref(py_None);
        return py_None;
    }
    return py_gen_finish(gen, frame);
}
static PyObject *fail_resume(PyObject *gen, PyObject *frame) {
    (void)gen; (void)frame;
    py_raise_owned(py_exc_new(2, "direct generator failed"));
    return NULL;
}
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    PyObject *value = py_int_from_i64(42);
    PyObject *gen = py_gen_new((void *)resume, value);
    py_decref(value);
    PyObject *roots[1] = {py_virtual_thread_new(gen)};
    int32_t map[1] = {1};
    pcc_gc_frame_enter(map, roots);
    py_decref(gen);
    py_gc_collect();
    if (py_virtual_thread_start(roots[0]) != 0) return 2;
    if (py_virtual_thread_run_until_idle(16) <= 0) return 3;
    if (calls != 2 || py_virtual_thread_outcome(roots[0]) != 1) return 4;
    value = py_virtual_thread_result(roots[0]);
    int overflow = 0;
    if (py_int_to_i64(value, &overflow) != 42 || overflow) return 5;
    py_decref(value);
    pcc_gc_store_root(&roots[0], NULL);
    calls = 0;
    gen = py_gen_new((void *)resume, py_None);
    roots[0] = py_virtual_thread_new(gen);
    py_decref(gen);
    if (py_virtual_thread_start(roots[0]) != 0) return 6;
    if (py_virtual_thread_cancel(roots[0]) < 0) return 7;
    if (py_virtual_thread_run_until_idle(16) < 0) return 8;
    if (calls != 0 || py_virtual_thread_outcome(roots[0]) != 3) return 9;
    pcc_gc_store_root(&roots[0], NULL);
    gen = py_gen_new((void *)fail_resume, py_None);
    roots[0] = py_virtual_thread_new(gen);
    py_decref(gen);
    if (py_virtual_thread_start(roots[0]) != 0) return 10;
    if (py_virtual_thread_run_until_idle(16) < 0) return 11;
    if (py_virtual_thread_outcome(roots[0]) != 2 || py_current_exception()) return 12;
    pcc_gc_store_root(&roots[0], NULL);
    pcc_gc_frame_leave(roots);
    if (py_virtual_thread_ready_count() || py_virtual_thread_timer_count() || py_virtual_thread_io_wait_count()) return 13;
    puts("direct-generator-task-ok");
    return 0;
}
''')
    executable = tmp_path / "direct_generator"
    built = subprocess.run(["clang", "-I" + str(root / "pcc/py_runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable)],
        capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "direct-generator-task-ok"
