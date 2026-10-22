"""``__del__`` runs on a temporarily resurrected object.

py_instance_dealloc calls the finalizer at refcount 0.  The finalizer's
argument tuple takes and drops a reference to ``self``; without CPython's
temporary resurrection (PyObject_CallFinalizerFromDealloc) that drop reached
zero inside the call and freed the object there, and the outer dealloc then
re-tracked the freed cell.  GC0's cycle collector later visited that cell and
crashed, so every program that freed ``__del__`` instances and then collected
died under the refcount backend.
"""

import os
import subprocess
import sys


PROGRAM = """\
import gc

finalized = 0
saved = []


class Plain:
    def __init__(self, n):
        self.n = n
        self.blob = b"x" * 64

    def __del__(self):
        global finalized
        finalized += 1


class Phoenix:
    def __init__(self, n):
        self.n = n

    def __del__(self):
        saved.append(self)


def churn() -> None:
    items = [Plain(i) for i in range(200)]
    del items


def main() -> None:
    churn()
    gc.collect()
    print(finalized)
    phoenix = Phoenix(5)
    del phoenix
    gc.collect()
    print(len(saved), saved[0].n)
    saved.clear()
    gc.collect()
    print(finalized, len(saved))


main()
"""


def test_finalizer_resurrection_matches_python_under_every_backend(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    source = tmp_path / "finalizer_resurrection.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "finalizer_resurrection"
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
