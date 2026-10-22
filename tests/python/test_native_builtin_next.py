"""Native next(<genexpr>, default) lowering."""
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


def test_next_genexpr_default_uses_native_iterator_protocol():
    program = textwrap.dedent(
        """
        def f():
            return next((x for x in [5, 6]), 0)
        """
    )
    ir = _compile_to_ll(program, "native_builtin_next_ir", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_obj_iter" in body, body
    assert "@py_obj_next" in body, body
    assert "@py_obj_getitem" not in body, body
    assert "cpy.builtin.next" not in body, body
    assert "cpy.call2.next" not in body, body


def test_native_next_genexpr_runtime_round_trip(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                print(next((x for x in [5, 6]), 0))
                print(next((x for x in []), 9))

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "5\n9\n"
