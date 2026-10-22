from __future__ import annotations

import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python, count_py_cpy_fallback_calls


def test_os_makedirs_emits_native_no_libpython_call(tmp_path):
    src = tmp_path / "makedirs_ir.py"
    ll = tmp_path / "makedirs_ir.ll"
    src.write_text(
        textwrap.dedent(
            """
            import os

            def make(path: str, mode: int, ok: bool):
                os.makedirs(path, mode=mode, exist_ok=ok)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(ll),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = ll.read_text(encoding="utf-8")
    assert "call ptr @py_os_makedirs" in ir_text
    assert count_py_cpy_fallback_calls(ir_text) == 0


@pytest.mark.parametrize("runtime_kind", ["pcc-py"])
def test_os_makedirs_runtime_and_exist_ok(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
    runtime_kind,
):
    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    target = tmp_path / runtime_kind / "nested" / "leaf"
    src = tmp_path / f"makedirs_{runtime_kind}.py"
    exe = tmp_path / f"makedirs_{runtime_kind}.out"
    src.write_text(
        textwrap.dedent(
            f"""
            import os

            target = {str(target)!r}
            os.makedirs(target, mode=0o700)
            print(os.path.isdir(target))
            os.makedirs(target, exist_ok=True)
            try:
                os.makedirs(target, exist_ok=False)
            except OSError:
                print("exists")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nexists\n"
    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700
