"""Native system/process module lowering helpers for layer-1 codegen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir
from pcc.python_target import (
    PYTHON_TARGET_MAJOR,
    PYTHON_TARGET_MINOR,
    PYTHON_TARGET_MICRO,
    PYTHON_TARGET_VERSION_INFO,
)

from ..py_ast import Attr, BoolLit, Call, Expr, Name, StrLit, StrType
from . import marshal
from .layer1_support import _native_stdlib_semantic_provider

_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class NativeSystemLoweringMixin:
    def _emit_sys_version_info_tuple(self) -> ir.Value:
        values = PYTHON_TARGET_VERSION_INFO
        version_info = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(values))],
            name=self._fresh("sys.version_info.tuple"),
        )
        for idx, value in enumerate(values):
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [
                    version_info,
                    ir.Constant(_I64, idx),
                    self.builder.call(
                        self.runtime["py_int_from_i64"],
                        [ir.Constant(_I64, value)],
                        name=self._fresh("sys.version_info.part"),
                    ),
                ],
                name=self._fresh("sys.version_info.set"),
            )
        return version_info

    def _emit_sys_version_info_attr(self, name: str) -> Optional[ir.Value]:
        values = {
            "major": PYTHON_TARGET_MAJOR,
            "minor": PYTHON_TARGET_MINOR,
            "micro": PYTHON_TARGET_MICRO,
        }
        value = values.get(name)
        if value is None:
            return None
        return self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, value)],
            name=self._fresh(f"sys.version_info.{name}"),
        )

    def _subprocess_check_output_text_mode(self, expr: Call) -> Optional[bool]:
        text_mode = False
        seen_text = False
        for key, value in expr.kwargs:
            if key in ("text", "universal_newlines"):
                if not isinstance(value, BoolLit) or not value.value:
                    return None
                seen_text = True
                text_mode = True
                continue
            if key == "stderr":
                if not (
                    isinstance(value, Attr)
                    and value.name == "STDOUT"
                    and isinstance(value.obj, Name)
                    and self._native_builtin_module_for_name(value.obj.ident)
                    == "subprocess"
                ):
                    return None
                continue
            return None
        # text=True  -> True  (py_subprocess_check_output result decoded to str)
        # no text=   -> False (native bytes; downstream .decode()/bytes methods
        #              stay native via _maybe_emit_bytes_method_via_dyn)
        # unsupported kwarg above already returned None to bail to CPython.
        return True if seen_text else False

    def _emit_native_called_process_error(
        self,
        module_alias: str,
        rc: ir.Value,
        argv_obj: ir.Value,
        span,
    ) -> ir.Value:
        """Instantiate the pcc-Python ``subprocess.CalledProcessError``.

        ``subprocess`` is compiled from ``pcc/py_stdlib/subprocess.py`` in the
        recursive native-stdlib closure. Reuse that exported class and its
        ``__init__`` instead of growing a second subprocess exception model in
        the C runtime.
        """
        module_name = self._native_builtin_module_for_name(module_alias)
        native_exports = self._native_module_exports or {}
        export_info = native_exports.get(module_name or "", {}).get(
            "CalledProcessError"
        )
        if not isinstance(export_info, dict) or export_info.get("kind") != "class":
            # The module-level static-native export table is keyed by which
            # module is being compiled, which says nothing about whether this
            # lowering fires: every module split out of pipeline.py since that
            # allow-list was written reached here without the declaration.
            # The provider is admitted on its own.
            export_info = _native_stdlib_semantic_provider(
                module_name, "CalledProcessError"
            )
        if not isinstance(export_info, dict) or export_info.get("kind") != "class":
            raise NotImplementedError(
                "native subprocess check=True requires the "
                "pcc-Python CalledProcessError export"
            )
        class_info = self._declare_native_module_extern_class(
            owning_module=export_info.get("owning_module", module_name),
            class_name=export_info["class_name"],
            field_names=export_info.get("field_names", ()),
            methods=export_info.get("methods", ()),
            local_name="CalledProcessError",
            base_names=export_info.get("base_names", ()),
            field_types=export_info.get("field_types", ()),
        )
        if class_info.init_fn is None:
            raise NotImplementedError(
                "native subprocess check=True requires " "CalledProcessError.__init__"
            )

        cls_obj = self.builder.load(
            class_info.global_var,
            name=self._fresh("subprocess.CalledProcessError.class"),
        )
        exc = self.builder.call(
            self.runtime["py_instance_new"],
            [cls_obj],
            name=self._fresh("subprocess.CalledProcessError"),
        )
        self._gc_pin(exc)
        none_obj = self._emit_none_literal()
        self.builder.call(
            class_info.init_fn,
            [exc, rc, argv_obj, none_obj, none_obj],
        )
        self._gc_unpin(exc)
        self._emit_post_call_err_check(
            span,
            release_on_error=(exc,),
        )
        return exc

    def _emit_native_subprocess_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "subprocess"
        ):
            return None
        if attr.name not in ("check_output", "check_call"):
            return None
        if len(expr.args) != 1:
            return None
        if attr.name == "check_output":
            text_mode = self._subprocess_check_output_text_mode(expr)
            if text_mode is None:
                return None
        elif expr.kwargs:
            return None
        argv = self._emit_expr(expr.args[0])
        if argv in getattr(self, "_cpy_values", ()):
            return None
        argv_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            argv,
            expr.args[0].ty,
        )
        if attr.name == "check_output":
            res = self.builder.call(
                self.runtime["py_subprocess_check_output"],
                [argv_obj],
                name=self._fresh("subprocess.check_output"),
            )
            self._emit_post_call_err_check(expr.span)
            if text_mode:
                res = self.builder.call(
                    self.runtime["py_bytes_decode"],
                    [res],
                    name=self._fresh("subprocess.check_output.text"),
                )
            return res
        rc = self.builder.call(
            self.runtime["py_subprocess_run"],
            [argv_obj, ir.Constant(_I32, 0)],
            name=self._fresh("subprocess.check_call"),
        )
        failed = self.builder.icmp_signed(
            "!=",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("subprocess.check_call.failed"),
        )
        fn = self.current_function
        fail_bb = fn.append_basic_block(name=self._fresh("subprocess.check_call.fail"))
        ok_bb = fn.append_basic_block(name=self._fresh("subprocess.check_call.ok"))
        self.builder.cbranch(failed, fail_bb, ok_bb)
        self.builder.position_at_end(fail_bb)
        exc = self._emit_native_called_process_error(
            attr.obj.ident,
            rc,
            argv_obj,
            expr.span,
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)
        res = self.builder.call(
            self.runtime["py_int_from_i64"],
            [rc],
            name=self._fresh("subprocess.check_call.result"),
        )
        self._emit_post_call_err_check(expr.span)
        return res

    def _emit_native_subprocess_run_stmt(self, expr: Call) -> bool:
        if not isinstance(expr.func, Attr):
            return False
        attr = expr.func
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "subprocess"
            or attr.name != "run"
            or len(expr.args) != 1
        ):
            return False
        check_true = False
        capture_output_val = ir.Constant(_I32, 0)
        timeout_ms = None
        for key, value in expr.kwargs:
            if key == "check":
                if not isinstance(value, BoolLit) or not value.value:
                    return False
                check_true = True
            elif key == "capture_output":
                if isinstance(value, BoolLit):
                    capture_output_val = ir.Constant(
                        _I32,
                        1 if value.value else 0,
                    )
                else:
                    raw = self._emit_expr(value)
                    truthy = self._truthy(raw, value.ty)
                    capture_output_val = self.builder.zext(
                        truthy,
                        _I32,
                        name=self._fresh("subprocess.capture"),
                    )
            elif key == "text":
                if not isinstance(value, BoolLit):
                    return False
            elif key == "timeout":
                timeout_seconds = self._emit_expr_as_i64(value)
                timeout_ms = self.builder.mul(
                    timeout_seconds,
                    ir.Constant(_I64, 1000),
                    name=self._fresh("subprocess.timeout.ms"),
                )
            else:
                return False
        if not check_true:
            return False
        argv = self._emit_expr(expr.args[0])
        if argv in getattr(self, "_cpy_values", ()):
            return False
        argv_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            argv,
            expr.args[0].ty,
        )
        if timeout_ms is None:
            rc = self.builder.call(
                self.runtime["py_subprocess_run"],
                [argv_obj, capture_output_val],
                name=self._fresh("subprocess.run"),
            )
        else:
            rc = self.builder.call(
                self.runtime["py_subprocess_run_timeout"],
                [argv_obj, capture_output_val, timeout_ms],
                name=self._fresh("subprocess.run.timeout"),
            )
        failed = self.builder.icmp_signed(
            "!=",
            rc,
            ir.Constant(_I64, 0),
            name=self._fresh("subprocess.run.failed"),
        )
        fn = self.current_function
        fail_bb = fn.append_basic_block(name=self._fresh("subprocess.run.fail"))
        ok_bb = fn.append_basic_block(name=self._fresh("subprocess.run.ok"))
        if timeout_ms is None:
            self.builder.cbranch(failed, fail_bb, ok_bb)
        else:
            timeout_bb = fn.append_basic_block(
                name=self._fresh("subprocess.run.timeout")
            )
            status_bb = fn.append_basic_block(name=self._fresh("subprocess.run.status"))
            timed_out = self.builder.icmp_signed(
                "==",
                rc,
                ir.Constant(_I64, -124),
                name=self._fresh("subprocess.run.timed_out"),
            )
            self.builder.cbranch(timed_out, timeout_bb, status_bb)

            self.builder.position_at_end(timeout_bb)
            timeout_exc = self.builder.call(
                self.runtime["py_exc_new"],
                [
                    ir.Constant(_I64, 7),
                    self._ptr_to_cstr(
                        self._cstr_global(
                            "subprocess.run timed out",
                            self._fresh(".err.subprocess.run.timeout"),
                        )
                    ),
                ],
                name=self._fresh("subprocess.run.timeout.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [timeout_exc])
            timeout_err_target = getattr(self, "_try_err_block", None)
            if timeout_err_target is None:
                timeout_err_target = self._ensure_fn_err_exit()
            self.builder.branch(timeout_err_target)

            self.builder.position_at_end(status_bb)
            self.builder.cbranch(failed, fail_bb, ok_bb)
        self.builder.position_at_end(fail_bb)
        exc = self._emit_native_called_process_error(
            attr.obj.ident,
            rc,
            argv_obj,
            expr.span,
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)
        return True

    def _emit_native_shutil_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "shutil"
            or attr.name != "which"
            or len(expr.args) != 1
            or expr.kwargs
        ):
            return None
        name_obj = self._emit_as_object(expr.args[0])
        return self.builder.call(
            self.runtime["py_shutil_which"],
            [name_obj],
            name=self._fresh("shutil.which"),
        )

    def _emit_native_shlex_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "shlex"
            or attr.name != "split"
            or len(expr.args) != 1
        ):
            return None
        if expr.kwargs:
            if len(expr.kwargs) != 1:
                return None
            kw_name, kw_value = expr.kwargs[0]
            if kw_name != "posix":
                return None
            if not isinstance(kw_value, BoolLit) or not kw_value.value:
                return None
        return self.builder.call(
            self.runtime["py_shlex_split"],
            [self._emit_as_object(expr.args[0])],
            name=self._fresh("shlex.split"),
        )

    def _emit_native_sysconfig_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "sysconfig"
            or attr.name != "get_config_var"
            or len(expr.args) != 1
            or expr.kwargs
        ):
            return None
        return self.builder.call(
            self.runtime["py_sysconfig_get_config_var"],
            [self._emit_as_object(expr.args[0])],
            name=self._fresh("sysconfig.get_config_var"),
        )

    def _native_builtin_stream_kind_for_expr(
        self,
        expr: Expr,
    ) -> Optional[str]:
        if (
            isinstance(expr, Attr)
            and expr.name in ("stdin", "stdout", "stderr")
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "sys"
        ):
            return expr.name
        if isinstance(expr, Name):
            value_kind = self._native_builtin_value_for_name(expr.ident)
            if value_kind == "sys.stdin":
                return "stdin"
            if value_kind == "sys.stdout":
                return "stdout"
            if value_kind == "sys.stderr":
                return "stderr"
        return None

    def _emit_native_sys_stream_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs:
            return None
        stream_kind = self._native_builtin_stream_kind_for_expr(attr.obj)
        if stream_kind is None:
            return None
        if stream_kind == "stdin":
            if attr.name == "readline" and not expr.args:
                return self.builder.call(
                    self.runtime["py_sys_stdin_readline"],
                    [],
                    name=self._fresh("sys.stdin.readline"),
                )
            return None
        if attr.name == "flush" and not expr.args:
            return self._emit_none_literal()
        if len(expr.args) != 1 or attr.name != "write":
            return None
        helper = (
            "py_sys_stdout_write" if stream_kind == "stdout" else "py_sys_stderr_write"
        )
        return self.builder.call(
            self.runtime[helper],
            [self._emit_as_object(expr.args[0])],
            name=self._fresh(f"sys.{stream_kind}.write"),
        )

    def _try_emit_native_file_stream_print(self, call: Call) -> bool:
        """Lower ``print(*args, file=sys.stderr|sys.stdout, ...)`` natively.

        The runtime helpers write directly to file descriptors 1 and 2, so a
        bool-literal ``flush=`` is a no-op: there is no userspace stream buffer
        left to flush.  A dynamic flush expression still falls back because
        evaluating its truthiness can have observable behavior.

        Restrictions: ``sep`` / ``end`` must be string literals when
        present (otherwise we'd need a runtime helper to read their bytes).
        Other kwargs trigger fallback.
        """
        file_expr = None
        sep_expr = None
        end_expr = None
        for k, v in call.kwargs:
            if k == "file":
                file_expr = v
            elif k == "sep":
                sep_expr = v
            elif k == "end":
                end_expr = v
            elif k == "flush" and isinstance(v, BoolLit):
                continue
            else:
                return False
        if file_expr is None:
            return False
        stream_kind = self._native_builtin_stream_kind_for_expr(file_expr)
        if stream_kind not in ("stdout", "stderr"):
            return False
        helper = self.runtime[
            "py_sys_stdout_write" if stream_kind == "stdout" else "py_sys_stderr_write"
        ]
        sep_str = " "
        end_str = "\n"
        if sep_expr is not None:
            if not isinstance(sep_expr, StrLit):
                return False
            sep_str = sep_expr.value
        if end_expr is not None:
            if not isinstance(end_expr, StrLit):
                return False
            end_str = end_expr.value
        # Empty print(file=...) just writes the end string.
        if not call.args:
            end_v = self._emit_str_literal(end_str)
            self.builder.call(helper, [end_v])
            return True
        sep_val = None
        for i, arg in enumerate(call.args):
            if i > 0:
                if sep_val is None:
                    sep_val = self._emit_str_literal(sep_str)
                self.builder.call(helper, [sep_val])
            arg_obj = self._emit_as_object(arg)
            if isinstance(arg.ty, StrType):
                arg_str = arg_obj
            else:
                arg_str = self.builder.call(
                    self.runtime["py_obj_str"],
                    [arg_obj],
                    name=self._fresh("print.str"),
                )
            self.builder.call(helper, [arg_str])
        end_v = self._emit_str_literal(end_str)
        self.builder.call(helper, [end_v])
        return True

    def _emit_native_sys_exit_call(self, expr: Call) -> Optional[ir.Value]:
        if expr.kwargs or len(expr.args) > 1:
            return None
        kind = None
        if isinstance(expr.func, Name):
            kind = self._native_builtin_value_for_name(expr.func.ident)
        elif isinstance(expr.func, Attr):
            kind = self._native_builtin_value_kind_for_expr(expr.func)
        if kind != "sys.exit":
            return None
        code_i64 = ir.Constant(_I64, 0)
        if len(expr.args) == 1:
            code_val = self._emit_expr(expr.args[0])
            code_i64 = self._to_int64(code_val, expr.args[0].ty)
        self.builder.call(
            self.runtime["py_process_exit"],
            [code_i64],
        )
        return ir.Constant(_CSTR, None)

    def _declare_strlen(self) -> ir.Function:
        fn = self.module.globals.get("strlen")
        if isinstance(fn, ir.Function):
            return fn
        fnty = ir.FunctionType(_I64, [_CSTR], var_arg=False)
        fn = ir.Function(self.module, fnty, name="strlen")
        fn.linkage = "external"
        return fn

    def _emit_program_argv_list(self) -> ir.Value:
        argc = self.builder.call(
            self.runtime["py_program_argc"],
            [],
            name=self._fresh("argv.argc"),
        )
        lst = self.builder.call(
            self.runtime["py_list_new"],
            [argc],
            name=self._fresh("argv.list"),
        )
        idx_slot = self._alloca_in_entry(_I64, name=self._fresh("argv.i"))
        self.builder.store(ir.Constant(_I64, 0), idx_slot)
        fn = self.builder._block.function
        cond_bb = fn.append_basic_block(name=self._fresh("argv.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("argv.body"))
        step_bb = fn.append_basic_block(name=self._fresh("argv.step"))
        end_bb = fn.append_basic_block(name=self._fresh("argv.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("argv.cur"))
        more = self.builder.icmp_signed(
            "<",
            cur,
            argc,
            name=self._fresh("argv.more"),
        )
        self.builder.cbranch(more, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        raw = self.builder.call(
            self.runtime["py_program_argv"],
            [cur],
            name=self._fresh("argv.raw"),
        )
        n_bytes = self.builder.call(
            self._declare_strlen(),
            [raw],
            name=self._fresh("argv.len"),
        )
        item = self.builder.call(
            self.runtime["py_str_new"],
            [raw, n_bytes],
            name=self._fresh("argv.str"),
        )
        self.builder.call(
            self.runtime["py_list_append"],
            [lst, item],
        )
        self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        cur2 = self.builder.load(idx_slot, name=self._fresh("argv.cur2"))
        nxt = self.builder.add(
            cur2,
            ir.Constant(_I64, 1),
            name=self._fresh("argv.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        return lst


__all__ = ["NativeSystemLoweringMixin"]
