"""pcc.stdlib.copy — shallow + deep copy skeleton."""

from __future__ import annotations

import copy as _native_copy


class Error(Exception):
    pass


def copy(x):
    """Shallow copy through the compiler's pcc-object runtime primitive."""
    return _native_copy.copy(x)


def deepcopy(x, memo=None):
    """Recursive copy through the pcc runtime's memoizing primitive."""
    return _native_copy.deepcopy(x)
