"""Basic statement and expression control-flow lowering for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir
from pcc.python_target import PYTHON_TARGET_VERSION_INFO

from ..py_ast import Assign, Attr, BoolExpr, BoolLit, BoolType, Break, Compare, If, IfExpr, IntLit, Name, NoneLit, NoneType, Try, TupleExpr, While
from . import marshal
from .method_call_lowering import _method_pointer_provenance


_CSTR = ir.IntType(8).as_pointer()
_TARGET_SYS_VERSION_INFO = PYTHON_TARGET_VERSION_INFO


class ControlFlowLoweringMixin:
    def _static_bool_condition(self, expr) -> object:
        if isinstance(expr, BoolLit):
            return bool(expr.value)
        if isinstance(expr, Name):
            if self._native_builtin_value_for_name(expr.ident) == "typing.TYPE_CHECKING":
                return False
        if (
            isinstance(expr, Attr)
            and expr.name == "TYPE_CHECKING"
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "typing"
        ):
            return False
        if isinstance(expr, Compare) and expr.op in ("is", "is not"):
            def optional_missing_none(e) -> bool:
                return (
                    isinstance(e, Name)
                    and self._native_builtin_value_for_name(e.ident)
                    == "pcc.optional_import_missing.None"
                )

            lhs_missing = optional_missing_none(expr.lhs)
            rhs_missing = optional_missing_none(expr.rhs)
            lhs_none = isinstance(expr.lhs, NoneLit) or isinstance(expr.lhs.ty, NoneType)
            rhs_none = isinstance(expr.rhs, NoneLit) or isinstance(expr.rhs.ty, NoneType)
            if (lhs_missing and rhs_none) or (rhs_missing and lhs_none):
                return expr.op == "is"
        if isinstance(expr, Compare) and expr.op in (
            "==",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
        ):
            lhs_is_version = (
                isinstance(expr.lhs, Attr)
                and expr.lhs.name == "version_info"
                and isinstance(expr.lhs.obj, Name)
                and self._native_builtin_module_for_name(expr.lhs.obj.ident) == "sys"
            )
            if lhs_is_version and isinstance(expr.rhs, TupleExpr):
                rhs_values = []
                for item in expr.rhs.elems:
                    if not isinstance(item, IntLit):
                        return None
                    rhs_values.append(item.value)
                rhs = tuple(rhs_values)
                if expr.op == "==":
                    return _TARGET_SYS_VERSION_INFO == rhs
                if expr.op == "!=":
                    return _TARGET_SYS_VERSION_INFO != rhs
                if expr.op == "<":
                    return _TARGET_SYS_VERSION_INFO < rhs
                if expr.op == "<=":
                    return _TARGET_SYS_VERSION_INFO <= rhs
                if expr.op == ">":
                    return _TARGET_SYS_VERSION_INFO > rhs
                return _TARGET_SYS_VERSION_INFO >= rhs
        return None

    def _emit_condition_value(self, cond_expr) -> ir.Value:
        """Evaluate one truth test, consuming its temporary object owner."""
        if isinstance(cond_expr, BoolExpr):
            left = self._emit_condition_value(cond_expr.left)
            left_end = self.builder.block
            right_bb = self.current_function.append_basic_block(self._fresh("cond.right"))
            end_bb = self.current_function.append_basic_block(self._fresh("cond.end"))
            if cond_expr.op == "and":
                self.builder.cbranch(left, right_bb, end_bb)
            else:
                self.builder.cbranch(left, end_bb, right_bb)
            self.builder.position_at_end(right_bb)
            right = self._emit_condition_value(cond_expr.right)
            right_end = self.builder.block
            self.builder.branch(end_bb)
            self.builder.position_at_end(end_bb)
            result = self.builder.phi(ir.IntType(1), name=self._fresh("cond.result"))
            result.add_incoming(left, left_end)
            result.add_incoming(right, right_end)
            return result
        payload = self._maybe_emit_valueclass_constructor_payload(cond_expr.ty, cond_expr)
        if payload is not None:
            return self._truthy(payload, cond_expr.ty)
        value = self._emit_expr(cond_expr)
        if value in getattr(self, "_cpy_values", ()):
            if not self._cpy_value_is_owned(value):
                self.builder.call(self.runtime["py_cpy_incref"], [value])
                self._mark_owned_cpy_value(value)
            result = self._truthy(value, cond_expr.ty)
            self.builder.call(self.runtime["py_cpy_decref"], [value])
            self._forget_owned_cpy_value(value)
            return result
        provenance = _method_pointer_provenance(
            self, value, cond_expr.ty, source_expr=cond_expr,
            newly_owned=self._owned_release_needed(value, cond_expr),
        )
        managed = provenance[1] and self._pcc_pointer_source_needs_pin(cond_expr)
        if not managed:
            return self._truthy(value, cond_expr.ty)
        if not provenance[3]:
            value = self._gc_retain(value, name=self._fresh("cond.retain"))
        self._gc_pin(value)
        old_pcc = self._current_try_err_block()
        target = old_pcc if old_pcc is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (), (), target, "cond.error.cleanup", ((value, True),),
        )
        try:
            result = self._truthy(value, cond_expr.ty)
        finally:
            self._try_err_block = old_pcc
        self._gc_unpin(value)
        self._gc_release(value)
        return result

    def _emit_if(self, stmt: If) -> None:
        static_cond = self._static_bool_condition(stmt.cond)
        if static_cond is not None:
            if static_cond:
                self._emit_stmts(stmt.body)
            elif stmt.else_body:
                self._emit_stmts(stmt.else_body)
            return

        cond_i1 = self._emit_condition_value(stmt.cond)
        class_attr_state_before = dict(
            getattr(self, "_class_attr_runtime_state", {})
        )

        fn = self.current_function
        then_bb = fn.append_basic_block(name=self._fresh("if.then"))
        else_bb = fn.append_basic_block(name=self._fresh("if.else"))
        merge_bb = fn.append_basic_block(name=self._fresh("if.end"))

        self.builder.cbranch(cond_i1, then_bb, else_bb)

        self.builder.position_at_end(then_bb)
        self._emit_stmts(stmt.body)
        class_attr_state_then = dict(
            getattr(self, "_class_attr_runtime_state", {})
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(merge_bb)

        self.builder.position_at_end(else_bb)
        self._class_attr_runtime_state = dict(class_attr_state_before)
        if stmt.else_body:
            self._emit_stmts(stmt.else_body)
        class_attr_state_else = dict(
            getattr(self, "_class_attr_runtime_state", {})
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(merge_bb)

        self.builder.position_at_end(merge_bb)
        merged_class_attr_state = {}
        merged_keys = []
        for key in class_attr_state_then:
            merged_keys.append(key)
        for key in class_attr_state_else:
            if key not in class_attr_state_then:
                merged_keys.append(key)
        for key in merged_keys:
            then_state = class_attr_state_then.get(key)
            else_state = class_attr_state_else.get(key)
            if then_state == else_state:
                if then_state is not None:
                    merged_class_attr_state[key] = then_state
            else:
                merged_class_attr_state[key] = "unknown"
        self._class_attr_runtime_state = merged_class_attr_state

    def _emit_if_expr(self, expr: IfExpr) -> ir.Value:
        """Lower ``then_e if cond else else_e`` into a diamond CFG plus phi."""
        static_cond = self._static_bool_condition(expr.cond)
        if static_cond is not None:
            selected = expr.then_e if static_cond else expr.else_e
            selected_val = self._emit_expr(selected)
            coerced = self._coerce(selected_val, selected.ty, expr.ty)
            if selected_val in getattr(self, "_cpy_values", ()):
                if self._cpy_value_is_owned(selected_val):
                    return self._mark_owned_cpy_value(coerced)
                return self._mark_cpy_value(coerced)
            if (
                isinstance(coerced.type, ir.PointerType)
                and self._is_object(expr.ty)
                and not self._expr_returns_unsafe_raw_pointer(selected)
            ):
                selected_is_owned = (
                    self._value_is_owned_object(selected_val)
                    or self._pcc_pointer_source_is_owned(selected)
                )
                if not selected_is_owned:
                    coerced = self._gc_retain(
                        coerced,
                        name=self._fresh("ternary.static.retain"),
                    )
                self._note_owned_object_value(coerced)
            return coerced

        result_ty = expr.ty
        cond_b = self._emit_condition_value(expr.cond)

        fn = self.current_function
        then_bb = fn.append_basic_block(name=self._fresh("ternary_true"))
        else_bb = fn.append_basic_block(name=self._fresh("ternary_false"))
        join_bb = fn.append_basic_block(name=self._fresh("ternary_end"))
        self.builder.cbranch(cond_b, then_bb, else_bb)

        self.builder.position_at_end(then_bb)
        then_val = self._emit_expr(expr.then_e)
        then_val = self._coerce(then_val, expr.then_e.ty, result_ty)
        then_exit = self.builder._block

        self.builder.position_at_end(else_bb)
        else_val = self._emit_expr(expr.else_e)
        else_val = self._coerce(else_val, expr.else_e.ty, result_ty)
        else_exit = self.builder._block

        phi_ty = self._storage_ir_type(result_ty)
        cpy_result = False
        if isinstance(phi_ty, ir.PointerType):
            then_is_cpy = then_val in getattr(self, "_cpy_values", ())
            else_is_cpy = else_val in getattr(self, "_cpy_values", ())
            if then_is_cpy or else_is_cpy:
                cpy_result = True
                self.builder.position_at_end(then_exit)
                if not then_is_cpy:
                    then_val, then_owned = (
                        self._marshal_to_cpython_consuming_source(
                        then_val,
                        expr.then_e.ty,
                        expr.then_e,
                        )
                    )
                else:
                    then_owned = self._cpy_value_is_owned(then_val)
                self._guard_cpy_value_not_null(then_val)
                if then_owned:
                    self._forget_owned_cpy_value(then_val)
                else:
                    self.builder.call(self.runtime["py_cpy_incref"], [then_val])
                then_exit = self.builder._block

                self.builder.position_at_end(else_exit)
                if not else_is_cpy:
                    else_val, else_owned = (
                        self._marshal_to_cpython_consuming_source(
                        else_val,
                        expr.else_e.ty,
                        expr.else_e,
                        )
                    )
                else:
                    else_owned = self._cpy_value_is_owned(else_val)
                self._guard_cpy_value_not_null(else_val)
                if else_owned:
                    self._forget_owned_cpy_value(else_val)
                else:
                    self.builder.call(self.runtime["py_cpy_incref"], [else_val])
                else_exit = self.builder._block
            if not isinstance(then_val.type, ir.PointerType):
                self.builder.position_at_end(then_exit)
                then_val = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    then_val,
                    expr.then_e.ty,
                )
                then_exit = self.builder._block
            if not isinstance(else_val.type, ir.PointerType):
                self.builder.position_at_end(else_exit)
                else_val = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    else_val,
                    expr.else_e.ty,
                )
                else_exit = self.builder._block

        pcc_owned_result = (
            isinstance(phi_ty, ir.PointerType)
            and not cpy_result
            and self._is_object(result_ty)
            and not self._expr_returns_unsafe_raw_pointer(expr)
        )
        if pcc_owned_result:
            then_is_owned = (
                self._value_is_owned_object(then_val)
                or self._pcc_pointer_source_is_owned(expr.then_e)
            )
            if not then_is_owned:
                self.builder.position_at_end(then_exit)
                then_val = self._gc_retain(
                    then_val,
                    name=self._fresh("ternary.then.retain"),
                )
                then_exit = self.builder._block
            else_is_owned = (
                self._value_is_owned_object(else_val)
                or self._pcc_pointer_source_is_owned(expr.else_e)
            )
            if not else_is_owned:
                self.builder.position_at_end(else_exit)
                else_val = self._gc_retain(
                    else_val,
                    name=self._fresh("ternary.else.retain"),
                )
                else_exit = self.builder._block

        self.builder.position_at_end(then_exit)
        self.builder.branch(join_bb)
        self.builder.position_at_end(else_exit)
        self.builder.branch(join_bb)

        self.builder.position_at_end(join_bb)
        phi = self.builder.phi(phi_ty, name=self._fresh("ternary"))
        phi.add_incoming(then_val, then_exit)
        phi.add_incoming(else_val, else_exit)
        if cpy_result:
            return self._mark_owned_cpy_value(phi)
        if pcc_owned_result:
            self._note_owned_object_value(phi)
        return phi

    def _emit_if_expr_as_pcc_object(self, expr: IfExpr) -> ir.Value:
        """Lower a conditional expression whose result crosses an object boundary."""
        static_cond = self._static_bool_condition(expr.cond)
        if static_cond is not None:
            selected = expr.then_e if static_cond else expr.else_e
            return self._emit_expr_as_pcc_object(selected)

        cond_b = self._emit_condition_value(expr.cond)

        fn = self.current_function
        then_bb = fn.append_basic_block(name=self._fresh("ternary_obj_true"))
        else_bb = fn.append_basic_block(name=self._fresh("ternary_obj_false"))
        join_bb = fn.append_basic_block(name=self._fresh("ternary_obj_end"))
        self.builder.cbranch(cond_b, then_bb, else_bb)

        self.builder.position_at_end(then_bb)
        then_val = self._emit_expr_as_pcc_object(expr.then_e)
        if (
            isinstance(expr.then_e, Name)
            and getattr(self, "_exact_int_env_flags", {}).get(
                expr.then_e.ident,
                False,
            )
        ):
            then_val = self._gc_retain(
                then_val,
                name=self._fresh("ternary.obj.then.retain"),
            )
        then_exit = self.builder._block

        self.builder.position_at_end(else_bb)
        else_val = self._emit_expr_as_pcc_object(expr.else_e)
        if (
            isinstance(expr.else_e, Name)
            and getattr(self, "_exact_int_env_flags", {}).get(
                expr.else_e.ident,
                False,
            )
        ):
            else_val = self._gc_retain(
                else_val,
                name=self._fresh("ternary.obj.else.retain"),
            )
        else_exit = self.builder._block

        self.builder.position_at_end(then_exit)
        self.builder.branch(join_bb)
        self.builder.position_at_end(else_exit)
        self.builder.branch(join_bb)

        self.builder.position_at_end(join_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh("ternary.obj"))
        phi.add_incoming(then_val, then_exit)
        phi.add_incoming(else_val, else_exit)
        return phi

    def _emit_while(self, stmt: While) -> None:
        if stmt.else_body:
            from dataclasses import replace as _replace

            broke_name = self._fresh("whileelse_broke")
            span = stmt.span
            broke_lit_false = BoolLit(
                span=span,
                ty=BoolType(name="bool"),
                value=False,
            )
            broke_lit_true = BoolLit(
                span=span,
                ty=BoolType(name="bool"),
                value=True,
            )
            broke_ref = Name(
                span=span,
                ty=BoolType(name="bool"),
                ident=broke_name,
            )
            set_broke_true = Assign(
                span=span,
                targets=(broke_ref,),
                value=broke_lit_true,
                annotation=BoolType(name="bool"),
            )

            def tag_breaks(stmts):
                out = []
                for s in stmts:
                    if isinstance(s, Break):
                        out.append(set_broke_true)
                        out.append(s)
                        continue
                    if isinstance(s, If):
                        out.append(
                            _replace(
                                s,
                                body=tag_breaks(s.body),
                                else_body=tag_breaks(s.else_body),
                            )
                        )
                        continue
                    if isinstance(s, Try):
                        new_handlers_list = []
                        for h in s.handlers:
                            new_handlers_list.append(
                                _replace(h, body=tag_breaks(h.body))
                            )
                        new_handlers = tuple(new_handlers_list)
                        out.append(
                            _replace(
                                s,
                                body=tag_breaks(s.body),
                                else_body=tag_breaks(s.else_body),
                                finally_body=tag_breaks(s.finally_body),
                                handlers=new_handlers,
                            )
                        )
                        continue
                    out.append(s)
                return tuple(out)

            ir_ty = self._map_type(BoolType(name="bool"))
            alloca = self._alloca_in_entry(
                ir_ty,
                name=f"{broke_name}.addr",
            )
            self.builder.store(ir.Constant(ir_ty, 0), alloca)
            self.env[broke_name] = (alloca, ir_ty, BoolType(name="bool"))

            new_stmt = _replace(
                stmt,
                body=tag_breaks(stmt.body),
                else_body=(),
            )
            self._emit_while(new_stmt)
            broke_val = self.builder.load(
                alloca,
                name=self._fresh("whileelse.broke"),
            )
            should_else = self.builder.icmp_unsigned(
                "==",
                broke_val,
                ir.Constant(ir_ty, 0),
                name=self._fresh("whileelse.should_else"),
            )
            else_bb = self.current_function.append_basic_block(
                name=self._fresh("whileelse.else"),
            )
            end_bb = self.current_function.append_basic_block(
                name=self._fresh("whileelse.end"),
            )
            self.builder.cbranch(should_else, else_bb, end_bb)
            self.builder.position_at_end(else_bb)
            self._emit_stmts(stmt.else_body)
            if not self._builder_block_is_terminated():
                self.builder.branch(end_bb)
            self.builder.position_at_end(end_bb)
            return
        fn = self.current_function
        class_attr_state_before = dict(
            getattr(self, "_class_attr_runtime_state", {})
        )
        cond_bb = fn.append_basic_block(name=self._fresh("while.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("while.body"))
        latch_bb = fn.append_basic_block(name=self._fresh("while.latch"))
        end_bb = fn.append_basic_block(name=self._fresh("while.end"))

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        cond_i1 = self._emit_condition_value(stmt.cond)
        self.builder.cbranch(cond_i1, body_bb, end_bb)

        self.loop_stack.append((latch_bb, end_bb, self._loop_finally_base()))
        self.builder.position_at_end(body_bb)
        self._class_attr_runtime_state = dict(class_attr_state_before)
        old_class_attr_loop_depth = getattr(
            self,
            "_class_attr_mutation_in_loop_depth",
            0,
        )
        self._class_attr_mutation_in_loop_depth = old_class_attr_loop_depth + 1
        self._emit_stmts(stmt.body)
        self._class_attr_mutation_in_loop_depth = old_class_attr_loop_depth
        class_attr_state_body = dict(
            getattr(self, "_class_attr_runtime_state", {})
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(latch_bb)
        self.loop_stack.pop()

        self.builder.position_at_end(latch_bb)
        self._emit_thread_safepoint()
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        merged_class_attr_state = dict(class_attr_state_before)
        for key in class_attr_state_body:
            before_state = class_attr_state_before.get(key)
            body_state = class_attr_state_body.get(key)
            if before_state == body_state:
                if body_state is not None:
                    merged_class_attr_state[key] = body_state
            else:
                merged_class_attr_state[key] = "unknown"
        self._class_attr_runtime_state = merged_class_attr_state
