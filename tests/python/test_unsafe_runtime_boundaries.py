from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


def _compile_probe(tmp_path: Path) -> str:
    source = tmp_path / "unsafe_runtime_boundaries.py"
    llvm_ir = tmp_path / "unsafe_runtime_boundaries.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export, c_ptr\n"
        "from pcc.unsafe import (\n"
        "    call_i32_ptr1, call_i64_i64_ptr, call_i64_ptr1,\n"
        "    call_i64_ptr_i64_i64_ptr, call_ptr0, call_ptr3, call_ptr4, call_void_ptr0,\n"
        "    call_void_ptr_i64_ptr,\n"
        "    dynamic_library_close,\n"
        "    dynamic_library_open, dynamic_library_symbol, gc_backend_current,\n"
        "    int_to_ptr, ptr_is_null, ptr_to_int, thread_safepoint,\n"
        ")\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"unsafe_callback_pair\")\n"
        "def unsafe_callback_pair(fn: c_ptr, context: c_ptr, handle: i64) -> i64:\n"
        "    return call_i64_i64_ptr(fn, handle, context)\n"
        "@c_abi_export(\"unsafe_i32_callback\")\n"
        "def unsafe_i32_callback(fn: c_ptr, context: c_ptr) -> i64:\n"
        "    return call_i32_ptr1(fn, context)\n"
        "@c_abi_export(\"unsafe_size_callback\")\n"
        "def unsafe_size_callback(fn: c_ptr, data: c_ptr, size: i64, count: i64, context: c_ptr) -> i64:\n"
        "    return call_i64_ptr_i64_i64_ptr(fn, data, size, count, context)\n"
        "@c_abi_export(\"unsafe_pointer_callback\")\n"
        "def unsafe_pointer_callback(fn: c_ptr, raw: i64) -> i64:\n"
        "    return call_i64_ptr1(fn, int_to_ptr(raw))\n"
        "@c_abi_export(\"unsafe_pointer_bits\")\n"
        "def unsafe_pointer_bits(value: c_ptr) -> i64:\n"
        "    return ptr_to_int(value)\n"
        "@c_abi_export(\"unsafe_callback_quad\")\n"
        "def unsafe_callback_quad(fn: c_ptr, a: c_ptr, b: c_ptr, c: c_ptr, d: c_ptr):\n"
        "    return call_ptr4(fn, a, b, c, d)\n"
        "@c_abi_export(\"unsafe_callback_triple\")\n"
        "def unsafe_callback_triple(fn: c_ptr, a: c_ptr, b: c_ptr, c: c_ptr):\n"
        "    return call_ptr3(fn, a, b, c)\n"
        "@c_abi_export(\"unsafe_callback_zero\")\n"
        "def unsafe_callback_zero(fn: c_ptr) -> None:\n"
        "    call_void_ptr0(fn)\n"
        "@c_abi_export(\"unsafe_pointer_callback_zero\")\n"
        "def unsafe_pointer_callback_zero(fn: c_ptr):\n"
        "    return call_ptr0(fn)\n"
        "@c_abi_export(\"unsafe_slot_callback\")\n"
        "def unsafe_slot_callback(fn: c_ptr, slot: c_ptr, role: i64, context: c_ptr) -> None:\n"
        "    call_void_ptr_i64_ptr(fn, slot, role, context)\n"
        "@c_abi_export(\"unsafe_runtime_boundary\")\n"
        "def unsafe_runtime_boundary() -> i64:\n"
        "    thread_safepoint()\n"
        "    return gc_backend_current()\n"
        "@c_abi_export(\"unsafe_dynamic_call\")\n"
        "def unsafe_dynamic_call(path: c_ptr, name: c_ptr, raw: i64) -> i64:\n"
        "    handle = dynamic_library_open(path)\n"
        "    if ptr_is_null(handle) != 0:\n"
        "        return -101\n"
        "    fn = dynamic_library_symbol(handle, name)\n"
        "    if ptr_is_null(fn) != 0:\n"
        "        dynamic_library_close(handle)\n"
        "        return -102\n"
        "    result: i64 = call_i64_ptr1(fn, int_to_ptr(raw))\n"
        "    if dynamic_library_close(handle) != 0:\n"
        "        return -103\n"
        "    return result\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return llvm_ir.read_text(encoding="utf-8")


def _compile_platform_guarded_loader_probe(tmp_path: Path) -> str:
    source = tmp_path / "unsafe_platform_guarded_loader.py"
    llvm_ir = tmp_path / "unsafe_platform_guarded_loader.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export, c_ptr\n"
        "from pcc.unsafe import (\n"
        "    dynamic_library_close, dynamic_library_open,\n"
        "    dynamic_library_symbol, ptr_is_null,\n"
        ")\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export(\"guarded_loader_is_unavailable\")\n"
        "def guarded_loader_is_unavailable(path: c_ptr, name: c_ptr) -> i64:\n"
        "    handle = dynamic_library_open(path, \"darwin\")\n"
        "    if ptr_is_null(handle) == 0:\n"
        "        return 0\n"
        "    symbol = dynamic_library_symbol(handle, name, \"darwin\")\n"
        "    if ptr_is_null(symbol) == 0:\n"
        "        return 0\n"
        "    if dynamic_library_close(handle, \"darwin\") != 0:\n"
        "        return 0\n"
        "    return 1\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return llvm_ir.read_text(encoding="utf-8")


def test_dynamic_loader_platform_guard_links_without_non_target_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    llvm_ir = _compile_platform_guarded_loader_probe(tmp_path)

    assert "@dlopen" not in llvm_ir
    assert "@dlsym" not in llvm_ir
    assert "@dlclose" not in llvm_ir

    llvm_path = tmp_path / "unsafe_platform_guarded_loader.ll"
    obj = tmp_path / "unsafe_platform_guarded_loader.o"
    build = subprocess.run(
        ["clang", "-c", str(llvm_path), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert undefined.stdout.strip() == ""

    harness = tmp_path / "unsafe_platform_guarded_loader_harness.c"
    executable = tmp_path / "unsafe_platform_guarded_loader_harness"
    harness.write_text(
        "#include <stdint.h>\n"
        "int64_t guarded_loader_is_unavailable(void *, void *);\n"
        "int main(void) {\n"
        "  return guarded_loader_is_unavailable(0, 0) == 1 ? 0 : 1;\n"
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
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def _build_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = _compile_probe(tmp_path)
    source = tmp_path / "unsafe_runtime_boundaries.ll"
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "unsafe_runtime_boundaries.s"
        source.write_text(emit_self_asm(llvm_ir), encoding="utf-8")
    obj = tmp_path / ("unsafe_runtime_boundaries_" + emitter + ".o")
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_raw_callback_dynamic_loader_and_runtime_boundaries_roundtrip(
    tmp_path: Path, emitter: str
):
    supported = (
        sys.platform == "darwin" and platform.machine() == "arm64"
    ) or (sys.platform.startswith("linux") and platform.machine() == "x86_64")
    assert supported

    obj = _build_object(tmp_path, emitter)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    undefined_names = {
        line.split()[-1].lstrip("_")
        for line in undefined.stdout.splitlines()
        if line.strip()
    }
    assert undefined_names == {
        "dlclose",
        "dlopen",
        "dlsym",
        "pcc_gc_backend",
        "pcc_thread_safepoint",
    }

    driver = tmp_path / "boundary_driver.c"
    driver_lib = tmp_path / (
        "libboundary_driver.dylib"
        if sys.platform == "darwin"
        else "libboundary_driver.so"
    )
    driver.write_text(
        "#include <stdint.h>\n"
        "int64_t boundary_driver(void *value) {\n"
        "  return (uintptr_t)value == 0xCAFEu ? -73 : -74;\n"
        "}\n",
        encoding="utf-8",
    )
    driver_build = subprocess.run(
        [
            "clang",
            *( ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"] ),
            str(driver),
            "-o",
            str(driver_lib),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert driver_build.returncode == 0, driver_build.stdout + driver_build.stderr

    harness = tmp_path / "unsafe_boundary_harness.c"
    executable = tmp_path / ("unsafe_boundary_harness_" + emitter)
    harness.write_text(
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        "int64_t unsafe_callback_pair(void *, void *, uint64_t);\n"
        "int64_t unsafe_i32_callback(void *, void *);\n"
        "int64_t unsafe_size_callback(void *, void *, uint64_t, uint64_t, void *);\n"
        "int64_t unsafe_pointer_callback(void *, uint64_t);\n"
        "int64_t unsafe_pointer_bits(void *);\n"
        "void *unsafe_callback_quad(void *, void *, void *, void *, void *);\n"
        "void *unsafe_callback_triple(void *, void *, void *, void *);\n"
        "void unsafe_callback_zero(void *);\n"
        "void *unsafe_pointer_callback_zero(void *);\n"
        "void unsafe_slot_callback(void *, void *, int64_t, void *);\n"
        "int64_t unsafe_runtime_boundary(void);\n"
        "int64_t unsafe_dynamic_call(const char *, const char *, uint64_t);\n"
        "static int64_t safepoints = 0;\n"
        "void pcc_thread_safepoint(void) { safepoints++; }\n"
        "int64_t pcc_gc_backend(void) { return 4; }\n"
        "static int64_t pair_cb(uint64_t h, void *p) {\n"
        "  return (int64_t)h + *(int64_t *)p;\n"
        "}\n"
        "static int32_t i32_cb(void *p) {\n"
        "  return *(int64_t *)p == 7 ? -17 : 18;\n"
        "}\n"
        "static uint64_t size_cb(void *data, uint64_t size, uint64_t count, void *p) {\n"
        "  return data == (void *)0x1234u && size == 2 && *(int64_t *)p == 7 ? count : 0;\n"
        "}\n"
        "static int64_t ptr_cb(void *p) {\n"
        "  return (uintptr_t)p == 0xBEEFu ? 91 : -1;\n"
        "}\n"
        "static void *quad_cb(void *a, void *b, void *c, void *d) {\n"
        "  uintptr_t total = (uintptr_t)a + (uintptr_t)b + (uintptr_t)c + (uintptr_t)d;\n"
        "  return (void *)total;\n"
        "}\n"
        "static void *triple_cb(void *a, void *b, void *c) {\n"
        "  return (void *)((uintptr_t)a + (uintptr_t)b + (uintptr_t)c);\n"
        "}\n"
        "static void *slot_seen;\n"
        "static int64_t zero_hits;\n"
        "static void zero_cb(void) { zero_hits++; }\n"
        "static void *pointer_zero_cb(void) { return (void *)0xD00Du; }\n"
        "static int64_t role_seen;\n"
        "static void *context_seen;\n"
        "static void slot_cb(void *slot, int64_t role, void *context) {\n"
        "  slot_seen = slot; role_seen = role; context_seen = context;\n"
        "}\n"
        "int main(int argc, char **argv) {\n"
        "  int64_t context = 7;\n"
        "  if (argc != 2) return 1;\n"
        "  if (unsafe_callback_pair(pair_cb, &context, 35) != 42) return 2;\n"
        "  if (unsafe_i32_callback(i32_cb, &context) != -17) return 12;\n"
        "  if (unsafe_size_callback(size_cb, (void *)0x1234u, 2, 9, &context) != 9) return 13;\n"
        "  if (unsafe_pointer_callback(ptr_cb, 0xBEEFu) != 91) return 3;\n"
        "  if (unsafe_pointer_bits((void *)0x12340u) != 0x12340) return 11;\n"
        "  if (unsafe_callback_quad(quad_cb, (void *)1, (void *)2, (void *)3, (void *)4) != (void *)10) return 7;\n"
        "  if (unsafe_callback_triple(triple_cb, (void *)4, (void *)5, (void *)6) != (void *)15) return 9;\n"
        "  unsafe_callback_zero(zero_cb);\n"
        "  if (zero_hits != 1) return 8;\n"
        "  if (unsafe_pointer_callback_zero(pointer_zero_cb) != (void *)0xD00Du) return 10;\n"
        "  unsafe_slot_callback(slot_cb, (void *)0xABCDu, 3, &context);\n"
        "  if (slot_seen != (void *)0xABCDu || role_seen != 3 || context_seen != &context) return 6;\n"
        "  if (unsafe_runtime_boundary() != 4 || safepoints != 1) return 4;\n"
        "  if (unsafe_dynamic_call(argv[1], \"boundary_driver\", 0xCAFEu) != -73) return 5;\n"
        "  puts(\"unsafe-boundaries-ok\");\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    link = subprocess.run(
        [
            "clang",
            str(harness),
            str(obj),
            *( ["-ldl"] if sys.platform.startswith("linux") else [] ),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable), str(driver_lib)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "unsafe-boundaries-ok\n"


def test_runtime_boundary_intrinsic_does_not_whitelist_unregistered_gc_extern(
    tmp_path: Path,
):
    source = tmp_path / "unsafe_boundary_escape.py"
    llvm_ir = tmp_path / "unsafe_boundary_escape.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export, c_int64, extern\n"
        "from pcc.unsafe import thread_safepoint\n"
        "__pcc_freestanding__ = True\n"
        "pcc_gc_unverified_escape = extern(\"pcc_gc_unverified_escape\", (), c_int64)\n"
        "@c_abi_export(\"escape\")\n"
        "def escape() -> i64:\n"
        "    thread_safepoint()\n"
        "    return pcc_gc_unverified_escape()\n",
        encoding="utf-8",
    )
    with pytest.raises(
        pipeline.PyPipelineError,
        match="managed-runtime reference|outside its verified closure",
    ):
        pipeline.compile_python(
            str(source),
            str(llvm_ir),
            emit_llvm_only=True,
            libpython_mode="off",
            python_library=True,
        )


def test_open_readonly_and_open_file_share_the_vararg_open_declaration(tmp_path: Path):
    source = tmp_path / "open_pair.py"
    llvm_ir = tmp_path / "open_pair.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export, c_ptr\n"
        "from pcc.unsafe import open_file, open_readonly\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('open_pair')\n"
        "def open_pair(path: c_ptr) -> i64:\n"
        "    first: i64 = open_readonly(path)\n"
        "    second: i64 = open_file(path, 1, 1)\n"
        "    return first + second\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple="arm64-apple-macosx11.0",
    )
    text = llvm_ir.read_text(encoding="utf-8")
    declarations = [line for line in text.splitlines() if line.startswith("declare ")]
    assert sum("@open(ptr, i32, ...)" in line for line in declarations) == 1


@pytest.mark.parametrize('target', ['x86_64-unknown-linux-gnu','aarch64-unknown-linux-gnu','arm64-apple-macosx11.0','x86_64-pc-windows-msvc'])
def test_open_file_flags_owned_target_lowering(tmp_path, target):
    import re
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source=tmp_path/'open_flags.py';out=tmp_path/'open_flags.ll'
    source.write_text('''from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import open_file_flags
__pcc_freestanding__ = True
@c_abi_export('probe_open_flags')
def probe(path: c_ptr, flags: i64, permissions: i64, directory: i64) -> i64:
    return open_file_flags(path, flags, permissions, directory)
''')
    compile_python(str(source),str(out),emit_llvm_only=True,python_library=True,libpython_mode='off',target_triple=target)
    text=out.read_text();verify_ir_text(text)
    assert not re.search(r'\bcall[^\n]*@py_cpy_', text)
    if 'linux' in target:
        assert '@openat(' not in text
        assert ('syscall' in text) if target.startswith('x86') else ('svc' in text)
    elif 'apple' in target:
        assert '@openat(i32, ptr, i32, ...)' in text
    else:
        assert '@openat(' not in text and 'ret i64 -38' in text
