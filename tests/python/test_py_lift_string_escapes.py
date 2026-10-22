"""The native parser decodes literal escapes exactly like CPython.

``_decode_escapes`` handled only the single-character escapes, ``\\xNN`` and
``\\uNNNN``: ``\\U0001f600`` stayed ten literal characters, ``\\N{...}`` was
kept verbatim, ``\\012`` became NUL followed by ``12``, a backslash-newline
survived inside the string, and a bytes literal decoded ``\\u`` although bytes
have no such escape.  pcc0 and pcc1 both parse with it, so every compiled
program saw those strings.
"""

import ast
import os
import subprocess
import sys

import pytest

from pcc.frontends.python.py_lift import _decode_escapes


STR_CASES = [
    r"h\u00e9\u4e2d\U0001f600z",
    r"a\012b\0c\7\177\101x",
    r"tab\tnl\nquote\'dq\"bs\\",
    r"\a\b\f\v\r",
    r"\N{LATIN SMALL LETTER E WITH ACUTE}\N{SNOWMAN}",
    r"keep\q\d",
    r"\x41\x7f\xff",
]


def _cpython_str(raw):
    return ast.literal_eval('"' + raw + '"')


@pytest.mark.parametrize("raw", STR_CASES)
def test_str_escapes_match_cpython(raw):
    assert _decode_escapes(raw) == _cpython_str(raw)


def test_backslash_newline_continues_the_literal():
    assert _decode_escapes("ab\\\ncd") == "abcd"


def test_bytes_keep_unicode_escapes_and_decode_octal_hex():
    raw = r"\u00e9\x41\101\N{X}\0"
    expected = ast.literal_eval('b"' + raw + '"').decode("latin-1")
    assert _decode_escapes(raw, True) == expected


@pytest.mark.parametrize("raw", [r"\x4", r"\u12", r"\U0011ffff", r"\N{NO SUCH NAME}", r"\Nx"])
def test_malformed_escapes_are_syntax_errors(raw):
    with pytest.raises(SyntaxError):
        _decode_escapes(raw)


PROGRAM = r'''def main():
    s = "h\u00e9\u4e2d\U0001f600z"
    print(len(s), [ord(c) for c in s])
    print(repr("a\012b\0c\177"), len("\N{SNOWMAN}"))
    b = b"\u00e9\x41\101"
    print(len(b), list(b))
    print("one\
two")


main()
'''


def test_compiled_program_sees_cpython_escapes(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "escapes.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=60,
    )
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "escapes"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    ran = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ),
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == reference.stdout
