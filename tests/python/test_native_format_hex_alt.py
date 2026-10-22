"""Native alternate-form hex format spec ``#x`` / ``#X`` in no-libpython mode.

``f"{n:#x}"`` / ``"{:#x}".format(n)`` (the ``0x``/``0X`` prefix) previously
raised "unsupported format specifier" from the runtime spec engine
``py_obj_format`` (``format_int_builtin``).  Now the ``#`` flag and ``X``
conversion are handled; the rare ``#`` + zero-pad edge falls back.
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


def test_format_hex_alternate_form_matches_cpython(tmp_path, monkeypatch):
    src = tmp_path / "hx.py"
    exe = tmp_path / "hx.out"
    program = textwrap.dedent("""
        def main() -> None:
            n = 42
            print(f"{n:#x}")
            print(f"{n:#X}")
            print(f"{-42:#x}")
            print(f"{255:x}")
            print(f"{255:>8x}|")
            print("{:#x}".format(255))
            print("{0:#X}".format(255))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_format_octal_binary_matches_cpython(tmp_path, monkeypatch):
    # Octal {:o} / binary {:b} int conversions (incl. #o/#b prefixes, sign,
    # zero-pad) were missing from format_int_builtin; now generated inline.
    src = tmp_path / "ob.py"
    exe = tmp_path / "ob.out"
    program = textwrap.dedent("""
        def main() -> None:
            print(f"{42:b}")
            print(f"{42:o}")
            print(f"{42:#b}")
            print(f"{42:#o}")
            print(f"{-5:b}")
            print(f"{42:08b}")
            print("{:b}".format(255))
            print("{0:#o}".format(64))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
