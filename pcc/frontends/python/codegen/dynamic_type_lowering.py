"""Enum and dynamic type helper lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, Call, Name

_I64 = ir.IntType(64)


class DynamicTypeLoweringMixin:
    def _maybe_emit_enum_member_attr(self, expr: Attr) -> Optional[ir.Value]:
        if expr.name not in ("name", "value"):
            return None
        if not (isinstance(expr.obj, Attr) and isinstance(expr.obj.obj, Name)):
            return None
        class_name = expr.obj.obj.ident
        member_name = expr.obj.name
        info = self.class_lowering.classes.get(class_name)
        if info is None:
            return None
        enum_members = getattr(info, "enum_members", {})
        enum_string_members = getattr(info, "enum_string_members", {})
        if member_name not in enum_members and member_name not in enum_string_members:
            return None
        if expr.name == "name":
            return self._emit_str_literal(member_name)
        if member_name in enum_string_members:
            return self._emit_str_literal(enum_string_members[member_name])
        return ir.Constant(_I64, int(enum_members[member_name]))

    def _emit_owned_dynamic_type_constructor(self, expr: Call, operands) -> ir.Value:
        """Keep type operands alive across later evaluation and runtime entry."""
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("type.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        arguments = []
        try:
            for operand in operands:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                owner = self._emit_slot_call_operand(operand, "type.argument")
                arguments.append(owner)
                roots.append(owner)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_class_new_from_objects", tuple(arguments), result_slot=output,
                span=expr.span,
            )
            self._release_slot_call_roots(tuple(arguments))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("type.result.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _maybe_emit_dynamic_type_constructor(self, expr: Call) -> Optional[ir.Value]:
        if expr.kwargs or len(expr.args) != 3:
            return None
        # Use the evaluated dictionary itself, including aliases, mutations
        # and real dict subclasses. A remembered literal AST is not its owner.
        return self._emit_owned_dynamic_type_constructor(expr, expr.args)
