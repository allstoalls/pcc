"""Typing and protocol helper lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, BinOp, Call, Expr, IntLit, Name, StrLit, Subscript, TupleExpr

_I1 = ir.IntType(1)
_TYPING_METADATA_ALIAS_VALUES = frozenset(
    (
        "typing.Literal",
        "typing.Union",
        "typing.Any",
        "typing.Type",
        "typing.ClassVar",
        "typing.Final",
        "typing.NoReturn",
        "typing.Callable",
        "typing.Iterator",
        "typing.Iterable",
        "typing.Sequence",
        "typing.Mapping",
        "typing.List",
        "typing.Dict",
        "typing.Set",
        "typing.Tuple",
        "typing.SupportsIndex",
        "typing.TypeAlias",
        "typing.TypedDict",
    )
)


class TypingProtocolMixin:
    def _typing_type_alias_annotation(self, annotation: object) -> bool:
        return (
            getattr(annotation, "name", None) == "TypeAlias"
            and self._native_builtin_value_aliases.get("TypeAlias")
            == "typing.TypeAlias"
        )

    def _typing_typevar_name(self, expr: Expr) -> Optional[str]:
        if not isinstance(expr, Call) or len(expr.args) != 1:
            return None
        if not isinstance(expr.func, Name):
            return None
        ident = expr.func.ident
        if ident in self.env or ident in self._module_globals:
            return None
        if self._native_builtin_value_aliases.get(ident) != "typing.TypeVar":
            return None
        arg = expr.args[0]
        if isinstance(arg, StrLit):
            return arg.value
        return None

    def _typing_optional_arg_name(self, expr: Expr) -> Optional[str]:
        if not isinstance(expr, Subscript) or not isinstance(expr.obj, Name):
            return None
        ident = expr.obj.ident
        if ident in self.env or ident in self._module_globals:
            return None
        if self._native_builtin_value_aliases.get(ident) != "typing.Optional":
            return None
        idx = expr.idx
        if isinstance(idx, Name):
            return idx.ident
        if isinstance(idx, Attr):
            return idx.name
        return None

    def _typing_optional_alias_arg_name(self, expr: Expr) -> Optional[str]:
        direct = self._typing_optional_arg_name(expr)
        if direct is not None:
            return direct
        if isinstance(expr, Name):
            return self._typing_optional_aliases.get(expr.ident)
        return None

    def _typing_metadata_alias_expr(self, expr: Expr) -> bool:
        if isinstance(expr, BinOp) and expr.op == "|":
            return self._typing_metadata_alias_expr(
                expr.lhs
            ) or self._typing_metadata_alias_expr(expr.rhs)
        if isinstance(expr, TupleExpr):
            return any(self._typing_metadata_alias_expr(elem) for elem in expr.elems)
        if isinstance(expr, Call) and isinstance(expr.func, Name):
            ident = expr.func.ident
            if ident not in self.env and ident not in self._module_globals:
                return (
                    self._native_builtin_value_aliases.get(ident)
                    == "typing.TypeAliasType"
                )
        if isinstance(expr, Subscript):
            if self._typing_metadata_alias_expr(expr.obj):
                return True
            # ``dict[Engine, list[tuple]]`` where ``Engine`` was itself an
            # elided typing-metadata alias: the alias has no runtime binding
            # (its assignment emitted no IR), so any subscript referencing it
            # must be treated as metadata too or codegen would load a NULL
            # module global at runtime.
            return self._typing_metadata_alias_expr(
                expr.idx
            ) or self._typing_metadata_alias_index_ref(expr.idx)
        value_kind = None
        if isinstance(expr, Name):
            if expr.ident in self._typing_metadata_aliases:
                return True
            if expr.ident in self.env or expr.ident in self._module_globals:
                return False
            value_kind = self._native_builtin_value_aliases.get(expr.ident)
        elif (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "typing"
        ):
            value_kind = "typing." + expr.name
        return value_kind in _TYPING_METADATA_ALIAS_VALUES

    def _typing_metadata_alias_index_ref(self, expr: Expr) -> bool:
        if isinstance(expr, Name):
            return expr.ident in self._typing_metadata_aliases
        if isinstance(expr, TupleExpr):
            return any(
                self._typing_metadata_alias_index_ref(elem) for elem in expr.elems
            )
        if isinstance(expr, Subscript):
            return self._typing_metadata_alias_index_ref(
                expr.obj
            ) or self._typing_metadata_alias_index_ref(expr.idx)
        return False

    def _maybe_emit_typing_alias_name_attr(self, expr: Attr) -> Optional[ir.Value]:
        if expr.name != "__name__":
            return None
        obj = expr.obj
        origin_is_native = False
        if isinstance(obj, Call) and isinstance(obj.func, Name):
            origin_name = obj.func.ident
            if origin_name not in self.env and origin_name not in self._module_globals:
                origin_is_native = (
                    self._native_builtin_value_aliases.get(origin_name)
                    == "typing.get_origin"
                )
        if (
            isinstance(obj, Call)
            and isinstance(obj.func, Name)
            and origin_is_native
            and len(obj.args) == 1
            and not obj.kwargs
            and self._typing_optional_alias_arg_name(obj.args[0]) is not None
        ):
            return self._emit_str_literal("Optional")
        args_is_native = False
        if (
            isinstance(obj, Subscript)
            and isinstance(obj.obj, Call)
            and isinstance(obj.obj.func, Name)
        ):
            args_name = obj.obj.func.ident
            if args_name not in self.env and args_name not in self._module_globals:
                args_is_native = (
                    self._native_builtin_value_aliases.get(args_name)
                    == "typing.get_args"
                )
        if (
            isinstance(obj, Subscript)
            and isinstance(obj.idx, IntLit)
            and int(obj.idx.value) == 0
            and isinstance(obj.obj, Call)
            and isinstance(obj.obj.func, Name)
            and args_is_native
            and len(obj.obj.args) == 1
            and not obj.obj.kwargs
        ):
            arg_name = self._typing_optional_alias_arg_name(obj.obj.args[0])
            if arg_name is not None:
                return self._emit_str_literal(arg_name)
        return None

    def _maybe_emit_protocol_isinstance(
        self,
        obj_expr: Expr,
        cls_ident: str,
    ) -> Optional[ir.Value]:
        cls_info = self.class_lowering.classes.get(cls_ident)
        if cls_info is None:
            return None
        required = tuple(getattr(cls_info, "protocol_members", ()))
        if not required:
            return None
        obj_hint = self._class_hint_for_expr(obj_expr)
        if obj_hint is None:
            return None
        obj_info = self.class_lowering.classes.get(obj_hint)
        if obj_info is None:
            return None
        available = set(obj_info.field_names)
        available.update(obj_info.class_attrs.keys())
        ok = all(name in available for name in required)
        return ir.Constant(_I1, 1 if ok else 0)
