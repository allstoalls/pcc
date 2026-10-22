"""Bulk-created generator frames retain the ordinary traced-list contract."""

from pathlib import Path
import subprocess

from tests.owned_c_execution import compile_owned_c_with_runtime


def test_generator_frame_none_slots_survive_each_collector(tmp_path: Path, pcc_runtime_archive):
    source = tmp_path / "frame_init.c"
    source.write_text('''#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
extern PyObject *py_gen_frame_new(int64_t slots);
int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    int counts[3] = {0, 1, 33};
    for (int n = 0; n < 3; n++) {
        int count = counts[n];
        PyObject *roots[1] = {py_gen_frame_new(count)};
        int32_t map[1] = {1};
        if (!roots[0]) return 2;
        pcc_gc_frame_enter(map, roots);
        if (py_list_len(roots[0]) != count) return 3;
        for (int i = 0; i < count; i++) {
            PyObject *value = py_list_get(roots[0], i);
            if (value != py_None) return 4;
            py_decref(value);
        }
        if (count) {
            PyObject *child = py_str_new("alive", 5);
            py_list_set(roots[0], count - 1, child);
            py_decref(child);
        }
        py_gc_collect();
        if (py_list_len(roots[0]) != count) return 5;
        if (count) {
            PyObject *child = py_list_get(roots[0], count - 1);
            if (!child || py_str_len(child) != 5) return 6;
            py_decref(child);
        }
        pcc_gc_frame_leave(roots);
        py_decref(roots[0]);
    }
    puts("generator-frame-init-ok");
    return 0;
}
''')
    executable = tmp_path / "frame_init"
    compile_owned_c_with_runtime(source, executable, runtime_archive=pcc_runtime_archive)
    for backend in range(5):
        ran = subprocess.run([str(executable), str(backend)], capture_output=True,
                             text=True, timeout=15)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "generator-frame-init-ok"
