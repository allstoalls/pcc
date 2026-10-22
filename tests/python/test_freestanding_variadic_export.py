from pathlib import Path
import inspect
import subprocess

from pcc.frontends.python import pipeline


def test_variadic_decorator_scan_uses_bootstrap_safe_explicit_loop():
    from pcc.frontends.python.codegen.user_function_decl_lowering import (
        UserFunctionDeclLoweringMixin,
    )

    source = inspect.getsource(UserFunctionDeclLoweringMixin._declare_user_function)
    assert "c_abi_variadic = any(" not in source
    assert "while c_abi_index < len(c_abi_decorators):" in source


def test_freestanding_c_abi_variadic_export_reads_integer_arguments(tmp_path: Path):
    source = tmp_path / "variadic_export.py"
    llvm_ir = tmp_path / "variadic_export.ll"
    obj = tmp_path / "variadic_export.o"
    harness = tmp_path / "variadic_export_harness.c"
    executable = tmp_path / "variadic_export_harness"
    source.write_text(
        "from pcc.extern import c_abi_variadic_export\n"
        "from pcc.unsafe import va_start, va_arg_i64, va_end\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_variadic_export('pcc_sum_varargs')\n"
        "def pcc_sum_varargs(seed: int) -> int:\n"
        "    cursor = va_start()\n"
        "    first = va_arg_i64(cursor)\n"
        "    second = va_arg_i64(cursor)\n"
        "    va_end(cursor)\n"
        "    return seed + first + second\n",
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
    definition = next(
        line for line in ir_text.splitlines() if "pcc_sum_varargs" in line and line.startswith("define ")
    )
    assert "define i64 @pcc_sum_varargs(i64 " in definition
    assert "..." in definition
    assert "llvm.va_start" in ir_text
    assert "va_arg" in ir_text
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    harness.write_text(
        r"""
long pcc_sum_varargs(long seed, ...);
int main(void) {
    return pcc_sum_varargs(5, 7L, 11L) == 23 ? 0 : 1;
}
""",
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


def test_self_backend_c_abi_variadic_export_reads_integer_arguments(tmp_path: Path):
    from pcc.backend.self_backend_dispatch import emit_self_asm

    source = tmp_path / "variadic_export_self.py"
    llvm_ir = tmp_path / "variadic_export_self.ll"
    asm = tmp_path / "variadic_export_self.s"
    obj = tmp_path / "variadic_export_self.o"
    harness = tmp_path / "variadic_export_self_harness.c"
    executable = tmp_path / "variadic_export_self_harness"
    source.write_text(
        "from pcc.extern import c_abi_variadic_export\n"
        "from pcc.unsafe import va_start, va_arg_i64, va_end\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_variadic_export('pcc_sum_varargs_self')\n"
        "def pcc_sum_varargs_self(seed: int) -> int:\n"
        "    cursor = va_start()\n"
        "    first = va_arg_i64(cursor)\n"
        "    second = va_arg_i64(cursor)\n"
        "    va_end(cursor)\n"
        "    return seed + first + second\n",
        encoding="utf-8",
    )
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    asm.write_text(
        emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", "-c", str(asm), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    harness.write_text(
        r"""
long pcc_sum_varargs_self(long seed, ...);
int main(void) {
    return pcc_sum_varargs_self(5, 7L, 11L) == 23 ? 0 : 1;
}
""",
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


def test_fixed_va_list_cursor_reads_pointer_float_and_integer(tmp_path: Path):
    source = tmp_path / "fixed_va_list.py"
    llvm_ir = tmp_path / "fixed_va_list.ll"
    obj = tmp_path / "fixed_va_list.o"
    harness = tmp_path / "fixed_va_list_harness.c"
    executable = tmp_path / "fixed_va_list_harness"
    source.write_text(
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import load_i8, va_cursor, va_arg_ptr, va_arg_f64, va_arg_i64\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('pcc_read_va_list')\n"
        "def pcc_read_va_list(ap) -> int:\n"
        "    cursor = va_cursor(ap)\n"
        "    label = va_arg_ptr(cursor)\n"
        "    ratio = va_arg_f64(cursor)\n"
        "    number = va_arg_i64(cursor)\n"
        "    if load_i8(label, 0) == 111 and ratio > 3.4 and ratio < 3.6:\n"
        "        return number\n"
        "    return -1\n",
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
    harness.write_text(
        r"""
#include <stdarg.h>
long pcc_read_va_list(va_list ap);
static long call_reader(int marker, ...) {
    long result;
    va_list ap;
    va_start(ap, marker);
    result = pcc_read_va_list(ap);
    va_end(ap);
    return result;
}
int main(void) {
    return call_reader(0, "owned", 3.5, 9L) == 9 ? 0 : 1;
}
""",
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


def test_typed_c_abi_export_preserves_narrow_integer_and_pointer_abi(
    tmp_path: Path,
):
    source = tmp_path / "typed_export.py"
    llvm_ir = tmp_path / "typed_export.ll"
    obj = tmp_path / "typed_export.o"
    harness = tmp_path / "typed_export_harness.c"
    executable = tmp_path / "typed_export_harness"
    source.write_text(
        "from pcc.extern import c_abi_typed_export\n"
        "from pcc.unsafe import ptr_is_null\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_typed_export('pcc_typed_i32', 'i32', ('i32', 'ptr'))\n"
        "def pcc_typed_i32(value: int, marker) -> int:\n"
        "    if ptr_is_null(marker):\n"
        "        return 99\n"
        "    return value + 1\n",
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
    assert "define i32 @pcc_typed_i32(i32" in ir_text
    assert "ptr %marker" in ir_text
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    harness.write_text(
        "#include <stdint.h>\n"
        "int32_t pcc_typed_i32(int32_t value, void *marker);\n"
        "int main(void) {\n"
        "  int marker = 0;\n"
        "  return pcc_typed_i32(-7, &marker) == -6 ? 0 : 1;\n"
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


def test_typed_and_variadic_exports_compose_for_narrow_fixed_arguments(
    tmp_path: Path,
):
    source = tmp_path / "typed_variadic_export.py"
    llvm_ir = tmp_path / "typed_variadic_export.ll"
    obj = tmp_path / "typed_variadic_export.o"
    harness = tmp_path / "typed_variadic_export_harness.c"
    executable = tmp_path / "typed_variadic_export_harness"
    source.write_text(
        "from pcc.extern import c_abi_typed_export, c_abi_variadic_export\n"
        "from pcc.unsafe import va_start, va_arg_i32, va_end\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_typed_export('pcc_typed_varargs', 'i32', ('i32',))\n"
        "@c_abi_variadic_export('pcc_typed_varargs')\n"
        "def pcc_typed_varargs(seed: int) -> int:\n"
        "    cursor = va_start()\n"
        "    value = va_arg_i32(cursor)\n"
        "    va_end(cursor)\n"
        "    return seed + value\n",
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
    definition = next(
        line
        for line in ir_text.splitlines()
        if "pcc_typed_varargs" in line and line.startswith("define ")
    )
    assert "define i32 @pcc_typed_varargs(i32 " in definition
    assert "..." in definition
    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    harness.write_text(
        "#include <stdint.h>\n"
        "int32_t pcc_typed_varargs(int32_t seed, ...);\n"
        "int main(void) {\n"
        "  return pcc_typed_varargs(-7, 11) == 4 ? 0 : 1;\n"
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
