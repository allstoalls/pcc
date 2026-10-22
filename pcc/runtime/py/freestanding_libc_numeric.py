"""Freestanding pcc-Python definitions for the residual numeric libc ABI.

These symbols are called by existing pcc-Python runtime modules and the
transitional C-API shim.  Keeping their definitions here removes the vendored
musl object closure from the production archive.  The implementation uses no
managed Python objects and is safe during runtime bootstrap.
"""

from pcc import i64
from pcc.extern import (
    c_abi_export,
    c_abi_typed_export,
    c_int32,
    c_void,
    extern,
)
from pcc.unsafe import (
    cstr,
    f64_div,
    f64_bits,
    f64_signbit,
    float_to_i64,
    i64_to_float,
    load_f64,
    load_i64,
    load_i8,
    logical_shift_right_i64,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_f64,
    store_i64,
    store_ptr,
    unsigned_div_i64,
    unsigned_rem_i64,
)

__pcc_freestanding__ = True


pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)


@c_abi_typed_export("toupper", "i32", ("i32",))
def pcc_toupper(value: i64) -> i64:
    # The owned runtime currently uses the fixed C locale. Valid inputs are
    # unsigned-char values or EOF; bytes outside ASCII lowercase stay intact.
    if value >= 97 and value <= 122:
        return value - 32
    return value


@c_abi_export("pcc_numeric_ascii_lower")
def _ascii_lower(value: i64) -> i64:
    if value >= 65 and value <= 90:
        return value + 32
    return value


@c_abi_export("pcc_numeric_is_space")
def _is_space(value: i64) -> bool:
    return (
        value == 32
        or value == 9
        or value == 10
        or value == 13
        or value == 11
        or value == 12
    )


@c_abi_typed_export("atoi", "i32", ("ptr",))
def atoi(text) -> i64:
    # The runtime's fixed C locale. ISO C leaves out-of-int-range inputs
    # undefined; accumulate negatively so INT_MIN itself never overflows.
    # This function deliberately does not promise strtol's errno behavior.
    offset: i64 = 0
    byte: i64 = load_i8(text, offset) & 255
    while _is_space(byte):
        offset = offset + 1
        byte = load_i8(text, offset) & 255
    negative: i64 = 0
    if byte == 45 or byte == 43:
        negative = 1 if byte == 45 else 0
        offset = offset + 1
        byte = load_i8(text, offset) & 255
    value: i64 = 0
    while byte >= 48 and byte <= 57:
        value = value * 10 - (byte - 48)
        offset = offset + 1
        byte = load_i8(text, offset) & 255
    if negative != 0:
        return value
    return 0 - value


@c_abi_export("pcc_numeric_match_word")
def _match_word(text, word, length: i64) -> bool:
    index: i64 = 0
    while index < length:
        if _ascii_lower(load_i8(text, index) & 255) != load_i8(word, index):
            return False
        index = index + 1
    return True


@c_abi_export("pcc_numeric_float_from_bits")
def _float_from_bits(bits: i64) -> float:
    slot = stack_alloc(8)
    store_i64(slot, 0, bits)
    return load_f64(slot, 0)


@c_abi_export("pcc_numeric_positive_infinity")
def _positive_infinity() -> float:
    return _float_from_bits(9218868437227405312)


@c_abi_export("pcc_numeric_not_a_number")
def _not_a_number() -> float:
    return _float_from_bits(9221120237041090560)


@c_abi_export("pcc_numeric_nan_result")
def _nan_result(value: float) -> float:
    # Arithmetic propagation preserves a quiet payload and makes a signaling
    # NaN quiet while raising FE_INVALID, as the libm entrypoints require.
    return value + value


@c_abi_export("pcc_numeric_math_invalid")
def _math_invalid(value: float) -> float:
    pcc_errno_set(33)  # EDOM
    zero: float = value - value
    return f64_div(zero, zero)


@c_abi_export("pcc_numeric_math_divzero")
def _math_divzero(negative: i64) -> float:
    pcc_errno_set(34)  # ERANGE
    numerator: float = 1.0
    if negative != 0:
        numerator = -1.0
    return f64_div(numerator, 0.0)


@c_abi_export("pcc_numeric_math_overflow")
def _math_overflow(negative: i64) -> float:
    pcc_errno_set(34)  # ERANGE
    factor: float = _float_from_bits((1023 + 769) * 4503599627370496)
    if negative != 0:
        factor = 0.0 - factor
    return factor * _float_from_bits((1023 + 769) * 4503599627370496)


@c_abi_export("pcc_numeric_math_underflow")
def _math_underflow(negative: i64) -> float:
    pcc_errno_set(34)  # ERANGE
    factor: float = _float_from_bits((1023 - 767) * 4503599627370496)
    if negative != 0:
        factor = 0.0 - factor
    return factor * _float_from_bits((1023 - 767) * 4503599627370496)


@c_abi_export("pcc_numeric_is_nan")
def _is_nan(value: float) -> bool:
    bits: i64 = f64_bits(value)
    exponent: i64 = logical_shift_right_i64(bits, 52) & 2047
    fraction: i64 = bits & 4503599627370495
    return exponent == 2047 and fraction != 0


@c_abi_export("pcc_numeric_is_signaling_nan")
def _is_signaling_nan(value: float) -> bool:
    bits: i64 = f64_bits(value)
    exponent: i64 = logical_shift_right_i64(bits, 52) & 2047
    fraction: i64 = bits & 4503599627370495
    return (
        exponent == 2047
        and fraction != 0
        and (fraction & 2251799813685248) == 0
    )


@c_abi_export("pcc_numeric_is_infinite")
def _is_infinite(value: float) -> bool:
    bits: i64 = f64_bits(value)
    exponent: i64 = logical_shift_right_i64(bits, 52) & 2047
    fraction: i64 = bits & 4503599627370495
    return exponent == 2047 and fraction == 0


@c_abi_export("pcc_numeric_absolute")
def _absolute(value: float) -> float:
    bits: i64 = f64_bits(value) & 9223372036854775807
    slot = stack_alloc(8)
    store_i64(slot, 0, bits)
    return load_f64(slot, 0)


@c_abi_export("pcc_numeric_digit_value")
def _digit_value(value: i64) -> i64:
    value = value & 255
    if value >= 48 and value <= 57:
        return value - 48
    if value >= 65 and value <= 70:
        return value - 55
    if value >= 97 and value <= 102:
        return value - 87
    return -1


@c_abi_export("pcc_numeric_small_power10")
def _small_power10(exponent: i64) -> i64:
    value: i64 = 1
    while exponent > 0:
        value = value * 10
        exponent = exponent - 1
    return value


@c_abi_export("pcc_numeric_small_power2")
def _small_power2(exponent: i64) -> i64:
    value: i64 = 1
    while exponent > 0:
        value = value * 2
        exponent = exponent - 1
    return value


@c_abi_export("pcc_numeric_floor_div_positive")
def _floor_div_positive(value: i64, divisor: i64) -> i64:
    if value >= 0:
        return unsigned_div_i64(value, divisor)
    absolute: i64 = 0 - value
    quotient: i64 = unsigned_div_i64(absolute, divisor)
    if unsigned_rem_i64(absolute, divisor) != 0:
        quotient = quotient + 1
    return 0 - quotient


@c_abi_export("pcc_numeric_decimal")
def _decimal(text, original, end_pointer, negative: i64) -> float:
    # Double-precision specialization of musl's decfloat scanner.  Base-1e9
    # limbs retain the complete significant input (with sticky truncation only
    # after 1116 decimal digits), then binary scaling and an explicit tail
    # choose the correctly rounded binary64 result.
    limbs = stack_alloc(1024)
    cursor = text
    digit_groups: i64 = 0
    group_digits: i64 = 0
    radix_position: i64 = 0
    decimal_digits: i64 = 0
    got_digit: i64 = 0
    got_radix: i64 = 0

    character: i64 = load_i8(cursor, 0) & 255
    while character == 48:
        got_digit: i64 = 1
        cursor = ptr_add(cursor, 1)
        character = load_i8(cursor, 0) & 255
    if character == 46:
        got_radix: i64 = 1
        cursor = ptr_add(cursor, 1)
        character = load_i8(cursor, 0) & 255
        while character == 48:
            got_digit: i64 = 1
            radix_position = radix_position - 1
            cursor = ptr_add(cursor, 1)
            character = load_i8(cursor, 0) & 255

    store_i64(limbs, 0, 0)
    while (character >= 48 and character <= 57) or character == 46:
        if character == 46:
            if got_radix != 0:
                break
            got_radix: i64 = 1
            radix_position = decimal_digits
        elif digit_groups < 125:
            decimal_digits = decimal_digits + 1
            digit: i64 = character - 48
            if group_digits != 0:
                store_i64(
                    limbs,
                    digit_groups * 8,
                    load_i64(limbs, digit_groups * 8) * 10 + digit,
                )
            else:
                store_i64(limbs, digit_groups * 8, digit)
            group_digits = group_digits + 1
            if group_digits == 9:
                digit_groups = digit_groups + 1
                group_digits: i64 = 0
            got_digit: i64 = 1
        else:
            decimal_digits = decimal_digits + 1
            if character != 48:
                store_i64(limbs, 124 * 8, load_i64(limbs, 124 * 8) | 1)
        cursor = ptr_add(cursor, 1)
        character = load_i8(cursor, 0) & 255
    if got_radix == 0:
        radix_position = decimal_digits

    if got_digit != 0 and (character == 101 or character == 69):
        exponent_mark = cursor
        exponent_cursor = ptr_add(cursor, 1)
        exponent_negative: i64 = 0
        character = load_i8(exponent_cursor, 0) & 255
        if character == 45:
            exponent_negative: i64 = 1
            exponent_cursor = ptr_add(exponent_cursor, 1)
            character = load_i8(exponent_cursor, 0) & 255
        elif character == 43:
            exponent_cursor = ptr_add(exponent_cursor, 1)
            character = load_i8(exponent_cursor, 0) & 255
        exponent_value: i64 = 0
        exponent_digits: i64 = 0
        while character >= 48 and character <= 57:
            exponent_digits = exponent_digits + 1
            if exponent_value < 100000:
                exponent_value = exponent_value * 10 + character - 48
            exponent_cursor = ptr_add(exponent_cursor, 1)
            character = load_i8(exponent_cursor, 0) & 255
        if exponent_digits != 0:
            if exponent_negative != 0:
                exponent_value = 0 - exponent_value
            radix_position = radix_position + exponent_value
            cursor = exponent_cursor
        else:
            cursor = exponent_mark

    if got_digit == 0:
        pcc_errno_set(22)  # EINVAL, matching the retained musl oracle.
        if ptr_is_null(end_pointer) == 0:
            store_ptr(end_pointer, 0, original)
        return 0.0
    if ptr_is_null(end_pointer) == 0:
        store_ptr(end_pointer, 0, cursor)
    if load_i64(limbs, 0) == 0:
        if negative != 0:
            return -0.0
        return 0.0
    if radix_position > 537:
        return _math_overflow(negative)
    if radix_position < -1180:
        return _math_underflow(negative)

    if group_digits != 0:
        while group_digits < 9:
            store_i64(
                limbs,
                digit_groups * 8,
                load_i64(limbs, digit_groups * 8) * 10,
            )
            group_digits = group_digits + 1
        digit_groups = digit_groups + 1

    first: i64 = 0
    after_last: i64 = digit_groups
    binary_exponent: i64 = 0
    radix: i64 = radix_position
    while load_i64(limbs, (after_last - 1) * 8) == 0:
        after_last = after_last - 1

    remainder9: i64 = 0
    if radix >= 0:
        remainder9 = unsigned_rem_i64(radix, 9)
    else:
        remainder9 = unsigned_rem_i64(0 - radix, 9)
        if remainder9 != 0:
            remainder9 = 9 - remainder9
    if remainder9 != 0:
        divisor: i64 = _small_power10(9 - remainder9)
        carry: i64 = 0
        index: i64 = first
        while index != after_last:
            limb: i64 = load_i64(limbs, index * 8)
            tail: i64 = unsigned_rem_i64(limb, divisor)
            store_i64(
                limbs,
                index * 8,
                unsigned_div_i64(limb, divisor) + carry,
            )
            carry = unsigned_div_i64(1000000000, divisor) * tail
            if index == first and load_i64(limbs, index * 8) == 0:
                first = (first + 1) & 127
                radix = radix - 9
            index = (index + 1) & 127
        if carry != 0:
            store_i64(limbs, after_last * 8, carry)
            after_last = (after_last + 1) & 127
        radix = radix + 9 - remainder9

    while True:
        leading: i64 = load_i64(limbs, first * 8)
        second: i64 = 0
        if ((first + 1) & 127) != after_last:
            second = load_i64(limbs, ((first + 1) & 127) * 8)
        enough: i64 = 0
        if radix > 18:
            enough: i64 = 1
        elif radix == 18:
            if leading > 9007199 or (
                leading == 9007199 and second >= 254740991
            ):
                enough: i64 = 1
        if enough != 0:
            break
        carry: i64 = 0
        index = (after_last - 1) & 127
        while True:
            limb = load_i64(limbs, index * 8)
            combined: i64 = limb * 536870912 + carry
            if combined > 1000000000:
                carry = unsigned_div_i64(combined, 1000000000)
                store_i64(
                    limbs,
                    index * 8,
                    unsigned_rem_i64(combined, 1000000000),
                )
            else:
                carry: i64 = 0
                store_i64(limbs, index * 8, combined)
            if (
                index == ((after_last - 1) & 127)
                and index != first
                and load_i64(limbs, index * 8) == 0
            ):
                after_last = index
            if index == first:
                break
            index = (index - 1) & 127
        binary_exponent = binary_exponent - 29
        if carry != 0:
            radix = radix + 9
            first = (first - 1) & 127
            if first == after_last:
                after_last = (after_last - 1) & 127
                store_i64(
                    limbs,
                    ((after_last - 1) & 127) * 8,
                    load_i64(limbs, ((after_last - 1) & 127) * 8)
                    | load_i64(limbs, after_last * 8),
                )
            store_i64(limbs, first * 8, carry)

    while True:
        threshold_index: i64 = 0
        while threshold_index < 2:
            index = (first + threshold_index) & 127
            threshold: i64 = 9007199
            if threshold_index == 1:
                threshold: i64 = 254740991
            if index == after_last or load_i64(limbs, index * 8) < threshold:
                threshold_index: i64 = 2
                break
            if load_i64(limbs, index * 8) > threshold:
                break
            threshold_index = threshold_index + 1
        if threshold_index == 2 and radix == 18:
            break
        shift: i64 = 1
        if radix > 27:
            shift: i64 = 9
        binary_exponent = binary_exponent + shift
        carry: i64 = 0
        index = first
        while index != after_last:
            limb = load_i64(limbs, index * 8)
            discarded: i64 = limb & (_small_power2(shift) - 1)
            store_i64(
                limbs,
                index * 8,
                logical_shift_right_i64(limb, shift) + carry,
            )
            carry = logical_shift_right_i64(1000000000, shift) * discarded
            if index == first and load_i64(limbs, index * 8) == 0:
                first = (first + 1) & 127
                radix = radix - 9
            index = (index + 1) & 127
        if carry != 0:
            if ((after_last + 1) & 127) != first:
                store_i64(limbs, after_last * 8, carry)
                after_last = (after_last + 1) & 127
            else:
                store_i64(
                    limbs,
                    ((after_last - 1) & 127) * 8,
                    load_i64(limbs, ((after_last - 1) & 127) * 8) | 1,
                )

    assembled: float = 0.0
    assembled_count: i64 = 0
    while assembled_count < 2:
        index = (first + assembled_count) & 127
        if index == after_last:
            store_i64(limbs, after_last * 8, 0)
            after_last = (after_last + 1) & 127
        assembled = 1000000000.0 * assembled + i64_to_float(
            load_i64(limbs, index * 8)
        )
        assembled_count = assembled_count + 1
    if negative != 0:
        assembled = 0.0 - assembled

    precision_bits: i64 = 53
    denormal: i64 = 0
    available_bits: i64 = 1127 + binary_exponent
    if precision_bits > available_bits:
        precision_bits = available_bits
        if precision_bits < 0:
            precision_bits: i64 = 0
        denormal: i64 = 1

    fraction: float = 0.0
    bias: float = 0.0
    if precision_bits < 53:
        bias = pcc_scalbn(1.0, 105 - precision_bits)
        if negative != 0:
            bias = 0.0 - bias
        fraction = pcc_fmod(assembled, pcc_scalbn(1.0, 53 - precision_bits))
        assembled = assembled - fraction
        assembled = assembled + bias

    tail_index: i64 = (first + 2) & 127
    if tail_index != after_last:
        tail_limb: i64 = load_i64(limbs, tail_index * 8)
        more_tail: i64 = 0
        if ((tail_index + 1) & 127) != after_last:
            more_tail: i64 = 1
        sign_quarter: float = 0.25
        if negative != 0:
            sign_quarter = -0.25
        if tail_limb < 500000000 and (tail_limb != 0 or more_tail != 0):
            fraction = fraction + sign_quarter
        elif tail_limb > 500000000:
            fraction = fraction + 3.0 * sign_quarter
        elif tail_limb == 500000000:
            if more_tail == 0:
                fraction = fraction + 2.0 * sign_quarter
            else:
                fraction = fraction + 3.0 * sign_quarter
        if 53 - precision_bits >= 2 and pcc_fmod(fraction, 1.0) == 0.0:
            fraction = fraction + 1.0

    assembled = assembled + fraction
    assembled = assembled - bias
    if binary_exponent + 53 > 1019:
        if _absolute(assembled) >= 9007199254740992.0:
            if denormal != 0 and precision_bits == 1127 + binary_exponent:
                denormal: i64 = 0
            assembled = assembled * 0.5
            binary_exponent = binary_exponent + 1
    if binary_exponent + 53 > 1024 or (denormal != 0 and fraction != 0.0):
        pcc_errno_set(34)  # ERANGE
    return pcc_scalbn(assembled, binary_exponent)


@c_abi_export("pcc_numeric_hexadecimal")
def _hexadecimal(text, end_pointer, negative: i64) -> float:
    cursor = text
    significand: i64 = 0
    fraction: float = 0.0
    scale: float = 1.0
    got_tail: i64 = 0
    got_radix: i64 = 0
    got_digit: i64 = 0
    radix_position: i64 = 0
    digit_count: i64 = 0
    character: i64 = load_i8(cursor, 0) & 255
    while character == 48:
        got_digit: i64 = 1
        cursor = ptr_add(cursor, 1)
        character = load_i8(cursor, 0) & 255
    if character == 46:
        got_radix: i64 = 1
        cursor = ptr_add(cursor, 1)
        character = load_i8(cursor, 0) & 255
        while character == 48:
            got_digit: i64 = 1
            radix_position = radix_position - 1
            cursor = ptr_add(cursor, 1)
            character = load_i8(cursor, 0) & 255

    digit: i64 = _digit_value(character)
    while digit >= 0 or character == 46:
        if character == 46:
            if got_radix != 0:
                break
            radix_position = digit_count
            got_radix: i64 = 1
        else:
            got_digit: i64 = 1
            if digit_count < 8:
                significand = significand * 16 + digit
            elif digit_count < 14:
                scale = f64_div(scale, 16.0)
                fraction = fraction + i64_to_float(digit) * scale
            elif digit != 0 and got_tail == 0:
                fraction = fraction + 0.5 * scale
                got_tail: i64 = 1
            digit_count = digit_count + 1
        cursor = ptr_add(cursor, 1)
        character = load_i8(cursor, 0) & 255
        digit = _digit_value(character)
    if got_radix == 0:
        radix_position = digit_count
    while digit_count < 8:
        significand = significand * 16
        digit_count = digit_count + 1

    binary_exponent: i64 = 4 * radix_position - 32
    if character == 112 or character == 80:
        exponent_mark = cursor
        exponent_cursor = ptr_add(cursor, 1)
        exponent_negative: i64 = 0
        character = load_i8(exponent_cursor, 0) & 255
        if character == 45:
            exponent_negative: i64 = 1
            exponent_cursor = ptr_add(exponent_cursor, 1)
            character = load_i8(exponent_cursor, 0) & 255
        elif character == 43:
            exponent_cursor = ptr_add(exponent_cursor, 1)
            character = load_i8(exponent_cursor, 0) & 255
        exponent_value: i64 = 0
        exponent_digits: i64 = 0
        while character >= 48 and character <= 57:
            exponent_digits = exponent_digits + 1
            if exponent_value < 100000:
                exponent_value = exponent_value * 10 + character - 48
            exponent_cursor = ptr_add(exponent_cursor, 1)
            character = load_i8(exponent_cursor, 0) & 255
        if exponent_digits != 0:
            if exponent_negative != 0:
                exponent_value = 0 - exponent_value
            binary_exponent = binary_exponent + exponent_value
            cursor = exponent_cursor
        else:
            cursor = exponent_mark
    if ptr_is_null(end_pointer) == 0:
        store_ptr(end_pointer, 0, cursor)
    if significand == 0:
        if negative != 0:
            return -0.0
        return 0.0
    if binary_exponent > 1074:
        return _math_overflow(negative)
    if binary_exponent < -1180:
        return _math_underflow(negative)

    while significand < 2147483648:
        if fraction >= 0.5:
            significand = significand + significand + 1
            fraction = fraction + fraction - 1.0
        else:
            significand = significand + significand
            fraction = fraction + fraction
        binary_exponent = binary_exponent - 1

    precision_bits: i64 = 53
    available_bits: i64 = 32 + binary_exponent + 1074
    if precision_bits > available_bits:
        precision_bits = available_bits
        if precision_bits < 0:
            precision_bits: i64 = 0
    bias: float = 0.0
    if precision_bits < 53:
        bias = pcc_scalbn(1.0, 84 - precision_bits)
        if negative != 0:
            bias = 0.0 - bias
    if precision_bits < 32 and fraction != 0.0 and (significand & 1) == 0:
        significand = significand + 1
        fraction = 0.0
    sign_value: float = 1.0
    if negative != 0:
        sign_value = -1.0
    result: float = bias + sign_value * i64_to_float(significand)
    result = result + sign_value * fraction
    result = result - bias
    if result == 0.0:
        pcc_errno_set(34)  # ERANGE
    return pcc_scalbn(result, binary_exponent)


@c_abi_export("strtod")
def pcc_strtod(text, end_pointer) -> float:
    original = text
    if ptr_is_null(text) != 0:
        if ptr_is_null(end_pointer) == 0:
            store_ptr(end_pointer, 0, null())
        return 0.0
    while _is_space(load_i8(text, 0) & 255):
        text = ptr_add(text, 1)
    negative: i64 = 0
    if load_i8(text, 0) == 45:
        negative: i64 = 1
        text = ptr_add(text, 1)
    elif load_i8(text, 0) == 43:
        text = ptr_add(text, 1)

    number_start = text
    if _match_word(text, cstr("inf"), 3):
        end = ptr_add(text, 3)
        if _match_word(text, cstr("infinity"), 8):
            end = ptr_add(text, 8)
        if ptr_is_null(end_pointer) == 0:
            store_ptr(end_pointer, 0, end)
        infinity: float = _positive_infinity()
        if negative != 0:
            return 0.0 - infinity
        return infinity
    if _match_word(text, cstr("nan"), 3):
        end = ptr_add(text, 3)
        if load_i8(end, 0) == 40:
            payload = ptr_add(end, 1)
            payload_cursor = payload
            while True:
                character: i64 = load_i8(payload_cursor, 0) & 255
                if (
                    (character >= 48 and character <= 57)
                    or (character >= 65 and character <= 90)
                    or (character >= 97 and character <= 122)
                    or character == 95
                ):
                    payload_cursor = ptr_add(payload_cursor, 1)
                    continue
                if character == 41:
                    end = ptr_add(payload_cursor, 1)
                break
        if ptr_is_null(end_pointer) == 0:
            store_ptr(end_pointer, 0, end)
        return _not_a_number()

    # A hexadecimal prefix is committed only when at least one hexadecimal
    # digit exists on either side of the radix point.  Otherwise decimal
    # scanning consumes the leading zero and leaves the end pointer at 'x'.
    if load_i8(text, 0) == 48 and (
        load_i8(text, 1) == 120 or load_i8(text, 1) == 88
    ):
        hexadecimal_start = ptr_add(text, 2)
        first_hex: i64 = _digit_value(load_i8(hexadecimal_start, 0))
        if first_hex >= 0 or (
            load_i8(hexadecimal_start, 0) == 46
            and _digit_value(load_i8(hexadecimal_start, 1)) >= 0
        ):
            return _hexadecimal(hexadecimal_start, end_pointer, negative)
    return _decimal(number_start, original, end_pointer, negative)


@c_abi_export("scalbn")
def pcc_scalbn(value: float, exponent: i64) -> float:
    # Native C callers pass the second parameter as i32. On the supported ABIs
    # the argument register is zero-extended; recover negative i32 values when
    # this pcc-Python definition is lowered with its internal i64 integer type.
    if exponent >= 2147483648 and exponent <= 4294967295:
        exponent = exponent - 4294967296
    if _is_nan(value):
        return _nan_result(value)
    if _is_infinite(value) or value == 0.0:
        return value

    absolute_bits: i64 = f64_bits(_absolute(value))
    biased_exponent: i64 = logical_shift_right_i64(absolute_bits, 52) & 2047
    input_exponent: i64 = biased_exponent - 1023
    if biased_exponent == 0:
        fraction_bits: i64 = absolute_bits & 4503599627370495
        highest_bit: i64 = -1
        while fraction_bits != 0:
            fraction_bits = logical_shift_right_i64(fraction_bits, 1)
            highest_bit = highest_bit + 1
        input_exponent = highest_bit - 1074
    target_exponent: i64 = input_exponent + exponent
    if target_exponent > 1023:
        return _math_overflow(f64_signbit(value))
    if target_exponent < -1075:
        return _math_underflow(f64_signbit(value))

    result: float = value
    # Port of musl scalbn: the 2**-969 pre-scaling keeps the final multiply
    # below 2**-53 in the subnormal range and therefore avoids double rounding.
    if exponent > 1023:
        result = result * 8.98846567431158e307
        exponent = exponent - 1023
        if exponent > 1023:
            result = result * 8.98846567431158e307
            exponent = exponent - 1023
            if exponent > 1023:
                exponent: i64 = 1023
    elif exponent < -1022:
        result = result * 2.004168360008973e-292
        exponent = exponent + 969
        if exponent < -1022:
            result = result * 2.004168360008973e-292
            exponent = exponent + 969
            if exponent < -1022:
                exponent: i64 = -1022
    factor_bits: i64 = (1023 + exponent) * 4503599627370496
    factor_slot = stack_alloc(8)
    store_i64(factor_slot, 0, factor_bits)
    scaled: float = result * load_f64(factor_slot, 0)
    if (
        _is_infinite(scaled)
        or scaled == 0.0
        or _absolute(scaled) < 2.2250738585072014e-308
    ):
        pcc_errno_set(34)  # ERANGE; the multiply owns the FP flag.
    return scaled


@c_abi_export("fabs")
def pcc_fabs(value: float) -> float:
    return _absolute(value)


@c_abi_export("floor")
def pcc_floor(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if value == 0.0 or _is_infinite(value):
        return value
    absolute: float = _absolute(value)
    # Every binary64 value at or above 2**52 is already integral.
    if absolute >= 4503599627370496.0:
        return value
    truncated: i64 = float_to_i64(value)
    result: float = i64_to_float(truncated)
    if value < result:
        result = result - 1.0
    return result


@c_abi_export("ceil")
def pcc_ceil(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if value == 0.0 or _is_infinite(value):
        return value
    if _absolute(value) >= 4503599627370496.0:
        return value
    truncated: i64 = float_to_i64(value)
    result: float = i64_to_float(truncated)
    if result < value:
        result = result + 1.0
    if result == 0.0 and f64_signbit(value) != 0:
        return -0.0
    return result


@c_abi_export("pcc_numeric_round_ties_even")
def _round_ties_even(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if value == 0.0 or _is_infinite(value):
        return value
    absolute: float = _absolute(value)
    if absolute >= 4503599627370496.0:
        return value
    lower: float = pcc_floor(value)
    fraction: float = value - lower
    rounded: float = lower
    if fraction > 0.5:
        rounded = lower + 1.0
    elif fraction == 0.5:
        lower_integer: i64 = float_to_i64(lower)
        if (lower_integer & 1) != 0:
            rounded = lower + 1.0
    if rounded == 0.0 and f64_signbit(value) != 0:
        return -0.0
    return rounded


@c_abi_export("rint")
def pcc_rint(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if value == 0.0 or _is_infinite(value):
        return value
    absolute: float = _absolute(value)
    if absolute >= 4503599627370496.0:
        return value
    # musl's add/subtract-toint construction delegates the decision to the
    # active hardware rounding mode and naturally raises FE_INEXACT for a
    # finite non-integral operand.  LLVM's strict (non-fast-math) fadd/fsub
    # preserve these operations in both native backends.
    to_integer: float = 4503599627370496.0
    rounded: float = 0.0
    if f64_signbit(value) != 0:
        rounded = value - to_integer
        rounded = rounded + to_integer
    else:
        rounded = value + to_integer
        rounded = rounded - to_integer
    if rounded == 0.0:
        if f64_signbit(value) != 0:
            return -0.0
        return 0.0
    return rounded


@c_abi_export("sqrt")
def pcc_sqrt(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if value == 0.0:
        return value
    if value < 0.0:
        return _math_invalid(value)
    if _is_infinite(value):
        return value

    # Normalize to [1, 4), solve there, then restore the even binary exponent.
    # This keeps Newton's iteration away from overflow and subnormal underflow.
    scaled: float = value
    scale: float = 1.0
    while scaled >= 4.0:
        scaled = scaled * 0.25
        scale = scale * 2.0
    while scaled < 1.0:
        scaled = scaled * 4.0
        scale = scale * 0.5
    estimate: float = 0.5 * (scaled + 1.0)
    iteration: i64 = 0
    while iteration < 8:
        estimate = 0.5 * (estimate + f64_div(scaled, estimate))
        iteration = iteration + 1

    # Newton converges to one of the two binary64 values around the exact
    # root. Select between adjacent candidates using an error-free split of
    # the lower candidate's square, rather than inheriting the iteration's
    # final rounding direction (sqrt(2) is the canonical counterexample).
    quotient: float = f64_div(scaled, estimate)
    lower: float = estimate
    if quotient < lower:
        lower = quotient
    lower_bits: i64 = f64_bits(lower)
    upper_slot = stack_alloc(8)
    store_i64(upper_slot, 0, lower_bits + 1)
    upper: float = load_f64(upper_slot, 0)
    split: float = 134217729.0 * lower
    large_part: float = split - lower
    lower_high: float = split - large_part
    lower_low: float = lower - lower_high
    product: float = lower * lower
    product_error: float = (
        (lower_high * lower_high - product)
        + 2.0 * lower_high * lower_low
        + lower_low * lower_low
    )
    residual: float = (scaled - product) - product_error
    step: float = upper - lower
    midpoint_delta: float = lower * step + 0.25 * step * step
    if residual > midpoint_delta:
        estimate = upper
    elif residual < midpoint_delta:
        estimate = lower
    elif (lower_bits & 1) != 0:
        estimate = upper
    else:
        estimate = lower
    return estimate * scale


@c_abi_export("hypot")
def pcc_hypot(left: float, right: float) -> float:
    a: float = _absolute(left)
    b: float = _absolute(right)
    if _is_infinite(a) or _is_infinite(b):
        if _is_signaling_nan(left):
            _nan_result(left)
        if _is_signaling_nan(right):
            _nan_result(right)
        return _positive_infinity()
    if _is_nan(a) or _is_nan(b):
        return a + b
    if b > a:
        temporary: float = a
        a = b
        b = temporary
    if a == 0.0:
        return 0.0
    if b == 0.0:
        return a
    ratio: float = f64_div(b, a)
    root: float = pcc_sqrt(1.0 + ratio * ratio)
    maximum: float = _float_from_bits(9218868437227405311)
    if a > f64_div(maximum, root):
        return _math_overflow(0)
    result: float = a * root
    if _is_infinite(result) or (
        result != 0.0 and result < 2.2250738585072014e-308
    ):
        pcc_errno_set(34)  # ERANGE; the arithmetic owns the matching FP flag.
    return result


@c_abi_export("exp")
def pcc_exp(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if _is_infinite(value):
        if f64_signbit(value) != 0:
            return 0.0
        return value
    if value > 709.782712893384:
        return _math_overflow(0)
    if value < -745.1332191019411:
        return _math_underflow(0)

    # Range reduction is a ties-to-even algorithmic step and must not inherit
    # the caller's directed mode as public rint does.
    nearest: float = _round_ties_even(value * 1.4426950408889634)
    exponent: i64 = float_to_i64(nearest)
    reduced: float = value - nearest * 0.6931471803691238
    reduced = reduced - nearest * 1.9082149292705877e-10

    # exp(reduced), |reduced| <= log(2)/2. Eighteen Taylor terms put the
    # truncation error below binary64 rounding noise on this compact interval.
    term: float = 1.0
    total: float = 1.0
    index: i64 = 1
    while index <= 18:
        term = f64_div(term * reduced, i64_to_float(index))
        total = total + term
        index = index + 1
    result: float = pcc_scalbn(total, exponent)
    if result != 0.0 and _absolute(result) < 2.2250738585072014e-308:
        pcc_errno_set(34)  # ERANGE for a finite subnormal result.
    return result


@c_abi_export("log")
def pcc_log(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if value == 0.0:
        return _math_divzero(1)
    if value < 0.0:
        return _math_invalid(value)
    if _is_infinite(value):
        return value

    # Bring the mantissa close to one. The atanh identity below then has
    # |z| <= 0.172, so a short odd series is accurate over all exponents.
    mantissa: float = value
    exponent: i64 = 0
    while mantissa >= 1.4142135623730951:
        mantissa = mantissa * 0.5
        exponent = exponent + 1
    while mantissa < 0.7071067811865476:
        mantissa = mantissa * 2.0
        exponent = exponent - 1
    z: float = f64_div(mantissa - 1.0, mantissa + 1.0)
    z_squared: float = z * z
    term = z
    series: float = z
    divisor: i64 = 3
    while divisor <= 31:
        term = term * z_squared
        series = series + f64_div(term, i64_to_float(divisor))
        divisor = divisor + 2
    result: float = 2.0 * series
    exponent_float: float = i64_to_float(exponent)
    result = result + exponent_float * 0.6931471803691238
    return result + exponent_float * 1.9082149292705877e-10


@c_abi_export("log2")
def pcc_log2(value: float) -> float:
    # Reuse log's domain/NaN/errno behavior before normalization. Reducing
    # first avoids multiplying a rounded large log and makes powers of two
    # exact, including every subnormal power down to 2**-1074.
    if _is_nan(value) or value <= 0.0 or _is_infinite(value):
        return pcc_log(value)
    mantissa: float = value
    exponent: i64 = 0
    while mantissa >= 1.4142135623730951:
        mantissa = mantissa * 0.5
        exponent = exponent + 1
    while mantissa < 0.7071067811865476:
        mantissa = mantissa * 2.0
        exponent = exponent - 1
    return i64_to_float(exponent) + pcc_log(mantissa) * 1.4426950408889634


@c_abi_export("log10")
def pcc_log10(value: float) -> float:
    if _is_nan(value) or value <= 0.0 or _is_infinite(value):
        return pcc_log(value)
    mantissa: float = value
    exponent: i64 = 0
    while mantissa >= 1.4142135623730951:
        mantissa = mantissa * 0.5
        exponent = exponent + 1
    while mantissa < 0.7071067811865476:
        mantissa = mantissa * 2.0
        exponent = exponent - 1
    # Split log10(2) keeps the exponent contribution's low part through the
    # addition to the reduced logarithm (the fdlibm split constants).
    exponent_float: float = i64_to_float(exponent)
    reduced: float = pcc_log(mantissa) * 0.4342944819032518
    return exponent_float * 0.30102999566361177 + (
        reduced + exponent_float * 3.694239077158931e-13
    )


# The Payne-Hanek reducer below is a freestanding pcc-Python port of fdlibm's
# e_rem_pio2.c and k_rem_pio2.c (also carried by musl/msun).
#
# Copyright (C) 1993 by Sun Microsystems, Inc. All rights reserved.
# Developed at SunSoft, a Sun Microsystems, Inc. business.
# Permission to use, copy, modify, and distribute this software is freely
# granted, provided that this notice is preserved.


@c_abi_export("pcc_numeric_two_over_pi_digit")
def _two_over_pi_digit(index: i64) -> i64:
    # 396 hexadecimal digits (1584 bits) of 2/pi, in 24-bit chunks. Keeping
    # the table as an ASCII C literal avoids managed global list ownership.
    table = cstr("A2F9836E4E441529FC2757D1F534DDC0DB6295993C439041FE5163ABDEBBC561B7246E3A424DD2E006492EEA09D1921CFE1DEB1CB129A73EE88235F52EBB4484E99C7026B45F7E413991D639835339F49C845F8BBDF9283B1FF897FFDE05980FEF2F118B5A0A6D1F6D367ECF27CB09B74F463F669E5FEA2D7527BAC7EBE5F17B3D0739F78A5292EA6BFB5FB11F8D5D0856033046FC7B6BABF0CFBC209AF4361DA9E391615EE61B086599855F14A068408DFFD8804D73273106061556CA73A8C960E27BC08C6B")
    offset: i64 = index * 6
    value: i64 = 0
    cursor: i64 = 0
    while cursor < 6:
        character: i64 = load_i8(table, offset + cursor)
        digit: i64 = character - 48
        if character >= 65:
            digit = character - 55
        value = value * 16 | digit
        cursor = cursor + 1
    return value


@c_abi_export("pcc_numeric_pio2_chunk")
def _pio2_chunk(index: i64) -> float:
    if index == 0:
        return 1.57079625129699707031
    if index == 1:
        return 7.54978941586159635335e-08
    if index == 2:
        return 5.39030252995776476554e-15
    if index == 3:
        return 3.28200341580791294123e-22
    return 1.27065575308067607349e-29


@c_abi_export("pcc_numeric_kernel_rem_pio2")
def _kernel_rem_pio2(x_values, count: i64, exponent: i64, output) -> i64:
    # This is fdlibm's double-precision (prec=2, jk=4) kernel. All scratch
    # arrays are raw stack storage, so argument reduction has no runtime/GC
    # dependency even when called during early bootstrap.
    q_values = stack_alloc(160)
    f_values = stack_alloc(160)
    iq_values = stack_alloc(160)
    fq_values = stack_alloc(160)
    jx: i64 = count - 1
    jk: i64 = 4
    jp: i64 = 4
    jv: i64 = _floor_div_positive(exponent - 3, 24)
    if jv < 0:
        jv: i64 = 0
    q0: i64 = exponent - 24 * (jv + 1)

    table_index: i64 = jv - jx
    maximum: i64 = jx + jk
    index: i64 = 0
    while index <= maximum:
        table_value: float = 0.0
        if table_index >= 0:
            table_value = i64_to_float(_two_over_pi_digit(table_index))
        store_f64(f_values, index * 8, table_value)
        index = index + 1
        table_index = table_index + 1

    index: i64 = 0
    while index <= jk:
        product: float = 0.0
        component: i64 = 0
        while component <= jx:
            product = product + load_f64(x_values, component * 8) * load_f64(
                f_values, (jx + index - component) * 8
            )
            component = component + 1
        store_f64(q_values, index * 8, product)
        index = index + 1

    jz: i64 = jk
    quotient: float = 0.0
    integer_part: i64 = 0
    high_half: i64 = 0
    while True:
        index: i64 = 0
        source_index: i64 = jz
        quotient = load_f64(q_values, jz * 8)
        while source_index > 0:
            carry_float: float = i64_to_float(
                float_to_i64(5.9604644775390625e-08 * quotient)
            )
            chunk: i64 = float_to_i64(
                quotient - 16777216.0 * carry_float
            )
            store_i64(iq_values, index * 8, chunk)
            quotient = load_f64(q_values, (source_index - 1) * 8) + carry_float
            index = index + 1
            source_index = source_index - 1

        quotient = pcc_scalbn(quotient, q0)
        quotient = quotient - 8.0 * pcc_floor(quotient * 0.125)
        integer_part = float_to_i64(quotient)
        quotient = quotient - i64_to_float(integer_part)
        high_half: i64 = 0
        if q0 > 0:
            last_chunk: i64 = load_i64(iq_values, (jz - 1) * 8)
            upper: i64 = logical_shift_right_i64(last_chunk, 24 - q0)
            integer_part = integer_part + upper
            last_chunk = last_chunk - upper * _small_power2(24 - q0)
            store_i64(iq_values, (jz - 1) * 8, last_chunk)
            high_half = logical_shift_right_i64(last_chunk, 23 - q0)
        elif q0 == 0:
            high_half = logical_shift_right_i64(
                load_i64(iq_values, (jz - 1) * 8), 23
            )
        elif quotient >= 0.5:
            high_half: i64 = 2

        if high_half > 0:
            integer_part = integer_part + 1
            carry: i64 = 0
            index: i64 = 0
            while index < jz:
                chunk = load_i64(iq_values, index * 8)
                if carry == 0:
                    if chunk != 0:
                        carry: i64 = 1
                        chunk = 16777216 - chunk
                else:
                    chunk = 16777215 - chunk
                store_i64(iq_values, index * 8, chunk)
                index = index + 1
            if q0 == 1:
                chunk = load_i64(iq_values, (jz - 1) * 8) & 8388607
                store_i64(iq_values, (jz - 1) * 8, chunk)
            elif q0 == 2:
                chunk = load_i64(iq_values, (jz - 1) * 8) & 4194303
                store_i64(iq_values, (jz - 1) * 8, chunk)
            if high_half == 2:
                quotient = 1.0 - quotient
                if carry != 0:
                    quotient = quotient - pcc_scalbn(1.0, q0)

        recompute: i64 = 0
        if quotient == 0.0:
            combined: i64 = 0
            index = jz - 1
            while index >= jk:
                combined = combined | load_i64(iq_values, index * 8)
                index = index - 1
            if combined == 0:
                extra: i64 = 1
                while load_i64(iq_values, (jk - extra) * 8) == 0:
                    extra = extra + 1
                index = jz + 1
                while index <= jz + extra:
                    digit_index: i64 = jv + index
                    store_f64(
                        f_values,
                        (jx + index) * 8,
                        i64_to_float(_two_over_pi_digit(digit_index)),
                    )
                    product = 0.0
                    component: i64 = 0
                    while component <= jx:
                        product = product + load_f64(
                            x_values, component * 8
                        ) * load_f64(
                            f_values, (jx + index - component) * 8
                        )
                        component = component + 1
                    store_f64(q_values, index * 8, product)
                    index = index + 1
                jz = jz + extra
                recompute: i64 = 1
        if recompute != 0:
            continue
        break

    if quotient == 0.0:
        jz = jz - 1
        q0 = q0 - 24
        while load_i64(iq_values, jz * 8) == 0:
            jz = jz - 1
            q0 = q0 - 24
    else:
        quotient = pcc_scalbn(quotient, 0 - q0)
        if quotient >= 16777216.0:
            carry_float = i64_to_float(
                float_to_i64(5.9604644775390625e-08 * quotient)
            )
            store_i64(
                iq_values,
                jz * 8,
                float_to_i64(quotient - 16777216.0 * carry_float),
            )
            jz = jz + 1
            q0 = q0 + 24
            store_i64(iq_values, jz * 8, float_to_i64(carry_float))
        else:
            store_i64(iq_values, jz * 8, float_to_i64(quotient))

    weight: float = pcc_scalbn(1.0, q0)
    index = jz
    while index >= 0:
        store_f64(
            q_values,
            index * 8,
            weight * i64_to_float(load_i64(iq_values, index * 8)),
        )
        weight = weight * 5.9604644775390625e-08
        index = index - 1

    index = jz
    while index >= 0:
        product = 0.0
        component: i64 = 0
        while component <= jp and component <= jz - index:
            product = product + _pio2_chunk(component) * load_f64(
                q_values, (index + component) * 8
            )
            component = component + 1
        store_f64(fq_values, (jz - index) * 8, product)
        index = index - 1

    remainder: float = 0.0
    index = jz
    while index >= 0:
        remainder = remainder + load_f64(fq_values, index * 8)
        index = index - 1
    tail: float = load_f64(fq_values, 0) - remainder
    index: i64 = 1
    while index <= jz:
        tail = tail + load_f64(fq_values, index * 8)
        index = index + 1
    if high_half != 0:
        remainder = 0.0 - remainder
        tail = 0.0 - tail
    store_f64(output, 0, remainder)
    store_f64(output, 8, tail)
    return integer_part & 7


@c_abi_export("pcc_numeric_large_rem_pio2")
def _large_rem_pio2(value: float, output) -> i64:
    absolute: float = _absolute(value)
    bits: i64 = f64_bits(absolute)
    biased_exponent: i64 = logical_shift_right_i64(bits, 52) & 2047
    exponent: i64 = biased_exponent - 1046
    scaled: float = pcc_scalbn(absolute, 0 - exponent)
    pieces = stack_alloc(24)
    index: i64 = 0
    while index < 2:
        piece: float = pcc_floor(scaled)
        store_f64(pieces, index * 8, piece)
        scaled = (scaled - piece) * 16777216.0
        index = index + 1
    store_f64(pieces, 16, scaled)
    count: i64 = 3
    while count > 1 and load_f64(pieces, (count - 1) * 8) == 0.0:
        count = count - 1
    quadrant: i64 = _kernel_rem_pio2(pieces, count, exponent, output)
    if f64_signbit(value) != 0:
        store_f64(output, 0, 0.0 - load_f64(output, 0))
        store_f64(output, 8, 0.0 - load_f64(output, 8))
        return 0 - quadrant
    return quadrant


@c_abi_export("pcc_numeric_trig_reduce")
def _trig_reduce(value: float, output) -> i64:
    absolute: float = _absolute(value)
    if absolute <= 0.7853981633974483:
        store_f64(output, 0, value)
        store_f64(output, 8, 0.0)
        return 0
    if absolute >= 1048576.0:
        return _large_rem_pio2(value, output)

    quadrant_float: float = pcc_floor(
        absolute * 0.6366197723675814 + 0.5
    )
    quadrant: i64 = float_to_i64(quadrant_float)
    remainder: float = absolute - quadrant_float * 1.5707963267341256
    correction: float = quadrant_float * 6.077100506506192e-11
    head: float = remainder - correction
    input_exponent: i64 = (
        logical_shift_right_i64(f64_bits(absolute), 52) & 2047
    )
    head_exponent: i64 = (
        logical_shift_right_i64(f64_bits(_absolute(head)), 52) & 2047
    )
    if input_exponent - head_exponent > 16:
        temporary: float = remainder
        correction = quadrant_float * 6.077100506303966e-11
        remainder = temporary - correction
        correction = (
            quadrant_float * 2.0222662487959506e-21
            - ((temporary - remainder) - correction)
        )
        head = remainder - correction
        head_exponent = (
            logical_shift_right_i64(f64_bits(_absolute(head)), 52) & 2047
        )
        if input_exponent - head_exponent > 49:
            temporary = remainder
            correction = quadrant_float * 2.0222662487111665e-21
            remainder = temporary - correction
            correction = (
                quadrant_float * 8.4784276603689e-32
                - ((temporary - remainder) - correction)
            )
            head = remainder - correction
    tail: float = (remainder - head) - correction
    if f64_signbit(value) != 0:
        head = 0.0 - head
        tail = 0.0 - tail
        quadrant = 0 - quadrant
    store_f64(output, 0, head)
    store_f64(output, 8, tail)
    return quadrant


@c_abi_export("pcc_numeric_kernel_sin")
def _kernel_sin(value: float, tail: float) -> float:
    squared: float = value * value
    fourth: float = squared * squared
    polynomial: float = 8.333333333322489e-03
    polynomial = polynomial + squared * (
        -1.984126982985795e-04 + squared * 2.7557313707070068e-06
    )
    polynomial = polynomial + squared * fourth * (
        -2.5050760253406863e-08 + squared * 1.5896909952115501e-10
    )
    cubic: float = squared * value
    if tail == 0.0:
        return value + cubic * (-1.6666666666666632e-01 + squared * polynomial)
    return value - (
        (squared * (0.5 * tail - cubic * polynomial) - tail)
        - cubic * -1.6666666666666632e-01
    )


@c_abi_export("pcc_numeric_kernel_cos")
def _kernel_cos(value: float, tail: float) -> float:
    squared: float = value * value
    fourth: float = squared * squared
    polynomial: float = squared * (
        4.16666666666666e-02
        + squared * (-1.388888888887411e-03 + squared * 2.480158728947673e-05)
    )
    polynomial = polynomial + fourth * fourth * (
        -2.7557314351390663e-07
        + squared * (2.087572321298175e-09 + squared * -1.1359647557788195e-11)
    )
    half_squared: float = 0.5 * squared
    leading: float = 1.0 - half_squared
    return leading + (
        ((1.0 - leading) - half_squared)
        + (squared * polynomial - value * tail)
    )


@c_abi_export("sin")
def pcc_sin(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if _is_infinite(value):
        return _math_invalid(value)
    if value == 0.0:
        return value
    reduction = stack_alloc(16)
    quadrant: i64 = _trig_reduce(value, reduction)
    reduced: float = load_f64(reduction, 0)
    tail: float = load_f64(reduction, 8)
    lane: i64 = quadrant & 3
    if lane == 0:
        return _kernel_sin(reduced, tail)
    if lane == 1:
        return _kernel_cos(reduced, tail)
    if lane == 2:
        return 0.0 - _kernel_sin(reduced, tail)
    return 0.0 - _kernel_cos(reduced, tail)


@c_abi_export("cos")
def pcc_cos(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if _is_infinite(value):
        return _math_invalid(value)
    reduction = stack_alloc(16)
    quadrant: i64 = _trig_reduce(value, reduction)
    reduced: float = load_f64(reduction, 0)
    tail: float = load_f64(reduction, 8)
    lane: i64 = quadrant & 3
    if lane == 0:
        return _kernel_cos(reduced, tail)
    if lane == 1:
        return 0.0 - _kernel_sin(reduced, tail)
    if lane == 2:
        return 0.0 - _kernel_cos(reduced, tail)
    return _kernel_sin(reduced, tail)


@c_abi_export("tan")
def pcc_tan(value: float) -> float:
    if _is_nan(value):
        return _nan_result(value)
    if _is_infinite(value):
        return _math_invalid(value)
    if value == 0.0:
        return value
    reduction = stack_alloc(16)
    quadrant: i64 = _trig_reduce(value, reduction)
    reduced: float = load_f64(reduction, 0)
    tail: float = load_f64(reduction, 8)
    sine: float = _kernel_sin(reduced, tail)
    cosine: float = _kernel_cos(reduced, tail)
    # A single high/low Payne-Hanek reduction retains the tiny remainder
    # near odd multiples of pi/2, unlike reducing two independent calls.
    if (quadrant & 1) != 0:
        return f64_div(0.0 - cosine, sine)
    return f64_div(sine, cosine)


@c_abi_export("pcc_numeric_atan_positive")
def _atan_positive(value: float) -> float:
    identifier: i64 = -1
    transformed: float = value
    if value < 0.4375:
        identifier: i64 = -1
    elif value < 1.1875:
        if value < 0.6875:
            identifier: i64 = 0
            transformed = f64_div(2.0 * value - 1.0, 2.0 + value)
        else:
            identifier: i64 = 1
            transformed = f64_div(value - 1.0, value + 1.0)
    elif value < 2.4375:
        identifier: i64 = 2
        transformed = f64_div(value - 1.5, 1.0 + 1.5 * value)
    else:
        identifier: i64 = 3
        transformed = f64_div(-1.0, value)

    squared: float = transformed * transformed
    fourth: float = squared * squared
    even: float = 1.6285820115365782e-02
    even = 4.976877994615932e-02 + fourth * even
    even = 6.661073137387531e-02 + fourth * even
    even = 9.090887133436507e-02 + fourth * even
    even = 1.4285714272503466e-01 + fourth * even
    even = 3.333333333333293e-01 + fourth * even
    even = squared * even
    odd: float = -3.6531572744216916e-02
    odd = -5.8335701337905735e-02 + fourth * odd
    odd = -7.69187620504483e-02 + fourth * odd
    odd = -1.1111110405462356e-01 + fourth * odd
    odd = -1.9999999999876483e-01 + fourth * odd
    odd = fourth * odd
    correction: float = transformed * (even + odd)
    if identifier < 0:
        return transformed - correction
    high: float = 0.4636476090008061
    low: float = 2.2698777452961687e-17
    if identifier == 1:
        high = 0.7853981633974483
        low = 3.061616997868383e-17
    elif identifier == 2:
        high = 0.982793723247329
        low = 1.3903311031230998e-17
    elif identifier == 3:
        high = 1.5707963267948966
        low = 6.123233995736766e-17
    return high - ((correction - low) - transformed)


@c_abi_export("atan2")
def pcc_atan2(y_value: float, x_value: float) -> float:
    if _is_nan(x_value) or _is_nan(y_value):
        return x_value + y_value
    y_negative: i64 = f64_signbit(y_value)
    x_negative: i64 = f64_signbit(x_value)
    if y_value == 0.0:
        if x_negative == 0:
            return y_value
        if y_negative != 0:
            return -3.141592653589793
        return 3.141592653589793
    if x_value == 0.0:
        if y_negative != 0:
            return -1.5707963267948966
        return 1.5707963267948966
    if _is_infinite(x_value) and _is_infinite(y_value):
        angle: float = 0.7853981633974483
        if x_negative != 0:
            angle = 2.356194490192345
        if y_negative != 0:
            return 0.0 - angle
        return angle
    if _is_infinite(y_value):
        if y_negative != 0:
            return -1.5707963267948966
        return 1.5707963267948966
    if _is_infinite(x_value):
        if x_negative == 0:
            return y_value * 0.0
        if y_negative != 0:
            return -3.141592653589793
        return 3.141592653589793

    ratio: float = _absolute(f64_div(y_value, x_value))
    angle = _atan_positive(ratio)
    if x_negative != 0:
        angle = 3.141592653589793 - angle
    if y_negative != 0:
        return 0.0 - angle
    return angle


@c_abi_export("fmod")
def pcc_fmod(left: float, right: float) -> float:
    if _is_nan(left) or _is_nan(right):
        return left + right
    if right == 0.0 or _is_infinite(left):
        return _math_invalid(left)
    if _is_infinite(right):
        return left
    remainder: float = _absolute(left)
    divisor: float = _absolute(right)
    if remainder < divisor:
        return left
    if remainder == divisor:
        return left * 0.0

    # Binary long division avoids converting an unbounded quotient to i64.
    # At most the binary64 exponent range (~2100 iterations) is visited.
    scaled: float = divisor
    while scaled <= remainder * 0.5:
        scaled = scaled * 2.0
    while scaled >= divisor:
        if remainder >= scaled:
            remainder = remainder - scaled
        scaled = scaled * 0.5
    if remainder == 0.0:
        return left * 0.0
    if f64_signbit(left) != 0:
        return 0.0 - remainder
    return remainder


@c_abi_export("pcc_numeric_integer_parity")
def _integer_parity(value: float) -> i64:
    # -1 means non-integral, 0 even, and 1 odd.
    absolute: float = _absolute(value)
    if absolute >= 9007199254740992.0:
        # Binary64 spacing is at least two here, so every finite value is even.
        return 0
    integer: i64 = float_to_i64(value)
    if i64_to_float(integer) != value:
        return -1
    return integer & 1


@c_abi_export("pow")
def pcc_pow(base: float, exponent: float) -> float:
    if exponent == 0.0:
        if _is_signaling_nan(base):
            return base + exponent
        return 1.0
    if base == 1.0:
        if _is_signaling_nan(exponent):
            return base + exponent
        return 1.0
    if _is_nan(base) or _is_nan(exponent):
        return base + exponent

    absolute_base: float = _absolute(base)
    if _is_infinite(exponent):
        if absolute_base == 1.0:
            return 1.0
        if (absolute_base > 1.0) == (exponent > 0.0):
            return _positive_infinity()
        return 0.0

    parity: i64 = _integer_parity(exponent)
    if base == 0.0:
        if exponent < 0.0:
            if f64_signbit(base) != 0 and parity == 1:
                return _math_divzero(1)
            return _math_divzero(0)
        if f64_signbit(base) != 0 and parity == 1:
            return base
        return 0.0
    if _is_infinite(base):
        if exponent > 0.0:
            if f64_signbit(base) != 0 and parity == 1:
                return base
            return absolute_base
        if f64_signbit(base) != 0 and parity == 1:
            return f64_div(1.0, base)
        return 0.0
    # Keep the square-root cases on the direct freestanding implementation.
    # The generic exp(log(x) * y) path loses avoidable precision for +/-0.5.
    if exponent == 0.5:
        return pcc_sqrt(base)
    if exponent == -0.5:
        return f64_div(1.0, pcc_sqrt(base))

    # Avoid fptosi poison for huge finite exponents. Such binary64 values are
    # even integers, and only the overflow/underflow direction remains.
    if _absolute(exponent) >= 9223372036854775808.0:
        if absolute_base == 1.0:
            return 1.0
        if (absolute_base > 1.0) == (exponent > 0.0):
            return _math_overflow(0)
        return _math_underflow(0)

    integer_exponent: i64 = float_to_i64(exponent)
    if parity >= 0:
        factor: float = base
        if integer_exponent < 0:
            integer_exponent = 0 - integer_exponent
            # Invert before exponentiation.  Computing base**abs(y) first can
            # raise a spurious overflow on a result whose only correct event
            # is reciprocal underflow (for example 1e200**-2).
            factor = f64_div(1.0, base)
        result: float = 1.0
        while integer_exponent > 0:
            if (integer_exponent & 1) != 0:
                result = result * factor
            integer_exponent = logical_shift_right_i64(integer_exponent, 1)
            if integer_exponent > 0:
                factor = factor * factor
        if (
            _is_infinite(result)
            or result == 0.0
            or _absolute(result) < 2.2250738585072014e-308
        ):
            pcc_errno_set(34)
        return result
    if base < 0.0:
        return _math_invalid(base)
    return pcc_exp(exponent * pcc_log(base))
