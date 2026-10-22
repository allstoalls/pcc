"""Focused object-emission regressions for the platform runtime blockers."""

from pathlib import Path
import re

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.ir.optimization.driver import optimize_ir
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline_targets import host_target_triple


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module, target", [
    ("py_capi_stdio_runtime", "x86_64-unknown-linux-gnu"),
    ("freestanding_windows", "x86_64-pc-windows-msvc"),
    ("freestanding_windows_system", "x86_64-pc-windows-msvc"),
    ("freestanding_windows_time", "x86_64-pc-windows-msvc"),
    ("freestanding_windows_socket", "x86_64-pc-windows-msvc"),
    ("freestanding_windows_process", "x86_64-pc-windows-msvc"),
])
def test_platform_runtime_blocker_emits_owned_object(tmp_path, module, target):
    runtime = ROOT / "pcc" / "runtime"
    path = tmp_path / "module.ll"
    _compile_runtime_module(module, str(runtime / "py" / (module + ".py")), str(path), target)
    ir_text = optimize_ir(path.read_text(), runtime_ir_passes(str(runtime)))
    data = emit_owned_object(ir_text, target)
    assert data.startswith(b"\x7fELF" if "linux" in target else b"\x64\x86")


@pytest.mark.parametrize("operator, constant", [("<", "0"), ("==", "-1"), ("!=", "~0"), (">=", "+0")])
def test_freestanding_raw_extern_integer_comparison_has_no_boxing(tmp_path, operator, constant):
    source = tmp_path / "compare.py"
    output = tmp_path / "compare.ll"
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import extern, c_int, c_ptr, c_abi_export\n"
        "from pcc.unsafe import null\n"
        "__pcc_freestanding__ = True\n"
        "status = extern('CloseHandle', (c_ptr,), c_int)\n"
        "@c_abi_export('probe')\n"
        "def probe() -> i64:\n"
        f"    if status(null()) {operator} {constant}:\n"
        "        return 1\n"
        "    return 0\n"
    )
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", target_triple=host_target_triple())
    text = output.read_text()
    assert "icmp" in text
    assert not re.search(r"\bcall\b[^@\n]*@(py_int_from_i64|py_obj_)", text)


def test_raw_pointer_ternary_and_extern_comparison_execute(tmp_path):
    import subprocess
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    from pcc.driver.project import TranslationUnit

    source = tmp_path / "pointer.py"
    output = tmp_path / "pointer.ll"
    source.write_text(
        "from pcc.extern import c_abi_export, c_ptr, c_int, extern\n"
        "from pcc.unsafe import cstr, load_i8, null\n"
        "__pcc_freestanding__ = True\n"
        "status = extern('CloseHandle', (c_ptr,), c_int)\n"
        "@c_abi_export('probe_pointer')\n"
        "def probe_pointer(path: c_ptr) -> c_ptr:\n"
        "    return cstr('.') if load_i8(path, 0) == 0 else path\n"
        "@c_abi_export('probe_negative')\n"
        "def probe_negative() -> i64:\n"
        "    if status(null()) < 0:\n"
        "        return -1\n"
        "    return 0\n"
        "@c_abi_export('probe_join')\n"
        "def probe_join(flag: i64) -> i64:\n"
        "    return status(null()) if flag else 11\n"
    )
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", target_triple=host_target_triple())
    text = output.read_text()
    assert not re.search(r"\bcall\b[^@\n]*@(pcc_gc_retain|py_int_from_i64)", text)
    evaluator = CEvaluator()
    units = evaluator.compile_translation_units([TranslationUnit(
        name="main.c", path="", source='char *probe_pointer(char *p); long long probe_negative(void); long long probe_join(long long flag); int CloseHandle(void *handle) {return -1234;} int main(void) {return probe_pointer("")[0] == 46 && probe_pointer("x")[0] == 120 && probe_negative() == -1 && probe_join(0) == 11 && probe_join(1) == -1234 ? 0 : 1;}'
    )], use_compile_cache=False)
    executable = tmp_path / "native"
    evaluator.emit_executable(units + [("pointer.py", text, None, ())], str(executable))
    assert subprocess.run([str(executable)], timeout=10).returncode == 0
