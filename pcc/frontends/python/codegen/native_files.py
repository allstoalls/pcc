"""Native file-object lowering helpers."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Assign, Attr, AugAssign, Call, Delete, DynType, FuncDef, Import,
    Name, NoneLit, NoneType, StrLit, StrType, With,
)
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.hoist_analysis import (
    _dataclass_field_names, _dataclass_field_value,
)
from pcc.frontends.python.codegen.hoist_boxing import function_local_bindings
from pcc.frontends.python.codegen.local_bound_lowering import check_local_bound

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


def _builtin_open_namespace_can_change(host):
    """Fail closed when this unit mutates or exposes a builtin namespace.

    Function-local bindings mask outer aliases. A qualified intrinsic cannot
    model a runtime replacement, reflective access, or an escaped module.
    """
    def imports(block):
        names = set()
        pending = list(block)
        while pending:
            node = pending.pop()
            if isinstance(node, FuncDef):
                continue
            if isinstance(node, Import):
                for module, alias in node.names:
                    if module == "builtins":
                        names.add(alias or module)
                continue
            if node is None or isinstance(node, (str, int, float, bool, bytes)):
                continue
            if isinstance(node, (tuple, list)):
                pending.extend(node)
                continue
            for field in _dataclass_field_names(node):
                if field not in ("span", "ty", "annotation", "return_ty"):
                    pending.append(_dataclass_field_value(node, field, None))
        return names

    def walk(node, aliases):
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            return False
        if isinstance(node, (tuple, list)):
            return any(walk(item, aliases) for item in node)
        if isinstance(node, Import):
            return False
        if isinstance(node, FuncDef):
            for arg in node.args:
                if walk(arg.default, aliases):
                    return True
            local = (aliases - set(function_local_bindings(node))) | imports(node.body)
            return walk(node.body, local)
        if isinstance(node, Name):
            return node.ident in aliases
        if isinstance(node, (Assign, AugAssign, Delete)):
            targets = (node.target,) if isinstance(node, AugAssign) else node.targets
            for target in targets:
                if (isinstance(target, Attr) and target.name == "open"
                        and isinstance(target.obj, Name) and target.obj.ident in aliases):
                    return True
        if isinstance(node, Attr) and isinstance(node.obj, Name) and node.obj.ident in aliases:
            return node.name == "__dict__"
        for field in _dataclass_field_names(node):
            if field not in ("span", "ty", "annotation", "return_ty"):
                if walk(_dataclass_field_value(node, field, None), aliases):
                    return True
        return False

    return walk(host.ast_module.body, imports(host.ast_module.body))


class NativeFilesLoweringMixin:
    def _emit_native_open_call(self, expr: Call, result_slot=None) -> Optional[ir.Value]:
        """Lower an unshadowed builtin open through authoritative operands."""
        func = expr.func
        if isinstance(func, Name):
            if func.ident != "open":
                return None
            if (func.ident in self.env or func.ident in self._module_globals
                    or func.ident in self.functions
                    or func.ident in getattr(self, "_cpy_module_env", {})
                    or func.ident in getattr(self, "_cpy_star_module_env", {})):
                return None
        elif isinstance(func, Attr) and isinstance(func.obj, Name):
            if (func.name != "open"
                    or self._native_builtin_module_for_name(func.obj.ident) != "builtins"):
                return None
            # A real runtime binding/override cannot be replaced by a direct
            # call to the default builtin. First-class open binding is not
            # implemented by this intrinsic producer.
            if (func.obj.ident in self._module_globals
                    or self._native_module_attr_global_if_exists("builtins", "open") is not None
                    or _builtin_open_namespace_can_change(self)):
                raise L1CodegenError(
                    "native builtins.open requires an unmodified builtin namespace"
                )
            check_local_bound(self, func.obj)
        else:
            return None
        if not 1 <= len(expr.args) <= 2 or self._has_unpacked_operands(expr.args, expr.kwargs):
            return None
        # Preserve every option operand and its evaluation order. The runtime
        # validates codec/error/newline support before opening the file.
        if any(key not in ("encoding", "errors", "newline") for key, _ in expr.kwargs):
            return None

        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = result_slot if result_slot is not None else self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("file.open.result")
            roots.append(output)
        operands = []
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            path = self._emit_os_path_slot_operand(expr.args[0], "file.open.path")
            operands.append(path)
            roots.append(path)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            mode_expr = (expr.args[1] if len(expr.args) == 2
                         else StrLit(span=expr.span, ty=StrType(name="str"), value="r"))
            mode = self._emit_os_path_slot_operand(mode_expr, "file.open.mode")
            operands.append(mode)
            roots.append(mode)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            options = {}
            for key, value in expr.kwargs:
                option = self._emit_os_path_slot_operand(value, "file.open." + key)
                options[key] = option
                operands.append(option)
                roots.append(option)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            for key in ("encoding", "errors", "newline"):
                if key not in options:
                    option = self._emit_slot_call_operand(NoneLit(span=expr.span, ty=NoneType(name="None")), "file.open." + key)
                    options[key] = option
                    operands.append(option)
                    roots.append(option)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_file_open_options",
                (path, mode, options["encoding"], options["errors"], options["newline"]),
                result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots(tuple(operands))
            result = (self._take_slot_call_root(output) if sink is None
                      else self.builder.load(output, name=self._fresh("file.open.current")))
            self._native_file_values.add(result)
            return result
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_native_file_method(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs:
            return None
        if not isinstance(attr.obj, Name):
            return None
        if not self._native_file_env_flags.get(attr.obj.ident, False):
            return None
        if attr.name in ("read", "readline") and len(expr.args) <= 1:
            # These helpers return a NEW str/bytes owner. Keep the receiver
            # authoritative across limit evaluation and publish the result
            # before TLS checks, lease retirement or operand disposal.
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("file.read.result")
                roots.append(output)
            operands = []
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                receiver = self._emit_slot_call_operand(attr.obj, "file.read.receiver")
                operands.append(receiver)
                roots.append(receiver)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                limit = ir.Constant(_I64, -1)
                if expr.args:
                    argument = self._emit_slot_call_operand(expr.args[0], "file.read.limit")
                    operands.append(argument)
                    roots.append(argument)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    limit = self._slot_call_runtime_call(
                        "py_index_i64_checked", (argument,), span=expr.args[0].span,
                    )
                runtime_name = "py_file_readline" if attr.name == "readline" else "py_file_read"
                suffix = (limit,)
                if attr.name == "read" and not expr.args:
                    runtime_name = "py_file_read_all"
                    suffix = ()
                self._slot_call_runtime_call(
                    runtime_name, (receiver,), result_slot=output,
                    suffix_args=suffix, span=expr.span,
                )
                self._release_slot_call_roots(tuple(operands))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh("file.read.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        if attr.name == "write" and len(expr.args) == 1:
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("file.write.result")
                roots.append(output)
            operands = []
            try:
                for argument, label in ((attr.obj, "file.write.receiver"), (expr.args[0], "file.write.text")):
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    operand = self._emit_os_path_slot_operand(argument, label)
                    operands.append(operand)
                    roots.append(operand)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call("py_file_write", tuple(operands), result_slot=output, span=expr.span)
                self._release_slot_call_roots(tuple(operands))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh("file.write.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        if attr.name in ("tell", "flush", "fileno") and not expr.args:
            # These unary helpers return a NEW owner (boxed int or None).
            # Publish through the original Call's sink before error checks,
            # lease retirement or disposal, just like other owned producers.
            return self._emit_owned_unary_runtime_call(expr, "py_file_" + attr.name)
        recv = self._emit_expr(attr.obj)
        if attr.name == "seek" and 1 <= len(expr.args) <= 2:
            offset_v = self._emit_expr(expr.args[0])
            offset_i64 = self._to_int64(offset_v, expr.args[0].ty)
            if len(expr.args) == 2:
                whence_v = self._emit_expr(expr.args[1])
                whence_i64 = self._to_int64(whence_v, expr.args[1].ty)
            else:
                whence_i64 = ir.Constant(_I64, 0)
            result = self.builder.call(
                self.runtime["py_file_seek"],
                [recv, offset_i64, whence_i64],
                name=self._fresh("file.seek"),
            )
            # Raises ValueError (closed file) / OSError (bad seek).
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        if attr.name == "close" and not expr.args:
            self.builder.call(self.runtime["py_file_close"], [recv])
            return self._emit_none_literal()
        return None

    def _emit_native_fileinput_call(self, expr: Call) -> Optional[ir.Value]:
        func = expr.func
        if (
            not isinstance(func, Attr)
            or func.name != "FileInput"
            or not isinstance(func.obj, Name)
            or self._native_builtin_module_for_name(func.obj.ident) != "fileinput"
            or len(expr.args) != 1
        ):
            return None
        openhook = self._emit_none_literal()
        for key, value in expr.kwargs:
            if key != "openhook":
                return None
            openhook = self._emit_expr_with_native_callable_values(value)
        files = self._emit_as_object(expr.args[0])
        result = self.builder.call(
            self.runtime["py_fileinput_new"],
            [files, openhook],
            name=self._fresh("fileinput.FileInput"),
        )
        if not hasattr(self, "_native_fileinput_values"):
            self._native_fileinput_values = set()
        self._native_fileinput_values.add(result)
        return result

    def _emit_native_fileinput_method(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs or expr.args:
            return None
        if not isinstance(attr.obj, Name):
            return None
        if not getattr(self, "_native_fileinput_env_flags", {}).get(
            attr.obj.ident, False
        ):
            return None
        helpers = {
            "readline": "py_fileinput_readline",
            "filename": "py_fileinput_filename",
            "lineno": "py_fileinput_lineno",
            "filelineno": "py_fileinput_filelineno",
            "isfirstline": "py_fileinput_isfirstline",
            "close": "py_fileinput_close",
        }
        helper = helpers.get(attr.name)
        if helper is None:
            return None
        recv = self._emit_expr(attr.obj)
        return self.builder.call(
            self.runtime[helper],
            [recv],
            name=self._fresh("fileinput." + attr.name),
        )

    def _emit_native_file_with(self, stmt: With) -> bool:
        ctx_expr, as_expr = stmt.items[0]
        if not isinstance(ctx_expr, Call) or (as_expr is not None and not isinstance(as_expr, Name)):
            return False
        func = ctx_expr.func
        if not ((isinstance(func, Name) and func.ident == "open")
                or (isinstance(func, Attr) and func.name == "open")):
            return False
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        context_root = self._new_slot_call_root("with.file.manager")
        roots = [context_root]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            file_val = self._emit_native_open_call(ctx_expr, result_slot=context_root)
            if file_val is None:
                self._release_slot_call_roots(tuple(roots))
                return False
            enter_root = self._new_slot_call_root("with.file.enter")
            roots.append(enter_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_copy_source(enter_root, context_root, span=stmt.span)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if isinstance(as_expr, Name):
            self._native_file_env_flags[as_expr.ident] = True
        self._emit_native_context_body(stmt, context_root, enter_root, "py_file_close")
        return True


    def _emit_native_tempdir_with(self, stmt: With) -> bool:
        # TemporaryDirectory is an ordinary managed class. The generic context
        # path keeps the manager separate from __enter__'s stable name string,
        # honors delete=False, and owns cleanup across every control-flow exit.
        return False


__all__ = ["NativeFilesLoweringMixin"]
