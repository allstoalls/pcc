# pcc virtual-thread scheduling oracles and production timer route

Status: CPU-only oracles landed; the timer heap, Darwin-kqueue/portable-poll
waitset, and bounded runtime-effect/root-lifecycle event gate are integrated
into the production C scheduler. Linux epoll remains future work.

## Purpose

The pcc virtual-thread runtime (`pcc/runtime/src/pcc_threads.c`, Loom-shaped
scheduler; see `docs/investigations/virtual-threads-runtime-prerequisites.md`)
originally backed two blocking-wait structures with O(n) data structures that
do not scale to ~1M parked virtual threads:

1. **Timer queue (replaced)** — production now uses `PccTimerHeap`, keyed on
   `(deadline, seq)`, plus a live-map for lazy O(1)-amortized cancellation.
   Stable pooled scheduler nodes own the parked-thread GC roots. The former
   sorted singly-linked insertion walk is absent from the production route.
2. **IO wait route (replaced)** — stable per-vthread nodes still own roots, but
   one `PccIoWaitSet` aggregates unique fds. Darwin/BSD use kqueue; the explicit
   fallback issues one `poll(2)` over unique fds and feeds the common waitset.
   The former one-poll-syscall-per-vthread loop is absent.

The Python oracles were deliberately landed before the production route. They
remain the deterministic executable specification used to diff the C helper;
focused production tests separately prove timer ordering, cancellation, root
retention, and done-thread skip under GC0..4.

## Modules

| File | Role |
|---|---|
| `pcc/vthread/timer_oracle.py` | Scalable timer structure + naive baseline |
| `pcc/vthread/io_waitset_oracle.py` | IO waitset abstraction (poll fallback + kqueue-sim) |
| `tests/vthread/` | Invariant + agreement + op-count tests |
| `pcc/runtime/src/py_timer_heap.[ch]` | Dependency-free production heap helper |
| `pcc/runtime/src/pcc_threads.c` | Production timer ownership, roots, pooling, wake/cancel route |
| `pcc/diagnostics/contracts/runtime_effects.py` | Shared production-event vocabulary and root/state contract checker |
| `tests/python/test_vthread_timer_heap_scheduler.py` | Production C + pcc-Python archive ordering gate |
| `tests/python/gc_production_contract/test_vthread_timer_cancel.py` | Production GC0..4 cancel/root/done-skip gate |
| `tests/python/gc_production_contract/test_vthread_runtime_effect_events.py` | Production GC0..4 transition/effect/root event gate |

Class map:

- `timer_oracle.MinHeapTimerQueue` — chosen structure.
- `timer_oracle.NaiveSortedListTimerQueue` — cost-model mirror of the current C
  sorted linked list; the op-count baseline.
- `timer_oracle.TimerOpCounts` — insert/cancel/expiry/comparison instrumentation.
- `io_waitset_oracle.IOWaitSet` — abstract base (add / remove / wait_count /
  set_ready / clear_ready / wait).
- `io_waitset_oracle.PollWaitSet` — level-triggered poll fallback (mirrors C).
- `io_waitset_oracle.KqueueSimWaitSet` — pure-Python kqueue/epoll readiness
  simulation, level + edge triggered.
- `io_waitset_oracle.SkippedReason` / `real_kqueue_backend()` — the real
  syscall path, reported as `SKIPPED_WITH_REASON`.

The `pcc/vthread/` package does **not** import from or modify `pcc/__init__.py`;
both oracle modules import standalone. Tests load them by file path via
`tests/vthread/conftest.py` so they never pull in the compiler package.

## Timer-structure choice: binary min-heap + lazy cancellation

Chosen: a **binary min-heap** keyed on `(deadline, seq)` with an authoritative
`timer_id -> (deadline, seq)` live-set map for O(1) membership and lazy
cancellation.

- insert: **O(log n)** (heap sift-up).
- cancel: **O(1)** amortized (mark dead in the live-set; the stale heap slot is
  skipped when it surfaces at the root).
- expire_due: **O(k log n)** for `k` due entries (each `heappop` is `O(log n)`;
  stale/cancelled slots are dropped in the same pass).
- peek soonest deadline: **O(1)**.

### Why not a hashed timing wheel (for the first slice)

A hashed/hierarchical timing wheel gives **O(1)-amortized** insert and expiry,
which is asymptotically better, but only under preconditions the current
vthread API does not provide:

- a **fixed tick granularity** (the wheel advances by whole ticks), and
- a **bounded max-timeout horizon** per wheel level (far-future deadlines need
  hierarchical wheels or overflow lists, adding cascade cost).

The vthread sleep API (`py_virtual_thread_sleep(vt, delay_ms)`) accepts an
arbitrary `delay_ms`, and the poller is driven by irregular `now` snapshots from
`pcc_vthread_now_ms()` rather than a fixed tick loop. A min-heap has **no such
precondition**, is trivially deterministic (the `seq` tiebreak reproduces the C
list's FIFO-among-equal-deadlines behavior exactly), and is a smaller, safer
first mirror for the C slice. The timing wheel remains a documented future
optimization once the scheduler adopts a fixed tick cadence — at which point a
wheel oracle can be added alongside this one and diffed against it.

### Timer semantics mirrored (must not be weakened)

- Expiry order is **nondecreasing by deadline**; among equal deadlines it is
  **FIFO** (matches the C `<= deadline` stable list walk).
- `expire_due(now)` releases every entry with `deadline <= now` (C breaks on
  `deadline_ms > now`, so `deadline == now` is due).
- **Root retention**: an id stays registered (counts toward `pending_count`,
  remains cancellable) until it is expired or cancelled — mirroring the C entry
  owning a GC root handle for the parked thread until the entry is freed.
- **Done/cancelled skip**: a cancelled (or superseded/rescheduled) entry that
  surfaces at the root is dropped without being returned — mirroring the C
  poller's `state == PCC_VTHREAD_PARKED` check that skips a stale timer.

## IO waitset: abstraction + two readiness models

`IOWaitSet` is the abstraction. Two concrete models implement it:

- **`PollWaitSet` (level-triggered fallback)** re-scans every registered fd on
  each `wait`, exactly like the C `while (*cur != NULL)` loop calling
  `pcc_vthread_fd_ready()` per entry. As long as an fd is ready and registered,
  every `wait` reports it. It carries a `scan_steps` counter that exposes the
  O(n) per-poll cost being replaced.
- **`KqueueSimWaitSet` (kqueue/epoll simulation)** delivers from a
  **pending-event queue** driven by readiness *transitions*, i.e. O(ready) per
  wait, no full-set rescan. It supports level- and edge-triggered filters:
  - level mode re-arms while the fd is ready and therefore **agrees with the
    poll fallback** on the readiness sequence (tested, scripted + randomized);
  - edge mode delivers one event per `false->true` readiness transition.

Event flags are the POSIX `poll` bits the C runtime uses
(`POLLIN`/`POLLOUT` + always-reported `POLLERR`/`POLLHUP`/`POLLNVAL`), so the C
slice maps them onto kqueue filters (`EVFILT_READ`/`EVFILT_WRITE`) or `epoll`
events without redefining semantics. Interest-mask filtering and inclusive
deadline timeout (`deadline <= now`, `None`==infinite==C `-1`) match the C
poller. Ready wins over timeout at the same tick (C treats an expired entry as
`ready==1`).

### Real kqueue path

The CPU-only oracle still returns a `SkippedReason` because it issues no live-fd
syscalls. Separately, the production C scheduler owns the real `kevent(2)` path
on Darwin/BSD. `PCC_VTHREAD_IO_BACKEND=poll` forces the fallback for parity
tests; the selected mode is observable through
`py_virtual_thread_io_backend()`. Linux epoll is not implemented.

## Production state and remaining plan

1. **Timer queue — DONE.** `pcc_threads.c` embeds the binary heap and live map.
   Each stable pooled timer node registers its thread slot through
   `pcc_gc_scheduler_root_register_handle`; expiry transfers ownership to one
   ready-queue root, while cancellation/complete/unpark unregister and recycle
   the timer node immediately. Lazy stale heap tuples contain only opaque ids,
   never unregistered `PyObject *` roots.
2. **IO waitset — DONE for kqueue + poll fallback.** Production aggregates
   same-fd waiters into one kernel registration without merging their GC-root
   nodes or per-wait deadlines. Darwin/BSD select kqueue automatically; other
   platforms and the forced test mode select one-call live poll. Pipe, Unix
   socketpair, deadline timeout, same-fd multi-waiter, early completion, and
   root transfer pass under GC0..4. Linux epoll remains a separate future
   backend, not an implied claim.
3. **Mirror discipline** — the pcc-Python timer structure mirrors the C helper,
   while both C-runtime and pcc-Python archives use the same C scheduler seam
   and the same slot-based root/update contract. The pure pcc-Python IO class
   remains the syscall-free semantic mirror; the pcc-Python archive gets live
   kqueue/poll through the shared C-level kernel seam, not a second root model.
4. **Production event contract — DONE for the scoped scheduler route.** An
   allocation-free 4096-entry event buffer records start, park, unpark, resume,
   timer/IO park and wake, cancellation, completion, ready enqueue, and actual
   scheduler-root handle enter/leave operations. Overflow is fail-visible via
   a dropped-event counter rather than allocation or overwrite. The Python
   checker maps these observations into the shared `RuntimeEffect` vocabulary,
   validates event state/root-delta schema, rejects negative root balance or
   scheduler visibility without a root, and requires final balance zero.

## Claim boundary

The modules under `pcc/vthread/` are still a **CPU-only oracle**, not by
themselves a runtime or 1M-vthread result. They validate the *algorithms* (min-heap
timer ordering/cancellation/retention; poll-fallback vs kqueue-sim readiness
agreement) via deterministic operation-count and sequence oracles. They prove
asymptotic separation from the naive O(n) baseline on operation counts.

Separately, focused production gates prove the no-libpython C and pcc-Python
archive timer routes plus Darwin kqueue / poll-fallback IO semantics under
GC0..4. The same focused route proves that its production transition events
reflect actual root-handle transfers and satisfy the shared effect/root
checker. It does **not** prove a measured 1M-parked-vthread wall-clock result,
unbounded trace retention, or Linux epoll. `SKIPPED_WITH_REASON` applies only
to the CPU-only oracle/pure pcc-Python mirror and to platforms without kqueue,
not to the tested Darwin C scheduler route.

## Gate command

```bash
env -u LC_ALL uv run pytest tests/vthread -q -n0
env -u LC_ALL uv run pytest -q -n0 \
  tests/python/test_vthread_timer_heap_scheduler.py \
  tests/python/gc_production_contract/test_vthread_timer_cancel.py \
  tests/python/gc_production_contract/test_vthread_io_waitset_runtime.py \
  tests/python/gc_production_contract/test_vthread_runtime_effect_events.py
```
