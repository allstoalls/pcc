"""Small IR declaration helpers for Layer-1 codegen."""
from __future__ import annotations

from pcc.ir.compat import ir


_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_CSTR = _I8.as_pointer()
_SANITIZED_IR_NAME_HINT_CACHE: dict[str, str] = {}


def _sanitize_ir_name_hint(hint: str) -> str:
    """Keep generated IR names printable without relying on LLVM quoting."""
    cached = _SANITIZED_IR_NAME_HINT_CACHE.get(hint)
    if cached is not None:
        return cached
    out = []
    for ch in hint:
        if (
            "a" <= ch <= "z"
            or "A" <= ch <= "Z"
            or "0" <= ch <= "9"
            or ch in "._$-"
        ):
            out.append(ch)
        else:
            out.append("_")
    sanitized = "".join(out).strip("._-")
    if not sanitized:
        _SANITIZED_IR_NAME_HINT_CACHE[hint] = "t"
        return "t"
    first = sanitized[0]
    if not ("a" <= first <= "z" or "A" <= first <= "Z" or first in "_$"):
        sanitized = "t." + sanitized
    _SANITIZED_IR_NAME_HINT_CACHE[hint] = sanitized
    return sanitized


class IrDeclHelperMixin:
    def _fresh(self, hint: str = "t") -> str:
        self._tmp_counter += 1
        return _sanitize_ir_name_hint(hint) + "." + str(self._tmp_counter)

    def _declare_printf(self) -> ir.Function:
        existing = self.module.globals.get("printf")
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_I32, [_CSTR], var_arg=True)
        fn = ir.Function(self.module, fnty, name="printf")
        fn.linkage = "external"
        return fn

    def _declare_external_function(
        self,
        name: str,
        ret_ty: ir.Type,
        param_tys: list[ir.Type],
        *,
        var_arg: bool = False,
    ) -> ir.Function:
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(ret_ty, param_tys, var_arg=var_arg)
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "external"
        return fn
