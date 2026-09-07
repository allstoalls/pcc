"""Builtin type/attribute helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    Call,
    DictType,
    DynType,
    Expr,
    ListType,
    Name,
    StrLit,
    StrType,
    TupleExpr,
    TupleType,
)
from . import marshal
from .errors import L1CodegenError

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class BuiltinTypeAttrLoweringMixin:
    def _emit_getattr_builtin(self, expr: Call) -> ir.Value:
        """``getattr(obj, name)`` / ``getattr(obj, name, default)``.
        CPython-backed receivers go through the real CPython builtin so
        module objects and the three-arg default form keep Python
        semantics. Native receivers use ``py_obj_getattr`` directly,
        with a null-check fallback for the defaulted form.
        """
        native_module_getattr = self._maybe_emit_native_module_getattr(expr)
        if native_module_getattr is not None:
            return native_module_getattr
        if self._expr_looks_cpython(expr.args[0]):
            fn_val = self._load_cpython_builtin("getattr")
            return self._emit_cpy_func_call(
                fn_val,
                "getattr",
                tuple(expr.args),
            )
        obj_val = self._emit_expr_as_pcc_object(expr.args[0])
        name_expr = expr.args[1]
        if isinstance(name_expr, StrLit):
            name_ptr = self._attr_name_ptr(name_expr.value)
        else:
            # Dynamic name — marshal and use py_str_utf8 to grab
            # the C string.
            nv = self._emit_expr(name_expr)
            n_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                nv,
                name_expr.ty,
            )
            name_ptr = self.builder.call(
                self.runtime["py_str_utf8"],
                [n_obj],
                name=self._fresh("getattr.name"),
            )
        # Python evaluates every call argument left-to-right before invoking
        # the callable.  In particular, evaluate the default before
        # py_obj_getattr can install the AttributeError that this three-arg
        # form is responsible for swallowing.  Deferring default evaluation
        # until after the lookup leaks that pending exception into any runtime
        # calls made while constructing the default value.
        default_obj = (
            self._emit_as_object(expr.args[2]) if len(expr.args) == 3 else None
        )
        # 2-arg getattr must raise on a miss; the 3-arg form swallows the
        # miss into the default, so it uses the no-raise probe and skips the
        # AttributeError construction entirely.
        getattr_runtime = (
            "py_obj_getattr" if len(expr.args) == 2 else "py_obj_getattr_maybe"
        )
        got = self.builder.call(
            self.runtime[getattr_runtime],
            [obj_val, name_ptr],
            name=self._fresh("getattr"),
        )
        if len(expr.args) == 2:
            # ``py_obj_getattr`` returns a NEW owned reference on every non-NULL
            # edge (type-class increfs its interned class, instance/class attr
            # loads incref the stored value, and the bound-method/cext paths
            # fabricate a fresh object).  The emitter therefore records the
            # result as owned so a discarded ``getattr(o, 'x')`` releases it
            # exactly once, mirroring the dynamic-method-call and dict.get
            # precedents.  The AST classifier is NOT flipped: the native-module
            # and cpython getattr branches (which return earlier) have their own
            # unaudited ownership and must not be over-released.
            self._note_owned_object_value(got)
            return got
        assert default_obj is not None
        # 3-arg: make BOTH phi edges own exactly one reference so the merged
        # result is uniformly owned.
        #   - present edge: ``got`` is owned; the (already-evaluated) default is
        #     unused here, so release it when it was an owned temporary.
        #   - missing edge: the default becomes the result; retain it when it
        #     was a borrowed value so the result owns a reference, and keep the
        #     temporary's own reference when it was already owned (transfer).
        default_is_owned = self._expr_returns_owned_object(expr.args[2])
        is_missing = self.builder.icmp_signed(
            "==",
            got,
            ir.Constant(_CSTR, None),
            name=self._fresh("getattr.missing"),
        )
        parent_fn = self.current_function
        missing_bb = parent_fn.append_basic_block(name=self._fresh("getattr.missing"))
        present_bb = parent_fn.append_basic_block(name=self._fresh("getattr.present"))
        end_bb = parent_fn.append_basic_block(name=self._fresh("getattr.end"))
        self.builder.cbranch(is_missing, missing_bb, present_bb)
        self.builder.position_at_end(missing_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        default_result = default_obj
        if not default_is_owned:
            default_result = self._gc_retain(
                default_obj, name=self._fresh("getattr.default.retain")
            )
        self.builder.branch(end_bb)
        missing_exit = self.builder._block
        self.builder.position_at_end(present_bb)
        if default_is_owned:
            self._gc_release(
                default_obj,
                self._release_context_label("getattr.default.unused"),
            )
        self.builder.branch(end_bb)
        present_exit = self.builder._block
        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh("getattr.default"))
        phi.add_incoming(default_result, missing_exit)
        phi.add_incoming(got, present_exit)
        self._note_owned_object_value(phi)
        return phi

    def _emit_attr_name_ptr_arg(self, expr: Expr, label: str) -> ir.Value:
        if isinstance(expr, StrLit):
            return self._attr_name_ptr(expr.value)
        raw = self._emit_expr(expr)
        obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            raw,
            expr.ty,
        )
        return self.builder.call(
            self.runtime["py_str_utf8"],
            [obj],
            name=self._fresh(label),
        )

    def _emit_setattr_builtin(self, expr: Call) -> ir.Value:
        target = expr.args[0]
        if isinstance(target, Name):
            module_name = getattr(self, "_native_module_aliases", {}).get(target.ident)
            if module_name is not None:
                name_ptr = self._emit_attr_name_ptr_arg(
                    expr.args[1],
                    "setattr.module.name",
                )
                value = self._emit_expr_as_pcc_object(expr.args[2])
                module_name_ptr = self._ptr_to_cstr(
                    self._cstr_global(
                        module_name,
                        f".setattr.module.{module_name}",
                    )
                )
                status = self.builder.call(
                    self.runtime["py_module_attr_set"],
                    [module_name_ptr, name_ptr, value],
                    name=self._fresh("setattr.module.rc"),
                )
                self._emit_attribute_error_if_status_failed(
                    status,
                    "module attribute",
                    expr.span,
                )
                return self._emit_none_literal()
        obj = self._emit_as_object(expr.args[0])
        name_ptr = self._emit_attr_name_ptr_arg(
            expr.args[1],
            "setattr.name",
        )
        value = self._emit_expr_as_pcc_object(expr.args[2])
        status = self.builder.call(
            self.runtime["py_obj_setattr"],
            [obj, name_ptr, value],
            name=self._fresh("setattr.rc"),
        )
        self._emit_attribute_error_if_status_failed(
            status,
            "attribute",
            expr.span,
        )
        return self._emit_none_literal()

    def _emit_delattr_builtin(self, expr: Call) -> ir.Value:
        obj = self._emit_as_object(expr.args[0])
        name_ptr = self._emit_attr_name_ptr_arg(
            expr.args[1],
            "delattr.name",
        )
        self.builder.call(
            self.runtime["py_obj_delattr"],
            [obj, name_ptr],
            name=self._fresh("delattr.rc"),
        )
        return self._emit_none_literal()

    def _emit_type_builtin(self, expr: Call) -> ir.Value:
        """``type(obj)`` — returns the runtime class PyObject*.
        Uses ``py_obj_getattr(obj, "__class__")`` which the runtime
        resolves on any pcc-native object. For a CPython value (libpython
        mode), native ``py_obj_getattr`` mishandles the real CPython object
        (returns a bogus value), so route ``__class__`` through
        ``py_cpy_getattr`` and tag the resulting CPython type as a cpy value
        (so ``type(x).__name__`` etc. also dispatch through libpython).
        Inert in no-libpython mode (``_cpy_values`` is empty)."""
        obj_val = self._emit_as_object(expr.args[0])
        if obj_val in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(obj_val)
            obj_owned = self._cpy_value_is_owned(obj_val)
            cpy_name = self._ptr_to_cstr(
                self._cstr_global("__class__", ".cpy.attr.__class__")
            )
            cls = self.builder.call(
                self.runtime["py_cpy_getattr"],
                [obj_val, cpy_name],
                name=self._fresh("cpy.type"),
            )
            if obj_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [obj_val])
                self._forget_owned_cpy_value(obj_val)
            self._mark_owned_cpy_value(cls)
            self._guard_cpy_value_not_null(cls)
            return cls
        name_ptr = self._attr_name_ptr("__class__")
        return self.builder.call(
            self.runtime["py_obj_getattr"],
            [obj_val, name_ptr],
            name=self._fresh("type"),
        )

    def _emit_len_call(self, expr: Call) -> ir.Value:
        """``len(x)`` → type-specialised runtime call.

        For typed containers we dispatch to the type-specific runtime
        helper (``py_list_len`` etc.); otherwise we go through the
        generic ``py_obj_len``.
        """
        if len(expr.args) != 1:
            raise L1CodegenError(f"len() takes exactly 1 arg, got {len(expr.args)}")
        arg = expr.args[0]
        weak_dict_kind = self._weak_dict_kind_for_expr(arg)
        # Class-based ``__len__`` fast path.
        dunder = self._try_dispatch_dunder_unary(arg, "__len__", ())
        if dunder is not None:
            return dunder
        obj = self._emit_expr(arg)
        # CPython-backed value: dispatch through py_cpy_len (PyObject_Length).
        if obj in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(obj)
            result = self.builder.call(
                self.runtime["py_cpy_len"],
                [obj],
                name=self._fresh("cpy.len"),
            )
            if self._cpy_value_is_owned(obj):
                self.builder.call(self.runtime["py_cpy_decref"], [obj])
                self._forget_owned_cpy_value(obj)
            return result
        if weak_dict_kind == "value":
            return self.builder.call(
                self.runtime["py_weak_value_dict_len"],
                [obj],
                name=self._fresh("weak.value.dict.len"),
            )
        if weak_dict_kind == "key":
            return self.builder.call(
                self.runtime["py_weak_key_dict_len"],
                [obj],
                name=self._fresh("weak.key.dict.len"),
            )
        aty = arg.ty
        # ``len`` consumes its operand: an owned temporary (a call result, a
        # literal, a concatenation) must be released here or it leaks once per
        # evaluation -- ``len(f(i))`` with an exact-list ``f`` retained 116 MB
        # over 300k calls (tests/python/test_ownership_str_iadd_and_len_call_result.py).
        typed_len = {
            ListType: "py_list_len",
            StrType: "py_str_len",
            DictType: "py_dict_len",
            TupleType: "py_tuple_len",
        }
        for len_ty, helper in typed_len.items():
            if isinstance(aty, len_ty):
                result = self.builder.call(
                    self.runtime[helper], [obj], name=self._fresh(helper[3:])
                )
                self._gc_release_if_owned(obj, arg)
                return result
        # Fallback through the generic helper. Any object with a
        # __len__ gets the right answer; non-sized types raise via the
        # runtime.
        boxed = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, obj, aty
        )
        result = self.builder.call(
            self.runtime["py_obj_len"], [boxed], name=self._fresh("obj.len")
        )
        self._gc_release_if_owned(boxed, arg)
        return result

    def _emit_str_builtin(self, expr: Call) -> ir.Value:
        """``str(x)`` -> owned PCC string, matching CPython's new-ref result."""
        if len(expr.args) in (2, 3) and not expr.kwargs:
            source = self._emit_expr_as_pcc_object(expr.args[0])
            encoding = self._emit_expr_as_pcc_object(expr.args[1])
            errors = (
                self._emit_expr_as_pcc_object(expr.args[2])
                if len(expr.args) == 3
                else self._emit_str_literal("strict")
            )
            return self.builder.call(
                self.runtime["py_bytes_decode_with_encoding"],
                [source, encoding, errors],
                name=self._fresh("str.decode.bytes"),
            )
        if len(expr.args) != 1:
            raise NotImplementedError("str() with multi-arg not supported")
        arg = expr.args[0]
        if isinstance(arg.ty, StrType):
            v = self._emit_expr(arg)
            if v in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(v)
                result = self.builder.call(
                    self.runtime["py_cpy_to_pcc_str"],
                    [v],
                    name=self._fresh("cpy.to_pcc_str"),
                )
                if self._cpy_value_is_owned(v):
                    self.builder.call(self.runtime["py_cpy_decref"], [v])
                    self._forget_owned_cpy_value(v)
                self._guard_cpy_value_not_null(result)
                self._emit_post_call_err_check(getattr(arg, "span", None))
                return result
            if self._expr_returns_owned_object(arg):
                return v
            return self._gc_retain(v, name=self._fresh("str.retain"))
        if self._expr_looks_cpython(arg):
            v = self._emit_expr(arg)
            if v in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(v)
                result = self.builder.call(
                    self.runtime["py_cpy_to_pcc_str"],
                    [v],
                    name=self._fresh("cpy.to_pcc_str"),
                )
                if self._cpy_value_is_owned(v):
                    self.builder.call(self.runtime["py_cpy_decref"], [v])
                    self._forget_owned_cpy_value(v)
                self._guard_cpy_value_not_null(result)
                self._emit_post_call_err_check(getattr(arg, "span", None))
                return result
        boxed = self._emit_expr_as_pcc_object(arg)
        result = self.builder.call(
            self.runtime["py_obj_str"], [boxed], name=self._fresh("obj.str")
        )
        # py_obj_str borrows its operand, so an owned temporary produced by the
        # argument expression is consumed here.  Release it exactly once
        # (symmetric with repr/ascii/hash); the shared helper leaves a borrowed
        # operand alone, so str(name) gains no release.
        self._gc_release_if_owned(boxed, arg)
        return self.builder.call(
            self.runtime["pcc_gc_resolve_owned_ptr"],
            [result],
            name=self._fresh("obj.str.resolve"),
        )

    def _emit_enumerate_builtin(self, expr: Call) -> Optional[ir.Value]:
        """Value-position ``enumerate(iterable[, start])`` -> eager
        (index, item) tuple list via the C-only ``py_enumerate_list``
        helper (the for-loop form never reaches here — for
        normalization desugars it to a counter). Returns None for
        shapes this lowering does not cover so the caller falls
        through unchanged."""
        start_expr = None
        if len(expr.args) == 1 and not expr.kwargs:
            pass
        elif len(expr.args) == 2 and not expr.kwargs:
            start_expr = expr.args[1]
        elif (
            len(expr.args) == 1
            and len(expr.kwargs) == 1
            and expr.kwargs[0][0] == "start"
        ):
            start_expr = expr.kwargs[0][1]
        else:
            return None
        iter_obj = self._emit_as_object(expr.args[0])
        if start_expr is not None:
            start_val = self._emit_expr_as_i64(start_expr)
        else:
            start_val = ir.Constant(_I64, 0)
        result = self.builder.call(
            self.runtime["py_enumerate_list"],
            [iter_obj, start_val],
            name=self._fresh("enumerate.list"),
        )
        # py_obj_iter raises TypeError for non-iterables; iteration can
        # propagate pending exceptions.
        self._emit_post_call_err_check(getattr(expr, "span", None))
        return result

    # Encoding-name sets mirror the ``str.encode`` lowering in
    # ``string_method_lowering.py`` so the two-arg ``bytes``/``bytearray``
    # constructors accept exactly the encodings the encode helpers already
    # cover natively; every other encoding falls through to libpython.
    _UTF8_ENCODING_NAMES = ("utf-8", "utf8", "UTF-8", "UTF8")
    _LATIN1_ENCODING_NAMES = ("latin-1", "latin1")

    def _bytes_encoding_helper_for_arg(self, encoding_expr: Expr) -> Optional[str]:
        """Return the runtime encode helper name for a ``bytes(str, encoding)``
        encoding literal, or ``None`` when the encoding is dynamic / unsupported
        (caller then falls through to the libpython fallback)."""
        if not isinstance(encoding_expr, StrLit):
            return None
        if encoding_expr.value in self._UTF8_ENCODING_NAMES:
            return "py_str_utf8_encode"
        if encoding_expr.value in self._LATIN1_ENCODING_NAMES:
            return "py_str_latin1_encode"
        return None

    def _emit_str_encode_to_bytes(
        self,
        str_expr: Expr,
        helper: str,
        label: str,
    ) -> ir.Value:
        """Encode a str expression to a bytes object via the given runtime
        encode helper. The helper reads the raw ``PY_TYPE_STR`` layout, so the
        argument must be materialized as a pcc str object."""
        src = self._emit_expr_as_pcc_object(str_expr)
        return self.builder.call(
            self.runtime[helper],
            [src],
            name=self._fresh(label),
        )

    def _emit_bytes_family_builtin(
        self,
        expr: Call,
        name: str,
    ) -> Optional[ir.Value]:
        if expr.kwargs:
            return None
        if name == "bytes":
            if len(expr.args) == 1:
                src = self._emit_as_object(expr.args[0])
                result = self.builder.call(
                    self.runtime["py_bytes_from_obj"],
                    [src],
                    name=self._fresh("bytes.from"),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                return result
            if not expr.args:
                return self.builder.call(
                    self.runtime["py_bytes_new"],
                    [ir.Constant(_CSTR, None), ir.Constant(_I64, 0)],
                    name=self._fresh("bytes.empty"),
                )
            if len(expr.args) == 2 and isinstance(expr.args[0].ty, StrType):
                # bytes(str, encoding-literal) -> encode the str directly with
                # the matching runtime helper. Only literal utf-8 / latin-1
                # encodings are handled natively; anything else returns None so
                # the caller falls through to the libpython fallback.
                helper = self._bytes_encoding_helper_for_arg(expr.args[1])
                if helper is not None:
                    return self._emit_str_encode_to_bytes(
                        expr.args[0], helper, "bytes.encode"
                    )
            return None
        if (
            name == "bytearray"
            and len(expr.args) == 2
            and isinstance(expr.args[0].ty, StrType)
        ):
            # bytearray(str, encoding-literal) -> encode the str to bytes, then
            # wrap the bytes object as a bytearray (mirrors CPython's
            # bytearray(str, encoding) two-arg form).
            helper = self._bytes_encoding_helper_for_arg(expr.args[1])
            if helper is not None:
                encoded = self._emit_str_encode_to_bytes(
                    expr.args[0], helper, "bytearray.encode.bytes"
                )
                return self.builder.call(
                    self.runtime["py_bytearray_from_obj"],
                    [encoded],
                    name=self._fresh("bytearray.encode"),
                )
        if name == "bytearray" and len(expr.args) == 1:
            src = self._emit_as_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_bytearray_from_obj"],
                [src],
                name=self._fresh("bytearray.from"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        if name == "bytearray" and not expr.args:
            # bytearray() -> empty bytearray, built from an empty bytes object
            # (mirrors the bytes() 0-arg path above). Without this the 0-arg
            # form forced the libpython fallback.
            empty = self.builder.call(
                self.runtime["py_bytes_new"],
                [ir.Constant(_CSTR, None), ir.Constant(_I64, 0)],
                name=self._fresh("bytearray.empty.bytes"),
            )
            return self.builder.call(
                self.runtime["py_bytearray_from_obj"],
                [empty],
                name=self._fresh("bytearray.empty"),
            )
        if name == "memoryview" and len(expr.args) == 1:
            src = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_memoryview_new"],
                [src],
                name=self._fresh("memoryview.new"),
            )
        return None
