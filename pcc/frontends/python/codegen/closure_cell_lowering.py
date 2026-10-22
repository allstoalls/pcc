"""Compiler-owned access to a closure's ordinary list-backed storage.

An internal NULL payload is unbound. Python None is the distinct py_None
object. The normal list getter/setter retain the shared GC slot/barrier and
publication-before-finalization contracts; no physical list layout is exposed
here. Only impossible source identifiers emitted by hoist_boxing reach this
lowering, so user callables cannot accidentally select it.
"""
from pcc.ir.compat import ir
from pcc.frontends.python.py_ast import (
    BoolLit,
    Call,
    Name,
    StrLit,
)
from pcc.frontends.python.codegen.hoist_boxing import (
    CELL_CAPTURE,
    CELL_READ,
    CELL_UNBOUND,
    cell_capture_key,
)


def emit_closure_cell_expr(host, expr):
    if not isinstance(expr, Call) or not isinstance(expr.func, Name):
        return None
    capture = expr.func.ident == CELL_CAPTURE
    if capture:
        if len(expr.args) != 2 or expr.kwargs or not isinstance(expr.args[1], StrLit):
            raise ValueError("malformed compiler class-cell capture")
    else:
        if expr.func.ident == CELL_UNBOUND and not expr.args and not expr.kwargs:
            return ir.Constant(ir.IntType(8).as_pointer(), None)
        if expr.func.ident != CELL_READ:
            return None
        if (len(expr.args) != 3 or expr.kwargs
                or not isinstance(expr.args[1], StrLit)
                or not isinstance(expr.args[2], BoolLit)):
            raise ValueError("malformed compiler closure-cell read")

    sink = host._slot_call_result_sink(expr)
    output = sink
    roots = []
    if output is None:
        output = host._new_slot_call_root("cell.payload")
        roots.append(output)
    previous = host._current_try_err_block()
    target = previous if previous is not None else host._ensure_fn_err_exit()
    saved_cleanup = host._cpy_operand_cleanup_block
    try:
        host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
        host._cpy_operand_cleanup_block = host._try_err_block
        owner = host._emit_slot_call_operand(expr.args[0], "cell.source")
        roots.append(owner)
        host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
        host._cpy_operand_cleanup_block = host._try_err_block
        if capture:
            key = cell_capture_key(expr.args[1].value)
            host._slot_call_runtime_call(
                "py_instance_getattr_default", (owner,), result_slot=output,
                suffix_args=(host._attr_name_ptr(key),), span=expr.span,
            )
            value = host.builder.load(output, name=host._fresh("class.cell.capture"))
            host._emit_attribute_error_if_null(value, key, expr.span)
        else:
            # py_list_getitem returns NEW: GC0 retains the selected item;
            # other modes retain the actual traced item under the graph lock
            # and preserve its result root through the terminal return bridge.
            # Keep the actual list owner leased through that call, and publish
            # its result before error probing or releasing the list temporary.
            host._slot_call_runtime_call(
                "py_list_getitem", (owner,), result_slot=output,
                suffix_args=(ir.Constant(ir.IntType(64), 0),), span=expr.span,
            )
            value = host.builder.load(output, name=host._fresh("cell.value"))
            # The runtime already raised IndexError for malformed empty cells.
            # Only a successful NULL payload denotes an unbound variable.
            bound = host.builder.icmp_unsigned(
                "!=", value, ir.Constant(value.type, None),
                name=host._fresh("cell.bound"),
            )
            ready = host.current_function.append_basic_block(name=host._fresh("cell.bound.ok"))
            missing = host.current_function.append_basic_block(name=host._fresh("cell.bound.error"))
            host.builder.cbranch(bound, ready, missing)
            host.builder.position_at_end(missing)
            name = expr.args[1].value
            if expr.args[2].value:
                exc = "NameError"
                message = "cannot access free variable '" + name + "' where it is not associated with a value in enclosing scope"
            else:
                exc = "UnboundLocalError"
                message = "cannot access local variable '" + name + "' where it is not associated with a value"
            host._emit_builtin_exception_and_branch(exc, message, expr.span)
            host.builder.position_at_end(ready)
        host._release_slot_call_roots((owner,))
        host._slot_call_note_published(output)
        if sink is not None:
            return host.builder.load(output, name=host._fresh("cell.current"))
        return host._take_slot_call_root(output)
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cleanup
