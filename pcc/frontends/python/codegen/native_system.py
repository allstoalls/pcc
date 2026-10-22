"""Native system/process module lowering helpers for layer-1 codegen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir
from pcc.driver.python_target import PYTHON_TARGET_MAJOR, PYTHON_TARGET_MINOR, PYTHON_TARGET_MICRO, PYTHON_TARGET_VERSION_INFO

from pcc.frontends.python.py_ast import Attr, BoolLit, Call, DynType, Expr, IntType, Name, Raise, StrLit, StrType
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.layer1_support import _native_stdlib_semantic_provider

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
        boxed = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, value)],
            name=self._fresh(f"sys.version_info.{name}"),
        )
        self._note_owned_object_value(boxed)
        return boxed

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

        ``subprocess`` is compiled from ``pcc/stdlib/subprocess.py`` in the
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

        # The runtime status is a machine i64, whereas the semantic provider
        # may use an ordinary Python-int (object) parameter. The declared
        # operand ABI, not scaffold mode or stale export metadata, is final.
        status_type = class_info.init_fn.args[1].type
        if status_type != rc.type and not isinstance(status_type, ir.PointerType):
            raise NotImplementedError(
                "native subprocess returncode requires an i64 or object ABI"
            )
        argument_roots = [self._extern_enter_root(
            argv_obj, False, "subprocess.CalledProcessError.argv"
        )]
        previous_err = self._current_try_err_block()
        error_target = previous_err
        if error_target is None:
            error_target = self._ensure_fn_err_exit()
        self._try_err_block = self._extern_cleanup_block(
            tuple(argument_roots), error_target
        )
        if isinstance(status_type, ir.PointerType):
            rc = self.builder.call(
                self.runtime["py_int_from_i64"], [rc],
                name=self._fresh("subprocess.CalledProcessError.returncode"),
            )
            self._emit_post_call_err_check(span, release_on_error=(rc,))
            argument_roots.append(self._extern_enter_root(
                rc, True, "subprocess.CalledProcessError.returncode"
            ))
            self._try_err_block = self._extern_cleanup_block(
                tuple(argument_roots), error_target
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
        self._emit_post_call_err_check(
            span, pinned_release_on_error=((exc, True),)
        )
        none_obj = self._emit_none_literal()
        if isinstance(status_type, ir.PointerType):
            rc = self._extern_load_root(argument_roots[1])
        argv_obj = self._extern_load_root(argument_roots[0])
        self.builder.call(
            class_info.init_fn,
            [exc, rc, argv_obj, none_obj, none_obj],
        )
        self._emit_post_call_err_check(
            span,
            pinned_release_on_error=((exc, True),),
        )
        self._extern_release_roots(argument_roots)
        self._gc_unpin(exc)
        self._try_err_block = previous_err
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
        if attr.name == "check_output":
            # A bytes result and its optional decoded string are independent
            # NEW owners. Keep argv and bytes authoritative until the next
            # producer publishes, including its error and cleanup edges.
            if self._expr_looks_cpython(expr.args[0]):
                return None
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("subprocess.output.result")
                roots.append(output)
            temporary_start = len(roots)
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                argv_root = self._emit_slot_call_operand(expr.args[0], "subprocess.output.argv")
                roots.append(argv_root)
                produced = output
                if text_mode:
                    produced = self._new_slot_call_root("subprocess.output.bytes")
                    roots.append(produced)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_subprocess_check_output", (argv_root,),
                    result_slot=produced, span=expr.span,
                )
                if text_mode:
                    self._slot_call_runtime_call(
                        "py_bytes_decode", (produced,), result_slot=output, span=expr.span,
                    )
                self._release_slot_call_roots(tuple(roots[temporary_start:]))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh("subprocess.output.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
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
                # Captured output belongs to the CalledProcessError (and the
                # result); only the provider's run() keeps it.
                if not isinstance(value, BoolLit) or value.value:
                    return False
            elif key == "text":
                if not isinstance(value, BoolLit):
                    return False
            elif key == "timeout":
                # Fractional seconds are real timeouts; this lane has only
                # whole seconds.
                if not isinstance(value.ty, IntType):
                    return False
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
        ):
            return None
        if attr.name == "rmtree" and 1 <= len(expr.args) <= 2:
            ignore_errors = ir.Constant(_I32, 0)
            if len(expr.args) == 2:
                if expr.kwargs:
                    return None
                raw = self._emit_expr(expr.args[1])
                ignore_errors = self.builder.zext(
                    self._truthy(raw, expr.args[1].ty),
                    _I32,
                    name=self._fresh("shutil.rmtree.ignore_errors"),
                )
            elif expr.kwargs:
                if len(expr.kwargs) != 1 or expr.kwargs[0][0] != "ignore_errors":
                    return None
                value_expr = expr.kwargs[0][1]
                raw = self._emit_expr(value_expr)
                ignore_errors = self.builder.zext(
                    self._truthy(raw, value_expr.ty),
                    _I32,
                    name=self._fresh("shutil.rmtree.ignore_errors"),
                )
            result = self.builder.call(
                self.runtime["py_shutil_rmtree"],
                [self._emit_as_object(expr.args[0]), ignore_errors],
                name=self._fresh("shutil.rmtree"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        if attr.name != "which" or len(expr.args) != 1:
            return None
        # which(cmd, mode=F_OK | X_OK, path=None): only ``path=`` is native.
        if any(key != "path" for key, _value in expr.kwargs):
            return None
        name_obj = self._emit_as_object(expr.args[0])
        if not expr.kwargs:
            return self.builder.call(
                self.runtime["py_shutil_which"],
                [name_obj],
                name=self._fresh("shutil.which"),
            )
        path_obj = self._emit_as_object(expr.kwargs[0][1])
        result = self.builder.call(
            self.runtime["py_shutil_which_path"],
            [name_obj, path_obj],
            name=self._fresh("shutil.which.path"),
        )
        self._emit_post_call_err_check(getattr(expr, "span", None))
        return result

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
        # The runtime returns a NEW list, including the empty case. The
        # unary producer owns its argument and publishes that result before
        # either the caller's next operand or operand cleanup can park.
        return self._emit_owned_unary_runtime_call(expr, "py_shlex_split")

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
                # The NEW line is published into its owner before any
                # failure branch, as every slot-call producer must.
                return self._emit_owned_operand_runtime_call(expr, "py_sys_stdin_readline", 0)
            return None
        if attr.name == "flush" and not expr.args:
            return self._emit_none_literal()
        if len(expr.args) != 1 or attr.name != "write":
            return None
        helper = (
            "py_sys_stdout_write" if stream_kind == "stdout" else "py_sys_stderr_write"
        )
        # write() returns a NEW character count, as TextIOWrapper.write does.
        return self._emit_owned_operand_runtime_call(expr, helper, 1)

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
        if self._has_starred_unpack(call.args):
            # ``print(*items, file=sys.stderr)``: the per-arg loop below would
            # evaluate the splat marker itself; the tuple path expands it.
            return False
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
        def write(text):
            # A failed write raises OSError where the print call is, as the
            # stream's write() would; its character count is not kept.
            self.builder.call(helper, [text])
            self._emit_post_call_err_check(call.span)

        # Empty print(file=...) just writes the end string.
        if not call.args:
            write(self._emit_str_literal(end_str))
            return True
        sep_val = None
        for i, arg in enumerate(call.args):
            if i > 0:
                if sep_val is None:
                    sep_val = self._emit_str_literal(sep_str)
                write(sep_val)
            arg_obj = self._emit_as_object(arg)
            if isinstance(arg.ty, StrType):
                arg_str = arg_obj
            else:
                arg_str = self.builder.call(
                    self.runtime["py_obj_str"],
                    [arg_obj],
                    name=self._fresh("print.str"),
                )
                self._emit_post_call_err_check(call.span)
            write(arg_str)
        write(self._emit_str_literal(end_str))
        return True

    def _emit_native_sys_exit_call(self, expr: Call) -> Optional[ir.Value]:
        if expr.kwargs or len(expr.args) > 1:
            return None
        kind = None
        if isinstance(expr.func, Name):
            kind = self._native_builtin_value_for_name(expr.func.ident)
        elif isinstance(expr.func, Attr):
            kind = self._native_builtin_value_kind_for_expr(expr.func)
            if (
                expr.func.name == "getrecursionlimit"
                and isinstance(expr.func.obj, Name)
                and self._native_builtin_module_for_name(expr.func.obj.ident) == "sys"
            ):
                kind = "sys.getrecursionlimit"
        if kind == "sys.getrecursionlimit" and not expr.args:
            limit = self.builder.call(
                self.runtime["py_sys_getrecursionlimit"], [],
                name=self._fresh("sys.getrecursionlimit"),
            )
            boxed = self.builder.call(
                self.runtime["py_int_from_i64"], [limit],
                name=self._fresh("sys.getrecursionlimit.box"),
            )
            # Boxing makes a NEW owner, which a slot-call operand adopts.
            self._note_owned_object_value(boxed)
            return boxed
        if kind == "sys.setrecursionlimit" and len(expr.args) == 1:
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            root = self._emit_slot_call_operand(expr.args[0], "sys.recursion.limit")
            try:
                self._try_err_block = self._slot_call_cleanup_block((root,), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                limit = self.builder.call(
                    self.runtime["py_index_i64_checked_slots"],
                    [self._as_gc_ptr(root)], name=self._fresh("sys.recursion.index"),
                )
                self._emit_post_call_err_check(expr.span)
                self.builder.call(self.runtime["py_sys_setrecursionlimit"], [limit])
                self._emit_post_call_err_check(expr.span)
                self._release_slot_call_roots((root,))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return self._emit_none_literal()
        if kind != "sys.exit":
            return None
        # ``sys.exit(code)`` raises ``SystemExit(code)`` as in CPython, so
        # ``finally`` blocks run and ``except SystemExit`` can catch it; an
        # uncaught one exits with its code (``py_exc_handle_uncaught``).
        # Exiting the process directly also mangled non-int codes.
        span = getattr(expr, "span", None)
        dyn = DynType(name="dyn")
        self._emit_raise(
            Raise(
                span=span,
                exc=Call(
                    span=span,
                    ty=dyn,
                    func=Name(span=span, ty=dyn, ident="SystemExit"),
                    args=tuple(expr.args),
                    kwargs=(),
                ),
                cause=None,
            )
        )
        after = self.current_function.append_basic_block(
            name=self._fresh("sys.exit.after")
        )
        self.builder.position_at_end(after)
        return self._emit_none_literal()

    def _declare_strlen(self) -> ir.Function:
        fn = self.module.globals.get("strlen")
        if isinstance(fn, ir.Function):
            return fn
        fnty = ir.FunctionType(_I64, [_CSTR], var_arg=False)
        fn = ir.Function(self.module, fnty, name="strlen")
        fn.linkage = "external"
        return fn

    def _emit_program_argv_list(self) -> ir.Value:
        return self._take_slot_call_root(self._emit_slot_call_program_argv(None, "argv"))

    def _emit_slot_call_program_argv(self, span, label):
        """Own the existing kernel argv projection throughout list population.

        This only repairs publication. The existing projection creates a new
        list; shared sys.argv identity and mutation need their own contract.
        """
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [output]
        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            argc = self.builder.call(self.runtime["py_program_argc"], [], name=self._fresh("argv.argc"))
            self._slot_call_runtime_call("py_list_new", (), result_slot=output,
                                         suffix_args=(argc,), span=span)
            item = self._new_slot_call_root(label + ".item")
            roots.append(item)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            idx_slot = self._alloca_in_entry(_I64, name=self._fresh("argv.i"))
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            fn = self.current_function
            cond_bb = fn.append_basic_block(name=self._fresh("argv.cond"))
            body_bb = fn.append_basic_block(name=self._fresh("argv.body"))
            end_bb = fn.append_basic_block(name=self._fresh("argv.end"))
            self.builder.branch(cond_bb)
            self.builder.position_at_end(cond_bb)
            cur = self.builder.load(idx_slot, name=self._fresh("argv.cur"))
            more = self.builder.icmp_signed("<", cur, argc, name=self._fresh("argv.more"))
            self.builder.cbranch(more, body_bb, end_bb)
            self.builder.position_at_end(body_bb)
            raw = self.builder.call(self.runtime["py_program_argv"], [cur], name=self._fresh("argv.raw"))
            n_bytes = self.builder.call(self._declare_strlen(), [raw], name=self._fresh("argv.len"))
            # The kernel argv C string is unmanaged process-lifetime storage.
            self._slot_call_runtime_call("py_str_new", (), result_slot=item,
                                         suffix_args=(raw, n_bytes), span=span)
            self._slot_call_runtime_call("py_list_append", (output, item), span=span)
            self.builder.call(self.runtime["pcc_gc_store_root"],
                              [self._as_gc_ptr(item), ir.Constant(_CSTR, None)])
            nxt = self.builder.add(cur, ir.Constant(_I64, 1), name=self._fresh("argv.next"))
            self.builder.store(nxt, idx_slot)
            self.builder.branch(cond_bb)
            self.builder.position_at_end(end_bb)
            self._release_slot_call_roots((item,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output


__all__ = ["NativeSystemLoweringMixin"]
