"""Phase 4c.16-prep: pcc-Python port of py_str_accessors.c.

PyStrObject accessors and small string helpers. Most helpers are byte-level;
py_str_len and py_str_find preserve Python's codepoint-visible semantics.
The remaining py_str.c body keeps only py_str_new.

PyStrObject layout (from py_internal.h):
    offset  0   PyObjectHeader   (16 bytes)
    offset 16   byte_len         (i64)
    offset 24   cp_len           (i64, -1 if not yet computed)
    offset 32   hash             (i64, -1 if not yet computed)
    offset 40   data[]           (UTF-8 bytes + NUL, flexible array)
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int32, c_int64, c_void
from pcc.runtime.py.py_abi_constants import (
    PYBYTESOBJECT_BYTE_LEN_OFFSET,
    PYBYTESOBJECT_DATA_OFFSET,
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_REFCOUNT_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYSTROBJECT_BYTE_LEN_OFFSET,
    PYSTROBJECT_CP_LEN_OFFSET,
    PYSTROBJECT_DATA_OFFSET,
    PYSTROBJECT_HASH_OFFSET,
    PYSTROBJECT_SIZE,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PY_FLAG_GC_MALLOC_ALLOC,
    PY_FLAG_GC_PINNED,
    PY_FLAG_IMMORTAL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)
from pcc.unsafe import (
    cstr,
    define_global_ptr_null,
    define_global_i32,
    global_addr,
    stack_alloc,
    free,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memmove,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
)

py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_payload = extern("py_str_payload", (c_ptr,), c_ptr)
py_str_exact_copy = extern("py_str_exact_copy", (c_ptr,), c_ptr)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)


@c_abi_export("py_str_check")
def py_str_check(value) -> int:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_STR:
        return 1
    return 0 if ptr_is_null(py_str_payload(value)) != 0 else 1

py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_unicode_encode_error = extern("py_unicode_encode_error", (c_ptr, c_ptr, c_int64, c_int64, c_ptr), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
define_global_i32("pcc_str_encode_borrowed_frame_map", -3)
define_global_i32("pcc_str_encode_owned_frame_map", 4)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_obj_call_method1 = extern("py_obj_call_method1", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_mem_alloc = extern("py_mem_alloc", (c_int64,), c_ptr)
py_mem_free = extern("py_mem_free", (c_ptr,), c_void)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_pointer_register = extern("pcc_gc_pointer_register", (c_ptr,), c_int64)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
pcc_debug_bad_str_concat = extern(
    "pcc_debug_bad_str_concat",
    (c_ptr, c_ptr, c_int64, c_int64),
    c_void,
)


def _str_alloc(byte_len: int):
    # Local replica of py_str_alloc — alloc PyStrObject sized for
    # byte_len + NUL. PyStrObject = 40 bytes.
    if byte_len < 0:
        return null()
    if byte_len > 9223372036854775807 - 41:
        return null()
    s = pcc_gc_alloc(PYSTROBJECT_SIZE + byte_len + 1, PY_TYPE_STR, 0)
    if ptr_is_null(s) != 0:
        return null()
    store_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET, byte_len)  # byte_len
    store_i64(s, PYSTROBJECT_CP_LEN_OFFSET, -1)  # cp_len
    store_i64(s, PYSTROBJECT_HASH_OFFSET, -1)  # hash
    store_i8(s, PYSTROBJECT_DATA_OFFSET + byte_len, 0)  # NUL terminator
    return s


def _is_ascii_ws(c: int) -> int:
    if c == 32:  # ' '
        return 1
    if c == 9:  # '\t'
        return 1
    if c == 10:  # '\n'
        return 1
    if c == 13:  # '\r'
        return 1
    if c == 11:  # '\v'
        return 1
    if c == 12:  # '\f'
        return 1
    return 0


def _bytes_eq(a, b, n: int) -> int:
    # Byte-by-byte memcmp equivalent; returns 1 if equal, 0 otherwise.
    i: int = 0
    while i < n:
        if (load_i8(a, i) & 0xFF) != (load_i8(b, i) & 0xFF):
            return 0
        i = i + 1
    return 1


def _utf8_codepoint_count(data, byte_len: int) -> int:
    count: int = 0
    i: int = 0
    while i < byte_len:
        b: int = load_i8(data, i) & 0xFF
        if (b & 0xC0) != 0x80:
            count = count + 1
        i = i + 1
    return count


def _byte_find(hay, hay_len: int, need, need_len: int) -> int:
    if need_len == 0:
        return 0
    if need_len > hay_len:
        return -1
    last: int = hay_len - need_len
    i: int = 0
    while i <= last:
        first_hay: int = load_i8(hay, i) & 0xFF
        first_need: int = load_i8(need, 0) & 0xFF
        if first_hay == first_need:
            if _bytes_eq(ptr_add(hay, i), need, need_len) != 0:
                return i
        i = i + 1
    return -1


def _byte_rfind(hay, hay_len: int, need, need_len: int) -> int:
    if need_len == 0:
        return hay_len
    if need_len > hay_len:
        return -1
    last: int = hay_len - need_len
    i: int = last
    while i >= 0:
        first_hay: int = load_i8(hay, i) & 0xFF
        first_need: int = load_i8(need, 0) & 0xFF
        if first_hay == first_need:
            if _bytes_eq(ptr_add(hay, i), need, need_len) != 0:
                return i
        i = i - 1
    return -1


def _stringlike_data(o):
    if ptr_is_null(o) != 0:
        return null()
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_STR:
        return ptr_add(o, PYSTROBJECT_DATA_OFFSET)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return ptr_add(o, PYBYTESOBJECT_DATA_OFFSET)
    return null()


def _stringlike_len(o) -> int:
    if ptr_is_null(o) != 0:
        return 0
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_STR or tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return load_i64(o, PYBYTESOBJECT_BYTE_LEN_OFFSET)
    return 0


def _byte_offset_to_cp_offset(s, byte_off: int) -> int:
    if byte_off <= 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if byte_off >= n:
        byte_off = n
    # All-ASCII fast path.  A codepoint count equal to the byte length means
    # every byte is its own codepoint, so the byte offset IS the codepoint
    # offset and no scan is needed.  The count itself is cached on the object
    # by _str_cp_len, so this is O(1) after the first call.
    #
    # Without it, every str.find()/index()/rfind() on a long string pays a scan
    # from the start just to convert the result -- making find O(n) beyond its
    # own search.  Measured: replacing a character loop with repeated find()
    # calls was 6.4x FASTER on CPython and 6.2x SLOWER under pcc1 (65 ms ->
    # 405 ms on a 286 KB module), because N finds became O(N^2).  IR text,
    # source text and symbol names are pure ASCII, so this path is the common
    # case for the compiler's own workload.
    if _str_cp_len(s) == n:
        return byte_off
    return _utf8_codepoint_count(ptr_add(s, PYSTROBJECT_DATA_OFFSET), byte_off)


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
    # Once _str_cp_len has proved an ASCII-only string, codepoint
    # offsets are byte offsets. The parser/lexer index source text by
    # position; without this fast path each s[i] rescans from byte 0.
    cached_cp_len: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    if cached_cp_len == byte_len:
        if cp_idx >= byte_len:
            return byte_len
        return cp_idx
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    seen: int = 0
    i: int = 0
    while i < byte_len:
        b: int = load_i8(data, i) & 0xFF
        if (b & 0xC0) != 0x80:
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
    b: int = load_i8(ptr_add(s, PYSTROBJECT_DATA_OFFSET), byte_off) & 0xFF
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


def _normalise_index(i: int, cp_len: int) -> int:
    if i < 0:
        i = i + cp_len
    if i < 0 or i >= cp_len:
        return -1
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


@c_abi_export("py_str_byte_len")
def py_str_byte_len(s) -> int:
    if ptr_is_null(s) == 0 and _type_of(s) != PY_TYPE_STR:
        s = py_str_payload(s)
    if ptr_is_null(s) != 0:
        return 0
    return load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)


@c_abi_export("py_str_utf8")
def py_str_utf8(s):
    if ptr_is_null(s) != 0:
        return null()
    return ptr_add(s, PYSTROBJECT_DATA_OFFSET)


@c_abi_export("pcc_capi_str_utf8_pinned")
def pcc_capi_str_utf8_pinned(s):
    pcc_gc_pin(s)
    return py_str_utf8(s)


@c_abi_export("py_str_len")
def py_str_len(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    if cp < 0:
        cp = _utf8_codepoint_count(ptr_add(s, PYSTROBJECT_DATA_OFFSET), load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET))
        store_i64(s, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return cp


@c_abi_export("py_str_ord")
def py_str_ord(s) -> int:
    if ptr_is_null(s) != 0:
        return -1
    return _utf8_ord_at_byte(s, 0)


def _utf8_ord_at_byte(s, byte_off: int) -> int:
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if byte_off < 0:
        return -1
    if byte_off >= byte_len:
        return -1
    remaining: int = byte_len - byte_off
    data = ptr_add(ptr_add(s, PYSTROBJECT_DATA_OFFSET), byte_off)
    b0: int = load_i8(data, 0) & 255
    if b0 < 128:
        return b0
    if (b0 & 224) == 192:
        if remaining < 2:
            return -1
        return ((b0 & 31) << 6) | (load_i8(data, 1) & 63)
    if (b0 & 240) == 224:
        if remaining < 3:
            return -1
        return (
            ((b0 & 15) << 12) | ((load_i8(data, 1) & 63) << 6) | (load_i8(data, 2) & 63)
        )
    if (b0 & 248) == 240:
        if remaining < 4:
            return -1
        return (
            ((b0 & 7) << 18)
            | ((load_i8(data, 1) & 63) << 12)
            | ((load_i8(data, 2) & 63) << 6)
            | (load_i8(data, 3) & 63)
        )
    return -1


@c_abi_export("py_str_ord_at_i64")
def py_str_ord_at_i64(s, idx: int) -> int:
    if ptr_is_null(s) != 0:
        return -1
    cp_len: int = _str_cp_len(s)
    real: int = _normalise_index(idx, cp_len)
    if real < 0:
        return -1
    if load_i64(s, PYSTROBJECT_CP_LEN_OFFSET) == load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET):
        return load_i8(ptr_add(s, PYSTROBJECT_DATA_OFFSET), real) & 255
    bo: int = _utf8_byte_offset_for_codepoint(s, real)
    return _utf8_ord_at_byte(s, bo)


@c_abi_export("py_str_byte_at_i64")
def py_str_byte_at_i64(s, idx: int) -> int:
    if ptr_is_null(s) != 0:
        return -1
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if idx < 0:
        return -1
    if idx >= byte_len:
        return -1
    return load_i8(ptr_add(s, PYSTROBJECT_DATA_OFFSET), idx) & 255


def _encode_name_equal(obj, expected: c_ptr, size: int) -> int:
    if load_i64(obj, PYSTROBJECT_BYTE_LEN_OFFSET) != size:
        return 0
    data = ptr_add(obj, PYSTROBJECT_DATA_OFFSET)
    index: int = 0
    while index < size:
        if (load_i8(data, index) & 255) != (load_i8(expected, index) & 255):
            return 0
        index = index + 1
    return 1


def _encode_codec_equal(obj, expected: c_ptr, size: int) -> int:
    # codecs.normalize_encoding: lowercase ASCII, collapse punctuation to an
    # underscore and drop leading/trailing separators. Dots are significant.
    data = ptr_add(obj, PYSTROBJECT_DATA_OFFSET)
    length: int = load_i64(obj, PYSTROBJECT_BYTE_LEN_OFFSET)
    index: int = 0
    output: int = 0
    separator: int = 0
    while index < length:
        char: int = load_i8(data, index) & 255
        if char >= 65 and char <= 90:
            char = char + 32
        if (char >= 97 and char <= 122) or (char >= 48 and char <= 57) or char == 46:
            if separator != 0 and output != 0:
                if output >= size or load_i8(expected, output) != 95:
                    return 0
                output = output + 1
            separator = 0
            if output >= size or (load_i8(expected, output) & 255) != char:
                return 0
            output = output + 1
        else:
            separator = 1
        index = index + 1
    if output == size:
        return 1
    return 0


# BEGIN GENERATED CODEC NAMES (scripts/gen_codec_names.py)
# CPython 3.15.0: 493 text codec names, 18 binary transforms.
def _codec_text_names():
    return cstr(" 037 1026 1125 1140 1250 1251 1252 1253 1254 1255 1256 1257 1258 273 424 437 500 646 775 850 852 855 857 858 860 861 862 863 864 865 866 869 874 8859 932 936 949 950 ansi_x3.4_1968 ansi_x3.4_1986 ansi_x3_4_1968 arabic ascii asmo_708 big5 big5_hkscs big5_tw big5hkscs ccsid00858 ccsid01140 charmap chinese cp00858 cp01140 cp037 cp1006 cp1026 cp1051 cp1125 cp1140 cp1250 cp1251 cp1252 cp1253 cp1254 cp1255 cp1256 cp1257 cp1258 cp1361 cp154 cp273 cp367 cp424 cp437 cp500 cp65001 cp720 cp737 cp775 cp819 cp850 cp852 cp855 cp856 cp857 cp858 cp860 cp861 cp862 cp863 cp864 cp865 cp866 cp866u cp869 cp874 cp875 cp932 cp936 cp949 cp950 cp_gr cp_is csascii csbig5 csbig5hkscs cseuckr cseucpkdfmtjapanese csgb18030 csgb2312 csgbk cshproman8 csibm00858 csibm01140 csibm037 csibm1026 csibm273 csibm424 csibm500 csibm855 csibm857 csibm858 csibm860 csibm861 csibm863 csibm864 csibm865 csibm866 csibm869 csiso2022jp csiso2022jp2 csiso2022kr csiso58gb231280 csiso885913 csiso885914 csiso885915 csiso885916 csiso88598e csiso88598i csisolatin1 csisolatin2 csisolatin3 csisolatin4 csisolatin5 csisolatin6 csisolatinarabic csisolatincyrillic csisolatingreek csisolatinhebrew cskoi8r cskoi8u csksc56011987 cskz1048 csmacintosh cspc775baltic cspc850multilingual cspc862latinhebrew cspc8codepage437 cspcp852 csptcp154 csshiftjis cstis620 csunicode11utf7 csutf16 csutf16be csutf16le csutf32 csutf32be csutf32le csutf7 csutf8 cswindows1250 cswindows1251 cswindows1252 cswindows1253 cswindows1254 cswindows1255 cswindows1256 cswindows1257 cswindows1258 cswindows31j cswindows874 cyrillic cyrillic_asian ebcdic_cp_be ebcdic_cp_ca ebcdic_cp_ch ebcdic_cp_he ebcdic_cp_nl ebcdic_cp_us ebcdic_cp_wt ebcdic_us_37_euro ecma_114 ecma_118 elot_928 euc_cn euc_jis2004 euc_jis_2004 euc_jisx0213 euc_jp euc_kr euccn eucgb2312_cn eucjis2004 eucjisx0213 eucjp euckr gb18030 gb18030_2000 gb2312 gb2312_1980 gb2312_80 gb_2312_80 gbk greek greek8 hebrew hkscs hp_roman8 hz hz_gb hz_gb_2312 hzgb ibm00858 ibm01140 ibm037 ibm039 ibm1026 ibm1051 ibm1125 ibm1140 ibm273 ibm367 ibm424 ibm437 ibm500 ibm775 ibm819 ibm850 ibm852 ibm855 ibm857 ibm858 ibm860 ibm861 ibm862 ibm863 ibm864 ibm865 ibm866 ibm869 idna iso2022_jp iso2022_jp_1 iso2022_jp_2 iso2022_jp_2004 iso2022_jp_3 iso2022_jp_ext iso2022_kr iso2022jp iso2022jp_1 iso2022jp_2 iso2022jp_2004 iso2022jp_3 iso2022jp_ext iso2022kr iso646_us iso8859 iso8859_1 iso8859_10 iso8859_11 iso8859_13 iso8859_14 iso8859_15 iso8859_16 iso8859_2 iso8859_3 iso8859_4 iso8859_5 iso8859_6 iso8859_7 iso8859_8 iso8859_9 iso_2022_jp iso_2022_jp_1 iso_2022_jp_2 iso_2022_jp_2004 iso_2022_jp_3 iso_2022_jp_ext iso_2022_kr iso_646.irv_1991 iso_8859_1 iso_8859_10 iso_8859_10_1992 iso_8859_11 iso_8859_11_2001 iso_8859_13 iso_8859_14 iso_8859_14_1998 iso_8859_15 iso_8859_16 iso_8859_16_2001 iso_8859_1_1987 iso_8859_2 iso_8859_2_1987 iso_8859_3 iso_8859_3_1988 iso_8859_4 iso_8859_4_1988 iso_8859_5 iso_8859_5_1988 iso_8859_6 iso_8859_6_1987 iso_8859_7 iso_8859_7_1987 iso_8859_8 iso_8859_8_1988 iso_8859_8_e iso_8859_8_i iso_8859_9 iso_8859_9_1989 iso_celtic iso_ir_100 iso_ir_101 iso_ir_109 iso_ir_110 iso_ir_126 iso_ir_127 iso_ir_138 iso_ir_144 iso_ir_148 iso_ir_149 iso_ir_157 iso_ir_166 iso_ir_199 iso_ir_226 iso_ir_58 iso_ir_6 jisx0213 johab koi8_r koi8_t koi8_u korean ks_c_5601 ks_c_5601_1987 ks_c_5601_1989 ks_x_1001 ksc5601 ksc_5601 ksx1001 kz1048 kz_1048 l1 l10 l2 l3 l4 l5 l6 l7 l8 l9 latin latin1 latin10 latin2 latin3 latin4 latin5 latin6 latin7 latin8 latin9 latin_1 latin_10 latin_2 latin_3 latin_4 latin_5 latin_6 latin_7 latin_8 latin_9 mac mac_arabic mac_centeuro mac_croatian mac_cyrillic mac_farsi mac_greek mac_iceland mac_latin2 mac_roman mac_romanian mac_turkish maccentraleurope maccyrillic macgreek maciceland macintosh maclatin2 macroman macturkish ms1361 ms874 ms932 ms936 ms949 ms950 ms_kanji mskanji palmos pc_multilingual_850_euro pt154 ptcp154 punycode r8 raw_unicode_escape rk1048 roman8 ruscii s_jis s_jis_2004 s_jisx0213 shift_jis shift_jis_2004 shift_jisx0213 shiftjis shiftjis2004 shiftjisx0213 sjis sjis_2004 sjisx0213 strk1048_2002 thai tis620 tis_620 tis_620_0 tis_620_2529_0 tis_620_2529_1 u16 u32 u7 u8 u_jis uhc ujis undefined unicode_1_1_utf_7 unicode_escape unicodebigunmarked unicodelittleunmarked us us_ascii utf utf16 utf32 utf7 utf8 utf8_sig utf8_ucs2 utf8_ucs4 utf_16 utf_16_be utf_16_le utf_16be utf_16le utf_32 utf_32_be utf_32_le utf_32be utf_32le utf_7 utf_8 utf_8_sig windows_1250 windows_1251 windows_1252 windows_1253 windows_1254 windows_1255 windows_1256 windows_1257 windows_1258 windows_31j windows_874 windows_936 x_mac_japanese x_mac_korean x_mac_simp_chinese x_mac_trad_chinese ")


def _codec_binary_names():
    return cstr(" base64 base64_codec base_64 bz2 bz2_codec hex hex_codec quopri quopri_codec quoted_printable quotedprintable rot13 rot_13 uu uu_codec zip zlib zlib_codec ")
# END GENERATED CODEC NAMES


def _codec_normalize(obj, buffer, capacity: int) -> int:
    """codecs.normalize_encoding of a lowercased name into ``buffer``: its
    length, or -1 when it does not fit (no CPython codec name is that long)."""
    data = ptr_add(obj, PYSTROBJECT_DATA_OFFSET)
    length: int = load_i64(obj, PYSTROBJECT_BYTE_LEN_OFFSET)
    index: int = 0
    output: int = 0
    separator: int = 0
    while index < length:
        char: int = load_i8(data, index) & 255
        if char >= 65 and char <= 90:
            char = char + 32
        if (char >= 97 and char <= 122) or (char >= 48 and char <= 57) or char == 46:
            if separator != 0 and output != 0:
                if output >= capacity:
                    return -1
                store_i8(buffer, output, 95)
                output = output + 1
            separator = 0
            if output >= capacity:
                return -1
            store_i8(buffer, output, char)
            output = output + 1
        else:
            separator = 1
        index = index + 1
    return output


def _codec_name_listed(table, name, length: int) -> int:
    """Whether ``name`` (``length`` bytes) is a word of a space-separated
    table that starts and ends with a space."""
    index: int = 0
    while load_i8(table, index) != 0:
        if load_i8(table, index) == 32:
            offset: int = 0
            matched: int = 1
            while offset < length and matched != 0:
                if load_i8(table, index + 1 + offset) != load_i8(name, offset):
                    matched = 0
                offset = offset + 1
            if matched != 0 and load_i8(table, index + 1 + length) == 32:
                return 1
        index = index + 1
    return 0


def _codec_kind(name, length: int) -> int:
    """0 unknown to CPython, 1 a text codec, 2 a binary transform; as
    encodings.search_function, also with dots read as underscores."""
    if length <= 0:
        return 0
    if _codec_name_listed(_codec_binary_names(), name, length) != 0:
        return 2
    if _codec_name_listed(_codec_text_names(), name, length) != 0:
        return 1
    index: int = 0
    dotted: int = 0
    while index < length:
        if load_i8(name, index) == 46:
            store_i8(name, index, 95)
            dotted = 1
        index = index + 1
    if dotted == 0:
        return 0
    if _codec_name_listed(_codec_binary_names(), name, length) != 0:
        return 2
    if _codec_name_listed(_codec_text_names(), name, length) != 0:
        return 1
    return 0


def _codec_lookup_error(encoding, direction: int) -> None:
    """The error for a codec name without a native codec, as CPython's
    codec lookup reports it: LookupError "unknown encoding: X" for a name
    that is no codec, LookupError "'X' is not a text encoding" for a binary
    transform (``direction`` 1 encode, 2 decode adds the codecs function to
    use; 0, open(), adds nothing) and NotImplementedError for a CPython text
    codec the native runtime does not implement."""
    normalized = stack_alloc(72)
    kind: int = _codec_kind(normalized, _codec_normalize(encoding, normalized, 64))
    message = stack_alloc(320)
    at: int = 0
    prefix = cstr("unknown encoding: ")
    if kind == 1:
        prefix = cstr("native text codec '")
    elif kind == 2:
        prefix = cstr("'")
    while load_i8(prefix, at) != 0:
        store_i8(message, at, load_i8(prefix, at))
        at = at + 1
    data = ptr_add(encoding, PYSTROBJECT_DATA_OFFSET)
    length: int = load_i64(encoding, PYSTROBJECT_BYTE_LEN_OFFSET)
    if length > 200:
        length = 200
    index: int = 0
    while index < length:
        store_i8(message, at, load_i8(data, index))
        at = at + 1
        index = index + 1
    suffix = cstr("")
    if kind == 1:
        suffix = cstr("' is not implemented")
    elif kind == 2:
        if direction == 1:
            suffix = cstr("' is not a text encoding; use codecs.encode() to handle arbitrary codecs")
        elif direction == 2:
            suffix = cstr("' is not a text encoding; use codecs.decode() to handle arbitrary codecs")
        else:
            suffix = cstr("' is not a text encoding")
    index = 0
    while load_i8(suffix, index) != 0:
        store_i8(message, at, load_i8(suffix, index))
        at = at + 1
        index = index + 1
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(11 if kind == 1 else 13, message))


def _encode_codec_id(encoding) -> int:
    if ptr_is_null(encoding):
        return 0
    if (_encode_codec_equal(encoding, cstr("utf_8"), 5)
        or _encode_codec_equal(encoding, cstr("utf8"), 4)
        or _encode_codec_equal(encoding, cstr("csutf8"), 6)
        or _encode_codec_equal(encoding, cstr("utf8_ucs2"), 9)
        or _encode_codec_equal(encoding, cstr("utf8_ucs4"), 9)
        or _encode_codec_equal(encoding, cstr("utf"), 3)
        or _encode_codec_equal(encoding, cstr("u8"), 2)
        or _encode_codec_equal(encoding, cstr("cp65001"), 7)):
        return 0
    if (_encode_codec_equal(encoding, cstr("ascii"), 5)
        or _encode_codec_equal(encoding, cstr("us_ascii"), 8)
        or _encode_codec_equal(encoding, cstr("646"), 3)
        or _encode_codec_equal(encoding, cstr("ansi_x3_4_1968"), 14)
        or _encode_codec_equal(encoding, cstr("csascii"), 7)
        or _encode_codec_equal(encoding, cstr("iso_ir_6"), 8)
        or _encode_codec_equal(encoding, cstr("us"), 2)
        or _encode_codec_equal(encoding, cstr("ansi_x3.4_1968"), 14)
        or _encode_codec_equal(encoding, cstr("ansi_x3.4_1986"), 14)
        or _encode_codec_equal(encoding, cstr("cp367"), 5)
        or _encode_codec_equal(encoding, cstr("ibm367"), 6)
        or _encode_codec_equal(encoding, cstr("iso646_us"), 9)
        or _encode_codec_equal(encoding, cstr("iso_646.irv_1991"), 16)):
        return 1
    if (_encode_codec_equal(encoding, cstr("latin_1"), 7)
        or _encode_codec_equal(encoding, cstr("latin1"), 6)
        or _encode_codec_equal(encoding, cstr("8859"), 4)
        or _encode_codec_equal(encoding, cstr("iso8859"), 7)
        or _encode_codec_equal(encoding, cstr("latin"), 5)
        or _encode_codec_equal(encoding, cstr("l1"), 2)
        or _encode_codec_equal(encoding, cstr("iso8859_1"), 9)
        or _encode_codec_equal(encoding, cstr("iso_8859_1"), 10)
        or _encode_codec_equal(encoding, cstr("iso_8859_1_1987"), 15)
        or _encode_codec_equal(encoding, cstr("iso_ir_100"), 10)
        or _encode_codec_equal(encoding, cstr("cp819"), 5)
        or _encode_codec_equal(encoding, cstr("ibm819"), 6)
        or _encode_codec_equal(encoding, cstr("csisolatin1"), 11)):
        return 2
    return -1


def _encode_error_id(errors) -> int:
    if ptr_is_null(errors):
        return 0
    if _encode_name_equal(errors, cstr("strict"), 6):
        return 0
    if _encode_name_equal(errors, cstr("ignore"), 6):
        return 1
    if _encode_name_equal(errors, cstr("replace"), 7):
        return 2
    if _encode_name_equal(errors, cstr("surrogateescape"), 15):
        return 3
    if _encode_name_equal(errors, cstr("surrogatepass"), 13):
        return 4
    if _encode_name_equal(errors, cstr("backslashreplace"), 16):
        return 5
    if _encode_name_equal(errors, cstr("xmlcharrefreplace"), 17):
        return 6
    if _encode_name_equal(errors, cstr("namereplace"), 11):
        return 7
    return -1



@c_abi_export("py_text_codec_id")
def py_text_codec_id(encoding) -> int:
    if _encode_validate_name(encoding) == 0:
        return -1
    codec: int = _encode_codec_id(encoding)
    if codec < 0:
        _codec_lookup_error(encoding, 0)
    return codec


@c_abi_export("py_text_decode_codec_id")
def py_text_decode_codec_id(encoding) -> int:
    """py_text_codec_id for a decode: a binary transform names
    codecs.decode() in its LookupError, as bytes.decode() reports it."""
    if _encode_validate_name(encoding) == 0:
        return -1
    codec: int = _encode_codec_id(encoding)
    if codec < 0:
        _codec_lookup_error(encoding, 2)
    return codec


@c_abi_export("py_text_error_id")
def py_text_error_id(errors) -> int:
    if _encode_validate_name(errors) == 0:
        return -1
    # Like CPython, an unknown handler is an error only when first needed.
    return _encode_error_id(errors)

def _encode_width(cp: int) -> int:
    if cp < 128:
        return 1
    if cp < 2048:
        return 2
    if cp < 65536:
        return 3
    return 4


def _encode_invalid(cp: int, codec: int) -> int:
    if codec == 0:
        if cp >= 55296 and cp <= 57343:
            return 1
        return 0
    if codec == 1:
        return 1 if cp > 127 else 0
    return 1 if cp > 255 else 0


def _encode_raise_unicode(s, codec: int, byte_at: int, start: int) -> None:
    end: int = start
    length: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    while byte_at < length:
        cp: int = _utf8_ord_at_byte(s, byte_at)
        if _encode_invalid(cp, codec) == 0:
            break
        byte_at = byte_at + _encode_width(cp)
        end = end + 1
    if codec == 0:
        py_unicode_encode_error(s, cstr("utf-8"), start, end, cstr("surrogates not allowed"))
    elif codec == 1:
        py_unicode_encode_error(s, cstr("ascii"), start, end, cstr("ordinal not in range(128)"))
    else:
        py_unicode_encode_error(s, cstr("latin-1"), start, end, cstr("ordinal not in range(256)"))


def _encode_validate_name(name) -> int:
    if ptr_is_null(name):
        return 1
    if is_tagged_int(name) or load_i32(name, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("encode() argument must be str")))
        return 0
    length: int = load_i64(name, PYSTROBJECT_BYTE_LEN_OFFSET)
    byte_at: int = 0
    index: int = 0
    while byte_at < length:
        cp: int = _utf8_ord_at_byte(name, byte_at)
        if cp == 0:
            py_raise_owned(py_exc_new(2, cstr("embedded null character")))
            return 0
        if cp >= 55296 and cp <= 57343:
            _encode_raise_unicode(name, 0, byte_at, index)
            return 0
        byte_at = byte_at + _encode_width(cp)
        index = index + 1
    return 1


def _str_encode_codec(s, codec: int, mode: int):
    # The guarded exports hold a counted address lease for s throughout this
    # body, including malloc, character helpers, error construction and the
    # result allocation. Scratch storage is independent of the moving GC.
    if ptr_is_null(s) or is_tagged_int(s) or load_i32(s, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("encode() requires str")))
        return null()
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    capacity: int = byte_len
    if mode == 5 or mode == 6:
        if byte_len > 922337203685477580:
            py_raise_owned(py_exc_new(15, cstr("encoded string is too long")))
            return null()
        capacity = byte_len * 10
    buf = malloc(capacity + 1)
    if ptr_is_null(buf):
        py_raise_owned(py_exc_new(19, cstr("cannot allocate encoded string")))
        return null()
    raw = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_at: int = 0
    index: int = 0
    output: int = 0
    while byte_at < byte_len:
        cp: int = _utf8_ord_at_byte(s, byte_at)
        width: int = _encode_width(cp)
        invalid: int = _encode_invalid(cp, codec)
        if invalid == 0 or (codec == 0 and mode == 4):
            if codec == 0:
                step: int = 0
                while step < width:
                    store_i8(buf, output, load_i8(raw, byte_at + step))
                    output = output + 1
                    step = step + 1
            else:
                store_i8(buf, output, cp)
                output = output + 1
        elif mode == 1:
            pass
        elif mode == 2:
            store_i8(buf, output, 63)
            output = output + 1
        elif mode == 3 and cp >= 56448 and cp <= 56575:
            store_i8(buf, output, cp - 56320)
            output = output + 1
        elif mode == 5:
            store_i8(buf, output, 92)
            digits: int = 4
            marker: int = 117
            if cp < 256:
                digits = 2
                marker = 120
            elif cp >= 65536:
                digits = 8
                marker = 85
            store_i8(buf, output + 1, marker)
            output = output + 2
            shift: int = (digits - 1) * 4
            while shift >= 0:
                digit: int = (cp >> shift) & 15
                store_i8(buf, output, 48 + digit if digit < 10 else 87 + digit)
                output = output + 1
                shift = shift - 4
        elif mode == 6:
            store_i8(buf, output, 38)
            store_i8(buf, output + 1, 35)
            output = output + 2
            divisor: int = 1
            while divisor <= cp // 10:
                divisor = divisor * 10
            while divisor > 0:
                store_i8(buf, output, 48 + ((cp // divisor) % 10))
                output = output + 1
                divisor = divisor // 10
            store_i8(buf, output, 59)
            output = output + 1
        else:
            free(buf)
            if mode == -1:
                py_raise_owned(py_exc_new(13, cstr("unknown error handler name")))
            elif mode == 7:
                py_raise_owned(py_exc_new(11, cstr("pcc-native namereplace requires Unicode character names")))
            else:
                _encode_raise_unicode(s, codec, byte_at, index)
            return null()
        byte_at = byte_at + width
        index = index + 1
    result = py_bytes_new(buf, output)
    free(buf)
    return result


def _str_encode_names_leased(s, encoding, errors):
    # NULL denotes an omitted argument. Python None remains a TypeError.
    if _encode_validate_name(encoding) == 0:
        return null()
    if _encode_validate_name(errors) == 0:
        return null()
    codec: int = _encode_codec_id(encoding)
    if codec < 0:
        _codec_lookup_error(encoding, 1)
        return null()
    return _str_encode_codec(s, codec, _encode_error_id(errors))


def _encode_guard_error(kind: int, message: c_ptr) -> None:
    # Infrastructure cleanup must never replace the original codec exception.
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))


@c_abi_export("_pcc_str_encode_guarded")
def _str_encode_guarded(s, encoding, errors, forced_codec: int):
    # Stage incoming borrowed pointers before retention, then keep separate
    # owned slots and local counted leases for every raw codec view. Initial
    # frame publication still consumes the shared runtime-entry contract:
    # threaded raw ABI callers must keep incoming addresses leased until it
    # completes, because frame_enter itself may park before linking slots.
    borrowed = stack_alloc(24)
    store_ptr(borrowed, 0, s)
    store_ptr(borrowed, 8, encoding)
    store_ptr(borrowed, 16, errors)
    pcc_gc_frame_enter(global_addr("pcc_str_encode_borrowed_frame_map"), borrowed)
    owned = stack_alloc(32)
    memset(owned, 0, 32)
    pcc_gc_frame_enter(global_addr("pcc_str_encode_owned_frame_map"), owned)
    index: int = 0
    while index < 3:
        pcc_py_gc_minor_graph_lock()
        value = pcc_gc_load_ptr(null(), ptr_add(borrowed, index * 8))
        pcc_gc_store_root(ptr_add(owned, index * 8), value)
        pcc_py_gc_minor_graph_unlock()
        index = index + 1
    pcc_gc_frame_leave(borrowed)
    tokens = stack_alloc(24)
    store_i64(tokens, 0, -1)
    store_i64(tokens, 8, -1)
    store_i64(tokens, 16, -1)
    index = 0
    while index < 3 and py_err_occurred() == 0:
        acquired: int = pcc_gc_foreign_lease_acquire(ptr_add(owned, index * 8))
        store_i64(tokens, index * 8, acquired)
        if acquired < 0:
            if acquired == -2:
                _encode_guard_error(15, cstr("string encoding address lease overflow"))
            else:
                _encode_guard_error(7, cstr("string encoding requires a stable managed owner"))
            break
        index = index + 1
    result_slot = ptr_add(owned, 24)
    if index == 3:
        if forced_codec < 0:
            result = _str_encode_names_leased(
                pcc_gc_load_ptr(null(), owned),
                pcc_gc_load_ptr(null(), ptr_add(owned, 8)),
                pcc_gc_load_ptr(null(), ptr_add(owned, 16)),
            )
        else:
            policy: int = forced_codec // 3
            result = _str_encode_codec(pcc_gc_load_ptr(null(), owned), forced_codec % 3,
                                       -1 if policy == 8 else policy)
        # Transfer the newly produced owner directly into the already traced
        # result slot before lease release or any source-owner finalizer.
        store_ptr(result_slot, 0, result)
    index = 2
    while index >= 0:
        acquired = load_i64(tokens, index * 8)
        if acquired >= 0:
            status: int = pcc_gc_foreign_lease_release(ptr_add(owned, index * 8), acquired)
            if status < 0:
                _encode_guard_error(7, cstr("string encoding address lease cleanup failed"))
        index = index - 1
    index = 2
    while index >= 0:
        pcc_gc_store_root(ptr_add(owned, index * 8), null())
        index = index - 1
    # Use the shared final no-park transfer after all allocating/error/finalizer
    # work. The result stays in its authoritative slot until frame retirement.
    prior: int = 0
    pcc_py_gc_minor_graph_lock()
    result = pcc_gc_load_ptr(null(), result_slot)
    if not ptr_is_null(result) and not is_tagged_int(result):
        prior = load_i32(result, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
    pcc_gc_pin(result)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(owned)
    result = pcc_gc_take_pinned_slot(result_slot, prior)
    if py_err_occurred() != 0:
        py_decref(result)
        return null()
    return result


@c_abi_export("py_str_encode_with_encoding")
def py_str_encode_with_encoding(s, encoding, errors):
    return _str_encode_guarded(s, encoding, errors, -1)


@c_abi_export("py_text_encode_ids")
def py_text_encode_ids(s, codec: int, mode: int):
    # Mode -1 is an unknown handler name, reported at the first character
    # that needs it; it travels as policy 8 in the packed codec id.
    if codec < 0 or codec > 2 or mode < -1 or mode > 7:
        py_raise_owned(py_exc_new(11, cstr("unsupported native text encoding policy")))
        return null()
    return _str_encode_guarded(s, null(), null(), codec + 3 * (8 if mode < 0 else mode))


@c_abi_export("py_str_utf8_encode")
def py_str_utf8_encode(s):
    return _str_encode_guarded(s, null(), null(), 0)


@c_abi_export("py_str_latin1_encode")
def py_str_latin1_encode(s):
    return _str_encode_guarded(s, null(), null(), 2)


@c_abi_export("py_str_ascii_encode")
def py_str_ascii_encode(s):
    return _str_encode_guarded(s, null(), null(), 1)


@c_abi_export("py_str_byte_slice_i64")
def py_str_byte_slice_i64(s, lo: int, hi: int):
    if ptr_is_null(s) != 0:
        return null()
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if lo < 0:
        lo = 0
    if hi < lo:
        hi = lo
    if lo > byte_len:
        lo = byte_len
    if hi > byte_len:
        hi = byte_len
    out = _str_from_range(ptr_add(ptr_add(s, PYSTROBJECT_DATA_OFFSET), lo), hi - lo)
    if ptr_is_null(out) == 0:
        if load_i64(s, PYSTROBJECT_CP_LEN_OFFSET) == byte_len:
            store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, load_i64(out, PYSTROBJECT_BYTE_LEN_OFFSET))
    return out


@c_abi_export("py_chr_from_i64")
def py_chr_from_i64(codepoint: int):
    if codepoint < 0 or codepoint > 1114111:
        py_raise_owned(py_exc_new(2, cstr("chr() arg not in range(0x110000)")))  # PY_EXC_VALUEERROR
        return null()
    # A lone surrogate is a valid one-character str, stored in the same
    # generalized UTF-8 width that surrogateescape decoding produces.
    length: int = 0
    out = _str_alloc(4)
    if ptr_is_null(out) != 0:
        return null()
    data = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    if codepoint <= 127:
        store_i8(data, 0, codepoint)
        length = 1
    elif codepoint <= 2047:
        store_i8(data, 0, 192 | (codepoint >> 6))
        store_i8(data, 1, 128 | (codepoint & 63))
        length = 2
    elif codepoint <= 65535:
        store_i8(data, 0, 224 | (codepoint >> 12))
        store_i8(data, 1, 128 | ((codepoint >> 6) & 63))
        store_i8(data, 2, 128 | (codepoint & 63))
        length = 3
    else:
        store_i8(data, 0, 240 | (codepoint >> 18))
        store_i8(data, 1, 128 | ((codepoint >> 12) & 63))
        store_i8(data, 2, 128 | ((codepoint >> 6) & 63))
        store_i8(data, 3, 128 | (codepoint & 63))
        length = 4
    store_i64(out, PYSTROBJECT_BYTE_LEN_OFFSET, length)
    store_i8(data, length, 0)
    return out


@c_abi_export("py_str_eq")
def py_str_eq(a, b) -> int:
    if ptr_eq(a, b) != 0:
        return 1
    if ptr_is_null(a) != 0:
        return 0
    if ptr_is_null(b) != 0:
        return 0
    la: int = load_i64(a, PYSTROBJECT_BYTE_LEN_OFFSET)
    lb: int = load_i64(b, PYSTROBJECT_BYTE_LEN_OFFSET)
    if la != lb:
        return 0
    if la == 0:
        return 1
    da = ptr_add(a, PYSTROBJECT_DATA_OFFSET)
    db = ptr_add(b, PYSTROBJECT_DATA_OFFSET)
    return _bytes_eq(da, db, la)


@c_abi_export("py_str_contains")
def py_str_contains(s, sub) -> int:
    if ptr_is_null(s) != 0:
        return 0
    if ptr_is_null(sub) != 0:
        return 0
    sn: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    pn: int = load_i64(sub, PYSTROBJECT_BYTE_LEN_OFFSET)
    bo: int = _byte_find(ptr_add(s, PYSTROBJECT_DATA_OFFSET), sn, ptr_add(sub, PYSTROBJECT_DATA_OFFSET), pn)
    if bo < 0:
        return 0
    return 1


@c_abi_export("py_str_find")
def py_str_find(s, sub) -> int:
    if ptr_is_null(s) != 0:
        return -1
    if ptr_is_null(sub) != 0:
        return -1
    sn: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    pn: int = load_i64(sub, PYSTROBJECT_BYTE_LEN_OFFSET)
    bo: int = _byte_find(ptr_add(s, PYSTROBJECT_DATA_OFFSET), sn, ptr_add(sub, PYSTROBJECT_DATA_OFFSET), pn)
    if bo < 0:
        return -1
    return _byte_offset_to_cp_offset(s, bo)


@c_abi_export("py_str_rfind")
def py_str_rfind(s, sub) -> int:
    if ptr_is_null(s) != 0:
        return -1
    if ptr_is_null(sub) != 0:
        return -1
    sn: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    pn: int = load_i64(sub, PYSTROBJECT_BYTE_LEN_OFFSET)
    bo: int = _byte_rfind(ptr_add(s, PYSTROBJECT_DATA_OFFSET), sn, ptr_add(sub, PYSTROBJECT_DATA_OFFSET), pn)
    if bo < 0:
        return -1
    return _byte_offset_to_cp_offset(s, bo)


@c_abi_export("py_str_find_range")
def py_str_find_range(s, sub, start: int, end: int) -> int:
    # str.find(sub, start[, end]) with codepoint-based window; returns the
    # absolute codepoint offset of the first match, or -1. Mirrors CPython
    # stringlib_find_slice (ADJUST_INDICES over codepoint units).
    if ptr_is_null(s) != 0:
        return -1
    if ptr_is_null(sub) != 0:
        return -1
    cp_len: int = _str_cp_len(s)
    # ADJUST_INDICES: end clamped to [0, cp_len]; start only wrapped for
    # negatives (a start past cp_len is left large so it fails the check
    # below, matching "abc".find("", cp_len+1) == -1).
    e: int = end
    st: int = start
    if e > cp_len:
        e = cp_len
    elif e < 0:
        e = e + cp_len
        if e < 0:
            e = 0
    if st < 0:
        st = st + cp_len
        if st < 0:
            st = 0
    result: int = -1
    ok: int = 1
    if st > cp_len:
        ok = 0
    if e < st:
        ok = 0
    if ok != 0:
        start_byte: int = _utf8_byte_offset_for_codepoint(s, st)
        end_byte: int = _utf8_byte_offset_for_codepoint(s, e)
        win_len: int = end_byte - start_byte
        if win_len >= 0:
            pn: int = load_i64(sub, PYSTROBJECT_BYTE_LEN_OFFSET)
            bo: int = _byte_find(
                ptr_add(s, PYSTROBJECT_DATA_OFFSET + start_byte), win_len, ptr_add(sub, PYSTROBJECT_DATA_OFFSET), pn
            )
            if bo >= 0:
                result = _byte_offset_to_cp_offset(s, start_byte + bo)
    return result


@c_abi_export("py_str_rfind_range")
def py_str_rfind_range(s, sub, start: int, end: int) -> int:
    # str.rfind(sub, start[, end]) with codepoint-based window.
    if ptr_is_null(s) != 0:
        return -1
    if ptr_is_null(sub) != 0:
        return -1
    cp_len: int = _str_cp_len(s)
    e: int = end
    st: int = start
    if e > cp_len:
        e = cp_len
    elif e < 0:
        e = e + cp_len
        if e < 0:
            e = 0
    if st < 0:
        st = st + cp_len
        if st < 0:
            st = 0
    result: int = -1
    ok: int = 1
    if st > cp_len:
        ok = 0
    if e < st:
        ok = 0
    if ok != 0:
        start_byte: int = _utf8_byte_offset_for_codepoint(s, st)
        end_byte: int = _utf8_byte_offset_for_codepoint(s, e)
        win_len: int = end_byte - start_byte
        if win_len >= 0:
            pn: int = load_i64(sub, PYSTROBJECT_BYTE_LEN_OFFSET)
            bo: int = _byte_rfind(
                ptr_add(s, PYSTROBJECT_DATA_OFFSET + start_byte), win_len, ptr_add(sub, PYSTROBJECT_DATA_OFFSET), pn
            )
            if bo >= 0:
                result = _byte_offset_to_cp_offset(s, start_byte + bo)
    return result


def _str_tailmatch_one(s, needle, start: int, end: int, suffix: int) -> int:
    if ptr_is_null(needle) != 0 or is_tagged_int(needle) != 0:
        py_raise_owned(py_exc_new(3, cstr("prefix or suffix must be str")))
        return -1
    if load_i32(needle, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("prefix or suffix must be str")))
        return -1
    length: int = _str_cp_len(s)
    if start < 0:
        start = start + length
        if start < 0:
            start = 0
    if end < 0:
        end = end + length
        if end < 0:
            end = 0
    elif end > length:
        end = length
    if start > length or end < start:
        return 0
    lo: int = _utf8_byte_offset_for_codepoint(s, start)
    hi: int = _utf8_byte_offset_for_codepoint(s, end)
    size: int = load_i64(needle, PYSTROBJECT_BYTE_LEN_OFFSET)
    if size > hi - lo:
        return 0
    if suffix != 0:
        lo = hi - size
    return _bytes_eq(ptr_add(s, PYSTROBJECT_DATA_OFFSET + lo),
                     ptr_add(needle, PYSTROBJECT_DATA_OFFSET), size)


@c_abi_export("py_str_tailmatch_range")
def py_str_tailmatch_range(s, needle, start: int, end: int, suffix: int) -> int:
    if ptr_is_null(s) != 0 or is_tagged_int(s) != 0:
        py_raise_owned(py_exc_new(3, cstr("prefix/suffix receiver must be str")))
        return -1
    if load_i32(s, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("prefix/suffix receiver must be str")))
        return -1
    if ptr_is_null(needle) == 0 and is_tagged_int(needle) == 0:
        if load_i32(needle, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
            length: int = load_i64(needle, PYTUPLEOBJECT_LEN_OFFSET)
            index: int = 0
            while index < length:
                item = load_ptr(needle, PYTUPLEOBJECT_ITEMS_OFFSET + index * 8)
                result: int = _str_tailmatch_one(s, item, start, end, suffix)
                if result != 0:
                    return result
                index = index + 1
            return 0
    return _str_tailmatch_one(s, needle, start, end, suffix)


@c_abi_export("py_str_startswith")
def py_str_startswith(s, prefix) -> int:
    if ptr_is_null(s) != 0:
        return 0
    if ptr_is_null(prefix) != 0:
        return 0
    if load_i32(prefix, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        n: int = py_tuple_len(prefix)
        i: int = 0
        while i < n:
            item = py_tuple_get(prefix, i)
            ok: int = py_str_startswith(s, item)
            py_decref(item)
            if ok != 0:
                return 1
            i = i + 1
        return 0
    ds = _stringlike_data(s)
    dp = _stringlike_data(prefix)
    if ptr_is_null(ds) != 0 or ptr_is_null(dp) != 0:
        return 0
    ls: int = _stringlike_len(s)
    lp: int = _stringlike_len(prefix)
    if lp > ls:
        return 0
    if lp == 0:
        return 1
    return _bytes_eq(ds, dp, lp)


@c_abi_export("py_str_endswith")
def py_str_endswith(s, suffix) -> int:
    if ptr_is_null(s) != 0:
        return 0
    if ptr_is_null(suffix) != 0:
        return 0
    if load_i32(suffix, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_TUPLE:
        n: int = py_tuple_len(suffix)
        i: int = 0
        while i < n:
            item = py_tuple_get(suffix, i)
            ok: int = py_str_endswith(s, item)
            py_decref(item)
            if ok != 0:
                return 1
            i = i + 1
        return 0
    ds0 = _stringlike_data(s)
    df = _stringlike_data(suffix)
    if ptr_is_null(ds0) != 0 or ptr_is_null(df) != 0:
        return 0
    ls: int = _stringlike_len(s)
    lf: int = _stringlike_len(suffix)
    if lf > ls:
        return 0
    if lf == 0:
        return 1
    ds = ptr_add(ds0, ls - lf)
    return _bytes_eq(ds, df, lf)


@c_abi_export("py_str_isdigit")
def py_str_isdigit(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if n == 0:
        return 0
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        if c < 48:  # '0'
            return 0
        if c > 57:  # '9'
            return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isalpha")
def py_str_isalpha(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if n == 0:
        return 0
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        ok: int = 0
        if c >= 97:  # 'a'
            if c <= 122:  # 'z'
                ok = 1
        if ok == 0:
            if c >= 65:  # 'A'
                if c <= 90:  # 'Z'
                    ok = 1
        if ok == 0:
            return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isspace")
def py_str_isspace(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if n == 0:
        return 0
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        # ASCII whitespace: ' ' \t \n \r \v \f
        ws: int = 0
        if c == 32:
            ws = 1
        if c == 9:
            ws = 1
        if c == 10:
            ws = 1
        if c == 13:
            ws = 1
        if c == 11:
            ws = 1
        if c == 12:
            ws = 1
        if ws == 0:
            return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isalnum")
def py_str_isalnum(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if n == 0:
        return 0
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        ok: int = 0
        if c >= 48:  # '0'
            if c <= 57:  # '9'
                ok = 1
        if ok == 0:
            if c >= 97:  # 'a'
                if c <= 122:  # 'z'
                    ok = 1
        if ok == 0:
            if c >= 65:  # 'A'
                if c <= 90:  # 'Z'
                    ok = 1
        if ok == 0:
            return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isupper")
def py_str_isupper(s) -> int:
    # True iff there is at least one cased (ASCII letter) char and no lowercase
    # one (CPython ignores non-cased chars). Mirrors py_str_isupper in
    # py_str_accessors.c.
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    has_upper: int = 0
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        if c >= 97 and c <= 122:  # a-z -> not isupper
            return 0
        if c >= 65 and c <= 90:  # A-Z
            has_upper = 1
        i = i + 1
    return has_upper


@c_abi_export("py_str_islower")
def py_str_islower(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    has_lower: int = 0
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        if c >= 65 and c <= 90:  # A-Z -> not islower
            return 0
        if c >= 97 and c <= 122:  # a-z
            has_lower = 1
        i = i + 1
    return has_lower


@c_abi_export("py_str_isascii")
def py_str_isascii(s) -> int:
    # True iff every byte is < 0x80 (empty string is True). Exact CPython
    # semantics. Mirrors py_str_isascii in py_str_accessors.c.
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        if c >= 128:
            return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isidentifier")
def py_str_isidentifier(s) -> int:
    # ASCII scope (mirrors py_str_isalpha's ASCII-only rule): empty -> False;
    # first char [A-Za-z_]; remaining chars [A-Za-z0-9_]. Matches CPython for
    # ASCII-only input. Mirrors py_str_isidentifier in py_str_accessors.c.
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if n == 0:
        return 0
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        alpha: int = 0
        if c >= 97 and c <= 122:  # a-z
            alpha = 1
        if c >= 65 and c <= 90:  # A-Z
            alpha = 1
        if c == 95:  # '_'
            alpha = 1
        if i == 0:
            if alpha == 0:
                return 0
        else:
            ok: int = alpha
            if c >= 48 and c <= 57:  # 0-9
                ok = 1
            if ok == 0:
                return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isprintable")
def py_str_isprintable(s) -> int:
    # ASCII scope: empty -> True; else every byte must be a printable ASCII
    # char in [0x20, 0x7E]. Non-ASCII bytes count as non-printable. Matches
    # CPython for ASCII-only input. Mirrors py_str_isprintable in
    # py_str_accessors.c.
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        if c < 32:
            return 0
        if c > 126:
            return 0
        i = i + 1
    return 1


@c_abi_export("py_str_isnumeric")
def py_str_isnumeric(s) -> int:
    # ASCII scope (mirrors py_str_isdigit): empty -> False; else every char
    # must be an ASCII digit '0'..'9'. Matches CPython for ASCII-only input.
    return py_str_isdigit(s)


@c_abi_export("py_str_isdecimal")
def py_str_isdecimal(s) -> int:
    # ASCII scope (mirrors py_str_isdigit): empty -> False; else every char
    # must be an ASCII decimal digit '0'..'9'.
    return py_str_isdigit(s)


@c_abi_export("py_str_istitle")
def py_str_istitle(s) -> int:
    # ASCII scope: True iff there is at least one cased (ASCII letter) char and
    # titlecasing holds: an uppercase char must not follow a cased char, and a
    # lowercase char must follow a cased char. Mirrors py_str_istitle in
    # py_str_accessors.c.
    if ptr_is_null(s) != 0:
        return 0
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    cased: int = 0
    prev_cased: int = 0
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        is_upper: int = 0
        if c >= 65 and c <= 90:  # A-Z
            is_upper = 1
        is_lower: int = 0
        if c >= 97 and c <= 122:  # a-z
            is_lower = 1
        if is_upper == 1:
            if prev_cased == 1:
                return 0
            prev_cased = 1
            cased = 1
        else:
            if is_lower == 1:
                if prev_cased == 0:
                    return 0
                prev_cased = 1
                cased = 1
            else:
                prev_cased = 0
        i = i + 1
    return cased


@c_abi_export("py_str_index_of")
def py_str_index_of(s, sub) -> int:
    # str.index(sub): like find() but raises ValueError when sub is absent.
    # Named *_of to avoid the existing py_str_index (s[i] subscript helper).
    idx: int = py_str_find(s, sub)
    if idx < 0:
        py_raise_owned(py_exc_new(2, cstr("substring not found")))  # PY_EXC_VALUEERROR
        return -1
    return idx


@c_abi_export("py_str_rindex_of")
def py_str_rindex_of(s, sub) -> int:
    # str.rindex(sub): like rfind() but raises ValueError when sub is absent.
    idx: int = py_str_rfind(s, sub)
    if idx < 0:
        py_raise_owned(py_exc_new(2, cstr("substring not found")))  # PY_EXC_VALUEERROR
        return -1
    return idx


@c_abi_export("py_str_index_of_range")
def py_str_index_of_range(s, sub, start: int, end: int) -> int:
    # str.index(sub, start[, end]): find_range() but raises ValueError if absent.
    idx: int = py_str_find_range(s, sub, start, end)
    if idx < 0:
        py_raise_owned(py_exc_new(2, cstr("substring not found")))  # PY_EXC_VALUEERROR
        return -1
    return idx


@c_abi_export("py_str_rindex_of_range")
def py_str_rindex_of_range(s, sub, start: int, end: int) -> int:
    # str.rindex(sub, start[, end]): rfind_range() but raises ValueError if absent.
    idx: int = py_str_rfind_range(s, sub, start, end)
    if idx < 0:
        py_raise_owned(py_exc_new(2, cstr("substring not found")))  # PY_EXC_VALUEERROR
        return -1
    return idx


@c_abi_export("py_textwrap_dedent")
def py_textwrap_dedent(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    margin_start: int = -1
    margin_len: int = 0
    start: int = 0
    while start < n:
        next_pos: int = start
        while next_pos < n and (load_i8(data, next_pos) & 0xFF) != 10:
            next_pos = next_pos + 1
        if next_pos < n:
            next_pos = next_pos + 1
        body_end: int = next_pos
        if body_end > start and (load_i8(data, body_end - 1) & 0xFF) == 10:
            body_end = body_end - 1
        if body_end > start and (load_i8(data, body_end - 1) & 0xFF) == 13:
            body_end = body_end - 1

        blank: int = 1
        i: int = start
        while i < body_end and blank != 0:
            ch: int = load_i8(data, i) & 0xFF
            if ch != 32 and ch != 9:
                blank = 0
            i = i + 1
        if blank == 0:
            indent_len: int = 0
            scanning: int = 1
            while start + indent_len < body_end and scanning != 0:
                ch = load_i8(data, start + indent_len) & 0xFF
                if ch == 32 or ch == 9:
                    indent_len = indent_len + 1
                else:
                    scanning = 0
            if margin_start < 0:
                margin_start = start
                margin_len = indent_len
            else:
                limit: int = margin_len
                if indent_len < limit:
                    limit = indent_len
                common: int = 0
                while common < limit and (
                    (load_i8(data, margin_start + common) & 0xFF)
                    == (load_i8(data, start + common) & 0xFF)
                ):
                    common = common + 1
                margin_len = common
        start = next_pos

    result = _str_alloc(n)
    if ptr_is_null(result) != 0:
        return null()
    out = ptr_add(result, PYSTROBJECT_DATA_OFFSET)
    out_len: int = 0
    start = 0
    while start < n:
        next_pos = start
        while next_pos < n and (load_i8(data, next_pos) & 0xFF) != 10:
            next_pos = next_pos + 1
        if next_pos < n:
            next_pos = next_pos + 1
        body_end = next_pos
        if body_end > start and (load_i8(data, body_end - 1) & 0xFF) == 10:
            body_end = body_end - 1
        if body_end > start and (load_i8(data, body_end - 1) & 0xFF) == 13:
            body_end = body_end - 1

        blank = 1
        i = start
        while i < body_end and blank != 0:
            ch = load_i8(data, i) & 0xFF
            if ch != 32 and ch != 9:
                blank = 0
            i = i + 1
        content_start: int = start + margin_len
        if blank != 0:
            content_start = body_end
        content_len: int = body_end - content_start
        if content_len > 0:
            memmove(ptr_add(out, out_len), ptr_add(data, content_start), content_len)
            out_len = out_len + content_len
        ending_len: int = next_pos - body_end
        if ending_len > 0:
            memmove(ptr_add(out, out_len), ptr_add(data, body_end), ending_len)
            out_len = out_len + ending_len
        start = next_pos
    store_i64(result, PYSTROBJECT_BYTE_LEN_OFFSET, out_len)
    store_i64(result, PYSTROBJECT_CP_LEN_OFFSET, -1)
    store_i64(result, PYSTROBJECT_HASH_OFFSET, -1)
    store_i8(result, 40 + out_len, 0)
    return result


@c_abi_export("py_str_strip")
def py_str_strip(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    lo: int = 0
    hi: int = n
    done: int = 0
    while lo < hi and done == 0:
        c: int = load_i8(data, lo) & 0xFF
        if _is_ascii_ws(c) == 0:
            done = 1
        else:
            lo = lo + 1
    done = 0
    while hi > lo and done == 0:
        c: int = load_i8(data, hi - 1) & 0xFF
        if _is_ascii_ws(c) == 0:
            done = 1
        else:
            hi = hi - 1
    return py_str_new(ptr_add(data, lo), hi - lo)


@c_abi_export("py_str_lstrip")
def py_str_lstrip(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    lo: int = 0
    hi: int = n
    done: int = 0
    while lo < hi and done == 0:
        c: int = load_i8(data, lo) & 0xFF
        if _is_ascii_ws(c) == 0:
            done = 1
        else:
            lo = lo + 1
    return py_str_new(ptr_add(data, lo), hi - lo)


@c_abi_export("py_str_rstrip")
def py_str_rstrip(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    lo: int = 0
    hi: int = n
    done: int = 0
    while hi > lo and done == 0:
        c: int = load_i8(data, hi - 1) & 0xFF
        if _is_ascii_ws(c) == 0:
            done = 1
        else:
            hi = hi - 1
    return py_str_new(ptr_add(data, lo), hi - lo)


def _byte_in_chars(c: int, chars_data, n: int) -> int:
    k: int = 0
    while k < n:
        cc: int = load_i8(chars_data, k) & 0xFF
        if cc == c:
            return 1
        k = k + 1
    return 0


@c_abi_export("py_str_strip_chars")
def py_str_strip_chars(s, chars):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    cn: int = load_i64(chars, PYSTROBJECT_BYTE_LEN_OFFSET)
    cdata = ptr_add(chars, PYSTROBJECT_DATA_OFFSET)
    lo: int = 0
    hi: int = n
    done: int = 0
    while lo < hi and done == 0:
        c: int = load_i8(data, lo) & 0xFF
        if _byte_in_chars(c, cdata, cn) == 0:
            done = 1
        else:
            lo = lo + 1
    done = 0
    while hi > lo and done == 0:
        c: int = load_i8(data, hi - 1) & 0xFF
        if _byte_in_chars(c, cdata, cn) == 0:
            done = 1
        else:
            hi = hi - 1
    return py_str_new(ptr_add(data, lo), hi - lo)


@c_abi_export("py_str_lstrip_chars")
def py_str_lstrip_chars(s, chars):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    cn: int = load_i64(chars, PYSTROBJECT_BYTE_LEN_OFFSET)
    cdata = ptr_add(chars, PYSTROBJECT_DATA_OFFSET)
    lo: int = 0
    hi: int = n
    done: int = 0
    while lo < hi and done == 0:
        c: int = load_i8(data, lo) & 0xFF
        if _byte_in_chars(c, cdata, cn) == 0:
            done = 1
        else:
            lo = lo + 1
    return py_str_new(ptr_add(data, lo), hi - lo)


@c_abi_export("py_str_rstrip_chars")
def py_str_rstrip_chars(s, chars):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    cn: int = load_i64(chars, PYSTROBJECT_BYTE_LEN_OFFSET)
    cdata = ptr_add(chars, PYSTROBJECT_DATA_OFFSET)
    lo: int = 0
    hi: int = n
    done: int = 0
    while hi > lo and done == 0:
        c: int = load_i8(data, hi - 1) & 0xFF
        if _byte_in_chars(c, cdata, cn) == 0:
            done = 1
        else:
            hi = hi - 1
    return py_str_new(ptr_add(data, lo), hi - lo)


@c_abi_export("py_str_upper")
def py_str_upper(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    out = _str_alloc(n)
    if ptr_is_null(out) != 0:
        return null()
    src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(src, i) & 0xFF
        if c >= 97:  # 'a'
            if c <= 122:  # 'z'
                c = c - 32  # 'a'-'A' = 32
        store_i8(dst, i, c)
        i = i + 1
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return out


@c_abi_export("py_str_lower")
def py_str_lower(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    out = _str_alloc(n)
    if ptr_is_null(out) != 0:
        return null()
    src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(src, i) & 0xFF
        if c >= 65:  # 'A'
            if c <= 90:  # 'Z'
                c = c + 32  # 'a'-'A' = 32
        store_i8(dst, i, c)
        i = i + 1
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return out


@c_abi_export("py_str_capitalize")
def py_str_capitalize(s):
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    out = _str_alloc(n)
    if ptr_is_null(out) != 0:
        return null()
    src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(src, i) & 0xFF
        if i == 0:
            if c >= 97:  # 'a'
                if c <= 122:  # 'z'
                    c = c - 32
        else:
            if c >= 65:  # 'A'
                if c <= 90:  # 'Z'
                    c = c + 32
        store_i8(dst, i, c)
        i = i + 1
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return out


@c_abi_export("py_str_swapcase")
def py_str_swapcase(s):
    # ASCII swapcase, mirrors py_str_accessors.c::py_str_swapcase.
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    out = _str_alloc(n)
    if ptr_is_null(out) != 0:
        return null()
    src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < n:
        c: int = load_i8(src, i) & 0xFF
        if c >= 97:
            if c <= 122:
                c = c - 32  # a-z -> upper
        else:
            if c >= 65:
                if c <= 90:
                    c = c + 32  # A-Z -> lower
        store_i8(dst, i, c)
        i = i + 1
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return out


@c_abi_export("py_str_title")
def py_str_title(s):
    # ASCII titlecase, mirrors py_str_accessors.c::py_str_title.
    if ptr_is_null(s) != 0:
        return null()
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    out = _str_alloc(n)
    if ptr_is_null(out) != 0:
        return null()
    src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    prev_alpha: int = 0
    i: int = 0
    while i < n:
        c: int = load_i8(src, i) & 0xFF
        is_alpha: int = 0
        if c >= 97:
            if c <= 122:
                is_alpha = 1
        if c >= 65:
            if c <= 90:
                is_alpha = 1
        if is_alpha != 0:
            if prev_alpha == 0:
                if c >= 97:
                    if c <= 122:
                        c = c - 32  # word start -> upper
            else:
                if c >= 65:
                    if c <= 90:
                        c = c + 32  # inside word -> lower
        store_i8(dst, i, c)
        prev_alpha = is_alpha
        i = i + 1
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp)
    return out


@c_abi_export("py_str_casefold")
def py_str_casefold(s):
    # ASCII casefold == lower (mirrors py_str_accessors.c::py_str_casefold).
    return py_str_lower(s)


def _str_concat_subclasses(a, b):
    # Subclass storage is a separate owner. Make independently owned exact
    # snapshots under the payload's scoped lease before any subsequent poll.
    left = py_str_exact_copy(a)
    if ptr_is_null(left) != 0:
        return null()
    pcc_gc_pin(left)
    right = py_str_exact_copy(b)
    if ptr_is_null(right) != 0:
        pcc_gc_unpin(left)
        py_decref(left)
        return null()
    pcc_gc_pin(right)
    result = py_str_concat(left, right)
    pcc_gc_pin(result)
    pcc_gc_unpin(right)
    py_decref(right)
    pcc_gc_unpin(left)
    py_decref(left)
    pcc_gc_unpin(result)
    return result


@c_abi_export("py_str_concat")
def py_str_concat(a, b):
    if ptr_is_null(a) != 0:
        return null()
    if ptr_is_null(b) != 0:
        return null()
    backend: int = pcc_gc_backend()
    moving_inputs: bool = backend == 3 or backend == 4
    if moving_inputs:
        a = pcc_gc_note_relocation_read(a)
        b = pcc_gc_note_relocation_read(b)
    tag_a: int = _type_of(a)
    tag_b: int = _type_of(b)
    if tag_a != PY_TYPE_STR:
        return _str_concat_subclasses(a, b)
    if tag_b != PY_TYPE_STR:
        return _str_concat_subclasses(a, b)
    la: int = load_i64(a, PYSTROBJECT_BYTE_LEN_OFFSET)
    lb: int = load_i64(b, PYSTROBJECT_BYTE_LEN_OFFSET)
    if la < 0:
        pcc_debug_bad_str_concat(a, b, tag_a, tag_b)
        return null()
    if lb < 0:
        pcc_debug_bad_str_concat(a, b, tag_a, tag_b)
        return null()
    if la > 9223372036854775807 - lb:
        pcc_debug_bad_str_concat(a, b, tag_a, tag_b)
        return null()
    total: int = la + lb
    cp_a: int = load_i64(a, PYSTROBJECT_CP_LEN_OFFSET)
    cp_b: int = load_i64(b, PYSTROBJECT_CP_LEN_OFFSET)
    cp_len: int = -1
    if cp_a >= 0 and cp_b >= 0:
        cp_len = cp_a + cp_b
    tmp = null()
    if moving_inputs and total > 0:
        tmp = malloc(total)
        if ptr_is_null(tmp) != 0:
            return null()
        if la > 0:
            memmove(tmp, ptr_add(a, PYSTROBJECT_DATA_OFFSET), la)
        if lb > 0:
            memmove(ptr_add(tmp, la), ptr_add(b, PYSTROBJECT_DATA_OFFSET), lb)
    out = _str_alloc(total)
    if ptr_is_null(out) != 0:
        if ptr_is_null(tmp) == 0:
            free(tmp)
        return null()
    out_data = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    if moving_inputs and total > 0:
        memmove(out_data, tmp, total)
        free(tmp)
    else:
        if la > 0:
            memmove(out_data, ptr_add(a, PYSTROBJECT_DATA_OFFSET), la)
        if lb > 0:
            memmove(ptr_add(out_data, la), ptr_add(b, PYSTROBJECT_DATA_OFFSET), lb)
    if cp_len >= 0:
        store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp_len)
    return out


@c_abi_export("py_str_repeat")
def py_str_repeat(s, n):
    if ptr_is_null(s) != 0:
        return null()
    count: int = _int_or_default(n, 0)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    if count <= 0:
        return py_str_new(null(), 0)
    if byte_len == 0:
        return py_str_new(null(), 0)
    if count > 9223372036854775807 // byte_len:
        return null()
    total: int = count * byte_len
    out = _str_alloc(total)
    if ptr_is_null(out) != 0:
        return null()
    src = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    while i < count:
        memmove(ptr_add(dst, i * byte_len), src, byte_len)
        i = i + 1
    cp: int = load_i64(s, PYSTROBJECT_CP_LEN_OFFSET)
    if cp >= 0:
        store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, cp * count)
    return out


define_global_ptr_null("pcc_str_ascii_chars")


@c_abi_export("pcc_str_ascii_char")
def _ascii_char_str(c: int):
    """The shared immortal one-character string for ASCII code point ``c``.

    CPython hands out cached objects for one-character latin-1 strings, and
    character-scanning loops (lexers, IR text passes, ``for ch in s``) build
    one per step: they were over half of all allocations in a pcc1 compile.
    The table and its strings are malloc'd immortal objects registered as
    managed pointers, like the ``sys`` stream objects, so no collector moves,
    traces or frees them.  A lost race only leaks one equivalent string.
    """
    table = global_load_ptr("pcc_str_ascii_chars")
    if ptr_is_null(table) != 0:
        table = malloc(128 * 8)
        if ptr_is_null(table) != 0:
            return null()
        memset(table, 0, 128 * 8)
        global_store_ptr("pcc_str_ascii_chars", table)
    cached = load_ptr(table, c * 8)
    if ptr_is_null(cached) == 0:
        return cached
    out = malloc(PYSTROBJECT_SIZE + 2)
    if ptr_is_null(out) != 0:
        return null()
    memset(out, 0, PYSTROBJECT_SIZE + 2)
    store_i64(out, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(out, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_STR)
    store_i32(
        out, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_IMMORTAL | PY_FLAG_GC_MALLOC_ALLOC
    )
    if pcc_gc_pointer_register(out) < 0:
        free(out)
        return null()
    store_i64(out, PYSTROBJECT_BYTE_LEN_OFFSET, 1)
    store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, 1)
    store_i64(out, PYSTROBJECT_HASH_OFFSET, -1)
    store_i8(out, PYSTROBJECT_DATA_OFFSET, c)
    store_ptr(table, c * 8, out)
    return out


@c_abi_export("py_str_index")
def py_str_index(s, idx_obj):
    if ptr_is_null(s) != 0:
        return null()
    idx: int = _int_or_default(idx_obj, 0)
    cp_len: int = _str_cp_len(s)
    real: int = _normalise_index(idx, cp_len)
    if real < 0:
        py_raise_owned(py_exc_new(5, cstr("string index out of range")))  # PY_EXC_INDEXERROR
        return null()
    return py_str_char_at_byte(s, _utf8_byte_offset_for_codepoint(s, real))


@c_abi_export("py_str_char_at_byte")
def py_str_char_at_byte(s, bo: int):
    """The one-character str starting at byte offset ``bo``, a codepoint start.

    A caller walking a str in order keeps the byte offset itself, so a step
    costs the character's width instead of a rescan of a non-ASCII string
    from its first byte.  A str subclass instance is read through its payload,
    as ``py_str_byte_len`` does for the bound of such a walk.
    """
    if ptr_is_null(s) == 0 and _type_of(s) != PY_TYPE_STR:
        s = py_str_payload(s)
    w: int = _utf8_codepoint_byte_len(s, bo)
    if w == 1:
        c: int = load_i8(s, PYSTROBJECT_DATA_OFFSET + bo) & 0xFF
        if c < 128:
            cached = _ascii_char_str(c)
            if ptr_is_null(cached) == 0:
                return cached
    out = _str_from_range(ptr_add(ptr_add(s, PYSTROBJECT_DATA_OFFSET), bo), w)
    if ptr_is_null(out) == 0:
        store_i64(out, PYSTROBJECT_CP_LEN_OFFSET, 1)
    return out


@c_abi_export("py_str_codepoint_byte_len")
def py_str_codepoint_byte_len(s, bo: int) -> int:
    """UTF-8 width of the character starting at byte offset ``bo`` (0 past the end)."""
    if ptr_is_null(s) == 0 and _type_of(s) != PY_TYPE_STR:
        s = py_str_payload(s)
    return _utf8_codepoint_byte_len(s, bo)


@c_abi_export("py_str_codepoint_at")
def py_str_codepoint_at(s, idx: int) -> int:
    """Code point of ``s[idx]`` without building the one-character string.

    The value-model projection of a character: compiled ``s[i] == "c"`` and
    ``s[i] in "abc"`` only need the code point.  Index normalisation and the
    IndexError are ``py_str_index``'s; out of range returns -1 with the
    exception set.
    """
    if ptr_is_null(s) != 0:
        return -1
    real: int = _normalise_index(idx, _str_cp_len(s))
    if real < 0:
        py_raise_owned(py_exc_new(5, cstr("string index out of range")))  # PY_EXC_INDEXERROR
        return -1
    return _utf8_ord_at_byte(s, _utf8_byte_offset_for_codepoint(s, real))


@c_abi_export("py_str_contains_codepoint")
def py_str_contains_codepoint(t, cp: int) -> int:
    """``c in t`` for a one-character ``c`` with code point ``cp``.

    UTF-8 is self-synchronizing, so a byte-sequence match of ``cp``'s encoding
    always starts on a code point boundary; ASCII is a plain byte scan.
    """
    if ptr_is_null(t) != 0 or cp < 0:
        return 0
    n: int = load_i64(t, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(t, PYSTROBJECT_DATA_OFFSET)
    i: int = 0
    if cp < 128:
        while i < n:
            if (load_i8(data, i) & 255) == cp:
                return 1
            i = i + 1
        return 0
    b0: int = 0
    b1: int = 0
    b2: int = 0
    b3: int = 0
    width: int = 0
    if cp < 0x800:
        b0 = 0xC0 | (cp >> 6)
        b1 = 0x80 | (cp & 63)
        width = 2
    elif cp < 0x10000:
        b0 = 0xE0 | (cp >> 12)
        b1 = 0x80 | ((cp >> 6) & 63)
        b2 = 0x80 | (cp & 63)
        width = 3
    else:
        b0 = 0xF0 | (cp >> 18)
        b1 = 0x80 | ((cp >> 12) & 63)
        b2 = 0x80 | ((cp >> 6) & 63)
        b3 = 0x80 | (cp & 63)
        width = 4
    while i + width <= n:
        if (
            (load_i8(data, i) & 255) == b0
            and (load_i8(data, i + 1) & 255) == b1
            and (width < 3 or (load_i8(data, i + 2) & 255) == b2)
            and (width < 4 or (load_i8(data, i + 3) & 255) == b3)
        ):
            return 1
        i = i + 1
    return 0


@c_abi_export("py_str_count")
def py_str_count(s, sub) -> int:
    return py_str_count_range(s, sub, null(), null())


@c_abi_export("py_str_count_range")
def py_str_count_range(s, sub, start, end) -> int:
    if ptr_is_null(s) != 0:
        return 0
    if ptr_is_null(sub) != 0:
        return 0
    cp_len: int = _str_cp_len(s)
    lo: int = _int_or_default(start, 0)
    hi: int = _int_or_default(end, cp_len)
    # Match CPython's empty-substring boundary: a start past len(s) returns
    # zero rather than the one match an already-clamped empty slice would have.
    if lo > cp_len:
        return 0
    if lo < 0:
        lo = lo + cp_len
        if lo < 0:
            lo = 0
    if hi < 0:
        hi = hi + cp_len
        if hi < 0:
            hi = 0
    elif hi > cp_len:
        hi = cp_len
    if hi < lo:
        return 0

    pn: int = load_i64(sub, PYSTROBJECT_BYTE_LEN_OFFSET)
    if pn == 0:
        return hi - lo + 1
    byte_lo: int = _utf8_byte_offset_for_codepoint(s, lo)
    byte_hi: int = _utf8_byte_offset_for_codepoint(s, hi)
    sdata = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    pdata = ptr_add(sub, PYSTROBJECT_DATA_OFFSET)
    count: int = 0
    i: int = byte_lo
    while i + pn <= byte_hi:
        # Compare pn bytes at sdata+i vs pdata.
        ok: int = 1
        k: int = 0
        while k < pn and ok == 1:
            ba: int = load_i8(sdata, i + k) & 0xFF
            bb: int = load_i8(pdata, k) & 0xFF
            if ba != bb:
                ok = 0
            k = k + 1
        if ok == 1:
            count = count + 1
            i = i + pn
        else:
            i = i + 1
    return count


@c_abi_export("py_str_hash")
def py_str_hash(s) -> int:
    if ptr_is_null(s) != 0:
        return 0
    cached: int = load_i64(s, PYSTROBJECT_HASH_OFFSET)
    if cached != -1:
        return cached
    h: int = -3750763034362895579
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    n: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    i: int = 0
    while i < n:
        b: int = load_i8(data, i) & 0xFF
        h = h ^ b
        h = h * 1099511628211
        i = i + 1
    if h == -1:
        h = -2
    store_i64(s, PYSTROBJECT_HASH_OFFSET, h)
    return h


def _list_append_str_range(lst, data, start: int, n: int) -> int:
    part = _str_from_range(ptr_add(data, start), n)
    if ptr_is_null(part) != 0:
        py_decref(lst)
        return -1
    py_list_append(lst, part)
    py_decref(part)
    return 0


def _str_splitlines_impl(s, keepends: int):
    if ptr_is_null(s) != 0:
        return null()
    out = py_list_new(4)
    if ptr_is_null(out) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    start: int = 0
    i: int = 0
    while i < byte_len:
        c: int = load_i8(data, i) & 0xFF
        if c == 13 or c == 10:
            end: int = i
            after: int = i + 1
            if c == 13:
                if i + 1 < byte_len:
                    nxt: int = load_i8(data, i + 1) & 0xFF
                    if nxt == 10:
                        after = i + 2
            frag_end: int = end
            if keepends != 0:
                frag_end = after
            if _list_append_str_range(out, data, start, frag_end - start) != 0:
                return null()
            i = after
            start = after
        else:
            i = i + 1
    if start < byte_len:
        if _list_append_str_range(out, data, start, byte_len - start) != 0:
            return null()
    return out


@c_abi_export("py_str_splitlines_keepends")
def py_str_splitlines_keepends(s, keepends: int):
    real_keepends: int = 0
    if keepends != 0:
        real_keepends = 1
    return _str_splitlines_impl(s, real_keepends)


@c_abi_export("py_str_splitlines")
def py_str_splitlines(s):
    return _str_splitlines_impl(s, 0)


def _str_split_whitespace(s):
    out = py_list_new(4)
    if ptr_is_null(out) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    i: int = 0
    while i < byte_len:
        while i < byte_len and _is_ascii_ws(load_i8(data, i) & 0xFF) != 0:
            i = i + 1
        if i >= byte_len:
            return out
        start: int = i
        while i < byte_len and _is_ascii_ws(load_i8(data, i) & 0xFF) == 0:
            i = i + 1
        if _list_append_str_range(out, data, start, i - start) != 0:
            return null()
    return out


def _fill_byte_count(pad: int, fillobj) -> int:
    # Total bytes for `pad` fill codepoints (fill default ' ' is 1 byte).
    if ptr_is_null(fillobj) != 0:
        return pad
    return pad * load_i64(fillobj, PYSTROBJECT_BYTE_LEN_OFFSET)


def _fill_pad(buf, pos: int, pad: int, fillobj) -> int:
    # Write `pad` fill codepoints into buf starting at pos; return new pos.
    if ptr_is_null(fillobj) != 0:
        p: int = 0
        while p < pad:
            store_i8(buf, pos, 32)  # ' '
            pos = pos + 1
            p = p + 1
        return pos
    fill_bytes: int = load_i64(fillobj, PYSTROBJECT_BYTE_LEN_OFFSET)
    fill_data = ptr_add(fillobj, PYSTROBJECT_DATA_OFFSET)
    q: int = 0
    while q < pad:
        b: int = 0
        while b < fill_bytes:
            store_i8(buf, pos, load_i8(fill_data, b))
            pos = pos + 1
            b = b + 1
        q = q + 1
    return pos


@c_abi_export("py_str_rjust")
def py_str_rjust(s, width: int, fillobj):
    n = py_str_len(s)
    if width <= n:
        py_incref(s)
        return s
    pad: int = width - n
    s_bytes: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    s_data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    pad_bytes: int = _fill_byte_count(pad, fillobj)
    buf = py_mem_alloc(s_bytes + pad_bytes + 1)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = _fill_pad(buf, 0, pad, fillobj)
    k: int = 0
    while k < s_bytes:
        store_i8(buf, pos + k, load_i8(s_data, k))
        k = k + 1
    store_i8(buf, pos + s_bytes, 0)
    out = py_str_new(buf, pos + s_bytes)
    py_mem_free(buf)
    return out


@c_abi_export("py_str_ljust")
def py_str_ljust(s, width: int, fillobj):
    n = py_str_len(s)
    if width <= n:
        py_incref(s)
        return s
    pad: int = width - n
    s_bytes: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    s_data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    pad_bytes: int = _fill_byte_count(pad, fillobj)
    buf = py_mem_alloc(s_bytes + pad_bytes + 1)
    if ptr_is_null(buf) != 0:
        return null()
    k: int = 0
    while k < s_bytes:
        store_i8(buf, k, load_i8(s_data, k))
        k = k + 1
    pos: int = _fill_pad(buf, s_bytes, pad, fillobj)
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


def _re_escape_is_special(c: int) -> int:
    # CPython 3.7+ re.escape set: ()[]{}?*+-|^$\.&~# plus whitespace.
    if c == 40 or c == 41 or c == 91 or c == 93 or c == 123 or c == 125:
        return 1
    if c == 63 or c == 42 or c == 43 or c == 45 or c == 124 or c == 94:
        return 1
    if c == 36 or c == 92 or c == 46 or c == 38 or c == 126 or c == 35:
        return 1
    if c == 32 or c == 9 or c == 10 or c == 13 or c == 11 or c == 12:
        return 1
    return 0


@c_abi_export("py_re_escape")
def py_re_escape(s):
    if ptr_is_null(s) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    buf = py_mem_alloc(byte_len * 2 + 1)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = 0
    i: int = 0
    while i < byte_len:
        c: int = load_i8(data, i) & 0xFF
        if _re_escape_is_special(c) != 0:
            store_i8(buf, pos, 92)  # backslash
            pos = pos + 1
        store_i8(buf, pos, c)
        pos = pos + 1
        i = i + 1
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


@c_abi_export("py_str_rsplit_maxsplit")
def py_str_rsplit_maxsplit(s, sep, maxsplit: int):
    # Right split with a maxsplit limit (sep is a non-empty str; the lowering
    # only dispatches the sep-given form).  rsplit without a limit == split.
    if ptr_is_null(s) != 0:
        return null()
    if maxsplit < 0:
        return py_str_split(s, sep)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    sep_data = ptr_add(sep, PYSTROBJECT_DATA_OFFSET)
    sep_len: int = load_i64(sep, PYSTROBJECT_BYTE_LEN_OFFSET)
    if sep_len == 0:
        return py_list_new(0)
    if maxsplit == 0:
        out0 = py_list_new(1)
        if ptr_is_null(out0) != 0:
            return null()
        if _list_append_str_range(out0, data, 0, byte_len) != 0:
            return null()
        return out0
    # Scan from the right; store the kept separator byte positions in
    # ascending slots so the result builds left-to-right (no reversal).
    positions = py_mem_alloc(maxsplit * 8)
    if ptr_is_null(positions) != 0:
        return null()
    count: int = 0
    i: int = byte_len - sep_len
    while i >= 0 and count < maxsplit:
        if _bytes_eq(ptr_add(data, i), sep_data, sep_len) != 0:
            store_i64(positions, (maxsplit - 1 - count) * 8, i)
            count = count + 1
            i = i - sep_len
        else:
            i = i - 1
    out = py_list_new(count + 1)
    if ptr_is_null(out) != 0:
        py_mem_free(positions)
        return null()
    prev: int = 0
    j: int = maxsplit - count
    while j < maxsplit:
        p: int = load_i64(positions, j * 8)
        if _list_append_str_range(out, data, prev, p - prev) != 0:
            py_mem_free(positions)
            return null()
        prev = p + sep_len
        j = j + 1
    py_mem_free(positions)
    if _list_append_str_range(out, data, prev, byte_len - prev) != 0:
        return null()
    return out


@c_abi_export("py_str_center")
def py_str_center(s, width: int, fillobj):
    n = py_str_len(s)
    if width <= n:
        py_incref(s)
        return s
    marg: int = width - n
    left: int = marg // 2 + (marg & width & 1)  # CPython center split
    right: int = marg - left
    s_bytes: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    s_data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    pad_l: int = _fill_byte_count(left, fillobj)
    pad_r: int = _fill_byte_count(right, fillobj)
    buf = py_mem_alloc(s_bytes + pad_l + pad_r + 1)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = _fill_pad(buf, 0, left, fillobj)
    k: int = 0
    while k < s_bytes:
        store_i8(buf, pos + k, load_i8(s_data, k))
        k = k + 1
    pos = pos + s_bytes
    pos = _fill_pad(buf, pos, right, fillobj)
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


@c_abi_export("py_str_zfill")
def py_str_zfill(s, width: int):
    n = py_str_len(s)
    if width <= n:
        py_incref(s)
        return s
    pad: int = width - n
    s_bytes: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    s_data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    sign: int = 0
    if s_bytes > 0:
        c0: int = load_i8(s_data, 0) & 0xFF
        if c0 == 43 or c0 == 45:  # leading '+' or '-'
            sign = 1
    buf = py_mem_alloc(s_bytes + pad + 1)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = 0
    if sign != 0:
        store_i8(buf, 0, load_i8(s_data, 0))
        pos = 1
    z: int = 0
    while z < pad:
        store_i8(buf, pos, 48)  # '0'
        pos = pos + 1
        z = z + 1
    k: int = sign
    while k < s_bytes:
        store_i8(buf, pos, load_i8(s_data, k))
        pos = pos + 1
        k = k + 1
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


@c_abi_export("py_str_expandtabs")
def py_str_expandtabs(s, tabsize: int):
    # str.expandtabs(tabsize): '\t' -> spaces up to the next tabsize column
    # boundary; '\n'/'\r' reset the column. Mirrors py_str_expandtabs in
    # py_str_accessors.c (ASCII/byte-oriented column tracking).
    if ptr_is_null(s) != 0:
        return null()
    s_bytes: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    s_data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    mult: int = 1
    if tabsize > 1:
        mult = tabsize
    buf = py_mem_alloc(s_bytes * mult + 1)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = 0
    col: int = 0
    i: int = 0
    while i < s_bytes:
        c: int = load_i8(s_data, i) & 0xFF
        if c == 9:  # '\t'
            if tabsize > 0:
                spaces: int = tabsize - (col % tabsize)
                k: int = 0
                while k < spaces:
                    store_i8(buf, pos, 32)  # ' '
                    pos = pos + 1
                    k = k + 1
                col = col + spaces
        elif c == 10 or c == 13:  # '\n' or '\r'
            store_i8(buf, pos, c)
            pos = pos + 1
            col = 0
        else:
            store_i8(buf, pos, c)
            pos = pos + 1
            col = col + 1
        i = i + 1
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


@c_abi_export("py_str_translate")
def py_str_translate(s, table):
    # str.translate(table): map each byte through table (dict {ord:ord|str|None}).
    # Absent->keep, None->delete, int->that byte, str->its bytes. Two-pass (size
    # then fill). Mirrors py_str_translate in py_str_accessors.c (byte/ASCII).
    if ptr_is_null(s) != 0:
        return null()
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    none = global_load_ptr("py_None")
    out_len: int = 0
    i: int = 0
    while i < byte_len:
        c: int = load_i8(data, i) & 0xFF
        key = py_int_from_i64(c)
        val = py_dict_get(table, key)
        py_decref(key)
        if ptr_is_null(val) != 0:
            out_len = out_len + 1
        elif ptr_eq(val, none) != 0:
            py_decref(val)
        elif _type_of(val) == PY_TYPE_STR:  # PY_TYPE_STR
            out_len = out_len + load_i64(val, PYSTROBJECT_BYTE_LEN_OFFSET)
            py_decref(val)
        else:
            out_len = out_len + 1
            py_decref(val)
        i = i + 1
    buf = py_mem_alloc(out_len + 1)
    if ptr_is_null(buf) != 0:
        return null()
    pos: int = 0
    i = 0
    while i < byte_len:
        c2: int = load_i8(data, i) & 0xFF
        key2 = py_int_from_i64(c2)
        val2 = py_dict_get(table, key2)
        py_decref(key2)
        if ptr_is_null(val2) != 0:
            store_i8(buf, pos, c2)
            pos = pos + 1
        elif ptr_eq(val2, none) != 0:
            py_decref(val2)
        elif _type_of(val2) == PY_TYPE_STR:  # PY_TYPE_STR
            vlen: int = load_i64(val2, PYSTROBJECT_BYTE_LEN_OFFSET)
            vdata = ptr_add(val2, PYSTROBJECT_DATA_OFFSET)
            j: int = 0
            while j < vlen:
                store_i8(buf, pos, load_i8(vdata, j))
                pos = pos + 1
                j = j + 1
            py_decref(val2)
        else:
            nc: int = py_int_value_i64(val2)
            store_i8(buf, pos, nc & 0xFF)
            pos = pos + 1
            py_decref(val2)
        i = i + 1
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


@c_abi_export("py_str_maketrans")
def py_str_maketrans(x, y):
    # str.maketrans(x, y) -> {ord(x[i]): ord(y[i])} for the 2-arg form (equal
    # length). Mirrors py_str_maketrans in py_str_accessors.c. py_dict_set
    # increfs key+value, so the fresh ints are decref'd after.
    xlen: int = load_i64(x, PYSTROBJECT_BYTE_LEN_OFFSET)
    ylen: int = load_i64(y, PYSTROBJECT_BYTE_LEN_OFFSET)
    if xlen != ylen:
        py_raise_owned(
            py_exc_new(
                2, cstr("the first two maketrans arguments must have equal length")
            )
        )
        return null()
    xdata = ptr_add(x, PYSTROBJECT_DATA_OFFSET)
    ydata = ptr_add(y, PYSTROBJECT_DATA_OFFSET)
    d = py_dict_new()
    if ptr_is_null(d) != 0:
        return null()
    i: int = 0
    while i < xlen:
        k = py_int_from_i64(load_i8(xdata, i) & 0xFF)
        v = py_int_from_i64(load_i8(ydata, i) & 0xFF)
        py_dict_set(d, k, v)
        py_decref(k)
        py_decref(v)
        i = i + 1
    return d


@c_abi_export("py_str_removeprefix")
def py_str_removeprefix(s, prefix):
    if ptr_is_null(s) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    p_data = ptr_add(prefix, PYSTROBJECT_DATA_OFFSET)
    p_len: int = load_i64(prefix, PYSTROBJECT_BYTE_LEN_OFFSET)
    if p_len > 0 and p_len <= byte_len and _bytes_eq(data, p_data, p_len) != 0:
        return _str_from_range(ptr_add(data, p_len), byte_len - p_len)
    py_incref(s)
    return s


@c_abi_export("py_str_removesuffix")
def py_str_removesuffix(s, suffix):
    if ptr_is_null(s) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    suf_data = ptr_add(suffix, PYSTROBJECT_DATA_OFFSET)
    suf_len: int = load_i64(suffix, PYSTROBJECT_BYTE_LEN_OFFSET)
    if (
        suf_len > 0
        and suf_len <= byte_len
        and _bytes_eq(ptr_add(data, byte_len - suf_len), suf_data, suf_len) != 0
    ):
        return _str_from_range(data, byte_len - suf_len)
    py_incref(s)
    return s


@c_abi_export("py_str_partition")
def py_str_partition(s, sep):
    # (before, sep, after) on first occurrence; (s, "", "") if not found.
    # Byte-level: sep boundaries fall on codepoint boundaries for valid UTF-8.
    if ptr_is_null(s) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    sep_data = ptr_add(sep, PYSTROBJECT_DATA_OFFSET)
    sep_len: int = load_i64(sep, PYSTROBJECT_BYTE_LEN_OFFSET)
    found: int = -1
    if sep_len > 0:
        i: int = 0
        while i + sep_len <= byte_len and found < 0:
            if _bytes_eq(ptr_add(data, i), sep_data, sep_len) != 0:
                found = i
            else:
                i = i + 1
    t = py_tuple_new(3)
    if ptr_is_null(t) != 0:
        return null()
    if found < 0:
        py_tuple_set_item(t, 0, s)  # set_item increfs; s is borrowed
        e1 = _str_from_range(data, 0)
        py_tuple_set_item(t, 1, e1)
        py_decref(e1)
        e2 = _str_from_range(data, 0)
        py_tuple_set_item(t, 2, e2)
        py_decref(e2)
    else:
        before = _str_from_range(data, found)
        py_tuple_set_item(t, 0, before)
        py_decref(before)
        mid = _str_from_range(ptr_add(data, found), sep_len)
        py_tuple_set_item(t, 1, mid)
        py_decref(mid)
        after = _str_from_range(
            ptr_add(data, found + sep_len), byte_len - found - sep_len
        )
        py_tuple_set_item(t, 2, after)
        py_decref(after)
    return t


@c_abi_export("py_str_rpartition")
def py_str_rpartition(s, sep):
    # (before, sep, after) on the LAST occurrence; ("", "", s) if not found
    # (rpartition puts the original at the END, unlike partition). Mirrors
    # py_str_rpartition in py_str_accessors.c. No break: loop while found < 0.
    if ptr_is_null(s) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    sep_data = ptr_add(sep, PYSTROBJECT_DATA_OFFSET)
    sep_len: int = load_i64(sep, PYSTROBJECT_BYTE_LEN_OFFSET)
    found: int = -1
    if sep_len > 0 and sep_len <= byte_len:
        i: int = byte_len - sep_len
        while i >= 0 and found < 0:
            if _bytes_eq(ptr_add(data, i), sep_data, sep_len) != 0:
                found = i
            else:
                i = i - 1
    t = py_tuple_new(3)
    if ptr_is_null(t) != 0:
        return null()
    if found < 0:
        e0 = _str_from_range(data, 0)
        py_tuple_set_item(t, 0, e0)
        py_decref(e0)
        e1 = _str_from_range(data, 0)
        py_tuple_set_item(t, 1, e1)
        py_decref(e1)
        py_tuple_set_item(t, 2, s)  # original at the END
    else:
        before = _str_from_range(data, found)
        py_tuple_set_item(t, 0, before)
        py_decref(before)
        mid = _str_from_range(ptr_add(data, found), sep_len)
        py_tuple_set_item(t, 1, mid)
        py_decref(mid)
        after = _str_from_range(
            ptr_add(data, found + sep_len), byte_len - found - sep_len
        )
        py_tuple_set_item(t, 2, after)
        py_decref(after)
    return t


@c_abi_export("py_str_split")
def py_str_split(s, sep):
    if ptr_is_null(s) != 0:
        return null()
    if _type_of(s) != PY_TYPE_STR:
        # dyn-receiver .split fast path can reach non-str objects
        # (e.g. re.Pattern); dispatch generically instead of casting.
        # getattr + call (not py_obj_call_method1, which prepends the
        # receiver and breaks instance-dict function attributes)
        method = py_obj_getattr(s, cstr("split"))
        if ptr_is_null(method) != 0:
            return null()
        args = py_tuple_new(1)
        if ptr_is_null(args) != 0:
            py_decref(method)
            return null()
        if _is_none_or_null(sep) != 0:
            py_tuple_set_item(args, 0, global_load_ptr("py_None"))
        else:
            py_tuple_set_item(args, 0, sep)
        out = py_obj_call(method, args, null())
        py_decref(method)
        py_decref(args)
        return out
    if _is_none_or_null(sep) != 0:
        return _str_split_whitespace(s)

    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    sep_data = ptr_add(sep, PYSTROBJECT_DATA_OFFSET)
    sep_len: int = load_i64(sep, PYSTROBJECT_BYTE_LEN_OFFSET)
    if sep_len == 0:
        return py_list_new(0)

    out = py_list_new(4)
    if ptr_is_null(out) != 0:
        return null()

    start: int = 0
    i: int = 0
    sep_first: int = load_i8(sep_data, 0) & 0xFF
    while i + sep_len <= byte_len:
        ok: int = 0
        first: int = load_i8(data, i) & 0xFF
        if first == sep_first:
            if _bytes_eq(ptr_add(data, i), sep_data, sep_len) != 0:
                ok = 1
        if ok != 0:
            if _list_append_str_range(out, data, start, i - start) != 0:
                return null()
            i = i + sep_len
            start = i
        else:
            i = i + 1

    if _list_append_str_range(out, data, start, byte_len - start) != 0:
        return null()
    return out


def _str_split_whitespace_maxsplit(s, maxsplit: int):
    if maxsplit < 0:
        return _str_split_whitespace(s)
    out = py_list_new(4)
    if ptr_is_null(out) != 0:
        return null()
    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    i: int = 0
    splits: int = 0
    while i < byte_len:
        while i < byte_len and _is_ascii_ws(load_i8(data, i) & 0xFF) != 0:
            i = i + 1
        if i >= byte_len:
            return out
        if splits >= maxsplit:
            if _list_append_str_range(out, data, i, byte_len - i) != 0:
                return null()
            return out
        start: int = i
        while i < byte_len and _is_ascii_ws(load_i8(data, i) & 0xFF) == 0:
            i = i + 1
        if _list_append_str_range(out, data, start, i - start) != 0:
            return null()
        splits = splits + 1
    return out


@c_abi_export("py_str_split_maxsplit")
def py_str_split_maxsplit(s, sep, maxsplit: int):
    if ptr_is_null(s) != 0:
        return null()
    if _type_of(s) != PY_TYPE_STR:
        # generic dispatch for non-str receivers (see py_str_split)
        method = py_obj_getattr(s, cstr("split"))
        if ptr_is_null(method) != 0:
            return null()
        args = py_tuple_new(2)
        if ptr_is_null(args) != 0:
            py_decref(method)
            return null()
        ms = py_int_from_i64(maxsplit)
        if ptr_is_null(ms) != 0:
            py_decref(method)
            py_decref(args)
            return null()
        if _is_none_or_null(sep) != 0:
            py_tuple_set_item(args, 0, global_load_ptr("py_None"))
        else:
            py_tuple_set_item(args, 0, sep)
        py_tuple_set_item(args, 1, ms)
        py_decref(ms)
        out = py_obj_call(method, args, null())
        py_decref(method)
        py_decref(args)
        return out
    if maxsplit < 0:
        return py_str_split(s, sep)
    if _is_none_or_null(sep) != 0:
        return _str_split_whitespace_maxsplit(s, maxsplit)

    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    byte_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    sep_data = ptr_add(sep, PYSTROBJECT_DATA_OFFSET)
    sep_len: int = load_i64(sep, PYSTROBJECT_BYTE_LEN_OFFSET)
    if sep_len == 0:
        return py_list_new(0)

    out = py_list_new(4)
    if ptr_is_null(out) != 0:
        return null()

    start: int = 0
    i: int = 0
    splits: int = 0
    sep_first: int = load_i8(sep_data, 0) & 0xFF
    while i + sep_len <= byte_len and splits < maxsplit:
        ok: int = 0
        first: int = load_i8(data, i) & 0xFF
        if first == sep_first:
            if _bytes_eq(ptr_add(data, i), sep_data, sep_len) != 0:
                ok = 1
        if ok != 0:
            if _list_append_str_range(out, data, start, i - start) != 0:
                return null()
            i = i + sep_len
            start = i
            splits = splits + 1
        else:
            i = i + 1

    if _list_append_str_range(out, data, start, byte_len - start) != 0:
        return null()
    return out


@c_abi_export("py_str_join")
def py_str_join(sep, lst):
    if ptr_is_null(sep) != 0:
        return null()
    if ptr_is_null(lst) != 0:
        return null()
    sep = pcc_gc_note_relocation_read(sep)
    lst = pcc_gc_note_relocation_read(lst)
    if _type_of(sep) != PY_TYPE_STR:
        return null()
    sequence_tag: int = _type_of(lst)
    if sequence_tag != PY_TYPE_LIST and sequence_tag != PY_TYPE_TUPLE:
        return null()
    length: int = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    if sequence_tag == PY_TYPE_TUPLE:
        length = load_i64(lst, PYTUPLEOBJECT_LEN_OFFSET)
    if length == 0:
        return py_str_new(null(), 0)

    sep_len: int = load_i64(sep, PYSTROBJECT_BYTE_LEN_OFFSET)
    if sep_len < 0:
        return null()
    if sequence_tag == PY_TYPE_LIST:
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    else:
        items = ptr_add(lst, PYTUPLEOBJECT_ITEMS_OFFSET)
    total: int = 0
    i: int = 0
    while i < length:
        e = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        if ptr_is_null(e) != 0:
            return null()
        tag: int = _type_of(e)
        if tag != PY_TYPE_STR:
            return null()
        if i > 0:
            if total > 9223372036854775807 - sep_len:
                return null()
            total = total + sep_len
        elem_len: int = load_i64(e, PYSTROBJECT_BYTE_LEN_OFFSET)
        if elem_len < 0:
            return null()
        if total > 9223372036854775807 - elem_len:
            return null()
        total = total + elem_len
        i = i + 1

    out = _str_alloc(total)
    if ptr_is_null(out) != 0:
        return null()

    # Result allocation may run a relocating collector.  Do not reuse the
    # pre-allocation object or list-items pointers while copying the payload.
    sep = pcc_gc_note_relocation_read(sep)
    lst = pcc_gc_note_relocation_read(lst)
    if sequence_tag == PY_TYPE_LIST:
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    else:
        items = ptr_add(lst, PYTUPLEOBJECT_ITEMS_OFFSET)
    out_data = ptr_add(out, PYSTROBJECT_DATA_OFFSET)
    sep_data = ptr_add(sep, PYSTROBJECT_DATA_OFFSET)
    off: int = 0
    i = 0
    while i < length:
        e = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        if i > 0 and sep_len > 0:
            memmove(ptr_add(out_data, off), sep_data, sep_len)
            off = off + sep_len
        elem_len = load_i64(e, PYSTROBJECT_BYTE_LEN_OFFSET)
        if elem_len > 0:
            memmove(ptr_add(out_data, off), ptr_add(e, PYSTROBJECT_DATA_OFFSET), elem_len)
            off = off + elem_len
        i = i + 1
    return out


def _py_str_replace_impl(s, old, new_value, maxreplace: int):
    if ptr_is_null(s) != 0:
        return null()
    if ptr_is_null(old) != 0:
        return null()
    if ptr_is_null(new_value) != 0:
        return null()

    data = ptr_add(s, PYSTROBJECT_DATA_OFFSET)
    old_data = ptr_add(old, PYSTROBJECT_DATA_OFFSET)
    new_data = ptr_add(new_value, PYSTROBJECT_DATA_OFFSET)
    s_len: int = load_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET)
    old_len: int = load_i64(old, PYSTROBJECT_BYTE_LEN_OFFSET)
    new_len: int = load_i64(new_value, PYSTROBJECT_BYTE_LEN_OFFSET)

    if old_len == 0:
        return py_str_new(data, s_len)
    if maxreplace == 0:
        return py_str_new(data, s_len)

    matches: int = 0
    i: int = 0
    old_first: int = load_i8(old_data, 0) & 0xFF
    while i + old_len <= s_len:
        ok: int = 0
        first: int = load_i8(data, i) & 0xFF
        if first == old_first:
            if _bytes_eq(ptr_add(data, i), old_data, old_len) != 0:
                ok = 1
        if ok != 0:
            matches = matches + 1
            if maxreplace > 0:
                if matches >= maxreplace:
                    break
            i = i + old_len
        else:
            i = i + 1

    if matches == 0:
        return py_str_new(data, s_len)

    total: int = s_len + ((new_len - old_len) * matches)
    if total < 0:
        return null()
    out = _str_alloc(total)
    if ptr_is_null(out) != 0:
        return null()
    dst = ptr_add(out, PYSTROBJECT_DATA_OFFSET)

    read: int = 0
    write: int = 0
    replaced: int = 0
    while read + old_len <= s_len:
        match2: int = 0
        first2: int = load_i8(data, read) & 0xFF
        if first2 == old_first:
            if _bytes_eq(ptr_add(data, read), old_data, old_len) != 0:
                match2 = 1
        if match2 != 0:
            do_replace: int = 0
            if maxreplace < 0:
                do_replace = 1
            elif replaced < maxreplace:
                do_replace = 1
            if do_replace != 0:
                if new_len > 0:
                    memmove(ptr_add(dst, write), new_data, new_len)
                    write = write + new_len
                replaced = replaced + 1
                read = read + old_len
            else:
                if old_len > 0:
                    memmove(ptr_add(dst, write), old_data, old_len)
                    write = write + old_len
                read = read + old_len
        else:
            b: int = load_i8(data, read) & 0xFF
            store_i8(dst, write, b)
            write = write + 1
            read = read + 1
    while read < s_len:
        b2: int = load_i8(data, read) & 0xFF
        store_i8(dst, write, b2)
        write = write + 1
        read = read + 1
    return out


@c_abi_export("py_str_replace")
def py_str_replace(s, old, new_value):
    return _py_str_replace_impl(s, old, new_value, -1)


@c_abi_export("py_str_replace_count")
def py_str_replace_count(s, old, new_value, maxreplace: int):
    return _py_str_replace_impl(s, old, new_value, maxreplace)
