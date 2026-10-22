"""Opt-in bounded events for the existing host Mach-O link owner.

The process-tree supervisor already preserves stderr. No Python profiling
hook, frame retention, object snapshot or additional RSS sampler is needed.
"""

import json
import os
import sys
import time


def phase_callback_from_environment():
    try:
        if (sys.implementation.name != "cpython"
                or os.environ.get("PCC_MACHO_LINK_PHASE_EVENTS", "") != "1"):
            return None
        pid = os.getpid()
        sequence = 0
        failed = False
    except BaseException:
        return None

    def record(event, *, section_index=None, segment=None, section=None,
               size_bytes=None, parts=None, consuming=None):
        nonlocal sequence, failed
        if failed:
            return
        try:
            row = {
                "schema": "pcc.macho-link-phase.v1",
                "pid": pid,
                "sequence": sequence,
                "monotonic_ns": time.monotonic_ns(),
                "event": event,
            }
            if section_index is not None:
                row.update(section_index=section_index, segment=segment, section=section,
                           size_bytes=size_bytes, parts=parts, consuming=consuming)
            sys.stderr.write("PCC_MACHO_LINK_PHASE " + json.dumps(row, separators=(",", ":")) + "\n")
            sys.stderr.flush()
            sequence += 1
        except BaseException:
            # Observation cannot replace a compilation/cleanup exception.
            # Disable this sink without keeping an exception or traceback.
            failed = True

    return record


def emit_phase(callback, event):
    if callback is not None:
        try:
            callback(event)
        except BaseException:
            # Private sinks share the stderr sink's non-interference rule.
            pass


def emit_section_phase(callback, event, index, key, size, parts, consuming):
    """Report existing scalar join facts, never a section or backing owner."""
    if callback is not None:
        try:
            callback(event, section_index=index, segment=key[0], section=key[1],
                     size_bytes=size, parts=parts, consuming=consuming)
        except BaseException:
            # Old one-argument private sinks remain valid for ordinary events.
            pass
