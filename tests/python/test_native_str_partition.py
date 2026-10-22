"""Native ``str.partition(sep)`` in no-libpython mode.

``partition`` previously fell back to libpython (no native lowering); it now
lowers to ``py_str_partition`` (C runtime + pcc-Python port), returning the
``(before, sep, after)`` 3-tuple, or ``(s, '', '')`` when ``sep`` is absent.
This shape is common in package/import code (``name.partition('.')``).

The test runs the compiled binary and compares to the CPython reference for
the same program (differential), so escaping/quoting is never hand-encoded.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, encoding="utf-8",
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_str_partition_matches_cpython(tmp_path, monkeypatch):
    src = tmp_path / "partition.py"
    exe = tmp_path / "partition.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("a-b-c".partition("-"))
            print("abc".partition("x"))
            print("key=val".partition("="))
            print("==".partition("="))
            print("".partition("-"))
            before, sep, after = "pkg.mod.sub".partition(".")
            print(before, "|", sep, "|", after)
            print("h\\u00e9llo-w\\u00f6rld".partition("-"))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    ).stdout
    # The strict ``libpython_mode="off"`` compile above already proves no
    # libpython fallback (strict mode hard-errors on any py_cpy_* emission);
    # this asserts the runtime result matches CPython.
    assert _run(exe) == cpython


def test_str_rjust_ljust_matches_cpython(tmp_path, monkeypatch):
    # rjust/ljust were libpython fallbacks; now native py_str_rjust/ljust
    # (codepoint width via py_str_len, default ' ' or custom fillchar).
    src = tmp_path / "just.py"
    exe = tmp_path / "just.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("[" + "hi".rjust(5) + "]")
            print("[" + "hi".ljust(5) + "]")
            print("[" + "hi".rjust(5, "*") + "]")
            print("[" + "hi".ljust(5, "-") + "]")
            print("[" + "hi".rjust(1) + "]")
            print("[" + "42".rjust(5, "0") + "]")
            print("[" + "h\\u00e9".rjust(4) + "]")
            print("[" + "abc".ljust(6, ".") + "]")

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    ).stdout
    assert _run(exe) == cpython


def test_str_removeprefix_removesuffix_matches_cpython(tmp_path, monkeypatch):
    # removeprefix/removesuffix were libpython fallbacks; now native
    # py_str_removeprefix/removesuffix (byte-level prefix/suffix slice).
    src = tmp_path / "rp.py"
    exe = tmp_path / "rp.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("test_foo".removeprefix("test_"))
            print("foo.py".removesuffix(".py"))
            print("plain".removeprefix("xxx"))
            print("plain".removesuffix("xxx"))
            print("__main__".removeprefix("__"))
            print("abc".removeprefix(""))
            print("abc".removesuffix(""))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    ).stdout
    assert _run(exe) == cpython


def test_str_rsplit_matches_cpython(tmp_path, monkeypatch):
    # rsplit was a libpython fallback; now native py_str_rsplit_maxsplit
    # (right-scan; rsplit(sep) without a limit == split(sep)). Common in
    # module/path parsing (``name.rsplit('.', 1)``).
    src = tmp_path / "rs.py"
    exe = tmp_path / "rs.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("a.b.c.d".rsplit(".", 1))
            print("a.b.c.d".rsplit(".", 2))
            print("a.b.c".rsplit("."))
            print("abc".rsplit(".", 1))
            print("a.b".rsplit(".", 5))
            print("pkg.mod.ext".rsplit(".", 1))
            print("/a/b/c".rsplit("/", 1))
            print("x".rsplit(".", 0))
            print("a::b::c".rsplit("::", 1))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    ).stdout
    assert _run(exe) == cpython


def test_str_center_zfill_matches_cpython(tmp_path, monkeypatch):
    # center/zfill were libpython fallbacks; now native py_str_center/zfill.
    # center uses CPython's marg//2 + (marg & width & 1) split; zfill keeps a
    # leading +/- sign before the zero padding.
    src = tmp_path / "cz.py"
    exe = tmp_path / "cz.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("[" + "hi".center(6) + "]")
            print("[" + "hi".center(5) + "]")
            print("[" + "hi".center(7, "*") + "]")
            print("[" + "abc".center(2) + "]")
            print("[" + "42".zfill(5) + "]")
            print("[" + "-42".zfill(5) + "]")
            print("[" + "+7".zfill(4) + "]")
            print("[" + "5".zfill(1) + "]")
            print("[" + "x".center(4, ".") + "]")

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    ).stdout
    assert _run(exe) == cpython
