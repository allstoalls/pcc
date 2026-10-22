"""CPython parity for ``str`` methods, ported from
``Lib/test/test_str.py`` and ``Lib/test/test_unicode.py``.

All tests use ``libpython_mode="off"`` so we exercise pcc's native
``str`` runtime, not CPython's ``PyUnicode_*``.

Reference contract (from CPython):
  * ``str`` is immutable; methods return new ``str`` instances
  * indexing / slicing yields ``str`` (not bytes); negative indices
    work end-relative
  * ``in`` is substring search
  * ``str.join`` / ``str.split`` / ``str.replace`` / ``str.strip`` /
    ``str.find`` / ``str.startswith`` / ``str.endswith`` / case
    conversions are the canonical surface
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_str_split_join(tmp_path, monkeypatch):
    src = tmp_path / "str_split_join.py"
    exe = tmp_path / "str_split_join.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            parts = "a,b,c,d".split(",")
            print(parts[0], parts[1], parts[2], parts[3])
            print(",".join(parts))
            print("-".join(["x", "y", "z"]))
            print("/".join(("tuple", "items")))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "a b c d",
        "a,b,c,d",
        "x-y-z",
        "tuple/items",
    ]


def test_str_replace(tmp_path, monkeypatch):
    src = tmp_path / "str_replace.py"
    exe = tmp_path / "str_replace.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print("hello world".replace("world", "pcc"))
            print("aaa".replace("a", "b"))
            print("foo bar foo".replace("foo", "X", 1))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["hello pcc", "bbb", "X bar foo"]


def test_str_strip(tmp_path, monkeypatch):
    src = tmp_path / "str_strip.py"
    exe = tmp_path / "str_strip.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print("  spaces  ".strip())
            print("xxhelloxx".strip("x"))
            print("  left".lstrip())
            print("right  ".rstrip())

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["spaces", "hello", "left", "right"]


def test_str_find_startswith_endswith(tmp_path, monkeypatch):
    src = tmp_path / "str_find.py"
    exe = tmp_path / "str_find.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print("hello world".find("world"))
            print("hello world".find("missing"))
            print("hello".startswith("he"))
            print("hello".startswith("lo"))
            print("hello".endswith("lo"))
            print("hello".endswith("he"))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "6",
        "-1",
        "True",
        "False",
        "True",
        "False",
    ]


def test_str_case_conversion(tmp_path, monkeypatch):
    src = tmp_path / "str_case.py"
    exe = tmp_path / "str_case.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print("Hello".upper())
            print("Hello".lower())
            print("hello world".capitalize())

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["HELLO", "hello", "Hello world"]


def test_str_slicing(tmp_path, monkeypatch):
    src = tmp_path / "str_slice.py"
    exe = tmp_path / "str_slice.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            s = "abcdef"
            print(s[0])
            print(s[5])
            print(s[-1])
            print(s[1:4])
            print(s[:3])
            print(s[3:])
            print(s[::2])

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "a",
        "f",
        "f",
        "bcd",
        "abc",
        "def",
        "ace",
    ]


def test_str_concat_repeat(tmp_path, monkeypatch):
    src = tmp_path / "str_concat.py"
    exe = tmp_path / "str_concat.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print("foo" + "bar")
            print("ab" * 3)
            print("x" * 0)

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).splitlines() == ["foobar", "ababab", ""]


def test_str_in_operator(tmp_path, monkeypatch):
    src = tmp_path / "str_in.py"
    exe = tmp_path / "str_in.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print("ell" in "hello")
            print("xyz" in "hello")
            print("" in "hello")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "False", "True"]


def test_str_len(tmp_path, monkeypatch):
    src = tmp_path / "str_len.py"
    exe = tmp_path / "str_len.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            print(len(""))
            print(len("a"))
            print(len("hello"))
            print(len("a b c"))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0", "1", "5", "5"]


def test_str_fstring_basic(tmp_path, monkeypatch):
    src = tmp_path / "str_fstring.py"
    exe = tmp_path / "str_fstring.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            x = 42
            name = "pcc"
            print(f"x={x}")
            print(f"name={name}")
            print(f"{x} + {x} = {x + x}")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "x=42",
        "name=pcc",
        "42 + 42 = 84",
    ]


def test_str_fstring_format_spec(tmp_path, monkeypatch):
    src = tmp_path / "str_fstring_spec.py"
    exe = tmp_path / "str_fstring_spec.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            x = 1234.5
            print(f"{x:.2f}")
            n = 1234567
            print(f"{n:,}")
            i = 7
            print(f"{i:03d}")

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1234.50", "1,234,567", "007"]


def test_str_bytes_literal_nonascii(tmp_path, monkeypatch):
    src = tmp_path / "bytes_literal.py"
    exe = tmp_path / "bytes_literal.out"
    src.write_text(
        textwrap.dedent(r"""
        def main() -> None:
            b = b"\xff\x00\x01"
            print(len(b))
            print(b[0])
            s = b"abcdef"
            print(s[1:4].decode())
            print(b[::-1][0])
            print(b[::2][1])
            ba = bytearray(b)
            print(ba[:2][0])

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["3", "255", "bcd", "1", "1", "255"]
