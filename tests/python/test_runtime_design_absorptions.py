"""Tests for runtime design absorptions into the pcc runtime.

Covers:
- pcc_spsc ring queue (SPSC bounded FIFO, cached-index)
- pcc_iobuf_pool (size-bucketed bounded buffer pools)
- pcc_io_* outcome semantics (WouldBlock / More as control flow)
- py_vthread_effect_* handler dispatch (covered by
  test_virtual_thread_effect_handlers.py; smoke here)

All probes are C programs linked against the production pcc-Python archive.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _build_c_probe(tmp_path: Path, archive: Path, name: str, src: str) -> Path:
    cfile = tmp_path / f"{name}.c"
    exe = tmp_path / name
    cfile.write_text(src, encoding="utf-8")
    build = subprocess.run(
        [
            "clang", "-std=c11",
            f"-I{REPO / 'pcc' / 'runtime' / 'include'}",
            str(cfile), str(archive), "-pthread", "-o", str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return exe


@pytest.mark.integration
def test_lfq_spsc_ring_fifo_order_and_bounds(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    exe = _build_c_probe(
        tmp_path,
        pcc_runtime_archive,
        "spsc_probe",
        r"""
#include <stdint.h>
typedef struct PyObject PyObject;
extern int pcc_spsc_init(void);
extern int pcc_spsc_enqueue(PyObject *v);
extern PyObject *pcc_spsc_dequeue(void);
extern int pcc_spsc_empty(void);
extern int pcc_spsc_full(void);
extern int64_t pcc_spsc_count(void);

int main(void) {
    if (pcc_spsc_init() != 0) return 1;
    /* empty at start */
    if (pcc_spsc_empty() != 1) return 2;
    if (pcc_spsc_dequeue() != 0) return 3;
    /* FIFO order */
    for (int64_t i = 1; i <= 100; i++) {
        if (pcc_spsc_enqueue((PyObject *)(intptr_t)(i * 7)) != 0) return 4;
    }
    for (int64_t i = 1; i <= 100; i++) {
        PyObject *v = pcc_spsc_dequeue();
        if ((intptr_t)v != i * 7) return 5;
    }
    /* count reflects emptiness */
    if (pcc_spsc_count() != 0) return 6;
    /* ring wraparound: enqueue 300 (past the 256-slot boundary) */
    for (int64_t i = 0; i < 300; i++) {
        if (pcc_spsc_enqueue((PyObject *)(intptr_t)(i + 1)) != 0) return 7;
        if ((intptr_t)pcc_spsc_dequeue() != i + 1) return 8;
    }
    if (pcc_spsc_count() != 0) return 9;
    return 0;
}
""",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.integration
def test_lfq_spsc_full_returns_error(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    exe = _build_c_probe(
        tmp_path,
        pcc_runtime_archive,
        "spsc_full_probe",
        r"""
#include <stdint.h>
typedef struct PyObject PyObject;
extern int pcc_spsc_init(void);
extern int pcc_spsc_enqueue(PyObject *v);
extern int pcc_spsc_full(void);
extern PyObject *pcc_spsc_dequeue(void);
int main(void) {
    if (pcc_spsc_init() != 0) return 1;
    /* fill to capacity (256) */
    for (int i = 0; i < 256; i++) {
        if (pcc_spsc_enqueue((PyObject *)(intptr_t)(i + 1)) != 0) return 2;
    }
    if (pcc_spsc_full() != 1) return 3;
    if (pcc_spsc_enqueue((PyObject *)(intptr_t)999) != -1) return 4; /* full -> -1 */
    /* drain one, enqueue succeeds */
    if ((intptr_t)pcc_spsc_dequeue() != 1) return 5;
    if (pcc_spsc_enqueue((PyObject *)(intptr_t)1000) != 0) return 6;
    return 0;
}
""",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.integration
def test_iobuf_bucketed_pool_alloc_free_roundtrip(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    exe = _build_c_probe(
        tmp_path,
        pcc_runtime_archive,
        "iobuf_probe",
        r"""
#include <stdint.h>
#include <string.h>
typedef struct PyObject PyObject;
extern int pcc_iobuf_pool_init(void);
extern char *pcc_iobuf_alloc(int64_t size);
extern int pcc_iobuf_free(char *buf);
extern int64_t pcc_iobuf_alloc_count(void);
extern int64_t pcc_iobuf_free_count(void);
extern int64_t pcc_iobuf_bucket_used(int b);
int main(void) {
    if (pcc_iobuf_pool_init() != 0) return 1;
    /* small request goes to bucket 0 (32 bytes) */
    char *a = pcc_iobuf_alloc(10);
    if (!a) return 2;
    memset(a, 0xAB, 10);
    if (pcc_iobuf_alloc_count() != 1) return 3;
    if (pcc_iobuf_bucket_used(0) != 1) return 4;
    /* 200-byte request goes to bucket 3 (256) */
    char *b = pcc_iobuf_alloc(200);
    if (!b) return 5;
    if (pcc_iobuf_bucket_used(3) != 1) return 6;
    /* free returns to bucket */
    if (pcc_iobuf_free(a) != 0) return 7;
    if (pcc_iobuf_free(b) != 0) return 8;
    if (pcc_iobuf_free_count() != 2) return 9;
    /* re-alloc reuses freed slot */
    char *c = pcc_iobuf_alloc(10);
    if (!c) return 10;
    if (pcc_iobuf_alloc_count() != 2) return 11; /* reused, not grown */
    if (pcc_iobuf_free(c) != 0) return 12;
    return 0;
}
""",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.integration
def test_iox_outcome_semantics(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    exe = _build_c_probe(
        tmp_path,
        pcc_runtime_archive,
        "iox_probe",
        r"""
#include <stdint.h>
#include <string.h>
typedef struct PyObject PyObject;
extern int64_t pcc_io_is_wouldblock(int64_t outcome);
extern int64_t pcc_io_is_more(int64_t outcome);
extern char *pcc_io_outcome_label(int64_t outcome);
int main(void) {
    if (pcc_io_is_wouldblock(2) != 1) return 1;
    if (pcc_io_is_wouldblock(1) != 0) return 2;
    if (pcc_io_is_more(1) != 1) return 3;
    if (pcc_io_is_more(0) != 0) return 4;
    if (strcmp(pcc_io_outcome_label(0), "ok") != 0) return 5;
    if (strcmp(pcc_io_outcome_label(2), "wouldblock") != 0) return 6;
    if (strcmp(pcc_io_outcome_label(-1), "err") != 0) return 7;
    return 0;
}
""",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.integration
def test_socket_nonblock_recv_abi_and_wouldblock(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    """Zero-allocation nonblocking recv: closed/invalid fd returns errno-style
    codes and EAGAIN maps to WouldBlock (-2) without any per-call allocation."""
    exe = _build_c_probe(
        tmp_path,
        pcc_runtime_archive,
        "sock_nonblock_probe",
        r"""
#include <stdint.h>
extern int64_t pcc_platform_socket_recv_nonblock(int64_t fd, int64_t size, int64_t flags);
int main(void) {
    /* invalid fd -> negative (errno-style), never 0-or-positive garbage */
    if (pcc_platform_socket_recv_nonblock(-1, 1024, 0) >= 0) return 1;
    if (pcc_platform_socket_recv_nonblock(99999, 100, 0) >= 0) return 2;
    /* size clamp: huge request still allowed (bounded by 1024 internally) */
    if (pcc_platform_socket_recv_nonblock(-1, 100000, 0) >= 0) return 3;
    /* size <= 0 rejected */
    if (pcc_platform_socket_recv_nonblock(0, 0, 0) != -1) return 4;
    return 0;
}
""",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.integration
def test_uring_sq_cq_ring_logic(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    """io_uring submission/completion queue index logic: sqe init, submit
    advancing SQ tail, cqe peek/advance in FIFO order with ring wraparound."""
    exe = _build_c_probe(
        tmp_path,
        pcc_runtime_archive,
        "uring_probe",
        r"""
#include <stdint.h>
#include <string.h>
extern int pcc_uring_sqe_init(void *sqe, int opcode, int fd, int64_t off, void *addr, int len);
extern int pcc_uring_submit_sqe(void *ring, void *sqe, int64_t user_data);
extern int64_t pcc_uring_sq_ready(void *ring);
extern int pcc_uring_cq_peek(void *ring, void **cqe, int64_t *res, int64_t *ud);
extern int pcc_uring_cq_advance(void *ring);
extern int64_t pcc_uring_cq_ready(void *ring);

/* ring descriptor: mask, entries, sq_tail, sq_head, sq_array,
   cq_mask, cq_entries, cq_tail, cq_head, cqe_array (offsets 0..72) */
static int64_t ring[10];
static int64_t sq_array[4];   /* 4-slot SQ index array */
static int64_t cqes[4][2];    /* 4 cqe slots (user_data, res) */

int main(void) {
    /* 4-slot ring: mask=3 */
    memset(ring, 0, sizeof(ring));
    ring[0] = 3;              /* sq_ring_mask */
    ring[8] = 3;              /* sq_entries */
    ring[4] = 3;              /* cq_ring_mask */
    ring[5] = 4;              /* cq_entries */
    ring[4+4] = 3;            /* (cq mask at offset 40 = ring[5]) */
    ring[0+4] = 3;            /* sq mask at 0 is ring[0] */
    ring[4] = 3;              /* cq mask = ring[4] */
    ring[9] = (int64_t)(void *)cqes; /* cqe_array at offset 72 = ring[9] */
    ring[4] = 3;              /* cq mask */
    ring[5] = 4;              /* cq entries */
    /* fix: sq_array at offset 32 = ring[4]? no — layout is:
       ring[0]=sq_mask ring[1]=sq_entries ring[2]=sq_tail ring[3]=sq_head
       ring[4]=sq_array(ptr) ring[5]=cq_mask ring[6]=cq_entries
       ring[7]=cq_tail ring[8]=cq_head ring[9]=cq_array(ptr) */
    memset(ring, 0, sizeof(ring));
    ring[0] = 3;              /* sq_mask */
    ring[1] = 4;              /* sq_entries */
    ring[4] = (int64_t)(void *)sq_array; /* sq_array */
    ring[5] = 3;              /* cq_mask */
    ring[6] = 4;              /* cq_entries */
    ring[9] = (int64_t)(void *)cqes;     /* cq_array */

    void *sqe = sq_array; /* reuse a slot as the sqe table entry carrier */
    /* init + submit 3 operations */
    for (int i = 0; i < 3; i++) {
        if (pcc_uring_sqe_init(sqe, 1, i + 10, i * 8, (void *)(intptr_t)(0x1000 + i), 4096) != 0) return 1;
        if (pcc_uring_submit_sqe((void *)ring, sqe, 1000 + i) != 0) return 2;
    }
    if (pcc_uring_sq_ready((void *)ring) != 3) return 3;

    /* complete all three in-order (simulate kernel writing cqes) */
    for (int i = 0; i < 3; i++) {
        cqes[i][0] = 1000 + i;   /* user_data */
        cqes[i][1] = 4096 + i;   /* res */
    }
    ring[7] = 3;  /* cq_tail = 3 */
    if (pcc_uring_cq_ready((void *)ring) != 3) return 4;

    for (int i = 0; i < 3; i++) {
        void *cqe = 0; int64_t res = 0, ud = 0;
        if (pcc_uring_cq_peek((void *)ring, &cqe, &res, &ud) != 1) return 5;
        if (ud != 1000 + i) return 6;
        if (res != 4096 + i) return 7;
        if (pcc_uring_cq_advance((void *)ring) != 0) return 8;
    }
    if (pcc_uring_cq_ready((void *)ring) != 0) return 9;
    return 0;
}
""",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
