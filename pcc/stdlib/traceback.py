"""Owned current-exception formatting through the runtime frame records.

Native format_exc/print_exc support their default options. Explicit limits,
chain suppression and alternate print destinations fail closed until the
runtime formatter supports them. Other traceback APIs remain unsupported.
"""
from __future__ import annotations

import sys as _native_sys
import traceback as _host_traceback
from pcc.extern import c_obj, c_void, extern

_format_current = extern("py_exc_traceback_format_current", (), c_obj)
_print_current = extern("py_exc_traceback_print_current", (), c_void)


def format_exception(etype, value, tb) -> list[str]:
    raise NotImplementedError(
        "format_exception awaits runtime frame-table marshalling"
    )


def format_exc(limit=None, chain=True) -> str:
    if _native_sys.implementation.name != "pcc":
        return _host_traceback.format_exc(limit=limit, chain=chain)
    if limit is not None:
        raise NotImplementedError("native traceback.format_exc limit is not implemented")
    if chain is not True:
        raise NotImplementedError("native traceback.format_exc chain option is not implemented")
    return _format_current()


def print_exc(limit=None, file=None, chain=True) -> None:
    if _native_sys.implementation.name != "pcc":
        _host_traceback.print_exc(limit=limit, file=file, chain=chain)
        return
    if limit is not None:
        raise NotImplementedError("native traceback.print_exc limit is not implemented")
    if file is not None:
        raise NotImplementedError("native traceback.print_exc file option is not implemented")
    if chain is not True:
        raise NotImplementedError("native traceback.print_exc chain option is not implemented")
    _print_current()


def print_exception(etype, value, tb) -> None:
    raise NotImplementedError(
        "print_exception awaits frame-table marshalling"
    )
