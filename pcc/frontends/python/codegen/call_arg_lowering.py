"""Call argument shape helpers for L1CodeGen."""
from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Call, DictType, DynType, Expr, Name, StrType
from pcc.frontends.python.codegen import marshal


_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_KWARGS_MERGE = "__pcc_kwargs_merge__"


class CallArgLoweringMixin:
    def _bridge_cpy_arglist_operand(
        self,
        value: ir.Value,
        cpy_live_owned,
        rooted_pcc_on_error=(),
    ) -> tuple[ir.Value, bool]:
        """Convert a checked CPython operand before pcc-list insertion."""
        if cpy_live_owned is None or value not in getattr(self, "_cpy_values", ()):
            return value, False
        self._guard_cpy_value_not_null(
            value,
            tuple(cpy_live_owned),
            rooted_pcc_on_error,
        )
        value_owned = self._cpy_value_is_owned(value)
        cleanup = list(cpy_live_owned)
        if value_owned:
            cleanup.append(value)
        bridged = self.builder.call(
            self.runtime["py_cpy_to_pcc_obj"],
            [value],
            name=self._fresh("call.arg.cpy.to_pcc"),
        )
        self._guard_cpy_value_not_null(
            bridged,
            tuple(cleanup),
            rooted_pcc_on_error,
        )
        if value_owned:
            self.builder.call(self.runtime["py_cpy_decref"], [value])
            self._forget_owned_cpy_value(value)
        return bridged, True

    def _call_arg_node_kind(self, node: object) -> str:
        try:
            return type(node).__name__
        except AttributeError:
            return ""

    def _is_call_arg_call_node(self, node: object) -> bool:
        return isinstance(node, Call) or self._call_arg_node_kind(node) == "Call"

    def _is_call_arg_name_node(self, node: object) -> bool:
        return isinstance(node, Name) or self._call_arg_node_kind(node) == "Name"

    def _is_starred_unpack(self, arg_exprs: tuple) -> bool:
        if len(arg_exprs) != 1:
            return False
        arg = arg_exprs[0]
        return (
            self._is_call_arg_call_node(arg)
            and self._is_call_arg_name_node(arg.func)
            and arg.func.ident in ("*", "__starred__")
            and len(arg.args) == 1
            and not arg.kwargs
        )

    def _is_starred_unpack_expr(self, arg: Expr) -> bool:
        return (
            self._is_call_arg_call_node(arg)
            and self._is_call_arg_name_node(arg.func)
            and arg.func.ident in ("*", "__starred__")
            and len(arg.args) == 1
            and not arg.kwargs
        )

    def _has_starred_unpack(self, arg_exprs: tuple[Expr, ...]) -> bool:
        return any(self._is_starred_unpack_expr(arg) for arg in arg_exprs)

    def _has_starstar_unpack(self, arg_exprs, kwargs) -> bool:
        """``f(**mapping)``: py_lift keeps the operand as a ``**`` marker call
        among the arguments, the CPython-ast parser under a ``**`` keyword."""
        for arg in arg_exprs:
            if (
                self._is_call_arg_call_node(arg)
                and self._is_call_arg_name_node(arg.func)
                and arg.func.ident == "**"
                and len(arg.args) == 1
                and not arg.kwargs
            ):
                return True
        return any(key == "**" for key, _value in kwargs)

    def _has_unpacked_operands(self, arg_exprs, kwargs) -> bool:
        """``*`` or ``**`` unpacking among a call's operands. A lowering that
        takes fixed operands leaves such a call to the generic call, which
        binds the unpacked arguments."""
        return self._has_starred_unpack(arg_exprs) or self._has_starstar_unpack(arg_exprs, kwargs)

    def _split_starstar_kwargs_unpack(
        self,
        arg_exprs: tuple[Expr, ...],
    ) -> tuple[tuple[Expr, ...], Expr] | None:
        positional: list[Expr] = []
        kwargs_exprs: list[Expr] = []
        for arg in arg_exprs:
            if (
                isinstance(arg, Call)
                and isinstance(arg.func, Name)
                and arg.func.ident == "**"
                and len(arg.args) == 1
                and not arg.kwargs
            ):
                kwargs_exprs.append(arg.args[0])
                continue
            if kwargs_exprs:
                return None
            positional.append(arg)
        if not kwargs_exprs:
            return None
        if len(kwargs_exprs) != 1:
            # Several ** operands merge left to right into one keyword dict.
            # A key repeated across them is CPython's TypeError, not the
            # silent overwrite a plain dict update would give, so they go
            # through the duplicate-checking merge (``__pcc_kwargs_merge__``).
            span = kwargs_exprs[0].span
            merged = Call(
                span=span,
                ty=DictType(
                    name="dict",
                    key=StrType(name="str"),
                    value=DynType(name="dyn"),
                ),
                func=Name(span=span, ty=DynType(name="dyn"), ident=_KWARGS_MERGE),
                args=tuple(kwargs_exprs),
                kwargs=(),
            )
            return tuple(positional), merged
        return tuple(positional), kwargs_exprs[0]

    def _is_kwargs_merge(self, expr) -> bool:
        return (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident == _KWARGS_MERGE
        )

    def _reject_kwargs_merge_with_explicit_keywords(self, kwargs_expr, kwargs) -> None:
        """Several ``**`` operands merge left to right before any explicit
        keyword value is evaluated, which is source order only when there is
        no explicit keyword to interleave; otherwise fail closed as before."""
        if not self._is_kwargs_merge(kwargs_expr):
            return
        for kw_name, _kw_expr in kwargs:
            if kw_name != "**":
                raise NotImplementedError(
                    "call cannot yet preserve source order for multiple "
                    "**mapping operands interleaved with explicit keywords"
                )

    def _emit_kwargs_merge(self, expr: Call) -> ir.Value:
        """Lower ``__pcc_kwargs_merge__(m1, m2, ...)`` to one owned dict."""
        acc = ir.Constant(_CSTR, None)
        for operand in expr.args:
            mapping = self._emit_as_object(operand)
            merged = self.builder.call(
                self.runtime["py_call_merge_kwargs_unique"],
                [acc, mapping],
                name=self._fresh("call.kwargs.merge.unique"),
            )
            if not isinstance(acc, ir.Constant):
                self._gc_release(acc)
            self._gc_release_if_owned(mapping, operand)
            self._emit_post_call_err_check(getattr(expr, "span", None))
            acc = merged
        return acc

    def _emit_pcc_args_list(
        self,
        arg_exprs: tuple[Expr, ...],
        name_hint: str,
        cpy_live_owned=None,
        cpy_temp_root_out=None,
    ) -> ir.Value:
        lst = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(f"call.args.{name_hint}"),
        )
        if cpy_live_owned is not None:
            # ``py_list_new`` is a pcc-runtime allocator.  Route its error
            # through the active pcc try target (and release the live CPython
            # callable/operands) instead of treating NULL as a libpython error,
            # which would incorrectly bypass a surrounding ``try``.
            self._emit_post_call_err_check(
                cpy_release_on_error=tuple(cpy_live_owned),
            )
            lst_root = self._enter_container_temp_root(
                lst,
                self._fresh("cpy.call.args"),
            )
            rooted_pcc_on_error = ((lst, lst_root),)
        else:
            lst_root = None
            rooted_pcc_on_error = ()
        for arg in arg_exprs:
            if self._is_starred_unpack_expr(arg):
                if cpy_live_owned is None:
                    inner = self._emit_expr(arg.args[0])
                else:
                    inner = self._emit_expr_with_cpy_operand_cleanup(
                        arg.args[0],
                        tuple(cpy_live_owned),
                        rooted_pcc_on_error,
                    )
                inner, inner_bridged = self._bridge_cpy_arglist_operand(
                    inner,
                    cpy_live_owned,
                    rooted_pcc_on_error,
                )
                self.builder.call(
                    self.runtime["py_list_extend"],
                    [lst, inner],
                )
                if inner_bridged:
                    self._gc_release(inner)
                elif cpy_live_owned is not None:
                    self._gc_release_if_owned(inner, arg.args[0])
                if cpy_live_owned is not None:
                    self._emit_post_call_err_check(
                        getattr(arg, "span", None),
                        cpy_release_on_error=tuple(cpy_live_owned),
                        rooted_release_on_error=rooted_pcc_on_error,
                    )
                continue
            if cpy_live_owned is None:
                v = self._emit_expr(arg)
            else:
                v = self._emit_expr_with_cpy_operand_cleanup(
                    arg,
                    tuple(cpy_live_owned),
                    rooted_pcc_on_error,
                )
            if v in getattr(self, "_cpy_values", ()) and cpy_live_owned is not None:
                v_obj, v_bridged = self._bridge_cpy_arglist_operand(
                    v,
                    cpy_live_owned,
                    rooted_pcc_on_error,
                )
            else:
                v_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    arg.ty,
                )
                v_bridged = False
            self.builder.call(
                self.runtime["py_list_append"],
                [lst, v_obj],
            )
            if v_bridged or self._container_store_temp_needs_release(
                arg,
                arg.ty,
                False,
            ):
                self._gc_release(v_obj)
            if cpy_live_owned is not None:
                self._emit_post_call_err_check(
                    getattr(arg, "span", None),
                    cpy_release_on_error=tuple(cpy_live_owned),
                    rooted_release_on_error=rooted_pcc_on_error,
                )
        if cpy_live_owned is not None:
            if cpy_temp_root_out is None:
                self._leave_container_temp_root(lst_root)
            else:
                cpy_temp_root_out.append(lst_root)
        return lst
