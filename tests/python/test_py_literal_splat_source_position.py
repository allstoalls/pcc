"""An iterable splat is expanded where it is written, not only when last.

``[a, *b, c]``, ``[*b, *c]`` and the tuple spellings used to be refused with
"iterable splat cannot precede following literal operands until expansion is
lowered at its source position". Both consumers in ``literal_lowering`` --
the native ``py_list_new``/``py_list_append``/``py_list_extend`` builder and
the CPython ``list()``/``append``/``extend`` builder -- already replayed the
recorded ``ops`` in source order, so the restriction outlived what it
described.

The gap blocked three modules in the C-frontend self-host closure:
``pcc.evaluater.c_evaluator``, ``pcc.backend.self_backend_aarch64_darwin`` and
``pcc.backend.x86_64_asm_driver`` (``[ElfSymbol.null(), *local, *global]``).

CPython is the oracle, including for evaluation order: the operands are
evaluated left to right before the container is filled, and a splat may
appear more than once.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


_SOURCE = '''
import fnmatch


def middle(a, b: list, c) -> list:
    return [a, *b, c]


def two_splats(b: list, c: list) -> list:
    return [*b, *c]


def splat_between(b: list, c, d: list) -> list:
    return [*b, c, *d]


def tuple_middle(a, b: list, c) -> tuple:
    return (a, *b, c)


def tuple_two_splats(b: list, c: list) -> tuple:
    return (*b, *c)


def with_cpython(a, values: list) -> list:
    return [a, *fnmatch.filter(values, "*.py"), "tail"]


def order_of_evaluation(log: list, b: list) -> list:
    def note(tag):
        log.append(tag)
        return tag

    return [note("one"), *b, note("two"), *b]


def main() -> None:
    print(middle(0, [1, 2], 3))
    print(two_splats([1], [2, 3]))
    print(splat_between([1], 2, [3, 4]))
    print(tuple_middle(0, [1, 2], 3))
    print(tuple_two_splats([1], [2, 3]))
    print(with_cpython("head", ["x.py", "y.txt"]))
    log = []
    print(order_of_evaluation(log, ["b"]))
    print(log)
    print(middle(0, [], 3))
    print(two_splats([], []))


main()
'''

_EXPECTED = [
    "[0, 1, 2, 3]",
    "[1, 2, 3]",
    "[1, 2, 3, 4]",
    "(0, 1, 2, 3)",
    "(1, 2, 3)",
    "['head', 'x.py', 'tail']",
    "['one', 'b', 'two', 'b']",
    "['one', 'two']",
    "[0, 3]",
    "[]",
]


def test_splat_before_other_operands_matches_cpython(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "literal_splat_positions.py"
    exe = tmp_path / "literal_splat_positions.out"
    src.write_text(_SOURCE, encoding="utf-8")
    # ``with_cpython`` needs the CPython arm to actually run rather than be
    # replaced by a fail-closed stub, so this links against libpython.
    compile_python(str(src), str(exe), libpython_mode="on")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == _EXPECTED


@pytest.mark.parametrize(
    "shape",
    [
        "[a, *b, c]",
        "[*b, *c]",
        "[*b, a, *c]",
        "(a, *b, c)",
        "(*b, *c)",
    ],
)
def test_splat_shapes_lower_without_libpython(tmp_path, shape):
    """The native builder path takes the same shapes with no CPython arm."""
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "shape.py"
    out = tmp_path / "shape.ll"
    src.write_text(
        textwrap.dedent(
            """
            def probe(a, b: list, c: list):
                return %s
            """
            % shape
        ),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    text = out.read_text(encoding="utf-8")
    assert "@py_list_extend" in text or "@py_tuple_concat" in text, text[:400]


def test_the_three_closure_modules_lower_under_strict_no_libpython():
    """The modules the restriction blocked, compiled the way pcc1 compiles."""
    import os

    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    repo_root = Path(__file__).resolve().parents[2]
    for rel, mod in (
        ("pcc/evaluater/c_evaluator.py", "pcc.evaluater.c_evaluator"),
        (
            "pcc/backend/self_backend_aarch64_darwin.py",
            "pcc.backend.self_backend_aarch64_darwin",
        ),
        ("pcc/backend/x86_64_asm_driver.py", "pcc.backend.x86_64_asm_driver"),
    ):
        path = repo_root / rel
        typed = type_infer.infer_module(
            parse_and_lift(path.read_text(encoding="utf-8"), str(path), mod)
        )
        codegen = L1CodeGen(typed, False, "on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._module_source_path = str(path)
        codegen._target_triple = ""
        codegen.generate(typed)
