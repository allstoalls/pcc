"""pcc.stdlib.platform — narrow ``platform`` skeleton."""
from __future__ import annotations

import sys as _sys

from pcc.driver.python_target import PYTHON_TARGET_FULL_VERSION, PYTHON_TARGET_VERSION_PARTS
from pcc.extern import c_int64, c_obj, extern
from pcc.unsafe import ptr_is_null


# The existing runtime provider returns an owned Python string, not a raw cstr.
_machine_native = extern("py_platform_machine_str", (), c_obj)
_error_pending = extern("py_err_occurred", (), c_int64)


def system() -> str:
    p = _sys.platform
    if p.startswith("darwin"):
        return "Darwin"
    if p.startswith("linux"):
        return "Linux"
    if p.startswith("win"):
        return "Windows"
    return p


def machine() -> str:
    """Use host Python on the host and the owned target provider natively."""
    if _sys.implementation.name != "pcc":
        # Do not import/call this module recursively in the native branch.
        import platform as _host_platform

        return _host_platform.machine()
    result = _machine_native()
    if _error_pending():
        raise
    if ptr_is_null(result):
        raise RuntimeError("owned platform.machine helper returned NULL without an exception")
    return result


def python_version() -> str:
    return PYTHON_TARGET_FULL_VERSION


def platform() -> str:
    return system()


def node() -> str:
    return ""


def python_implementation() -> str:
    return _sys.implementation.name


def python_version_tuple():
    return PYTHON_TARGET_VERSION_PARTS


def uname():
    sys_name = system()
    return (sys_name, node(), "", "", machine(), "")
