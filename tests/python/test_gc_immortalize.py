"""Regression coverage for ``gc.immortalize`` (pcc_gc_immortalize).

The runtime's ``PY_FLAG_IMMORTAL`` early-return existed in
py_incref/py_decref but nothing user-facing could set it. ``gc.immortalize``
is pcc's native equivalent of the ``PyUnstable_Object_SetImmortal`` escape
free-threaded CPython 3.14 added for NumPy's shared-singleton refcount
contention (labs.quansight.org/blog/scaling-numpy-on-free-threaded-python).

Two behaviors are pinned:

  1. An immortalized object is never deallocated: a ``__del__`` canary must
     NOT fire after the last reference is dropped.
  2. Under ``PCC_WITH_THREADS=1``, four threads hammering the refcount of an
     immortalized shared instance still compute correct results (the flag
     removes the refcount traffic; it must not corrupt the object).

Scaling itself is benchmarked by benchmarks/python/shared_refcount_
contention*.py and recorded in the investigation, not asserted here.
"""
from __future__ import annotations

import subprocess

import pytest


@pytest.fixture
def threaded_runtime(monkeypatch, threaded_pcc_runtime_archive):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(threaded_pcc_runtime_archive))


CANARY_SRC = '''
import gc


class Canary:
    def __init__(self, v: int) -> None:
        self.v = v

    def __del__(self) -> None:
        print("DEL fired v=" + str(self.v))


def main() -> None:
    c = Canary(7)
    gc.immortalize(c)
    c = Canary(8)
    print("survivor v=" + str(c.v))
    print("DONE")


if __name__ == "__main__":
    main()
'''


SHARED_THREADS_SRC = '''
import gc
from threading import Thread


class Shared:
    def __init__(self, v: int) -> None:
        self.v = v


SHARED = Shared(7)


def touch(o: Shared) -> Shared:
    return o


def worker(idx: int, rounds: int) -> None:
    acc = 0
    i = 0
    while i < rounds:
        s = touch(SHARED)
        acc = acc + s.v
        i = i + 1
    print("t" + str(idx) + " acc=" + str(acc))


def main() -> None:
    gc.immortalize(SHARED)
    threads: list = []
    t = 0
    while t < 4:
        th = Thread(target=worker, args=(t, 10000))
        threads.append(th)
        t = t + 1
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    print("DONE")


if __name__ == "__main__":
    main()
'''


def _compile_and_run(tmp_path, name: str, source: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    src.write_text(source)
    exe = tmp_path / f"{name}.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"{name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_immortalized_object_is_never_deallocated(tmp_path, threaded_runtime):
    out = _compile_and_run(tmp_path, "immortal_canary", CANARY_SRC)
    lines = [ln.strip() for ln in out.strip().splitlines()]
    assert "DONE" in lines, f"missing DONE marker:\n{out}"
    assert "survivor v=8" in lines, f"rebinding broke:\n{out}"
    # The immortalized first Canary (v=7) must never run __del__; the
    # second, mortal Canary (v=8) is free to finalize at scope exit.
    assert "DEL fired v=7" not in lines, (
        f"immortalized object was deallocated:\n{out}"
    )


def test_immortalized_shared_object_threads_read_correctly(
    tmp_path, threaded_runtime
):
    out = _compile_and_run(tmp_path, "immortal_shared", SHARED_THREADS_SRC)
    lines = [ln.strip() for ln in out.strip().splitlines()]
    assert "DONE" in lines, f"missing DONE marker:\n{out}"
    worker_lines = [ln for ln in lines if ln.startswith("t") and " acc=" in ln]
    assert len(worker_lines) == 4, f"expected 4 workers:\n{out}"
    for ln in worker_lines:
        assert ln.endswith(" acc=70000"), f"wrong worker result: {ln}\n{out}"
