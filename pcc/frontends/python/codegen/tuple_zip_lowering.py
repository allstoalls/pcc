"""Tuple and zip builtin lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, Call, ClassType, DictType, DynType, ListExpr, ListType, TupleExpr, TupleType
from pcc.frontends.python.codegen.bootstrap_trace import bootstrap_trace_enabled
from pcc.frontends.python.codegen import marshal

_I64 = ir.IntType(64)


def _type_name(ty: object) -> str:
    try:
        name = ty.name
    except AttributeError:
        return ""
    return str(name)


def _expr_kind(expr: object) -> str:
    return type(expr).__name__


def _expr_elems(expr: object):
    try:
        return expr.elems
    except AttributeError:
        return None


def _is_literal_tuple_or_list(expr: object) -> bool:
    if isinstance(expr, (ListExpr, TupleExpr)):
        return True
    kind = _expr_kind(expr)
    if kind not in ("ListExpr", "TupleExpr"):
        return False
    return _expr_elems(expr) is not None


def _dict_key_type(ty: object):
    try:
        return ty.key
    except AttributeError:
        return DynType(name="dyn")


class TupleZipLoweringMixin:
    def _maybe_emit_tuple_method(self, expr: Call) -> Optional[ir.Value]:
        """``t.count(x)`` / ``t.index(x[, start[, stop]])`` for a tuple, via the
        runtime py_tuple_count / py_tuple_index[_range] helpers (all return
        i64). index() raises ValueError when absent (or absent within the
        [start, stop) window). Other methods fall back."""
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        if expr.kwargs or self._has_unpacked_operands(expr.args, expr.kwargs):
            return None
        name = attr.name
        if name not in ("count", "index"):
            return None
        n_args = len(expr.args)
        if name == "count":
            if n_args != 1:
                return None
            recv = self._emit_as_object(attr.obj)
            item = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_tuple_count"],
                [recv, item],
                name=self._fresh("tuple.count"),
            )
        # name == "index": accept the 1-arg form and the 2/3-arg
        # index(x, start[, stop]) form.
        if n_args not in (1, 2, 3):
            return None
        recv = self._emit_as_object(attr.obj)
        item = self._emit_as_object(expr.args[0])
        if n_args == 1:
            res = self.builder.call(
                self.runtime["py_tuple_index"],
                [recv, item],
                name=self._fresh("tuple.index"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return res
        # 2/3-arg range form. start is required here; stop is a NULL
        # PyObject* when omitted, which the runtime treats as "to the end".
        null_obj = ir.Constant(ir.IntType(8).as_pointer(), None)
        start = self._emit_as_object(expr.args[1])
        stop = self._emit_as_object(expr.args[2]) if n_args == 3 else null_obj
        res = self.builder.call(
            self.runtime["py_tuple_index_range"],
            [recv, item, start, stop],
            name=self._fresh("tuple.index.range"),
        )
        self._emit_post_call_err_check(getattr(expr, "span", None))
        return res

    def _maybe_emit_tuple_builtin(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """Use the shared rooted sequence transaction, preserving identity."""
        return self._maybe_emit_list_builtin(expr, tuple_result=True)

    def _maybe_emit_zip_builtin(self, expr: Call) -> Optional[ir.Value]:
        """Materialise zip in registered roots, publishing before any cleanup.

        The result, each input (including a dict's keys view), and each loop
        temporary has an independent owning slot. Raw runtime arguments are
        exposed only under counted leases; no SSA object crosses an allocation,
        error check, root retirement, or the evaluation of a later argument.
        """
        if not expr.args:
            return None
        strict_expr = None
        for kwarg_name, kwarg_value in expr.kwargs:
            if kwarg_name != "strict":
                return None
            strict_expr = kwarg_value
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("zip.result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if self._is_starred_unpack(expr.args):
                # This helper returns a NEW list. Its internal allocations
                # remain the runtime's responsibility; the outer rows object
                # is leased and the result is published before that lease ends.
                rows_expr = expr.args[0].args[0]
                rows = self._emit_slot_call_operand(rows_expr, "zip.star.rows")
                roots.append(rows)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_zip_star", (rows,), result_slot=output, span=expr.span,
                )
            else:
                sources = []
                lengths = []
                dynamic_sources = []
                for index, arg in enumerate(expr.args):
                    source = self._emit_slot_call_operand(arg, f"zip.arg.{index}")
                    roots.append(source)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    dynamic = isinstance(arg.ty, DynType) or _type_name(arg.ty) == "dyn"
                    if isinstance(arg.ty, DictType) or _type_name(arg.ty) == "dict":
                        # Positional getitem on a mapping is key lookup, so
                        # preserve the existing keys-view normalisation.
                        keys = self._new_slot_call_root(f"zip.arg.{index}.keys")
                        roots.append(keys)
                        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                        self._cpy_operand_cleanup_block = self._try_err_block
                        self._slot_call_runtime_call(
                            "py_dict_keys", (source,), result_slot=keys, span=expr.span,
                        )
                        source = keys
                    sources.append(source)
                    dynamic_sources.append(dynamic)
                    lengths.append(self._slot_call_runtime_call(
                        "py_obj_len", (source,), span=expr.span,
                    ))
                    if dynamic:
                        self._emit_zip_scalar_guard(source, expr.span)
                fn = self.current_function
                if strict_expr is not None and len(lengths) > 1:
                    strict_i1 = self._emit_condition_value(strict_expr)
                    mismatch = ir.Constant(ir.IntType(1), 0)
                    for index, length in enumerate(lengths[1:], start=1):
                        unequal = self.builder.icmp_signed(
                            "!=", length, lengths[0], name=self._fresh(f"zip.strict.ne.{index}"),
                        )
                        mismatch = self.builder.or_(mismatch, unequal, name=self._fresh("zip.strict.any"))
                    violated = self.builder.and_(strict_i1, mismatch, name=self._fresh("zip.strict.violated"))
                    bad_bb = fn.append_basic_block(name=self._fresh("zip.strict.bad"))
                    ok_bb = fn.append_basic_block(name=self._fresh("zip.strict.ok"))
                    self.builder.cbranch(violated, bad_bb, ok_bb)
                    self.builder.position_at_end(bad_bb)
                    self._emit_builtin_exception_and_branch(
                        "ValueError", "zip() arguments have different lengths (strict=True)", expr.span,
                    )
                    self.builder.position_at_end(ok_bb)
                min_len = lengths[0]
                for index, length in enumerate(lengths[1:], start=1):
                    take_length = self.builder.icmp_signed(
                        "<", length, min_len, name=self._fresh(f"zip.min.cmp.{index}"),
                    )
                    min_len = self.builder.select(take_length, length, min_len, name=self._fresh("zip.min"))
                self._slot_call_runtime_call(
                    "py_list_new", (), result_slot=output, suffix_args=(min_len,), span=expr.span,
                )
                index_slot = self._alloca_in_entry(_I64, name="zip.idx.addr")
                self.builder.store(ir.Constant(_I64, 0), index_slot)
                cond_bb = fn.append_basic_block(name=self._fresh("zip.cond"))
                body_bb = fn.append_basic_block(name=self._fresh("zip.body"))
                end_bb = fn.append_basic_block(name=self._fresh("zip.end"))
                self.builder.branch(cond_bb)
                self.builder.position_at_end(cond_bb)
                current = self.builder.load(index_slot, name=self._fresh("zip.idx"))
                condition = self.builder.icmp_signed("<", current, min_len, name=self._fresh("zip.cond.i1"))
                self.builder.cbranch(condition, body_bb, end_bb)
                self.builder.position_at_end(body_bb)
                boxed_index = self._new_slot_call_root("zip.index")
                row = self._new_slot_call_root("zip.row")
                loop_roots = tuple(roots) + (boxed_index, row)
                self._try_err_block = self._slot_call_cleanup_block(loop_roots, target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_int_from_i64", (), result_slot=boxed_index, suffix_args=(current,), span=expr.span,
                )
                self._slot_call_runtime_call(
                    "py_tuple_new", (), result_slot=row,
                    suffix_args=(ir.Constant(_I64, len(sources)),), span=expr.span,
                )
                for index, source in enumerate(sources):
                    element = self._new_slot_call_root(f"zip.element.{index}")
                    self._try_err_block = self._slot_call_cleanup_block(loop_roots + (element,), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call(
                        "py_obj_getitem", (source, boxed_index), result_slot=element, span=expr.span,
                    )
                    if dynamic_sources[index]:
                        value = self.builder.load(element, name=self._fresh("zip.element.current"))
                        missing = self.builder.icmp_unsigned("==", value, ir.Constant(value.type, None))
                        bad_bb = fn.append_basic_block(name=self._fresh("zip.elem.bad"))
                        ok_bb = fn.append_basic_block(name=self._fresh("zip.elem.ok"))
                        self.builder.cbranch(missing, bad_bb, ok_bb)
                        self.builder.position_at_end(bad_bb)
                        self._emit_builtin_exception_and_branch(
                            "TypeError", "zip() argument is not indexable from 0..len-1"
                            " (mappings iterate by key in CPython; unsupported here for dyn sources)", expr.span,
                        )
                        self.builder.position_at_end(ok_bb)
                    # Both setters retain. Release our owner only after the
                    # retaining call has returned and all its leases ended.
                    self._slot_call_runtime_call(
                        "py_tuple_set_item", (row, element),
                        suffix_args=(ir.Constant(_I64, index),), argument_order=(0, 2, 1), span=expr.span,
                    )
                    self._release_slot_call_roots((element,))
                    self._try_err_block = self._slot_call_cleanup_block(loop_roots, target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call("py_list_append", (output, row), span=expr.span)
                self._release_slot_call_roots((boxed_index, row))
                self.builder.store(self.builder.add(current, ir.Constant(_I64, 1)), index_slot)
                self.builder.branch(cond_bb)
                self.builder.position_at_end(end_bb)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            self._release_slot_call_roots(tuple(roots if sink is not None else roots[1:]))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("zip.result.current"))
        return self._take_slot_call_root(output)

    def _emit_zip_scalar_guard(self, source, span) -> None:
        """Keep the legacy dyn scalar rejection without an unleased raw load."""
        from pcc.frontends.python.codegen.freestanding_abi_constants import (
            PY_TYPE_BOOL,
            PY_TYPE_FLOAT,
            PY_TYPE_INT,
            PY_TYPE_NONE,
        )

        tag = self._slot_call_runtime_call("py_obj_type_tag", (source,), span=span)
        scalar = ir.Constant(ir.IntType(1), 0)
        for tag_value in (PY_TYPE_INT, PY_TYPE_FLOAT, PY_TYPE_BOOL, PY_TYPE_NONE):
            matches = self.builder.icmp_signed("==", tag, ir.Constant(_I64, tag_value))
            scalar = self.builder.or_(scalar, matches)
        bad_bb = self.current_function.append_basic_block(name=self._fresh("zip.scalar.bad"))
        ok_bb = self.current_function.append_basic_block(name=self._fresh("zip.scalar.ok"))
        self.builder.cbranch(scalar, bad_bb, ok_bb)
        self.builder.position_at_end(bad_bb)
        # CPython's iter() error; the call always raises, so its error edge
        # leaves this block and the continuation is never reached.
        self._slot_call_runtime_call("py_obj_raise_not_iterable", (source,), span=span)
        self.builder.branch(self._current_try_err_block() or self._ensure_fn_err_exit())
        self.builder.position_at_end(ok_bb)
