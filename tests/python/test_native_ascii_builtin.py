"""Native ascii() escapes non-ASCII text inside any object's repr().

CPython's ascii(obj) is repr(obj) re-encoded with the ascii codec's
backslashreplace handler. The native runtime escaped str operands only, so
ascii() of a list or dict printed non-ASCII elements raw.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap


PROGRAM = textwrap.dedent(
    """
    class Label:
        def __repr__(self):
            return "Label(\\u00e9\\u20ac)"


    def main() -> None:
        print(ascii("plain"), ascii("caf\\u00e9"))
        print(ascii(["\\u00e9", "\\u20ac", "\\U0001f600", 1, None]))
        print(ascii(("x\\ufffd", b"\\xff")))
        print(ascii({"k\\u00e9": ["\\u2603"]}))
        print(ascii(Label()))
        print(ascii([1.5, True, "ascii only"]))
        print(ascii(b"\\xe9".decode("utf-8", "surrogateescape")))
        print(ascii([b"\\xe9".decode("utf-8", "surrogateescape")]))


    main()
    """
).lstrip()


def test_native_ascii_escapes_non_ascii_in_any_repr(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "ascii_builtin.py"
    exe = tmp_path / "ascii_builtin.out"
    src.write_text(PROGRAM)
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert native.returncode == 0, native.stderr

    python3 = shutil.which("python3")
    assert python3 is not None, "python3 oracle not found on PATH"
    ref = subprocess.run([python3, str(src)], capture_output=True, text=True, timeout=30)
    assert ref.returncode == 0, ref.stderr
    assert native.stdout == ref.stdout
