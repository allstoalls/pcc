"""``exc.__traceback__`` and the exception type name in AttributeError.

``__traceback__`` used to raise ``AttributeError: 'object' object has no
attribute '__traceback__'`` -- which also broke ``async with``, whose lowering
passes the caught exception's traceback to ``__aexit__``.  The chain is built
from the runtime's frame records (outermost frame first, ``tb_next`` inward),
so names and line numbers must match CPython's exactly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


SOURCE = """
def inner():
    raise KeyError("k")

def middle():
    inner()

def outer():
    try:
        middle()
    except KeyError as e:
        tb = e.__traceback__
        names = []
        lines = []
        while tb is not None:
            names.append(tb.tb_frame.f_code.co_name)
            lines.append(tb.tb_lineno)
            tb = tb.tb_next
        print(names)
        print(lines)
        print(type(e.__traceback__).__name__)
        try:
            e.nope
        except AttributeError as err:
            print(err)

def plain():
    try:
        raise ValueError("boom")
    except ValueError as e:
        print(e.__traceback__ is not None)
        print(getattr(e, "__traceback__", "missing") is not None)

outer()
plain()
"""


def test_traceback_chain_and_exception_attribute_error_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(SOURCE).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert native.returncode == 0, native.stderr
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30
    )
    assert expected.returncode == 0, expected.stderr
    assert native.stdout == expected.stdout
