"""pcc-Python port of py_io_waitset.c (scalable vthread IO waitset structure).

Mirror of the C runtime structure ``pcc/runtime/src/py_io_waitset.c`` and of
the CPU-only oracle ``pcc/vthread/io_waitset_oracle.py`` (``PollWaitSet`` +
``KqueueSimWaitSet``). The dependency-free C twin is embedded by production
``pcc_threads.c``: it owns Darwin kqueue or the unique-fd live-poll fallback,
while stable scheduler nodes retain per-vthread GC roots. See
``docs/design/pcc-vthread-oracles.md``.

Three named readiness backends, one abstraction:

* POLL FALLBACK -- the level-triggered fallback. Readiness is FED explicitly
  through :meth:`set_ready` (standing in for ``poll(2)``'s revents), so it is
  deterministic and diffable against the oracle's ``PollWaitSet`` without live
  fds. On each :meth:`wait` every registered fd is rescanned against its fed
  readiness mask, exactly like the C ``while (*cur != NULL)`` loop. Available
  everywhere.

* DARWIN KQUEUE -- the real ``kqueue``/``kevent(2)`` notifier over LIVE fds is
  owned by ``freestanding_io_waitset.py``, including its EVFILT_USER interrupt
  channel. This deterministic host mirror does NOT issue those syscalls.
  Requesting it reports a machine-readable skip, mirroring how the oracle's
  ``real_kqueue_backend()`` returns ``SkippedReason`` and how the standalone C
  ``pcc_io_waitset_real_kqueue_skip`` reports the same path when kqueue is
  unavailable.

* LINUX EPOLL -- the registration/readiness semantics are represented by
  :class:`EpollIoWaitSet` for deterministic oracle work.  The production
  freestanding pcc-Python owner uses compiler-owned
  ``epoll_create1``/``epoll_ctl``/``epoll_wait`` lowering on Linux x86_64.
  This host-executed mirror never issues live syscalls and therefore reports
  its own live capability as unavailable rather than borrowing host Python.

Faithfulness note
-----------------
This port is written in the pcc-Python subset (classes + list + dict + int),
also valid CPython, so a host test can exercise it directly against the oracle,
exactly like ``tests/vthread/test_timer_oracle.py`` exercises the oracle. It
preserves the same observable semantics the C poll fallback guarantees:

  * interest-mask filtering (only requested bits + always-reported error bits);
  * inclusive deadline timeout (``deadline <= now``; ``deadline < 0`` infinite);
  * ready wins over timeout at the same tick;
  * one-shot delivery: a delivered / timed-out fd is unregistered.
"""

__pcc_runtime_port__ = True

# POSIX poll event bits, matching pcc_vthread_fd_ready's use of poll(2) and the
# oracle's constants. Inlined at use sites where needed (pcc-Python module-level
# int constants can be zeroed in stripped library .o builds).
PCC_IO_POLLIN = 0x0001
PCC_IO_POLLOUT = 0x0004
PCC_IO_POLLERR = 0x0008
PCC_IO_POLLHUP = 0x0010
PCC_IO_POLLNVAL = 0x0020

# Bits reported even when not requested (C: revents & (events | POLLERR |
# POLLHUP | POLLNVAL)).
PCC_IO_ALWAYS_REPORTED = 0x0008 | 0x0010 | 0x0020

PCC_IO_WAITSET_BACKEND_POLL = 0
PCC_IO_WAITSET_BACKEND_KQUEUE = 1
PCC_IO_WAITSET_BACKEND_EPOLL = 2


class IoReadyEvent:
    """One delivered readiness event (mirrors oracle ReadyEvent / C
    PccIoReadyEvent)."""

    def __init__(self, fd: int, events: int) -> None:
        self.fd = fd
        self.events = events


class IoWaitResult:
    """Result of one :meth:`PollIoWaitSet.wait` drain.

    ``ready`` is a list of :class:`IoReadyEvent`; ``timed_out`` is a list of
    fds. They are disjoint per fd, matching the C poller and the oracle.
    """

    def __init__(self) -> None:
        self.ready = []  # type: list
        self.timed_out = []  # type: list


class PollIoWaitSet:
    """Level-triggered poll fallback (mirrors the C poll backend + oracle
    PollWaitSet).

    Rescans every registered fd on each :meth:`wait`, exactly like the C
    ``while (*cur != NULL)`` loop calling ``pcc_vthread_fd_ready`` per entry.
    An fd that stays ready and registered is reported on every wait.
    """

    def __init__(self) -> None:
        # fd -> [interest, deadline, ready_mask]. deadline < 0 == infinite.
        self._regs = {}  # type: dict

    def add(self, fd: int, interest: int, deadline: int, edge: int) -> int:
        # Poll fallback is inherently level-triggered; edge is ignored here
        # (matches the oracle and the C poll backend). Re-adding updates.
        existing = 0
        if fd in self._regs:
            existing = self._regs[fd][2]
        self._regs[fd] = [interest, deadline, existing]
        return 0

    def remove(self, fd: int) -> int:
        if fd in self._regs:
            del self._regs[fd]
            return 1
        return 0

    def count(self) -> int:
        return len(self._regs)

    def set_ready(self, fd: int, events: int) -> None:
        if fd in self._regs:
            reg = self._regs[fd]
            reg[2] = reg[2] | events

    def clear_ready(self, fd: int) -> None:
        if fd in self._regs:
            self._regs[fd][2] = 0

    def wait(self, now: int) -> IoWaitResult:
        result = IoWaitResult()
        # Snapshot keys so removal during the scan is safe.
        fds = []  # type: list
        for fd in self._regs:
            fds.append(fd)
        for fd in fds:
            if fd not in self._regs:
                continue
            reg = self._regs[fd]
            interest = reg[0]
            deadline = reg[1]
            ready_mask = reg[2]
            hit = ready_mask & (interest | (0x0008 | 0x0010 | 0x0020))
            expired = deadline >= 0 and deadline <= now
            if hit != 0:
                result.ready.append(IoReadyEvent(fd, hit))
                del self._regs[fd]
            elif expired:
                result.timed_out.append(fd)
                del self._regs[fd]
        return result


class EpollIoWaitSet:
    """Deterministic one-shot model of the Linux epoll backend.

    This class models registration, interest filtering, error delivery and
    deadline arbitration only.  ``set_ready`` stands in for epoll events; it
    does not issue live syscalls.  That separation makes the absent production
    syscall route explicit while allowing the scheduler contract to be tested.
    """

    def __init__(self) -> None:
        # fd -> [interest, deadline, ready_mask, edge, generation]
        self._regs = {}  # type: dict
        self._next_generation = 0

    def add(self, fd: int, interest: int, deadline: int, edge: int) -> int:
        ready = 0
        if fd in self._regs:
            ready = self._regs[fd][2]
        self._next_generation += 1
        self._regs[fd] = [
            interest,
            deadline,
            ready,
            1 if edge else 0,
            self._next_generation,
        ]
        return 0

    def remove(self, fd: int) -> int:
        if fd in self._regs:
            del self._regs[fd]
            return 1
        return 0

    def count(self) -> int:
        return len(self._regs)

    def set_ready(self, fd: int, events: int) -> None:
        if fd in self._regs:
            self.set_ready_token(fd, self._regs[fd][4], events)

    def generation(self, fd: int) -> int:
        if fd not in self._regs:
            return 0
        return self._regs[fd][4]

    def set_ready_token(self, fd: int, generation: int, events: int) -> None:
        """Feed a kernel event only when its registration token is current."""
        if fd in self._regs and self._regs[fd][4] == generation:
            self._regs[fd][2] = self._regs[fd][2] | events

    def clear_ready(self, fd: int) -> None:
        if fd in self._regs:
            self._regs[fd][2] = 0

    def wait(self, now: int) -> IoWaitResult:
        result = IoWaitResult()
        fds = []  # type: list
        for fd in self._regs:
            fds.append(fd)
        for fd in fds:
            if fd not in self._regs:
                continue
            reg = self._regs[fd]
            hit = reg[2] & (reg[0] | (0x0008 | 0x0010 | 0x0020))
            expired = reg[1] >= 0 and reg[1] <= now
            if hit != 0:
                result.ready.append(IoReadyEvent(fd, hit))
                del self._regs[fd]
            elif expired:
                result.timed_out.append(fd)
                del self._regs[fd]
        return result


def kqueue_available() -> int:
    """Whether the real kqueue backend is available in THIS runtime tier.

    This deterministic host mirror never provides the real kqueue syscall
    path, so this always returns 0 here. The production freestanding
    pcc-Python owner reports the target capability independently.
    """
    return 0


def real_kqueue_skip():
    """Machine-readable skip marker for the real-kqueue path in this port.

    Mirrors the oracle's ``real_kqueue_backend()`` SkippedReason and the C
    ``pcc_io_waitset_real_kqueue_skip``. Returns ``[path, reason]``.
    """
    return [
        "io_waitset.real_kqueue",
        (
            "real kqueue/kevent requires live-fd syscalls; the pcc-Python "
            "host mirror has no kqueue backend. The production freestanding "
            "runtime owns EVFILT_READ/EVFILT_WRITE via kevent(2) on Darwin."
        ),
    ]


def epoll_available() -> int:
    """Whether this runtime tier can issue live epoll syscalls."""
    return 0


def real_epoll_skip():
    return [
        "io_waitset.real_epoll",
        (
            "This deterministic host mirror does not issue live syscalls; "
            "the freestanding pcc-Python runtime owns Linux x86_64 epoll."
        ),
    ]


def backend_label(backend: int) -> str:
    if backend == 0:
        return "poll"
    if backend == 1:
        return "kqueue"
    if backend == 2:
        return "epoll"
    return "unknown"


def default_backend(platform_name: str, live_epoll: int = 0) -> int:
    """Choose only a backend whose live capability has been proven."""
    if platform_name == "darwin":
        return 1
    if platform_name == "linux" and live_epoll != 0:
        return 2
    return 0
