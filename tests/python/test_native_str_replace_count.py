"""Three-argument ``str.replace`` native lowering."""
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


def test_str_replace_count_dispatches_to_native_helper():
    program = textwrap.dedent(
        """
        def f(s: str):
            return s.replace("a", "b", 2)
        """
    )

    ir_text = _compile_to_ll(program, "native_str_replace_count_ir", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_str_replace_count" in body, body
    assert "cpy.fn.replace" not in body, body


def test_native_str_replace_count_runtime_matches_count(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                print("aaaa".replace("a", "b", 2))
                print("aaaa".replace("a", "b", 0))
                print("aaaa".replace("aa", "c", 1))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "bbaa\naaaa\ncaa\n"
