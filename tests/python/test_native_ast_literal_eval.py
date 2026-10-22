"""Native ``ast.literal_eval`` agrees with CPython's.

pcc/stdlib/ast.py accepted only integers and flat integer tuples, so the
array-core tooling (pcc -m pcc.package array-core --literal "[[1,2],[3,4]]")
lost its literal natively: value_from_literal caught the ValueError and
returned an empty array. The provider now parses CPython's literal grammar.
The first test keeps the original integer/tuple surface ("(2,,3)" is a
SyntaxError in CPython, as it now is here); the second prints each case's
value or exception type and must match CPython under every collector.
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_ast_literal_eval_integer_tuple_subset_no_libpython(tmp_path):
    source = tmp_path / "prog.py"
    source.write_text(
        "import ast\n"
        "print(ast.literal_eval('2'))\n"
        "print(ast.literal_eval(' (2) '))\n"
        "print(ast.literal_eval('(2,)'))\n"
        "print(ast.literal_eval('(2, 3)'))\n"
        "print(ast.literal_eval('2, 3'))\n"
        "print(ast.literal_eval('()'))\n"
        "try:\n"
        "    ast.literal_eval('(2,,3)')\n"
        "except SyntaxError:\n"
        "    print('invalid')\n",
        encoding="utf-8",
    )
    executable = tmp_path / "prog_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=20, env=env
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "2",
        "2",
        "(2,)",
        "(2, 3)",
        "(2, 3)",
        "()",
        "invalid",
    ]


CASES = [
    "1", "-1", "+1", "- 1", "0", "00", "007", "1_000", "1__0", "0x_ff", "0xFF", "0o17", "0b101",
    "0b2", "1.5", ".5", "1.", "1e3", "1E-3", "1_0.5e1_0", "1e", "2j", "1+2j", "-1-2.5j",
    "1.5+0j", "2j+1", "1+2", "1+-2j", "--1", "-True", "True", "False", "None", "none", "x",
    "'a'", '"b"', "'''c\nd'''", "r'\\n'", "b'x\\x00y'", "rb'\\x'", "B'a' b'b'", "'a' 'b'",
    "'a' b'b'", "'\\t\\n\\\\\\''", "'\\x41\\101\\u00e9'", "u'u'", "f'x'", "'unterminated",
    "()", "(1)", "(1,)", "(1, 2,)", "[]", "[1, [2, (3,)], {'k': [4]}]", "{}", "{1, 2}",
    "{1: 2, 'a': None,}", "{1, 2:3}", "{1:2, 3}", "set()", "set([1])", "1, 2", "1,", "1, 2 3",
    "[1,,2]", "  [1,\n 2]  ", "[1, # c\n 2]", "1\n2", "\t3", "", "   ", "[[1,2,3],[4,5,6]]",
    "[1.5, -2, True]", "{[1]: 2}",
]

_PROGRAM = """
import ast

CASES = {cases!r}


def main():
    for case in CASES:
        try:
            value = ast.literal_eval(case)
        except Exception as exc:
            print(type(exc).__name__)
            continue
        print(type(value).__name__, repr(value))


main()
"""


def test_literal_eval_matches_cpython_natively(tmp_path):
    source = tmp_path / "literals.py"
    source.write_text(_PROGRAM.format(cases=CASES), encoding="utf-8")
    exe = tmp_path / "literals_bin"
    env = os.environ.copy()
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stderr
    expected = subprocess.run([sys.executable, "-W", "ignore", str(source)], text=True,
                              capture_output=True, timeout=60, env=env, cwd=tmp_path)
    assert expected.returncode == 0, expected.stderr
    assert len(expected.stdout.splitlines()) == len(CASES)
    for backend in range(5):
        native = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60,
                                env=dict(env, PCC_GC_BACKEND=str(backend)))
        assert native.returncode == 0, (backend, native.stderr)
        assert native.stdout == expected.stdout, backend
