"""Bounded observations for serial self-backend workers, never backend policy.

Only the outer worker reads its timing flag or allocates PID/item identity.
This process-local context contains scalars only and follows synchronous,
properly nested worker calls. It is not general thread-local emitter tracing.
No compiler inputs, results, exceptions or callbacks are retained here.
"""

import sys
import time


active = False
_context_id = 0
_context_pid = -1
_context_item = -1
_context_phase = ""
_next_context_id = 0


def write_event(
    event: str, phase: str, pid: int, item: int, outcome: str = "none",
) -> None:
    """A returned phase is not artifact validation; inner spans belong to emit."""
    try:
        if event not in ("begin", "complete", "failed"):
            return
        if phase not in (
            "read_normalize", "packed_emit_assemble", "emit", "assemble_encode",
            "assemble", "encode", "object_write", "assembly_write",
            "external_assemble", "result_write",
            "arm_parse", "arm_prepare", "arm_module_emit",
        ):
            return
        if outcome not in ("none", "declined", "returned"):
            outcome = "none"
        clock_ok = False
        now = 0.0
        try:
            now = time.monotonic()
            clock_ok = now > 0 and now < float("inf")
        except BaseException:
            pass
        if not clock_ok:
            now = 0.0
        message = (
            "pcc self backend worker phase event=" + event
            + " phase=" + phase + " pid=" + str(pid) + " item=" + str(item)
            + " monotonic_s=" + str(now) + " clock_ok=" + str(int(clock_ok))
            + " outcome=" + outcome
        )
        if phase in ("arm_parse", "arm_prepare", "arm_module_emit"):
            message += " parent=emit"
        sys.stderr.write(message[:1023] + "\n")
        sys.stderr.flush()
    except BaseException:
        pass


def _record_inner(event: str, phase: str) -> None:
    # State transitions must not depend on a diagnostic sink returning.
    try:
        write_event(event, phase, _context_pid, _context_item)
    except BaseException:
        pass


def open_worker(pid: int, item: int) -> tuple[int, int, int, int, str]:
    """Save the enclosing scalar context and arm only this text callback."""
    global active, _context_id, _context_pid, _context_item, _context_phase
    global _next_context_id
    saved = (0, 0, -1, -1, "")
    try:
        _next_context_id += 1
        saved = (_next_context_id, _context_id, _context_pid, _context_item, _context_phase)
        _context_id = _next_context_id
        _context_pid = pid
        _context_item = item
        _context_phase = "worker"
        active = True
        return saved
    except BaseException:
        close_worker(saved, True)
        return (0, 0, -1, -1, "")


def mask_worker() -> tuple[int, int, int, int, str]:
    """Suspend an active caller for an entirely disabled nested worker."""
    global active, _context_id, _context_pid, _context_item, _context_phase
    saved = (0, 0, -1, -1, "")
    try:
        if not active:
            return saved
        saved = (_context_id, _context_id, _context_pid, _context_item, _context_phase)
        active = False
        _context_id = 0
        _context_pid = -1
        _context_item = -1
        _context_phase = ""
        return saved
    except BaseException:
        close_worker(saved, False)
        return (0, 0, -1, -1, "")


def arm_text() -> int:
    """Only the first ARM text entry may claim this worker callback."""
    global _context_phase
    try:
        if active and _context_phase == "worker":
            _context_phase = "arm_armed"
            return _context_id
    except BaseException:
        pass
    return 0


def begin_parse() -> int:
    """A nested prepare must not consume its caller's active parse scope."""
    global _context_phase
    try:
        if active and _context_phase == "arm_armed":
            _context_phase = "arm_parse"
            _record_inner("begin", "arm_parse")
            return _context_id
    except BaseException:
        pass
    return 0


def parsed(scope: int) -> None:
    global _context_phase
    try:
        if scope > 0 and scope == _context_id and _context_phase == "arm_parse":
            _record_inner("complete", "arm_parse")
            _context_phase = "arm_prepare"
            _record_inner("begin", "arm_prepare")
    except BaseException:
        pass


def prepared(scope: int) -> None:
    global _context_phase
    try:
        if scope > 0 and scope == _context_id and _context_phase == "arm_prepare":
            _record_inner("complete", "arm_prepare")
            _context_phase = "arm_prepared"
    except BaseException:
        pass


def begin_module_emit(scope: int) -> None:
    global _context_phase
    try:
        if scope > 0 and scope == _context_id and _context_phase == "arm_prepared":
            _context_phase = "arm_module_emit"
            _record_inner("begin", "arm_module_emit")
    except BaseException:
        pass


def close_worker(saved: tuple[int, int, int, int, str], failed: bool) -> None:
    """Restore on every exit; normal return completes only module emission.

    arm_module_emit includes the prepared emitter and text callback return/
    tuple packaging. Incomplete parse/prepare observations are never promoted
    to successful completion merely because the outer callback returned.
    """
    global active, _context_id, _context_pid, _context_item, _context_phase
    try:
        if saved[0] <= 0:
            return
        try:
            if _context_id == saved[0]:
                if failed and _context_phase in ("arm_parse", "arm_prepare", "arm_module_emit"):
                    _record_inner("failed", _context_phase)
                elif not failed and _context_phase == "arm_module_emit":
                    _record_inner("complete", "arm_module_emit")
        finally:
            _context_id = saved[1]
            _context_pid = saved[2]
            _context_item = saved[3]
            _context_phase = saved[4]
            active = _context_id > 0
    except BaseException:
        pass
