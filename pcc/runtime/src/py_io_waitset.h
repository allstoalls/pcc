/* py_io_waitset.h - scalable virtual-thread IO waitset (C mirror).
 *
 * This is the C-runtime mirror of the CPU-only oracle
 * ``pcc/vthread/io_waitset_oracle.py`` (``PollWaitSet`` +
 * ``KqueueSimWaitSet``; see ``docs/design/pcc-vthread-oracles.md``). It
 * is embedded by the production vthread scheduler in ``pcc_threads.c``. The
 * scheduler retains stable per-vthread nodes for GC-root ownership, but this
 * waitset now owns the live-fd readiness index: Darwin/BSD use kqueue and the
 * fallback performs one poll(2) call over unique registered fds. The former
 * one-poll-syscall-per-entry loop is absent from ``py_virtual_thread_poll_io``.
 *
 * Two readiness backends, one abstraction (mirroring the oracle):
 *
 *   * POLL FALLBACK (``PCC_IO_WAITSET_BACKEND_POLL``) - the level-triggered
 *     fallback. Readiness is fed explicitly through
 *     ``pcc_io_waitset_set_ready`` (mirroring the oracle's ``set_ready``, which
 *     stands in for ``poll(2)``'s revents). On each ``wait`` every registered
 *     fd is checked against its current readiness mask, exactly like the C
 *     ``while (*cur != NULL)`` loop. As long as an fd stays ready and
 *     registered, every wait reports it. This backend does NOT call any
 *     syscall and owns no real fds, so it is deterministic and diffable against
 *     the oracle. It is available on every platform.
 *
 *   * DARWIN KQUEUE (``PCC_IO_WAITSET_BACKEND_KQUEUE``) - a real
 *     ``kqueue``/``kevent(2)`` readiness notifier over LIVE fds, compiled only
 *     on Darwin/BSD (``__APPLE__`` / ``__FreeBSD__`` ...). It maps the POSIX
 *     poll interest bits onto kqueue filters (``EVFILT_READ`` for ``POLLIN``,
 *     ``EVFILT_WRITE`` for ``POLLOUT``) and reports ``EV_EOF`` as the
 *     always-reported ``POLLHUP`` bit. On non-kqueue platforms this backend is
 *     UNAVAILABLE and ``pcc_io_waitset_kqueue_available()`` returns 0.
 *
 *   * LINUX EPOLL (``PCC_IO_WAITSET_BACKEND_EPOLL``) - a real bounded
 *     ``epoll_wait(2)`` notifier. Registrations use ``EPOLLONESHOT`` and a
 *     generation-bearing data token, so an event queued for a closed fd cannot
 *     wake a later registration that reuses the same integer descriptor.
 *
 * Semantics mirrored from the oracle (must not be weakened), matching the C
 * poller's ``revents & (events | POLLERR | POLLHUP | POLLNVAL)`` behavior:
 *
 *   * Interest-mask filtering: only requested bits (plus the always-reported
 *     error bits ``POLLERR``/``POLLHUP``/``POLLNVAL``) count as ready.
 *   * One-shot delivery: a delivered fd is unregistered (the C poller splices
 *     the entry out and unparks the thread).
 *   * Inclusive deadline timeout: an fd whose ``deadline <= now`` times out
 *     (C: ``deadline_ms <= now``); ``deadline < 0`` means infinite.
 *   * Ready wins over timeout at the same tick (C treats an expired entry as
 *     ``ready == 1``).
 *
 * Deliberately dependency-free at the Python-runtime layer: no ``PyObject``,
 * no GC, no libpython. The live backends additionally use the platform kernel
 * readiness/interrupt APIs (kqueue EVFILT_USER or epoll eventfd), while the
 * poll structure stays deterministic and syscall-free.
 */
#ifndef PY_IO_WAITSET_H
#define PY_IO_WAITSET_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* POSIX poll event bits, matching pcc_vthread_fd_ready's use of poll(2) and the
 * oracle's POLLIN/POLLOUT/... constants. Defined locally (not pulled from
 * <poll.h>) so the poll-fallback structure stays dependency-free and the
 * values agree with the oracle regardless of the host <poll.h>. */
#define PCC_IO_POLLIN 0x0001
#define PCC_IO_POLLOUT 0x0004
#define PCC_IO_POLLERR 0x0008
#define PCC_IO_POLLHUP 0x0010
#define PCC_IO_POLLNVAL 0x0020

/* Bits reported even when not explicitly requested (C:
 * revents & (events | POLLERR | POLLHUP | POLLNVAL)). */
#define PCC_IO_ALWAYS_REPORTED (PCC_IO_POLLERR | PCC_IO_POLLHUP | PCC_IO_POLLNVAL)

typedef enum PccIoWaitSetBackend {
    PCC_IO_WAITSET_BACKEND_POLL = 0,   /* level-triggered fed-readiness fallback */
    PCC_IO_WAITSET_BACKEND_KQUEUE = 1, /* real kqueue/kevent (Darwin/BSD only) */
    PCC_IO_WAITSET_BACKEND_EPOLL = 2   /* real epoll (Linux only) */
} PccIoWaitSetBackend;

/* One delivered readiness event (mirrors oracle ReadyEvent). */
typedef struct PccIoReadyEvent {
    int64_t fd;
    int64_t events; /* requested bits that are ready (+ always-reported bits) */
} PccIoReadyEvent;

/* Registration of one fd. deadline < 0 means infinite (C: -1). */
typedef struct PccIoWaitSlot {
    int64_t fd;
    int64_t interest;   /* requested event mask */
    int64_t deadline;   /* logical-clock deadline; <0 == infinite */
    int64_t ready_mask; /* fed readiness (poll fallback) / cached level state */
    uint8_t edge;       /* edge-triggered on live kqueue/epoll backends */
    uint8_t state;      /* 0 empty, 1 live */
    uint16_t _padding;
    uint32_t generation; /* rejects stale events after fd reuse */
} PccIoWaitSlot;

typedef struct PccIoWaitSet {
    PccIoWaitSetBackend backend;

    PccIoWaitSlot *slots; /* dense array of live/empty registrations */
    int64_t len;
    int64_t cap;
    int64_t live_count;

    /* Reusable output scratch so wait() need not allocate; grown on demand. */
    PccIoReadyEvent *ready_buf;
    int64_t ready_cap;
    int64_t *timeout_buf;
    int64_t timeout_cap;

    /* Kernel notifier fd (kqueue/epoll; -1 on the poll backend). */
    int kq_fd;
    uint32_t next_generation;

    /* epoll's compiler-owned eventfd interrupt channel. kqueue uses an
     * EVFILT_USER registration on kq_fd instead, so wake_fd stays -1 there. */
    int wake_fd;
    uint32_t _wake_padding;
} PccIoWaitSet;

/* Result of one wait() drain. The arrays alias the waitset's reusable scratch
 * and are valid only until the next wait()/dispose(). */
typedef struct PccIoWaitResult {
    const PccIoReadyEvent *ready;
    int64_t ready_len;
    const int64_t *timed_out; /* fds */
    int64_t timeout_len;
} PccIoWaitResult;

/* Opaque-to-callers storage for a split live wait. prepare() snapshots all
 * mutable waitset state while the scheduler lock is held; block() touches
 * only the stable kernel notifier and this batch; finish() resolves generation
 * tokens against the current slot table after the scheduler lock is retaken.
 * Keep this layout in sync with py/freestanding_io_waitset.py (64 bytes). */
typedef struct PccIoWaitBatch {
    void *events;
    int64_t event_capacity;
    int64_t event_count;
    int64_t current_now;
    int64_t effective_deadline;
    int64_t wait_deadline;
    int64_t backend;
    int64_t status;
} PccIoWaitBatch;

/* Lifecycle. init selects the backend; init with KQUEUE on a non-kqueue
 * platform fails (returns -1) - callers must probe availability first and fall
 * back to POLL. Returns 0 on success, -1 on failure. */
int pcc_io_waitset_init(PccIoWaitSet *ws, PccIoWaitSetBackend backend);
void pcc_io_waitset_dispose(PccIoWaitSet *ws);

/* Register fd with an interest mask, deadline (<0 == infinite), and edge flag
 * (edge is honored by live kqueue/epoll; the poll fallback is inherently
 * level-triggered and ignores it, matching the oracle). Re-adding an fd updates
 * its registration. Returns 0 on success, -1 on failure. */
int pcc_io_waitset_add(
    PccIoWaitSet *ws,
    int64_t fd,
    int64_t interest,
    int64_t deadline,
    int edge
);

/* Unregister fd. Returns 1 if it was registered, else 0. */
int pcc_io_waitset_remove(PccIoWaitSet *ws, int64_t fd);

/* Number of registered fds (mirrors py_virtual_thread_io_wait_count). */
int64_t pcc_io_waitset_count(const PccIoWaitSet *ws);

/* Feed readiness for a registered fd (poll fallback / test-driver hook,
 * mirroring the oracle's set_ready that stands in for poll(2) revents). On the
 * kqueue backend this records a cached level state used only for edge-transition
 * bookkeeping; real readiness there comes from kevent(2). */
void pcc_io_waitset_set_ready(PccIoWaitSet *ws, int64_t fd, int64_t events);

/* Clear fed readiness for fd (mirrors oracle clear_ready). */
void pcc_io_waitset_clear_ready(PccIoWaitSet *ws, int64_t fd);

/* Drain ready + timed-out fds for logical clock ``now``. On the poll fallback
 * this rescans registered fds against their fed readiness (level-triggered).
 * On live backends this performs an immediate kevent(2)/epoll_wait(2) collect,
 * then scans deadlines for timeouts. Delivered/timed-out fds are
 * unregistered (one-shot). Fills *out with arrays aliasing the waitset's
 * reusable scratch (valid until the next call). Returns 0 on success, -1 on
 * failure. */
int pcc_io_waitset_wait(PccIoWaitSet *ws, int64_t now, PccIoWaitResult *out);

/* Live backends may block until readiness or the earlier of an aggregate fd
 * deadline and ``wait_deadline``. All deadlines are absolute monotonic
 * milliseconds; a negative wait_deadline is infinite. EINTR retries recompute
 * the remaining duration from that absolute deadline. The legacy wait()
 * above is an immediate observation equivalent to wait_until(ws, now, now). */
int pcc_io_waitset_wait_until(
    PccIoWaitSet *ws,
    int64_t now,
    int64_t wait_deadline,
    PccIoWaitResult *out
);

/* Interrupt one live wait without acquiring scheduler-owned state. Repeated
 * calls coalesce. POLL has no blocking kernel wait and treats this as a no-op.
 * Returns 0 on success, -1 on a hard notifier failure. */
int pcc_io_waitset_interrupt(PccIoWaitSet *ws);

/* Three-phase live wait used by the virtual-thread scheduler to avoid holding
 * its global mutex across kevent/epoll_wait. prepare and finish require the
 * caller's waitset-ownership lock; block deliberately runs without it. */
int pcc_io_waitset_wait_prepare(
    PccIoWaitSet *ws,
    int64_t now,
    int64_t wait_deadline,
    PccIoWaitBatch *batch
);
int pcc_io_waitset_wait_block(PccIoWaitSet *ws, PccIoWaitBatch *batch);
int pcc_io_waitset_wait_finish(
    PccIoWaitSet *ws,
    PccIoWaitBatch *batch,
    PccIoWaitResult *out
);
void pcc_io_waitset_wait_discard(PccIoWaitBatch *batch);

/* ---- platform capability + skip reason (mirrors oracle SkippedReason) ----- */

/* 1 if the real kqueue backend is available on this platform, else 0. */
int pcc_io_waitset_kqueue_available(void);

/* A machine-readable skip marker for the real-kqueue path when it is not
 * available, mirroring the oracle's SkippedReason(path, reason). ``path`` and
 * ``reason`` point at static strings. Returns 1 and fills *out when the path is
 * skipped (kqueue unavailable); returns 0 (leaving *out untouched) when kqueue
 * IS available and therefore not skipped. */
typedef struct PccIoWaitSetSkip {
    const char *path;
    const char *reason;
} PccIoWaitSetSkip;

int pcc_io_waitset_real_kqueue_skip(PccIoWaitSetSkip *out);

/* Linux live-epoll capability and its machine-readable skip reason. */
int pcc_io_waitset_epoll_available(void);
int pcc_io_waitset_real_epoll_skip(PccIoWaitSetSkip *out);

/* Stable backend reporting used by the pcc1 scheduler and gateway. */
const char *pcc_io_waitset_backend_label(int64_t backend);
int64_t pcc_io_waitset_default_backend(void);

#ifdef __cplusplus
}
#endif

#endif /* PY_IO_WAITSET_H */
