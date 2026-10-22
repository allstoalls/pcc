"""CPython's default reprs, class ``__module__``/``__qualname__`` and a stable
``id()``.

A plain instance used to print as ``<object tag=136>`` (``str()`` returned
NULL), a class as ``<object tag=10>``, and ``Writer.__module__`` raised.  The
forwarding collectors move objects, so ``id()`` (then a raw ``ptrtoint``)
could change during an object's lifetime; it now reports the stable object id
there, and ``object.__repr__`` shows the same value.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


LIB = """
class C:
    pass
"""

MAIN = """
import gc
import libm

class Writer:
    pass

class Named:
    def __repr__(self):
        return "Named!"

def f():
    pass

w = Writer()
s = str(w)
print(s.startswith("<__main__.Writer object at 0x"), s.endswith(">"), repr(w) == s)
print(str(Writer), repr(Writer), type(w))
print(Writer.__module__, Writer.__qualname__, Writer.__name__)
print(Named(), [Named()])
print(libm.C, libm.C.__module__)
c = libm.C()
print(repr(c).startswith("<libm.C object at 0x"), hex(id(c)) in repr(c))
print(repr(f).startswith("<function f at 0x"))
print(str(int), str(type(None)), repr(ValueError), str(type(3.5)))
before = id(w)
junk = [[i] for i in range(20000)]
del junk
gc.collect()
print(id(w) == before, hex(id(w)) in repr(w))
"""


@pytest.mark.parametrize("gc_backend", ["0", "4"])
def test_default_reprs_and_stable_id_match_cpython(tmp_path, monkeypatch, gc_backend):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_GC_BACKEND", gc_backend)
    (tmp_path / "libm.py").write_text(textwrap.dedent(LIB).lstrip(), encoding="utf-8")
    src = tmp_path / "main.py"
    exe = tmp_path / "main.out"
    src.write_text(textwrap.dedent(MAIN).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert native.returncode == 0, native.stderr
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )
    assert expected.returncode == 0, expected.stderr
    assert native.stdout == expected.stdout
