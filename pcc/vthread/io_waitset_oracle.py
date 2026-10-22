"""CPU-only oracle for the virtual-thread IO waitset abstraction.

This module is an ORACLE, not the runtime. It designs and validates the
readiness model that a later C slice will mirror, replacing the current
per-poll O(n) linked-list scan in ``pcc/runtime/src/pcc_threads.c``
(``pcc_vthread_poll_add_locked`` prepends to ``pcc_vthread_poll_queue``;
``py_virtual_thread_poll_io`` walks the whole list calling ``poll(2)`` per
entry via ``pcc_vthread_fd_ready``).

Claim boundary
--------------
* CPU-only Python oracle. Not the runtime C implementation.
* The real ``kqueue``/``epoll`` integration is NOT implemented here; the
  platform path reports :class:`SkippedReason` (``SKIPPED_WITH_REASON``).
* No real sockets/fds, no syscalls, no threads. Readiness is fed explicitly
  through :meth:`set_ready` so the two waitset implementations can be diffed
  deterministically. ``now`` is an explicit logical clock, matching how the C
  poller reads ``pcc_vthread_now_ms()`` snapshots.
* Does NOT prove a 1M-fd result; it proves the poll-fallback and the
  kqueue-style readiness model agree on the same readiness sequence and honor
  add/remove/timeout invariants.

Two readiness models, one abstraction
--------------------------------------
Both implement :class:`IOWaitSet`:

* :class:`PollWaitSet` — the *level-triggered* fallback. On each
  :meth:`wait`, it re-scans every registered fd against its current readiness
  mask, mirroring the C poll-per-entry loop. As long as an fd stays readable
  and is still registered, every ``wait`` reports it. This is the semantics of
  ``poll(2)``/``select`` and of the current C runtime.

* :class:`KqueueSimWaitSet` — a pure-Python simulation of a readiness-notifier
  (kqueue/epoll) supporting BOTH level- and edge-triggered filters. It keeps a
  pending-event set that a readiness *transition* populates. In level mode it
  behaves exactly like the poll fallback (re-arms while ready), so the two
  agree. In edge mode a readiness event is delivered once per transition.

Event flags mirror the POSIX ``poll`` bits the C runtime uses
(``POLLIN``/``POLLOUT`` plus the always-reported error bits
``POLLERR``/``POLLHUP``/``POLLNVAL``), so a later C slice can map them onto
kqueue filters (``EVFILT_READ``/``EVFILT_WRITE``) without redefining semantics.

Level vs edge, made observable (persistent registrations)
---------------------------------------------------------
The default registration is *one-shot* (``oneshot=True``): the fd is unregistered
the moment it is delivered, mirroring the current C poller's park/unpark. That
model cannot *show* the level-vs-edge distinction, because a fd never survives to
a second ``wait``. Registering with ``oneshot=False`` keeps the fd across
deliveries, exposing the defining semantics:

* level (``PollWaitSet``, or ``KqueueSimWaitSet`` without ``edge``): a fd that
  stays ready and registered is re-reported on *every* ``wait`` until it is
  drained (:meth:`clear_ready`) or removed. This is exactly what CPython's
  level-triggered ``selectors``/``poll(2)`` does — an unread readable fd shows up
  on each ``select`` call.
* edge (``KqueueSimWaitSet`` with ``edge=True``): a persistently-ready fd fires
  *once* per false->true transition. Staying ready does not re-fire; the fd must
  be cleared and set again to fire a second time.

So for a persistently-ready, non-one-shot fd, level delivers on every wait while
edge delivers exactly once — the two intentionally *disagree*, which is the point
of the distinction.

EINTR / spurious-wakeup retry parity
------------------------------------
A real ``poll``/``kevent`` can return early with no events because a signal
interrupted the syscall (``EINTR``) or the kernel woke the thread spuriously.
Under PEP 475 CPython auto-retries on ``EINTR``, so from the caller's view an
interrupted wait simply returns *empty* and the caller loops again. The invariant
a correct waitset must preserve: an interrupted wait consumes nothing — no
readiness is drained, no deadline is treated as expired, no fd is unregistered —
so the retried wait sees exactly the same pending state and delivers each event
exactly once (never lost, never doubled). :meth:`IOWaitSet.wait` models this via
``interrupted=True``, and both waitset models must agree on the resulting
sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# POSIX poll event bits, matching pcc_vthread_fd_ready's use of poll(2).
POLLIN = 0x0001
POLLOUT = 0x0004
POLLERR = 0x0008
POLLHUP = 0x0010
POLLNVAL = 0x0020

# Bits that are reported even when not explicitly requested (C:
# revents & (events | POLLERR | POLLHUP | POLLNVAL)).
ALWAYS_REPORTED = POLLERR | POLLHUP | POLLNVAL


@dataclass(frozen=True)
class SkippedReason:
    """Marks a code path deliberately not implemented in this CPU-only oracle."""

    path: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"SKIPPED_WITH_REASON[{self.path}]: {self.reason}"


def real_kqueue_backend() -> SkippedReason:
    """The real-kqueue path is out of scope for this oracle.

    A later runtime C slice implements ``EVFILT_READ``/``EVFILT_WRITE`` via
    ``kevent(2)`` on Darwin/BSD (and ``epoll_wait`` on Linux). This oracle only
    validates the abstract readiness model that the C slice mirrors.
    """
    return SkippedReason(
        path="io_waitset.real_kqueue",
        reason=(
            "real kqueue/epoll requires syscalls and live fds; this is a "
            "CPU-only oracle. The C runtime slice owns EVFILT_READ/EVFILT_WRITE "
            "via kevent(2) (Darwin/BSD) and epoll_wait (Linux)."
        ),
    )


@dataclass
class ReadyEvent:
    fd: int
    events: int  # bits actually ready and requested (+ ALWAYS_REPORTED bits)


@dataclass
class _Registration:
    fd: int
    interest: int  # requested event mask
    deadline: Optional[int]  # logical-clock deadline; None == infinite (C: -1)
    edge: bool  # edge-triggered (kqueue-sim only; poll fallback ignores)
    oneshot: bool = True  # unregister on delivery (park/unpark); False = persistent


@dataclass
class WaitResult:
    """Result of one :meth:`IOWaitSet.wait` drain.

    ``ready`` and ``timed_out`` are disjoint per fd: an fd is drained either
    because it became ready or because its deadline passed. This mirrors the C
    poller, where an expired entry is treated as ready==1 and removed.
    """

    ready: List[ReadyEvent] = field(default_factory=list)
    timed_out: List[int] = field(default_factory=list)  # fds

    def woken_fds(self) -> List[int]:
        return [e.fd for e in self.ready] + list(self.timed_out)


class IOWaitSet:
    """Abstract waitset. Concrete models share add/remove/count/wait shape."""

    def add(
        self,
        fd: int,
        interest: int,
        deadline: Optional[int] = None,
        edge: bool = False,
        oneshot: bool = True,
    ) -> None:
        raise NotImplementedError

    def remove(self, fd: int) -> bool:
        raise NotImplementedError

    def wait_count(self) -> int:
        raise NotImplementedError

    def set_ready(self, fd: int, events: int) -> None:
        """Feed readiness for a registered fd (test/driver hook).

        Replaces the real ``poll(2)`` revents. In edge models this records a
        transition; in level models it sets the persistent ready mask.
        """
        raise NotImplementedError

    def clear_ready(self, fd: int) -> None:
        raise NotImplementedError

    def wait(self, now: int, interrupted: bool = False) -> WaitResult:
        """Drain one readiness pass.

        ``interrupted=True`` models an ``EINTR``/spurious wakeup: the wait
        returns empty and consumes nothing (no readiness drained, no deadline
        expired, no fd unregistered), so a retry sees the identical state.
        """
        raise NotImplementedError


class PollWaitSet(IOWaitSet):
    """Level-triggered poll fallback (mirrors the current C runtime).

    Re-scans every registered fd on each :meth:`wait`, exactly like the C
    ``while (*cur != NULL)`` loop calling ``pcc_vthread_fd_ready`` per entry.
    An fd that stays ready and registered is reported on every wait.
    """

    def __init__(self) -> None:
        self._regs: Dict[int, _Registration] = {}
        self._ready_mask: Dict[int, int] = {}
        self.scan_steps = 0  # instrumentation: per-entry rescans (the O(n) cost)

    def add(self, fd, interest, deadline=None, edge=False, oneshot=True):
        # Poll fallback is inherently level-triggered; edge is a no-op here.
        self._regs[fd] = _Registration(
            fd, interest, deadline, edge=False, oneshot=oneshot
        )

    def remove(self, fd):
        self._ready_mask.pop(fd, None)
        return self._regs.pop(fd, None) is not None

    def wait_count(self):
        return len(self._regs)

    def set_ready(self, fd, events):
        self._ready_mask[fd] = self._ready_mask.get(fd, 0) | events

    def clear_ready(self, fd):
        self._ready_mask[fd] = 0

    def wait(self, now, interrupted=False):
        result = WaitResult()
        if interrupted:
            # EINTR / spurious wakeup: return empty, consume nothing. The
            # linear scan does not even run (the syscall was interrupted before
            # reporting), so a retry sees the identical registration state.
            return result
        for fd in list(self._regs.keys()):
            self.scan_steps += 1  # the linear per-entry cost being replaced
            reg = self._regs[fd]
            expired = reg.deadline is not None and reg.deadline <= now
            mask = self._ready_mask.get(fd, 0)
            hit = mask & (reg.interest | ALWAYS_REPORTED)
            if expired and hit == 0:
                result.timed_out.append(fd)
                del self._regs[fd]
                self._ready_mask.pop(fd, None)
            elif hit != 0:
                result.ready.append(ReadyEvent(fd=fd, events=hit))
                if reg.oneshot:
                    # park/unpark: delivered fd leaves the set.
                    del self._regs[fd]
                    self._ready_mask.pop(fd, None)
                # else: level-triggered persistent registration. The ready mask
                # stays set, so the next wait re-reports it (matching CPython's
                # level-triggered selectors: an undrained readable fd fires on
                # every select until clear_ready()/remove()).
        return result


class KqueueSimWaitSet(IOWaitSet):
    """Pure-Python simulation of a kqueue/epoll readiness notifier.

    Maintains a *pending event queue* driven by readiness transitions instead
    of rescanning all fds. Supports level- and edge-triggered registrations:

    * level (default): re-arms while the fd remains ready, so behaves exactly
      like :class:`PollWaitSet` and agrees with it on the readiness sequence.
    * edge: a transition into "ready" delivers exactly one event; the fd must
      re-transition (clear then set) to fire again.

    This models what a real ``kqueue`` gives: O(ready) delivery, no full-set
    rescan. The oracle asserts the level-mode sequence equals the poll
    fallback's.
    """

    def __init__(self) -> None:
        self._regs: Dict[int, _Registration] = {}
        self._ready_mask: Dict[int, int] = {}
        # fds with a pending (undelivered) readiness event.
        self._pending: List[int] = []
        self._pending_set: set[int] = set()

    def add(self, fd, interest, deadline=None, edge=False, oneshot=True):
        self._regs[fd] = _Registration(
            fd, interest, deadline, edge=edge, oneshot=oneshot
        )
        # A registration that is already ready arms immediately (kqueue posts
        # the current level state when you add a level-triggered filter).
        mask = self._ready_mask.get(fd, 0)
        if mask & (interest | ALWAYS_REPORTED):
            self._arm(fd)

    def remove(self, fd):
        self._ready_mask.pop(fd, None)
        self._disarm(fd)
        return self._regs.pop(fd, None) is not None

    def wait_count(self):
        return len(self._regs)

    def _arm(self, fd):
        if fd not in self._pending_set:
            self._pending.append(fd)
            self._pending_set.add(fd)

    def _disarm(self, fd):
        if fd in self._pending_set:
            self._pending_set.discard(fd)
            self._pending = [x for x in self._pending if x != fd]

    def set_ready(self, fd, events):
        prev = self._ready_mask.get(fd, 0)
        new = prev | events
        self._ready_mask[fd] = new
        reg = self._regs.get(fd)
        if reg is None:
            return
        relevant = reg.interest | ALWAYS_REPORTED
        was_ready = (prev & relevant) != 0
        now_ready = (new & relevant) != 0
        if reg.edge:
            # Edge: arm only on the false->true transition.
            if now_ready and not was_ready:
                self._arm(fd)
        else:
            # Level: armed whenever currently ready.
            if now_ready:
                self._arm(fd)

    def clear_ready(self, fd):
        self._ready_mask[fd] = 0
        reg = self._regs.get(fd)
        if reg is not None and not reg.edge:
            # Level: no longer ready -> disarm any pending event.
            self._disarm(fd)

    def wait(self, now, interrupted=False):
        result = WaitResult()
        if interrupted:
            # EINTR / spurious wakeup: return empty and consume nothing. The
            # pending queue, ready masks, deadlines, and registrations are all
            # left intact, so the retried wait delivers exactly the same events
            # (never lost because kevent was interrupted, never doubled because
            # we did not partially drain).
            return result
        # 1) Deliver pending readiness events (O(ready), not O(n)).
        pending = self._pending
        self._pending = []
        self._pending_set = set()
        delivered: set[int] = set()
        for fd in pending:
            reg = self._regs.get(fd)
            if reg is None:
                continue
            mask = self._ready_mask.get(fd, 0)
            hit = mask & (reg.interest | ALWAYS_REPORTED)
            if hit == 0:
                continue
            result.ready.append(ReadyEvent(fd=fd, events=hit))
            delivered.add(fd)
            if reg.oneshot:
                del self._regs[fd]
                self._ready_mask.pop(fd, None)
            elif not reg.edge:
                # Persistent level filter: still ready, so re-arm for the next
                # wait (kqueue re-posts a level filter that stays satisfied).
                self._arm(fd)
            # Persistent EDGE filter: leave disarmed. It only re-fires on the
            # next false->true transition (clear_ready then set_ready), which is
            # the whole point of the level-vs-edge distinction.
        # 2) Timeouts: only fds with a deadline can time out, so scan those.
        # An fd that was delivered ready *this* wait must NOT also time out this
        # tick -- ready wins over timeout at the same tick, matching the poll
        # fallback's per-fd elif structure. This only matters for persistent
        # (non-one-shot) registrations, since one-shot fds are already gone.
        for fd in list(self._regs.keys()):
            if fd in delivered:
                continue
            reg = self._regs[fd]
            if reg.deadline is not None and reg.deadline <= now:
                result.timed_out.append(fd)
                del self._regs[fd]
                self._ready_mask.pop(fd, None)
                self._disarm(fd)
        return result


__all__ = [
    "POLLIN",
    "POLLOUT",
    "POLLERR",
    "POLLHUP",
    "POLLNVAL",
    "ALWAYS_REPORTED",
    "SkippedReason",
    "real_kqueue_backend",
    "ReadyEvent",
    "WaitResult",
    "IOWaitSet",
    "PollWaitSet",
    "KqueueSimWaitSet",
]
