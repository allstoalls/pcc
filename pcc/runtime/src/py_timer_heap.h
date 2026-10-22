/* py_timer_heap.h - scalable virtual-thread timer structure (C mirror).
 *
 * This is the C-runtime mirror of the CPU-only oracle
 * ``pcc/vthread/timer_oracle.py::MinHeapTimerQueue`` (see
 * ``docs/design/pcc-vthread-oracles.md``). It implements the SAME algorithm:
 *
 *   * a binary min-heap keyed on ``(deadline, seq)`` (insert O(log n),
 *     extract-min O(log n), peek O(1)); and
 *   * lazy cancellation via an authoritative ``timer_id -> (deadline, seq)``
 *     live map: cancel(id) is O(1) amortized and only marks the id dead, the
 *     stale heap slot being skipped when it surfaces at the root during
 *     pop_expired.
 *
 * The production vthread scheduler in ``pcc_threads.c`` owns one of these
 * heaps.  It replaced the former O(n)-insert sorted singly-linked list.  The
 * structure deliberately remains independent of PyObject/GC details and
 * carries only an opaque ``int64_t timer_id``; the scheduler maps each id to a
 * stable pooled node whose thread slot is registered through the shared
 * scheduler-root-handle contract.
 *
 * Semantics mirrored from the oracle (must not be weakened):
 *   * Expiry order is nondecreasing by deadline; FIFO among equal deadlines
 *     (the monotonic insertion ``seq`` is the tiebreaker, matching the C list's
 *     stable ``<= deadline`` walk).
 *   * pop_expired(now) drains every id with ``deadline <= now`` (inclusive; the
 *     C poller breaks on ``deadline_ms > now``).
 *   * Root retention: an id stays registered (counts toward size, remains
 *     cancellable) until it is expired or cancelled.
 *   * Done/cancelled skip: a cancelled or superseded id surfacing at the root
 *     is dropped without being returned.
 *
 * Deliberately dependency-free: no ``PyObject``, no GC, no libpython. It uses
 * only ``<stdint.h>``/``<stdlib.h>``/``<string.h>`` so it remains independently
 * testable even though the production scheduler now embeds it.
 */
#ifndef PY_TIMER_HEAP_H
#define PY_TIMER_HEAP_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PccTimerHeapNode {
    int64_t deadline;
    int64_t seq;
    int64_t timer_id;
} PccTimerHeapNode;

/* Authoritative live registration: an open-addressing hash map from
 * timer_id -> (deadline, seq). A heap node is "stale" (cancelled or superseded
 * by a reschedule) iff the live map has no matching (deadline, seq) for its id.
 */
typedef struct PccTimerLiveSlot {
    int64_t timer_id;
    int64_t deadline;
    int64_t seq;
    uint8_t state; /* 0 empty, 1 live, 2 tombstone */
} PccTimerLiveSlot;

typedef struct PccTimerHeap {
    PccTimerHeapNode *nodes; /* binary min-heap, 0-based array */
    int64_t heap_len;
    int64_t heap_cap;

    PccTimerLiveSlot *live; /* open-addressing hash table */
    int64_t live_count;     /* number of state==1 (registered) ids */
    int64_t live_used;      /* live + tombstone slots (for load factor) */
    int64_t live_cap;       /* power-of-two capacity */

    int64_t seq; /* monotonic insertion counter (FIFO tiebreak) */
} PccTimerHeap;

/* Lifecycle. Returns 0 on success, -1 on allocation failure. */
int pcc_timer_heap_init(PccTimerHeap *h);
void pcc_timer_heap_dispose(PccTimerHeap *h);

/* Register timer_id to fire at deadline (O(log n)). Re-inserting an already
 * registered id reschedules it (old heap slot becomes stale, skipped later).
 * Returns 0 on success, -1 on allocation failure. */
int pcc_timer_heap_insert(PccTimerHeap *h, int64_t deadline, int64_t timer_id);

/* Cancel a registered id (O(1) amortized). Returns 1 if it was live, else 0.
 * Lazy: the heap slot is left in place and skipped when it surfaces. */
int pcc_timer_heap_cancel(PccTimerHeap *h, int64_t timer_id);

/* True (1) if timer_id is still registered (not expired, not cancelled). */
int pcc_timer_heap_is_registered(const PccTimerHeap *h, int64_t timer_id);

/* Number of ids still registered (mirrors py_virtual_thread_timer_count). */
int64_t pcc_timer_heap_size(const PccTimerHeap *h);

/* Peek the soonest live deadline into *out_deadline (O(1)-amortized: it may
 * pop leading stale slots first). Returns 1 if a live entry exists, else 0. */
int pcc_timer_heap_peek(PccTimerHeap *h, int64_t *out_deadline);

/* Drain all ids with deadline <= now, in nondecreasing-deadline / FIFO order,
 * skipping stale (cancelled/superseded) slots. Expired ids are written to
 * out_ids[0..return-1] (caller-provided buffer of at least out_cap entries)
 * and unregistered. Returns the number drained. If the caller-provided buffer
 * is too small the drain stops at out_cap (the remaining due ids stay
 * registered for the next call). Pass out_ids==NULL / out_cap==0 to just
 * count+drain without collecting ids. */
int64_t pcc_timer_heap_pop_expired(
    PccTimerHeap *h,
    int64_t now,
    int64_t *out_ids,
    int64_t out_cap
);

#ifdef __cplusplus
}
#endif

#endif /* PY_TIMER_HEAP_H */
