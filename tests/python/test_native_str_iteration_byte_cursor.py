"""Iterating a non-ASCII str is linear in native pcc.

pcc stores str as UTF-8.  The sequence iterator used to fetch each item with
``py_str_index(s, i)``, and a ``for ch in s`` loop over a str-typed value with
``py_str_slice(s, i, i + 1)``; both convert the codepoint index ``i`` by
rescanning the bytes from the start of any non-ASCII string, so ``list(s)``,
``iter(s)`` and ``for`` loops were quadratic.  Under pcc1 that kept the Stage2
export of a non-ASCII module such as ``pcc.frontends.python.type_infer`` at
197 s (run 38411293966) after its scanner switched to ``list(source)``.  Both
now keep the byte offset of the next character.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


_PROBE = '''
def main():
    s = "a\\u00e9\\u65e5\\U0001f680b" * 3 + "\\x7f" + "z"
    chars = list(s)
    print(len(chars), len(s), chars == [c for c in s], "".join(chars) == s)
    print([ord(c) for c in chars])
    it = iter("\\u00e9\\U0001f680a")
    print(ord(next(it)), ord(next(it)), next(it))
    try:
        next(it)
        print("no StopIteration")
    except StopIteration:
        print("StopIteration")
    print(list(""), list("abc"), tuple("x\\u00e9"))
    words = [w for w in "\\u00e9t\\u00e9 caf\\u00e9".split(" ")]
    print([list(w) == [c for c in w] for w in words])
    collected = []
    for ch in s:
        collected.append(ch)
    print(collected == chars)
    kept = []
    for ch in s:
        if ch == "b":
            continue
        kept.append(ord(ch))
    print(kept)
    width = 0
    for ch in "x\\u00e9\\U0001f680":
        width += len(ch.encode("utf-8"))
    print(width)
    for ch in "":
        print("never")
    print("".join([c.upper() for c in s.lower() if c.isascii()]))
    print([c for c in s if c != "b"] == [c for c in chars if c != "b"])
    print(sorted({c for c in s}), sorted({c: ord(c) for c in s}.items()))
    print([a + b for a in "\\u00e9x" for b in "y\\u65e5"])
    print([c for c in ""], [c * 2 for c in "\\U0001f680"])
    print(s[0], s[1], s[2], ord(s[3]), s[-1], s[-2], s[-15])
    try:
        print(s[len(s)])
    except IndexError:
        print("IndexError")


main()
'''

_LONG_PROBE = '''
def main():
    text = "\\u00e9" * 300000 + "x"
    chars = list(text)
    print(len(chars), chars[0] == "\\u00e9", chars[-1])
    count = 0
    last = ""
    for ch in text:
        count += 1
        last = ch
    print(count, last)
    kept = [ch for ch in text if ch != "x"]
    print(len(kept))


main()
'''


def _build(tmp_path, name, text, python_program_compiler, pcc_runtime_archive):
    source = tmp_path / (name + ".py")
    source.write_text(text, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / name
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    return output, expected.stdout


def test_native_str_iteration_matches_cpython(tmp_path, pcc_runtime_archive, python_program_compiler):
    output, expected = _build(tmp_path, "str_iteration_probe", _PROBE,
                              python_program_compiler, pcc_runtime_archive)
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected, f"GC{gc}: {ran.stdout}"


def test_native_list_of_long_non_ascii_str_is_linear(tmp_path, pcc_runtime_archive, python_program_compiler):
    # 300,001 items.  Rescanning from byte 0 per item touches ~9e10 bytes
    # (minutes); a byte cursor touches 600 KB.  The timeout only has to tell
    # those apart.
    output, expected = _build(tmp_path, "str_iteration_long", _LONG_PROBE,
                              python_program_compiler, pcc_runtime_archive)
    ran = subprocess.run(
        [str(output)], capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PCC_GC_BACKEND="0"),
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout == expected
