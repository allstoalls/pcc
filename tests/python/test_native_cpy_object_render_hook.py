"""Regression for the CPython-PyObject render hook
(``py_format_try_cpy_object_into_fd`` + ``py_format_cpy_object_hook``).

When pcc compiles with ``--python-libpython=auto`` / ``on`` and a
CPython PyObject reaches pcc's print path (e.g., from a dict / list
that holds a libpython-bridged value), the runtime would otherwise
render ``<object tag=N>`` because the type-tag at ``obj+8`` is the
truncated low 32 bits of ``ob_type`` (a heap address), not a
``PY_TYPE_*`` enum.  The hook routes such objects through
``PyObject_Str`` so they print correctly.

See:
- ``pcc/runtime/src/py_format.c``: defines
  ``py_format_cpy_object_hook`` + ``py_format_try_cpy_object_into_fd``
  with a tag-threshold guard.
- ``pcc/runtime/src/py_libpython.c::py_cpy_ensure_init``: installs
  the hook on first libpython init.
- ``pcc/runtime/py/py_print_fmt.py`` (and ``.c``): consult the
  helper before emitting ``<object tag=N>``.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile_auto(monkeypatch, src: Path, exe: Path) -> None:
    """Compile with ``--python-libpython=auto`` so libpython is linked
    iff fallback is needed (it is, because ``json.JSONDecoder`` is
    looked up via CPython).  Self backend + ir-scaffold=on stays
    consistent with the rest of the suite.
    """
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="auto", backend="self",
    )


def test_cpython_class_in_pcc_dict_prints_via_hook(tmp_path, monkeypatch):
    """A CPython class object stored in a pcc dict must print via
    ``PyObject_Str`` (matching CPython's ``str(cls)``) rather than as
    ``<object tag=N>``.

    Before the hook landed, this exact shape rendered
    ``<object tag=53891200>`` (or similar — the truncated
    ``&PyType_Type`` address).
    """
    src = tmp_path / "hook.py"
    exe = tmp_path / "hook.out"
    src.write_text(textwrap.dedent("""
        import json
        d = {}
        d["k"] = json.JSONDecoder
        print(d["k"])
    """).lstrip(), encoding="utf-8")
    _compile_auto(monkeypatch, src, exe)
    proc = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # CPython prints classes as ``<class 'module.Name'>``; the hook
    # routes the value through PyObject_Str so the output matches
    # exactly.  Without the hook this would have been a pcc
    # ``<object tag=N>`` literal.
    assert proc.stdout.strip() == "<class 'json.decoder.JSONDecoder'>", (
        f"unexpected stdout: {proc.stdout!r}"
    )
