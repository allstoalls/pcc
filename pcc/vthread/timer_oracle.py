"""CPU-only oracle for a scalable virtual-thread timer structure.

This module is an ORACLE, not the runtime. It exists to design and validate
the *algorithm* that a later C slice under ``pcc/runtime/src/pcc_threads.c``
will mirror, replacing the current O(n)-insert sorted singly-linked timer
queue (``pcc_vthread_timer_add_locked`` / ``py_virtual_thread_poll_timers``).

Claim boundary
--------------
* CPU-only Python oracle. Not the runtime C implementation.
* Does NOT prove a 1M-parked-vthread result; it proves the *structure* is
  O(log n) insert / O(1)-amortized cancel / O(k log n) expiry via an
  operation-count comparison against a naive sorted list.
* No wall-clock, no threads, no libpython. ``now`` is an explicit integer
  logical clock supplied by the caller, exactly as the C reference uses
  ``pcc_vthread_now_ms()`` snapshots.

Structure choice: binary min-heap keyed on ``(deadline, seq)`` with lazy
cancellation
--------------------------------------------------------------------------
Two candidates were considered:

* **Binary min-heap** (chosen). Insert O(log n), extract-min O(log n),
  peek-min O(1). Cancellation is *lazy*: ``cancel(id)`` marks the entry dead in
  an O(1) side map and the dead entry is skipped (not paid for in ordering)
  when it surfaces at the root during ``expire_due``. This keeps the hot path
  (insert + expire) logarithmic regardless of how deadlines are distributed,
  and needs no assumption about the deadline range.

* **Hashed / hierarchical timing wheel** (added as a *second* oracle in this
  slice, see :class:`HashedTimingWheelTimerQueue`). A wheel gives O(1)
  insert/expire *amortized*, but only under a bounded, known tick granularity
  and a bounded max-timeout horizon; far-future deadlines need overflow lists /
  hierarchical wheels that cascade down at extra cost. The vthread sleep API
  (``py_virtual_thread_sleep(vt, delay_ms)``) takes an arbitrary ``delay_ms``
  and the poller is driven by irregular ``now`` snapshots rather than a fixed
  tick, so the wheel is built as a **hierarchical** wheel (Linux-kernel /
  Kafka-style): a fixed tick quantizes each level, and higher levels cascade
  their buckets down as the low-level wheel wraps, so an arbitrary ``delay_ms``
  and an arbitrary forward ``now`` jump both stay correct. It is validated to
  produce **byte-identical** expiry sequences to the min-heap (and to the naive
  baseline) on scripted + randomized cases, so the C slice may pick either
  structure behind one contract. The min-heap remains the first C mirror
  because it has no tick/horizon precondition and is a smaller correct mirror;
  the wheel is the documented next optimization once a fixed tick cadence
  exists (see ``docs/design/pcc-vthread-oracles.md``).

Semantics mirrored from the C reference (must not be weakened)
--------------------------------------------------------------
* Expiry order is nondecreasing by deadline. Among equal deadlines the C
  sorted list walks ``(*cur)->deadline_ms <= deadline_ms`` before inserting,
  i.e. FIFO among equal deadlines. The heap reproduces this with a monotonic
  insertion ``seq`` as the tiebreaker.
* ``expire_due(now)`` releases every entry with ``deadline <= now`` (the C loop
  breaks on ``entry->deadline_ms > now``), in nondecreasing deadline order.
* Root retention: an id stays registered (counts toward ``pending_count`` and
  can be cancelled) until it is either expired by ``expire_due`` or cancelled.
  This mirrors the C entry owning a GC root handle for the parked thread until
  the entry is freed.
* Done/cancelled skip: a cancelled entry surfacing at the root is dropped
  without being returned, mirroring the C poller's state check that skips a
  timer whose thread is no longer ``PCC_VTHREAD_PARKED``.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class TimerOpCounts:
    """Operation-count instrumentation for the O(log n) claim.

    ``comparisons`` counts heap sift comparisons (the dominant cost); the naive
    baseline counts list scan steps into the same field so the two are directly
    comparable.
    """

    inserts: int = 0
    cancels: int = 0
    expiries: int = 0
    comparisons: int = 0


@dataclass
class _Entry:
    deadline: int
    seq: int
    timer_id: int


class MinHeapTimerQueue:
    """Scalable timer queue: binary min-heap + lazy cancellation.

    The heap stores ``(deadline, seq, timer_id)`` tuples. A live-set map from
    ``timer_id`` to its current ``(deadline, seq)`` is the source of truth for
    membership and cancellation; a heap tuple is *stale* if it does not match
    the live-set entry (re-inserted id) or the id is absent (cancelled/expired).
    """

    def __init__(self) -> None:
        # Heap of (deadline, seq, timer_id). Python heapq is a binary min-heap.
        self._heap: List[Tuple[int, int, int]] = []
        # Authoritative live registration: timer_id -> (deadline, seq).
        self._live: Dict[int, Tuple[int, int]] = {}
        self._seq: int = 0
        self.counts = TimerOpCounts()

    # -- introspection -----------------------------------------------------

    def pending_count(self) -> int:
        """Number of ids still registered (not expired, not cancelled).

        Mirrors ``py_virtual_thread_timer_count()``: it counts logical pending
        timers, not stale heap slots.
        """
        return len(self._live)

    def is_registered(self, timer_id: int) -> bool:
        return timer_id in self._live

    def __len__(self) -> int:
        return len(self._live)

    # -- mutation ----------------------------------------------------------

    def insert(self, deadline: int, timer_id: int) -> None:
        """Register ``timer_id`` to fire at ``deadline`` (O(log n)).

        Re-inserting an already-registered id reschedules it (the old heap
        tuple becomes stale and is skipped later). This matches a vthread being
        re-parked on a new deadline without a separate cancel.
        """
        self._seq += 1
        seq = self._seq
        self._live[timer_id] = (deadline, seq)
        before = len(self._heap)
        heapq.heappush(self._heap, (deadline, seq, timer_id))
        # heappush sifts up: at most ceil(log2(n+1)) comparisons.
        self.counts.inserts += 1
        self.counts.comparisons += _sift_up_cost(before + 1)

    def cancel(self, timer_id: int) -> bool:
        """Cancel a registered id (O(1) amortized). Returns True if it was live.

        Lazy: the heap tuple is left in place and skipped when it surfaces.
        Mirrors the C poller dropping a timer whose thread is no longer parked.
        """
        self.counts.cancels += 1
        if timer_id in self._live:
            del self._live[timer_id]
            return True
        return False

    def expire_due(self, now: int) -> List[int]:
        """Pop and return ids with ``deadline <= now`` in expiry order.

        Order is nondecreasing by deadline, FIFO among equal deadlines. Stale
        tuples (cancelled or re-scheduled ids) are skipped without being
        returned. Expired ids are unregistered (root retention ends).
        """
        self.counts.expiries += 1
        out: List[int] = []
        heap = self._heap
        live = self._live
        while heap:
            deadline, seq, timer_id = heap[0]
            live_entry = live.get(timer_id)
            if live_entry is None or live_entry != (deadline, seq):
                # Stale: cancelled, or superseded by a later insert. Drop it.
                heapq.heappop(heap)
                self.counts.comparisons += _sift_down_cost(len(heap) + 1)
                continue
            if deadline > now:
                # Root of the heap is in the future -> nothing else is due.
                break
            heapq.heappop(heap)
            self.counts.comparisons += _sift_down_cost(len(heap) + 1)
            del live[timer_id]
            out.append(timer_id)
        return out


class NaiveSortedListTimerQueue:
    """Reference baseline mirroring the current C sorted singly-linked list.

    ``pcc_vthread_timer_add_locked`` walks ``while (*cur && (*cur)->deadline <=
    deadline)`` before splicing, so insert is O(n) and expiry pops from the
    head. This class reproduces that cost model exactly so the oracle can show
    the heap wins on operation count for large N.
    """

    def __init__(self) -> None:
        # Sorted list of (deadline, seq, timer_id), head = soonest.
        self._items: List[Tuple[int, int, int]] = []
        self._live: set[int] = set()
        self._seq: int = 0
        self.counts = TimerOpCounts()

    def pending_count(self) -> int:
        return len(self._live)

    def __len__(self) -> int:
        return len(self._live)

    def is_registered(self, timer_id: int) -> bool:
        return timer_id in self._live

    def insert(self, deadline: int, timer_id: int) -> None:
        self._seq += 1
        seq = self._seq
        # Reschedule: drop any prior slot for this id first.
        if timer_id in self._live:
            self._items = [it for it in self._items if it[2] != timer_id]
        self._live.add(timer_id)
        # Linear scan to the insertion point == the C list walk.
        i = 0
        n = len(self._items)
        while i < n and self._items[i][0] <= deadline:
            self.counts.comparisons += 1
            i += 1
        self._items.insert(i, (deadline, seq, timer_id))
        self.counts.inserts += 1

    def cancel(self, timer_id: int) -> bool:
        self.counts.cancels += 1
        if timer_id in self._live:
            self._live.discard(timer_id)
            self._items = [it for it in self._items if it[2] != timer_id]
            return True
        return False

    def expire_due(self, now: int) -> List[int]:
        self.counts.expiries += 1
        out: List[int] = []
        while self._items:
            deadline, _seq, timer_id = self._items[0]
            if deadline > now:
                break
            self._items.pop(0)
            if timer_id in self._live:
                self._live.discard(timer_id)
                out.append(timer_id)
        return out


class HashedTimingWheelTimerQueue:
    """Hierarchical timing wheel: O(1)-amortized insert / advance.

    This is the *second* structure the C slice may adopt. It is a Linux-kernel /
    Kafka-style **hierarchical** wheel that stays correct under the vthread API's
    arbitrary ``delay_ms`` and irregular forward ``now`` snapshots (the min-heap's
    preconditions do not apply), and it is validated to produce **byte-identical**
    expiry sequences to :class:`MinHeapTimerQueue` and
    :class:`NaiveSortedListTimerQueue`.

    Structure
    ---------
    Everything is keyed on integer *ticks* of width ``tick``. Tick ``t`` covers
    the half-open real interval ``[t*tick, (t+1)*tick)``; a deadline ``d`` lives
    in tick ``d // tick``.

    * ``cur_tick`` is the wheel's current logical tick (``base = cur_tick*tick``
      is the time floor). Level 0 is a wheel of ``slots`` buckets covering ticks
      ``[cur_tick, cur_tick + slots)``. Level ``L`` covers ``slots**(L+1)`` ticks
      at ``slots**L`` ticks per bucket, and *overflows* one bucket down into the
      lower levels each time the lower wheel wraps. Levels are created lazily, so
      an arbitrary far-future ``delay_ms`` never overflows a fixed horizon.
    * ``expire_due(now)`` advances ``cur_tick`` toward ``now // tick`` one tick
      at a time. Each tick the clock *leaves behind* has its level-0 bucket
      unconditionally due (the whole tick is in the past). At the final,
      possibly partial, tick (``now`` mid-tick) the current bucket is drained
      keeping entries whose exact ``deadline > now`` (Kafka/Linux-kernel wheel
      discipline; exact deadlines are preserved, ticks only bucket them).

    Membership / cancellation / reschedule are handled exactly like the heap: an
    authoritative ``timer_id -> (deadline, seq)`` live map is the source of
    truth, and a bucket entry is *stale* (skipped, not returned) if it does not
    match the live map. This gives O(1) cancel and reschedule with no wheel walk.

    FIFO among equal deadlines
    --------------------------
    A bucket can hold entries that resolve to the same absolute deadline but were
    inserted at different times or cascaded from different levels, so relying on
    bucket-append order alone would not match the heap's ``(deadline, seq)``
    tiebreak. ``expire_due`` therefore collects the due live entries for the pass
    and returns them ordered by ``(deadline, seq)`` -- identical to the heap's
    ``k`` extract-mins -- guaranteeing exact parity.
    """

    #: Number of buckets per wheel level. Power of two keeps the mapping cheap.
    DEFAULT_SLOTS = 256
    #: Tick granularity in the same integer units as ``deadline`` / ``now``.
    DEFAULT_TICK = 1

    def __init__(self, tick: int = DEFAULT_TICK, slots: int = DEFAULT_SLOTS) -> None:
        if tick < 1:
            raise ValueError("tick must be >= 1")
        if slots < 2:
            raise ValueError("slots must be >= 2")
        self._tick = tick
        self._slots = slots
        # levels[L] is a list of `slots` buckets; each bucket is a list of
        # (deadline, seq, timer_id) tuples. Append order is preserved for
        # stability; the final expiry order is (deadline, seq) at drain time.
        self._levels: List[List[List[Tuple[int, int, int]]]] = []
        # Authoritative live registration: timer_id -> (deadline, seq).
        self._live: Dict[int, Tuple[int, int]] = {}
        self._seq: int = 0
        # Current logical tick floor of the wheel (base == cur_tick * tick).
        self._cur_tick: int = 0
        # Entries already due when placed (deadline <= base): a flat pending
        # list, drained first, in (deadline, seq) order.
        self._overdue: List[Tuple[int, int, int]] = []
        self.counts = TimerOpCounts()

    # -- introspection -----------------------------------------------------

    def pending_count(self) -> int:
        """Number of ids still registered (not expired, not cancelled)."""
        return len(self._live)

    def is_registered(self, timer_id: int) -> bool:
        return timer_id in self._live

    def __len__(self) -> int:
        return len(self._live)

    # -- internal wheel geometry -------------------------------------------

    def _ensure_level(self, level: int) -> None:
        while len(self._levels) <= level:
            self._levels.append([[] for _ in range(self._slots)])

    def _place(self, entry: Tuple[int, int, int]) -> None:
        """Insert an entry tuple into the correct (level, bucket).

        An entry whose tick is at or before ``cur_tick`` is already due and goes
        to the overdue list (it will be returned on the next drain if its exact
        deadline is <= now). Otherwise the level is the smallest ``L`` such that
        the tick-distance from ``cur_tick`` fits inside level ``L``'s span, and
        the bucket is the distance measured in that level's coarser sub-ticks.
        """
        deadline = entry[0]
        target_tick = deadline // self._tick
        ticks = target_tick - self._cur_tick
        if ticks < 0:
            # Tick strictly in the past: due (exact deadline re-checked at drain).
            self._overdue.append(entry)
            return
        if ticks == 0:
            # Exactly the current tick: goes to level 0's current bucket so the
            # final drain in expire_due reaps it against the exact deadline.
            self._ensure_level(0)
            self._levels[0][self._cur_tick % self._slots].append(entry)
            return
        level = 0
        span = self._slots  # ticks covered by level 0
        while ticks >= span:
            level += 1
            span *= self._slots
        self._ensure_level(level)
        # Sub-tick width of this level (ticks per bucket step) == slots**level.
        step = span // self._slots
        # Absolute bucket index of the target tick within this level's wheel.
        bucket = (target_tick // step) % self._slots
        self._levels[level][bucket].append(entry)

    def _reap_current_level0_bucket(self, due: List[Tuple[int, int, int]], now: int) -> None:
        """Move the current level-0 bucket's live entries into ``due``.

        Called for each whole tick the clock leaves behind, and once more for the
        final (possibly partial) tick. For a fully-passed tick every entry is due
        by construction; for the final tick each entry is kept in place if its
        exact ``deadline > now``. Stale entries are dropped.
        """
        if not self._levels:
            return
        idx = self._cur_tick % self._slots
        bucket = self._levels[0][idx]
        if not bucket:
            return
        live = self._live
        keep: List[Tuple[int, int, int]] = []
        for entry in bucket:
            live_entry = live.get(entry[2])
            if live_entry is None or live_entry != (entry[0], entry[1]):
                continue  # stale: cancelled or superseded
            if entry[0] <= now:
                due.append(entry)
            else:
                keep.append(entry)
        self._levels[0][idx] = keep

    def _cascade_level(self, level: int) -> None:
        """Overflow level ``level``'s current bucket down into lower levels.

        Called when the wheel below has completed a full rotation into this
        level's next bucket. Re-places every live entry (recomputing its
        now-closer level/bucket); stale entries are dropped so buckets do not
        grow without bound.
        """
        self._ensure_level(level)
        step = self._slots ** level
        idx = (self._cur_tick // step) % self._slots
        bucket = self._levels[level][idx]
        if not bucket:
            return
        self._levels[level][idx] = []
        for entry in bucket:
            live_entry = self._live.get(entry[2])
            if live_entry is None or live_entry != (entry[0], entry[1]):
                continue  # stale: cancelled or superseded
            self._place(entry)

    # -- mutation ----------------------------------------------------------

    def insert(self, deadline: int, timer_id: int) -> None:
        """Register ``timer_id`` to fire at ``deadline`` (O(1) amortized).

        Re-inserting an already-registered id reschedules it: the old bucket
        tuple becomes stale (skipped later) and a fresh tuple is placed.
        """
        self._seq += 1
        seq = self._seq
        self._live[timer_id] = (deadline, seq)
        self._place((deadline, seq, timer_id))
        self.counts.inserts += 1
        # O(1) amortized: a single placement, no scan.
        self.counts.comparisons += 1

    def cancel(self, timer_id: int) -> bool:
        """Cancel a registered id (O(1)). Returns True if it was live.

        Lazy: the bucket tuple is left in place and skipped when it surfaces,
        mirroring the C poller dropping a timer whose thread is no longer parked.
        """
        self.counts.cancels += 1
        if timer_id in self._live:
            del self._live[timer_id]
            return True
        return False

    def expire_due(self, now: int) -> List[int]:
        """Pop and return ids with ``deadline <= now`` in expiry order.

        Order is nondecreasing by deadline, FIFO among equal deadlines (matches
        the heap's ``(deadline, seq)`` tiebreak). Stale tuples (cancelled or
        rescheduled ids) are skipped without being returned. Expired ids are
        unregistered (root retention ends). ``now`` only moves forward, mirroring
        the monotonic ``pcc_vthread_now_ms`` snapshots.
        """
        self.counts.expiries += 1
        due: List[Tuple[int, int, int]] = []
        live = self._live

        # The overdue holding list: entries whose tick was already reached when
        # placed/cascaded. Their exact deadline decides due-ness now.
        keep_overdue: List[Tuple[int, int, int]] = []
        for entry in self._overdue:
            live_entry = live.get(entry[2])
            if live_entry is None or live_entry != (entry[0], entry[1]):
                continue  # stale
            if entry[0] <= now:
                due.append(entry)
            else:
                keep_overdue.append(entry)
        self._overdue = keep_overdue

        # Advance the clock to now's tick, one tick at a time. Each whole tick
        # the clock leaves behind is fully in the past -> its level-0 bucket is
        # unconditionally due; the final (possibly partial) tick is drained
        # against the exact deadline.
        target_tick = now // self._tick if now >= 0 else 0
        while self._cur_tick < target_tick:
            # Reap the bucket the clock is leaving (a whole passed tick).
            self._reap_current_level0_bucket(due, now)
            self._cur_tick += 1
            # When level 0 wraps, overflow level 1 down; when level 1 wraps,
            # overflow level 2; and so on up the hierarchy.
            level = 1
            wrap = self._slots
            while (self._cur_tick % wrap) == 0 and level < len(self._levels):
                self._cascade_level(level)
                level += 1
                wrap *= self._slots
        # Drain the final (current) tick against the exact deadline.
        self._reap_current_level0_bucket(due, now)

        # FIFO among equal deadlines == (deadline, seq) order, identical to the
        # heap's k extract-mins. The sort cost is O(k log k) over the k due
        # entries, the same asymptotic the heap pays for the same k pops.
        due.sort(key=lambda e: (e[0], e[1]))
        out: List[int] = []
        for _deadline, _seq, timer_id in due:
            del live[timer_id]
            out.append(timer_id)
            self.counts.comparisons += 1
        return out


def _sift_up_cost(n: int) -> int:
    """Worst-case parent comparisons for a push into a heap of size n."""
    if n <= 1:
        return 0
    return _int_log2(n)


def _sift_down_cost(n: int) -> int:
    """Worst-case child comparisons for a pop from a heap of size n."""
    if n <= 1:
        return 0
    return _int_log2(n)


def _int_log2(n: int) -> int:
    """floor(log2(n)) for n >= 1, computed without float rounding."""
    bits = 0
    v = n
    while v > 1:
        v >>= 1
        bits += 1
    return bits


__all__ = [
    "TimerOpCounts",
    "MinHeapTimerQueue",
    "NaiveSortedListTimerQueue",
    "HashedTimingWheelTimerQueue",
]
