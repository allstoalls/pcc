"""pcc-Python owners for the two CPython-compatible snprintf wrappers."""

__pcc_runtime_port__ = True

from pcc.extern import (
    c_abi_typed_export,
    c_abi_variadic_export,
    c_int64,
    c_ptr,
    extern,
)
from pcc.unsafe import null, va_cursor, va_end, va_start


pcc_stdio_format_core = extern(
    "pcc_stdio_format_core",
    (c_ptr, c_int64, c_ptr, c_ptr, c_ptr),
    c_int64,
)


@c_abi_typed_export(
    "PyOS_vsnprintf",
    "i32",
    ("ptr", "i64", "ptr", "ptr"),
)
def PyOS_vsnprintf(output, size: int, format_string, va_list) -> int:
    return pcc_stdio_format_core(
        output,
        size,
        null(),
        format_string,
        va_cursor(va_list),
    )


@c_abi_typed_export(
    "PyOS_snprintf",
    "i32",
    ("ptr", "i64", "ptr"),
)
@c_abi_variadic_export("PyOS_snprintf")
def PyOS_snprintf(output, size: int, format_string) -> int:
    cursor = va_start()
    result: int = pcc_stdio_format_core(
        output,
        size,
        null(),
        format_string,
        cursor,
    )
    va_end(cursor)
    return result
