"""Generic array-core layout semantics for pcc package/runtime planning.

This module is intentionally package-agnostic. NumPy is one consumer, but the
model here only describes common ndarray-like metadata: shape, C-contiguous
strides, minimal dtype inference, and diagnostics for layouts pcc cannot claim
as rectangular arrays yet.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import reduce
from operator import mul
from typing import Iterable

from pcc.package.array_numeric import (
    coerce_float,
    wrap_integer,
    float_binary,
    float_unary,
    float_sum,
    number_compare,
)


_DTYPE_ITEMSIZE = {
    "bool": 1,
    "int8": 1,
    "int16": 2,
    "int32": 4,
    "int64": 8,
    "uint8": 1,
    "uint16": 2,
    "uint32": 4,
    "uint64": 8,
    "float32": 4,
    "float64": 8,
    "object": 8,
}
_DTYPE_FORMAT = {
    "bool": "?",
    "int8": "b",
    "int16": "h",
    "int32": "i",
    "int64": "q",
    "uint8": "B",
    "uint16": "H",
    "uint32": "I",
    "uint64": "Q",
    "float32": "f",
    "float64": "d",
    "object": "O",
}
_INTEGER_DTYPES = {"int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"}
_FLOAT_DTYPES = {"float32", "float64"}
_INTEGER_DTYPE_META = {
    "int8": (8, True),
    "int16": (16, True),
    "int32": (32, True),
    "int64": (64, True),
    "uint8": (8, False),
    "uint16": (16, False),
    "uint32": (32, False),
    "uint64": (64, False),
}


@dataclass(frozen=True)
class ArrayCoreLayout:
    shape: tuple[int, ...]
    dtype: str
    itemsize: int
    strides: tuple[int, ...]
    size: int
    nbytes: int
    c_contiguous: bool
    diagnostics: tuple[dict[str, object], ...] = ()

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def as_dict(self) -> dict[str, object]:
        report: dict[str, object] = {
            "ok": self.ok,
            "shape": list(self.shape),
            "ndim": self.ndim,
            "dtype": self.dtype,
            "dtype_format": dtype_format(self.dtype),
            "itemsize": self.itemsize,
            "strides": list(self.strides),
            "size": self.size,
            "nbytes": self.nbytes,
            "c_contiguous": self.c_contiguous,
            "diagnostics": list(self.diagnostics),
        }
        dtype_range_value = dtype_range(self.dtype)
        if dtype_range_value is not None:
            report["dtype_range"] = list(dtype_range_value)
            report["dtype_signed"] = self.dtype.startswith("int")
        return report


@dataclass(frozen=True)
class ArrayCoreValue:
    layout: ArrayCoreLayout
    data: tuple[object, ...]
    owns_data: bool = True
    view: bool = False
    base_shape: tuple[int, ...] | None = None

    @property
    def shape(self) -> tuple[int, ...]:
        return self.layout.shape

    @property
    def dtype(self) -> str:
        return self.layout.dtype

    @property
    def ok(self) -> bool:
        return self.layout.ok

    def as_nested_list(self) -> object:
        if not self.shape:
            return self.data[0] if self.data else None
        if len(self.data) != self.layout.size:
            return list(self.data)
        index = 0

        def build(axis: int) -> object:
            nonlocal index
            if axis == len(self.shape):
                value = self.data[index]
                index += 1
                return value
            return [build(axis + 1) for _ in range(self.shape[axis])]

        return build(0)

    def as_dict(self) -> dict[str, object]:
        report = self.layout.as_dict()
        report["data"] = self.as_nested_list()
        report["flat_data"] = list(self.data)
        report["owns_data"] = self.owns_data
        report["repr"] = array_core_repr(self)
        report["view"] = self.view
        if self.dtype == "object":
            report["object_policy"] = {
                "allowed": [
                    "storage",
                    "index",
                    "take",
                    "put",
                    "putmask",
                    "compress",
                    "roll",
                    "flip",
                    "transpose",
                    "swapaxes",
                    "moveaxis",
                    "rot90",
                    "reshape",
                    "ravel",
                    "flatten",
                    "copy",
                    "repr",
                ],
                "unsupported": ["numeric_ufunc", "numeric_reduce", "typed_memoryview"],
            }
        if self.base_shape is not None:
            report["base_shape"] = list(self.base_shape)
        return report


def dtype_itemsize(dtype: str) -> int:
    return _DTYPE_ITEMSIZE.get(dtype, _DTYPE_ITEMSIZE["object"])


def dtype_format(dtype: str) -> str:
    return _DTYPE_FORMAT.get(dtype, _DTYPE_FORMAT["object"])


def dtype_range(dtype: str) -> tuple[int, int] | None:
    dtype_name = normalize_dtype(dtype)
    if dtype_name == "bool":
        return (0, 1)
    meta = _INTEGER_DTYPE_META.get(dtype_name)
    if meta is None:
        return None
    bits, signed = meta
    if signed:
        high = (1 << (bits - 1)) - 1
        return (-(1 << (bits - 1)), high)
    return (0, (1 << bits) - 1)


def normalize_dtype(dtype: str | None) -> str:
    if dtype is None or dtype == "" or dtype == "auto":
        return "object"
    lowered = dtype.lower()
    aliases = {
        "bool_": "bool",
        "boolean": "bool",
        "int": "int64",
        "int_": "int64",
        "long": "int64",
        "longlong": "int64",
        "byte": "int8",
        "short": "int16",
        "intc": "int32",
        "uint": "uint64",
        "uint_": "uint64",
        "ulong": "uint64",
        "float": "float64",
        "float_": "float64",
        "single": "float32",
        "double": "float64",
        "object_": "object",
        "pyobject": "object",
    }
    return aliases.get(lowered, lowered if lowered in _DTYPE_ITEMSIZE else "object")


def c_contiguous_strides(shape: Iterable[int], itemsize: int) -> tuple[int, ...]:
    dims = tuple(int(dim) for dim in shape)
    if not dims:
        return ()
    strides: list[int] = [0] * len(dims)
    stride = int(itemsize)
    for i in range(len(dims) - 1, -1, -1):
        strides[i] = stride
        dim = dims[i]
        stride *= dim if dim > 0 else 1
    return tuple(strides)


def _size(shape: tuple[int, ...]) -> int:
    if not shape:
        return 1
    if any(dim == 0 for dim in shape):
        return 0
    return reduce(mul, shape, 1)


def _scalar_dtype(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int64"
    if isinstance(value, float):
        return "float64"
    return "object"


def _promote_dtype(left: str, right: str) -> str:
    if left == right:
        return left
    if "object" in (left, right):
        return "object"
    if "float64" in (left, right):
        return "float64"
    if "float32" in (left, right):
        return "float32"
    if left in _INTEGER_DTYPES or right in _INTEGER_DTYPES or left == "bool" or right == "bool":
        return "int64"
    return "object"


def _infer_value(value: object) -> tuple[tuple[int, ...], str, list[dict[str, object]]]:
    if isinstance(value, (list, tuple)):
        if not value:
            return (0,), "object", [
                {
                    "code": "PCC-ARRAY-EMPTY-DTYPE",
                    "message": "empty array literal needs an explicit dtype for a precise layout",
                }
            ]
        child_shapes: list[tuple[int, ...]] = []
        dtype = "bool"
        diagnostics: list[dict[str, object]] = []
        for child in value:
            child_shape, child_dtype, child_diagnostics = _infer_value(child)
            child_shapes.append(child_shape)
            dtype = _promote_dtype(dtype, child_dtype)
            diagnostics.extend(child_diagnostics)
        first_shape = child_shapes[0]
        if any(shape != first_shape for shape in child_shapes):
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-RAGGED",
                    "message": "array literal is ragged; pcc cannot claim rectangular ndarray layout",
                    "child_shapes": [list(shape) for shape in child_shapes],
                }
            )
            return (len(value),), "object", diagnostics
        return (len(value),) + first_shape, dtype, diagnostics
    return (), _scalar_dtype(value), []


def layout_from_shape(
    shape: Iterable[int],
    *,
    dtype: str = "object",
    require_rectangular: bool = False,
    diagnostics: Iterable[dict[str, object]] = (),
    strides: Iterable[int] | None = None,
    c_contiguous: bool | None = None,
) -> ArrayCoreLayout:
    dims = tuple(int(dim) for dim in shape)
    rows = list(diagnostics)
    if any(dim < 0 for dim in dims):
        rows.append(
            {
                "code": "PCC-ARRAY-NEGATIVE-DIMENSION",
                "message": "array shape dimensions must be non-negative",
            }
        )
    dtype_name = normalize_dtype(dtype)
    itemsize = dtype_itemsize(dtype_name)
    stride_tuple = tuple(int(stride) for stride in strides) if strides is not None else c_contiguous_strides(dims, itemsize)
    contiguous = c_contiguous if c_contiguous is not None else stride_tuple == c_contiguous_strides(dims, itemsize)
    total = _size(dims)
    if require_rectangular and any(row.get("code") == "PCC-ARRAY-RAGGED" for row in rows):
        rows.append(
            {
                "code": "PCC-ARRAY-REQUIRES-RECTANGULAR",
                "message": "rectangular layout was required but the input is ragged",
            }
        )
    return ArrayCoreLayout(
        shape=dims,
        dtype=dtype_name,
        itemsize=itemsize,
        strides=stride_tuple,
        size=total,
        nbytes=total * itemsize,
        c_contiguous=contiguous,
        diagnostics=tuple(rows),
    )


def infer_array_layout(
    value: object,
    *,
    dtype: str | None = "auto",
    require_rectangular: bool = False,
) -> ArrayCoreLayout:
    shape, inferred_dtype, diagnostics = _infer_value(value)
    dtype_name = inferred_dtype if dtype in (None, "", "auto") else normalize_dtype(dtype)
    return layout_from_shape(
        shape,
        dtype=dtype_name,
        require_rectangular=require_rectangular,
        diagnostics=diagnostics,
    )


def infer_literal_layout(
    literal: str,
    *,
    dtype: str | None = "auto",
    require_rectangular: bool = False,
) -> ArrayCoreLayout:
    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError) as exc:
        return layout_from_shape(
            (),
            dtype="object",
            diagnostics=[
                {
                    "code": "PCC-ARRAY-LITERAL-PARSE-FAILED",
                    "message": str(exc),
                }
            ],
        )
    return infer_array_layout(value, dtype=dtype, require_rectangular=require_rectangular)


def parse_shape(text: str) -> tuple[int, ...]:
    stripped = text.strip()
    if not stripped:
        return ()
    return tuple(int(part.strip()) for part in stripped.split(",") if part.strip())


def parse_int_list(text: str) -> tuple[int, ...]:
    return parse_shape(text)


def _parse_scalar_literal(text: str) -> object:
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return text


def _coerce_scalar(value: object, dtype: str) -> object:
    if dtype == "bool":
        return bool(value)
    if dtype in _INTEGER_DTYPES:
        return _coerce_integer(value, dtype)
    if dtype in _FLOAT_DTYPES:
        return coerce_float(value)
    return value


def _coerce_integer(value: object, dtype: str) -> int:
    raw = int(value)
    meta = _INTEGER_DTYPE_META[dtype]
    bits, signed = meta
    return wrap_integer(raw, bits, signed)


def _flatten_rectangular(value: object, shape: tuple[int, ...], dtype: str) -> list[object]:
    if not shape:
        return [_coerce_scalar(value, dtype)]
    if not isinstance(value, (list, tuple)):
        return [_coerce_scalar(value, dtype)]
    out: list[object] = []
    for child in value:
        out.extend(_flatten_rectangular(child, shape[1:], dtype))
    return out


def value_from_python(
    value: object,
    *,
    dtype: str | None = "auto",
    require_rectangular: bool = False,
) -> ArrayCoreValue:
    layout = infer_array_layout(
        value,
        dtype=dtype,
        require_rectangular=require_rectangular,
    )
    if layout.ok:
        data = tuple(_flatten_rectangular(value, layout.shape, layout.dtype))
    elif isinstance(value, (list, tuple)) and layout.shape == (len(value),):
        data = tuple(value)
    else:
        data = ()
    return ArrayCoreValue(layout=layout, data=data)


def value_from_literal(
    literal: str,
    *,
    dtype: str | None = "auto",
    require_rectangular: bool = False,
) -> ArrayCoreValue:
    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError) as exc:
        layout = layout_from_shape(
            (),
            dtype="object",
            diagnostics=[
                {
                    "code": "PCC-ARRAY-LITERAL-PARSE-FAILED",
                    "message": str(exc),
                }
            ],
        )
        return ArrayCoreValue(layout=layout, data=())
    return value_from_python(
        value,
        dtype=dtype,
        require_rectangular=require_rectangular,
    )


def array_full(shape: Iterable[int], fill_value: object, *, dtype: str | None = "auto") -> ArrayCoreValue:
    dtype_name = _scalar_dtype(fill_value) if dtype in (None, "", "auto") else normalize_dtype(dtype)
    layout = layout_from_shape(shape, dtype=dtype_name)
    if not layout.ok:
        return ArrayCoreValue(layout=layout, data=())
    return ArrayCoreValue(
        layout=layout,
        data=tuple(_coerce_scalar(fill_value, dtype_name) for _ in range(layout.size)),
    )


def array_full_from_literal(shape: Iterable[int], fill_literal: str, *, dtype: str | None = "auto") -> ArrayCoreValue:
    return array_full(shape, _parse_scalar_literal(fill_literal), dtype=dtype)


def array_full_like(value: ArrayCoreValue, fill_value: object, *, dtype: str | None = "auto") -> ArrayCoreValue:
    dtype_name = value.dtype if dtype in (None, "", "auto") else normalize_dtype(dtype)
    layout = layout_from_shape(value.shape, dtype=dtype_name, diagnostics=value.layout.diagnostics)
    if not layout.ok:
        return ArrayCoreValue(layout=layout, data=(), owns_data=True, view=False)
    return ArrayCoreValue(
        layout=layout,
        data=tuple(_coerce_scalar(fill_value, dtype_name) for _ in range(layout.size)),
        owns_data=True,
        view=False,
    )


def array_full_like_from_literal(value: ArrayCoreValue, fill_literal: str, *, dtype: str | None = "auto") -> ArrayCoreValue:
    return array_full_like(value, _parse_scalar_literal(fill_literal), dtype=dtype)


def array_zeros_like(value: ArrayCoreValue, *, dtype: str | None = "auto") -> ArrayCoreValue:
    return array_full_like(value, 0, dtype=dtype)


def array_ones_like(value: ArrayCoreValue, *, dtype: str | None = "auto") -> ArrayCoreValue:
    return array_full_like(value, 1, dtype=dtype)


def array_zeros(shape: Iterable[int], *, dtype: str | None = "auto") -> ArrayCoreValue:
    dtype_name = "float64" if dtype in (None, "", "auto") else normalize_dtype(dtype)
    return array_full(shape, 0, dtype=dtype_name)


def array_ones(shape: Iterable[int], *, dtype: str | None = "auto") -> ArrayCoreValue:
    dtype_name = "float64" if dtype in (None, "", "auto") else normalize_dtype(dtype)
    return array_full(shape, 1, dtype=dtype_name)


def array_eye(spec: str, *, dtype: str | None = "auto") -> ArrayCoreValue:
    diagnostics: list[dict[str, object]] = []
    try:
        parts = [int(part.strip()) for part in spec.split(",") if part.strip()]
        if len(parts) == 1:
            rows = parts[0]
            cols = parts[0]
            diagonal = 0
        elif len(parts) == 2:
            rows, cols = parts
            diagonal = 0
        elif len(parts) == 3:
            rows, cols, diagonal = parts
        else:
            raise ValueError("eye expects n, n,m, or n,m,k")
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-EYE-PARSE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype="object", diagnostics=diagnostics), data=())
    dtype_name = "float64" if dtype in (None, "", "auto") else normalize_dtype(dtype)
    layout = layout_from_shape((rows, cols), dtype=dtype_name)
    if not layout.ok:
        return ArrayCoreValue(layout=layout, data=())
    out: list[object] = []
    for row in range(rows):
        for col in range(cols):
            out.append(_coerce_scalar(1 if col - row == diagonal else 0, dtype_name))
    return ArrayCoreValue(layout=layout, data=tuple(out))


def array_linspace(spec: str, *, dtype: str | None = "auto") -> ArrayCoreValue:
    diagnostics: list[dict[str, object]] = []
    try:
        parts = [_parse_scalar_literal(part.strip()) for part in spec.split(",") if part.strip()]
        if len(parts) == 2:
            start, stop = parts
            num = 50
        elif len(parts) == 3:
            start, stop, raw_num = parts
            num = int(raw_num)
        else:
            raise ValueError("linspace expects start,stop or start,stop,num")
        if num < 0:
            raise ValueError("linspace num must be non-negative")
        numeric_start = float(start)
        numeric_stop = float(stop)
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-LINSPACE-PARSE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype="object", diagnostics=diagnostics), data=())
    dtype_name = "float64" if dtype in (None, "", "auto") else normalize_dtype(dtype)
    if num == 0:
        return ArrayCoreValue(layout=layout_from_shape((0,), dtype=dtype_name), data=())
    if num == 1:
        data = (_coerce_scalar(numeric_start, dtype_name),)
    else:
        step = (numeric_stop - numeric_start) / (num - 1)
        data = tuple(_coerce_scalar(numeric_start + step * index, dtype_name) for index in range(num))
    return ArrayCoreValue(layout=layout_from_shape((num,), dtype=dtype_name), data=data)


def array_arange(spec: str, *, dtype: str | None = "auto") -> ArrayCoreValue:
    diagnostics: list[dict[str, object]] = []
    try:
        parts = [_parse_scalar_literal(part.strip()) for part in spec.split(",") if part.strip()]
        if len(parts) == 1:
            start: object = 0
            stop = parts[0]
            step: object = 1
        elif len(parts) == 2:
            start, stop = parts
            step = 1
        elif len(parts) == 3:
            start, stop, step = parts
        else:
            raise ValueError("arange expects stop, start,stop, or start,stop,step")
        numeric_start = float(start) if isinstance(start, float) or isinstance(stop, float) or isinstance(step, float) else int(start)
        numeric_stop = float(stop) if isinstance(numeric_start, float) or isinstance(stop, float) or isinstance(step, float) else int(stop)
        numeric_step = float(step) if isinstance(numeric_start, float) or isinstance(numeric_stop, float) or isinstance(step, float) else int(step)
        if numeric_step == 0:
            raise ValueError("arange step must not be zero")
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARANGE-PARSE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype="object", diagnostics=diagnostics), data=())

    dtype_name = ("float64" if any(isinstance(part, float) for part in parts) else "int64") if dtype in (None, "", "auto") else normalize_dtype(dtype)
    values: list[object] = []
    current = numeric_start
    if numeric_step > 0:
        while current < numeric_stop:
            values.append(_coerce_scalar(current, dtype_name))
            current += numeric_step
    else:
        while current > numeric_stop:
            values.append(_coerce_scalar(current, dtype_name))
            current += numeric_step
    return ArrayCoreValue(
        layout=layout_from_shape((len(values),), dtype=dtype_name),
        data=tuple(values),
    )


def _flat_index(shape: tuple[int, ...], indices: tuple[int, ...]) -> int:
    if not shape:
        return 0
    flat = 0
    stride = 1
    for axis in range(len(shape) - 1, -1, -1):
        flat += indices[axis] * stride
        stride *= shape[axis]
    return flat


def _iter_indices(shape: tuple[int, ...]) -> list[tuple[int, ...]]:
    if not shape:
        return [()]
    out: list[tuple[int, ...]] = []

    def visit(prefix: tuple[int, ...], axis: int) -> None:
        if axis == len(shape):
            out.append(prefix)
            return
        for i in range(shape[axis]):
            visit(prefix + (i,), axis + 1)

    visit((), 0)
    return out


def _parse_index_part(text: str) -> int | slice | str:
    token = text.strip()
    if token in {"None", "none", "newaxis"}:
        return "newaxis"
    if token == "...":
        return "ellipsis"
    if ":" not in token:
        return int(token)
    pieces = token.split(":")
    if len(pieces) > 3:
        raise ValueError(f"invalid slice: {text}")
    start = int(pieces[0]) if pieces[0] else None
    stop = int(pieces[1]) if len(pieces) > 1 and pieces[1] else None
    step = int(pieces[2]) if len(pieces) > 2 and pieces[2] else None
    return slice(start, stop, step)


def parse_index_spec(text: str) -> tuple[int | slice | str, ...]:
    stripped = text.strip()
    if not stripped:
        return ()
    return tuple(_parse_index_part(part) for part in stripped.split(","))


def _expand_index_parts(
    parts: tuple[int | slice | str, ...],
    ndim: int,
) -> tuple[tuple[int | slice | str, ...] | None, str | None]:
    ellipsis_count = sum(1 for part in parts if part == "ellipsis")
    if ellipsis_count > 1:
        return None, "multiple ellipsis entries are not supported"
    consumed = sum(1 for part in parts if not (isinstance(part, str) and part in {"newaxis", "ellipsis"}))
    if ellipsis_count == 0:
        return (parts, None) if consumed == ndim else (None, "index rank must match array rank for the current array-core subset")
    fill = ndim - consumed
    if fill < 0:
        return None, "index rank must match array rank for the current array-core subset"
    expanded: list[int | slice | str] = []
    for part in parts:
        if part == "ellipsis":
            expanded.extend(slice(None) for _ in range(fill))
        else:
            expanded.append(part)
    return tuple(expanded), None


def array_index(value: ArrayCoreValue, index_spec: str) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    try:
        parts = parse_index_spec(index_spec)
    except (TypeError, ValueError) as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-INDEX-PARSE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    parts, expand_error = _expand_index_parts(parts, value.layout.ndim)
    if parts is None:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-INDEX-RANK-MISMATCH" if expand_error and "rank" in expand_error else "PCC-ARRAY-INDEX-PARSE-FAILED",
                "message": expand_error or "index parse failed",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    source_axes: list[list[int]] = []
    output_shape: list[int] = []
    source_axis = 0
    for part in parts:
        if part == "newaxis":
            output_shape.append(1)
            continue
        dim = value.shape[source_axis]
        if isinstance(part, int):
            index = part + dim if part < 0 else part
            if index < 0 or index >= dim:
                diagnostics.append(
                    {
                        "code": "PCC-ARRAY-INDEX-OUT-OF-BOUNDS",
                        "message": "array index is out of bounds",
                    }
                )
                return ArrayCoreValue(
                    layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
                    data=(),
                )
            source_axes.append([index])
        else:
            try:
                assert isinstance(part, slice)
                indices = list(range(dim))[part]
            except ValueError as exc:
                diagnostics.append(
                    {
                        "code": "PCC-ARRAY-INDEX-PARSE-FAILED",
                        "message": str(exc),
                    }
                )
                return ArrayCoreValue(
                    layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
                    data=(),
                )
            source_axes.append(indices)
            output_shape.append(len(indices))
        source_axis += 1
    if source_axis != value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-INDEX-RANK-MISMATCH",
                "message": "index rank must match array rank for the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    out_data: list[object] = []

    def visit(prefix: tuple[int, ...], axis: int) -> None:
        if axis == len(source_axes):
            out_data.append(value.data[_flat_index(value.shape, prefix)])
            return
        for source_index in source_axes[axis]:
            visit(prefix + (source_index,), axis + 1)

    visit((), 0)
    return ArrayCoreValue(
        layout=layout_from_shape(tuple(output_shape), dtype=value.dtype, diagnostics=diagnostics),
        data=tuple(out_data),
    )


def array_diagonal(value: ArrayCoreValue, offset: int = 0) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-DIAGONAL-RANK-UNSUPPORTED",
                "message": "diagonal currently supports 2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    rows, cols = value.shape
    start_row = 0 if offset >= 0 else -offset
    start_col = offset if offset >= 0 else 0
    length = min(rows - start_row, cols - start_col)
    if length < 0:
        length = 0
    out_data = tuple(value.data[(start_row + i) * cols + start_col + i] for i in range(length))
    stride = value.layout.strides[0] + value.layout.strides[1]
    return ArrayCoreValue(
        layout=layout_from_shape(
            (length,),
            dtype=value.dtype,
            strides=(stride,),
            c_contiguous=length <= 1,
        ),
        data=out_data,
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_sort(value: ArrayCoreValue, *, axis: int | None = -1) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SORT-UNSUPPORTED",
                "message": "object-array sort is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 0:
        return ArrayCoreValue(layout=value.layout, data=value.data)
    if axis is None:
        data = tuple(sorted(value.data))
        return ArrayCoreValue(layout=layout_from_shape((len(data),), dtype=value.dtype), data=data)
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    if value.layout.ndim == 1:
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype), data=tuple(sorted(value.data)))
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SORT-RANK-UNSUPPORTED",
                "message": "sort currently supports scalar/1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    rows, cols = value.shape
    out = list(value.data)
    if normalized_axis == 0:
        for col in range(cols):
            col_values = sorted(value.data[row * cols + col] for row in range(rows))
            for row, item in enumerate(col_values):
                out[row * cols + col] = item
    else:
        for row in range(rows):
            start = row * cols
            out[start : start + cols] = sorted(value.data[start : start + cols])
    return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype), data=tuple(out))


def array_argsort(value: ArrayCoreValue, *, axis: int | None = -1) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGSORT-UNSUPPORTED",
                "message": "object-array argsort is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype="int64", diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 0:
        return ArrayCoreValue(layout=layout_from_shape((), dtype="int64"), data=(0,))
    if axis is None:
        indexed = sorted(range(len(value.data)), key=lambda index: value.data[index])
        return ArrayCoreValue(layout=layout_from_shape((len(indexed),), dtype="int64"), data=tuple(indexed))
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype="int64", diagnostics=diagnostics), data=())
    if value.layout.ndim == 1:
        indexed = sorted(range(value.shape[0]), key=lambda index: value.data[index])
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64"), data=tuple(indexed))
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGSORT-RANK-UNSUPPORTED",
                "message": "argsort currently supports scalar/1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64", diagnostics=diagnostics), data=())
    rows, cols = value.shape
    out = [0 for _ in value.data]
    if normalized_axis == 0:
        for col in range(cols):
            indices = sorted(range(rows), key=lambda row: value.data[row * cols + col])
            for row, source_row in enumerate(indices):
                out[row * cols + col] = source_row
    else:
        for row in range(rows):
            start = row * cols
            indices = sorted(range(cols), key=lambda col: value.data[start + col])
            out[start : start + cols] = indices
    return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64"), data=tuple(out))


def array_searchsorted(
    value: ArrayCoreValue,
    queries: ArrayCoreValue,
    *,
    side: str = "left",
) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics) + list(queries.layout.diagnostics)
    if side not in {"left", "right"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SEARCHSORTED-SIDE-UNSUPPORTED",
                "message": "searchsorted side must be left or right",
                "side": side,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(queries.shape, dtype="int64", diagnostics=diagnostics), data=())
    if value.dtype == "object" or queries.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SEARCHSORTED-UNSUPPORTED",
                "message": "object-array searchsorted is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(queries.shape, dtype="int64", diagnostics=diagnostics), data=())
    if value.layout.ndim != 1:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SEARCHSORTED-RANK-UNSUPPORTED",
                "message": "searchsorted currently supports 1D sorted arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(queries.shape, dtype="int64", diagnostics=diagnostics), data=())
    out: list[int] = []
    for query in queries.data:
        pos = 0
        while pos < len(value.data):
            current = value.data[pos]
            if side == "left":
                if current >= query:  # type: ignore[operator]
                    break
            elif current > query:  # type: ignore[operator]
                break
            pos += 1
        out.append(pos)
    return ArrayCoreValue(layout=layout_from_shape(queries.shape, dtype="int64"), data=tuple(out))


def _normalize_kth(kth: int, length: int) -> int:
    if kth < 0:
        kth += length
    return kth


def array_partition(value: ArrayCoreValue, kth: int, *, axis: int = -1) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PARTITION-UNSUPPORTED",
                "message": "object-array partition is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    if value.layout.ndim == 0:
        normalized_kth = _normalize_kth(kth, 1)
        if normalized_kth != 0:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-PARTITION-KTH-OUT-OF-BOUNDS",
                    "message": "partition kth is out of bounds for the selected axis",
                    "kth": kth,
                }
            )
            return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
        return ArrayCoreValue(layout=value.layout, data=value.data)
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    axis_len = value.shape[normalized_axis]
    normalized_kth = _normalize_kth(kth, axis_len)
    if normalized_kth < 0 or normalized_kth >= axis_len:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PARTITION-KTH-OUT-OF-BOUNDS",
                "message": "partition kth is out of bounds for the selected axis",
                "kth": kth,
                "axis_size": axis_len,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    if value.layout.ndim == 1:
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype), data=tuple(sorted(value.data)))
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PARTITION-RANK-UNSUPPORTED",
                "message": "partition currently supports scalar/1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    return array_sort(value, axis=normalized_axis)


def array_argpartition(value: ArrayCoreValue, kth: int, *, axis: int = -1) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGPARTITION-UNSUPPORTED",
                "message": "object-array argpartition is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64", diagnostics=diagnostics), data=())
    if value.layout.ndim == 0:
        normalized_kth = _normalize_kth(kth, 1)
        if normalized_kth != 0:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-ARGPARTITION-KTH-OUT-OF-BOUNDS",
                    "message": "argpartition kth is out of bounds for the selected axis",
                    "kth": kth,
                }
            )
            return ArrayCoreValue(layout=layout_from_shape((), dtype="int64", diagnostics=diagnostics), data=())
        return ArrayCoreValue(layout=layout_from_shape((), dtype="int64"), data=(0,))
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype="int64", diagnostics=diagnostics), data=())
    axis_len = value.shape[normalized_axis]
    normalized_kth = _normalize_kth(kth, axis_len)
    if normalized_kth < 0 or normalized_kth >= axis_len:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGPARTITION-KTH-OUT-OF-BOUNDS",
                "message": "argpartition kth is out of bounds for the selected axis",
                "kth": kth,
                "axis_size": axis_len,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64", diagnostics=diagnostics), data=())
    if value.layout.ndim == 1:
        indices = sorted(range(value.shape[0]), key=lambda index: value.data[index])
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64"), data=tuple(indices))
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGPARTITION-RANK-UNSUPPORTED",
                "message": "argpartition currently supports scalar/1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype="int64", diagnostics=diagnostics), data=())
    return array_argsort(value, axis=normalized_axis)


def _broadcast_shape(
    left: tuple[int, ...],
    right: tuple[int, ...],
) -> tuple[tuple[int, ...], list[dict[str, object]]]:
    out: list[int] = []
    diagnostics: list[dict[str, object]] = []
    max_rank = max(len(left), len(right))
    for offset in range(1, max_rank + 1):
        ldim = left[-offset] if offset <= len(left) else 1
        rdim = right[-offset] if offset <= len(right) else 1
        if ldim == rdim:
            out.append(ldim)
        elif ldim == 1:
            out.append(rdim)
        elif rdim == 1:
            out.append(ldim)
        else:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-BROADCAST-INCOMPATIBLE",
                    "message": "array operands cannot be broadcast together",
                    "left_shape": list(left),
                    "right_shape": list(right),
                }
            )
            return (), diagnostics
    return tuple(reversed(out)), diagnostics


def _broadcast_flat_index(shape: tuple[int, ...], out_index: tuple[int, ...]) -> int:
    if not shape:
        return 0
    offset = len(out_index) - len(shape)
    source_index: list[int] = []
    for axis, dim in enumerate(shape):
        value = out_index[offset + axis]
        source_index.append(0 if dim == 1 else value)
    return _flat_index(shape, tuple(source_index))


def _result_dtype(op: str, left: str, right: str) -> str:
    if left == "object" or right == "object":
        return "object"
    if op == "div":
        return "float64"
    return _promote_dtype(left, right)


def _apply_ufunc(op: str, left: object, right: object) -> object:
    if isinstance(left, float) or isinstance(right, float):
        return float_binary(op, coerce_float(left), coerce_float(right))
    if op == "add":
        return left + right  # type: ignore[operator]
    if op == "sub":
        return left - right  # type: ignore[operator]
    if op == "mul":
        return left * right  # type: ignore[operator]
    if op == "div":
        return left / right  # type: ignore[operator]
    raise ValueError(f"unsupported array-core ufunc: {op}")


def _unary_op_name(op: str) -> str:
    return {
        "negative": "neg",
        "absolute": "abs",
        "not": "logical_not",
    }.get(op, op)


def _unary_result_dtype(op: str, dtype: str) -> str:
    if op == "logical_not":
        return "bool"
    return dtype


def _apply_unary_op(op: str, value: object) -> object:
    if isinstance(value, float) and op in {"neg", "abs"}:
        return float_unary(op, value)
    if op == "neg":
        return -value  # type: ignore[operator]
    if op == "abs":
        return abs(value)  # type: ignore[arg-type]
    if op == "logical_not":
        return not bool(value)
    raise ValueError(f"unsupported array-core unary ufunc: {op}")


def array_unary_op(value: ArrayCoreValue, op: str) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    op_name = _unary_op_name(op)
    if op_name not in {"neg", "abs", "logical_not"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-UNARY-UNSUPPORTED",
                "message": f"unsupported array-core unary ufunc: {op}",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-OBJECT-UNARY-UNSUPPORTED",
                "message": "object-array unary ufuncs are not supported by the current array-core subset",
            }
        )
    if value.dtype == "bool" and op_name == "neg":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-UNARY-DTYPE-UNSUPPORTED",
                "message": "negative is not supported for bool arrays by the current array-core subset",
            }
        )
    dtype = _unary_result_dtype(op_name, value.dtype)
    if diagnostics:
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    out_data: list[object] = []
    try:
        for item in value.data:
            out_data.append(_coerce_scalar(_apply_unary_op(op_name, item), dtype))
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-UNARY-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=dtype),
        data=tuple(out_data),
    )


def array_clip(value: ArrayCoreValue, bounds: str) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    try:
        parts = [part.strip() for part in bounds.split(",", 1)]
        if len(parts) != 2 or parts[0] == "" or parts[1] == "":
            raise ValueError("clip expects min,max scalar bounds")
        lower = _parse_scalar_literal(parts[0])
        upper = _parse_scalar_literal(parts[1])
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-CLIP-PARSE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-OBJECT-CLIP-UNSUPPORTED",
                "message": "object-array clip is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    dtype = _promote_dtype(value.dtype, _promote_dtype(_scalar_dtype(lower), _scalar_dtype(upper)))
    out_data: list[object] = []
    try:
        for item in value.data:
            clipped = item
            if clipped < lower:  # type: ignore[operator]
                clipped = lower
            if clipped > upper:  # type: ignore[operator]
                clipped = upper
            out_data.append(_coerce_scalar(clipped, dtype))
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-CLIP-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=dtype),
        data=tuple(out_data),
    )


def array_binary_op(left: ArrayCoreValue, right: ArrayCoreValue, op: str) -> ArrayCoreValue:
    diagnostics = list(left.layout.diagnostics) + list(right.layout.diagnostics)
    op_name = {"subtract": "sub", "multiply": "mul", "divide": "div"}.get(op, op)
    if op_name not in {"add", "sub", "mul", "div"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-UFUNC-UNSUPPORTED",
                "message": f"unsupported array-core ufunc: {op}",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype="object", diagnostics=diagnostics),
            data=(),
        )
    shape, broadcast_diagnostics = _broadcast_shape(left.shape, right.shape)
    diagnostics.extend(broadcast_diagnostics)
    dtype = _result_dtype(op_name, left.dtype, right.dtype)
    if dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-OBJECT-UFUNC-UNSUPPORTED",
                "message": "object-array numeric ufuncs are not supported by the current array-core subset",
            }
        )
    if diagnostics:
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    out_data: list[object] = []
    try:
        for out_index in _iter_indices(shape):
            lval = left.data[_broadcast_flat_index(left.shape, out_index)]
            rval = right.data[_broadcast_flat_index(right.shape, out_index)]
            out_data.append(_coerce_scalar(_apply_ufunc(op_name, lval, rval), dtype))
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-UFUNC-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=dtype),
        data=tuple(out_data),
    )


def _matmul_output_shape(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...] | None:
    if len(left) == 1 and len(right) == 1:
        return () if left[0] == right[0] else None
    if len(left) == 2 and len(right) == 1:
        return (left[0],) if left[1] == right[0] else None
    if len(left) == 1 and len(right) == 2:
        return (right[1],) if left[0] == right[0] else None
    if len(left) == 2 and len(right) == 2:
        return (left[0], right[1]) if left[1] == right[0] else None
    return None


def _matmul_result_dtype(left: str, right: str) -> str:
    dtype = _promote_dtype(left, right)
    return "int64" if dtype == "bool" else dtype


def array_matmul(left: ArrayCoreValue, right: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(left.layout.diagnostics) + list(right.layout.diagnostics)
    dtype = _matmul_result_dtype(left.dtype, right.dtype)
    if left.dtype == "object" or right.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-MATMUL-OBJECT-UNSUPPORTED",
                "message": "object-array matrix multiplication is not supported by the current array-core subset",
            }
        )
    output_shape = _matmul_output_shape(left.shape, right.shape)
    if output_shape is None:
        code = "PCC-ARRAY-MATMUL-RANK-UNSUPPORTED" if len(left.shape) not in {1, 2} or len(right.shape) not in {1, 2} else "PCC-ARRAY-MATMUL-SHAPE-MISMATCH"
        diagnostics.append(
            {
                "code": code,
                "message": "array-core matmul supports compatible 1D/2D operands only",
                "left_shape": list(left.shape),
                "right_shape": list(right.shape),
            }
        )
        output_shape = ()
    if diagnostics:
        return ArrayCoreValue(
            layout=layout_from_shape(output_shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )

    out_data: list[object] = []
    if len(left.shape) == 1 and len(right.shape) == 1:
        total = sum(left.data[i] * right.data[i] for i in range(left.shape[0]))  # type: ignore[operator]
        out_data.append(_coerce_scalar(total, dtype))
    elif len(left.shape) == 2 and len(right.shape) == 1:
        rows, inner = left.shape
        for row in range(rows):
            total = sum(left.data[row * inner + i] * right.data[i] for i in range(inner))  # type: ignore[operator]
            out_data.append(_coerce_scalar(total, dtype))
    elif len(left.shape) == 1 and len(right.shape) == 2:
        inner, cols = right.shape
        for col in range(cols):
            total = sum(left.data[i] * right.data[i * cols + col] for i in range(inner))  # type: ignore[operator]
            out_data.append(_coerce_scalar(total, dtype))
    else:
        rows, inner = left.shape
        _, cols = right.shape
        for row in range(rows):
            for col in range(cols):
                total = sum(left.data[row * inner + i] * right.data[i * cols + col] for i in range(inner))  # type: ignore[operator]
                out_data.append(_coerce_scalar(total, dtype))
    return ArrayCoreValue(
        layout=layout_from_shape(output_shape, dtype=dtype),
        data=tuple(out_data),
    )


def _apply_compare(op: str, left: object, right: object) -> bool:
    return number_compare(op, left, right)


def array_compare(left: ArrayCoreValue, right: ArrayCoreValue, op: str) -> ArrayCoreValue:
    diagnostics = list(left.layout.diagnostics) + list(right.layout.diagnostics)
    op_name = {"==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}.get(op, op)
    if op_name not in {"eq", "ne", "lt", "le", "gt", "ge"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COMPARE-UNSUPPORTED",
                "message": f"unsupported array-core comparison: {op}",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype="bool", diagnostics=diagnostics),
            data=(),
        )
    if (left.dtype == "object" or right.dtype == "object") and op_name not in {"eq", "ne"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-OBJECT-COMPARE-UNSUPPORTED",
                "message": "object-array ordered comparisons are not supported by the current array-core subset",
            }
        )
    shape, broadcast_diagnostics = _broadcast_shape(left.shape, right.shape)
    diagnostics.extend(broadcast_diagnostics)
    if diagnostics:
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype="bool", diagnostics=diagnostics),
            data=(),
        )
    out_data: list[object] = []
    try:
        for out_index in _iter_indices(shape):
            lval = left.data[_broadcast_flat_index(left.shape, out_index)]
            rval = right.data[_broadcast_flat_index(right.shape, out_index)]
            out_data.append(_apply_compare(op_name, lval, rval))
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COMPARE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype="bool", diagnostics=diagnostics),
            data=(),
        )
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype="bool"),
        data=tuple(out_data),
    )


def array_where(mask: ArrayCoreValue, true_value: ArrayCoreValue, false_value: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(mask.layout.diagnostics) + list(true_value.layout.diagnostics) + list(false_value.layout.diagnostics)
    if mask.dtype != "bool":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-WHERE-MASK-DTYPE-UNSUPPORTED",
                "message": "where selection requires a bool mask",
            }
        )
    value_shape, value_diagnostics = _broadcast_shape(true_value.shape, false_value.shape)
    diagnostics.extend(value_diagnostics)
    shape, mask_diagnostics = _broadcast_shape(value_shape, mask.shape)
    diagnostics.extend(mask_diagnostics)
    dtype = _promote_dtype(true_value.dtype, false_value.dtype)
    if diagnostics:
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    out_data: list[object] = []
    for out_index in _iter_indices(shape):
        cond = mask.data[_broadcast_flat_index(mask.shape, out_index)]
        source = true_value if bool(cond) else false_value
        out_data.append(_coerce_scalar(source.data[_broadcast_flat_index(source.shape, out_index)], dtype))
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=dtype),
        data=tuple(out_data),
    )


def array_concatenate(left: ArrayCoreValue, right: ArrayCoreValue, *, axis: int = 0) -> ArrayCoreValue:
    diagnostics = list(left.layout.diagnostics) + list(right.layout.diagnostics)
    dtype = _promote_dtype(left.dtype, right.dtype)
    if left.layout.ndim != right.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-CONCAT-RANK-MISMATCH",
                "message": "concatenate operands must have the same rank",
                "left_shape": list(left.shape),
                "right_shape": list(right.shape),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    normalized_axis = _normalize_axis(axis, left.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= left.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": left.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    if left.layout.ndim == 1:
        shape = (left.shape[0] + right.shape[0],)
        if diagnostics:
            return ArrayCoreValue(layout=layout_from_shape(shape, dtype=dtype, diagnostics=diagnostics), data=())
        data = tuple(_coerce_scalar(item, dtype) for item in left.data + right.data)
        return ArrayCoreValue(layout=layout_from_shape(shape, dtype=dtype), data=data)
    if left.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-CONCAT-RANK-UNSUPPORTED",
                "message": "the current concatenate subset supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    left_rows, left_cols = left.shape
    right_rows, right_cols = right.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        if left_cols != right_cols:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-CONCAT-SHAPE-MISMATCH",
                    "message": "concatenate axis 0 requires equal column counts for 2D arrays",
                    "left_shape": list(left.shape),
                    "right_shape": list(right.shape),
                }
            )
        shape = (left_rows + right_rows, left_cols)
        if diagnostics:
            return ArrayCoreValue(layout=layout_from_shape(shape, dtype=dtype, diagnostics=diagnostics), data=())
        out_data.extend(left.data)
        out_data.extend(right.data)
    else:
        if left_rows != right_rows:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-CONCAT-SHAPE-MISMATCH",
                    "message": "concatenate axis 1 requires equal row counts for 2D arrays",
                    "left_shape": list(left.shape),
                    "right_shape": list(right.shape),
                }
            )
        shape = (left_rows, left_cols + right_cols)
        if diagnostics:
            return ArrayCoreValue(layout=layout_from_shape(shape, dtype=dtype, diagnostics=diagnostics), data=())
        for row in range(left_rows):
            out_data.extend(left.data[row * left_cols : (row + 1) * left_cols])
            out_data.extend(right.data[row * right_cols : (row + 1) * right_cols])
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=dtype),
        data=tuple(_coerce_scalar(item, dtype) for item in out_data),
    )


def array_stack(left: ArrayCoreValue, right: ArrayCoreValue, *, axis: int = 0) -> ArrayCoreValue:
    diagnostics = list(left.layout.diagnostics) + list(right.layout.diagnostics)
    dtype = _promote_dtype(left.dtype, right.dtype)
    if left.shape != right.shape:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-STACK-SHAPE-MISMATCH",
                "message": "stack operands must have identical shapes",
                "left_shape": list(left.shape),
                "right_shape": list(right.shape),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    if left.layout.ndim != 1:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-STACK-RANK-UNSUPPORTED",
                "message": "the current stack subset supports 1D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    normalized_axis = axis + 2 if axis < 0 else axis
    if normalized_axis < 0 or normalized_axis > 1:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for stack result",
                "axis": axis,
                "ndim": 2,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    if diagnostics:
        return ArrayCoreValue(layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics), data=())
    if normalized_axis == 0:
        shape = (2, left.shape[0])
        data = tuple(_coerce_scalar(item, dtype) for item in left.data + right.data)
    else:
        shape = (left.shape[0], 2)
        out: list[object] = []
        for index in range(left.shape[0]):
            out.append(left.data[index])
            out.append(right.data[index])
        data = tuple(_coerce_scalar(item, dtype) for item in out)
    return ArrayCoreValue(layout=layout_from_shape(shape, dtype=dtype), data=data)


def array_repeat(value: ArrayCoreValue, repeats: int, *, axis: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    count = int(repeats)
    if count < 0:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-REPEAT-NEGATIVE",
                "message": "repeat count must be non-negative",
                "repeats": count,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if axis is None:
        out_data = [item for item in value.data for _ in range(count)]
        return ArrayCoreValue(
            layout=layout_from_shape((len(out_data),), dtype=value.dtype, diagnostics=diagnostics),
            data=tuple(out_data),
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    if value.layout.ndim == 1:
        return array_repeat(value, count, axis=None)
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-REPEAT-RANK-UNSUPPORTED",
                "message": "repeat currently supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        for row in range(rows):
            start = row * cols
            row_data = value.data[start : start + cols]
            for _ in range(count):
                out_data.extend(row_data)
        shape = (rows * count, cols)
    else:
        for row in range(rows):
            for col in range(cols):
                item = value.data[row * cols + col]
                for _ in range(count):
                    out_data.append(item)
        shape = (rows, cols * count)
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=value.dtype, diagnostics=diagnostics),
        data=tuple(out_data),
    )


def array_tile(value: ArrayCoreValue, reps: Iterable[int]) -> ArrayCoreValue:
    reps_tuple = tuple(int(rep) for rep in reps)
    diagnostics = list(value.layout.diagnostics)
    if not reps_tuple:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-TILE-REPS-EMPTY",
                "message": "tile requires at least one repeat dimension",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    if any(rep < 0 for rep in reps_tuple):
        diagnostics.append(
            {
                "code": "PCC-ARRAY-TILE-REPS-NEGATIVE",
                "message": "tile repeat dimensions must be non-negative",
                "reps": list(reps_tuple),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    if value.layout.ndim == 0:
        shape = reps_tuple
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=value.dtype, diagnostics=diagnostics),
            data=tuple(value.data[0] for _ in range(_size(shape))),
        )
    if value.layout.ndim == 1:
        if len(reps_tuple) == 1:
            out_data: list[object] = []
            for _ in range(reps_tuple[0]):
                out_data.extend(value.data)
            return ArrayCoreValue(
                layout=layout_from_shape((len(out_data),), dtype=value.dtype, diagnostics=diagnostics),
                data=tuple(out_data),
            )
        if len(reps_tuple) == 2:
            rows, col_reps = reps_tuple
            out_data = []
            for _ in range(rows):
                for _ in range(col_reps):
                    out_data.extend(value.data)
            return ArrayCoreValue(
                layout=layout_from_shape((rows, value.shape[0] * col_reps), dtype=value.dtype, diagnostics=diagnostics),
                data=tuple(out_data),
            )
    if value.layout.ndim == 2 and len(reps_tuple) in {1, 2}:
        row_reps, col_reps = (1, reps_tuple[0]) if len(reps_tuple) == 1 else reps_tuple
        rows, cols = value.shape
        out_data = []
        for _ in range(row_reps):
            for row in range(rows):
                start = row * cols
                row_data = value.data[start : start + cols]
                for _ in range(col_reps):
                    out_data.extend(row_data)
        return ArrayCoreValue(
            layout=layout_from_shape((rows * row_reps, cols * col_reps), dtype=value.dtype, diagnostics=diagnostics),
            data=tuple(out_data),
        )
    diagnostics.append(
        {
            "code": "PCC-ARRAY-TILE-RANK-UNSUPPORTED",
            "message": "tile currently supports scalar/1D/2D arrays with one or two repeat dimensions",
            "shape": list(value.shape),
            "reps": list(reps_tuple),
        }
    )
    return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())


def array_roll(value: ArrayCoreValue, shift: int, *, axis: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    amount = int(shift)
    if value.layout.size == 0:
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype), data=())
    if axis is None:
        size = len(value.data)
        offset = amount % size
        data = value.data[-offset:] + value.data[:-offset] if offset else value.data
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=tuple(data),
            owns_data=True,
            view=False,
            base_shape=None,
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    if value.layout.ndim == 0:
        return ArrayCoreValue(layout=value.layout, data=value.data, owns_data=True, view=False, base_shape=None)
    if value.layout.ndim == 1:
        return array_roll(value, amount, axis=None)
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ROLL-RANK-UNSUPPORTED",
                "message": "roll currently supports scalar/1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        offset = amount % rows if rows else 0
        row_order = list(range(rows - offset, rows)) + list(range(0, rows - offset)) if offset else list(range(rows))
        for row in row_order:
            start = row * cols
            out_data.extend(value.data[start : start + cols])
    else:
        offset = amount % cols if cols else 0
        col_order = list(range(cols - offset, cols)) + list(range(0, cols - offset)) if offset else list(range(cols))
        for row in range(rows):
            for col in col_order:
                out_data.append(value.data[row * cols + col])
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
        data=tuple(out_data),
        owns_data=True,
        view=False,
        base_shape=None,
    )


def _broadcast_to_strides(value: ArrayCoreValue, target: tuple[int, ...]) -> tuple[int, ...] | None:
    if len(value.shape) > len(target):
        return None
    offset = len(target) - len(value.shape)
    out: list[int] = []
    for axis, dim in enumerate(target):
        if axis < offset:
            out.append(0)
            continue
        source_axis = axis - offset
        source_dim = value.shape[source_axis]
        if source_dim == dim:
            out.append(value.layout.strides[source_axis])
        elif source_dim == 1:
            out.append(0)
        else:
            return None
    return tuple(out)


def array_broadcast_to(value: ArrayCoreValue, shape: Iterable[int]) -> ArrayCoreValue:
    target = tuple(int(dim) for dim in shape)
    diagnostics = list(value.layout.diagnostics)
    broadcast_shape, broadcast_diagnostics = _broadcast_shape(value.shape, target)
    strides = _broadcast_to_strides(value, target)
    if broadcast_diagnostics or broadcast_shape != target or strides is None:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-BROADCAST-TO-SHAPE-MISMATCH",
                "message": "array cannot be broadcast to the requested shape",
                "source_shape": list(value.shape),
                "target_shape": list(target),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(target, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if diagnostics:
        return ArrayCoreValue(
            layout=layout_from_shape(target, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    out_data: list[object] = []
    for out_index in _iter_indices(target):
        out_data.append(value.data[_broadcast_flat_index(value.shape, out_index)])
    contiguous = target == value.shape and value.layout.c_contiguous
    return ArrayCoreValue(
        layout=layout_from_shape(
            target,
            dtype=value.dtype,
            strides=strides,
            c_contiguous=contiguous,
        ),
        data=tuple(out_data),
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_reshape(value: ArrayCoreValue, shape: Iterable[int]) -> ArrayCoreValue:
    target = tuple(int(dim) for dim in shape)
    diagnostics = list(value.layout.diagnostics)
    if _size(target) != value.layout.size:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-RESHAPE-SIZE-MISMATCH",
                "message": "reshape target must have the same number of elements",
                "source_shape": list(value.shape),
                "target_shape": list(target),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(target, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    return ArrayCoreValue(
        layout=layout_from_shape(target, dtype=value.dtype),
        data=value.data,
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_ravel(value: ArrayCoreValue) -> ArrayCoreValue:
    return array_reshape(value, (value.layout.size,))


def array_flatten(value: ArrayCoreValue) -> ArrayCoreValue:
    return ArrayCoreValue(
        layout=layout_from_shape((value.layout.size,), dtype=value.dtype),
        data=tuple(value.data),
        owns_data=True,
        view=False,
        base_shape=None,
    )


def array_transpose(value: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.layout.ndim == 1:
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype),
            data=value.data,
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-TRANSPOSE-RANK-UNSUPPORTED",
                "message": "the current array-core transpose subset supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    rows, cols = value.shape
    out_data = [value.data[row * cols + col] for col in range(cols) for row in range(rows)]
    return ArrayCoreValue(
        layout=layout_from_shape(
            (cols, rows),
            dtype=value.dtype,
            strides=(value.layout.strides[1], value.layout.strides[0]),
            c_contiguous=False,
        ),
        data=tuple(out_data),
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_flip(value: ArrayCoreValue, *, axis: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.layout.ndim == 0:
        return ArrayCoreValue(
            layout=value.layout,
            data=value.data,
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if value.layout.ndim > 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-FLIP-RANK-UNSUPPORTED",
                "message": "flip currently supports scalar/1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    axes: tuple[int, ...]
    if axis is None:
        axes = tuple(range(value.layout.ndim))
    else:
        normalized_axis = _normalize_axis(axis, value.layout.ndim)
        if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                    "message": "axis is out of bounds for array",
                    "axis": axis,
                    "ndim": value.layout.ndim,
                }
            )
            return ArrayCoreValue(
                layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
                data=(),
                owns_data=False,
                view=True,
                base_shape=value.shape,
            )
        axes = (normalized_axis,)
    strides = list(value.layout.strides)
    for flipped_axis in axes:
        strides[flipped_axis] = -strides[flipped_axis]
    if value.layout.ndim == 1:
        out_data = tuple(reversed(value.data))
    else:
        rows, cols = value.shape
        out_data = []
        row_range = range(rows - 1, -1, -1) if 0 in axes else range(rows)
        col_range = range(cols - 1, -1, -1) if 1 in axes else range(cols)
        for row in row_range:
            for col in col_range:
                out_data.append(value.data[row * cols + col])
        out_data = tuple(out_data)
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=value.dtype, strides=tuple(strides), c_contiguous=False),
        data=out_data,
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_squeeze(value: ArrayCoreValue, axis: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if axis is None:
        shape = tuple(dim for dim in value.shape if dim != 1)
    else:
        normalized_axis = _normalize_axis(axis, value.layout.ndim)
        if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                    "message": "axis is out of bounds for array",
                    "axis": axis,
                    "ndim": value.layout.ndim,
                }
            )
            return ArrayCoreValue(
                layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
                data=(),
                owns_data=False,
                view=True,
                base_shape=value.shape,
            )
        if value.shape[normalized_axis] != 1:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-SQUEEZE-AXIS-NOT-ONE",
                    "message": "squeeze axis must have length one",
                    "axis": axis,
                    "axis_size": value.shape[normalized_axis],
                }
            )
            return ArrayCoreValue(
                layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
                data=(),
                owns_data=False,
                view=True,
                base_shape=value.shape,
            )
        shape = value.shape[:normalized_axis] + value.shape[normalized_axis + 1 :]
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=value.dtype),
        data=value.data,
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_expand_dims(value: ArrayCoreValue, axis: int) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    normalized_axis = axis + value.layout.ndim + 1 if axis < 0 else axis
    if normalized_axis < 0 or normalized_axis > value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    shape = value.shape[:normalized_axis] + (1,) + value.shape[normalized_axis:]
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=value.dtype),
        data=value.data,
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_swapaxes(value: ArrayCoreValue, axis1: object, axis2: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if axis2 is None and isinstance(axis1, (list, tuple)):
        axes = tuple(int(axis) for axis in axis1)
    elif axis2 is None:
        axes = (int(axis1),)
    else:
        axes = (int(axis1), int(axis2))
    if len(axes) != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SWAPAXES-AXES-INVALID",
                "message": "swapaxes expects exactly two axes",
                "axes": list(axes),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    normalized = (_normalize_axis(axes[0], value.layout.ndim), _normalize_axis(axes[1], value.layout.ndim))
    if (
        normalized[0] < 0
        or normalized[0] >= value.layout.ndim
        or normalized[1] < 0
        or normalized[1] >= value.layout.ndim
    ):
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axes": list(axes),
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if value.layout.ndim > 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-SWAPAXES-RANK-UNSUPPORTED",
                "message": "swapaxes currently supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if normalized[0] == normalized[1] or value.layout.ndim == 1:
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype),
            data=value.data,
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    rows, cols = value.shape
    out_data = [value.data[row * cols + col] for col in range(cols) for row in range(rows)]
    return ArrayCoreValue(
        layout=layout_from_shape(
            (cols, rows),
            dtype=value.dtype,
            strides=(value.layout.strides[1], value.layout.strides[0]),
            c_contiguous=False,
        ),
        data=tuple(out_data),
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def array_moveaxis(value: ArrayCoreValue, source: object, destination: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if destination is None and isinstance(source, (list, tuple)):
        axes = tuple(int(axis) for axis in source)
    elif destination is None:
        axes = (int(source),)
    else:
        axes = (int(source), int(destination))
    if len(axes) != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-MOVEAXIS-AXES-INVALID",
                "message": "moveaxis expects source,destination axes",
                "axes": list(axes),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    normalized_source = _normalize_axis(axes[0], value.layout.ndim)
    normalized_destination = _normalize_axis(axes[1], value.layout.ndim)
    if (
        normalized_source < 0
        or normalized_source >= value.layout.ndim
        or normalized_destination < 0
        or normalized_destination >= value.layout.ndim
    ):
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axes": list(axes),
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if value.layout.ndim > 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-MOVEAXIS-RANK-UNSUPPORTED",
                "message": "moveaxis currently supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    if normalized_source == normalized_destination or value.layout.ndim == 1:
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype),
            data=value.data,
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    return array_transpose(value)


def array_rot90(value: ArrayCoreValue, k: int = 1) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ROT90-RANK-UNSUPPORTED",
                "message": "rot90 currently supports 2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    turns = int(k) % 4
    if turns == 0:
        return ArrayCoreValue(
            layout=layout_from_shape(
                value.shape,
                dtype=value.dtype,
                strides=value.layout.strides,
                c_contiguous=value.layout.c_contiguous,
            ),
            data=value.data,
            owns_data=False,
            view=True,
            base_shape=value.shape,
        )
    rows, cols = value.shape
    out_data: list[object] = []
    if turns == 1:
        for col in range(cols - 1, -1, -1):
            for row in range(rows):
                out_data.append(value.data[row * cols + col])
        shape = (cols, rows)
        strides = (-value.layout.strides[1], value.layout.strides[0])
    elif turns == 2:
        out_data = list(reversed(value.data))
        shape = value.shape
        strides = (-value.layout.strides[0], -value.layout.strides[1])
    else:
        for col in range(cols):
            for row in range(rows - 1, -1, -1):
                out_data.append(value.data[row * cols + col])
        shape = (cols, rows)
        strides = (value.layout.strides[1], -value.layout.strides[0])
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=value.dtype, strides=strides, c_contiguous=False),
        data=tuple(out_data),
        owns_data=False,
        view=True,
        base_shape=value.shape,
    )


def _normalize_axis(axis: int, ndim: int) -> int:
    return axis + ndim if axis < 0 else axis


def _reduce_values(items: Iterable[object], kind: str) -> object:
    values = list(items)
    if (
        kind in {"sum", "mean"}
        and values
        and all(isinstance(value, float) for value in values)
    ):
        total = float_sum(values)
        return total / len(values) if kind == "mean" else total
    if kind == "sum":
        return sum(values)  # type: ignore[arg-type]
    if kind == "prod":
        return reduce(mul, values, 1)  # type: ignore[arg-type]
    if kind == "any":
        return any(values)
    if kind == "all":
        return all(values)
    if kind == "mean":
        return sum(values) / len(values)  # type: ignore[arg-type]
    if kind == "min":
        return min(values)
    return max(values)


def _reduce_result_dtype(value: ArrayCoreValue, kind: str) -> str:
    if kind in {"any", "all"}:
        return "bool"
    if kind == "mean":
        return "float64"
    if kind in {"sum", "prod"}:
        return "float64" if value.dtype in _FLOAT_DTYPES else "int64"
    return value.dtype


def _arg_reduce_values(items: Iterable[object], kind: str) -> int:
    values = list(items)
    best_index = 0
    best_value = values[0]
    for index, current in enumerate(values[1:], start=1):
        if (kind == "argmin" and current < best_value) or (kind == "argmax" and current > best_value):
            best_index = index
            best_value = current
    return best_index


def array_reduce(
    value: ArrayCoreValue,
    kind: str,
    *,
    axis: int | None = None,
    keepdims: bool = False,
) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if kind not in {"sum", "prod", "min", "max", "mean", "any", "all"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-REDUCE-UNSUPPORTED",
                "message": f"unsupported array-core reduction: {kind}",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-REDUCE-UNSUPPORTED",
                "message": "object-array reductions are not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if not value.data:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-REDUCE-EMPTY",
                "message": "cannot reduce an empty array in the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    dtype = _reduce_result_dtype(value, kind)
    if axis is None:
        result = _reduce_values(value.data, kind)
        shape = tuple(1 for _ in value.shape) if keepdims else ()
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype),
            data=(_coerce_scalar(result, dtype),),
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 1:
        result = _reduce_values(value.data, kind)
        shape = (1,) if keepdims else ()
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype),
            data=(_coerce_scalar(result, dtype),),
        )
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-REDUCE-RANK-UNSUPPORTED",
                "message": "axis reductions currently support 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        for col in range(cols):
            out_data.append(_coerce_scalar(_reduce_values((value.data[row * cols + col] for row in range(rows)), kind), dtype))
        shape = (1, cols) if keepdims else (cols,)
    else:
        for row in range(rows):
            start = row * cols
            out_data.append(_coerce_scalar(_reduce_values(value.data[start : start + cols], kind), dtype))
        shape = (rows, 1) if keepdims else (rows,)
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=dtype),
        data=tuple(out_data),
    )


def array_arg_reduce(
    value: ArrayCoreValue,
    kind: str,
    *,
    axis: int | None = None,
    keepdims: bool = False,
) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    dtype = "int64"
    if kind not in {"argmin", "argmax"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGREDUCE-UNSUPPORTED",
                "message": f"unsupported array-core arg reduction: {kind}",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGREDUCE-UNSUPPORTED",
                "message": "object-array arg reductions are not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if not value.data:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGREDUCE-EMPTY",
                "message": "cannot arg-reduce an empty array in the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if axis is None:
        result = _arg_reduce_values(value.data, kind)
        shape = tuple(1 for _ in value.shape) if keepdims else ()
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype),
            data=(result,),
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 1:
        result = _arg_reduce_values(value.data, kind)
        shape = (1,) if keepdims else ()
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype),
            data=(result,),
        )
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-ARGREDUCE-RANK-UNSUPPORTED",
                "message": "axis arg reductions currently support 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        for col in range(cols):
            out_data.append(_arg_reduce_values((value.data[row * cols + col] for row in range(rows)), kind))
        shape = (1, cols) if keepdims else (cols,)
    else:
        for row in range(rows):
            start = row * cols
            out_data.append(_arg_reduce_values(value.data[start : start + cols], kind))
        shape = (rows, 1) if keepdims else (rows,)
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=dtype),
        data=tuple(out_data),
    )


def _count_nonzero_values(items: Iterable[object]) -> int:
    count = 0
    for item in items:
        if bool(item):
            count += 1
    return count


def array_count_nonzero(
    value: ArrayCoreValue,
    *,
    axis: int | None = None,
    keepdims: bool = False,
) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    dtype = "int64"
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COUNT-NONZERO-UNSUPPORTED",
                "message": "object-array count_nonzero is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if axis is None:
        result = _count_nonzero_values(value.data)
        shape = tuple(1 for _ in value.shape) if keepdims else ()
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype),
            data=(result,),
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 1:
        result = _count_nonzero_values(value.data)
        shape = (1,) if keepdims else ()
        return ArrayCoreValue(
            layout=layout_from_shape(shape, dtype=dtype),
            data=(result,),
        )
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-COUNT-NONZERO-RANK-UNSUPPORTED",
                "message": "axis count_nonzero currently supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        for col in range(cols):
            out_data.append(_count_nonzero_values(value.data[row * cols + col] for row in range(rows)))
        shape = (1, cols) if keepdims else (cols,)
    else:
        for row in range(rows):
            start = row * cols
            out_data.append(_count_nonzero_values(value.data[start : start + cols]))
        shape = (rows, 1) if keepdims else (rows,)
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=dtype),
        data=tuple(out_data),
    )


def array_nonzero(value: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-NONZERO-UNSUPPORTED",
                "message": "object-array nonzero is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((value.layout.ndim, 0), dtype="int64", diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 0:
        if value.data and bool(value.data[0]):
            return ArrayCoreValue(layout=layout_from_shape((1, 1), dtype="int64"), data=(0,))
        return ArrayCoreValue(layout=layout_from_shape((1, 0), dtype="int64"), data=())
    if value.layout.ndim == 1:
        indices = [index for index, item in enumerate(value.data) if bool(item)]
        return ArrayCoreValue(layout=layout_from_shape((1, len(indices)), dtype="int64"), data=tuple(indices))
    if value.layout.ndim == 2:
        rows, cols = value.shape
        row_indices: list[int] = []
        col_indices: list[int] = []
        for row in range(rows):
            for col in range(cols):
                if bool(value.data[row * cols + col]):
                    row_indices.append(row)
                    col_indices.append(col)
        return ArrayCoreValue(
            layout=layout_from_shape((2, len(row_indices)), dtype="int64"),
            data=tuple(row_indices + col_indices),
        )
    diagnostics.append(
        {
            "code": "PCC-ARRAY-NONZERO-RANK-UNSUPPORTED",
            "message": "nonzero currently supports scalar/1D/2D arrays only",
        }
    )
    return ArrayCoreValue(
        layout=layout_from_shape((value.layout.ndim, 0), dtype="int64", diagnostics=diagnostics),
        data=(),
    )


def array_argwhere(value: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ARGWHERE-UNSUPPORTED",
                "message": "object-array argwhere is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((0, value.layout.ndim), dtype="int64", diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 0:
        if value.data and bool(value.data[0]):
            return ArrayCoreValue(layout=layout_from_shape((1, 0), dtype="int64"), data=())
        return ArrayCoreValue(layout=layout_from_shape((0, 0), dtype="int64"), data=())
    if value.layout.ndim == 1:
        indices = [index for index, item in enumerate(value.data) if bool(item)]
        return ArrayCoreValue(layout=layout_from_shape((len(indices), 1), dtype="int64"), data=tuple(indices))
    if value.layout.ndim == 2:
        rows, cols = value.shape
        out_data: list[int] = []
        count = 0
        for row in range(rows):
            for col in range(cols):
                if bool(value.data[row * cols + col]):
                    out_data.extend([row, col])
                    count += 1
        return ArrayCoreValue(
            layout=layout_from_shape((count, 2), dtype="int64"),
            data=tuple(out_data),
        )
    diagnostics.append(
        {
            "code": "PCC-ARRAY-ARGWHERE-RANK-UNSUPPORTED",
            "message": "argwhere currently supports scalar/1D/2D arrays only",
        }
    )
    return ArrayCoreValue(
        layout=layout_from_shape((0, value.layout.ndim), dtype="int64", diagnostics=diagnostics),
        data=(),
    )


def array_flatnonzero(value: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-FLATNONZERO-UNSUPPORTED",
                "message": "object-array flatnonzero is not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((0,), dtype="int64", diagnostics=diagnostics), data=())
    indices = [index for index, item in enumerate(value.data) if bool(item)]
    return ArrayCoreValue(layout=layout_from_shape((len(indices),), dtype="int64"), data=tuple(indices))


def _cumulative_result_dtype(value: ArrayCoreValue) -> str:
    if value.dtype in _FLOAT_DTYPES:
        return value.dtype
    return "int64"


def _cumulative_values(items: Iterable[object], kind: str, dtype: str) -> list[object]:
    out: list[object] = []
    if kind == "cumsum":
        total: object = 0.0 if dtype in _FLOAT_DTYPES else 0
        for item in items:
            total = total + item  # type: ignore[operator]
            out.append(_coerce_scalar(total, dtype))
        return out
    total = 1.0 if dtype in _FLOAT_DTYPES else 1
    for item in items:
        total = total * item  # type: ignore[operator]
        out.append(_coerce_scalar(total, dtype))
    return out


def array_cumulative(
    value: ArrayCoreValue,
    kind: str,
    *,
    axis: int | None = None,
) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics)
    if kind not in {"cumsum", "cumprod"}:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-CUMULATIVE-UNSUPPORTED",
                "message": f"unsupported array-core cumulative operation: {kind}",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.dtype == "object":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-CUMULATIVE-UNSUPPORTED",
                "message": "object-array cumulative operations are not supported by the current array-core subset",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    dtype = _cumulative_result_dtype(value)
    if axis is None:
        out = _cumulative_values(value.data, kind, dtype)
        return ArrayCoreValue(
            layout=layout_from_shape((len(out),), dtype=dtype),
            data=tuple(out),
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 1:
        out = _cumulative_values(value.data, kind, dtype)
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=dtype),
            data=tuple(out),
        )
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-CUMULATIVE-RANK-UNSUPPORTED",
                "message": "axis cumulative operations currently support 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=dtype, diagnostics=diagnostics),
            data=(),
        )
    rows, cols = value.shape
    out_data = [0] * len(value.data)
    if normalized_axis == 0:
        for col in range(cols):
            cumulative = _cumulative_values(
                (value.data[row * cols + col] for row in range(rows)),
                kind,
                dtype,
            )
            for row, item in enumerate(cumulative):
                out_data[row * cols + col] = item
    else:
        for row in range(rows):
            start = row * cols
            cumulative = _cumulative_values(value.data[start : start + cols], kind, dtype)
            out_data[start : start + cols] = cumulative
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=dtype),
        data=tuple(out_data),
    )


def array_take(value: ArrayCoreValue, indices: Iterable[int], *, axis: int | None = None) -> ArrayCoreValue:
    normalized_indices = tuple(int(index) for index in indices)
    diagnostics = list(value.layout.diagnostics)
    if axis is None:
        out_data: list[object] = []
        for index in normalized_indices:
            actual = index + value.layout.size if index < 0 else index
            if actual < 0 or actual >= value.layout.size:
                diagnostics.append(
                    {
                        "code": "PCC-ARRAY-INDEX-OUT-OF-BOUNDS",
                        "message": "array index is out of bounds",
                    }
                )
                return ArrayCoreValue(
                    layout=layout_from_shape((len(normalized_indices),), dtype=value.dtype, diagnostics=diagnostics),
                    data=(),
                )
            out_data.append(value.data[actual])
        return ArrayCoreValue(
            layout=layout_from_shape((len(normalized_indices),), dtype=value.dtype),
            data=tuple(out_data),
        )
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if value.layout.ndim == 1:
        return array_take(value, normalized_indices, axis=None)
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-TAKE-RANK-UNSUPPORTED",
                "message": "take currently supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        for index in normalized_indices:
            actual = index + rows if index < 0 else index
            if actual < 0 or actual >= rows:
                diagnostics.append(
                    {
                        "code": "PCC-ARRAY-INDEX-OUT-OF-BOUNDS",
                        "message": "array index is out of bounds",
                    }
                )
                return ArrayCoreValue(
                    layout=layout_from_shape((len(normalized_indices), cols), dtype=value.dtype, diagnostics=diagnostics),
                    data=(),
                )
            start = actual * cols
            out_data.extend(value.data[start : start + cols])
        shape = (len(normalized_indices), cols)
    else:
        for row in range(rows):
            for index in normalized_indices:
                actual = index + cols if index < 0 else index
                if actual < 0 or actual >= cols:
                    diagnostics.append(
                        {
                            "code": "PCC-ARRAY-INDEX-OUT-OF-BOUNDS",
                            "message": "array index is out of bounds",
                        }
                    )
                    return ArrayCoreValue(
                        layout=layout_from_shape((rows, len(normalized_indices)), dtype=value.dtype, diagnostics=diagnostics),
                        data=(),
                    )
                out_data.append(value.data[row * cols + actual])
        shape = (rows, len(normalized_indices))
    return ArrayCoreValue(
        layout=layout_from_shape(shape, dtype=value.dtype),
        data=tuple(out_data),
    )


def array_put(value: ArrayCoreValue, indices: Iterable[int], replacement: ArrayCoreValue) -> ArrayCoreValue:
    normalized_indices = tuple(int(index) for index in indices)
    diagnostics = list(value.layout.diagnostics) + list(replacement.layout.diagnostics)
    if not replacement.data:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PUT-VALUES-EMPTY",
                "message": "put requires at least one replacement value",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    try:
        replacement_data = tuple(_coerce_scalar(item, value.dtype) for item in replacement.data)
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PUT-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    out_data = list(value.data)
    for offset, index in enumerate(normalized_indices):
        actual = index + value.layout.size if index < 0 else index
        if actual < 0 or actual >= value.layout.size:
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-INDEX-OUT-OF-BOUNDS",
                    "message": "array index is out of bounds",
                }
            )
            return ArrayCoreValue(
                layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics),
                data=(),
            )
        out_data[actual] = replacement_data[offset % len(replacement_data)]
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=value.dtype),
        data=tuple(out_data),
        owns_data=True,
        view=False,
        base_shape=None,
    )


def array_putmask(value: ArrayCoreValue, mask: ArrayCoreValue, replacement: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics) + list(mask.layout.diagnostics) + list(replacement.layout.diagnostics)
    if mask.dtype != "bool":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PUTMASK-MASK-DTYPE-UNSUPPORTED",
                "message": "putmask requires a bool mask",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    if mask.shape != value.shape:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PUTMASK-SHAPE-MISMATCH",
                "message": "putmask mask shape must match the array shape",
                "array_shape": list(value.shape),
                "mask_shape": list(mask.shape),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    if not replacement.data:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PUTMASK-VALUES-EMPTY",
                "message": "putmask requires at least one replacement value",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    try:
        replacement_data = tuple(_coerce_scalar(item, value.dtype) for item in replacement.data)
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-PUTMASK-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    out_data = list(value.data)
    selected = 0
    for index, flag in enumerate(mask.data):
        if bool(flag):
            out_data[index] = replacement_data[selected % len(replacement_data)]
            selected += 1
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=value.dtype),
        data=tuple(out_data),
        owns_data=True,
        view=False,
        base_shape=None,
    )


def array_mask(value: ArrayCoreValue, mask: ArrayCoreValue) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics) + list(mask.layout.diagnostics)
    if mask.dtype != "bool":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-MASK-DTYPE-UNSUPPORTED",
                "message": "boolean mask selection requires a bool mask",
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
            data=(),
        )
    if mask.shape == value.shape:
        out_data = [item for item, flag in zip(value.data, mask.data) if bool(flag)]
        return ArrayCoreValue(
            layout=layout_from_shape((len(out_data),), dtype=value.dtype),
            data=tuple(out_data),
        )
    if value.layout.ndim == 2 and mask.layout.ndim == 1 and mask.shape[0] == value.shape[0]:
        rows, cols = value.shape
        out_data: list[object] = []
        selected_rows = 0
        for row in range(rows):
            if bool(mask.data[row]):
                selected_rows += 1
                start = row * cols
                out_data.extend(value.data[start : start + cols])
        return ArrayCoreValue(
            layout=layout_from_shape((selected_rows, cols), dtype=value.dtype),
            data=tuple(out_data),
        )
    diagnostics.append(
        {
            "code": "PCC-ARRAY-MASK-SHAPE-MISMATCH",
            "message": "boolean mask shape must match the array shape or the leading axis for 2D arrays",
            "array_shape": list(value.shape),
            "mask_shape": list(mask.shape),
        }
    )
    return ArrayCoreValue(
        layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics),
        data=(),
    )


def array_compress(value: ArrayCoreValue, condition: ArrayCoreValue, *, axis: int | None = None) -> ArrayCoreValue:
    diagnostics = list(value.layout.diagnostics) + list(condition.layout.diagnostics)
    if condition.dtype != "bool":
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COMPRESS-MASK-DTYPE-UNSUPPORTED",
                "message": "compress requires a bool condition",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    if condition.layout.ndim != 1:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COMPRESS-SHAPE-MISMATCH",
                "message": "compress condition must be 1D for the current array-core subset",
                "condition_shape": list(condition.shape),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    if axis is None:
        if condition.shape[0] != len(value.data):
            diagnostics.append(
                {
                    "code": "PCC-ARRAY-COMPRESS-SHAPE-MISMATCH",
                    "message": "compress condition length must match the flattened array length",
                    "condition_shape": list(condition.shape),
                    "value_shape": list(value.shape),
                }
            )
            return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
        out_data = [item for item, flag in zip(value.data, condition.data) if bool(flag)]
        return ArrayCoreValue(layout=layout_from_shape((len(out_data),), dtype=value.dtype), data=tuple(out_data))
    normalized_axis = _normalize_axis(axis, value.layout.ndim)
    if normalized_axis < 0 or normalized_axis >= value.layout.ndim:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-AXIS-OUT-OF-BOUNDS",
                "message": "axis is out of bounds for array",
                "axis": axis,
                "ndim": value.layout.ndim,
            }
        )
        return ArrayCoreValue(layout=layout_from_shape((), dtype=value.dtype, diagnostics=diagnostics), data=())
    axis_len = value.shape[normalized_axis]
    if condition.shape[0] != axis_len:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COMPRESS-SHAPE-MISMATCH",
                "message": "compress condition length must match the selected axis length",
                "condition_shape": list(condition.shape),
                "value_shape": list(value.shape),
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    selected = [index for index, flag in enumerate(condition.data) if bool(flag)]
    if value.layout.ndim == 1:
        out_data = [value.data[index] for index in selected]
        return ArrayCoreValue(layout=layout_from_shape((len(out_data),), dtype=value.dtype), data=tuple(out_data))
    if value.layout.ndim != 2:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-COMPRESS-RANK-UNSUPPORTED",
                "message": "compress currently supports 1D/2D arrays only",
            }
        )
        return ArrayCoreValue(layout=layout_from_shape(value.shape, dtype=value.dtype, diagnostics=diagnostics), data=())
    rows, cols = value.shape
    out_data: list[object] = []
    if normalized_axis == 0:
        for row in selected:
            start = row * cols
            out_data.extend(value.data[start : start + cols])
        shape = (len(selected), cols)
    else:
        for row in range(rows):
            for col in selected:
                out_data.append(value.data[row * cols + col])
        shape = (rows, len(selected))
    return ArrayCoreValue(layout=layout_from_shape(shape, dtype=value.dtype), data=tuple(out_data))


def array_astype(value: ArrayCoreValue, dtype: str) -> ArrayCoreValue:
    dtype_name = normalize_dtype(dtype)
    diagnostics = list(value.layout.diagnostics)
    try:
        data = tuple(_coerce_scalar(item, dtype_name) for item in value.data)
    except Exception as exc:
        diagnostics.append(
            {
                "code": "PCC-ARRAY-ASTYPE-FAILED",
                "message": str(exc),
            }
        )
        return ArrayCoreValue(
            layout=layout_from_shape(value.shape, dtype=dtype_name, diagnostics=diagnostics),
            data=(),
        )
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=dtype_name),
        data=data,
        owns_data=True,
        view=False,
        base_shape=None,
    )


def array_copy(value: ArrayCoreValue) -> ArrayCoreValue:
    return ArrayCoreValue(
        layout=layout_from_shape(value.shape, dtype=value.dtype),
        data=tuple(value.data),
        owns_data=True,
        view=False,
        base_shape=None,
    )


def _format_scalar(value: object) -> str:
    if isinstance(value, str):
        return repr(value)
    return repr(value)


def _format_nested(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_format_nested(child) for child in value) + "]"
    return _format_scalar(value)


def array_core_repr(value: ArrayCoreValue) -> str:
    if value.layout.ndim == 2:
        rows = value.as_nested_list()
        if isinstance(rows, list):
            rendered = ",\n       ".join(_format_nested(row) for row in rows)
            return "array([" + rendered + "])"
    return "array(" + _format_nested(value.as_nested_list()) + ")"
