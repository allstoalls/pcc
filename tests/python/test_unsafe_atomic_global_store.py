from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    source = tmp_path / "atomic_global_store.py"
    llvm_ir = tmp_path / "atomic_global_store.ll"
    source.write_text(
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import (\n"
        "    atomic_load_i32, atomic_store_i32, define_global_i32, global_addr,\n"
        ")\n"
        "__pcc_freestanding__ = True\n"
        "define_global_i32(\"atomic_global_store_slot\", 1)\n"
        "@c_abi_export(\"atomic_global_store_clear\")\n"
        "def atomic_global_store_clear() -> int:\n"
        "    atomic_store_i32(\n"
        "        global_addr(\"atomic_global_store_slot\"), 0, 0, \"release\"\n"
        "    )\n"
        "    return atomic_load_i32(\n"
        "        global_addr(\"atomic_global_store_slot\"), 0, \"acquire\"\n"
        "    )\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    object_source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        object_source = tmp_path / "atomic_global_store.s"
        object_source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("atomic_global_store_" + emitter + ".o")
    build = subprocess.run(
        ["clang", "-c", str(object_source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_atomic_store_i32_constant_to_global_uses_the_constant(
    tmp_path: Path, emitter: str
):
    obj = _compile_object(tmp_path, emitter)
    harness = tmp_path / "atomic_global_store_harness.c"
    executable = tmp_path / ("atomic_global_store_harness_" + emitter)
    harness.write_text(
        "long atomic_global_store_clear(void);\n"
        "int main(void) { return atomic_global_store_clear() == 0 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_thread_local_global_intrinsics_emit_tls_and_isolate_threads(
    tmp_path: Path,
) -> None:
    source = tmp_path / "thread_local_globals.py"
    llvm_ir = tmp_path / "thread_local_globals.ll"
    obj = tmp_path / "thread_local_globals.o"
    source.write_text(
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import (\n"
        "    define_thread_local_i32, define_thread_local_ptr_null,\n"
        "    global_addr, global_load_ptr, load_i32, ptr_is_null, store_i32,\n"
        ")\n"
        "__pcc_freestanding__ = True\n"
        "define_thread_local_i32(\"unsafe_tls_i32\", 7)\n"
        "define_thread_local_ptr_null(\"unsafe_tls_ptr\")\n"
        "@c_abi_export(\"unsafe_tls_get\")\n"
        "def unsafe_tls_get() -> int:\n"
        "    return load_i32(global_addr(\"unsafe_tls_i32\"), 0)\n"
        "@c_abi_export(\"unsafe_tls_set\")\n"
        "def unsafe_tls_set(value: int) -> None:\n"
        "    store_i32(global_addr(\"unsafe_tls_i32\"), 0, value)\n"
        "@c_abi_export(\"unsafe_tls_ptr_is_null\")\n"
        "def unsafe_tls_ptr_is_null() -> int:\n"
        "    return ptr_is_null(global_load_ptr(\"unsafe_tls_ptr\"))\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    assert 'thread_local global i32 7' in ir_text
    assert 'thread_local global ptr null' in ir_text

    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    harness = tmp_path / "thread_local_globals_harness.c"
    executable = tmp_path / "thread_local_globals_harness"
    harness.write_text(
        r'''
#include <pthread.h>
#include <stdint.h>

int64_t unsafe_tls_get(void);
void unsafe_tls_set(int64_t value);
int64_t unsafe_tls_ptr_is_null(void);

static void *worker(void *raw) {
    intptr_t value = (intptr_t)raw;
    if (unsafe_tls_get() != 7) return (void *)(intptr_t)101;
    if (!unsafe_tls_ptr_is_null()) return (void *)(intptr_t)102;
    unsafe_tls_set(value);
    return (void *)(intptr_t)unsafe_tls_get();
}

int main(void) {
    pthread_t first;
    pthread_t second;
    void *first_result = 0;
    void *second_result = 0;
    if (unsafe_tls_get() != 7 || !unsafe_tls_ptr_is_null()) return 1;
    unsafe_tls_set(11);
    if (pthread_create(&first, 0, worker, (void *)(intptr_t)23) != 0) return 2;
    if (pthread_create(&second, 0, worker, (void *)(intptr_t)29) != 0) return 3;
    if (pthread_join(first, &first_result) != 0) return 4;
    if (pthread_join(second, &second_result) != 0) return 5;
    if ((intptr_t)first_result != 23 || (intptr_t)second_result != 29) return 6;
    if (unsafe_tls_get() != 11 || !unsafe_tls_ptr_is_null()) return 7;
    return 0;
}
''',
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-pthread", "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_unsigned_greater_i64_compares_raw_u64_bit_patterns(tmp_path: Path) -> None:
    source = tmp_path / "unsigned_greater_i64.py"
    llvm_ir = tmp_path / "unsigned_greater_i64.ll"
    obj = tmp_path / "unsigned_greater_i64.o"
    source.write_text(
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import unsigned_greater_i64\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"unsigned_greater_i64_probe\")\n"
        "def probe(lhs: int, rhs: int) -> int:\n"
        "    return unsigned_greater_i64(lhs, rhs)\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    harness = tmp_path / "unsigned_greater_i64_harness.c"
    executable = tmp_path / "unsigned_greater_i64_harness"
    harness.write_text(
        "#include <stdint.h>\n"
        "int64_t unsigned_greater_i64_probe(int64_t, int64_t);\n"
        "int main(void) {\n"
        "  if (!unsigned_greater_i64_probe(-1, 0)) return 1;\n"
        "  if (unsigned_greater_i64_probe(0, -1)) return 2;\n"
        "  if (!unsigned_greater_i64_probe(-1, -2)) return 3;\n"
        "  if (unsigned_greater_i64_probe(7, 7)) return 4;\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr
