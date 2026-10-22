"""Call argument resolution and direct-call unpack lowering."""

from __future__ import annotations

from typing import Optional

from pcc.frontends.python.py_ast import (
    BoolLit,
    Call,
    DictExpr,
    DictType,
    DynType,
    Expr,
    FuncDef,
    IntLit,
    IntType,
    ListType,
    Name,
    NoneLit,
    NoneType,
    RawPointerType,
    SourceSpan,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    UnaryOp,
    ValueArrayType,
)
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.str_default_cells import (
    funcdef_defined_here,
    str_default_cell_ref,
)


def _call_kwargs_merge(span, operands):
    """``__pcc_kwargs_merge__(m1, m2, ...)``: call keywords merged in order,
    a repeated key raising CPython's TypeError (see ``_emit_kwargs_merge``)."""
    return Call(
        span=span,
        ty=DictType(
            name="dict",
            key=StrType(name="str"),
            value=DynType(name="dyn"),
        ),
        func=Name(span, DynType(name="dyn"), "__pcc_kwargs_merge__"),
        args=operands,
        kwargs=(),
    )


# The tagged small-int lane, as LiteralLoweringMixin._STATIC_TAGGED_INT_MIN
# and _MAX: py_int_from_i64 encodes these values without allocating.
# Spelled as expressions: an over-lane literal is a bignum constant, which
# tests/python/test_selfhost_no_over_lane_int_literals.py keeps out of the
# compiler source.
_TAGGED_INT_MIN = -(1 << 62)
_TAGGED_INT_MAX = (1 << 62) - 1


def _static_default_literal(expr, local_strings: bool = False) -> bool:
    """A default the call site can rebuild as the very object it binds.

    Python evaluates a default once and binds that same object on every
    call. None, a bool and a tagged small int are one object wherever they
    are produced. A str literal is one pooled object per module, so the
    defining module (``local_strings``) rebuilds it exactly, and another
    module loads the owner's object from its cell (``str_default_cells``).
    A float, bytes, tuple or heap int literal is a separate object per
    producer, so its identity belongs to the binder.
    """
    if isinstance(expr, (NoneLit, BoolLit)):
        return True
    if local_strings and isinstance(expr, StrLit):
        return True
    if str_default_cell_ref(expr) is not None:
        return True
    value = None
    if isinstance(expr, IntLit):
        value = int(expr.value)
    elif (isinstance(expr, UnaryOp) and expr.op in ("-", "+")
            and isinstance(expr.operand, IntLit)):
        value = int(expr.operand.value)
        if expr.op == "-":
            value = -value
    if value is None:
        return False
    return _TAGGED_INT_MIN <= value <= _TAGGED_INT_MAX


def _statically_bindable_call(args, kwargs, formals, skip_self, local_strings: bool = False) -> bool:
    """True when a keyword/default call binds exactly as the runtime binder.

    Each keyword names a distinct formal that accepts a keyword, keywords
    appear in formal order (so direct formal-order evaluation keeps Python's
    left-to-right argument order), and every unbound formal has a default
    that ``_static_default_literal`` accepts. Anything else, including calls
    that raise TypeError, stays with the published callable's binder.
    """
    params = []
    for index, formal in enumerate(formals):
        if skip_self and index == 0 or formal.name == "":
            continue
        if formal.kind not in ("pos", "pos_only", "kw_only"):
            return False
        params.append(formal)
    positional = 0
    for formal in params:
        if formal.kind == "kw_only":
            break
        positional += 1
    if len(args) > positional:
        return False
    bound = [False] * len(params)
    for index in range(len(args)):
        bound[index] = True
    previous = len(args) - 1
    for name, _value in kwargs:
        target = -1
        for index, formal in enumerate(params):
            if formal.name == name and formal.kind != "pos_only":
                target = index
                break
        if target < 0 or bound[target] or target < previous:
            return False
        bound[target] = True
        previous = target
    for index, formal in enumerate(params):
        if bound[index]:
            continue
        if not formal.has_default or formal.default is None:
            return False
        if not _static_default_literal(formal.default, local_strings):
            return False
    return True


class CallResolutionLoweringMixin:
    def _ordinary_call_needs_runtime_binding(self, expr, fd, skip_self=False, require_object_result=False):
        """Classify before emitting anything or rearranging the original Call.

        A direct ordinary-Python ABI call is admitted only when
        ``_statically_bindable_call`` proves it binds exactly as the published
        callable's signature binder would. All other shapes belong to that
        binder, including calls that will raise TypeError. An annotation is
        never an arity proof. Explicit low-level/value boundaries retain their
        separate ABI.
        """
        if (self._freestanding_module or self._runtime_port_module
                or self._module_has_c_abi_export or fd is None
                or fd.manual_pointer_abi
                or self._func_c_abi_export_symbol(fd) is not None):
            return False
        if fd.is_async:
            # Calling an async function creates a coroutine object regardless
            # of the eventual return annotation. Its published callable owns
            # argument binding and writes that new owner directly into the
            # consumer's registered output slot before retiring arguments.
            # The direct adapter path returns a raw coroutine after cleanup.
            return True
        types = [fd.return_ty]
        for formal in fd.args:
            types.append(formal.annotation)
        for ty in types:
            if (isinstance(ty, (RawPointerType, ValueArrayType))
                    or isinstance(ty, IntType) and ty.name != "int"
                    or ty is not None and self._is_valueclass_payload_type(ty)):
                return False
        if require_object_result:
            # Some direct ABIs still return a raw object after argument
            # cleanup. An object-operand consumer requires publication into
            # its pre-registered output slot at the actual return boundary.
            return True
        if self._has_starred_unpack(expr.args):
            return True
        if self._split_starstar_kwargs_unpack(expr.args) is not None:
            return True
        return not _statically_bindable_call(
            expr.args, expr.kwargs, fd.args, skip_self, funcdef_defined_here(self, fd),
        )

    def _call_resolution_span_or_none(self, node):
        try:
            return node.span
        except AttributeError:
            return None

    def _subscript_expr_for_unpack(
        self,
        src: Expr,
        key: Expr,
        elem_ty: Type,
    ) -> Subscript:
        return Subscript(
            span=self._call_resolution_span_or_none(src),
            ty=elem_ty,
            obj=src,
            idx=key,
        )

    def _index_expr_for_unpack(self, src: Expr, idx: int) -> Subscript:
        elem_ty: Type = DynType(name="dyn")
        if isinstance(src.ty, ListType):
            elem_ty = src.ty.elem
        elif isinstance(src.ty, TupleType):
            if 0 <= idx < len(src.ty.elems):
                elem_ty = src.ty.elems[idx]
            elif src.ty.elems:
                elem_ty = src.ty.elems[0]
        key = IntLit(
            span=self._call_resolution_span_or_none(src),
            ty=IntType(name="int"),
            value=idx,
        )
        return self._subscript_expr_for_unpack(src, key, elem_ty)

    def _dict_key_expr_for_unpack(
        self,
        src: Expr,
        key_name: str,
    ) -> Subscript:
        elem_ty: Type = DynType(name="dyn")
        if isinstance(src.ty, DictType):
            elem_ty = src.ty.value
        key = StrLit(
            span=self._call_resolution_span_or_none(src),
            ty=StrType(name="str"),
            value=key_name,
        )
        return self._subscript_expr_for_unpack(src, key, elem_ty)

    def _expand_direct_call_unpacks(
        self,
        positional: tuple,
        kwargs_pairs: tuple,
        formal_args: tuple,
        skip_self: bool,
    ) -> tuple[tuple, tuple]:
        has_kw_unpack = any(name == "**" for name, _expr in kwargs_pairs)
        if not positional and not has_kw_unpack:
            return positional, kwargs_pairs
        has_pos_unpack = False
        for e in positional:
            if self._is_starred_unpack_expr(e) or (
                isinstance(e, Call)
                and isinstance(e.func, Name)
                and e.func.ident == "**"
            ):
                has_pos_unpack = True
                break
        if not has_pos_unpack and not has_kw_unpack:
            return positional, kwargs_pairs

        formals: list = []
        src_i = 0
        while src_i < len(formal_args):
            formal = formal_args[src_i]
            if skip_self and src_i == 0:
                src_i += 1
                continue
            if formal.name != "":
                formals.append(formal)
            src_i += 1

        plain_kwargs: list = []
        kwdict_srcs: list[Expr] = []
        for kw_name, kw_expr in kwargs_pairs:
            if kw_name == "**":
                kwdict_srcs.append(kw_expr)
            else:
                plain_kwargs.append((kw_name, kw_expr))

        explicit_kw = {name for name, _expr in plain_kwargs}
        positional_formals: list = []
        saw_var_pos = False
        for formal in formals:
            if formal.kind == "*args":
                saw_var_pos = True
                continue
            if formal.kind == "**kwargs":
                continue
            if (
                not saw_var_pos
                and (formal.kind == "pos" or formal.kind == "pos_only")
                and formal.name not in explicit_kw
            ):
                positional_formals.append(formal)

        plain_pos: list = []
        star_src: Optional[Expr] = None
        # Number of plain positional args that appear *before* the starred
        # splat in source order. The splat expansion must be inserted at this
        # index so ``f(1, *x, 3)`` keeps the argument order ``1, *x, 3`` rather
        # than moving the splat to the end (which produced ``1, 3, *x``).
        star_prefix_count = 0
        for e in positional:
            if self._is_starred_unpack_expr(e):
                if star_src is not None:
                    return positional, kwargs_pairs
                star_src = e.args[0]
                star_prefix_count = len(plain_pos)
                continue
            if (
                isinstance(e, Call)
                and isinstance(e.func, Name)
                and e.func.ident == "**"
                and len(e.args) == 1
                and not e.kwargs
            ):
                kwdict_srcs.append(e.args[0])
                continue
            plain_pos.append(e)
        kwdict_src: Optional[Expr] = None
        if len(kwdict_srcs) == 1:
            kwdict_src = kwdict_srcs[0]
        elif len(kwdict_srcs) > 1:
            # Call keywords, not a dict display: a key repeated across the
            # ** operands is CPython's TypeError, so merge through the
            # duplicate-checking helper instead of ``{**a, **b}``.
            span = self._call_resolution_span_or_none(kwdict_srcs[0])
            kwdict_src = _call_kwargs_merge(span, tuple(kwdict_srcs))

        expanded_pos = list(plain_pos)
        if star_src is not None:
            star_count: Optional[int] = None
            star_src_kind = ""
            try:
                star_src_kind = type(star_src).__name__
            except AttributeError:
                star_src_kind = ""
            if isinstance(star_src, TupleExpr):
                star_count = len(star_src.elems)
            elif isinstance(star_src.ty, TupleType) and len(star_src.ty.elems) > 0:
                star_count = len(star_src.ty.elems)
            if star_count is not None:
                needed = star_count
            else:
                required_pos = 0
                formal_i = 0
                while formal_i < len(formals):
                    formal = formals[formal_i]
                    if formal.kind == "*args":
                        break
                    if formal.kind == "pos" or formal.kind == "pos_only":
                        has_explicit_kw = False
                        kw_i = 0
                        while kw_i < len(kwargs_pairs):
                            kw_pair = kwargs_pairs[kw_i]
                            if kw_pair[0] == formal.name:
                                has_explicit_kw = True
                                break
                            kw_i += 1
                        if not has_explicit_kw and not formal.has_default:
                            required_pos += 1
                    formal_i += 1
                if required_pos == 0 and len(plain_pos) == 0:
                    formal_i = 0
                    while formal_i < len(formal_args):
                        formal = formal_args[formal_i]
                        if skip_self and formal_i == 0:
                            formal_i += 1
                            continue
                        if formal.kind == "*args":
                            break
                        if formal.name != "":
                            if formal.kind != "**kwargs" and formal.kind != "kw_only":
                                has_explicit_kw = False
                                kw_i = 0
                                while kw_i < len(kwargs_pairs):
                                    kw_pair = kwargs_pairs[kw_i]
                                    if kw_pair[0] == formal.name:
                                        has_explicit_kw = True
                                        break
                                    kw_i += 1
                                if not has_explicit_kw and formal.default is None:
                                    required_pos += 1
                        formal_i += 1
                needed = required_pos - len(plain_pos)
            if needed == 0 and len(plain_pos) == 0 and star_src_kind != "TupleExpr":
                required_pos = 0
                formal_i = 0
                while formal_i < len(formal_args):
                    formal = formal_args[formal_i]
                    if skip_self and formal_i == 0:
                        formal_i += 1
                        continue
                    if formal.kind == "*args":
                        break
                    if formal.name != "":
                        if formal.kind != "**kwargs" and formal.kind != "kw_only":
                            has_explicit_kw = False
                            kw_i = 0
                            while kw_i < len(kwargs_pairs):
                                kw_pair = kwargs_pairs[kw_i]
                                if kw_pair[0] == formal.name:
                                    has_explicit_kw = True
                                    break
                                kw_i += 1
                            if not has_explicit_kw and formal.default is None:
                                required_pos += 1
                    formal_i += 1
                needed = required_pos
            if needed < 0:
                return positional, kwargs_pairs
            # A runtime-sized splat that feeds the *args param ENTIRELY — the
            # plain positionals exactly fill the fixed positional formals, so
            # every splat element goes to *args. Static expansion can't size it
            # (star_count is None), so it was silently dropped -> empty *args
            # (e.g. g(*xs) with def g(*args) returned no args). Emit a marker the
            # resolver turns into tuple(star_src) for the *args slot. Splats that
            # also fill fixed slots, or co-occur with **kwargs, keep the old path.
            has_var_pos = False
            for f in formals:
                if f.kind == "*args":
                    has_var_pos = True
                    break
            if (
                has_var_pos
                and star_count is None
                and kwdict_src is None
                and star_prefix_count == len(plain_pos)
                and len(expanded_pos) == len(positional_formals)
            ):
                marker = Call(
                    span=getattr(star_src, "span", None),
                    ty=star_src.ty,
                    func=Name(
                        getattr(star_src, "span", None),
                        DynType(name="dyn"),
                        "__star_to_varargs__",
                    ),
                    args=(star_src,),
                    kwargs=(),
                )
                expanded_pos.append(marker)
                return tuple(expanded_pos), tuple(kwargs_pairs)
            # Insert the splat expansion at the splat's source position so
            # positional args after the splat keep their order, e.g.
            # ``f(1, *x, 3)`` -> ``[1, x[0], x[1], ..., 3]``.
            star_exprs: list = []
            i = 0
            while i < needed:
                star_exprs.append(self._index_expr_for_unpack(star_src, i))
                i += 1
            expanded_pos = (
                expanded_pos[:star_prefix_count]
                + star_exprs
                + expanded_pos[star_prefix_count:]
            )

        expanded_kwargs = list(plain_kwargs)
        if kwdict_src is not None:
            filled = set()
            i = 0
            while i < len(expanded_pos) and i < len(positional_formals):
                filled.add(positional_formals[i].name)
                i += 1
            existing = set(explicit_kw)
            for formal in formals:
                if formal.kind in ("*args", "**kwargs", "pos_only"):
                    continue
                if formal.name in filled or formal.name in existing:
                    continue
                expanded_kwargs.append(
                    (
                        formal.name,
                        self._dict_key_expr_for_unpack(kwdict_src, formal.name),
                    )
                )
                existing.add(formal.name)

        return tuple(expanded_pos), tuple(expanded_kwargs)

    def _resolve_call_kwargs(
        self,
        positional: tuple,
        kwargs_pairs: tuple,
        formal_args: tuple,
        skip_self: bool = False,
    ) -> list:
        """Reorder positional + keyword call args to match formals.

        Returns an Expr list in formal-parameter order. Missing slots
        are filled from ``Arg.default``; an unbound slot without a
        default raises L1CodegenError, as do duplicate binds, unknown
        keywords, and surplus positionals.
        """
        if not kwargs_pairs:
            has_unpack = False
            for e in positional:
                if self._is_starred_unpack_expr(e) or (
                    isinstance(e, Call)
                    and isinstance(e.func, Name)
                    and e.func.ident == "**"
                ):
                    has_unpack = True
                    break
            if not has_unpack:
                expected_pos = 0
                fast_ok = True
                src_i = 0
                while src_i < len(formal_args):
                    formal = formal_args[src_i]
                    if skip_self and src_i == 0:
                        src_i += 1
                        continue
                    if formal.name == "":
                        src_i += 1
                        continue
                    if formal.kind == "pos" or formal.kind == "pos_only":
                        expected_pos += 1
                        src_i += 1
                        continue
                    fast_ok = False
                    break
                if fast_ok and len(positional) == expected_pos:
                    return list(positional)

        raw_positional = positional
        raw_positional_len = len(raw_positional)
        raw_kwdict_srcs: list[Expr] = []
        for raw_arg in raw_positional:
            if (
                isinstance(raw_arg, Call)
                and isinstance(raw_arg.func, Name)
                and raw_arg.func.ident == "**"
                and len(raw_arg.args) == 1
                and not raw_arg.kwargs
            ):
                raw_kwdict_srcs.append(raw_arg.args[0])
        for raw_kw_name, raw_kw_expr in kwargs_pairs:
            if raw_kw_name == "**":
                raw_kwdict_srcs.append(raw_kw_expr)
        raw_first_kind = ""
        if raw_positional_len > 0:
            try:
                raw_first_kind = type(raw_positional[0]).__name__
            except AttributeError:
                raw_first_kind = ""
        positional, kwargs_pairs = self._expand_direct_call_unpacks(
            positional,
            kwargs_pairs,
            formal_args,
            skip_self,
        )
        formals: list = []
        src_i = 0
        while src_i < len(formal_args):
            formal = formal_args[src_i]
            # Bound instance/class method calls already provide the
            # receiver separately. Strip the first formal regardless of
            # its source-level name (`self`, `cls`, `self_or_op`, ...).
            if skip_self and src_i == 0:
                src_i += 1
                continue
            # Filter out the bare ``*`` separator — a kw_only marker
            # with an empty name has no runtime param. ``*args`` /
            # ``**kwargs`` with real names are separate kinds.
            if formal.name != "":
                formals.append(formal)
            src_i += 1
        n_formal = len(formals)
        resolved: list = []
        i = 0
        while i < n_formal:
            resolved.append(None)
            i += 1
        var_pos_idx = -1
        var_kw_idx = -1
        pos_formal_indices: list = []
        saw_var_pos = False
        i = 0
        while i < n_formal:
            f = formals[i]
            if f.kind == "*args":
                var_pos_idx = i
                saw_var_pos = True
            elif f.kind == "**kwargs":
                var_kw_idx = i
            elif not saw_var_pos and (f.kind == "pos" or f.kind == "pos_only"):
                pos_formal_indices.append(i)
            i += 1
        extra_pos: list[Expr] = []
        i = 0
        while i < len(positional):
            e = positional[i]
            if i < len(pos_formal_indices):
                resolved[pos_formal_indices[i]] = e
                i += 1
                continue
            if var_pos_idx >= 0:
                extra_pos.append(e)
                i += 1
                continue
            raise L1CodegenError(
                f"too many positional args: got {len(positional)}, "
                f"expected at most {len(pos_formal_indices)}"
            )

        synth_span = None
        for expr in positional:
            span = self._call_resolution_span_or_none(expr)
            if span is not None:
                synth_span = span
                break
        kw_i = 0
        while synth_span is None and kw_i < len(kwargs_pairs):
            kw_pair = kwargs_pairs[kw_i]
            kw_expr = kw_pair[1]
            span = self._call_resolution_span_or_none(kw_expr)
            if span is not None:
                synth_span = span
            kw_i += 1
        formal_i = 0
        while synth_span is None and formal_i < len(formals):
            formal = formals[formal_i]
            if formal.default is not None:
                span = self._call_resolution_span_or_none(formal.default)
                if span is not None:
                    synth_span = span
            formal_i += 1
        cur = getattr(self, "current_func_def", None)
        if synth_span is None and cur is not None:
            span = self._call_resolution_span_or_none(cur)
            if span is not None:
                synth_span = span
        if synth_span is None:
            mod = self.module.name or "<generated>"
            synth_span = SourceSpan(
                file=mod,
                line=1,
                col=1,
                end_line=1,
                end_col=1,
            )
        if var_pos_idx >= 0:
            if (
                len(extra_pos) == 1
                and isinstance(extra_pos[0], Call)
                and isinstance(extra_pos[0].func, Name)
                and extra_pos[0].func.ident == "__star_to_varargs__"
            ):
                # *args fed entirely by a runtime splat g(*xs): the *args tuple
                # IS tuple(xs). tuple(<seq>) is already a working builtin.
                star_src = extra_pos[0].args[0]
                resolved[var_pos_idx] = Call(
                    span=synth_span,
                    ty=TupleType(name="tuple", elems=()),
                    func=Name(synth_span, DynType(name="dyn"), "tuple"),
                    args=(star_src,),
                    kwargs=(),
                )
            else:
                resolved[var_pos_idx] = TupleExpr(
                    span=synth_span,
                    ty=TupleType(
                        name="tuple",
                        elems=tuple(e.ty for e in extra_pos),
                    ),
                    elems=tuple(extra_pos),
                )

        extra_kwargs: list[tuple[str, Expr]] = []
        kw_i = 0
        while kw_i < len(kwargs_pairs):
            kw_pair = kwargs_pairs[kw_i]
            kw_name = kw_pair[0]
            kw_expr = kw_pair[1]
            idx = -1
            j = 0
            while j < n_formal:
                f = formals[j]
                if f.kind != "*args" and f.kind != "**kwargs":
                    if f.name == kw_name:
                        idx = j
                        break
                j += 1
            if idx < 0:
                if var_kw_idx >= 0:
                    extra_kwargs.append((kw_name, kw_expr))
                    kw_i += 1
                    continue
                formal_names = ",".join(f.name for f in formals)
                raise L1CodegenError(
                    f"unexpected keyword argument {kw_name!r}; "
                    f"formals=({formal_names})"
                )
            if formals[idx].kind == "pos_only":
                if var_kw_idx >= 0:
                    extra_kwargs.append((kw_name, kw_expr))
                    kw_i += 1
                    continue
                formal_names = ",".join(f.name for f in formals)
                raise L1CodegenError(
                    f"unexpected keyword argument {kw_name!r}; "
                    f"formals=({formal_names})"
                )
            if resolved[idx] is not None:
                raise L1CodegenError(f"duplicate value for argument {kw_name!r}")
            resolved[idx] = kw_expr
            kw_i += 1

        if var_kw_idx >= 0:
            kw_pairs: list = []
            kw_i = 0
            while kw_i < len(extra_kwargs):
                kw_pair = extra_kwargs[kw_i]
                kw_name = kw_pair[0]
                kw_expr = kw_pair[1]
                kw_pairs.append(
                    (
                        StrLit(
                            span=self._call_resolution_span_or_none(kw_expr),
                            ty=StrType(name="str"),
                            value=kw_name,
                        ),
                        kw_expr,
                    )
                )
                kw_i += 1
            explicit_kw_dict = DictExpr(
                span=synth_span,
                ty=DictType(
                    name="dict",
                    key=StrType(name="str"),
                    value=DynType(name="dyn"),
                ),
                pairs=tuple(kw_pairs),
            )
            if raw_kwdict_srcs:
                if len(raw_kwdict_srcs) > 1 and kw_pairs:
                    raise NotImplementedError(
                        "call cannot yet preserve source order for multiple "
                        "**mapping operands interleaved with explicit keywords"
                    )
                # ``f(**a, **b)`` / ``f(**a, x=1)`` into ``**kw``: CPython
                # raises TypeError for a key given twice across the operands
                # and keywords; a dict display ``{**a, "x": 1}`` would
                # overwrite.
                resolved[var_kw_idx] = _call_kwargs_merge(
                    synth_span,
                    tuple(raw_kwdict_srcs) + (explicit_kw_dict,),
                )
            else:
                resolved[var_kw_idx] = explicit_kw_dict

        i = 0
        while i < n_formal:
            formal = formals[i]
            if resolved[i] is None:
                if formal.kind == "*args":
                    resolved[i] = TupleExpr(
                        span=synth_span,
                        ty=TupleType(name="tuple", elems=()),
                        elems=(),
                    )
                    continue
                if formal.kind == "**kwargs":
                    resolved[i] = DictExpr(
                        span=synth_span,
                        ty=DictType(
                            name="dict",
                            key=StrType(name="str"),
                            value=DynType(name="dyn"),
                        ),
                        pairs=(),
                    )
                    continue
                if not formal.has_default:
                    # Name the formals and which of them the callee signature
                    # says carry defaults.  Without this the message says only
                    # "missing required argument", which cannot distinguish a
                    # genuine caller mistake from a signature that lost a
                    # default on the way in (a dataclass
                    # `field(default_factory=...)` crossing a module boundary
                    # is the case this caught).
                    shape = ",".join(
                        (f.name or "?")
                        + ("=" if f.has_default else "")
                        + ("<" + type(f.default).__name__ + ">"
                           if f.default is not None else "<none>")
                        for f in formals
                    )
                    supplied = ",".join(str(k) for k, _ in kwargs_pairs)
                    raise L1CodegenError(
                        f"missing required argument {formal.name!r} "
                        f"(positional={len(positional)}, "
                        f"raw_positional={raw_positional_len}, "
                        f"raw_first={raw_first_kind}, "
                        f"kwargs={len(kwargs_pairs)}, "
                        f"formals={len(formals)}; "
                        f"signature={shape}; supplied={supplied})"
                    )
                if formal.default is None:
                    resolved[i] = NoneLit(
                        span=synth_span,
                        ty=NoneType(name="None"),
                    )
                else:
                    resolved[i] = formal.default
            i += 1
        return resolved
