"""pcc-owned content comparison used by Python build tools.

Meson's reproducibility check calls :func:`cmp` with ``shallow=False``.  That
path is a byte-for-byte comparison and needs no host ``stat`` module.  CPython's
``shallow=True`` optimization is intentionally fail-closed until pcc owns the
full stat-signature contract (mode, size, and mtime); silently treating it as a
content comparison would not be the documented CPython operation.
"""

from __future__ import annotations


_BUFSIZE = 8192


def cmp(f1, f2, shallow: bool = True) -> bool:
    """Compare two files using the supported CPython-compatible full mode."""
    if shallow:
        raise NotImplementedError(
            "filecmp shallow comparison awaits native stat metadata"
        )

    with open(f1, "rb") as left:
        with open(f2, "rb") as right:
            while True:
                left_chunk = left.read(_BUFSIZE)
                right_chunk = right.read(_BUFSIZE)
                if left_chunk != right_chunk:
                    return False
                if left_chunk == b"":
                    return True


def clear_cache() -> None:
    """CPython API compatibility; the pcc implementation keeps no cache."""
    return None
