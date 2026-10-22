"""Target-owned C scalar facts shared by preprocessing, SSA and codegen.

Language formats and object layout are independent of argument-register
classification. Aggregate consumers reuse these facts without importing an
emitter or querying the host's C compiler.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcc.backend.self_backend_target_match import (
    is_aarch64_darwin_triple, is_aarch64_linux_triple,
    is_x86_64_linux_triple, is_x86_64_windows_triple,
)


def c_target_triple(target_triple=None) -> str:
    if not target_triple:
        from pcc.frontends.python.pipeline_targets import host_target_triple
        target_triple = host_target_triple()
    triple = str(target_triple)
    if not (
        is_aarch64_darwin_triple(triple) or is_aarch64_linux_triple(triple)
        or is_x86_64_linux_triple(triple) or is_x86_64_windows_triple(triple)
    ):
        raise ValueError("unsupported C target ABI: " + triple)
    return triple


def long_double_width(target_triple=None) -> int:
    triple = c_target_triple(target_triple)
    if is_aarch64_linux_triple(triple):
        return 128
    if is_x86_64_linux_triple(triple):
        return 80
    if is_x86_64_windows_triple(triple) and triple.lower().endswith("-gnu"):
        return 80
    return 64


def c_size_type_name(target_triple=None) -> str:
    return "unsigned " + c_ptrdiff_type_name(target_triple)


def c_ptrdiff_type_name(target_triple=None) -> str:
    triple = c_target_triple(target_triple)
    return "long long" if is_x86_64_windows_triple(triple) else "long"


def builtin_integer_is_unsigned(names, target_triple=None) -> bool:
    if "unsigned" in names:
        return True
    if "signed" in names:
        return False
    triple = c_target_triple(target_triple)
    if names == ["char"] or names == ("char",):
        return is_aarch64_linux_triple(triple)
    if names == ["wchar_t"] or names == ("wchar_t",):
        return is_aarch64_linux_triple(triple) or is_x86_64_windows_triple(triple)
    return len(names) == 1 and names[0] in (
        "size_t", "uintptr_t", "uintmax_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    )


def c_target_predefines(target_triple=None) -> dict[str, str]:
    """The preprocessor reports the same model used for C type projection."""
    triple = c_target_triple(target_triple)
    long_size = builtin_scalar_layout(["long"], triple).size
    wchar_size = builtin_scalar_layout(["wchar_t"], triple).size
    width = long_double_width(triple)
    result = {
        "__SIZEOF_LONG__": str(long_size),
        "__SIZEOF_WCHAR_T__": str(wchar_size),
        "__SIZEOF_LONG_DOUBLE__": str(floating_scalar_layout(width).size),
        "__SIZE_TYPE__": c_size_type_name(triple),
        "__PTRDIFF_TYPE__": c_ptrdiff_type_name(triple),
        "__LDBL_DECIMAL_DIG__": "17", "__LDBL_DIG__": "15", "__LDBL_MANT_DIG__": "53",
        "__LDBL_DENORM_MIN__": "4.9406564584124654e-324L",
        "__LDBL_EPSILON__": "2.2204460492503131e-16L",
        "__LDBL_MAX__": "1.7976931348623157e+308L",
        "__LDBL_MAX_10_EXP__": "308", "__LDBL_MAX_EXP__": "1024",
        "__LDBL_MIN__": "2.2250738585072014e-308L",
        "__LDBL_MIN_10_EXP__": "(-307)", "__LDBL_MIN_EXP__": "(-1021)",
        "__LDBL_NORM_MAX__": "1.7976931348623157e+308L",
    }
    unsigned_wchar = builtin_integer_is_unsigned(["wchar_t"], triple)
    result["__WCHAR_TYPE__"] = "unsigned short" if wchar_size == 2 else "unsigned int" if unsigned_wchar else "int"
    result["__WCHAR_MAX__"] = "65535" if wchar_size == 2 else "4294967295U" if unsigned_wchar else "2147483647"
    if width == 80:
        result.update({
            "__LDBL_MANT_DIG__": "64", "__LDBL_DIG__": "18", "__LDBL_DECIMAL_DIG__": "21",
            "__LDBL_EPSILON__": "1.08420217248550443401e-19L",
            "__LDBL_MAX__": "1.18973149535723176502e+4932L",
            "__LDBL_NORM_MAX__": "1.18973149535723176502e+4932L",
            "__LDBL_MIN__": "3.36210314311209350626e-4932L",
            "__LDBL_DENORM_MIN__": "3.64519953188247460253e-4951L",
            "__LDBL_MAX_EXP__": "16384", "__LDBL_MIN_EXP__": "(-16381)",
            "__LDBL_MAX_10_EXP__": "4932", "__LDBL_MIN_10_EXP__": "(-4931)",
        })
    elif width == 128:
        result.update({
            "__LDBL_MANT_DIG__": "113", "__LDBL_DIG__": "33", "__LDBL_DECIMAL_DIG__": "36",
            "__LDBL_EPSILON__": "1.92592994438723585305597794258492732e-34L",
            "__LDBL_MAX__": "1.18973149535723176508575932662800702e+4932L",
            "__LDBL_NORM_MAX__": "1.18973149535723176508575932662800702e+4932L",
            "__LDBL_MIN__": "3.36210314311209350626267781732175260e-4932L",
            "__LDBL_DENORM_MIN__": "6.47517511943802511092443895822764655e-4966L",
            "__LDBL_MAX_EXP__": "16384", "__LDBL_MIN_EXP__": "(-16381)",
            "__LDBL_MAX_10_EXP__": "4932", "__LDBL_MIN_10_EXP__": "(-4931)",
        })
    return result


@dataclass(frozen=True)
class CAbiScalarLayout:
    size: int
    alignment: int


def integer_scalar_layout(bit_width: int) -> CAbiScalarLayout:
    if bit_width <= 0:
        raise ValueError(f"invalid integer width: {bit_width}")
    size = max(1, (bit_width + 7) // 8)
    # __int128 is 16-byte aligned (arm64 AAPCS/Darwin and x86-64 SysV); the
    # self backend lays i128 out the same way.
    return CAbiScalarLayout(size=size, alignment=16 if size == 16 else min(size, 8))


def floating_scalar_layout(bit_width: int) -> CAbiScalarLayout:
    if bit_width == 80:
        return CAbiScalarLayout(size=16, alignment=16)
    if bit_width not in (16, 32, 64, 128):
        raise ValueError(f"unsupported floating width: {bit_width}")
    size = bit_width // 8
    return CAbiScalarLayout(size=size, alignment=size)


def pointer_scalar_layout() -> CAbiScalarLayout:
    return CAbiScalarLayout(size=8, alignment=8)


def builtin_scalar_layout(names: list[str] | tuple[str, ...], target_triple=None) -> CAbiScalarLayout:
    """Return storage layout for one C builtin spelling on this target."""
    triple = c_target_triple(target_triple)
    normalized = tuple(sorted(name for name in names if name not in (
        "signed", "unsigned", "const", "volatile", "restrict", "register",
    )))

    if normalized == ("_Bool",):
        return integer_scalar_layout(8)
    if "char" in normalized:
        return integer_scalar_layout(8)
    if "short" in normalized:
        return integer_scalar_layout(16)
    if normalized.count("long") >= 2:
        return integer_scalar_layout(64)
    if normalized == ("double", "long"):
        return floating_scalar_layout(long_double_width(triple))
    if normalized == ("_Float16",):
        return floating_scalar_layout(16)
    if "long" in normalized:
        return integer_scalar_layout(32 if is_x86_64_windows_triple(triple) else 64)
    if "float" in normalized:
        return floating_scalar_layout(32)
    if "double" in normalized:
        return floating_scalar_layout(64)
    if "wchar_t" in normalized:
        return integer_scalar_layout(16 if is_x86_64_windows_triple(triple) else 32)
    if normalized in (("size_t",), ("ssize_t",), ("ptrdiff_t",), ("intptr_t",), ("uintptr_t",)):
        return pointer_scalar_layout()
    if normalized == ("__int128",):
        return integer_scalar_layout(128)
    if "int" in normalized or not normalized:
        return integer_scalar_layout(32)
    if "void" in normalized:
        raise ValueError("void has no object size")
    raise ValueError(f"unsupported builtin scalar layout: {' '.join(names)}")


def integer_literal_value(raw: str) -> int:
    value = raw.rstrip("uUlL")
    if value.startswith(("0x", "0X")):
        return int(value, 16)
    if value.startswith(("0b", "0B")):
        return int(value, 2)
    if value.startswith("0") and len(value) > 1 and value[1:].isdigit():
        return int(value, 8)
    return int(value, 10)


def integer_literal_type_name(raw: str, target_triple=None) -> str:
    lower = raw.lower()
    has_unsigned = "u" in lower
    long_count = lower.count("l")
    value = raw.rstrip("uUlL")
    parsed = integer_literal_value(raw)
    non_decimal = value.startswith(("0x", "0X", "0b", "0B")) or (value.startswith("0") and len(value) > 1)
    if long_count >= 2:
        candidates = ("unsigned long long",) if has_unsigned else ("long long", "unsigned long long") if non_decimal else ("long long",)
    elif long_count == 1:
        candidates = ("unsigned long", "unsigned long long") if has_unsigned else ("long", "unsigned long", "long long", "unsigned long long") if non_decimal else ("long", "long long")
    elif has_unsigned:
        candidates = ("unsigned int", "unsigned long", "unsigned long long")
    elif non_decimal:
        candidates = ("int", "unsigned int", "long", "unsigned long", "long long", "unsigned long long")
    else:
        candidates = ("int", "long", "long long")
    for candidate in candidates:
        width = builtin_scalar_layout(candidate.split(), target_triple).size * 8
        limit = (1 << width) - 1 if candidate.startswith("unsigned") else (1 << (width - 1)) - 1
        if parsed <= limit:
            return candidate
    raise ValueError("integer literal exceeds supported C scalar types: " + raw)
