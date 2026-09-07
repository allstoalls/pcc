"""Frame stores may consume an owned local, but must retain borrowed values."""

from pathlib import Path
import os
import re
import subprocess
import sys

import pytest


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_frame_replacement_preserves_finalizers_during_collection(tmp_path, monkeypatch, pcc_py_runtime_archive, enabled):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "frame_finalizers.py"
    source.write_text('''import gc
events = []
class Token:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        events.append(self.label)
        gc.collect()
def worker():
    first = Token("first")
    second = Token("second")
    yield None
    first = Token("new-first")
    second = Token("new-second")
    yield None
def exercise():
    iterator = worker()
    next(iterator)
    print(events)
    next(iterator)
    print(events)
    iterator.close()
exercise()
gc.collect()
print(sorted(events))
''')
    oracle = subprocess.run([sys.executable, str(source)], text=True, capture_output=True, check=True, timeout=10)
    source.write_text('from pcc.extern import extern, c_int64\n'
        'backend = extern("pcc_gc_backend", (), c_int64)\n'
        'print(backend())\n' + source.read_text())
    monkeypatch.setenv("PCC_MOVE_GENERATOR_FRAME_OWNERS", enabled)
    executable = tmp_path / "frame_finalizers"
    compile_python(str(source), str(executable), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(executable)], text=True, capture_output=True, timeout=15,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout == str(backend) + "\n" + oracle.stdout


def test_unaccepted_frame_moves_stay_out_of_application_codegen(tmp_path, monkeypatch):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "owner_shape.py"
    source.write_text('''def worker(seed):
    saved = [seed]
    yield saved
iterator = worker(42)
print(next(iterator))
''')
    counts = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("PCC_MOVE_GENERATOR_FRAME_OWNERS", enabled)
        output = tmp_path / ("owner_" + enabled + ".ll")
        compile_python(str(source), str(output), emit_llvm_only=True,
                       backend="self", libpython_mode="off", ir_scaffold_mode="on")
        body = re.search(r"define[^\n]*worker__gen_resume[^\n]*\{\n(.*?)\n\}", output.read_text(), re.S)
        assert body is not None
        calls = re.findall(r"call[^\n]*@py_list_set_from_owned_root\([^\n]+", body.group(1))
        counts.append(len(calls))
        if enabled == "1":
            assert "gen.save.addresses" not in body.group(1)
            assert not re.search(r"call[^\n]*@py_list_get_for_frame\(", body.group(1))
    assert counts == [0, 0], "the unaccepted experiment must not change application codegen"


@pytest.mark.parametrize("runtime_kind", ["c", "py"])
def test_frame_restore_and_save_move_one_owner(tmp_path, request, runtime_kind):
    archive = request.getfixturevalue("c_runtime_archive" if runtime_kind == "c" else "pcc_py_runtime_archive")
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "frame_roundtrip.c"
    source.write_text('''#include "py_runtime.h"
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    int64_t baseline = py_gc_get_count(0);
    PyObject *roots[2] = {py_gen_frame_new(1), py_list_new(0)};
    int32_t map[1] = {2};
    pcc_gc_frame_enter(map, roots);
    PyObject *number = py_int_from_i64(42);
    py_list_append(roots[1], number);
    py_decref(number);
    py_list_set(roots[0], 0, roots[1]);
    pcc_gc_store_root(&roots[1], NULL);
    for (int iteration = 0; iteration < 10; iteration++) {
        roots[1] = py_list_get_for_frame(roots[0], 0);
        PyObject *saved = py_list_get(roots[0], 0);
        if ((pcc_gc_backend() == 0 && saved != py_None) ||
            (pcc_gc_backend() != 0 && saved != roots[1])) return 2;
        py_decref(saved);
        py_gc_collect();
        number = py_list_get(roots[1], 0);
        int overflow = 0;
        if (py_int_to_i64(number, &overflow) != 42 || overflow) return 3;
        py_decref(number);
        /* Switching between nonmoving collectors must not lose ownership. */
        if (atoi(argv[1]) == 0 && iteration == 2 && pcc_gc_set_backend(1) != 0) return 4;
        if (atoi(argv[1]) == 0 && iteration == 4 && pcc_gc_set_backend(0) != 0) return 5;
        unsigned char owned = 1;
        py_list_set_from_owned_root(roots[0], 0, &roots[1], &owned);
        if (owned) pcc_gc_store_root(&roots[1], NULL);
        else roots[1] = NULL;
        py_gc_collect();
    }
    pcc_gc_store_root(&roots[0], NULL);
    pcc_gc_frame_leave(roots);
    py_gc_collect();
    if (atoi(argv[1]) == 0 && py_gc_get_count(0) != baseline) return 6;
    puts("frame-owner-roundtrip-ok");
    return 0;
}
''')
    executable = tmp_path / "frame_roundtrip"
    command = ["clang", "-I" + str(root / "pcc/py_runtime/include"), str(source),
               str(archive), "-pthread", "-o", str(executable)]
    if runtime_kind == "c":
        control = subprocess.run([*command, "-Dpy_list_get_for_frame=py_list_get"],
                                 capture_output=True, text=True, timeout=30)
        assert control.returncode == 0, control.stderr
        ran = subprocess.run([str(executable), "0"], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 2, "the ordinary retaining getter must leave the frame slot occupied"
    built = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "frame-owner-roundtrip-ok"


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
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    int64_t baseline = py_gc_get_count(0);
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
    if (atoi(argv[1]) == 0 && py_gc_get_count(0) != baseline) return 10;
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
