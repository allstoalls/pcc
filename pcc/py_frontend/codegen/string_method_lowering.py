"""String method lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BoolLit,
    NoneLit,
    ByteArrayType,
    BytesType,
    Call,
    DynType,
    Expr,
    IntLit,
    IntType,
    StrLit,
    StrType,
)
from . import marshal
from .freestanding_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES

_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()

_STR_METHOD_NATIVE = frozenset(
    {
        "upper",
        "lower",
        "capitalize",
        "swapcase",
        "title",
        "casefold",
        "strip",
        "lstrip",
        "rstrip",
        "split",
        "rsplit",
        "partition",
        "rpartition",
        "translate",
        "removeprefix",
        "removesuffix",
        "rjust",
        "ljust",
        "center",
        "zfill",
        "expandtabs",
        "join",
        "replace",
        "find",
        "rfind",
        "count",
        "encode",
        "startswith",
        "endswith",
        "splitlines",
        "isdigit",
        "isalpha",
        "isspace",
        "isalnum",
        "isupper",
        "islower",
        "isascii",
        "isidentifier",
        "isprintable",
        "isnumeric",
        "isdecimal",
        "istitle",
        "index",
        "rindex",
    }
)
_BYTES_METHOD_NATIVE = frozenset(
    {
        "decode",
        "hex",
        "upper",
        "translate",
        "replace",
    }
)


def _str_method_arg(host, e: Expr) -> ir.Value:
    v = host._emit_expr(e)
    return marshal.marshal_to_object(
        host.builder,
        host.module,
        host.runtime,
        v,
        e.ty,
    )


def _str_i32_to_i1(host, v: ir.Value, nm: str) -> ir.Value:
    return host.builder.icmp_signed(
        "!=",
        v,
        ir.Constant(_I32, 0),
        name=host._fresh(nm),
    )


# CPython uses PY_SSIZE_T_MAX as the default ``end`` for
# str.find/rfind/index/rindex; the runtime *_range helpers clamp any
# end > cp_len down to cp_len, so this sentinel reproduces "no end given".
_STR_FIND_END_DEFAULT = ((0x7FFFFFFF << 32) | 0xFFFFFFFF)


def _str_find_range_bounds(host, expr: Call):
    """Return the (start, end) i64 codepoint bounds for a 2- or 3-arg
    ``find``/``rfind``/``index``/``rindex`` call. ``start`` is
    ``expr.args[1]``; ``end`` is ``expr.args[2]`` if present, else the
    PY_SSIZE_T_MAX sentinel that the runtime clamps to cp_len."""
    start = host._emit_expr_as_i64(expr.args[1])
    if len(expr.args) >= 3:
        end = host._emit_expr_as_i64(expr.args[2])
    else:
        end = ir.Constant(_I64, _STR_FIND_END_DEFAULT)
    return start, end


class StringMethodLoweringMixin:
    def _emit_native_str_join(self, recv: ir.Value, arg_expr: Expr, prefix: str):
        """Call ``py_str_join`` while its temporary sequence stays rooted.

        A list literal is unpinned when literal construction finishes.  The
        join runtime allocates the result before reading the elements again,
        so a sufficiently large result can run GC in that gap.  Keep the
        sequence pinned across the call, and keep the result pinned while the
        owned argument is released.
        """
        items = _str_method_arg(self, arg_expr)
        self._gc_pin(items)
        result = self.builder.call(
            self.runtime["py_str_join"],
            [recv, items],
            name=self._fresh(prefix + ".join"),
        )
        self._gc_pin(result)
        self._gc_unpin(items)
        self._gc_release_if_owned(items, arg_expr)
        self._gc_unpin(result)
        return result

    def _emit_native_bytes_join(self, recv: ir.Value, arg_expr: Expr, prefix: str):
        """Call ``py_bytes_join`` with the same rooting discipline as str.join.

        Only statically typed bytes/bytearray receivers reach this helper;
        a DynType ``.join`` stays on the str path because the two names
        overlap and a runtime tag split is not owned here.
        """
        items = _str_method_arg(self, arg_expr)
        self._gc_pin(items)
        result = self.builder.call(
            self.runtime["py_bytes_join"],
            [recv, items],
            name=self._fresh(prefix + ".join"),
        )
        self._gc_pin(result)
        self._gc_unpin(items)
        self._gc_release_if_owned(items, arg_expr)
        self._gc_unpin(result)
        self._emit_post_call_err_check(getattr(arg_expr, "span", None))
        return result

    def _extract_splitlines_keepends(self, expr: Call):
        """Return the ``keepends`` constant bool for a
        ``splitlines(True)`` / ``splitlines(keepends=…)`` call, or ``None``
        if the caller passed neither (the bare ``splitlines()`` form)."""
        # Positional: splitlines(True) / splitlines(0).
        if expr.args:
            v = expr.args[0]
            if isinstance(v, BoolLit):
                return bool(v.value)
            if isinstance(v, IntLit):
                return bool(v.value)
            # Non-constant — treat as True (preserves line endings) to be safe.
            return True
        for key, v in expr.kwargs or ():
            if key == "keepends":
                if isinstance(v, BoolLit):
                    return bool(v.value)
                if isinstance(v, IntLit):
                    return bool(v.value)
                # Non-constant keepends — treat as ``True`` to be
                # safe; produced output preserves line endings.
                return True
        return None

    def _emit_str_method_with_receiver(self, expr: Call, recv: ir.Value, dynamic: bool):
        receiver_expr = expr.func.obj
        owned = self._owned_release_needed(recv, receiver_expr)
        # Borrowed locals need an independent owner when a later argument can
        # run user code and replace the original binding.
        if not owned and any(
            not isinstance(arg, (StrLit, IntLit, BoolLit, NoneLit))
            for arg in expr.args
        ):
            recv = self._gc_retain(recv, name=self._fresh("str.receiver.retain"))
            owned = True
        if not self._pcc_pointer_source_needs_pin(receiver_expr):
            owned = False
        old_pcc = self._current_try_err_block()
        old_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
        if owned:
            self._gc_pin(recv)
            pcc_target = old_pcc if old_pcc is not None else self._ensure_fn_err_exit()
            cpy_target = old_cpy if old_cpy is not None else self._ensure_fn_err_exit()
            pcc_cleanup = self._make_cpy_operand_cleanup_block(
                (), (), pcc_target, "str.receiver.pcc.cleanup", ((recv, True),),
            )
            cpy_cleanup = pcc_cleanup
            if cpy_target is not pcc_target:
                cpy_cleanup = self._make_cpy_operand_cleanup_block(
                    (), (), cpy_target, "str.receiver.cpy.cleanup", ((recv, True),),
                )
            self._try_err_block = pcc_cleanup
            self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            if dynamic:
                result = self._emit_dyn_str_method_body(expr, recv)
            else:
                result = self._emit_str_method_body(expr, recv)
        finally:
            self._try_err_block = old_pcc
            self._cpy_operand_cleanup_block = old_cpy
        result_is_object = result is not None and isinstance(result.type, ir.PointerType)
        root = None
        if result_is_object:
            self._note_owned_object_value(result)
            if owned:
                root = self._enter_container_temp_root(result, self._fresh("str.result"))
        if owned:
            self._gc_unpin(recv)
            self._gc_release(recv)
        if root is not None:
            result = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), self._as_gc_ptr(root)],
                name=self._fresh("str.result.current"),
            )
            self._leave_container_temp_root(root)
            self._note_owned_object_value(result)
        return result

    def _maybe_emit_str_method_via_dyn(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """DynType receiver whose method name matches one of the
        pcc-native str helpers — dispatch through the same runtime
        entries used by the StrType fast path. If the runtime value
        isn't actually a str, the helper crashes cleanly, matching
        Python's AttributeError behaviour in the spirit of 'no
        libpython'."""
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name not in _STR_METHOD_NATIVE:
            return None
        if isinstance(attr.obj.ty, (BytesType, ByteArrayType)):
            # A statically bytes/bytearray receiver must NOT be forced onto the
            # StrType fast path: py_str_upper on a bytearray reads the raw bytes
            # as a string (e.g. ``bytearray(b"abz").upper()`` -> garbage). Bail
            # so the precise bytes branch (py_bytes_*) in
            # method_call_expression_lowering handles it.
            return None
        # The only kwarg we recognise on a str method today is
        # ``splitlines(keepends=…)``. Everything else is routed via
        # the caller's fallback.
        if expr.kwargs and not (
            attr.name == "splitlines" and self._kwargs_are_only_keepends(expr.kwargs)
        ):
            return None
        # Re-use the StrType fast path by recovering the StrType
        # marshal for the receiver. The dyn value is already a
        # PyObject*; marshal_to_object is a no-op when it already
        # is.
        # Build an expr clone whose obj.ty is StrType so the
        # existing helper's type checks line up. Because ``expr`` is
        # a frozen dataclass we go directly to the dispatch using
        # the same implementation inlined here.
        name = attr.name
        recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            recv = self.builder.call(
                self.runtime["py_cpy_to_pcc_obj"],
                [recv],
                name=self._fresh(f"cpy.str.{name}.recv"),
            )
            self._note_owned_object_value(recv)
        # Guard against a native-scalar Dyn payload (i1 from a short-
        # circuit ``or``, i64 from an unboxed attribute read, etc.).
        # Box to PyObject* before passing to the py_str_* helpers
        # which all expect a pointer operand.
        if not isinstance(recv.type, ir.PointerType):
            recv = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                recv,
                attr.obj.ty,
            )

        return self._emit_str_method_with_receiver(expr, recv, True)

    def _emit_dyn_str_method_body(self, expr: Call, recv: ir.Value):
        attr = expr.func
        name = attr.name
        if (
            name
            in (
                "upper",
                "lower",
                "capitalize",
                "swapcase",
                "title",
                "casefold",
                "strip",
                "lstrip",
                "rstrip",
            )
            and not expr.args
        ):
            fn = {
                "upper": "py_str_upper",
                "lower": "py_str_lower",
                "capitalize": "py_str_capitalize",
                "swapcase": "py_str_swapcase",
                "title": "py_str_title",
                "casefold": "py_str_casefold",
                "strip": "py_str_strip",
                "lstrip": "py_str_lstrip",
                "rstrip": "py_str_rstrip",
            }[name]
            return self.builder.call(
                self.runtime[fn],
                [recv],
                name=self._fresh(f"dyn.str.{name}"),
            )
        if name in ("strip", "lstrip", "rstrip") and len(expr.args) == 1:
            fn = {
                "strip": "py_str_strip_chars",
                "lstrip": "py_str_lstrip_chars",
                "rstrip": "py_str_rstrip_chars",
            }[name]
            return self.builder.call(
                self.runtime[fn],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh(f"dyn.str.{name}.chars"),
            )
        if name == "count" and 1 <= len(expr.args) <= 3:
            if len(expr.args) >= 2:
                start_obj = self._emit_as_object(expr.args[1])
                end_obj = (
                    self._emit_as_object(expr.args[2])
                    if len(expr.args) == 3
                    else self._emit_none_literal()
                )
                return self.builder.call(
                    self.runtime["py_str_count_range"],
                    [recv, _str_method_arg(self, expr.args[0]), start_obj, end_obj],
                    name=self._fresh("dyn.str.count.range"),
                )
            return self.builder.call(
                self.runtime["py_str_count"],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh("dyn.str.count"),
            )
        if (
            name
            in (
                "isdigit",
                "isalpha",
                "isspace",
                "isalnum",
                "isupper",
                "islower",
                "isascii",
                "isidentifier",
                "isprintable",
                "isnumeric",
                "isdecimal",
                "istitle",
            )
            and not expr.args
        ):
            fn = {
                "isdigit": "py_str_isdigit",
                "isalpha": "py_str_isalpha",
                "isspace": "py_str_isspace",
                "isalnum": "py_str_isalnum",
                "isupper": "py_str_isupper",
                "islower": "py_str_islower",
                "isascii": "py_str_isascii",
                "isidentifier": "py_str_isidentifier",
                "isprintable": "py_str_isprintable",
                "isnumeric": "py_str_isnumeric",
                "isdecimal": "py_str_isdecimal",
                "istitle": "py_str_istitle",
            }[name]
            i64v = self.builder.call(
                self.runtime[fn],
                [recv],
                name=self._fresh(f"dyn.str.{name}"),
            )
            return self.builder.icmp_signed(
                "!=",
                i64v,
                ir.Constant(_I64, 0),
                name=self._fresh(f"dyn.str.{name}.i1"),
            )
        if (
            name == "encode"
            and len(expr.args) == 1
            and isinstance(expr.args[0], StrLit)
            and expr.args[0].value in ("latin-1", "latin1")
        ):
            return self.builder.call(
                self.runtime["py_str_latin1_encode"],
                [recv],
                name=self._fresh("dyn.str.encode.latin1"),
            )
        if name == "encode" and (
            len(expr.args) == 0
            or (
                len(expr.args) == 1
                and isinstance(expr.args[0], StrLit)
                and expr.args[0].value in ("utf-8", "utf8", "UTF-8", "UTF8")
            )
        ):
            return self.builder.call(
                self.runtime["py_str_utf8_encode"],
                [recv],
                name=self._fresh("dyn.str.encode.utf8"),
            )
        if name == "splitlines" and len(expr.args) <= 1:
            keepends = self._extract_splitlines_keepends(expr)
            if keepends is None:
                return self.builder.call(
                    self.runtime["py_str_splitlines"],
                    [recv],
                    name=self._fresh("dyn.str.splitlines"),
                )
            return self.builder.call(
                self.runtime["py_str_splitlines_keepends"],
                [recv, ir.Constant(_I32, 1 if keepends else 0)],
                name=self._fresh("dyn.str.splitlines.keepends"),
            )
        if name == "split" and len(expr.args) <= 2:
            # ``split()`` with no args splits on whitespace — pass
            # NULL PyObject* to the runtime sep arg, which switches
            # py_str_split to the whitespace path.
            if expr.args:
                sep = _str_method_arg(self, expr.args[0])
            else:
                sep = ir.Constant(_CSTR, None)
            if len(expr.args) == 2:
                maxsplit = self._emit_expr_as_i64(expr.args[1])
                return self.builder.call(
                    self.runtime["py_str_split_maxsplit"],
                    [recv, sep, maxsplit],
                    name=self._fresh("dyn.str.split.maxsplit"),
                )
            return self.builder.call(
                self.runtime["py_str_split"],
                [recv, sep],
                name=self._fresh("dyn.str.split"),
            )
        if name == "rsplit" and len(expr.args) <= 2:
            if not expr.args:
                # rsplit() with no args == split() with no args: whitespace
                # split, no limit -> identical parts in identical order.
                return self.builder.call(
                    self.runtime["py_str_split"],
                    [recv, ir.Constant(_CSTR, None)],
                    name=self._fresh("dyn.str.rsplit.ws"),
                )
            sep = _str_method_arg(self, expr.args[0])
            if len(expr.args) == 2:
                maxsplit = self._emit_expr_as_i64(expr.args[1])
                return self.builder.call(
                    self.runtime["py_str_rsplit_maxsplit"],
                    [recv, sep, maxsplit],
                    name=self._fresh("dyn.str.rsplit"),
                )
            return self.builder.call(
                self.runtime["py_str_split"],
                [recv, sep],
                name=self._fresh("dyn.str.rsplit.nolimit"),
            )
        if name in ("removeprefix", "removesuffix") and len(expr.args) == 1:
            arg = _str_method_arg(self, expr.args[0])
            fn = (
                "py_str_removeprefix"
                if name == "removeprefix"
                else "py_str_removesuffix"
            )
            return self.builder.call(
                self.runtime[fn],
                [recv, arg],
                name=self._fresh("dyn.str." + name),
            )
        if name == "partition" and len(expr.args) == 1:
            sep = _str_method_arg(self, expr.args[0])
            return self.builder.call(
                self.runtime["py_str_partition"],
                [recv, sep],
                name=self._fresh("dyn.str.partition"),
            )
        if name == "rpartition" and len(expr.args) == 1:
            sep = _str_method_arg(self, expr.args[0])
            return self.builder.call(
                self.runtime["py_str_rpartition"],
                [recv, sep],
                name=self._fresh("dyn.str.rpartition"),
            )
        if name == "translate" and len(expr.args) == 1:
            table = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_str_translate"],
                [recv, table],
                name=self._fresh("dyn.str.translate"),
            )
        if name in ("rjust", "ljust", "center") and 1 <= len(expr.args) <= 2:
            width = self._emit_expr_as_i64(expr.args[0])
            if len(expr.args) == 2:
                fill = _str_method_arg(self, expr.args[1])
            else:
                fill = ir.Constant(_CSTR, None)
            justfn = {
                "rjust": "py_str_rjust",
                "ljust": "py_str_ljust",
                "center": "py_str_center",
            }[name]
            return self.builder.call(
                self.runtime[justfn],
                [recv, width, fill],
                name=self._fresh("dyn.str." + name),
            )
        if name == "zfill" and len(expr.args) == 1:
            width = self._emit_expr_as_i64(expr.args[0])
            return self.builder.call(
                self.runtime["py_str_zfill"],
                [recv, width],
                name=self._fresh("dyn.str.zfill"),
            )
        if name == "expandtabs" and len(expr.args) <= 1 and not expr.kwargs:
            if expr.args:
                tabsize = self._emit_expr_as_i64(expr.args[0])
            else:
                tabsize = ir.Constant(_I64, 8)
            return self.builder.call(
                self.runtime["py_str_expandtabs"],
                [recv, tabsize],
                name=self._fresh("dyn.str.expandtabs"),
            )
        if name == "join" and len(expr.args) == 1:
            return self._emit_native_str_join(recv, expr.args[0], "dyn.str")
        if name == "replace" and len(expr.args) == 2:
            return self.builder.call(
                self.runtime["py_str_replace"],
                [
                    recv,
                    _str_method_arg(self, expr.args[0]),
                    _str_method_arg(self, expr.args[1]),
                ],
                name=self._fresh("dyn.str.replace"),
            )
        if name == "replace" and len(expr.args) == 3:
            maxreplace = self._emit_expr_as_i64(expr.args[2])
            return self.builder.call(
                self.runtime["py_str_replace_count"],
                [
                    recv,
                    _str_method_arg(self, expr.args[0]),
                    _str_method_arg(self, expr.args[1]),
                    maxreplace,
                ],
                name=self._fresh("dyn.str.replace.count"),
            )
        if name == "find" and len(expr.args) == 1:
            return self.builder.call(
                self.runtime["py_str_find"],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh("dyn.str.find"),
            )
        if name in ("find", "rfind") and 2 <= len(expr.args) <= 3 and not expr.kwargs:
            fn = "py_str_find_range" if name == "find" else "py_str_rfind_range"
            needle = _str_method_arg(self, expr.args[0])
            start, end = _str_find_range_bounds(self, expr)
            return self.builder.call(
                self.runtime[fn],
                [recv, needle, start, end],
                name=self._fresh(f"dyn.str.{name}.range"),
            )
        if name in ("index", "rindex") and len(expr.args) == 1:
            idx_fn = "py_str_index_of" if name == "index" else "py_str_rindex_of"
            res = self.builder.call(
                self.runtime[idx_fn],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh(f"dyn.str.{name}"),
            )
            # index/rindex raise ValueError when the substring is absent;
            # emit the post-call error check so a surrounding try/except can
            # catch it (mirrors subscript_lowering).
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return res
        if name in ("index", "rindex") and 2 <= len(expr.args) <= 3 and not expr.kwargs:
            idx_fn = (
                "py_str_index_of_range" if name == "index" else "py_str_rindex_of_range"
            )
            needle = _str_method_arg(self, expr.args[0])
            start, end = _str_find_range_bounds(self, expr)
            res = self.builder.call(
                self.runtime[idx_fn],
                [recv, needle, start, end],
                name=self._fresh(f"dyn.str.{name}.range"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return res
        if name == "rfind" and len(expr.args) == 1:
            return self.builder.call(
                self.runtime["py_str_rfind"],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh("dyn.str.rfind"),
            )
        if name in ("startswith", "endswith") and len(expr.args) == 1:
            fn = {"startswith": "py_str_startswith", "endswith": "py_str_endswith"}[
                name
            ]
            i32v = self.builder.call(
                self.runtime[fn],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh(f"dyn.str.{name}"),
            )
            return self.builder.icmp_signed(
                "!=",
                i32v,
                ir.Constant(_I32, 0),
                name=self._fresh(f"dyn.str.{name}.i1"),
            )
        return None

    def _maybe_emit_bytes_method_via_dyn(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """DynType receiver whose method name matches a pcc-native bytes
        helper. This is the bytes analogue of ``_maybe_emit_str_method_via_dyn``:
        it keeps common call chains like ``subprocess.check_output(...).decode()``
        on the no-libpython path after the producer returns a PyObject*."""
        attr = expr.func
        assert isinstance(attr, Attr)
        name = attr.name
        if name not in _BYTES_METHOD_NATIVE:
            return None
        if name in ("replace", "translate", "upper"):
            # These names overlap with str helpers. With a DynType
            # receiver, choosing the bytes helper first sends ordinary
            # dynamic strings such as parser token text into
            # py_bytes_replace/upper/translate and raises a bogus
            # bytes-like TypeError. Statically typed bytes/bytearray
            # calls still use the precise bytes branch in
            # method_call_expression_lowering.
            return None
        recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            recv = self.builder.call(
                self.runtime["py_cpy_to_pcc_obj"],
                [recv],
                name=self._fresh(f"cpy.bytes.{name}.recv"),
            )
        if not isinstance(recv.type, ir.PointerType):
            recv = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                recv,
                attr.obj.ty,
            )
        if name == "decode":
            encoding_arg = None
            errors_arg = None
            ok = True
            if len(expr.args) >= 1:
                encoding_arg = expr.args[0]
            if len(expr.args) >= 2:
                errors_arg = expr.args[1]
            if len(expr.args) > 2:
                ok = False
            for kname, kval in expr.kwargs or ():
                if kname == "encoding" and encoding_arg is None:
                    encoding_arg = kval
                elif kname == "errors" and errors_arg is None:
                    errors_arg = kval
                else:
                    ok = False
            if not ok:
                raise NotImplementedError(
                    "bytes.decode() accepts at most encoding and errors"
                )
            # A DynType receiver is not proof of bytes. Method names overlap
            # user classes (notably JSONDecoder.decode), so preserve Python
            # dispatch with a runtime tag guard and a generic getattr/call
            # slow path. Statically typed bytes still use the direct branch in
            # MethodCallExpressionLoweringMixin.
            tag = self.builder.call(
                self.runtime["py_obj_type_tag"],
                [recv],
                name=self._fresh("dyn.bytes.decode.tag"),
            )
            is_bytes = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_BYTES),
                name=self._fresh("dyn.bytes.decode.is_bytes"),
            )
            is_bytearray = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_BYTEARRAY),
                name=self._fresh("dyn.bytes.decode.is_bytearray"),
            )
            is_bytes_like = self.builder.or_(
                is_bytes,
                is_bytearray,
                name=self._fresh("dyn.bytes.decode.is_bytes_like"),
            )
            parent_fn = self.current_function
            bytes_bb = parent_fn.append_basic_block(
                name=self._fresh("dyn.bytes.decode.bytes")
            )
            object_bb = parent_fn.append_basic_block(
                name=self._fresh("dyn.bytes.decode.object")
            )
            end_bb = parent_fn.append_basic_block(
                name=self._fresh("dyn.bytes.decode.end")
            )
            self.builder.cbranch(is_bytes_like, bytes_bb, object_bb)

            self.builder.position_at_end(bytes_bb)
            encoding = (
                self._emit_expr_as_pcc_object(encoding_arg)
                if encoding_arg is not None
                else self._emit_str_literal("utf-8")
            )
            errors = (
                self._emit_expr_as_pcc_object(errors_arg)
                if errors_arg is not None
                else self._emit_str_literal("strict")
            )
            bytes_result = self.builder.call(
                self.runtime["py_bytes_decode_with_encoding"],
                [recv, encoding, errors],
                name=self._fresh("dyn.bytes.decode"),
            )
            self.builder.branch(end_bb)
            bytes_exit = self.builder.block

            self.builder.position_at_end(object_bb)
            callable_obj = self.builder.call(
                self.runtime["py_obj_getattr"],
                [recv, self._attr_name_ptr("decode")],
                name=self._fresh("dyn.decode.callable"),
            )
            kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
            arg_exprs = expr.args
            kwargs_expr = None
            if kwdict_unpack is not None:
                arg_exprs, kwargs_expr = kwdict_unpack
            args_owned = not self._is_starred_unpack(arg_exprs)
            args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                expr.kwargs,
                kwargs_expr,
                expr.span,
            )
            object_result = self.builder.call(
                self.runtime["py_obj_call"],
                [callable_obj, args_tuple, kwargs_obj],
                name=self._fresh("dyn.decode.call"),
            )
            if args_owned:
                self._gc_release(args_tuple)
            if expr.kwargs:
                self._gc_release(kwargs_obj)
            self._gc_release(callable_obj)
            self._emit_post_call_err_check(expr.span)
            self.builder.branch(end_bb)
            object_exit = self.builder.block

            self.builder.position_at_end(end_bb)
            result = self.builder.phi(
                _CSTR,
                name=self._fresh("dyn.decode.result"),
            )
            result.add_incoming(bytes_result, bytes_exit)
            result.add_incoming(object_result, object_exit)
            return result
        if name == "hex" and not expr.args and not expr.kwargs:
            return self.builder.call(
                self.runtime["py_bytes_hex"],
                [recv],
                name=self._fresh("dyn.bytes.hex"),
            )
        if name == "upper" and not expr.args and not expr.kwargs:
            return self.builder.call(
                self.runtime["py_bytes_upper"],
                [recv],
                name=self._fresh("dyn.bytes.upper"),
            )
        if name == "translate" and len(expr.args) == 1 and not expr.kwargs:
            table = self._emit_as_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_bytes_translate"],
                [recv, table],
                name=self._fresh("dyn.bytes.translate"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        if name == "replace" and len(expr.args) == 2 and not expr.kwargs:
            old_obj = self._emit_as_object(expr.args[0])
            new_obj = self._emit_as_object(expr.args[1])
            result = self.builder.call(
                self.runtime["py_bytes_replace"],
                [recv, old_obj, new_obj],
                name=self._fresh("dyn.bytes.replace"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        return None

    def _maybe_emit_str_method(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """Dispatch selected ``str`` methods via the pcc str runtime."""
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs and not (
            (attr.name == "splitlines" and self._kwargs_are_only_keepends(expr.kwargs))
            or attr.name == "format"
        ):
            return None
        name = attr.name
        recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            recv = self.builder.call(
                self.runtime["py_cpy_to_pcc_obj"],
                [recv],
                name=self._fresh(f"cpy.str.{name}.recv"),
            )
            self._note_owned_object_value(recv)
        # Receiver may be a non-pointer when it came from an ``or``
        # / ``and`` phi that ended at i1 / i64. Box to PyObject* so
        # the py_str_* runtime sees a proper pcc string.
        if not isinstance(recv.type, ir.PointerType):
            recv = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                recv,
                attr.obj.ty,
            )

        return self._emit_str_method_with_receiver(expr, recv, False)

    def _emit_str_method_body(self, expr: Call, recv: ir.Value):
        attr = expr.func
        name = attr.name
        if name == "format":
            return self._maybe_emit_literal_str_format(expr)
        if name == "format_map":
            return self._maybe_emit_literal_str_format_map(expr)
        if (
            name
            in (
                "upper",
                "lower",
                "capitalize",
                "swapcase",
                "title",
                "casefold",
                "strip",
                "lstrip",
                "rstrip",
            )
            and not expr.args
        ):
            fn = {
                "upper": "py_str_upper",
                "lower": "py_str_lower",
                "capitalize": "py_str_capitalize",
                "swapcase": "py_str_swapcase",
                "title": "py_str_title",
                "casefold": "py_str_casefold",
                "strip": "py_str_strip",
                "lstrip": "py_str_lstrip",
                "rstrip": "py_str_rstrip",
            }[name]
            return self.builder.call(
                self.runtime[fn],
                [recv],
                name=self._fresh(f"str.{name}"),
            )
        if name in ("strip", "lstrip", "rstrip") and len(expr.args) == 1:
            fn = {
                "strip": "py_str_strip_chars",
                "lstrip": "py_str_lstrip_chars",
                "rstrip": "py_str_rstrip_chars",
            }[name]
            return self.builder.call(
                self.runtime[fn],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh(f"str.{name}.chars"),
            )
        if name == "count" and 1 <= len(expr.args) <= 3:
            if len(expr.args) >= 2:
                start_obj = self._emit_as_object(expr.args[1])
                end_obj = (
                    self._emit_as_object(expr.args[2])
                    if len(expr.args) == 3
                    else self._emit_none_literal()
                )
                return self.builder.call(
                    self.runtime["py_str_count_range"],
                    [recv, _str_method_arg(self, expr.args[0]), start_obj, end_obj],
                    name=self._fresh("str.count.range"),
                )
            return self.builder.call(
                self.runtime["py_str_count"],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh("str.count"),
            )
        if (
            name
            in (
                "isdigit",
                "isalpha",
                "isspace",
                "isalnum",
                "isupper",
                "islower",
                "isascii",
                "isidentifier",
                "isprintable",
                "isnumeric",
                "isdecimal",
                "istitle",
            )
            and not expr.args
        ):
            fn = {
                "isdigit": "py_str_isdigit",
                "isalpha": "py_str_isalpha",
                "isspace": "py_str_isspace",
                "isalnum": "py_str_isalnum",
                "isupper": "py_str_isupper",
                "islower": "py_str_islower",
                "isascii": "py_str_isascii",
                "isidentifier": "py_str_isidentifier",
                "isprintable": "py_str_isprintable",
                "isnumeric": "py_str_isnumeric",
                "isdecimal": "py_str_isdecimal",
                "istitle": "py_str_istitle",
            }[name]
            i64v = self.builder.call(
                self.runtime[fn],
                [recv],
                name=self._fresh(f"str.{name}"),
            )
            return self.builder.icmp_signed(
                "!=",
                i64v,
                ir.Constant(_I64, 0),
                name=self._fresh(f"str.{name}.i1"),
            )
        if (
            name == "encode"
            and len(expr.args) == 1
            and isinstance(expr.args[0], StrLit)
            and expr.args[0].value in ("latin-1", "latin1")
        ):
            return self.builder.call(
                self.runtime["py_str_latin1_encode"],
                [recv],
                name=self._fresh("str.encode.latin1"),
            )
        if name == "encode" and (
            len(expr.args) == 0
            or (
                len(expr.args) == 1
                and isinstance(expr.args[0], StrLit)
                and expr.args[0].value in ("utf-8", "utf8", "UTF-8", "UTF8")
            )
        ):
            return self.builder.call(
                self.runtime["py_str_utf8_encode"],
                [recv],
                name=self._fresh("str.encode.utf8"),
            )
        if name == "splitlines" and len(expr.args) <= 1:
            keepends = self._extract_splitlines_keepends(expr)
            if keepends is None:
                return self.builder.call(
                    self.runtime["py_str_splitlines"],
                    [recv],
                    name=self._fresh("str.splitlines"),
                )
            return self.builder.call(
                self.runtime["py_str_splitlines_keepends"],
                [recv, ir.Constant(_I32, 1 if keepends else 0)],
                name=self._fresh("str.splitlines.keepends"),
            )
        if name == "split":
            if len(expr.args) > 2:
                return None
            if expr.args:
                sep = _str_method_arg(self, expr.args[0])
            else:
                sep = ir.Constant(_CSTR, None)
            if len(expr.args) == 2:
                maxsplit = self._emit_expr_as_i64(expr.args[1])
                return self.builder.call(
                    self.runtime["py_str_split_maxsplit"],
                    [recv, sep, maxsplit],
                    name=self._fresh("str.split.maxsplit"),
                )
            return self.builder.call(
                self.runtime["py_str_split"],
                [recv, sep],
                name=self._fresh("str.split"),
            )
        if name == "rsplit" and len(expr.args) <= 2:
            if not expr.args:
                # rsplit() with no args == split() with no args: whitespace
                # split, no limit -> identical parts in identical order.
                return self.builder.call(
                    self.runtime["py_str_split"],
                    [recv, ir.Constant(_CSTR, None)],
                    name=self._fresh("str.rsplit.ws"),
                )
            sep = _str_method_arg(self, expr.args[0])
            if len(expr.args) == 2:
                maxsplit = self._emit_expr_as_i64(expr.args[1])
                return self.builder.call(
                    self.runtime["py_str_rsplit_maxsplit"],
                    [recv, sep, maxsplit],
                    name=self._fresh("str.rsplit"),
                )
            # rsplit(sep) without a limit yields the same parts as split(sep).
            return self.builder.call(
                self.runtime["py_str_split"],
                [recv, sep],
                name=self._fresh("str.rsplit.nolimit"),
            )
        if name in ("removeprefix", "removesuffix") and len(expr.args) == 1:
            arg = _str_method_arg(self, expr.args[0])
            fn = (
                "py_str_removeprefix"
                if name == "removeprefix"
                else "py_str_removesuffix"
            )
            return self.builder.call(
                self.runtime[fn],
                [recv, arg],
                name=self._fresh("str." + name),
            )
        if name == "partition" and len(expr.args) == 1:
            sep = _str_method_arg(self, expr.args[0])
            return self.builder.call(
                self.runtime["py_str_partition"],
                [recv, sep],
                name=self._fresh("str.partition"),
            )
        if name == "rpartition" and len(expr.args) == 1:
            sep = _str_method_arg(self, expr.args[0])
            return self.builder.call(
                self.runtime["py_str_rpartition"],
                [recv, sep],
                name=self._fresh("str.rpartition"),
            )
        if name == "translate" and len(expr.args) == 1:
            table = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_str_translate"],
                [recv, table],
                name=self._fresh("str.translate"),
            )
        if name in ("rjust", "ljust", "center") and 1 <= len(expr.args) <= 2:
            width = self._emit_expr_as_i64(expr.args[0])
            if len(expr.args) == 2:
                fill = _str_method_arg(self, expr.args[1])
            else:
                fill = ir.Constant(_CSTR, None)
            justfn = {
                "rjust": "py_str_rjust",
                "ljust": "py_str_ljust",
                "center": "py_str_center",
            }[name]
            return self.builder.call(
                self.runtime[justfn],
                [recv, width, fill],
                name=self._fresh("str." + name),
            )
        if name == "zfill" and len(expr.args) == 1:
            width = self._emit_expr_as_i64(expr.args[0])
            return self.builder.call(
                self.runtime["py_str_zfill"],
                [recv, width],
                name=self._fresh("str.zfill"),
            )
        if name == "expandtabs" and len(expr.args) <= 1 and not expr.kwargs:
            if expr.args:
                tabsize = self._emit_expr_as_i64(expr.args[0])
            else:
                tabsize = ir.Constant(_I64, 8)
            return self.builder.call(
                self.runtime["py_str_expandtabs"],
                [recv, tabsize],
                name=self._fresh("str.expandtabs"),
            )
        if name == "join":
            if len(expr.args) != 1:
                return None
            return self._emit_native_str_join(recv, expr.args[0], "str")
        if name == "replace":
            if len(expr.args) == 2:
                return self.builder.call(
                    self.runtime["py_str_replace"],
                    [
                        recv,
                        _str_method_arg(self, expr.args[0]),
                        _str_method_arg(self, expr.args[1]),
                    ],
                    name=self._fresh("str.replace"),
                )
            if len(expr.args) == 3:
                maxreplace = self._emit_expr_as_i64(expr.args[2])
                return self.builder.call(
                    self.runtime["py_str_replace_count"],
                    [
                        recv,
                        _str_method_arg(self, expr.args[0]),
                        _str_method_arg(self, expr.args[1]),
                        maxreplace,
                    ],
                    name=self._fresh("str.replace.count"),
                )
            return None
        if name in ("find", "rfind"):
            if expr.kwargs or not (1 <= len(expr.args) <= 3):
                return None
            if len(expr.args) == 1:
                fn = "py_str_find" if name == "find" else "py_str_rfind"
                return self.builder.call(
                    self.runtime[fn],
                    [recv, _str_method_arg(self, expr.args[0])],
                    name=self._fresh(f"str.{name}"),
                )
            fn = "py_str_find_range" if name == "find" else "py_str_rfind_range"
            needle = _str_method_arg(self, expr.args[0])
            start, end = _str_find_range_bounds(self, expr)
            return self.builder.call(
                self.runtime[fn],
                [recv, needle, start, end],
                name=self._fresh(f"str.{name}.range"),
            )
        if name in ("index", "rindex"):
            if expr.kwargs or not (1 <= len(expr.args) <= 3):
                return None
            if len(expr.args) == 1:
                idx_fn = "py_str_index_of" if name == "index" else "py_str_rindex_of"
                res = self.builder.call(
                    self.runtime[idx_fn],
                    [recv, _str_method_arg(self, expr.args[0])],
                    name=self._fresh(f"str.{name}"),
                )
            else:
                idx_fn = (
                    "py_str_index_of_range"
                    if name == "index"
                    else "py_str_rindex_of_range"
                )
                needle = _str_method_arg(self, expr.args[0])
                start, end = _str_find_range_bounds(self, expr)
                res = self.builder.call(
                    self.runtime[idx_fn],
                    [recv, needle, start, end],
                    name=self._fresh(f"str.{name}.range"),
                )
            # ValueError on absent substring -> emit err check so try/except
            # can catch it (mirrors subscript_lowering).
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return res
        if name in ("startswith", "endswith"):
            if len(expr.args) != 1:
                return None
            fn = {"startswith": "py_str_startswith", "endswith": "py_str_endswith"}[
                name
            ]
            i32v = self.builder.call(
                self.runtime[fn],
                [recv, _str_method_arg(self, expr.args[0])],
                name=self._fresh(f"str.{name}"),
            )
            return _str_i32_to_i1(self, i32v, f"str.{name}.i1")
        return None
