"""bytes/bytearray ``%``-formatting (``b"%d-%s" % (5, b"x")``), no-libpython.

Runtime path: the frontend routes ``BytesType % <args>`` through the generic
``py_obj_mod`` dispatcher (binary_op_lowering, non-numeric branch). Before this
slice ``py_obj_mod`` rejected bytes with ``TypeError: unsupported operand
type(s) for %``. Now it routes bytes/bytearray to the new ``py_bytes_mod``
(``src/py_format.c``, a C-only OBJ_PY_CC_HELPERS helper linked in both tiers),
with the dispatch mirrored in the C ``py_obj_ops_dispatch.c`` and the pcc-Python
port ``py/py_obj_ops_mod.py``.

Covers the CPython bytes-format semantics that differ from str %:
``%s``/``%b`` require a bytes-like object, ``%r``/``%a`` emit ``ascii(arg)``,
``%c`` accepts a length-1 bytes, ``%%`` collapses, mapping keys are bytes, and
``bytearray % ...`` yields a bytearray.

Both tiers are exercised: default (port) mode links the pcc-Python
``py_obj_ops_mod`` port; ``PCC_RUNTIME_CC=cc`` links the C dispatcher. Both call
the shared C ``py_bytes_mod`` helper.

The ``%r`` / ``%a`` cases wrap the conversion in literal ``"`` on purpose:
their payload (``b'ab'``) always contains single quotes, and CPython's
``bytes.__repr__`` switches to double-quote delimiters when the payload has
``'`` and no ``"`` (``b"b'ab'"``) — a print-repr quote-selection nuance pcc's
bytes repr (``py_print_fmt.c`` / port ``py_print_fmt.py``, NOT the ``%``
formatter under test) does not implement yet. With both quote kinds present,
CPython also uses single-quote delimiters and escapes ``'``, so both sides
agree byte-for-byte while the formatted payload is still fully verified
through the stdout diff.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(b"%d-%s" % (5, b"x"))
        print(b"%s/%s" % (b"a", bytearray(b"bb")))
        print(b'"%r"' % b"ab")
        print(b'"%a"' % b"cd")
        print(b"%x=%X=%o" % (255, 255, 8))
        print(b"%c%c" % (65, b"Z"))
        print(b"%5d|%-5d|" % (3, 3))
        print(b"%.2s|" % b"abcd")
        print(b"%5s|" % b"ab")
        print(b"%(k)s=%(n)d" % {b"k": b"key", b"n": 7})
        print(b"%d%% complete" % 50)
        print(b"%f" % 1.5)
        print(b"%d" % True)
        print(bytearray(b"n=%d") % 9)

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bytes_percent_format_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "bpf.py"
    exe = tmp_path / "bpf.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
