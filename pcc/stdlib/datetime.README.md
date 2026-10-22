# Owned datetime implementation

This implementation is adapted from CPython 3.15.0rc1's pure-Python
`Lib/_pydatetime.py` implementation. It imports no `datetime` or `_datetime`
module and has no C-extension or host-datetime fallback. Gregorian calendar
validation/arithmetic, date/time construction, durations, fixed UTC offsets,
ISO formatting, equality and the `tzinfo` protocol retain that implementation.

Upstream SHA-256: `3a55fdc98158cf969b2a1ba827576eb6a9d7e588c8a0401b3e7e4fbe0ff649c1`.

The Python Software Foundation license and the upstream license notices are
included in `datetime.LICENSE.txt`. The three `strptime` methods raise an
explicit capability error until an owned `_strptime` parser exists, instead of
importing an unowned parser. `datetime.__repr__` puts `fold` before `tzinfo`,
matching the CPython 3.15 public class; the upstream pure-Python reference puts
these keywords in the opposite order.
The constant `datetime.__slots__ = time.__slots__` is expanded to the identical
literal tuple because the native frontend currently requires literal slots.
`_format_time` selects identical fixed f-string formats in explicit branches,
so ISO formatting does not require dynamic `str.format` lowering. Non-string
`timespec` values are rejected with `TypeError`, as by CPython's public API.

Host differential tests qualify the APIs needed by general TOML, including
constructor validation, immutable public properties, timedelta normalization,
UTC offsets and ISO representation. This is not a claim of complete stdlib or
native parity. Native construction, platform-clock support, generic formatting
lowering and imported-module behavior need their own emitted-execution gates.
The compiler's current dynamic `strftime` shortcut is not datetime value
formatting evidence.
