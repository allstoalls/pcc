"""CPython fallback call-shape lowering for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolLit,
    BytesLit,
    Call,
    DictExpr,
    DictType,
    Expr,
    FloatLit,
    FuncType,
    IntLit,
    Lambda,
    ListExpr,
    Name,
    NoneLit,
    StrLit,
    TupleExpr,
)


_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()



# Literal forms whose evaluation cannot raise.  Deliberately a WHITELIST: any
# node not listed -- a call, an attribute, a subscript, a name that might be
# unbound -- is treated as able to raise.
_RAISE_FREE_LEAF_NODES = (IntLit, FloatLit, StrLit, BytesLit, BoolLit, NoneLit)
_RAISE_FREE_CONTAINER_NODES = (TupleExpr, ListExpr)


def _expr_cannot_raise(expr) -> bool:
    """True when evaluating *expr* provably cannot raise.

    NOT WIRED UP -- see the `[DENIED]` note below.  Kept because the predicate
    itself is correct and unit-tested; what was wrong was the conclusion drawn
    from it.

    Container construction from raise-free elements allocates, and allocation
    failure aborts rather than raising, so the whole literal is raise-free.

    This exists because operand cleanup blocks were emitted unconditionally:
    in the generated `_l1_codegen_static_methods` module -- a pure constant
    table -- **63.1% of all 72100 basic blocks were cleanup or error blocks**
    (39284 cleanup + 6224 err) that can never be entered, along with 46960
    `pcc_gc_store_root` and 52579 `pcc_gc_unpin` calls guarding them.  That one
    module became the largest emit shard of a self-hosted build.

    `[DENIED]` Skipping the cleanup edge for such operands **breaks the build**:

        self precise stack-map analysis in '_pcc_py_module_top_...':
        managed root state disagrees at block ...

    A cleanup block is not dead weight even when unreachable.  It carries
    `pcc_gc_store_root(root, null)` and `pcc_gc_frame_leave_lifo`, so it
    participates in the *managed root state* the precise stack-map analysis
    reconciles at every join.  Dropping the edge leaves the join with two
    disagreeing root states and the analysis refuses the function.

    A follow-up guess -- "then just do not open a root for operands that cannot
    raise" -- is ALSO wrong, and is recorded here so it is not tried again:
    `_enter_container_temp_root` exists for **GC** safety, not exception
    safety.  Its own docstring says a literal's container is a bare SSA
    temporary while its elements are populated, and populating them ALLOCATES,
    so a tracing backend on another thread can collect it.  Whether the
    elements can raise has nothing to do with whether the root is needed.

    So the 63% is not established to be removable at all.  Anyone attacking it
    should first explain where ~240 blocks per literal element actually come
    from, with IR evidence, rather than assuming the cleanup blocks are waste.
    """
    if isinstance(expr, _RAISE_FREE_LEAF_NODES):
        return True
    if isinstance(expr, _RAISE_FREE_CONTAINER_NODES):
        for element in expr.elems:
            if not _expr_cannot_raise(element):
                return False
        return True
    if isinstance(expr, DictExpr):
        for key_expr, value_expr in expr.pairs:
            if not _expr_cannot_raise(key_expr):
                return False
            if not _expr_cannot_raise(value_expr):
                return False
        return True
    return False

class CpyCallLoweringMixin:
    def _require_supported_cpy_kw_mapping(self, kwargs_expr: Expr) -> None:
        """Accept only statically dict-shaped ``**`` operands for now.

        CPython expands an arbitrary mapping at the mapping's source position
        (running ``keys``/``__getitem__`` before later operands).  The current
        libpython helper accepts an already-materialized dict and would defer
        or reject that protocol.  A conservative compile-time boundary keeps
        ordering and error behavior honest until there is a mapping-expansion
        bridge ABI.
        """
        kwargs_ty = getattr(kwargs_expr, "ty", None)
        if isinstance(kwargs_ty, DictType) or type(kwargs_ty).__name__ == "DictType":
            return
        raise NotImplementedError(
            "CPython fallback **mapping requires a statically dict-typed "
            "operand; arbitrary mapping expansion is not yet source-ordered"
        )

    def _make_cpy_operand_cleanup_block(
        self,
        live_owned: tuple[ir.Value, ...],
        rooted_pcc: tuple[tuple[ir.Value, ir.Value], ...],
        target: ir.Block,
        name: str,
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
        rooted_pcc_lifetimes: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Block:
        """Build one operand-evaluation unwind edge without moving the caller."""
        cleanup = self.current_function.append_basic_block(
            name=self._fresh(name),
        )
        save_block = self.builder._block
        self.builder.position_at_end(cleanup)
        for owned in live_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [owned])
        for pcc_value, root_slot in rooted_pcc:
            self._leave_container_temp_root(root_slot)
            self._gc_release(pcc_value)
        for pcc_value, release_owned in pinned_pcc:
            self._gc_unpin(pcc_value)
            if release_owned:
                self._gc_release(pcc_value)
        self._release_rooted_pcc_lifetimes(rooted_pcc_lifetimes)
        self.builder.branch(target)
        self.builder.position_at_end(save_block)
        return cleanup

    def _release_rooted_pcc_lifetimes(
        self,
        roots: tuple[tuple[ir.Value, bool], ...],
    ) -> None:
        """Leave traced temporary roots and balance their source owners.

        Each entry is ``(root_slot, release_owned)``.  Reloading before the
        root is left is essential for relocating collectors: an SSA pointer
        captured before a later operand was lowered is no longer authoritative.
        Reverse order also preserves the LIFO frame-root contract.
        """
        for root_slot, release_owned in reversed(roots):
            root_ptr = self._as_gc_ptr(
                root_slot,
                name=self._fresh("operand.tmp.root.ptr"),
            )
            rooted_value = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), root_ptr],
                name=self._fresh("operand.tmp.rooted"),
            )
            self._leave_container_temp_root(root_slot)
            if release_owned:
                self._gc_release(rooted_value, known_object=True)

    def _emit_expr_with_cpy_operand_cleanup(
        self,
        expr: Expr,
        live_owned: tuple[ir.Value, ...],
        rooted_pcc: tuple[tuple[ir.Value, ir.Value], ...] = (),
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
        as_pcc_object: bool = False,
        as_object: bool = False,
        as_i64: bool = False,
        rooted_pcc_lifetimes: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value:
        """Evaluate a pcc-form operand while preserving outer CPython temps.

        Unlike ``_emit_checked_cpython_call_arg``, this leaves the result in
        pcc form for the starred-argument list builder.  Its only job is to
        redirect nested pcc and CPython error branches through cleanup of the
        already-live callable/arguments and rooted aggregate.
        """
        if (
            not live_owned
            and not rooted_pcc
            and not pinned_pcc
            and not rooted_pcc_lifetimes
        ):
            if as_object:
                return self._emit_as_object(expr)
            if as_i64:
                return self._emit_expr_as_i64(expr)
            if as_pcc_object:
                return self._emit_expr_as_pcc_object(expr)
            return self._emit_expr(expr)
        previous_cpy_cleanup = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        previous_pcc_target = self._current_try_err_block()
        pcc_target = previous_pcc_target
        if pcc_target is None:
            pcc_target = self._ensure_fn_err_exit()
        pcc_cleanup = self._make_cpy_operand_cleanup_block(
            live_owned,
            rooted_pcc,
            pcc_target,
            "cpy.operand.pcc.cleanup",
            pinned_pcc,
            rooted_pcc_lifetimes,
        )
        cpy_target = previous_cpy_cleanup
        if cpy_target is None:
            cpy_target = self._ensure_fn_err_exit()
        if cpy_target is pcc_target:
            cpy_cleanup = pcc_cleanup
        else:
            cpy_cleanup = self._make_cpy_operand_cleanup_block(
                live_owned,
                rooted_pcc,
                cpy_target,
                "cpy.operand.cpy.cleanup",
                pinned_pcc,
                rooted_pcc_lifetimes,
            )
        self._try_err_block = pcc_cleanup
        self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            if as_object:
                return self._emit_as_object(expr)
            if as_i64:
                return self._emit_expr_as_i64(expr)
            if as_pcc_object:
                return self._emit_expr_as_pcc_object(expr)
            return self._emit_expr(expr)
        finally:
            self._try_err_block = previous_pcc_target
            self._cpy_operand_cleanup_block = previous_cpy_cleanup

    def _release_cpy_callable_if_owned(self, fn_val: ir.Value) -> None:
        """Release a fresh callable; borrowed globals/locals stay alive."""
        if not self._cpy_value_is_owned(fn_val):
            return
        self.builder.call(self.runtime["py_cpy_decref"], [fn_val])
        self._forget_owned_cpy_value(fn_val)

    def _guard_cpy_value_not_null(
        self,
        value: ir.Value,
        owned_on_error: tuple[ir.Value, ...] = (),
        rooted_pcc_on_error: tuple[tuple[ir.Value, ir.Value], ...] = (),
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        pcc_release_on_error: tuple[ir.Value, ...] = (),
    ) -> None:
        """Stop argument evaluation when a libpython bridge returns NULL.

        CPython keeps its exception in bridge-owned thread-local state, not in
        pcc's ``py_err_occurred`` slot.  A direct pointer guard therefore has
        to run before another bridge call can clear or replace that first
        exception.  Values in ``owned_on_error`` were produced by earlier
        source operands and have not yet been transferred to a stealing
        helper.
        """
        is_null = self.builder.icmp_unsigned(
            "==",
            value,
            ir.Constant(value.type, None),
            name=self._fresh("cpy.arg.isnull"),
        )
        err_target = getattr(self, "_cpy_operand_cleanup_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        cont = self.current_function.append_basic_block(
            name=self._fresh("cpy.arg.cont"),
        )
        cleanup_values = owned_on_error
        if (
            not cleanup_values
            and not rooted_pcc_on_error
            and not pinned_pcc_on_error
            and not pcc_release_on_error
        ):
            self.builder.cbranch(is_null, err_target, cont)
        else:
            cleanup = self.current_function.append_basic_block(
                name=self._fresh("cpy.arg.err.cleanup"),
            )
            self.builder.cbranch(is_null, cleanup, cont)
            self.builder.position_at_end(cleanup)
            for owned in cleanup_values:
                self.builder.call(self.runtime["py_cpy_decref"], [owned])
            for pcc_value, root_slot in rooted_pcc_on_error:
                self._leave_container_temp_root(root_slot)
                self._gc_release(pcc_value)
            for pcc_value, release_owned in pinned_pcc_on_error:
                self._gc_unpin(pcc_value)
                if release_owned:
                    self._gc_release(pcc_value)
            for pcc_value in pcc_release_on_error:
                self._gc_release(pcc_value)
            self.builder.branch(err_target)
        self.builder.position_at_end(cont)

    def _guard_cpy_status_not_negative(
        self,
        status: ir.Value,
        owned_on_error: tuple[ir.Value, ...] = (),
        rooted_pcc_on_error: tuple[tuple[ir.Value, ir.Value], ...] = (),
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        pcc_release_on_error: tuple[ir.Value, ...] = (),
    ) -> None:
        """Branch on a CPython C-API ``-1`` status with ref cleanup."""
        failed = self.builder.icmp_signed(
            "<",
            status,
            ir.Constant(status.type, 0),
            name=self._fresh("cpy.status.failed"),
        )
        err_target = getattr(self, "_cpy_operand_cleanup_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        cont = self.current_function.append_basic_block(
            name=self._fresh("cpy.status.cont"),
        )
        if (
            not owned_on_error
            and not rooted_pcc_on_error
            and not pinned_pcc_on_error
            and not pcc_release_on_error
        ):
            self.builder.cbranch(failed, err_target, cont)
        else:
            cleanup = self.current_function.append_basic_block(
                name=self._fresh("cpy.status.err.cleanup"),
            )
            self.builder.cbranch(failed, cleanup, cont)
            self.builder.position_at_end(cleanup)
            for owned in owned_on_error:
                self.builder.call(self.runtime["py_cpy_decref"], [owned])
            for pcc_value, root_slot in rooted_pcc_on_error:
                self._leave_container_temp_root(root_slot)
                self._gc_release(pcc_value)
            for pcc_value, release_owned in pinned_pcc_on_error:
                self._gc_unpin(pcc_value)
                if release_owned:
                    self._gc_release(pcc_value)
            for pcc_value in pcc_release_on_error:
                self._gc_release(pcc_value)
            self.builder.branch(err_target)
        self.builder.position_at_end(cont)

    def _emit_cpython_call_arg(self, expr: Expr) -> tuple[ir.Value, bool]:
        """Emit one argument for a CPython call boundary.

        Lambda values need a real CPython callable wrapper here. The generic
        expression path may prefer pcc-native callable objects, which are not
        valid PyObject callables for libpython APIs such as sorted(key=...).
        """

        if isinstance(expr, Lambda):
            simple = self._maybe_emit_simple_lambda(expr)
            if simple is not None:
                return simple, True
            wrapped = self._maybe_emit_lambda_wrap(expr)
            if wrapped is not None:
                return wrapped, True
            raise NotImplementedError(
                "CPython fallback call cannot pass unsupported lambda"
            )
        v = self._emit_expr(expr)
        return self._marshal_to_cpython_consuming_source(v, expr.ty, expr)

    def _begin_cpy_operand_evaluation(self, fn_val: ir.Value) -> list[ir.Value]:
        """Guard the callable before evaluating any source operands."""
        self._guard_cpy_value_not_null(fn_val)
        if self._cpy_value_is_owned(fn_val):
            return [fn_val]
        return []

    def _emit_checked_cpython_call_arg(
        self,
        expr: Expr,
        live_owned: list[ir.Value],
        rooted_pcc_on_error: tuple[tuple[ir.Value, ir.Value], ...] = (),
    ) -> tuple[ir.Value, bool]:
        """Evaluate one operand, stopping before the next on CPython NULL."""
        live_tuple = tuple(live_owned)
        previous_cpy_cleanup = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        previous_pcc_target = self._current_try_err_block()
        if live_tuple or rooted_pcc_on_error:
            pcc_target = previous_pcc_target
            if pcc_target is None:
                pcc_target = self._ensure_fn_err_exit()
            pcc_cleanup = self._make_cpy_operand_cleanup_block(
                live_tuple,
                rooted_pcc_on_error,
                pcc_target,
                "cpy.operand.pcc.cleanup",
            )
            cpy_target = previous_cpy_cleanup
            if cpy_target is None:
                cpy_target = self._ensure_fn_err_exit()
            if cpy_target is pcc_target:
                cpy_cleanup = pcc_cleanup
            else:
                cpy_cleanup = self._make_cpy_operand_cleanup_block(
                    live_tuple,
                    rooted_pcc_on_error,
                    cpy_target,
                    "cpy.operand.cpy.cleanup",
                )
            self._try_err_block = pcc_cleanup
            self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            value, is_owned = self._emit_cpython_call_arg(expr)
        finally:
            self._try_err_block = previous_pcc_target
            self._cpy_operand_cleanup_block = previous_cpy_cleanup
        self._guard_cpy_value_not_null(
            value,
            live_tuple,
            rooted_pcc_on_error,
        )
        if is_owned:
            live_owned.append(value)
        return value, is_owned

    def _emit_cpy_call_kwdict(
        self,
        fn_val: ir.Value,
        name_hint: str,
        pos_exprs: tuple[Expr, ...],
        kwargs_expr: Expr,
    ) -> ir.Value:
        """Dispatch ``callable(*pos_exprs, **kwargs_expr)`` through a
        CPython kwargs-dict helper."""
        self._require_supported_cpy_kw_mapping(kwargs_expr)
        if self._is_starred_unpack(pos_exprs):
            return self._emit_cpy_call_list_kwdict(
                fn_val,
                name_hint,
                pos_exprs[0],
                kwargs_expr,
            )
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        n_pos = len(pos_exprs)
        pos_vals: list[ir.Value] = []
        pos_owned: list[bool] = []
        for arg in pos_exprs:
            ca, is_owned = self._emit_checked_cpython_call_arg(arg, live_owned)
            pos_vals.append(ca)
            pos_owned.append(is_owned)
        if n_pos == 0:
            pos_argv_ptr = ir.Constant(_CSTR, None)
        else:
            pos_arr_ty = ir.ArrayType(_CSTR, n_pos)
            pos_argv = self._alloca_in_entry(
                pos_arr_ty,
                name=f"cpy.pos.{name_hint}",
            )
            for i, (ca, is_owned) in enumerate(zip(pos_vals, pos_owned)):
                if not is_owned:
                    self.builder.call(
                        self.runtime["py_cpy_incref"],
                        [ca],
                    )
                    live_owned.append(ca)
                else:
                    self._forget_owned_cpy_value(ca)
                gep = self.builder.gep(
                    pos_argv,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"pos.{i}"),
                )
                self.builder.store(ca, gep)
            pos_argv_ptr = self.builder.bitcast(
                pos_argv,
                _CSTR,
                name=self._fresh("pos.p"),
            )
        kw_cpy, kw_owned = self._emit_checked_cpython_call_arg(
            kwargs_expr,
            live_owned,
        )
        result = self.builder.call(
            self.runtime["py_cpy_call_kwdict"],
            [fn_val, ir.Constant(_I64, n_pos), pos_argv_ptr, kw_cpy],
            name=self._fresh(f"cpy.callkwdict.{name_hint}"),
        )
        if kw_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [kw_cpy])
            self._forget_owned_cpy_value(kw_cpy)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_call_arglist(
        self,
        fn_val: ir.Value,
        name_hint: str,
        arg_exprs: tuple[Expr, ...],
    ) -> ir.Value:
        """Dispatch ``callable(pos..., *iters...)`` via ``py_cpy_call_list``."""
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        args_list_roots = []
        args_list = self._emit_pcc_args_list(
            arg_exprs,
            name_hint,
            cpy_live_owned=live_owned,
            cpy_temp_root_out=args_list_roots,
        )
        args_list_root = args_list_roots[0]
        result = self.builder.call(
            self.runtime["py_cpy_call_list"],
            [fn_val, args_list],
            name=self._fresh(f"cpy.callargs.{name_hint}"),
        )
        self._leave_container_temp_root(args_list_root)
        self._gc_release(args_list)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_call_arglist_kwdict(
        self,
        fn_val: ir.Value,
        name_hint: str,
        arg_exprs: tuple[Expr, ...],
        kwargs_expr: Expr,
    ) -> ir.Value:
        """Dispatch ``callable(pos..., *iters..., **mapping)`` via the
        list+kwdict helper."""
        self._require_supported_cpy_kw_mapping(kwargs_expr)
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        args_list_roots = []
        args_list = self._emit_pcc_args_list(
            arg_exprs,
            name_hint,
            cpy_live_owned=live_owned,
            cpy_temp_root_out=args_list_roots,
        )
        args_list_root = args_list_roots[0]
        kw_cpy, kw_owned = self._emit_checked_cpython_call_arg(
            kwargs_expr,
            live_owned,
            ((args_list, args_list_root),),
        )
        result = self.builder.call(
            self.runtime["py_cpy_call_list_kwdict"],
            [fn_val, args_list, kw_cpy],
            name=self._fresh(f"cpy.callargskw.{name_hint}"),
        )
        if kw_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [kw_cpy])
            self._forget_owned_cpy_value(kw_cpy)
        self._leave_container_temp_root(args_list_root)
        self._gc_release(args_list)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_call_list_kwdict(
        self,
        fn_val: ir.Value,
        name_hint: str,
        starred_call: "Call",
        kwargs_expr: Expr,
    ) -> ir.Value:
        """Dispatch ``fn(*args, **kwargs_dict)`` through a dedicated
        helper that converts the pcc container to a CPython tuple."""
        self._require_supported_cpy_kw_mapping(kwargs_expr)
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        inner = starred_call.args[0]
        iter_val = self._emit_expr_with_cpy_operand_cleanup(
            inner,
            tuple(live_owned),
        )
        iter_pcc_owned = False
        if iter_val in getattr(self, "_cpy_values", ()):
            iter_val, iter_pcc_owned = self._bridge_cpy_arglist_operand(
                iter_val,
                live_owned,
            )
        elif (
            isinstance(iter_val.type, ir.PointerType)
            and self._raw_scaffold_object_rhs_is_owned(inner)
            and self._expr_returns_owned_object(inner)
        ):
            iter_pcc_owned = True
        iter_root = None
        rooted_iter = ()
        if iter_pcc_owned:
            iter_root = self._enter_container_temp_root(
                iter_val,
                self._fresh("cpy.call.exact.args"),
            )
            rooted_iter = ((iter_val, iter_root),)
        kw_cpy, kw_owned = self._emit_checked_cpython_call_arg(
            kwargs_expr,
            live_owned,
            rooted_iter,
        )
        result = self.builder.call(
            self.runtime["py_cpy_call_list_kwdict"],
            [fn_val, iter_val, kw_cpy],
            name=self._fresh(f"cpy.calllistkw.{name_hint}"),
        )
        if kw_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [kw_cpy])
            self._forget_owned_cpy_value(kw_cpy)
        if iter_pcc_owned:
            self._leave_container_temp_root(iter_root)
            self._gc_release(iter_val)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_call_kwdict_plus(
        self,
        fn_val: ir.Value,
        name_hint: str,
        pos_exprs: tuple[Expr, ...],
        kwargs: tuple,
        kwargs_expr: Expr,
        operand_order=(),
        source_arg_exprs=(),
    ) -> ir.Value:
        """Dispatch ``callable(*pos, k=v, **mapping)`` through a helper
        that merges explicit kwargs into the mapping before the call."""
        self._require_supported_cpy_kw_mapping(kwargs_expr)
        if self._has_starred_unpack(pos_exprs):
            raise NotImplementedError(
                "CPython fallback call cannot yet preserve source order for "
                "combined *args, explicit keywords, and **mapping"
            )

        # The lift keeps ordinary keyword values in ``kwargs`` but encodes
        # ``**mapping`` operands in the positional sentinel stream.  Use the
        # Call node's explicit cross-list order metadata: parser spans only
        # preserve lines, so same-line columns cannot recover this ordering.
        mapping_positions = []
        explicit_positions = []
        for order_position, entry in enumerate(operand_order):
            kind, index = entry
            if kind == "kw":
                explicit_positions.append(order_position)
                continue
            if kind != "arg" or index < 0 or index >= len(source_arg_exprs):
                continue
            source_arg = source_arg_exprs[index]
            if (
                isinstance(source_arg, Call)
                and isinstance(source_arg.func, Name)
                and source_arg.func.ident == "**"
                and len(source_arg.args) == 1
                and not source_arg.kwargs
            ):
                mapping_positions.append(order_position)

        mapping_before_explicit = False
        if kwargs:
            if not mapping_positions or not explicit_positions:
                raise NotImplementedError(
                    "CPython fallback call is missing source operand-order "
                    "metadata for explicit keywords and **mapping"
                )
            mapping_first = mapping_positions[0]
            mapping_last = mapping_positions[-1]
            explicit_first = explicit_positions[0]
            explicit_last = explicit_positions[-1]
            if mapping_last < explicit_first:
                mapping_before_explicit = True
            elif explicit_last < mapping_first:
                mapping_before_explicit = False
            else:
                raise NotImplementedError(
                    "CPython fallback call cannot yet preserve interleaved "
                    "explicit-keyword/**mapping evaluation order"
                )

        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        pos_vals: list[ir.Value] = []
        pos_owned: list[bool] = []
        for arg in pos_exprs:
            ca, is_owned = self._emit_checked_cpython_call_arg(arg, live_owned)
            pos_vals.append(ca)
            pos_owned.append(is_owned)
        if pos_vals:
            pos_arr_ty = ir.ArrayType(_CSTR, len(pos_vals))
            pos_argv = self._alloca_in_entry(
                pos_arr_ty,
                name=f"cpy.posmix.{name_hint}",
            )
            for i, (ca, is_owned) in enumerate(zip(pos_vals, pos_owned)):
                if not is_owned:
                    self.builder.call(
                        self.runtime["py_cpy_incref"],
                        [ca],
                    )
                    # The helper has not run yet.  Until it steals this bump,
                    # a later keyword/mapping failure must release it.
                    live_owned.append(ca)
                else:
                    self._forget_owned_cpy_value(ca)
                gep = self.builder.gep(
                    pos_argv,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"posmix.{i}"),
                )
                self.builder.store(ca, gep)
            pos_argv_ptr = self.builder.bitcast(
                pos_argv,
                _CSTR,
                name=self._fresh("posmix.p"),
            )
        else:
            pos_argv_ptr = ir.Constant(_CSTR, None)

        kw_cpy = None
        kw_owned = False
        if mapping_before_explicit:
            kw_cpy, kw_owned = self._emit_checked_cpython_call_arg(
                kwargs_expr,
                live_owned,
            )

        if kwargs:
            names_arr_ty = ir.ArrayType(_CSTR, len(kwargs))
            vals_arr_ty = ir.ArrayType(_CSTR, len(kwargs))
            names_arr = self._alloca_in_entry(
                names_arr_ty,
                name=f"cpy.mixn.{name_hint}",
            )
            vals_arr = self._alloca_in_entry(
                vals_arr_ty,
                name=f"cpy.mixv.{name_hint}",
            )
            kw_vals: list[ir.Value] = []
            kw_owned_flags: list[bool] = []
            for i, (kw_name, kw_expr) in enumerate(kwargs):
                name_gv = self._cstr_global(
                    kw_name,
                    f".cpy.mixkw.{name_hint}.{i}",
                )
                ngep = self.builder.gep(
                    names_arr,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"mixn.{i}"),
                )
                self.builder.store(self._ptr_to_cstr(name_gv), ngep)
                ca, is_owned = self._emit_checked_cpython_call_arg(
                    kw_expr,
                    live_owned,
                )
                kw_vals.append(ca)
                kw_owned_flags.append(is_owned)
                vgep = self.builder.gep(
                    vals_arr,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"mixv.{i}"),
                )
                self.builder.store(ca, vgep)
            names_ptr = self.builder.bitcast(
                names_arr,
                _CSTR,
                name=self._fresh("mixn.p"),
            )
            vals_ptr = self.builder.bitcast(
                vals_arr,
                _CSTR,
                name=self._fresh("mixv.p"),
            )
        else:
            names_ptr = ir.Constant(_CSTR, None)
            vals_ptr = ir.Constant(_CSTR, None)
            kw_vals = []
            kw_owned_flags = []

        if not mapping_before_explicit:
            kw_cpy, kw_owned = self._emit_checked_cpython_call_arg(
                kwargs_expr,
                live_owned,
            )

        result = self.builder.call(
            self.runtime["py_cpy_call_kwdict_plus"],
            [
                fn_val,
                ir.Constant(_I64, len(pos_vals)),
                pos_argv_ptr,
                ir.Constant(_I64, len(kwargs)),
                names_ptr,
                vals_ptr,
                kw_cpy,
            ],
            name=self._fresh(f"cpy.callmix.{name_hint}"),
        )
        if kw_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [kw_cpy])
            self._forget_owned_cpy_value(kw_cpy)
        for ca, is_owned in zip(kw_vals, kw_owned_flags):
            if is_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [ca])
                self._forget_owned_cpy_value(ca)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_call_list(
        self,
        fn_val: ir.Value,
        name_hint: str,
        starred_call: "Call",
    ) -> ir.Value:
        """Dispatch ``fn_val(*iterable)`` through ``py_cpy_call_list``.

        ``starred_call`` is the ``Call(Name("__starred__"), (iterable,))``
        sentinel wrapping the single splat arg."""
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        inner = starred_call.args[0]
        iter_val = self._emit_expr_with_cpy_operand_cleanup(
            inner,
            tuple(live_owned),
        )
        iter_pcc_owned = False
        if iter_val in getattr(self, "_cpy_values", ()):
            iter_val, iter_pcc_owned = self._bridge_cpy_arglist_operand(
                iter_val,
                live_owned,
            )
        elif (
            isinstance(iter_val.type, ir.PointerType)
            and self._raw_scaffold_object_rhs_is_owned(inner)
            and self._expr_returns_owned_object(inner)
        ):
            iter_pcc_owned = True
        iter_root = None
        if iter_pcc_owned:
            iter_root = self._enter_container_temp_root(
                iter_val,
                self._fresh("cpy.call.exact.args"),
            )
        result = self.builder.call(
            self.runtime["py_cpy_call_list"],
            [fn_val, iter_val],
            name=self._fresh(f"cpy.calllist.{name_hint}"),
        )
        if iter_pcc_owned:
            self._leave_container_temp_root(iter_root)
            self._gc_release(iter_val)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_func_call(
        self,
        fn_val: ir.Value,
        name_hint: str,
        arg_exprs: tuple[Expr, ...],
    ) -> ir.Value:
        """Dispatch ``fn_val(args)`` via py_cpy_callN for a CPython
        callable already loaded into ``fn_val`` (e.g. from a
        ``from mod import fn`` binding). Args marshal via
        ``_marshal_to_cpython``. Shares the argv path with
        ``_emit_cpy_method_call_src``."""
        kwdict_unpack = self._split_starstar_kwargs_unpack(arg_exprs)
        if kwdict_unpack is not None:
            pos_exprs, kwargs_expr = kwdict_unpack
            if self._is_starred_unpack(pos_exprs):
                return self._emit_cpy_call_list_kwdict(
                    fn_val,
                    name_hint,
                    pos_exprs[0],
                    kwargs_expr,
                )
            if self._has_starred_unpack(pos_exprs):
                return self._emit_cpy_call_arglist_kwdict(
                    fn_val,
                    name_hint,
                    pos_exprs,
                    kwargs_expr,
                )
            return self._emit_cpy_call_kwdict(
                fn_val,
                name_hint,
                pos_exprs,
                kwargs_expr,
            )
        if self._is_starred_unpack(arg_exprs):
            return self._emit_cpy_call_list(
                fn_val,
                name_hint,
                arg_exprs[0],
            )
        if self._has_starred_unpack(arg_exprs):
            return self._emit_cpy_call_arglist(fn_val, name_hint, arg_exprs)
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        cpy_args: list[ir.Value] = []
        owned: list[bool] = []
        for arg in arg_exprs:
            cpy_arg, is_owned = self._emit_checked_cpython_call_arg(
                arg,
                live_owned,
            )
            cpy_args.append(cpy_arg)
            owned.append(is_owned)
        n = len(cpy_args)
        if n == 0:
            result = self.builder.call(
                self.runtime["py_cpy_call_noargs"],
                [fn_val],
                name=self._fresh(f"cpy.call0.{name_hint}"),
            )
        elif n == 1:
            result = self.builder.call(
                self.runtime["py_cpy_call1"],
                [fn_val, cpy_args[0]],
                name=self._fresh(f"cpy.call1.{name_hint}"),
            )
        elif n == 2:
            result = self.builder.call(
                self.runtime["py_cpy_call2"],
                [fn_val] + cpy_args,
                name=self._fresh(f"cpy.call2.{name_hint}"),
            )
        elif n == 3:
            result = self.builder.call(
                self.runtime["py_cpy_call3"],
                [fn_val] + cpy_args,
                name=self._fresh(f"cpy.call3.{name_hint}"),
            )
        else:
            ptr_arr_ty = ir.ArrayType(_CSTR, n)
            argv = self._alloca_in_entry(
                ptr_arr_ty,
                name=f"cpy.argv.{name_hint}",
            )
            for i, (ca, is_owned) in enumerate(zip(cpy_args, owned)):
                if not is_owned:
                    self.builder.call(
                        self.runtime["py_cpy_incref"],
                        [ca],
                    )
                else:
                    self._forget_owned_cpy_value(ca)
                gep = self.builder.gep(
                    argv,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"argv.{i}"),
                )
                self.builder.store(ca, gep)
            argv_p = self.builder.gep(
                argv,
                [ir.Constant(_I32, 0), ir.Constant(_I32, 0)],
                inbounds=True,
                name=self._fresh("argv.p"),
            )
            result = self.builder.call(
                self.runtime["py_cpy_call_argv"],
                [fn_val, ir.Constant(_I64, n), argv_p],
                name=self._fresh(f"cpy.callN.{name_hint}"),
            )
        if n <= 3:
            for ca, is_owned in zip(cpy_args, owned):
                if is_owned:
                    self.builder.call(self.runtime["py_cpy_decref"], [ca])
                    self._forget_owned_cpy_value(ca)
        self._release_cpy_callable_if_owned(fn_val)
        # Tag the result as a CPython value so ``print(it)`` and
        # similar downstream operations go through the conversion
        # path rather than treating the PyObject* as a pcc str.
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_method_call_src(
        self,
        mod_val: ir.Value,
        attr_name: str,
        arg_exprs: tuple[Expr, ...],
        kwargs: tuple = (),
        operand_order=(),
        receiver_owned=None,
    ) -> ir.Value:
        """Lower ``<CPython value>.method(args)`` through py_cpy_getattr
        + py_cpy_callN with scalar → CPython marshalling for typed args
        (int / float / str)."""
        # A fresh receiver may itself be the NULL result of a failed CPython
        # call.  Stop before getattr so that call's pending exception remains
        # the first failure; NULL carries no reference to release.
        self._guard_cpy_value_not_null(mod_val)
        attr_ptr = self._ptr_to_cstr(
            self._cstr_global(attr_name, f".cpy.attr.{attr_name}")
        )
        fn_val = self._mark_owned_cpy_value(
            self.builder.call(
                self.runtime["py_cpy_getattr"],
                [mod_val, attr_ptr],
                name=self._fresh(f"cpy.fn.{attr_name}"),
            )
        )
        # PyObject_GetAttr returns a new reference and, for bound methods,
        # retains whatever receiver state the callable needs.  This helper
        # therefore consumes an owned receiver immediately after the getattr,
        # on both the success and NULL-result edges.  Normal CPython-expression
        # callers use the tracked ownership bit; typed-native marshalling
        # callers pass their explicit ``owned`` result because those fresh
        # values are intentionally not entered into the expression tracker.
        if receiver_owned is None:
            receiver_owned = self._cpy_value_is_owned(mod_val)
        if receiver_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [mod_val])
            self._forget_owned_cpy_value(mod_val)

        kwdict_unpack = self._split_starstar_kwargs_unpack(arg_exprs)
        if kwdict_unpack is not None:
            pos_exprs, kwargs_expr = kwdict_unpack
            if kwargs:
                return self._emit_cpy_call_kwdict_plus(
                    fn_val,
                    attr_name,
                    pos_exprs,
                    kwargs,
                    kwargs_expr,
                    operand_order,
                    arg_exprs,
                )
            if self._is_starred_unpack(pos_exprs):
                return self._emit_cpy_call_list_kwdict(
                    fn_val,
                    attr_name,
                    pos_exprs[0],
                    kwargs_expr,
                )
            if self._has_starred_unpack(pos_exprs):
                return self._emit_cpy_call_arglist_kwdict(
                    fn_val,
                    attr_name,
                    pos_exprs,
                    kwargs_expr,
                )
            return self._emit_cpy_call_kwdict(
                fn_val,
                attr_name,
                pos_exprs,
                kwargs_expr,
            )

        if kwargs:
            return self._finish_cpy_call_kw(
                fn_val,
                attr_name,
                arg_exprs,
                kwargs,
                operand_order,
            )

        if self._is_starred_unpack(arg_exprs):
            return self._emit_cpy_call_list(
                fn_val,
                attr_name,
                arg_exprs[0],
            )
        if self._has_starred_unpack(arg_exprs):
            return self._emit_cpy_call_arglist(fn_val, attr_name, arg_exprs)

        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        # Marshal each arg from its pcc native form to a CPython PyObject*.
        # ``owned`` parallel tracks whether we created the CPython ref
        # (and therefore must decref after the call).
        cpy_args: list[ir.Value] = []
        owned: list[bool] = []
        for arg in arg_exprs:
            cpy_arg, is_owned = self._emit_checked_cpython_call_arg(
                arg,
                live_owned,
            )
            cpy_args.append(cpy_arg)
            owned.append(is_owned)

        n = len(cpy_args)
        if n == 0:
            result = self.builder.call(
                self.runtime["py_cpy_call_noargs"],
                [fn_val],
                name=self._fresh(f"cpy.call0.{attr_name}"),
            )
        elif n == 1:
            result = self.builder.call(
                self.runtime["py_cpy_call1"],
                [fn_val, cpy_args[0]],
                name=self._fresh(f"cpy.call1.{attr_name}"),
            )
        elif n == 2:
            result = self.builder.call(
                self.runtime["py_cpy_call2"],
                [fn_val] + cpy_args,
                name=self._fresh(f"cpy.call2.{attr_name}"),
            )
        elif n == 3:
            result = self.builder.call(
                self.runtime["py_cpy_call3"],
                [fn_val] + cpy_args,
                name=self._fresh(f"cpy.call3.{attr_name}"),
            )
        else:
            # Build an alloca argv[n] array and dispatch via
            # py_cpy_call_argv (PyObject_Call over a fresh tuple). The
            # runtime helper steals each argv[i] ref, so we do NOT
            # decref the owned args afterwards — only borrowed args
            # need a fresh ref (py_cpy_from_* produces one already).
            ptr_arr_ty = ir.ArrayType(_CSTR, n)
            argv = self._alloca_in_entry(
                ptr_arr_ty,
                name=f"cpy.argv.{attr_name}",
            )
            for i, (ca, is_owned) in enumerate(zip(cpy_args, owned)):
                if not is_owned:
                    # Caller-owned borrowed ref — promote to owned via
                    # ``py_cpy_incref`` so ``py_cpy_call_argv``'s
                    # ref-stealing via PyTuple_SetItem doesn't double-
                    # free the caller's handle. The bumped ref is
                    # balanced by the PyTuple_SetItem steal inside
                    # the helper.
                    self.builder.call(
                        self.runtime["py_cpy_incref"],
                        [ca],
                    )
                else:
                    self._forget_owned_cpy_value(ca)
                idx0 = ir.Constant(_I32, 0)
                idx = ir.Constant(_I32, i)
                slot = self.builder.gep(
                    argv, [idx0, idx], inbounds=True, name=self._fresh(f"argv.{i}")
                )
                self.builder.store(ca, slot)
            # Decay the array pointer to a ``ptr`` for the varargs call.
            argv_ptr = self.builder.bitcast(
                argv,
                _CSTR,
                name=self._fresh("argv.ptr"),
            )
            result = self.builder.call(
                self.runtime["py_cpy_call_argv"],
                [fn_val, ir.Constant(_I64, n), argv_ptr],
                name=self._fresh(f"cpy.calln.{attr_name}"),
            )
            # py_cpy_call_argv stole each owned ref; skip the decref
            # loop below.
            self._release_cpy_callable_if_owned(fn_val)
            self._mark_owned_cpy_value(result)
            return result

        # Release only the CPython args we owned (native scalars we
        # boxed). Borrowed DynType/CPython values keep their
        # caller-owned ref.
        for ca, is_owned in zip(cpy_args, owned):
            if is_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [ca])
                self._forget_owned_cpy_value(ca)
        self._release_cpy_callable_if_owned(fn_val)

        # Mark the result as a CPython value so downstream print/str go
        # through the conversion path.
        self._mark_owned_cpy_value(result)
        return result

    def _emit_cpy_method_call1_value(
        self,
        mod_val: ir.Value,
        attr_name: str,
        arg_val: ir.Value,
        *,
        arg_owned: bool,
        receiver_owned: bool,
    ) -> ir.Value:
        """Call one CPython method with an already-evaluated argument.

        Reflected comparisons sometimes dispatch the method on the right-hand
        CPython operand, but Python still requires evaluating the left operand
        first.  The expression-based method helper cannot accept that earlier
        value without evaluating it a second time, so this narrow value form
        owns the same acquire/call/release contract for one precomputed arg.
        """
        arg_cleanup = (arg_val,) if arg_owned else ()
        self._guard_cpy_value_not_null(mod_val, arg_cleanup)
        attr_ptr = self._ptr_to_cstr(
            self._cstr_global(attr_name, f".cpy.attr.{attr_name}")
        )
        fn_val = self._mark_owned_cpy_value(
            self.builder.call(
                self.runtime["py_cpy_getattr"],
                [mod_val, attr_ptr],
                name=self._fresh(f"cpy.fn.{attr_name}"),
            )
        )
        if receiver_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [mod_val])
            self._forget_owned_cpy_value(mod_val)
        self._guard_cpy_value_not_null(fn_val, arg_cleanup)
        result = self.builder.call(
            self.runtime["py_cpy_call1"],
            [fn_val, arg_val],
            name=self._fresh(f"cpy.call1.{attr_name}"),
        )
        if arg_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [arg_val])
            self._forget_owned_cpy_value(arg_val)
        self._release_cpy_callable_if_owned(fn_val)
        return self._mark_owned_cpy_value(result)

    def _finish_cpy_call_kw(
        self,
        fn_val: ir.Value,
        name_hint: str,
        pos_exprs: tuple[Expr, ...],
        kwargs: tuple,
        operand_order=(),
    ) -> ir.Value:
        """Dispatch a CPython callable with mixed positional + keyword
        arguments through ``py_cpy_call_kw``. Positional refs are stolen
        into the tuple; keyword refs are borrowed by PyDict_SetItem so
        we still decref our owned kw values after."""
        kwdict_unpack = self._split_starstar_kwargs_unpack(pos_exprs)
        if kwdict_unpack is not None:
            unpacked_pos, kwargs_expr = kwdict_unpack
            return self._emit_cpy_call_kwdict_plus(
                fn_val,
                name_hint,
                unpacked_pos,
                kwargs,
                kwargs_expr,
                operand_order,
                pos_exprs,
            )
        if self._has_starred_unpack(pos_exprs):
            # The current kw helper accepts a flat positional argv; feeding it
            # the pcc ``*`` sentinel would silently pass one object instead of
            # expanding the iterable.  It also cannot preserve legal source
            # forms where an explicit keyword precedes the starred operand.
            raise NotImplementedError(
                "CPython fallback call cannot yet preserve combined *args "
                "and explicit keywords"
            )
        live_owned = self._begin_cpy_operand_evaluation(fn_val)
        n_pos = len(pos_exprs)
        n_kw = len(kwargs)
        pos_vals: list[ir.Value] = []
        pos_owned: list[bool] = []
        for arg in pos_exprs:
            ca, is_owned = self._emit_checked_cpython_call_arg(arg, live_owned)
            pos_vals.append(ca)
            pos_owned.append(is_owned)
        kw_vals: list[ir.Value] = []
        kw_owned: list[bool] = []
        for _name, kv in kwargs:
            ca, is_owned = self._emit_checked_cpython_call_arg(kv, live_owned)
            kw_vals.append(ca)
            kw_owned.append(is_owned)

        # Build positional argv[n_pos]
        if n_pos == 0:
            pos_argv_ptr = ir.Constant(_CSTR, None)
        else:
            pos_arr_ty = ir.ArrayType(_CSTR, n_pos)
            pos_argv = self._alloca_in_entry(
                pos_arr_ty,
                name=f"cpy.pos.{name_hint}",
            )
            for i, (ca, is_owned) in enumerate(zip(pos_vals, pos_owned)):
                if not is_owned:
                    self.builder.call(
                        self.runtime["py_cpy_incref"],
                        [ca],
                    )
                else:
                    self._forget_owned_cpy_value(ca)
                gep = self.builder.gep(
                    pos_argv,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"pos.{i}"),
                )
                self.builder.store(ca, gep)
            pos_argv_ptr = self.builder.bitcast(
                pos_argv,
                _CSTR,
                name=self._fresh("pos.p"),
            )

        if n_kw == 0:
            names_ptr = ir.Constant(_CSTR, None)
            vals_ptr = ir.Constant(_CSTR, None)
        else:
            names_arr_ty = ir.ArrayType(_CSTR, n_kw)
            vals_arr_ty = ir.ArrayType(_CSTR, n_kw)
            names_arr = self._alloca_in_entry(
                names_arr_ty,
                name=f"cpy.kwn.{name_hint}",
            )
            vals_arr = self._alloca_in_entry(
                vals_arr_ty,
                name=f"cpy.kwv.{name_hint}",
            )
            for i, (kwn, _kv) in enumerate(kwargs):
                name_gv = self._cstr_global(
                    kwn,
                    f".cpy.kwname.{name_hint}.{i}.{kwn}",
                )
                ngep = self.builder.gep(
                    names_arr,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"kwn.{i}"),
                )
                self.builder.store(self._ptr_to_cstr(name_gv), ngep)
                vgep = self.builder.gep(
                    vals_arr,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, i)],
                    inbounds=True,
                    name=self._fresh(f"kwv.{i}"),
                )
                self.builder.store(kw_vals[i], vgep)
            names_ptr = self.builder.bitcast(
                names_arr,
                _CSTR,
                name=self._fresh("kwn.p"),
            )
            vals_ptr = self.builder.bitcast(
                vals_arr,
                _CSTR,
                name=self._fresh("kwv.p"),
            )

        result = self.builder.call(
            self.runtime["py_cpy_call_kw"],
            [
                fn_val,
                ir.Constant(_I64, n_pos),
                pos_argv_ptr,
                ir.Constant(_I64, n_kw),
                names_ptr,
                vals_ptr,
            ],
            name=self._fresh(f"cpy.callkw.{name_hint}"),
        )
        # kw_vals are borrowed by PyDict_SetItemString (refcount
        # incremented by CPython); decref any we owned.
        for ca, is_owned in zip(kw_vals, kw_owned):
            if is_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [ca])
                self._forget_owned_cpy_value(ca)
        self._release_cpy_callable_if_owned(fn_val)
        self._mark_owned_cpy_value(result)
        return result
