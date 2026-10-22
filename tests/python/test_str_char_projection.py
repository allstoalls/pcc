"""A character read from a str is a value, not an object.

``s[i]`` of a str is exactly one character.  Compared with a one-character
literal, or tested for membership in a str, only its code point matters, so
the compiler lowers those tests to ``py_str_codepoint_at`` (plus
``py_str_contains_codepoint`` for a non-literal str) and never builds the
one-character string.  Character-scanning loops -- pcc's own IR text passes --
built one such string per character: over half of all allocations in a pcc1
compile.  Where a one-character string is still materialized, ASCII ones are
shared immortal objects, as CPython shares its latin-1 characters (identity of
equal strings stays an implementation detail, so it is not asserted here).
"""

import os
import re
import subprocess
import sys


PROGRAM = '''CHARSET = "abcdefghijklmnopqrstuvwxyz_."


def scan(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "%":
            i += 1
            continue
        end = i + 1
        while end < len(text) and text[end] in CHARSET:
            end += 1
        out.append(text[i + 1 : end])
        i = end
    return out


def classify(s: str, i: int) -> str:
    if s[i] == "x":
        return "x"
    if "y" == s[i]:
        return "y"
    if s[i] in "0123456789":
        return "digit"
    if s[i] not in "aeiou":
        return "consonant"
    return "vowel"


def main():
    print(scan("  %a.b = add %cd, %e_f ; %"))
    print([classify("xy7bu", i) for i in range(5)])
    print([classify("xy7bu", -i) for i in range(1, 6)])
    uni = "h\\u00e9\\u4e2d\\U0001f600z"
    print([uni[i] == "\\u4e2d" for i in range(len(uni))])
    print([uni[i] in "\\u00e9\\U0001f600" for i in range(len(uni))])
    print([uni[i] in CHARSET for i in range(len(uni))])
    try:
        print("abc"[3] == "c")
    except IndexError as exc:
        print("IndexError", exc)
    try:
        print("abc"[-4] in "abc")
    except IndexError as exc:
        print("IndexError", exc)
    word = "state"
    print(word[1] == "tt"[0], word[1:2] == word[1], len(word[1:2]), word[1:2] + word[4])


main()
'''


def _compile_to_ir(tmp_path, source_text, name):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / (name + ".py")
    out = tmp_path / (name + ".ll")
    src.write_text(source_text, encoding="utf-8")
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    return out.read_text(encoding="utf-8")


def _function_body(ir_text, suffix):
    match = re.search(r"^define [^\n]*@user_\w*" + suffix + r"\(.*?^}", ir_text, re.S | re.M)
    assert match, suffix
    return match.group(0)


def test_character_tests_build_no_one_character_string(tmp_path):
    ir_text = _compile_to_ir(tmp_path, PROGRAM, "char_projection_ir")
    classify = _function_body(ir_text, "classify")
    assert "@py_str_codepoint_at(" in classify
    assert "@py_str_index(" not in classify
    assert "@py_str_eq(" not in classify
    scan = _function_body(ir_text, "scan")
    assert "@py_str_contains_codepoint(" in scan
    assert "@py_str_index(" not in scan


def test_character_projection_matches_cpython_under_all_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "char_projection.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=60,
    )
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "char_projection"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == reference.stdout, f"GC{backend}: {ran.stdout!r}"
