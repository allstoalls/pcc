from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pcc1_gate import repo_root

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

from pcc.package.array_model import (
    array_arange,
    array_arg_reduce,
    array_argwhere,
    array_argpartition,
    array_argsort,
    array_astype,
    array_binary_op,
    array_broadcast_to,
    array_clip,
    array_compare,
    array_compress,
    array_concatenate,
    array_count_nonzero,
    array_copy,
    array_cumulative,
    array_diagonal,
    array_eye,
    array_expand_dims,
    array_flatten,
    array_flatnonzero,
    array_flip,
    array_full_like_from_literal,
    array_full_from_literal,
    array_index,
    array_linspace,
    array_mask,
    array_matmul,
    array_moveaxis,
    array_nonzero,
    array_ones,
    array_ones_like,
    array_put,
    array_putmask,
    array_ravel,
    array_reduce,
    array_repeat,
    array_reshape,
    array_roll,
    array_rot90,
    array_searchsorted,
    array_sort,
    array_partition,
    array_squeeze,
    array_stack,
    array_swapaxes,
    array_take,
    array_transpose,
    array_unary_op,
    array_where,
    array_tile,
    array_zeros,
    array_zeros_like,
    dtype_format,
    dtype_range,
    infer_array_layout,
    infer_literal_layout,
    layout_from_shape,
    value_from_literal,
)
from pcc.package.array_core import array_core_report


REPO = repo_root()
def _find_current_pcc1() -> Path | None:
    return find_current_pcc1(REPO)


def test_array_core_infers_shape_strides_and_dtype():
    layout = infer_array_layout([[1, 2, 3], [4, 5, 6]])
    assert layout.ok is True
    assert layout.shape == (2, 3)
    assert layout.ndim == 2
    assert layout.dtype == "int64"
    assert layout.itemsize == 8
    assert layout.strides == (24, 8)
    assert layout.size == 6
    assert layout.nbytes == 48

    assert infer_array_layout([True, False]).dtype == "bool"
    assert infer_array_layout([1, 2.5]).dtype == "float64"
    assert layout_from_shape((4, 0, 2), dtype="float64").strides == (16, 16, 8)
    assert layout_from_shape((2, 3), dtype="int32").itemsize == 4
    assert layout_from_shape((2, 3), dtype="uint8").nbytes == 6
    assert layout_from_shape((2, 3), dtype="float32").strides == (12, 4)
    assert dtype_format("uint8") == "B"
    assert dtype_range("int8") == (-128, 127)
    assert dtype_range("uint8") == (0, 255)


def test_array_core_reports_ragged_layout_without_claiming_ok():
    layout = infer_literal_layout("[[1, 2], [3]]", require_rectangular=True)
    assert layout.ok is False
    codes = [diag["code"] for diag in layout.diagnostics]
    assert "PCC-ARRAY-RAGGED" in codes
    assert "PCC-ARRAY-REQUIRES-RECTANGULAR" in codes
    assert layout.shape == (2,)
    assert layout.dtype == "object"


def test_array_core_report_is_json_ready():
    report = array_core_report(literal="[[1,2,3],[4,5,6]]")
    assert report["schema"] == "pcc.array-core.v1"
    assert report["source"] == "literal"
    assert report["shape"] == [2, 3]
    assert report["strides"] == [24, 8]
    assert report["dtype"] == "int64"


def test_array_core_value_index_broadcast_ufunc_and_repr():
    value = value_from_literal("[[1,2,3],[4,5,6]]")
    assert value.ok is True
    assert value.shape == (2, 3)
    assert value.data == (1, 2, 3, 4, 5, 6)
    assert value.as_dict()["repr"] == "array([[1, 2, 3],\n       [4, 5, 6]])"

    column = array_index(value, ":,1")
    assert column.ok is True
    assert column.shape == (2,)
    assert column.data == (2, 5)

    stepped_columns = array_index(value, ":,::2")
    assert stepped_columns.ok is True
    assert stepped_columns.shape == (2, 2)
    assert stepped_columns.as_nested_list() == [[1, 3], [4, 6]]

    reversed_all = array_index(value, "::-1,::-1")
    assert reversed_all.ok is True
    assert reversed_all.shape == (2, 3)
    assert reversed_all.as_nested_list() == [[6, 5, 4], [3, 2, 1]]

    newaxis_column = array_index(value, ":,None,1")
    assert newaxis_column.ok is True
    assert newaxis_column.shape == (2, 1)
    assert newaxis_column.as_nested_list() == [[2], [5]]

    ellipsis_column = array_index(value, "...,1")
    assert ellipsis_column.ok is True
    assert ellipsis_column.shape == (2,)
    assert ellipsis_column.data == (2, 5)

    vector_newaxis = array_index(value_from_literal("[1,2,3]"), "None,...")
    assert vector_newaxis.ok is True
    assert vector_newaxis.shape == (1, 3)
    assert vector_newaxis.as_nested_list() == [[1, 2, 3]]

    bad_ellipsis = array_index(value, "...,...")
    assert bad_ellipsis.ok is False
    assert bad_ellipsis.layout.diagnostics[0]["code"] == "PCC-ARRAY-INDEX-PARSE-FAILED"

    bad_step = array_index(value, "::0,:")
    assert bad_step.ok is False
    assert bad_step.layout.diagnostics[0]["code"] == "PCC-ARRAY-INDEX-PARSE-FAILED"

    diagonal = array_diagonal(value)
    assert diagonal.ok is True
    assert diagonal.shape == (2,)
    assert diagonal.data == (1, 5)
    assert diagonal.view is True
    assert diagonal.owns_data is False
    assert diagonal.base_shape == (2, 3)
    assert diagonal.layout.strides == (32,)
    assert array_diagonal(value, offset=1).data == (2, 6)
    assert array_diagonal(value, offset=-1).data == (4,)

    summed = array_binary_op(value, value_from_literal("[10,20,30]"), "add")
    assert summed.ok is True
    assert summed.shape == (2, 3)
    assert summed.dtype == "int64"
    assert summed.as_nested_list() == [[11, 22, 33], [14, 25, 36]]

    divided = array_binary_op(value_from_literal("[2,4]"), value_from_literal("2"), "div")
    assert divided.ok is True
    assert divided.dtype == "float64"
    assert divided.data == (1.0, 2.0)

    bad = array_binary_op(value_from_literal("[1,2]"), value_from_literal("[1,2,3]"), "add")
    assert bad.ok is False
    assert bad.layout.diagnostics[0]["code"] == "PCC-ARRAY-BROADCAST-INCOMPATIBLE"


def test_array_core_transforms_reductions_and_ownership_metadata():
    value = value_from_literal("[[1,2,3],[4,5,6]]")

    reshaped = array_reshape(value, (3, 2))
    assert reshaped.ok is True
    assert reshaped.shape == (3, 2)
    assert reshaped.as_nested_list() == [[1, 2], [3, 4], [5, 6]]
    assert reshaped.view is True
    assert reshaped.owns_data is False
    assert reshaped.base_shape == (2, 3)

    raveled = array_ravel(value)
    assert raveled.shape == (6,)
    assert raveled.data == (1, 2, 3, 4, 5, 6)
    assert raveled.view is True

    flattened = array_flatten(value)
    assert flattened.shape == (6,)
    assert flattened.data == (1, 2, 3, 4, 5, 6)
    assert flattened.view is False
    assert flattened.owns_data is True
    assert flattened.base_shape is None

    transposed = array_transpose(value)
    assert transposed.shape == (3, 2)
    assert transposed.as_nested_list() == [[1, 4], [2, 5], [3, 6]]
    assert transposed.layout.c_contiguous is False
    assert transposed.layout.strides == (8, 24)

    swapped = array_swapaxes(value, 0, 1)
    assert swapped.shape == (3, 2)
    assert swapped.as_nested_list() == [[1, 4], [2, 5], [3, 6]]
    assert swapped.view is True
    assert swapped.owns_data is False
    assert swapped.base_shape == (2, 3)
    assert swapped.layout.strides == (8, 24)

    swapped_1d = array_swapaxes(value_from_literal("[1,2,3]"), 0, 0)
    assert swapped_1d.shape == (3,)
    assert swapped_1d.data == (1, 2, 3)
    assert swapped_1d.view is True

    moved = array_moveaxis(value, 0, 1)
    assert moved.shape == (3, 2)
    assert moved.as_nested_list() == [[1, 4], [2, 5], [3, 6]]
    assert moved.view is True
    assert moved.owns_data is False
    assert moved.base_shape == (2, 3)
    assert moved.layout.strides == (8, 24)

    moved_1d = array_moveaxis(value_from_literal("[1,2,3]"), 0, 0)
    assert moved_1d.shape == (3,)
    assert moved_1d.data == (1, 2, 3)
    assert moved_1d.view is True

    rotated = array_rot90(value)
    assert rotated.shape == (3, 2)
    assert rotated.as_nested_list() == [[3, 6], [2, 5], [1, 4]]
    assert rotated.view is True
    assert rotated.owns_data is False
    assert rotated.base_shape == (2, 3)
    assert rotated.layout.strides == (-8, 24)

    rotated_twice = array_rot90(value, 2)
    assert rotated_twice.shape == (2, 3)
    assert rotated_twice.as_nested_list() == [[6, 5, 4], [3, 2, 1]]
    assert rotated_twice.layout.strides == (-24, -8)

    rotated_clockwise = array_rot90(value, -1)
    assert rotated_clockwise.shape == (3, 2)
    assert rotated_clockwise.as_nested_list() == [[4, 1], [5, 2], [6, 3]]
    assert rotated_clockwise.layout.strides == (8, -24)

    flipped_all = array_flip(value)
    assert flipped_all.shape == (2, 3)
    assert flipped_all.as_nested_list() == [[6, 5, 4], [3, 2, 1]]
    assert flipped_all.view is True
    assert flipped_all.owns_data is False
    assert flipped_all.base_shape == (2, 3)
    assert flipped_all.layout.strides == (-24, -8)

    flipped_axis1 = array_flip(value, axis=1)
    assert flipped_axis1.as_nested_list() == [[3, 2, 1], [6, 5, 4]]
    assert flipped_axis1.layout.strides == (24, -8)

    rolled_flat = array_roll(value, 2)
    assert rolled_flat.shape == (2, 3)
    assert rolled_flat.as_nested_list() == [[5, 6, 1], [2, 3, 4]]
    assert rolled_flat.owns_data is True
    assert rolled_flat.view is False

    rolled_axis0 = array_roll(value, 1, axis=0)
    assert rolled_axis0.as_nested_list() == [[4, 5, 6], [1, 2, 3]]

    rolled_axis1 = array_roll(value, -1, axis=1)
    assert rolled_axis1.as_nested_list() == [[2, 3, 1], [5, 6, 4]]

    squeezed = array_squeeze(value_from_literal("[[[1,2,3]]]", dtype="int64"))
    assert squeezed.shape == (3,)
    assert squeezed.data == (1, 2, 3)
    assert squeezed.view is True
    assert squeezed.base_shape == (1, 1, 3)

    squeezed_axis = array_squeeze(value_from_literal("[[1,2,3]]"), axis=0)
    assert squeezed_axis.shape == (3,)

    bad_squeeze = array_squeeze(value, axis=0)
    assert bad_squeeze.ok is False
    assert bad_squeeze.layout.diagnostics[0]["code"] == "PCC-ARRAY-SQUEEZE-AXIS-NOT-ONE"

    expanded = array_expand_dims(value, axis=1)
    assert expanded.shape == (2, 1, 3)
    assert expanded.data == value.data
    assert expanded.view is True
    assert expanded.base_shape == (2, 3)

    assert array_reduce(value, "sum").data == (21,)
    assert array_reduce(value, "prod").data == (720,)
    assert array_reduce(value, "min").data == (1,)
    assert array_reduce(value, "max").data == (6,)
    assert array_reduce(value, "mean").dtype == "float64"
    assert array_reduce(value, "mean").data == (3.5,)
    assert array_reduce(value_from_literal("[0,1,0]"), "any").data == (True,)
    assert array_reduce(value_from_literal("[1,2,3]"), "all").data == (True,)
    assert array_reduce(value, "sum", axis=0).as_nested_list() == [5, 7, 9]
    assert array_reduce(value, "prod", axis=0).as_nested_list() == [4, 10, 18]
    assert array_reduce(value, "mean", axis=0).as_nested_list() == [2.5, 3.5, 4.5]
    assert array_reduce(value, "max", axis=1, keepdims=True).as_nested_list() == [[3], [6]]
    assert array_reduce(value, "prod", axis=1, keepdims=True).as_nested_list() == [[6], [120]]
    assert array_reduce(value, "mean", axis=1, keepdims=True).as_nested_list() == [[2.0], [5.0]]
    truthy = value_from_literal("[[0,1,0],[0,0,0]]")
    assert array_reduce(truthy, "any", axis=1).as_nested_list() == [True, False]
    assert array_reduce(truthy, "all", axis=1, keepdims=True).as_nested_list() == [[False], [False]]
    assert array_arg_reduce(value_from_literal("[3,1,2]"), "argmin").data == (1,)
    assert array_arg_reduce(value_from_literal("[3,1,2]"), "argmax").data == (0,)
    assert array_arg_reduce(value, "argmax", axis=0).as_nested_list() == [1, 1, 1]
    assert array_arg_reduce(value, "argmin", axis=1, keepdims=True).as_nested_list() == [[0], [0]]
    assert array_count_nonzero(value_from_literal("[0,1,2,0]")).data == (2,)
    assert array_count_nonzero(value_from_literal("[[0,1,0],[2,0,3]]"), axis=1).as_nested_list() == [1, 2]
    assert array_count_nonzero(value_from_literal("[[0,1,0],[2,0,3]]"), axis=0, keepdims=True).as_nested_list() == [[1, 1, 1]]
    assert array_nonzero(value_from_literal("[0,1,2,0]")).as_nested_list() == [[1, 2]]
    assert array_nonzero(value_from_literal("[[0,1,0],[2,0,3]]")).as_nested_list() == [[0, 1, 1], [1, 0, 2]]
    assert array_nonzero(value_from_literal("0")).shape == (1, 0)
    assert array_argwhere(value_from_literal("[0,1,2,0]")).as_nested_list() == [[1], [2]]
    assert array_argwhere(value_from_literal("[[0,1,0],[2,0,3]]")).as_nested_list() == [[0, 1], [1, 0], [1, 2]]
    assert array_argwhere(value_from_literal("0")).shape == (0, 0)
    assert array_argwhere(value_from_literal("1")).shape == (1, 0)
    assert array_flatnonzero(value_from_literal("[[0,1,0],[2,0,3]]")).data == (1, 3, 5)
    assert array_flatnonzero(value_from_literal("0")).shape == (0,)
    assert array_flatnonzero(value_from_literal("1")).data == (0,)
    assert array_cumulative(value, "cumsum").shape == (6,)
    assert array_cumulative(value, "cumsum").data == (1, 3, 6, 10, 15, 21)
    assert array_cumulative(value, "cumsum", axis=0).as_nested_list() == [[1, 2, 3], [5, 7, 9]]
    assert array_cumulative(value, "cumprod", axis=1).as_nested_list() == [[1, 2, 6], [4, 20, 120]]
    assert array_sort(value_from_literal("[3,1,2]")).data == (1, 2, 3)
    assert array_sort(value_from_literal("[[3,1,2],[6,5,4]]")).as_nested_list() == [[1, 2, 3], [4, 5, 6]]
    assert array_sort(value_from_literal("[[4,2,6],[1,5,3]]"), axis=0).as_nested_list() == [[1, 2, 3], [4, 5, 6]]
    assert array_argsort(value_from_literal("[3,1,2]")).data == (1, 2, 0)
    assert array_argsort(value_from_literal("[[3,1,2],[6,5,4]]")).as_nested_list() == [[1, 2, 0], [2, 1, 0]]
    assert array_argsort(value_from_literal("[[4,2,6],[1,5,3]]"), axis=0).as_nested_list() == [[1, 0, 1], [0, 1, 0]]
    assert array_searchsorted(value_from_literal("[1,3,5]"), value_from_literal("4")).data == (2,)
    assert array_searchsorted(value_from_literal("[1,3,5]"), value_from_literal("[0,3,7]")).data == (0, 1, 3)
    assert array_searchsorted(value_from_literal("[1,3,5]"), value_from_literal("[0,3,7]"), side="right").data == (
        0,
        2,
        3,
    )
    assert array_partition(value_from_literal("[3,1,2]"), 1).data == (1, 2, 3)
    assert array_partition(value_from_literal("[[4,2,6],[1,5,3]]"), 1, axis=0).as_nested_list() == [[1, 2, 3], [4, 5, 6]]
    assert array_argpartition(value_from_literal("[3,1,2]"), 1).data == (1, 2, 0)
    assert array_argpartition(value_from_literal("[[4,2,6],[1,5,3]]"), 1, axis=0).as_nested_list() == [
        [1, 0, 1],
        [0, 1, 0],
    ]

    casted = array_astype(value, "float64")
    assert casted.dtype == "float64"
    assert casted.data == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert casted.owns_data is True
    assert casted.view is False

    copied = array_copy(transposed)
    assert copied.as_nested_list() == [[1, 4], [2, 5], [3, 6]]
    assert copied.owns_data is True
    assert copied.view is False

    bad = array_reshape(value, (4, 2))
    assert bad.ok is False
    assert bad.layout.diagnostics[0]["code"] == "PCC-ARRAY-RESHAPE-SIZE-MISMATCH"

    taken_rows = array_take(value, [1, 0], axis=0)
    assert taken_rows.shape == (2, 3)
    assert taken_rows.as_nested_list() == [[4, 5, 6], [1, 2, 3]]

    taken_cols = array_take(value, [2, 0], axis=1)
    assert taken_cols.shape == (2, 2)
    assert taken_cols.as_nested_list() == [[3, 1], [6, 4]]

    put_value = array_put(value, [0, -1, 2], value_from_literal("[9,8]"))
    assert put_value.shape == (2, 3)
    assert put_value.as_nested_list() == [[9, 2, 9], [4, 5, 8]]
    assert put_value.owns_data is True
    assert put_value.view is False

    putmask_value = array_putmask(
        value,
        value_from_literal("[[True,False,True],[False,True,False]]", dtype="bool"),
        value_from_literal("[9,8]"),
    )
    assert putmask_value.shape == (2, 3)
    assert putmask_value.as_nested_list() == [[9, 2, 8], [4, 9, 6]]
    assert putmask_value.owns_data is True
    assert putmask_value.view is False

    bad_axis = array_reduce(value, "sum", axis=3)
    assert bad_axis.ok is False
    assert bad_axis.layout.diagnostics[0]["code"] == "PCC-ARRAY-AXIS-OUT-OF-BOUNDS"

    masked_flat = array_mask(value, value_from_literal("[[True,False,True],[False,True,False]]"))
    assert masked_flat.shape == (3,)
    assert masked_flat.data == (1, 3, 5)

    masked_rows = array_mask(value, value_from_literal("[False, True]"))
    assert masked_rows.shape == (1, 3)
    assert masked_rows.as_nested_list() == [[4, 5, 6]]

    compressed_flat = array_compress(value, value_from_literal("[True,False,True,False,True,False]", dtype="bool"))
    assert compressed_flat.shape == (3,)
    assert compressed_flat.data == (1, 3, 5)

    compressed_rows = array_compress(value, value_from_literal("[False, True]", dtype="bool"), axis=0)
    assert compressed_rows.shape == (1, 3)
    assert compressed_rows.as_nested_list() == [[4, 5, 6]]

    compressed_cols = array_compress(value, value_from_literal("[True, False, True]", dtype="bool"), axis=1)
    assert compressed_cols.shape == (2, 2)
    assert compressed_cols.as_nested_list() == [[1, 3], [4, 6]]

    object_value = value_from_literal("['a', 'b']", dtype="object")
    assert object_value.as_dict()["object_policy"]["allowed"] == [
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
    ]
    object_op = array_binary_op(object_value, value_from_literal("['c', 'd']", dtype="object"), "add")
    assert object_op.ok is False
    assert object_op.layout.diagnostics[0]["code"] == "PCC-ARRAY-OBJECT-UFUNC-UNSUPPORTED"

    compared = array_compare(value, value_from_literal("[2,5,4]"), "gt")
    assert compared.dtype == "bool"
    assert compared.shape == (2, 3)
    assert compared.as_nested_list() == [[False, False, False], [True, False, True]]

    selected = array_where(
        value_from_literal("[[True,False,True],[False,True,False]]"),
        value,
        value_from_literal("0"),
    )
    assert selected.shape == (2, 3)
    assert selected.as_nested_list() == [[1, 0, 3], [0, 5, 0]]

    uint8_value = value_from_literal("[255,256,-1]", dtype="uint8")
    assert uint8_value.dtype == "uint8"
    assert uint8_value.data == (255, 0, 255)
    assert uint8_value.as_dict()["dtype_range"] == [0, 255]
    assert uint8_value.as_dict()["dtype_signed"] is False

    int8_value = array_astype(value_from_literal("[128,129,-129]"), "int8")
    assert int8_value.dtype == "int8"
    assert int8_value.data == (-128, -127, 127)

    wrapped = array_binary_op(value_from_literal("[255]", dtype="uint8"), value_from_literal("[1]", dtype="uint8"), "add")
    assert wrapped.dtype == "uint8"
    assert wrapped.data == (0,)

    negated = array_unary_op(value_from_literal("[1,-2,3]"), "neg")
    assert negated.dtype == "int64"
    assert negated.data == (-1, 2, -3)

    logical_not = array_unary_op(value_from_literal("[True,False,0,2]"), "logical_not")
    assert logical_not.dtype == "bool"
    assert logical_not.data == (False, True, True, False)

    object_unary = array_unary_op(object_value, "abs")
    assert object_unary.ok is False
    assert object_unary.layout.diagnostics[0]["code"] == "PCC-ARRAY-OBJECT-UNARY-UNSUPPORTED"

    clipped = array_clip(value_from_literal("[-2,0,3,9]"), "0,5")
    assert clipped.ok is True
    assert clipped.dtype == "int64"
    assert clipped.data == (0, 0, 3, 5)

    clipped_float = array_clip(value_from_literal("[0,2,4]"), "1.5,3.5")
    assert clipped_float.ok is True
    assert clipped_float.dtype == "float64"
    assert clipped_float.data == (1.5, 2.0, 3.5)

    object_clip = array_clip(object_value, "0,1")
    assert object_clip.ok is False
    assert object_clip.layout.diagnostics[0]["code"] == "PCC-ARRAY-OBJECT-CLIP-UNSUPPORTED"

    broadcasted = array_broadcast_to(value_from_literal("[1,2,3]"), (2, 3))
    assert broadcasted.ok is True
    assert broadcasted.shape == (2, 3)
    assert broadcasted.layout.strides == (0, 8)
    assert broadcasted.as_nested_list() == [[1, 2, 3], [1, 2, 3]]
    assert broadcasted.view is True
    assert broadcasted.owns_data is False
    assert broadcasted.base_shape == (3,)

    bad_broadcast_to = array_broadcast_to(value_from_literal("[1,2]"), (2, 3))
    assert bad_broadcast_to.ok is False
    assert bad_broadcast_to.layout.diagnostics[0]["code"] == "PCC-ARRAY-BROADCAST-TO-SHAPE-MISMATCH"

    repeated_flat = array_repeat(value_from_literal("[1,2,3]"), 2)
    assert repeated_flat.shape == (6,)
    assert repeated_flat.data == (1, 1, 2, 2, 3, 3)

    repeated_cols = array_repeat(value, 2, axis=1)
    assert repeated_cols.shape == (2, 6)
    assert repeated_cols.as_nested_list() == [[1, 1, 2, 2, 3, 3], [4, 4, 5, 5, 6, 6]]

    tiled_vector = array_tile(value_from_literal("[1,2]"), (3,))
    assert tiled_vector.shape == (6,)
    assert tiled_vector.data == (1, 2, 1, 2, 1, 2)

    tiled_matrix = array_tile(value_from_literal("[[1,2],[3,4]]"), (2, 1))
    assert tiled_matrix.shape == (4, 2)
    assert tiled_matrix.as_nested_list() == [[1, 2], [3, 4], [1, 2], [3, 4]]

    bad_repeat = array_repeat(value, -1)
    assert bad_repeat.ok is False
    assert bad_repeat.layout.diagnostics[0]["code"] == "PCC-ARRAY-REPEAT-NEGATIVE"

    filled = array_full_from_literal((2, 3), "7", dtype="int16")
    assert filled.shape == (2, 3)
    assert filled.dtype == "int16"
    assert filled.as_nested_list() == [[7, 7, 7], [7, 7, 7]]

    ranged = array_arange("1,6,2")
    assert ranged.shape == (3,)
    assert ranged.data == (1, 3, 5)

    ranged_float = array_arange("0,1,0.25")
    assert ranged_float.shape == (4,)
    assert ranged_float.dtype == "float64"
    assert ranged_float.data == (0.0, 0.25, 0.5, 0.75)

    zeros = array_zeros((2, 2), dtype="int32")
    assert zeros.dtype == "int32"
    assert zeros.as_nested_list() == [[0, 0], [0, 0]]

    ones = array_ones((3,), dtype="uint8")
    assert ones.dtype == "uint8"
    assert ones.data == (1, 1, 1)

    zeros_like = array_zeros_like(value_from_literal("[[1,2],[3,4]]", dtype="uint8"))
    assert zeros_like.dtype == "uint8"
    assert zeros_like.shape == (2, 2)
    assert zeros_like.as_nested_list() == [[0, 0], [0, 0]]
    assert zeros_like.owns_data is True
    assert zeros_like.view is False

    ones_like = array_ones_like(value_from_literal("[1.5,2.5]"))
    assert ones_like.dtype == "float64"
    assert ones_like.data == (1.0, 1.0)

    full_like = array_full_like_from_literal(value, "9", dtype="int16")
    assert full_like.dtype == "int16"
    assert full_like.shape == (2, 3)
    assert full_like.as_nested_list() == [[9, 9, 9], [9, 9, 9]]

    eye = array_eye("2,3,1", dtype="int64")
    assert eye.shape == (2, 3)
    assert eye.as_nested_list() == [[0, 1, 0], [0, 0, 1]]

    linspace = array_linspace("0,1,5")
    assert linspace.dtype == "float64"
    assert linspace.data == (0.0, 0.25, 0.5, 0.75, 1.0)

    concat_rows = array_concatenate(value, value_from_literal("[[7,8,9]]"), axis=0)
    assert concat_rows.shape == (3, 3)
    assert concat_rows.as_nested_list() == [[1, 2, 3], [4, 5, 6], [7, 8, 9]]

    concat_cols = array_concatenate(value, value_from_literal("[[7],[8]]"), axis=1)
    assert concat_cols.shape == (2, 4)
    assert concat_cols.as_nested_list() == [[1, 2, 3, 7], [4, 5, 6, 8]]

    stacked = array_stack(value_from_literal("[1,2,3]"), value_from_literal("[4,5,6]"), axis=1)
    assert stacked.shape == (3, 2)
    assert stacked.as_nested_list() == [[1, 4], [2, 5], [3, 6]]

    bad_concat = array_concatenate(value, value_from_literal("[[7,8]]"), axis=0)
    assert bad_concat.ok is False
    assert bad_concat.layout.diagnostics[0]["code"] == "PCC-ARRAY-CONCAT-SHAPE-MISMATCH"

    dot = array_matmul(value_from_literal("[1,2,3]"), value_from_literal("[4,5,6]"))
    assert dot.shape == ()
    assert dot.data == (32,)

    matvec = array_matmul(value, value_from_literal("[7,8,9]"))
    assert matvec.shape == (2,)
    assert matvec.data == (50, 122)

    matmat = array_matmul(value, value_from_literal("[[7,8],[9,10],[11,12]]"))
    assert matmat.shape == (2, 2)
    assert matmat.as_nested_list() == [[58, 64], [139, 154]]

    bad_matmul = array_matmul(value, value_from_literal("[1,2]"))
    assert bad_matmul.ok is False
    assert bad_matmul.layout.diagnostics[0]["code"] == "PCC-ARRAY-MATMUL-SHAPE-MISMATCH"


def test_pcc_package_array_core_cli(tmp_path):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["shape"] == [2, 3]
    assert report["strides"] == [24, 8]
    assert report["dtype"] == "int64"

    bad = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3]]",
            "--require-rectangular",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad.returncode == 2
    bad_report = json.loads(bad.stdout)
    assert bad_report["ok"] is False
    assert bad_report["diagnostics"][0]["code"] == "PCC-ARRAY-RAGGED"

    op = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--op",
            "add",
            "--rhs",
            "[10,20,30]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    op_report = json.loads(op.stdout)
    assert op_report["ok"] is True
    assert op_report["shape"] == [2, 3]
    assert op_report["data"] == [[11, 22, 33], [14, 25, 36]]
    assert op_report["repr"] == "array([[11, 22, 33],\n       [14, 25, 36]])"

    unary = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,-2,3]",
            "--unary",
            "abs",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    unary_report = json.loads(unary.stdout)
    assert unary_report["ok"] is True
    assert unary_report["unary"] == "abs"
    assert unary_report["data"] == [1, 2, 3]

    unary_bool = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[True,False,0,2]",
            "--unary",
            "logical_not",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    unary_bool_report = json.loads(unary_bool.stdout)
    assert unary_bool_report["ok"] is True
    assert unary_bool_report["dtype"] == "bool"
    assert unary_bool_report["data"] == [False, True, True, False]

    clipped = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[-2,0,3,9]",
            "--clip",
            "0,5",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    clipped_report = json.loads(clipped.stdout)
    assert clipped_report["ok"] is True
    assert clipped_report["clip"] == "0,5"
    assert clipped_report["dtype"] == "int64"
    assert clipped_report["data"] == [0, 0, 3, 5]

    clipped_float = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[0,2,4]",
            "--clip",
            "1.5,3.5",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    clipped_float_report = json.loads(clipped_float.stdout)
    assert clipped_float_report["ok"] is True
    assert clipped_float_report["clip"] == "1.5,3.5"
    assert clipped_float_report["dtype"] == "float64"
    assert clipped_float_report["data"] == [1.5, 2.0, 3.5]

    bad_unary = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[True,False]",
            "--unary",
            "neg",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_unary.returncode == 2
    bad_unary_report = json.loads(bad_unary.stdout)
    assert bad_unary_report["diagnostics"][0]["code"] == "PCC-ARRAY-UNARY-DTYPE-UNSUPPORTED"

    broadcast_to = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--broadcast-to",
            "2,3",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    broadcast_to_report = json.loads(broadcast_to.stdout)
    assert broadcast_to_report["ok"] is True
    assert broadcast_to_report["broadcast_to"] == "2,3"
    assert broadcast_to_report["shape"] == [2, 3]
    assert broadcast_to_report["strides"] == [0, 8]
    assert broadcast_to_report["data"] == [[1, 2, 3], [1, 2, 3]]
    assert broadcast_to_report["view"] is True
    assert broadcast_to_report["base_shape"] == [3]

    bad_broadcast_to = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2]",
            "--broadcast-to",
            "2,3",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_broadcast_to.returncode == 2
    bad_broadcast_to_report = json.loads(bad_broadcast_to.stdout)
    assert bad_broadcast_to_report["diagnostics"][0]["code"] == "PCC-ARRAY-BROADCAST-TO-SHAPE-MISMATCH"

    repeated = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--repeat",
            "2",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    repeated_report = json.loads(repeated.stdout)
    assert repeated_report["ok"] is True
    assert repeated_report["repeat"] == 2
    assert repeated_report["axis"] == 1
    assert repeated_report["shape"] == [2, 6]
    assert repeated_report["data"] == [[1, 1, 2, 2, 3, 3], [4, 4, 5, 5, 6, 6]]

    tiled = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3,4]]",
            "--tile",
            "2,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    tiled_report = json.loads(tiled.stdout)
    assert tiled_report["ok"] is True
    assert tiled_report["tile"] == "2,1"
    assert tiled_report["shape"] == [4, 2]
    assert tiled_report["data"] == [[1, 2], [3, 4], [1, 2], [3, 4]]

    rolled = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--roll",
            "2",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    rolled_report = json.loads(rolled.stdout)
    assert rolled_report["ok"] is True
    assert rolled_report["roll"] == 2
    assert rolled_report["shape"] == [2, 3]
    assert rolled_report["data"] == [[5, 6, 1], [2, 3, 4]]
    assert rolled_report["owns_data"] is True
    assert rolled_report["view"] is False

    rolled_axis1 = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--roll",
            "-1",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    rolled_axis1_report = json.loads(rolled_axis1.stdout)
    assert rolled_axis1_report["ok"] is True
    assert rolled_axis1_report["roll"] == -1
    assert rolled_axis1_report["axis"] == 1
    assert rolled_axis1_report["data"] == [[2, 3, 1], [5, 6, 4]]

    bad_repeat = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2]",
            "--repeat",
            "-1",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_repeat.returncode == 2
    bad_repeat_report = json.loads(bad_repeat.stdout)
    assert bad_repeat_report["diagnostics"][0]["code"] == "PCC-ARRAY-REPEAT-NEGATIVE"

    indexed = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            ":,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    index_report = json.loads(indexed.stdout)
    assert index_report["ok"] is True
    assert index_report["shape"] == [2]
    assert index_report["data"] == [2, 5]

    stepped_index = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            ":,::2",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    stepped_index_report = json.loads(stepped_index.stdout)
    assert stepped_index_report["ok"] is True
    assert stepped_index_report["shape"] == [2, 2]
    assert stepped_index_report["data"] == [[1, 3], [4, 6]]

    reversed_index = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            "::-1,::-1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    reversed_index_report = json.loads(reversed_index.stdout)
    assert reversed_index_report["ok"] is True
    assert reversed_index_report["shape"] == [2, 3]
    assert reversed_index_report["data"] == [[6, 5, 4], [3, 2, 1]]

    bad_step_index = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            "::0,:",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_step_index.returncode == 2
    bad_step_index_report = json.loads(bad_step_index.stdout)
    assert bad_step_index_report["diagnostics"][0]["code"] == "PCC-ARRAY-INDEX-PARSE-FAILED"

    newaxis_index = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            ":,None,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    newaxis_index_report = json.loads(newaxis_index.stdout)
    assert newaxis_index_report["ok"] is True
    assert newaxis_index_report["shape"] == [2, 1]
    assert newaxis_index_report["data"] == [[2], [5]]

    ellipsis_index = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            "...,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    ellipsis_index_report = json.loads(ellipsis_index.stdout)
    assert ellipsis_index_report["ok"] is True
    assert ellipsis_index_report["shape"] == [2]
    assert ellipsis_index_report["data"] == [2, 5]

    diagonal = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--diagonal",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    diagonal_report = json.loads(diagonal.stdout)
    assert diagonal_report["ok"] is True
    assert diagonal_report["diagonal"] == 1
    assert diagonal_report["shape"] == [2]
    assert diagonal_report["data"] == [2, 6]
    assert diagonal_report["view"] is True
    assert diagonal_report["owns_data"] is False

    bad_broadcast = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2]",
            "--op",
            "add",
            "--rhs",
            "[1,2,3]",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_broadcast.returncode == 2
    bad_broadcast_report = json.loads(bad_broadcast.stdout)
    assert bad_broadcast_report["ok"] is False
    assert bad_broadcast_report["diagnostics"][0]["code"] == "PCC-ARRAY-BROADCAST-INCOMPATIBLE"

    transformed = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--transpose",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    transformed_report = json.loads(transformed.stdout)
    assert transformed_report["ok"] is True
    assert transformed_report["shape"] == [3, 2]
    assert transformed_report["data"] == [[1, 4], [2, 5], [3, 6]]
    assert transformed_report["view"] is True
    assert transformed_report["owns_data"] is False
    assert transformed_report["base_shape"] == [2, 3]

    swapped = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--swapaxes",
            "0,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    swapped_report = json.loads(swapped.stdout)
    assert swapped_report["ok"] is True
    assert swapped_report["swapaxes"] == "0,1"
    assert swapped_report["shape"] == [3, 2]
    assert swapped_report["data"] == [[1, 4], [2, 5], [3, 6]]
    assert swapped_report["view"] is True
    assert swapped_report["owns_data"] is False
    assert swapped_report["base_shape"] == [2, 3]

    bad_swapaxes = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--swapaxes",
            "0,1",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_swapaxes.returncode == 2
    bad_swapaxes_report = json.loads(bad_swapaxes.stdout)
    assert bad_swapaxes_report["diagnostics"][0]["code"] == "PCC-ARRAY-AXIS-OUT-OF-BOUNDS"

    moved = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--moveaxis",
            "0,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    moved_report = json.loads(moved.stdout)
    assert moved_report["ok"] is True
    assert moved_report["moveaxis"] == "0,1"
    assert moved_report["shape"] == [3, 2]
    assert moved_report["data"] == [[1, 4], [2, 5], [3, 6]]
    assert moved_report["view"] is True
    assert moved_report["owns_data"] is False
    assert moved_report["base_shape"] == [2, 3]

    moved_1d = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--moveaxis",
            "0,0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    moved_1d_report = json.loads(moved_1d.stdout)
    assert moved_1d_report["ok"] is True
    assert moved_1d_report["shape"] == [3]
    assert moved_1d_report["data"] == [1, 2, 3]
    assert moved_1d_report["view"] is True

    rotated = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--rot90",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    rotated_report = json.loads(rotated.stdout)
    assert rotated_report["ok"] is True
    assert rotated_report["rot90"] == 1
    assert rotated_report["shape"] == [3, 2]
    assert rotated_report["data"] == [[3, 6], [2, 5], [1, 4]]
    assert rotated_report["strides"] == [-8, 24]
    assert rotated_report["view"] is True
    assert rotated_report["owns_data"] is False
    assert rotated_report["base_shape"] == [2, 3]

    rotated_clockwise = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--rot90",
            "-1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    rotated_clockwise_report = json.loads(rotated_clockwise.stdout)
    assert rotated_clockwise_report["ok"] is True
    assert rotated_clockwise_report["rot90"] == -1
    assert rotated_clockwise_report["data"] == [[4, 1], [5, 2], [6, 3]]
    assert rotated_clockwise_report["strides"] == [8, -24]

    bad_rot90 = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--rot90",
            "1",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_rot90.returncode == 2
    bad_rot90_report = json.loads(bad_rot90.stdout)
    assert bad_rot90_report["diagnostics"][0]["code"] == "PCC-ARRAY-ROT90-RANK-UNSUPPORTED"

    bad_moveaxis = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--moveaxis",
            "0,1",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_moveaxis.returncode == 2
    bad_moveaxis_report = json.loads(bad_moveaxis.stdout)
    assert bad_moveaxis_report["diagnostics"][0]["code"] == "PCC-ARRAY-AXIS-OUT-OF-BOUNDS"

    flipped = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--flip",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    flipped_report = json.loads(flipped.stdout)
    assert flipped_report["ok"] is True
    assert flipped_report["flip"] is True
    assert flipped_report["shape"] == [2, 3]
    assert flipped_report["data"] == [[6, 5, 4], [3, 2, 1]]
    assert flipped_report["strides"] == [-24, -8]
    assert flipped_report["view"] is True
    assert flipped_report["owns_data"] is False
    assert flipped_report["base_shape"] == [2, 3]

    flipped_axis1 = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--flip",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    flipped_axis1_report = json.loads(flipped_axis1.stdout)
    assert flipped_axis1_report["ok"] is True
    assert flipped_axis1_report["axis"] == 1
    assert flipped_axis1_report["data"] == [[3, 2, 1], [6, 5, 4]]
    assert flipped_axis1_report["strides"] == [24, -8]

    flattened = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--flatten",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    flattened_report = json.loads(flattened.stdout)
    assert flattened_report["ok"] is True
    assert flattened_report["flatten"] is True
    assert flattened_report["shape"] == [6]
    assert flattened_report["data"] == [1, 2, 3, 4, 5, 6]
    assert flattened_report["view"] is False
    assert flattened_report["owns_data"] is True
    assert "base_shape" not in flattened_report

    squeezed = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3]]",
            "--squeeze",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    squeezed_report = json.loads(squeezed.stdout)
    assert squeezed_report["ok"] is True
    assert squeezed_report["shape"] == [3]
    assert squeezed_report["data"] == [1, 2, 3]
    assert squeezed_report["view"] is True
    assert squeezed_report["base_shape"] == [1, 3]

    expanded = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--expand-dims",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    expanded_report = json.loads(expanded.stdout)
    assert expanded_report["ok"] is True
    assert expanded_report["shape"] == [2, 1, 3]
    assert expanded_report["flat_data"] == [1, 2, 3, 4, 5, 6]
    assert expanded_report["view"] is True

    bad_squeeze = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--squeeze",
            "--squeeze-axis",
            "0",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_squeeze.returncode == 2
    bad_squeeze_report = json.loads(bad_squeeze.stdout)
    assert bad_squeeze_report["diagnostics"][0]["code"] == "PCC-ARRAY-SQUEEZE-AXIS-NOT-ONE"

    reduced = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "sum",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    reduced_report = json.loads(reduced.stdout)
    assert reduced_report["ok"] is True
    assert reduced_report["shape"] == []
    assert reduced_report["data"] == 21
    assert reduced_report["dtype"] == "int64"

    prod_reduced = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "prod",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    prod_reduced_report = json.loads(prod_reduced.stdout)
    assert prod_reduced_report["ok"] is True
    assert prod_reduced_report["shape"] == []
    assert prod_reduced_report["data"] == 720
    assert prod_reduced_report["dtype"] == "int64"

    mean_reduced = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "mean",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    mean_reduced_report = json.loads(mean_reduced.stdout)
    assert mean_reduced_report["ok"] is True
    assert mean_reduced_report["shape"] == []
    assert mean_reduced_report["data"] == 3.5
    assert mean_reduced_report["dtype"] == "float64"

    casted = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2]",
            "--astype",
            "float64",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    casted_report = json.loads(casted.stdout)
    assert casted_report["ok"] is True
    assert casted_report["dtype"] == "float64"
    assert casted_report["data"] == [1.0, 2.0]

    axis_reduced = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "sum",
            "--axis",
            "0",
            "--keepdims",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    axis_reduced_report = json.loads(axis_reduced.stdout)
    assert axis_reduced_report["ok"] is True
    assert axis_reduced_report["shape"] == [1, 3]
    assert axis_reduced_report["data"] == [[5, 7, 9]]
    assert axis_reduced_report["axis"] == 0
    assert axis_reduced_report["keepdims"] is True

    axis_prod = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "prod",
            "--axis",
            "1",
            "--keepdims",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    axis_prod_report = json.loads(axis_prod.stdout)
    assert axis_prod_report["ok"] is True
    assert axis_prod_report["shape"] == [2, 1]
    assert axis_prod_report["data"] == [[6], [120]]

    axis_mean = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "mean",
            "--axis",
            "1",
            "--keepdims",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    axis_mean_report = json.loads(axis_mean.stdout)
    assert axis_mean_report["ok"] is True
    assert axis_mean_report["shape"] == [2, 1]
    assert axis_mean_report["data"] == [[2.0], [5.0]]

    any_axis = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[0,0,0]]",
            "--reduce",
            "any",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    any_axis_report = json.loads(any_axis.stdout)
    assert any_axis_report["ok"] is True
    assert any_axis_report["shape"] == [2]
    assert any_axis_report["dtype"] == "bool"
    assert any_axis_report["data"] == [True, False]

    argmax_axis = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,5,3],[4,2,6]]",
            "--argreduce",
            "argmax",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    argmax_axis_report = json.loads(argmax_axis.stdout)
    assert argmax_axis_report["ok"] is True
    assert argmax_axis_report["argreduce"] == "argmax"
    assert argmax_axis_report["shape"] == [3]
    assert argmax_axis_report["dtype"] == "int64"
    assert argmax_axis_report["data"] == [1, 0, 1]

    counted = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--count-nonzero",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    counted_report = json.loads(counted.stdout)
    assert counted_report["ok"] is True
    assert counted_report["count_nonzero"] is True
    assert counted_report["shape"] == [2]
    assert counted_report["dtype"] == "int64"
    assert counted_report["data"] == [1, 2]

    nonzero = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--nonzero",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    nonzero_report = json.loads(nonzero.stdout)
    assert nonzero_report["ok"] is True
    assert nonzero_report["nonzero"] is True
    assert nonzero_report["shape"] == [2, 3]
    assert nonzero_report["dtype"] == "int64"
    assert nonzero_report["data"] == [[0, 1, 1], [1, 0, 2]]

    argwhere = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--argwhere",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    argwhere_report = json.loads(argwhere.stdout)
    assert argwhere_report["ok"] is True
    assert argwhere_report["argwhere"] is True
    assert argwhere_report["shape"] == [3, 2]
    assert argwhere_report["dtype"] == "int64"
    assert argwhere_report["data"] == [[0, 1], [1, 0], [1, 2]]

    flatnonzero = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--flatnonzero",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    flatnonzero_report = json.loads(flatnonzero.stdout)
    assert flatnonzero_report["ok"] is True
    assert flatnonzero_report["flatnonzero"] is True
    assert flatnonzero_report["shape"] == [3]
    assert flatnonzero_report["dtype"] == "int64"
    assert flatnonzero_report["data"] == [1, 3, 5]

    cumulative = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--cumulative",
            "cumsum",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    cumulative_report = json.loads(cumulative.stdout)
    assert cumulative_report["ok"] is True
    assert cumulative_report["cumulative"] == "cumsum"
    assert cumulative_report["shape"] == [2, 3]
    assert cumulative_report["data"] == [[1, 2, 3], [5, 7, 9]]

    sorted_rows = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[3,1,2],[6,5,4]]",
            "--sort",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    sorted_rows_report = json.loads(sorted_rows.stdout)
    assert sorted_rows_report["ok"] is True
    assert sorted_rows_report["sort"] is True
    assert sorted_rows_report["shape"] == [2, 3]
    assert sorted_rows_report["data"] == [[1, 2, 3], [4, 5, 6]]

    sorted_axis0 = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[4,2,6],[1,5,3]]",
            "--sort",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    sorted_axis0_report = json.loads(sorted_axis0.stdout)
    assert sorted_axis0_report["ok"] is True
    assert sorted_axis0_report["axis"] == 0
    assert sorted_axis0_report["data"] == [[1, 2, 3], [4, 5, 6]]

    argsorted_rows = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[3,1,2],[6,5,4]]",
            "--argsort",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    argsorted_rows_report = json.loads(argsorted_rows.stdout)
    assert argsorted_rows_report["ok"] is True
    assert argsorted_rows_report["argsort"] is True
    assert argsorted_rows_report["dtype"] == "int64"
    assert argsorted_rows_report["data"] == [[1, 2, 0], [2, 1, 0]]

    argsorted_axis0 = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[4,2,6],[1,5,3]]",
            "--argsort",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    argsorted_axis0_report = json.loads(argsorted_axis0.stdout)
    assert argsorted_axis0_report["ok"] is True
    assert argsorted_axis0_report["axis"] == 0
    assert argsorted_axis0_report["data"] == [[1, 0, 1], [0, 1, 0]]

    searchsorted_left = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,3,5]",
            "--searchsorted",
            "[0,3,7]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    searchsorted_left_report = json.loads(searchsorted_left.stdout)
    assert searchsorted_left_report["ok"] is True
    assert searchsorted_left_report["searchsorted"] == "[0,3,7]"
    assert searchsorted_left_report["side"] == "left"
    assert searchsorted_left_report["dtype"] == "int64"
    assert searchsorted_left_report["data"] == [0, 1, 3]

    searchsorted_right = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,3,5]",
            "--searchsorted",
            "[0,3,7]",
            "--side",
            "right",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    searchsorted_right_report = json.loads(searchsorted_right.stdout)
    assert searchsorted_right_report["ok"] is True
    assert searchsorted_right_report["side"] == "right"
    assert searchsorted_right_report["data"] == [0, 2, 3]

    partitioned_axis0 = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[4,2,6],[1,5,3]]",
            "--partition",
            "1",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    partitioned_axis0_report = json.loads(partitioned_axis0.stdout)
    assert partitioned_axis0_report["ok"] is True
    assert partitioned_axis0_report["partition"] == 1
    assert partitioned_axis0_report["axis"] == 0
    assert partitioned_axis0_report["data"] == [[1, 2, 3], [4, 5, 6]]

    argpartitioned_rows = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[3,1,2],[6,5,4]]",
            "--argpartition",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    argpartitioned_rows_report = json.loads(argpartitioned_rows.stdout)
    assert argpartitioned_rows_report["ok"] is True
    assert argpartitioned_rows_report["argpartition"] == 1
    assert argpartitioned_rows_report["dtype"] == "int64"
    assert argpartitioned_rows_report["data"] == [[1, 2, 0], [2, 1, 0]]

    taken = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--take",
            "2,0",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    taken_report = json.loads(taken.stdout)
    assert taken_report["ok"] is True
    assert taken_report["shape"] == [2, 2]
    assert taken_report["data"] == [[3, 1], [6, 4]]

    put = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--put",
            "0,-1,2",
            "--put-values",
            "[9,8]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    put_report = json.loads(put.stdout)
    assert put_report["ok"] is True
    assert put_report["put"] == "0,-1,2"
    assert put_report["put_values"] == "[9,8]"
    assert put_report["shape"] == [2, 3]
    assert put_report["data"] == [[9, 2, 9], [4, 5, 8]]

    putmask = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--putmask",
            "[[True,False,True],[False,True,False]]",
            "--putmask-values",
            "[9,8]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    putmask_report = json.loads(putmask.stdout)
    assert putmask_report["ok"] is True
    assert putmask_report["putmask"] == "[[True,False,True],[False,True,False]]"
    assert putmask_report["putmask_values"] == "[9,8]"
    assert putmask_report["shape"] == [2, 3]
    assert putmask_report["data"] == [[9, 2, 8], [4, 9, 6]]

    dtype_report = array_core_report(shape="2,3", dtype="float32")
    assert dtype_report["dtype"] == "float32"
    assert dtype_report["dtype_format"] == "f"
    assert dtype_report["itemsize"] == 4
    assert dtype_report["strides"] == [12, 4]

    uint8_cast = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[255,256,-1]",
            "--dtype",
            "uint8",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    uint8_report = json.loads(uint8_cast.stdout)
    assert uint8_report["ok"] is True
    assert uint8_report["dtype"] == "uint8"
    assert uint8_report["dtype_format"] == "B"
    assert uint8_report["dtype_range"] == [0, 255]
    assert uint8_report["dtype_signed"] is False
    assert uint8_report["data"] == [255, 0, 255]

    int8_cast = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[128,129,-129]",
            "--astype",
            "int8",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    int8_report = json.loads(int8_cast.stdout)
    assert int8_report["ok"] is True
    assert int8_report["dtype"] == "int8"
    assert int8_report["dtype_range"] == [-128, 127]
    assert int8_report["dtype_signed"] is True
    assert int8_report["data"] == [-128, -127, 127]

    filled = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--shape",
            "2,3",
            "--dtype",
            "int16",
            "--fill",
            "7",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    filled_report = json.loads(filled.stdout)
    assert filled_report["ok"] is True
    assert filled_report["shape"] == [2, 3]
    assert filled_report["dtype"] == "int16"
    assert filled_report["data"] == [[7, 7, 7], [7, 7, 7]]
    assert filled_report["fill"] == "7"

    ranged = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--arange",
            "1,6,2",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    ranged_report = json.loads(ranged.stdout)
    assert ranged_report["ok"] is True
    assert ranged_report["source"] == "arange"
    assert ranged_report["shape"] == [3]
    assert ranged_report["data"] == [1, 3, 5]

    ranged_float = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--arange",
            "0,1,0.25",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    ranged_float_report = json.loads(ranged_float.stdout)
    assert ranged_float_report["ok"] is True
    assert ranged_float_report["source"] == "arange"
    assert ranged_float_report["shape"] == [4]
    assert ranged_float_report["dtype"] == "float64"
    assert ranged_float_report["data"] == [0.0, 0.25, 0.5, 0.75]

    zeros = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--zeros",
            "2,2",
            "--dtype",
            "int32",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    zeros_report = json.loads(zeros.stdout)
    assert zeros_report["ok"] is True
    assert zeros_report["source"] == "zeros"
    assert zeros_report["data"] == [[0, 0], [0, 0]]
    assert zeros_report["dtype"] == "int32"

    zeros_like = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3,4]]",
            "--dtype",
            "uint8",
            "--zeros-like",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    zeros_like_report = json.loads(zeros_like.stdout)
    assert zeros_like_report["ok"] is True
    assert zeros_like_report["zeros_like"] is True
    assert zeros_like_report["shape"] == [2, 2]
    assert zeros_like_report["dtype"] == "uint8"
    assert zeros_like_report["data"] == [[0, 0], [0, 0]]
    assert zeros_like_report["owns_data"] is True
    assert zeros_like_report["view"] is False

    full_like = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3,4]]",
            "--full-like",
            "7",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    full_like_report = json.loads(full_like.stdout)
    assert full_like_report["ok"] is True
    assert full_like_report["full_like"] == "7"
    assert full_like_report["shape"] == [2, 2]
    assert full_like_report["data"] == [[7, 7], [7, 7]]

    eye = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--eye",
            "2,3,1",
            "--dtype",
            "int64",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    eye_report = json.loads(eye.stdout)
    assert eye_report["ok"] is True
    assert eye_report["source"] == "eye"
    assert eye_report["shape"] == [2, 3]
    assert eye_report["data"] == [[0, 1, 0], [0, 0, 1]]

    linspace = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--linspace",
            "0,1,5",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    linspace_report = json.loads(linspace.stdout)
    assert linspace_report["ok"] is True
    assert linspace_report["source"] == "linspace"
    assert linspace_report["dtype"] == "float64"
    assert linspace_report["data"] == [0.0, 0.25, 0.5, 0.75, 1.0]

    concat = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--concat",
            "[[7],[8]]",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    concat_report = json.loads(concat.stdout)
    assert concat_report["ok"] is True
    assert concat_report["shape"] == [2, 4]
    assert concat_report["data"] == [[1, 2, 3, 7], [4, 5, 6, 8]]

    stack = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--stack",
            "[4,5,6]",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    stack_report = json.loads(stack.stdout)
    assert stack_report["ok"] is True
    assert stack_report["shape"] == [3, 2]
    assert stack_report["data"] == [[1, 4], [2, 5], [3, 6]]

    bad_concat = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--concat",
            "[[7,8]]",
            "--axis",
            "0",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_concat.returncode == 2
    bad_concat_report = json.loads(bad_concat.stdout)
    assert bad_concat_report["diagnostics"][0]["code"] == "PCC-ARRAY-CONCAT-SHAPE-MISMATCH"

    masked = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--mask",
            "[[True,False,True],[False,True,False]]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    masked_report = json.loads(masked.stdout)
    assert masked_report["ok"] is True
    assert masked_report["shape"] == [3]
    assert masked_report["data"] == [1, 3, 5]

    compressed = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--compress",
            "[True,False,True]",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    compressed_report = json.loads(compressed.stdout)
    assert compressed_report["ok"] is True
    assert compressed_report["compress"] == "[True,False,True]"
    assert compressed_report["axis"] == 1
    assert compressed_report["shape"] == [2, 2]
    assert compressed_report["data"] == [[1, 3], [4, 6]]

    compressed_flat = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--compress",
            "[True,False,True,False,True,False]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    compressed_flat_report = json.loads(compressed_flat.stdout)
    assert compressed_flat_report["ok"] is True
    assert compressed_flat_report["shape"] == [3]
    assert compressed_flat_report["data"] == [1, 3, 5]

    object_report = array_core_report(literal="['a','b']", dtype="object")
    assert object_report["dtype"] == "object"
    assert "compress" in object_report["object_policy"]["allowed"]
    assert "numeric_ufunc" in object_report["object_policy"]["unsupported"]

    compared = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--compare",
            "gt",
            "--rhs",
            "[2,5,4]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    compared_report = json.loads(compared.stdout)
    assert compared_report["ok"] is True
    assert compared_report["dtype"] == "bool"
    assert compared_report["data"] == [[False, False, False], [True, False, True]]

    where = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--where",
            "[[True,False,True],[False,True,False]]",
            "--otherwise",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    where_report = json.loads(where.stdout)
    assert where_report["ok"] is True
    assert where_report["shape"] == [2, 3]
    assert where_report["data"] == [[1, 0, 3], [0, 5, 0]]

    matmul = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--matmul",
            "[[7,8],[9,10],[11,12]]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    matmul_report = json.loads(matmul.stdout)
    assert matmul_report["ok"] is True
    assert matmul_report["shape"] == [2, 2]
    assert matmul_report["data"] == [[58, 64], [139, 154]]
    assert matmul_report["matmul"] == "[[7,8],[9,10],[11,12]]"

    bad_matmul = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--matmul",
            "[1,2]",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert bad_matmul.returncode == 2
    bad_matmul_report = json.loads(bad_matmul.stdout)
    assert bad_matmul_report["diagnostics"][0]["code"] == "PCC-ARRAY-MATMUL-SHAPE-MISMATCH"


def test_pcc1_array_core_cli_does_not_need_host_python(tmp_path):
    pcc1 = _find_current_pcc1()
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 binary with native array-core shim")
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    report = json.loads(proc.stdout)
    assert report["schema"] == "pcc.array-core.v1"
    assert report["ok"] is True
    assert report["shape"] == [2, 3]
    assert report["strides"] == [24, 8]
    assert report["dtype"] == "int64"

    diagonal = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--diagonal",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    diagonal_report = json.loads(diagonal.stdout)
    assert diagonal_report["ok"] is True
    assert diagonal_report["diagonal"] == 1
    assert diagonal_report["shape"] == [2]
    assert diagonal_report["data"] == [2, 6]
    assert diagonal_report["view"] is True
    assert diagonal_report["owns_data"] is False

    op = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--op",
            "add",
            "--rhs",
            "[10,20,30]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    op_report = json.loads(op.stdout)
    assert op_report["ok"] is True
    assert op_report["shape"] == [2, 3]
    assert op_report["data"] == [[11, 22, 33], [14, 25, 36]]

    unary = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,-2,3]",
            "--unary",
            "abs",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    unary_report = json.loads(unary.stdout)
    assert unary_report["ok"] is True
    assert unary_report["unary"] == "abs"
    assert unary_report["data"] == [1, 2, 3]

    unary_bool = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[True,False,0,2]",
            "--unary",
            "logical_not",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    unary_bool_report = json.loads(unary_bool.stdout)
    assert unary_bool_report["ok"] is True
    assert unary_bool_report["dtype"] == "bool"
    assert unary_bool_report["data"] == [False, True, True, False]

    clipped = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[-2,0,3,9]",
            "--clip",
            "0,5",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    clipped_report = json.loads(clipped.stdout)
    assert clipped_report["ok"] is True
    assert clipped_report["clip"] == "0,5"
    assert clipped_report["dtype"] == "int64"
    assert clipped_report["data"] == [0, 0, 3, 5]

    clipped_float = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[0,2,4]",
            "--clip",
            "1.5,3.5",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    clipped_float_report = json.loads(clipped_float.stdout)
    assert clipped_float_report["ok"] is True
    assert clipped_float_report["clip"] == "1.5,3.5"
    assert clipped_float_report["dtype"] == "float64"
    assert clipped_float_report["data"] == [1.5, 2.0, 3.5]

    broadcast_to = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--broadcast-to",
            "2,3",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    broadcast_to_report = json.loads(broadcast_to.stdout)
    assert broadcast_to_report["ok"] is True
    assert broadcast_to_report["broadcast_to"] == "2,3"
    assert broadcast_to_report["shape"] == [2, 3]
    assert broadcast_to_report["strides"] == [0, 8]
    assert broadcast_to_report["data"] == [[1, 2, 3], [1, 2, 3]]
    assert broadcast_to_report["view"] is True
    assert broadcast_to_report["base_shape"] == [3]

    zeros_like = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3,4]]",
            "--dtype",
            "uint8",
            "--zeros-like",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    zeros_like_report = json.loads(zeros_like.stdout)
    assert zeros_like_report["ok"] is True
    assert zeros_like_report["zeros_like"] is True
    assert zeros_like_report["dtype"] == "uint8"
    assert zeros_like_report["data"] == [[0, 0], [0, 0]]

    full_like = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3,4]]",
            "--full-like",
            "7",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    full_like_report = json.loads(full_like.stdout)
    assert full_like_report["ok"] is True
    assert full_like_report["full_like"] == "7"
    assert full_like_report["data"] == [[7, 7], [7, 7]]

    repeated = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--repeat",
            "2",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    repeated_report = json.loads(repeated.stdout)
    assert repeated_report["ok"] is True
    assert repeated_report["repeat"] == 2
    assert repeated_report["shape"] == [2, 6]
    assert repeated_report["data"] == [[1, 1, 2, 2, 3, 3], [4, 4, 5, 5, 6, 6]]

    tiled = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2],[3,4]]",
            "--tile",
            "2,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    tiled_report = json.loads(tiled.stdout)
    assert tiled_report["ok"] is True
    assert tiled_report["tile"] == "2,1"
    assert tiled_report["data"] == [[1, 2], [3, 4], [1, 2], [3, 4]]

    stepped_index = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            ":,::2",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    stepped_index_report = json.loads(stepped_index.stdout)
    assert stepped_index_report["ok"] is True
    assert stepped_index_report["shape"] == [2, 2]
    assert stepped_index_report["data"] == [[1, 3], [4, 6]]

    reversed_index = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            "::-1,::-1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    reversed_index_report = json.loads(reversed_index.stdout)
    assert reversed_index_report["ok"] is True
    assert reversed_index_report["shape"] == [2, 3]
    assert reversed_index_report["data"] == [[6, 5, 4], [3, 2, 1]]

    bad_step_index = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            "::0,:",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert bad_step_index.returncode == 2
    bad_step_index_report = json.loads(bad_step_index.stdout)
    assert bad_step_index_report["diagnostics"][0]["code"] == "PCC-ARRAY-INDEX-PARSE-FAILED"

    newaxis_index = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            ":,None,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    newaxis_index_report = json.loads(newaxis_index.stdout)
    assert newaxis_index_report["ok"] is True
    assert newaxis_index_report["shape"] == [2, 1]
    assert newaxis_index_report["data"] == [[2], [5]]

    ellipsis_index = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--index",
            "...,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    ellipsis_index_report = json.loads(ellipsis_index.stdout)
    assert ellipsis_index_report["ok"] is True
    assert ellipsis_index_report["shape"] == [2]
    assert ellipsis_index_report["data"] == [2, 5]

    rolled = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--roll",
            "2",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    rolled_report = json.loads(rolled.stdout)
    assert rolled_report["ok"] is True
    assert rolled_report["roll"] == 2
    assert rolled_report["data"] == [[5, 6, 1], [2, 3, 4]]
    assert rolled_report["owns_data"] is True
    assert rolled_report["view"] is False

    rolled_axis1 = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--roll",
            "-1",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    rolled_axis1_report = json.loads(rolled_axis1.stdout)
    assert rolled_axis1_report["ok"] is True
    assert rolled_axis1_report["axis"] == 1
    assert rolled_axis1_report["data"] == [[2, 3, 1], [5, 6, 4]]

    transposed = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--transpose",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    transposed_report = json.loads(transposed.stdout)
    assert transposed_report["ok"] is True
    assert transposed_report["shape"] == [3, 2]
    assert transposed_report["data"] == [[1, 4], [2, 5], [3, 6]]
    assert transposed_report["view"] is True

    swapped = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--swapaxes",
            "0,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    swapped_report = json.loads(swapped.stdout)
    assert swapped_report["ok"] is True
    assert swapped_report["swapaxes"] == "0,1"
    assert swapped_report["shape"] == [3, 2]
    assert swapped_report["data"] == [[1, 4], [2, 5], [3, 6]]
    assert swapped_report["view"] is True

    bad_swapaxes = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--swapaxes",
            "0,1",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert bad_swapaxes.returncode == 2
    bad_swapaxes_report = json.loads(bad_swapaxes.stdout)
    assert bad_swapaxes_report["diagnostics"][0]["code"] == "PCC-ARRAY-AXIS-OUT-OF-BOUNDS"

    moved = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--moveaxis",
            "0,1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    moved_report = json.loads(moved.stdout)
    assert moved_report["ok"] is True
    assert moved_report["moveaxis"] == "0,1"
    assert moved_report["shape"] == [3, 2]
    assert moved_report["data"] == [[1, 4], [2, 5], [3, 6]]
    assert moved_report["view"] is True

    moved_1d = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--moveaxis",
            "0,0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    moved_1d_report = json.loads(moved_1d.stdout)
    assert moved_1d_report["ok"] is True
    assert moved_1d_report["shape"] == [3]
    assert moved_1d_report["data"] == [1, 2, 3]
    assert moved_1d_report["view"] is True

    rotated = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--rot90",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    rotated_report = json.loads(rotated.stdout)
    assert rotated_report["ok"] is True
    assert rotated_report["rot90"] == 1
    assert rotated_report["shape"] == [3, 2]
    assert rotated_report["data"] == [[3, 6], [2, 5], [1, 4]]
    assert rotated_report["strides"] == [-8, 24]
    assert rotated_report["view"] is True

    rotated_clockwise = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--rot90",
            "-1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    rotated_clockwise_report = json.loads(rotated_clockwise.stdout)
    assert rotated_clockwise_report["ok"] is True
    assert rotated_clockwise_report["rot90"] == -1
    assert rotated_clockwise_report["data"] == [[4, 1], [5, 2], [6, 3]]

    bad_rot90 = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--rot90",
            "1",
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert bad_rot90.returncode == 2
    bad_rot90_report = json.loads(bad_rot90.stdout)
    assert bad_rot90_report["diagnostics"][0]["code"] == "PCC-ARRAY-ROT90-RANK-UNSUPPORTED"

    flipped = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--flip",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    flipped_report = json.loads(flipped.stdout)
    assert flipped_report["ok"] is True
    assert flipped_report["flip"] is True
    assert flipped_report["shape"] == [2, 3]
    assert flipped_report["data"] == [[6, 5, 4], [3, 2, 1]]
    assert flipped_report["strides"] == [-24, -8]
    assert flipped_report["view"] is True

    flattened = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--flatten",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    flattened_report = json.loads(flattened.stdout)
    assert flattened_report["ok"] is True
    assert flattened_report["flatten"] is True
    assert flattened_report["shape"] == [6]
    assert flattened_report["data"] == [1, 2, 3, 4, 5, 6]
    assert flattened_report["view"] is False
    assert flattened_report["owns_data"] is True
    assert "base_shape" not in flattened_report

    squeezed = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3]]",
            "--squeeze",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    squeezed_report = json.loads(squeezed.stdout)
    assert squeezed_report["ok"] is True
    assert squeezed_report["shape"] == [3]
    assert squeezed_report["data"] == [1, 2, 3]

    expanded = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--expand-dims",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    expanded_report = json.loads(expanded.stdout)
    assert expanded_report["ok"] is True
    assert expanded_report["shape"] == [2, 1, 3]
    assert expanded_report["flat_data"] == [1, 2, 3, 4, 5, 6]

    reduced = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "sum",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    reduced_report = json.loads(reduced.stdout)
    assert reduced_report["ok"] is True
    assert reduced_report["shape"] == []
    assert reduced_report["data"] == 21

    prod_reduced = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "prod",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    prod_reduced_report = json.loads(prod_reduced.stdout)
    assert prod_reduced_report["ok"] is True
    assert prod_reduced_report["shape"] == []
    assert prod_reduced_report["data"] == 720
    assert prod_reduced_report["dtype"] == "int64"

    mean_reduced = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "mean",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    mean_reduced_report = json.loads(mean_reduced.stdout)
    assert mean_reduced_report["ok"] is True
    assert mean_reduced_report["shape"] == []
    assert mean_reduced_report["data"] == 3.5
    assert mean_reduced_report["dtype"] == "float64"

    axis_reduced = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "sum",
            "--axis",
            "0",
            "--keepdims",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    axis_reduced_report = json.loads(axis_reduced.stdout)
    assert axis_reduced_report["ok"] is True
    assert axis_reduced_report["shape"] == [1, 3]

    axis_prod = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "prod",
            "--axis",
            "1",
            "--keepdims",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    axis_prod_report = json.loads(axis_prod.stdout)
    assert axis_prod_report["ok"] is True
    assert axis_prod_report["shape"] == [2, 1]
    assert axis_prod_report["data"] == [[6], [120]]

    axis_mean = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--reduce",
            "mean",
            "--axis",
            "1",
            "--keepdims",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    axis_mean_report = json.loads(axis_mean.stdout)
    assert axis_mean_report["ok"] is True
    assert axis_mean_report["shape"] == [2, 1]
    assert axis_mean_report["data"] == [[2.0], [5.0]]
    assert axis_reduced_report["data"] == [[5, 7, 9]]

    any_axis = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[0,0,0]]",
            "--reduce",
            "any",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    any_axis_report = json.loads(any_axis.stdout)
    assert any_axis_report["ok"] is True
    assert any_axis_report["shape"] == [2]
    assert any_axis_report["dtype"] == "bool"
    assert any_axis_report["data"] == [True, False]

    argmax_axis = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,5,3],[4,2,6]]",
            "--argreduce",
            "argmax",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    argmax_axis_report = json.loads(argmax_axis.stdout)
    assert argmax_axis_report["ok"] is True
    assert argmax_axis_report["argreduce"] == "argmax"
    assert argmax_axis_report["shape"] == [3]
    assert argmax_axis_report["data"] == [1, 0, 1]

    counted = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--count-nonzero",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    counted_report = json.loads(counted.stdout)
    assert counted_report["ok"] is True
    assert counted_report["count_nonzero"] is True
    assert counted_report["shape"] == [2]
    assert counted_report["data"] == [1, 2]

    nonzero = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--nonzero",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    nonzero_report = json.loads(nonzero.stdout)
    assert nonzero_report["ok"] is True
    assert nonzero_report["nonzero"] is True
    assert nonzero_report["shape"] == [2, 3]
    assert nonzero_report["data"] == [[0, 1, 1], [1, 0, 2]]

    argwhere = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--argwhere",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    argwhere_report = json.loads(argwhere.stdout)
    assert argwhere_report["ok"] is True
    assert argwhere_report["argwhere"] is True
    assert argwhere_report["shape"] == [3, 2]
    assert argwhere_report["data"] == [[0, 1], [1, 0], [1, 2]]

    flatnonzero = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[0,1,0],[2,0,3]]",
            "--flatnonzero",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    flatnonzero_report = json.loads(flatnonzero.stdout)
    assert flatnonzero_report["ok"] is True
    assert flatnonzero_report["flatnonzero"] is True
    assert flatnonzero_report["shape"] == [3]
    assert flatnonzero_report["data"] == [1, 3, 5]

    cumulative = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--cumulative",
            "cumsum",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    cumulative_report = json.loads(cumulative.stdout)
    assert cumulative_report["ok"] is True
    assert cumulative_report["cumulative"] == "cumsum"
    assert cumulative_report["shape"] == [2, 3]
    assert cumulative_report["data"] == [[1, 2, 3], [5, 7, 9]]

    sorted_rows = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[3,1,2],[6,5,4]]",
            "--sort",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    sorted_rows_report = json.loads(sorted_rows.stdout)
    assert sorted_rows_report["ok"] is True
    assert sorted_rows_report["sort"] is True
    assert sorted_rows_report["shape"] == [2, 3]
    assert sorted_rows_report["data"] == [[1, 2, 3], [4, 5, 6]]

    sorted_axis0 = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[4,2,6],[1,5,3]]",
            "--sort",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    sorted_axis0_report = json.loads(sorted_axis0.stdout)
    assert sorted_axis0_report["ok"] is True
    assert sorted_axis0_report["axis"] == 0
    assert sorted_axis0_report["data"] == [[1, 2, 3], [4, 5, 6]]

    argsorted_rows = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[3,1,2],[6,5,4]]",
            "--argsort",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    argsorted_rows_report = json.loads(argsorted_rows.stdout)
    assert argsorted_rows_report["ok"] is True
    assert argsorted_rows_report["argsort"] is True
    assert argsorted_rows_report["dtype"] == "int64"
    assert argsorted_rows_report["data"] == [[1, 2, 0], [2, 1, 0]]

    argsorted_axis0 = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[4,2,6],[1,5,3]]",
            "--argsort",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    argsorted_axis0_report = json.loads(argsorted_axis0.stdout)
    assert argsorted_axis0_report["ok"] is True
    assert argsorted_axis0_report["axis"] == 0
    assert argsorted_axis0_report["data"] == [[1, 0, 1], [0, 1, 0]]

    searchsorted_left = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,3,5]",
            "--searchsorted",
            "[0,3,7]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    searchsorted_left_report = json.loads(searchsorted_left.stdout)
    assert searchsorted_left_report["ok"] is True
    assert searchsorted_left_report["searchsorted"] == "[0,3,7]"
    assert searchsorted_left_report["side"] == "left"
    assert searchsorted_left_report["dtype"] == "int64"
    assert searchsorted_left_report["data"] == [0, 1, 3]

    searchsorted_right = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,3,5]",
            "--searchsorted",
            "[0,3,7]",
            "--side",
            "right",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    searchsorted_right_report = json.loads(searchsorted_right.stdout)
    assert searchsorted_right_report["ok"] is True
    assert searchsorted_right_report["side"] == "right"
    assert searchsorted_right_report["data"] == [0, 2, 3]

    partitioned_axis0 = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[4,2,6],[1,5,3]]",
            "--partition",
            "1",
            "--axis",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    partitioned_axis0_report = json.loads(partitioned_axis0.stdout)
    assert partitioned_axis0_report["ok"] is True
    assert partitioned_axis0_report["partition"] == 1
    assert partitioned_axis0_report["axis"] == 0
    assert partitioned_axis0_report["data"] == [[1, 2, 3], [4, 5, 6]]

    argpartitioned_rows = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[3,1,2],[6,5,4]]",
            "--argpartition",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    argpartitioned_rows_report = json.loads(argpartitioned_rows.stdout)
    assert argpartitioned_rows_report["ok"] is True
    assert argpartitioned_rows_report["argpartition"] == 1
    assert argpartitioned_rows_report["dtype"] == "int64"
    assert argpartitioned_rows_report["data"] == [[1, 2, 0], [2, 1, 0]]

    taken = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--take",
            "2,0",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    taken_report = json.loads(taken.stdout)
    assert taken_report["ok"] is True
    assert taken_report["shape"] == [2, 2]
    assert taken_report["data"] == [[3, 1], [6, 4]]

    put = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--put",
            "0,-1,2",
            "--put-values",
            "[9,8]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    put_report = json.loads(put.stdout)
    assert put_report["ok"] is True
    assert put_report["put"] == "0,-1,2"
    assert put_report["put_values"] == "[9,8]"
    assert put_report["shape"] == [2, 3]
    assert put_report["data"] == [[9, 2, 9], [4, 5, 8]]

    putmask = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--putmask",
            "[[True,False,True],[False,True,False]]",
            "--putmask-values",
            "[9,8]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    putmask_report = json.loads(putmask.stdout)
    assert putmask_report["ok"] is True
    assert putmask_report["putmask"] == "[[True,False,True],[False,True,False]]"
    assert putmask_report["putmask_values"] == "[9,8]"
    assert putmask_report["shape"] == [2, 3]
    assert putmask_report["data"] == [[9, 2, 8], [4, 9, 6]]

    masked = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--mask",
            "[[True,False,True],[False,True,False]]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    masked_report = json.loads(masked.stdout)
    assert masked_report["ok"] is True
    assert masked_report["shape"] == [3]
    assert masked_report["data"] == [1, 3, 5]

    compressed = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--compress",
            "[True,False,True]",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    compressed_report = json.loads(compressed.stdout)
    assert compressed_report["ok"] is True
    assert compressed_report["compress"] == "[True,False,True]"
    assert compressed_report["axis"] == 1
    assert compressed_report["shape"] == [2, 2]
    assert compressed_report["data"] == [[1, 3], [4, 6]]

    compared = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--compare",
            "gt",
            "--rhs",
            "[2,5,4]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    compared_report = json.loads(compared.stdout)
    assert compared_report["ok"] is True
    assert compared_report["dtype"] == "bool"
    assert compared_report["data"] == [[False, False, False], [True, False, True]]

    where = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--where",
            "[[True,False,True],[False,True,False]]",
            "--otherwise",
            "0",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    where_report = json.loads(where.stdout)
    assert where_report["ok"] is True
    assert where_report["shape"] == [2, 3]
    assert where_report["data"] == [[1, 0, 3], [0, 5, 0]]

    matmul = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--matmul",
            "[[7,8],[9,10],[11,12]]",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    matmul_report = json.loads(matmul.stdout)
    assert matmul_report["ok"] is True
    assert matmul_report["shape"] == [2, 2]
    assert matmul_report["data"] == [[58, 64], [139, 154]]

    filled = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--shape",
            "2,3",
            "--dtype",
            "int16",
            "--fill",
            "7",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    filled_report = json.loads(filled.stdout)
    assert filled_report["ok"] is True
    assert filled_report["shape"] == [2, 3]
    assert filled_report["data"] == [[7, 7, 7], [7, 7, 7]]

    ranged = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--arange",
            "1,6,2",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    ranged_report = json.loads(ranged.stdout)
    assert ranged_report["ok"] is True
    assert ranged_report["source"] == "arange"
    assert ranged_report["data"] == [1, 3, 5]

    ranged_float = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--arange",
            "0,1,0.25",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    ranged_float_report = json.loads(ranged_float.stdout)
    assert ranged_float_report["ok"] is True
    assert ranged_float_report["source"] == "arange"
    assert ranged_float_report["shape"] == [4]
    assert ranged_float_report["dtype"] == "float64"
    assert ranged_float_report["data"] == [0.0, 0.25, 0.5, 0.75]

    zeros = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--zeros",
            "2,2",
            "--dtype",
            "int32",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    zeros_report = json.loads(zeros.stdout)
    assert zeros_report["ok"] is True
    assert zeros_report["source"] == "zeros"
    assert zeros_report["data"] == [[0, 0], [0, 0]]

    eye = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--eye",
            "2,3,1",
            "--dtype",
            "int64",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    eye_report = json.loads(eye.stdout)
    assert eye_report["ok"] is True
    assert eye_report["source"] == "eye"
    assert eye_report["data"] == [[0, 1, 0], [0, 0, 1]]

    linspace = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--linspace",
            "0,1,5",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    linspace_report = json.loads(linspace.stdout)
    assert linspace_report["ok"] is True
    assert linspace_report["source"] == "linspace"
    assert linspace_report["data"] == [0.0, 0.25, 0.5, 0.75, 1.0]

    concat = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[[1,2,3],[4,5,6]]",
            "--concat",
            "[[7],[8]]",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    concat_report = json.loads(concat.stdout)
    assert concat_report["ok"] is True
    assert concat_report["shape"] == [2, 4]
    assert concat_report["data"] == [[1, 2, 3, 7], [4, 5, 6, 8]]

    stack = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[1,2,3]",
            "--stack",
            "[4,5,6]",
            "--axis",
            "1",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    stack_report = json.loads(stack.stdout)
    assert stack_report["ok"] is True
    assert stack_report["shape"] == [3, 2]
    assert stack_report["data"] == [[1, 4], [2, 5], [3, 6]]

    uint8_cast = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[255,256,-1]",
            "--dtype",
            "uint8",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    uint8_report = json.loads(uint8_cast.stdout)
    assert uint8_report["ok"] is True
    assert uint8_report["dtype"] == "uint8"
    assert uint8_report["dtype_format"] == "B"
    assert uint8_report["dtype_range"] == [0, 255]
    assert uint8_report["data"] == [255, 0, 255]

    int8_cast = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pcc.package",
            "array-core",
            "--literal",
            "[128,129,-129]",
            "--astype",
            "int8",
            "--json",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    int8_report = json.loads(int8_cast.stdout)
    assert int8_report["ok"] is True
    assert int8_report["dtype"] == "int8"
    assert int8_report["dtype_range"] == [-128, 127]
    assert int8_report["data"] == [-128, -127, 127]
