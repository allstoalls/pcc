from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1
from pcc.backend.self_backend import emit_aarch64_darwin_asm
from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
from pcc.frontends.python import pipeline


REPO = Path(__file__).absolute().parents[2]
_PAIR_IR = "%Pair = type { double, double }"


def test_typed_c_abi_export_supports_structural_f64_pair_argument_and_return(
    tmp_path: Path,
):
    source = tmp_path / "typed_pair_export.py"
    llvm_ir = tmp_path / "typed_pair_export.ll"
    obj = tmp_path / "typed_pair_export.o"
    harness = tmp_path / "typed_pair_export_harness.c"
    executable = tmp_path / "typed_pair_export_harness"
    source.write_text(
        "from pcc.extern import c_abi_typed_export\n"
        "from pcc.unsafe import f64_pair_first, f64_pair_make, f64_pair_second\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_typed_export('pcc_shift_f64_pair', '{f64,f64}', ('{f64,f64}',))\n"
        "def pcc_shift_f64_pair(value: complex) -> complex:\n"
        "    return f64_pair_make(\n"
        "        f64_pair_first(value) + 1.25,\n"
        "        f64_pair_second(value) - 2.5,\n"
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
    ir_text = llvm_ir.read_text(encoding="utf-8")
    definition = next(
        line
        for line in ir_text.splitlines()
        if "pcc_shift_f64_pair" in line and line.startswith("define ")
    )
    assert "define { double, double } @pcc_shift_f64_pair({ double, double }" in definition

    build = subprocess.run(
        ["clang", "-c", str(llvm_ir), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    harness.write_text(
        "typedef struct { double first; double second; } F64Pair;\n"
        "F64Pair pcc_shift_f64_pair(F64Pair value);\n"
        "int main(void) {\n"
        "  F64Pair value = {3.0, 4.0};\n"
        "  F64Pair result = pcc_shift_f64_pair(value);\n"
        "  return result.first == 4.25 && result.second == 1.5 ? 0 : 1;\n"
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


def test_current_pcc1_compiles_structural_f64_pair_export(tmp_path: Path):
    """Keep typed aggregate declaration construction valid in the self host."""
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for typed aggregate declaration regression"
        )
    source = tmp_path / "typed_pair_export.py"
    llvm_ir = tmp_path / "typed_pair_export.ll"
    source.write_text(
        "from pcc.extern import c_abi_typed_export\n"
        "from pcc.unsafe import f64_pair_first, f64_pair_second\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_typed_export('pcc_pair_sum', 'f64', ('{f64,f64}',))\n"
        "def pcc_pair_sum(value: complex) -> float:\n"
        "    return f64_pair_first(value) + f64_pair_second(value)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "--python-library",
            "--python-libpython=off",
            "--emit-llvm=" + str(llvm_ir),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "double @pcc_pair_sum({ double, double }" in llvm_ir.read_text(
        encoding="utf-8"
    )


def test_aarch64_darwin_self_backend_uses_hfa_registers_for_f64_pair_boundary():
    ir_text = f'''\
target triple = "arm64-apple-darwin23.6.0"

{_PAIR_IR}

define %Pair @pcc_pair_identity(%Pair %value) {{
entry:
  ret %Pair %value
}}

define %Pair @pcc_pair_forward(%Pair %value) {{
entry:
  %result = call %Pair @pcc_pair_identity(%Pair %value)
  ret %Pair %result
}}
'''.strip()

    asm_text = emit_aarch64_darwin_asm(ir_text)

    assert "str d0" in asm_text
    assert "str d1" in asm_text
    assert "ldr d0" in asm_text
    assert "ldr d1" in asm_text
    assert "bl _pcc_pair_identity" in asm_text


def test_x86_64_sysv_self_backend_uses_sse_classes_for_f64_pair_boundary():
    ir_text = f'''\
target triple = "x86_64-unknown-linux-gnu"

{_PAIR_IR}

define %Pair @pcc_pair_identity(%Pair %value) {{
entry:
  ret %Pair %value
}}

define %Pair @pcc_pair_forward(%Pair %value) {{
entry:
  %result = call %Pair @pcc_pair_identity(%Pair %value)
  ret %Pair %result
}}
'''.strip()

    asm_text = emit_x86_64_linux_asm(ir_text)

    # The owned emitter transfers exact FP bits via a scratch GPR. Check both
    # lanes and their offsets, not the obsolete direct-movsd spill spelling.
    incoming = (
        "  movq r11, xmm0\n"
        "  mov QWORD PTR [r10], r11\n"
        "  movq r11, xmm1\n"
        "  mov QWORD PTR [r10 + 8], r11"
    )
    outgoing = (
        "  mov rax, QWORD PTR [r10]\n"
        "  movq xmm0, rax\n"
        "  mov rax, QWORD PTR [r10 + 8]\n"
        "  movq xmm1, rax"
    )
    # Two function entries plus the forwarding call's return, and two returns
    # plus the forwarding call's arguments must each transfer both SSE lanes.
    assert asm_text.count(incoming) == 3
    assert asm_text.count(outgoing) == 3
    assert "call pcc_pair_identity" in asm_text
    from pcc.backend.elf_x86_64 import parse_relocatable
    from pcc.backend.owned_object_emit import emit_owned_object

    obj = parse_relocatable(emit_owned_object(ir_text, "x86_64-unknown-linux-gnu"))
    defined = {symbol.name for symbol in obj.symbols if symbol.section_index != 0}
    assert {"pcc_pair_identity", "pcc_pair_forward"} <= defined


def test_pcc_python_complex_aggregate_exports_match_c_behavior(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    harness = tmp_path / "complex_aggregate_harness.c"
    executable = tmp_path / "complex_aggregate_harness"
    harness.write_text(
        r'''
typedef void PyObject;
typedef struct { double real; double imag; } Py_complex;

PyObject *PyComplex_FromDoubles(double real, double imag);
PyObject *PyComplex_FromCComplex(Py_complex value);
Py_complex PyComplex_AsCComplex(PyObject *obj);
double PyComplex_RealAsDouble(PyObject *obj);
double PyComplex_ImagAsDouble(PyObject *obj);
PyObject *PyFloat_FromDouble(double value);
void Py_DECREF(PyObject *obj);

int main(void) {
    PyObject *z = PyComplex_FromDoubles(3.0, 4.0);
    if (z == 0) return 1;
    Py_complex raw = PyComplex_AsCComplex(z);
    if (raw.real != 3.0 || raw.imag != 4.0) return 2;

    Py_complex made = {5.0, 6.0};
    PyObject *from_c = PyComplex_FromCComplex(made);
    if (from_c == 0) return 3;
    if (PyComplex_RealAsDouble(from_c) != 5.0) return 4;
    if (PyComplex_ImagAsDouble(from_c) != 6.0) return 5;

    PyObject *flt = PyFloat_FromDouble(2.5);
    if (flt == 0) return 6;
    raw = PyComplex_AsCComplex(flt);
    if (raw.real != 2.5 || raw.imag != 0.0) return 7;

    Py_DECREF(flt);
    Py_DECREF(from_c);
    Py_DECREF(z);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            str(harness),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr
