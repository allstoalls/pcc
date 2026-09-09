"""Misc statement/expression lowering helpers for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    ByteArrayType,
    Call,
    BoolType,
    DynType,
    ExprStmt,
    IntType,
    Name,
    Subscript,
)
from .errors import L1CodegenError
from .freestanding_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_LIST
from .for_loop_lowering import _for_prepare_owned_object_target, _for_store_owned_target
from .method_call_lowering import _method_pointer_provenance

_I64 = ir.IntType(64)


class StmtMiscLoweringMixin:
    def _emit_expr_stmt(self, stmt: ExprStmt) -> None:
        # Special-case top-level ``print(...)``.
        expr = stmt.expr
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident == "print"
        ):
            self._emit_print_call(expr)
            return
        if isinstance(expr, Call) and self._maybe_emit_static_test_runner_stmt(expr):
            return
        if isinstance(expr, Call) and self._emit_native_subprocess_run_stmt(expr):
            return
        if isinstance(expr, Call) and isinstance(expr.func, Attr):
            native_gc_callbacks_method = self._emit_native_gc_callbacks_method(expr)
            if native_gc_callbacks_method is not None:
                return
        if isinstance(expr, Call) and self._maybe_emit_bytearray_extend_stmt(expr):
            return
        if isinstance(expr, Call) and self._maybe_emit_bytearray_append_stmt(expr):
            return
        if isinstance(expr, Call) and self._maybe_emit_bytearray_insert_stmt(expr):
            return
        # Otherwise evaluate for side-effects.
        value = self._emit_expr(expr)
        if value in getattr(self, "_cpy_values", ()):
            if self._cpy_value_is_owned(value):
                self._guard_cpy_value_not_null(value)
                self.builder.call(self.runtime["py_cpy_decref"], [value])
                self._forget_owned_cpy_value(value)
            return
        self._gc_release_if_owned(value, expr)

    def _maybe_emit_bytearray_extend_stmt(self, expr: Call) -> bool:
        if (
            not isinstance(expr.func, Attr)
            or expr.func.name != "extend"
            or len(expr.args) != 1
            or expr.kwargs
        ):
            return False
        target = expr.func.obj
        if not isinstance(target, (Name, Attr, Subscript)):
            return False
        if isinstance(target.ty, ByteArrayType):
            self._emit_bytearray_extend_store(target, expr.args[0], expr.span)
            return True
        if isinstance(target.ty, DynType) and isinstance(target, Attr):
            return self._emit_dyn_attr_bytearray_extend_stmt(expr, target)
        return False

    def _emit_bytearray_extend_store(self, target, arg_expr, span) -> None:
        recv_obj = self._emit_as_object(target)
        arg_obj = self._emit_as_object(arg_expr)
        result = self.builder.call(
            self.runtime["py_bytearray_extend"],
            [recv_obj, arg_obj],
            name=self._fresh("bytearray.extend"),
        )
        self._emit_post_call_err_check(span)
        self._gc_release_if_owned(arg_obj, arg_expr)
        self._store_unpack_target(
            target,
            result,
            ByteArrayType(name="bytearray"),
            value_is_owned=True,
        )

    def _emit_dyn_attr_bytearray_extend_stmt(self, expr: Call, target: Attr) -> bool:
        if self.current_function is None:
            return False
        recv_obj = self._emit_as_object(target)
        tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [recv_obj],
            name=self._fresh("dyn.extend.recv.tag"),
        )
        is_bytearray = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_BYTEARRAY),
            name=self._fresh("dyn.extend.recv.is_bytearray"),
        )
        fn = self.current_function
        bytearray_bb = fn.append_basic_block(name=self._fresh("dyn.extend.bytearray"))
        not_bytearray_bb = fn.append_basic_block(
            name=self._fresh("dyn.extend.not_bytearray")
        )
        list_bb = fn.append_basic_block(name=self._fresh("dyn.extend.list"))
        generic_bb = fn.append_basic_block(name=self._fresh("dyn.extend.generic"))
        done_bb = fn.append_basic_block(name=self._fresh("dyn.extend.done"))
        self.builder.cbranch(is_bytearray, bytearray_bb, not_bytearray_bb)

        self.builder.position_at_end(bytearray_bb)
        arg_obj = self._emit_as_object(expr.args[0])
        result = self.builder.call(
            self.runtime["py_bytearray_extend"],
            [recv_obj, arg_obj],
            name=self._fresh("dyn.bytearray.extend"),
        )
        self._emit_post_call_err_check(expr.span)
        self._gc_release_if_owned(arg_obj, expr.args[0])
        self._store_unpack_target(
            target,
            result,
            ByteArrayType(name="bytearray"),
            value_is_owned=True,
        )
        self.builder.branch(done_bb)

        self.builder.position_at_end(not_bytearray_bb)
        is_list = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_LIST),
            name=self._fresh("dyn.extend.recv.is_list"),
        )
        self.builder.cbranch(is_list, list_bb, generic_bb)

        self.builder.position_at_end(list_bb)
        self._emit_dyn_list_native_method(recv_obj, expr)
        self.builder.branch(done_bb)

        self.builder.position_at_end(generic_bb)
        fallback_value = self._emit_generic_dyn_method_call_on_value(
            recv_obj,
            "extend",
            expr,
        )
        self._gc_release_if_owned(fallback_value, expr)
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        return True

    def _maybe_emit_bytearray_append_stmt(self, expr: Call) -> bool:
        if (
            not isinstance(expr.func, Attr)
            or expr.func.name != "append"
            or len(expr.args) != 1
            or expr.kwargs
        ):
            return False
        target = expr.func.obj
        if not isinstance(target, (Name, Attr, Subscript)):
            return False
        if isinstance(target.ty, ByteArrayType):
            self._emit_bytearray_append_store(target, expr.args[0], expr.span)
            return True
        if isinstance(target.ty, DynType) and isinstance(target, Attr):
            return self._emit_dyn_attr_bytearray_append_stmt(expr, target)
        return False

    def _emit_bytearray_append_store(self, target, arg_expr, span) -> None:
        recv_obj = self._emit_as_object(target)
        arg_obj = self._emit_as_object(arg_expr)
        result = self.builder.call(
            self.runtime["py_bytearray_append"],
            [recv_obj, arg_obj],
            name=self._fresh("bytearray.append"),
        )
        self._emit_post_call_err_check(span)
        self._gc_release_if_owned(arg_obj, arg_expr)
        self._store_unpack_target(
            target,
            result,
            ByteArrayType(name="bytearray"),
            value_is_owned=True,
        )

    def _maybe_emit_bytearray_insert_stmt(self, expr: Call) -> bool:
        if (
            not isinstance(expr.func, Attr)
            or expr.func.name != "insert"
            or len(expr.args) != 2
            or expr.kwargs
        ):
            return False
        target = expr.func.obj
        if not isinstance(target, (Name, Attr, Subscript)):
            return False
        if isinstance(target.ty, ByteArrayType):
            self._emit_bytearray_insert_store(
                target, expr.args[0], expr.args[1], expr.span
            )
            return True
        return False

    def _emit_bytearray_insert_store(
        self, target, index_expr, value_expr, span
    ) -> None:
        # bytearray.insert(index, byte) grows the buffer, so the runtime helper
        # rebuilds a fresh object; re-bind the target to the result (same model
        # as append/extend). The index/value objects are consumed by the call.
        recv_obj = self._emit_as_object(target)
        index_obj = self._emit_as_object(index_expr)
        value_obj = self._emit_as_object(value_expr)
        result = self.builder.call(
            self.runtime["py_bytearray_insert"],
            [recv_obj, index_obj, value_obj],
            name=self._fresh("bytearray.insert"),
        )
        self._emit_post_call_err_check(span)
        self._gc_release_if_owned(index_obj, index_expr)
        self._gc_release_if_owned(value_obj, value_expr)
        self._store_unpack_target(
            target,
            result,
            ByteArrayType(name="bytearray"),
            value_is_owned=True,
        )

    def _emit_dyn_attr_bytearray_append_stmt(self, expr: Call, target: Attr) -> bool:
        if self.current_function is None:
            return False
        recv_obj = self._emit_as_object(target)
        tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [recv_obj],
            name=self._fresh("dyn.append.recv.tag"),
        )
        is_bytearray = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_BYTEARRAY),
            name=self._fresh("dyn.append.recv.is_bytearray"),
        )
        fn = self.current_function
        bytearray_bb = fn.append_basic_block(name=self._fresh("dyn.append.bytearray"))
        not_bytearray_bb = fn.append_basic_block(
            name=self._fresh("dyn.append.not_bytearray")
        )
        list_bb = fn.append_basic_block(name=self._fresh("dyn.append.list"))
        generic_bb = fn.append_basic_block(name=self._fresh("dyn.append.generic"))
        done_bb = fn.append_basic_block(name=self._fresh("dyn.append.done"))
        self.builder.cbranch(is_bytearray, bytearray_bb, not_bytearray_bb)

        self.builder.position_at_end(bytearray_bb)
        arg_obj = self._emit_as_object(expr.args[0])
        result = self.builder.call(
            self.runtime["py_bytearray_append"],
            [recv_obj, arg_obj],
            name=self._fresh("dyn.bytearray.append"),
        )
        self._emit_post_call_err_check(expr.span)
        self._gc_release_if_owned(arg_obj, expr.args[0])
        self._store_unpack_target(
            target,
            result,
            ByteArrayType(name="bytearray"),
            value_is_owned=True,
        )
        self.builder.branch(done_bb)

        self.builder.position_at_end(not_bytearray_bb)
        is_list = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_LIST),
            name=self._fresh("dyn.append.recv.is_list"),
        )
        self.builder.cbranch(is_list, list_bb, generic_bb)

        self.builder.position_at_end(list_bb)
        self._emit_dyn_list_native_method(recv_obj, expr)
        self.builder.branch(done_bb)

        self.builder.position_at_end(generic_bb)
        fallback_value = self._emit_generic_dyn_method_call_on_value(
            recv_obj,
            "append",
            expr,
        )
        self._gc_release_if_owned(fallback_value, expr)
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        return True

    def _emit_walrus(self, expr: Call) -> ir.Value:
        """Lower ``x := value`` from the ``_walrus`` sentinel emitted
        by ``pcc.parse.py_lift._e_Assign`` — evaluate the value,
        store into ``x`` via the name-assign helper, return the value
        for use in the surrounding expression."""
        if len(expr.args) != 2:
            raise L1CodegenError("_walrus sentinel expects (target, value) args")
        target, value_expr = expr.args
        # direct valueclass constructors store as payloads (mirrors the
        # plain-assignment lowering), not identity instances
        value_payload = self._maybe_emit_valueclass_constructor_payload(
            value_expr.ty,
            value_expr,
        )
        exact_value = None
        if (
            value_payload is None
            and self._walrus_target_needs_exact_int(target)
            and isinstance(value_expr.ty, (IntType, BoolType))
        ):
            exact_value = self._emit_exact_int_operand_object(value_expr)
        value = (
            value_payload
            if value_payload is not None
            else exact_value
            if exact_value is not None
            else self._emit_expr(value_expr)
        )
        provenance = _method_pointer_provenance(
            self, value, value_expr.ty, source_expr=value_expr,
            newly_owned=self._owned_release_needed(value, value_expr),
        )
        if provenance[1]:
            # The expression result and each target binding have independent
            # lifetimes. Normalize the result to NEW, including borrowed RHSs.
            if not provenance[3]:
                value = self._gc_retain(value, name=self._fresh("walrus.result"))
            self._note_owned_object_value(value)
        if isinstance(target, Call) and self._is_walrus_sentinel(target):
            self._store_walrus_chain_target(target, value, value_expr.ty)
            return value
        if not isinstance(target, (Name, Attr, Subscript)):
            raise NotImplementedError(
                "walrus target must be a plain Name, Attr, or Subscript"
            )
        if isinstance(target, Name):
            self._store_walrus_single_target(target, value, value_expr.ty)
        else:
            if isinstance(target, Attr):
                self._emit_attr_store_value(target, value, value_expr.ty)
            else:
                self._emit_subscript_store_value(target, value, value_expr.ty)
        if value_payload is not None:
            # the surrounding expression sees an object (the walrus expr is
            # Dyn-typed); the local keeps the payload storage
            boxed = self._emit_valueclass_payload_to_object(
                value_payload,
                value_expr.ty,
            )
            if boxed is not None:
                return boxed
        return value

    def _is_walrus_sentinel(self, expr) -> bool:
        # Total over expressions on purpose.  Half the call sites guarded with
        # isinstance(..., Call) first and half did not, so a Subscript reached
        # the .func read and crashed the frontend.  One predicate that answers
        # for any node beats four call-site guards a new caller can forget.
        return (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident in ("_walrus", "__walrus__")
            and len(expr.args) == 2
        )

    def _walrus_target_needs_exact_int(self, target) -> bool:
        if isinstance(target, Name):
            return (
                target.ident
                in getattr(self, "_planned_exact_int_local_names", set())
                and isinstance(target.ty, IntType)
            )
        if isinstance(target, Call) and self._is_walrus_sentinel(target):
            return self._walrus_target_needs_exact_int(
                target.args[0]
            ) or self._walrus_target_needs_exact_int(target.args[1])
        return False

    def _store_walrus_single_target(self, target, value: ir.Value, value_ty) -> None:
        if isinstance(target, Name):
            if (
                self.current_func_def is not None
                and target.ident not in getattr(self, "_current_global_names", set())
                and target.ident not in getattr(self, "_planned_exact_int_local_names", set())
                and self._value_is_owned_object(value)
            ):
                # A plain pointer store does not retain or register a local.
                # Use the shared replaceable-owner slot protocol so consuming
                # the surrounding truth test cannot destroy this binding.
                self._gc_pin(value)
                slot = _for_prepare_owned_object_target(self, target.ident, value_ty)
                binding = self._gc_retain(value, name=self._fresh("walrus.binding"))
                _for_store_owned_target(self, target.ident, slot, binding)
                self._gc_unpin(value)
                return
            self._store_value_at_name(
                target,
                value,
                value_ty,
                value_is_owned=False,
            )
            return
        if isinstance(target, Attr):
            self._emit_attr_store_value(target, value, value_ty)
            return
        if isinstance(target, Subscript):
            self._emit_subscript_store_value(target, value, value_ty)
            return
        if isinstance(target, Call) and self._is_walrus_sentinel(target):
            self._store_walrus_chain_target(target, value, value_ty)
            return
        raise NotImplementedError(
            "walrus target must be a plain Name, Attr, or Subscript"
        )

    def _store_walrus_chain_target(self, target: Call, value: ir.Value, value_ty) -> None:
        """Store one value into a chained assignment encoded by py_lift.

        ``a = b = c`` is represented as ``a = _walrus(b, c)``. Longer
        chains can put a nested ``_walrus`` in target position; in that case
        the right-hand name is another assignment target, not a value to
        re-evaluate.
        """
        lhs, rhs_target = target.args
        if isinstance(rhs_target, Call) and self._is_walrus_sentinel(rhs_target):
            self._store_walrus_chain_target(rhs_target, value, value_ty)
        elif isinstance(rhs_target, (Name, Attr, Subscript)):
            self._store_walrus_single_target(rhs_target, value, value_ty)
        self._store_walrus_single_target(lhs, value, value_ty)
