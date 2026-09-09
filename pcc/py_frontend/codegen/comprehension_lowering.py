"""Comprehension lowering helpers for L1CodeGen."""
from __future__ import annotations

from dataclasses import replace as _dataclass_replace
from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Assign,
    Attr,
    BoolLit,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    DictType,
    DynType,
    Expr,
    IntType,
    ListExpr,
    ListType,
    MemoryViewType,
    Name,
    SetType,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
)
from . import marshal
from .builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG
from .errors import L1CodegenError
from .for_loop_lowering import _for_prepare_owned_object_target, _for_store_owned_target
from .layer1_support import _dataclass_field_names, _dataclass_field_value


_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


class ComprehensionLoweringMixin:
    def _emit_operator_getter(self, getter_name: str, key) -> ir.Value:
        """Emit ``operator.<getter_name>(key)`` at the current builder
        position, returning the resulting CPython callable pointer.
        The result is registered in ``_cpy_values`` so downstream uses
        route through the CPython path."""
        mod_name_gv = self._cstr_global("operator", ".cpy.operator_modname")
        mod_val = self.builder.call(
            self.runtime["py_cpy_import"],
            [self._ptr_to_cstr(mod_name_gv)],
            name=self._fresh("cpy.operator"),
        )
        self._guard_cpy_value_not_null(mod_val)
        attr_gv = self._cstr_global(
            getter_name,
            f".cpy.operator.{getter_name}",
        )
        fn_val = self._mark_owned_cpy_value(
            self.builder.call(
                self.runtime["py_cpy_getattr"],
                [mod_val, self._ptr_to_cstr(attr_gv)],
                name=self._fresh(f"cpy.{getter_name}"),
            )
        )
        self.builder.call(self.runtime["py_cpy_decref"], [mod_val])
        self._guard_cpy_value_not_null(fn_val)
        # Marshal the key: str → CPython str, int → CPython int.
        if isinstance(key, int):
            key_cpy = self._mark_owned_cpy_value(
                self.builder.call(
                    self.runtime["py_cpy_from_i64"],
                    [ir.Constant(_I64, key)],
                    name=self._fresh(f"{getter_name}.key.int"),
                )
            )
        else:
            key_bytes = self._utf8_byte_values(key)
            key_gv = self._cstr_global(key, f".cpy.getter.key.{getter_name}")
            # Build CPython str from the C string. ``PyUnicode_FromString``
            # isn't exposed via the pcc ABI directly; use
            # ``py_str_new + py_cpy_from_pccstr`` as the bridge.
            pcc_str = self.builder.call(
                self.runtime["py_str_new"],
                [
                    self._ptr_to_cstr(key_gv),
                    ir.Constant(_I64, len(key_bytes)),
                ],
                name=self._fresh(f"{getter_name}.key.pccstr"),
            )
            key_cpy = self._mark_owned_cpy_value(
                self.builder.call(
                    self.runtime["py_cpy_from_pccstr"],
                    [pcc_str],
                    name=self._fresh(f"{getter_name}.key.cpystr"),
                )
            )
            self._gc_release(pcc_str)
        self._guard_cpy_value_not_null(key_cpy, (fn_val,))
        result = self.builder.call(
            self.runtime["py_cpy_call1"],
            [fn_val, key_cpy],
            name=self._fresh(f"cpy.{getter_name}.call"),
        )
        self.builder.call(self.runtime["py_cpy_decref"], [key_cpy])
        self._forget_owned_cpy_value(key_cpy)
        self.builder.call(self.runtime["py_cpy_decref"], [fn_val])
        self._forget_owned_cpy_value(fn_val)
        self._guard_cpy_value_not_null(result)
        return self._mark_owned_cpy_value(result)
    def _emit_comprehension(self, expr: Call, kind: str) -> ir.Value:
        """Lower list/set/dict comprehension sentinels into explicit
        loops over a freshly-allocated runtime container.

        The native parser lifts comprehensions to::

            _list_comp(elt,          _gen_clause(target, iter, (ifs,)), ...)
            _set_comp(elt,           _gen_clause(...), ...)
            _dict_comp(TupleExpr(k,v), _gen_clause(...), ...)

        while the CPython-AST path emits::

            __listcomp__(elt,
                         ((target, iter, (ifs,), is_async), ...))
            __setcomp__(elt,  ...)
            __dictcomp__(key, val,
                         ((target, iter, (ifs,), is_async), ...))

        Only single-generator, non-async forms with a plain ``Name``
        target are supported here.
        """
        if not isinstance(expr.func, Name):
            raise NotImplementedError("comprehension sentinel lost its name")
        sentinel = expr.func.ident
        is_native = not sentinel.startswith("__")

        # Extract the element expression + any auxiliary per-kind value.
        if kind == "dict":
            if is_native:
                if len(expr.args) < 2:
                    raise NotImplementedError(
                        "_dict_comp expects (TupleExpr(k,v), _gen_clause…)"
                    )
                elt = expr.args[0]
                if not isinstance(elt, TupleExpr) or len(elt.elems) != 2:
                    raise NotImplementedError(
                        "_dict_comp element must be TupleExpr(k, v)"
                    )
                key_expr, val_expr = elt.elems
                gen_args = expr.args[1:]
            else:  # __dictcomp__(key, val, ((...),))
                if len(expr.args) != 3:
                    raise NotImplementedError(
                        "__dictcomp__ expects (key, val, generators)"
                    )
                key_expr, val_expr, gens_tuple = expr.args
                gen_args = (gens_tuple,)
        else:
            if len(expr.args) < 2:
                raise NotImplementedError(
                    f"{sentinel} expects elt plus at least one generator"
                )
            elt_expr = expr.args[0]
            gen_args = expr.args[1:]

        # Decode generator clauses.
        def _native_gen(gen_call: Expr):
            if not (
                isinstance(gen_call, Call)
                and isinstance(gen_call.func, Name)
                and gen_call.func.ident == "_gen_clause"
                and len(gen_call.args) == 3
            ):
                return None
            target, iter_e, ifs_tuple = gen_call.args
            return target, iter_e, ifs_tuple, False

        def _cpy_gen(gen_tuple: Expr):
            if not (isinstance(gen_tuple, TupleExpr) and len(gen_tuple.elems) == 4):
                return None
            target, iter_e, ifs_tuple, is_async = gen_tuple.elems
            async_flag = isinstance(is_async, BoolLit) and is_async.value
            return target, iter_e, ifs_tuple, async_flag

        generators: list = []
        if is_native:
            for g in gen_args:
                u = _native_gen(g)
                if u is None:
                    raise NotImplementedError(f"malformed {sentinel} generator clause")
                generators.append(u)
        else:
            gens_tuple = gen_args[0]
            if not isinstance(gens_tuple, TupleExpr):
                raise NotImplementedError(
                    f"{sentinel} generators arg must be a TupleExpr"
                )
            for g in gens_tuple.elems:
                u = _cpy_gen(g)
                if u is None:
                    raise NotImplementedError(f"malformed {sentinel} generator tuple")
                generators.append(u)

        for _, _, _, is_async in generators:
            if is_async:
                raise NotImplementedError("Layer 1 comprehensions are sync-only")
        # Desugar tuple targets: ``for (a, b) in pairs`` becomes a fresh
        # scalar name + an unpack-assign that the inner body emits
        # before its own work. Stash the unpacks per-generator so the
        # innermost body in the chain below sees them at the right
        # nesting level.
        tuple_unpacks: list = []
        desugared = []
        for target, iter_e, ifs_tuple, is_async in generators:
            if isinstance(target, TupleExpr):
                tmp_name = self._fresh("comp_pair")
                # The temp carries the iter's *element* type so the
                # tuple-unpack runtime branch picks the right shape.
                elem_ty = DynType(name="dyn")
                if isinstance(iter_e.ty, ListType):
                    elem_ty = iter_e.ty.elem
                elif isinstance(iter_e.ty, TupleType) and iter_e.ty.elems:
                    first = iter_e.ty.elems[0]
                    if all(
                        _same_type_kind(e, first) and e == first
                        for e in iter_e.ty.elems
                    ):
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
                desugared.append((tmp_ref, iter_e, ifs_tuple, is_async))
                tuple_unpacks.append(unpack_stmt)
            elif isinstance(target, Name):
                desugared.append((target, iter_e, ifs_tuple, is_async))
                tuple_unpacks.append(None)
            elif isinstance(target, (Attr, Subscript)):
                tmp_name = self._fresh("comp_target")
                tmp_ref = Name(
                    span=target.span,
                    ty=DynType(name="dyn"),
                    ident=tmp_name,
                )
                assign_stmt = Assign(
                    span=target.span,
                    targets=(target,),
                    value=tmp_ref,
                    annotation=None,
                )
                desugared.append((tmp_ref, iter_e, ifs_tuple, is_async))
                tuple_unpacks.append(assign_stmt)
            else:
                raise NotImplementedError(
                    "Layer 1 comprehension target must be a Name or "
                    "TupleExpr/Attr/Subscript"
                )
        generators = desugared

        # Python 3 gives comprehensions their own scope: the loop target(s)
        # (and any names bound by a tuple-unpack target) must NOT leak into
        # the enclosing function scope, and must NOT overwrite an outer
        # variable of the same name that is read after the comprehension.
        # Collect every name that the loop bodies will bind into ``self.env``
        # so we can save the outer binding and restore it once the whole
        # comprehension has been emitted.
        comp_bound_names: set[str] = set()
        for (tgt, _iter_e, _ifs, _is_async), unpack in zip(
            generators, tuple_unpacks
        ):
            if isinstance(tgt, Name):
                comp_bound_names.add(tgt.ident)
            if isinstance(unpack, Assign):
                # A tuple-unpack target binds its element names; an
                # attr/subscript target binds nothing new (it mutates an
                # existing object), so only harvest plain Names.
                self._collect_comprehension_target_names(
                    unpack.targets[0], comp_bound_names
                )

        # CPython evaluates the OUTERMOST iterable in the *enclosing* scope
        # (it is computed once, before the comprehension scope exists), so a
        # name it references must resolve to the outer binding even when it
        # collides with a comprehension target: ``[w for w in w]`` iterates
        # the outer list ``w``. The loop-path helpers only evaluate the
        # iterable *after* the outer bindings have been dropped below, which
        # would mis-resolve such a collision into a runtime NameError.
        # Pre-evaluate the level-0 iterable (or just the offending
        # range()/enumerate() arguments, keeping those fast paths) into a
        # fresh temp while the outer bindings are still live, and point the
        # generator at the temp instead. Inner-level iterables are left
        # alone: they are evaluated inside the outer loops, where the outer
        # targets' fresh comprehension bindings are exactly what Python
        # scoping requires them to see.
        if generators:
            tgt0, iter0, ifs0, async0 = generators[0]
            iter0_safe = self._prehoist_outermost_comp_iter(
                iter0, comp_bound_names
            )
            if iter0_safe is not iter0:
                generators[0] = (tgt0, iter0_safe, ifs0, async0)

        saved_env_entries: dict[str, object] = {}
        saved_cpy_flags: dict[str, object] = {}
        saved_exact_flags: dict[str, object] = {}
        _MISSING = object()
        cpy_flags = getattr(self, "_cpy_env_flags", None)
        exact_flags = getattr(self, "_exact_int_env_flags", None)
        for nm in comp_bound_names:
            saved_env_entries[nm] = self.env.get(nm, _MISSING)
            if cpy_flags is not None:
                saved_cpy_flags[nm] = cpy_flags.get(nm, _MISSING)
            else:
                saved_cpy_flags[nm] = _MISSING
            if exact_flags is not None:
                saved_exact_flags[nm] = exact_flags.get(nm, _MISSING)
            else:
                saved_exact_flags[nm] = _MISSING
            # Drop the outer binding up front so each loop path allocates a
            # *fresh* slot for the comprehension target. Otherwise a fast
            # path that reuses an existing same-type slot (e.g. the native
            # ``range`` loop reusing an outer ``i64`` local) would write loop
            # values into the outer variable's storage and corrupt it at
            # runtime, defeating the compile-time env restore below.
            self.env.pop(nm, None)
            if cpy_flags is not None:
                cpy_flags.pop(nm, None)
            if exact_flags is not None:
                exact_flags.pop(nm, None)

        # Allocate result container.
        if kind == "list":
            container = self.builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("listcomp"),
            )
        elif kind == "set":
            container = self.builder.call(
                self.runtime["py_set_new"],
                [],
                name=self._fresh("setcomp"),
            )
        elif kind == "dict":
            container = self.builder.call(
                self.runtime["py_dict_new"],
                [],
                name=self._fresh("dictcomp"),
            )
        else:
            raise NotImplementedError(f"comprehension kind {kind!r} not supported")

        self._gc_pin(container)
        outer_error = self._current_try_err_block()
        outer_cpy_error = getattr(self, "_cpy_operand_cleanup_block", None)
        error_target = outer_error if outer_error is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (), (), error_target, "comp.result.error", ((container, True),),
        )
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            self._emit_comprehension_level(
                kind,
                container,
                generators,
                tuple_unpacks,
                0,
                elt_expr if kind != "dict" else None,
                key_expr if kind == "dict" else None,
                val_expr if kind == "dict" else None,
            )
        finally:
            self._try_err_block = outer_error
            self._cpy_operand_cleanup_block = outer_cpy_error
            # Restore the enclosing scope's bindings for every comprehension
            # target name: delete names that did not exist before the
            # comprehension, and reinstate the prior slot/type for names that
            # shadowed an outer variable. This is what makes ``x = 99;
            # [x for x in range(5)]; print(x)`` print ``99`` (Python 3),
            # instead of leaking the last loop value.
            cpy_flags = getattr(self, "_cpy_env_flags", None)
            exact_flags = getattr(self, "_exact_int_env_flags", None)
            for nm, prior in saved_env_entries.items():
                current = self.env.get(nm)
                if current is not None and self._is_valueclass_payload_type(current[2]):
                    root_alloca, _root_ir_ty, root_ty = current
                    zero = ir.Constant(_I32, 0)
                    for path in self._valueclass_payload_pointer_field_paths(root_ty):
                        indices = [zero]
                        for path_idx in path:
                            indices.append(ir.Constant(_I32, path_idx))
                        field_slot = self.builder.gep(
                            root_alloca,
                            indices,
                            inbounds=True,
                            name=self._fresh(f"{nm}.comp.clear"),
                        )
                        self.builder.store(ir.Constant(_CSTR, None), field_slot)
                if prior is _MISSING:
                    self.env.pop(nm, None)
                else:
                    self.env[nm] = prior
                if cpy_flags is not None:
                    prior_flag = saved_cpy_flags.get(nm, _MISSING)
                    if prior_flag is _MISSING:
                        cpy_flags.pop(nm, None)
                    else:
                        cpy_flags[nm] = prior_flag
                if exact_flags is not None:
                    prior_exact = saved_exact_flags.get(nm, _MISSING)
                    if prior_exact is _MISSING:
                        exact_flags.pop(nm, None)
                    else:
                        exact_flags[nm] = prior_exact
        self._gc_unpin(container)
        self._note_owned_object_value(container)
        return container

    def _collect_comprehension_target_names(self, target, out: set) -> None:
        """Collect plain-``Name`` identifiers bound by a comprehension loop
        target, recursing through nested tuple/list unpack targets. Attribute
        and subscript targets bind no new local name (they mutate an existing
        object), so they contribute nothing here."""
        if isinstance(target, Name):
            out.add(target.ident)
        elif isinstance(target, (TupleExpr, ListExpr)):
            for elem in target.elems:
                self._collect_comprehension_target_names(elem, out)

    def _comp_expr_references_names(self, node, names: set) -> bool:
        """Generic dataclass-AST walk: does ``node`` reference any of
        ``names`` via a plain ``Name`` expression? Conservative — used
        only to decide whether the outermost comprehension iterable must
        be pre-evaluated before the comp-bound names are dropped."""
        if isinstance(node, Name):
            return node.ident in names
        if isinstance(node, Expr):
            for field_name in _dataclass_field_names(node):
                if self._comp_expr_references_names(
                    _dataclass_field_value(node, field_name), names
                ):
                    return True
            return False
        if isinstance(node, tuple):
            return any(
                self._comp_expr_references_names(e, names) for e in node
            )
        return False

    def _comp_hoist_iter_temp(self, expr: Expr) -> Name:
        """Evaluate ``expr`` at the current position (the enclosing
        scope's bindings are still live) into a fresh local temp and
        return a ``Name`` reference carrying the original type, so the
        typed loop fast paths still dispatch on it."""
        tmp_ref = Name(
            span=expr.span,
            ty=expr.ty,
            ident=self._fresh("comp_iter"),
        )
        self._emit_assign(
            Assign(
                span=expr.span,
                targets=(tmp_ref,),
                value=expr,
                annotation=None,
            )
        )
        return tmp_ref

    def _prehoist_outermost_comp_iter(self, iter_e: Expr, bound: set) -> Expr:
        """Return an iterable expression that is safe to evaluate after
        the comp-bound names have been dropped from ``self.env``.

        If ``iter_e`` does not reference any comp-bound name it is
        returned unchanged. A ``range``/``xrange``/``enumerate`` call
        keeps its call shape (so the dedicated loop fast paths still
        fire) and only the offending arguments are pre-evaluated;
        anything else is pre-evaluated wholesale into a temp."""
        if not self._comp_expr_references_names(iter_e, bound):
            return iter_e
        if (
            isinstance(iter_e, Call)
            and isinstance(iter_e.func, Name)
            and iter_e.func.ident in ("range", "xrange", "enumerate")
        ):
            new_args = tuple(
                self._comp_hoist_iter_temp(a)
                if self._comp_expr_references_names(a, bound)
                else a
                for a in iter_e.args
            )
            new_kwargs = tuple(
                (
                    kw,
                    self._comp_hoist_iter_temp(v)
                    if self._comp_expr_references_names(v, bound)
                    else v,
                )
                for kw, v in iter_e.kwargs
            )
            return _dataclass_replace(
                iter_e, args=new_args, kwargs=new_kwargs
            )
        return self._comp_hoist_iter_temp(iter_e)

    def _emit_comprehension_after_bind(
        self,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        _target, _iter_e, ifs_tuple, _is_async = generators[idx]
        if_exprs: tuple = ()
        if isinstance(ifs_tuple, TupleExpr):
            if_exprs = ifs_tuple.elems
        unpack_stmt = None
        if idx < len(tuple_unpacks):
            unpack_stmt = tuple_unpacks[idx]
        if unpack_stmt is not None:
            self._emit_assign(unpack_stmt)

        if_exits: list = []
        for cond_expr in if_exprs:
            cond_b = self._emit_condition_value(cond_expr)
            keep_bb = self.current_function.append_basic_block(
                name=self._fresh(f"{kind}comp.keep"),
            )
            skip_bb = self.current_function.append_basic_block(
                name=self._fresh(f"{kind}comp.skip"),
            )
            self.builder.cbranch(cond_b, keep_bb, skip_bb)
            self.builder.position_at_end(keep_bb)
            if_exits.append(skip_bb)

        self._emit_comprehension_level(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx + 1,
            elt_expr,
            key_expr,
            val_expr,
        )

        i = len(if_exits) - 1
        while i >= 0:
            skip_bb = if_exits[i]
            if not self._builder_block_is_terminated():
                self.builder.branch(skip_bb)
            self.builder.position_at_end(skip_bb)
            i -= 1
    def _emit_enumerate_loop_in_comp(
        self,
        target: Name,
        inner_iter: Expr,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """Lower ``enumerate(xs)`` inside a comprehension.

        The comprehension's tuple-target desugar already rewrote
        ``[... for (i, x) in enumerate(xs)]`` so ``target`` is a
        fresh scalar Name carrying the iter's element type
        (``tuple[int, X]``); the ``i, x = __comp_pair__`` unpack
        statement is emitted by ``_emit_comprehension_after_bind``.

        We build a 2-element tuple per iteration and bind it to
        ``target`` so the unpack sees the expected shape.
        """
        inner_val = self._emit_expr(inner_iter)
        if inner_val in getattr(self, "_cpy_values", ()):
            iter_obj = self.builder.call(
                self.runtime["py_cpy_to_pcc_obj"],
                [inner_val],
                name=self._fresh("enum.cpy.bridge"),
            )
        elif isinstance(inner_iter.ty, DictType):
            iter_obj = self.builder.call(
                self.runtime["py_dict_keys"],
                [inner_val],
                name=self._fresh("enum.dict.keys"),
            )
        else:
            iter_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                inner_val,
                inner_iter.ty,
            )
        n_val = self.builder.call(
            self.runtime["py_obj_len"],
            [iter_obj],
            name=self._fresh("enum.len"),
        )

        target_alloca = self._alloca_in_entry(
            _CSTR,
            name=f"{target.ident}.addr",
        )
        self.env[target.ident] = (
            target_alloca,
            _CSTR,
            DynType(name="dyn"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="enum.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        fn = self.current_function
        cond_bb = fn.append_basic_block(name=self._fresh("enum.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("enum.body"))
        step_bb = fn.append_basic_block(name=self._fresh("enum.step"))
        end_bb = fn.append_basic_block(name=self._fresh("enum.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("enum.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("enum.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("enum.idx.box"),
        )
        elem_obj = self.builder.call(
            self.runtime["py_obj_getitem"],
            [iter_obj, idx_box],
            name=self._fresh("enum.elem"),
        )
        pair = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 2)],
            name=self._fresh("enum.pair.new"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [pair, ir.Constant(_I64, 0), idx_box],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [pair, ir.Constant(_I64, 1), elem_obj],
        )
        self.builder.store(pair, target_alloca)
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("enum.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("enum.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
    def _emit_comprehension_indexed(
        self,
        target: Name,
        iter_val: ir.Value,
        iter_ty,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """Indexed iteration over a typed list / tuple: same shape
        as ``_emit_for_list_index`` but the inner block advances the
        explicit comprehension context instead of a Python callback."""
        fn = self.current_function
        iter_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            iter_val,
            iter_ty,
        )
        source_owned = self._owned_release_needed(iter_val, generators[idx][1])
        source_root = self._enter_container_temp_root(iter_obj, self._fresh("comp.source"))
        outer_error = self._current_try_err_block()
        outer_cpy_error = getattr(self, "_cpy_operand_cleanup_block", None)
        cleanup_bb = fn.append_basic_block(self._fresh("comp.loop.error"))
        self._try_err_block = cleanup_bb
        self._cpy_operand_cleanup_block = cleanup_bb
        if isinstance(iter_ty, ListType):
            len_helper = "py_list_len"
            get_helper = "py_list_get"
            elem_ty = iter_ty.elem
        else:
            len_helper = "py_tuple_len"
            get_helper = "py_tuple_get"
            elem_ty = DynType(name="dyn")
        n_val = self.builder.call(
            self.runtime[len_helper],
            [iter_obj],
            name=self._fresh("comp.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="comp.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        target_ident = target.ident
        target_ir_ty = self._storage_ir_type(elem_ty)
        if isinstance(target_ir_ty, ir.PointerType):
            target_slot = _for_prepare_owned_object_target(self, target_ident, elem_ty)
            alloca = target_slot[0]
        else:
            target_slot = None
            alloca = self._alloca_in_entry(target_ir_ty, name=f"{target_ident}.addr")
            self.env[target_ident] = (alloca, target_ir_ty, elem_ty)

        cond_bb = fn.append_basic_block(name=self._fresh("comp.idx.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("comp.idx.body"))
        step_bb = fn.append_basic_block(name=self._fresh("comp.idx.step"))
        end_bb = fn.append_basic_block(name=self._fresh("comp.idx.end"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("comp.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("comp.cond"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)
        elem_obj = self.builder.call(
            self.runtime[get_helper],
            [iter_obj, cur],
            name=self._fresh("comp.elem"),
        )
        self._emit_post_call_err_check(None)
        if target_slot is not None:
            _for_store_owned_target(self, target_ident, target_slot, elem_obj)
        else:
            native_val = None
            if self._is_valueclass_payload_type(elem_ty):
                native_val = self._emit_object_to_valueclass_payload(
                    elem_obj,
                    elem_ty,
                )
            if native_val is None:
                native_val = marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    elem_obj,
                    elem_ty,
                )
            self.builder.store(native_val, alloca)
            self._gc_release(elem_obj)
        if self._is_valueclass_payload_type(elem_ty):
            self._ensure_valueclass_payload_gc_roots(
                target_ident,
                alloca,
                elem_ty,
            )
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)
        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("comp.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("comp.idx.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)
        self._try_err_block = outer_error
        self._cpy_operand_cleanup_block = outer_cpy_error
        self.builder.position_at_end(cleanup_bb)
        if target_slot is not None:
            self.builder.call(self.runtime["pcc_gc_store_root"], [self._as_gc_ptr(alloca), ir.Constant(_CSTR, None)])
            flag = self._ensure_owned_local_flag(target_ident, alloca)
            self.builder.store(ir.Constant(_I1, 0), flag)
        self._release_rooted_pcc_lifetimes(((source_root, source_owned),))
        self.builder.branch(outer_error if outer_error is not None else self._ensure_fn_err_exit())
        self.builder.position_at_end(end_bb)
        if target_slot is not None:
            self.builder.call(self.runtime["pcc_gc_store_root"], [self._as_gc_ptr(alloca), ir.Constant(_CSTR, None)])
            flag = self._ensure_owned_local_flag(target_ident, alloca)
            self.builder.store(ir.Constant(_I1, 0), flag)
        self._release_rooted_pcc_lifetimes(((source_root, source_owned),))
    def _emit_comprehension_obj_indexed(
        self,
        target: Name,
        iter_val: ir.Value,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """Generic DynType iteration via ``py_obj_len`` +
        ``py_obj_getitem`` — mirrors ``_emit_for_obj_index``."""
        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_obj_len"],
            [iter_val],
            name=self._fresh("comp.obj.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="comp.obj.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        target_ident = target.ident
        alloca = self._alloca_in_entry(
            _CSTR,
            name=f"{target_ident}.addr",
        )
        self.env[target_ident] = (alloca, _CSTR, DynType(name="dyn"))

        cond_bb = fn.append_basic_block(name=self._fresh("comp.obj.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("comp.obj.body"))
        step_bb = fn.append_basic_block(name=self._fresh("comp.obj.step"))
        end_bb = fn.append_basic_block(name=self._fresh("comp.obj.end"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("comp.obj.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("comp.obj.cond"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("comp.obj.idx.box"),
        )
        elem = self.builder.call(
            self.runtime["py_obj_getitem"],
            [iter_val, idx_box],
            name=self._fresh("comp.obj.elem"),
        )
        self.builder.store(elem, alloca)
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)
        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("comp.obj.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("comp.obj.idx.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
    def _emit_comprehension_obj_iterator(
        self,
        target: Name,
        iter_val: ir.Value,
        iter_ty: Type,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """Generic object iteration via ``iter(obj)`` / ``next(it)``."""
        fn = self.current_function
        if not isinstance(iter_val.type, ir.PointerType):
            iter_val = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                iter_val,
                iter_ty,
            )
        iterator = self.builder.call(
            self.runtime["py_obj_iter"],
            [iter_val],
            name=self._fresh("comp.iter.obj"),
        )
        self._emit_post_call_err_check(getattr(target, "span", None))

        target_ident = target.ident
        alloca = self._alloca_in_entry(
            _CSTR,
            name=f"{target_ident}.addr",
        )
        self.env[target_ident] = (alloca, _CSTR, DynType(name="dyn"))

        header_bb = fn.append_basic_block(name=self._fresh("comp.iter.next"))
        body_bb = fn.append_basic_block(name=self._fresh("comp.iter.body"))
        maybe_end_bb = fn.append_basic_block(name=self._fresh("comp.iter.maybe_end"))
        clear_bb = fn.append_basic_block(name=self._fresh("comp.iter.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("comp.iter.propagate"))
        end_bb = fn.append_basic_block(name=self._fresh("comp.iter.end"))

        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        item = self.builder.call(
            self.runtime["py_obj_next"],
            [iterator],
            name=self._fresh("comp.iter.item"),
        )
        is_null = self.builder.icmp_unsigned(
            "==",
            item,
            ir.Constant(_CSTR, None),
            name=self._fresh("comp.iter.null"),
        )
        self.builder.cbranch(is_null, maybe_end_bb, body_bb)

        self.builder.position_at_end(body_bb)
        self.builder.store(item, alloca)
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(header_bb)

        self.builder.position_at_end(maybe_end_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("comp.iter.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, _BUILTIN_EXC_TAG["StopIteration"])],
            name=self._fresh("comp.iter.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("comp.iter.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("comp.iter.stop_i1"),
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)

        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(end_bb)

        self.builder.position_at_end(propagate_bb)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(end_bb)
    def _emit_comprehension_generator(
        self,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """Emit one generator level for an explicit comprehension
        context. Supports ``range(...)`` iters,
        ``enumerate(xs)`` (desugar to indexed loop with a synthetic
        counter), CPython iterables, typed list / tuple / dict
        containers, and generic DynType containers via ``py_obj_len``
        + ``py_obj_getitem``."""
        target, iter_e, _ifs_tuple, _is_async = generators[idx]
        # Fast path: range(...) iter.
        if (
            isinstance(iter_e, Call)
            and isinstance(iter_e.func, Name)
            and iter_e.func.ident in ("range", "xrange")
        ):
            self._emit_range_loop(
                target,
                iter_e,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        # enumerate(xs) — the comprehension tuple-target desugaring
        # above synthesises ``__comp_pair__`` whose value we build
        # here as a ``(i, xs_elem)`` pair to feed into the unpack.
        if (
            isinstance(iter_e, Call)
            and isinstance(iter_e.func, Name)
            and iter_e.func.ident == "enumerate"
            and len(iter_e.args) == 1
            and not iter_e.kwargs
        ):
            enumerate_source_ty = iter_e.args[0].ty
            enumerate_indexed = isinstance(
                enumerate_source_ty,
                (
                    ListType,
                    TupleType,
                    DictType,
                    StrType,
                    BytesType,
                    ByteArrayType,
                    MemoryViewType,
                ),
            ) or (
                isinstance(enumerate_source_ty, ClassType)
                and enumerate_source_ty.name in (
                    "bytes",
                    "bytearray",
                    "memoryview",
                )
            )
            if enumerate_indexed:
                self._emit_enumerate_loop_in_comp(
                    target,
                    iter_e.args[0],
                    kind,
                    container,
                    generators,
                    tuple_unpacks,
                    idx,
                    elt_expr,
                    key_expr,
                    val_expr,
                )
                return
        iter_val = self._emit_expr(iter_e)
        if iter_val in getattr(self, "_cpy_values", ()):
            self._emit_cpy_iter_loop(
                target,
                iter_val,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        iter_ty = iter_e.ty
        if isinstance(iter_ty, (ListType, TupleType)):
            self._emit_comprehension_indexed(
                target,
                iter_val,
                iter_ty,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        if isinstance(iter_ty, DictType):
            keys_val = self.builder.call(
                self.runtime["py_dict_keys"],
                [iter_val],
                name=self._fresh("comp.dict.keys"),
            )
            self._note_owned_object_value(keys_val)
            self._gc_release_if_owned(iter_val, iter_e)
            synthetic_ty = ListType(name="list", elem=iter_ty.key)
            self._emit_comprehension_indexed(
                target,
                keys_val,
                synthetic_ty,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        if isinstance(iter_ty, StrType):
            self._emit_comprehension_str_chars(
                target,
                iter_val,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        if isinstance(iter_ty, (BytesType, ByteArrayType, MemoryViewType)) or (
            isinstance(iter_ty, ClassType)
            and iter_ty.name in ("bytes", "bytearray", "memoryview")
        ):
            self._emit_comprehension_obj_indexed(
                target,
                iter_val,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        if isinstance(iter_ty, (DynType, SetType)):
            # Iterate DynType sources via the iterator protocol
            # (py_obj_iter/py_obj_next), matching the statement for-loop and
            # the ClassType arm below. The older len+py_obj_getitem path
            # (_emit_comprehension_obj_indexed) silently produced an empty
            # result for iterator-only objects such as generators, which have
            # no length / __getitem__. See
            # docs/investigations/sequence-builtins-len-getitem-not-iterator-protocol.md
            self._emit_comprehension_obj_iterator(
                target,
                iter_val,
                iter_ty,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        if isinstance(iter_ty, ClassType):
            self._emit_comprehension_obj_iterator(
                target,
                iter_val,
                iter_ty,
                kind,
                container,
                generators,
                tuple_unpacks,
                idx,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        span = getattr(iter_e, "span", None)
        where = f" at {span.file}:{span.line}:{span.col}" if span is not None else ""
        raise NotImplementedError(
            "Layer 1 comprehension iter must be range(...) or a "
            "CPython-backed iterable; got "
            f"{type(iter_ty).__name__}(name={getattr(iter_ty, 'name', '?')!r})"
            f"{where}"
        )
    def _emit_comprehension_str_chars(
        self,
        target: Name,
        iter_val: ir.Value,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        """StrType comprehension iter: slice each char."""
        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_str_len"],
            [iter_val],
            name=self._fresh("comp.str.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name="comp.str.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)
        one_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, 1)],
            name=self._fresh("comp.str.step"),
        )
        tgt_name = target.ident
        if tgt_name not in self.env:
            alloca = self._alloca_in_entry(
                _CSTR,
                name=f"{tgt_name}.addr",
            )
            self.env[tgt_name] = (alloca, _CSTR, StrType(name="str"))

        cond_bb = fn.append_basic_block(name=self._fresh("comp.str.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("comp.str.body"))
        step_bb = fn.append_basic_block(name=self._fresh("comp.str.step_bb"))
        end_bb = fn.append_basic_block(name=self._fresh("comp.str.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("comp.str.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh("comp.str.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        lo_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("comp.str.lo"),
        )
        hi = self.builder.add(
            cur,
            ir.Constant(_I64, 1),
            name=self._fresh("comp.str.hi.i64"),
        )
        hi_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [hi],
            name=self._fresh("comp.str.hi"),
        )
        ch = self.builder.call(
            self.runtime["py_str_slice"],
            [iter_val, lo_box, hi_box, one_box],
            name=self._fresh("comp.str.ch"),
        )
        alloca, _, _ = self.env[tgt_name]
        self.builder.store(ch, alloca)
        self._emit_comprehension_after_bind(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )
        if not self._builder_block_is_terminated():
            self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("comp.str.idx2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("comp.str.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
