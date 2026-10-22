"""P0 threaded runtime and concurrent-collector acceptance tests.

docs/plans/p0-threaded-concurrent-gc-runtime.md, packages 2-6: every native
runtime is threaded, stack frames of every mutator are rooted by the
precise stack walk (Darwin) or by registration, blocked waits run exited,
GC0 reference counts are inline atomics, and GC1/GC2 mark on a collector
thread. Each program is compiled once and run under all five collectors;
its output must equal CPython's.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

BACKENDS = ("0", "1", "2", "3", "4")

THREADS_ALLOCATE_AND_COLLECT = '''import gc
import threading


def work(index, out):
    total = 0
    for round_index in range(40):
        table = {}
        for i in range(2000):
            table[i] = str(i) * 2
        kept = [table[key] for key in table if key % 7 == 0]
        total += len(kept) + len(table)
        if round_index % 10 == 0:
            gc.collect()
    out[index] = total


def main():
    out = [0] * 4
    threads = [threading.Thread(target=work, args=(i, out)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(out, sum(out))


main()
'''

BLOCKED_WAITERS_DURING_COLLECTION = '''import gc
import threading


def waiter(cond, state, out, index):
    with cond:
        while not state[0]:
            cond.wait()
    out[index] = sum(len(str(i)) for i in range(1000))


def main():
    cond = threading.Condition()
    state = [False]
    out = [0] * 3
    threads = [threading.Thread(target=waiter, args=(cond, state, out, i)) for i in range(3)]
    for thread in threads:
        thread.start()
    junk = []
    for i in range(20000):
        junk.append({"k": [i, str(i)]})
        if i % 5000 == 0:
            gc.collect()
    with cond:
        state[0] = True
        cond.notify_all()
    for thread in threads:
        thread.join()
    print(len(junk), out)


main()
'''

DEEP_STACKS_AND_GENERATORS = '''import gc


class Node:
    def __init__(self, left, right, value):
        self.left = left
        self.right = right
        self.value = value


def build(depth):
    if depth == 0:
        return Node(None, None, 1)
    node = Node(build(depth - 1), build(depth - 1), depth)
    if depth == 6:
        gc.collect()
    return node


def total(node):
    if node is None:
        return 0
    return node.value + total(node.left) + total(node.right)


def chunks(count):
    for i in range(count):
        yield [i] * 3


def main():
    tree = build(10)
    print(total(tree))
    acc = 0
    for chunk in chunks(2000):
        acc += sum(chunk)
        if acc % 997 == 0:
            gc.collect()
    print(acc)


main()
'''

CROSS_THREAD_FINALIZER = '''import gc
import threading

events = []


class Tracked:
    def __init__(self, name):
        self.name = name

    def __del__(self):
        events.append(self.name)


def drop(holder):
    holder.pop()


def main():
    holder = [Tracked("a"), Tracked("b")]
    worker = threading.Thread(target=drop, args=(holder,))
    worker.start()
    worker.join()
    print(events)
    holder.pop()
    gc.collect()
    print(events)


main()
'''

PROGRAMS = {
    "threads_allocate_and_collect": THREADS_ALLOCATE_AND_COLLECT,
    "blocked_waiters_during_collection": BLOCKED_WAITERS_DURING_COLLECTION,
    "deep_stacks_and_generators": DEEP_STACKS_AND_GENERATORS,
    "cross_thread_finalizer": CROSS_THREAD_FINALIZER,
}


def _environment(backend=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    env.pop("PCC_GC_STACKMAP_ROOTS", None)
    if backend is not None:
        env["PCC_GC_BACKEND"] = backend
    return env


def _compile(tmp_path: Path, name: str, source: str) -> Path:
    src = tmp_path / (name + ".py")
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / name
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    return exe


def _cpython(source: str) -> str:
    run = subprocess.run([sys.executable, "-c", source], text=True, capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return run.stdout


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_program_matches_cpython_under_every_collector(tmp_path, name):
    source = PROGRAMS[name]
    expected = _cpython(source)
    exe = _compile(tmp_path, name, source)
    for backend in BACKENDS:
        run = subprocess.run(
            [str(exe)], text=True, capture_output=True, timeout=300, env=_environment(backend),
        )
        assert run.returncode == 0, (backend, run.stderr[-2000:])
        assert run.stdout == expected, (backend, run.stdout, expected)


def test_stack_frames_use_the_mapped_frame_entries_where_tables_publish(tmp_path, monkeypatch):
    from pcc.backend.precise_stackmap import precise_stackmap_target_publishes_tables
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.delenv("PCC_PRECISE_STACKMAP_TABLES", raising=False)
    src = tmp_path / "frames.py"
    src.write_text(
        "def pair(n):\n"
        "    items = [n, n + 1]\n"
        "    return items\n"
        "\n"
        "def numbers(n):\n"
        "    for i in range(n):\n"
        "        yield [i]\n",
        encoding="utf-8",
    )
    out = tmp_path / "frames.ll"
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    text = out.read_text(encoding="utf-8")
    start = text.index("@user_frames_pair(")
    body = text[start:text.index("\n}\n", start)]
    if precise_stackmap_target_publishes_tables(None):
        assert "_mapped(" in body, body
        assert "@pcc_gc_frame_enter_lifo(" not in body
    else:
        assert "_mapped(" not in body


def test_main_publishes_the_table_before_any_frame_enters(tmp_path, monkeypatch):
    from pcc.backend.precise_stackmap import precise_stackmap_target_publishes_tables
    from pcc.frontends.python.pipeline import compile_python

    if not precise_stackmap_target_publishes_tables(None):
        pytest.fail("this host links no published stack-map table; run on a Darwin host")
    monkeypatch.delenv("PCC_PRECISE_STACKMAP_TABLES", raising=False)
    src = tmp_path / "prog.py"
    src.write_text("value = [1, 2]\nprint(len(value))\n", encoding="utf-8")
    out = tmp_path / "prog.ll"
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    text = out.read_text(encoding="utf-8")
    start = text.index("define i32 @main(")
    body = text[start:text.index("\n}\n", start)]
    publish = body.index("@pcc_gc_register_precise_stackmap_table(")
    for marker in ("@pcc_gc_frame_enter", "@pcc_thread_safepoint("):
        if marker in body:
            assert publish < body.index(marker), body[:4000]


SCALING = '''import sys
import threading
import time


def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def worker(count, out, index):
    s = 0
    for _ in range(count):
        s += fib(24)
    out[index] = s


def run(threads_count):
    jobs = 32
    out = [0] * threads_count
    threads = [threading.Thread(target=worker, args=(jobs // threads_count, out, i))
               for i in range(threads_count)]
    start = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return time.perf_counter() - start, sum(out)


def main():
    one, total_one = run(1)
    four, total_four = run(4)
    print(total_one == total_four, round(one / four, 2))


main()
'''


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="P0 packages 4-6: CPU-bound threads must scale on 4 cores")
def test_cpu_bound_threads_scale_on_four_cores(tmp_path):
    exe = _compile(tmp_path, "scaling", SCALING)
    run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=600, env=_environment("0"))
    assert run.returncode == 0, run.stderr
    same, speedup = run.stdout.split()
    assert same == "True"
    assert float(speedup) >= 2.5, run.stdout
