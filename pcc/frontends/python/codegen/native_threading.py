"""Native ``threading`` lowering helpers for layer-1 codegen.

This module is intentionally a mixin: the owning ``L1CodeGen`` instance keeps
the IR builder, runtime symbol table, GC ownership helpers, and native-module
alias tracking.  Keeping the methods here moves the threading-specific
dispatch surface out of the already-large ``layer1.py`` file without changing
the public codegen shape.
"""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, Call, ClassType, DictExpr, Expr, Import, ImportFrom, ListExpr, ListType, Module, Name, NoneLit, Subscript, Type
from pcc.frontends.python.codegen.layer1_support import _import_from_level_or_zero, _import_from_module_or_empty
from pcc.frontends.python.codegen.vthread_effect_analysis import vthread_delegate_frame_name


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
            "BoundedSemaphore",
        }
    )


    def _threading_method_parameters(self, kind: str, method: str) -> Optional[tuple[str, ...]]:
        """Optional positional-or-keyword parameters of a method whose
        arguments the runtime converts with CPython's rules (``*_args``)."""
        if method == "acquire" and kind in ("Lock", "RLock", "Semaphore"):
            return ("blocking", "timeout")
        if kind == "Semaphore" and method == "release":
            return ("n",)
        if kind == "Event" and method == "wait":
            return ("timeout",)
        if kind == "Thread" and method == "join":
            return ("timeout",)
        return None

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

    def _native_threading_new(self, runtime_name, args, label):
        """A threading constructor's NEW reference, recorded with its owner.

        Unrecorded, the object could not be a slot-call operand
        (``items.append(threading.current_thread())``: "slot-call operand has
        no authoritative source or owned result").
        """
        value = self.builder.call(self.runtime[runtime_name], args, name=self._fresh(label))
        self._note_owned_object_value(value)
        return value

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
            return self._native_threading_new("py_threading_current_thread", [], "threading.current_thread")
        if kind == "threading.Lock" and not args and not kwargs:
            return self._native_threading_new("py_threading_lock_new", [], "threading.Lock")
        if kind == "threading.RLock" and not args and not kwargs:
            return self._native_threading_new("py_threading_rlock_new", [], "threading.RLock")
        if kind == "threading.Event" and not args and not kwargs:
            return self._native_threading_new("py_threading_event_new", [], "threading.Event")
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
            return self._native_threading_new("py_threading_condition_new", [lock_obj], "threading.Condition")
        if kind in ("threading.Semaphore", "threading.BoundedSemaphore"):
            values = self._threading_arg_map(args, kwargs, ("value",))
            if values is None:
                return None
            value_expr = values.get("value")
            value_obj = (
                ir.Constant(_CSTR, None)
                if value_expr is None
                else self._emit_as_object(value_expr)
            )
            bounded = ir.Constant(_I64, 1 if kind == "threading.BoundedSemaphore" else 0)
            label = kind
            result = self.builder.call(
                self.runtime["py_threading_semaphore_new_args"], [value_obj, bounded],
                name=self._fresh(label),
            )
            if value_expr is not None:
                self._gc_release_if_owned(value_obj, value_expr)
            failed = self.builder.icmp_unsigned(
                "==", self.builder.ptrtoint(result, _I64), ir.Constant(_I64, 0),
                name=self._fresh(label + ".failed"),
            )
            fail_bb = self.current_function.append_basic_block(name=self._fresh(label + ".fail"))
            ok_bb = self.current_function.append_basic_block(name=self._fresh(label + ".ok"))
            self.builder.cbranch(failed, fail_bb, ok_bb)
            self.builder.position_at_end(fail_bb)
            self._threading_raise_unless_pending(label + " failed")
            self.builder.position_at_end(ok_bb)
            self._note_owned_object_value(result)
            return result
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
            if kw_expr is not None and (
                isinstance(kw_expr, NoneLit)
                or (isinstance(kw_expr, DictExpr) and len(kw_expr.pairs) == 0)
            ):
                kw_expr = None
            target_expr = values.get("target")
            target_obj = self._emit_threading_callable_object(target_expr)
            target_root = self._enter_container_temp_root(target_obj, self._fresh("thread.target"))
            target_owned = self._value_is_owned_object(target_obj)
            if target_expr is not None:
                target_owned = target_owned or self._owned_release_needed(target_obj, target_expr)
            roots = ((target_root, target_owned),)
            args_expr = values.get("args")
            if args_expr is None:
                args_obj = self._emit_empty_tuple("thread.args")
                args_owned = True
            else:
                args_obj = self._emit_expr_with_cpy_operand_cleanup(
                    args_expr, (), as_object=True, rooted_pcc_lifetimes=roots,
                )
                args_owned = self._owned_release_needed(args_obj, args_expr)
            args_root = self._enter_container_temp_root(args_obj, self._fresh("thread.args"))
            roots = roots + ((args_root, args_owned),)
            callable_root = target_root
            if kw_expr is not None:
                # Thread.run calls target(*args, **kwargs): bind the keywords
                # to the target, and the thread calls partial(*args).
                kw_obj = self._emit_expr_with_cpy_operand_cleanup(
                    kw_expr, (), as_object=True, rooted_pcc_lifetimes=roots,
                )
                kw_owned = self._owned_release_needed(kw_obj, kw_expr)
                kw_root = self._enter_container_temp_root(kw_obj, self._fresh("thread.kwargs"))
                roots = roots + ((kw_root, kw_owned),)
                bound = self._emit_empty_tuple("thread.bound.args")
                bound_root = self._enter_container_temp_root(bound, self._fresh("thread.bound.args"))
                roots = roots + ((bound_root, True),)
                bound_target = self.builder.call(
                    self.runtime["py_functools_partial_kw"],
                    [
                        self.builder.call(
                            self.runtime["pcc_gc_load_ptr"],
                            [ir.Constant(_CSTR, None), self._as_gc_ptr(slot)],
                            name=self._fresh("thread.partial.operand"),
                        )
                        for slot in (target_root, bound_root, kw_root)
                    ],
                    name=self._fresh("thread.target.keywords"),
                )
                callable_root = self._enter_container_temp_root(
                    bound_target, self._fresh("thread.target.bound"),
                )
                roots = roots + ((callable_root, True),)
            operands = []
            for slot in (callable_root, args_root):
                operands.append(self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), self._as_gc_ptr(slot)],
                    name=self._fresh("thread.argument.current"),
                ))
            result = self.builder.call(
                self.runtime["py_threading_thread_new"], operands,
                name=self._fresh("threading.Thread"),
            )
            result_root = self._enter_container_temp_root(result, self._fresh("thread.result"))
            self._release_rooted_pcc_lifetimes(roots)
            result = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), self._as_gc_ptr(result_root)],
                name=self._fresh("thread.result.current"),
            )
            self._gc_pin(result)
            self._leave_container_temp_root(result_root)
            self._gc_unpin(result)
            self._note_owned_object_value(result)
            return result
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

    def _threading_raise_unless_pending(self, message: str) -> None:
        """Branch to the error exit, raising RuntimeError(``message``) only
        when the failed runtime call left no exception of its own."""
        pending = self.builder.call(self.runtime["py_err_occurred"], [])
        has_error = self.builder.icmp_signed("!=", pending, ir.Constant(_I64, 0))
        make_error = self.current_function.append_basic_block(name=self._fresh("threading.make.error"))
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.cbranch(has_error, err_target, make_error)
        self.builder.position_at_end(make_error)
        error = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 7), self._ptr_to_cstr(self._cstr_global(message, self._fresh(".err.threading")))],
            name=self._fresh("threading.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [error])
        self.builder.branch(err_target)

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
        if kind == "BoundedSemaphore":
            kind = "Semaphore"
        method = attr.name
        parameters = self._threading_method_parameters(kind, method)
        arg_map: Optional[dict[str, Expr]] = None
        if parameters is not None and (expr.args or expr.kwargs or (kind == "Thread" and method == "join")):
            # An invalid shape (too many arguments, an unknown keyword) is
            # left to the dynamic bound method, whose signature binder raises
            # CPython's TypeError.
            arg_map = self._threading_arg_map(expr.args, expr.kwargs, parameters)
            if arg_map is None:
                return None
        if kind == "Condition" and method == "wait":
            # Let the ordinary owned function binder report invalid/expanded
            # calls. Do not evaluate the receiver twice on this fallback.
            if len(expr.args) > 1 or any(key != "timeout" for key, _ in expr.kwargs):
                return None
            if len(expr.kwargs) > 1 or (expr.args and expr.kwargs):
                return None
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
            self._threading_raise_unless_pending(hint + " failed")
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
            self._threading_raise_unless_pending(hint + " failed")
            self.builder.position_at_end(ok_bb)

        def _flag_call(runtime_name: str, hint: str) -> ir.Value:
            # 1/0 answers (locked()); a negative code is a failure.
            raw = self.builder.call(self.runtime[runtime_name], [recv], name=self._fresh(hint + ".rc"))
            _check_negative_rc(raw, hint)
            _release_recv_if_owned()
            return self.builder.icmp_signed("!=", raw, ir.Constant(_I64, 0), name=self._fresh(hint))

        def _args_call(runtime_name: str, hint: str) -> ir.Value:
            """``runtime_name(receiver, *parameters)`` with each optional
            parameter as an object (NULL when omitted); the runtime converts
            them as CPython does. Returns the raw code: 0 True, 1 False."""
            outer_error = self._current_try_err_block()
            if outer_error is None:
                outer_error = self._ensure_fn_err_exit()
            # The receiver and every converted operand stay rooted until the
            # call: a later operand may allocate and move an earlier one.
            roots = [self._enter_virtual_thread_operand_root(recv, recv_source, hint + ".receiver")]

            def cleanup_block(held):
                block = self.current_function.append_basic_block(name=self._fresh(hint + ".operand.cleanup"))
                resume = self.builder.block
                self.builder.position_at_end(block)
                self._release_rooted_pcc_lifetimes(tuple(held))
                self.builder.branch(outer_error)
                self.builder.position_at_end(resume)
                return block

            operand_slots = []
            previous_error = self._try_err_block
            try:
                for parameter in parameters:
                    operand_expr = arg_map.get(parameter)
                    if operand_expr is None:
                        operand_slots.append(None)
                        continue
                    self._try_err_block = cleanup_block(roots)
                    value = self._emit_as_object(operand_expr)
                    root = self._enter_virtual_thread_operand_root(value, operand_expr, hint + "." + parameter)
                    roots.append(root)
                    operand_slots.append(root)
            finally:
                self._try_err_block = previous_error
            receiver = self._load_virtual_thread_operand_root(roots[0])
            operands = [receiver]
            for root in operand_slots:
                if root is None:
                    operands.append(ir.Constant(_CSTR, None))
                else:
                    operands.append(self._load_virtual_thread_operand_root(root))
            raw = self.builder.call(self.runtime[runtime_name], operands, name=self._fresh(hint + ".rc"))
            failed = self.builder.icmp_signed("<", raw, ir.Constant(_I64, 0), name=self._fresh(hint + ".failed"))
            fail_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".fail"))
            ok_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".ok"))
            self.builder.cbranch(failed, fail_bb, ok_bb)
            self.builder.position_at_end(fail_bb)
            previous_error = self._try_err_block
            self._try_err_block = cleanup_block(roots)
            try:
                self._threading_raise_unless_pending(hint + " failed")
            finally:
                self._try_err_block = previous_error
            self.builder.position_at_end(ok_bb)
            self._release_rooted_pcc_lifetimes(tuple(roots))
            return raw

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

        def _condition_wait_call() -> ir.Value:
            hint = "threading.cond.wait"
            generator = len(self._generator_ctx_stack) > 0
            outer_error = self._current_try_err_block()
            if outer_error is None:
                outer_error = self._ensure_fn_err_exit()
            operand_cleanup = self.current_function.append_basic_block(
                name=self._fresh(hint + ".operand.cleanup"),
            )
            if generator:
                hidden = vthread_delegate_frame_name(expr, "pcc.threading.receiver")
                recv_slot = self._generator_ctx_stack[-1]["frame_slots"][hidden][1]
                self.builder.call(self.runtime["pcc_gc_store_root"],
                                  [self._as_gc_ptr(recv_slot), recv])
                _release_recv_if_owned()
                receiver_root = None
            else:
                receiver_root = self._enter_virtual_thread_operand_root(recv, recv_source, hint)
                recv_slot = receiver_root[0]

            def load_receiver() -> ir.Value:
                return self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), self._as_gc_ptr(recv_slot)],
                    name=self._fresh(hint + ".receiver"),
                )

            def release_receiver() -> None:
                if generator:
                    _release_recv_if_owned(recv_slot)
                else:
                    self._release_rooted_pcc_lifetimes((receiver_root,))

            call_bb = self.builder.block
            self.builder.position_at_end(operand_cleanup)
            release_receiver()
            self.builder.branch(outer_error)
            self.builder.position_at_end(call_bb)
            timeout_expr = expr.args[0] if expr.args else None
            if expr.kwargs:
                timeout_expr = expr.kwargs[0][1]
            timeout_value = None
            if timeout_expr is not None:
                previous_error = self._try_err_block
                self._try_err_block = operand_cleanup
                try:
                    timeout_value = self._emit_as_object(timeout_expr)
                finally:
                    self._try_err_block = previous_error
            prefix = "py_threading_condition_wait_vthread" if generator else "py_threading_condition_wait"
            args = [load_receiver()]
            if timeout_value is not None:
                prefix += "_timeout"
                args.append(timeout_value)
            raw = self.builder.call(self.runtime[prefix], args, name=self._fresh(hint + ".rc"))
            if timeout_value is not None:
                self._gc_release_if_owned(timeout_value, timeout_expr)

            def check_rc(value: ir.Value) -> None:
                failed = self.builder.icmp_signed("<", value, ir.Constant(_I64, 0))
                fail_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".fail"))
                ok_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".ok"))
                self.builder.cbranch(failed, fail_bb, ok_bb)
                self.builder.position_at_end(fail_bb)
                pending = self.builder.call(self.runtime["py_err_occurred"], [])
                has_error = self.builder.icmp_signed("!=", pending, ir.Constant(_I64, 0))
                make_error = self.current_function.append_basic_block(name=self._fresh(hint + ".make.error"))
                self.builder.cbranch(has_error, operand_cleanup, make_error)
                self.builder.position_at_end(make_error)
                error = self.builder.call(self.runtime["py_exc_new"], [ir.Constant(_I64, 7),
                    self._ptr_to_cstr(self._cstr_global(hint + " failed", self._fresh(".err.condition")))])
                self.builder.call(self.runtime["py_raise"], [error])
                self.builder.branch(operand_cleanup)
                self.builder.position_at_end(ok_bb)

            check_rc(raw)
            if not generator:
                result = self._threading_bool_result(raw, hint)
                release_receiver()
                return result

            # Only the entry marked 1 parks; 2 is a genuine elapsed timeout.
            # On resume, reacquire and recheck the FIFO token. A timer or an
            # unrelated unpark cannot make wait() return True.
            result_slot = self._alloca_in_entry(_I64, name=self._fresh(hint + ".result"))
            self.builder.store(raw, result_slot)
            select_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".select"))
            park_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".park"))
            done_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".done"))
            cancelled_bb = self.current_function.append_basic_block(name=self._fresh(hint + ".cancelled"))
            self.builder.branch(select_bb)
            self.builder.position_at_end(select_bb)
            selected = self.builder.load(result_slot)
            parked = self.builder.icmp_signed("==", selected, ir.Constant(_I64, 1))
            self.builder.cbranch(parked, park_bb, done_bb)
            self.builder.position_at_end(cancelled_bb)
            self.builder.call(self.runtime["py_threading_condition_wait_cancel"], [load_receiver()])
            self.builder.branch(operand_cleanup)
            self.builder.position_at_end(park_bb)
            self._emit_generator_yield_value(self._emit_none_literal(), resume_err_target=cancelled_bb)
            acquired = self.builder.call(self.runtime["py_threading_condition_acquire"], [load_receiver()])
            check_rc(acquired)
            resumed = self.builder.call(self.runtime["py_threading_condition_wait_resume"], [load_receiver()])
            check_rc(resumed)
            self.builder.store(resumed, result_slot)
            self.builder.branch(select_bb)
            self.builder.position_at_end(done_bb)
            result = self._threading_bool_result(self.builder.load(result_slot), hint)
            release_receiver()
            return result

        if kind in ("Lock", "RLock"):
            prefix = "py_threading_lock" if kind == "Lock" else "py_threading_rlock"
            if method == "acquire":
                if arg_map is not None:
                    raw = _args_call("py_threading_lock_acquire_args", "threading.acquire")
                    return self._threading_bool_result(raw, "threading.acquire")
                if len(self._generator_ctx_stack) > 0:
                    # A virtual thread parks on the lock's FIFO; the release
                    # hands the lock over (an RLock names it as owner).
                    return _vthread_park_bool_call(
                        prefix + "_acquire_vthread",
                        "threading.acquire",
                    )
                return _bool_call(prefix + "_acquire", "threading.acquire")
            if method == "release":
                return _void_call(prefix + "_release")
            if method == "locked":
                return _flag_call("py_threading_lock_locked", "threading.locked")
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
                if arg_map is not None:
                    raw = _args_call("py_threading_event_wait_args", "threading.event.wait")
                    return self._threading_bool_result(raw, "threading.event.wait")
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
                return _condition_wait_call()
            if method == "notify":
                return _void_call("py_threading_condition_notify")
            if method == "notify_all":
                return _void_call("py_threading_condition_notify_all")
            if method == "__enter__":
                self.builder.call(
                    self.runtime["py_threading_condition_acquire"], [recv]
                )
                return recv
            if method == "__exit__":
                return _void_call("py_threading_condition_release")
        if kind == "Semaphore":
            if method == "acquire" and arg_map is not None:
                raw = _args_call("py_threading_semaphore_acquire_args", "threading.sem.acquire")
                return self._threading_bool_result(raw, "threading.sem.acquire")
            if method == "release" and arg_map is not None:
                _args_call("py_threading_semaphore_release_args", "threading.sem.release")
                return self._emit_none_literal()
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
                _args_call("py_threading_thread_join_args", "threading.thread.join")
                return self._emit_none_literal()
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
