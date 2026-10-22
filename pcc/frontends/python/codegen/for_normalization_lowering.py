"""For-loop AST normalization helpers for Layer-1 Python codegen."""
from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Assign, AugAssign, Call, DictType, DynType, Expr, For, IntLit, IntType, ListType, Name, Subscript, TupleExpr, TupleType, Type
from pcc.frontends.python.codegen.errors import L1CodegenError


_I64 = ir.IntType(64)


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


def _for_has_attr(obj, name: str) -> bool:
    return hasattr(obj, name)


def _for_type_name(obj) -> str:
    try:
        return str(obj.ty.name)
    except AttributeError:
        return ""


def _for_is_name(obj) -> bool:
    return isinstance(obj, Name) or _for_has_attr(obj, "ident")


def _for_is_tuple_expr(obj) -> bool:
    if isinstance(obj, TupleExpr):
        return True
    ty_name = _for_type_name(obj)
    return _for_has_attr(obj, "elems") and (
        ty_name == "tuple" or ty_name == "tuple_variadic"
    )


def _for_is_call(obj) -> bool:
    return isinstance(obj, Call) or (
        _for_has_attr(obj, "func")
        and _for_has_attr(obj, "args")
        and _for_has_attr(obj, "kwargs")
    )


def _for_is_call_name(obj, name: str) -> bool:
    if not _for_is_call(obj):
        return False
    try:
        func = obj.func
        return _for_is_name(func) and func.ident == name
    except AttributeError:
        return False


class ForNormalizationLoweringMixin:
    def _for_iter_is_enumerate(self, stmt: For) -> bool:
        it = stmt.iter
        if not _for_is_call_name(it, "enumerate"):
            return False
        if len(it.args) == 1 and not it.kwargs:
            return True
        # ``enumerate(iterable, start)`` / ``enumerate(iterable, start=N)``
        # — second arg is the starting index. Accept any literal/int
        # expression; the codegen adds the offset to the counter.
        if len(it.args) == 2 and not it.kwargs:
            return True
        if len(it.args) == 1 and len(it.kwargs) == 1:
            (kn, _kv) = it.kwargs[0]
            return kn == "start"
        return False

    def _for_iter_is_zip(self, stmt: For) -> bool:
        """``for <...> in zip(xs, ys, ...):`` — plain positional zips only.

        Any kwarg (in practice ``strict=``) declines the rewrite: this
        indexed-loop form has nowhere to put the equal-length enforcement,
        so the generic ``zip`` builtin — the single owner of the strict
        semantics — materialises the pairs and raises ValueError on
        mismatch."""
        it = stmt.iter
        if not (
            _for_is_call_name(it, "zip")
            and len(it.args) >= 2
        ):
            return False
        if it.kwargs:
            return False
        for source in it.args:
            source_ty = source.ty
            if isinstance(source_ty, DictType) or getattr(
                source_ty, "name", ""
            ) == "dict":
                # This rewrite indexes every source positionally as
                # ``source[i]``, and for a mapping that is a KEY lookup for
                # 0, 1, 2 …  Iterating a mapping yields its keys, so
                # ``zip(d, ...)`` raised KeyError here.  Wrapping the source
                # in ``list(...)`` would rebuild the key list on every
                # iteration, so decline the rewrite instead and let the
                # generic ``zip`` builtin — which normalises mappings once —
                # materialise the pairs.
                return False
        return True

    def _normalise_for_zip(self, stmt: For) -> For:
        """Rewrite ``for (a, b, ...) in zip(xs, ys, ...):`` into::

            for __zip_i__<k> in range(min(len(xs), len(ys), ...)):
                (a, b, ...) = (xs[__zip_i__<k>], ys[__zip_i__<k>], ...)
                <orig body>

        Accepts any tuple-arity on the target (pcc normalises tuple
        targets further down the pipeline) and any iterable count.
        """
        it = stmt.iter
        assert isinstance(it, Call)
        xs_list = it.args
        span = stmt.span
        int_ty = IntType(name="int")
        idx_name = self._fresh("zip_i")
        idx_ref = Name(span=span, ty=int_ty, ident=idx_name)

        # Build ``min(len(xs0), len(xs1), ...)``.
        def _len_call(e):
            return Call(
                span=span,
                ty=int_ty,
                func=Name(span=span, ty=DynType(name="dyn"), ident="len"),
                args=(e,),
                kwargs=(),
            )

        if len(xs_list) == 1:
            stop_expr = _len_call(xs_list[0])
        else:
            # ``min(a, b, c, ...)`` — only the 2-arg form is wired as a
            # builtin fast path, so chain it left-associatively.
            stop_expr = _len_call(xs_list[0])
            for rest in xs_list[1:]:
                stop_expr = Call(
                    span=span,
                    ty=int_ty,
                    func=Name(span=span, ty=DynType(name="dyn"), ident="min"),
                    args=(stop_expr, _len_call(rest)),
                    kwargs=(),
                )
        # ``range(stop_expr)`` drives the indexed walk.
        new_iter = Call(
            span=span,
            ty=DynType(name="dyn"),
            func=Name(span=span, ty=DynType(name="dyn"), ident="range"),
            args=(stop_expr,),
            kwargs=(),
        )
        # Build ``(a, b, ...) = (xs0[i], xs1[i], ...)`` prelude.
        # Derive each subscript's type from its list/tuple elem type so
        # downstream store code picks the correct i64/ptr slot rather
        # than defaulting to DynType (which would mix ptr and i64 in
        # the same alloca).
        def _subscript_ty(xs: Expr) -> Type:
            xt = xs.ty
            if isinstance(xt, ListType):
                return xt.elem
            if isinstance(xt, TupleType) and xt.elems:
                # Assume homogenous for zip purposes — falls back to
                # the first element type which is usually correct.
                return xt.elems[0]
            return DynType(name="dyn")

        pair_elems = tuple(
            Subscript(
                span=span,
                ty=_subscript_ty(xs),
                obj=xs,
                idx=idx_ref,
            )
            for xs in xs_list
        )
        if _for_is_tuple_expr(stmt.target):
            # Re-use the existing tuple target.
            assign_unpack = Assign(
                span=span,
                targets=(stmt.target,),
                value=TupleExpr(
                    span=span,
                    ty=TupleType(
                        name="tuple",
                        elems=tuple(e.ty for e in pair_elems),
                    ),
                    elems=pair_elems,
                ),
                annotation=None,
            )
        elif _for_is_name(stmt.target):
            # ``for pair in zip(...):`` — bind whole tuple.
            assign_unpack = Assign(
                span=span,
                targets=(stmt.target,),
                value=TupleExpr(
                    span=span,
                    ty=TupleType(
                        name="tuple",
                        elems=tuple(e.ty for e in pair_elems),
                    ),
                    elems=pair_elems,
                ),
                annotation=None,
            )
        else:
            raise NotImplementedError(
                "zip() target must be a Name or TupleExpr of Names"
            )
        new_body = (assign_unpack,) + tuple(stmt.body)
        return For(
            span=stmt.span,
            target=idx_ref,
            iter=new_iter,
            body=new_body,
            else_body=stmt.else_body,
            is_async=stmt.is_async,
        )

    def _normalise_for_enumerate(self, stmt: For) -> For:
        """Rewrite ``for <target> in enumerate(xs):`` into the
        equivalent manually-indexed loop::

            __enum_i__<k> = 0
            for <target-sans-index> in xs:
                <target> = (__enum_i__<k>, <target-sans-index>)
                <orig body>
                __enum_i__<k> = __enum_i__<k> + 1

        The synthetic counter is an annotated int so inference keeps
        it on the native path; tuple-target unpacking picks up the
        rest via the existing ``_normalise_for_tuple_target`` helper.
        """
        it = stmt.iter
        assert isinstance(it, Call)
        inner_iter = it.args[0]
        # ``enumerate(iter, start)`` — optional start value, positional
        # or keyword. Evaluate at loop entry and seed the counter with it.
        start_expr = None
        if len(it.args) == 2:
            start_expr = it.args[1]
        elif len(it.kwargs) == 1 and it.kwargs[0][0] == "start":
            start_expr = it.kwargs[0][1]
        span = stmt.span
        int_ty = IntType(name="int")
        # Inside a generator the running counter must live in a persisted
        # frame slot (reserved by _collect_generator_frame_names under the
        # deterministic _generator_enum_cnt_name); use that name and a boxed
        # (DynType) counter so it survives yields.  Otherwise the counter is a
        # raw entry-block alloca that resets to NULL on resume, leaving the
        # enumerate index as ``<null>`` after the first item.  Outside a
        # generator keep the unboxed native-int fast path.
        in_generator = len(self._generator_ctx_stack) > 0
        if in_generator:
            cnt_name = self._generator_enum_cnt_name(stmt)
            cnt_ty: Type = DynType(name="dyn")
        else:
            cnt_name = self._fresh("enum_i")
            cnt_ty = int_ty
        one_lit = IntLit(span=span, ty=int_ty, value=1)
        cnt_ref = Name(span=span, ty=cnt_ty, ident=cnt_name)

        # Insert the counter init *before* the for-loop itself by
        # synthesising an Assign statement and prepending it to the
        # caller's scope. Since we can't rewrite the surrounding
        # body here, fold the init into the pre-loop region by
        # stashing it on the codegen — the emitter will see it on
        # the next ``_emit_stmt`` entry. That infra is intrusive;
        # instead, emit the init *inline* here via a direct alloca
        # + store, side-stepping the need to touch the parent list.
        # The tuple-normaliser runs after us, so pre-loop allocation
        # inside the body avoids re-running inside each iteration.
        # We use a dedicated "pre-loop bootstrap" list on ``self``.
        # Simplest: add the init as the first statement of the new
        # stmt's body. This re-inits the counter every iteration —
        # wrong. Instead, emit the init via a small runtime routine
        # using an explicit @_pcc_py_* helper. Since that's overkill
        # for a desugar, we register a per-loop alloca outside the
        # body using an auxiliary stash consumed by _emit_stmts.
        #
        # Pragmatic approach: leave the alloca/store emission to
        # ``_emit_for`` itself, and only rewrite the AST to handle
        # the bookkeeping. Hand the ``_emit_for`` a counter name
        # plus target binding via a side-channel on ``stmt``.
        # Synthesize a target Name for the "value" slot.
        if _for_is_tuple_expr(stmt.target) and len(stmt.target.elems) == 2:
            idx_target, val_target = stmt.target.elems
            if not _for_is_name(idx_target):
                raise NotImplementedError("enumerate() index target must be a Name")
            assign_idx = Assign(
                span=span,
                targets=(idx_target,),
                value=cnt_ref,
                annotation=(None if in_generator else int_ty),
            )
            if _for_is_name(val_target):
                new_target = val_target
                prelude = (assign_idx,)
            elif _for_is_tuple_expr(val_target):
                # ``for i, (a, b) in enumerate(xs):`` — introduce a
                # synthetic single-Name target for the outer loop,
                # then unpack that Name into the user's TupleExpr
                # before the body runs.
                fresh_name = self._fresh("enum_val_pair")
                new_target = Name(
                    span=span,
                    ty=DynType(name="dyn"),
                    ident=fresh_name,
                )
                unpack_inner = Assign(
                    span=span,
                    targets=(val_target,),
                    value=new_target,
                    annotation=None,
                )
                prelude = (assign_idx, unpack_inner)
            else:
                raise NotImplementedError(
                    "enumerate() value target must be a Name or TupleExpr"
                )
        elif _for_is_name(stmt.target):
            # ``for pair in enumerate(xs):`` — bind the whole tuple.
            tup_target = stmt.target
            val_name = self._fresh("enum_val")
            val_target = Name(span=span, ty=DynType(name="dyn"), ident=val_name)
            pair_expr = TupleExpr(
                span=span,
                ty=TupleType(name="tuple", elems=(int_ty, val_target.ty)),
                elems=(cnt_ref, val_target),
            )
            assign_pair = Assign(
                span=span,
                targets=(tup_target,),
                value=pair_expr,
                annotation=None,
            )
            new_target = val_target
            prelude = (assign_pair,)
        else:
            raise NotImplementedError(
                "enumerate() target must be a Name or (Name, Name)"
            )

        incr_stmt = AugAssign(
            span=span,
            target=cnt_ref,
            op="+=",
            value=one_lit,
        )
        # The increment runs right after the index binding, BEFORE the
        # user body: a ``continue`` in the body must not skip it, or the
        # counter stops matching the iteration number.
        new_body = prelude + (incr_stmt,) + tuple(stmt.body)

        # Emit the counter alloca + zero-store *now* so the rewritten
        # for-loop body sees ``__enum_i__`` already bound in ``self.env``.
        # Requires an active IRBuilder; _emit_for is called during
        # body lowering so the builder is positioned on the enclosing
        # block.
        if start_expr is None:
            start_i64 = ir.Constant(_I64, 0)
        else:
            start_i64 = self._emit_expr_as_i64(start_expr)
        if in_generator:
            # The frame slot for cnt_name was already created by the resume
            # function (cnt_name is a frame_name); initialise it in place with
            # a boxed int.  This init runs once on first entry; on resume the
            # dispatch jumps to the post-yield block and reloads the spilled
            # (incremented) value from the heap frame, skipping this store.
            slot_entry = self.env.get(cnt_name)
            if slot_entry is None:
                raise L1CodegenError(
                    "generator enumerate counter missing frame slot "
                    f"{cnt_name!r}"
                )
            start_box = self.builder.call(
                self.runtime["py_int_from_i64"],
                [start_i64],
                name=self._fresh("enum.start.box"),
            )
            self.builder.store(start_box, slot_entry[0])
        else:
            ir_ty = self._storage_ir_type(int_ty)
            alloca = self._alloca_in_entry(ir_ty, name=f"{cnt_name}.addr")
            start_val = start_i64
            if isinstance(ir_ty, ir.PointerType):
                start_val = self.builder.call(
                    self.runtime["py_int_from_i64"],
                    [start_val],
                    name=self._fresh("enum.start.box"),
                )
            self.builder.store(start_val, alloca)
            self.env[cnt_name] = (alloca, ir_ty, int_ty)

        return For(
            span=stmt.span,
            target=new_target,
            iter=inner_iter,
            body=new_body,
            else_body=stmt.else_body,
            is_async=stmt.is_async,
        )

    def _normalise_for_tuple_target(self, stmt: For) -> For:
        """Rewrite ``for (a, b) in items:`` into::

            for __foritem__<k> in items:
                a, b = __foritem__<k>
                <original body>

        The fresh Name carries the iter's element type so the existing
        tuple-unpack assignment codegen (literal / runtime branch)
        picks the right shape.
        """
        target = stmt.target
        assert _for_is_tuple_expr(target)
        tmp_name = self._fresh("foritem")
        # Unlike a user loop target, this owner is needed only while the
        # leading unpack publishes its elements. Keeping the final tuple in
        # the synthetic slot extends every element's lifetime past the loop.
        self._for_unpack_temporary_names.add(tmp_name)
        iter_ty = stmt.iter.ty
        elem_ty: Type = DynType(name="dyn")
        if isinstance(iter_ty, ListType):
            elem_ty = iter_ty.elem
        elif isinstance(iter_ty, TupleType) and iter_ty.elems:
            first = iter_ty.elems[0]
            if all(_same_type_kind(e, first) and e == first for e in iter_ty.elems):
                elem_ty = first
        tmp_ref = Name(
            span=target.span,
            ty=elem_ty,
            ident=tmp_name,
        )
        unpack_stmt = Assign(
            span=target.span,
            targets=(target,),
            value=tmp_ref,
            annotation=None,
        )
        new_body = (unpack_stmt,) + tuple(stmt.body)
        return For(
            span=stmt.span,
            target=tmp_ref,
            iter=stmt.iter,
            body=new_body,
            else_body=stmt.else_body,
            is_async=stmt.is_async,
        )
