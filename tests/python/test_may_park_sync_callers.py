"""A parking callable called from a synchronous caller runs to completion.

Effect analysis compiles every callable that (transitively) waits on a native
``threading`` Event, Condition or Semaphore with the resumable generator ABI.
A resumable caller delegates to it.  The module body and other synchronous
callers used to receive the child generator as the call's value, so the
callee's body silently never ran: the program printed nothing and exited 0.
A real generator function keeps Python semantics and still returns its
generator.
"""

import os
import subprocess
import sys


PROGRAM = """\
from threading import Event, Semaphore


class Gate:
    def __init__(self):
        self.sem = Semaphore(4)

    def enter(self) -> int:
        self.sem.acquire()
        return 7


def take(sem) -> int:
    print("take")
    sem.acquire()
    return 41


def nested(sem) -> int:
    return take(sem) + 1


def waits_then_yields(ev):
    ev.wait()
    yield 1
    yield 2


def main() -> None:
    sem = Semaphore(3)
    print(take(sem))
    print(nested(sem))
    print(Gate().enter())
    ev = Event()
    ev.set()
    print(list(waits_then_yields(ev)))


main()
print(take(Semaphore(1)))
print(Gate().enter())
"""


def test_parking_calls_from_synchronous_callers_match_python(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    source = tmp_path / "may_park_sync.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "may_park_sync"
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
