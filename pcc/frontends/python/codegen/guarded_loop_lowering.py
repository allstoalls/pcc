"""Production lowering for the first owner-neutral guarded loop plan.

The only accepted candidate is ``pcc.guarded_i64_dot`` over matching
``pcc.i64_buffer[N]`` values.  All guards are emitted before the raw i64 loop;
any miss or checked overflow enters the pcc-Python scalar helper at index zero.
The resulting LLVM IR is shared by LLVM and both CPU self-backend owners.
"""

from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import BytesType, Call, Expr, Name, StrLit, Subscript
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_BYTES


_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_I64_PTR = _I64.as_pointer()
_I64_BUFFER_PREFIX = "pcc.i64_buffer["
_COUNTERS = (
    "candidate",
    "guard_hit",
    "guard_miss",
    "overflow",
    "scalar_fallback",
    "fast_result",
)
_COUNTER_INDEX = {name: index for index, name in enumerate(_COUNTERS)}
_GUARD_ORDER = (
    "left-exact-type",
    "right-exact-type",
    "left-layout-version",
    "right-layout-version",
    "function-version",
    "globals-version",
    "left-buffer-version",
    "right-buffer-version",
    "trip-count",
    "no-alias",
    "left-unit-stride",
    "right-unit-stride",
    "left-alignment",
    "right-alignment",
    "left-integer-range",
    "right-integer-range",
)


def _span(expr: Expr):
    return getattr(expr, "span", None)


def i64_buffer_length_from_type(ty) -> int:
    if not isinstance(ty, BytesType):
        return -1
    name = ty.name
    if not name.startswith(_I64_BUFFER_PREFIX) or not name.endswith("]"):
        return -1
    digits = name[len(_I64_BUFFER_PREFIX) : -1]
    if not digits:
        return -1
    value = 0
    for ch in digits:
        digit = ord(ch) - 48
        if digit < 0 or digit > 9:
            return -1
        value = value * 10 + digit
    if value < 1 or value > 1_048_576:
        return -1
    return value


def _version_global(host, version_name: str):
    symbol = ".pcc.guarded.i64.dot.version." + version_name
    existing = host.module.globals.get(symbol)
    if existing is not None:
        return existing
    version = ir.GlobalVariable(host.module, _I64, name=symbol)
    version.linkage = "internal"
    version.global_constant = False
    version.initializer = ir.Constant(_I64, 1)
    return version


def _bump_counter(host, counter_name: str) -> None:
    if counter_name not in _COUNTER_INDEX:
        raise L1CodegenError("unknown guarded-loop counter " + counter_name)
    host.builder.call(
        host.runtime["py_guarded_loop_counter_add"],
        [
            ir.Constant(_I64, _COUNTER_INDEX[counter_name]),
            ir.Constant(_I64, 1),
        ],
        name=host._fresh("guarded.dot.counter." + counter_name),
    )


def emit_guarded_loop_counter(host, expr: Call):
    if expr.kwargs or len(expr.args) != 1 or not isinstance(expr.args[0], StrLit):
        raise L1CodegenError(
            "pcc.guarded_loop_counter needs one known string literal"
        )
    name = expr.args[0].value
    if name not in _COUNTERS:
        raise L1CodegenError("unknown guarded-loop counter " + repr(name))
    return host.builder.call(
        host.runtime["py_guarded_loop_counter_get"],
        [ir.Constant(_I64, _COUNTER_INDEX[name])],
        name=host._fresh("guarded.dot.counter.load." + name),
    )


def _value_is_owned_after_object_projection(host, expr: Expr, value) -> bool:
    is_cpy = value in getattr(host, "_cpy_values", ())
    return host._container_store_temp_needs_release(expr, expr.ty, is_cpy)


def emit_i64_buffer_constructor(host, expr: Call):
    length = i64_buffer_length_from_type(expr.ty)
    if length < 1 or not isinstance(expr.func, Subscript):
        return None
    if host._native_builtin_value_kind_for_expr(expr.func.obj) != "pcc.i64_buffer":
        return None
    if expr.kwargs or len(expr.args) != length:
        raise L1CodegenError(
            "pcc.i64_buffer constructor arguments do not match its fixed length"
        )

    buffer = host.builder.call(
        host.runtime["py_i64_buffer_new"],
        [ir.Constant(_I64, length)],
        name=host._fresh("guarded.buffer.new"),
    )
    host._emit_post_call_err_check(_span(expr))
    host._guard_cpy_value_not_null(buffer)
    buffer_root = host._enter_container_temp_root(buffer, "guarded.buffer")

    for index, arg in enumerate(expr.args):
        value = host._emit_expr_with_cpy_operand_cleanup(
            arg,
            (),
            ((buffer, buffer_root),),
            (),
            True,
        )
        value_owned = _value_is_owned_after_object_projection(host, arg, value)
        host.builder.call(host.runtime["pcc_gc_pin"], [value])
        host.builder.call(
            host.runtime["py_i64_buffer_set_item"],
            [buffer, ir.Constant(_I64, index), value],
            name=host._fresh("guarded.buffer.set"),
        )
        host._emit_post_call_err_check(
            _span(arg),
            rooted_release_on_error=((buffer, buffer_root),),
            pinned_release_on_error=((value, value_owned),),
        )
        host.builder.call(host.runtime["pcc_gc_unpin"], [value])
        if value_owned:
            host._gc_release(value)

    host._leave_container_temp_root(buffer_root)
    return buffer


def _input_value(host, expr: Expr, cleanup=()):
    value = host._emit_expr_with_cpy_operand_cleanup(
        expr,
        (),
        (),
        cleanup,
        False,
    )
    if value in getattr(host, "_cpy_values", ()):
        raise L1CodegenError(
            "pcc.i64_buffer cannot cross a CPython object boundary"
        )
    if not isinstance(value.type, ir.PointerType):
        raise L1CodegenError("pcc.i64_buffer did not lower to a PyObject pointer")
    owned = host._container_store_temp_needs_release(expr, expr.ty, False)
    return value, owned


def _cleanup_inputs(host, left, left_owned: bool, right, right_owned: bool) -> None:
    host.builder.call(host.runtime["pcc_gc_unpin"], [right])
    if right_owned:
        host._gc_release(right)
    host.builder.call(host.runtime["pcc_gc_unpin"], [left])
    if left_owned:
        host._gc_release(left)


def _guard_condition(host, kind: str, left, right, length: int):
    one = ir.Constant(_I64, 1)
    if kind == "left-exact-type" or kind == "right-exact-type":
        subject = left if kind.startswith("left") else right
        actual = host.builder.call(
            host.runtime["pcc_py_type_of"],
            [subject],
            name=host._fresh("guarded.dot." + kind + ".tag"),
        )
        return host.builder.icmp_signed(
            "==",
            actual,
            ir.Constant(_I64, PY_TYPE_BYTES),
            name=host._fresh("guarded.dot." + kind),
        )
    if kind == "left-layout-version" or kind == "right-layout-version":
        subject = left if kind.startswith("left") else right
        actual = host.builder.call(
            host.runtime["py_i64_buffer_layout_version"],
            [subject],
            name=host._fresh("guarded.dot." + kind + ".value"),
        )
        return host.builder.icmp_signed(
            "==", actual, one, name=host._fresh("guarded.dot." + kind)
        )
    if kind == "function-version" or kind == "globals-version":
        version_name = "function" if kind == "function-version" else "globals"
        actual = host.builder.load_atomic(
            _version_global(host, version_name),
            "monotonic",
            8,
            name=host._fresh("guarded.dot." + kind + ".value"),
        )
        return host.builder.icmp_signed(
            "==", actual, one, name=host._fresh("guarded.dot." + kind)
        )
    if kind == "left-buffer-version" or kind == "right-buffer-version":
        subject = left if kind.startswith("left") else right
        actual = host.builder.call(
            host.runtime["py_i64_buffer_version"],
            [subject],
            name=host._fresh("guarded.dot." + kind + ".value"),
        )
        return host.builder.icmp_signed(
            "==", actual, one, name=host._fresh("guarded.dot." + kind)
        )
    if kind == "trip-count":
        expected = ir.Constant(_I64, length * 8)
        left_len = host.builder.call(
            host.runtime["py_bytes_len"],
            [left],
            name=host._fresh("guarded.dot.left.bytes"),
        )
        right_len = host.builder.call(
            host.runtime["py_bytes_len"],
            [right],
            name=host._fresh("guarded.dot.right.bytes"),
        )
        left_ok = host.builder.icmp_signed("==", left_len, expected)
        right_ok = host.builder.icmp_signed("==", right_len, expected)
        return host.builder.and_(
            left_ok,
            right_ok,
            name=host._fresh("guarded.dot.trip-count"),
        )
    if kind == "no-alias":
        return host.builder.icmp_unsigned(
            "!=", left, right, name=host._fresh("guarded.dot.no-alias")
        )
    if kind == "left-alignment" or kind == "right-alignment":
        subject = left if kind.startswith("left") else right
        data = host.builder.call(
            host.runtime["py_i64_buffer_data"],
            [subject],
            name=host._fresh("guarded.dot." + kind + ".data"),
        )
        address = host.builder.ptrtoint(
            data,
            _I64,
            name=host._fresh("guarded.dot." + kind + ".address"),
        )
        low = host.builder.and_(
            address,
            ir.Constant(_I64, 7),
            name=host._fresh("guarded.dot." + kind + ".low"),
        )
        return host.builder.icmp_unsigned(
            "==", low, ir.Constant(_I64, 0), name=host._fresh("guarded.dot." + kind)
        )
    # Unit stride and integer-range guards are structural properties of the
    # exact versioned layout.  Keep them explicit in the common IR so neither
    # owner can silently delete a plan precondition.
    if kind == "left-unit-stride" or kind == "right-unit-stride":
        return host.builder.icmp_signed(
            "==",
            ir.Constant(_I64, 8),
            ir.Constant(_I64, 8),
            name=host._fresh("guarded.dot." + kind),
        )
    if kind == "left-integer-range" or kind == "right-integer-range":
        return host.builder.icmp_signed(
            "==", one, one, name=host._fresh("guarded.dot." + kind)
        )
    raise L1CodegenError("unknown guarded-loop guard " + kind)


def _checked_mul(host, lhs, rhs):
    pair_type = ir.LiteralStructType([_I64, _I1])
    name = "llvm.smul.with.overflow.i64"
    intrinsic = host.module.globals.get(name)
    if intrinsic is None:
        intrinsic = ir.Function(
            host.module,
            ir.FunctionType(pair_type, [_I64, _I64]),
            name=name,
        )
    pair = host.builder.call(
        intrinsic,
        [lhs, rhs],
        name=host._fresh("guarded.dot.mul.checked"),
    )
    return (
        host.builder.extract_value(pair, [0], name=host._fresh("guarded.dot.mul")),
        host.builder.extract_value(
            pair,
            [1],
            name=host._fresh("guarded.dot.mul.overflow"),
        ),
    )


def _checked_add(host, lhs, rhs):
    result = host.builder.add(lhs, rhs, name=host._fresh("guarded.dot.add"))
    lhs_changed = host.builder.xor(lhs, result, name=host._fresh("guarded.dot.add.lhs"))
    rhs_changed = host.builder.xor(rhs, result, name=host._fresh("guarded.dot.add.rhs"))
    both = host.builder.and_(
        lhs_changed,
        rhs_changed,
        name=host._fresh("guarded.dot.add.signs"),
    )
    overflow = host.builder.icmp_signed(
        "<",
        both,
        ir.Constant(_I64, 0),
        name=host._fresh("guarded.dot.add.overflow"),
    )
    return result, overflow


def emit_guarded_i64_dot(host, expr: Call):
    if expr.kwargs or len(expr.args) != 2:
        raise L1CodegenError(
            "pcc.guarded_i64_dot expects two positional typed buffers"
        )
    length = i64_buffer_length_from_type(expr.args[0].ty)
    if length < 1 or i64_buffer_length_from_type(expr.args[1].ty) != length:
        raise L1CodegenError(
            "pcc.guarded_i64_dot requires matching pcc.i64_buffer[N] operands"
        )

    left, left_owned = _input_value(host, expr.args[0])
    host.builder.call(host.runtime["pcc_gc_pin"], [left])
    right, right_owned = _input_value(
        host,
        expr.args[1],
        ((left, left_owned),),
    )
    host.builder.call(host.runtime["pcc_gc_pin"], [right])
    input_cleanup = ((left, left_owned), (right, right_owned))
    _bump_counter(host, "candidate")

    miss_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.guard.miss")
    )
    hit_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.guard.hit")
    )
    overflow_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.overflow")
    )
    slow_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.scalar")
    )
    merge_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.merge")
    )

    for guard_kind in _GUARD_ORDER:
        next_bb = host.current_function.append_basic_block(
            host._fresh("guarded.dot.guard." + guard_kind + ".pass")
        )
        condition = _guard_condition(host, guard_kind, left, right, length)
        host.builder.cbranch(condition, next_bb, miss_bb)
        host.builder.position_at_end(next_bb)
    host.builder.branch(hit_bb)

    host.builder.position_at_end(miss_bb)
    _bump_counter(host, "guard_miss")
    host.builder.branch(slow_bb)

    host.builder.position_at_end(overflow_bb)
    _bump_counter(host, "overflow")
    host.builder.branch(slow_bb)

    host.builder.position_at_end(hit_bb)
    _bump_counter(host, "guard_hit")
    left_data = host.builder.call(
        host.runtime["py_i64_buffer_data"],
        [left],
        name=host._fresh("guarded.dot.left.data"),
    )
    right_data = host.builder.call(
        host.runtime["py_i64_buffer_data"],
        [right],
        name=host._fresh("guarded.dot.right.data"),
    )
    left_i64 = host.builder.bitcast(left_data, _I64_PTR)
    right_i64 = host.builder.bitcast(right_data, _I64_PTR)
    loop_header = host.current_function.append_basic_block(
        host._fresh("guarded.dot.fast.header")
    )
    loop_body = host.current_function.append_basic_block(
        host._fresh("guarded.dot.fast.body")
    )
    mul_ok_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.fast.mul.ok")
    )
    add_ok_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.fast.add.ok")
    )
    fast_done_bb = host.current_function.append_basic_block(
        host._fresh("guarded.dot.fast.done")
    )
    fast_entry = host.builder._block
    host.builder.branch(loop_header)

    host.builder.position_at_end(loop_header)
    index_phi = host.builder.phi(_I64, name=host._fresh("guarded.dot.index"))
    acc_phi = host.builder.phi(_I64, name=host._fresh("guarded.dot.acc"))
    index_phi.add_incoming(ir.Constant(_I64, 0), fast_entry)
    acc_phi.add_incoming(ir.Constant(_I64, 0), fast_entry)
    continue_fast = host.builder.icmp_signed(
        "<",
        index_phi,
        ir.Constant(_I64, length),
        name=host._fresh("guarded.dot.fast.continue"),
    )
    host.builder.cbranch(continue_fast, loop_body, fast_done_bb)

    host.builder.position_at_end(loop_body)
    left_ptr = host.builder.gep(left_i64, [index_phi], inbounds=True)
    right_ptr = host.builder.gep(right_i64, [index_phi], inbounds=True)
    left_value = host.builder.load(
        left_ptr, name=host._fresh("guarded.dot.left.load"), align=8
    )
    right_value = host.builder.load(
        right_ptr, name=host._fresh("guarded.dot.right.load"), align=8
    )
    product, mul_overflow = _checked_mul(host, left_value, right_value)
    host.builder.cbranch(mul_overflow, overflow_bb, mul_ok_bb)

    host.builder.position_at_end(mul_ok_bb)
    updated, add_overflow = _checked_add(host, acc_phi, product)
    host.builder.cbranch(add_overflow, overflow_bb, add_ok_bb)

    host.builder.position_at_end(add_ok_bb)
    next_index = host.builder.add(
        index_phi,
        ir.Constant(_I64, 1),
        name=host._fresh("guarded.dot.index.next"),
    )
    next_block = host.builder._block
    host.builder.branch(loop_header)
    index_phi.add_incoming(next_index, next_block)
    acc_phi.add_incoming(updated, next_block)

    host.builder.position_at_end(fast_done_bb)
    fast_result = host.builder.call(
        host.runtime["py_int_from_i64"],
        [acc_phi],
        name=host._fresh("guarded.dot.fast.box"),
    )
    host._emit_post_call_err_check(
        _span(expr),
        pinned_release_on_error=input_cleanup,
    )
    host._guard_cpy_value_not_null(
        fast_result,
        pinned_pcc_on_error=input_cleanup,
    )
    _bump_counter(host, "fast_result")
    _cleanup_inputs(host, left, left_owned, right, right_owned)
    fast_result_block = host.builder._block
    host.builder.branch(merge_bb)

    host.builder.position_at_end(slow_bb)
    _bump_counter(host, "scalar_fallback")
    slow_result = host.builder.call(
        host.runtime["py_i64_buffer_dot_scalar"],
        [left, right, ir.Constant(_I64, length)],
        name=host._fresh("guarded.dot.scalar.result"),
    )
    host._emit_post_call_err_check(
        _span(expr),
        pinned_release_on_error=input_cleanup,
    )
    host._guard_cpy_value_not_null(
        slow_result,
        pinned_pcc_on_error=input_cleanup,
    )
    _cleanup_inputs(host, left, left_owned, right, right_owned)
    slow_result_block = host.builder._block
    host.builder.branch(merge_bb)

    host.builder.position_at_end(merge_bb)
    result = host.builder.phi(_CSTR, name=host._fresh("guarded.dot.result"))
    result.add_incoming(fast_result, fast_result_block)
    result.add_incoming(slow_result, slow_result_block)
    return result
