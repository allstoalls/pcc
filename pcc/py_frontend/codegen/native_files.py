"""Native file-object lowering helpers."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import Attr, Call, DynType, Name, StrType, With

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class NativeFilesLoweringMixin:
    def _emit_native_open_call(self, expr: Call) -> Optional[ir.Value]:
        """Lower the common text-mode ``open(path, mode?, encoding=...)``
        surface to pcc's runtime file object.

        The runtime stores bytes and assumes UTF-8 text. ``encoding`` /
        ``errors`` / ``newline`` are accepted as compatibility kwargs
        and ignored; unsupported kwargs fall back to CPython.
        """
        if not isinstance(expr.func, Name) or expr.func.ident != "open":
            return None
        if len(expr.args) < 1 or len(expr.args) > 2:
            return None
        for key, _value in expr.kwargs:
            if key not in ("encoding", "errors", "newline"):
                return None
        path_obj = self._emit_os_path_arg_object(expr.args[0])
        if len(expr.args) == 2:
            mode_v = self._emit_expr(expr.args[1])
            mode_obj = self._emit_value_as_pcc_object_or_bridge(
                mode_v,
                expr.args[1].ty,
                "cpy.file.mode",
            )
        else:
            mode_obj = self._emit_str_literal("r")
        result = self.builder.call(
            self.runtime["py_file_open"],
            [path_obj, mode_obj],
            name=self._fresh("file.open"),
        )
        # ``py_file_open`` reports an OSError through the runtime exception
        # channel and returns NULL.  Both plain ``open()`` and ``with open()``
        # share this helper, so branch to the active handler before either
        # caller can treat the failure sentinel as a file object.
        self._emit_post_call_err_check(getattr(expr, "span", None))
        if not hasattr(self, "_native_file_values"):
            self._native_file_values = set()
        self._native_file_values.add(result)
        return result

    def _emit_native_file_method(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs:
            return None
        if not isinstance(attr.obj, Name):
            return None
        if not self._native_file_env_flags.get(attr.obj.ident, False):
            return None
        recv = self._emit_expr(attr.obj)
        if attr.name == "read":
            if not expr.args:
                return self.builder.call(
                    self.runtime["py_file_read_all"],
                    [recv],
                    name=self._fresh("file.read"),
                )
            if len(expr.args) == 1:
                limit_v = self._emit_expr(expr.args[0])
                limit_i64 = self._to_int64(limit_v, expr.args[0].ty)
                return self.builder.call(
                    self.runtime["py_file_read"],
                    [recv, limit_i64],
                    name=self._fresh("file.read"),
                )
        if attr.name == "write" and len(expr.args) == 1:
            text_v = self._emit_expr(expr.args[0])
            text_obj = self._emit_value_as_pcc_object_or_bridge(
                text_v,
                expr.args[0].ty,
                "cpy.file.write.arg",
            )
            return self.builder.call(
                self.runtime["py_file_write"],
                [recv, text_obj],
                name=self._fresh("file.write"),
            )
        if attr.name == "readline" and len(expr.args) <= 1:
            if expr.args:
                limit_v = self._emit_expr(expr.args[0])
                limit_i64 = self._to_int64(limit_v, expr.args[0].ty)
            else:
                limit_i64 = ir.Constant(_I64, -1)
            result = self.builder.call(
                self.runtime["py_file_readline"],
                [recv, limit_i64],
                name=self._fresh("file.readline"),
            )
            # Raises ValueError on a closed file.
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
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
        if attr.name == "tell" and not expr.args:
            result = self.builder.call(
                self.runtime["py_file_tell"],
                [recv],
                name=self._fresh("file.tell"),
            )
            # Raises ValueError on a closed file.
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        if attr.name == "flush" and not expr.args:
            result = self.builder.call(
                self.runtime["py_file_flush"],
                [recv],
                name=self._fresh("file.flush"),
            )
            # Raises ValueError on a closed file.
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        if attr.name == "fileno" and not expr.args:
            result = self.builder.call(
                self.runtime["py_file_fileno"],
                [recv],
                name=self._fresh("file.fileno"),
            )
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
        if (
            not isinstance(ctx_expr, Call)
            or not isinstance(ctx_expr.func, Name)
            or ctx_expr.func.ident != "open"
            or not isinstance(as_expr, Name)
        ):
            return False
        file_val = self._emit_native_open_call(ctx_expr)
        if file_val is None:
            return False

        slot = self.env.get(as_expr.ident)
        if slot is None:
            alloca = self._alloca_in_entry(
                _CSTR,
                name=f"{as_expr.ident}.addr",
                init_null=True,
            )
            self.env[as_expr.ident] = (alloca, _CSTR, DynType(name="dyn"))
            slot = self.env[as_expr.ident]

        # ``with open(...) as f`` bypasses the normal Assign lowering, but
        # the binding still owns a GC-managed file object.  Register the
        # alloca as an updateable frame root before the body can allocate;
        # otherwise tracing backend #1 can sweep the file during a long
        # write loop, while moving backends can leave ``f`` pointing at the
        # pre-relocation object.  Keep the ordinary owned-local flag/release
        # contract as well so the closed file object is released when the
        # function binding dies.
        self._ensure_owned_local_gc_root(as_expr.ident, slot[0], _CSTR)
        replacing_owned = as_expr.ident in getattr(
            self, "_owned_local_names", set()
        ) and as_expr.ident in getattr(self, "_owned_local_has_value", set())
        if replacing_owned:
            # The context expression is evaluated before its ``as`` target
            # is rebound.  Pin the newly opened file while releasing the old
            # binding so a collector step in that release cannot move it
            # before it reaches the rooted slot.
            self._gc_pin(file_val)
            self._release_existing_owned_local(as_expr.ident)
        self.builder.store(file_val, slot[0])
        # Re-add compile-time rooted-name bookkeeping after replacement;
        # _release_existing_owned_local deliberately discards the name while
        # leaving the alloca registration alive for the whole frame.
        self._ensure_owned_local_gc_root(as_expr.ident, slot[0], _CSTR)
        self.builder.call(
            self.runtime["pcc_gc_note_write_barrier"],
            [ir.Constant(_CSTR, None), file_val],
        )
        if replacing_owned:
            self._gc_unpin(file_val)
        self._owned_local_names.add(as_expr.ident)
        self._owned_local_has_value.add(as_expr.ident)
        owned_flag = self._ensure_owned_local_flag(as_expr.ident, slot[0])
        self.builder.store(ir.Constant(_I1, 1), owned_flag)
        self._native_file_env_flags[as_expr.ident] = True
        self._cpy_env_flags.pop(as_expr.ident, None)

        self._emit_stmts(stmt.body)
        if not self._builder_block_is_terminated():
            # Reload through the root/update barrier: GC3/GC4 may have moved
            # the file while lowering calls in the with-body were running.
            current_file = self._emit_name(as_expr)
            self.builder.call(self.runtime["py_file_close"], [current_file])
        return True

    def _emit_native_tempdir_with(self, stmt: With) -> bool:
        ctx_expr, as_expr = stmt.items[0]
        if (
            not isinstance(ctx_expr, Call)
            or not isinstance(ctx_expr.func, Attr)
            or not isinstance(ctx_expr.func.obj, Name)
            or self._native_builtin_module_for_name(ctx_expr.func.obj.ident)
            != "tempfile"
            or ctx_expr.func.name != "TemporaryDirectory"
            or ctx_expr.args
            or not isinstance(as_expr, Name)
        ):
            return False
        prefix_expr = None
        for key, value in ctx_expr.kwargs:
            if key != "prefix":
                return False
            prefix_expr = value
        prefix_obj = (
            self._emit_as_object(prefix_expr)
            if prefix_expr is not None
            else self._emit_str_literal("tmp")
        )
        tmp_val = self.builder.call(
            self.runtime["py_tempdir_new"],
            [prefix_obj],
            name=self._fresh("tempfile.TemporaryDirectory"),
        )
        self._emit_post_call_err_check(stmt.span)
        self._note_owned_object_value(tmp_val)
        entered = self._gc_retain(tmp_val)
        if hasattr(self, "_cpy_env_flags"):
            self._cpy_env_flags.pop(as_expr.ident, None)
        self._emit_native_context_body(stmt, tmp_val, entered, "py_tempdir_cleanup")
        return True


__all__ = ["NativeFilesLoweringMixin"]
