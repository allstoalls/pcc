"""CLI/reporting front door for generic array-core layout semantics."""
from __future__ import annotations

import argparse
import json

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
    array_put,
    array_putmask,
    array_ravel,
    array_reduce,
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
    array_ones,
    array_ones_like,
    layout_from_shape,
    parse_int_list,
    parse_shape,
    array_repeat,
    value_from_literal,
    array_tile,
    array_zeros,
    array_zeros_like,
)


def array_core_report(
    *,
    shape: str | None = None,
    literal: str | None = None,
    dtype: str = "auto",
    require_rectangular: bool = False,
    fill: str | None = None,
    arange: str | None = None,
    zeros: str | None = None,
    ones: str | None = None,
    zeros_like: bool = False,
    ones_like: bool = False,
    full_like: str | None = None,
    eye: str | None = None,
    linspace: str | None = None,
    op: str | None = None,
    rhs: str | None = None,
    matmul: str | None = None,
    concat: str | None = None,
    stack: str | None = None,
    unary: str | None = None,
    clip: str | None = None,
    broadcast_to: str | None = None,
    repeat: int | None = None,
    tile: str | None = None,
    roll: int | None = None,
    rot90: int | None = None,
    compare: str | None = None,
    index: str | None = None,
    diagonal: int | None = None,
    reshape: str | None = None,
    ravel: bool = False,
    flatten: bool = False,
    flip: bool = False,
    transpose: bool = False,
    swapaxes: str | None = None,
    moveaxis: str | None = None,
    squeeze: bool = False,
    squeeze_axis: int | None = None,
    expand_dims: int | None = None,
    reduce: str | None = None,
    argreduce: str | None = None,
    count_nonzero: bool = False,
    nonzero: bool = False,
    argwhere: bool = False,
    flatnonzero: bool = False,
    cumulative: str | None = None,
    sort: bool = False,
    argsort: bool = False,
    searchsorted: str | None = None,
    side: str = "left",
    partition: int | None = None,
    argpartition: int | None = None,
    axis: int | None = None,
    keepdims: bool = False,
    take: str | None = None,
    put: str | None = None,
    put_values: str | None = None,
    putmask: str | None = None,
    putmask_values: str | None = None,
    mask: str | None = None,
    compress: str | None = None,
    where: str | None = None,
    otherwise: str | None = None,
    astype: str | None = None,
    copy: bool = False,
) -> dict[str, object]:
    if linspace is not None:
        value = array_linspace(linspace, dtype=dtype)
        report = value.as_dict()
        source = "linspace"
    elif eye is not None:
        value = array_eye(eye, dtype=dtype)
        report = value.as_dict()
        source = "eye"
    elif zeros is not None:
        value = array_zeros(parse_shape(zeros), dtype=dtype)
        report = value.as_dict()
        source = "zeros"
    elif ones is not None:
        value = array_ones(parse_shape(ones), dtype=dtype)
        report = value.as_dict()
        source = "ones"
    elif arange is not None:
        value = array_arange(arange, dtype=dtype)
        report = value.as_dict()
        source = "arange"
    elif literal is not None:
        value = value_from_literal(
            literal,
            dtype=dtype,
            require_rectangular=require_rectangular,
        )
        if zeros_like:
            value = array_zeros_like(value)
        if ones_like:
            value = array_ones_like(value)
        if full_like is not None:
            value = array_full_like_from_literal(value, full_like)
        if unary is not None:
            value = array_unary_op(value, unary)
        if clip is not None:
            value = array_clip(value, clip)
        if broadcast_to is not None:
            value = array_broadcast_to(value, parse_shape(broadcast_to))
        if repeat is not None:
            value = array_repeat(value, repeat, axis=axis)
        if tile is not None:
            value = array_tile(value, parse_shape(tile))
        if roll is not None:
            value = array_roll(value, roll, axis=axis)
        if rot90 is not None:
            value = array_rot90(value, rot90)
        if op is not None:
            right = value_from_literal(rhs if rhs is not None else "0")
            value = array_binary_op(value, right, op)
        if matmul is not None:
            right = value_from_literal(matmul)
            value = array_matmul(value, right)
        if concat is not None:
            right = value_from_literal(concat)
            value = array_concatenate(value, right, axis=axis or 0)
        if stack is not None:
            right = value_from_literal(stack)
            value = array_stack(value, right, axis=axis or 0)
        if compare is not None:
            right = value_from_literal(rhs if rhs is not None else "0")
            value = array_compare(value, right, compare)
        if index is not None:
            value = array_index(value, index)
        if diagonal is not None:
            value = array_diagonal(value, diagonal)
        if reshape is not None:
            value = array_reshape(value, parse_shape(reshape))
        if ravel:
            value = array_ravel(value)
        if flatten:
            value = array_flatten(value)
        if flip:
            value = array_flip(value, axis=axis)
        if transpose:
            value = array_transpose(value)
        if swapaxes is not None:
            value = array_swapaxes(value, parse_int_list(swapaxes))
        if moveaxis is not None:
            value = array_moveaxis(value, parse_int_list(moveaxis))
        if squeeze:
            value = array_squeeze(value, axis=squeeze_axis)
        if expand_dims is not None:
            value = array_expand_dims(value, expand_dims)
        if reduce is not None:
            value = array_reduce(value, reduce, axis=axis, keepdims=keepdims)
        if argreduce is not None:
            value = array_arg_reduce(value, argreduce, axis=axis, keepdims=keepdims)
        if count_nonzero:
            value = array_count_nonzero(value, axis=axis, keepdims=keepdims)
        if nonzero:
            value = array_nonzero(value)
        if argwhere:
            value = array_argwhere(value)
        if flatnonzero:
            value = array_flatnonzero(value)
        if cumulative is not None:
            value = array_cumulative(value, cumulative, axis=axis)
        if sort:
            value = array_sort(value, axis=axis if axis is not None else -1)
        if argsort:
            value = array_argsort(value, axis=axis if axis is not None else -1)
        if searchsorted is not None:
            value = array_searchsorted(value, value_from_literal(searchsorted), side=side)
        if partition is not None:
            value = array_partition(value, partition, axis=axis if axis is not None else -1)
        if argpartition is not None:
            value = array_argpartition(value, argpartition, axis=axis if axis is not None else -1)
        if take is not None:
            value = array_take(value, parse_int_list(take), axis=axis)
        if put is not None:
            value = array_put(value, parse_int_list(put), value_from_literal(put_values if put_values is not None else "0"))
        if putmask is not None:
            value = array_putmask(
                value,
                value_from_literal(putmask, dtype="bool"),
                value_from_literal(putmask_values if putmask_values is not None else "0"),
            )
        if mask is not None:
            value = array_mask(value, value_from_literal(mask, dtype="bool"))
        if compress is not None:
            value = array_compress(value, value_from_literal(compress, dtype="bool"), axis=axis)
        if where is not None:
            value = array_where(
                value_from_literal(where, dtype="bool"),
                value,
                value_from_literal(otherwise if otherwise is not None else "0"),
            )
        if astype is not None:
            value = array_astype(value, astype)
        if copy:
            value = array_copy(value)
        report = value.as_dict()
        source = "literal"
    elif fill is not None:
        value = array_full_from_literal(parse_shape(shape or ""), fill, dtype=dtype)
        report = value.as_dict()
        source = "shape"
    else:
        layout = layout_from_shape(parse_shape(shape or ""), dtype=dtype)
        report = layout.as_dict()
        source = "shape"
    report["source"] = source
    report["schema"] = "pcc.array-core.v1"
    if fill is not None:
        report["fill"] = fill
    if arange is not None:
        report["arange"] = arange
    if zeros is not None:
        report["zeros"] = zeros
    if ones is not None:
        report["ones"] = ones
    if zeros_like:
        report["zeros_like"] = True
    if ones_like:
        report["ones_like"] = True
    if full_like is not None:
        report["full_like"] = full_like
    if eye is not None:
        report["eye"] = eye
    if linspace is not None:
        report["linspace"] = linspace
    if op is not None:
        report["op"] = op
        report["rhs"] = rhs
    if matmul is not None:
        report["matmul"] = matmul
    if concat is not None:
        report["concat"] = concat
    if stack is not None:
        report["stack"] = stack
    if unary is not None:
        report["unary"] = unary
    if clip is not None:
        report["clip"] = clip
    if broadcast_to is not None:
        report["broadcast_to"] = broadcast_to
    if repeat is not None:
        report["repeat"] = repeat
    if tile is not None:
        report["tile"] = tile
    if roll is not None:
        report["roll"] = roll
    if rot90 is not None:
        report["rot90"] = rot90
    if compare is not None:
        report["compare"] = compare
        report["rhs"] = rhs
    if index is not None:
        report["index"] = index
    if diagonal is not None:
        report["diagonal"] = diagonal
    if reshape is not None:
        report["reshape"] = reshape
    if ravel:
        report["ravel"] = True
    if flatten:
        report["flatten"] = True
    if flip:
        report["flip"] = True
    if transpose:
        report["transpose"] = True
    if swapaxes is not None:
        report["swapaxes"] = swapaxes
    if moveaxis is not None:
        report["moveaxis"] = moveaxis
    if squeeze:
        report["squeeze"] = True
    if squeeze_axis is not None:
        report["squeeze_axis"] = squeeze_axis
    if expand_dims is not None:
        report["expand_dims"] = expand_dims
    if reduce is not None:
        report["reduce"] = reduce
    if argreduce is not None:
        report["argreduce"] = argreduce
    if count_nonzero:
        report["count_nonzero"] = True
    if nonzero:
        report["nonzero"] = True
    if argwhere:
        report["argwhere"] = True
    if flatnonzero:
        report["flatnonzero"] = True
    if cumulative is not None:
        report["cumulative"] = cumulative
    if sort:
        report["sort"] = True
    if argsort:
        report["argsort"] = True
    if searchsorted is not None:
        report["searchsorted"] = searchsorted
        report["side"] = side
    if partition is not None:
        report["partition"] = partition
    if argpartition is not None:
        report["argpartition"] = argpartition
    if axis is not None:
        report["axis"] = axis
    if keepdims:
        report["keepdims"] = True
    if take is not None:
        report["take"] = take
    if put is not None:
        report["put"] = put
        report["put_values"] = put_values
    if putmask is not None:
        report["putmask"] = putmask
        report["putmask_values"] = putmask_values
    if mask is not None:
        report["mask"] = mask
    if compress is not None:
        report["compress"] = compress
    if where is not None:
        report["where"] = where
        report["otherwise"] = otherwise
    if astype is not None:
        report["astype"] = astype
    if copy:
        report["copy"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package array-core")
    parser.add_argument("--shape", default=None)
    parser.add_argument("--literal", default=None)
    parser.add_argument("--dtype", default="auto")
    parser.add_argument("--require-rectangular", action="store_true")
    parser.add_argument("--fill", default=None)
    parser.add_argument("--arange", default=None)
    parser.add_argument("--zeros", default=None)
    parser.add_argument("--ones", default=None)
    parser.add_argument("--zeros-like", action="store_true")
    parser.add_argument("--ones-like", action="store_true")
    parser.add_argument("--full-like", default=None)
    parser.add_argument("--eye", default=None)
    parser.add_argument("--linspace", default=None)
    parser.add_argument("--op", choices=["add", "sub", "mul", "div", "subtract", "multiply", "divide"], default=None)
    parser.add_argument("--rhs", default=None)
    parser.add_argument("--matmul", default=None)
    parser.add_argument("--concat", default=None)
    parser.add_argument("--stack", default=None)
    parser.add_argument("--unary", choices=["neg", "negative", "abs", "absolute", "logical_not", "not"], default=None)
    parser.add_argument("--clip", default=None)
    parser.add_argument("--broadcast-to", default=None)
    parser.add_argument("--repeat", type=int, default=None)
    parser.add_argument("--tile", default=None)
    parser.add_argument("--roll", type=int, default=None)
    parser.add_argument("--rot90", type=int, default=None)
    parser.add_argument("--compare", choices=["eq", "ne", "lt", "le", "gt", "ge", "==", "!=", "<", "<=", ">", ">="], default=None)
    parser.add_argument("--index", default=None)
    parser.add_argument("--diagonal", type=int, default=None)
    parser.add_argument("--reshape", default=None)
    parser.add_argument("--ravel", action="store_true")
    parser.add_argument("--flatten", action="store_true")
    parser.add_argument("--flip", action="store_true")
    parser.add_argument("--transpose", action="store_true")
    parser.add_argument("--swapaxes", default=None)
    parser.add_argument("--moveaxis", default=None)
    parser.add_argument("--squeeze", action="store_true")
    parser.add_argument("--squeeze-axis", type=int, default=None)
    parser.add_argument("--expand-dims", type=int, default=None)
    parser.add_argument("--reduce", choices=["sum", "prod", "min", "max", "mean", "any", "all"], default=None)
    parser.add_argument("--argreduce", choices=["argmin", "argmax"], default=None)
    parser.add_argument("--count-nonzero", action="store_true")
    parser.add_argument("--nonzero", action="store_true")
    parser.add_argument("--argwhere", action="store_true")
    parser.add_argument("--flatnonzero", action="store_true")
    parser.add_argument("--cumulative", choices=["cumsum", "cumprod"], default=None)
    parser.add_argument("--sort", action="store_true")
    parser.add_argument("--argsort", action="store_true")
    parser.add_argument("--searchsorted", default=None)
    parser.add_argument("--side", choices=["left", "right"], default="left")
    parser.add_argument("--partition", type=int, default=None)
    parser.add_argument("--argpartition", type=int, default=None)
    parser.add_argument("--axis", type=int, default=None)
    parser.add_argument("--keepdims", action="store_true")
    parser.add_argument("--take", default=None)
    parser.add_argument("--put", default=None)
    parser.add_argument("--put-values", default=None)
    parser.add_argument("--putmask", default=None)
    parser.add_argument("--putmask-values", default=None)
    parser.add_argument("--mask", default=None)
    parser.add_argument("--compress", default=None)
    parser.add_argument("--where", default=None)
    parser.add_argument("--otherwise", default=None)
    parser.add_argument("--astype", default=None)
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    result = array_core_report(
        shape=ns.shape,
        literal=ns.literal,
        dtype=ns.dtype,
        require_rectangular=ns.require_rectangular,
        fill=ns.fill,
        arange=ns.arange,
        zeros=ns.zeros,
        ones=ns.ones,
        zeros_like=ns.zeros_like,
        ones_like=ns.ones_like,
        full_like=ns.full_like,
        eye=ns.eye,
        linspace=ns.linspace,
        op=ns.op,
        rhs=ns.rhs,
        matmul=ns.matmul,
        concat=ns.concat,
        stack=ns.stack,
        unary=ns.unary,
        clip=ns.clip,
        broadcast_to=ns.broadcast_to,
        repeat=ns.repeat,
        tile=ns.tile,
        roll=ns.roll,
        rot90=ns.rot90,
        compare=ns.compare,
        index=ns.index,
        diagonal=ns.diagonal,
        reshape=ns.reshape,
        ravel=ns.ravel,
        flatten=ns.flatten,
        flip=ns.flip,
        transpose=ns.transpose,
        swapaxes=ns.swapaxes,
        moveaxis=ns.moveaxis,
        squeeze=ns.squeeze,
        squeeze_axis=ns.squeeze_axis,
        expand_dims=ns.expand_dims,
        reduce=ns.reduce,
        argreduce=ns.argreduce,
        count_nonzero=ns.count_nonzero,
        nonzero=ns.nonzero,
        argwhere=ns.argwhere,
        flatnonzero=ns.flatnonzero,
        cumulative=ns.cumulative,
        sort=ns.sort,
        argsort=ns.argsort,
        searchsorted=ns.searchsorted,
        side=ns.side,
        partition=ns.partition,
        argpartition=ns.argpartition,
        axis=ns.axis,
        keepdims=ns.keepdims,
        take=ns.take,
        put=ns.put,
        put_values=ns.put_values,
        putmask=ns.putmask,
        putmask_values=ns.putmask_values,
        mask=ns.mask,
        compress=ns.compress,
        where=ns.where,
        otherwise=ns.otherwise,
        astype=ns.astype,
        copy=ns.copy,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
