import sys

from pcc.package.array_numeric import binary_token, cast_token, compare_tokens, float_token, reduce_tokens, token_float, token_integer, token_is_float, token_truth, unary_token


def _write_text(text: str, *, err: bool = False, nl: bool = True) -> None:
    if nl:
        if text.endswith("\n"):
            if err:
                sys.stderr.write(text)
            else:
                sys.stdout.write(text)
        else:
            if err:
                sys.stderr.write(text + "\n")
            else:
                sys.stdout.write(text + "\n")
    else:
        if err:
            sys.stderr.write(text)
        else:
            sys.stdout.write(text)


def _json_escape(text: str) -> str:
    out = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            out += "\\\\"
        elif ch == '"':
            out += '\"'
        elif ch == "\n":
            out += "\\n"
        elif ch == "\r":
            out += "\\r"
        elif ch == "\t":
            out += "\\t"
        else:
            out += ch
        i += 1
    return out


def _json_str(text: str) -> str:
    return '"' + _json_escape(text) + '"'


def _json_int_list(items) -> str:
    out = "["
    i = 0
    while i < len(items):
        if i > 0:
            out += ", "
        out += str(int(items[i]))
        i += 1
    out += "]"
    return out


def _native_find_from(text: str, needle: str, start: int) -> int:
    if needle == "":
        return start
    i = start
    limit = len(text) - len(needle)
    while i <= limit:
        j = 0
        matched = True
        while j < len(needle):
            if text[i + j] != needle[j]:
                matched = False
                break
            j += 1
        if matched:
            return i
        i += 1
    return -1


def _native_list_contains(items, value) -> bool:
    i = 0
    while i < len(items):
        if items[i] == value:
            return True
        i += 1
    return False


def _native_array_dtype_itemsize(dtype: str) -> int:
    if dtype == "bool" or dtype == "int8" or dtype == "uint8":
        return 1
    if dtype == "int16" or dtype == "uint16":
        return 2
    if dtype == "int32" or dtype == "uint32" or dtype == "float32":
        return 4
    if dtype == "int64" or dtype == "uint64" or dtype == "float64" or dtype == "object":
        return 8
    return 8


def _native_array_dtype_format(dtype: str) -> str:
    if dtype == "bool":
        return "?"
    if dtype == "int8":
        return "b"
    if dtype == "int16":
        return "h"
    if dtype == "int32":
        return "i"
    if dtype == "int64":
        return "q"
    if dtype == "uint8":
        return "B"
    if dtype == "uint16":
        return "H"
    if dtype == "uint32":
        return "I"
    if dtype == "uint64":
        return "Q"
    if dtype == "float32":
        return "f"
    if dtype == "float64":
        return "d"
    return "O"


def _native_array_normalize_dtype(dtype: str) -> str:
    d = (dtype or "auto").lower()
    if d == "auto" or d == "":
        return "object"
    if d == "bool" or d == "bool_" or d == "boolean":
        return "bool"
    if d == "int8" or d == "byte":
        return "int8"
    if d == "int16" or d == "short":
        return "int16"
    if d == "int32" or d == "intc":
        return "int32"
    if d == "int" or d == "int_" or d == "long" or d == "longlong" or d == "int64":
        return "int64"
    if d == "uint8":
        return "uint8"
    if d == "uint16":
        return "uint16"
    if d == "uint32":
        return "uint32"
    if d == "uint" or d == "uint_" or d == "ulong" or d == "uint64":
        return "uint64"
    if d == "float32" or d == "single":
        return "float32"
    if d == "float" or d == "float_" or d == "double" or d == "float64":
        return "float64"
    if d == "object" or d == "object_" or d == "pyobject":
        return "object"
    return "object"


def _native_array_is_integer_dtype(dtype: str) -> bool:
    return (
        dtype == "int8"
        or dtype == "int16"
        or dtype == "int32"
        or dtype == "int64"
        or dtype == "uint8"
        or dtype == "uint16"
        or dtype == "uint32"
        or dtype == "uint64"
    )


def _native_array_integer_bits(dtype: str) -> int:
    if dtype == "int8" or dtype == "uint8":
        return 8
    if dtype == "int16" or dtype == "uint16":
        return 16
    if dtype == "int32" or dtype == "uint32":
        return 32
    return 64


def _native_array_integer_signed(dtype: str) -> bool:
    return dtype == "int8" or dtype == "int16" or dtype == "int32" or dtype == "int64"


def _native_array_int_pow2(bits: int) -> int:
    # Dtype metadata handles the 64-bit limits as text. Remaining widths fit
    # in i64; numeric wrapping itself belongs to array_numeric.wrap_integer.
    value = 1
    i = 0
    while i < bits:
        value *= 2
        i += 1
    return value


def _native_array_dtype_range_json(dtype: str) -> str:
    if dtype == "bool":
        return "[0, 1]"
    if not _native_array_is_integer_dtype(dtype):
        return "null"
    bits = _native_array_integer_bits(dtype)
    # 2**63 and 2**64 do not fit the raw i64 lane this module is compiled
    # in (pcc1 read them as 0, reporting uint64 as [0, -1]); the 64-bit
    # limits are text, as _native_array_int_pow2 documents.
    if _native_array_integer_signed(dtype):
        if bits == 64:
            return "[-9223372036854775808, 9223372036854775807]"
        low = -_native_array_int_pow2(bits - 1)
        high = _native_array_int_pow2(bits - 1) - 1
        return "[" + str(low) + ", " + str(high) + "]"
    if bits == 64:
        return "[0, 18446744073709551615]"
    high = _native_array_int_pow2(bits) - 1
    return "[0, " + str(high) + "]"


def _native_array_parse_shape(text: str):
    dims = []
    token = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == ",":
            if token.strip() != "":
                try:
                    dims.append(int(token.strip()))
                except Exception:
                    dims.append(-1)
            token = ""
        else:
            token += ch
        i += 1
    if token.strip() != "":
        try:
            dims.append(int(token.strip()))
        except Exception:
            dims.append(-1)
    return dims


def _native_array_split_commas(text: str):
    parts = []
    token = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == ",":
            if token.strip() != "":
                parts.append(token.strip())
            token = ""
        else:
            token += ch
        i += 1
    if token.strip() != "":
        parts.append(token.strip())
    return parts


def _native_array_size(shape) -> int:
    if len(shape) == 0:
        return 1
    total = 1
    i = 0
    while i < len(shape):
        dim = shape[i]
        if dim == 0:
            return 0
        total *= dim
        i += 1
    return total


def _native_array_strides(shape, itemsize: int):
    strides = []
    i = 0
    while i < len(shape):
        strides.append(0)
        i += 1
    stride = itemsize
    i = len(shape) - 1
    while i >= 0:
        strides[i] = stride
        dim = shape[i]
        if dim > 0:
            stride *= dim
        i -= 1
    return strides


def _native_array_literal_dtype(text: str) -> str:
    has_quote = False
    has_float = False
    has_int = False
    has_bool = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"' or ch == "'":
            has_quote = True
        if ch == "." or (
            (ch == "e" or ch == "E") and i > 0 and "0" <= text[i - 1] <= "9"
        ):
            has_float = True
        if "0" <= ch <= "9":
            has_int = True
        i += 1
    if (
        _native_find_from(text, "True", 0) >= 0
        or _native_find_from(text, "False", 0) >= 0
    ):
        has_bool = True
    if has_quote:
        return "object"
    if has_float:
        return "float64"
    if has_int:
        return "int64"
    if has_bool:
        return "bool"
    return "object"


def _native_array_literal_shape_and_diagnostics(text: str):
    stripped = text.strip()
    diagnostics = []
    if stripped == "" or stripped == "[]":
        diagnostics.append("PCC-ARRAY-EMPTY-DTYPE")
        return [[0], diagnostics]
    if not stripped.startswith("["):
        return [[], diagnostics]
    is_2d = stripped.startswith("[[")
    if is_2d:
        row_counts = []
        depth = 0
        cols = 0
        token = False
        i = 0
        while i < len(stripped):
            ch = stripped[i]
            if ch == "[":
                if depth == 1:
                    cols = 0
                    token = False
                depth += 1
            elif ch == "]":
                if depth == 2:
                    if token:
                        cols += 1
                    row_counts.append(cols)
                    token = False
                depth -= 1
            elif ch == ",":
                if depth == 2 and token:
                    cols += 1
                    token = False
            elif depth == 2 and ch != " " and ch != "\n" and ch != "\t":
                token = True
            i += 1
        if len(row_counts) == 0:
            diagnostics.append("PCC-ARRAY-LITERAL-PARSE-FAILED")
            return [[], diagnostics]
        first = row_counts[0]
        j = 0
        while j < len(row_counts):
            if row_counts[j] != first:
                diagnostics.append("PCC-ARRAY-RAGGED")
                return [[len(row_counts)], diagnostics]
            j += 1
        return [[len(row_counts), first], diagnostics]
    count = 0
    depth = 0
    token = False
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            if depth == 1 and token:
                count += 1
                token = False
            depth -= 1
        elif ch == ",":
            if depth == 1 and token:
                count += 1
                token = False
        elif depth == 1 and ch != " " and ch != "\n" and ch != "\t":
            token = True
        i += 1
    return [[count], diagnostics]


def _native_array_literal_values(text: str):
    stripped = text.strip()
    if stripped == "":
        return []
    if not stripped.startswith("["):
        return [stripped]
    values = []
    token = ""
    quote = ""
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if quote != "":
            token += ch
            if ch == quote:
                values.append(token.strip())
                token = ""
                quote = ""
        elif ch == '"' or ch == "'":
            quote = ch
            token = ch
        elif (
            ch == "[" or ch == "]" or ch == "," or ch == " " or ch == "\n" or ch == "\t"
        ):
            if token.strip() != "":
                values.append(token.strip())
                token = ""
        else:
            token += ch
        i += 1
    if token.strip() != "":
        values.append(token.strip())
    return values


def _native_array_token_json(token: str) -> str:
    stripped = token.strip()
    if stripped == "True":
        return "true"
    if stripped == "False":
        return "false"
    if stripped == "nan":
        return "NaN"
    if stripped == "inf":
        return "Infinity"
    if stripped == "-inf":
        return "-Infinity"
    if stripped.startswith('"') or stripped.startswith("'"):
        inner = stripped[1:]
        if len(inner) > 0 and (inner.endswith('"') or inner.endswith("'")):
            inner = inner[:-1]
        return _json_str(inner)
    return stripped


def _native_array_values_json(values) -> str:
    out = "["
    i = 0
    while i < len(values):
        if i > 0:
            out += ", "
        out += _native_array_token_json(values[i])
        i += 1
    out += "]"
    return out


def _native_array_data_json(shape, values) -> str:
    if len(shape) > 0 and len(values) != _native_array_size(shape):
        return _native_array_values_json(values)
    if len(shape) == 0:
        if len(values) == 0:
            return "null"
        return _native_array_token_json(values[0])
    if len(shape) == 1:
        return _native_array_values_json(values)
    if len(shape) == 2:
        rows = shape[0]
        cols = shape[1]
        out = "["
        r = 0
        while r < rows:
            if r > 0:
                out += ", "
            row = []
            c = 0
            while c < cols:
                pos = r * cols + c
                if pos < len(values):
                    row.append(values[pos])
                c += 1
            out += _native_array_values_json(row)
            r += 1
        out += "]"
        return out
    return _native_array_values_json(values)


def _native_array_token_repr(token: str) -> str:
    # repr() of the scalar a token spells, as pcc.package.array_model renders
    # array items: True/False stay, strings take Python quotes, and a float
    # token prints as its value (4e20 -> 4e+20, 2.50 -> 2.5).
    stripped = token.strip()
    if stripped.startswith('"') or stripped.startswith("'"):
        inner = stripped[1:]
        if len(inner) > 0 and (inner.endswith('"') or inner.endswith("'")):
            inner = inner[:-1]
        return repr(inner)
    if stripped in ("True", "False", "None", "nan", "inf", "-inf"):
        return stripped
    if _native_array_is_float_token(stripped):
        return repr(float(stripped))
    return stripped


def _native_array_items_repr(values) -> str:
    out = "["
    i = 0
    while i < len(values):
        if i > 0:
            out += ", "
        out += _native_array_token_repr(values[i])
        i += 1
    return out + "]"


def _native_array_nested_repr(shape, values, axis: int, start: int) -> str:
    if axis == len(shape) - 1:
        return _native_array_items_repr(values[start:start + shape[axis]])
    stride = 1
    k = axis + 1
    while k < len(shape):
        stride *= shape[k]
        k += 1
    out = "["
    i = 0
    while i < shape[axis]:
        if i > 0:
            out += ", "
        out += _native_array_nested_repr(shape, values, axis + 1, start + i * stride)
        i += 1
    return out + "]"


def _native_array_repr(shape, values) -> str:
    # Mirror pcc.package.array_model.array_core_repr: a 2-D array puts each
    # row on its own line, aligned under the first.
    if len(shape) == 0:
        if len(values) == 0:
            return "array(None)"
        return "array(" + _native_array_token_repr(values[0]) + ")"
    if len(values) != _native_array_size(shape):
        if len(shape) == 2:
            items = []
            i = 0
            while i < len(values):
                items.append(_native_array_token_repr(values[i]))
                i += 1
            return "array([" + ",\n       ".join(items) + "])"
        return "array(" + _native_array_items_repr(values) + ")"
    if len(shape) == 2:
        rows = []
        r = 0
        while r < shape[0]:
            rows.append(_native_array_items_repr(values[r * shape[1]:(r + 1) * shape[1]]))
            r += 1
        return "array([" + ",\n       ".join(rows) + "])"
    return "array(" + _native_array_nested_repr(shape, values, 0, 0) + ")"


def _native_array_is_float_token(token: str) -> bool:
    return (
        _native_find_from(token, ".", 0) >= 0
        or _native_find_from(token, "e", 0) >= 0
        or _native_find_from(token, "E", 0) >= 0
    )


def _native_array_op_dtype(op: str, left_dtype: str, right_dtype: str) -> str:
    if left_dtype == "object" or right_dtype == "object":
        return "object"
    if op == "div":
        return "float64"
    if left_dtype == right_dtype:
        return left_dtype
    if left_dtype == "float64" or right_dtype == "float64":
        return "float64"
    if left_dtype == "float32" or right_dtype == "float32":
        return "float32"
    if left_dtype != "bool" or right_dtype != "bool":
        return "int64"
    return "bool"






def _native_array_token_is_number(token: str) -> bool:
    try:
        if token_is_float(token):
            token_float(token)
        else:
            token_integer(token)
        return True
    except ValueError:
        return False


def _native_array_arange_uses_float(arange_text: str) -> bool:
    parts = _native_array_split_commas(arange_text)
    i = 0
    while i < len(parts):
        if token_is_float(parts[i]):
            return True
        i += 1
    return False


def _native_array_cast_values(values, dtype: str):
    out = []
    i = 0
    while i < len(values):
        out.append(cast_token(values[i], dtype))
        i += 1
    return out


def _native_array_apply_op(left: str, right: str, op: str, dtype: str) -> str:
    return binary_token(left, right, op, dtype)


def _native_array_unary_op_name(op: str) -> str:
    if op == "negative":
        return "neg"
    if op == "absolute":
        return "abs"
    if op == "not":
        return "logical_not"
    return op


def _native_array_unary_op(shape, values, dtype: str, op: str, diagnostics):
    op_name = _native_array_unary_op_name(op)
    if not (op_name == "neg" or op_name == "abs" or op_name == "logical_not"):
        diagnostics.append("PCC-ARRAY-UNARY-UNSUPPORTED")
        return [shape, [], dtype]
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-OBJECT-UNARY-UNSUPPORTED")
        return [shape, [], dtype]
    if dtype == "bool" and op_name == "neg":
        diagnostics.append("PCC-ARRAY-UNARY-DTYPE-UNSUPPORTED")
        return [shape, [], dtype]
    out_dtype = dtype
    if op_name == "logical_not":
        out_dtype = "bool"
    out = []
    i = 0
    while i < len(values):
        out.append(unary_token(values[i], op_name, out_dtype))
        i += 1
    return [shape, out, out_dtype]


def _native_array_clip(shape, values, dtype: str, clip_text: str, diagnostics):
    comma = _native_find_from(clip_text, ",", 0)
    if comma < 0:
        diagnostics.append("PCC-ARRAY-CLIP-PARSE-FAILED")
        return [shape, [], dtype]
    lower_text = clip_text[:comma].strip()
    upper_text = clip_text[comma + 1 :].strip()
    if lower_text == "" or upper_text == "":
        diagnostics.append("PCC-ARRAY-CLIP-PARSE-FAILED")
        return [shape, [], dtype]
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-OBJECT-CLIP-UNSUPPORTED")
        return [shape, [], dtype]
    out_dtype = dtype
    if _native_array_is_float_token(lower_text) or _native_array_is_float_token(
        upper_text
    ):
        out_dtype = "float64"
    out = []
    i = 0
    while i < len(values):
        current = values[i]
        if compare_tokens(current, lower_text, "lt"):
            current = lower_text
        if compare_tokens(current, upper_text, "gt"):
            current = upper_text
        out.append(cast_token(current, out_dtype))
        i += 1
    return [shape, out, out_dtype]


def _native_array_broadcast_shape(left_shape, right_shape, diagnostics):
    reversed_out = []
    max_rank = len(left_shape)
    if len(right_shape) > max_rank:
        max_rank = len(right_shape)
    offset = 1
    while offset <= max_rank:
        ldim = 1
        rdim = 1
        if offset <= len(left_shape):
            ldim = left_shape[len(left_shape) - offset]
        if offset <= len(right_shape):
            rdim = right_shape[len(right_shape) - offset]
        if ldim == rdim:
            reversed_out.append(ldim)
        elif ldim == 1:
            reversed_out.append(rdim)
        elif rdim == 1:
            reversed_out.append(ldim)
        else:
            diagnostics.append("PCC-ARRAY-BROADCAST-INCOMPATIBLE")
            return []
        offset += 1
    out = []
    i = len(reversed_out) - 1
    while i >= 0:
        out.append(reversed_out[i])
        i -= 1
    return out


def _native_array_flat_index(shape, indices) -> int:
    if len(shape) == 0:
        return 0
    flat = 0
    stride = 1
    axis = len(shape) - 1
    while axis >= 0:
        flat += indices[axis] * stride
        stride *= shape[axis]
        axis -= 1
    return flat


def _native_array_broadcast_flat_index(shape, out_index) -> int:
    if len(shape) == 0:
        return 0
    source = []
    offset = len(out_index) - len(shape)
    i = 0
    while i < len(shape):
        value = out_index[offset + i]
        if shape[i] == 1:
            source.append(0)
        else:
            source.append(value)
        i += 1
    return _native_array_flat_index(shape, source)


def _native_array_broadcast_to_strides(shape, dtype: str, target_shape):
    if len(shape) > len(target_shape):
        return []
    source_strides = _native_array_strides(shape, _native_array_dtype_itemsize(dtype))
    offset = len(target_shape) - len(shape)
    out = []
    axis = 0
    while axis < len(target_shape):
        if axis < offset:
            out.append(0)
        else:
            source_axis = axis - offset
            source_dim = shape[source_axis]
            target_dim = target_shape[axis]
            if source_dim == target_dim:
                out.append(source_strides[source_axis])
            elif source_dim == 1:
                out.append(0)
            else:
                return []
        axis += 1
    return out


def _native_array_broadcast_to(
    shape, values, dtype: str, target_text: str, diagnostics
):
    target_shape = _native_array_parse_shape(target_text)
    local_diags = []
    out_shape = _native_array_broadcast_shape(shape, target_shape, local_diags)
    strides = _native_array_broadcast_to_strides(shape, dtype, target_shape)
    if (
        len(local_diags) > 0
        or out_shape != target_shape
        or len(strides) != len(target_shape)
    ):
        diagnostics.append("PCC-ARRAY-BROADCAST-TO-SHAPE-MISMATCH")
        return [
            target_shape,
            [],
            dtype,
            _native_array_strides(target_shape, _native_array_dtype_itemsize(dtype)),
            True,
        ]
    out = []
    if len(target_shape) == 0:
        out.append(values[0])
    elif len(target_shape) == 1:
        i = 0
        while i < target_shape[0]:
            out_index = [i]
            out.append(values[_native_array_broadcast_flat_index(shape, out_index)])
            i += 1
    elif len(target_shape) == 2:
        r = 0
        while r < target_shape[0]:
            c = 0
            while c < target_shape[1]:
                out_index = [r, c]
                out.append(values[_native_array_broadcast_flat_index(shape, out_index)])
                c += 1
            r += 1
    else:
        diagnostics.append("PCC-ARRAY-RANK-UNSUPPORTED")
        return [
            target_shape,
            [],
            dtype,
            _native_array_strides(target_shape, _native_array_dtype_itemsize(dtype)),
            False,
        ]
    c_contiguous = shape == target_shape
    return [target_shape, out, dtype, strides, c_contiguous]


def _native_array_repeat(
    shape, values, dtype: str, repeats_text: str, axis_text: str, diagnostics
):
    repeats = int(repeats_text)
    if repeats < 0:
        diagnostics.append("PCC-ARRAY-REPEAT-NEGATIVE")
        return [[], [], dtype]
    out = []
    if axis_text == "":
        i = 0
        while i < len(values):
            j = 0
            while j < repeats:
                out.append(values[i])
                j += 1
            i += 1
        return [[len(out)], out, dtype]
    axis = int(axis_text)
    if axis < 0:
        axis += len(shape)
    if axis < 0 or axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if len(shape) == 1:
        return _native_array_repeat(shape, values, dtype, repeats_text, "", diagnostics)
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-REPEAT-RANK-UNSUPPORTED")
        return [[], [], dtype]
    rows = shape[0]
    cols = shape[1]
    if axis == 0:
        r = 0
        while r < rows:
            j = 0
            while j < repeats:
                c = 0
                while c < cols:
                    out.append(values[r * cols + c])
                    c += 1
                j += 1
            r += 1
        return [[rows * repeats, cols], out, dtype]
    r = 0
    while r < rows:
        c = 0
        while c < cols:
            j = 0
            while j < repeats:
                out.append(values[r * cols + c])
                j += 1
            c += 1
        r += 1
    return [[rows, cols * repeats], out, dtype]


def _native_array_tile_reps(shape, values, dtype: str, reps, diagnostics):
    if len(reps) == 0:
        diagnostics.append("PCC-ARRAY-TILE-REPS-EMPTY")
        return [[], [], dtype]
    i = 0
    while i < len(reps):
        if reps[i] < 0:
            diagnostics.append("PCC-ARRAY-TILE-REPS-NEGATIVE")
            return [[], [], dtype]
        i += 1
    out = []
    if len(shape) == 0:
        size = _native_array_size(reps)
        i = 0
        while i < size:
            out.append(values[0])
            i += 1
        return [reps, out, dtype]
    if len(shape) == 1:
        if len(reps) == 1:
            r = 0
            while r < reps[0]:
                i = 0
                while i < len(values):
                    out.append(values[i])
                    i += 1
                r += 1
            return [[len(out)], out, dtype]
        if len(reps) == 2:
            row_reps = reps[0]
            col_reps = reps[1]
            r = 0
            while r < row_reps:
                c = 0
                while c < col_reps:
                    i = 0
                    while i < len(values):
                        out.append(values[i])
                        i += 1
                    c += 1
                r += 1
            return [[row_reps, shape[0] * col_reps], out, dtype]
    if len(shape) == 2 and (len(reps) == 1 or len(reps) == 2):
        row_reps = 1
        col_reps = reps[0]
        if len(reps) == 2:
            row_reps = reps[0]
            col_reps = reps[1]
        rows = shape[0]
        cols = shape[1]
        rr = 0
        while rr < row_reps:
            r = 0
            while r < rows:
                cc = 0
                while cc < col_reps:
                    c = 0
                    while c < cols:
                        out.append(values[r * cols + c])
                        c += 1
                    cc += 1
                r += 1
            rr += 1
        return [[rows * row_reps, cols * col_reps], out, dtype]
    diagnostics.append("PCC-ARRAY-TILE-RANK-UNSUPPORTED")
    return [[], [], dtype]


def _native_array_tile(shape, values, dtype: str, tile_text: str, diagnostics):
    return _native_array_tile_reps(
        shape,
        values,
        dtype,
        _native_array_parse_shape(tile_text),
        diagnostics,
    )


def _native_array_roll(
    shape, values, dtype: str, shift_text: str, axis_text: str, diagnostics
):
    shift = int(shift_text)
    if _native_array_size(shape) == 0:
        return [shape, [], dtype]
    out = []
    axis = _native_array_axis_value(axis_text)
    if axis == -999999:
        offset = shift % len(values)
        i = 0
        while i < len(values):
            source = (i - offset) % len(values)
            out.append(values[source])
            i += 1
        return [shape, out, dtype]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [shape, [], dtype]
    if len(shape) == 0:
        return [shape, _native_array_copy_values(values), dtype]
    if len(shape) == 1:
        return _native_array_roll(shape, values, dtype, shift_text, "", diagnostics)
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-ROLL-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    rows = shape[0]
    cols = shape[1]
    if normalized_axis == 0:
        offset = shift % rows if rows > 0 else 0
        r = 0
        while r < rows:
            source_row = (r - offset) % rows
            c = 0
            while c < cols:
                out.append(values[source_row * cols + c])
                c += 1
            r += 1
    else:
        offset = shift % cols if cols > 0 else 0
        r = 0
        while r < rows:
            c = 0
            while c < cols:
                source_col = (c - offset) % cols
                out.append(values[r * cols + source_col])
                c += 1
            r += 1
    return [shape, out, dtype]


def _native_array_binary_op(
    left_shape, left_values, left_dtype: str, right_text: str, op: str, diagnostics
):
    right_shape = []
    if right_text.strip().startswith("["):
        parsed = _native_array_literal_shape_and_diagnostics(right_text)
        right_shape = parsed[0]
        right_diags = parsed[1]
        i = 0
        while i < len(right_diags):
            diagnostics.append(right_diags[i])
            i += 1
    right_values = _native_array_literal_values(right_text)
    right_dtype = _native_array_literal_dtype(right_text)
    op_name = op
    if op_name == "subtract":
        op_name = "sub"
    elif op_name == "multiply":
        op_name = "mul"
    elif op_name == "divide":
        op_name = "div"
    if not (
        op_name == "add" or op_name == "sub" or op_name == "mul" or op_name == "div"
    ):
        diagnostics.append("PCC-ARRAY-UFUNC-UNSUPPORTED")
        return [[], [], "object"]
    out_shape = _native_array_broadcast_shape(left_shape, right_shape, diagnostics)
    dtype = _native_array_op_dtype(op_name, left_dtype, right_dtype)
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-OBJECT-UFUNC-UNSUPPORTED")
    if len(diagnostics) > 0:
        return [out_shape, [], dtype]
    out_values = []
    if len(out_shape) == 0:
        out_values.append(
            _native_array_apply_op(left_values[0], right_values[0], op_name, dtype)
        )
    elif len(out_shape) == 1:
        i = 0
        while i < out_shape[0]:
            out_index = [i]
            lv = left_values[_native_array_broadcast_flat_index(left_shape, out_index)]
            rv = right_values[
                _native_array_broadcast_flat_index(right_shape, out_index)
            ]
            out_values.append(_native_array_apply_op(lv, rv, op_name, dtype))
            i += 1
    elif len(out_shape) == 2:
        r = 0
        while r < out_shape[0]:
            c = 0
            while c < out_shape[1]:
                out_index = [r, c]
                lv = left_values[
                    _native_array_broadcast_flat_index(left_shape, out_index)
                ]
                rv = right_values[
                    _native_array_broadcast_flat_index(right_shape, out_index)
                ]
                out_values.append(_native_array_apply_op(lv, rv, op_name, dtype))
                c += 1
            r += 1
    else:
        diagnostics.append("PCC-ARRAY-RANK-UNSUPPORTED")
        return [out_shape, [], dtype]
    return [out_shape, out_values, dtype]


def _native_array_matmul_dtype(left_dtype: str, right_dtype: str) -> str:
    dtype = _native_array_op_dtype("mul", left_dtype, right_dtype)
    if dtype == "bool":
        return "int64"
    return dtype




def _native_array_matmul(
    left_shape, left_values, left_dtype: str, right_text: str, diagnostics
):
    right_shape = []
    if right_text.strip().startswith("["):
        parsed = _native_array_literal_shape_and_diagnostics(right_text)
        right_shape = parsed[0]
        right_diags = parsed[1]
        i = 0
        while i < len(right_diags):
            diagnostics.append(right_diags[i])
            i += 1
    right_values = _native_array_literal_values(right_text)
    right_dtype = _native_array_literal_dtype(right_text)
    dtype = _native_array_matmul_dtype(left_dtype, right_dtype)
    if left_dtype == "object" or right_dtype == "object":
        diagnostics.append("PCC-ARRAY-MATMUL-OBJECT-UNSUPPORTED")
    out_shape = []
    left_rank = len(left_shape)
    right_rank = len(right_shape)
    if not (
        (left_rank == 1 or left_rank == 2) and (right_rank == 1 or right_rank == 2)
    ):
        diagnostics.append("PCC-ARRAY-MATMUL-RANK-UNSUPPORTED")
    elif left_rank == 1 and right_rank == 1:
        if left_shape[0] != right_shape[0]:
            diagnostics.append("PCC-ARRAY-MATMUL-SHAPE-MISMATCH")
    elif left_rank == 2 and right_rank == 1:
        if left_shape[1] != right_shape[0]:
            diagnostics.append("PCC-ARRAY-MATMUL-SHAPE-MISMATCH")
        out_shape = [left_shape[0]]
    elif left_rank == 1 and right_rank == 2:
        if left_shape[0] != right_shape[0]:
            diagnostics.append("PCC-ARRAY-MATMUL-SHAPE-MISMATCH")
        out_shape = [right_shape[1]]
    else:
        if left_shape[1] != right_shape[0]:
            diagnostics.append("PCC-ARRAY-MATMUL-SHAPE-MISMATCH")
        out_shape = [left_shape[0], right_shape[1]]
    if len(diagnostics) > 0:
        return [out_shape, [], dtype]
    out = []
    if left_rank == 1 and right_rank == 1:
        products = []
        i = 0
        while i < left_shape[0]:
            products.append(binary_token(left_values[i], right_values[i], "mul", dtype))
            i += 1
        out.append(reduce_tokens(products, "sum", dtype))
        return [[], out, dtype]
    if left_rank == 2 and right_rank == 1:
        rows = left_shape[0]
        inner = left_shape[1]
        r = 0
        while r < rows:
            products = []
            i = 0
            while i < inner:
                products.append(binary_token(left_values[r * inner + i], right_values[i], "mul", dtype))
                i += 1
            out.append(reduce_tokens(products, "sum", dtype))
            r += 1
        return [out_shape, out, dtype]
    if left_rank == 1 and right_rank == 2:
        inner = right_shape[0]
        cols = right_shape[1]
        c = 0
        while c < cols:
            products = []
            i = 0
            while i < inner:
                products.append(binary_token(left_values[i], right_values[i * cols + c], "mul", dtype))
                i += 1
            out.append(reduce_tokens(products, "sum", dtype))
            c += 1
        return [out_shape, out, dtype]
    rows = left_shape[0]
    inner = left_shape[1]
    cols = right_shape[1]
    r = 0
    while r < rows:
        c = 0
        while c < cols:
            products = []
            i = 0
            while i < inner:
                products.append(binary_token(left_values[r * inner + i], right_values[i * cols + c], "mul", dtype))
                i += 1
            out.append(reduce_tokens(products, "sum", dtype))
            c += 1
        r += 1
    return [out_shape, out, dtype]


def _native_array_concat(
    left_shape,
    left_values,
    left_dtype: str,
    right_text: str,
    axis_text: str,
    diagnostics,
):
    right_shape = []
    if right_text.strip().startswith("["):
        parsed = _native_array_literal_shape_and_diagnostics(right_text)
        right_shape = parsed[0]
        right_diags = parsed[1]
        i = 0
        while i < len(right_diags):
            diagnostics.append(right_diags[i])
            i += 1
    right_values = _native_array_literal_values(right_text)
    right_dtype = _native_array_literal_dtype(right_text)
    dtype = _native_array_op_dtype("add", left_dtype, right_dtype)
    if len(left_shape) != len(right_shape):
        diagnostics.append("PCC-ARRAY-CONCAT-RANK-MISMATCH")
        return [[], [], dtype]
    axis = 0
    if axis_text != "":
        axis = int(axis_text)
    if axis < 0:
        axis += len(left_shape)
    if axis < 0 or axis >= len(left_shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    out = []
    if len(left_shape) == 1:
        if axis != 0:
            diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
            return [[], [], dtype]
        i = 0
        while i < len(left_values):
            out.append(left_values[i])
            i += 1
        i = 0
        while i < len(right_values):
            out.append(right_values[i])
            i += 1
        return [
            [left_shape[0] + right_shape[0]],
            _native_array_cast_values(out, dtype),
            dtype,
        ]
    if len(left_shape) != 2:
        diagnostics.append("PCC-ARRAY-CONCAT-RANK-UNSUPPORTED")
        return [[], [], dtype]
    left_rows = left_shape[0]
    left_cols = left_shape[1]
    right_rows = right_shape[0]
    right_cols = right_shape[1]
    if axis == 0:
        if left_cols != right_cols:
            diagnostics.append("PCC-ARRAY-CONCAT-SHAPE-MISMATCH")
            return [[left_rows + right_rows, left_cols], [], dtype]
        i = 0
        while i < len(left_values):
            out.append(left_values[i])
            i += 1
        i = 0
        while i < len(right_values):
            out.append(right_values[i])
            i += 1
        return [
            [left_rows + right_rows, left_cols],
            _native_array_cast_values(out, dtype),
            dtype,
        ]
    if left_rows != right_rows:
        diagnostics.append("PCC-ARRAY-CONCAT-SHAPE-MISMATCH")
        return [[left_rows, left_cols + right_cols], [], dtype]
    r = 0
    while r < left_rows:
        c = 0
        while c < left_cols:
            out.append(left_values[r * left_cols + c])
            c += 1
        c = 0
        while c < right_cols:
            out.append(right_values[r * right_cols + c])
            c += 1
        r += 1
    return [
        [left_rows, left_cols + right_cols],
        _native_array_cast_values(out, dtype),
        dtype,
    ]


def _native_array_stack(
    left_shape,
    left_values,
    left_dtype: str,
    right_text: str,
    axis_text: str,
    diagnostics,
):
    right_shape = []
    if right_text.strip().startswith("["):
        parsed = _native_array_literal_shape_and_diagnostics(right_text)
        right_shape = parsed[0]
        right_diags = parsed[1]
        i = 0
        while i < len(right_diags):
            diagnostics.append(right_diags[i])
            i += 1
    right_values = _native_array_literal_values(right_text)
    right_dtype = _native_array_literal_dtype(right_text)
    dtype = _native_array_op_dtype("add", left_dtype, right_dtype)
    if len(left_shape) != 1 or len(right_shape) != 1:
        diagnostics.append("PCC-ARRAY-STACK-RANK-UNSUPPORTED")
        return [[], [], dtype]
    if left_shape[0] != right_shape[0]:
        diagnostics.append("PCC-ARRAY-STACK-SHAPE-MISMATCH")
        return [[], [], dtype]
    axis = 0
    if axis_text != "":
        axis = int(axis_text)
    if axis < 0:
        axis += 2
    out = []
    if axis == 0:
        i = 0
        while i < len(left_values):
            out.append(left_values[i])
            i += 1
        i = 0
        while i < len(right_values):
            out.append(right_values[i])
            i += 1
        return [[2, left_shape[0]], _native_array_cast_values(out, dtype), dtype]
    if axis == 1:
        i = 0
        while i < left_shape[0]:
            out.append(left_values[i])
            out.append(right_values[i])
            i += 1
        return [[left_shape[0], 2], _native_array_cast_values(out, dtype), dtype]
    diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
    return [[], [], dtype]


def _native_array_compare_token(
    left: str, right: str, op: str, left_dtype: str, right_dtype: str
) -> str:
    if left_dtype == "object" or right_dtype == "object":
        if op == "eq":
            return "True" if left == right else "False"
        if op == "ne":
            return "True" if left != right else "False"
        return "False"
    ok = compare_tokens(left, right, op)
    return "True" if ok else "False"


def _native_array_compare(
    left_shape, left_values, left_dtype: str, right_text: str, op: str, diagnostics
):
    op_name = op
    if op_name == "==":
        op_name = "eq"
    elif op_name == "!=":
        op_name = "ne"
    elif op_name == "<":
        op_name = "lt"
    elif op_name == "<=":
        op_name = "le"
    elif op_name == ">":
        op_name = "gt"
    elif op_name == ">=":
        op_name = "ge"
    if not (
        op_name == "eq"
        or op_name == "ne"
        or op_name == "lt"
        or op_name == "le"
        or op_name == "gt"
        or op_name == "ge"
    ):
        diagnostics.append("PCC-ARRAY-COMPARE-UNSUPPORTED")
        return [[], [], "bool"]
    right_shape = []
    if right_text.strip().startswith("["):
        parsed = _native_array_literal_shape_and_diagnostics(right_text)
        right_shape = parsed[0]
        right_diags = parsed[1]
        i = 0
        while i < len(right_diags):
            diagnostics.append(right_diags[i])
            i += 1
    right_values = _native_array_literal_values(right_text)
    right_dtype = _native_array_literal_dtype(right_text)
    if (left_dtype == "object" or right_dtype == "object") and not (
        op_name == "eq" or op_name == "ne"
    ):
        diagnostics.append("PCC-ARRAY-OBJECT-COMPARE-UNSUPPORTED")
    out_shape = _native_array_broadcast_shape(left_shape, right_shape, diagnostics)
    if len(diagnostics) > 0:
        return [out_shape, [], "bool"]
    out_values = []
    if len(out_shape) == 0:
        out_values.append(
            _native_array_compare_token(
                left_values[0], right_values[0], op_name, left_dtype, right_dtype
            )
        )
    elif len(out_shape) == 1:
        i = 0
        while i < out_shape[0]:
            out_index = [i]
            lv = left_values[_native_array_broadcast_flat_index(left_shape, out_index)]
            rv = right_values[
                _native_array_broadcast_flat_index(right_shape, out_index)
            ]
            out_values.append(
                _native_array_compare_token(lv, rv, op_name, left_dtype, right_dtype)
            )
            i += 1
    elif len(out_shape) == 2:
        r = 0
        while r < out_shape[0]:
            c = 0
            while c < out_shape[1]:
                out_index = [r, c]
                lv = left_values[
                    _native_array_broadcast_flat_index(left_shape, out_index)
                ]
                rv = right_values[
                    _native_array_broadcast_flat_index(right_shape, out_index)
                ]
                out_values.append(
                    _native_array_compare_token(
                        lv, rv, op_name, left_dtype, right_dtype
                    )
                )
                c += 1
            r += 1
    else:
        diagnostics.append("PCC-ARRAY-RANK-UNSUPPORTED")
        return [out_shape, [], "bool"]
    return [out_shape, out_values, "bool"]


def _native_array_parse_slice_indices(text: str, dim: int, diagnostics):
    token = text.strip()
    parts = []
    current = ""
    i = 0
    while i < len(token):
        ch = token[i]
        if ch == ":":
            parts.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    parts.append(current.strip())
    if len(parts) == 1:
        if parts[0] == "":
            diagnostics.append("PCC-ARRAY-INDEX-PARSE-FAILED")
            return [[], True]
        index = int(token)
        if index < 0:
            index += dim
        if index < 0 or index >= dim:
            diagnostics.append("PCC-ARRAY-INDEX-OUT-OF-BOUNDS")
            return [[], True]
        return [[index], True]
    if len(parts) > 3:
        diagnostics.append("PCC-ARRAY-INDEX-PARSE-FAILED")
        return [[], False]
    step = 1
    if len(parts) == 3 and parts[2] != "":
        step = int(parts[2])
    if step == 0:
        diagnostics.append("PCC-ARRAY-INDEX-PARSE-FAILED")
        return [[], False]
    out = []
    if step > 0:
        start = 0 if parts[0] == "" else int(parts[0])
        stop = dim if len(parts) < 2 or parts[1] == "" else int(parts[1])
        if start < 0:
            start += dim
        if stop < 0:
            stop += dim
        if start < 0:
            start = 0
        if start > dim:
            start = dim
        if stop < 0:
            stop = 0
        if stop > dim:
            stop = dim
        i = start
        while i < stop:
            out.append(i)
            i += step
    else:
        start = dim - 1 if parts[0] == "" else int(parts[0])
        stop = -1
        if len(parts) >= 2 and parts[1] != "":
            stop = int(parts[1])
            if stop < 0:
                stop += dim
        if start < 0:
            start += dim
        if start >= dim:
            start = dim - 1
        if start < -1:
            start = -1
        if stop >= dim:
            stop = dim - 1
        if stop < -1:
            stop = -1
        i = start
        while i > stop:
            out.append(i)
            i += step
    return [out, False]


def _native_array_is_newaxis_token(text: str) -> bool:
    token = text.strip()
    return token == "None" or token == "none" or token == "newaxis"


def _native_array_index(shape, values, dtype: str, index_spec: str, diagnostics):
    parts = []
    token = ""
    i = 0
    while i < len(index_spec):
        ch = index_spec[i]
        if ch == ",":
            parts.append(token.strip())
            token = ""
        else:
            token += ch
        i += 1
    if token.strip() != "":
        parts.append(token.strip())
    ellipsis_count = 0
    consumed = 0
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "...":
            ellipsis_count += 1
        elif _native_array_is_newaxis_token(part):
            pass
        else:
            consumed += 1
        i += 1
    if ellipsis_count > 1:
        diagnostics.append("PCC-ARRAY-INDEX-PARSE-FAILED")
        return [[], [], dtype]
    if ellipsis_count == 0:
        if consumed != len(shape):
            diagnostics.append("PCC-ARRAY-INDEX-RANK-MISMATCH")
            return [[], [], dtype]
    else:
        fill = len(shape) - consumed
        if fill < 0:
            diagnostics.append("PCC-ARRAY-INDEX-RANK-MISMATCH")
            return [[], [], dtype]
        expanded = []
        i = 0
        while i < len(parts):
            part = parts[i]
            if part == "...":
                j = 0
                while j < fill:
                    expanded.append(":")
                    j += 1
            else:
                expanded.append(part)
            i += 1
        parts = expanded
    if len(shape) > 2:
        diagnostics.append("PCC-ARRAY-RANK-UNSUPPORTED")
        return [[], [], dtype]
    source_axes = []
    output_shape = []
    source_axis = 0
    i = 0
    while i < len(parts):
        part = parts[i]
        if _native_array_is_newaxis_token(part):
            output_shape.append(1)
        else:
            if source_axis >= len(shape):
                diagnostics.append("PCC-ARRAY-INDEX-RANK-MISMATCH")
                return [[], [], dtype]
            parsed = _native_array_parse_slice_indices(
                part, shape[source_axis], diagnostics
            )
            if len(diagnostics) > 0:
                return [output_shape, [], dtype]
            indices = parsed[0]
            scalar = parsed[1]
            source_axes.append(indices)
            if not scalar:
                output_shape.append(len(indices))
            source_axis += 1
        i += 1
    if source_axis != len(shape):
        diagnostics.append("PCC-ARRAY-INDEX-RANK-MISMATCH")
        return [output_shape, [], dtype]
    out_values = []
    if len(shape) == 0:
        out_values = _native_array_copy_values(values)
    elif len(shape) == 1:
        i = 0
        while i < len(source_axes[0]):
            out_values.append(values[source_axes[0][i]])
            i += 1
    else:
        ri = 0
        while ri < len(source_axes[0]):
            ci = 0
            while ci < len(source_axes[1]):
                out_values.append(
                    values[source_axes[0][ri] * shape[1] + source_axes[1][ci]]
                )
                ci += 1
            ri += 1
    return [output_shape, out_values, dtype]


def _native_array_diagonal(shape, values, dtype: str, diagonal_text: str, diagnostics):
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-DIAGONAL-RANK-UNSUPPORTED")
        return [[], [], dtype]
    offset = 0
    if diagonal_text != "":
        offset = int(diagonal_text)
    rows = shape[0]
    cols = shape[1]
    start_row = 0
    start_col = 0
    if offset >= 0:
        start_col = offset
    else:
        start_row = -offset
    length = rows - start_row
    col_length = cols - start_col
    if col_length < length:
        length = col_length
    if length < 0:
        length = 0
    out = []
    i = 0
    while i < length:
        out.append(values[(start_row + i) * cols + start_col + i])
        i += 1
    return [[length], out, dtype]


def _native_array_copy_values(values):
    out = []
    i = 0
    while i < len(values):
        out.append(values[i])
        i += 1
    return out


def _native_array_reshape(shape, values, dtype: str, reshape_text: str, diagnostics):
    target = _native_array_parse_shape(reshape_text)
    if _native_array_size(target) != _native_array_size(shape):
        diagnostics.append("PCC-ARRAY-RESHAPE-SIZE-MISMATCH")
        return [target, [], dtype]
    return [target, _native_array_copy_values(values), dtype]


def _native_array_ravel(shape, values, dtype: str, diagnostics):
    return [[_native_array_size(shape)], _native_array_copy_values(values), dtype]


def _native_array_transpose(shape, values, dtype: str, diagnostics):
    if len(shape) == 1:
        return [shape, _native_array_copy_values(values), dtype]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-TRANSPOSE-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    rows = shape[0]
    cols = shape[1]
    out = []
    c = 0
    while c < cols:
        r = 0
        while r < rows:
            out.append(values[r * cols + c])
            r += 1
        c += 1
    return [[cols, rows], out, dtype]


def _native_array_swapaxes(shape, values, dtype: str, axes_text: str, diagnostics):
    axes = _native_array_parse_shape(axes_text)
    if len(axes) != 2:
        diagnostics.append("PCC-ARRAY-SWAPAXES-AXES-INVALID")
        return [shape, [], dtype]
    axis0 = _native_array_axis_normalize(axes[0], len(shape))
    axis1 = _native_array_axis_normalize(axes[1], len(shape))
    if axis0 < 0 or axis0 >= len(shape) or axis1 < 0 or axis1 >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [shape, [], dtype]
    if len(shape) > 2:
        diagnostics.append("PCC-ARRAY-SWAPAXES-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    if axis0 == axis1 or len(shape) == 1:
        return [shape, _native_array_copy_values(values), dtype]
    return _native_array_transpose(shape, values, dtype, diagnostics)


def _native_array_moveaxis(shape, values, dtype: str, axes_text: str, diagnostics):
    axes = _native_array_parse_shape(axes_text)
    if len(axes) != 2:
        diagnostics.append("PCC-ARRAY-MOVEAXIS-AXES-INVALID")
        return [shape, [], dtype]
    source = _native_array_axis_normalize(axes[0], len(shape))
    destination = _native_array_axis_normalize(axes[1], len(shape))
    if (
        source < 0
        or source >= len(shape)
        or destination < 0
        or destination >= len(shape)
    ):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [shape, [], dtype]
    if len(shape) > 2:
        diagnostics.append("PCC-ARRAY-MOVEAXIS-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    if source == destination or len(shape) == 1:
        return [shape, _native_array_copy_values(values), dtype]
    return _native_array_transpose(shape, values, dtype, diagnostics)


def _native_array_rot90(shape, values, dtype: str, k_text: str, diagnostics):
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-ROT90-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    turns = int(k_text) % 4
    if turns == 0:
        return [shape, _native_array_copy_values(values), dtype]
    rows = shape[0]
    cols = shape[1]
    out = []
    if turns == 1:
        c = cols - 1
        while c >= 0:
            r = 0
            while r < rows:
                out.append(values[r * cols + c])
                r += 1
            c -= 1
        return [[cols, rows], out, dtype]
    if turns == 2:
        i = len(values) - 1
        while i >= 0:
            out.append(values[i])
            i -= 1
        return [shape, out, dtype]
    c = 0
    while c < cols:
        r = rows - 1
        while r >= 0:
            out.append(values[r * cols + c])
            r -= 1
        c += 1
    return [[cols, rows], out, dtype]


def _native_array_flip(shape, values, dtype: str, axis_text: str, diagnostics):
    if len(shape) == 0:
        return [shape, _native_array_copy_values(values), dtype]
    if len(shape) > 2:
        diagnostics.append("PCC-ARRAY-FLIP-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    axis = _native_array_axis_value(axis_text)
    flip_axis0 = False
    flip_axis1 = False
    if axis == -999999:
        flip_axis0 = True
        if len(shape) == 2:
            flip_axis1 = True
    else:
        normalized_axis = _native_array_axis_normalize(axis, len(shape))
        if normalized_axis < 0 or normalized_axis >= len(shape):
            diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
            return [shape, [], dtype]
        if normalized_axis == 0:
            flip_axis0 = True
        else:
            flip_axis1 = True
    out = []
    if len(shape) == 1:
        i = shape[0] - 1
        while i >= 0:
            out.append(values[i])
            i -= 1
        return [shape, out, dtype]
    rows = shape[0]
    cols = shape[1]
    r = rows - 1 if flip_axis0 else 0
    while r >= 0 if flip_axis0 else r < rows:
        c = cols - 1 if flip_axis1 else 0
        while c >= 0 if flip_axis1 else c < cols:
            out.append(values[r * cols + c])
            if flip_axis1:
                c -= 1
            else:
                c += 1
        if flip_axis0:
            r -= 1
        else:
            r += 1
    return [shape, out, dtype]


def _native_array_squeeze(shape, values, dtype: str, axis_text: str, diagnostics):
    out_shape = []
    if axis_text == "":
        i = 0
        while i < len(shape):
            if shape[i] != 1:
                out_shape.append(shape[i])
            i += 1
        return [out_shape, _native_array_copy_values(values), dtype]
    axis = int(axis_text)
    if axis < 0:
        axis += len(shape)
    if axis < 0 or axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if shape[axis] != 1:
        diagnostics.append("PCC-ARRAY-SQUEEZE-AXIS-NOT-ONE")
        return [shape, [], dtype]
    i = 0
    while i < len(shape):
        if i != axis:
            out_shape.append(shape[i])
        i += 1
    return [out_shape, _native_array_copy_values(values), dtype]


def _native_array_expand_dims(shape, values, dtype: str, axis_text: str, diagnostics):
    axis = int(axis_text)
    if axis < 0:
        axis += len(shape) + 1
    if axis < 0 or axis > len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    out_shape = []
    i = 0
    while i < axis:
        out_shape.append(shape[i])
        i += 1
    out_shape.append(1)
    while i < len(shape):
        out_shape.append(shape[i])
        i += 1
    return [out_shape, _native_array_copy_values(values), dtype]


def _native_array_sort_values(values):
    out = _native_array_copy_values(values)
    i = 1
    while i < len(out):
        current = out[i]
        j = i - 1
        while j >= 0 and compare_tokens(out[j], current, "gt"):
            out[j + 1] = out[j]
            j -= 1
        out[j + 1] = current
        i += 1
    return out


def _native_array_argsort_values(values):
    indices = []
    i = 0
    while i < len(values):
        indices.append(i)
        i += 1
    i = 1
    while i < len(indices):
        current_index = indices[i]
        j = i - 1
        while (
            j >= 0
            and compare_tokens(values[indices[j]], values[current_index], "gt")
        ):
            indices[j + 1] = indices[j]
            j -= 1
        indices[j + 1] = current_index
        i += 1
    out = []
    i = 0
    while i < len(indices):
        out.append(str(indices[i]))
        i += 1
    return out


def _native_array_sort(shape, values, dtype: str, axis_text: str, diagnostics):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-SORT-UNSUPPORTED")
        return [shape, [], dtype]
    if len(shape) == 0:
        return [shape, _native_array_copy_values(values), dtype]
    axis = -1
    if axis_text != "":
        axis = int(axis_text)
    if axis < 0:
        axis += len(shape)
    if axis < 0 or axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if len(shape) == 1:
        return [shape, _native_array_sort_values(values), dtype]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-SORT-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    rows = shape[0]
    cols = shape[1]
    out = _native_array_copy_values(values)
    if axis == 0:
        c = 0
        while c < cols:
            col_values = []
            r = 0
            while r < rows:
                col_values.append(values[r * cols + c])
                r += 1
            col_values = _native_array_sort_values(col_values)
            r = 0
            while r < rows:
                out[r * cols + c] = col_values[r]
                r += 1
            c += 1
    else:
        r = 0
        while r < rows:
            row_values = []
            c = 0
            while c < cols:
                row_values.append(values[r * cols + c])
                c += 1
            row_values = _native_array_sort_values(row_values)
            c = 0
            while c < cols:
                out[r * cols + c] = row_values[c]
                c += 1
            r += 1
    return [shape, out, dtype]


def _native_array_argsort(shape, values, dtype: str, axis_text: str, diagnostics):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-ARGSORT-UNSUPPORTED")
        return [shape, [], "int64"]
    if len(shape) == 0:
        return [shape, ["0"], "int64"]
    axis = -1
    if axis_text != "":
        axis = int(axis_text)
    if axis < 0:
        axis += len(shape)
    if axis < 0 or axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], "int64"]
    if len(shape) == 1:
        return [shape, _native_array_argsort_values(values), "int64"]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-ARGSORT-RANK-UNSUPPORTED")
        return [shape, [], "int64"]
    rows = shape[0]
    cols = shape[1]
    out = []
    i = 0
    while i < len(values):
        out.append("0")
        i += 1
    if axis == 0:
        c = 0
        while c < cols:
            col_values = []
            r = 0
            while r < rows:
                col_values.append(values[r * cols + c])
                r += 1
            col_indices = _native_array_argsort_values(col_values)
            r = 0
            while r < rows:
                out[r * cols + c] = col_indices[r]
                r += 1
            c += 1
    else:
        r = 0
        while r < rows:
            row_values = []
            c = 0
            while c < cols:
                row_values.append(values[r * cols + c])
                c += 1
            row_indices = _native_array_argsort_values(row_values)
            c = 0
            while c < cols:
                out[r * cols + c] = row_indices[c]
                c += 1
            r += 1
    return [shape, out, "int64"]


def _native_array_searchsorted(
    shape, values, dtype: str, query_text: str, side: str, diagnostics
):
    if side != "left" and side != "right":
        diagnostics.append("PCC-ARRAY-SEARCHSORTED-SIDE-UNSUPPORTED")
        return [[], [], "int64"]
    query_parsed = _native_array_literal_shape_and_diagnostics(query_text)
    query_shape = query_parsed[0]
    query_diagnostics = query_parsed[1]
    i = 0
    while i < len(query_diagnostics):
        diagnostics.append(query_diagnostics[i])
        i += 1
    query_dtype = _native_array_literal_dtype(query_text)
    if dtype == "object" or query_dtype == "object":
        diagnostics.append("PCC-ARRAY-SEARCHSORTED-UNSUPPORTED")
        return [query_shape, [], "int64"]
    if len(shape) != 1:
        diagnostics.append("PCC-ARRAY-SEARCHSORTED-RANK-UNSUPPORTED")
        return [query_shape, [], "int64"]
    query_values = _native_array_literal_values(query_text)
    out = []
    q = 0
    while q < len(query_values):
        pos = 0
        while pos < len(values):
            if side == "left":
                if compare_tokens(values[pos], query_values[q], "ge"):
                    break
            elif compare_tokens(values[pos], query_values[q], "gt"):
                break
            pos += 1
        out.append(str(pos))
        q += 1
    return [query_shape, out, "int64"]


def _native_array_normalize_kth(kth_text: str, axis_len: int):
    kth = int(kth_text)
    if kth < 0:
        kth += axis_len
    return kth


def _native_array_partition(
    shape, values, dtype: str, kth_text: str, axis_text: str, diagnostics
):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-PARTITION-UNSUPPORTED")
        return [shape, [], dtype]
    if len(shape) == 0:
        kth = _native_array_normalize_kth(kth_text, 1)
        if kth != 0:
            diagnostics.append("PCC-ARRAY-PARTITION-KTH-OUT-OF-BOUNDS")
            return [shape, [], dtype]
        return [shape, _native_array_copy_values(values), dtype]
    axis = -1
    if axis_text != "":
        axis = int(axis_text)
    if axis < 0:
        axis += len(shape)
    if axis < 0 or axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    kth = _native_array_normalize_kth(kth_text, shape[axis])
    if kth < 0 or kth >= shape[axis]:
        diagnostics.append("PCC-ARRAY-PARTITION-KTH-OUT-OF-BOUNDS")
        return [shape, [], dtype]
    if len(shape) == 1 or len(shape) == 2:
        return _native_array_sort(shape, values, dtype, axis_text, diagnostics)
    diagnostics.append("PCC-ARRAY-PARTITION-RANK-UNSUPPORTED")
    return [shape, [], dtype]


def _native_array_argpartition(
    shape, values, dtype: str, kth_text: str, axis_text: str, diagnostics
):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-ARGPARTITION-UNSUPPORTED")
        return [shape, [], "int64"]
    if len(shape) == 0:
        kth = _native_array_normalize_kth(kth_text, 1)
        if kth != 0:
            diagnostics.append("PCC-ARRAY-ARGPARTITION-KTH-OUT-OF-BOUNDS")
            return [shape, [], "int64"]
        return [shape, ["0"], "int64"]
    axis = -1
    if axis_text != "":
        axis = int(axis_text)
    if axis < 0:
        axis += len(shape)
    if axis < 0 or axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], "int64"]
    kth = _native_array_normalize_kth(kth_text, shape[axis])
    if kth < 0 or kth >= shape[axis]:
        diagnostics.append("PCC-ARRAY-ARGPARTITION-KTH-OUT-OF-BOUNDS")
        return [shape, [], "int64"]
    if len(shape) == 1 or len(shape) == 2:
        return _native_array_argsort(shape, values, dtype, axis_text, diagnostics)
    diagnostics.append("PCC-ARRAY-ARGPARTITION-RANK-UNSUPPORTED")
    return [shape, [], "int64"]


def _native_array_full(shape, fill_text: str, dtype: str, diagnostics):
    values = []
    token = _native_array_cast_values([fill_text], dtype)[0]
    size = _native_array_size(shape)
    i = 0
    while i < size:
        values.append(token)
        i += 1
    return [shape, values, dtype]


def _native_array_arange(arange_text: str, dtype: str, diagnostics):
    parts = _native_array_split_commas(arange_text)
    if len(parts) == 1:
        start = "0"
        stop = parts[0]
        step = "1"
    elif len(parts) == 2:
        start = parts[0]
        stop = parts[1]
        step = "1"
    elif len(parts) == 3:
        start = parts[0]
        stop = parts[1]
        step = parts[2]
    else:
        diagnostics.append("PCC-ARRAY-ARANGE-PARSE-FAILED")
        return [[], [], dtype]
    if not (_native_array_token_is_number(start) and _native_array_token_is_number(stop)
            and _native_array_token_is_number(step)):
        diagnostics.append("PCC-ARRAY-ARANGE-PARSE-FAILED")
        return [[], [], dtype]
    if _native_array_arange_uses_float(arange_text):
        start = float_token(token_float(start))
        stop = float_token(token_float(stop))
        step = float_token(token_float(step))
    if not token_truth(step):
        diagnostics.append("PCC-ARRAY-ARANGE-PARSE-FAILED")
        return [[], [], dtype]
    values = []
    current = start
    comparison = "lt" if compare_tokens(step, "0", "gt") else "gt"
    while compare_tokens(current, stop, comparison):
        values.append(cast_token(current, dtype))
        next_value = binary_token(current, step, "add", "object")
        if compare_tokens(next_value, current, "eq"):
            diagnostics.append("PCC-ARRAY-ARANGE-PARSE-FAILED")
            return [[], [], dtype]
        current = next_value
    return [[len(values)], values, dtype]


def _native_array_eye(eye_text: str, dtype: str, diagnostics):
    parts = _native_array_parse_shape(eye_text)
    if len(parts) == 1:
        rows = parts[0]
        cols = parts[0]
        diagonal = 0
    elif len(parts) == 2:
        rows = parts[0]
        cols = parts[1]
        diagonal = 0
    elif len(parts) == 3:
        rows = parts[0]
        cols = parts[1]
        diagonal = parts[2]
    else:
        diagnostics.append("PCC-ARRAY-EYE-PARSE-FAILED")
        return [[], [], dtype]
    shape = [rows, cols]
    values = []
    r = 0
    while r < rows:
        c = 0
        while c < cols:
            if c - r == diagonal:
                values.append(cast_token("1", dtype))
            else:
                values.append(cast_token("0", dtype))
            c += 1
        r += 1
    return [shape, values, dtype]


def _native_array_linspace(linspace_text: str, dtype: str, diagnostics):
    parts = _native_array_split_commas(linspace_text)
    if len(parts) == 2:
        count = 50
    elif len(parts) == 3:
        count = int(parts[2])
    else:
        diagnostics.append("PCC-ARRAY-LINSPACE-PARSE-FAILED")
        return [[], [], dtype]
    if count < 0:
        diagnostics.append("PCC-ARRAY-LINSPACE-PARSE-FAILED")
        return [[], [], dtype]
    start = token_float(parts[0])
    stop = token_float(parts[1])
    values = []
    if count == 0:
        return [[0], values, dtype]
    if count == 1:
        values.append(cast_token(float_token(start), dtype))
        return [[1], values, dtype]
    step = (stop - start) / (count - 1)
    i = 0
    while i < count:
        values.append(cast_token(float_token(start + step * i), dtype))
        i += 1
    return [[count], values, dtype]


def _native_array_axis_value(axis_text: str) -> int:
    if axis_text == "":
        return -999999
    return int(axis_text)


def _native_array_axis_normalize(axis: int, ndim: int) -> int:
    if axis < 0:
        return axis + ndim
    return axis


def _native_array_reduce_token(values, kind: str, dtype: str) -> str:
    return reduce_tokens(values, kind, dtype)


def _native_array_reduce(
    shape, values, dtype: str, kind: str, axis_text: str, keepdims: bool, diagnostics
):
    if not (
        kind == "sum"
        or kind == "prod"
        or kind == "min"
        or kind == "max"
        or kind == "mean"
        or kind == "any"
        or kind == "all"
    ):
        diagnostics.append("PCC-ARRAY-REDUCE-UNSUPPORTED")
        return [[], [], dtype]
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-REDUCE-UNSUPPORTED")
        return [[], [], dtype]
    if len(values) == 0:
        diagnostics.append("PCC-ARRAY-REDUCE-EMPTY")
        return [[], [], dtype]
    if kind == "any" or kind == "all":
        dtype = "bool"
    elif kind == "mean":
        dtype = "float64"
    elif (
        (kind == "sum" or kind == "prod") and dtype != "float32" and dtype != "float64"
    ):
        dtype = "int64"
    axis = _native_array_axis_value(axis_text)
    if axis == -999999:
        result = _native_array_reduce_token(values, kind, dtype)
        out_shape = []
        if keepdims:
            i = 0
            while i < len(shape):
                out_shape.append(1)
                i += 1
        return [out_shape, [result], dtype]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if len(shape) == 1:
        result = _native_array_reduce_token(values, kind, dtype)
        if keepdims:
            return [[1], [result], dtype]
        return [[], [result], dtype]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-AXIS-REDUCE-RANK-UNSUPPORTED")
        return [[], [], dtype]
    rows = shape[0]
    cols = shape[1]
    out = []
    if normalized_axis == 0:
        c = 0
        while c < cols:
            slice_values = []
            r = 0
            while r < rows:
                slice_values.append(values[r * cols + c])
                r += 1
            out.append(
                _native_array_reduce_token(slice_values, kind, dtype)
            )
            c += 1
        if keepdims:
            return [[1, cols], out, dtype]
        return [[cols], out, dtype]
    r = 0
    while r < rows:
        slice_values = []
        c = 0
        while c < cols:
            slice_values.append(values[r * cols + c])
            c += 1
        out.append(
            _native_array_reduce_token(slice_values, kind, dtype)
        )
        r += 1
    if keepdims:
        return [[rows, 1], out, dtype]
    return [[rows], out, dtype]


def _native_array_arg_reduce_token(values, kind: str) -> int:
    best_index = 0
    i = 1
    while i < len(values):
        op = "lt" if kind == "argmin" else "gt"
        if compare_tokens(values[i], values[best_index], op):
            best_index = i
        i += 1
    return best_index


def _native_array_arg_reduce(
    shape, values, dtype: str, kind: str, axis_text: str, keepdims: bool, diagnostics
):
    if not (kind == "argmin" or kind == "argmax"):
        diagnostics.append("PCC-ARRAY-ARGREDUCE-UNSUPPORTED")
        return [[], [], "int64"]
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-ARGREDUCE-UNSUPPORTED")
        return [[], [], "int64"]
    if len(values) == 0:
        diagnostics.append("PCC-ARRAY-ARGREDUCE-EMPTY")
        return [[], [], "int64"]
    axis = _native_array_axis_value(axis_text)
    if axis == -999999:
        result = _native_array_arg_reduce_token(values, kind)
        out_shape = []
        if keepdims:
            i = 0
            while i < len(shape):
                out_shape.append(1)
                i += 1
        return [out_shape, [str(result)], "int64"]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], "int64"]
    if len(shape) == 1:
        result = _native_array_arg_reduce_token(values, kind)
        if keepdims:
            return [[1], [str(result)], "int64"]
        return [[], [str(result)], "int64"]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-AXIS-ARGREDUCE-RANK-UNSUPPORTED")
        return [[], [], "int64"]
    rows = shape[0]
    cols = shape[1]
    out = []
    if normalized_axis == 0:
        c = 0
        while c < cols:
            slice_values = []
            r = 0
            while r < rows:
                slice_values.append(values[r * cols + c])
                r += 1
            out.append(str(_native_array_arg_reduce_token(slice_values, kind)))
            c += 1
        if keepdims:
            return [[1, cols], out, "int64"]
        return [[cols], out, "int64"]
    r = 0
    while r < rows:
        slice_values = []
        c = 0
        while c < cols:
            slice_values.append(values[r * cols + c])
            c += 1
        out.append(str(_native_array_arg_reduce_token(slice_values, kind)))
        r += 1
    if keepdims:
        return [[rows, 1], out, "int64"]
    return [[rows], out, "int64"]


def _native_array_count_nonzero_values(values) -> int:
    count = 0
    i = 0
    while i < len(values):
        if token_truth(values[i]):
            count += 1
        i += 1
    return count


def _native_array_count_nonzero(
    shape, values, dtype: str, axis_text: str, keepdims: bool, diagnostics
):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-COUNT-NONZERO-UNSUPPORTED")
        return [[], [], "int64"]
    axis = _native_array_axis_value(axis_text)
    if axis == -999999:
        result = _native_array_count_nonzero_values(values)
        out_shape = []
        if keepdims:
            i = 0
            while i < len(shape):
                out_shape.append(1)
                i += 1
        return [out_shape, [str(result)], "int64"]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], "int64"]
    if len(shape) == 1:
        result = _native_array_count_nonzero_values(values)
        if keepdims:
            return [[1], [str(result)], "int64"]
        return [[], [str(result)], "int64"]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-AXIS-COUNT-NONZERO-RANK-UNSUPPORTED")
        return [[], [], "int64"]
    rows = shape[0]
    cols = shape[1]
    out = []
    if normalized_axis == 0:
        c = 0
        while c < cols:
            slice_values = []
            r = 0
            while r < rows:
                slice_values.append(values[r * cols + c])
                r += 1
            out.append(str(_native_array_count_nonzero_values(slice_values)))
            c += 1
        if keepdims:
            return [[1, cols], out, "int64"]
        return [[cols], out, "int64"]
    r = 0
    while r < rows:
        slice_values = []
        c = 0
        while c < cols:
            slice_values.append(values[r * cols + c])
            c += 1
        out.append(str(_native_array_count_nonzero_values(slice_values)))
        r += 1
    if keepdims:
        return [[rows, 1], out, "int64"]
    return [[rows], out, "int64"]


def _native_array_nonzero(shape, values, dtype: str, diagnostics):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-NONZERO-UNSUPPORTED")
        return [[len(shape), 0], [], "int64"]
    if len(shape) == 0:
        if len(values) > 0 and token_truth(values[0]):
            return [[1, 1], ["0"], "int64"]
        return [[1, 0], [], "int64"]
    if len(shape) == 1:
        out = []
        i = 0
        while i < len(values):
            if token_truth(values[i]):
                out.append(str(i))
            i += 1
        return [[1, len(out)], out, "int64"]
    if len(shape) == 2:
        rows = shape[0]
        cols = shape[1]
        row_indices = []
        col_indices = []
        r = 0
        while r < rows:
            c = 0
            while c < cols:
                if token_truth(values[r * cols + c]):
                    row_indices.append(str(r))
                    col_indices.append(str(c))
                c += 1
            r += 1
        out = []
        i = 0
        while i < len(row_indices):
            out.append(row_indices[i])
            i += 1
        i = 0
        while i < len(col_indices):
            out.append(col_indices[i])
            i += 1
        return [[2, len(row_indices)], out, "int64"]
    diagnostics.append("PCC-ARRAY-NONZERO-RANK-UNSUPPORTED")
    return [[len(shape), 0], [], "int64"]


def _native_array_argwhere(shape, values, dtype: str, diagnostics):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-ARGWHERE-UNSUPPORTED")
        return [[0, len(shape)], [], "int64"]
    if len(shape) == 0:
        if len(values) > 0 and token_truth(values[0]):
            return [[1, 0], [], "int64"]
        return [[0, 0], [], "int64"]
    if len(shape) == 1:
        out = []
        i = 0
        while i < len(values):
            if token_truth(values[i]):
                out.append(str(i))
            i += 1
        return [[len(out), 1], out, "int64"]
    if len(shape) == 2:
        rows = shape[0]
        cols = shape[1]
        out = []
        count = 0
        r = 0
        while r < rows:
            c = 0
            while c < cols:
                if token_truth(values[r * cols + c]):
                    out.append(str(r))
                    out.append(str(c))
                    count += 1
                c += 1
            r += 1
        return [[count, 2], out, "int64"]
    diagnostics.append("PCC-ARRAY-ARGWHERE-RANK-UNSUPPORTED")
    return [[0, len(shape)], [], "int64"]


def _native_array_flatnonzero(shape, values, dtype: str, diagnostics):
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-FLATNONZERO-UNSUPPORTED")
        return [[0], [], "int64"]
    out = []
    i = 0
    while i < len(values):
        if token_truth(values[i]):
            out.append(str(i))
        i += 1
    return [[len(out)], out, "int64"]


def _native_array_cumulative_dtype(dtype: str) -> str:
    if dtype == "float32" or dtype == "float64":
        return dtype
    return "int64"


def _native_array_cumulative_values(values, kind: str, dtype: str):
    out = []
    total = "0" if kind == "cumsum" else "1"
    op = "add" if kind == "cumsum" else "mul"
    i = 0
    while i < len(values):
        total = binary_token(total, values[i], op, dtype)
        out.append(total)
        i += 1
    return out


def _native_array_cumulative(
    shape, values, dtype: str, kind: str, axis_text: str, diagnostics
):
    if not (kind == "cumsum" or kind == "cumprod"):
        diagnostics.append("PCC-ARRAY-CUMULATIVE-UNSUPPORTED")
        return [[], [], dtype]
    if dtype == "object":
        diagnostics.append("PCC-ARRAY-CUMULATIVE-UNSUPPORTED")
        return [[], [], dtype]
    dtype = _native_array_cumulative_dtype(dtype)
    axis = _native_array_axis_value(axis_text)
    if axis == -999999:
        out = _native_array_cumulative_values(values, kind, dtype)
        return [[len(out)], out, dtype]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if len(shape) == 1:
        return [shape, _native_array_cumulative_values(values, kind, dtype), dtype]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-AXIS-CUMULATIVE-RANK-UNSUPPORTED")
        return [[], [], dtype]
    rows = shape[0]
    cols = shape[1]
    out = []
    i = 0
    while i < len(values):
        out.append("0")
        i += 1
    if normalized_axis == 0:
        c = 0
        while c < cols:
            slice_values = []
            r = 0
            while r < rows:
                slice_values.append(values[r * cols + c])
                r += 1
            slice_out = _native_array_cumulative_values(slice_values, kind, dtype)
            r = 0
            while r < rows:
                out[r * cols + c] = slice_out[r]
                r += 1
            c += 1
    else:
        r = 0
        while r < rows:
            slice_values = []
            c = 0
            while c < cols:
                slice_values.append(values[r * cols + c])
                c += 1
            slice_out = _native_array_cumulative_values(slice_values, kind, dtype)
            c = 0
            while c < cols:
                out[r * cols + c] = slice_out[c]
                c += 1
            r += 1
    return [shape, out, dtype]


def _native_array_take(
    shape, values, dtype: str, take_text: str, axis_text: str, diagnostics
):
    indices = _native_array_parse_shape(take_text)
    axis = _native_array_axis_value(axis_text)
    out = []
    if axis == -999999:
        i = 0
        while i < len(indices):
            actual = indices[i]
            if actual < 0:
                actual += _native_array_size(shape)
            if actual < 0 or actual >= _native_array_size(shape):
                diagnostics.append("PCC-ARRAY-INDEX-OUT-OF-BOUNDS")
                return [[len(indices)], [], dtype]
            out.append(values[actual])
            i += 1
        return [[len(indices)], out, dtype]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if len(shape) == 1:
        return _native_array_take(shape, values, dtype, take_text, "", diagnostics)
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-TAKE-RANK-UNSUPPORTED")
        return [[], [], dtype]
    rows = shape[0]
    cols = shape[1]
    if normalized_axis == 0:
        i = 0
        while i < len(indices):
            actual = indices[i]
            if actual < 0:
                actual += rows
            if actual < 0 or actual >= rows:
                diagnostics.append("PCC-ARRAY-INDEX-OUT-OF-BOUNDS")
                return [[len(indices), cols], [], dtype]
            c = 0
            while c < cols:
                out.append(values[actual * cols + c])
                c += 1
            i += 1
        return [[len(indices), cols], out, dtype]
    r = 0
    while r < rows:
        i = 0
        while i < len(indices):
            actual = indices[i]
            if actual < 0:
                actual += cols
            if actual < 0 or actual >= cols:
                diagnostics.append("PCC-ARRAY-INDEX-OUT-OF-BOUNDS")
                return [[rows, len(indices)], [], dtype]
            out.append(values[r * cols + actual])
            i += 1
        r += 1
    return [[rows, len(indices)], out, dtype]


def _native_array_put(
    shape, values, dtype: str, put_text: str, put_values_text: str, diagnostics
):
    indices = _native_array_parse_shape(put_text)
    replacement_text = put_values_text
    if replacement_text == "":
        replacement_text = "0"
    replacement_values = _native_array_literal_values(replacement_text)
    if len(replacement_values) == 0:
        diagnostics.append("PCC-ARRAY-PUT-VALUES-EMPTY")
        return [shape, [], dtype]
    replacement_values = _native_array_cast_values(replacement_values, dtype)
    out = _native_array_copy_values(values)
    size = _native_array_size(shape)
    i = 0
    while i < len(indices):
        actual = indices[i]
        if actual < 0:
            actual += size
        if actual < 0 or actual >= size:
            diagnostics.append("PCC-ARRAY-INDEX-OUT-OF-BOUNDS")
            return [shape, [], dtype]
        out[actual] = replacement_values[i % len(replacement_values)]
        i += 1
    return [shape, out, dtype]


def _native_array_putmask(
    shape, values, dtype: str, mask_text: str, putmask_values_text: str, diagnostics
):
    parsed = _native_array_literal_shape_and_diagnostics(mask_text)
    mask_shape = parsed[0]
    mask_diagnostics = parsed[1]
    i = 0
    while i < len(mask_diagnostics):
        diagnostics.append(mask_diagnostics[i])
        i += 1
    mask_dtype = _native_array_literal_dtype(mask_text)
    if mask_dtype != "bool":
        diagnostics.append("PCC-ARRAY-PUTMASK-MASK-DTYPE-UNSUPPORTED")
        return [shape, [], dtype]
    if len(mask_shape) != len(shape):
        diagnostics.append("PCC-ARRAY-PUTMASK-SHAPE-MISMATCH")
        return [shape, [], dtype]
    i = 0
    while i < len(shape):
        if shape[i] != mask_shape[i]:
            diagnostics.append("PCC-ARRAY-PUTMASK-SHAPE-MISMATCH")
            return [shape, [], dtype]
        i += 1
    replacement_text = putmask_values_text
    if replacement_text == "":
        replacement_text = "0"
    replacement_values = _native_array_literal_values(replacement_text)
    if len(replacement_values) == 0:
        diagnostics.append("PCC-ARRAY-PUTMASK-VALUES-EMPTY")
        return [shape, [], dtype]
    replacement_values = _native_array_cast_values(replacement_values, dtype)
    mask_values = _native_array_literal_values(mask_text)
    out = _native_array_copy_values(values)
    selected = 0
    i = 0
    while i < len(out) and i < len(mask_values):
        if mask_values[i] == "True":
            out[i] = replacement_values[selected % len(replacement_values)]
            selected += 1
        i += 1
    return [shape, out, dtype]


def _native_array_mask(shape, values, dtype: str, mask_text: str, diagnostics):
    parsed = _native_array_literal_shape_and_diagnostics(mask_text)
    mask_shape = parsed[0]
    mask_diagnostics = parsed[1]
    i = 0
    while i < len(mask_diagnostics):
        diagnostics.append(mask_diagnostics[i])
        i += 1
    mask_dtype = _native_array_literal_dtype(mask_text)
    if mask_dtype != "bool":
        diagnostics.append("PCC-ARRAY-MASK-DTYPE-UNSUPPORTED")
        return [[], [], dtype]
    mask_values = _native_array_literal_values(mask_text)
    out = []
    if len(mask_shape) == len(shape):
        same = True
        i = 0
        while i < len(shape):
            if shape[i] != mask_shape[i]:
                same = False
            i += 1
        if same:
            i = 0
            while i < len(values) and i < len(mask_values):
                if mask_values[i] == "True":
                    out.append(values[i])
                i += 1
            return [[len(out)], out, dtype]
    if len(shape) == 2 and len(mask_shape) == 1 and mask_shape[0] == shape[0]:
        rows = shape[0]
        cols = shape[1]
        selected_rows = 0
        r = 0
        while r < rows:
            if mask_values[r] == "True":
                selected_rows += 1
                c = 0
                while c < cols:
                    out.append(values[r * cols + c])
                    c += 1
            r += 1
        return [[selected_rows, cols], out, dtype]
    diagnostics.append("PCC-ARRAY-MASK-SHAPE-MISMATCH")
    return [[], [], dtype]


def _native_array_compress(
    shape, values, dtype: str, condition_text: str, axis_text: str, diagnostics
):
    parsed = _native_array_literal_shape_and_diagnostics(condition_text)
    condition_shape = parsed[0]
    condition_diagnostics = parsed[1]
    i = 0
    while i < len(condition_diagnostics):
        diagnostics.append(condition_diagnostics[i])
        i += 1
    condition_dtype = _native_array_literal_dtype(condition_text)
    if condition_dtype != "bool":
        diagnostics.append("PCC-ARRAY-COMPRESS-MASK-DTYPE-UNSUPPORTED")
        return [[], [], dtype]
    if len(condition_shape) != 1:
        diagnostics.append("PCC-ARRAY-COMPRESS-SHAPE-MISMATCH")
        return [[], [], dtype]
    condition_values = _native_array_literal_values(condition_text)
    axis = _native_array_axis_value(axis_text)
    out = []
    if axis == -999999:
        if condition_shape[0] != len(values):
            diagnostics.append("PCC-ARRAY-COMPRESS-SHAPE-MISMATCH")
            return [[], [], dtype]
        i = 0
        while i < len(values):
            if condition_values[i] == "True":
                out.append(values[i])
            i += 1
        return [[len(out)], out, dtype]
    normalized_axis = _native_array_axis_normalize(axis, len(shape))
    if normalized_axis < 0 or normalized_axis >= len(shape):
        diagnostics.append("PCC-ARRAY-AXIS-OUT-OF-BOUNDS")
        return [[], [], dtype]
    if condition_shape[0] != shape[normalized_axis]:
        diagnostics.append("PCC-ARRAY-COMPRESS-SHAPE-MISMATCH")
        return [shape, [], dtype]
    if len(shape) == 1:
        i = 0
        while i < len(values):
            if condition_values[i] == "True":
                out.append(values[i])
            i += 1
        return [[len(out)], out, dtype]
    if len(shape) != 2:
        diagnostics.append("PCC-ARRAY-COMPRESS-RANK-UNSUPPORTED")
        return [shape, [], dtype]
    rows = shape[0]
    cols = shape[1]
    if normalized_axis == 0:
        selected_rows = 0
        r = 0
        while r < rows:
            if condition_values[r] == "True":
                selected_rows += 1
                c = 0
                while c < cols:
                    out.append(values[r * cols + c])
                    c += 1
            r += 1
        return [[selected_rows, cols], out, dtype]
    selected_cols = 0
    c = 0
    while c < cols:
        if condition_values[c] == "True":
            selected_cols += 1
        c += 1
    r = 0
    while r < rows:
        c = 0
        while c < cols:
            if condition_values[c] == "True":
                out.append(values[r * cols + c])
            c += 1
        r += 1
    return [[rows, selected_cols], out, dtype]


def _native_array_where(
    mask_text: str,
    true_shape,
    true_values,
    true_dtype: str,
    false_text: str,
    diagnostics,
):
    parsed = _native_array_literal_shape_and_diagnostics(mask_text)
    mask_shape = parsed[0]
    mask_diagnostics = parsed[1]
    i = 0
    while i < len(mask_diagnostics):
        diagnostics.append(mask_diagnostics[i])
        i += 1
    mask_dtype = _native_array_literal_dtype(mask_text)
    if mask_dtype != "bool":
        diagnostics.append("PCC-ARRAY-WHERE-MASK-DTYPE-UNSUPPORTED")
    mask_values = _native_array_literal_values(mask_text)
    false_shape = []
    if false_text.strip().startswith("["):
        parsed_false = _native_array_literal_shape_and_diagnostics(false_text)
        false_shape = parsed_false[0]
        false_diagnostics = parsed_false[1]
        i = 0
        while i < len(false_diagnostics):
            diagnostics.append(false_diagnostics[i])
            i += 1
    false_values = _native_array_literal_values(false_text)
    false_dtype = _native_array_literal_dtype(false_text)
    value_shape = _native_array_broadcast_shape(true_shape, false_shape, diagnostics)
    out_shape = _native_array_broadcast_shape(value_shape, mask_shape, diagnostics)
    dtype = _native_array_op_dtype("add", true_dtype, false_dtype)
    if true_dtype == "object" or false_dtype == "object":
        dtype = "object"
    if len(diagnostics) > 0:
        return [out_shape, [], dtype]
    out = []
    if len(out_shape) == 0:
        chosen = true_values[0] if mask_values[0] == "True" else false_values[0]
        out.append(chosen)
    elif len(out_shape) == 1:
        i = 0
        while i < out_shape[0]:
            out_index = [i]
            mv = mask_values[_native_array_broadcast_flat_index(mask_shape, out_index)]
            if mv == "True":
                out.append(
                    true_values[
                        _native_array_broadcast_flat_index(true_shape, out_index)
                    ]
                )
            else:
                out.append(
                    false_values[
                        _native_array_broadcast_flat_index(false_shape, out_index)
                    ]
                )
            i += 1
    elif len(out_shape) == 2:
        r = 0
        while r < out_shape[0]:
            c = 0
            while c < out_shape[1]:
                out_index = [r, c]
                mv = mask_values[
                    _native_array_broadcast_flat_index(mask_shape, out_index)
                ]
                if mv == "True":
                    out.append(
                        true_values[
                            _native_array_broadcast_flat_index(true_shape, out_index)
                        ]
                    )
                else:
                    out.append(
                        false_values[
                            _native_array_broadcast_flat_index(false_shape, out_index)
                        ]
                    )
                c += 1
            r += 1
    else:
        diagnostics.append("PCC-ARRAY-RANK-UNSUPPORTED")
        return [out_shape, [], dtype]
    return [out_shape, out, dtype]


def _native_array_astype(shape, values, dtype: str, target_dtype: str, diagnostics):
    target = _native_array_normalize_dtype(target_dtype)
    return [shape, _native_array_cast_values(values, target), target]


def _native_array_diagnostics_json(codes) -> str:
    out = "["
    i = 0
    while i < len(codes):
        if i > 0:
            out += ", "
        code = codes[i]
        out += "{"
        out += '"code": ' + _json_str(code)
        if code == "PCC-ARRAY-RAGGED":
            out += ', "message": ' + _json_str(
                "array literal is ragged; pcc cannot claim rectangular ndarray layout"
            )
        elif code == "PCC-ARRAY-EMPTY-DTYPE":
            out += ', "message": ' + _json_str(
                "empty array literal needs an explicit dtype for a precise layout"
            )
        elif code == "PCC-ARRAY-NEGATIVE-DIMENSION":
            out += ', "message": ' + _json_str(
                "array shape dimensions must be non-negative"
            )
        elif code == "PCC-ARRAY-ARANGE-PARSE-FAILED":
            out += ', "message": ' + _json_str(
                "arange expects stop, start,stop, or start,stop,step with a nonzero step"
            )
        elif code == "PCC-ARRAY-EYE-PARSE-FAILED":
            out += ', "message": ' + _json_str("eye expects n, n,m, or n,m,k")
        elif code == "PCC-ARRAY-LINSPACE-PARSE-FAILED":
            out += ', "message": ' + _json_str(
                "linspace expects start,stop or start,stop,num with non-negative num"
            )
        elif code == "PCC-ARRAY-REQUIRES-RECTANGULAR":
            out += ', "message": ' + _json_str(
                "rectangular layout was required but the input is ragged"
            )
        elif code == "PCC-ARRAY-BROADCAST-INCOMPATIBLE":
            out += ', "message": ' + _json_str(
                "array operands cannot be broadcast together"
            )
        elif code == "PCC-ARRAY-BROADCAST-TO-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "array cannot be broadcast to the requested shape"
            )
        elif code == "PCC-ARRAY-REPEAT-NEGATIVE":
            out += ', "message": ' + _json_str("repeat count must be non-negative")
        elif code == "PCC-ARRAY-REPEAT-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "repeat currently supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-TILE-REPS-EMPTY":
            out += ', "message": ' + _json_str(
                "tile requires at least one repeat dimension"
            )
        elif code == "PCC-ARRAY-TILE-REPS-NEGATIVE":
            out += ', "message": ' + _json_str(
                "tile repeat dimensions must be non-negative"
            )
        elif code == "PCC-ARRAY-TILE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "tile currently supports scalar/1D/2D arrays with one or two repeat dimensions"
            )
        elif code == "PCC-ARRAY-ROLL-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "roll currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-UFUNC-UNSUPPORTED":
            out += ', "message": ' + _json_str("unsupported array-core ufunc")
        elif code == "PCC-ARRAY-OBJECT-UFUNC-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array numeric ufuncs are not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-UNARY-UNSUPPORTED":
            out += ', "message": ' + _json_str("unsupported array-core unary ufunc")
        elif code == "PCC-ARRAY-OBJECT-UNARY-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array unary ufuncs are not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-UNARY-DTYPE-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "unary ufunc is not supported for this dtype by the current array-core subset"
            )
        elif code == "PCC-ARRAY-UNARY-FAILED":
            out += ', "message": ' + _json_str("array-core unary ufunc failed")
        elif code == "PCC-ARRAY-CLIP-PARSE-FAILED":
            out += ', "message": ' + _json_str("clip expects min,max scalar bounds")
        elif code == "PCC-ARRAY-OBJECT-CLIP-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array clip is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-CLIP-FAILED":
            out += ', "message": ' + _json_str("array-core clip failed")
        elif code == "PCC-ARRAY-MATMUL-OBJECT-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array matrix multiplication is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-MATMUL-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "array-core matmul supports 1D/2D operands only"
            )
        elif code == "PCC-ARRAY-MATMUL-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "array-core matmul operands have incompatible shapes"
            )
        elif code == "PCC-ARRAY-CONCAT-RANK-MISMATCH":
            out += ', "message": ' + _json_str(
                "concatenate operands must have the same rank"
            )
        elif code == "PCC-ARRAY-CONCAT-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "the current concatenate subset supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-CONCAT-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "concatenate operands have incompatible shapes"
            )
        elif code == "PCC-ARRAY-STACK-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "the current stack subset supports 1D arrays only"
            )
        elif code == "PCC-ARRAY-STACK-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "stack operands must have identical shapes"
            )
        elif code == "PCC-ARRAY-COMPARE-UNSUPPORTED":
            out += ', "message": ' + _json_str("unsupported array-core comparison")
        elif code == "PCC-ARRAY-COMPARE-FAILED":
            out += ', "message": ' + _json_str("array-core comparison failed")
        elif code == "PCC-ARRAY-OBJECT-COMPARE-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array ordered comparisons are not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-INDEX-PARSE-FAILED":
            out += ', "message": ' + _json_str("array index parse failed")
        elif code == "PCC-ARRAY-INDEX-RANK-MISMATCH":
            out += ', "message": ' + _json_str(
                "index rank must match array rank for the current array-core subset"
            )
        elif code == "PCC-ARRAY-INDEX-OUT-OF-BOUNDS":
            out += ', "message": ' + _json_str("array index is out of bounds")
        elif code == "PCC-ARRAY-DIAGONAL-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "diagonal currently supports 2D arrays only"
            )
        elif code == "PCC-ARRAY-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "array rank is unsupported by the native bootstrap array-core subset"
            )
        elif code == "PCC-ARRAY-RESHAPE-SIZE-MISMATCH":
            out += ', "message": ' + _json_str(
                "reshape target must have the same number of elements"
            )
        elif code == "PCC-ARRAY-TRANSPOSE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "the current array-core transpose subset supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-SWAPAXES-AXES-INVALID":
            out += ', "message": ' + _json_str("swapaxes expects exactly two axes")
        elif code == "PCC-ARRAY-SWAPAXES-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "swapaxes currently supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-MOVEAXIS-AXES-INVALID":
            out += ', "message": ' + _json_str(
                "moveaxis expects source,destination axes"
            )
        elif code == "PCC-ARRAY-MOVEAXIS-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "moveaxis currently supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-ROT90-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "rot90 currently supports 2D arrays only"
            )
        elif code == "PCC-ARRAY-FLIP-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "flip currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-SQUEEZE-AXIS-NOT-ONE":
            out += ', "message": ' + _json_str("squeeze axis must have length one")
        elif code == "PCC-ARRAY-REDUCE-UNSUPPORTED":
            out += ', "message": ' + _json_str("unsupported array-core reduction")
        elif code == "PCC-ARRAY-REDUCE-EMPTY":
            out += ', "message": ' + _json_str(
                "cannot reduce an empty array in the current array-core subset"
            )
        elif code == "PCC-ARRAY-AXIS-OUT-OF-BOUNDS":
            out += ', "message": ' + _json_str("axis is out of bounds for array")
        elif code == "PCC-ARRAY-AXIS-REDUCE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "axis reductions currently support 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-ARGREDUCE-UNSUPPORTED":
            out += ', "message": ' + _json_str("unsupported array-core arg reduction")
        elif code == "PCC-ARRAY-ARGREDUCE-EMPTY":
            out += ', "message": ' + _json_str(
                "cannot arg-reduce an empty array in the current array-core subset"
            )
        elif code == "PCC-ARRAY-AXIS-ARGREDUCE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "axis arg reductions currently support 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-COUNT-NONZERO-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array count_nonzero is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-AXIS-COUNT-NONZERO-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "axis count_nonzero currently supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-NONZERO-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array nonzero is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-NONZERO-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "nonzero currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-ARGWHERE-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array argwhere is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-ARGWHERE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "argwhere currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-FLATNONZERO-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array flatnonzero is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-CUMULATIVE-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "unsupported array-core cumulative operation"
            )
        elif code == "PCC-ARRAY-AXIS-CUMULATIVE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "axis cumulative operations currently support 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-TAKE-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "take currently supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-PUT-VALUES-EMPTY":
            out += ', "message": ' + _json_str(
                "put requires at least one replacement value"
            )
        elif code == "PCC-ARRAY-PUT-FAILED":
            out += ', "message": ' + _json_str("array-core put failed")
        elif code == "PCC-ARRAY-PUTMASK-MASK-DTYPE-UNSUPPORTED":
            out += ', "message": ' + _json_str("putmask requires a bool mask")
        elif code == "PCC-ARRAY-PUTMASK-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "putmask mask shape must match the array shape"
            )
        elif code == "PCC-ARRAY-PUTMASK-VALUES-EMPTY":
            out += ', "message": ' + _json_str(
                "putmask requires at least one replacement value"
            )
        elif code == "PCC-ARRAY-PUTMASK-FAILED":
            out += ', "message": ' + _json_str("array-core putmask failed")
        elif code == "PCC-ARRAY-MASK-DTYPE-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "boolean mask selection requires a bool mask"
            )
        elif code == "PCC-ARRAY-MASK-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "boolean mask shape must match the array shape or the leading axis for 2D arrays"
            )
        elif code == "PCC-ARRAY-COMPRESS-MASK-DTYPE-UNSUPPORTED":
            out += ', "message": ' + _json_str("compress requires a bool condition")
        elif code == "PCC-ARRAY-COMPRESS-SHAPE-MISMATCH":
            out += ', "message": ' + _json_str(
                "compress condition length must match the selected array extent"
            )
        elif code == "PCC-ARRAY-COMPRESS-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "compress currently supports 1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-WHERE-MASK-DTYPE-UNSUPPORTED":
            out += ', "message": ' + _json_str("where selection requires a bool mask")
        elif code == "PCC-ARRAY-SORT-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array sort is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-SORT-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "sort currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-ARGSORT-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array argsort is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-ARGSORT-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "argsort currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-SEARCHSORTED-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array searchsorted is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-SEARCHSORTED-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "searchsorted currently supports 1D sorted arrays only"
            )
        elif code == "PCC-ARRAY-SEARCHSORTED-SIDE-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "searchsorted side must be left or right"
            )
        elif code == "PCC-ARRAY-PARTITION-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array partition is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-PARTITION-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "partition currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-PARTITION-KTH-OUT-OF-BOUNDS":
            out += ', "message": ' + _json_str(
                "partition kth is out of bounds for the selected axis"
            )
        elif code == "PCC-ARRAY-ARGPARTITION-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "object-array argpartition is not supported by the current array-core subset"
            )
        elif code == "PCC-ARRAY-ARGPARTITION-RANK-UNSUPPORTED":
            out += ', "message": ' + _json_str(
                "argpartition currently supports scalar/1D/2D arrays only"
            )
        elif code == "PCC-ARRAY-ARGPARTITION-KTH-OUT-OF-BOUNDS":
            out += ', "message": ' + _json_str(
                "argpartition kth is out of bounds for the selected axis"
            )
        else:
            out += ', "message": ' + _json_str("array-core layout diagnostic")
        out += "}"
        i += 1
    out += "]"
    return out


def _native_array_core_json(
    shape,
    dtype: str,
    source: str,
    diagnostics,
    values,
    fill,
    arange: str,
    zeros: str,
    ones: str,
    zeros_like: bool,
    ones_like: bool,
    full_like,
    eye: str,
    linspace: str,
    op: str,
    rhs,
    matmul,
    concat,
    stack,
    unary: str,
    clip: str,
    broadcast_to: str,
    repeat,
    tile,
    roll: str,
    rot90: str,
    compare: str,
    index: str,
    diagonal: str,
    reshape: str,
    ravel: bool,
    flatten: bool,
    flip: bool,
    transpose: bool,
    swapaxes: str,
    moveaxis: str,
    squeeze: bool,
    squeeze_axis_text: str,
    expand_dims_text: str,
    reduce_name: str,
    argreduce_name: str,
    count_nonzero: bool,
    nonzero: bool,
    argwhere: bool,
    flatnonzero: bool,
    cumulative: str,
    sort_value: bool,
    argsort_value: bool,
    searchsorted: str,
    search_side: str,
    partition: str,
    argpartition: str,
    axis_text: str,
    keepdims: bool,
    take: str,
    put: str,
    put_values: str,
    putmask: str,
    putmask_values: str,
    mask: str,
    compress: str,
    where: str,
    otherwise,
    astype: str,
    copy_value: bool,
    view: bool,
    owns_data: bool,
    base_shape,
    strides_override,
    c_contiguous: bool,
) -> str:
    i = 0
    while i < len(shape):
        if shape[i] < 0 and not _native_list_contains(
            diagnostics, "PCC-ARRAY-NEGATIVE-DIMENSION"
        ):
            diagnostics.append("PCC-ARRAY-NEGATIVE-DIMENSION")
        i += 1
    itemsize = _native_array_dtype_itemsize(dtype)
    strides = _native_array_strides(shape, itemsize)
    if strides_override is not None:
        strides = strides_override
    size = _native_array_size(shape)
    out = "{"
    out += '"c_contiguous": ' + ("true" if c_contiguous else "false")
    out += ', "diagnostics": ' + _native_array_diagnostics_json(diagnostics)
    if values is not None:
        out += ', "data": ' + _native_array_data_json(shape, values)
    out += ', "dtype": ' + _json_str(dtype)
    out += ', "dtype_format": ' + _json_str(_native_array_dtype_format(dtype))
    if dtype == "bool" or _native_array_is_integer_dtype(dtype):
        out += ', "dtype_range": ' + _native_array_dtype_range_json(dtype)
        out += ', "dtype_signed": ' + (
            "true" if _native_array_integer_signed(dtype) else "false"
        )
    if values is not None:
        out += ', "flat_data": ' + _native_array_values_json(values)
    if fill is not None:
        out += ', "fill": ' + _json_str(fill)
    if arange != "":
        out += ', "arange": ' + _json_str(arange)
    if zeros != "":
        out += ', "zeros": ' + _json_str(zeros)
    if ones != "":
        out += ', "ones": ' + _json_str(ones)
    if zeros_like:
        out += ', "zeros_like": true'
    if ones_like:
        out += ', "ones_like": true'
    if full_like != "":
        out += ', "full_like": ' + _json_str(full_like)
    if eye != "":
        out += ', "eye": ' + _json_str(eye)
    if linspace != "":
        out += ', "linspace": ' + _json_str(linspace)
    out += ', "itemsize": ' + str(itemsize)
    out += ', "nbytes": ' + str(size * itemsize)
    out += ', "ndim": ' + str(len(shape))
    out += ', "ok": ' + ("true" if len(diagnostics) == 0 else "false")
    if op != "":
        out += ', "op": ' + _json_str(op)
    if matmul != "":
        out += ', "matmul": ' + _json_str(matmul)
    if concat != "":
        out += ', "concat": ' + _json_str(concat)
    if stack != "":
        out += ', "stack": ' + _json_str(stack)
    if unary != "":
        out += ', "unary": ' + _json_str(unary)
    if clip != "":
        out += ', "clip": ' + _json_str(clip)
    if broadcast_to != "":
        out += ', "broadcast_to": ' + _json_str(broadcast_to)
    if repeat != "":
        out += ', "repeat": ' + str(repeat)
    if tile != "":
        out += ', "tile": ' + _json_str(tile)
    if roll != "":
        out += ', "roll": ' + roll
    if rot90 != "":
        out += ', "rot90": ' + rot90
    if index != "":
        out += ', "index": ' + _json_str(index)
    if diagonal != "":
        out += ', "diagonal": ' + diagonal
    if compare != "":
        out += ', "compare": ' + _json_str(compare)
    out += ', "owns_data": ' + ("true" if owns_data else "false")
    if dtype == "object":
        out += ', "object_policy": {"allowed": ["storage", "index", "take", "put", "putmask", "compress", "roll", "flip", "transpose", "swapaxes", "moveaxis", "rot90", "reshape", "ravel", "flatten", "copy", "repr"], "unsupported": ["numeric_ufunc", "numeric_reduce", "typed_memoryview"]}'
    if values is not None:
        out += ', "repr": ' + _json_str(_native_array_repr(shape, values))
    if rhs != "":
        out += ', "rhs": ' + _json_str(rhs)
    if reshape != "":
        out += ', "reshape": ' + _json_str(reshape)
    if ravel:
        out += ', "ravel": true'
    if flatten:
        out += ', "flatten": true'
    if flip:
        out += ', "flip": true'
    if transpose:
        out += ', "transpose": true'
    if swapaxes != "":
        out += ', "swapaxes": ' + _json_str(swapaxes)
    if moveaxis != "":
        out += ', "moveaxis": ' + _json_str(moveaxis)
    if squeeze:
        out += ', "squeeze": true'
    if squeeze_axis_text != "":
        out += ', "squeeze_axis": ' + squeeze_axis_text
    if expand_dims_text != "":
        out += ', "expand_dims": ' + expand_dims_text
    if reduce_name != "":
        out += ', "reduce": ' + _json_str(reduce_name)
    if argreduce_name != "":
        out += ', "argreduce": ' + _json_str(argreduce_name)
    if count_nonzero:
        out += ', "count_nonzero": true'
    if nonzero:
        out += ', "nonzero": true'
    if argwhere:
        out += ', "argwhere": true'
    if flatnonzero:
        out += ', "flatnonzero": true'
    if cumulative != "":
        out += ', "cumulative": ' + _json_str(cumulative)
    if sort_value:
        out += ', "sort": true'
    if argsort_value:
        out += ', "argsort": true'
    if searchsorted != "":
        out += ', "searchsorted": ' + _json_str(searchsorted)
        out += ', "side": ' + _json_str(search_side)
    if partition != "":
        out += ', "partition": ' + partition
    if argpartition != "":
        out += ', "argpartition": ' + argpartition
    if axis_text != "":
        out += ', "axis": ' + axis_text
    if keepdims:
        out += ', "keepdims": true'
    if take != "":
        out += ', "take": ' + _json_str(take)
    if put != "":
        out += ', "put": ' + _json_str(put)
        out += ', "put_values": ' + _json_str(put_values)
    if putmask != "":
        out += ', "putmask": ' + _json_str(putmask)
        out += ', "putmask_values": ' + _json_str(putmask_values)
    if mask != "":
        out += ', "mask": ' + _json_str(mask)
    if compress != "":
        out += ', "compress": ' + _json_str(compress)
    if where != "":
        out += ', "where": ' + _json_str(where)
    if otherwise != "":
        out += ', "otherwise": ' + _json_str(otherwise)
    if astype != "":
        out += ', "astype": ' + _json_str(astype)
    if copy_value:
        out += ', "copy": true'
    out += ', "view": ' + ("true" if view else "false")
    if base_shape is not None:
        out += ', "base_shape": ' + _json_int_list(base_shape)
    out += ', "schema": "pcc.array-core.v1"'
    out += ', "shape": ' + _json_int_list(shape)
    out += ', "size": ' + str(size)
    out += ', "source": ' + _json_str(source)
    out += ', "strides": ' + _json_int_list(strides)
    out += "}"
    return out


def _run_native_package_array_core_impl(module_args) -> int:
    shape_text = ""
    literal = ""
    dtype = "auto"
    require_rectangular = False
    fill = None
    arange = ""
    zeros = ""
    ones = ""
    zeros_like = False
    ones_like = False
    full_like = ""
    eye = ""
    linspace = ""
    op = ""
    rhs = ""
    matmul = ""
    concat = ""
    stack = ""
    unary = ""
    clip = ""
    broadcast_to = ""
    repeat = ""
    tile = ""
    roll = ""
    rot90 = ""
    compare = ""
    index = ""
    diagonal = ""
    reshape = ""
    ravel = False
    flatten = False
    flip = False
    transpose = False
    swapaxes = ""
    moveaxis = ""
    squeeze = False
    squeeze_axis_text = ""
    expand_dims_text = ""
    reduce_name = ""
    argreduce_name = ""
    count_nonzero = False
    nonzero = False
    argwhere = False
    flatnonzero = False
    cumulative = ""
    sort_value = False
    argsort_value = False
    searchsorted = ""
    search_side = "left"
    partition = ""
    argpartition = ""
    axis_text = ""
    keepdims = False
    take = ""
    put = ""
    put_values = ""
    putmask = ""
    putmask_values = ""
    mask = ""
    compress = ""
    where = ""
    otherwise = ""
    astype = ""
    copy_value = False
    i = 0
    while i < len(module_args):
        arg = module_args[i]
        if arg == "--shape":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--shape requires a value", "ok": false}')
                return 2
            shape_text = module_args[i + 1]
            i += 1
        elif arg.startswith("--shape="):
            shape_text = arg.split("=", 1)[1]
        elif arg == "--literal":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--literal requires a value", "ok": false}')
                return 2
            literal = module_args[i + 1]
            i += 1
        elif arg.startswith("--literal="):
            literal = arg.split("=", 1)[1]
        elif arg == "--dtype":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--dtype requires a value", "ok": false}')
                return 2
            dtype = module_args[i + 1]
            i += 1
        elif arg.startswith("--dtype="):
            dtype = arg.split("=", 1)[1]
        elif arg == "--require-rectangular":
            require_rectangular = True
        elif arg == "--fill":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--fill requires a value", "ok": false}')
                return 2
            fill = module_args[i + 1]
            i += 1
        elif arg.startswith("--fill="):
            fill = arg.split("=", 1)[1]
        elif arg == "--arange":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--arange requires a value", "ok": false}')
                return 2
            arange = module_args[i + 1]
            i += 1
        elif arg.startswith("--arange="):
            arange = arg.split("=", 1)[1]
        elif arg == "--zeros":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--zeros requires a value", "ok": false}')
                return 2
            zeros = module_args[i + 1]
            i += 1
        elif arg.startswith("--zeros="):
            zeros = arg.split("=", 1)[1]
        elif arg == "--ones":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--ones requires a value", "ok": false}')
                return 2
            ones = module_args[i + 1]
            i += 1
        elif arg.startswith("--ones="):
            ones = arg.split("=", 1)[1]
        elif arg == "--zeros-like":
            zeros_like = True
        elif arg == "--ones-like":
            ones_like = True
        elif arg == "--full-like":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--full-like requires a value", "ok": false}')
                return 2
            full_like = module_args[i + 1]
            i += 1
        elif arg.startswith("--full-like="):
            full_like = arg.split("=", 1)[1]
        elif arg == "--eye":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--eye requires a value", "ok": false}')
                return 2
            eye = module_args[i + 1]
            i += 1
        elif arg.startswith("--eye="):
            eye = arg.split("=", 1)[1]
        elif arg == "--linspace":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--linspace requires a value", "ok": false}')
                return 2
            linspace = module_args[i + 1]
            i += 1
        elif arg.startswith("--linspace="):
            linspace = arg.split("=", 1)[1]
        elif arg == "--op":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--op requires a value", "ok": false}')
                return 2
            op = module_args[i + 1]
            i += 1
        elif arg.startswith("--op="):
            op = arg.split("=", 1)[1]
        elif arg == "--rhs":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--rhs requires a value", "ok": false}')
                return 2
            rhs = module_args[i + 1]
            i += 1
        elif arg.startswith("--rhs="):
            rhs = arg.split("=", 1)[1]
        elif arg == "--matmul":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--matmul requires a value", "ok": false}')
                return 2
            matmul = module_args[i + 1]
            i += 1
        elif arg.startswith("--matmul="):
            matmul = arg.split("=", 1)[1]
        elif arg == "--concat":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--concat requires a value", "ok": false}')
                return 2
            concat = module_args[i + 1]
            i += 1
        elif arg.startswith("--concat="):
            concat = arg.split("=", 1)[1]
        elif arg == "--stack":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--stack requires a value", "ok": false}')
                return 2
            stack = module_args[i + 1]
            i += 1
        elif arg.startswith("--stack="):
            stack = arg.split("=", 1)[1]
        elif arg == "--unary":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--unary requires a value", "ok": false}')
                return 2
            unary = module_args[i + 1]
            i += 1
        elif arg.startswith("--unary="):
            unary = arg.split("=", 1)[1]
        elif arg == "--clip":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--clip requires a value", "ok": false}')
                return 2
            clip = module_args[i + 1]
            i += 1
        elif arg.startswith("--clip="):
            clip = arg.split("=", 1)[1]
        elif arg == "--broadcast-to":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--broadcast-to requires a value", "ok": false}')
                return 2
            broadcast_to = module_args[i + 1]
            i += 1
        elif arg.startswith("--broadcast-to="):
            broadcast_to = arg.split("=", 1)[1]
        elif arg == "--repeat":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--repeat requires a value", "ok": false}')
                return 2
            repeat = module_args[i + 1]
            i += 1
        elif arg.startswith("--repeat="):
            repeat = arg.split("=", 1)[1]
        elif arg == "--tile":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--tile requires a value", "ok": false}')
                return 2
            tile = module_args[i + 1]
            i += 1
        elif arg.startswith("--tile="):
            tile = arg.split("=", 1)[1]
        elif arg == "--roll":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--roll requires a value", "ok": false}')
                return 2
            roll = module_args[i + 1]
            i += 1
        elif arg.startswith("--roll="):
            roll = arg.split("=", 1)[1]
        elif arg == "--rot90":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--rot90 requires a value", "ok": false}')
                return 2
            rot90 = module_args[i + 1]
            i += 1
        elif arg.startswith("--rot90="):
            rot90 = arg.split("=", 1)[1]
        elif arg == "--compare":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--compare requires a value", "ok": false}')
                return 2
            compare = module_args[i + 1]
            i += 1
        elif arg.startswith("--compare="):
            compare = arg.split("=", 1)[1]
        elif arg == "--index":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--index requires a value", "ok": false}')
                return 2
            index = module_args[i + 1]
            i += 1
        elif arg.startswith("--index="):
            index = arg.split("=", 1)[1]
        elif arg == "--diagonal":
            diagonal = "0"
            if i + 1 < len(module_args) and not module_args[i + 1].startswith("--"):
                diagonal = module_args[i + 1]
                i += 1
        elif arg.startswith("--diagonal="):
            diagonal = arg.split("=", 1)[1]
        elif arg == "--reshape":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--reshape requires a value", "ok": false}')
                return 2
            reshape = module_args[i + 1]
            i += 1
        elif arg.startswith("--reshape="):
            reshape = arg.split("=", 1)[1]
        elif arg == "--ravel":
            ravel = True
        elif arg == "--flatten":
            flatten = True
        elif arg == "--flip":
            flip = True
        elif arg == "--transpose":
            transpose = True
        elif arg == "--swapaxes":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--swapaxes requires a value", "ok": false}')
                return 2
            swapaxes = module_args[i + 1]
            i += 1
        elif arg.startswith("--swapaxes="):
            swapaxes = arg.split("=", 1)[1]
        elif arg == "--moveaxis":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--moveaxis requires a value", "ok": false}')
                return 2
            moveaxis = module_args[i + 1]
            i += 1
        elif arg.startswith("--moveaxis="):
            moveaxis = arg.split("=", 1)[1]
        elif arg == "--squeeze":
            squeeze = True
        elif arg == "--squeeze-axis":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--squeeze-axis requires a value", "ok": false}')
                return 2
            squeeze_axis_text = module_args[i + 1]
            i += 1
        elif arg.startswith("--squeeze-axis="):
            squeeze_axis_text = arg.split("=", 1)[1]
        elif arg == "--expand-dims":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--expand-dims requires a value", "ok": false}')
                return 2
            expand_dims_text = module_args[i + 1]
            i += 1
        elif arg.startswith("--expand-dims="):
            expand_dims_text = arg.split("=", 1)[1]
        elif arg == "--reduce":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--reduce requires a value", "ok": false}')
                return 2
            reduce_name = module_args[i + 1]
            i += 1
        elif arg.startswith("--reduce="):
            reduce_name = arg.split("=", 1)[1]
        elif arg == "--argreduce":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--argreduce requires a value", "ok": false}')
                return 2
            argreduce_name = module_args[i + 1]
            i += 1
        elif arg.startswith("--argreduce="):
            argreduce_name = arg.split("=", 1)[1]
        elif arg == "--count-nonzero":
            count_nonzero = True
        elif arg == "--nonzero":
            nonzero = True
        elif arg == "--argwhere":
            argwhere = True
        elif arg == "--flatnonzero":
            flatnonzero = True
        elif arg == "--cumulative":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--cumulative requires a value", "ok": false}')
                return 2
            cumulative = module_args[i + 1]
            i += 1
        elif arg.startswith("--cumulative="):
            cumulative = arg.split("=", 1)[1]
        elif arg == "--sort":
            sort_value = True
        elif arg == "--argsort":
            argsort_value = True
        elif arg == "--searchsorted":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--searchsorted requires a value", "ok": false}')
                return 2
            searchsorted = module_args[i + 1]
            i += 1
        elif arg.startswith("--searchsorted="):
            searchsorted = arg.split("=", 1)[1]
        elif arg == "--side":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--side requires a value", "ok": false}')
                return 2
            search_side = module_args[i + 1]
            i += 1
        elif arg.startswith("--side="):
            search_side = arg.split("=", 1)[1]
        elif arg == "--partition":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--partition requires a value", "ok": false}')
                return 2
            partition = module_args[i + 1]
            i += 1
        elif arg.startswith("--partition="):
            partition = arg.split("=", 1)[1]
        elif arg == "--argpartition":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--argpartition requires a value", "ok": false}')
                return 2
            argpartition = module_args[i + 1]
            i += 1
        elif arg.startswith("--argpartition="):
            argpartition = arg.split("=", 1)[1]
        elif arg == "--axis":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--axis requires a value", "ok": false}')
                return 2
            axis_text = module_args[i + 1]
            i += 1
        elif arg.startswith("--axis="):
            axis_text = arg.split("=", 1)[1]
        elif arg == "--keepdims":
            keepdims = True
        elif arg == "--take":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--take requires a value", "ok": false}')
                return 2
            take = module_args[i + 1]
            i += 1
        elif arg.startswith("--take="):
            take = arg.split("=", 1)[1]
        elif arg == "--put":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--put requires a value", "ok": false}')
                return 2
            put = module_args[i + 1]
            i += 1
        elif arg.startswith("--put="):
            put = arg.split("=", 1)[1]
        elif arg == "--put-values":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--put-values requires a value", "ok": false}')
                return 2
            put_values = module_args[i + 1]
            i += 1
        elif arg.startswith("--put-values="):
            put_values = arg.split("=", 1)[1]
        elif arg == "--putmask":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--putmask requires a value", "ok": false}')
                return 2
            putmask = module_args[i + 1]
            i += 1
        elif arg.startswith("--putmask="):
            putmask = arg.split("=", 1)[1]
        elif arg == "--putmask-values":
            if i + 1 >= len(module_args):
                _write_text(
                    '{"error": "--putmask-values requires a value", "ok": false}'
                )
                return 2
            putmask_values = module_args[i + 1]
            i += 1
        elif arg.startswith("--putmask-values="):
            putmask_values = arg.split("=", 1)[1]
        elif arg == "--mask":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--mask requires a value", "ok": false}')
                return 2
            mask = module_args[i + 1]
            i += 1
        elif arg.startswith("--mask="):
            mask = arg.split("=", 1)[1]
        elif arg == "--compress":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--compress requires a value", "ok": false}')
                return 2
            compress = module_args[i + 1]
            i += 1
        elif arg.startswith("--compress="):
            compress = arg.split("=", 1)[1]
        elif arg == "--where":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--where requires a value", "ok": false}')
                return 2
            where = module_args[i + 1]
            i += 1
        elif arg.startswith("--where="):
            where = arg.split("=", 1)[1]
        elif arg == "--otherwise":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--otherwise requires a value", "ok": false}')
                return 2
            otherwise = module_args[i + 1]
            i += 1
        elif arg.startswith("--otherwise="):
            otherwise = arg.split("=", 1)[1]
        elif arg == "--astype":
            if i + 1 >= len(module_args):
                _write_text('{"error": "--astype requires a value", "ok": false}')
                return 2
            astype = module_args[i + 1]
            i += 1
        elif arg.startswith("--astype="):
            astype = arg.split("=", 1)[1]
        elif arg == "--copy":
            copy_value = True
        elif arg == "--json":
            pass
        i += 1
    diagnostics = []
    source = "shape"
    values = None
    view = False
    owns_data = True
    base_shape = None
    strides_override = None
    c_contiguous = True
    if linspace != "":
        source = "linspace"
        if dtype == "auto" or dtype == "":
            dtype = "float64"
        dtype = _native_array_normalize_dtype(dtype)
        result = _native_array_linspace(linspace, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    elif eye != "":
        source = "eye"
        if dtype == "auto" or dtype == "":
            dtype = "float64"
        dtype = _native_array_normalize_dtype(dtype)
        result = _native_array_eye(eye, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    elif zeros != "":
        source = "zeros"
        if dtype == "auto" or dtype == "":
            dtype = "float64"
        dtype = _native_array_normalize_dtype(dtype)
        shape = _native_array_parse_shape(zeros)
        result = _native_array_full(shape, "0", dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    elif ones != "":
        source = "ones"
        if dtype == "auto" or dtype == "":
            dtype = "float64"
        dtype = _native_array_normalize_dtype(dtype)
        shape = _native_array_parse_shape(ones)
        result = _native_array_full(shape, "1", dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    elif arange != "":
        source = "arange"
        if dtype == "auto" or dtype == "":
            if _native_array_arange_uses_float(arange):
                dtype = "float64"
            else:
                dtype = "int64"
        dtype = _native_array_normalize_dtype(dtype)
        result = _native_array_arange(arange, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    elif literal != "":
        parsed = _native_array_literal_shape_and_diagnostics(literal)
        shape = parsed[0]
        diagnostics = parsed[1]
        values = _native_array_literal_values(literal)
        source = "literal"
        if dtype == "auto" or dtype == "":
            if _native_list_contains(diagnostics, "PCC-ARRAY-RAGGED"):
                dtype = "object"
            else:
                dtype = _native_array_literal_dtype(literal)
    else:
        shape = _native_array_parse_shape(shape_text)
    if require_rectangular and _native_list_contains(diagnostics, "PCC-ARRAY-RAGGED"):
        diagnostics.append("PCC-ARRAY-REQUIRES-RECTANGULAR")
    fill_dtype_auto = dtype == "auto" or dtype == ""
    dtype = _native_array_normalize_dtype(dtype)
    if literal == "" and arange == "" and fill is not None:
        if fill_dtype_auto:
            dtype = _native_array_literal_dtype(fill)
        dtype = _native_array_normalize_dtype(dtype)
        result = _native_array_full(shape, fill, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and values is not None:
        values = _native_array_cast_values(values, dtype)
    if literal != "" and zeros_like:
        result = _native_array_full(shape, "0", dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and ones_like:
        result = _native_array_full(shape, "1", dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and full_like != "":
        result = _native_array_full(shape, full_like, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and unary != "":
        result = _native_array_unary_op(shape, values, dtype, unary, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and clip != "":
        result = _native_array_clip(shape, values, dtype, clip, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and broadcast_to != "":
        base_shape = shape
        result = _native_array_broadcast_to(
            shape, values, dtype, broadcast_to, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        strides_override = result[3]
        c_contiguous = result[4]
        view = True
        owns_data = False
    if literal != "" and repeat != "":
        result = _native_array_repeat(
            shape, values, dtype, repeat, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and tile != "":
        tile_reps = _native_array_parse_shape(tile)
        result = _native_array_tile_reps(shape, values, dtype, tile_reps, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and roll != "":
        result = _native_array_roll(shape, values, dtype, roll, axis_text, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and rot90 != "":
        old_shape = shape
        old_dtype = dtype
        result = _native_array_rot90(shape, values, dtype, rot90, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        base_shape = old_shape
        strides_override = None
        c_contiguous = True
        if len(old_shape) == 2:
            turns = int(rot90) % 4
            itemsize = _native_array_dtype_itemsize(old_dtype)
            stride0 = old_shape[1] * itemsize
            stride1 = itemsize
            if turns == 1:
                strides_override = [-stride1, stride0]
                c_contiguous = False
            elif turns == 2:
                strides_override = [-stride0, -stride1]
                c_contiguous = False
            elif turns == 3:
                strides_override = [stride1, -stride0]
                c_contiguous = False
    if literal != "" and op != "":
        if rhs == "":
            rhs = "0"
        result = _native_array_binary_op(shape, values, dtype, rhs, op, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and matmul != "":
        result = _native_array_matmul(shape, values, dtype, matmul, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and concat != "":
        result = _native_array_concat(
            shape, values, dtype, concat, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and stack != "":
        result = _native_array_stack(
            shape, values, dtype, stack, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and compare != "":
        if rhs == "":
            rhs = "0"
        result = _native_array_compare(shape, values, dtype, rhs, compare, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and index != "":
        result = _native_array_index(shape, values, dtype, index, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and diagonal != "":
        base_shape = shape
        result = _native_array_diagonal(shape, values, dtype, diagonal, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        if len(base_shape) == 2:
            itemsize = _native_array_dtype_itemsize(dtype)
            strides_override = [(base_shape[1] + 1) * itemsize]
            c_contiguous = len(shape) == 0 or shape[0] <= 1
    if literal != "" and reshape != "":
        base_shape = shape
        result = _native_array_reshape(shape, values, dtype, reshape, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        strides_override = None
        c_contiguous = True
    if literal != "" and ravel:
        base_shape = shape
        result = _native_array_ravel(shape, values, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        strides_override = None
        c_contiguous = True
    if literal != "" and flatten:
        result = _native_array_ravel(shape, values, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and flip:
        old_shape = shape
        result = _native_array_flip(shape, values, dtype, axis_text, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        base_shape = old_shape
        itemsize = _native_array_dtype_itemsize(dtype)
        if len(old_shape) == 1:
            strides_override = [-itemsize]
        elif len(old_shape) == 2:
            stride0 = old_shape[1] * itemsize
            stride1 = itemsize
            axis_value_for_stride = _native_array_axis_value(axis_text)
            if axis_value_for_stride == -999999:
                stride0 = -stride0
                stride1 = -stride1
            else:
                normalized_axis_for_stride = _native_array_axis_normalize(
                    axis_value_for_stride, len(old_shape)
                )
                if normalized_axis_for_stride == 0:
                    stride0 = -stride0
                elif normalized_axis_for_stride == 1:
                    stride1 = -stride1
            strides_override = [stride0, stride1]
        else:
            strides_override = None
        c_contiguous = len(old_shape) == 0
    if literal != "" and transpose:
        base_shape = shape
        old_shape = shape
        old_dtype = dtype
        result = _native_array_transpose(shape, values, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        if len(old_shape) == 2:
            itemsize = _native_array_dtype_itemsize(old_dtype)
            strides_override = [itemsize, old_shape[1] * itemsize]
            c_contiguous = False
    if literal != "" and swapaxes != "":
        base_shape = shape
        old_shape = shape
        old_dtype = dtype
        result = _native_array_swapaxes(shape, values, dtype, swapaxes, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        strides_override = None
        c_contiguous = True
        if len(old_shape) == 2 and shape != old_shape:
            itemsize = _native_array_dtype_itemsize(old_dtype)
            strides_override = [itemsize, old_shape[1] * itemsize]
            c_contiguous = False
    if literal != "" and moveaxis != "":
        base_shape = shape
        old_shape = shape
        old_dtype = dtype
        result = _native_array_moveaxis(shape, values, dtype, moveaxis, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        strides_override = None
        c_contiguous = True
        if len(old_shape) == 2 and shape != old_shape:
            itemsize = _native_array_dtype_itemsize(old_dtype)
            strides_override = [itemsize, old_shape[1] * itemsize]
            c_contiguous = False
    if literal != "" and squeeze:
        base_shape = shape
        result = _native_array_squeeze(
            shape, values, dtype, squeeze_axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        strides_override = None
        c_contiguous = True
    if literal != "" and expand_dims_text != "":
        base_shape = shape
        result = _native_array_expand_dims(
            shape, values, dtype, expand_dims_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = True
        owns_data = False
        strides_override = None
        c_contiguous = True
    if literal != "" and reduce_name != "":
        result = _native_array_reduce(
            shape, values, dtype, reduce_name, axis_text, keepdims, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and argreduce_name != "":
        result = _native_array_arg_reduce(
            shape, values, dtype, argreduce_name, axis_text, keepdims, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and count_nonzero:
        result = _native_array_count_nonzero(
            shape, values, dtype, axis_text, keepdims, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
    if literal != "" and nonzero:
        result = _native_array_nonzero(shape, values, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and argwhere:
        result = _native_array_argwhere(shape, values, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and flatnonzero:
        result = _native_array_flatnonzero(shape, values, dtype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and cumulative != "":
        result = _native_array_cumulative(
            shape, values, dtype, cumulative, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and sort_value:
        result = _native_array_sort(shape, values, dtype, axis_text, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and argsort_value:
        result = _native_array_argsort(shape, values, dtype, axis_text, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and searchsorted != "":
        result = _native_array_searchsorted(
            shape, values, dtype, searchsorted, search_side, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and partition != "":
        result = _native_array_partition(
            shape, values, dtype, partition, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and argpartition != "":
        result = _native_array_argpartition(
            shape, values, dtype, argpartition, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and take != "":
        result = _native_array_take(shape, values, dtype, take, axis_text, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and put != "":
        result = _native_array_put(shape, values, dtype, put, put_values, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and putmask != "":
        result = _native_array_putmask(
            shape, values, dtype, putmask, putmask_values, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and mask != "":
        result = _native_array_mask(shape, values, dtype, mask, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and compress != "":
        result = _native_array_compress(
            shape, values, dtype, compress, axis_text, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and where != "":
        if otherwise == "":
            otherwise = "0"
        result = _native_array_where(
            where, shape, values, dtype, otherwise, diagnostics
        )
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and astype != "":
        result = _native_array_astype(shape, values, dtype, astype, diagnostics)
        shape = result[0]
        values = result[1]
        dtype = result[2]
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    if literal != "" and copy_value:
        values = _native_array_copy_values(values)
        view = False
        owns_data = True
        base_shape = None
        strides_override = None
        c_contiguous = True
    report = _native_array_core_json(
        shape,
        dtype,
        source,
        diagnostics,
        values,
        fill,
        arange,
        zeros,
        ones,
        zeros_like,
        ones_like,
        full_like,
        eye,
        linspace,
        op,
        rhs,
        matmul,
        concat,
        stack,
        unary,
        clip,
        broadcast_to,
        repeat,
        tile,
        roll,
        rot90,
        compare,
        index,
        diagonal,
        reshape,
        ravel,
        flatten,
        flip,
        transpose,
        swapaxes,
        moveaxis,
        squeeze,
        squeeze_axis_text,
        expand_dims_text,
        reduce_name,
        argreduce_name,
        count_nonzero,
        nonzero,
        argwhere,
        flatnonzero,
        cumulative,
        sort_value,
        argsort_value,
        searchsorted,
        search_side,
        partition,
        argpartition,
        axis_text,
        keepdims,
        take,
        put,
        put_values,
        putmask,
        putmask_values,
        mask,
        compress,
        where,
        otherwise,
        astype,
        copy_value,
        view,
        owns_data,
        base_shape,
        strides_override,
        c_contiguous,
    )
    _write_text(report)
    return 2 if _native_find_from(report, '"ok": false', 0) >= 0 else 0


def _run_native_package_array_core_from_pcc1(module_args) -> int:
    try:
        return _run_native_package_array_core_impl(module_args)
    except ValueError as exc:
        code = str(exc)
        if not code.startswith("PCC-ARRAY-"):
            code = "PCC-ARRAY-NUMERIC-PARSE-FAILED"
        _write_text('{"ok": false, "diagnostics": [{"code": ' + _json_str(code) + '}]}')
        return 2
    except OverflowError:
        _write_text('{"ok": false, "diagnostics": [{"code": "PCC-ARRAY-NUMERIC-OVERFLOW-UNSUPPORTED"}]}')
        return 2
    except ZeroDivisionError:
        _write_text('{"ok": false, "diagnostics": [{"code": "PCC-ARRAY-UFUNC-FAILED"}]}')
        return 2
