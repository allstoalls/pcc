"""Regression tests for the runtime timer min-heap structure.

This is the runtime-structure mirror slice of the CPU-only timer oracle
(``pcc/vthread/timer_oracle.py``). Two views of the oracle's
``MinHeapTimerQueue`` are diffed against it:

  * ``pcc/runtime/py/py_timer_heap.py`` -- the pcc-Python port, run
    in-process (also valid CPython) on scripted cases + a randomized parity
    sequence;
  * the production ``freestanding_timer_heap`` members of the runtime
    archive, driven through their C ABI (``src/py_timer_heap.h``) by a small
    harness that diffs them against a dataset generated from the same oracle.
"""

from __future__ import annotations

import importlib.util
import random
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from pcc.diagnostics.dependency_verdict import probe_first_executable_dependency

import pytest

# ``vthread_timer_oracle`` is loaded by tests/vthread/conftest.py.
import vthread_timer_oracle as ORACLE


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "AGENTS.md").is_file():
            return parent
    raise RuntimeError("could not locate repo root (AGENTS.md not found)")


def _load_port():
    name = "pcc_runtime_py_timer_heap_port"
    if name in sys.modules:
        return sys.modules[name]
    path = _repo_root() / "pcc" / "runtime" / "py" / "py_timer_heap.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PORT = _load_port()


# ======================================================================
# pcc-Python port vs oracle (in-process; no build required)
# ======================================================================


def test_port_expire_order_nondecreasing_deadline():
    q = PORT.MinHeapTimerQueue()
    q.insert(50, 1)
    q.insert(10, 2)
    q.insert(30, 3)
    q.insert(20, 4)
    assert q.pop_expired(100) == [2, 4, 3, 1]
    assert q.size() == 0


def test_port_fifo_among_equal_deadlines():
    q = PORT.MinHeapTimerQueue()
    for tid in (5, 6, 7, 8):
        q.insert(42, tid)
    assert q.pop_expired(42) == [5, 6, 7, 8]


def test_port_expire_boundary_inclusive():
    q = PORT.MinHeapTimerQueue()
    q.insert(100, 1)
    assert q.pop_expired(99) == []
    assert q.pop_expired(100) == [1]


def test_port_partial_leaves_future_registered():
    q = PORT.MinHeapTimerQueue()
    q.insert(10, 1)
    q.insert(20, 2)
    q.insert(30, 3)
    assert q.pop_expired(20) == [1, 2]
    assert q.size() == 1
    assert q.is_registered(3)
    assert q.pop_expired(30) == [3]
    assert q.size() == 0


def test_port_cancel_removes_from_expiry():
    q = PORT.MinHeapTimerQueue()
    q.insert(10, 1)
    q.insert(20, 2)
    q.insert(30, 3)
    assert q.cancel(2) is True
    assert q.size() == 2
    assert q.pop_expired(100) == [1, 3]


def test_port_cancel_unknown_id_is_false():
    q = PORT.MinHeapTimerQueue()
    q.insert(10, 1)
    assert q.cancel(999) is False
    assert q.cancel(1) is True
    assert q.cancel(1) is False


def test_port_done_skip_does_not_return_cancelled_entry():
    q = PORT.MinHeapTimerQueue()
    q.insert(5, 1)
    q.insert(5, 2)
    q.cancel(1)
    assert q.pop_expired(5) == [2]


def test_port_reschedule_supersedes_old_deadline():
    q = PORT.MinHeapTimerQueue()
    q.insert(10, 1)
    q.insert(90, 1)
    assert q.size() == 1
    assert q.pop_expired(10) == []
    assert q.pop_expired(90) == [1]


def test_port_cancel_then_reinsert_same_id():
    q = PORT.MinHeapTimerQueue()
    q.insert(10, 1)
    assert q.cancel(1) is True
    q.insert(50, 1)
    assert q.pop_expired(10) == []
    assert q.pop_expired(50) == [1]


def test_port_root_retained_until_expired_or_cancelled():
    q = PORT.MinHeapTimerQueue()
    q.insert(100, 1)
    for now in (0, 10, 50, 99):
        assert q.pop_expired(now) == []
        assert q.is_registered(1)
        assert q.size() == 1
    assert q.pop_expired(100) == [1]
    assert not q.is_registered(1)


def test_port_peek_skips_stale_and_reports_soonest():
    q = PORT.MinHeapTimerQueue()
    q.insert(30, 3)
    q.insert(10, 1)
    q.insert(20, 2)
    out = [0]
    assert q.peek(out) is True
    assert out[0] == 10
    q.cancel(1)
    out = [0]
    assert q.peek(out) is True
    assert out[0] == 20
    q.cancel(2)
    q.cancel(3)
    out = [0]
    assert q.peek(out) is False


def test_port_matches_oracle_random_sequence():
    rng = random.Random(99)
    n = 2000
    heap = ORACLE.MinHeapTimerQueue()
    port = PORT.MinHeapTimerQueue()
    for tid in range(n):
        d = rng.randint(0, 5000)
        heap.insert(d, tid)
        port.insert(d, tid)
    for tid in rng.sample(range(n), 300):
        heap.cancel(tid)
        port.cancel(tid)
    for now in (1000, 2500, 4000, 5000):
        a = heap.expire_due(now)
        b = port.pop_expired(now)
        assert a == b, f"mismatch at now={now}: oracle={a} port={b}"
    assert heap.pending_count() == port.size() == 0


def test_port_large_n_expiry_is_sorted_by_deadline():
    rng = random.Random(1234)
    n = 5000
    port = PORT.MinHeapTimerQueue()
    deadlines = {}
    for tid in range(n):
        d = rng.randint(0, 10_000)
        deadlines[tid] = d
        port.insert(d, tid)
    assert port.size() == n
    got = port.pop_expired(10_000)
    got_deadlines = [deadlines[t] for t in got]
    assert got_deadlines == sorted(got_deadlines)
    assert len(got) == n
    assert port.size() == 0


# ======================================================================
# C runtime structure vs oracle (compiled standalone with cc)
# ======================================================================


# Structured system-cc prerequisite: UNAVAILABLE emits an explicit verdict
# with no runtime claim; when a compiler is present the C parity assertions
# below stay hard (AUD-P2-DEPENDENCY-SYSTEM-CC-VERDICT).
CC_VERDICT = probe_first_executable_dependency(("cc", "clang", "gcc"))


def _oracle_dataset(n: int, ncancel: int, steps):
    """Build a deterministic dataset + its oracle expiry sequence."""
    rng = random.Random(99)
    heap = ORACLE.MinHeapTimerQueue()
    inserts = []
    for tid in range(n):
        d = rng.randint(0, 5000)
        inserts.append((d, tid))
        heap.insert(d, tid)
    cancels = sorted(rng.sample(range(n), ncancel))
    for tid in cancels:
        heap.cancel(tid)
    seq = [(now, heap.expire_due(now)) for now in steps]
    assert heap.pending_count() == 0
    return inserts, cancels, seq


def test_c_abi_timer_heap_matches_oracle_dataset(tmp_path, pcc_runtime_archive):
    if not CC_VERDICT.available:
        pytest.fail(CC_VERDICT.skip_reason())
    cc = CC_VERDICT.resolved_path

    root = _repo_root()
    src_dir = root / "pcc" / "runtime" / "src"
    heap_h = src_dir / "py_timer_heap.h"
    assert heap_h.is_file()

    n, ncancel = 200, 30
    steps = (1000, 2500, 4000, 5000)
    inserts, cancels, seq = _oracle_dataset(n, ncancel, steps)

    def c_arr(name, values):
        body = ",".join(str(v) for v in values)
        return f"static const int64_t {name}[{len(values)}]={{{body}}};"

    flat = []
    step_now = []
    step_len = []
    for now, out in seq:
        step_now.append(now)
        step_len.append(len(out))
        flat.extend(out)

    harness = tmp_path / "timer_heap_diff.c"
    harness.write_text(
        "#include \"py_timer_heap.h\"\n"
        "#include <stdio.h>\n"
        + c_arr("INS_D", [d for d, _ in inserts]) + "\n"
        + c_arr("INS_T", [t for _, t in inserts]) + "\n"
        + c_arr("CANCELS", cancels) + "\n"
        + c_arr("STEP_NOW", step_now) + "\n"
        + c_arr("STEP_LEN", step_len) + "\n"
        + c_arr("EXPECT", flat) + "\n"
        + f"#define N {n}\n"
        + f"#define NCANCEL {len(cancels)}\n"
        + f"#define NSTEP {len(steps)}\n"
        + textwrap.dedent(
            r"""
            int main(void){
                PccTimerHeap h;
                if(pcc_timer_heap_init(&h)){ fprintf(stderr,"init\n"); return 1; }
                for(int i=0;i<N;i++) pcc_timer_heap_insert(&h, INS_D[i], INS_T[i]);
                if(pcc_timer_heap_size(&h)!=N){ fprintf(stderr,"size-insert %lld\n",(long long)pcc_timer_heap_size(&h)); return 2; }
                for(int i=0;i<NCANCEL;i++) if(pcc_timer_heap_cancel(&h,CANCELS[i])!=1){ fprintf(stderr,"cancel %lld\n",(long long)CANCELS[i]); return 3; }
                if(pcc_timer_heap_size(&h)!=N-NCANCEL){ fprintf(stderr,"size-cancel %lld\n",(long long)pcc_timer_heap_size(&h)); return 4; }
                int64_t buf[N];
                int64_t off=0;
                for(int s=0;s<NSTEP;s++){
                    int64_t got = pcc_timer_heap_pop_expired(&h, STEP_NOW[s], buf, N);
                    if(got != STEP_LEN[s]){ fprintf(stderr,"step %d len %lld != %lld\n", s,(long long)got,(long long)STEP_LEN[s]); return 5; }
                    for(int64_t k=0;k<got;k++)
                        if(buf[k]!=EXPECT[off+k]){ fprintf(stderr,"step %d idx %lld got %lld exp %lld\n",s,(long long)k,(long long)buf[k],(long long)EXPECT[off+k]); return 6; }
                    off += got;
                }
                if(pcc_timer_heap_size(&h)!=0){ fprintf(stderr,"final-size %lld\n",(long long)pcc_timer_heap_size(&h)); return 7; }
                pcc_timer_heap_dispose(&h);
                printf("dataset-ok\n");
                return 0;
            }
            """
        )
    )

    exe = tmp_path / "timer_heap_diff.out"
    build = subprocess.run(
        [
            cc,
            "-std=c11",
            "-Wall",
            "-Wextra",
            f"-I{src_dir}",
            str(harness),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "dataset-ok"


def test_c_abi_timer_heap_scripted_semantics(tmp_path, pcc_runtime_archive):
    if not CC_VERDICT.available:
        pytest.fail(CC_VERDICT.skip_reason())
    cc = CC_VERDICT.resolved_path

    root = _repo_root()
    src_dir = root / "pcc" / "runtime" / "src"

    harness = tmp_path / "timer_heap_semantics.c"
    harness.write_text(textwrap.dedent(r"""
        #include "py_timer_heap.h"
        #include <stdio.h>
        static int fail(const char *m){ fprintf(stderr,"FAIL: %s\n", m); return 1; }
        int main(void){
            PccTimerHeap h;
            int64_t buf[8];

            /* boundary inclusive */
            if(pcc_timer_heap_init(&h)) return fail("init");
            pcc_timer_heap_insert(&h,100,1);
            if(pcc_timer_heap_pop_expired(&h,99,buf,8)!=0) return fail("b99");
            if(pcc_timer_heap_pop_expired(&h,100,buf,8)!=1||buf[0]!=1) return fail("b100");
            pcc_timer_heap_dispose(&h);

            /* fifo among equal deadlines */
            pcc_timer_heap_init(&h);
            for(int t=5;t<=8;t++) pcc_timer_heap_insert(&h,42,t);
            if(pcc_timer_heap_pop_expired(&h,42,buf,8)!=4) return fail("fifo-n");
            if(buf[0]!=5||buf[1]!=6||buf[2]!=7||buf[3]!=8) return fail("fifo-order");
            pcc_timer_heap_dispose(&h);

            /* cancel skips at root, unknown cancel is 0 */
            pcc_timer_heap_init(&h);
            pcc_timer_heap_insert(&h,5,1);
            pcc_timer_heap_insert(&h,5,2);
            if(pcc_timer_heap_cancel(&h,1)!=1) return fail("cancel-live");
            if(pcc_timer_heap_cancel(&h,999)!=0) return fail("cancel-unknown");
            if(pcc_timer_heap_pop_expired(&h,5,buf,8)!=1||buf[0]!=2) return fail("doneskip");
            pcc_timer_heap_dispose(&h);

            /* reschedule supersedes old deadline */
            pcc_timer_heap_init(&h);
            pcc_timer_heap_insert(&h,10,1);
            pcc_timer_heap_insert(&h,90,1);
            if(pcc_timer_heap_size(&h)!=1) return fail("resched-size");
            if(pcc_timer_heap_pop_expired(&h,10,buf,8)!=0) return fail("resched-early");
            if(pcc_timer_heap_pop_expired(&h,90,buf,8)!=1||buf[0]!=1) return fail("resched-late");
            pcc_timer_heap_dispose(&h);

            /* partial + root retention */
            pcc_timer_heap_init(&h);
            pcc_timer_heap_insert(&h,10,1);
            pcc_timer_heap_insert(&h,20,2);
            pcc_timer_heap_insert(&h,30,3);
            if(pcc_timer_heap_pop_expired(&h,20,buf,8)!=2) return fail("partial-n");
            if(!pcc_timer_heap_is_registered(&h,3)||pcc_timer_heap_size(&h)!=1) return fail("retain");
            pcc_timer_heap_dispose(&h);

            /* peek reports soonest, skipping stale */
            pcc_timer_heap_init(&h);
            pcc_timer_heap_insert(&h,30,3);
            pcc_timer_heap_insert(&h,10,1);
            pcc_timer_heap_insert(&h,20,2);
            int64_t d=-1;
            if(pcc_timer_heap_peek(&h,&d)!=1||d!=10) return fail("peek1");
            pcc_timer_heap_cancel(&h,1);
            d=-1;
            if(pcc_timer_heap_peek(&h,&d)!=1||d!=20) return fail("peek2");
            pcc_timer_heap_dispose(&h);

            printf("semantics-ok\n");
            return 0;
        }
    """).lstrip())

    exe = tmp_path / "timer_heap_semantics.out"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", f"-I{src_dir}",
         str(harness), str(pcc_runtime_archive), "-lm", "-o", str(exe)],
        capture_output=True, text=True, timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "semantics-ok"


def test_timer_heap_port_is_in_the_production_archive():
    """The production archive carries the timer heap the C ABI tests link."""
    makefile = (_repo_root() / "pcc" / "runtime" / "Makefile").read_text(
        encoding="utf-8"
    )
    assert "freestanding_timer_heap" in makefile.split(
        "FREESTANDING_PY_MODULES =", 1
    )[1].splitlines()[0]
