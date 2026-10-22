"""Python chained comparisons in the bootstrap-safe parser."""
from __future__ import annotations

import subprocess
import textwrap


def test_chained_comparison_uses_python_and_semantics(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                print(0 <= 0 <= 0)
                print(0 <= 1 <= 0)
                print(0 <= -1 <= 0)
                print(1 <= 1 <= 2)

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\nFalse\nFalse\nTrue\n"
