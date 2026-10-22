"""Shared packed call classification for self-backend analyses."""

from __future__ import annotations


CALL_FLAG_INDIRECT = 1
CALL_FLAG_VARARG = 2
CALL_FLAG_FRAME_ENTER = 4
CALL_FLAG_FRAME_LEAVE = 8
CALL_FLAG_LLVM_INTRINSIC = 16
CALL_FLAG_EXCEPTION_POLL = 32
CALL_FLAG_CONTINUATION = 64
CALL_FLAG_LOOP_SAFEPOINT = 128

CALL_FLAG_FRAME_PROTOCOL = CALL_FLAG_FRAME_ENTER | CALL_FLAG_FRAME_LEAVE

# The ``_mapped`` entries register a stack frame only when no precise stack
# walk roots it; for planning they are the same enter/leave protocol.
_FRAME_ENTER_CALLEES = frozenset((
    "pcc_gc_frame_enter", "pcc_gc_frame_enter_lifo",
    "pcc_gc_frame_enter_mapped", "pcc_gc_frame_enter_lifo_mapped",
))
_FRAME_LEAVE_CALLEES = frozenset((
    "pcc_gc_frame_leave", "pcc_gc_frame_leave_lifo",
    "pcc_gc_frame_leave_mapped", "pcc_gc_frame_leave_lifo_mapped",
))
CALL_FLAG_STACKMAP_SKIP = CALL_FLAG_FRAME_PROTOCOL | CALL_FLAG_LLVM_INTRINSIC


def classify_call_flags(
    callee: str,
    is_indirect: bool,
    is_vararg: bool,
) -> int:
    """Classify one parsed call while its callee spelling is already hot."""

    flags = (
        (CALL_FLAG_INDIRECT if is_indirect else 0)
        | (CALL_FLAG_VARARG if is_vararg else 0)
    )
    if is_indirect:
        return flags
    if callee.startswith("pcc_gc_frame_enter") and callee in _FRAME_ENTER_CALLEES:
        return flags | CALL_FLAG_FRAME_ENTER
    if callee.startswith("pcc_gc_frame_leave") and callee in _FRAME_LEAVE_CALLEES:
        return flags | CALL_FLAG_FRAME_LEAVE
    if callee.startswith("llvm."):
        return flags | CALL_FLAG_LLVM_INTRINSIC
    if callee == "py_err_occurred":
        return flags | CALL_FLAG_EXCEPTION_POLL
    if (
        "continuation" in callee
        or "__gen_resume" in callee
        or "__vthread_resume" in callee
    ):
        return flags | CALL_FLAG_CONTINUATION
    if (
        callee == "pcc_thread_safepoint"
        or callee == "pcc_thread_safepoint_gate"
        or callee == "pcc_gc_safepoint"
    ):
        return flags | CALL_FLAG_LOOP_SAFEPOINT
    return flags


__all__ = [
    "CALL_FLAG_CONTINUATION",
    "CALL_FLAG_EXCEPTION_POLL",
    "CALL_FLAG_FRAME_ENTER",
    "CALL_FLAG_FRAME_LEAVE",
    "CALL_FLAG_FRAME_PROTOCOL",
    "CALL_FLAG_INDIRECT",
    "CALL_FLAG_LLVM_INTRINSIC",
    "CALL_FLAG_LOOP_SAFEPOINT",
    "CALL_FLAG_STACKMAP_SKIP",
    "CALL_FLAG_VARARG",
    "classify_call_flags",
]
