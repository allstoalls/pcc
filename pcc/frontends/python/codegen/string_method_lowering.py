"""String method lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
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
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.freestanding_abi_constants import (
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_STR,
)

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
    def _encode_acquire_root_leases(self, roots, target, span, leases=(), start=0):
        # Roots follow relocation during argument evaluation. Counted leases
        # additionally stabilize each raw ABI address even if a callback or
        # overlapping alias clears the object's legacy Boolean pin flag.
        acquired_leases = list(leases)
        for root in roots[start:]:
            acquired = self.builder.call(
                self.runtime["pcc_gc_foreign_lease_acquire"],
                [self._as_gc_ptr(root[0])], name=self._fresh("encode.lease.acquire"),
            )
            failed = self.builder.icmp_signed("<", acquired, ir.Constant(_I64, 0))
            error = self.current_function.append_basic_block(self._fresh("encode.lease.error"))
            ready = self.current_function.append_basic_block(self._fresh("encode.lease.ready"))
            self.builder.cbranch(failed, error, ready)
            self.builder.position_at_end(error)
            cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(acquired_leases))
            self._try_err_block = cleanup
            self._cpy_operand_cleanup_block = cleanup
            overflow = self.current_function.append_basic_block(self._fresh("encode.lease.overflow"))
            invalid = self.current_function.append_basic_block(self._fresh("encode.lease.invalid"))
            self.builder.cbranch(
                self.builder.icmp_signed("==", acquired, ir.Constant(_I64, -2)), overflow, invalid,
            )
            self.builder.position_at_end(overflow)
            self._emit_builtin_exception_and_branch(
                "OverflowError", "string encoding address lease overflow", span,
            )
            self.builder.position_at_end(invalid)
            self._emit_builtin_exception_and_branch(
                "RuntimeError", "string encoding requires a stable managed owner", span,
            )
            self.builder.position_at_end(ready)
            acquired_leases.append((root, acquired))
        cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(acquired_leases))
        self._try_err_block = cleanup
        self._cpy_operand_cleanup_block = cleanup
        return acquired_leases

    def _encode_capture_result_root(self, result, roots, label):
        result_root = self._extern_enter_root(result, True, label)
        # Preserve the earliest caller pin lease if a runtime result aliases an
        # operand; never let our temporary acquisitions become the prior state.
        prior = result_root[2]
        for root in reversed(roots):
            alias = self.builder.icmp_unsigned(
                "==", self._extern_load_root(result_root), self._extern_load_root(root)
            )
            prior = self.builder.select(alias, root[2], prior)
        return (result_root[0], True, prior)

    def _emit_str_encode_call(self, expr: Call, recv: ir.Value):
        # Source-order evaluation and conversion are separate. Reloadable roots
        # retain borrowed values and preserve pre-existing pin leases when an
        # argument runs user code, mutates the receiver binding, or aliases an
        # earlier argument. The shared root helper owns exactly one transfer.
        receiver = self._extern_enter_root(
            recv, self._owned_release_needed(recv, expr.func.obj), "encode.receiver"
        )
        roots = [receiver]
        encoding_root = None
        errors_root = None
        operands = []
        for index, operand in enumerate(expr.args):
            operands.append(("encoding" if index == 0 else "errors", operand))
        operands.extend(expr.kwargs)
        binding_error = ""
        if len(expr.args) > 2:
            binding_error = "encode() takes at most 2 positional arguments"
        previous = self._current_try_err_block()
        previous_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
        target = previous if previous is not None else self._ensure_fn_err_exit()
        leases = []
        try:
            for key, operand in operands:
                cleanup = self._extern_cleanup_block(tuple(roots), target)
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                value = self._emit_expr_with_cpy_operand_cleanup(
                    operand, (), as_pcc_object=True,
                )
                root = self._extern_enter_root(
                    value, self._owned_release_needed(value, operand), "encode.argument"
                )
                roots.append(root)
                if key == "encoding":
                    if encoding_root is not None:
                        binding_error = "encode() got multiple values for argument 'encoding'"
                    encoding_root = root
                elif key == "errors":
                    if errors_root is not None:
                        binding_error = "encode() got multiple values for argument 'errors'"
                    errors_root = root
                else:
                    binding_error = "encode() got an unexpected keyword argument '" + key + "'"
            self._try_err_block = self._extern_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if binding_error:
                message = self._pooled_cstr_ptr(binding_error, ".encode.binding_error")
                exc = self.builder.call(
                    self.runtime["py_exc_new"], [ir.Constant(_I64, 3), message],
                    name=self._fresh("encode.binding.exc"),
                )
                self.builder.call(self.runtime["py_raise"], [exc])
                self._gc_release(exc)
                self._emit_post_call_err_check(expr.span)
                self._extern_release_roots(tuple(roots))
                return self._emit_none_literal()
            leases = self._encode_acquire_root_leases(roots, target, expr.span)
            encoding = ir.Constant(_CSTR, None)
            errors = ir.Constant(_CSTR, None)
            if encoding_root is not None:
                encoding = self._extern_load_root(encoding_root)
            if errors_root is not None:
                errors = self._extern_load_root(errors_root)
            result = self.builder.call(
                self.runtime["py_str_encode_with_encoding"],
                [self._extern_load_root(receiver), encoding, errors],
                name=self._fresh("str.encode"),
            )
            self._emit_post_call_err_check(expr.span)
            result_root = self._encode_capture_result_root(result, roots, "encode.result")
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = previous_cpy
        release_failed = self._extern_release_foreign_leases(tuple(leases))
        self._extern_check_lease_cleanup(release_failed, tuple(roots) + (result_root,))
        self._extern_release_roots(tuple(roots))
        result = self._extern_take_root(result_root)
        self._note_owned_object_value(result)
        return result

    def _emit_owned_bytes_decode(self, expr, dynamic):
        """Keep decoder inputs owned and publish either branch before cleanup."""
        if self._has_unpacked_operands(expr.args, expr.kwargs):
            return None
        operands = []
        seen = set()
        if len(expr.args) > 2:
            raise NotImplementedError("bytes.decode() accepts at most encoding and errors")
        for index, operand in enumerate(expr.args):
            key = "encoding" if index == 0 else "errors"
            operands.append((key, operand))
            seen.add(key)
        for key, operand in expr.kwargs:
            if key not in ("encoding", "errors") or key in seen:
                raise NotImplementedError("bytes.decode() accepts at most encoding and errors")
            operands.append((key, operand))
            seen.add(key)
        return self._emit_owned_decode(expr, expr.func.obj, operands, dynamic)

    def _emit_owned_decode(self, expr, receiver_expr, operands, dynamic):
        """Decode ``receiver_expr`` with owned inputs into expr's output root.

        ``bytes.decode(...)`` and ``str(object, encoding[, errors])`` share it;
        only the method call has a dynamic receiver (``dynamic``).
        """
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("decode.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(receiver_expr, "decode.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            generic_block = None
            done_block = None
            if dynamic:
                tag = self._slot_call_runtime_call("py_obj_type_tag", (receiver,), span=expr.span)
                is_bytes = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_BYTES))
                is_bytearray = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_BYTEARRAY))
                is_native = self.builder.or_(is_bytes, is_bytearray, name=self._fresh("decode.is_native"))
                native_block = self.current_function.append_basic_block(self._fresh("decode.native"))
                generic_block = self.current_function.append_basic_block(self._fresh("decode.generic"))
                done_block = self.current_function.append_basic_block(self._fresh("decode.done"))
                self.builder.cbranch(is_native, native_block, generic_block)
                self.builder.position_at_end(native_block)

            branch_roots = list(roots)
            encoding = None
            errors = None
            for key, operand in operands:
                value = self._emit_slot_call_operand(operand, "decode." + key)
                branch_roots.append(value)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if key == "encoding":
                    encoding = value
                else:
                    errors = value
            if encoding is None:
                default_encoding = StrLit(span=expr.span, ty=StrType(name="str"), value="utf-8")
                encoding = self._emit_slot_call_operand(default_encoding, "decode.encoding.default")
                branch_roots.append(encoding)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            if errors is None:
                default_errors = StrLit(span=expr.span, ty=StrType(name="str"), value="strict")
                errors = self._emit_slot_call_operand(default_errors, "decode.errors.default")
                branch_roots.append(errors)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_bytes_decode_with_encoding", (receiver, encoding, errors),
                result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots(tuple(branch_roots[len(roots):]))
            if dynamic:
                self.builder.branch(done_block)
                self.builder.position_at_end(generic_block)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                method = self._new_slot_call_root("decode.method")
                branch_roots = list(roots) + [method]
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_obj_getattr", (receiver,), result_slot=method,
                    suffix_args=(self._attr_name_ptr("decode"),), span=expr.span,
                )
                current = self.builder.load(method, name=self._fresh("decode.callable"))
                self._emit_attribute_error_if_null(current, "decode", expr.func.span)
                positional, keywords = self._slot_call_split_operands(expr)
                args = self._emit_slot_call_args_tuple(positional, "decode.args")
                branch_roots.append(args)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                kwargs = self._emit_slot_call_kwargs_object(keywords, None, expr.span, "decode.kwargs", method)
                branch_roots.append(kwargs)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                status = self.builder.call(
                    self.runtime["py_obj_call_slots"],
                    [self._as_gc_ptr(method), self._as_gc_ptr(args), self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
                    name=self._fresh("decode.invoke"),
                )
                self._slot_call_note_published(output)
                self._slot_call_check_status(status, "decode method call", expr.span)
                self._emit_post_call_err_check(expr.span)
                self._release_slot_call_roots(tuple(branch_roots[len(roots):]))
                self.builder.branch(done_block)
                self.builder.position_at_end(done_block)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_note_published(output)
            self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("decode.output"))
        return self._take_slot_call_root(output)

    def _emit_bytes_decode_call(self, recv, receiver_expr, operands, span):
        if not self._owned_release_needed(recv, receiver_expr):
            recv = self._gc_retain(recv, name=self._fresh("decode.receiver.retain"))
        self._gc_pin(recv)
        pinned = [(recv, True)]
        encoding = self._emit_str_literal("utf-8")
        errors = self._emit_str_literal("strict")
        for key, operand in operands:
            value = self._emit_expr_with_cpy_operand_cleanup(
                operand, (), pinned_pcc=tuple(pinned), as_pcc_object=True,
            )
            if not self._owned_release_needed(value, operand):
                value = self._gc_retain(value, name=self._fresh("decode.argument.retain"))
            self._gc_pin(value)
            pinned.append((value, True))
            if key == "encoding":
                encoding = value
            else:
                errors = value
        old_target = self._current_try_err_block()
        target = old_target if old_target is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (), (), target, "decode.error.cleanup", tuple(pinned),
        )
        try:
            result = self.builder.call(
                self.runtime["py_bytes_decode_with_encoding"],
                [recv, encoding, errors], name=self._fresh("bytes.decode"),
            )
            self._emit_post_call_err_check(span)
        finally:
            self._try_err_block = old_target
        self._note_owned_object_value(result)
        self._gc_pin(result)
        for value, _owned in pinned:
            self._gc_unpin(value)
            self._gc_release(value)
        self._gc_unpin(result)
        return result

    def _emit_str_tailmatch_range(self, expr: Call, recv: ir.Value):
        # Bounds invoke __index__ only after all call arguments are evaluated.
        arguments = []
        pinned = []
        for arg in expr.args:
            value = self._emit_expr_with_cpy_operand_cleanup(
                arg, (), pinned_pcc=tuple(pinned), as_object=True,
            )
            if not self._owned_release_needed(value, arg):
                value = self._gc_retain(value, name=self._fresh("str.tailmatch.retain"))
            self._gc_pin(value)
            arguments.append(value)
            pinned.append((value, True))
        old_target = self._current_try_err_block()
        target = old_target if old_target is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (), (), target, "str.tailmatch.cleanup", tuple(pinned),
        )
        try:
            start = self.builder.call(
                self.runtime["py_slice_index_i64"],
                [arguments[1], ir.Constant(_I64, 0)],
                name=self._fresh("str.tailmatch.start"),
            )
            self._emit_post_call_err_check(expr.span)
            end = ir.Constant(_I64, _STR_FIND_END_DEFAULT)
            if len(arguments) == 3:
                end = self.builder.call(self.runtime["py_slice_index_i64"],
                                        [arguments[2], end],
                                        name=self._fresh("str.tailmatch.end"))
                self._emit_post_call_err_check(expr.span)
            result = self.builder.call(
                self.runtime["py_str_tailmatch_range"],
                [recv, arguments[0], start, end,
                 ir.Constant(_I64, int(expr.func.name == "endswith"))],
                name=self._fresh("str.tailmatch.range"),
            )
            self._emit_post_call_err_check(expr.span)
        finally:
            self._try_err_block = old_target
        for value in arguments:
            self._gc_unpin(value)
            self._gc_release(value)
        return self.builder.icmp_signed("!=", result, ir.Constant(_I64, 0),
                                        name=self._fresh("str.tailmatch.bit"))

    def _emit_owned_native_join_call(self, expr: Call):
        """Publish a native join result before releasing either input owner."""
        binary = isinstance(expr.func.obj.ty, (BytesType, ByteArrayType))
        runtime_name = "py_bytes_join" if binary else "py_str_join"
        label = "bytes.join" if binary else "str.join"
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(label + ".result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            separator = self._emit_slot_call_operand(expr.func.obj, label + ".separator")
            roots.append(separator)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            items = self._emit_slot_call_operand(expr.args[0], label + ".items")
            roots.append(items)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                runtime_name, (separator, items), result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots((separator, items))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh(label + ".current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

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
        self._note_owned_object_value(result)
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
        self._note_owned_object_value(result)
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

    def _emit_owned_padding_method(self, expr):
        """Publish text/byte padding before retiring any input owner.

        Dynamic receivers need an actual type check: encode() and imported
        returns can lose their static bytes type, while user methods sharing
        these names must retain ordinary attribute lookup and call binding.
        """
        attr = expr.func
        if (attr.name not in ("ljust", "rjust") or len(expr.args) not in (1, 2)
                or expr.kwargs or self._has_unpacked_operands(expr.args, expr.kwargs)
                or self._expr_looks_cpython(attr.obj)):
            return None
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("padding.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(attr.obj, "padding.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            dynamic = isinstance(attr.obj.ty, DynType)
            generic_block = None
            done_block = None
            byte_family = isinstance(attr.obj.ty, (BytesType, ByteArrayType))
            if dynamic:
                tag = self._slot_call_runtime_call("py_obj_type_tag", (receiver,), span=expr.span)
                is_bytes = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_BYTES))
                is_bytearray = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_BYTEARRAY))
                byte_family = self.builder.or_(is_bytes, is_bytearray)
                is_string = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_STR))
                is_native = self.builder.or_(byte_family, is_string)
                native_block = self.current_function.append_basic_block(self._fresh("padding.native"))
                generic_block = self.current_function.append_basic_block(self._fresh("padding.generic"))
                done_block = self.current_function.append_basic_block(self._fresh("padding.done"))
                self.builder.cbranch(is_native, native_block, generic_block)
                self.builder.position_at_end(native_block)

            branch_roots = list(roots)
            operands = []
            for argument in expr.args:
                operand = self._emit_slot_call_operand(argument, "padding.argument")
                operands.append(operand)
                branch_roots.append(operand)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            # Evaluate fill before __index__, just as ordinary Python calls do.
            # Each argument and the receiver remain independent owning roots.
            width = self.builder.call(
                self.runtime["py_index_i64_checked_slots"],
                [self._as_gc_ptr(operands[0])], name=self._fresh("padding.width"),
            )
            self._emit_post_call_err_check(expr.span)
            arguments = (receiver,)
            suffix = (width, ir.Constant(_CSTR, None))
            order = ()
            if len(operands) == 2:
                arguments = (receiver, operands[1])
                suffix = (width,)
                order = (0, 2, 1)
            if dynamic:
                bytes_block = self.current_function.append_basic_block(self._fresh("padding.bytes"))
                text_block = self.current_function.append_basic_block(self._fresh("padding.text"))
                ready_block = self.current_function.append_basic_block(self._fresh("padding.ready"))
                self.builder.cbranch(byte_family, bytes_block, text_block)
                self.builder.position_at_end(bytes_block)
                self._slot_call_runtime_call(
                    "py_bytes_" + attr.name, arguments, result_slot=output,
                    suffix_args=suffix, argument_order=order, span=expr.span,
                )
                self.builder.branch(ready_block)
                self.builder.position_at_end(text_block)
                self._slot_call_runtime_call(
                    "py_str_" + attr.name, arguments, result_slot=output,
                    suffix_args=suffix, argument_order=order, span=expr.span,
                )
                self.builder.branch(ready_block)
                self.builder.position_at_end(ready_block)
            else:
                self._slot_call_runtime_call(
                    ("py_bytes_" if byte_family else "py_str_") + attr.name,
                    arguments, result_slot=output, suffix_args=suffix,
                    argument_order=order, span=expr.span,
                )
            self._release_slot_call_roots(tuple(operands))
            if dynamic:
                self.builder.branch(done_block)
                self.builder.position_at_end(generic_block)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                method = self._new_slot_call_root("padding.method")
                branch_roots = list(roots) + [method]
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_obj_getattr", (receiver,), result_slot=method,
                    suffix_args=(self._attr_name_ptr(attr.name),), span=expr.span,
                )
                current = self.builder.load(method, name=self._fresh("padding.callable"))
                self._emit_attribute_error_if_null(current, attr.name, attr.span)
                positional, keywords = self._slot_call_split_operands(expr)
                args = self._emit_slot_call_args_tuple(positional, "padding.args")
                branch_roots.append(args)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                kwargs = self._emit_slot_call_kwargs_object(keywords, None, expr.span, "padding.kwargs", method)
                branch_roots.append(kwargs)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                status = self.builder.call(
                    self.runtime["py_obj_call_slots"],
                    [self._as_gc_ptr(method), self._as_gc_ptr(args), self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
                    name=self._fresh("padding.invoke"),
                )
                self._slot_call_note_published(output)
                self._slot_call_check_status(status, "padding method call", expr.span)
                self._emit_post_call_err_check(expr.span)
                self._release_slot_call_roots(tuple(branch_roots[len(roots):]))
                self.builder.branch(done_block)
                self.builder.position_at_end(done_block)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_note_published(output)
            self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("padding.output"))
        return self._take_slot_call_root(output)

    def _emit_owned_strip_method(self, expr):
        """Dispatch strip through the actual text/byte family with rooted inputs.

        A dynamic binary read must retain its bytes result. Other receivers keep
        ordinary method lookup before argument evaluation, and None selects
        whitespace stripping without reading it as a character buffer.
        """
        attr = expr.func
        if (attr.name not in ("strip", "lstrip", "rstrip") or len(expr.args) > 1
                or expr.kwargs or self._has_unpacked_operands(expr.args, expr.kwargs)
                or self._expr_looks_cpython(attr.obj)):
            return None
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("strip.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(attr.obj, "strip.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            dynamic = isinstance(attr.obj.ty, DynType)
            generic_block = None
            done_block = None
            byte_family = isinstance(attr.obj.ty, (BytesType, ByteArrayType))
            if dynamic:
                tag = self._slot_call_runtime_call("py_obj_type_tag", (receiver,), span=expr.span)
                is_bytes = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_BYTES))
                is_bytearray = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_BYTEARRAY))
                byte_family = self.builder.or_(is_bytes, is_bytearray)
                is_string = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_STR))
                is_native = self.builder.or_(byte_family, is_string)
                native_block = self.current_function.append_basic_block(self._fresh("strip.native"))
                generic_block = self.current_function.append_basic_block(self._fresh("strip.generic"))
                done_block = self.current_function.append_basic_block(self._fresh("strip.done"))
                self.builder.cbranch(is_native, native_block, generic_block)
                self.builder.position_at_end(native_block)

            branch_roots = list(roots)
            operands = []
            for argument in expr.args:
                operand = self._emit_slot_call_operand(argument, "strip.argument")
                operands.append(operand)
                branch_roots.append(operand)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            if dynamic:
                bytes_block = self.current_function.append_basic_block(self._fresh("strip.bytes"))
                text_block = self.current_function.append_basic_block(self._fresh("strip.text"))
                ready_block = self.current_function.append_basic_block(self._fresh("strip.ready"))
                self.builder.cbranch(byte_family, bytes_block, text_block)
                native_branches = ((bytes_block, "py_bytes_"), (text_block, "py_str_"))
            else:
                native_branches = ((None, "py_bytes_" if byte_family else "py_str_"),)
            for native_block, prefix in native_branches:
                if native_block is not None:
                    self.builder.position_at_end(native_block)
                runtime_name = prefix + attr.name
                if not operands:
                    self._slot_call_runtime_call(
                        runtime_name, (receiver,), result_slot=output, span=expr.span,
                    )
                else:
                    chars = operands[0]
                    chars_tag = self._slot_call_runtime_call("py_obj_type_tag", (chars,), span=expr.span)
                    default_block = self.current_function.append_basic_block(self._fresh("strip.default"))
                    chars_block = self.current_function.append_basic_block(self._fresh("strip.chars"))
                    result_block = self.current_function.append_basic_block(self._fresh("strip.result"))
                    is_none = self.builder.icmp_signed("==", chars_tag, ir.Constant(_I64, PY_TYPE_NONE))
                    self.builder.cbranch(is_none, default_block, chars_block)
                    self.builder.position_at_end(default_block)
                    self._slot_call_runtime_call(
                        runtime_name, (receiver,), result_slot=output, span=expr.span,
                    )
                    self.builder.branch(result_block)
                    self.builder.position_at_end(chars_block)
                    if prefix == "py_bytes_":
                        valid_chars = self.builder.icmp_signed("==", chars_tag, ir.Constant(_I64, PY_TYPE_BYTES))
                        for tag_value in (PY_TYPE_BYTEARRAY, PY_TYPE_MEMORYVIEW):
                            matches = self.builder.icmp_signed("==", chars_tag, ir.Constant(_I64, tag_value))
                            valid_chars = self.builder.or_(valid_chars, matches)
                        message = "strip argument must be a bytes-like object or None"
                    else:
                        valid_chars = self.builder.icmp_signed("==", chars_tag, ir.Constant(_I64, PY_TYPE_STR))
                        message = "strip argument must be str or None"
                    valid_block = self.current_function.append_basic_block(self._fresh("strip.valid"))
                    invalid_block = self.current_function.append_basic_block(self._fresh("strip.invalid"))
                    self.builder.cbranch(valid_chars, valid_block, invalid_block)
                    self.builder.position_at_end(invalid_block)
                    self._emit_builtin_exception_and_branch("TypeError", message, expr.span)
                    self.builder.position_at_end(valid_block)
                    self._slot_call_runtime_call(
                        runtime_name + "_chars", (receiver, chars), result_slot=output, span=expr.span,
                    )
                    self.builder.branch(result_block)
                    self.builder.position_at_end(result_block)
                if dynamic:
                    self.builder.branch(ready_block)
            if dynamic:
                self.builder.position_at_end(ready_block)
            self._release_slot_call_roots(tuple(operands))
            if dynamic:
                self.builder.branch(done_block)
                self.builder.position_at_end(generic_block)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                method = self._new_slot_call_root("strip.method")
                branch_roots = list(roots) + [method]
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_obj_getattr", (receiver,), result_slot=method,
                    suffix_args=(self._attr_name_ptr(attr.name),), span=expr.span,
                )
                current = self.builder.load(method, name=self._fresh("strip.callable"))
                self._emit_attribute_error_if_null(current, attr.name, attr.span)
                positional, keywords = self._slot_call_split_operands(expr)
                args = self._emit_slot_call_args_tuple(positional, "strip.args")
                branch_roots.append(args)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                kwargs = self._emit_slot_call_kwargs_object(keywords, None, expr.span, "strip.kwargs", method)
                branch_roots.append(kwargs)
                self._try_err_block = self._slot_call_cleanup_block(tuple(branch_roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                status = self.builder.call(
                    self.runtime["py_obj_call_slots"],
                    [self._as_gc_ptr(method), self._as_gc_ptr(args), self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
                    name=self._fresh("strip.invoke"),
                )
                self._slot_call_note_published(output)
                self._slot_call_check_status(status, "strip method call", expr.span)
                self._emit_post_call_err_check(expr.span)
                self._release_slot_call_roots(tuple(branch_roots[len(roots):]))
                self.builder.branch(done_block)
                self.builder.position_at_end(done_block)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_note_published(output)
            self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("strip.output"))
        return self._take_slot_call_root(output)

    def _maybe_emit_owned_str_result(self, expr):
        """Evaluate native string operands in roots and publish before cleanup.

        Only the explicit object-returning shapes below enter this producer.
        Scalar-returning, encoding, formatting, foreign and keyword binding
        routes keep their separate contracts.
        """
        attr = expr.func
        if expr.kwargs or self._has_unpacked_operands(expr.args, expr.kwargs):
            return None
        if self._expr_looks_cpython(attr.obj):
            return None
        name = attr.name
        if name in ("ljust", "rjust"):
            return self._emit_owned_padding_method(expr)
        if name in ("strip", "lstrip", "rstrip"):
            return self._emit_owned_strip_method(expr)
        runtime_name = None
        object_count = len(expr.args)
        scalar_index = -1
        null_separator = False
        if name in ("upper", "lower", "capitalize", "swapcase", "title", "casefold"):
            if expr.args:
                return None
            runtime_name = "py_str_" + name
        elif name == "zfill":
            if len(expr.args) != 1:
                return None
            runtime_name = "py_str_zfill"
            scalar_index = 0
            object_count = 0
        elif name in ("split", "rsplit"):
            if len(expr.args) > 2:
                return None
            runtime_name = "py_str_split"
            null_separator = not expr.args
            if len(expr.args) == 2:
                runtime_name = "py_str_" + name + "_maxsplit"
                scalar_index = 1
                object_count = 1
        elif name == "replace":
            if len(expr.args) not in (2, 3):
                return None
            runtime_name = "py_str_replace"
            if len(expr.args) == 3:
                runtime_name += "_count"
                scalar_index = 2
                object_count = 2
        elif name in ("partition", "rpartition", "removeprefix", "removesuffix"):
            if len(expr.args) != 1:
                return None
            runtime_name = "py_str_" + name
        else:
            return None

        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("str.method.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(attr.obj, "str.method.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            arguments = [receiver]
            for index, argument in enumerate(expr.args):
                operand = self._emit_slot_call_operand(argument, "str.method.argument")
                roots.append(operand)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if index < object_count:
                    arguments.append(operand)
            suffix = ()
            if null_separator:
                suffix = (ir.Constant(_CSTR, None),)
            if scalar_index >= 0:
                # The checked conversion receives the actual owning argument
                # slot. Other arguments stay rooted while __index__ executes.
                scalar = self.builder.call(
                    self.runtime["py_index_i64_checked_slots"],
                    [self._as_gc_ptr(roots[-1])],
                    name=self._fresh("str.method.index"),
                )
                self._emit_post_call_err_check(expr.span)
                suffix = (scalar,)
            self._slot_call_runtime_call(
                runtime_name, tuple(arguments), result_slot=output,
                suffix_args=suffix, span=expr.span,
            )
            self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("str.method.output"))
        return self._take_slot_call_root(output)

    def _emit_str_method_with_receiver(self, expr: Call, recv: ir.Value, dynamic: bool):
        if expr.func.name == "encode":
            return self._emit_str_encode_call(expr, recv)
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
            cpy_target = old_cpy if old_cpy is not None else pcc_target
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
        if self._has_unpacked_operands(expr.args, expr.kwargs):
            # The helpers take fixed operands; the generic method call binds
            # an unpacked argument list (``module.join(*parts)``).
            return None
        if isinstance(attr.obj.ty, (BytesType, ByteArrayType)):
            # A statically bytes/bytearray receiver must NOT be forced onto the
            # StrType fast path: py_str_upper on a bytearray reads the raw bytes
            # as a string (e.g. ``bytearray(b"abz").upper()`` -> garbage). Bail
            # so the precise bytes branch (py_bytes_*) in
            # method_call_expression_lowering handles it.
            return None
        # Encode owns encoding/errors binding, including invalid keywords.
        # Other string helpers keep their established keyword contracts.
        if expr.kwargs and not (
            (attr.name == "splitlines" and self._kwargs_are_only_keepends(expr.kwargs))
            or attr.name == "encode"
        ):
            return None
        owned_result = self._maybe_emit_owned_str_result(expr)
        if owned_result is not None:
            return owned_result
        if (attr.name == "join" and len(expr.args) == 1
                and not self._expr_looks_cpython(attr.obj)):
            return self._emit_owned_native_join_call(expr)
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
        if name in ("startswith", "endswith") and 2 <= len(expr.args) <= 3:
            return self._emit_str_tailmatch_range(expr, recv)
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
        if self._has_unpacked_operands(expr.args, expr.kwargs):
            # As for str: an unpacked argument list needs the generic call.
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
        if name == "decode" and not self._expr_looks_cpython(attr.obj):
            owned = self._emit_owned_bytes_decode(expr, True)
            if owned is not None:
                return owned
        if name == "hex" and not expr.args and not expr.kwargs and not self._expr_looks_cpython(attr.obj):
            return self._emit_owned_unary_runtime_call(expr, "py_bytes_hex")
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
            operands = []
            if expr.args:
                operands.append(("encoding", expr.args[0]))
            if len(expr.args) == 2:
                operands.append(("errors", expr.args[1]))
            operands.extend(expr.kwargs)
            bytes_result = self._emit_bytes_decode_call(recv, attr.obj, operands, expr.span)
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
            self._note_owned_object_value(result)
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
        if attr.name == "encode" and self._has_unpacked_operands(expr.args, expr.kwargs):
            return None
        if expr.kwargs and not (
            (attr.name == "splitlines" and self._kwargs_are_only_keepends(expr.kwargs))
            or attr.name in ("format", "encode")
        ):
            return None
        owned_result = self._maybe_emit_owned_str_result(expr)
        if owned_result is not None:
            return owned_result
        if (attr.name == "join" and len(expr.args) == 1
                and not self._expr_looks_cpython(attr.obj)):
            return self._emit_owned_native_join_call(expr)
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
            if 2 <= len(expr.args) <= 3:
                return self._emit_str_tailmatch_range(expr, recv)
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
