"""String slicing split out from py_str_accessors.

Common string indexing and length helpers stay in ``py_str_accessors.py``.
``py_str_slice`` is separated so generic getitem dispatch does not pull the
slicing implementation into executables that only index strings.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.runtime.py.py_abi_constants import PYOBJECTHEADER_TYPE_TAG_OFFSET, PYSTROBJECT_BYTE_LEN_OFFSET, PYSTROBJECT_CP_LEN_OFFSET, PYSTROBJECT_DATA_OFFSET, PYSTROBJECT_HASH_OFFSET, PYSTROBJECT_SIZE, PY_TYPE_INT, PY_TYPE_STR
from pcc.unsafe import (
    free,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    malloc,
    memmove,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i8,
    store_i64,
)


pcc_str_ascii_char = extern("pcc_str_ascii_char", (c_int64,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_raise_zero_slice_step = extern("py_raise_zero_slice_step", (), c_void)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)


def _str_alloc(byte_len: int):
    if byte_len < 0:
        return null()
    if byte_len > 9223372036854775807 - 41:
        return null()
    s = pcc_gc_alloc(PYSTROBJECT_SIZE + byte_len + 1, PY_TYPE_STR, 0)
    if ptr_is_null(s) != 0:
        return null()
    store_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET, byte_len)
    store_i64(s, PYSTROBJECT_CP_LEN_OFFSET, -1)
    store_i64(s, PYSTROBJECT_HASH_OFFSET, -1)
    store_i8(s, PYSTROBJECT_DATA_OFFSET + byte_len, 0)
    return s


def _utf8_codepoint_count(data, byte_len: int) -> int:
    count: int = 0
    i: int = 0
    while i < byte_len:
        b: int = load_i8(data, i) & 255
        if (b & 192) != 128:
            count = count + 1
        i = i + 1
    return count


def _str_cp_len(s) -> int:
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    if cp < 0:
        cp = _utf8_codepoint_count(ptr_add(s, PYSTROBJECT_DATA_OFFSET), load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET))
        store_i64(s, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return cp


def _utf8_byte_offset_for_codepoint(s, cp_idx: int) -> int:
    if cp_idx <= 0:
        return 0
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    cached_cp_len: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    if cached_cp_len == byte_len:
        if cp_idx >= byte_len:
            return byte_len
        return cp_idx
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    seen: int = 0
    i: int = 0
    while i < byte_len:
        b: int = load_i8(data, i) & 255
        if (b & 192) != 128:
            if seen == cp_idx:
                return i
            seen = seen + 1
        i = i + 1
    return byte_len


def _utf8_codepoint_byte_len(s, byte_off: int) -> int:
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if byte_off < 0:
        return 0
    if byte_off >= byte_len:
        return 0
    b: int = load_i8(ptr_add(s, PYSTROBJECT_DATA_OFFSET), byte_off) & 255
    if (b & 128) == 0:
        return 1
    if (b & 224) == 192:
        return 2
    if (b & 240) == 224:
        return 3
    if (b & 248) == 240:
        return 4
    return 1


def _clamp_slice_index(i: int, cp_len: int) -> int:
    if i < 0:
        i = i + cp_len
        if i < 0:
            i = 0
    elif i > cp_len:
        i = cp_len
    return i


def _is_none_or_null(obj) -> int:
    if ptr_is_null(obj) != 0:
        return 1
    if ptr_eq(obj, global_load_ptr("py_None")) != 0:
        return 1
    return 0


def _str_from_range(data, n: int):
    out = _str_alloc(n)
    if ptr_is_null(out) != 0:
        return null()
    if n > 0:
        memmove(ptr_add(out, PYSTROBJECT_DATA_OFFSET), data, n)
    return out


def _type_of(obj) -> int:
    if is_tagged_int(obj) != 0:
        return PY_TYPE_INT
    return load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)


def _int_or_default(obj, default_value: int) -> int:
    if ptr_is_null(obj) != 0:
        return default_value
    if ptr_eq(obj, global_load_ptr("py_None")) != 0:
        return default_value
    if _type_of(obj) == PY_TYPE_INT:
        return py_int_value_i64(obj)
    return default_value


@c_abi_export("py_str_slice")
def py_str_slice(s, lo, hi, step):
    if ptr_is_null(s) != 0:
        return null()
    cp_len: int = _str_cp_len(s)
    step_v: int = 1
    if _is_none_or_null(step) == 0:
        step_v = _int_or_default(step, 1)
    if step_v == 0:
        py_raise_zero_slice_step()
        return null()

    if step_v > 0:
        lo_v: int = 0
        hi_v: int = cp_len
        if _is_none_or_null(lo) == 0:
            lo_v = _int_or_default(lo, 0)
        if _is_none_or_null(hi) == 0:
            hi_v = _int_or_default(hi, cp_len)
        lo_v = _clamp_slice_index(lo_v, cp_len)
        hi_v = _clamp_slice_index(hi_v, cp_len)
        if lo_v >= hi_v:
            return py_str_new(null(), 0)

        if step_v == 1:
            bo_lo: int = _utf8_byte_offset_for_codepoint(s, lo_v)
            bo_hi: int = _utf8_byte_offset_for_codepoint(s, hi_v)
            if bo_hi - bo_lo == 1:
                c: int = load_i8(s, PYSTROBJECT_DATA_OFFSET + bo_lo) & 0xFF
                if c < 128:
                    # The shared immortal one-character string, as for s[i].
                    cached = pcc_str_ascii_char(c)
                    if ptr_is_null(cached) == 0:
                        return cached
            return _str_from_range(ptr_add(ptr_add(s, PYSTROBJECT_DATA_OFFSET), bo_lo), bo_hi - bo_lo)

        bo_lo2: int = _utf8_byte_offset_for_codepoint(s, lo_v)
        bo_hi2: int = _utf8_byte_offset_for_codepoint(s, hi_v)
        cap: int = bo_hi2 - bo_lo2
        out = _str_alloc(cap)
        if ptr_is_null(out) != 0:
            return null()
        src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
        dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
        out_bytes: int = 0
        out_cps: int = 0
        cp_index: int = lo_v
        bpos: int = bo_lo2
        next_target: int = lo_v
        while bpos < bo_hi2:
            w: int = _utf8_codepoint_byte_len(s, bpos)
            if cp_index == next_target:
                memmove(ptr_add(dst, out_bytes), ptr_add(src, bpos), w)
                out_bytes = out_bytes + w
                out_cps = out_cps + 1
                next_target = next_target + step_v
            bpos = bpos + w
            cp_index = cp_index + 1
        store_i64(out, PYSTROBJECT_BYTE_LEN_OFFSET, out_bytes)
        store_i8(dst, out_bytes, 0)
        store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, out_cps)
        return out

    default_lo: int = cp_len - 1
    default_hi: int = -1
    lo_v2: int = default_lo
    hi_v2: int = default_hi
    if _is_none_or_null(lo) == 0:
        lo_v2 = _int_or_default(lo, default_lo)
    if _is_none_or_null(hi) == 0:
        hi_v2 = _int_or_default(hi, default_hi)

    if lo_v2 < 0:
        lo_v2 = lo_v2 + cp_len
    if lo_v2 >= cp_len:
        lo_v2 = cp_len - 1
    if lo_v2 < 0:
        return py_str_new(null(), 0)

    if _is_none_or_null(hi) == 0:
        if hi_v2 < 0:
            hi_v2 = hi_v2 + cp_len
        if hi_v2 < -1:
            hi_v2 = -1
        if hi_v2 > cp_len:
            hi_v2 = cp_len

    if lo_v2 <= hi_v2:
        return py_str_new(null(), 0)

    span: int = lo_v2 - hi_v2
    pos_step: int = -step_v
    out_n: int = (span + pos_step - 1) // pos_step
    cp_off = malloc((cp_len + 1) * 8)
    if ptr_is_null(cp_off) != 0:
        return null()

    src2 = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    cp: int = 0
    i: int = 0
    while i < byte_len:
        b: int = load_i8(src2, i) & 255
        if (b & 192) != 128:
            store_i64(cp_off, cp * 8, i)
            cp = cp + 1
        i = i + 1
    store_i64(cp_off, cp_len * 8, byte_len)

    out2 = _str_alloc(byte_len)
    if ptr_is_null(out2) != 0:
        free(cp_off)
        return null()
    dst2 = ptr_add(out2, PYSTROBJECT_DATA_OFFSET)
    out_bytes2: int = 0
    k: int = 0
    while k < out_n:
        cp_idx: int = lo_v2 + step_v * k
        start: int = load_i64(cp_off, cp_idx * 8)
        end: int = load_i64(cp_off, (cp_idx + 1) * 8)
        width: int = end - start
        memmove(ptr_add(dst2, out_bytes2), ptr_add(src2, start), width)
        out_bytes2 = out_bytes2 + width
        k = k + 1
    store_i64(out2, PYSTROBJECT_BYTE_LEN_OFFSET, out_bytes2)
    store_i8(dst2, out_bytes2, 0)
    store_i64(out2, PYSTROBJECT_CP_LEN_OFFSET, out_n)
    free(cp_off)
    return out2
