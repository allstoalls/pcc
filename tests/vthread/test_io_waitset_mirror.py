"""Regression tests for the runtime IO-waitset structure.

This is the structure/mirror gate for the CPU-only IO-waitset oracle
(``pcc/vthread/io_waitset_oracle.py``), mirroring the oracle's
``PollWaitSet`` (the level-triggered poll fallback) plus the real Darwin
``kqueue`` readiness backend:

  * ``pcc/runtime/py/py_io_waitset.py`` -- the pcc-Python port, run
    in-process (also valid CPython) on scripted cases + a randomized parity
    sequence (poll fallback only);
  * the production ``freestanding_io_waitset`` members of the runtime
    archive, driven through their C ABI (``src/py_io_waitset.h``) by a small
    harness that diffs the poll fallback against a dataset generated from the
    same oracle and exercises the real ``kqueue`` backend over live pipe fds
    when this platform provides it (``SKIPPED_WITH_REASON`` off Darwin/BSD,
    mirroring the oracle's ``real_kqueue_backend()``).
"""

from __future__ import annotations

import importlib.util
import random
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ``vthread_io_waitset_oracle`` is loaded by tests/vthread/conftest.py.
import vthread_io_waitset_oracle as ORACLE


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "AGENTS.md").is_file():
            return parent
    raise RuntimeError("could not locate repo root (AGENTS.md not found)")


def _load_port():
    name = "pcc_runtime_py_io_waitset_port"
    if name in sys.modules:
        return sys.modules[name]
    path = _repo_root() / "pcc" / "runtime" / "py" / "py_io_waitset.py"
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


def _port_add(ws, fd, interest, deadline):
    ws.add(fd, interest, -1 if deadline is None else deadline, 0)


def test_port_delivers_ready_fd():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 3, ORACLE.POLLIN, None)
    assert ws.count() == 1
    ws.set_ready(3, ORACLE.POLLIN)
    res = ws.wait(0)
    assert [e.fd for e in res.ready] == [3]
    assert res.ready[0].events & ORACLE.POLLIN
    assert ws.count() == 0  # one-shot


def test_port_not_ready_stays_registered():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 5, ORACLE.POLLIN, None)
    res = ws.wait(0)
    assert res.ready == [] and res.timed_out == []
    assert ws.count() == 1


def test_port_error_bits_always_reported():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 9, ORACLE.POLLIN, None)  # only asked for readable
    ws.set_ready(9, ORACLE.POLLHUP)        # hangup arrives
    res = ws.wait(0)
    assert [e.fd for e in res.ready] == [9]
    assert res.ready[0].events & ORACLE.POLLHUP


def test_port_interest_filters_unrequested_bits():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 2, ORACLE.POLLIN, None)
    ws.set_ready(2, ORACLE.POLLOUT)  # writable, but only want readable
    res = ws.wait(0)
    assert res.ready == []
    assert ws.count() == 1


def test_port_timeout_inclusive_and_ready_wins():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 4, ORACLE.POLLIN, 100)
    assert ws.wait(50).timed_out == []
    assert ws.wait(100).timed_out == [4]  # deadline inclusive
    _port_add(ws, 5, ORACLE.POLLIN, 100)
    ws.set_ready(5, ORACLE.POLLIN)
    res = ws.wait(100)  # ready and expired -> ready wins
    assert [e.fd for e in res.ready] == [5]
    assert res.timed_out == []


def test_port_infinite_deadline_never_times_out():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 1, ORACLE.POLLIN, None)
    for now in (0, 10**9):
        assert ws.wait(now).timed_out == []
    assert ws.count() == 1


def test_port_remove():
    ws = PORT.PollIoWaitSet()
    _port_add(ws, 7, ORACLE.POLLIN, None)
    assert ws.remove(7) == 1
    assert ws.count() == 0
    assert ws.remove(7) == 0
    _port_add(ws, 8, ORACLE.POLLIN, None)
    ws.set_ready(8, ORACLE.POLLIN)
    ws.remove(8)
    assert ws.wait(0).ready == []


def _drive_oracle(events):
    ws = ORACLE.PollWaitSet()
    trace = []
    for op in events:
        kind = op[0]
        if kind == "add":
            _, fd, interest, deadline = op
            ws.add(fd, interest, deadline=deadline, edge=False)
        elif kind == "ready":
            _, fd, ev = op
            ws.set_ready(fd, ev)
        elif kind == "remove":
            _, fd = op
            ws.remove(fd)
        elif kind == "wait":
            _, now = op
            res = ws.wait(now)
            trace.append(
                (
                    sorted((e.fd, e.events) for e in res.ready),
                    sorted(res.timed_out),
                )
            )
    return trace


def _drive_port(events):
    ws = PORT.PollIoWaitSet()
    trace = []
    for op in events:
        kind = op[0]
        if kind == "add":
            _, fd, interest, deadline = op
            _port_add(ws, fd, interest, deadline)
        elif kind == "ready":
            _, fd, ev = op
            ws.set_ready(fd, ev)
        elif kind == "remove":
            _, fd = op
            ws.remove(fd)
        elif kind == "wait":
            _, now = op
            res = ws.wait(now)
            trace.append(
                (
                    sorted((e.fd, e.events) for e in res.ready),
                    sorted(res.timed_out),
                )
            )
    return trace


def _scripted():
    return [
        ("add", 1, ORACLE.POLLIN, None),
        ("add", 2, ORACLE.POLLIN, 30),
        ("add", 3, ORACLE.POLLOUT, None),
        ("wait", 0),
        ("ready", 1, ORACLE.POLLIN),
        ("wait", 5),
        ("ready", 3, ORACLE.POLLOUT),
        ("wait", 10),
        ("wait", 30),
        ("add", 4, ORACLE.POLLIN, None),
        ("ready", 4, ORACLE.POLLHUP),
        ("remove", 4),
        ("wait", 40),
    ]


def test_port_matches_oracle_scripted():
    script = _scripted()
    assert _drive_port(script) == _drive_oracle(script)


def test_port_matches_oracle_randomized():
    rng = random.Random(2026)
    for _ in range(50):
        script = []
        fds = list(range(1, 9))
        for fd in fds:
            deadline = rng.choice([None, rng.randint(1, 20)])
            script.append(("add", fd, ORACLE.POLLIN, deadline))
        now = 0
        for _step in range(20):
            r = rng.random()
            if r < 0.5:
                script.append(("ready", rng.choice(fds), ORACLE.POLLIN))
            elif r < 0.65:
                script.append(("remove", rng.choice(fds)))
            else:
                now += rng.randint(1, 6)
                script.append(("wait", now))
        assert _drive_port(script) == _drive_oracle(script), (
            f"divergence on script: {script}"
        )


def test_port_real_kqueue_skipped_with_reason():
    assert PORT.kqueue_available() == 0
    skip = PORT.real_kqueue_skip()
    assert skip[0] == "io_waitset.real_kqueue"
    assert "kqueue" in skip[1].lower()


# ======================================================================
# C runtime structure vs oracle (compiled standalone with cc)
# ======================================================================


def _c_compiler():
    for cc in ("cc", "clang", "gcc"):
        if shutil.which(cc):
            return cc
    return None


def _oracle_poll_dataset(script):
    """Replay a scripted op sequence through the oracle PollWaitSet, capturing
    per-wait (ready fds+events, timed-out fds) so the C poll fallback can be
    diffed against it."""
    return _drive_oracle(script)


def test_c_abi_io_waitset_poll_matches_oracle_dataset(tmp_path, pcc_runtime_archive):
    cc = _c_compiler()
    if cc is None:
        pytest.fail("no C compiler available")

    root = _repo_root()
    src_dir = root / "pcc" / "runtime" / "src"
    ws_h = src_dir / "py_io_waitset.h"
    assert ws_h.is_file()

    # Deterministic scripted + randomized sequence, encoded as an opcode stream
    # the C harness replays step-by-step against the same oracle output.
    rng = random.Random(7)
    script = _scripted()
    fds = list(range(1, 7))
    for fd in fds:
        deadline = rng.choice([-1, rng.randint(1, 25)])
        script.append(("add", fd, ORACLE.POLLIN, None if deadline < 0 else deadline))
    now = 0
    for _step in range(30):
        r = rng.random()
        if r < 0.5:
            script.append(("ready", rng.choice(fds), ORACLE.POLLIN))
        elif r < 0.6:
            script.append(("ready", rng.choice(fds), ORACLE.POLLHUP))
        elif r < 0.72:
            script.append(("remove", rng.choice(fds)))
        else:
            now += rng.randint(1, 7)
            script.append(("wait", now))

    trace = _oracle_poll_dataset(script)

    # Encode the opcode stream. op codes: 0=add 1=ready 2=remove 3=wait.
    OP_ADD, OP_READY, OP_REMOVE, OP_WAIT = 0, 1, 2, 3
    ops = []      # (opcode, a, b, c) flattened
    wait_ready = []   # per-wait: sorted list of (fd, events)
    wait_timeout = [] # per-wait: sorted list of fd
    wi = 0
    for op in script:
        kind = op[0]
        if kind == "add":
            _, fd, interest, deadline = op
            ops.append((OP_ADD, fd, interest, -1 if deadline is None else deadline))
        elif kind == "ready":
            _, fd, ev = op
            ops.append((OP_READY, fd, ev, 0))
        elif kind == "remove":
            _, fd = op
            ops.append((OP_REMOVE, fd, 0, 0))
        elif kind == "wait":
            _, now2 = op
            ops.append((OP_WAIT, now2, 0, 0))
            ready, timed = trace[wi]
            wait_ready.append(ready)
            wait_timeout.append(timed)
            wi += 1

    def c_arr(name, values):
        body = ",".join(str(v) for v in values) if values else "0"
        n = len(values) if values else 1
        return f"static const int64_t {name}[{n}]={{{body}}};"

    op_code = [o[0] for o in ops]
    op_a = [o[1] for o in ops]
    op_b = [o[2] for o in ops]
    op_c = [o[3] for o in ops]

    # Flatten expected wait results.
    ready_fd = []
    ready_ev = []
    ready_len = []
    for ready in wait_ready:
        ready_len.append(len(ready))
        for fd, ev in ready:
            ready_fd.append(fd)
            ready_ev.append(ev)
    timeout_fd = []
    timeout_len = []
    for timed in wait_timeout:
        timeout_len.append(len(timed))
        for fd in timed:
            timeout_fd.append(fd)

    harness = tmp_path / "io_waitset_diff.c"
    harness.write_text(
        "#include \"py_io_waitset.h\"\n"
        "#include <stdio.h>\n"
        "#include <stdlib.h>\n"
        + c_arr("OP_CODE", op_code) + "\n"
        + c_arr("OP_A", op_a) + "\n"
        + c_arr("OP_B", op_b) + "\n"
        + c_arr("OP_C", op_c) + "\n"
        + c_arr("READY_FD", ready_fd) + "\n"
        + c_arr("READY_EV", ready_ev) + "\n"
        + c_arr("READY_LEN", ready_len) + "\n"
        + c_arr("TIMEOUT_FD", timeout_fd) + "\n"
        + c_arr("TIMEOUT_LEN", timeout_len) + "\n"
        + f"#define NOP {len(ops)}\n"
        + f"#define NWAIT {len(ready_len)}\n"
        + textwrap.dedent(
            r"""
            static int cmp_i64(const void *a, const void *b){
                int64_t x=*(const int64_t*)a, y=*(const int64_t*)b;
                return x<y?-1:(x>y?1:0);
            }
            /* sort a small array of (fd,ev) pairs by fd for order-independent
             * comparison with the oracle's sorted output */
            static void sort_pairs(int64_t *fd, int64_t *ev, int64_t n){
                for(int64_t i=1;i<n;i++){
                    int64_t f=fd[i], e=ev[i], j=i-1;
                    while(j>=0 && fd[j]>f){ fd[j+1]=fd[j]; ev[j+1]=ev[j]; j--; }
                    fd[j+1]=f; ev[j+1]=e;
                }
            }
            int main(void){
                PccIoWaitSet ws;
                if(pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_POLL)){
                    fprintf(stderr,"init\n"); return 1;
                }
                int64_t roff=0, toff=0;
                int wi=0;
                for(int i=0;i<NOP;i++){
                    int64_t code=OP_CODE[i];
                    if(code==0){ /* add */
                        pcc_io_waitset_add(&ws, OP_A[i], OP_B[i], OP_C[i], 0);
                    } else if(code==1){ /* ready */
                        pcc_io_waitset_set_ready(&ws, OP_A[i], OP_B[i]);
                    } else if(code==2){ /* remove */
                        pcc_io_waitset_remove(&ws, OP_A[i]);
                    } else { /* wait */
                        PccIoWaitResult r;
                        if(pcc_io_waitset_wait(&ws, OP_A[i], &r)){
                            fprintf(stderr,"wait-fail %d\n", i); return 2;
                        }
                        int64_t rl=READY_LEN[wi];
                        if(r.ready_len != rl){
                            fprintf(stderr,"wait %d ready_len %lld != %lld\n",
                                wi,(long long)r.ready_len,(long long)rl); return 3;
                        }
                        /* copy + sort our ready by fd, compare to oracle */
                        int64_t gfd[64], gev[64];
                        for(int64_t k=0;k<r.ready_len;k++){ gfd[k]=r.ready[k].fd; gev[k]=r.ready[k].events; }
                        sort_pairs(gfd, gev, r.ready_len);
                        for(int64_t k=0;k<rl;k++){
                            if(gfd[k]!=READY_FD[roff+k] || gev[k]!=READY_EV[roff+k]){
                                fprintf(stderr,"wait %d ready[%lld] got (%lld,%lld) exp (%lld,%lld)\n",
                                    wi,(long long)k,(long long)gfd[k],(long long)gev[k],
                                    (long long)READY_FD[roff+k],(long long)READY_EV[roff+k]);
                                return 4;
                            }
                        }
                        roff += rl;
                        int64_t tl=TIMEOUT_LEN[wi];
                        if(r.timeout_len != tl){
                            fprintf(stderr,"wait %d timeout_len %lld != %lld\n",
                                wi,(long long)r.timeout_len,(long long)tl); return 5;
                        }
                        int64_t gt[64];
                        for(int64_t k=0;k<r.timeout_len;k++) gt[k]=r.timed_out[k];
                        qsort(gt, (size_t)r.timeout_len, sizeof(int64_t), cmp_i64);
                        for(int64_t k=0;k<tl;k++){
                            if(gt[k]!=TIMEOUT_FD[toff+k]){
                                fprintf(stderr,"wait %d timeout[%lld] got %lld exp %lld\n",
                                    wi,(long long)k,(long long)gt[k],(long long)TIMEOUT_FD[toff+k]);
                                return 6;
                            }
                        }
                        toff += tl;
                        wi++;
                    }
                }
                pcc_io_waitset_dispose(&ws);
                printf("dataset-ok\n");
                return 0;
            }
            """
        )
    )

    exe = tmp_path / "io_waitset_diff.out"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", f"-I{src_dir}",
         str(harness), str(pcc_runtime_archive), "-lm", "-o", str(exe)],
        capture_output=True, text=True, timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "dataset-ok"


def test_c_abi_io_waitset_semantics_and_capability(tmp_path, pcc_runtime_archive):
    """Scripted C semantics for the poll fallback + capability/skip probe, and
    (on kqueue platforms) the real kevent(2) backend over live pipe fds."""
    cc = _c_compiler()
    if cc is None:
        pytest.fail("no C compiler available")

    root = _repo_root()
    src_dir = root / "pcc" / "runtime" / "src"

    harness = tmp_path / "io_waitset_semantics.c"
    harness.write_text(textwrap.dedent(r"""
        #include "py_io_waitset.h"
        #include <stdio.h>
        #include <string.h>
        #include <fcntl.h>
        #include <time.h>
        #include <unistd.h>
        static int fail(const char *m){ fprintf(stderr,"FAIL: %s\n", m); return 1; }
        int main(void){
            PccIoWaitSet ws;
            PccIoWaitResult r;

            /* poll fallback: ready delivery + one-shot removal */
            if(pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_POLL)) return fail("init");
            pcc_io_waitset_add(&ws, 3, PCC_IO_POLLIN, -1, 0);
            if(pcc_io_waitset_count(&ws)!=1) return fail("count");
            if(pcc_io_waitset_wait(&ws,0,&r)) return fail("wait0");
            if(r.ready_len!=0 || r.timeout_len!=0) return fail("empty");
            pcc_io_waitset_set_ready(&ws, 3, PCC_IO_POLLIN);
            if(pcc_io_waitset_wait(&ws,0,&r)) return fail("wait1");
            if(r.ready_len!=1 || r.ready[0].fd!=3 || !(r.ready[0].events & PCC_IO_POLLIN)) return fail("deliver");
            if(pcc_io_waitset_count(&ws)!=0) return fail("oneshot");

            /* error bit always reported; interest filters unrequested */
            pcc_io_waitset_add(&ws, 9, PCC_IO_POLLIN, -1, 0);
            pcc_io_waitset_set_ready(&ws, 9, PCC_IO_POLLHUP);
            pcc_io_waitset_wait(&ws,0,&r);
            if(r.ready_len!=1 || !(r.ready[0].events & PCC_IO_POLLHUP)) return fail("hup");
            pcc_io_waitset_add(&ws, 2, PCC_IO_POLLIN, -1, 0);
            pcc_io_waitset_set_ready(&ws, 2, PCC_IO_POLLOUT);
            pcc_io_waitset_wait(&ws,0,&r);
            if(r.ready_len!=0 || pcc_io_waitset_count(&ws)!=1) return fail("filter");
            pcc_io_waitset_remove(&ws, 2);

            /* timeout inclusive; ready wins over timeout at same tick */
            pcc_io_waitset_add(&ws, 4, PCC_IO_POLLIN, 100, 0);
            pcc_io_waitset_wait(&ws,50,&r);
            if(r.timeout_len!=0) return fail("early");
            pcc_io_waitset_wait(&ws,100,&r);
            if(r.timeout_len!=1 || r.timed_out[0]!=4) return fail("timeout");
            pcc_io_waitset_add(&ws, 5, PCC_IO_POLLIN, 100, 0);
            pcc_io_waitset_set_ready(&ws, 5, PCC_IO_POLLIN);
            pcc_io_waitset_wait(&ws,100,&r);
            if(r.ready_len!=1 || r.timeout_len!=0) return fail("readywins");

            /* remove semantics */
            pcc_io_waitset_add(&ws, 7, PCC_IO_POLLIN, -1, 0);
            if(pcc_io_waitset_remove(&ws,7)!=1) return fail("rm");
            if(pcc_io_waitset_remove(&ws,7)!=0) return fail("rm2");
            pcc_io_waitset_dispose(&ws);

            /* capability + skip marker consistency */
            int avail = pcc_io_waitset_kqueue_available();
            PccIoWaitSetSkip skip; memset(&skip,0,sizeof(skip));
            int skipped = pcc_io_waitset_real_kqueue_skip(&skip);
            if(avail){
                if(skipped!=0) return fail("avail-but-skipped");
            } else {
                if(skipped!=1) return fail("unavail-not-skipped");
                if(skip.path==NULL || strcmp(skip.path,"io_waitset.real_kqueue")!=0) return fail("skip-path");
                if(skip.reason==NULL) return fail("skip-reason");
            }

            /* real kqueue backend over live pipe fds (Darwin/BSD only) */
            if(avail){
                PccIoWaitSet kq;
                if(pcc_io_waitset_init(&kq, PCC_IO_WAITSET_BACKEND_KQUEUE)) return fail("kq-init");
                int fds[2];
                if(pipe(fds)!=0) return fail("pipe");
                int rfd=fds[0], wfd=fds[1];
                fcntl(rfd, F_SETFL, O_NONBLOCK);
                pcc_io_waitset_add(&kq, rfd, PCC_IO_POLLIN, -1, 0);
                pcc_io_waitset_wait(&kq,0,&r);
                if(r.ready_len!=0) return fail("kq-empty");
                if(pcc_io_waitset_count(&kq)!=1) return fail("kq-retain");
                if(write(wfd,"x",1)!=1) return fail("kq-write");
                pcc_io_waitset_wait(&kq,0,&r);
                if(r.ready_len!=1 || r.ready[0].fd!=rfd || !(r.ready[0].events & PCC_IO_POLLIN)) return fail("kq-deliver");
                if(pcc_io_waitset_count(&kq)!=0) return fail("kq-oneshot");
                /* EOF -> POLLHUP */
                char b; ssize_t rd=read(rfd,&b,1); (void)rd;
                pcc_io_waitset_add(&kq, rfd, PCC_IO_POLLIN, -1, 0);
                close(wfd);
                pcc_io_waitset_wait(&kq,0,&r);
                if(r.ready_len!=1 || !(r.ready[0].events & (PCC_IO_POLLIN|PCC_IO_POLLHUP))) return fail("kq-eof");
                close(rfd);
                /* kqueue timeout path: the kernel backends re-read the
                 * monotonic clock after waiting, so deadlines are real
                 * CLOCK_MONOTONIC milliseconds, not the poll tick. */
                int fds2[2]; if(pipe(fds2)!=0) return fail("pipe2");
                struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
                int64_t t0 = (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
                pcc_io_waitset_add(&kq, fds2[0], PCC_IO_POLLIN, t0 + 100, 0);
                pcc_io_waitset_wait(&kq,t0,&r);
                if(r.timeout_len!=0) return fail("kq-early");
                usleep(120000);
                pcc_io_waitset_wait(&kq,t0 + 120,&r);
                if(r.timeout_len!=1 || r.timed_out[0]!=fds2[0]) return fail("kq-timeout");
                close(fds2[0]); close(fds2[1]);
                pcc_io_waitset_dispose(&kq);
                printf("kqueue-live-ok\n");
            } else {
                printf("kqueue-skipped\n");
            }
            printf("semantics-ok\n");
            return 0;
        }
    """).lstrip())

    exe = tmp_path / "io_waitset_semantics.out"
    build = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", f"-I{src_dir}",
         str(harness), str(pcc_runtime_archive), "-lm", "-o", str(exe)],
        capture_output=True, text=True, timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip().endswith("semantics-ok")


def test_io_waitset_port_is_in_the_production_archive():
    """The production archive carries the waitset the C ABI tests link."""
    makefile = (_repo_root() / "pcc" / "runtime" / "Makefile").read_text(
        encoding="utf-8"
    )
    assert "freestanding_io_waitset" in makefile.split("PY_MODULES =", 1)[1].splitlines()[0]
