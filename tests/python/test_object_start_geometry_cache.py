"""Cached geometry must never cache object lifetime or accept interior bytes."""

import os
import subprocess


def test_repeated_object_start_checks_observe_retirement_and_reuse(tmp_path, pcc_py_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = tmp_path / "object_geometry.py"
    source.write_text('''from pcc.extern import extern, c_rawptr, c_ptr, c_int64
from pcc.unsafe import free, ptr_add, ptr_diff, load_i64, store_i64, store_i32
allocate = extern("pcc_allocator_alloc_object", (c_int64,), c_rawptr)
publish = extern("pcc_gc_granule_object_publish", (c_ptr,), c_int64)
retire = extern("pcc_gc_granule_object_retire", (c_ptr,), c_int64)
is_object = extern("pcc_gc_granule_is_object_start", (c_ptr,), c_int64)
def main():
    for size in (16, 32, 56, 72, 128, 256, 1024, 16384):
        pointer = allocate(size)
        assert is_object(pointer) == -1
        store_i64(pointer, 0, 1)
        store_i32(pointer, 8, 3)
        store_i32(pointer, 12, 0)
        assert publish(pointer) == 1
        index = 0
        while index < 32:
            assert is_object(pointer) == 1
            assert is_object(ptr_add(pointer, 1)) == -1
            assert is_object(ptr_add(pointer, 8)) == -1
            index += 1
        live = load_i64(pointer, -48)
        store_i64(pointer, -48, 123)
        assert is_object(pointer) == -1
        store_i64(pointer, -48, live)
        assert retire(pointer) == 1
        assert is_object(pointer) == -1
        free(pointer)
        assert is_object(pointer) == -1
        reused = allocate(size)
        assert ptr_diff(reused, pointer) == 0
        assert is_object(reused) == -1
        store_i64(reused, 0, 1)
        store_i32(reused, 8, 3)
        store_i32(reused, 12, 0)
        assert publish(reused) == 1
        assert is_object(reused) == 1
        assert retire(reused) == 1
        free(reused)
    print("OBJECT_GEOMETRY_OK")
main()
''')
    binary = tmp_path / "object_geometry"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, f"GC{backend}: {result.stdout}{result.stderr}"
        assert result.stdout.strip() == "OBJECT_GEOMETRY_OK"
