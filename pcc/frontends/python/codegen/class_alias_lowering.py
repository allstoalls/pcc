"""Class alias tracking helpers for Layer-1 codegen."""
from __future__ import annotations

from pcc.frontends.python.py_ast import Assign, Attr, Name


class ClassAliasLoweringMixin:
    def _resolve_class_alias(self, name: str) -> str:
        return self._class_aliases.get(name, name)

    def _class_object_hint_for_expr(self, expr) -> str | None:
        if isinstance(expr, Name):
            class_name = self._resolve_class_alias(expr.ident)
            if class_name in self.class_lowering.classes:
                return class_name
            return None
        if isinstance(expr, Attr):
            export = self._native_module_expr_export_info(expr.obj, expr.name)
            if export is None:
                return None
            _module_name, info = export
            if info.get("kind") != "class":
                return None
            if isinstance(expr.obj, Name):
                class_info = self._ensure_native_module_alias_class_export(
                    expr.obj.ident,
                    expr.name,
                )
                if class_info is not None:
                    return class_info.name
            return str(info.get("class_name", expr.name))
        return None

    def _maybe_register_class_alias_assign(self, stmt: Assign) -> bool:
        if len(stmt.targets) != 1:
            return False
        target = stmt.targets[0]
        value = stmt.value
        if not isinstance(target, Name) or not isinstance(value, Name):
            return False
        class_name = self._resolve_class_alias(value.ident)
        if class_name not in self.class_lowering.classes:
            return False
        self._class_aliases[target.ident] = class_name
        return True
