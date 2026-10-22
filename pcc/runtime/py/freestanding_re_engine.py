"""Freestanding pcc-Python byte-regex core.

This is the production owner of the strict ASCII regex subset mirrored by
``src/py_re_engine.c``.  It uses only raw fixed-layout buffers and compiler
intrinsics; the C implementation remains an independent differential oracle.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_rmw_i32,
    atomic_rmw_i64,
    atomic_load_i64,
    atomic_store_i32,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    logical_shift_right_i64,
    null,
    page_alloc,
    page_free,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


define_global_i64("pcc_re_compile_count_value", 0)
define_global_ptr_null("pcc_re_cache_head")
define_global_i32("pcc_re_cache_count", 0)
define_global_i32("pcc_re_cache_lock", 0)


thread_safepoint = extern("pcc_thread_safepoint", (), c_void)


# Raw layout literals below mirror: a 263192-byte program (24-byte header,
# 1024-byte name table, 4096 64-byte ops), 16392-byte fragments, and the
# opcode/parser/context offsets documented by the host-C oracle.


@c_abi_export("pcc_re_core__byte")
def _byte(text, index: i64) -> i64:
    return load_i8(text, index) & 255


@c_abi_export("pcc_re_core__copy_bytes")
def _copy_bytes(dst, dst_offset: i64, src, src_offset: i64, length: i64) -> None:
    index: i64 = 0
    while index < length:
        store_i8(dst, dst_offset + index, load_i8(src, src_offset + index))
        index = index + 1


@c_abi_export("pcc_re_core__zero_bytes")
def _zero_bytes(dst, offset: i64, length: i64) -> None:
    index: i64 = 0
    while index < length:
        store_i8(dst, offset + index, 0)
        index = index + 1


@c_abi_export("pcc_re_core__cstrlen")
def _cstrlen(text) -> i64:
    length: i64 = 0
    while load_i8(text, length) != 0:
        length = length + 1
    return length


@c_abi_export("pcc_re_core__cstr_equal")
def _cstr_equal(left, right) -> i64:
    index: i64 = 0
    while load_i8(left, index) == load_i8(right, index):
        if load_i8(left, index) == 0:
            return 1
        index = index + 1
    return 0


@c_abi_export("pcc_re_core__op")
def _op(program, index: i64):
    return ptr_add(program, 1048 + index * 64)


@c_abi_export("pcc_re_core__current")
def _current(ps) -> i64:
    return _byte(load_ptr(ps, 0), load_i64(ps, 8))


@c_abi_export("pcc_re_core__peek")
def _peek(ps, delta: i64) -> i64:
    return _byte(load_ptr(ps, 0), load_i64(ps, 8) + delta)


@c_abi_export("pcc_re_core__advance")
def _advance(ps, count: i64) -> None:
    store_i64(ps, 8, load_i64(ps, 8) + count)


@c_abi_export("pcc_re_core__set_error")
def _set_error(ps) -> None:
    store_i32(ps, 32, -1)


@c_abi_export("pcc_re_core__emit")
def _emit(ps, kind: i64) -> i64:
    program = load_ptr(ps, 16)
    index: i64 = load_i32(program, 0)
    if index >= 4096:
        _set_error(ps)
        return 0
    instruction = _op(program, index)
    _zero_bytes(instruction, 0, 64)
    store_i32(instruction, 0, kind)
    store_i32(instruction, 16, -1)
    store_i32(instruction, 20, -1)
    store_i32(instruction, 24, -1)
    store_i32(program, 0, index + 1)
    return index


@c_abi_export("pcc_re_core__class_set")
def _class_set(bitmap, value: i64) -> None:
    offset: i64 = logical_shift_right_i64(value, 3)
    position: i64 = value & 7
    bit: i64 = 1
    if position == 1:
        bit: i64 = 2
    elif position == 2:
        bit: i64 = 4
    elif position == 3:
        bit: i64 = 8
    elif position == 4:
        bit: i64 = 16
    elif position == 5:
        bit: i64 = 32
    elif position == 6:
        bit: i64 = 64
    elif position == 7:
        bit: i64 = 128
    store_i8(bitmap, offset, _byte(bitmap, offset) | bit)


@c_abi_export("pcc_re_core__class_has")
def _class_has(bitmap, value: i64) -> i64:
    byte: i64 = _byte(bitmap, logical_shift_right_i64(value, 3))
    return logical_shift_right_i64(byte, value & 7) & 1


@c_abi_export("pcc_re_core__class_perl")
def _class_perl(bitmap, kind: i64) -> None:
    value: i64 = 0
    if kind == 100:
        value: i64 = 48
        while value <= 57:
            _class_set(bitmap, value)
            value = value + 1
    elif kind == 119:
        value: i64 = 48
        while value <= 57:
            _class_set(bitmap, value)
            value = value + 1
        value: i64 = 65
        while value <= 90:
            _class_set(bitmap, value)
            value = value + 1
        value: i64 = 97
        while value <= 122:
            _class_set(bitmap, value)
            value = value + 1
        _class_set(bitmap, 95)
    elif kind == 115:
        _class_set(bitmap, 32)
        _class_set(bitmap, 9)
        _class_set(bitmap, 10)
        _class_set(bitmap, 13)
        _class_set(bitmap, 12)
        _class_set(bitmap, 11)


@c_abi_export("pcc_re_core__class_fold_case")
def _class_fold_case(bitmap) -> None:
    value: i64 = 97
    while value <= 122:
        if _class_has(bitmap, value) != 0:
            _class_set(bitmap, value - 32)
        value = value + 1
    value: i64 = 65
    while value <= 90:
        if _class_has(bitmap, value) != 0:
            _class_set(bitmap, value + 32)
        value = value + 1


@c_abi_export("pcc_re_core__class_negate")
def _class_negate(bitmap) -> None:
    index: i64 = 0
    while index < 32:
        store_i8(bitmap, index, _byte(bitmap, index) ^ 255)
        index = index + 1


@c_abi_export("pcc_re_core__literal_escape")
def _literal_escape(value: i64) -> i64:
    if value == 110:
        return 10
    if value == 116:
        return 9
    if value == 114:
        return 13
    if value == 102:
        return 12
    if value == 118:
        return 11
    if value == 92 or value == 46 or value == 42 or value == 43:
        return value
    if value == 63 or value == 40 or value == 41 or value == 91 or value == 93:
        return value
    if value == 123 or value == 125 or value == 124 or value == 94 or value == 36:
        return value
    if value == 45 or value == 47 or value == 39 or value == 34 or value == 32:
        return value
    if value == 44 or value == 58 or value == 59 or value == 61 or value == 60:
        return value
    if value == 62 or value == 35 or value == 33 or value == 38 or value == 126:
        return value
    if value == 64 or value == 37:
        return value
    return -1


@c_abi_export("pcc_re_core__hex_digit")
def _hex_digit(value: i64) -> i64:
    if value >= 48 and value <= 57:
        return value - 48
    if value >= 97 and value <= 102:
        return value - 97 + 10
    if value >= 65 and value <= 70:
        return value - 65 + 10
    return -1


@c_abi_export("pcc_re_core__hex_byte")
def _hex_byte(ps, delta: i64) -> i64:
    high: i64 = _hex_digit(_peek(ps, delta))
    low: i64 = _hex_digit(_peek(ps, delta + 1))
    if high < 0 or low < 0:
        return -1
    return high * 16 + low


@c_abi_export("pcc_re_core__frag_init")
def _frag_init(fragment) -> None:
    store_i32(fragment, 0, -1)
    store_i32(fragment, 4, 0)


@c_abi_export("pcc_re_core__frag_copy")
def _frag_copy(dst, src) -> None:
    store_i32(dst, 0, load_i32(src, 0))
    count: i64 = load_i32(src, 4)
    store_i32(dst, 4, count)
    index: i64 = 0
    while index < count:
        store_i32(dst, 8 + index * 4, load_i32(src, 8 + index * 4))
        index = index + 1


@c_abi_export("pcc_re_core__frag_add")
def _frag_add(ps, fragment, op_index: i64, field: i64) -> None:
    count: i64 = load_i32(fragment, 4)
    if count >= 4096:
        _set_error(ps)
        return
    store_i32(fragment, 8 + count * 4, op_index * 4 + field)
    store_i32(fragment, 4, count + 1)


@c_abi_export("pcc_re_core__frag_patch")
def _frag_patch(ps, fragment, target: i64) -> None:
    program = load_ptr(ps, 16)
    count: i64 = load_i32(fragment, 4)
    index: i64 = 0
    while index < count:
        patch: i64 = load_i32(fragment, 8 + index * 4)
        instruction = _op(program, logical_shift_right_i64(patch, 2))
        field: i64 = patch & 3
        if field == 0:
            store_i32(instruction, 24, target)
        elif field == 1:
            store_i32(instruction, 16, target)
        else:
            store_i32(instruction, 20, target)
        index = index + 1
    store_i32(fragment, 4, 0)


@c_abi_export("pcc_re_core__frag_merge")
def _frag_merge(ps, dst, src) -> None:
    dst_count: i64 = load_i32(dst, 4)
    src_count: i64 = load_i32(src, 4)
    index: i64 = 0
    while index < src_count:
        if dst_count >= 4096:
            _set_error(ps)
            return
        store_i32(dst, 8 + dst_count * 4, load_i32(src, 8 + index * 4))
        dst_count = dst_count + 1
        index = index + 1
    store_i32(dst, 4, dst_count)


@c_abi_export("pcc_re_core__frag_cat")
def _frag_cat(ps, accumulator, piece) -> None:
    if load_i32(piece, 0) < 0:
        return
    if load_i32(accumulator, 0) < 0:
        _frag_copy(accumulator, piece)
        return
    _frag_patch(ps, accumulator, load_i32(piece, 0))
    _frag_merge(ps, accumulator, piece)


@c_abi_export("pcc_re_core__single_atom")
def _single_atom(ps, fragment, atom_kind, atom_char, atom_class) -> i64:
    start: i64 = load_i32(fragment, 0)
    if start < 0 or load_i32(fragment, 4) != 1:
        return 0
    if logical_shift_right_i64(load_i32(fragment, 8), 2) != start:
        return 0
    instruction = _op(load_ptr(ps, 16), start)
    kind: i64 = load_i32(instruction, 0)
    if kind != 1 and kind != 2 and kind != 3:
        return 0
    store_i32(atom_kind, 0, kind)
    store_i32(atom_char, 0, load_i32(instruction, 16))
    _copy_bytes(atom_class, 0, instruction, 32, 32)
    return 1


@c_abi_export("pcc_re_core__parse_class")
def _parse_class(ps, out) -> i64:
    bitmap = stack_alloc(32)
    _zero_bytes(bitmap, 0, 32)
    negate: i64 = 0
    first: i64 = 1
    _advance(ps, 1)
    if _current(ps) == 94:
        negate: i64 = 1
        _advance(ps, 1)
    while True:
        if _current(ps) == 0:
            _set_error(ps)
            return 0
        if _current(ps) == 93 and first == 0:
            break
        first: i64 = 0
        low: i64 = 0
        if _current(ps) == 92:
            escape: i64 = _peek(ps, 1)
            _advance(ps, 2)
            if escape == 100 or escape == 119 or escape == 115:
                _class_perl(bitmap, escape)
                continue
            if escape == 68 or escape == 87 or escape == 83:
                temporary = stack_alloc(32)
                _zero_bytes(temporary, 0, 32)
                _class_perl(temporary, escape + 32)
                _class_negate(temporary)
                index: i64 = 0
                while index < 32:
                    store_i8(bitmap, index, _byte(bitmap, index) | _byte(temporary, index))
                    index = index + 1
                continue
            if escape == 120:
                low = _hex_byte(ps, 0)
                if low < 0:
                    _set_error(ps)
                    return 0
                _advance(ps, 2)
            elif escape == 98:
                low: i64 = 8
            else:
                low = _literal_escape(escape)
                if low < 0:
                    _set_error(ps)
                    return 0
        else:
            low = _current(ps)
            if low >= 128:
                _set_error(ps)
                return 0
            _advance(ps, 1)
        if _current(ps) == 45 and _peek(ps, 1) != 93 and _peek(ps, 1) != 0:
            _advance(ps, 1)
            high: i64 = 0
            if _current(ps) == 92:
                escape = _peek(ps, 1)
                _advance(ps, 2)
                if escape == 120:
                    high = _hex_byte(ps, 0)
                    if high < 0:
                        _set_error(ps)
                        return 0
                    _advance(ps, 2)
                else:
                    high = _literal_escape(escape)
                    if high < 0:
                        _set_error(ps)
                        return 0
            else:
                high = _current(ps)
                if high >= 128:
                    _set_error(ps)
                    return 0
                _advance(ps, 1)
            if high < low:
                _set_error(ps)
                return 0
            value: i64 = low
            while value <= high:
                _class_set(bitmap, value)
                value = value + 1
        else:
            _class_set(bitmap, low)
    _advance(ps, 1)
    if (load_i64(ps, 24) & 2) != 0:
        _class_fold_case(bitmap)
    if negate != 0:
        _class_negate(bitmap)
    index = _emit(ps, 3)
    if load_i32(ps, 32) != 0:
        return 0
    _copy_bytes(_op(load_ptr(ps, 16), index), 32, bitmap, 0, 32)
    _frag_init(out)
    store_i32(out, 0, index)
    _frag_add(ps, out, index, 0)
    return 1


@c_abi_export("pcc_re_core__parse_counts")
def _parse_counts(ps, minimum, maximum, infinite) -> i64:
    position: i64 = load_i64(ps, 8)
    _advance(ps, 1)
    low: i64 = -1
    high: i64 = -1
    inf: i64 = 0
    if _current(ps) >= 48 and _current(ps) <= 57:
        low: i64 = 0
        while _current(ps) >= 48 and _current(ps) <= 57:
            low = low * 10 + _current(ps) - 48
            if low > 9999:
                store_i64(ps, 8, position)
                return 0
            _advance(ps, 1)
    if _current(ps) == 44:
        _advance(ps, 1)
        if _current(ps) >= 48 and _current(ps) <= 57:
            high: i64 = 0
            while _current(ps) >= 48 and _current(ps) <= 57:
                high = high * 10 + _current(ps) - 48
                if high > 9999:
                    store_i64(ps, 8, position)
                    return 0
                _advance(ps, 1)
        else:
            inf: i64 = 1
    else:
        high = low
    if _current(ps) != 125:
        store_i64(ps, 8, position)
        return 0
    if low < 0 and high < 0 and inf == 0:
        store_i64(ps, 8, position)
        return 0
    if low < 0:
        low: i64 = 0
    if inf == 0 and high < low:
        store_i64(ps, 8, position)
        return 0
    _advance(ps, 1)
    store_i32(minimum, 0, low)
    store_i32(maximum, 0, high)
    store_i32(infinite, 0, inf)
    return 1


@c_abi_export("pcc_re_core__parse_atom")
def _parse_atom(ps, out, nullable) -> i64:
    _frag_init(out)
    store_i32(nullable, 0, 0)
    current: i64 = _current(ps)
    if current == 40:
        group_index: i64 = -1
        _advance(ps, 1)
        if _current(ps) == 63:
            if _peek(ps, 1) == 58:
                _advance(ps, 2)
            elif _peek(ps, 1) == 61 or _peek(ps, 1) == 33:
                # ``(?=...)`` / ``(?!...)`` lookahead.  Compiled as its own
                # sub-program ending in the assertion terminator (kind 20) so
                # the matcher can run it from the current position, keep the
                # verdict, and continue without consuming input -- kind 11
                # would publish the sub-match's end as the whole match's end.
                negate: i64 = 0
                if _peek(ps, 1) == 33:
                    negate = 1
                _advance(ps, 2)
                look_program = load_ptr(ps, 16)
                groups_before: i64 = load_i32(look_program, 4)
                inner = stack_alloc(16392)
                inner_nullable = stack_alloc(4)
                store_i32(inner_nullable, 0, 0)
                if _parse_alt(ps, inner, inner_nullable) == 0:
                    return 0
                if _current(ps) != 41:
                    _set_error(ps)
                    return 0
                _advance(ps, 1)
                if load_i32(load_ptr(ps, 16), 4) != groups_before:
                    # A capture written inside a lookahead would have to
                    # survive a failed negative assertion, and a failed
                    # positive one would have to be rolled back.  Refuse the
                    # pattern rather than answer with stale group spans.
                    _set_error(ps)
                    return 0
                assert_end: i64 = _emit(ps, 20)
                if load_i32(ps, 32) != 0:
                    return 0
                inner_start: i64 = load_i32(inner, 0)
                if inner_start < 0:
                    inner_start = assert_end
                _frag_patch(ps, inner, assert_end)
                look: i64 = _emit(ps, 19)
                if load_i32(ps, 32) != 0:
                    return 0
                look_instruction = _op(load_ptr(ps, 16), look)
                store_i32(look_instruction, 16, inner_start)
                store_i32(look_instruction, 20, negate)
                _frag_init(out)
                store_i32(out, 0, look)
                _frag_add(ps, out, look, 0)
                # An assertion consumes nothing, so it is nullable for the
                # empty-loop guards a quantifier would install around it.
                store_i32(nullable, 0, 1)
                return 1
            elif _peek(ps, 1) == 80 and _peek(ps, 2) == 60:
                _advance(ps, 3)
                if not (
                    (_current(ps) >= 65 and _current(ps) <= 90)
                    or (_current(ps) >= 97 and _current(ps) <= 122)
                    or _current(ps) == 95
                ):
                    _set_error(ps)
                    return 0
                name = stack_alloc(32)
                _zero_bytes(name, 0, 32)
                name_length: i64 = 0
                while _current(ps) != 0 and _current(ps) != 62:
                    ch: i64 = _current(ps)
                    if not (
                        (ch >= 65 and ch <= 90)
                        or (ch >= 97 and ch <= 122)
                        or (ch >= 48 and ch <= 57)
                        or ch == 95
                    ):
                        _set_error(ps)
                        return 0
                    if name_length >= 31:
                        _set_error(ps)
                        return 0
                    store_i8(name, name_length, ch)
                    name_length = name_length + 1
                    _advance(ps, 1)
                if _current(ps) != 62 or name_length == 0:
                    _set_error(ps)
                    return 0
                _advance(ps, 1)
                program = load_ptr(ps, 16)
                group_count: i64 = load_i32(program, 4)
                if group_count >= 31:
                    _set_error(ps)
                    return 0
                check: i64 = 1
                while check <= group_count:
                    if _cstr_equal(ptr_add(program, 24 + check * 32), name) != 0:
                        _set_error(ps)
                        return 0
                    check = check + 1
                group_index = group_count + 1
                store_i32(program, 4, group_index)
                _copy_bytes(program, 24 + group_index * 32, name, 0, name_length + 1)
            else:
                _set_error(ps)
                return 0
        else:
            program = load_ptr(ps, 16)
            group_count = load_i32(program, 4)
            if group_count >= 31:
                _set_error(ps)
                return 0
            group_index = group_count + 1
            store_i32(program, 4, group_index)
        open_save: i64 = -1
        close_save: i64 = -1
        if group_index >= 0:
            open_save = _emit(ps, 10)
            if load_i32(ps, 32) != 0:
                return 0
            store_i32(_op(load_ptr(ps, 16), open_save), 16, group_index * 2)
        body = stack_alloc(16392)
        body_nullable = stack_alloc(4)
        store_i32(body_nullable, 0, 0)
        if _parse_alt(ps, body, body_nullable) == 0:
            return 0
        if _current(ps) != 41:
            _set_error(ps)
            return 0
        _advance(ps, 1)
        store_i32(nullable, 0, load_i32(body_nullable, 0))
        if group_index >= 0:
            close_save = _emit(ps, 10)
            if load_i32(ps, 32) != 0:
                return 0
            program = load_ptr(ps, 16)
            store_i32(_op(program, close_save), 16, group_index * 2 + 1)
            body_start: i64 = load_i32(body, 0)
            if body_start < 0:
                body_start = close_save
            store_i32(_op(program, open_save), 24, body_start)
            _frag_patch(ps, body, close_save)
            _frag_init(out)
            store_i32(out, 0, open_save)
            _frag_add(ps, out, close_save, 0)
        else:
            if load_i32(body, 0) < 0:
                index: i64 = _emit(ps, 9)
                if load_i32(ps, 32) != 0:
                    return 0
                store_i32(out, 0, index)
                _frag_add(ps, out, index, 1)
                store_i32(nullable, 0, 1)
                return 1
            store_i32(out, 0, load_i32(body, 0))
            _frag_merge(ps, out, body)
        if load_i32(ps, 32) != 0:
            return 0
        return 1

    if current == 91:
        return _parse_class(ps, out)

    if current == 46:
        _advance(ps, 1)
        index = _emit(ps, 2)
        if load_i32(ps, 32) != 0:
            return 0
        store_i32(out, 0, index)
        _frag_add(ps, out, index, 0)
        return 1

    if current == 94 or current == 36:
        _advance(ps, 1)
        kind: i64 = 4
        if current == 36:
            kind: i64 = 5
        index = _emit(ps, kind)
        if load_i32(ps, 32) != 0:
            return 0
        store_i32(out, 0, index)
        _frag_add(ps, out, index, 0)
        store_i32(nullable, 0, 1)
        return 1

    if current == 92:
        escape: i64 = _peek(ps, 1)
        _advance(ps, 2)
        if (
            escape == 100
            or escape == 68
            or escape == 119
            or escape == 87
            or escape == 115
            or escape == 83
        ):
            bitmap = stack_alloc(32)
            _zero_bytes(bitmap, 0, 32)
            _class_perl(bitmap, escape | 32)
            if escape >= 65 and escape <= 90:
                _class_negate(bitmap)
            index = _emit(ps, 3)
            if load_i32(ps, 32) != 0:
                return 0
            _copy_bytes(_op(load_ptr(ps, 16), index), 32, bitmap, 0, 32)
            store_i32(out, 0, index)
            _frag_add(ps, out, index, 0)
            return 1
        if escape == 98 or escape == 66:
            kind: i64 = 6
            if escape == 66:
                kind: i64 = 7
            index = _emit(ps, kind)
            if load_i32(ps, 32) != 0:
                return 0
            store_i32(out, 0, index)
            _frag_add(ps, out, index, 0)
            store_i32(nullable, 0, 1)
            return 1
        if escape == 65 or escape == 90:
            kind: i64 = 17
            if escape == 90:
                kind: i64 = 18
            index = _emit(ps, kind)
            if load_i32(ps, 32) != 0:
                return 0
            store_i32(out, 0, index)
            _frag_add(ps, out, index, 0)
            store_i32(nullable, 0, 1)
            return 1
        if escape >= 49 and escape <= 57:
            # ``\1``..``\9`` -- match the same text the numbered group
            # captured.  The group has to exist already, exactly as CPython
            # requires ("invalid group reference" otherwise).
            group_ref: i64 = escape - 48
            if group_ref > load_i32(load_ptr(ps, 16), 4):
                _set_error(ps)
                return 0
            index = _emit(ps, 21)
            if load_i32(ps, 32) != 0:
                return 0
            store_i32(_op(load_ptr(ps, 16), index), 16, group_ref)
            store_i32(out, 0, index)
            _frag_add(ps, out, index, 0)
            # A backreference to a zero-width capture consumes nothing, so it
            # is nullable for the empty-loop guards a quantifier installs.
            store_i32(nullable, 0, 1)
            return 1
        literal: i64 = -1
        if escape == 120:
            literal = _hex_byte(ps, 0)
            if literal < 0:
                _set_error(ps)
                return 0
            _advance(ps, 2)
        elif escape == 122 or escape == 117 or escape == 78 or escape == 48:
            _set_error(ps)
            return 0
        else:
            literal = _literal_escape(escape)
            if literal < 0:
                _set_error(ps)
                return 0
        index = _emit(ps, 1)
        if load_i32(ps, 32) != 0:
            return 0
        store_i32(_op(load_ptr(ps, 16), index), 16, literal)
        store_i32(out, 0, index)
        _frag_add(ps, out, index, 0)
        return 1

    if current == 0 or current == 41 or current == 124:
        _set_error(ps)
        return 0
    if current == 42 or current == 43 or current == 63:
        _set_error(ps)
        return 0
    if current >= 128:
        _set_error(ps)
        return 0
    if current == 123:
        saved_position: i64 = load_i64(ps, 8)
        minimum = stack_alloc(4)
        maximum = stack_alloc(4)
        infinite = stack_alloc(4)
        if _parse_counts(ps, minimum, maximum, infinite) != 0:
            store_i64(ps, 8, saved_position)
            _set_error(ps)
            return 0
        store_i64(ps, 8, saved_position)
    _advance(ps, 1)
    index = _emit(ps, 1)
    if load_i32(ps, 32) != 0:
        return 0
    store_i32(_op(load_ptr(ps, 16), index), 16, current)
    store_i32(out, 0, index)
    _frag_add(ps, out, index, 0)
    return 1


@c_abi_export("pcc_re_core__build_question")
def _build_question(ps, atom, lazy: i64, out) -> None:
    split: i64 = _emit(ps, 8)
    if load_i32(ps, 32) != 0:
        return
    _frag_init(out)
    store_i32(out, 0, split)
    instruction = _op(load_ptr(ps, 16), split)
    if lazy != 0:
        _frag_add(ps, out, split, 1)
        store_i32(instruction, 20, load_i32(atom, 0))
    else:
        store_i32(instruction, 16, load_i32(atom, 0))
        _frag_add(ps, out, split, 2)
    _frag_merge(ps, out, atom)


@c_abi_export("pcc_re_core__build_guarded")
def _build_guarded(ps, atom, enter_out, check_out) -> i64:
    program = load_ptr(ps, 16)
    guard: i64 = load_i32(program, 8)
    if guard >= 16:
        _set_error(ps)
        return 0
    store_i32(program, 8, guard + 1)
    enter: i64 = _emit(ps, 15)
    check: i64 = _emit(ps, 16)
    if load_i32(ps, 32) != 0:
        return 0
    store_i32(_op(program, enter), 16, guard)
    store_i32(_op(program, check), 16, guard)
    store_i32(_op(program, enter), 24, load_i32(atom, 0))
    _frag_patch(ps, atom, check)
    store_i32(enter_out, 0, enter)
    store_i32(check_out, 0, check)
    return 1


@c_abi_export("pcc_re_core__build_star")
def _build_star(ps, atom, atom_nullable: i64, lazy: i64, out) -> None:
    if load_i32(atom, 0) < 0:
        jump: i64 = _emit(ps, 9)
        if load_i32(ps, 32) != 0:
            return
        _frag_init(out)
        store_i32(out, 0, jump)
        _frag_add(ps, out, jump, 1)
        return
    program = load_ptr(ps, 16)
    if atom_nullable == 0:
        split: i64 = _emit(ps, 8)
        if load_i32(ps, 32) != 0:
            return
        if lazy != 0:
            store_i32(_op(program, split), 20, load_i32(atom, 0))
        else:
            store_i32(_op(program, split), 16, load_i32(atom, 0))
        _frag_patch(ps, atom, split)
        _frag_init(out)
        store_i32(out, 0, split)
        field: i64 = 2
        if lazy != 0:
            field: i64 = 1
        _frag_add(ps, out, split, field)
        return
    enter_out = stack_alloc(4)
    check_out = stack_alloc(4)
    if _build_guarded(ps, atom, enter_out, check_out) == 0:
        return
    enter: i64 = load_i32(enter_out, 0)
    check: i64 = load_i32(check_out, 0)
    split = _emit(ps, 8)
    if load_i32(ps, 32) != 0:
        return
    store_i32(_op(program, check), 20, split)
    if lazy != 0:
        store_i32(_op(program, split), 20, enter)
    else:
        store_i32(_op(program, split), 16, enter)
    _frag_init(out)
    store_i32(out, 0, split)
    field: i64 = 2
    if lazy != 0:
        field: i64 = 1
    _frag_add(ps, out, split, field)
    _frag_add(ps, out, check, 0)


@c_abi_export("pcc_re_core__build_plus")
def _build_plus(ps, atom, atom_nullable: i64, lazy: i64, out) -> None:
    if load_i32(atom, 0) < 0:
        jump: i64 = _emit(ps, 9)
        if load_i32(ps, 32) != 0:
            return
        _frag_init(out)
        store_i32(out, 0, jump)
        _frag_add(ps, out, jump, 1)
        return
    program = load_ptr(ps, 16)
    if atom_nullable == 0:
        split: i64 = _emit(ps, 8)
        if load_i32(ps, 32) != 0:
            return
        if lazy != 0:
            store_i32(_op(program, split), 20, load_i32(atom, 0))
        else:
            store_i32(_op(program, split), 16, load_i32(atom, 0))
        _frag_patch(ps, atom, split)
        _frag_init(out)
        store_i32(out, 0, load_i32(atom, 0))
        field: i64 = 2
        if lazy != 0:
            field: i64 = 1
        _frag_add(ps, out, split, field)
        return
    enter_out = stack_alloc(4)
    check_out = stack_alloc(4)
    if _build_guarded(ps, atom, enter_out, check_out) == 0:
        return
    enter: i64 = load_i32(enter_out, 0)
    check: i64 = load_i32(check_out, 0)
    split = _emit(ps, 8)
    if load_i32(ps, 32) != 0:
        return
    store_i32(_op(program, check), 20, split)
    if lazy != 0:
        store_i32(_op(program, split), 20, enter)
    else:
        store_i32(_op(program, split), 16, enter)
    _frag_init(out)
    store_i32(out, 0, enter)
    field: i64 = 2
    if lazy != 0:
        field: i64 = 1
    _frag_add(ps, out, split, field)
    _frag_add(ps, out, check, 0)


@c_abi_export("pcc_re_core__build_fast")
def _build_fast(ps, atom, quantifier: i64, lazy: i64, out) -> i64:
    atom_kind = stack_alloc(4)
    atom_char = stack_alloc(4)
    atom_class = stack_alloc(32)
    if _single_atom(ps, atom, atom_kind, atom_char, atom_class) == 0:
        return 0
    kind: i64 = 14
    if quantifier == 42:
        kind: i64 = 12
    elif quantifier == 43:
        kind: i64 = 13
    index: i64 = _emit(ps, kind)
    if load_i32(ps, 32) != 0:
        return 1
    instruction = _op(load_ptr(ps, 16), index)
    store_i32(instruction, 4, lazy)
    store_i32(instruction, 8, load_i32(atom_kind, 0))
    store_i32(instruction, 12, load_i32(atom_char, 0))
    _copy_bytes(instruction, 32, atom_class, 0, 32)
    _frag_init(out)
    store_i32(out, 0, index)
    _frag_add(ps, out, index, 0)
    return 1


@c_abi_export("pcc_re_core__copy_atom_instruction")
def _copy_atom_instruction(ps, kind: i64, character: i64, bitmap, out) -> i64:
    index: i64 = _emit(ps, kind)
    if load_i32(ps, 32) != 0:
        return 0
    instruction = _op(load_ptr(ps, 16), index)
    store_i32(instruction, 16, character)
    store_i32(instruction, 12, character)
    _copy_bytes(instruction, 32, bitmap, 0, 32)
    _frag_init(out)
    store_i32(out, 0, index)
    _frag_add(ps, out, index, 0)
    return 1


@c_abi_export("pcc_re_core__parse_rep")
def _parse_rep(ps, out, nullable) -> i64:
    atom = stack_alloc(16392)
    atom_nullable = stack_alloc(4)
    store_i32(atom_nullable, 0, 0)
    if _parse_atom(ps, atom, atom_nullable) == 0:
        return 0
    quantifier: i64 = _current(ps)
    if quantifier != 42 and quantifier != 43 and quantifier != 63 and quantifier != 123:
        _frag_copy(out, atom)
        store_i32(nullable, 0, load_i32(atom_nullable, 0))
        return 1
    lazy: i64 = 0
    if quantifier == 123:
        minimum = stack_alloc(4)
        maximum = stack_alloc(4)
        infinite = stack_alloc(4)
        if _parse_counts(ps, minimum, maximum, infinite) == 0:
            _frag_copy(out, atom)
            store_i32(nullable, 0, load_i32(atom_nullable, 0))
            return 1
        if _current(ps) == 63:
            lazy: i64 = 1
            _advance(ps, 1)
        if _current(ps) == 42 or _current(ps) == 43 or _current(ps) == 63 or _current(ps) == 123:
            _set_error(ps)
            return 0
        low: i64 = load_i32(minimum, 0)
        high: i64 = load_i32(maximum, 0)
        inf: i64 = load_i32(infinite, 0)
        if low > 64 or (inf == 0 and high > 64):
            _set_error(ps)
            return 0
        atom_kind = stack_alloc(4)
        atom_char = stack_alloc(4)
        atom_class = stack_alloc(32)
        if _single_atom(ps, atom, atom_kind, atom_char, atom_class) == 0:
            _set_error(ps)
            return 0
        accumulator = stack_alloc(16392)
        _frag_copy(accumulator, atom)
        if low == 0 and inf == 0 and high == 0:
            jump: i64 = _emit(ps, 9)
            if load_i32(ps, 32) != 0:
                return 0
            _frag_init(out)
            store_i32(out, 0, jump)
            _frag_add(ps, out, jump, 1)
            store_i32(nullable, 0, 1)
            return 1
        if low == 0:
            piece = stack_alloc(16392)
            if inf == 0:
                if _build_fast(ps, accumulator, 63, lazy, piece) == 0:
                    _set_error(ps)
                    return 0
                _frag_copy(accumulator, piece)
            else:
                if _build_fast(ps, accumulator, 42, lazy, piece) == 0:
                    _set_error(ps)
                    return 0
                _frag_copy(out, piece)
                store_i32(nullable, 0, 1)
                return 1
        copy_index: i64 = 1
        while copy_index < low:
            copy = stack_alloc(16392)
            if _copy_atom_instruction(
                ps,
                load_i32(atom_kind, 0),
                load_i32(atom_char, 0),
                atom_class,
                copy,
            ) == 0:
                return 0
            _frag_cat(ps, accumulator, copy)
            if load_i32(ps, 32) != 0:
                return 0
            copy_index = copy_index + 1
        if inf != 0:
            tail = stack_alloc(16392)
            index = _emit(ps, 12)
            if load_i32(ps, 32) != 0:
                return 0
            instruction = _op(load_ptr(ps, 16), index)
            store_i32(instruction, 4, lazy)
            store_i32(instruction, 8, load_i32(atom_kind, 0))
            store_i32(instruction, 12, load_i32(atom_char, 0))
            _copy_bytes(instruction, 32, atom_class, 0, 32)
            _frag_init(tail)
            store_i32(tail, 0, index)
            _frag_add(ps, tail, index, 0)
            _frag_cat(ps, accumulator, tail)
        else:
            base_count: i64 = low
            if base_count == 0:
                base_count: i64 = 1
            extras: i64 = high - base_count
            extra_index: i64 = 0
            while extra_index < extras:
                piece = stack_alloc(16392)
                index = _emit(ps, 14)
                if load_i32(ps, 32) != 0:
                    return 0
                instruction = _op(load_ptr(ps, 16), index)
                store_i32(instruction, 4, lazy)
                store_i32(instruction, 8, load_i32(atom_kind, 0))
                store_i32(instruction, 12, load_i32(atom_char, 0))
                _copy_bytes(instruction, 32, atom_class, 0, 32)
                _frag_init(piece)
                store_i32(piece, 0, index)
                _frag_add(ps, piece, index, 0)
                _frag_cat(ps, accumulator, piece)
                if load_i32(ps, 32) != 0:
                    return 0
                extra_index = extra_index + 1
        _frag_copy(out, accumulator)
        is_nullable: i64 = 0
        if low == 0:
            is_nullable: i64 = 1
        store_i32(nullable, 0, is_nullable)
        return 1

    _advance(ps, 1)
    if _current(ps) == 63:
        lazy: i64 = 1
        _advance(ps, 1)
    if _current(ps) == 42 or _current(ps) == 43 or _current(ps) == 63 or _current(ps) == 123:
        _set_error(ps)
        return 0
    if load_i32(atom_nullable, 0) == 0 and _build_fast(ps, atom, quantifier, lazy, out) != 0:
        if load_i32(ps, 32) != 0:
            return 0
        is_nullable: i64 = 1
        if quantifier == 43:
            is_nullable: i64 = 0
        store_i32(nullable, 0, is_nullable)
        return 1
    if quantifier == 63:
        _build_question(ps, atom, lazy, out)
        store_i32(nullable, 0, 1)
    elif quantifier == 42:
        _build_star(ps, atom, load_i32(atom_nullable, 0), lazy, out)
        store_i32(nullable, 0, 1)
    else:
        _build_plus(ps, atom, load_i32(atom_nullable, 0), lazy, out)
        store_i32(nullable, 0, load_i32(atom_nullable, 0))
    if load_i32(ps, 32) != 0:
        return 0
    return 1


@c_abi_export("pcc_re_core__parse_cat")
def _parse_cat(ps, out, nullable) -> i64:
    accumulator = stack_alloc(16392)
    _frag_init(accumulator)
    accumulator_nullable: i64 = 1
    while _current(ps) != 0 and _current(ps) != 124 and _current(ps) != 41:
        piece = stack_alloc(16392)
        piece_nullable = stack_alloc(4)
        store_i32(piece_nullable, 0, 0)
        if _parse_rep(ps, piece, piece_nullable) == 0:
            return 0
        _frag_cat(ps, accumulator, piece)
        if load_i32(piece_nullable, 0) == 0:
            accumulator_nullable: i64 = 0
        if load_i32(ps, 32) != 0:
            return 0
    _frag_copy(out, accumulator)
    if load_i32(accumulator, 0) < 0:
        accumulator_nullable: i64 = 1
    store_i32(nullable, 0, accumulator_nullable)
    return 1


@c_abi_export("pcc_re_core__parse_alt")
def _parse_alt(ps, out, nullable) -> i64:
    left = stack_alloc(16392)
    left_nullable = stack_alloc(4)
    store_i32(left_nullable, 0, 0)
    if _parse_cat(ps, left, left_nullable) == 0:
        return 0
    if _current(ps) != 124:
        _frag_copy(out, left)
        store_i32(nullable, 0, load_i32(left_nullable, 0))
        return 1
    _advance(ps, 1)
    split: i64 = _emit(ps, 8)
    if load_i32(ps, 32) != 0:
        return 0
    rest = stack_alloc(16392)
    rest_nullable = stack_alloc(4)
    store_i32(rest_nullable, 0, 0)
    if _parse_alt(ps, rest, rest_nullable) == 0:
        return 0
    _frag_init(out)
    store_i32(out, 0, split)
    instruction = _op(load_ptr(ps, 16), split)
    if load_i32(left, 0) >= 0:
        store_i32(instruction, 16, load_i32(left, 0))
        _frag_merge(ps, out, left)
    else:
        _frag_add(ps, out, split, 1)
    if load_i32(rest, 0) >= 0:
        store_i32(instruction, 20, load_i32(rest, 0))
        _frag_merge(ps, out, rest)
    else:
        _frag_add(ps, out, split, 2)
    value: i64 = 0
    if load_i32(left_nullable, 0) != 0 or load_i32(rest_nullable, 0) != 0:
        value: i64 = 1
    store_i32(nullable, 0, value)
    if load_i32(ps, 32) != 0:
        return 0
    return 1


@c_abi_export("pcc_re_core__compile")
def _compile(pattern, flags: i64, program) -> i64:
    counter = global_addr("pcc_re_compile_count_value")
    atomic_rmw_i64("add", counter, 0, 1, "relaxed")
    store_i32(program, 0, 0)
    store_i32(program, 4, 0)
    store_i32(program, 8, 0)
    store_i64(program, 16, flags)
    _zero_bytes(program, 24, 1024)
    parser = stack_alloc(40)
    store_ptr(parser, 0, pattern)
    store_i64(parser, 8, 0)
    store_ptr(parser, 16, program)
    store_i64(parser, 24, flags)
    store_i32(parser, 32, 0)
    top = stack_alloc(16392)
    nullable = stack_alloc(4)
    store_i32(nullable, 0, 0)
    if _parse_alt(parser, top, nullable) == 0 or load_i32(parser, 32) != 0:
        return -1
    if _current(parser) != 0:
        return -1
    match_index: i64 = _emit(parser, 11)
    if load_i32(parser, 32) != 0:
        return -1
    if load_i32(top, 0) < 0:
        store_i32(top, 0, match_index)
    else:
        _frag_patch(parser, top, match_index)
    return load_i32(top, 0)


@c_abi_export("pcc_re_core__cache_acquire")
def _cache_acquire() -> None:
    while atomic_rmw_i32(
        "xchg", global_addr("pcc_re_cache_lock"), 0, 1, "acquire"
    ) != 0:
        thread_safepoint()


@c_abi_export("pcc_re_core__cache_release")
def _cache_release() -> None:
    atomic_store_i32(global_addr("pcc_re_cache_lock"), 0, 0, "release")


@c_abi_export("pcc_re_core__compiled_program")
def _compiled_program(pattern, flags: i64, scratch, program_out) -> i64:
    if (
        ptr_is_null(pattern) != 0
        or ptr_is_null(scratch) != 0
        or ptr_is_null(program_out) != 0
    ):
        return -3
    store_ptr(program_out, 0, null())
    _cache_acquire()
    node = global_load_ptr("pcc_re_cache_head")
    while ptr_is_null(node) == 0:
        if load_i64(node, 8) == flags and _cstr_equal(
            ptr_add(node, 263224), pattern
        ) != 0:
            start: i64 = load_i32(node, 16)
            store_ptr(program_out, 0, ptr_add(node, 32))
            _cache_release()
            return start
        node = load_ptr(node, 0)
    count_slot = global_addr("pcc_re_cache_count")
    count: i64 = load_i32(count_slot, 0)
    if count < 64:
        pattern_length: i64 = _cstrlen(pattern)
        requested: i64 = 263224 + pattern_length + 1
        mapping_size: i64 = (requested + 4095) & -4096
        fresh = page_alloc(mapping_size)
        if ptr_is_null(fresh) == 0:
            store_i64(fresh, 24, mapping_size)
            program = ptr_add(fresh, 32)
            cached_pattern = ptr_add(fresh, 263224)
            _copy_bytes(cached_pattern, 0, pattern, 0, pattern_length + 1)
            start = _compile(pattern, flags, program)
            if start >= 0:
                store_ptr(fresh, 0, global_load_ptr("pcc_re_cache_head"))
                store_i64(fresh, 8, flags)
                store_i32(fresh, 16, start)
                global_store_ptr("pcc_re_cache_head", fresh)
                store_i32(count_slot, 0, count + 1)
                store_ptr(program_out, 0, program)
                _cache_release()
                return start
            page_free(fresh, mapping_size)
            _cache_release()
            return start
    _cache_release()
    start = _compile(pattern, flags, scratch)
    if start >= 0:
        store_ptr(program_out, 0, scratch)
    return start


# Matcher context is 176 raw bytes: program/text/caps pointers, lengths,
# sixteen empty-loop guard slots, recursion depth, and a limit flag.


@c_abi_export("pcc_re_core__fold_byte")
def _fold_byte(value: i64) -> i64:
    if value >= 65 and value <= 90:
        return value + 32
    return value


@c_abi_export("pcc_re_core__is_word_byte")
def _is_word_byte(value: i64) -> i64:
    if value >= 48 and value <= 57:
        return 1
    if value >= 65 and value <= 90:
        return 1
    if value >= 97 and value <= 122:
        return 1
    if value == 95:
        return 1
    return 0


@c_abi_export("pcc_re_core__atom_ok")
def _atom_ok(instruction, kind: i64, text, length: i64, position: i64, flags: i64) -> i64:
    if position >= length:
        return 0
    value: i64 = _byte(text, position)
    if kind == 1:
        target: i64 = load_i32(instruction, 12) & 255
        if (flags & 2) != 0:
            if _fold_byte(value) == _fold_byte(target):
                return 1
            return 0
        if value == target:
            return 1
        return 0
    if kind == 2:
        if value != 10 or (flags & 16) != 0:
            return 1
        return 0
    if kind == 3:
        return _class_has(ptr_add(instruction, 32), value)
    return 0


@c_abi_export("pcc_re_core__match_star")
def _match_star(context, instruction, position: i64, minimum: i64) -> i64:
    text = load_ptr(context, 8)
    length: i64 = load_i64(context, 16)
    program = load_ptr(context, 0)
    flags: i64 = load_i64(program, 16)
    run: i64 = 0
    while _atom_ok(
        instruction,
        load_i32(instruction, 8),
        text,
        length,
        position + run,
        flags,
    ) != 0:
        run = run + 1
    if run < minimum:
        return 0
    if load_i32(instruction, 4) != 0:
        count: i64 = minimum
        while count <= run:
            result: i64 = _match(
                context, load_i32(instruction, 24), position + count
            )
            if result != 0:
                return result
            count = count + 1
        return 0
    count = run
    while count >= minimum:
        result = _match(context, load_i32(instruction, 24), position + count)
        if result != 0:
            return result
        count = count - 1
    return 0


@c_abi_export("pcc_re_core__match")
def _match(context, pc: i64, position: i64) -> i64:
    depth: i64 = load_i32(context, 168) + 1
    store_i32(context, 168, depth)
    if depth > 8192:
        store_i32(context, 172, 1)
        store_i32(context, 168, depth - 1)
        return 0
    program = load_ptr(context, 0)
    text = load_ptr(context, 8)
    length: i64 = load_i64(context, 16)
    flags: i64 = load_i64(program, 16)
    while True:
        if pc < 0 or pc >= load_i32(program, 0):
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        instruction = _op(program, pc)
        kind: i64 = load_i32(instruction, 0)
        if kind == 1:
            if position < length:
                value: i64 = _byte(text, position)
                target: i64 = load_i32(instruction, 16) & 255
                matched: i64 = 0
                if (flags & 2) != 0:
                    if _fold_byte(value) == _fold_byte(target):
                        matched: i64 = 1
                elif value == target:
                    matched: i64 = 1
                if matched != 0:
                    position = position + 1
                    pc = load_i32(instruction, 24)
                    continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 2:
            if position < length and (_byte(text, position) != 10 or (flags & 16) != 0):
                position = position + 1
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 3:
            if position < length and _class_has(
                ptr_add(instruction, 32), _byte(text, position)
            ) != 0:
                position = position + 1
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 4:
            at_start: i64 = 0
            if position == 0:
                at_start: i64 = 1
            elif (flags & 8) != 0 and position > 0 and _byte(text, position - 1) == 10:
                at_start: i64 = 1
            if at_start != 0:
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 5:
            at_end: i64 = 0
            if position == length:
                at_end: i64 = 1
            elif (flags & 8) != 0:
                if _byte(text, position) == 10:
                    at_end: i64 = 1
            elif position == length - 1 and _byte(text, position) == 10:
                at_end: i64 = 1
            if at_end != 0:
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 17:
            if position == 0:
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 18:
            if position == length:
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 6 or kind == 7:
            before: i64 = 0
            after: i64 = 0
            if position > 0:
                before = _is_word_byte(_byte(text, position - 1))
            if position < length:
                after = _is_word_byte(_byte(text, position))
            boundary: i64 = 0
            if before != after:
                boundary: i64 = 1
            accepted: i64 = boundary
            if kind == 7:
                if boundary == 0:
                    accepted: i64 = 1
                else:
                    accepted: i64 = 0
            if accepted != 0:
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 9:
            pc = load_i32(instruction, 16)
            continue
        if kind == 8:
            result: i64 = _match(context, load_i32(instruction, 16), position)
            if result != 0:
                store_i32(context, 168, load_i32(context, 168) - 1)
                return result
            pc = load_i32(instruction, 20)
            continue
        if kind == 10:
            slot: i64 = load_i32(instruction, 16)
            if slot < 0 or slot >= load_i32(context, 32):
                store_i32(context, 168, load_i32(context, 168) - 1)
                return 0
            caps = load_ptr(context, 24)
            old: i64 = load_i64(caps, slot * 8)
            store_i64(caps, slot * 8, position)
            result = _match(context, load_i32(instruction, 24), position)
            if result == 0:
                store_i64(caps, slot * 8, old)
            store_i32(context, 168, load_i32(context, 168) - 1)
            return result
        if kind == 15:
            slot = load_i32(instruction, 16)
            if slot < 0 or slot >= 16:
                store_i32(context, 168, load_i32(context, 168) - 1)
                return 0
            old = load_i64(context, 40 + slot * 8)
            store_i64(context, 40 + slot * 8, position)
            result = _match(context, load_i32(instruction, 24), position)
            if result == 0:
                store_i64(context, 40 + slot * 8, old)
            store_i32(context, 168, load_i32(context, 168) - 1)
            return result
        if kind == 16:
            slot = load_i32(instruction, 16)
            if slot >= 0 and slot < 16 and position != load_i64(
                context, 40 + slot * 8
            ):
                pc = load_i32(instruction, 20)
            else:
                pc = load_i32(instruction, 24)
            continue
        if kind == 12 or kind == 13:
            minimum: i64 = 0
            if kind == 13:
                minimum: i64 = 1
            result = _match_star(context, instruction, position, minimum)
            store_i32(context, 168, load_i32(context, 168) - 1)
            return result
        if kind == 14:
            if load_i32(instruction, 4) != 0:
                result = _match(context, load_i32(instruction, 24), position)
                if result != 0:
                    store_i32(context, 168, load_i32(context, 168) - 1)
                    return result
                if _atom_ok(
                    instruction,
                    load_i32(instruction, 8),
                    text,
                    length,
                    position,
                    flags,
                ) != 0:
                    position = position + 1
                    pc = load_i32(instruction, 24)
                    continue
                store_i32(context, 168, load_i32(context, 168) - 1)
                return 0
            if _atom_ok(
                instruction,
                load_i32(instruction, 8),
                text,
                length,
                position,
                flags,
            ) != 0:
                result = _match(context, load_i32(instruction, 24), position + 1)
                if result != 0:
                    store_i32(context, 168, load_i32(context, 168) - 1)
                    return result
            pc = load_i32(instruction, 24)
            continue
        if kind == 19:
            sub: i64 = _match(context, load_i32(instruction, 16), position)
            holds: i64 = 0
            if load_i32(instruction, 20) != 0:
                if sub == 0:
                    holds = 1
            elif sub != 0:
                holds = 1
            if holds != 0:
                # Zero width: continue from the same position.
                pc = load_i32(instruction, 24)
                continue
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 0
        if kind == 20:
            # Assertion sub-program success.  Unlike kind 11 this must not
            # write caps[1]: the enclosing match's end is not here.
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 1
        if kind == 21:
            # Backreference: the numbered group's captured text must appear
            # again at the current position.  An unmatched group never
            # matches, as in CPython.
            group_ref: i64 = load_i32(instruction, 16)
            ref_slot: i64 = group_ref * 2
            if ref_slot + 1 >= load_i32(context, 32):
                store_i32(context, 168, load_i32(context, 168) - 1)
                return 0
            ref_caps = load_ptr(context, 24)
            ref_start: i64 = load_i64(ref_caps, ref_slot * 8)
            ref_end: i64 = load_i64(ref_caps, (ref_slot + 1) * 8)
            if ref_start < 0 or ref_end < ref_start:
                store_i32(context, 168, load_i32(context, 168) - 1)
                return 0
            ref_len: i64 = ref_end - ref_start
            if position + ref_len > length:
                store_i32(context, 168, load_i32(context, 168) - 1)
                return 0
            ref_i: i64 = 0
            while ref_i < ref_len:
                ref_a: i64 = _byte(text, ref_start + ref_i)
                ref_b: i64 = _byte(text, position + ref_i)
                if (flags & 2) != 0:
                    if _fold_byte(ref_a) != _fold_byte(ref_b):
                        store_i32(context, 168, load_i32(context, 168) - 1)
                        return 0
                elif ref_a != ref_b:
                    store_i32(context, 168, load_i32(context, 168) - 1)
                    return 0
                ref_i = ref_i + 1
            position = position + ref_len
            pc = load_i32(instruction, 24)
            continue
        if kind == 11:
            caps = load_ptr(context, 24)
            store_i64(caps, 8, position)
            store_i32(context, 168, load_i32(context, 168) - 1)
            return 1
        store_i32(context, 168, load_i32(context, 168) - 1)
        return 0


@c_abi_export("pcc_re_core__run_flags")
def _run_flags(
    pattern,
    flags: i64,
    text,
    text_length: i64,
    start: i64,
    search: i64,
    caps,
    caps_length: i64,
    groups_out,
) -> i64:
    if (
        ptr_is_null(pattern) != 0
        or ptr_is_null(text) != 0
        or ptr_is_null(caps) != 0
        or ptr_is_null(groups_out) != 0
    ):
        return -3
    if start < 0:
        start: i64 = 0
    index: i64 = 0
    while index < text_length:
        if _byte(text, index) >= 128:
            return -4
        index = index + 1
    if (flags & ~26) != 0:
        return -1
    scratch = stack_alloc(263192)
    program_out = stack_alloc(8)
    start_pc: i64 = _compiled_program(pattern, flags, scratch, program_out)
    if start_pc < 0:
        return -1
    program = load_ptr(program_out, 0)
    group_count: i64 = load_i32(program, 4)
    store_i64(groups_out, 0, group_count)
    needed: i64 = (group_count + 1) * 2
    if caps_length < needed:
        return -3
    context = stack_alloc(176)
    store_ptr(context, 0, program)
    store_ptr(context, 8, text)
    store_i64(context, 16, text_length)
    store_ptr(context, 24, caps)
    store_i32(context, 32, needed)
    position: i64 = start
    last_position: i64 = start
    if search != 0:
        last_position = text_length
    while position <= last_position:
        index: i64 = 0
        while index < needed:
            store_i64(caps, index * 8, -1)
            index = index + 1
        index: i64 = 0
        while index < 16:
            store_i64(context, 40 + index * 8, -1)
            index = index + 1
        store_i32(context, 168, 0)
        store_i32(context, 172, 0)
        store_i64(caps, 0, position)
        result: i64 = _match(context, start_pc, position)
        if load_i32(context, 172) != 0:
            return -2
        if result == 1:
            return 1
        position = position + 1
    index: i64 = 0
    while index < needed:
        store_i64(caps, index * 8, -1)
        index = index + 1
    return 0


@c_abi_export("pcc_re_engine_supported")
def pcc_re_engine_supported(pattern) -> i64:
    if ptr_is_null(pattern) != 0:
        return 0
    scratch = stack_alloc(263192)
    program_out = stack_alloc(8)
    if _compiled_program(pattern, 0, scratch, program_out) >= 0:
        return 1
    return 0


@c_abi_export("pcc_re_engine_supported_flags")
def pcc_re_engine_supported_flags(pattern, flags: i64) -> i64:
    if ptr_is_null(pattern) != 0 or (flags & ~26) != 0:
        return 0
    scratch = stack_alloc(263192)
    program_out = stack_alloc(8)
    if _compiled_program(pattern, flags, scratch, program_out) >= 0:
        return 1
    return 0


@c_abi_export("pcc_re_engine_compile_count")
def pcc_re_engine_compile_count() -> i64:
    return atomic_load_i64(global_addr("pcc_re_compile_count_value"), 0, "relaxed")


@c_abi_export("pcc_re_engine_run_flags")
def pcc_re_engine_run_flags(
    pattern,
    flags: i64,
    text,
    text_length: i64,
    start: i64,
    search: i64,
    caps,
    caps_length: i64,
    groups_out,
) -> i64:
    return _run_flags(
        pattern,
        flags,
        text,
        text_length,
        start,
        search,
        caps,
        caps_length,
        groups_out,
    )


@c_abi_export("pcc_re_engine_run_from")
def pcc_re_engine_run_from(
    pattern,
    text,
    text_length: i64,
    start: i64,
    search: i64,
    caps,
    caps_length: i64,
    groups_out,
) -> i64:
    return _run_flags(
        pattern,
        0,
        text,
        text_length,
        start,
        search,
        caps,
        caps_length,
        groups_out,
    )


@c_abi_export("pcc_re_engine_run")
def pcc_re_engine_run(
    pattern,
    text,
    text_length: i64,
    search: i64,
    caps,
    caps_length: i64,
    groups_out,
) -> i64:
    return _run_flags(
        pattern,
        0,
        text,
        text_length,
        0,
        search,
        caps,
        caps_length,
        groups_out,
    )


@c_abi_export("pcc_re_engine_group_names_flags")
def pcc_re_engine_group_names_flags(pattern, flags: i64, out, out_length: i64) -> i64:
    if ptr_is_null(pattern) != 0 or ptr_is_null(out) != 0:
        return -3
    if (flags & ~26) != 0:
        return -1
    scratch = stack_alloc(263192)
    program_out = stack_alloc(8)
    if _compiled_program(pattern, flags, scratch, program_out) < 0:
        return -1
    program = load_ptr(program_out, 0)
    group_count: i64 = load_i32(program, 4)
    offset: i64 = 0
    group: i64 = 1
    while group <= group_count:
        name = ptr_add(program, 24 + group * 32)
        length: i64 = _cstrlen(name)
        if offset + length + 1 > out_length:
            return -3
        _copy_bytes(out, offset, name, 0, length + 1)
        offset = offset + length + 1
        group = group + 1
    return group_count


@c_abi_export("pcc_re_engine_group_names")
def pcc_re_engine_group_names(pattern, out, out_length: i64) -> i64:
    return pcc_re_engine_group_names_flags(pattern, 0, out, out_length)
