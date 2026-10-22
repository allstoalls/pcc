"""Literal-tuple membership keeps Python's semantics; valuebox fields are owned once.

``x in (a, b, c)`` against literals is compiled without building the tuple.
The unrolled chain compared every element and never checked for an error, so
a needle with ``__eq__`` saw extra calls and a raising ``__eq__`` was
swallowed (the exception surfaced later, somewhere else).  It now stops at
the first match and propagates errors; native int/bool needles compare as
integers.  A needle object created for the test was never released, and
boxing a valueclass payload leaked one reference per freshly boxed field
(nested valueboxes, boxed ints): a million ``Segment(...) in items`` tests
peaked at 606 MB under GC0 and 1.6 GB under GC4.
"""

import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

SEMANTICS = '''import pcc


@pcc.valueclass
class Point:
    x: int
    y: int


class Counter:
    calls = 0

    def __init__(self, hit):
        self.hit = hit

    def __eq__(self, other):
        Counter.calls += 1
        return other == self.hit

    def __hash__(self):
        return 1


class Boom:
    def __eq__(self, other):
        raise ValueError("boom " + str(other))


def pick(i):
    return [0, 1, 2, "a", None, 2.5][i]


def main() -> None:
    for i in range(6):
        v = pick(i)
        print(v in (1, 2), v not in ("a", None), v in (2.5,))
    for i in range(-2, 4):
        print(i in (0, 1), i not in (True, 3))
    b = True
    print(b in (1,), b in (0, 2))
    big = 2 ** 70
    print(big in (0, 1), (big - 2 ** 70) in (0, 1))
    c = Counter(2)
    print(c in (1, 2, 3), Counter.calls)
    try:
        print(Boom() in (1, 2))
    except ValueError as e:
        print("caught", e)
    print(Point(1, 2) in (1, 2), Point(1, 2) not in ("x",))


main()
'''

LEAK = '''import pcc


@pcc.valueclass
class Point:
    x: int
    y: int


@pcc.valueclass
class Segment:
    start: Point
    end: Point


def fresh(i: int) -> str:
    return "key-" + str(i)


def main() -> None:
    items = [Segment(Point(1, 2), Point(3, 4))]
    hits = 0
    for i in range(300000):
        if Segment(Point(i, i), Point(i, i)) in items:
            hits += 1
        if fresh(i) in ("key-1", "key-2"):
            hits += 1
    print(hits)


main()
'''


def _compile(tmp_path, name, source, python_program_compiler, runtime_archive):
    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / name
    python_program_compiler(
        str(src), str(exe), backend="self", libpython_mode="off",
        runtime_archive=str(runtime_archive),
    )
    return src, exe


def test_literal_tuple_membership_matches_cpython(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    src, exe = _compile(
        tmp_path, "tuple_membership", SEMANTICS,
        python_program_compiler, pcc_runtime_archive,
    )
    reference = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=str(REPO)),
    )
    assert reference.returncode == 0, reference.stderr
    assert "True 2\ncaught boom 1\n" in reference.stdout
    for backend in range(5):
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == reference.stdout, backend


def test_membership_needles_and_valuebox_fields_do_not_leak(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    _src, exe = _compile(
        tmp_path, "membership_leak", LEAK,
        python_program_compiler, pcc_runtime_archive,
    )
    for backend in (0, 4):
        run = subprocess.run(
            ["/usr/bin/time", "-l", str(exe)],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert run.returncode == 0, (backend, run.stderr)
        assert run.stdout == "2\n", backend
        max_rss = 0
        for line in run.stderr.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "maximum resident set size" in line:
                max_rss = int(parts[0])
        assert max_rss > 0, run.stderr
        # The leak grew ~600 bytes per iteration (180 MB here); a clean run
        # stays at a few MB.
        assert max_rss < 48 * 1024 * 1024, (backend, max_rss)
