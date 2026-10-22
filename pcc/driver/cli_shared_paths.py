"""Content-hash and source-walk helpers shared by both CLI entry points.

`pcc/driver/cli_core.py` and `pcc/driver/cli_bootstrap.py` each carried their own copy of
the FNV-1a hashing and `.py` source-walk helpers (AUD-P2-CLI-SHARED-HELPER-
DUPLICATION). Two copies of a hash function is two chances for the run-cache
key to drift between the host CLI and the bootstrap CLI, which is exactly the
kind of divergence a content-addressed cache cannot detect.

This module is inside the stage1 self-compile closure, so it is written in the
pcc-Python subset the bootstrap compiler accepts: explicit index loops, no
augmented xor-assign, no comprehensions. The bodies are the bootstrap copies
verbatim — they are valid host Python too, so deduplicating toward the subset
form keeps the compiled behavior byte-identical rather than re-deriving it.
"""

import os


def _fnv1a_update_u64(value: object, text: str) -> object:
    # The state is a u64, often 2**63 or more, which pcc's raw i64 int lane
    # cannot hold (pcc1 read it as 0, and now raises OverflowError).  It
    # crosses calls as an int object; the loop runs on two 32-bit halves.
    # The FNV prime is 2**40 + 435, so every intermediate stays below 2**42
    # and the result equals ``(h * 1099511628211) & 0xFFFFFFFFFFFFFFFF``.
    hi: int = (value >> 32) & 0xFFFFFFFF
    lo: int = value & 0xFFFFFFFF
    data = str(text or "")
    i = 0
    while i < len(data):
        lo = lo ^ (ord(data[i]) & 0xFF)
        t: int = lo * 435
        hi = (hi * 435 + (t >> 32) + ((lo & 0xFFFFFF) << 8)) & 0xFFFFFFFF
        lo = t & 0xFFFFFFFF
        i += 1
    return int(format(hi, "08x") + format(lo, "08x"), 16)


def _fnv1a_update_bytes_u64(value: object, data) -> object:
    # See _fnv1a_update_u64.
    hi: int = (value >> 32) & 0xFFFFFFFF
    lo: int = value & 0xFFFFFFFF
    i = 0
    while i < len(data):
        lo = lo ^ (data[i] & 0xFF)
        t: int = lo * 435
        hi = (hi * 435 + (t >> 32) + ((lo & 0xFFFFFF) << 8)) & 0xFFFFFFFF
        lo = t & 0xFFFFFFFF
        i += 1
    return int(format(hi, "08x") + format(lo, "08x"), 16)


def _iter_py_sources_under(root: str):
    root = os.path.abspath(root)
    out = []
    if os.path.isfile(root):
        if root.endswith(".py"):
            out.append(root)
        return out
    if not os.path.isdir(root):
        return out
    stack = [root]
    while len(stack) > 0:
        cur = stack.pop()
        try:
            names = sorted(os.listdir(cur))
        except OSError:
            names = []
        i = 0
        while i < len(names):
            name = names[i]
            if name in (
                ".git",
                ".hg",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                "build",
                "dist",
                ".venv",
                "venv",
            ):
                i += 1
                continue
            full = os.path.join(cur, name)
            if os.path.isdir(full):
                stack.append(full)
            elif name.endswith(".py"):
                out.append(os.path.abspath(full))
            i += 1
    out.sort()
    return out
