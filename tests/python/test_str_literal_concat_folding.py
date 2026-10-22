"""Adjacent string literals joined with ``+`` must fold to one literal.

CPython's peephole folds ``"a" + "b"``, so a loop containing a literal
concatenation costs it nothing. pcc allocated and concatenated on every
iteration: 300k iterations of ``"item" + "x"`` measured 7 ms on CPython against
95 ms under pcc1 — the widest per-operation gap in the benchmark — and 26 ms
after folding.

Folding is only valid when *both* sides are string literals. Anything else may
have a user ``__add__`` or a type only known at runtime, so these tests pin the
boundary as much as the optimisation.
"""

from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def _run(tmp_path, source: str) -> list[str]:
    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.splitlines()


def test_literal_concat_results_match_cpython(tmp_path):
    assert _run(
        tmp_path,
        '''
        def main() -> None:
            print("item" + "x")
            print("a" + "b" + "c")
            print("" + "z")
            print("z" + "")
            print(len("hello" + "world"))


        main()
        ''',
    ) == ["itemx", "abc", "z", "z", "10"]


def test_folding_does_not_swallow_a_dynamic_operand(tmp_path):
    """One literal plus a runtime value must still concatenate at runtime."""
    assert _run(
        tmp_path,
        '''
        def main() -> None:
            n = 5
            s = "v"
            print("v" + str(n))
            print(s + "w")
            print("q" + s)


        main()
        ''',
    ) == ["v5", "vw", "qv"]


def test_escapes_and_non_ascii_survive_folding(tmp_path):
    """The fold concatenates decoded values, so escapes and multibyte text must
    come through unchanged -- a fold done on raw source text would not."""
    assert _run(
        tmp_path,
        '''
        def main() -> None:
            print("a\\tb" + "c")
            print("\\u4e2d" + "\\u6587")
            print(len("\\u4e2d" + "x"))


        main()
        ''',
    ) == ["a\tb" + "c", "中文", "2"]
