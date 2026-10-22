"""Native value-position zip() lowering."""
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


def test_value_position_zip_uses_native_list_of_tuples():
    program = textwrap.dedent(
        """
        def f():
            return list(zip([1, 2], [3, 4]))
        """
    )
    ir = _compile_to_ll(program, "native_builtin_zip_ir", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_tuple_new" in body, body
    assert "@py_obj_getitem" in body, body
    assert "cpy.builtin.zip" not in body, body
    assert "cpy.call2.zip" not in body, body


def test_native_zip_runtime_round_trip(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                pairs = list(zip([1, 2], [3, 4]))
                print(len(pairs))
                print(pairs[0][0])
                print(pairs[1][1])

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
    assert run.stdout == "2\n1\n4\n"
