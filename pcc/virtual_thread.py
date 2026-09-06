"""Compiler-recognized virtual-thread helpers.

The pcc Python frontend lowers the scheduling operations natively.  ``call``
is also useful as an executable host-model adapter: under CPython it performs a
plain call, while pcc transparently delegates a returned parking continuation.
All scheduler operations still trap outside pcc lowering.
"""

from __future__ import annotations

from typing import Any, Callable


OUTCOME_PENDING = 0
OUTCOME_RETURNED = 1
OUTCOME_RAISED = 2
OUTCOME_CANCELLED = 3

RECV_VALUE = 1
RECV_SENDER_CLOSED = 2
RECV_RECEIVER_CLOSED = 3

SELECT_LEFT = 0
SELECT_RIGHT = 1


def _trap(name: str) -> None:
    raise NotImplementedError(f"pcc.virtual_thread.{name}() requires pcc lowering")


def spawn(fn: Callable[..., Any], *args: Any) -> Any:
    _trap("spawn")


def call(fn: Callable[..., Any], *args: Any) -> Any:
    """Invoke an open-world callback through pcc's resumable call boundary.

    A normal callback result is returned unchanged.  When a pcc-native
    callback returns a virtual-thread continuation, the compiler drives it and
    forwards every park through the current continuation before returning its
    final value.  This is the explicit boundary used by framework-owned route
    and middleware tables; arbitrary dynamic calls remain fail-closed.
    """
    return fn(*args)


class _Continuation:
    def __init__(self, fn, args, value, done):
        self.fn = fn
        self.args = args
        self.value = value
        self.done = done


def continuation(fn: Callable[..., Any], *args: Any) -> Any:
    """Construct a deferred, closed-world parking call for a factory."""
    return _Continuation(fn, args, None, False)


def completed(value: Any) -> Any:
    """Return a factory result without allocating a suspended-local frame."""
    return _Continuation(None, (), value, True)


def continuation_factory(fn):
    """A nonparking method returning completed(value) or continuation(fn, ...).

    Calls retain ordinary blocking-looking semantics. Under pcc, the body
    keeps the continuation return ABI without becoming another generator.
    Native factories currently require methods with an inferred/Any return.
    A factory cannot directly park or be passed to spawn; deferred targets
    must be ordinary closed-world resumable functions.
    """
    def invoke(*args, **kwargs):
        result = fn(*args, **kwargs)
        if not isinstance(result, _Continuation):
            raise TypeError("continuation factory must return completed() or continuation()")
        while isinstance(result, _Continuation):
            if result.done:
                return result.value
            result = result.fn(*result.args)
        return result
    invoke.__name__ = fn.__name__
    return invoke


def join(vthread: Any) -> Any:
    _trap("join")


def cancel(vthread: Any) -> bool:
    """Request cooperative cancellation; return whether it was accepted."""
    _trap("cancel")


def mpsc(capacity: int) -> Any:
    """Create a bounded multi-producer, single-consumer channel.

    The result is ``(sender, receiver)``.  ``capacity`` counts accepted values
    which have not yet been received and must be positive.
    """
    _trap("mpsc")


def oneshot() -> Any:
    """Create a single-value channel and return ``(sender, receiver)``."""
    _trap("oneshot")


def sender_clone(sender: Any) -> Any:
    """Create a distinct producer token for an open MPSC sender."""
    _trap("sender_clone")


def send(sender: Any, value: Any) -> bool:
    """Sequentially send ``value``, parking while a bounded channel is full.

    ``True`` means the value was accepted by the channel.  ``False`` means
    the receiver closed before acceptance.  Cancelling a still-parked send
    does not accept the value.
    """
    _trap("send")


def recv(receiver: Any) -> Any:
    """Sequentially receive ``(status, value)`` from a channel.

    ``status`` is one of ``RECV_VALUE``, ``RECV_SENDER_CLOSED`` or
    ``RECV_RECEIVER_CLOSED``.  A non-value result carries ``None``, so
    ``None`` itself remains a valid channel value.
    """
    _trap("recv")


def close_sender(sender: Any) -> bool:
    """Close one producer token; return whether this call changed its state."""
    _trap("close_sender")


def close_receiver(receiver: Any) -> bool:
    """Close the single consumer; return whether this call changed its state."""
    _trap("close_receiver")


def select2(left: Any, right: Any) -> Any:
    """Receive from either endpoint as ``(winner, status, value)``.

    ``winner`` is ``SELECT_LEFT`` or ``SELECT_RIGHT``.  When both endpoints
    are ready at entry the left endpoint wins; the losing operation is not
    consumed and is unregistered before the selecting task is made ready.
    """
    _trap("select2")


def run(carrier_count: int, max_steps: int) -> int:
    _trap("run")


def run_until_idle(max_steps: int) -> int:
    _trap("run_until_idle")


def carrier_pool_start(carrier_count: int) -> int:
    _trap("carrier_pool_start")


def carrier_pool_stop() -> int:
    _trap("carrier_pool_stop")


def io_backend() -> int:
    """Return 0=poll, 1=kqueue or 2=epoll for the active runtime."""
    _trap("io_backend")


def current() -> Any:
    _trap("current")


def yield_now() -> None:
    _trap("yield_now")


def sleep_current(delay_ms: int) -> None:
    _trap("sleep_current")


def block_current_on_fd(fd: int, events: int, timeout_ms: int) -> None:
    _trap("block_current_on_fd")


def readable(fd: int) -> None:
    """Park until ``fd`` may be readable; retry the nonblocking operation.

    The caller owns the raw descriptor and must keep it open without numeric
    fd reuse until this wait completes or the task is cancelled.
    """
    _trap("readable")


def writable(fd: int) -> None:
    """Park until ``fd`` may be writable; retry the nonblocking operation.

    The caller owns the raw descriptor and must keep it open without numeric
    fd reuse until this wait completes or the task is cancelled.
    """
    _trap("writable")


def tcp_listen(host: str, port: int, backlog: int = 128) -> int:
    """Create a nonblocking TCP listener owned by the caller."""
    _trap("tcp_listen")


def tcp_accept(listener_fd: int, timeout_ms: int = -1) -> int:
    """Accept one connection, parking the current virtual thread as needed."""
    _trap("tcp_accept")


def tcp_connect(host: str, port: int, timeout_ms: int = -1) -> int:
    """Connect one nonblocking socket, parking until SO_ERROR completes."""
    _trap("tcp_connect")


def tcp_recv(fd: int, max_bytes: int, timeout_ms: int = -1) -> bytes:
    """Receive up to ``max_bytes``; ``b''`` denotes orderly EOF."""
    _trap("tcp_recv")


def tcp_send_all(fd: int, data: bytes, timeout_ms: int = -1) -> None:
    """Send all bytes, preserving partial progress across readiness parks."""
    _trap("tcp_send_all")


def tcp_close(fd: int) -> None:
    """Close a descriptor owned by the sequential TCP caller."""
    _trap("tcp_close")


def result(vthread: Any) -> Any:
    _trap("result")


def exception(vthread: Any) -> Any:
    _trap("exception")


def outcome(vthread: Any) -> int:
    _trap("outcome")


def state(vthread: Any) -> int:
    _trap("state")


def sleep(vthread: Any, delay_ms: int) -> None:
    _trap("sleep")


def block_on_fd(vthread: Any, fd: int, events: int, timeout_ms: int) -> None:
    _trap("block_on_fd")


__all__ = [
    "OUTCOME_PENDING",
    "OUTCOME_RETURNED",
    "OUTCOME_RAISED",
    "OUTCOME_CANCELLED",
    "RECV_VALUE",
    "RECV_SENDER_CLOSED",
    "RECV_RECEIVER_CLOSED",
    "SELECT_LEFT",
    "SELECT_RIGHT",
    "spawn",
    "continuation",
    "completed",
    "continuation_factory",
    "call",
    "join",
    "cancel",
    "mpsc",
    "oneshot",
    "sender_clone",
    "send",
    "recv",
    "close_sender",
    "close_receiver",
    "select2",
    "run",
    "run_until_idle",
    "carrier_pool_start",
    "carrier_pool_stop",
    "io_backend",
    "current",
    "yield_now",
    "sleep_current",
    "block_current_on_fd",
    "readable",
    "writable",
    "tcp_listen",
    "tcp_accept",
    "tcp_connect",
    "tcp_recv",
    "tcp_send_all",
    "tcp_close",
    "result",
    "exception",
    "outcome",
    "state",
    "sleep",
    "block_on_fd",
]
