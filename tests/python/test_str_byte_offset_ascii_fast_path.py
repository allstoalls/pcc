"""``str.find`` must not rescan the string just to convert its own result.

``_byte_offset_to_cp_offset`` turned the byte position a search returns into a
Python codepoint index by counting codepoints from the start -- O(n) on top of
the search itself, so N searches over one string were O(N*n). The reverse
direction (``utf8_byte_offset_for_codepoint``) already had an all-ASCII fast
path; this adds the missing one, keyed on the cached ``cp_len == byte_len``.

Measured under pcc1 on a 286 KB module: rewriting a character loop as repeated
``find()`` calls went 65 ms -> 405 ms before the fast path and 65 ms -> 12 ms
after. The same rewrite is 6.4x *faster* on CPython, whose indices are O(1) --
which is exactly why a host-only measurement passed it as an optimisation.

The point of these tests is that the fast path must not change any answer.
"""

from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python

MIXED = "abc中文deféghi"


def _run(tmp_path, source: str) -> str:
    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_find_and_index_match_cpython_on_mixed_width_text(tmp_path):
    """Every answer below is what CPython gives for the same string."""
    source = f'''
        def main() -> None:
            s = {MIXED!r}
            print(len(s))
            print(s.find("\\u4e2d"))
            print(s.find("\\u6587"))
            print(s.find("def"))
            print(s.find("\\u00e9"))
            print(s.rfind("i"))
            print(s[3])
            print(s[3:5])
            print(s[7])


        main()
    '''
    got = _run(tmp_path, source).split()
    expected = [
        str(len(MIXED)),
        str(MIXED.find("中")),
        str(MIXED.find("文")),
        str(MIXED.find("def")),
        str(MIXED.find("é")),
        str(MIXED.rfind("i")),
        MIXED[3],
        MIXED[3:5],
        MIXED[7],
    ]
    assert got == expected


def test_ascii_string_offsets_are_unchanged(tmp_path):
    """The fast path's own case: byte offset and codepoint offset coincide."""
    text = "pure ascii text here"
    source = f'''
        def main() -> None:
            a = {text!r}
            print(len(a))
            print(a.find("text"))
            print(a[5:10])
            print(a.rfind("e"))


        main()
    '''
    got = _run(tmp_path, source).split("\n")[:4]
    assert got == [
        str(len(text)), str(text.find("text")), text[5:10], str(text.rfind("e"))
    ]


def test_find_after_multibyte_prefix_is_not_shifted_by_the_fast_path(tmp_path):
    """A string that is ASCII *after* a multibyte prefix must still convert:
    taking the fast path here would report byte offsets as codepoint offsets."""
    text = "中" + "x" * 40
    source = f'''
        def main() -> None:
            s = {text!r}
            print(len(s))
            print(s.find("x"))
            print(s.rfind("x"))


        main()
    '''
    got = _run(tmp_path, source).split()
    assert got == [str(len(text)), str(text.find("x")), str(text.rfind("x"))]
