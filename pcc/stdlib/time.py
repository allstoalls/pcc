"""pcc.stdlib.time — libc-backed ``time`` surface.

Scope: what pcc's own source (and the self-host benchmark harness)
actually calls.
"""

from __future__ import annotations

import time as _native_time
import sys as _native_sys

from pcc.extern import c_double, c_int, c_int64, c_obj, c_ptr, c_rawptr, c_str, c_void, extern
from pcc.unsafe import (
    load_i32, load_i64, load_ptr, ptr_is_null, stack_alloc, store_i64, strlen,
)
from pcc.stdlib import _structseq


# PCC calls the owned runtime ABI directly: its "time" import resolves to
# this provider. The sys implementation guard also preserves interpreted use.
_time_time: "extern" = extern("py_time_time", (), c_obj)
_time_monotonic: "extern" = extern("py_time_monotonic", (), c_obj)
_time_perf_counter: "extern" = extern("py_time_perf_counter", (), c_obj)
_time_time_ns: "extern" = extern("py_time_time_ns", (), c_obj)
_time_monotonic_ns: "extern" = extern("py_time_monotonic_ns", (), c_obj)
_time_perf_counter_ns: "extern" = extern("py_time_perf_counter_ns", (), c_obj)
_time_strftime: "extern" = extern("py_time_strftime", (c_obj,), c_obj)
_time_sleep: "extern" = extern("py_time_sleep", (c_obj,), c_obj)


def time() -> float:
    """Seconds since the epoch, as a float."""
    if _native_sys.implementation.name != "pcc":
        return float(_native_time.time())
    return float(_time_time())


def monotonic() -> float:
    if _native_sys.implementation.name != "pcc":
        return float(_native_time.monotonic())
    return float(_time_monotonic())


def perf_counter() -> float:
    if _native_sys.implementation.name != "pcc":
        return float(_native_time.perf_counter())
    return float(_time_perf_counter())


def time_ns() -> int:
    """Integer nanoseconds from the existing owned wall-clock ABI."""
    if _native_sys.implementation.name != "pcc":
        return int(_native_time.time_ns())
    return int(_time_time_ns())


def monotonic_ns() -> int:
    if _native_sys.implementation.name != "pcc":
        return int(_native_time.monotonic_ns())
    return int(_time_monotonic_ns())


def perf_counter_ns() -> int:
    if _native_sys.implementation.name != "pcc":
        return int(_native_time.perf_counter_ns())
    return int(_time_perf_counter_ns())


def strftime(fmt: str, /, *time_tuple) -> str:
    if time_tuple:
        raise NotImplementedError("owned time.strftime explicit-tuple formatting is not implemented")
    if _native_sys.implementation.name != "pcc":
        return str(_native_time.strftime(fmt))
    return str(_time_strftime(fmt))


_nanosleep: "extern" = extern(
    "usleep",
    (c_int64,),
    c_int64,
)


def sleep(seconds: float) -> None:
    """Use the same owned delay/error protocol as time.sleep dispatch."""
    if _native_sys.implementation.name != "pcc":
        _native_time.sleep(seconds)
        return
    _time_sleep(seconds)


_index_value = extern("py_obj_index", (c_obj,), c_obj)
_float_value = extern("py_float_to_f64", (c_obj,), c_double)
_localtime_r = extern("localtime_r", (c_rawptr, c_rawptr), c_rawptr)
_breakdown = extern("pcc_time_breakdown", (c_int64, c_int64, c_int64, c_str, c_rawptr), c_int64)
_errno_get = extern("pcc_errno_get", (), c_int)
_errno_set = extern("pcc_errno_set", (c_int,), c_void)
_errno_message = extern("pcc_errno_message_into", (c_int, c_rawptr, c_int64), c_int)
_new_text = extern("py_str_new", (c_rawptr, c_int64), c_obj)


class struct_time(tuple):
    __slots__ = ()
    __pcc_struct_sequence__ = True
    n_fields = 11
    n_sequence_fields = 9
    n_unnamed_fields = 0

    def __new__(cls, sequence, dict={}):
        return _structseq.new(cls, sequence, dict,
            ("tm_year", "tm_mon", "tm_mday", "tm_hour", "tm_min", "tm_sec",
             "tm_wday", "tm_yday", "tm_isdst", "tm_zone", "tm_gmtoff"), 9, "time.struct_time")

    @property
    def tm_year(self):
        return self[0]

    @property
    def tm_mon(self):
        return self[1]

    @property
    def tm_mday(self):
        return self[2]

    @property
    def tm_hour(self):
        return self[3]

    @property
    def tm_min(self):
        return self[4]

    @property
    def tm_sec(self):
        return self[5]

    @property
    def tm_wday(self):
        return self[6]

    @property
    def tm_yday(self):
        return self[7]

    @property
    def tm_isdst(self):
        return self[8]

    @property
    def tm_zone(self):
        return _structseq.hidden_fields(self)[0]

    @property
    def tm_gmtoff(self):
        return _structseq.hidden_fields(self)[1]

    def __repr__(self):
        names = ("tm_year", "tm_mon", "tm_mday", "tm_hour", "tm_min", "tm_sec",
                 "tm_wday", "tm_yday", "tm_isdst")
        parts = []
        for index in range(9):
            parts.append(names[index] + "=" + repr(self[index]))
        return "time.struct_time(" + ", ".join(parts) + ")"

    def __reduce__(self):
        return type(self), (tuple(self), {"tm_zone": self.tm_zone, "tm_gmtoff": self.tm_gmtoff})


def _timestamp_seconds(seconds):
    if seconds is None:
        seconds = time()
    if isinstance(seconds, int) or hasattr(type(seconds), "__index__"):
        integer = _index_value(seconds)
        if integer < -9223372036854775808 or integer >= 9223372036854775808:
            raise OverflowError("timestamp out of range for platform time_t")
        return integer
    if isinstance(seconds, float):
        value = _float_value(seconds)
    else:
        if not hasattr(type(seconds), "__float__") and not hasattr(type(seconds), "__index__"):
            raise TypeError("must be real number, not " + type(seconds).__name__)
        value = float(seconds)
    if value != value:
        raise ValueError("Invalid value NaN (not a number)")
    if value < -9223372036854775808 or value >= 9223372036854775808:
        raise OverflowError("timestamp out of range for platform time_t")
    integral = int(value)
    if integral > value:
        integral -= 1
    return integral


def _time_error(number):
    buffer = stack_alloc(256)
    size = _errno_message(number, buffer, 256)
    if size < 0:
        raise OSError(number, "time conversion failed")
    message = _new_text(buffer, strlen(buffer))
    if message is None:
        raise MemoryError("time error message allocation failed")
    raise OSError(number, message)


def _record_from_tm(buffer):
    zone_pointer = load_ptr(buffer, 48)
    zone = None
    if not ptr_is_null(zone_pointer):
        zone = _new_text(zone_pointer, strlen(zone_pointer))
    return struct_time((
        load_i32(buffer, 20) + 1900, load_i32(buffer, 16) + 1,
        load_i32(buffer, 12), load_i32(buffer, 8), load_i32(buffer, 4),
        load_i32(buffer, 0), (load_i32(buffer, 24) + 6) % 7,
        load_i32(buffer, 28) + 1, load_i32(buffer, 32), zone,
        load_i64(buffer, 40),
    ))


def localtime(seconds=None, /):
    value = _timestamp_seconds(seconds)
    clock = stack_alloc(8)
    buffer = stack_alloc(64)
    store_i64(clock, 0, value)
    _errno_set(0)
    if ptr_is_null(_localtime_r(clock, buffer)):
        number = _errno_get()
        if number == 0:
            raise NotImplementedError("owned localtime platform lacks a failure-status contract")
        if number == 95 and _native_sys.platform == "linux":
            raise NotImplementedError("owned localtime cannot decode the selected timezone data or rule")
        _time_error(number)
    return _record_from_tm(buffer)


def gmtime(seconds=None, /):
    value = _timestamp_seconds(seconds)
    buffer = stack_alloc(64)
    status = _breakdown(value, 0, 0, "GMT", buffer)
    if status != 0:
        # Calendar status -75 is an owned overflow code; errno numbers are
        # platform ABI values (Darwin EOVERFLOW is 84, Linux is 75).
        if status == -75 and _native_sys.platform == "darwin":
            _time_error(84)
        _time_error(-status)
    return _record_from_tm(buffer)
