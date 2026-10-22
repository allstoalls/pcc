"""pcc-Python port of py_timer_heap.c (scalable vthread timer structure).

Mirror of the C runtime structure ``pcc/runtime/src/py_timer_heap.c`` and of
the CPU-only oracle ``pcc/vthread/timer_oracle.py::MinHeapTimerQueue``. All
three implement the SAME algorithm so they can be diffed against one another:

  * a binary min-heap keyed on ``(deadline, seq)`` (insert O(log n), pop O(log
    n), peek O(1)); and
  * lazy cancellation via an authoritative ``timer_id -> (deadline, seq)`` live
    map: ``cancel`` is O(1) amortized and only marks the id dead; the stale
    heap slot is skipped when it surfaces at the root during ``pop_expired``.

The dependency-free C twin is now embedded in the production scheduler in
``pcc_threads.c`` and has replaced its O(n)-insert sorted timer list (see
``docs/design/pcc-vthread-oracles.md``). This pcc-Python class remains the
executable semantic mirror: the production archive uses the C twin as its
low-level heap helper while all parked-thread ownership stays in the one
slot-based scheduler-root-handle contract.

Faithfulness note
-----------------
This port is written in the pcc-Python subset (classes + list + dict + int),
which is also valid CPython, so a host test can exercise it directly against
the oracle exactly like ``tests/vthread/test_timer_oracle.py`` exercises the
oracle. The C mirror keeps its own hand-rolled binary heap + open-addressing
live map (the pcc-Python heap here leans on ``list`` sift primitives written
out longhand so the pcc frontend does not need ``heapq``). Both preserve the
same observable semantics:

  * expiry order nondecreasing by deadline, FIFO among equal deadlines;
  * ``pop_expired(now)`` drains every id with ``deadline <= now`` (inclusive);
  * root retention until expired or cancelled;
  * cancelled / superseded entries are dropped without being returned.
"""

__pcc_runtime_port__ = True


class MinHeapTimerQueue:
    """Binary min-heap timer queue with lazy cancellation."""

    def __init__(self) -> None:
        # Parallel arrays form the heap (avoids tuple allocation churn and maps
        # cleanly onto the C node array). Index i holds one timer node.
        self._deadline = []  # type: list
        self._seq = []  # type: list
        self._timer_id = []  # type: list
        # Authoritative live registration: timer_id -> [deadline, seq].
        self._live = {}  # type: dict
        self._seq_counter = 0

    # -- introspection -----------------------------------------------------

    def size(self) -> int:
        return len(self._live)

    def is_registered(self, timer_id: int) -> bool:
        return timer_id in self._live

    # -- heap primitives ---------------------------------------------------

    def _less(self, i: int, j: int) -> bool:
        di = self._deadline[i]
        dj = self._deadline[j]
        if di < dj:
            return True
        if di > dj:
            return False
        return self._seq[i] < self._seq[j]

    def _swap(self, i: int, j: int) -> None:
        d = self._deadline[i]
        self._deadline[i] = self._deadline[j]
        self._deadline[j] = d
        s = self._seq[i]
        self._seq[i] = self._seq[j]
        self._seq[j] = s
        t = self._timer_id[i]
        self._timer_id[i] = self._timer_id[j]
        self._timer_id[j] = t

    def _sift_up(self, i: int) -> None:
        done = False
        while i > 0 and not done:
            parent = (i - 1) // 2
            if self._less(i, parent):
                self._swap(i, parent)
                i = parent
            else:
                done = True

    def _sift_down(self, i: int) -> None:
        n = len(self._deadline)
        done = False
        while not done:
            left = 2 * i + 1
            right = left + 1
            smallest = i
            if left < n and self._less(left, smallest):
                smallest = left
            if right < n and self._less(right, smallest):
                smallest = right
            if smallest == i:
                done = True
            else:
                self._swap(i, smallest)
                i = smallest

    def _pop_root(self) -> None:
        last = len(self._deadline) - 1
        self._swap(0, last)
        # Drop the (old-root) tail entry.
        del self._deadline[last]
        del self._seq[last]
        del self._timer_id[last]
        if len(self._deadline) > 0:
            self._sift_down(0)

    def _root_is_stale(self) -> bool:
        tid = self._timer_id[0]
        if tid not in self._live:
            return True
        entry = self._live[tid]
        if entry[0] != self._deadline[0]:
            return True
        if entry[1] != self._seq[0]:
            return True
        return False

    # -- mutation ----------------------------------------------------------

    def insert(self, deadline: int, timer_id: int) -> None:
        """Register timer_id to fire at deadline (O(log n)).

        Re-inserting a registered id reschedules it: the live entry is
        overwritten and the old heap node becomes stale (skipped later).
        """
        self._seq_counter = self._seq_counter + 1
        seq = self._seq_counter
        self._live[timer_id] = [deadline, seq]
        i = len(self._deadline)
        self._deadline.append(deadline)
        self._seq.append(seq)
        self._timer_id.append(timer_id)
        self._sift_up(i)

    def cancel(self, timer_id: int) -> bool:
        """Cancel a registered id (O(1) amortized). Returns True if it was live.

        Lazy: only the live entry is removed; the stale heap slot is skipped.
        """
        if timer_id in self._live:
            del self._live[timer_id]
            return True
        return False

    def peek(self, out) -> bool:
        """Peek the soonest live deadline into out[0]. Returns True if any.

        May pop leading stale slots first (amortized O(1)).
        """
        result = False
        done = False
        while len(self._deadline) > 0 and not done:
            if self._root_is_stale():
                self._pop_root()
            else:
                if len(out) > 0:
                    out[0] = self._deadline[0]
                result = True
                done = True
        return result

    def pop_expired(self, now: int) -> list:
        """Return ids with deadline <= now in expiry order.

        Nondecreasing by deadline, FIFO among equal deadlines. Stale
        (cancelled / superseded) slots are skipped. Expired ids are
        unregistered (root retention ends).
        """
        out = []
        done = False
        while len(self._deadline) > 0 and not done:
            if self._root_is_stale():
                self._pop_root()
            elif self._deadline[0] > now:
                done = True
            else:
                tid = self._timer_id[0]
                del self._live[tid]
                self._pop_root()
                out.append(tid)
        return out
