"""Extern metadata helper lowering for Layer-1 Python codegen."""

from __future__ import annotations

from typing import Optional

from ..export_meta import decode_type
from ..py_ast import (
    Arg,
    Attr,
    Call,
    DynType,
    FuncDef,
    Name,
    NoneType,
    SourceSpan,
    StrLit,
    StrType,
)
from .errors import L1CodegenError

_DYN = DynType(name="dyn")
_NATIVE_DEFAULT_FUNC_SENTINEL = "__pcc_native_default_func_ref__"
_NATIVE_DEFAULT_GLOBAL_SENTINEL = "__pcc_native_default_global_ref__"


def _extern_default_expr(arg: dict, span: SourceSpan):
    ref = arg.get("default_native_func")
    if isinstance(ref, dict):
        owning_module = ref.get("owning_module")
        name = ref.get("name")
        if owning_module and name:
            return Call(
                span=span,
                ty=DynType(name="dyn"),
                func=Name(span, DynType(name="dyn"), _NATIVE_DEFAULT_FUNC_SENTINEL),
                args=(
                    StrLit(span, StrType(name="str"), str(owning_module)),
                    StrLit(span, StrType(name="str"), str(name)),
                ),
                kwargs=(),
            )
    gref = arg.get("default_native_global")
    if isinstance(gref, dict):
        owning_module = gref.get("owning_module")
        name = gref.get("name")
        if owning_module and name:
            default_expr = Call(
                span=span,
                ty=decode_type(gref.get("value_ty")) or DynType(name="dyn"),
                func=Name(span, DynType(name="dyn"), _NATIVE_DEFAULT_GLOBAL_SENTINEL),
                args=(
                    StrLit(span, StrType(name="str"), str(owning_module)),
                    StrLit(span, StrType(name="str"), str(name)),
                ),
                kwargs=(),
            )
            for attr_name in gref.get("attrs", ()):
                default_expr = Attr(
                    span=span,
                    ty=DynType(name="dyn"),
                    obj=default_expr,
                    name=str(attr_name),
                )
            return default_expr
    return arg.get("default")


class ExternFuncInfoLoweringMixin:
    def _extern_info_to_funcdef(self, name: str, info: dict) -> Optional[FuncDef]:
        call_sig = info.get("call_sig")
        if call_sig is None:
            return None
        span = SourceSpan(
            file="<extern>",
            line=0,
            col=0,
            end_line=0,
            end_col=0,
        )
        args = []
        for arg in call_sig:
            args.append(
                Arg(
                    name=arg["name"],
                    annotation=decode_type(arg.get("annotation")),
                    default=_extern_default_expr(arg, span),
                    kind=arg.get("kind", "pos"),
                    has_default=arg.get(
                        "has_default",
                        arg.get("default") is not None,
                    ),
                )
            )
        # The schema-carried bool survives self-hosted descriptor degradation
        # to ("dyn",); keep the reconstructed FuncDef's return coherent with
        # the void extern declaration (see
        # native_modules._extern_user_function_return_ir_type).
        if "returns_none" in info and bool(info["returns_none"]):
            return_ty = NoneType("None")
        else:
            return_ty = decode_type(info.get("return_ty")) or DynType(name="dyn")
        return FuncDef(
            span=span,
            name=name,
            args=tuple(args),
            return_ty=return_ty,
            body=(),
            decorators=(),
            is_async=bool(info.get("is_async", False)),
        )

    def _find_user_funcdef(self, name: str) -> FuncDef:
        # ``ast_module.body`` is authoritative after nested-function hoisting.
        # The `_ast_body` compatibility alias can remain stale in a compiled
        # stage when `setattr(self, "_ast_body", ...)` and fixed instance slots
        # take different paths, so consult the current module first.
        current_body = getattr(self.ast_module, "body", ())
        for stmt in current_body:
            # AST-wire workers reconstruct nodes in a sibling compiled module.
            # Under pcc1 that can make Python class identity differ even though
            # the wire node is structurally a FuncDef.  Match the stable node
            # kind as well as host-Python identity so top-level and hoisted
            # nested functions remain usable as first-class values.
            is_funcdef = isinstance(stmt, FuncDef)
            if not is_funcdef:
                is_funcdef = type(stmt).__name__ == "FuncDef"
            if is_funcdef and stmt.name == name:
                return stmt
        for stmt in self._ast_body:
            is_funcdef = isinstance(stmt, FuncDef)
            if not is_funcdef:
                is_funcdef = type(stmt).__name__ == "FuncDef"
            if is_funcdef and stmt.name == name:
                return stmt
        # Cross-module: name was imported from a native sibling via
        # ``from .other import name`` during multi-file compile.
        cm = self._cross_module_func_defs
        if name in cm and cm[name] is not None:
            return cm[name]
        block_defs = getattr(self, "_module_block_func_defs", {})
        if name in block_defs:
            return block_defs[name]
        raise L1CodegenError(f"no FuncDef for user function {name!r}")
