"""``finally`` must run on ``continue``/``break`` out of a try, no-libpython.

`break`/`continue` branched straight to the loop target without running the
pending `finally` blocks entered inside the loop (only `return` ran them). Fix
records the finally-stack depth at loop entry (3rd element of each `loop_stack`
frame) and `break`/`continue` run the finallys above that base before jumping
(`stmt_dispatch_lowering.py`). The boundary matters: a `finally` ENCLOSING the
loop must NOT run on an inner break/continue.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_finally_on_continue_break_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "fcb.py"
    exe = tmp_path / "fcb.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            # continue / break each run the try's finally
            for i in range(4):
                try:
                    if i == 1:
                        continue
                    if i == 3:
                        break
                    print("body", i)
                finally:
                    print("finally", i)
            print("---")
            # boundary: a finally ENCLOSING the loop runs ONCE (not per continue)
            try:
                for i in range(3):
                    try:
                        if i == 1:
                            continue
                        print("b", i)
                    finally:
                        print("inner-fin", i)
                print("after loop")
            finally:
                print("outer-fin")
            print("---")
            # nested loops: continue runs only the inner finally
            for a in range(2):
                for b in range(2):
                    try:
                        if b == 0:
                            continue
                        print("ab", a, b)
                    finally:
                        print("fin", a, b)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
