"""A sibling module's ABI slot type wins over the caller's int policy.

Modules choose their integer representation independently: a module that
imports ``pcc.unsafe``/``pcc.extern`` (or is named ``pcc.*``) keeps ints raw
(``i64``), everything else boxes them as objects.  When an application module
calls into such a module, the *callee's* slot type is authoritative -- passing
a boxed ``PyObject*`` into an ``i64`` parameter produced
``'%int.obj.neg' defined with type 'ptr' but expected 'i64'`` and only for
values that are emitted as objects in the caller (a negative literal, an
unproven expression).  This pins constructor arguments, plain function
arguments, and module-level int globals in both directions.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from pcc.frontends.python.pipeline import compile_python

_RAW_INT_MODULE = """
    \"\"\"A raw-int module: importing pcc.unsafe puts ints in the i64 lane.\"\"\"
    from pcc.unsafe import ptr_to_int, null  # noqa: F401  (scaffold marker)

    LIMIT = 64
    FLOOR = -7


    class Conn:
        def __init__(self, fd: int, name: str) -> None:
            self.fd = fd
            self.name = name

        def describe(self) -> str:
            return self.name + ":" + str(self.fd)

        def shifted(self, delta: int) -> int:
            return self.fd + delta


    def twice(value: int) -> int:
        return value * 2
    """

_APP = """
    from rawmod import Conn, FLOOR, LIMIT, twice

    conn = Conn(-1, "sock")
    print(conn.describe())
    print(conn.shifted(-4), conn.shifted(4))
    print(twice(-3), twice(4))
    print(LIMIT, FLOOR)
    """


def _build_and_run(tmp_path: Path) -> str:
    (tmp_path / "rawmod.py").write_text(textwrap.dedent(_RAW_INT_MODULE).lstrip(), encoding="utf-8")
    app = tmp_path / "app.py"
    app.write_text(textwrap.dedent(_APP).lstrip(), encoding="utf-8")
    exe = tmp_path / "app"
    compile_python(str(app), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self")
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_boxed_ints_reach_a_raw_int_siblings_scalar_slots(tmp_path: Path) -> None:
    assert _build_and_run(tmp_path) == "sock:-1\n-5 3\n-6 8\n64 -7\n"
