"""Native ``str.split(sep, maxsplit)`` dispatch."""
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


def test_str_split_maxsplit_uses_native_runtime():
    program = textwrap.dedent(
        """
        def f(text: str) -> str:
            return text.split("=", 1)[1]
        """
    )
    ir = _compile_to_ll(program, "str_split_maxsplit_ir", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_str_split_maxsplit" in body, body
    assert "cpy.fn.split" not in body, body
    assert "cpy.call" not in body, body


def test_str_split_maxsplit_runtime_matches_python(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                parts = "a=b=c".split("=", 1)
                print(parts[0])
                print(parts[1])
                zero = "a=b".split("=", 0)
                print(zero[0])
                ws = "  a  b  c ".split(None, 1)
                print(ws[0])
                print(ws[1])

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
    compile_python(str(src), str(exe))
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "a\nb=c\na=b\na\nb  c \n"
