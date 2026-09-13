"""Native ``threading`` lowering helpers for layer-1 codegen.

This module is intentionally a mixin: the owning ``L1CodeGen`` instance keeps
the IR builder, runtime symbol table, GC ownership helpers, and native-module
alias tracking.  Keeping the methods here moves the threading-specific
dispatch surface out of the already-large ``layer1.py`` file without changing
the public codegen shape.
"""
from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    Call,
    ClassType,
    DictExpr,
    Expr,
    Import,
    ImportFrom,
    ListExpr,
    ListType,
    Module,
    Name,
    NoneLit,
    Subscript,
    Type,
)
from .layer1_support import (
    _import_from_level_or_zero,
    _import_from_module_or_empty,
)
from .vthread_effect_analysis import vthread_delegate_frame_name


_I64 = ir.IntType(64)
_I1 = ir.IntType(1)
_CSTR = ir.IntType(8).as_pointer()


def _import_names(stmt: Import) -> tuple[tuple[str, str], ...]:
    return stmt.names


class NativeThreadingLoweringMixin:
    _THREADING_CONSTRUCTOR_NAMES = frozenset(
        {
            "Thread",
            "Lock",
            "RLock",
            "Event",
            "Condition",
            "Semaphore",
        }
    )

    def _module_imports_threading(self, module: Module) -> bool:
        for stmt in module.body:
            if isinstance(stmt, Import):
                for name, _asname in _import_names(stmt):
                    if name == "threading" or name.startswith("threading."):
                        return True
            if isinstance(stmt, ImportFrom):
                if (
                    _import_from_level_or_zero(stmt) == 0
                    and _import_from_module_or_empty(stmt) == "threading"
                ):
                    return True
        return False

    def _threading_constructor_kind_for_expr(self, expr: Expr) -> Optional[str]:
        if not isinstance(expr, Call):
            return None
        kind = None
        if isinstance(expr.func, Name):
            kind = self._native_builtin_value_for_name(expr.func.ident)
        elif isinstance(expr.func, Attr) and isinstance(expr.func.obj, Name):
            if self._native_builtin_module_for_name(expr.func.obj.ident) == "threading":
                kind = "threading." + expr.func.name
        if kind is None or not kind.startswith("threading."):
            return None
        tail = kind.split(".")[-1]
        if tail not in self._THREADING_CONSTRUCTOR_NAMES:
            return None
        return tail

    def _threading_kind_for_type(self, ty: Type) -> Optional[str]:
        if not isinstance(ty, ClassType):
            return None
        if ty.module and ty.module != "threading":
            return None
        if ty.name not in self._THREADING_CONSTRUCTOR_NAMES:
            return None
        # Avoid stealing a user-defined class that happens to be named
        # Lock/Thread/etc. Native threading aliases are not registered in
        # class_lowering.classes; local user classes are.
        if (
            hasattr(self, "class_lowering")
            and ty.name in self.class_lowering.classes
        ):
            return None
        return ty.name

    def _threading_list_elem_kind_for_type(self, ty: Type) -> Optional[str]:
        if not isinstance(ty, ListType):
            return None
        return self._threading_kind_for_type(ty.elem)

    def _threading_list_elem_kind_for_expr(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Name):
            kind = getattr(self, "_threading_list_elem_flags", {}).get(expr.ident)
            if kind is not None:
                return kind
        kind = self._threading_list_elem_kind_for_type(expr.ty)
        if kind is not None:
            return kind
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident in (
                "_list_comp",
                "__listcomp__",
                "_gen_comp",
                "__genexpr__",
            )
            and expr.args
        ):
            return (
                self._threading_constructor_kind_for_expr(expr.args[0])
                or self._threading_kind_for_receiver_expr(expr.args[0])
            )
        if not isinstance(expr, ListExpr) or not expr.elems:
            return None
        inferred: Optional[str] = None
        for elem in expr.elems:
            elem_kind = (
                self._threading_constructor_kind_for_expr(elem)
                or self._threading_kind_for_receiver_expr(elem)
            )
            if elem_kind is None:
                return None
            if inferred is None:
                inferred = elem_kind
            elif inferred != elem_kind:
                return None
        return inferred

    def _threading_kind_for_receiver_expr(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Name):
            kind = getattr(self, "_threading_env_flags", {}).get(expr.ident)
            if kind is not None:
                return kind
            slot = self.env.get(expr.ident)
            if slot is not None:
                kind = self._threading_kind_for_type(slot[2])
                if kind is not None:
                    return kind
        kind = self._threading_kind_for_type(expr.ty)
        if kind is not None:
            return kind
        if isinstance(expr, Subscript):
            kind = self._threading_list_elem_kind_for_type(expr.obj.ty)
            if kind is not None:
                return kind
            if isinstance(expr.obj, Name):
                return getattr(self, "_threading_list_elem_flags", {}).get(
                    expr.obj.ident
                )
        return None

    def _threading_arg_map(self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        names: tuple[str, ...],
    ) -> Optional[dict[str, Expr]]:
        values: dict[str, Expr] = {}
        if len(args) > len(names):
            return None
        for i, arg in enumerate(args):
            values[names[i]] = arg
        for key, value in kwargs:
            if key not in names or key in values:
                return None
            values[key] = value
        return values

    def _emit_threading_callable_object(self, expr: Optional[Expr]) -> ir.Value:
        if expr is None or isinstance(expr, NoneLit):
            return self._emit_none_literal()
        if isinstance(expr, Name):
            resolved_name = expr.ident
            fn_ir = self.functions.get(expr.ident)
            if fn_ir is None:
                direct_hoist = f"__nested_{expr.ident}"
                if direct_hoist in self.functions:
                    resolved_name = direct_hoist
                    fn_ir = self.functions[direct_hoist]
                else:
                    matches = [
                        name
                        for name in self.functions
                        if name.startswith(f"{direct_hoist}_")
                    ]
                    if len(matches) == 1:
                        resolved_name = matches[0]
                        fn_ir = self.functions[resolved_name]
            if fn_ir is not None:
                free_names = getattr(
                    self,
                    "_hoisted_capture_params",
                    {},
                ).get(resolved_name, ())
                return self._emit_native_func_value(
                    expr.ident,
                    resolved_name,
                    fn_ir,
                    tuple(free_names),
                )
        return self._emit_as_object(expr)

    def _emit_native_threading_value_call(self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kind == "threading.get_ident" and not args and not kwargs:
            return self.builder.call(
                self.runtime["py_threading_get_ident"],
                [],
                name=self._fresh("threading.get_ident"),
            )
        if kind == "threading.current_thread" and not args and not kwargs:
            return self.builder.call(
                self.runtime["py_threading_current_thread"],
                [],
                name=self._fresh("threading.current_thread"),
            )
        if kind == "threading.Lock" and not args and not kwargs:
            return self.builder.call(
                self.runtime["py_threading_lock_new"],
                [],
                name=self._fresh("threading.Lock"),
            )
        if kind == "threading.RLock" and not args and not kwargs:
            return self.builder.call(
                self.runtime["py_threading_rlock_new"],
                [],
                name=self._fresh("threading.RLock"),
            )
        if kind == "threading.Event" and not args and not kwargs:
            return self.builder.call(
                self.runtime["py_threading_event_new"],
                [],
                name=self._fresh("threading.Event"),
            )
        if kind == "threading.Condition":
            values = self._threading_arg_map(args, kwargs, ("lock",))
            if values is None:
                return None
            lock_expr = values.get("lock")
            lock_obj = (
                self._emit_none_literal()
                if lock_expr is None
                else self._emit_as_object(lock_expr)
            )
            return self.builder.call(
                self.runtime["py_threading_condition_new"],
                [lock_obj],
                name=self._fresh("threading.Condition"),
            )
        if kind == "threading.Semaphore":
            values = self._threading_arg_map(args, kwargs, ("value",))
            if values is None:
                return None
            value_expr = values.get("value")
            initial = (
                ir.Constant(_I64, 1)
                if value_expr is None
                else self._emit_expr_as_i64(value_expr)
            )
            return self.builder.call(
                self.runtime["py_threading_semaphore_new"],
                [initial],
                name=self._fresh("threading.Semaphore"),
            )
        if kind == "threading.Thread":
            values = self._threading_arg_map(
                args,
                kwargs,
                ("group", "target", "name", "args", "kwargs", "daemon"),
            )
            if values is None:
                return None
            group_expr = values.get("group")
            if group_expr is not None and not isinstance(group_expr, NoneLit):
                return None
            kw_expr = values.get("kwargs")
            if kw_expr is not None and not isinstance(kw_expr, NoneLit):
                if not (isinstance(kw_expr, DictExpr) and len(kw_expr.pairs) == 0):
                    raise NotImplementedError(
                        "native threading.Thread target kwargs are not supported yet"
                    )
            target_obj = self._emit_threading_callable_object(values.get("target"))
            args_expr = values.get("args")
            args_obj = (
                self._emit_empty_tuple("thread.args")
                if args_expr is None
                else self._emit_as_object(args_expr)
            )
            return self.builder.call(
                self.runtime["py_threading_thread_new"],
                [target_obj, args_obj],
                name=self._fresh("threading.Thread"),
            )
        return None

    def _emit_native_threading_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        if not isinstance(attr.obj, Name):
            return None
        if self._native_builtin_module_for_name(attr.obj.ident) != "threading":
            return None
        return self._emit_native_threading_value_call(
            "threading." + attr.name,
            expr.args,
            expr.kwargs,
        )

    def _threading_bool_result(self, raw: ir.Value, hint: str) -> ir.Value:
        return self.builder.icmp_signed(
            "==",
            raw,
            ir.Constant(_I64, 0),
            name=self._fresh(hint),
        )

    def _maybe_emit_threading_instance_method(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        kind = self._threading_kind_for_receiver_expr(attr.obj)
        if kind is None:
            return None
        method = attr.name
        recv = self._emit_expr(attr.obj)
        recv_source = attr.obj

        def _release_recv_if_owned(slot: Optional[ir.Value] = None) -> None:
            if slot is not None:
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(slot), ir.Constant(_CSTR, None)],
                )
            else:
                self._gc_release_if_owned(recv, recv_source)

        def _check_threading_rc(
            raw: ir.Value, hint: str, slot: Optional[ir.Value] = None
        ) -> None:
            failed = self.builder.icmp_signed(
                "!=",
                raw,
                ir.Constant(_I64, 0),
                name=self._fresh(hint + ".failed"),
            )
            fail_bb = self.current_function.append_basic_block(
                name=self._fresh(hint + ".fail"),
            )
            ok_bb = self.current_function.append_basic_block(
                name=self._fresh(hint + ".ok"),
            )
            self.builder.cbranch(failed, fail_bb, ok_bb)
            self.builder.position_at_end(fail_bb)
            _release_recv_if_owned(slot)
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [
                    ir.Constant(_I64, 7),
                    self._ptr_to_cstr(
                        self._cstr_global(
                            hint + " failed",
                            self._fresh(".err." + hint),
                        )
                    ),
                ],
                name=self._fresh(hint + ".exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            err_target = getattr(self, "_try_err_block", None)
            if err_target is None:
                err_target = self._ensure_fn_err_exit()
            self.builder.branch(err_target)
            self.builder.position_at_end(ok_bb)

        def _void_call(runtime_name: str) -> ir.Value:
            raw = self.builder.call(
                self.runtime[runtime_name],
                [recv],
                name=self._fresh(runtime_name + ".rc"),
            )
            _check_threading_rc(raw, runtime_name)
            _release_recv_if_owned()
            return self._emit_none_literal()

        def _bool_call(runtime_name: str, hint: str) -> ir.Value:
            raw = self.builder.call(
                self.runtime[runtime_name],
                [recv],
                name=self._fresh(hint + ".rc"),
            )
            _check_threading_rc(raw, hint)
            _release_recv_if_owned()
            return self._threading_bool_result(raw, hint)

        def _check_negative_rc(raw: ir.Value, hint: str) -> None:
            failed = self.builder.icmp_signed(
                "<",
                raw,
                ir.Constant(_I64, 0),
                name=self._fresh(hint + ".failed"),
            )
            fail_bb = self.current_function.append_basic_block(
                name=self._fresh(hint + ".fail"),
            )
            ok_bb = self.current_function.append_basic_block(
                name=self._fresh(hint + ".ok"),
            )
            self.builder.cbranch(failed, fail_bb, ok_bb)
            self.builder.position_at_end(fail_bb)
            _release_recv_if_owned()
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [
                    ir.Constant(_I64, 7),
                    self._ptr_to_cstr(
                        self._cstr_global(
                            hint + " failed",
                            self._fresh(".err." + hint),
                        )
                    ),
                ],
                name=self._fresh(hint + ".exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            err_target = getattr(self, "_try_err_block", None)
            if err_target is None:
                err_target = self._ensure_fn_err_exit()
            self.builder.branch(err_target)
            self.builder.position_at_end(ok_bb)

        def _vthread_park_bool_call(
            runtime_name: str,
            hint: str,
            reacquire_runtime_name: Optional[str] = None,
        ) -> ir.Value:
            raw = self.builder.call(
                self.runtime[runtime_name],
                [recv],
                name=self._fresh(hint + ".vthread.rc"),
            )
            _check_negative_rc(raw, hint)
            # Evaluate the receiver once and persist that exact primitive.
            # Attribute receivers can be owned temporaries and cannot remain
            # in SSA across a suspension, nor be re-read from a mutable field.
            hidden = vthread_delegate_frame_name(expr, "pcc.threading.receiver")
            recv_slot = self._generator_ctx_stack[-1]["frame_slots"][hidden][1]
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(recv_slot), recv],
            )
            _release_recv_if_owned()
            parked = self.builder.icmp_signed(
                "==",
                raw,
                ir.Constant(_I64, 1),
                name=self._fresh(hint + ".parked"),
            )
            park_bb = self.current_function.append_basic_block(
                name=self._fresh(hint + ".park"),
            )
            done_bb = self.current_function.append_basic_block(
                name=self._fresh(hint + ".done"),
            )
            self.builder.cbranch(parked, park_bb, done_bb)
            self.builder.position_at_end(park_bb)
            self._emit_generator_yield_value(self._emit_none_literal())
            if reacquire_runtime_name is not None:
                reacquire_recv = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), self._as_gc_ptr(recv_slot)],
                    name=self._fresh(hint + ".receiver"),
                )
                reacquire_rc = self.builder.call(
                    self.runtime[reacquire_runtime_name],
                    [reacquire_recv],
                    name=self._fresh(hint + ".reacquire.rc"),
                )
                _check_threading_rc(reacquire_rc, hint + ".reacquire", recv_slot)
            if not self._builder_block_is_terminated():
                self.builder.branch(done_bb)
            self.builder.position_at_end(done_bb)
            _release_recv_if_owned(recv_slot)
            return ir.Constant(_I1, 1)

        if kind in ("Lock", "RLock"):
            prefix = "py_threading_lock" if kind == "Lock" else "py_threading_rlock"
            if method == "acquire":
                if kind == "Lock" and len(self._generator_ctx_stack) > 0:
                    return _vthread_park_bool_call(
                        "py_threading_lock_acquire_vthread",
                        "threading.acquire",
                    )
                return _bool_call(prefix + "_acquire", "threading.acquire")
            if method == "release":
                return _void_call(prefix + "_release")
            if method == "__enter__":
                self.builder.call(self.runtime[prefix + "_acquire"], [recv])
                return recv
            if method == "__exit__":
                return _void_call(prefix + "_release")
        if kind == "Event":
            if method == "set":
                return _void_call("py_threading_event_set")
            if method == "clear":
                return _void_call("py_threading_event_clear")
            if method == "is_set":
                raw = self.builder.call(
                    self.runtime["py_threading_event_is_set"],
                    [recv],
                    name=self._fresh("threading.event.is_set.rc"),
                )
                _release_recv_if_owned()
                return self.builder.icmp_signed(
                    "!=",
                    raw,
                    ir.Constant(_I64, 0),
                    name=self._fresh("threading.event.is_set"),
                )
            if method == "wait":
                if len(self._generator_ctx_stack) > 0:
                    return _vthread_park_bool_call(
                        "py_threading_event_wait_vthread",
                        "threading.event.wait",
                    )
                return _bool_call("py_threading_event_wait", "threading.event.wait")
        if kind == "Condition":
            if method == "acquire":
                return _bool_call(
                    "py_threading_condition_acquire", "threading.cond.acquire"
                )
            if method == "release":
                return _void_call("py_threading_condition_release")
            if method == "wait":
                if len(self._generator_ctx_stack) > 0:
                    return _vthread_park_bool_call(
                        "py_threading_condition_wait_vthread",
                        "threading.cond.wait",
                        "py_threading_condition_acquire",
                    )
                return _bool_call("py_threading_condition_wait", "threading.cond.wait")
            if method in ("notify", "notify_all"):
                return _void_call("py_threading_condition_notify")
            if method == "__enter__":
                self.builder.call(
                    self.runtime["py_threading_condition_acquire"], [recv]
                )
                return recv
            if method == "__exit__":
                return _void_call("py_threading_condition_release")
        if kind == "Semaphore":
            if method == "acquire":
                if len(self._generator_ctx_stack) > 0:
                    return _vthread_park_bool_call(
                        "py_threading_semaphore_acquire_vthread",
                        "threading.sem.acquire",
                    )
                return _bool_call(
                    "py_threading_semaphore_acquire", "threading.sem.acquire"
                )
            if method == "release":
                return _void_call("py_threading_semaphore_release")
            if method == "__enter__":
                self.builder.call(
                    self.runtime["py_threading_semaphore_acquire"], [recv]
                )
                return recv
            if method == "__exit__":
                return _void_call("py_threading_semaphore_release")
        if kind == "Thread":
            if method == "start":
                return _void_call("py_threading_thread_start")
            if method == "join":
                return _void_call("py_threading_thread_join")
            if method == "is_alive":
                raw = self.builder.call(
                    self.runtime["py_threading_thread_is_alive"],
                    [recv],
                    name=self._fresh("threading.thread.is_alive.rc"),
                )
                _release_recv_if_owned()
                return self.builder.icmp_signed(
                    "!=",
                    raw,
                    ir.Constant(_I64, 0),
                    name=self._fresh("threading.thread.is_alive"),
                )
        return None


__all__ = ["NativeThreadingLoweringMixin"]
