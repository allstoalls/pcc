"""pcc-Python ``gc`` module backed by the shared collector kernel ABI."""

from __future__ import annotations

from pcc.extern import c_int32, c_int64, c_void, extern

_collect: "extern" = extern("pcc_gc_collect", (c_int32,), c_int64)
_enable: "extern" = extern("py_gc_enable", (), c_void)
_disable: "extern" = extern("py_gc_disable", (), c_void)
_is_enabled: "extern" = extern("py_gc_is_enabled", (), c_int64)


def collect(generation: int = -1) -> int:
    return _collect(generation)


def enable() -> None:
    _enable()


def disable() -> None:
    _disable()


def isenabled() -> bool:
    return bool(_is_enabled())
