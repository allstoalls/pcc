"""List, dict, and tuple literal lowering for L1CodeGen."""
from __future__ import annotations

import sys
import os

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolType,
    BytesType,
    Call,
    DictExpr,
    DictType,
    DynType,
    Expr,
    FloatType,
    FuncType,
    IntType,
    ListExpr,
    ListType,
    Name,
    NoneType,
    StrLit,
    StrType,
    TupleExpr,
    TupleType,
    Type,
)
from . import marshal
from .freestanding_abi_constants import (
    PY_TYPE_DICT,
    PY_TYPE_LIST,
    PY_TYPE_TUPLE,
)
from .runtime_abi import declare_runtime_global


_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class LiteralLoweringMixin:
    def _static_literal_init_function(self) -> ir.Function:
        """``void _pcc_py_static_literals_<mod>()``: registers every pooled
        static ``str`` object with the GC provenance index.

        Static literals live in the data segment, so the allocator's granule
        check cannot vouch for them and every ``py_incref`` / ``py_decref`` /
        pin of a literal (``if op == 'add'``, ``d['key']``, ``x == 'ret'``)
        fell through the whole locked provenance chain -- including the
        linear builtin-type scan -- before answering "not managed" and
        skipping the refcount.  That chain was the top self-time leaf of the
        ``str_eq_dispatch`` per-op row (12% ``_is_type_object``).  Registering
        the literals once at module start turns each of those queries into
        one hash probe.  The body is filled in by
        ``_finalize_static_literal_init`` once the pool is complete.
        """
        fn = self._static_literal_init_fn
        if fn is not None:
            return fn
        mod_name = self.ast_module.name or "mod"
        sanitised = mod_name.replace(".", "_").replace("-", "_")
        fn = ir.Function(
            self.module,
            ir.FunctionType(ir.VoidType(), []),
            name=f"_pcc_py_static_literals_{sanitised}",
        )
        fn.linkage = "internal"
        self._static_literal_init_fn = fn
        return fn

    def _emit_static_literal_init_call(self) -> None:
        if self._freestanding_module:
            return
        self.builder.call(self._static_literal_init_function(), [])

    def _finalize_static_literal_init(self) -> None:
        fn = self._static_literal_init_fn
        if fn is None:
            return
        mod_name = self.ast_module.name or "mod"
        sanitised = mod_name.replace(".", "_").replace("-", "_")
        guard = ir.GlobalVariable(
            self.module, _I32, name=f".pcc.static.literals.init.{sanitised}"
        )
        guard.linkage = "internal"
        guard.initializer = ir.Constant(_I32, 0)
        entry = fn.append_basic_block("entry")
        body_bb = fn.append_basic_block("body")
        done_bb = fn.append_basic_block("done")
        builder = ir.IRBuilder(entry)
        seen = builder.load(guard, name="static.literals.seen")
        builder.cbranch(
            builder.icmp_signed("!=", seen, ir.Constant(_I32, 0), name="static.literals.done"),
            done_bb,
            body_bb,
        )
        builder.position_at_end(body_bb)
        builder.store(ir.Constant(_I32, 1), guard)
        register = self.runtime["pcc_gc_pointer_register"]
        for gv in self._str_obj_pool.values():
            builder.call(register, [builder.bitcast(gv, _CSTR)])
        # Static float literals are the same kind of object as static string
        # literals -- immortal, in the data segment, pointed at rather than
        # allocated -- so they take the same one-shot registration. The pool
        # lives on the module because `marshal.marshal_to_object` fills it and
        # is a free function without access to this host.
        for gv in marshal.float_literal_pool(self.module).values():
            builder.call(register, [builder.bitcast(gv, _CSTR)])
        builder.branch(done_bb)
        builder.position_at_end(done_bb)
        builder.ret_void()

    def _emit_str_literal(self, value: str) -> ir.Value:
        existing = self._str_obj_pool.get(value)
        if existing is None:
            data = self._utf8_byte_values(value)
            body = data + [0]
            data_ty = ir.ArrayType(_I8, len(body))
            obj_ty = ir.LiteralStructType(
                [
                    _I64,
                    _I32,
                    _I32,
                    _I64,
                    _I64,
                    _I64,
                    data_ty,
                ]
            )
            self._str_counter += 1
            gv = ir.GlobalVariable(
                self.module,
                obj_ty,
                name=f".pystr.obj.{self._str_counter}",
            )
            gv.linkage = "internal"
            gv.global_constant = False
            gv.initializer = ir.Constant(
                obj_ty,
                [
                    ir.Constant(_I64, 1),
                    ir.Constant(_I32, 4),
                    ir.Constant(_I32, 1),
                    ir.Constant(_I64, len(data)),
                    ir.Constant(_I64, -1),
                    ir.Constant(_I64, -1),
                    ir.Constant(data_ty, body),
                ],
            )
            self._str_obj_pool[value] = gv
            existing = gv
        return existing

    def _emit_bytes_literal(self, value: bytes) -> ir.Value:
        self._str_counter += 1
        body = [int(b) & 0xFF for b in value] + [0]
        arr_ty = ir.ArrayType(_I8, len(body))
        gv = ir.GlobalVariable(
            self.module, arr_ty, name=f".pybytes.{self._str_counter}"
        )
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(arr_ty, body)
        zero = ir.Constant(_I32, 0)
        ptr = self.builder.gep(
            gv, [zero, zero], inbounds=True, name=self._fresh("pybytes.ptr")
        )
        return self.builder.call(
            self.runtime["py_bytes_new"],
            [ptr, ir.Constant(_I64, len(value))],
            name=self._fresh("bytes.new"),
        )

    def _emit_none_literal(self) -> ir.Value:
        gv = declare_runtime_global(self.module, "py_None")
        return self.builder.load(gv, name=self._fresh("none"))

    def _emit_int_literal_object(self, value) -> ir.Value:
        # `value` is deliberately UNANNOTATED.  An `int`-annotated parameter is
        # an i64-backed exact-int slot in pcc, so annotating it truncated any
        # literal above 2**63-1 to 0 right at this call boundary -- the last hop
        # of the parser chain, after `int(e.text, 0)`, the IntLit field store and
        # the `int(expr.value)` re-wrap had all been fixed.  Everything this body
        # does to `value` (comparisons, shift, or, str) is valid on a bignum.
        if -((0x40000000 << 32) | 0x0) <= value <= 4611686018427387903:
            # Tagged small int, computed here instead of at run time.
            # ``py_int_from_i64`` would take this exact branch and return
            # ``(v << 1) | 1`` without allocating, so the call bought nothing
            # — and int literals are everywhere in generated code.  The
            # result is also recorded as provably-not-a-GC-object so the
            # ownership lowering can skip its GC barriers entirely (pin /
            # unpin / release each begin by testing ``is_tagged_int`` and
            # returning, i.e. they are no-ops for this value).
            # No 64-bit mask here.  `value` is in the tagged lane, so
            # `(value << 1) | 1` lands in [-2**63+1, 2**63-1] -- already a
            # valid signed i64, and for negatives its bit pattern is identical
            # to the masked form.  The mask was not merely redundant, it was
            # fatal: a *literal* integer constant above the tagged lane
            # evaluates to 0 in pcc-compiled code (computed values such as
            # `(1 << 64) - 1` are fine), so under pcc2 the mask became `& 0`
            # and every int literal lowered to a NULL pointer -- pcc2 could not
            # print any integer.  See M5-SELFHOST-BIG-INT-LITERAL.
            tagged = (value << 1) | 1
            boxed = self.builder.inttoptr(
                ir.Constant(_I64, tagged),
                _CSTR,
                name=self._fresh("int.lit.tagged"),
            )
            self._note_never_gc_object(boxed)
            return boxed
        if -(1 << 63) <= value <= (1 << 63) - 1:
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [ir.Constant(_I64, value)],
                name=self._fresh("print.int.lit"),
            )
        gv, _ = self._cstr_literal(str(value))
        return self.builder.call(
            self.runtime["py_int_from_cstr"],
            [self._ptr_to_cstr(gv), ir.Constant(_I32, 10)],
            name=self._fresh("print.int.lit.big"),
        )

    def _emit_literal_str(self, s: str) -> ir.Value:
        return self._emit_str_literal(s)

    def _cpy_literal_cleanup_values(
        self,
        container: ir.Value,
        first_callable,
        second_callable,
        pending_owned,
        extra_owned=(),
    ) -> tuple[ir.Value, ...]:
        """Collect unique live CPython refs for one literal error edge."""
        values = [container]
        for candidate in (first_callable, second_callable):
            if candidate is not None:
                values.append(candidate)
        for candidate in pending_owned:
            if not any(existing is candidate for existing in values):
                values.append(candidate)
        for candidate in extra_owned:
            if not any(existing is candidate for existing in values):
                values.append(candidate)
        return tuple(values)

    def _emit_expr_with_native_callable_values(self, expr: Expr) -> ir.Value:
        old = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        value = self._emit_expr(expr)
        self._prefer_native_callable_values = old
        return value

    def _enter_container_temp_root(
        self,
        value: ir.Value,
        label: str,
    ) -> ir.Value:
        """Root a container while its literal elements are still being built.

        List/dict/tuple literals create the container first and then populate
        it. During that construction window the container is an SSA temporary,
        not yet stored into the assignment target's GC frame slot. Tracing
        backends can run from another real thread, so protect the temporary
        through the existing frame-root ABI until the literal is complete.
        """
        if not isinstance(value.type, ir.PointerType):
            return ir.Constant(_CSTR, None)
        slot = self._alloca_in_entry(
            _CSTR,
            name=label + ".tmp.root",
            init_null=True,
        )
        rooted_value = value
        if rooted_value.type != _CSTR:
            rooted_value = self.builder.bitcast(
                value,
                _CSTR,
                name=self._fresh(label + ".tmp.ptr"),
            )
        persistent_thread_root = (
            self.current_func_def is not None
            and getattr(self, "_runtime_threads_enabled", False)
        )
        if persistent_thread_root:
            old_rooted_value = self.builder.load(
                slot,
                name=self._fresh(label + ".tmp.old"),
            )
            self.builder.call(
                self.runtime["pcc_gc_unpin"],
                [old_rooted_value],
            )
        self._gc_pin(rooted_value)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [
                self._as_gc_ptr(slot, name=self._fresh(label + ".tmp.root.ptr")),
                rooted_value,
            ],
        )
        if persistent_thread_root:
            root_name = self._fresh(label + ".tmp.root.local")
            self.env[root_name] = (slot, _CSTR, DynType(name="dyn"))
            self._pinned_gc_rooted_local_names.add(root_name)
            # Route through the per-slot registry (not a raw entry enter) so
            # every function exit emits the balancing frame_leave for this
            # slot; cleanup no longer leaves roots by name.
            self._ensure_local_gc_frame_root(
                root_name,
                slot,
                _CSTR,
                self._gc_one_slot_frame_map(),
            )
        else:
            self._emit_current_gc_frame_enter_lifo(
                self._gc_one_slot_frame_map(),
                slot,
            )
        return slot

    def _leave_container_temp_root(self, slot: ir.Value) -> None:
        if isinstance(slot, ir.Constant):
            return
        slot_ptr = self._as_gc_ptr(
            slot,
            name=self._fresh("container.tmp.root.ptr"),
        )
        pinned = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), slot_ptr],
            name=self._fresh("container.tmp.unpin"),
        )
        if (
            self.current_func_def is not None
            and getattr(self, "_runtime_threads_enabled", False)
        ):
            # Thread-enabled functions register this entry-block slot for the
            # whole function so the epilogue remains responsible for the
            # balancing frame_leave.  The temporary value itself is no longer
            # live, though: clear and unpin it before its owner releases the
            # container, otherwise the persistent slot becomes a dangling GC
            # root until function exit.
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [
                    slot_ptr,
                    ir.Constant(_CSTR, None),
                ],
            )
            self._gc_unpin(pinned)
            return
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [
                slot_ptr,
                ir.Constant(_CSTR, None),
            ],
        )
        self._emit_gc_frame_leave_lifo_for_slot(slot)
        self._gc_unpin(pinned)

    def _emit_native_splat_sequence_at_source_position(
        self,
        elems: tuple[Expr, ...],
        *,
        tuple_result: bool,
        prefer_native_callables: bool = False,
    ) -> ir.Value:
        """Build one native list/tuple while visiting operands in source order.

        A starred iterable is expanded before the next operand is evaluated.
        This is observable when iteration raises or mutates state, so the
        general pre-materialized ``ops`` path cannot represent a non-terminal
        splat honestly.  The caller admits only statically pcc-native operands;
        mixed CPython-container layout remains on its explicit boundary.
        """
        lst = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("literal.splat.list"),
        )
        self._emit_post_call_err_check()
        lst_root = self._enter_container_temp_root(lst, "literal.splat.list")

        for elem in elems:
            is_splat = (
                isinstance(elem, Call)
                and isinstance(elem.func, Name)
                and elem.func.ident in ("*", "__starred__")
                and len(elem.args) == 1
            )
            source_expr = elem.args[0] if is_splat else elem
            previous_preference = self._prefer_native_callable_values
            if prefer_native_callables:
                self._prefer_native_callable_values = True
            try:
                value_obj = self._emit_expr_with_cpy_operand_cleanup(
                    source_expr,
                    (),
                    ((lst, lst_root),),
                    (),
                    True,
                )
            finally:
                self._prefer_native_callable_values = previous_preference

            value_owned = self._container_store_temp_needs_release(
                source_expr,
                source_expr.ty,
                False,
                value_obj,
            )
            value_pinned = (
                isinstance(value_obj.type, ir.PointerType)
                and self._pcc_pointer_source_needs_pin(source_expr)
            )
            if value_pinned:
                self._gc_pin(value_obj)

            runtime_name = "py_list_extend" if is_splat else "py_list_append"
            self.builder.call(self.runtime[runtime_name], [lst, value_obj])
            self._emit_post_call_err_check(
                getattr(source_expr, "span", None),
                release_on_error=(
                    (value_obj,) if value_owned and not value_pinned else ()
                ),
                rooted_release_on_error=((lst, lst_root),),
                pinned_release_on_error=(
                    ((value_obj, value_owned),) if value_pinned else ()
                ),
            )
            if value_pinned:
                self._gc_unpin(value_obj)
            if value_owned:
                self._gc_release(
                    value_obj,
                    self._release_expr_label("container", source_expr),
                )

        if not tuple_result:
            self._leave_container_temp_root(lst_root)
            return lst

        tup = self.builder.call(
            self.runtime["py_tuple_from_list"],
            [lst],
            name=self._fresh("literal.splat.tuple"),
        )
        self._emit_post_call_err_check(
            rooted_release_on_error=((lst, lst_root),),
        )
        tup_root = self._enter_container_temp_root(tup, "literal.splat.tuple")
        self._leave_container_temp_root(lst_root)
        self._gc_release(lst)
        self._leave_container_temp_root(tup_root)
        return tup

    def _emit_list_literal(self, expr: ListExpr) -> ir.Value:
        has_splat = any(
            isinstance(el, Call)
            and isinstance(el.func, Name)
            and el.func.ident in ("*", "__starred__")
            and len(el.args) == 1
            for el in expr.elems
        )
        # ListType/FuncType are canonical across compiled-module boundaries;
        # callable materialization is therefore driven by the inferred type,
        # not by a syntax-only list-of-function-names exception.
        prefer_native_callables = isinstance(expr.ty, ListType) and isinstance(
            expr.ty.elem,
            FuncType,
        )
        has_nonterminal_splat = any(
            isinstance(el, Call)
            and isinstance(el.func, Name)
            and el.func.ident in ("*", "__starred__")
            and len(el.args) == 1
            and index + 1 < len(expr.elems)
            for index, el in enumerate(expr.elems)
        )
        if has_nonterminal_splat and not any(
            self._expr_looks_cpython(
                el.args[0]
                if (
                    isinstance(el, Call)
                    and isinstance(el.func, Name)
                    and el.func.ident in ("*", "__starred__")
                    and len(el.args) == 1
                )
                else el
            )
            for el in expr.elems
        ):
            return self._emit_native_splat_sequence_at_source_position(
                expr.elems,
                tuple_result=False,
                prefer_native_callables=(
                    isinstance(expr.ty, ListType)
                    and isinstance(expr.ty.elem, FuncType)
                ),
            )
        if not has_splat and not any(
            self._expr_looks_cpython(el)
            or isinstance(el.ty, DynType)
            or self._is_valueclass_payload_type(el.ty)
            for el in expr.elems
        ):
            n_val = ir.Constant(_I64, len(expr.elems))
            lst = self.builder.call(
                self.runtime["py_list_new"],
                [n_val],
                name=self._fresh("list.new"),
            )
            self._emit_post_call_err_check()
            # The root protects the container while its elements are built;
            # an empty literal builds nothing.
            lst_root = (
                self._enter_container_temp_root(lst, "list")
                if expr.elems
                else ir.Constant(_CSTR, None)
            )
            for el in expr.elems:
                previous_preference = self._prefer_native_callable_values
                if prefer_native_callables:
                    self._prefer_native_callable_values = True
                try:
                    v_obj = self._emit_expr_with_cpy_operand_cleanup(
                        el,
                        (),
                        ((lst, lst_root),),
                        (),
                        True,
                    )
                finally:
                    self._prefer_native_callable_values = previous_preference
                release_temp = self._container_store_temp_needs_release(
                    el,
                    el.ty,
                    False,
                    v_obj,
                )
                temp_pinned = (
                    isinstance(v_obj.type, ir.PointerType)
                    and self._pcc_pointer_source_needs_pin(el)
                )
                if temp_pinned:
                    self._gc_pin(v_obj)
                self.builder.call(
                    self.runtime["py_list_append"],
                    [lst, v_obj],
                )
                self._emit_post_call_err_check(
                    getattr(el, "span", None),
                    release_on_error=(v_obj,) if release_temp and not temp_pinned else (),
                    rooted_release_on_error=((lst, lst_root),),
                    pinned_release_on_error=(
                        ((v_obj, release_temp),) if temp_pinned else ()
                    ),
                )
                if temp_pinned:
                    self._gc_unpin(v_obj)
                if release_temp:
                    self._gc_release(
                        v_obj,
                        self._release_expr_label("container", el),
                    )
            self._leave_container_temp_root(lst_root)
            return lst

        ops: list[tuple[str, ir.Value, Type, bool, Expr, bool]] = []
        cpy_values = self._cpy_values
        cpy_extend = False
        live_cpy_owned: list[ir.Value] = []
        live_pinned_pcc: list[tuple[ir.Value, bool]] = []
        for el_index, el in enumerate(expr.elems):
            if (
                isinstance(el, Call)
                and isinstance(el.func, Name)
                and el.func.ident in ("*", "__starred__")
                and len(el.args) == 1
            ):
                if el_index + 1 < len(expr.elems):
                    raise NotImplementedError(
                        "iterable splat cannot precede following literal "
                        "operands until expansion is lowered at its source "
                        "position"
                    )
                inner = self._emit_expr_with_cpy_operand_cleanup(
                    el.args[0],
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
                is_cpy = inner in cpy_values
                if is_cpy:
                    self._guard_cpy_value_not_null(
                        inner,
                        tuple(live_cpy_owned),
                        (),
                        tuple(live_pinned_pcc),
                    )
                    if self._cpy_value_is_owned(inner):
                        live_cpy_owned.append(inner)
                pinned = False
                if (
                    isinstance(inner.type, ir.PointerType)
                    and not is_cpy
                    and self._pcc_pointer_source_needs_pin(el.args[0])
                ):
                    self._gc_pin(inner)
                    pinned = True
                    live_pinned_pcc.append(
                        (
                            inner,
                            self._owned_release_needed(inner, el.args[0]) or self._pcc_pointer_source_is_owned(el.args[0]),
                        )
                    )
                ops.append(
                    ("extend", inner, el.args[0].ty, is_cpy, el.args[0], pinned)
                )
                if is_cpy:
                    cpy_extend = True
                continue
            if prefer_native_callables:
                if live_cpy_owned or live_pinned_pcc:
                    previous_preference = self._prefer_native_callable_values
                    self._prefer_native_callable_values = True
                    try:
                        v = self._emit_expr_with_cpy_operand_cleanup(
                            el,
                            tuple(live_cpy_owned),
                            (),
                            tuple(live_pinned_pcc),
                        )
                    finally:
                        self._prefer_native_callable_values = previous_preference
                else:
                    v = self._emit_expr_with_native_callable_values(el)
            else:
                valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
                    el.ty,
                    el,
                )
                if valueclass_payload is not None:
                    v = valueclass_payload
                else:
                    v = self._emit_expr_with_cpy_operand_cleanup(
                        el,
                        tuple(live_cpy_owned),
                        (),
                        tuple(live_pinned_pcc),
                    )
            is_cpy = v in cpy_values
            if is_cpy:
                self._guard_cpy_value_not_null(
                    v,
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
                if self._cpy_value_is_owned(v):
                    live_cpy_owned.append(v)
            pinned = False
            if (
                isinstance(v.type, ir.PointerType)
                and not is_cpy
                and self._pcc_pointer_source_needs_pin(el)
            ):
                self._gc_pin(v)
                pinned = True
                live_pinned_pcc.append(
                    (
                        v,
                        self._owned_release_needed(v, el) or self._pcc_pointer_source_is_owned(el),
                    )
                )
            ops.append(("append", v, el.ty, is_cpy, el, pinned))
        # Build a real CPython list when the literal SPREADS a cpy iterable
        # (``cpy_extend``) OR when EVERY element is a cpy value (e.g. ``[a, a]``
        # of numpy arrays passed to ``np.concatenate([...])``): bridging those
        # cpy elements to pcc objects would round-trip a real CPython object
        # cpy->pcc->cpy and lose it, so ``_emit_cpython_list_ops`` keeps them
        # borrowed. A MIXED literal — at least one native element, e.g.
        # ``[p, x, p]`` with a native ``str`` ``p`` — is a native pcc list: the
        # path below builds ``py_list_new``/``py_list_append`` and bridges only
        # the cpy elements via ``py_cpy_to_pcc_obj``. (A native element cannot be
        # kept cpy without a native->cpy round-trip anyway, so its presence
        # signals native-list intent.) Inert for all-native lists / no cpy
        # values => bootstrap unaffected.
        cpy_all_elems = bool(ops) and all(op[3] for op in ops)
        cpy_any = cpy_extend or cpy_all_elems
        if cpy_any:
            result = self._emit_cpython_list_ops(
                [(k, v, t) for (k, v, t, _, _, _) in ops],
                tuple(live_pinned_pcc),
            )
            for _op_kind, value, _value_ty, _is_cpy, src_expr, pinned in ops:
                if pinned:
                    self._gc_unpin(value)
                    self._gc_release_if_owned(value, src_expr)
            return result
        n_val = ir.Constant(_I64, len(expr.elems))
        lst = self.builder.call(
            self.runtime["py_list_new"],
            [n_val],
            name=self._fresh("list.new"),
        )
        pending_owned = list(live_cpy_owned)
        pending_pins = list(live_pinned_pcc)
        self._emit_post_call_err_check(
            cpy_release_on_error=tuple(pending_owned),
            pinned_release_on_error=tuple(pending_pins),
        )
        lst_root = self._enter_container_temp_root(lst, "list")
        for op_kind, value, value_ty, is_cpy, src_expr, pinned in ops:
            rooted_on_error = ((lst, lst_root),)
            pin_entry = None
            if pinned:
                for candidate in pending_pins:
                    if candidate[0] is value:
                        pin_entry = candidate
                        break
            if op_kind == "extend":
                self.builder.call(
                    self.runtime["py_list_extend"],
                    [lst, value],
                )
                self._emit_post_call_err_check(
                    getattr(src_expr, "span", None),
                    cpy_release_on_error=tuple(pending_owned),
                    rooted_release_on_error=rooted_on_error,
                    pinned_release_on_error=tuple(pending_pins),
                )
                if pinned:
                    self._gc_unpin(value)
                    if pin_entry is not None and pin_entry[1]:
                        self._gc_release(value)
                    pending_pins.remove(pin_entry)
                continue
            value_was_owned = is_cpy and self._cpy_value_is_owned(value)
            v_obj = self._emit_value_as_pcc_object_or_bridge(
                value,
                value_ty,
                "cpy.list.elem" if is_cpy else "list.elem",
                consume_valueclass_payload_fields=
                    self._valueclass_payload_expr_fields_are_owned(src_expr),
                cpy_owned_on_error=tuple(pending_owned),
                rooted_pcc_on_error=rooted_on_error,
                pinned_pcc_on_error=tuple(pending_pins),
            )
            if value_was_owned:
                pending_owned = [
                    owned for owned in pending_owned if owned is not value
                ]
            temp_needs_release = self._container_store_temp_needs_release(
                src_expr,
                value_ty,
                is_cpy,
                v_obj,
            )
            self.builder.call(
                self.runtime["py_list_append"],
                [lst, v_obj],
            )
            release_on_error = ()
            if temp_needs_release and not (
                pin_entry is not None
                and pin_entry[1]
                and v_obj is value
            ):
                release_on_error = (v_obj,)
            self._emit_post_call_err_check(
                getattr(src_expr, "span", None),
                release_on_error=release_on_error,
                cpy_release_on_error=tuple(pending_owned),
                rooted_release_on_error=rooted_on_error,
                pinned_release_on_error=tuple(pending_pins),
            )
            if pinned:
                self._gc_unpin(value)
            if temp_needs_release:
                self._gc_release(
                    v_obj,
                    self._release_expr_label("container", src_expr),
                )
            elif pin_entry is not None and pin_entry[1]:
                self._gc_release(value)
            if pin_entry is not None:
                pending_pins.remove(pin_entry)
        self._leave_container_temp_root(lst_root)
        return lst

    def _emit_dict_literal_with_splat(self, expr: DictExpr) -> ir.Value:
        # ``{**m, "k": v, **n}`` — the lift encodes each ``**mapping`` as a pair
        # whose key is the sentinel Name("**"). Build a fresh dict, merging a
        # splat via py_dict_update and setting an ordinary pair via py_dict_set,
        # in source order (CPython semantics: later entries win).
        d = self.builder.call(
            self.runtime["py_dict_new"], [], name=self._fresh("dict.new")
        )
        self._emit_post_call_err_check()
        dict_root = self._enter_container_temp_root(d, "dict")
        for k_expr, v_expr in expr.pairs:
            if isinstance(k_expr, Name) and k_expr.ident == "**":
                m_obj = self._emit_expr_with_cpy_operand_cleanup(
                    v_expr,
                    (),
                    ((d, dict_root),),
                    (),
                    True,
                )
                mapping_owned = self._container_store_temp_needs_release(
                    v_expr,
                    v_expr.ty,
                    False,
                    m_obj,
                )
                mapping_pinned = (
                    isinstance(m_obj.type, ir.PointerType)
                    and self._pcc_pointer_source_needs_pin(v_expr)
                )
                mapping_pin_cleanup = ()
                if mapping_pinned:
                    self._gc_pin(m_obj)
                    mapping_pin_cleanup = ((m_obj, mapping_owned),)
                self.builder.call(
                    self.runtime["py_dict_update"],
                    [d, m_obj],
                    name=self._fresh("dict.splat.update"),
                )
                self._emit_post_call_err_check(
                    getattr(v_expr, "span", None),
                    release_on_error=(
                        (m_obj,) if mapping_owned and not mapping_pinned else ()
                    ),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=mapping_pin_cleanup,
                )
                if mapping_pinned:
                    self._gc_unpin(m_obj)
                if mapping_owned:
                    self._gc_release(
                        m_obj,
                        self._release_expr_label("container", v_expr),
                    )
            else:
                k_obj = self._emit_expr_with_cpy_operand_cleanup(
                    k_expr,
                    (),
                    ((d, dict_root),),
                    (),
                    True,
                )
                key_owned = self._container_store_temp_needs_release(
                    k_expr,
                    k_expr.ty,
                    False,
                    k_obj,
                )
                key_pinned = (
                    isinstance(k_obj.type, ir.PointerType)
                    and self._pcc_pointer_source_needs_pin(k_expr)
                )
                if key_pinned:
                    self._gc_pin(k_obj)
                key_pin_cleanup = ((k_obj, key_owned),) if key_pinned else ()
                self._emit_post_call_err_check(
                    getattr(k_expr, "span", None),
                    release_on_error=(
                        (k_obj,) if key_owned and not key_pinned else ()
                    ),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=key_pin_cleanup,
                )
                v_obj = self._emit_expr_with_cpy_operand_cleanup(
                    v_expr,
                    (),
                    ((d, dict_root),),
                    key_pin_cleanup,
                    True,
                )
                value_owned = self._container_store_temp_needs_release(
                    v_expr,
                    v_expr.ty,
                    False,
                    v_obj,
                )
                value_pinned = (
                    isinstance(v_obj.type, ir.PointerType)
                    and self._pcc_pointer_source_needs_pin(v_expr)
                )
                if value_pinned:
                    self._gc_pin(v_obj)
                pinned_on_error = key_pin_cleanup
                if value_pinned:
                    pinned_on_error = pinned_on_error + ((v_obj, value_owned),)
                release_on_error = []
                if key_owned and not key_pinned:
                    release_on_error.append(k_obj)
                if value_owned and not value_pinned:
                    release_on_error.append(v_obj)
                self._emit_post_call_err_check(
                    getattr(v_expr, "span", None),
                    release_on_error=tuple(release_on_error),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=pinned_on_error,
                )
                self.builder.call(
                    self.runtime["py_dict_set"],
                    [d, k_obj, v_obj],
                )
                self._emit_post_call_err_check(
                    getattr(v_expr, "span", None),
                    release_on_error=tuple(release_on_error),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=pinned_on_error,
                )
                # py_dict_set borrows (balanced store); owned key/value
                # temps must be released here, mirroring py_list_append.
                if key_pinned:
                    self._gc_unpin(k_obj)
                if key_owned:
                    self._gc_release(
                        k_obj,
                        self._release_expr_label("container", k_expr),
                    )
                if value_pinned:
                    self._gc_unpin(v_obj)
                if value_owned:
                    self._gc_release(
                        v_obj,
                        self._release_expr_label("container", v_expr),
                    )
        self._leave_container_temp_root(dict_root)
        return d

    def _emit_dict_literal(self, expr: DictExpr) -> ir.Value:
        # ``{**m, ...}`` — the lift encodes a ``**mapping`` splat as a pair whose
        # key is the sentinel Name("**"). Route to the splat-aware builder so the
        # sentinel isn't emitted as a real key lookup (which raised a runtime
        # NameError "name '**' is not defined").
        if any(
            isinstance(k_expr, Name) and k_expr.ident == "**"
            for k_expr, _v_expr in expr.pairs
        ):
            return self._emit_dict_literal_with_splat(expr)
        # Native dict insertion may dispatch user ``__hash__``/``__eq__``.
        # Build one pair at a time so those effects (and failures) happen
        # before the next key/value expression, including for Dyn values that
        # came back from a pcc-native container.  CPython-domain operands keep
        # using the separate bridge path below.
        if not any(
            self._expr_looks_cpython(k_expr)
            or self._expr_looks_cpython(v_expr)
            for k_expr, v_expr in expr.pairs
        ):
            d = self.builder.call(
                self.runtime["py_dict_new"],
                [],
                name=self._fresh("dict.new"),
            )
            self._emit_post_call_err_check()
            dict_root = (
                self._enter_container_temp_root(d, "dict")
                if expr.pairs
                else ir.Constant(_CSTR, None)
            )
            for k_expr, v_expr in expr.pairs:
                k_obj = self._emit_expr_with_cpy_operand_cleanup(
                    k_expr,
                    (),
                    ((d, dict_root),),
                    (),
                    True,
                )
                key_owned = self._container_store_temp_needs_release(
                    k_expr,
                    k_expr.ty,
                    False,
                    k_obj,
                )
                key_pinned = (
                    isinstance(k_obj.type, ir.PointerType)
                    and self._pcc_pointer_source_needs_pin(k_expr)
                )
                if key_pinned:
                    self._gc_pin(k_obj)
                key_pin_cleanup = (
                    ((k_obj, key_owned),) if key_pinned else ()
                )
                self._emit_post_call_err_check(
                    getattr(k_expr, "span", None),
                    release_on_error=(
                        (k_obj,) if key_owned and not key_pinned else ()
                    ),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=key_pin_cleanup,
                )
                v_obj = self._emit_expr_with_cpy_operand_cleanup(
                    v_expr,
                    (),
                    ((d, dict_root),),
                    key_pin_cleanup,
                    True,
                )
                value_owned = self._container_store_temp_needs_release(
                    v_expr,
                    v_expr.ty,
                    False,
                    v_obj,
                )
                value_pinned = (
                    isinstance(v_obj.type, ir.PointerType)
                    and self._pcc_pointer_source_needs_pin(v_expr)
                )
                if value_pinned:
                    self._gc_pin(v_obj)
                pinned_on_error = key_pin_cleanup
                if value_pinned:
                    pinned_on_error = pinned_on_error + (
                        (v_obj, value_owned),
                    )
                unpinned_owned_on_error = []
                if key_owned and not key_pinned:
                    unpinned_owned_on_error.append(k_obj)
                if value_owned and not value_pinned:
                    unpinned_owned_on_error.append(v_obj)
                self._emit_post_call_err_check(
                    getattr(v_expr, "span", None),
                    release_on_error=tuple(unpinned_owned_on_error),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=pinned_on_error,
                )
                self.builder.call(
                    self.runtime["py_dict_set"],
                    [d, k_obj, v_obj],
                )
                self._emit_post_call_err_check(
                    getattr(v_expr, "span", None),
                    release_on_error=tuple(unpinned_owned_on_error),
                    rooted_release_on_error=((d, dict_root),),
                    pinned_release_on_error=pinned_on_error,
                )
                # py_dict_set borrows (balanced store); owned key/value
                # temps must be released here, mirroring py_list_append.
                if key_pinned:
                    self._gc_unpin(k_obj)
                if key_owned:
                    self._gc_release(
                        k_obj,
                        self._release_expr_label("container", k_expr),
                    )
                if value_pinned:
                    self._gc_unpin(v_obj)
                if value_owned:
                    self._gc_release(
                        v_obj,
                        self._release_expr_label("container", v_expr),
                    )
            self._leave_container_temp_root(dict_root)
            return d

        items: list[
            tuple[
                ir.Value,
                Type,
                bool,
                bool,
                ir.Value,
                Type,
                bool,
                bool,
                Expr,
                Expr,
            ]
        ] = []
        has_cpy_key = False
        cpy_values = self._cpy_values
        live_cpy_owned: list[ir.Value] = []
        live_pinned_pcc: list[tuple[ir.Value, bool]] = []
        for k_expr, v_expr in expr.pairs:
            k_payload = self._maybe_emit_valueclass_constructor_payload(
                k_expr.ty,
                k_expr,
            )
            if k_payload is not None:
                k = k_payload
            else:
                k = self._emit_expr_with_cpy_operand_cleanup(
                    k_expr,
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
            if k in cpy_values:
                self._guard_cpy_value_not_null(
                    k,
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
                if self._cpy_value_is_owned(k):
                    live_cpy_owned.append(k)
            k_is_cpy = k in cpy_values
            k_pinned = False
            if (
                isinstance(k.type, ir.PointerType)
                and not k_is_cpy
                and self._pcc_pointer_source_needs_pin(k_expr)
            ):
                self._gc_pin(k)
                k_pinned = True
                live_pinned_pcc.append(
                    (
                        k,
                        self._owned_release_needed(k, k_expr) or self._pcc_pointer_source_is_owned(k_expr),
                    )
                )
            v_payload = self._maybe_emit_valueclass_constructor_payload(
                v_expr.ty,
                v_expr,
            )
            if v_payload is not None:
                v = v_payload
            else:
                if live_cpy_owned or live_pinned_pcc:
                    # Native-callable preference only affects callable object
                    # materialization; preserve the surrounding CPython
                    # cleanup scope while evaluating this value operand.
                    previous_preference = self._prefer_native_callable_values
                    self._prefer_native_callable_values = True
                    try:
                        v = self._emit_expr_with_cpy_operand_cleanup(
                            v_expr,
                            tuple(live_cpy_owned),
                            (),
                            tuple(live_pinned_pcc),
                        )
                    finally:
                        self._prefer_native_callable_values = previous_preference
                else:
                    v = self._emit_expr_with_native_callable_values(v_expr)
            if v in cpy_values:
                self._guard_cpy_value_not_null(
                    v,
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
                if self._cpy_value_is_owned(v):
                    live_cpy_owned.append(v)
            v_is_cpy = v in cpy_values
            v_pinned = False
            if (
                isinstance(v.type, ir.PointerType)
                and not v_is_cpy
                and self._pcc_pointer_source_needs_pin(v_expr)
            ):
                self._gc_pin(v)
                v_pinned = True
                live_pinned_pcc.append(
                    (
                        v,
                        self._owned_release_needed(v, v_expr) or self._pcc_pointer_source_is_owned(v_expr),
                    )
                )
            items.append(
                (
                    k,
                    k_expr.ty,
                    k_is_cpy,
                    k_pinned,
                    v,
                    v_expr.ty,
                    v_is_cpy,
                    v_pinned,
                    k_expr,
                    v_expr,
                )
            )
            if k_is_cpy:
                has_cpy_key = True
        if has_cpy_key:
            if len(items) > 1:
                raise NotImplementedError(
                    "multi-pair CPython-key dict literal cannot preserve "
                    "per-pair insertion errors before later operand "
                    "evaluation"
                )
            result = self._emit_cpython_dict_items(
                [
                    (k, k_ty, v, v_ty)
                    for (
                        k,
                        k_ty,
                        _,
                        _,
                        v,
                        v_ty,
                        _,
                        _,
                        _,
                        _,
                    ) in items
                ],
                tuple(live_pinned_pcc),
            )
            for (
                k,
                _k_ty,
                _k_is_cpy,
                k_pinned,
                v,
                _v_ty,
                _v_is_cpy,
                v_pinned,
                k_expr,
                v_expr,
            ) in items:
                if k_pinned:
                    self._gc_unpin(k)
                    self._gc_release_if_owned(k, k_expr)
                if v_pinned:
                    self._gc_unpin(v)
                    self._gc_release_if_owned(v, v_expr)
            return result
        if len(items) > 1 and any(
            not isinstance(
                k_expr.ty,
                (BoolType, BytesType, FloatType, IntType, NoneType, StrType),
            )
            for (
                _k,
                _k_ty,
                _k_is_cpy,
                _k_pinned,
                _v,
                _v_ty,
                _v_is_cpy,
                _v_pinned,
                k_expr,
                _v_expr,
            ) in items
        ):
            raise NotImplementedError(
                "multi-pair dict literal with a user-observable key cannot "
                "delay hash/equality dispatch until after later operand "
                "evaluation"
            )
        d = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh("dict.new"),
        )
        pending_owned = list(live_cpy_owned)
        pending_pins = list(live_pinned_pcc)
        self._emit_post_call_err_check(
            cpy_release_on_error=tuple(pending_owned),
            pinned_release_on_error=tuple(pending_pins),
        )
        dict_root = self._enter_container_temp_root(d, "dict")
        for (
            k,
            k_ty,
            k_is_cpy,
            k_pinned,
            v,
            v_ty,
            v_is_cpy,
            v_pinned,
            k_expr,
            v_expr,
        ) in items:
            rooted_on_error = ((d, dict_root),)
            k_pin_entry = None
            v_pin_entry = None
            if k_pinned:
                for candidate in pending_pins:
                    if candidate[0] is k:
                        k_pin_entry = candidate
                        break
            if v_pinned:
                for candidate in pending_pins:
                    if candidate[0] is v and candidate is not k_pin_entry:
                        v_pin_entry = candidate
                        break
            k_obj = self._emit_value_as_pcc_object_or_bridge(
                k,
                k_ty,
                "dict.key",
                consume_valueclass_payload_fields=
                    self._valueclass_payload_expr_fields_are_owned(k_expr),
                cpy_owned_on_error=tuple(pending_owned),
                rooted_pcc_on_error=rooted_on_error,
                pinned_pcc_on_error=tuple(pending_pins),
            )
            key_temp_needs_release = self._container_store_temp_needs_release(
                k_expr,
                k_ty,
                k_is_cpy,
                k_obj,
            )
            key_release_on_error = ()
            if key_temp_needs_release and not (
                k_pin_entry is not None
                and k_pin_entry[1]
                and k_obj is k
            ):
                key_release_on_error = (k_obj,)
            self._emit_post_call_err_check(
                getattr(k_expr, "span", None),
                release_on_error=key_release_on_error,
                cpy_release_on_error=tuple(pending_owned),
                rooted_release_on_error=rooted_on_error,
                pinned_release_on_error=tuple(pending_pins),
            )
            value_was_owned = v_is_cpy and self._cpy_value_is_owned(v)
            v_obj = self._emit_value_as_pcc_object_or_bridge(
                v,
                v_ty,
                "cpy.dict.val" if v_is_cpy else "dict.val",
                consume_valueclass_payload_fields=
                    self._valueclass_payload_expr_fields_are_owned(v_expr),
                cpy_owned_on_error=tuple(pending_owned),
                rooted_pcc_on_error=rooted_on_error,
                pinned_pcc_on_error=tuple(pending_pins),
                pcc_release_on_error=key_release_on_error,
            )
            if value_was_owned:
                pending_owned = [
                    owned for owned in pending_owned if owned is not v
                ]
            value_temp_needs_release = self._container_store_temp_needs_release(
                v_expr,
                v_ty,
                v_is_cpy,
                v_obj,
            )
            value_release_on_error = ()
            if value_temp_needs_release and not (
                v_pin_entry is not None
                and v_pin_entry[1]
                and v_obj is v
            ):
                value_release_on_error = (v_obj,)
            self.builder.call(
                self.runtime["py_dict_set"],
                [d, k_obj, v_obj],
            )
            set_release_on_error = []
            for release_value in key_release_on_error + value_release_on_error:
                if not any(
                    existing is release_value
                    for existing in set_release_on_error
                ):
                    set_release_on_error.append(release_value)
            self._emit_post_call_err_check(
                getattr(v_expr, "span", None),
                release_on_error=tuple(set_release_on_error),
                cpy_release_on_error=tuple(pending_owned),
                rooted_release_on_error=rooted_on_error,
                pinned_release_on_error=tuple(pending_pins),
            )
            # py_dict_set borrows (balanced store); owned key/value temps
            # must be released here, mirroring the list-literal path.
            if k_pinned:
                self._gc_unpin(k)
            if v_pinned:
                self._gc_unpin(v)
            if key_temp_needs_release:
                self._gc_release(
                    k_obj,
                    self._release_expr_label("container", k_expr),
                )
            elif k_pin_entry is not None and k_pin_entry[1]:
                self._gc_release(k)
            if value_temp_needs_release:
                self._gc_release(
                    v_obj,
                    self._release_expr_label("container", v_expr),
                )
            elif v_pin_entry is not None and v_pin_entry[1]:
                self._gc_release(v)
            if k_pin_entry is not None:
                pending_pins.remove(k_pin_entry)
            if v_pin_entry is not None:
                pending_pins.remove(v_pin_entry)
        self._leave_container_temp_root(dict_root)
        return d

    def _emit_dict_builtin(self, expr: Call) -> ir.Value:
        if len(expr.args) > 1:
            raise NotImplementedError(
                f"dict() takes at most 1 positional arg at L2; got {len(expr.args)}"
            )
        if len(expr.args) == 1 and not expr.kwargs:
            if isinstance(expr.args[0].ty, DictType):
                # ``dict(mapping)`` is a shallow COPY, not a walk over
                # (key, value) pairs.  The generic loop below indexes the
                # source positionally, and for a dict that is a KEY lookup
                # for 0, 1, 2 …, so ``dict(d)`` silently returned an EMPTY
                # dict.  `_maybe_emit_dict_builtin` already implements the
                # copy correctly via py_dict_keys + py_dict_get.
                copied = self._maybe_emit_dict_builtin(expr)
                if copied is not None:
                    return copied
        out = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh("dict.new"),
        )
        out_root = self._enter_container_temp_root(out, "dict")
        if len(expr.args) == 0:
            for kw_name, kw_expr in expr.kwargs:
                key_obj = self._emit_str_literal(kw_name)
                val_obj = self._emit_expr_with_cpy_operand_cleanup(
                    kw_expr,
                    (),
                    ((out, out_root),),
                    (),
                    True,
                )
                self.builder.call(
                    self.runtime["py_dict_set"],
                    [out, key_obj, val_obj],
                )
            self._leave_container_temp_root(out_root)
            return out

        src_expr = expr.args[0]
        # Nested error edges (e.g. the source list literal's own allocation
        # checks) must leave ``out_root`` before joining the outer error
        # path, or the precise stack-map join sees imbalanced root state.
        src_val = self._emit_expr_with_cpy_operand_cleanup(
            src_expr,
            (),
            ((out, out_root),),
        )
        src_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            src_val,
            src_expr.ty,
        )
        fn = self.current_function
        src_is_dyn = isinstance(src_expr.ty, DynType)
        if not src_is_dyn and not isinstance(
            src_expr.ty, (DictType, ListType, TupleType)
        ):
            # The walk below indexes the source positionally, which only a
            # list or tuple of pairs supports.  Every other statically known
            # type either cannot hold pairs (int, str, ...) or cannot be
            # indexed positionally (set), and previously fell through to
            # py_obj_len's silent 0 / py_obj_getitem's silent NULL and
            # produced an empty or corrupt dict with no error.  Fail closed
            # at runtime (a set of pairs is CPython-legal but unimplemented
            # here — the diagnostic is the documented interim).
            self._leave_container_temp_root(out_root)
            self._gc_release(out, self._release_context_label("dict.new"))
            message = self._ptr_to_cstr(
                self._cstr_global(
                    "dict() argument must be a mapping or a "
                    "list/tuple of pairs",
                    ".dict.dyn.typeerror",
                )
            )
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 3), message],
                name=self._fresh("dict.static.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            err_target = (
                self._current_try_err_block() or self._ensure_fn_err_exit()
            )
            self.builder.branch(err_target)
            cont_bb = fn.append_basic_block(
                name=self._fresh("dict.static.badtype.cont")
            )
            # Unreachable continuation: the enclosing statement emitter
            # keeps building here and terminates the block as usual.
            self.builder.position_at_end(cont_bb)
            return out
        cond_bb = fn.append_basic_block(name=self._fresh("dict.from.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("dict.from.body"))
        step_bb = fn.append_basic_block(name=self._fresh("dict.from.step"))
        end_bb = fn.append_basic_block(name=self._fresh("dict.from.end"))
        if src_is_dyn:
            # The positional pairs walk below silently mis-handles a
            # dyn-held source: for a runtime dict, py_obj_getitem(d, i) is a
            # KEY lookup (raising KeyError mid-walk with no check); for a
            # non-iterable, py_obj_len returns 0 WITHOUT raising and the
            # result was a silent empty dict.  Dispatch on the runtime tag:
            # dict -> shallow copy (CPython semantics, via the moving-GC-safe
            # py_dict_update); list/tuple -> the pairs walk; anything else ->
            # fail closed with TypeError.
            tag = self.builder.call(
                self.runtime["py_obj_type_tag"],
                [src_obj],
                name=self._fresh("dict.src.tag"),
            )
            copy_bb = fn.append_basic_block(
                name=self._fresh("dict.dyn.copy")
            )
            notdict_bb = fn.append_basic_block(
                name=self._fresh("dict.dyn.notdict")
            )
            pairs_bb = fn.append_basic_block(
                name=self._fresh("dict.dyn.pairs")
            )
            bad_bb = fn.append_basic_block(
                name=self._fresh("dict.dyn.notiterable")
            )
            is_dict = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_DICT),
                name=self._fresh("dict.src.is.dict"),
            )
            self.builder.cbranch(is_dict, copy_bb, notdict_bb)
            self.builder.position_at_end(notdict_bb)
            is_list = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_LIST),
                name=self._fresh("dict.src.is.list"),
            )
            is_tuple = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_TUPLE),
                name=self._fresh("dict.src.is.tuple"),
            )
            pairs_ok = self.builder.or_(
                is_list,
                is_tuple,
                name=self._fresh("dict.src.pairs.ok"),
            )
            self.builder.cbranch(pairs_ok, pairs_bb, bad_bb)
            self.builder.position_at_end(bad_bb)
            self._leave_container_temp_root(out_root)
            self._gc_release(out, self._release_context_label("dict.new"))
            message = self._ptr_to_cstr(
                self._cstr_global(
                    "dict() argument must be a mapping or a "
                    "list/tuple of pairs",
                    ".dict.dyn.typeerror",
                )
            )
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 3), message],
                name=self._fresh("dict.dyn.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            err_target = (
                self._current_try_err_block() or self._ensure_fn_err_exit()
            )
            self.builder.branch(err_target)
            self.builder.position_at_end(copy_bb)
            self.builder.call(
                self.runtime["py_dict_update"],
                [out, src_obj],
            )
            self.builder.branch(end_bb)
            self.builder.position_at_end(pairs_bb)
        n_val = self.builder.call(
            self.runtime["py_obj_len"],
            [src_obj],
            name=self._fresh("dict.src.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="dict.src.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("dict.src.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("dict.src.more"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("dict.src.idx.box"),
        )
        pair_obj = self.builder.call(
            self.runtime["py_obj_getitem"],
            [src_obj, idx_box],
            name=self._fresh("dict.src.pair"),
        )
        zero_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("dict.pair.zero"),
        )
        one_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, 1)],
            name=self._fresh("dict.pair.one"),
        )
        key_obj = self.builder.call(
            self.runtime["py_obj_getitem"],
            [pair_obj, zero_box],
            name=self._fresh("dict.pair.key"),
        )
        val_obj = self.builder.call(
            self.runtime["py_obj_getitem"],
            [pair_obj, one_box],
            name=self._fresh("dict.pair.val"),
        )
        loop_temps = (
            val_obj,
            key_obj,
            pair_obj,
            one_box,
            zero_box,
            idx_box,
        )
        if src_is_dyn:
            # A dyn pair element can be a user-class object whose __getitem__
            # raises; the loop temps and the dict under construction are live
            # owned references on that edge.
            self._emit_post_call_err_check(
                src_expr.span,
                release_on_error=loop_temps,
                rooted_release_on_error=((out, out_root),),
            )
            # py_obj_getitem returns NULL WITHOUT raising for unsupported
            # tags (py_obj_ops_dispatch.c), so a non-pair element (e.g.
            # dict([(1, 2), 3])) produced NULL key/value here and previously
            # fed them straight into py_dict_set.  Fail closed.
            key_null = self.builder.icmp_unsigned(
                "==",
                key_obj,
                ir.Constant(key_obj.type, None),
                name=self._fresh("dict.pair.key.null"),
            )
            val_null = self.builder.icmp_unsigned(
                "==",
                val_obj,
                ir.Constant(val_obj.type, None),
                name=self._fresh("dict.pair.val.null"),
            )
            pair_bad = self.builder.or_(
                key_null,
                val_null,
                name=self._fresh("dict.pair.bad"),
            )
            badpair_bb = fn.append_basic_block(
                name=self._fresh("dict.pair.notpair")
            )
            pair_ok_bb = fn.append_basic_block(
                name=self._fresh("dict.pair.ok")
            )
            self.builder.cbranch(pair_bad, badpair_bb, pair_ok_bb)
            self.builder.position_at_end(badpair_bb)
            for temp in loop_temps:
                self._gc_release(
                    temp, self._release_context_label("dict.pair.bad")
                )
            self._leave_container_temp_root(out_root)
            self._gc_release(out, self._release_context_label("dict.new"))
            message = self._ptr_to_cstr(
                self._cstr_global(
                    "dict() iterable element is not a pair",
                    ".dict.pair.typeerror",
                )
            )
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 3), message],
                name=self._fresh("dict.pair.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            err_target = (
                self._current_try_err_block() or self._ensure_fn_err_exit()
            )
            self.builder.branch(err_target)
            self.builder.position_at_end(pair_ok_bb)
        self.builder.call(
            self.runtime["py_dict_set"],
            [out, key_obj, val_obj],
        )
        # py_dict_set RETAINS what it stores; every loop temp above is a NEW
        # ref (py_obj_getitem, py_int_from_i64), so without these releases
        # dict(pairs) leaked pair/key/value per entry — on the NORMAL path.
        # pcc_gc_release is NULL- and tagged-int-safe.
        for temp in loop_temps:
            self._gc_release(
                temp, self._release_context_label("dict.pair.temp")
            )
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("dict.src.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("dict.src.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        for kw_name, kw_expr in expr.kwargs:
            key_obj = self._emit_str_literal(kw_name)
            val_obj = self._emit_expr_with_cpy_operand_cleanup(
                kw_expr,
                (),
                ((out, out_root),),
                (),
                True,
            )
            self.builder.call(
                self.runtime["py_dict_set"],
                [out, key_obj, val_obj],
            )
        self._leave_container_temp_root(out_root)
        return out

    def _emit_tuple_literal(self, expr: TupleExpr) -> ir.Value:
        has_splat = any(
            isinstance(el, Call)
            and isinstance(el.func, Name)
            and el.func.ident in ("*", "__starred__")
            and len(el.args) == 1
            for el in expr.elems
        )
        has_nonterminal_splat = any(
            isinstance(el, Call)
            and isinstance(el.func, Name)
            and el.func.ident in ("*", "__starred__")
            and len(el.args) == 1
            and index + 1 < len(expr.elems)
            for index, el in enumerate(expr.elems)
        )
        if has_nonterminal_splat and not any(
            self._expr_looks_cpython(
                el.args[0]
                if (
                    isinstance(el, Call)
                    and isinstance(el.func, Name)
                    and el.func.ident in ("*", "__starred__")
                    and len(el.args) == 1
                )
                else el
            )
            for el in expr.elems
        ):
            return self._emit_native_splat_sequence_at_source_position(
                expr.elems,
                tuple_result=True,
            )
        if not has_splat:
            if not any(
                self._expr_looks_cpython(el)
                or isinstance(el.ty, DynType)
                or self._is_valueclass_payload_type(el.ty)
                for el in expr.elems
            ):
                n = len(expr.elems)
                n_val = ir.Constant(_I64, n)
                tup = self.builder.call(
                    self.runtime["py_tuple_new"],
                    [n_val],
                    name=self._fresh("tup.new"),
                )
                self._emit_post_call_err_check()
                tup_root = (
                    self._enter_container_temp_root(tup, "tuple")
                    if expr.elems
                    else ir.Constant(_CSTR, None)
                )
                for i, el in enumerate(expr.elems):
                    v_obj = self._emit_expr_with_cpy_operand_cleanup(
                        el,
                        (),
                        ((tup, tup_root),),
                        (),
                        True,
                    )
                    idx = ir.Constant(_I64, i)
                    release_temp = self._container_store_temp_needs_release(
                        el,
                        el.ty,
                        v_obj in self._cpy_values,
                        v_obj,
                    )
                    temp_pinned = (
                        isinstance(v_obj.type, ir.PointerType)
                        and self._pcc_pointer_source_needs_pin(el)
                    )
                    if temp_pinned:
                        self._gc_pin(v_obj)
                    self.builder.call(
                        self.runtime["py_tuple_set_item"],
                        [tup, idx, v_obj],
                    )
                    self._emit_post_call_err_check(
                        getattr(el, "span", None),
                        release_on_error=(
                            (v_obj,) if release_temp and not temp_pinned else ()
                        ),
                        rooted_release_on_error=((tup, tup_root),),
                        pinned_release_on_error=(
                            ((v_obj, release_temp),) if temp_pinned else ()
                        ),
                    )
                    if temp_pinned:
                        self.builder.call(
                            self.runtime["pcc_gc_unpin"],
                            [v_obj],
                        )
                    if release_temp:
                        self._gc_release(
                            v_obj,
                            self._release_expr_label("container", el),
                        )
                self._leave_container_temp_root(tup_root)
                return tup

            ops: list[tuple[str, ir.Value, Type, bool, bool, Expr]] = []
            live_cpy_owned: list[ir.Value] = []
            live_pinned_pcc: list[tuple[ir.Value, bool]] = []
            for el in expr.elems:
                valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
                    el.ty,
                    el,
                )
                if valueclass_payload is not None:
                    v = valueclass_payload
                else:
                    v = self._emit_expr_with_cpy_operand_cleanup(
                        el,
                        tuple(live_cpy_owned),
                        (),
                        tuple(live_pinned_pcc),
                    )
                is_cpy = v in self._cpy_values
                if is_cpy:
                    self._guard_cpy_value_not_null(
                        v,
                        tuple(live_cpy_owned),
                        (),
                        tuple(live_pinned_pcc),
                    )
                    if self._cpy_value_is_owned(v):
                        live_cpy_owned.append(v)
                pinned = False
                if (
                    isinstance(v.type, ir.PointerType)
                    and not is_cpy
                    and self._pcc_pointer_source_needs_pin(el)
                ):
                    self._gc_pin(v)
                    pinned = True
                    live_pinned_pcc.append(
                        (
                            v,
                            self._owned_release_needed(v, el) or self._pcc_pointer_source_is_owned(el),
                        )
                    )
                ops.append(("append", v, el.ty, is_cpy, pinned, el))
            n = len(ops)
            n_val = ir.Constant(_I64, n)
            tup = self.builder.call(
                self.runtime["py_tuple_new"],
                [n_val],
                name=self._fresh("tup.new"),
            )
            pending_owned = list(live_cpy_owned)
            pending_pins = list(live_pinned_pcc)
            self._emit_post_call_err_check(
                cpy_release_on_error=tuple(pending_owned),
                pinned_release_on_error=tuple(pending_pins),
            )
            tup_root = self._enter_container_temp_root(tup, "tuple")
            for i, (
                _op_kind,
                value,
                value_ty,
                is_cpy,
                pinned,
                src_expr,
            ) in enumerate(ops):
                rooted_on_error = ((tup, tup_root),)
                pin_entry = None
                if pinned:
                    for candidate in pending_pins:
                        if candidate[0] is value:
                            pin_entry = candidate
                            break
                value_was_owned = is_cpy and self._cpy_value_is_owned(value)
                v_obj = self._emit_value_as_pcc_object_or_bridge(
                    value,
                    value_ty,
                    "cpy.tup.elem" if is_cpy else "tup.elem",
                    consume_valueclass_payload_fields=
                        self._valueclass_payload_expr_fields_are_owned(src_expr),
                    cpy_owned_on_error=tuple(pending_owned),
                    rooted_pcc_on_error=rooted_on_error,
                    pinned_pcc_on_error=tuple(pending_pins),
                )
                if value_was_owned:
                    pending_owned = [
                        owned for owned in pending_owned if owned is not value
                    ]
                idx = ir.Constant(_I64, i)
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [tup, idx, v_obj],
                )
                temp_needs_release = self._container_store_temp_needs_release(
                    src_expr,
                    value_ty,
                    is_cpy,
                    v_obj,
                )
                release_on_error = ()
                if temp_needs_release and not (
                    pin_entry is not None
                    and pin_entry[1]
                    and v_obj is value
                ):
                    release_on_error = (v_obj,)
                self._emit_post_call_err_check(
                    getattr(src_expr, "span", None),
                    release_on_error=release_on_error,
                    cpy_release_on_error=tuple(pending_owned),
                    rooted_release_on_error=rooted_on_error,
                    pinned_release_on_error=tuple(pending_pins),
                )
                if pinned:
                    self._gc_unpin(value)
                if temp_needs_release:
                    self._gc_release(
                        v_obj,
                        self._release_expr_label("container", src_expr),
                    )
                elif pin_entry is not None and pin_entry[1]:
                    self._gc_release(value)
                if pin_entry is not None:
                    pending_pins.remove(pin_entry)
            self._leave_container_temp_root(tup_root)
            return tup

        ops: list[tuple[str, ir.Value, Type, bool, bool, Expr]] = []
        cpy_extend = False
        live_cpy_owned: list[ir.Value] = []
        live_pinned_pcc: list[tuple[ir.Value, bool]] = []
        for el_index, el in enumerate(expr.elems):
            if (
                isinstance(el, Call)
                and isinstance(el.func, Name)
                and el.func.ident in ("*", "__starred__")
                and len(el.args) == 1
            ):
                if el_index + 1 < len(expr.elems):
                    raise NotImplementedError(
                        "iterable splat cannot precede following literal "
                        "operands until expansion is lowered at its source "
                        "position"
                    )
                inner = self._emit_expr_with_cpy_operand_cleanup(
                    el.args[0],
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
                is_cpy = inner in self._cpy_values
                if is_cpy:
                    self._guard_cpy_value_not_null(
                        inner,
                        tuple(live_cpy_owned),
                        (),
                        tuple(live_pinned_pcc),
                    )
                    if self._cpy_value_is_owned(inner):
                        live_cpy_owned.append(inner)
                pinned = False
                if (
                    isinstance(inner.type, ir.PointerType)
                    and not is_cpy
                    and self._pcc_pointer_source_needs_pin(el.args[0])
                ):
                    self._gc_pin(inner)
                    pinned = True
                    live_pinned_pcc.append(
                        (
                            inner,
                            self._owned_release_needed(inner, el.args[0]) or self._pcc_pointer_source_is_owned(el.args[0]),
                        )
                    )
                ops.append(
                    ("extend", inner, el.args[0].ty, is_cpy, pinned, el.args[0])
                )
                if is_cpy:
                    cpy_extend = True
                continue
            valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
                el.ty,
                el,
            )
            if valueclass_payload is not None:
                v = valueclass_payload
            else:
                v = self._emit_expr_with_cpy_operand_cleanup(
                    el,
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
            is_cpy = v in self._cpy_values
            if is_cpy:
                self._guard_cpy_value_not_null(
                    v,
                    tuple(live_cpy_owned),
                    (),
                    tuple(live_pinned_pcc),
                )
                if self._cpy_value_is_owned(v):
                    live_cpy_owned.append(v)
            pinned = False
            if (
                isinstance(v.type, ir.PointerType)
                and not is_cpy
                and self._pcc_pointer_source_needs_pin(el)
            ):
                self._gc_pin(v)
                pinned = True
                live_pinned_pcc.append(
                    (
                        v,
                        self._owned_release_needed(v, el) or self._pcc_pointer_source_is_owned(el),
                    )
                )
            ops.append(("append", v, el.ty, is_cpy, pinned, el))
        if cpy_extend:
            result = self._emit_cpython_tuple_ops(
                [(kind, value, ty) for kind, value, ty, _, _, _ in ops],
                tuple(live_pinned_pcc),
            )
            for _op_kind, value, _value_ty, _is_cpy, pinned, src_expr in ops:
                if pinned:
                    self._gc_unpin(value)
                    self._gc_release_if_owned(value, src_expr)
            return result
        lst = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("tup.splat.list"),
        )
        pending_owned = list(live_cpy_owned)
        pending_pins = list(live_pinned_pcc)
        self._emit_post_call_err_check(
            cpy_release_on_error=tuple(pending_owned),
            pinned_release_on_error=tuple(pending_pins),
        )
        lst_root = self._enter_container_temp_root(lst, "tuple.splat.list")
        for op_kind, value, value_ty, is_cpy, pinned, src_expr in ops:
            rooted_on_error = ((lst, lst_root),)
            pin_entry = None
            if pinned:
                for candidate in pending_pins:
                    if candidate[0] is value:
                        pin_entry = candidate
                        break
            if op_kind == "extend":
                self.builder.call(
                    self.runtime["py_list_extend"],
                    [lst, value],
                )
                self._emit_post_call_err_check(
                    getattr(src_expr, "span", None),
                    cpy_release_on_error=tuple(pending_owned),
                    rooted_release_on_error=rooted_on_error,
                    pinned_release_on_error=tuple(pending_pins),
                )
                if pinned:
                    self._gc_unpin(value)
                    if pin_entry is not None and pin_entry[1]:
                        self._gc_release(value)
                    pending_pins.remove(pin_entry)
                continue
            value_was_owned = is_cpy and self._cpy_value_is_owned(value)
            v_obj = self._emit_value_as_pcc_object_or_bridge(
                value,
                value_ty,
                "cpy.tup.splat.elem" if is_cpy else "tup.splat.elem",
                cpy_owned_on_error=tuple(pending_owned),
                rooted_pcc_on_error=rooted_on_error,
                pinned_pcc_on_error=tuple(pending_pins),
            )
            if value_was_owned:
                pending_owned = [
                    owned for owned in pending_owned if owned is not value
                ]
            self.builder.call(
                self.runtime["py_list_append"],
                [lst, v_obj],
            )
            temp_needs_release = self._container_store_temp_needs_release(
                src_expr,
                value_ty,
                is_cpy,
                v_obj,
            )
            release_on_error = ()
            if temp_needs_release and not (
                pin_entry is not None
                and pin_entry[1]
                and v_obj is value
            ):
                release_on_error = (v_obj,)
            self._emit_post_call_err_check(
                getattr(src_expr, "span", None),
                release_on_error=release_on_error,
                cpy_release_on_error=tuple(pending_owned),
                rooted_release_on_error=rooted_on_error,
                pinned_release_on_error=tuple(pending_pins),
            )
            if pinned:
                self._gc_unpin(value)
            if temp_needs_release:
                self._gc_release(v_obj)
            elif pin_entry is not None and pin_entry[1]:
                self._gc_release(value)
            if pin_entry is not None:
                pending_pins.remove(pin_entry)
        n_val = self.builder.call(
            self.runtime["py_list_len"],
            [lst],
            name=self._fresh("tup.splat.len"),
        )
        self._emit_post_call_err_check(
            rooted_release_on_error=((lst, lst_root),),
        )
        tup = self.builder.call(
            self.runtime["py_tuple_new"],
            [n_val],
            name=self._fresh("tup.splat.new"),
        )
        self._emit_post_call_err_check(
            rooted_release_on_error=((lst, lst_root),),
        )
        tup_root = self._enter_container_temp_root(tup, "tuple.splat")
        fn = self.current_function
        idx_slot = self._alloca_in_entry(_I64, name="tup.splat.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)
        cond_bb = fn.append_basic_block(name=self._fresh("tup.sp.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("tup.sp.body"))
        step_bb = fn.append_basic_block(name=self._fresh("tup.sp.step"))
        end_bb = fn.append_basic_block(name=self._fresh("tup.sp.end"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("tup.sp.i"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("tup.sp.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)
        elem = self.builder.call(
            self.runtime["py_list_get"],
            [lst, cur],
            name=self._fresh("tup.sp.elem"),
        )
        self._emit_post_call_err_check(
            rooted_release_on_error=((tup, tup_root), (lst, lst_root)),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [tup, cur, elem],
        )
        self._emit_post_call_err_check(
            release_on_error=(elem,),
            rooted_release_on_error=((tup, tup_root), (lst, lst_root)),
        )
        self._gc_release(elem)
        self.builder.branch(step_bb)
        self.builder.position_at_end(step_bb)
        nxt = self.builder.add(
            cur,
            ir.Constant(_I64, 1),
            name=self._fresh("tup.sp.inc"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
        self._leave_container_temp_root(tup_root)
        self._leave_container_temp_root(lst_root)
        self._gc_release(lst)
        return tup

    def _emit_cpython_list_ops(
        self,
        ops: list[tuple[str, ir.Value, Type]],
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value:
        pending_owned = []
        for _op_kind, value, _value_ty in ops:
            if self._cpy_value_is_owned(value):
                pending_owned.append(value)
        list_ctor = self._load_cpython_builtin_with_cleanup(
            "list",
            tuple(pending_owned),
            pinned_pcc,
        )
        self._guard_cpy_value_not_null(
            list_ctor,
            tuple(pending_owned),
            (),
            pinned_pcc,
        )
        lst = self.builder.call(
            self.runtime["py_cpy_call_noargs"],
            [list_ctor],
            name=self._fresh("cpy.list"),
        )
        self.builder.call(self.runtime["py_cpy_decref"], [list_ctor])
        self._forget_owned_cpy_value(list_ctor)
        self._guard_cpy_value_not_null(
            lst,
            tuple(pending_owned),
            (),
            pinned_pcc,
        )
        self._mark_cpy_value(lst)
        append_fn = None
        extend_fn = None
        for op_kind, value, value_ty in ops:
            if op_kind == "append":
                if append_fn is None:
                    append_fn = self._emit_cpy_attr_with_cleanup(
                        lst,
                        "append",
                        self._cpy_literal_cleanup_values(
                            lst,
                            None,
                            extend_fn,
                            pending_owned,
                        ),
                        (),
                        pinned_pcc,
                    )
                    self._guard_cpy_value_not_null(
                        append_fn,
                        self._cpy_literal_cleanup_values(
                            lst,
                            None,
                            extend_fn,
                            pending_owned,
                        ),
                        (),
                        pinned_pcc,
                    )
                call_fn = append_fn
            else:
                if extend_fn is None:
                    extend_fn = self._emit_cpy_attr_with_cleanup(
                        lst,
                        "extend",
                        self._cpy_literal_cleanup_values(
                            lst,
                            append_fn,
                            None,
                            pending_owned,
                        ),
                        (),
                        pinned_pcc,
                    )
                    self._guard_cpy_value_not_null(
                        extend_fn,
                        self._cpy_literal_cleanup_values(
                            lst,
                            append_fn,
                            None,
                            pending_owned,
                        ),
                        (),
                        pinned_pcc,
                    )
                call_fn = extend_fn

            cpy_value, owned = self._marshal_to_cpython(value, value_ty)
            self._guard_cpy_value_not_null(
                cpy_value,
                self._cpy_literal_cleanup_values(
                    lst,
                    append_fn,
                    extend_fn,
                    pending_owned,
                ),
                (),
                pinned_pcc,
            )
            result = self.builder.call(
                self.runtime["py_cpy_call1"],
                [call_fn, cpy_value],
                name=self._fresh(
                    "cpy.list.append" if op_kind == "append"
                    else "cpy.list.extend"
                ),
            )
            if owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_value])
                self._forget_owned_cpy_value(cpy_value)
                remaining_owned = []
                for pending in pending_owned:
                    if pending is not cpy_value:
                        remaining_owned.append(pending)
                pending_owned = remaining_owned
            self._guard_cpy_value_not_null(
                result,
                self._cpy_literal_cleanup_values(
                    lst,
                    append_fn,
                    extend_fn,
                    pending_owned,
                ),
                (),
                pinned_pcc,
            )
            self.builder.call(self.runtime["py_cpy_decref"], [result])
        if append_fn is not None:
            self.builder.call(self.runtime["py_cpy_decref"], [append_fn])
            self._forget_owned_cpy_value(append_fn)
        if extend_fn is not None:
            self.builder.call(self.runtime["py_cpy_decref"], [extend_fn])
            self._forget_owned_cpy_value(extend_fn)
        return self._mark_owned_cpy_value(lst)

    def _emit_cpython_tuple_ops(
        self,
        ops: list[tuple[str, ir.Value, Type]],
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value:
        lst = self._emit_cpython_list_ops(ops, pinned_pcc)
        tuple_ctor = self._load_cpython_builtin_with_cleanup(
            "tuple",
            (lst,),
            pinned_pcc,
        )
        self._guard_cpy_value_not_null(tuple_ctor, (lst,), (), pinned_pcc)
        tup = self.builder.call(
            self.runtime["py_cpy_call1"],
            [tuple_ctor, lst],
            name=self._fresh("cpy.tuple"),
        )
        self.builder.call(self.runtime["py_cpy_decref"], [tuple_ctor])
        self._forget_owned_cpy_value(tuple_ctor)
        self.builder.call(self.runtime["py_cpy_decref"], [lst])
        self._forget_owned_cpy_value(lst)
        self._guard_cpy_value_not_null(tup, (), (), pinned_pcc)
        return self._mark_owned_cpy_value(tup)

    def _emit_cpython_dict_items(
        self,
        items: list[tuple[ir.Value, Type, ir.Value, Type]],
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value:
        pending_owned = []
        for k_val, _k_ty, v_val, _v_ty in items:
            if self._cpy_value_is_owned(k_val):
                pending_owned.append(k_val)
            if self._cpy_value_is_owned(v_val):
                pending_owned.append(v_val)
        dict_ctor = self._load_cpython_builtin_with_cleanup(
            "dict",
            tuple(pending_owned),
            pinned_pcc,
        )
        self._guard_cpy_value_not_null(
            dict_ctor,
            tuple(pending_owned),
            (),
            pinned_pcc,
        )
        d = self.builder.call(
            self.runtime["py_cpy_call_noargs"],
            [dict_ctor],
            name=self._fresh("cpy.dict"),
        )
        self.builder.call(self.runtime["py_cpy_decref"], [dict_ctor])
        self._forget_owned_cpy_value(dict_ctor)
        self._guard_cpy_value_not_null(
            d,
            tuple(pending_owned),
            (),
            pinned_pcc,
        )
        self._mark_cpy_value(d)
        for k_val, k_ty, v_val, v_ty in items:
            cpy_key, key_owned = self._marshal_to_cpython(k_val, k_ty)
            self._guard_cpy_value_not_null(
                cpy_key,
                self._cpy_literal_cleanup_values(
                    d,
                    None,
                    None,
                    pending_owned,
                ),
                (),
                pinned_pcc,
            )
            cpy_val, val_owned = self._marshal_to_cpython(v_val, v_ty)
            self._guard_cpy_value_not_null(
                cpy_val,
                self._cpy_literal_cleanup_values(
                    d,
                    None,
                    None,
                    pending_owned,
                    (cpy_key,) if key_owned else (),
                ),
                (),
                pinned_pcc,
            )
            status = self.builder.call(
                self.runtime["py_cpy_setitem"],
                [d, cpy_key, cpy_val],
                name=self._fresh("cpy.dict.setitem"),
            )
            if key_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_key])
                self._forget_owned_cpy_value(cpy_key)
            if val_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_val])
                self._forget_owned_cpy_value(cpy_val)
            remaining_owned = []
            for pending in pending_owned:
                if pending is not cpy_key and pending is not cpy_val:
                    remaining_owned.append(pending)
            pending_owned = remaining_owned
            self._guard_cpy_status_not_negative(
                status,
                self._cpy_literal_cleanup_values(
                    d,
                    None,
                    None,
                    pending_owned,
                ),
                (),
                pinned_pcc,
            )
        return self._mark_owned_cpy_value(d)
