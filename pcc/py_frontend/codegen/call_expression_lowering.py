"""Call expression lowering for L1CodeGen."""

from __future__ import annotations

import sys
import os
from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Arg,
    Attr,
    BoolLit,
    BoolType,
    Call,
    DictExpr,
    DictType,
    DynType,
    Expr,
    FloatLit,
    FloatType,
    IntLit,
    IntType,
    ListExpr,
    ListType,
    Name,
    NoneLit,
    NoneType,
    Slice,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    ValueArrayType,
)
from .bootstrap_trace import bootstrap_trace_enabled
from . import marshal
from .method_call_lowering import _method_pointer_provenance
from .builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG
from .errors import L1CodegenError
from .generator_lowering import emit_generator_may_park_call
from .guarded_loop_lowering import (
    emit_guarded_i64_dot,
    emit_guarded_loop_counter,
    emit_i64_buffer_constructor,
)

_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = ir.IntType(8).as_pointer()
_CPY_BUILTIN_FALLBACK = frozenset(
    {
        "open",
        "iter",
        "next",
        "sorted",
        "super",
        "property",
        "classmethod",
        "staticmethod",
        "hasattr",
        "hash",
        "id",
        "repr",
        "ord",
        "chr",
        "dir",
        "vars",
        "locals",
    }
)


def _ascii_decimal_digit(ch: str) -> int:
    c = ord(ch)
    if 48 <= c <= 57:
        return c - 48
    return -1


def _parse_simple_decimal_float(s: str):
    n = len(s)
    if n == 0:
        return None
    i = 0
    sign = 1.0
    if s[i] == "+":
        i += 1
    elif s[i] == "-":
        sign = -1.0
        i += 1
    if i >= n:
        return None

    value = 0.0
    saw_digit = False
    while i < n:
        d = _ascii_decimal_digit(s[i])
        if d < 0:
            break
        saw_digit = True
        value = value * 10.0 + d
        i += 1

    if i < n and s[i] == ".":
        i += 1
        place = 0.1
        while i < n:
            d = _ascii_decimal_digit(s[i])
            if d < 0:
                break
            saw_digit = True
            value = value + d * place
            place = place * 0.1
            i += 1

    if not saw_digit:
        return None

    if i < n and (s[i] == "e" or s[i] == "E"):
        i += 1
        exp_sign = 1
        if i < n and s[i] == "+":
            i += 1
        elif i < n and s[i] == "-":
            exp_sign = -1
            i += 1
        exp = 0
        saw_exp_digit = False
        while i < n:
            d = _ascii_decimal_digit(s[i])
            if d < 0:
                break
            saw_exp_digit = True
            exp = exp * 10 + d
            i += 1
        if not saw_exp_digit:
            return None
        if exp > 400:
            if exp_sign > 0:
                return sign * 1e309
            return sign * 0.0
        while exp > 0:
            if exp_sign > 0:
                value = value * 10.0
            else:
                value = value * 0.1
            exp -= 1

    if i != len(s):
        return None
    return sign * value


def _maybe_fold_str_to_float(s: str):
    stripped = s.strip()
    lowered = stripped.lower()
    if lowered in ("inf", "+inf", "infinity", "+infinity"):
        return 1e309
    if lowered in ("-inf", "-infinity"):
        return -1e309
    if lowered in ("nan", "+nan", "-nan"):
        inf = 1e309
        return inf - inf
    return _parse_simple_decimal_float(stripped)


def _call_name_ident(expr: object):
    if isinstance(expr, Name):
        return expr.ident
    try:
        return expr.ident
    except AttributeError:
        return None


def _call_attr_name(expr: object):
    if isinstance(expr, Attr):
        return expr.name
    try:
        return expr.name
    except AttributeError:
        return None


def _call_attr_obj(expr: object):
    if isinstance(expr, Attr):
        return expr.obj
    try:
        return expr.obj
    except AttributeError:
        return None


def _call_is_attr(expr: object) -> bool:
    return _call_attr_name(expr) is not None and _call_attr_obj(expr) is not None


def _replace_arg_with_none_default(arg):
    return Arg(
        name=arg.name,
        annotation=arg.annotation,
        default=NoneLit(
            span=None,
            ty=NoneType(name="None"),
        ),
        kind=arg.kind,
        has_default=True,
    )


class CallExpressionLoweringMixin:
    def _expr_span_or_none(self, expr):
        try:
            return expr.span
        except AttributeError:
            return None

    def _maybe_emit_value_array_constructor(self, expr: Call) -> ir.Value | None:
        array_ty = expr.ty
        if not isinstance(array_ty, ValueArrayType):
            return None
        if not isinstance(expr.func, Subscript) or expr.kwargs:
            return None
        payload_ty = self._value_array_payload_ir_type(array_ty)
        if payload_ty is None or len(expr.args) != array_ty.length:
            return None

        payload_slot = self._alloca_in_entry(
            payload_ty,
            name=self._fresh("value.array.tmp"),
        )
        zero = ir.Constant(_I32, 0)
        for index, arg_expr in enumerate(expr.args):
            elem_value = self._maybe_emit_valueclass_constructor_payload(
                array_ty.elem,
                arg_expr,
            )
            if elem_value is None:
                raw_value = self._emit_expr(arg_expr)
                elem_value = self._coerce(raw_value, arg_expr.ty, array_ty.elem)
            elem_ptr = self.builder.gep(
                payload_slot,
                [zero, ir.Constant(_I32, index)],
                inbounds=True,
                name=self._fresh(f"value.array.elem{index}"),
            )
            self.builder.store(elem_value, elem_ptr)
        return self.builder.load(
            payload_slot,
            name=self._fresh("value.array.payload"),
        )

    def _literal_method_dispatch_result_object(self, result, result_ty) -> ir.Value:
        if isinstance(result.type, ir.VoidType):
            return self._emit_none_literal()
        if isinstance(result.type, ir.PointerType):
            return result
        return marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            result,
            result_ty,
        )

    def _maybe_emit_literal_self_method_dict_dispatch_call(
        self,
        expr: Call,
    ) -> ir.Value | None:
        """Fast path for ``d[name](...)`` where ``d`` is a literal self-method map.

        This keeps Python semantics for unknown keys by falling back to the
        original dict lookup + dynamic call. It deliberately only optimizes
        maps whose values are ``self.<method>`` so the bound receiver cannot be
        invalidated by local rebinding.
        """

        if expr.kwargs:
            return None
        if not isinstance(expr.func, Subscript):
            return None
        sub = expr.func
        if not isinstance(sub.obj, Name):
            return None
        literal_map = getattr(self, "_literal_dict_expr_bindings", {}).get(
            sub.obj.ident
        )
        if not isinstance(literal_map, DictExpr) or not literal_map.pairs:
            return None

        receiver_class = self._self_receiver_class_name()
        current_class = getattr(self, "current_class", None)
        if receiver_class is None and current_class is not None:
            receiver_class = current_class.name
        if receiver_class is None or "self" not in self.env:
            return None

        resolved = self._literal_self_method_dispatch_entries(literal_map)
        if not resolved:
            return None

        is_virtual = sub.obj.ident in getattr(
            self,
            "_virtual_literal_dict_expr_bindings",
            set(),
        )
        key_obj = self._emit_as_object(sub.idx)
        self_val = self.builder.load(self.env["self"][0], name=self._fresh("self"))
        merge_bb = self.builder.function.append_basic_block(
            name=self._fresh("method.dict.dispatch.end")
        )
        incoming: list[tuple[ir.Value, ir.Block]] = []

        for key, method_info, method_fn, method_name in resolved:
            match_bb = self.builder.function.append_basic_block(
                name=self._fresh(f"method.dict.{method_name}")
            )
            next_bb = self.builder.function.append_basic_block(
                name=self._fresh("method.dict.next")
            )
            key_lit = self._emit_str_literal(key)
            eq = self.builder.call(
                self.runtime["py_str_eq"],
                [key_obj, key_lit],
                name=self._fresh("method.dict.key.eq"),
            )
            cond = self.builder.icmp_unsigned(
                "!=",
                eq,
                ir.Constant(_I64, 0),
                name=self._fresh("method.dict.key.hit"),
            )
            self.builder.cbranch(cond, match_bb, next_bb)

            self.builder.position_at_end(match_bb)
            result = self._emit_direct_method_call(
                method_fn,
                self_val,
                method_info,
                method_name,
                expr.args,
                kwargs=(),
            )
            if not self.builder.block.is_terminated:
                method_def = self.class_lowering._find_method_def(
                    method_info.name,
                    method_name,
                )
                result_ty = (
                    method_def.return_ty
                    if method_def is not None
                    else DynType(name="dyn")
                )
                result_obj = self._literal_method_dispatch_result_object(
                    result,
                    result_ty,
                )
                incoming.append((result_obj, self.builder.block))
                self.builder.branch(merge_bb)

            self.builder.position_at_end(next_bb)

        if is_virtual:
            exc = self.builder.call(
                self.runtime["py_exc_new_with_value"],
                [
                    ir.Constant(_I64, _BUILTIN_EXC_TAG["KeyError"]),
                    key_obj,
                ],
                name=self._fresh("method.dict.keyerror"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            self._gc_release(exc)
            frame_exc = self.builder.call(
                self.runtime["py_current_exception"],
                [],
                name=self._fresh("method.dict.frame.exc"),
            )
            self._emit_exception_frame(frame_exc, self._expr_span_or_none(expr))
            err_target = self._current_try_err_block()
            if err_target is None:
                err_target = self._ensure_fn_err_exit()
            self.builder.branch(err_target)
        else:
            dict_obj = self.builder.load(
                self.env[sub.obj.ident][0],
                name=self._fresh(f"{sub.obj.ident}.dispatch.dict"),
            )
            fn_val = self.builder.call(
                self.runtime["py_dict_get"],
                [dict_obj, key_obj],
                name=self._fresh("method.dict.fallback.fn"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            args_owned = not self._is_starred_unpack(expr.args)
            args_tuple = self._emit_dynamic_call_args_tuple(expr.args)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                (),
                None,
                self._expr_span_or_none(expr),
            )
            fallback_result = self.builder.call(
                self.runtime["py_obj_call"],
                [fn_val, args_tuple, kwargs_obj],
                name=self._fresh("method.dict.fallback.call"),
            )
            if args_owned:
                self._gc_release(args_tuple)
            self._gc_release(fn_val)
            if not self.builder.block.is_terminated:
                incoming.append((fallback_result, self.builder.block))
                self.builder.branch(merge_bb)

        self.builder.position_at_end(merge_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh("method.dict.dispatch.result"))
        for value, block in incoming:
            phi.add_incoming(value, block)
        return phi

    def _emit_globals_builtin(self) -> ir.Value:
        module_name = self.ast_module.name or "__main__"
        module_name_ptr = self._ptr_to_cstr(
            self._cstr_global(
                module_name,
                self._fresh(".globals.module"),
            )
        )
        globals_dict = self.builder.call(
            self.runtime["py_module_attrs_dict"],
            [module_name_ptr, ir.Constant(_I64, 1)],
            name=self._fresh("globals.dict"),
        )

        for global_name, (gv, declared_ty) in self._module_globals.items():
            if getattr(self, "_cpy_module_flags", {}).get(global_name, False):
                continue
            init_flag = self._module_global_init_flags.get(global_name)
            continue_bb = None
            if init_flag is not None:
                initialized = self.builder.load(
                    init_flag,
                    name=self._fresh(f"globals.{global_name}.initialized"),
                )
                publish_bb = self.current_function.append_basic_block(
                    self._fresh(f"globals.{global_name}.publish")
                )
                continue_bb = self.current_function.append_basic_block(
                    self._fresh(f"globals.{global_name}.continue")
                )
                self.builder.cbranch(initialized, publish_bb, continue_bb)
                self.builder.position_at_end(publish_bb)
            raw = self.builder.load(gv, name=self._fresh(f"globals.{global_name}"))
            obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                raw,
                declared_ty,
            )
            self.builder.call(
                self.runtime["py_module_attr_set"],
                [module_name_ptr, self._attr_name_ptr(global_name), obj],
                name=self._fresh(f"globals.set.{global_name}"),
            )
            if continue_bb is not None:
                self.builder.branch(continue_bb)
                self.builder.position_at_end(continue_bb)

        # Import statements create ordinary module-namespace bindings too.
        # Native extension and compiled-sibling module objects live in the
        # shared import-object registry rather than ``_module_globals``; publish
        # each binding that actually executed, while leaving untaken
        # conditional imports absent from the namespace.
        for import_name, gv in getattr(
            self,
            "_native_extension_module_env",
            {},
        ).items():
            imported = self.builder.load(
                gv,
                name=self._fresh(f"globals.import.{import_name}"),
            )
            is_bound = self.builder.icmp_unsigned(
                "!=",
                imported,
                ir.Constant(imported.type, None),
                name=self._fresh(f"globals.import.{import_name}.bound"),
            )
            publish_bb = self.current_function.append_basic_block(
                self._fresh(f"globals.import.{import_name}.publish")
            )
            continue_bb = self.current_function.append_basic_block(
                self._fresh(f"globals.import.{import_name}.continue")
            )
            self.builder.cbranch(is_bound, publish_bb, continue_bb)
            self.builder.position_at_end(publish_bb)
            self.builder.call(
                self.runtime["py_module_attr_set"],
                [module_name_ptr, self._attr_name_ptr(import_name), imported],
                name=self._fresh(f"globals.set.import.{import_name}"),
            )
            self.builder.branch(continue_bb)
            self.builder.position_at_end(continue_bb)

        for class_name, info in self.class_lowering.classes.items():
            if class_name in getattr(
                self,
                "_hoisted_class_capture_params",
                {},
            ):
                continue
            cls_obj = self.builder.load(
                info.global_var,
                name=self._fresh(f"globals.cls.{class_name}"),
            )
            self.builder.call(
                self.runtime["py_module_attr_set"],
                [module_name_ptr, self._attr_name_ptr(class_name), cls_obj],
                name=self._fresh(f"globals.set.cls.{class_name}"),
            )

        # The module side table owns its dictionary and exposes it here as a
        # borrowed reference.  A Python call result is owned, so retain before
        # handing it to assignment/temporary cleanup.  Without this bridge, a
        # function-local ``namespace = globals()`` releases the side table's
        # sole reference when the local dies and leaves later module lookups
        # pointing at freed memory.
        return self._gc_retain(
            globals_dict,
            name=self._fresh("globals.result.retain"),
        )

    def _function_arg_ir_type_or_none(self, fn, index: int, ir_arg):
        try:
            return ir_arg.type
        except AttributeError:
            pass
        try:
            fty = fn.function_type
            args = fty.args
            if index < len(args):
                return args[index]
        except AttributeError:
            pass
        try:
            fty = fn.ftype
            args = fty.args
            if index < len(args):
                return args[index]
        except AttributeError:
            pass
        return None

    def _func_has_click_decorator(self, fd) -> bool:
        decorators = self._func_decorators(fd)
        i = 0
        while i < len(decorators):
            d = decorators[i]
            if self._decorator_is_noop_whitelist(d):
                qn = self._decorator_qualname(d) or ""
                if qn.startswith("click."):
                    return True
            i += 1
        return False

    def _materialize_class_init_call_args(self, args: tuple) -> tuple:
        out = []
        pinned = []
        cpy_owned = []
        for arg in args:
            # Evaluate in source order through the normal expression emitter.
            # The constructor's argument slots are borrows; their temporary
            # owners must survive later arguments and __init__, then be freed.
            raw = self._emit_expr_with_cpy_operand_cleanup(
                arg, tuple(cpy_owned), pinned_pcc=tuple(pinned),
                as_pcc_object=isinstance(arg.ty, IntType),
            )
            is_cpy = raw in getattr(self, "_cpy_values", ())
            if is_cpy:
                if not self._cpy_value_is_owned(raw):
                    self.builder.call(self.runtime["py_cpy_incref"], [raw])
                cpy_owned.append(raw)
            else:
                provenance = _method_pointer_provenance(
                    self, raw, arg.ty, source_expr=arg,
                    newly_owned=self._owned_release_needed(raw, arg),
                )
                if provenance[1]:
                    if not provenance[3]:
                        raw = self._gc_retain(raw, name=self._fresh("ctor.arg.retain"))
                    self._gc_pin(raw)
                    pinned.append((raw, True))
            name = self._fresh("__pcc_ctor_arg")
            alloca = self._alloca_in_entry(
                raw.type, name=name + ".addr",
                init_null=isinstance(raw.type, ir.PointerType),
            )
            self.builder.store(raw, alloca)
            self.env[name] = (alloca, raw.type, arg.ty)
            if is_cpy:
                self._cpy_env_flags[name] = True
            out.append(Name(span=self._expr_span_or_none(arg), ty=arg.ty, ident=name))
        return tuple(out), tuple(pinned), tuple(cpy_owned)

    def _emit_class_init_call(self, class_name: str, args: tuple):
        args, pinned, cpy_owned = self._materialize_class_init_call_args(args)
        old_pcc = self._current_try_err_block()
        old_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
        pcc_target = old_pcc if old_pcc is not None else self._ensure_fn_err_exit()
        cpy_target = old_cpy if old_cpy is not None else self._ensure_fn_err_exit()
        pcc_cleanup = self._make_cpy_operand_cleanup_block(
            cpy_owned, (), pcc_target, "ctor.args.pcc.cleanup", pinned,
        )
        cpy_cleanup = pcc_cleanup
        if cpy_target is not pcc_target:
            cpy_cleanup = self._make_cpy_operand_cleanup_block(
                cpy_owned, (), cpy_target, "ctor.args.cpy.cleanup", pinned,
            )
        self._try_err_block = pcc_cleanup
        self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            result = self.class_lowering.emit_instantiate(class_name, args, self)
        finally:
            self._try_err_block = old_pcc
            self._cpy_operand_cleanup_block = old_cpy
        # Releasing an argument can run a finalizer that collects. The new
        # instance has not reached its caller's rooted assignment yet.
        self._gc_pin(result)
        for value, _owned in reversed(pinned):
            self._gc_unpin(value)
            self._gc_release(value)
        for value in reversed(cpy_owned):
            self.builder.call(self.runtime["py_cpy_decref"], [value])
            self._forget_owned_cpy_value(value)
        self._gc_unpin(result)
        return result

    def _maybe_emit_known_dunder_class_constructor(self, expr: Call):
        func = expr.func
        if not isinstance(func, Attr):
            return None
        if func.name != "__class__":
            return None
        try:
            class_name = self._class_hint_for_expr(func.obj)
        except Exception:
            class_name = None
        if class_name is None:
            if isinstance(func.obj, Name) and func.obj.ident == "self":
                class_name = self._self_receiver_class_name()
        if class_name is None:
            return None
        try:
            classes = self.class_lowering.classes
        except AttributeError:
            return None
        if class_name not in classes:
            return None
        return self._emit_call(
            Call(
                span=self._expr_span_or_none(expr),
                ty=expr.ty,
                func=Name(
                    span=self._expr_span_or_none(func),
                    ty=DynType(name="dyn"),
                    ident=class_name,
                ),
                args=expr.args,
                kwargs=expr.kwargs,
            )
        )

    def _emit_range_value_call(self, expr: Call) -> ir.Value:
        if expr.kwargs:
            raise NotImplementedError("Layer 1 range() has no keyword args")
        if len(expr.args) == 1:
            start_val: ir.Value = ir.Constant(_I64, 0)
            stop_val = self._emit_expr_as_i64(expr.args[0])
            step_val: ir.Value = ir.Constant(_I64, 1)
        elif len(expr.args) == 2:
            start_val = self._emit_expr_as_i64(expr.args[0])
            stop_val = self._emit_expr_as_i64(expr.args[1])
            step_val = ir.Constant(_I64, 1)
        elif len(expr.args) == 3:
            start_val = self._emit_expr_as_i64(expr.args[0])
            stop_val = self._emit_expr_as_i64(expr.args[1])
            step_val = self._emit_expr_as_i64(expr.args[2])
        else:
            raise L1CodegenError(f"range() takes 1-3 args; got {len(expr.args)}")

        out = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("range.list"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="range.value.idx.addr")
        self.builder.store(start_val, idx_slot)

        fn = self.current_function
        cond_bb = fn.append_basic_block(name=self._fresh("range.value.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("range.value.body"))
        step_bb = fn.append_basic_block(name=self._fresh("range.value.step"))
        end_bb = fn.append_basic_block(name=self._fresh("range.value.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("range.value.i"))
        zero64 = ir.Constant(_I64, 0)
        step_pos = self.builder.icmp_signed(
            ">", step_val, zero64, name=self._fresh("range.value.step.pos")
        )
        cond_pos = self.builder.icmp_signed(
            "<", cur, stop_val, name=self._fresh("range.value.fwd")
        )
        cond_neg = self.builder.icmp_signed(
            ">", cur, stop_val, name=self._fresh("range.value.bwd")
        )
        keep = self.builder.select(
            step_pos, cond_pos, cond_neg, name=self._fresh("range.value.keep")
        )
        self.builder.cbranch(keep, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        item = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("range.value.item"),
        )
        self.builder.call(
            self.runtime["py_list_append"],
            [out, item],
            name=self._fresh("range.value.append"),
        )
        self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        next_val = self.builder.add(
            cur,
            step_val,
            name=self._fresh("range.value.next"),
        )
        self.builder.store(next_val, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        return out

    def _name_binds_cpy_returning_callable(self, name: str) -> bool:
        """True if ``name`` is bound (in the current function or module scope)
        to a callable — e.g. a lambda or a function ref — whose body returns a
        CPython object. The indirect ``py_obj_call`` of such a value must tag
        its result cpy (mirrors the direct-funcdef path)."""
        bodies = []
        cur = getattr(self, "current_func_def", None)
        if cur is not None and getattr(cur, "body", None):
            bodies.append(cur.body)
        mod = getattr(self, "ast_module", None)
        if mod is not None and getattr(mod, "body", None):
            bodies.append(mod.body)
        for body in bodies:
            for stmt in body:
                targets = getattr(stmt, "targets", None)
                if (
                    targets
                    and len(targets) == 1
                    and isinstance(targets[0], Name)
                    and targets[0].ident == name
                    and hasattr(stmt, "value")
                ):
                    try:
                        if self._callable_expr_returns_cpython(stmt.value):
                            return True
                    except Exception:
                        pass
        return False

    def _emit_call(self, expr: Call) -> ir.Value:
        if bootstrap_trace_enabled(self.module.name):
            import os
            import sys

            if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE"):
                try:
                    func_type = type(expr.func).__name__
                except AttributeError:
                    func_type = ""
                try:
                    raw_ident = expr.func.ident
                except AttributeError:
                    raw_ident = "<missing>"
                try:
                    raw_name = expr.func.name
                except AttributeError:
                    raw_name = "<missing>"
                sys.stderr.write(
                    "debug: py_lift_call func_type="
                    + str(func_type)
                    + " ident="
                    + str(raw_ident)
                    + " name="
                    + str(raw_name)
                    + "\n"
                )
        value_array = self._maybe_emit_value_array_constructor(expr)
        if value_array is not None:
            return value_array
        i64_buffer = emit_i64_buffer_constructor(self, expr)
        if i64_buffer is not None:
            return i64_buffer
        func_expr = expr.func
        func_name = _call_name_ident(func_expr)
        func_attr_name = _call_attr_name(func_expr)
        func_attr_obj = _call_attr_obj(func_expr)
        pcc_intrinsic = self._native_builtin_value_kind_for_expr(func_expr)
        if pcc_intrinsic == "pcc.guarded_i64_dot":
            return emit_guarded_i64_dot(self, expr)
        if pcc_intrinsic == "pcc.guarded_loop_counter":
            return emit_guarded_loop_counter(self, expr)
        if (
            (func_name == "cast")
            or (
                func_attr_name == "cast" and _call_name_ident(func_attr_obj) == "typing"
            )
        ) and len(expr.args) == 2:
            return self._emit_expr(expr.args[1])

        native_sys_exit_call = self._emit_native_sys_exit_call(expr)
        if native_sys_exit_call is not None:
            return native_sys_exit_call
        native_replace_call = self._emit_native_dataclasses_replace_call(expr)
        if native_replace_call is not None:
            return native_replace_call
        if func_attr_name == "__class__":
            dunder_class_ctor = self._maybe_emit_known_dunder_class_constructor(expr)
            if dunder_class_ctor is not None:
                return dunder_class_ctor
        if (
            func_attr_name == "__setattr__"
            and _call_name_ident(func_attr_obj) == "object"
            and "object" not in self.env
            and len(expr.args) == 3
            and not expr.kwargs
        ):
            # ``object.__setattr__`` is the standard frozen-dataclass escape
            # used during ``__post_init__``.  It has the same three-operand
            # object/name/value ABI as builtin ``setattr`` for pcc-native
            # instances; lowering it before generic attribute-call dispatch
            # avoids importing CPython's builtin ``object`` in no-libpython
            # closures.  A user binding named ``object`` remains dynamic.
            return self._emit_setattr_builtin(expr)
        if _call_is_attr(func_expr):
            return self._emit_method_call(expr)
        if func_name is None:
            literal_dispatch = self._maybe_emit_literal_self_method_dict_dispatch_call(
                expr
            )
            if literal_dispatch is not None:
                return literal_dispatch
            fn_val = self._emit_expr(func_expr)
            if fn_val in getattr(self, "_cpy_values", ()):
                if expr.kwargs:
                    return self._finish_cpy_call_kw(
                        fn_val,
                        "expr",
                        expr.args,
                        expr.kwargs,
                        expr.operand_order,
                    )
                return self._emit_cpy_func_call(fn_val, "expr", expr.args)
            kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
            arg_exprs = expr.args
            kwargs_expr = None
            if kwdict_unpack is not None:
                arg_exprs, kwargs_expr = kwdict_unpack
            args_owned = not self._is_starred_unpack(arg_exprs)
            args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                expr.kwargs,
                kwargs_expr,
                self._expr_span_or_none(expr),
            )
            result = self.builder.call(
                self.runtime["py_obj_call"],
                [fn_val, args_tuple, kwargs_obj],
                name=self._fresh("obj.call"),
            )
            if args_owned:
                self._gc_release(args_tuple)
            if expr.kwargs:
                self._gc_release(kwargs_obj)
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        name = func_name
        if (self._funcdef_is_continuation_factory(self.current_func_def)
                and name in self._vthread_may_park_func_names):
            raise L1CodegenError("factory methods must defer parking calls with continuation()")
        # A closed-world ``may_park`` callee has a generator ABI even though
        # source code uses an ordinary blocking-looking call.  While lowering
        # its affected caller, transparently drive/delegate that child state
        # machine so the expression resumes with the callee's actual return
        # value instead of exposing a generator object to user code.
        if (
            len(getattr(self, "_generator_ctx_stack", ())) > 0
            and name in getattr(self, "_vthread_may_park_func_names", set())
        ):
            delegated = emit_generator_may_park_call(self, expr, name)
            if delegated is not None:
                return delegated
        unsafe_intrinsic = self._unsafe_intrinsic_for_name(name)
        if unsafe_intrinsic is not None:
            value = self._emit_unsafe_intrinsic_call(unsafe_intrinsic, expr)
            # Pointer-producing intrinsics are typed ``int`` outside
            # runtime-port mode (raw address as integer); project the IR
            # pointer to i64 so the value matches its static type.
            if (
                isinstance(getattr(expr, "ty", None), IntType)
                and isinstance(getattr(value, "type", None), ir.PointerType)
            ):
                value = self.builder.ptrtoint(
                    value, _I64, name=self._fresh("unsafe.addr.int")
                )
            return value
        if name in _BUILTIN_EXC_TAG and name not in self.env:
            return self._build_exception_value(expr)
        if name == "__await__":
            return self._emit_await_expr(expr)
        # Comprehension sentinels emitted by the parser. Lowered to an
        # explicit loop that appends into a runtime list/dict/set.
        if name in ("__listcomp__", "_list_comp", "_gen_comp", "__genexpr__"):
            # Generator expressions eagerly materialise to a list —
            # pcc doesn't support lazy generators yet; the common use
            # sites (``sum(x for x in xs)``, ``"".join(s for …)``)
            # iterate the result once so a list works identically.
            return self._emit_comprehension(expr, "list")
        if name in ("__setcomp__", "_set_comp"):
            return self._emit_comprehension(expr, "set")
        if name in ("__dictcomp__", "_dict_comp"):
            return self._emit_comprehension(expr, "dict")
        # print() has a bespoke kwarg parser (sep=, end=) handled inline.
        if name == "print":
            self._emit_print_call(expr)
            return ir.Constant(_I1, 0)
        if name == "__import__" and len(expr.args) == 1 and not expr.kwargs:
            imported = self._native_literal_dunder_import_module(expr)
            if imported is not None:
                return self._emit_native_module_placeholder(imported)
            if isinstance(expr.args[0], StrLit):
                if expr.args[0].value == "":
                    self._emit_builtin_exception_and_branch(
                        "ValueError",
                        "Empty module name",
                        self._expr_span_or_none(expr),
                    )
                    return ir.Constant(_CSTR, None)
                if "\x00" in expr.args[0].value:
                    self._emit_builtin_exception_and_branch(
                        "ValueError",
                        "module name contains a null character",
                        self._expr_span_or_none(expr),
                    )
                    return ir.Constant(_CSTR, None)
                self._emit_builtin_exception_and_branch(
                    "ModuleNotFoundError",
                    f"No module named {expr.args[0].value!r}",
                    self._expr_span_or_none(expr),
                )
                return ir.Constant(_CSTR, None)
        if name == "__import__" and not expr.kwargs and 1 <= len(expr.args) <= 5:
            if len(expr.args) == 5:
                level = expr.args[4]
                if not isinstance(level, IntLit) or level.value != 0:
                    raise NotImplementedError(
                        "Layer 1 builtin __import__() supports only absolute level 0"
                    )
            evaluated_args = []
            for arg in expr.args:
                evaluated_args.append(self._emit_as_object(arg))
            fromlist = (
                evaluated_args[3]
                if len(evaluated_args) >= 4
                else self._emit_none_literal()
            )
            result = self.builder.call(
                self.runtime["py_builtin_import"],
                [evaluated_args[0], fromlist],
                name=self._fresh("builtin.import"),
            )
            self._emit_post_call_err_check()
            return result
        if name == "open":
            native_open = self._emit_native_open_call(expr)
            if native_open is not None:
                return native_open
        native_fileinput = self._emit_native_fileinput_call(expr)
        if native_fileinput is not None:
            return native_fileinput
        if name == "slice":
            if expr.kwargs or len(expr.args) < 1 or len(expr.args) > 3:
                raise NotImplementedError(
                    "Layer 1 builtin slice() supports 1 to 3 positional args"
                )
            none_obj = self._emit_none_literal()
            if len(expr.args) == 1:
                start_obj = none_obj
                stop_obj = self._emit_as_object(expr.args[0])
                step_obj = none_obj
            else:
                start_obj = self._emit_as_object(expr.args[0])
                stop_obj = self._emit_as_object(expr.args[1])
                step_obj = (
                    self._emit_as_object(expr.args[2])
                    if len(expr.args) == 3
                    else none_obj
                )
            return self.builder.call(
                self.runtime["py_slice_new"],
                [start_obj, stop_obj, step_obj],
                name=self._fresh("slice.new"),
            )
        # Builtins below don't support kwargs — reject early.
        if expr.kwargs and name in ("range", "xrange", "len", "str", "isinstance"):
            raise NotImplementedError(
                f"Layer 1 builtin {name}() does not accept keyword args"
            )
        if name in ("range", "xrange"):
            return self._emit_range_value_call(expr)
        if name in ("_walrus", "__walrus__"):
            return self._emit_walrus(expr)
        if name == "__pcc_format_spec":
            result = self._emit_format_spec_builtin(expr)
            if result is not None:
                return result
        builtin_value = self._native_builtin_value_for_name(name)
        if builtin_value == "builtins.int" and 1 <= len(expr.args) <= 2:
            result = self._maybe_emit_int_builtin(expr)
            if result is not None:
                return result
        if builtin_value in (
            "math.floor",
            "math.ceil",
            "math.copysign",
            "math.sqrt",
            "math.pow",
            "math.trunc",
            "math.gcd",
            "math.factorial",
            "math.isqrt",
        ):
            result = self._emit_native_math_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value in ("re.match", "re.search", "re.fullmatch"):
            result = self._emit_native_re_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value is not None:
            result = self._emit_native_builtin_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value is not None and builtin_value.startswith("gc."):
            result = self._emit_native_gc_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value is not None and builtin_value.startswith("weakref."):
            result = self._emit_native_weakref_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value in ("asyncio.run", "asyncio.sleep"):
            result = self._emit_native_asyncio_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value is not None and builtin_value.startswith("threading."):
            result = self._emit_native_threading_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
            )
            if result is not None:
                return result
        if builtin_value is not None and builtin_value.startswith(
            "pcc.virtual_thread."
        ):
            result = self._emit_native_virtual_thread_value_call(
                builtin_value,
                expr.args,
                expr.kwargs,
                expr,
            )
            if result is not None:
                return result
        if builtin_value == "enum.auto" and not expr.args and not expr.kwargs:
            return self._emit_none_literal()
        if name == "len":
            return self._emit_len_call(expr)
        if name == "str":
            return self._emit_str_builtin(expr)
        if name in ("bytes", "bytearray", "memoryview"):
            result = self._emit_bytes_family_builtin(expr, name)
            if result is not None:
                return result
        if name == "object" and not expr.args and not expr.kwargs:
            # pcc only needs bare object() today as a unique identity sentinel.
            return self.builder.call(
                self.runtime["py_dict_new"],
                [],
                name=self._fresh("object.sentinel"),
            )
        if name == "dict":
            return self._emit_dict_builtin(expr)
        if name == "list" and not expr.args and not expr.kwargs:
            return self.builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("list.new"),
            )
        if name == "set" and not expr.args and not expr.kwargs:
            return self.builder.call(
                self.runtime["py_set_new"],
                [],
                name=self._fresh("set.new"),
            )
        if name == "tuple" and not expr.args and not expr.kwargs:
            return self.builder.call(
                self.runtime["py_tuple_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("tuple.new"),
            )
        if name == "enumerate":
            result = self._emit_enumerate_builtin(expr)
            if result is not None:
                return result
        if name == "isinstance":
            return self._emit_isinstance_call(expr)
        # ``field(default_factory=F)`` from ``dataclasses.field``
        # appears as the RHS of a dataclass body assign. At codegen
        # time we collapse it to a call of ``F()``. The other
        # ``field`` kwargs (init, repr, ...) are informational — pcc
        # doesn't vary emission based on them.
        if name == "field" and not expr.args:
            for k, v in expr.kwargs:
                if k == "default_factory":
                    if isinstance(v, Name):
                        # Known builtin factories return a new reference even
                        # when field(...) itself has an imprecise Dyn type.
                        if v.ident in ("list", "dict", "set", "tuple"):
                            factory_args = []
                            if v.ident in ("list", "tuple"):
                                factory_args.append(ir.Constant(_I64, 0))
                            value = self.builder.call(
                                self.runtime["py_" + v.ident + "_new"],
                                factory_args,
                                name=self._fresh("field." + v.ident),
                            )
                            self._note_owned_object_value(value)
                            return value
                    # Unknown factory — attempt to call it as a
                    # user function. Falls back to regular dispatch.
                    return self._emit_call(
                        Call(
                            span=self._expr_span_or_none(expr),
                            ty=expr.ty,
                            func=v,
                            args=(),
                            kwargs=(),
                        )
                    )
            # No default_factory → default value (None).
            return ir.Constant(_CSTR, None)
        # ``cls(args)`` inside a @classmethod body — treat as a
        # normal instantiation of the owning class. pcc doesn't
        # support calling arbitrary ``cls`` pointers yet, so we
        # resolve to the enclosing class statically. Note: gated on
        # ``current_method_kind == "classmethod"`` so that a local
        # re-bind ``cls = SomeClass if cond else OtherClass`` inside
        # an instance method doesn't masquerade as the classmethod
        # receiver.
        if (
            name == "cls"
            and "cls" in self.env
            and self.current_class is not None
            and self.current_method_kind == "classmethod"
        ):
            args = expr.args
            if expr.kwargs:
                # Walk the class's MRO looking for an ``__init__`` —
                # dataclass inheritance means SSAConstant may inherit
                # SSAValue's synthesized init. ``_resolve_method_mro``
                # already handles that walk.
                mro_info = self._resolve_method_mro(
                    self.current_class.name,
                    "__init__",
                )
                init_fd = None
                if mro_info is not None:
                    init_fd = self.class_lowering._find_method_def(
                        mro_info.name,
                        "__init__",
                    )
                if init_fd is not None:
                    args = tuple(
                        self._resolve_call_kwargs(
                            expr.args,
                            expr.kwargs,
                            init_fd.args,
                            skip_self=True,
                        )
                    )
                else:
                    args = expr.args  # fallthrough to original
            return self.class_lowering.emit_instantiate(
                self.current_class.name,
                args,
                self,
            )
        if name in ("min", "max") and not expr.kwargs and len(expr.args) == 2:
            return self._emit_min_max_builtin(expr, name)
        if name in ("min", "max") and not expr.kwargs and len(expr.args) >= 3:
            result = self._emit_min_max_variadic(expr, name)
            if result is not None:
                return result
        if name in ("min", "max") and len(expr.args) == 1:
            # Allow the ``default=`` kwarg: _maybe_emit_min_max_iter consumes it
            # (seeding the accumulator on an empty iterable) and returns None for
            # any other kwarg (e.g. key=, which falls through to libpython).
            result = self._maybe_emit_min_max_iter(expr, name)
            if result is not None:
                return result
        if name == "pow" and not expr.kwargs and len(expr.args) == 2:
            lhs = self._emit_expr(expr.args[0])
            rhs = self._emit_expr(expr.args[1])
            if isinstance(expr.ty, FloatType):
                # ``pow(2, 0.5)``: a float result needs both operands as
                # doubles. _emit_binop_float expects doubles, so coerce here
                # (the ``**`` operator path does the same via _to_double); a raw
                # boxed-int operand otherwise emits invalid 'ptr' vs 'double' IR.
                lf = self._to_double(lhs, expr.args[0].ty)
                rf = self._to_double(rhs, expr.args[1].ty)
                return self._emit_binop_float("**", lf, rf)
            # Route through the same object path the ``**`` operator uses
            # (py_int_pow, result kept as an object) rather than _emit_binop_int,
            # which force-unboxes to i64. A NEGATIVE integer exponent makes
            # py_int_pow return a float (pow(2, -2) == 0.25); the i64 unbox
            # truncated that to 0. Non-negative exponents still yield an int.
            return self._emit_runtime_int_binop_value(
                "**",
                lhs,
                expr.args[0].ty,
                rhs,
                expr.args[1].ty,
            )
        if name == "pow" and not expr.kwargs and len(expr.args) == 3:
            # 3-arg pow(b, e, mod): modular exponentiation. Box the three int
            # operands and call the runtime square-and-multiply helper, which
            # reduces mod ``mod`` every step (never materialises b**e, so it is
            # usable for crypto-size exponents). Result is a boxed int < mod;
            # marshal to the expr's int representation.
            b_obj = self._emit_as_object(expr.args[0])
            e_obj = self._emit_as_object(expr.args[1])
            m_obj = self._emit_as_object(expr.args[2])
            res = self.builder.call(
                self.runtime["py_int_pow_mod"],
                [b_obj, e_obj, m_obj],
                name=self._fresh("pow.mod"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                res,
                IntType(name="int"),
            )
        if name == "divmod" and not expr.kwargs and len(expr.args) == 2:
            if isinstance(expr.args[0].ty, FloatType) or isinstance(
                expr.args[1].ty, FloatType
            ):
                # float divmod: (floor(a/b), a - floor(a/b)*b), both floats,
                # matching CPython (divmod(7.5, 2) == (3.0, 1.5)).
                fa = self._to_double(self._emit_expr(expr.args[0]), expr.args[0].ty)
                fb = self._to_double(self._emit_expr(expr.args[1]), expr.args[1].ty)
                fq = self.builder.call(
                    self._get_floor_intrinsic(),
                    [self.builder.fdiv(fa, fb, name=self._fresh("divmod.f.div"))],
                    name=self._fresh("divmod.f.floor"),
                )
                fr = self.builder.fsub(
                    fa,
                    self.builder.fmul(fq, fb, name=self._fresh("divmod.f.qb")),
                    name=self._fresh("divmod.f.rem"),
                )
                fout = self.builder.call(
                    self.runtime["py_tuple_new"],
                    [ir.Constant(_I64, 2)],
                    name=self._fresh("divmod.f.tuple"),
                )
                fq_obj = self.builder.call(
                    self.runtime["py_float_from_f64"],
                    [fq],
                    name=self._fresh("divmod.f.q"),
                )
                fr_obj = self.builder.call(
                    self.runtime["py_float_from_f64"],
                    [fr],
                    name=self._fresh("divmod.f.r"),
                )
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [fout, ir.Constant(_I64, 0), fq_obj],
                )
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [fout, ir.Constant(_I64, 1), fr_obj],
                )
                self._gc_release(fq_obj)
                self._gc_release(fr_obj)
                return fout
            # int/dyn divmod: route through the object floordiv/mod runtime.
            # int//int delegates to py_int_floordiv/py_int_mod (bignum-aware),
            # so divmod(10**20, 7) is exact; the old i64 path reduced each
            # operand via _emit_expr_as_i64, truncating a bignum to its low 64
            # bits -> (0, 0). py_obj_* also handles any numeric/dunder operand
            # exactly like the // and % operators.
            lhs_obj = self._emit_as_object(expr.args[0])
            rhs_obj = self._emit_as_object(expr.args[1])
            q_obj = self.builder.call(
                self.runtime["py_obj_floordiv"],
                [lhs_obj, rhs_obj],
                name=self._fresh("divmod.q"),
            )
            # py_obj_floordiv raises for float-zero / type errors; int//int
            # returns NULL without raising on a zero divisor (deferred), so
            # surface that as ZeroDivisionError like the // operator does.
            self._emit_post_call_err_check(getattr(expr, "span", None))
            self._emit_zero_division_if_null(q_obj, "division by zero")
            r_obj = self.builder.call(
                self.runtime["py_obj_mod"],
                [lhs_obj, rhs_obj],
                name=self._fresh("divmod.r"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            self._emit_zero_division_if_null(r_obj, "division by zero")
            out = self.builder.call(
                self.runtime["py_tuple_new"],
                [ir.Constant(_I64, 2)],
                name=self._fresh("divmod.tuple"),
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [out, ir.Constant(_I64, 0), q_obj],
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [out, ir.Constant(_I64, 1), r_obj],
            )
            self._gc_release(q_obj)
            self._gc_release(r_obj)
            return out
        if name == "abs" and len(expr.args) == 1:
            return self._emit_abs_builtin(expr)
        if name in ("bin", "hex", "oct") and len(expr.args) == 1 and not expr.kwargs:
            # bin()/hex()/oct() -> base-prefixed string via the runtime; the
            # arg is boxed so int (tagged/heap) and bool all route natively.
            arg_obj = self._emit_as_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_builtin_" + name],
                [arg_obj],
                name=self._fresh(name),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        if name == "callable" and len(expr.args) == 1 and not expr.kwargs:
            # callable(x) -> py_True/py_False. The arg is boxed so any type
            # (tagged int, function, class, instance) routes natively; the
            # runtime classifies callability the same way py_obj_call
            # dispatches. Never raises, so no post-call err check is needed.
            # A bare user-function argument (``callable(f)``) must lower to
            # the NATIVE py_func value (tag PY_TYPE_FUNC), not the
            # ``py_cpy_wrap_pcc_*`` PyCFunction wrap — the cpy wrap drags
            # libpython back in and strict --python-libpython=off rejects
            # the whole compile ("generated IR still calls py_cpy_*").
            old_prefer_native = self._prefer_native_callable_values
            self._prefer_native_callable_values = True
            try:
                arg_obj = self._emit_as_object(expr.args[0])
            finally:
                self._prefer_native_callable_values = old_prefer_native
            return self.builder.call(
                self.runtime["py_builtin_callable"],
                [arg_obj],
                name=self._fresh("callable"),
            )
        if name in ("any", "all") and len(expr.args) == 1:
            result = self._maybe_emit_any_all_literal(expr, name)
            if result is not None:
                return result
        if name == "sum" and 1 <= len(expr.args) <= 2:
            result = self._maybe_emit_sum_literal(expr)
            if result is not None:
                return result
        if name == "zip":
            result = self._maybe_emit_zip_builtin(expr)
            if result is not None:
                return result
        if name == "globals" and not expr.args and not expr.kwargs:
            return self._emit_globals_builtin()
        if name == "iter":
            result = self._maybe_emit_iter_builtin(expr)
            if result is not None:
                return result
        if name == "next":
            result = self._maybe_emit_next_builtin(expr)
            if result is not None:
                return result
        if name == "int" and 1 <= len(expr.args) <= 2:
            result = self._maybe_emit_int_builtin(expr)
            if result is not None:
                return result
        if name == "bool" and len(expr.args) == 1:
            # ``bool(x)`` — truthiness check; reuse ``_truthy`` on the
            # operand's type. Zero args (``bool()`` → ``False``)
            # handled trivially.
            v = self._emit_expr(expr.args[0])
            return self._truthy(v, expr.args[0].ty)
        if name == "bool" and not expr.args:
            return ir.Constant(_I1, 0)
        if name == "format" and not expr.kwargs and 1 <= len(expr.args) <= 2:
            value_obj = self._emit_expr_as_pcc_object(expr.args[0])
            if len(expr.args) == 2:
                if (
                    isinstance(expr.args[0].ty, StrType)
                    and isinstance(expr.args[1], StrLit)
                    and expr.args[1].value == ""
                ):
                    # ``format(exact_str, "")`` is the bare-field f-string
                    # path.  Preserve the owned-result contract with
                    # ``py_obj_str`` while avoiding the generic formatter.
                    # The latter can only add custom ``__format__`` behavior
                    # for non-str/class values, which do not have StrType.
                    result = self.builder.call(
                        self.runtime["py_obj_str"],
                        [value_obj],
                        name=self._fresh("format.str.empty"),
                    )
                    self._emit_post_call_err_check(self._expr_span_or_none(expr))
                    return result
                spec_obj = self._emit_as_object(expr.args[1])
            else:
                spec_obj = self._emit_str_literal("")
            result = self.builder.call(
                self.runtime["py_obj_format"],
                [value_obj, spec_obj],
                name=self._fresh("format.obj"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        if name == "chr" and len(expr.args) == 1 and not expr.kwargs:
            v = self._emit_expr(expr.args[0])
            ty = expr.args[0].ty
            if isinstance(ty, BoolType) and self._ir_type_matches(v.type, _I1):
                v = self.builder.zext(
                    v,
                    _I64,
                    name=self._fresh("chr.from_bool"),
                )
            elif isinstance(ty, IntType):
                v = self._to_int64(v, ty)
            else:
                v = None
            if v is not None:
                return self.builder.call(
                    self.runtime["py_chr_from_i64"],
                    [v],
                    name=self._fresh("chr"),
                )
            codepoint = self.builder.call(
                self.runtime["py_obj_index_i64"],
                [self._emit_expr_as_pcc_object(expr.args[0])],
                name=self._fresh("chr.index"),
            )
            return self.builder.call(
                self.runtime["py_chr_from_i64"],
                [codepoint],
                name=self._fresh("chr"),
            )
        if name == "float" and len(expr.args) == 1:
            arg = expr.args[0]
            ty = arg.ty
            if isinstance(ty, FloatType):
                return self._emit_expr(arg)
            if isinstance(ty, (IntType, BoolType)):
                v = self._emit_expr(arg)
                return self._to_double(v, ty)
            # Issue 11.A.2: ``float("inf")`` / ``float("-inf")`` /
            # ``float("nan")`` and other StrLit args fold to a native
            # constant at codegen time so we don't pull libpython for
            # what should be a compile-time literal.
            if isinstance(arg, StrLit):
                folded = _maybe_fold_str_to_float(arg.value)
                if folded is not None:
                    return ir.Constant(_DOUBLE, folded)
            # ``float(<str>)`` (non-literal): parse the string at runtime via the
            # str-aware py_float_value_of (raises ValueError on a bad string).
            # Without this, a str routed through py_float_to_f64 wrongly yielded
            # 0.0.
            if isinstance(ty, StrType):
                arg_obj = self._emit_as_object(arg)
                result = self.builder.call(
                    self.runtime["py_float_value_of"],
                    [arg_obj],
                    name=self._fresh("float.str"),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                return result
            # DynType receiver — unbox via the pcc-native runtime helper.
            # py_float_value_of is str-aware (parses a runtime str, else unboxes
            # int/float/bool); py_cpy_to_f64 handles already-CPython refs.
            if isinstance(ty, DynType):
                v = self._emit_expr(arg)
                if isinstance(v.type, ir.PointerType):
                    if v in getattr(self, "_cpy_values", ()):
                        return self._to_double(v, ty)
                    result = self.builder.call(
                        self.runtime["py_float_value_of"],
                        [v],
                        name=self._fresh("float.value_of"),
                    )
                    self._emit_post_call_err_check(getattr(expr, "span", None))
                    return result
        if name == "complex" and not expr.kwargs and len(expr.args) <= 2:
            real = ir.Constant(_DOUBLE, 0.0)
            imag = ir.Constant(_DOUBLE, 0.0)
            if len(expr.args) >= 1:
                real_raw = self._emit_expr(expr.args[0])
                real = self._to_double(real_raw, expr.args[0].ty)
            if len(expr.args) == 2:
                imag_raw = self._emit_expr(expr.args[1])
                imag = self._to_double(imag_raw, expr.args[1].ty)
            return self.builder.call(
                self.runtime["py_complex_new"],
                [real, imag],
                name=self._fresh("complex.new"),
            )
        if name == "round" and not expr.kwargs and 1 <= len(expr.args) <= 2:
            raw = self._emit_expr(expr.args[0])
            value = self._to_double(raw, expr.args[0].ty)
            if len(expr.args) == 1:
                # round(x) uses banker's rounding (round half to even) via
                # libm rint(), matching CPython (round(2.5)==2, round(0.5)==0).
                rounded = self.builder.call(
                    self._get_rint_function(),
                    [value],
                    name=self._fresh("round.rint"),
                )
                as_i64 = self.builder.fptosi(
                    rounded,
                    _I64,
                    name=self._fresh("round.i64"),
                )
                return self.builder.call(
                    self.runtime["py_int_from_i64"],
                    [as_i64],
                    name=self._fresh("round.int"),
                )
            digits_raw = self._emit_expr(expr.args[1])
            digits_i64 = self._to_int64(digits_raw, expr.args[1].ty)
            rounded = self.builder.call(
                self.runtime["py_float_round_ndigits"],
                [value, digits_i64],
                name=self._fresh("round.float"),
            )
            if isinstance(expr.args[0].ty, (IntType, BoolType)):
                # round(int, ndigits) returns an int in CPython (round(12345,-2)
                # == 12300, not 12300.0). py_float_round_ndigits gives the right
                # value as a float object; convert back to int. Exact for the
                # common range (|value| < 2**53); huge ints lose precision (rare).
                rounded_d = self.builder.call(
                    self.runtime["py_float_to_f64"],
                    [rounded],
                    name=self._fresh("round.int.f64"),
                )
                as_i64 = self.builder.fptosi(
                    rounded_d, _I64, name=self._fresh("round.int.i64")
                )
                return self.builder.call(
                    self.runtime["py_int_from_i64"],
                    [as_i64],
                    name=self._fresh("round.int.box"),
                )
            return rounded
        if name in ("set", "frozenset") and len(expr.args) <= 1:
            # pcc has no distinct ``frozenset`` runtime type; treat
            # as ``set`` — immutable vs mutable doesn't matter for
            # the compile-free pcc path since we don't mutate the
            # constant containers declared as module globals.
            result = self._maybe_emit_set_builtin(expr)
            if result is not None:
                return result
        if name == "list" and len(expr.args) <= 1:
            result = self._maybe_emit_list_builtin(expr)
            if result is not None:
                return result
        if name == "tuple" and len(expr.args) <= 1:
            if bootstrap_trace_enabled(self.module.name):
                import os
                import sys

                if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE"):
                    sys.stderr.write(
                        "debug: call_tuple_branch args_len="
                        + str(len(expr.args))
                        + "\n"
                    )
            result = self._maybe_emit_tuple_builtin(expr)
            if bootstrap_trace_enabled(self.module.name):
                import os
                import sys

                if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE"):
                    sys.stderr.write(
                        "debug: call_tuple_branch result="
                        + str(result is not None)
                        + "\n"
                    )
            if result is not None:
                return result
        if name == "dict" and len(expr.args) <= 1:
            result = self._maybe_emit_dict_builtin(expr)
            if result is not None:
                return result
        if name == "staticmethod" and len(expr.args) == 1 and not expr.kwargs:
            return self._emit_expr_with_native_callable_values(expr.args[0])
        if name == "property" and 1 <= len(expr.args) <= 3 and not expr.kwargs:
            prop_args: list[ir.Value] = []
            for arg in expr.args:
                value = self._emit_expr_with_native_callable_values(arg)
                prop_args.append(
                    marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        value,
                        arg.ty,
                    )
                )
            while len(prop_args) < 3:
                prop_args.append(ir.Constant(_CSTR, None))
            return self.builder.call(
                self.runtime["py_property_new"],
                prop_args,
                name=self._fresh("property"),
            )
        if name == "classmethod" and len(expr.args) == 1 and not expr.kwargs:
            func_obj = self._emit_expr_with_native_callable_values(expr.args[0])
            return self.builder.call(
                self.runtime["py_classmethod_new"],
                [func_obj],
                name=self._fresh("classmethod"),
            )
        if name == "sorted" and len(expr.args) == 1:
            # sorted(x) or sorted(x, reverse=<bool const>). A constant
            # reverse=True reverses the result list in place after sorting.
            # key= (first-class function) and a non-constant reverse fall
            # through to the libpython path.
            reverse_const = None
            key_expr = None
            other_kwarg = False
            for kw_name, kw_val in expr.kwargs or ():
                if kw_name == "reverse" and isinstance(kw_val, BoolLit):
                    reverse_const = bool(kw_val.value)
                elif kw_name == "key":
                    key_expr = kw_val
                else:
                    other_kwarg = True
            # sorted(xs, key=<supported inline callable>): inline the key
            # extraction (no first-class-function boxing). An unsupported key
            # (or any other kwarg) yields None / falls through to the libpython
            # path — we must NOT run plain py_obj_sorted below, which would
            # silently ignore the key.
            if key_expr is not None:
                if not other_kwarg:
                    keyed = self._emit_sorted_with_key_lambda(
                        expr, key_expr, reverse_const
                    )
                    if keyed is not None:
                        return keyed
            elif not other_kwarg:
                # Custom-class elements with a user __lt__: the runtime
                # comparison primitive (py_obj_cmp_threeway, behind
                # py_obj_sorted) pointer-compares instances and never
                # dispatches a Python __lt__, so sorted() over such a list
                # would return it unordered. Route to the SAME static
                # __lt__ insertion sort that list.sort() uses, on a COPY
                # (sorted() returns a new list). See docs/investigations/
                # sorted-min-max-custom-lt-not-used-no-libpython.md.
                elem_hint = self._list_elem_class_hint_for_expr(expr.args[0])
                if elem_hint is None and isinstance(expr.args[0], Name):
                    elem_hint = self.env_list_elem_class_hint.get(expr.args[0].ident)
                if (
                    elem_hint is not None
                    and self._resolve_method_mro(elem_hint, "__lt__") is not None
                ):
                    src_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        self._emit_expr(expr.args[0]),
                        expr.args[0].ty,
                    )
                    new_list = self.builder.call(
                        self.runtime["py_list_new"],
                        [ir.Constant(_I64, 0)],
                        name=self._fresh("sorted.lt.copy"),
                    )
                    self.builder.call(
                        self.runtime["py_list_extend"],
                        [new_list, src_obj],
                    )
                    elem_ty = (
                        expr.args[0].ty.elem
                        if isinstance(expr.args[0].ty, ListType)
                        else DynType(name="dyn")
                    )
                    self._emit_list_sort_with_dunder_lt(new_list, elem_hint, elem_ty)
                    if reverse_const:
                        self.builder.call(
                            self.runtime["py_list_reverse"],
                            [new_list],
                        )
                    return new_list
                src_val = self._emit_expr(expr.args[0])
                src_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    src_val,
                    expr.args[0].ty,
                )
                result = self.builder.call(
                    self.runtime["py_obj_sorted"],
                    [src_obj],
                    name=self._fresh("sorted"),
                )
                if reverse_const:
                    self.builder.call(
                        self.runtime["py_list_reverse"],
                        [result],
                    )
                return result
        if name == "reversed" and len(expr.args) == 1 and not expr.kwargs:
            return self._emit_reversed_builtin(expr)
        if name == "repr" and len(expr.args) == 1:
            arg_obj = self._emit_expr_as_pcc_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_obj_repr"],
                [arg_obj],
                name=self._fresh("repr"),
            )
            # The runtime helper borrows its operand, so a temporary produced
            # by the argument expression is consumed here.  A borrowed operand
            # is left alone by the classifier.
            self._gc_release_if_owned(arg_obj, expr.args[0])
            return result
        if name == "ascii" and len(expr.args) == 1:
            arg_obj = self._emit_expr_as_pcc_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_obj_ascii"],
                [arg_obj],
                name=self._fresh("ascii"),
            )
            # The runtime helper borrows its operand, so a temporary produced
            # by the argument expression is consumed here.  A borrowed operand
            # is left alone by the classifier.
            self._gc_release_if_owned(arg_obj, expr.args[0])
            return result
        if name == "hash" and len(expr.args) == 1:
            arg_obj = self._emit_expr_as_pcc_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_obj_hash"],
                [arg_obj],
                name=self._fresh("hash"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            # Released after the error check so a raising __hash__ takes the
            # error edge with the operand still live.
            self._gc_release_if_owned(arg_obj, expr.args[0])
            return result
        if name == "id" and len(expr.args) == 1:
            v = self._emit_as_object(expr.args[0])
            return self.builder.ptrtoint(
                v,
                _I64,
                name=self._fresh("id"),
            )
        if name == "hasattr" and len(expr.args) == 2:
            native_module_hasattr = self._maybe_emit_native_module_hasattr(expr)
            if native_module_hasattr is not None:
                return native_module_hasattr
            # ``hasattr(x, "name")`` — pcc doesn't distinguish "missing"
            # from "present but None" without full dunder support, but
            # for the common usage (gate on attribute existence) the
            # presence-check via py_obj_getattr returning non-NULL
            # works on pcc-native classes.
            if self._expr_looks_cpython(expr.args[0]):
                fn_val = self._load_cpython_builtin("hasattr")
                got = self._emit_cpy_func_call(
                    fn_val,
                    "hasattr",
                    tuple(expr.args),
                )
                self._guard_cpy_value_not_null(got)
                as_i32 = self.builder.call(
                    self.runtime["py_cpy_truthy"],
                    [got],
                    name=self._fresh("hasattr.cpy.i32"),
                )
                self._guard_cpy_status_not_negative(as_i32, (got,))
                self.builder.call(self.runtime["py_cpy_decref"], [got])
                self._forget_owned_cpy_value(got)
                return self.builder.icmp_signed(
                    "!=",
                    as_i32,
                    ir.Constant(_I32, 0),
                    name=self._fresh("hasattr.cpy.i1"),
                )
            obj = self._emit_as_object(expr.args[0])
            nm = expr.args[1]
            if isinstance(nm, StrLit):
                name_ptr = self._attr_name_ptr(nm.value)
            else:
                nv = self._emit_expr(nm)
                n_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    nv,
                    nm.ty,
                )
                name_ptr = self.builder.call(
                    self.runtime["py_str_utf8"],
                    [n_obj],
                    name=self._fresh("hasattr.name"),
                )
            # The no-raise probe: a plain miss returns NULL without paying an
            # AttributeError construction that the branch below would clear.
            got = self.builder.call(
                self.runtime["py_obj_getattr_maybe"],
                [obj, name_ptr],
                name=self._fresh("hasattr.got"),
            )
            null = ir.Constant(_CSTR, None)
            present = self.builder.icmp_signed(
                "!=",
                got,
                null,
                name=self._fresh("hasattr.i1"),
            )
            parent_fn = self.current_function
            missing_bb = parent_fn.append_basic_block(
                name=self._fresh("hasattr.missing"),
            )
            present_bb = parent_fn.append_basic_block(
                name=self._fresh("hasattr.present"),
            )
            end_bb = parent_fn.append_basic_block(
                name=self._fresh("hasattr.end"),
            )
            self.builder.cbranch(present, present_bb, missing_bb)
            self.builder.position_at_end(missing_bb)
            self.builder.call(self.runtime["py_clear_exception"], [])
            self.builder.branch(end_bb)
            missing_exit = self.builder._block
            self.builder.position_at_end(present_bb)
            # Every py_obj_getattr(_maybe) edge returns an OWNED reference
            # (instance/class getattr, dunder-class, and the builtin bound
            # helpers all incref or fabricate).  hasattr only needs the i1, so
            # the found object must be released here or every probe that
            # fabricates (e.g. hasattr(lst, "pop") building a bound method)
            # leaks one object per call.
            self.builder.call(self.runtime["py_decref"], [got])
            self.builder.branch(end_bb)
            present_exit = self.builder._block
            self.builder.position_at_end(end_bb)
            phi = self.builder.phi(_I1, name=self._fresh("hasattr.result"))
            phi.add_incoming(ir.Constant(_I1, 0), missing_exit)
            phi.add_incoming(ir.Constant(_I1, 1), present_exit)
            return phi
        if name == "vars" and len(expr.args) == 1 and not expr.kwargs:
            result = self.builder.call(
                self.runtime["py_obj_vars"],
                [self._emit_expr_as_pcc_object(expr.args[0])],
                name=self._fresh("vars"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        if name == "issubclass" and len(expr.args) == 2:
            result = self._maybe_emit_issubclass_builtin(expr)
            if result is not None:
                return result
        if name == "ord" and len(expr.args) == 1:
            # ``ord(s)`` where s is a one-char str. Return the first
            # Unicode codepoint, matching CPython for valid pcc strings.
            ord_arg = expr.args[0]
            if (
                isinstance(ord_arg, Subscript)
                and not isinstance(ord_arg.idx, Slice)
                and isinstance(ord_arg.obj.ty, StrType)
            ):
                s_val = self._emit_expr(ord_arg.obj)
                idx_val = self._emit_expr_as_i64(ord_arg.idx)
                return self.builder.call(
                    self.runtime["py_str_ord_at_i64"],
                    [s_val, idx_val],
                    name=self._fresh("ord.at"),
                )
            s_val = self._emit_as_object(ord_arg)
            return self.builder.call(
                self.runtime["py_str_ord"],
                [s_val],
                name=self._fresh("ord"),
            )
        module_name_for_call = self.module.name or ""
        if (
            module_name_for_call == "pcc.parse.py_lex"
            and name == "_pcc_str_byte_len"
            and len(expr.args) == 1
        ):
            s_val = self._emit_expr(expr.args[0])
            return self.builder.call(
                self.runtime["py_str_byte_len"],
                [s_val],
                name=self._fresh("str.byte_len"),
            )
        if (
            module_name_for_call == "pcc.parse.py_lex"
            and name == "_pcc_str_byte_at"
            and len(expr.args) == 2
        ):
            s_val = self._emit_expr(expr.args[0])
            idx_val = self._emit_expr_as_i64(expr.args[1])
            return self.builder.call(
                self.runtime["py_str_byte_at_i64"],
                [s_val, idx_val],
                name=self._fresh("str.byte_at"),
            )
        if (
            module_name_for_call == "pcc.parse.py_lex"
            and name == "_pcc_str_byte_slice"
            and len(expr.args) == 3
        ):
            s_val = self._emit_expr(expr.args[0])
            lo_val = self._emit_expr_as_i64(expr.args[1])
            hi_val = self._emit_expr_as_i64(expr.args[2])
            return self.builder.call(
                self.runtime["py_str_byte_slice_i64"],
                [s_val, lo_val, hi_val],
                name=self._fresh("str.byte_slice"),
            )
        if name == "setattr" and len(expr.args) == 3:
            return self._emit_setattr_builtin(expr)
        if name == "delattr" and len(expr.args) == 2:
            return self._emit_delattr_builtin(expr)
        if name == "getattr" and 2 <= len(expr.args) <= 3:
            return self._emit_getattr_builtin(expr)
        if name == "type" and len(expr.args) == 3:
            dynamic_type = self._maybe_emit_dynamic_type_constructor(expr)
            if dynamic_type is not None:
                return dynamic_type
        if name == "type" and len(expr.args) == 1:
            return self._emit_type_builtin(expr)

        # Extern-C direct call (P6C.1): name bound to extern("symbol"...).
        extern_decls = getattr(self, "_extern_decls", {})
        if name in extern_decls:
            if expr.kwargs:
                raise NotImplementedError(
                    "Layer 1 extern-C calls do not accept keyword args"
                )
            return self._emit_extern_call(extern_decls[name], expr.args)

        # User class instantiation: ``MyClass(args)``.
        class_name = self._resolve_class_alias(name)
        if (
            hasattr(self, "class_lowering")
            and class_name in self.class_lowering.classes
        ):
            class_info = self.class_lowering.classes.get(class_name)
            if class_info is not None:
                metaclass_name = getattr(class_info, "metaclass_name", None)
                if metaclass_name is not None:
                    meta_info = self.class_lowering.classes.get(metaclass_name)
                    if meta_info is not None and "__call__" in meta_info.methods:
                        cls_ptr = self.class_lowering._load_class_object(
                            class_info,
                            ".meta.call.cls",
                        )
                        return self._emit_direct_method_call(
                            meta_info.methods["__call__"],
                            cls_ptr,
                            meta_info,
                            "__call__",
                            expr.args,
                            kwargs=expr.kwargs,
                        )

            def attach_hoisted_class_captures(inst: ir.Value) -> ir.Value:
                class_caps = getattr(
                    self,
                    "_hoisted_class_capture_params",
                    {},
                ).get(class_name, ())
                for fv in class_caps:
                    cap_expr = Name(
                        span=self._expr_span_or_none(expr),
                        ty=DynType(name="dyn"),
                        ident=fv,
                    )
                    raw_v = self._emit_name(cap_expr)
                    v_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        raw_v,
                        cap_expr.ty,
                    )
                    self.builder.call(
                        self.runtime["py_obj_setattr"],
                        [
                            inst,
                            self._attr_name_ptr(
                                self.class_lowering.mangle_private_attr_name(
                                    class_info,
                                    f"__pcc_cap_{fv}",
                                )
                            ),
                            v_obj,
                        ],
                    )
                return inst

            resolved_args = expr.args
            init_fd = self.class_lowering._find_method_def(class_name, "__init__")
            if init_fd is None:
                # Walk MRO for an inherited __init__.
                mro_info = self._resolve_method_mro(class_name, "__init__")
                if mro_info is not None:
                    init_fd = self.class_lowering._find_method_def(
                        mro_info.name,
                        "__init__",
                    )
            if init_fd is None:
                new_info = self._resolve_method_mro(class_name, "__new__")
                if new_info is None and class_info is not None:
                    if "__new__" in class_info.methods:
                        new_info = class_info
                if new_info is not None:
                    new_fn = new_info.methods.get("__new__")
                    if new_fn is not None:
                        cls_ptr = self.class_lowering._load_class_object(
                            class_info,
                            f"cls.{class_name}.__new__",
                        )
                        return attach_hoisted_class_captures(
                            self._emit_direct_method_call(
                                new_fn,
                                cls_ptr,
                                new_info,
                                "__new__",
                                expr.args,
                                kwargs=expr.kwargs,
                            )
                        )
                inst = self._emit_no_init_field_instance(
                    class_name,
                    expr.args,
                    expr.kwargs,
                )
                if inst is not None:
                    return attach_hoisted_class_captures(inst)
                if expr.kwargs:
                    inst = attach_hoisted_class_captures(
                        self.class_lowering.emit_instantiate(
                            class_name,
                            expr.args,
                            self,
                        )
                    )
                    for kw_name, kw_expr in expr.kwargs:
                        raw_v = self._emit_expr(kw_expr)
                        v_obj = marshal.marshal_to_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            raw_v,
                            kw_expr.ty,
                        )
                        self.builder.call(
                            self.runtime["py_obj_setattr"],
                            [inst, self._attr_name_ptr(kw_name), v_obj],
                        )
                    return inst
            else:
                resolved_args = tuple(
                    self._resolve_call_kwargs(
                        expr.args,
                        expr.kwargs,
                        init_fd.args,
                        skip_self=True,
                    )
                )
            if (
                class_info is not None
                and getattr(class_info, "owning_module", None)
                == "pcc.py_frontend.py_ast"
                and len(expr.args) + len(expr.kwargs)
                >= len(tuple(class_info.field_names))
            ):
                inst = self._emit_no_init_field_instance(
                    class_name,
                    expr.args,
                    expr.kwargs,
                    force=True,
                )
                if inst is not None:
                    return attach_hoisted_class_captures(inst)
            return attach_hoisted_class_captures(
                self._emit_class_init_call(class_name, resolved_args)
            )

        # Callable instance via ``__call__`` — ``double(5)`` where
        # ``double`` was assigned a class instance that defines
        # ``__call__``.
        if hasattr(self, "env_class_hint"):
            hint = self.env_class_hint.get(name)
            if hint is not None:
                info = self._resolve_method_mro(hint, "__call__")
                if info is not None:
                    obj_val = self._emit_name(
                        Name(
                            span=self._expr_span_or_none(expr),
                            ty=DynType(name="dyn"),
                            ident=name,
                        )
                    )
                    method_fn = info.methods["__call__"]
                    return self._emit_direct_method_call(
                        method_fn,
                        obj_val,
                        info,
                        "__call__",
                        expr.args,
                        kwargs=expr.kwargs,
                    )

        # Runtime bindings shadow same-named FuncDefs. This matters for
        # Python patterns such as ``f = obj.f; return f(...)`` inside a
        # function named ``f``.
        semantic_cross_module = name in getattr(
            self,
            "_cross_module_semantic_functions",
            {},
        )
        if (
            name in self.env
            or name in getattr(self, "_module_globals", {})
            or semantic_cross_module
        ):
            if semantic_cross_module:
                semantic_gv = self._native_extension_modules().get(name)
                if semantic_gv is None:
                    raise NotImplementedError(
                        "semantic cross-module function has no runtime binding: " + name
                    )
                fn_val = self.builder.load(
                    semantic_gv,
                    name=self._fresh(f"decorated.import.{name}"),
                )
            else:
                fn_val = self._emit_name(
                    Name(
                        span=self._expr_span_or_none(expr),
                        ty=DynType(name="dyn"),
                        ident=name,
                    ),
                )
            is_cpy_local = (
                fn_val in getattr(self, "_cpy_values", ())
                or getattr(self, "_cpy_env_flags", {}).get(name, False)
                or getattr(self, "_cpy_module_flags", {}).get(name, False)
            )
            if is_cpy_local:
                if expr.kwargs:
                    return self._finish_cpy_call_kw(
                        fn_val,
                        name,
                        expr.args,
                        expr.kwargs,
                        expr.operand_order,
                    )
                return self._emit_cpy_func_call(fn_val, name, expr.args)
            kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
            arg_exprs = expr.args
            kwargs_expr = None
            if kwdict_unpack is not None:
                arg_exprs, kwargs_expr = kwdict_unpack
            args_owned = not self._is_starred_unpack(arg_exprs)
            args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                expr.kwargs,
                kwargs_expr,
                self._expr_span_or_none(expr),
            )
            result = self.builder.call(
                self.runtime["py_obj_call"],
                [fn_val, args_tuple, kwargs_obj],
                name=self._fresh(f"{name}.obj.call"),
            )
            if args_owned:
                self._gc_release(args_tuple)
            if expr.kwargs:
                self._gc_release(kwargs_obj)
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            # If ``name`` is bound to a callable (e.g. a lambda) that returns a
            # CPython object, tag the indirect-call result cpy so downstream
            # attribute/op lowering uses py_cpy_* (a native getattr on a raw
            # PyObject* segfaults). Mirrors the direct-funcdef path below.
            if self._name_binds_cpy_returning_callable(name):
                # Every callable result crosses this ABI as a new reference.
                # Downstream CPython consumers therefore own and must consume
                # it, just like the direct user-function call path.
                self._mark_owned_cpy_value(result)
            return result

        fn = self.functions.get(name)
        if fn is None:
            native_star_val = self._load_from_native_extension_star_imports(name)
            if native_star_val is not None:
                kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
                arg_exprs = expr.args
                kwargs_expr = None
                if kwdict_unpack is not None:
                    arg_exprs, kwargs_expr = kwdict_unpack
                args_owned = not self._is_starred_unpack(arg_exprs)
                args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
                kwargs_obj = self._emit_dynamic_call_kwargs_object(
                    expr.kwargs,
                    kwargs_expr,
                    self._expr_span_or_none(expr),
                )
                result = self.builder.call(
                    self.runtime["py_obj_call"],
                    [native_star_val, args_tuple, kwargs_obj],
                    name=self._fresh(f"{name}.native.star.call"),
                )
                if args_owned:
                    self._gc_release(args_tuple)
                if expr.kwargs:
                    self._gc_release(kwargs_obj)
                self._gc_release(native_star_val)
                self._emit_post_call_err_check(self._expr_span_or_none(expr))
                return result
            # CPython-backed callable (e.g. a ``from .sibling import
            # foo`` where ``foo`` isn't a native-sibling FuncDef)
            # dispatches via PyObject_Call. Pulls libpython but is
            # correct for the import route.
            cpy_gv = getattr(self, "_cpy_module_env", {}).get(name)
            if cpy_gv is not None:
                fn_val = self.builder.load(
                    cpy_gv,
                    name=self._fresh(f"cpy.fn.{name}"),
                )
                if expr.kwargs:
                    return self._finish_cpy_call_kw(
                        fn_val,
                        name,
                        expr.args,
                        expr.kwargs,
                        expr.operand_order,
                    )
                return self._emit_cpy_func_call(fn_val, name, expr.args)
            star_val = self._load_from_cpy_star_imports(name)
            if star_val is not None:
                if expr.kwargs:
                    return self._finish_cpy_call_kw(
                        star_val,
                        name,
                        expr.args,
                        expr.kwargs,
                        expr.operand_order,
                    )
                return self._emit_cpy_func_call(star_val, name, expr.args)
            # Fallback: route well-known CPython stdlib builtins
            # (``open`` / ``iter`` / ``next`` / ``sorted`` / ``zip`` /
            # ``super`` / ``hasattr`` / etc.) through the libpython
            # fallback. Pulls libpython into the link step but lets
            # the solo-compile survey keep advancing on files that
            # use those callables.
            if name in _CPY_BUILTIN_FALLBACK:
                fn_val = self._load_cpython_builtin(name)
                if expr.kwargs:
                    return self._finish_cpy_call_kw(
                        fn_val,
                        name,
                        expr.args,
                        expr.kwargs,
                        expr.operand_order,
                    )
                return self._emit_cpy_func_call(fn_val, name, expr.args)
            # Local variable holding a callable (e.g. ``klass = self.
            # _select_struct_union_class(p[1]); klass(args)``). The
            # binding lives in env / module_globals. CPython-tagged
            # bindings still route through libpython; pcc-native
            # function/class objects dispatch through py_obj_call.
            if name in self.env or name in getattr(self, "_module_globals", {}):
                fn_val = self._emit_name(
                    Name(
                        span=self._expr_span_or_none(expr),
                        ty=DynType(name="dyn"),
                        ident=name,
                    ),
                )
                is_cpy_local = (
                    fn_val in getattr(self, "_cpy_values", ())
                    or getattr(self, "_cpy_env_flags", {}).get(name, False)
                    or getattr(self, "_cpy_module_flags", {}).get(name, False)
                )
                if is_cpy_local:
                    if expr.kwargs:
                        return self._finish_cpy_call_kw(
                            fn_val,
                            name,
                            expr.args,
                            expr.kwargs,
                            expr.operand_order,
                        )
                    return self._emit_cpy_func_call(fn_val, name, expr.args)
                kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
                arg_exprs = expr.args
                kwargs_expr = None
                if kwdict_unpack is not None:
                    arg_exprs, kwargs_expr = kwdict_unpack
                args_owned = not self._is_starred_unpack(arg_exprs)
                args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
                kwargs_obj = self._emit_dynamic_call_kwargs_object(
                    expr.kwargs,
                    kwargs_expr,
                    self._expr_span_or_none(expr),
                )
                result = self.builder.call(
                    self.runtime["py_obj_call"],
                    [fn_val, args_tuple, kwargs_obj],
                    name=self._fresh(f"{name}.obj.call"),
                )
                if args_owned:
                    self._gc_release(args_tuple)
                if expr.kwargs:
                    self._gc_release(kwargs_obj)
                self._emit_post_call_err_check(self._expr_span_or_none(expr))
                return result
            # Python resolves function names at runtime. If the compiler
            # cannot statically bind the callable, emit a normal name load
            # followed by the pcc-native dynamic call path. An actually
            # missing name then raises NameError at runtime instead of being
            # rejected during compilation; names populated by dynamic import
            # machinery can still be called.
            fn_val = self._emit_name(
                Name(
                    span=self._expr_span_or_none(expr),
                    ty=DynType(name="dyn"),
                    ident=name,
                ),
            )
            kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
            arg_exprs = expr.args
            kwargs_expr = None
            if kwdict_unpack is not None:
                arg_exprs, kwargs_expr = kwdict_unpack
            args_owned = not self._is_starred_unpack(arg_exprs)
            args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                expr.kwargs,
                kwargs_expr,
                self._expr_span_or_none(expr),
            )
            result = self.builder.call(
                self.runtime["py_obj_call"],
                [fn_val, args_tuple, kwargs_obj],
                name=self._fresh(f"{name}.dyn.call"),
            )
            if args_owned:
                self._gc_release(args_tuple)
            if expr.kwargs:
                self._gc_release(kwargs_obj)
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        ast_func_def = self._find_user_funcdef(name)
        if ast_func_def.is_async:
            return self._emit_async_user_function_call(
                name,
                fn,
                ast_func_def,
                expr.args,
                expr.kwargs,
            )
        cached_result = self._maybe_emit_lru_cached_user_function_call(
            name=name,
            fn=fn,
            ast_func_def=ast_func_def,
            args=expr.args,
            kwargs=expr.kwargs,
        )
        if cached_result is not None:
            return cached_result
        if self._decorators_are_native_functions(ast_func_def):
            return self._emit_decorated_user_function_call(
                name=name,
                fn=fn,
                ast_func_def=ast_func_def,
                args=expr.args,
                kwargs=expr.kwargs,
            )
        # Click-decorated entry functions (``@click.command``,
        # ``@click.pass_context``) expose params like
        # ``main(ctx, path, ...)`` that pcc treats as required, but
        # the module's own ``if __name__ == "__main__": main()`` call
        # invokes with no args because click fills them at runtime.
        # Synthesize NoneLit defaults for missing args when the callee
        # carries a click decorator — the ``main()`` call is compiled
        # but never actually exercised unless the binary is run as a
        # script (and in that case click's runtime wrapper supplies
        # the values).
        has_click_decorator = self._func_has_click_decorator(ast_func_def)
        call_kwargs = expr.kwargs
        hoist_caps = getattr(self, "_hoisted_capture_params", {}).get(name)
        if hoist_caps:
            present_kw = set()
            i = 0
            while i < len(call_kwargs):
                k, _value = call_kwargs[i]
                present_kw.add(k)
                i += 1
            extra_kw_list = []
            i = 0
            while i < len(hoist_caps):
                fv = hoist_caps[i]
                if fv not in present_kw:
                    extra_kw_list.append(
                        (
                            fv,
                            Name(
                                span=self._expr_span_or_none(expr),
                                ty=DynType(name="dyn"),
                                ident=fv,
                            ),
                        )
                    )
                i += 1
            extra_kw = tuple(extra_kw_list)
            if extra_kw:
                call_kwargs = call_kwargs + extra_kw
        if has_click_decorator:
            from ..py_ast import NoneLit as _NL, Arg as _Arg

            patched_list = []
            i = 0
            while i < len(ast_func_def.args):
                a = ast_func_def.args[i]
                patched_list.append(
                    a if a.default is not None else _replace_arg_with_none_default(a)
                )
                i += 1
            patched = tuple(patched_list)
            try:
                resolved_args = self._resolve_call_kwargs(
                    expr.args,
                    call_kwargs,
                    patched,
                )
            except L1CodegenError:
                resolved_args = self._resolve_call_kwargs(
                    expr.args,
                    call_kwargs,
                    ast_func_def.args,
                )
        else:
            try:
                resolved_args = self._resolve_call_kwargs(
                    expr.args,
                    call_kwargs,
                    ast_func_def.args,
                )
            except L1CodegenError as exc:
                raise L1CodegenError(
                    str(exc) + " while resolving call to " + repr(name)
                )
        runtime_formals = []
        i = 0
        while i < len(ast_func_def.args):
            a = ast_func_def.args[i]
            if a.name != "":
                runtime_formals.append(a)
            i += 1
        args_ir: list[ir.Value] = []
        pinned_arg_temps: list[tuple[ir.Value, bool]] = []
        i = 0
        while i < len(resolved_args) and i < len(runtime_formals) and i < len(fn.args):
            ast_arg = resolved_args[i]
            arg_def = runtime_formals[i]
            ir_arg = fn.args[i]
            target_ty = arg_def.annotation or DynType(name="dyn")
            param_ir_ty = self._function_arg_ir_type_or_none(fn, i, ir_arg)
            if param_ir_ty is None:
                param_ir_ty = self._abi_ir_type(
                    target_ty,
                    box_int_abi=self._should_box_python_ints(),
                )
            v = self._emit_arg_for_abi_param_with_cleanup(
                ast_arg,
                target_ty,
                param_ir_ty,
                tuple(pinned_arg_temps),
            )
            if (
                not getattr(self, "_freestanding_module", False)
                and not getattr(self, "_module_has_c_abi_export", False)
                and isinstance(v.type, ir.PointerType)
                and v not in getattr(self, "_cpy_values", ())
            ):
                owned = getattr(self, "_last_call_arg_owned_temp", False)
                self._gc_pin(v)
                pinned_arg_temps.append((v, owned))
            args_ir.append(v)
            i += 1
        call_name = (
            ""
            if isinstance(fn.function_type.return_type, ir.VoidType)
            else self._fresh(f"{name}_ret")
        )
        returns_cpython = self._user_func_returns_cpython(
            ast_func_def,
            runtime_formals,
            resolved_args,
        )
        result = self._call_user(
            fn,
            args_ir,
            call_name,
            span=self._expr_span_or_none(expr),
            root_result=self._is_object(ast_func_def.return_ty) and not returns_cpython,
            pinned_arg_temps=tuple(pinned_arg_temps),
        )
        for arg_value, owned in pinned_arg_temps:
            self._gc_unpin(arg_value)
            if owned:
                self._gc_release(
                    arg_value,
                    self._release_context_label("direct_call_arg"),
                )
        if returns_cpython:
            # Python/C-API call convention: a successful function result is
            # a new reference owned by the caller.  The callee promotes any
            # borrowed CPython return before returning it.
            self._mark_owned_cpy_value(result)
        return result
