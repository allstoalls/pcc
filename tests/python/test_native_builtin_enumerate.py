"""Native enumerate() lowering regressions."""
from __future__ import annotations

import subprocess
import textwrap


def test_native_enumerate_index_stays_numeric(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                xs = ["a", "b", "c"]
                out = []
                for i, _x in enumerate(xs):
                    out.append(i)
                print(out)

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
    assert run.stdout == "[0, 1, 2]\n"
