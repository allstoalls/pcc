"""print(...) lowering for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import BoolLit, BoolType, Call, FloatType, IntType
from pcc.frontends.python.codegen import marshal


_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class PrintLoweringMixin:
    def _is_native_print_flush_kw(self, k, vexpr) -> bool:
        # ``print(..., flush=True/False)``: the native ``py_print`` /
        # ``py_print_many`` runtime already flushes stdout per line, so a
        # bool-literal ``flush=`` is a no-op we can accept and drop (it only
        # controls *when* CPython flushes, never *what* bytes are emitted).
        # A non-literal ``flush=<expr>`` could have side effects when
        # evaluated for its truthiness, so leave those on the cpython path.
        return k == "flush" and isinstance(vexpr, BoolLit)

    def _print_kwargs_are_native(self, kwargs) -> bool:
        """True when every print kwarg is one we lower natively: ``sep`` /
        ``end`` (handled by ``py_print_many``) or a bool-literal ``flush=``
        (a no-op because the native print path already flushes)."""
        for k, vexpr in kwargs:
            if k == "sep" or k == "end":
                continue
            if self._is_native_print_flush_kw(k, vexpr):
                continue
            return False
        return True

    def _print_kwargs_route_to_file(self, kwargs) -> bool:
        """True when a ``file=`` kwarg is present and every kwarg is one
        ``py_print_to_file`` takes (``sep``/``end``/``file``/``flush``)."""
        has_file = False
        for k, _vexpr in kwargs:
            if k == "file":
                has_file = True
            elif k not in ("sep", "end", "flush"):
                return False
        return has_file

    def _print_kwargs_without_flush(self, kwargs):
        # Drop the accepted bool-literal ``flush=`` before delegating to the
        # ``sep``/``end`` emitters so they never emit a dead truthiness value.
        # With ``file=`` it is kept: a user file object's ``flush()`` runs.
        routes_to_file = False
        for k, _vexpr in kwargs:
            if k == "file":
                routes_to_file = True
        out = []
        for k, vexpr in kwargs:
            if not routes_to_file and self._is_native_print_flush_kw(k, vexpr):
                continue
            out.append((k, vexpr))
        return out

    def _load_print_many_tuple(self, tup, frame_slot):
        if frame_slot is None:
            return tup
        return self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [
                ir.Constant(_CSTR, None),
                self._as_gc_ptr(
                    frame_slot,
                    name=self._fresh("pr.args.frame.ptr"),
                ),
            ],
            name=self._fresh("pr.args.frame.value"),
        )

    def _emit_print_many_arg(self, tup, idx, arg, frame_slot=None) -> None:
        if isinstance(arg.ty, IntType):
            exact_obj = self._maybe_emit_exact_int_object(arg)
            if exact_obj is not None:
                current_tup = self._load_print_many_tuple(tup, frame_slot)
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [current_tup, idx, exact_obj],
                )
                return
        v = self._emit_expr(arg)
        if v in self._cpy_values:
            v_owned = self._cpy_value_is_owned(v)
            pcc_str = self.builder.call(
                self.runtime["py_cpy_to_pcc_str"],
                [v],
                name=self._fresh("cpy.str"),
            )
            self._guard_cpy_value_not_null(
                pcc_str,
                (v,) if v_owned else (),
            )
            if v_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [v])
                self._forget_owned_cpy_value(v)
            v_obj = pcc_str
        else:
            boxed_valueclass = self._emit_valueclass_payload_to_object(v, arg.ty)
            if boxed_valueclass is not None:
                v_obj = boxed_valueclass
            else:
                v_obj = marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, v, arg.ty
                )
        current_tup = self._load_print_many_tuple(tup, frame_slot)
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [current_tup, idx, v_obj],
        )

    def _emit_print_call(self, call: Call) -> None:
        # print(*items) / print(a, *rest): a positional *splat. The per-arg
        # paths below would emit the *m marker (Call(Name("*"),...)) as a
        # Name("*") lookup -> runtime "NameError: name '*'". Expand the args
        # into a runtime tuple first. sep/end kwargs and a bool-literal flush=
        # are handled natively here; other kwargs (file=, non-literal flush=)
        # fall through to the cpython path.
        if self._has_starred_unpack(call.args) and self._print_kwargs_are_native(
            call.kwargs or ()
        ):
            self._emit_print_many_splat(call)
            return
        if call.kwargs:
            if self._print_kwargs_are_native(call.kwargs):
                self._emit_print_many(call)
                return
            if self._try_emit_native_file_stream_print(call):
                return
            if self._print_kwargs_route_to_file(call.kwargs):
                # A file object no static type describes (``print(msg,
                # file=out)``): write through its ``write`` method natively.
                if self._has_starred_unpack(call.args):
                    self._emit_print_many_splat(call)
                else:
                    self._emit_print_many(call)
                return
            fn_val = self._load_cpython_builtin("print")
            result = self._finish_cpy_call_kw(
                fn_val,
                "print",
                call.args,
                call.kwargs,
                call.operand_order,
            )
            self._guard_cpy_value_not_null(result)
            self.builder.call(self.runtime["py_cpy_decref"], [result])
            self._forget_owned_cpy_value(result)
            return

        if len(call.args) == 0:
            null_obj = ir.Constant(_CSTR, None)
            self.builder.call(
                self.runtime["py_print_many"],
                [null_obj, null_obj, null_obj],
            )
            return

        if len(call.args) > 1:
            self._emit_print_many(call)
            return

        arg = call.args[0]
        arg_ty = arg.ty

        if isinstance(arg_ty, IntType):
            exact_obj = self._maybe_emit_exact_int_object(arg)
            if exact_obj is not None:
                self.builder.call(self.runtime["py_print"], [exact_obj])
                return

        value = self._emit_expr(arg)

        if value in self._cpy_values:
            value_owned = self._cpy_value_is_owned(value)
            pcc_str = self.builder.call(
                self.runtime["py_cpy_to_pcc_str"],
                [value],
                name=self._fresh("cpy.str"),
            )
            self._guard_cpy_value_not_null(
                pcc_str,
                (value,) if value_owned else (),
            )
            self.builder.call(self.runtime["py_print"], [pcc_str])
            if value_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [value])
                self._forget_owned_cpy_value(value)
            return

        if isinstance(arg_ty, IntType):
            if isinstance(value.type, ir.PointerType):
                self.builder.call(self.runtime["py_print"], [value])
                return
            obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, value, arg_ty
            )
            self.builder.call(self.runtime["py_print"], [obj])
            return
        if isinstance(arg_ty, FloatType):
            self._emit_print_float_value(value)
            return
        if isinstance(arg_ty, BoolType):
            obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, value, arg_ty
            )
            self.builder.call(self.runtime["py_print"], [obj])
            return

        boxed_valueclass = self._emit_valueclass_payload_to_object(value, arg_ty)
        if boxed_valueclass is not None:
            obj = boxed_valueclass
        else:
            obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, value, arg_ty
            )
        self.builder.call(self.runtime["py_print"], [obj])
        # py_print borrows (py_print_fmt.c: format + newline, no refcount
        # traffic), so an argument that produced a fresh reference is still
        # owned here.  `print(a[0])` leaked one reference per call for exactly
        # this reason.  A borrowed argument -- a rooted local, an immortal
        # singleton -- must not be released, which is what the classifier
        # decides.
        self._gc_release_if_owned(obj, arg)

    def _emit_print_float_value(self, value: ir.Value) -> None:
        # Box the raw double and route through the runtime py_print, which
        # formats floats via py_float_repr_shortest (CPython shortest-round-trip
        # repr) and flushes/newlines consistently. The old inline-printf path
        # used "%g" (6 significant figures) for non-integral values, so e.g.
        # print(10/3) emitted "3.33333" instead of "3.3333333333333335".
        boxed = self.builder.call(
            self.runtime["py_float_from_f64"],
            [value],
            name=self._fresh("print.float.box"),
        )
        self.builder.call(self.runtime["py_print"], [boxed])

    def _emit_print_many(self, call: Call) -> None:
        n = len(call.args)
        n_val = ir.Constant(_I64, n)
        tup = self.builder.call(
            self.runtime["py_tuple_new"], [n_val], name=self._fresh("pr.args")
        )
        self._emit_post_call_err_check(getattr(call, "span", None))
        tup_root = None
        tup_frame_slot = None
        tup_reload_slot = None
        if len(getattr(self, "_generator_ctx_stack", ())) > 0:
            ctx = self._generator_ctx_stack[-1]
            hidden = self._generator_print_args_name(call)
            frame_entry = ctx["frame_slots"].get(hidden)
            if frame_entry is None:
                raise RuntimeError("generator print missing persisted args slot")
            tup_frame_slot = frame_entry[1]
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [
                    self._as_gc_ptr(
                        tup_frame_slot,
                        name=self._fresh("pr.args.frame.root"),
                    ),
                    tup,
                ],
            )
            # The traced frame slot now owns the tuple.  Consume py_tuple_new's
            # direct owner so a park/resume transfers exactly one local owner.
            self._gc_release(tup)
            tup_reload_slot = tup_frame_slot
        else:
            # The tuple remains owned by this lowering until py_print_many
            # returns.  Use the ordinary temporary-container protocol so it
            # is pinned/rooted across later operand calls and so both pcc and
            # CPython exceptional edges can run the exact same cleanup.
            tup_root = self._enter_container_temp_root(
                tup,
                "pr.args",
            )
            tup_reload_slot = tup_root

        previous_cpy_cleanup = None
        previous_pcc_target = None
        if tup_frame_slot is None:
            previous_cpy_cleanup = getattr(
                self,
                "_cpy_operand_cleanup_block",
                None,
            )
            previous_pcc_target = self._current_try_err_block()
            pcc_target = previous_pcc_target
            if pcc_target is None:
                pcc_target = self._ensure_fn_err_exit()
            rooted_tuple = ((tup, tup_root),)
            pcc_cleanup = self._make_cpy_operand_cleanup_block(
                (),
                rooted_tuple,
                pcc_target,
                "print.args.pcc.cleanup",
            )
            cpy_target = previous_cpy_cleanup
            if cpy_target is None:
                cpy_target = pcc_target
            if cpy_target is pcc_target:
                cpy_cleanup = pcc_cleanup
            else:
                cpy_cleanup = self._make_cpy_operand_cleanup_block(
                    (),
                    rooted_tuple,
                    cpy_target,
                    "print.args.cpy.cleanup",
                )
            self._try_err_block = pcc_cleanup
            self._cpy_operand_cleanup_block = cpy_cleanup

        try:
            for i, arg in enumerate(call.args):
                idx = ir.Constant(_I64, i)
                self._emit_print_many_arg(tup, idx, arg, tup_reload_slot)

            sep_obj: Optional[ir.Value] = None
            end_obj: Optional[ir.Value] = None
            file_obj: Optional[ir.Value] = None
            flush_obj: Optional[ir.Value] = None
            for k, vexpr in self._print_kwargs_without_flush(call.kwargs):
                v = self._emit_expr(vexpr)
                boxed = marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, v, vexpr.ty
                )
                if k == "sep":
                    sep_obj = boxed
                elif k == "end":
                    end_obj = boxed
                elif k == "file":
                    file_obj = boxed
                elif k == "flush":
                    flush_obj = boxed
        finally:
            if tup_frame_slot is None:
                self._try_err_block = previous_pcc_target
                self._cpy_operand_cleanup_block = previous_cpy_cleanup

        current_tup = self._load_print_many_tuple(tup, tup_reload_slot)
        self._emit_print_many_or_file_call(
            current_tup, sep_obj, end_obj, file_obj, flush_obj
        )
        if tup_frame_slot is None:
            self._emit_post_call_err_check(
                getattr(call, "span", None),
                rooted_release_on_error=((tup, tup_root),),
            )
            self._leave_container_temp_root(tup_root)
            self._gc_release(tup)
        else:
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [tup_frame_slot, self._emit_none_literal()],
            )

    def _emit_print_many_or_file_call(
        self, args_tuple, sep_obj, end_obj, file_obj, flush_obj
    ) -> None:
        """``py_print_many`` for stdout, ``py_print_to_file`` for ``file=``.

        The caller's post-call error check covers both (``py_print_to_file``
        leaves the write's exception pending).
        """
        if file_obj is None:
            if sep_obj is None:
                sep_obj = self._emit_literal_str(" ")
            if end_obj is None:
                end_obj = self._emit_literal_str("\n")
            self.builder.call(
                self.runtime["py_print_many"],
                [args_tuple, sep_obj, end_obj],
            )
            return
        null = ir.Constant(_CSTR, None)
        self.builder.call(
            self.runtime["py_print_to_file"],
            [
                file_obj,
                args_tuple,
                sep_obj if sep_obj is not None else null,
                end_obj if end_obj is not None else null,
                flush_obj if flush_obj is not None else null,
            ],
            name=self._fresh("print.file"),
        )

    def _emit_print_many_splat(self, call: Call) -> None:
        """``print(*items)`` / ``print(a, *rest, b)``: build the positional
        args as a runtime list (``_emit_pcc_args_list`` expands every splat and
        appends the plain args), convert it to a tuple via
        ``py_call_merge_posargs`` (an empty base + the list as *args), then hand
        that tuple to ``py_print_many`` like the fixed-arity path. Only sep/end
        kwargs and a bool-literal flush= reach here (gated by the caller); the
        flush= no-op is dropped before the sep/end loop."""
        list_roots = []
        lst = self._emit_pcc_args_list(
            call.args,
            "print.splat",
            cpy_live_owned=[],
            cpy_temp_root_out=list_roots,
        )
        lst_root = list_roots[0]
        tup = self.builder.call(
            self.runtime["py_call_merge_posargs"],
            [ir.Constant(_CSTR, None), lst],
            name=self._fresh("pr.splat.args"),
        )
        self._emit_post_call_err_check(
            getattr(call, "span", None),
            rooted_release_on_error=((lst, lst_root),),
        )
        self._leave_container_temp_root(lst_root)
        self._gc_release(lst)

        tup_root = self._enter_container_temp_root(tup, "pr.splat")
        previous_cpy_cleanup = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        previous_pcc_target = self._current_try_err_block()
        pcc_target = previous_pcc_target
        if pcc_target is None:
            pcc_target = self._ensure_fn_err_exit()
        rooted_tuple = ((tup, tup_root),)
        pcc_cleanup = self._make_cpy_operand_cleanup_block(
            (),
            rooted_tuple,
            pcc_target,
            "print.splat.pcc.cleanup",
        )
        cpy_target = previous_cpy_cleanup
        if cpy_target is None:
            cpy_target = pcc_target
        if cpy_target is pcc_target:
            cpy_cleanup = pcc_cleanup
        else:
            cpy_cleanup = self._make_cpy_operand_cleanup_block(
                (),
                rooted_tuple,
                cpy_target,
                "print.splat.cpy.cleanup",
            )
        self._try_err_block = pcc_cleanup
        self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            sep_obj: Optional[ir.Value] = None
            end_obj: Optional[ir.Value] = None
            file_obj: Optional[ir.Value] = None
            flush_obj: Optional[ir.Value] = None
            for k, vexpr in self._print_kwargs_without_flush(call.kwargs):
                v = self._emit_expr(vexpr)
                boxed = marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, v, vexpr.ty
                )
                if k == "sep":
                    sep_obj = boxed
                elif k == "end":
                    end_obj = boxed
                elif k == "file":
                    file_obj = boxed
                elif k == "flush":
                    flush_obj = boxed
        finally:
            self._try_err_block = previous_pcc_target
            self._cpy_operand_cleanup_block = previous_cpy_cleanup

        current_tup = self._load_print_many_tuple(tup, tup_root)
        self._emit_print_many_or_file_call(
            current_tup, sep_obj, end_obj, file_obj, flush_obj
        )
        self._emit_post_call_err_check(
            getattr(call, "span", None),
            rooted_release_on_error=((tup, tup_root),),
        )
        self._leave_container_temp_root(tup_root)
        self._gc_release(tup)
