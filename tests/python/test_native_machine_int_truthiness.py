"""Truthiness of a raw machine integer (an extern ``c_int`` result).

The frontend types an extern call result as ``DynType`` while the lane is a
plain ``i32``/``i64``.  Asking the managed runtime for its truthiness boxed
that raw value, which a freestanding module must not do, and which cost a
box plus a runtime call on every machine-int condition.  A raw integer
cannot be an object pointer, so it is truthy exactly when it is nonzero.

The IR shape is checked on the freestanding route (where the boxed form
failed closed) and the same predicate is executed natively.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from pcc.frontends.python import pipeline


_FREESTANDING = (
    "from pcc.extern import extern, c_int, c_ptr, c_abi_export\n"
    "__pcc_freestanding__ = True\n"
    "CloseHandle = extern(\"CloseHandle\", (c_ptr,), c_int)\n"
    "\n"
    "@c_abi_export(\"truthy_probe\")\n"
    "def truthy_probe(handle: c_ptr) -> i64:\n"
    "    if CloseHandle(handle):\n"
    "        return 1\n"
    "    return 0\n"
)


def _compile_freestanding(tmp_path: Path, source: str) -> str:
    src = tmp_path / "kernel.py"
    out = tmp_path / "kernel.ll"
    src.write_text(source, encoding="utf-8")
    pipeline.compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, name: str) -> str:
    start = ir_text.index("define external i64 @" + name)
    return ir_text[start : ir_text.index("\n}", start)]


def test_freestanding_extern_int_condition_compares_the_raw_lane(tmp_path):
    body = _function_body(
        _compile_freestanding(tmp_path, _FREESTANDING), "truthy_probe"
    )

    assert "icmp ne i32 %extern.CloseHandle.ret" in body
    assert "truthy_raw" in body
    # HEAD boxed the i32 (``py_int_from_i64``) and asked ``py_obj_truthy``,
    # which a freestanding module rejects as a managed-runtime reference.
    assert "py_int_from_i64" not in body
    assert "py_obj_truthy" not in body


_NATIVE = '''from pcc.extern import extern, c_int

abs_c = extern("abs", (c_int,), c_int)


def main():
    for value in (-3, 0, 5):
        if abs_c(value):
            print("T")
        else:
            print("F")
    total = 0
    for value in (-1, 0, 2):
        while abs_c(value):
            total += 1
            value = 0
    print(total)
    print(0 if not abs_c(0) else 1, 1 if abs_c(-4) else 0)


main()
'''

_CPYTHON_EQUIVALENT = _NATIVE.replace(
    "from pcc.extern import extern, c_int\n\nabs_c = extern(\"abs\", (c_int,), c_int)",
    "abs_c = abs",
)


def _cpython():
    ran = subprocess.run(
        [sys.executable, "-c", _CPYTHON_EQUIVALENT],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def _pcc(tmp_path, compiler, archive, backends):
    source = tmp_path / "truthy.py"
    source.write_text(_NATIVE, encoding="utf-8")
    binary = tmp_path / "truthy"
    compiler(
        str(source),
        str(binary),
        backend="self",
        libpython_mode="off",
        runtime_archive=str(archive),
    )
    outputs = []
    for backend in backends:
        ran = subprocess.run(
            [str(binary)],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stderr}"
        outputs.append(ran.stdout)
    return outputs


def test_extern_int_conditions_execute_as_nonzero_tests(
    tmp_path, pcc_runtime_archive, python_program_compiler
):
    expected = _cpython()
    assert expected == "T\nF\nT\n2\n0 1\n"
    for output in _pcc(
        tmp_path, python_program_compiler, pcc_runtime_archive, range(5)
    ):
        assert output == expected
