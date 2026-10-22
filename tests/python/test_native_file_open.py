"""Native text-file open/read/write lowering."""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


def test_with_open_read_uses_native_file_runtime():
    program = textwrap.dedent(
        """
        def f(path: str):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        """
    )
    ir = _compile_to_ll(program, "native_file_read_ir", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_file_open" in body, body
    assert "@py_file_read_all" in body, body
    assert "cpy.builtin.open" not in body, body
    assert "cpy.fn.read" not in body, body
    assert "with.enter" not in body, body


def test_with_open_write_uses_native_file_runtime():
    program = textwrap.dedent(
        """
        def f(path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("abc")
        """
    )
    ir = _compile_to_ll(program, "native_file_write_ir", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_file_open" in body, body
    assert "@py_file_write" in body, body
    assert "@py_file_close" in body, body
    assert "cpy.builtin.open" not in body, body
    assert "cpy.fn.write" not in body, body
    assert "with.exit" not in body, body


def test_native_file_runtime_round_trip(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    data = tmp_path / "native-file.txt"
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            f"""
            PATH = {str(data)!r}

            def main() -> None:
                with open(PATH, "w", encoding="utf-8") as fh:
                    fh.write("alpha")
                with open(PATH, "r", encoding="utf-8") as fh:
                    print(fh.read())

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "alpha\n"
