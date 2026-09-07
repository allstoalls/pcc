"""Batch saving keeps source/frame owners and the tracing-collector fallback."""

from pathlib import Path
import re
import subprocess

import pytest


def test_bulk_save_dispatch_keeps_the_ordinary_fallback(tmp_path, monkeypatch):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "suspended.py"
    source.write_text('''def suspended(seed):
    first = [seed]
    second = first
    yield first
    second.append(seed)
    yield second
iterator = suspended(42)
print(next(iterator))
print(next(iterator))
''')
    calls = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("PCC_BULK_GENERATOR_FRAME_SAVE", enabled)
        output = tmp_path / ("save_" + enabled + ".ll")
        compile_python(str(source), str(output), backend="self", libpython_mode="off",
                       ir_scaffold_mode="on", emit_llvm_only=True)
        text = output.read_text()
        body = re.search(r"define[^\n]*suspended__gen_resume[^\n]*\{\n(.*?)\n\}", text, re.S)
        assert body is not None
        calls.append(len(re.findall(r"call[^\n]*@py_gen_frame_save\(", body.group(1))))
        if enabled == "1":
            assert "gen.save.fallback" in body.group(1)
            assert re.search(r"call[^\n]*@py_list_set\(", body.group(1))
    assert calls == [0, 2], "one bulk dispatch per yield, with the old collector path retained"


@pytest.mark.parametrize("runtime_kind", ["c", "py"])
def test_bulk_save_keeps_aliases_and_falls_back_without_mutation(tmp_path, request, runtime_kind):
    archive = request.getfixturevalue("c_runtime_archive" if runtime_kind == "c" else "pcc_py_runtime_archive")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "bulk_save.c"
    source.write_text('''#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
extern int64_t py_gen_frame_save(PyObject *, void *, int64_t);
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    PyObject *roots[3] = {py_gen_frame_new(2), py_list_new(0), py_list_new(0)};
    int32_t map[1] = {3};
    pcc_gc_frame_enter(map, roots);
    PyObject *value = py_int_from_i64(42);
    py_list_append(roots[1], value);
    py_decref(value);
    PyObject **slots[2] = {&roots[1], &roots[2]};
    if (py_gen_frame_save(roots[0], slots, 1) != 0) return 2;
    if (py_gen_frame_save(roots[0], NULL, 2) != 0) return 3;
    value = py_list_get(roots[0], 0);
    if (value != py_None) return 4;
    py_decref(value);
    py_gc_collect();
    int64_t saved = py_gen_frame_save(roots[0], slots, 2);
    if (saved != (atoi(argv[1]) == 0)) return 5;
    if (!saved) {
        value = py_list_get(roots[0], 0);
        if (value != py_None) return 6;
        py_decref(value);
        py_list_set(roots[0], 0, roots[1]);
        py_list_set(roots[0], 1, roots[2]);
    }
    /* Both saved slots may alias the same independently owned source. */
    pcc_gc_store_root(&roots[2], roots[1]);
    saved = py_gen_frame_save(roots[0], slots, 2);
    if (!saved) {
        py_list_set(roots[0], 0, roots[1]);
        py_list_set(roots[0], 1, roots[2]);
    }
    pcc_gc_store_root(&roots[1], NULL);
    pcc_gc_store_root(&roots[2], NULL);
    py_gc_collect();
    PyObject *left = py_list_get(roots[0], 0);
    PyObject *right = py_list_get(roots[0], 1);
    if (left != right || py_list_len(left) != 1) return 7;
    value = py_list_get(left, 0);
    int overflow = 0;
    if (py_int_to_i64(value, &overflow) != 42 || overflow) return 8;
    py_decref(value);
    py_decref(left);
    py_decref(right);
    pcc_gc_store_root(&roots[0], NULL);
    pcc_gc_frame_leave(roots);
    py_gc_collect();
    puts("bulk-frame-save-ok");
    return 0;
}
''')
    executable = tmp_path / "bulk_save"
    built = subprocess.run(["clang", "-I" + str(root / "pcc/py_runtime/include"),
        str(source), str(archive), "-pthread", "-o", str(executable)],
        capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "bulk-frame-save-ok"
