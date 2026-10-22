"""``x.clear()`` reaches the receiver's own ``clear``.

Three gaps, all silent: a typed set's ``clear`` was devirtualized to the only
user class defining ``clear`` (called with the set as ``self``); the dynamic
``py_obj_clear`` helper handled only list and dict and returned without
effect for a set, bytearray, user object or ``threading.Event``; and neither
set nor bytearray had a native ``clear`` at all.
"""

import os
import subprocess
import sys


PROGRAM = """\
from threading import Event


class Bag:
    def __init__(self):
        self.items = [1, 2]

    def clear(self):
        self.items = []
        print("bag-clear")


def dyn_clear(x):
    x.clear()
    return x


def main() -> None:
    s = {1, 2, 3}
    s.clear()
    print(len(s))
    s.add(9)
    print(sorted(s))
    ba = bytearray(b"abc")
    ba.clear()
    print(len(ba), ba)
    print(len(dyn_clear({4, 5})))
    print(len(dyn_clear([1])), len(dyn_clear({1: 2})))
    print(len(dyn_clear(bytearray(b"xyz"))))
    print(len(dyn_clear(Bag()).items))
    ev = Event()
    ev.set()
    print(dyn_clear(ev).is_set())
    try:
        dyn_clear(5)
    except AttributeError:
        print("no-clear")


main()
"""


def test_clear_dispatches_to_the_receiver_under_every_backend(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    source = tmp_path / "clear_dispatch.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "clear_dispatch"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == reference.stdout, backend
