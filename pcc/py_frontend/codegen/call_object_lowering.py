"""Callable object materialization helpers for Layer-1 codegen."""
from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    DictExpr,
    DictType,
    DynType,
    Expr,
    Name,
    SourceSpan,
    StrLit,
    StrType,
    TupleExpr,
    TupleType,
    Type,
)
from . import marshal
from .runtime_abi import declare_runtime_global


_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class CallObjectLoweringMixin:
    def _emit_call_arg_object(self, arg: Expr) -> ir.Value:
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            arg.ty,
            arg,
        )
        if valueclass_payload is not None:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                valueclass_payload,
                arg.ty,
            )
            if boxed_valueclass is not None:
                return boxed_valueclass

        raw = self._emit_expr(arg)
        if raw in getattr(self, "_cpy_values", ()):
            return self._emit_value_as_pcc_object_or_bridge(
                raw,
                arg.ty,
                "call.arg.bridge",
            )

        boxed_valueclass = self._emit_valueclass_payload_to_object(raw, arg.ty)
        if boxed_valueclass is not None:
            return boxed_valueclass

        return marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            raw,
            arg.ty,
        )

    def _emit_call_args_tuple(self, args: tuple[Expr, ...]) -> ir.Value:
        # Argument tuples have the same retaining slot contract as ordinary
        # tuple literals. Reuse their marshalling, temporary-owner release,
        # moving-GC protection and cleanup when a later argument raises.
        return self._emit_tuple_literal(TupleExpr(
            span=args[0].span if args else None,
            ty=TupleType(name="tuple", elems=tuple(arg.ty for arg in args)),
            elems=args,
        ))

    def _emit_dynamic_call_args_tuple(self, args: tuple[Expr, ...]) -> ir.Value:
        """Materialize args for a pcc-native dynamic callable call.

        The bootstrap-observability shape ``fn(*args, **kwargs)`` passes
        an existing pcc tuple through directly. Mixed starred calls are
        still represented as a pcc list; full tuple-normalization belongs
        with the broader callable ABI work.
        """
        if self._is_starred_unpack(args):
            return self._emit_as_object(args[0].args[0])
        if self._has_starred_unpack(args):
            lst = self._emit_pcc_args_list(args, "dyn")
            tup = self.builder.call(
                self.runtime["py_tuple_from_list"],
                [lst],
                name=self._fresh("call.args.splat.tuple"),
            )
            self._gc_release(lst)
            return tup
        return self._emit_call_args_tuple(args)

    def _emit_dynamic_call_kwargs_object(
        self,
        kwargs: tuple[tuple[str, Expr], ...],
        kwargs_expr: Optional[Expr],
        span: SourceSpan,
    ) -> ir.Value:
        if kwargs_expr is None and not kwargs:
            none_gv = declare_runtime_global(self.module, "py_None")
            return self.builder.load(none_gv, name=self._fresh("none"))
        if kwargs_expr is not None and not kwargs:
            star = self._emit_as_object(kwargs_expr)
            return self.builder.call(
                self.runtime["py_call_merge_kwargs"],
                [ir.Constant(_CSTR, None), star],
                name=self._fresh("call.kwargs.clone"),
            )

        current: Optional[ir.Value] = None
        pairs: list[tuple[Expr, Expr]] = []
        for kw_name, kw_expr in kwargs:
            if kw_name == "**":
                if current is None:
                    base = self._emit_dynamic_call_kwargs_dict_literal(
                        tuple(pairs),
                        span,
                    )
                elif pairs:
                    explicit = self._emit_dynamic_call_kwargs_dict_literal(
                        tuple(pairs),
                        span,
                    )
                    merged_explicit = self.builder.call(
                        self.runtime["py_call_merge_kwargs"],
                        [current, explicit],
                        name=self._fresh("call.kwargs.merge.explicit"),
                    )
                    self._gc_release(current)
                    self._gc_release(explicit)
                    base = merged_explicit
                else:
                    base = current
                pairs = []
                star = self._emit_as_object(kw_expr)
                current = self.builder.call(
                    self.runtime["py_call_merge_kwargs"],
                    [base, star],
                    name=self._fresh("call.kwargs.merge"),
                )
                self._gc_release(base)
                continue
            pairs.append(
                (
                    StrLit(
                        span=kw_expr.span,
                        ty=StrType(name="str"),
                        value=kw_name,
                    ),
                    kw_expr,
                )
            )
        if kwargs_expr is not None:
            if current is None:
                base = self._emit_dynamic_call_kwargs_dict_literal(
                    tuple(pairs),
                    span,
                )
            elif pairs:
                explicit = self._emit_dynamic_call_kwargs_dict_literal(
                    tuple(pairs),
                    span,
                )
                merged_explicit = self.builder.call(
                    self.runtime["py_call_merge_kwargs"],
                    [current, explicit],
                    name=self._fresh("call.kwargs.merge.explicit"),
                )
                self._gc_release(current)
                self._gc_release(explicit)
                base = merged_explicit
            else:
                base = current
            pairs = []
            star = self._emit_as_object(kwargs_expr)
            merged = self.builder.call(
                self.runtime["py_call_merge_kwargs"],
                [base, star],
                name=self._fresh("call.kwargs.merge"),
            )
            self._gc_release(base)
            return merged
        if current is not None:
            if pairs:
                explicit = self._emit_dynamic_call_kwargs_dict_literal(
                    tuple(pairs),
                    span,
                )
                merged = self.builder.call(
                    self.runtime["py_call_merge_kwargs"],
                    [current, explicit],
                    name=self._fresh("call.kwargs.merge.explicit"),
                )
                self._gc_release(current)
                self._gc_release(explicit)
                return merged
            return current
        return self._emit_dynamic_call_kwargs_dict_literal(tuple(pairs), span)

    def _emit_dynamic_call_kwargs_dict_literal(
        self,
        pairs: tuple[tuple[Expr, Expr], ...],
        span: SourceSpan,
    ) -> ir.Value:
        return self._emit_dict_literal(
            DictExpr(
                span=span,
                ty=DictType(
                    name="dict",
                    key=StrType(name="str"),
                    value=DynType(name="dyn"),
                ),
                pairs=pairs,
            )
        )

    def _emit_object_tuple_from_values(
        self,
        values: tuple[tuple[ir.Value, Type], ...],
        *,
        name: str,
    ) -> ir.Value:
        tup = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(values))],
            name=self._fresh(name),
        )
        for i, (raw, ty) in enumerate(values):
            obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                raw,
                ty,
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [tup, ir.Constant(_I64, i), obj],
            )
        return tup

    def _emit_empty_tuple(self, name: str) -> ir.Value:
        return self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(name),
        )
