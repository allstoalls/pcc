"""Name expression lowering helpers for L1CodeGen."""

from __future__ import annotations

import os
from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.builtin_function_values import (
    BUILTIN_HELPER_PREFIX,
    BUILTIN_VALUE_PREFIX,
    builtin_call_needs_wrapper,
    builtin_function_value_name_exists,
    builtin_function_value_names,
)
from pcc.frontends.python.py_ast import BoolType, ByteArrayType, BytesType, ClassType, ComplexType, DictType, DynType, FloatType, IntType, ListType, MemoryViewType, Name, NoneType, StrType, TupleType, Type
from pcc.frontends.python.codegen.builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG
from pcc.frontends.python.codegen.freestanding_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CLASS,
    PY_TYPE_CLASSMETHOD,
    PY_TYPE_COMPLEX,
    PY_TYPE_DICT,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_PROPERTY,
    PY_TYPE_SET,
    PY_TYPE_STATICMETHOD,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global
from pcc.frontends.python.codegen.local_bound_lowering import check_local_bound
from pcc.frontends.python.codegen.cpy_import_state import live_import_name_slot
from pcc.frontends.python.codegen.native_os import native_os_descriptor_constant

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = _I8.as_pointer()
_CPY_BUILTIN_TYPE_NAMES = frozenset(
    {
        "int",
        "str",
        "list",
        "dict",
        "tuple",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "set",
        "frozenset",
        "complex",
        "memoryview",
        "object",
        "type",
        "Exception",
        "BaseException",
    }
)
_NATIVE_BUILTIN_CALLABLE_NAMES = frozenset(
    {
        "staticmethod",
        "classmethod",
        "property",
        "abs",
        "bool",
        "bytes",
        "bytearray",
        "chr",
        "complex",
        "dict",
        "float",
        "id",
        "int",
        "isinstance",
        "iter",
        "list",
        "memoryview",
        "object",
        "range",
        "repr",
        "set",
        "str",
        "tuple",
        "type",
    }
)


def _is_nested_hoist_collision_name(name: str, direct_hoist: str) -> bool:
    prefix = f"{direct_hoist}_"
    if not name.startswith(prefix):
        return False
    suffix = name[len(prefix) :]
    return suffix.isdigit()


class NameLoweringMixin:
    def _emit_owned_namespace_runtime_value(self, runtime_name, arguments, expr=None):
        """Publish a native namespace API's NEW reference before any check."""
        sink = self._slot_call_result_sink(expr) if expr is not None else None
        output = sink
        if output is None:
            output = self._new_slot_call_root("namespace.value")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = () if sink is not None else (output,)
        self._try_err_block = self._slot_call_cleanup_block(roots, target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            descriptor_name = {
                "py_os_open_function": "open", "py_os_close_function": "close",
                "py_file_fdopen_function": "fdopen", "py_os_mkdir_function": "mkdir",
                "py_os_pwrite_function": "pwrite", "py_os_ftruncate_function": "ftruncate",
            }.get(runtime_name)
            span = expr.span if expr is not None else None
            if descriptor_name is not None and "os" in getattr(self, "_sibling_module_inits", ()):
                value = self.builder.call(
                    self.runtime["py_module_attr_get"],
                    [self._pooled_cstr_ptr("os", ".owned.module.os"), self._attr_name_ptr(descriptor_name)],
                    name=self._fresh("owned.module.member.current"),
                )
                self._publish_slot_call_owned(output, value, label="owned module member")
                self._emit_attribute_error_if_null(
                    self.builder.load(output), descriptor_name, span,
                )
            else:
                self._slot_call_runtime_call(
                    runtime_name, (), result_slot=output, suffix_args=arguments, span=span,
                )
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("namespace.value.current"))
        return self._take_slot_call_root(output)

    def _emit_owned_builtin_exception_class(self, tag, expr=None):
        """Copy a canonical class from its mapped cache into a real owner."""
        sink = self._slot_call_result_sink(expr) if expr is not None else None
        output = sink
        if output is None:
            output = self._new_slot_call_root("builtin.exception.class")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = () if sink is not None else (output,)
        self._try_err_block = self._slot_call_cleanup_block(roots, target)
        self._cpy_operand_cleanup_block = self._try_err_block
        span = expr.span if expr is not None else None
        try:
            tag_value = ir.Constant(_I64, tag)
            # This initializes the canonical cache and returns a BORROWED
            # value. Never transport that raw value across another call.
            self.builder.call(self.runtime["py_exc_builtin_class"], [tag_value])
            self._emit_post_call_err_check(span)
            source = self.builder.call(
                self.runtime["py_subs_exc_cache_slot"], [tag_value],
                name=self._fresh("builtin.exception.cache.slot"),
            )
            # The cache is a mapped owning root. Copying from its physical
            # slot permits relocation while establishing the caller's owner.
            self._slot_call_copy_source(output, source, span=span)
            self._slot_call_note_published(output)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("builtin.exception.class.current"))
        return self._take_slot_call_root(output)

    def _name_returns_native_builtin_callable_value(self, name: str) -> bool:
        value = self._native_builtin_value_for_name(name)
        if value is not None and value.startswith("builtins."):
            canonical = value[len("builtins."):]
            return (canonical in _NATIVE_BUILTIN_CALLABLE_NAMES
                    or self._builtin_function_wrapper(canonical) is not None)
        # Native builtin spelling is a fallback after actual local/global
        # bindings. Imported aliases were resolved above; an ordinary module
        # value or function with the same spelling keeps its own identity.
        if name in self.env or name in self._module_globals or name in self.functions:
            return False
        return (name in _NATIVE_BUILTIN_CALLABLE_NAMES
                or self._builtin_function_wrapper(name) is not None)

    def _builtin_function_wrapper(self, name: str):
        """The injected Python wrapper of builtin function ``name``, if any."""
        if name not in builtin_function_value_names():
            return None
        return self.functions.get(BUILTIN_VALUE_PREFIX + name)

    def _builtin_call_takes_wrapper(self, expr, name: str) -> bool:
        """A call of the unshadowed builtin ``name`` its direct lowering does
        not parse (a * or ** argument, a keyword it does not take): the call
        goes through the builtin's Python wrapper, CPython's own parsing.
        The wrappers themselves call builtins directly."""
        if not builtin_function_value_name_exists(name):
            return False
        if self.functions.get(BUILTIN_VALUE_PREFIX + name) is None:
            return False
        if name in self.env or name in self._module_globals or name in self.functions:
            return False
        current = self.current_func_def
        if current is not None and current.name.startswith(BUILTIN_HELPER_PREFIX):
            return False
        return builtin_call_needs_wrapper(name, expr)

    def _emit_builtin_function_value(self, name, wrapper, expr=None):
        """Publish the process-wide builtin function object for ``name``.

        The module's wrapper becomes a candidate function object (cached per
        module like any module-level def); the runtime keeps the first one
        published under ``name`` and flags it as a builtin, so every module
        reads the same object.
        """
        sink = self._slot_call_result_sink(expr) if expr is not None else None
        output = sink if sink is not None else self._new_slot_call_root("builtin.function.value")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = [] if sink is not None else [output]
        candidate = self._new_slot_call_root("builtin.function.candidate")
        roots.append(candidate)
        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        span = expr.span if expr is not None else None
        try:
            value = self._emit_native_func_value(name, BUILTIN_VALUE_PREFIX + name, wrapper, ())
            self._publish_slot_call_owned(candidate, value, label="builtin function candidate")
            self._slot_call_runtime_call(
                "py_builtin_function_publish", (candidate,), result_slot=output,
                suffix_args=(self._pooled_cstr_ptr(name, ".builtin.function.name"),), span=span,
            )
            self._release_slot_call_roots((candidate,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("builtin.function.current"))
        return self._take_slot_call_root(output)

    def _emit_native_builtin_callable_type_error(
        self,
        builder: ir.IRBuilder,
        message: str,
        name: str,
        suffix: str,
    ) -> None:
        zero = ir.Constant(_I32, 0)
        gv = self._cstr_global(message, f".builtin.{name}.{suffix}.type_error")
        msg_ptr = builder.gep(gv, [zero, zero], inbounds=True)
        exc = builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 3), msg_ptr],
            name=f"{name}.type_error",
        )
        builder.call(self.runtime["py_raise"], [exc])
        builder.ret(ir.Constant(_CSTR, None))

    def _emit_native_range_callable_value(self) -> ir.Value:
        """Materialize ``range`` as a native first-class callable.

        Direct ``range(...)`` calls use the static lowering in
        ``_emit_range_value_call``.  Aliases that escape through a module
        global need an ordinary ``PyFunc`` object as well; otherwise
        ``saved_range = range`` is compiler metadata only and importing
        ``saved_range`` from a sibling observes an uninitialized binding.
        """
        name = "range"
        adapter_name = "__pcc_builtin_callable_range"
        adapter = self.module.globals.get(adapter_name)
        if not isinstance(adapter, ir.Function):
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
            adapter = ir.Function(self.module, fnty, name=adapter_name)
            adapter.linkage = "internal"
            entry_bb = adapter.append_basic_block("entry")
            builder = ir.IRBuilder(entry_bb)
            _captures_arg, args_arg = adapter.args
            argc = builder.call(
                self.runtime["py_tuple_len"],
                [args_arg],
                name="range.argc",
            )
            one_bb = adapter.append_basic_block("range.one")
            two_check_bb = adapter.append_basic_block("range.two_check")
            two_bb = adapter.append_basic_block("range.two")
            three_check_bb = adapter.append_basic_block("range.three_check")
            three_bb = adapter.append_basic_block("range.three")
            arity_error_bb = adapter.append_basic_block("range.arity_error")
            setup_bb = adapter.append_basic_block("range.setup")

            is_one = builder.icmp_signed(
                "==", argc, ir.Constant(_I64, 1), name="range.argc1"
            )
            builder.cbranch(is_one, one_bb, two_check_bb)

            builder.position_at_end(two_check_bb)
            is_two = builder.icmp_signed(
                "==", argc, ir.Constant(_I64, 2), name="range.argc2"
            )
            builder.cbranch(is_two, two_bb, three_check_bb)

            builder.position_at_end(three_check_bb)
            is_three = builder.icmp_signed(
                "==", argc, ir.Constant(_I64, 3), name="range.argc3"
            )
            builder.cbranch(is_three, three_bb, arity_error_bb)

            builder.position_at_end(arity_error_bb)
            self._emit_native_builtin_callable_type_error(
                builder,
                "range() takes 1 to 3 arguments",
                name,
                "arity",
            )

            builder.position_at_end(one_bb)
            one_arg = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 0)],
                name="range.one.arg",
            )
            one_stop = builder.call(
                self.runtime["py_obj_index_i64"],
                [one_arg],
                name="range.one.stop",
            )
            builder.call(self.runtime["py_decref"], [one_arg])
            builder.branch(setup_bb)

            builder.position_at_end(two_bb)
            two_arg0 = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 0)],
                name="range.two.arg0",
            )
            two_arg1 = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 1)],
                name="range.two.arg1",
            )
            two_start = builder.call(
                self.runtime["py_obj_index_i64"],
                [two_arg0],
                name="range.two.start",
            )
            two_stop = builder.call(
                self.runtime["py_obj_index_i64"],
                [two_arg1],
                name="range.two.stop",
            )
            builder.call(self.runtime["py_decref"], [two_arg0])
            builder.call(self.runtime["py_decref"], [two_arg1])
            builder.branch(setup_bb)

            builder.position_at_end(three_bb)
            three_arg0 = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 0)],
                name="range.three.arg0",
            )
            three_arg1 = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 1)],
                name="range.three.arg1",
            )
            three_arg2 = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 2)],
                name="range.three.arg2",
            )
            three_start = builder.call(
                self.runtime["py_obj_index_i64"],
                [three_arg0],
                name="range.three.start",
            )
            three_stop = builder.call(
                self.runtime["py_obj_index_i64"],
                [three_arg1],
                name="range.three.stop",
            )
            three_step = builder.call(
                self.runtime["py_obj_index_i64"],
                [three_arg2],
                name="range.three.step",
            )
            builder.call(self.runtime["py_decref"], [three_arg0])
            builder.call(self.runtime["py_decref"], [three_arg1])
            builder.call(self.runtime["py_decref"], [three_arg2])
            builder.branch(setup_bb)

            builder.position_at_end(setup_bb)
            start = builder.phi(_I64, name="range.start")
            start.add_incoming(ir.Constant(_I64, 0), one_bb)
            start.add_incoming(two_start, two_bb)
            start.add_incoming(three_start, three_bb)
            stop = builder.phi(_I64, name="range.stop")
            stop.add_incoming(one_stop, one_bb)
            stop.add_incoming(two_stop, two_bb)
            stop.add_incoming(three_stop, three_bb)
            step = builder.phi(_I64, name="range.step")
            step.add_incoming(ir.Constant(_I64, 1), one_bb)
            step.add_incoming(ir.Constant(_I64, 1), two_bb)
            step.add_incoming(three_step, three_bb)

            out = builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name="range.list",
            )
            idx_slot = builder.alloca(_I64, name="range.idx.addr")
            builder.store(start, idx_slot)
            cond_bb = adapter.append_basic_block("range.cond")
            body_bb = adapter.append_basic_block("range.body")
            step_bb = adapter.append_basic_block("range.advance")
            end_bb = adapter.append_basic_block("range.end")
            builder.branch(cond_bb)

            builder.position_at_end(cond_bb)
            current = builder.load(idx_slot, name="range.current")
            step_positive = builder.icmp_signed(
                ">", step, ir.Constant(_I64, 0), name="range.step.positive"
            )
            forward = builder.icmp_signed("<", current, stop, name="range.forward")
            backward = builder.icmp_signed(">", current, stop, name="range.backward")
            keep = builder.select(
                step_positive,
                forward,
                backward,
                name="range.keep",
            )
            builder.cbranch(keep, body_bb, end_bb)

            builder.position_at_end(body_bb)
            item = builder.call(
                self.runtime["py_int_from_i64"],
                [current],
                name="range.item",
            )
            builder.call(self.runtime["py_list_append"], [out, item])
            builder.branch(step_bb)

            builder.position_at_end(step_bb)
            next_value = builder.add(current, step, name="range.next")
            builder.store(next_value, idx_slot)
            builder.branch(cond_bb)

            builder.position_at_end(end_bb)
            builder.ret(out)

        fn_obj = self.builder.call(
            self.runtime["py_builtin_function_value"],
            [adapter, self._attr_name_ptr(name)],
            name=self._fresh("range.builtin.func"),
        )
        self._note_owned_object_value(fn_obj)
        return fn_obj

    def _emit_native_builtin_callable_value(self, name: str, expr=None) -> Optional[ir.Value]:
        builtin_value = self._native_builtin_value_for_name(name)
        canonical_names = {
            "builtins.staticmethod": "staticmethod",
            "builtins.classmethod": "classmethod",
            "builtins.property": "property",
            "builtins.bool": "bool",
            "builtins.bytes": "bytes",
            "builtins.bytearray": "bytearray",
            "builtins.complex": "complex",
            "builtins.dict": "dict",
            "builtins.float": "float",
            "builtins.int": "int",
            "builtins.list": "list",
            "builtins.memoryview": "memoryview",
            "builtins.object": "object",
            "builtins.range": "range",
            "builtins.set": "set",
            "builtins.str": "str",
            "builtins.tuple": "tuple",
            "builtins.type": "type",
        }
        canonical_name = (name[len("builtins."):] if name.startswith("builtins.")
                          else canonical_names.get(builtin_value, name))
        if (builtin_value is not None and builtin_value.startswith("builtins.")
                and not name.startswith("builtins.")):
            canonical_name = builtin_value[len("builtins."):]
        wrapper = self._builtin_function_wrapper(canonical_name)
        if wrapper is not None:
            return self._emit_builtin_function_value(canonical_name, wrapper, expr)
        if canonical_name in _BUILTIN_EXC_TAG:
            return self._emit_owned_builtin_exception_class(_BUILTIN_EXC_TAG[canonical_name])
        if canonical_name not in _NATIVE_BUILTIN_CALLABLE_NAMES:
            return None
        if canonical_name == "range":
            return self._emit_native_range_callable_value()
        builtin_tags = {
            "staticmethod": PY_TYPE_STATICMETHOD,
            "classmethod": PY_TYPE_CLASSMETHOD,
            "property": PY_TYPE_PROPERTY,
            "bool": PY_TYPE_BOOL,
            "int": PY_TYPE_INT,
            "float": PY_TYPE_FLOAT,
            "str": PY_TYPE_STR,
            "list": PY_TYPE_LIST,
            "dict": PY_TYPE_DICT,
            "set": PY_TYPE_SET,
            "tuple": PY_TYPE_TUPLE,
            "bytes": PY_TYPE_BYTES,
            "bytearray": PY_TYPE_BYTEARRAY,
            "memoryview": PY_TYPE_MEMORYVIEW,
            "complex": PY_TYPE_COMPLEX,
            # The class of classes, the same object type(cls) returns.
            "type": PY_TYPE_CLASS,
            # ``py_builtin_type_for_tag`` maps unknown tags to the runtime's
            # canonical object class. There is deliberately no object-instance
            # tag in the object header enum: ordinary instances carry their
            # concrete user class.
            "object": -1,
        }
        if canonical_name in builtin_tags:
            return self._emit_owned_namespace_runtime_value(
                "py_builtin_type_for_tag",
                (ir.Constant(_I64, builtin_tags[canonical_name]),), expr,
            )
        if canonical_name == "abs":
            fn_obj = self.builder.call(
                self.runtime["py_builtin_function_value"],
                [self.runtime["py_builtin_abs_entry"], self._attr_name_ptr("abs")],
                name=self._fresh("abs.builtin.func"),
            )
            self._note_owned_object_value(fn_obj)
            return fn_obj
        if canonical_name in ("chr", "id", "isinstance", "iter", "repr"):
            name = canonical_name
            adapter_name = f"__pcc_builtin_callable_{name}"
            adapter = self.module.globals.get(adapter_name)
            if not isinstance(adapter, ir.Function):
                fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
                adapter = ir.Function(self.module, fnty, name=adapter_name)
                adapter.linkage = "internal"
                builder = ir.IRBuilder(adapter.append_basic_block("entry"))
                args_arg = adapter.args[1]
                argc = builder.call(
                    self.runtime["py_tuple_len"],
                    [args_arg],
                    name=f"{name}.argc",
                )
                expected = 2 if name == "isinstance" else 1
                is_valid = builder.icmp_signed(
                    "==",
                    argc,
                    ir.Constant(_I64, expected),
                    name=f"{name}.arity.ok",
                )
                ok_bb = adapter.append_basic_block("arity.ok")
                error_bb = adapter.append_basic_block("arity.error")
                builder.cbranch(is_valid, ok_bb, error_bb)

                builder.position_at_end(error_bb)
                self._emit_native_builtin_callable_type_error(
                    builder,
                    f"{name}() takes exactly {expected} argument"
                    + ("s" if expected != 1 else ""),
                    name,
                    "arity",
                )

                builder.position_at_end(ok_bb)
                arg0 = builder.call(
                    self.runtime["py_tuple_get"],
                    [args_arg, ir.Constant(_I64, 0)],
                    name=f"{name}.arg0",
                )
                if name == "chr":
                    codepoint = builder.call(
                        self.runtime["py_obj_index_i64"],
                        [arg0],
                        name="chr.index",
                    )
                    result = builder.call(
                        self.runtime["py_chr_from_i64"],
                        [codepoint],
                        name="chr.result",
                    )
                elif name == "id":
                    address = builder.call(
                        self.runtime["py_obj_id"], [arg0], name="id.address"
                    )
                    result = builder.call(
                        self.runtime["py_int_from_i64"],
                        [address],
                        name="id.result",
                    )
                elif name == "iter":
                    result = builder.call(
                        self.runtime["py_obj_iter"],
                        [arg0],
                        name="iter.result",
                    )
                elif name == "repr":
                    result = builder.call(
                        self.runtime["py_obj_repr"],
                        [arg0],
                        name="repr.result",
                    )
                else:
                    arg1 = builder.call(
                        self.runtime["py_tuple_get"],
                        [args_arg, ir.Constant(_I64, 1)],
                        name="isinstance.arg1",
                    )
                    bit = builder.call(
                        self.runtime["py_obj_isinstance"],
                        [arg0, arg1],
                        name="isinstance.bit",
                    )
                    result = builder.call(
                        self.runtime["py_bool_from_bit"],
                        [builder.trunc(bit, _I32)],
                        name="isinstance.result",
                    )
                    builder.call(self.runtime["py_decref"], [arg1])
                builder.call(self.runtime["py_decref"], [arg0])
                builder.ret(result)

            fn_obj = self.builder.call(
                self.runtime["py_builtin_function_value"],
                [adapter, self._attr_name_ptr(name)],
                name=self._fresh(f"{name}.builtin.func"),
            )
            self._note_owned_object_value(fn_obj)
            return fn_obj
        adapter_name = f"__pcc_builtin_callable_{name}"
        adapter = self.module.globals.get(adapter_name)
        if not isinstance(adapter, ir.Function):
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
            adapter = ir.Function(self.module, fnty, name=adapter_name)
            adapter.linkage = "internal"
            builder = ir.IRBuilder(adapter.append_basic_block("entry"))
            captures_arg, args_arg = adapter.args
            del captures_arg
            argc = builder.call(
                self.runtime["py_tuple_len"],
                [args_arg],
                name=f"{name}.argc",
            )
            no_args_bb = adapter.append_basic_block(f"{name}.no_args")
            one_arg_bb = adapter.append_basic_block(f"{name}.one_arg")
            too_many_bb = adapter.append_basic_block(f"{name}.too_many")
            has_no_args = builder.icmp_signed(
                "==",
                argc,
                ir.Constant(_I64, 0),
                name=f"{name}.argc0",
            )
            has_one_arg = builder.icmp_signed(
                "==",
                argc,
                ir.Constant(_I64, 1),
                name=f"{name}.argc1",
            )
            argc_check_bb = adapter.append_basic_block(f"{name}.argc_check")
            builder.cbranch(has_no_args, no_args_bb, argc_check_bb)

            builder.position_at_end(argc_check_bb)
            builder.cbranch(has_one_arg, one_arg_bb, too_many_bb)

            builder.position_at_end(no_args_bb)
            if name == "int":
                zero_obj = builder.call(
                    self.runtime["py_int_from_i64"],
                    [ir.Constant(_I64, 0)],
                    name="int.zero",
                )
                builder.ret(zero_obj)
            elif name == "float":
                zero_obj = builder.call(
                    self.runtime["py_float_from_f64"],
                    [ir.Constant(_DOUBLE, 0.0)],
                    name="float.zero",
                )
                builder.ret(zero_obj)
            else:
                empty_gv = self._cstr_global("", ".builtin.str.empty")
                empty_ptr = builder.gep(
                    empty_gv,
                    [ir.Constant(_I32, 0), ir.Constant(_I32, 0)],
                    inbounds=True,
                )
                empty = builder.call(
                    self.runtime["py_str_new"],
                    [empty_ptr, ir.Constant(_I64, 0)],
                    name="str.empty",
                )
                builder.ret(empty)

            builder.position_at_end(too_many_bb)
            self._emit_native_builtin_callable_type_error(
                builder,
                f"{name}() takes at most 1 argument",
                name,
                "arity",
            )

            builder.position_at_end(one_arg_bb)
            arg = builder.call(
                self.runtime["py_tuple_get"],
                [args_arg, ir.Constant(_I64, 0)],
                name=f"{name}.arg",
            )
            if name == "str":
                result = builder.call(
                    self.runtime["py_obj_str"],
                    [arg],
                    name="str.result",
                )
                builder.call(self.runtime["py_decref"], [arg])
                builder.ret(result)
            elif name == "float":
                value = builder.call(
                    self.runtime["py_float_value_of"],
                    [arg],
                    name="float.value",
                )
                builder.call(self.runtime["py_decref"], [arg])
                result = builder.call(
                    self.runtime["py_float_from_f64"],
                    [value],
                    name="float.result",
                )
                builder.ret(result)
            else:
                # ``int`` as a first-class callable: the one runtime owner
                # converts (exact floats, __int__/__index__, CPython's
                # TypeError).  This adapter had its own fptosi, which
                # saturated int(1e20), and a message without the type name.
                result = builder.call(
                    self.runtime["py_obj_as_int_object"],
                    [arg, ir.Constant(_I32, 10)],
                    name="int.result",
                )
                builder.call(self.runtime["py_decref"], [arg])
                builder.ret(result)

        fn_obj = self.builder.call(
            self.runtime["py_builtin_function_value"],
            [adapter, self._attr_name_ptr(name)],
            name=self._fresh(f"{name}.builtin.func"),
        )
        self._note_owned_object_value(fn_obj)
        return fn_obj

    def _static_runtime_type_name(self, ty: Type) -> Optional[str]:
        if isinstance(ty, StrType):
            return "str"
        if isinstance(ty, BytesType):
            return "bytes"
        if isinstance(ty, ByteArrayType):
            return "bytearray"
        if isinstance(ty, MemoryViewType):
            return "memoryview"
        if isinstance(ty, BoolType):
            return "bool"
        if isinstance(ty, IntType):
            return "int"
        if isinstance(ty, FloatType):
            return "float"
        if isinstance(ty, ComplexType):
            return "complex"
        if isinstance(ty, ListType):
            return "list"
        if isinstance(ty, DictType):
            return "dict"
        if isinstance(ty, TupleType):
            return "tuple"
        if isinstance(ty, NoneType):
            return "NoneType"
        if isinstance(ty, ClassType):
            # Class types (including exceptions) are subclassable; their dynamic type at
            # runtime can differ from the static type. We return None so that
            # type(obj).__name__ resolves dynamically via py_obj_type_name.
            return None
        return None

    def _annotation_runtime_name(self, ann: object) -> str:
        if isinstance(ann, IntType):
            return "int"
        if isinstance(ann, FloatType):
            return "float"
        if isinstance(ann, BoolType):
            return "bool"
        if isinstance(ann, StrType):
            return "str"
        if isinstance(ann, NoneType):
            return "None"
        if isinstance(ann, ListType):
            return "list"
        if isinstance(ann, DictType):
            return "dict"
        if isinstance(ann, TupleType):
            return "tuple"
        if isinstance(ann, ClassType):
            return ann.name
        if isinstance(ann, DynType):
            return "dyn"
        if isinstance(ann, Name):
            return ann.ident
        return type(ann).__name__

    def _emit_owned_module_name_lookup(self, expr):
        """Read a live module binding into a NEW owner, preserving NameError."""
        sink = self._slot_call_result_sink(expr)
        output = sink
        if output is None:
            output = self._new_slot_call_root("name.dynamic")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = () if sink is not None else (output,)
        self._try_err_block = self._slot_call_cleanup_block(roots, target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            module_name = self.ast_module.name or "__main__"
            module_name_ptr = self._ptr_to_cstr(
                self._cstr_global(
                    module_name,
                    self._fresh(".name.module"),
                )
            )
            dynamic_value = self.builder.call(
                self.runtime["py_module_attr_get"],
                [
                    module_name_ptr,
                    self._pooled_cstr_ptr(expr.ident, ".name.dynamic.attr"),
                ],
                name=self._fresh(f"name.dynamic.{expr.ident}"),
            )
            self._publish_slot_call_owned(output, dynamic_value, label="module name lookup")
            dynamic_value = self.builder.load(output, name=self._fresh("name.dynamic.current"))
            missing = self.builder.icmp_signed(
                "==",
                dynamic_value,
                ir.Constant(_CSTR, None),
                name=self._fresh(f"name.dynamic.{expr.ident}.missing"),
            )
            err_bb = self.current_function.append_basic_block(
                name=self._fresh(f"name.dynamic.{expr.ident}.err")
            )
            ok_bb = self.current_function.append_basic_block(
                name=self._fresh(f"name.dynamic.{expr.ident}.ok")
            )
            self.builder.cbranch(missing, err_bb, ok_bb)

            self.builder.position_at_end(err_bb)
            msg = self._pooled_cstr_ptr(
                "name '" + expr.ident + "' is not defined",
                ".name_error",
            )
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 10), msg],
                name=self._fresh("name_error"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            frame_exc = self.builder.call(
                self.runtime["py_current_exception"],
                [],
                name=self._fresh("name.frame.exc"),
            )
            self._emit_exception_frame(frame_exc, getattr(expr, "span", None))
            err_target = self._current_try_err_block()
            if err_target is None:
                err_target = self._ensure_fn_err_exit()
            self.builder.branch(err_target)

            self.builder.position_at_end(ok_bb)
            if sink is not None:
                return self.builder.load(output, name=self._fresh("name.dynamic.current"))
            return self._take_slot_call_root(output)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_ellipsis_literal(self, expr: Name) -> ir.Value:
        # py_substrate owns this exact physical singleton pointer slot. A
        # slot-call consumer copies from that authoritative source directly.
        gv = declare_runtime_global(self.module, "py_Ellipsis")
        sink = self._slot_call_result_sink(expr)
        if sink is not None:
            self._slot_call_copy_source(sink, gv, span=expr.span)
            self._slot_call_note_published(sink)
            return self.builder.load(sink, name=self._fresh("ellipsis.current"))
        return self.builder.load(gv, name=self._fresh("ellipsis"))

    def _emit_name(self, expr: Name) -> ir.Value:
        # Both parsers use this impossible identifier for literal syntax.
        # It cannot be shadowed by local, global or class namespace bindings.
        if expr.ident == "...":
            return self._emit_ellipsis_literal(expr)
        class_value = self._emit_class_namespace_name_root(expr, "class.name.value")
        if class_value is not None:
            return self._take_slot_call_root(class_value)
        check_local_bound(self, expr)
        if live_import_name_slot(self, expr.ident) is not None:
            return self._emit_owned_module_name_lookup(expr)
        slot = self.env.get(expr.ident)
        if slot is None:
            # Method-body ``__class__`` is a compiler-created cell in
            # CPython. A module-scope class keeps it in its module cell;
            # function-local class methods capture theirs (a local binding).
            if expr.ident == "__class__":
                current_class = getattr(self, "current_class", None)
                if current_class is not None:
                    value, _owned = self.class_lowering.emit_defining_class(
                        current_class, expr.span,
                    )
                    return value
            # Module-level dunder that pcc can resolve at compile time.
            # The entry module matches CPython's ``python myscript.py``
            # behavior; sibling modules in a multi-file compile see their
            # own module name so ``if __name__ == "__main__":`` blocks
            # stay dead in them (they are only entry-skipped via
            # _skip_program_main, not omitted from codegen).
            if expr.ident == "__name__":
                if self._skip_program_main and self.ast_module.name:
                    return self._emit_str_literal(self.ast_module.name)
                return self._emit_str_literal("__main__")
            if expr.ident == "__file__":
                # Source spans retain the parser input path. Use the same
                # value published on the compiled module object so code inside
                # a module and ``module.__file__`` agree.
                source_filename = getattr(self, "_module_source_path", "") or ""
                if source_filename:
                    source_filename = os.path.abspath(source_filename)
                else:
                    for stmt in self.ast_module.body:
                        span = getattr(stmt, "span", None)
                        filename = getattr(span, "file", "")
                        if filename and not filename.startswith("<"):
                            source_filename = os.path.abspath(filename)
                            break
                if source_filename == "":
                    source_filename = (
                        (self.ast_module.name or "pcc_py_module") + ".py"
                    )
                return self._emit_str_literal(source_filename)
            if expr.ident == "__package__":
                if not self._skip_program_main:
                    return self._emit_str_literal("")
                module_name = self.ast_module.name or ""
                source_filename = getattr(self, "_module_source_path", "") or ""
                if source_filename == "":
                    for stmt in self.ast_module.body:
                        span = getattr(stmt, "span", None)
                        filename = getattr(span, "file", "")
                        if filename and not filename.startswith("<"):
                            source_filename = filename
                            break
                if os.path.basename(source_filename) == "__init__.py":
                    return self._emit_str_literal(module_name)
                if "." in module_name:
                    return self._emit_str_literal(module_name.rsplit(".", 1)[0])
                return self._emit_str_literal("")
            if expr.ident == "__doc__":
                module_global = self._module_globals.get("__doc__")
                if module_global is not None:
                    return self.builder.load(
                        module_global[0],
                        name=self._fresh("__doc__"),
                    )
                imported = getattr(
                    self,
                    "_native_extension_module_env",
                    {},
                ).get("__doc__")
                if imported is not None:
                    return self.builder.load(
                        imported,
                        name=self._fresh("pcc.ext.__doc__"),
                    )
                cpy_imported = getattr(self, "_cpy_module_env", {}).get("__doc__")
                if cpy_imported is not None:
                    value = self.builder.load(
                        cpy_imported,
                        name=self._fresh("cpy.__doc__"),
                    )
                    if not hasattr(self, "_cpy_values"):
                        self._cpy_values = set()
                    self._cpy_values.add(value)
                    return value
                if self.ast_module.docstring is None:
                    return self._emit_none_literal()
                return self._emit_str_literal(self.ast_module.docstring)
            if expr.ident == "NotImplemented":
                gv = declare_runtime_global(self.module, "py_NotImplemented")
                sink = self._slot_call_result_sink(expr)
                if sink is not None:
                    # py_substrate defines this exact global as the immortal
                    # singleton's physical pointer slot. Shadowed Names have
                    # already taken the normal local/global source path.
                    self._slot_call_copy_source(sink, gv, span=expr.span)
                    self._slot_call_note_published(sink)
                    return self.builder.load(sink, name=self._fresh("notimplemented.current"))
                return self.builder.load(gv, name=self._fresh("notimplemented"))
            if expr.ident == "super":
                # ``super`` is also a first-class built-in type object.  The
                # call-lowering path handles actual ``super(...)`` semantics;
                # value-position uses (for example copyreg's dispatch table)
                # need a stable, hashable native identity of their own.
                return self.builder.call(
                    self.runtime["py_builtin_type_for_tag"],
                    [ir.Constant(_I64, -3)],
                    name=self._fresh("super.type.value"),
                )
            if expr.ident in ("True", "False"):
                return ir.Constant(_I1, 1 if expr.ident == "True" else 0)
            if expr.ident in _BUILTIN_EXC_TAG:
                # Exception classes are values too: packages capture them in
                # defaults (``ValueError=ValueError``), containers, and
                # aliases.  Reuse the runtime's cached native class object so
                # these value-position uses stay no-libpython and preserve
                # identity with exception matching/constructors.
                return self._emit_owned_builtin_exception_class(_BUILTIN_EXC_TAG[expr.ident], expr)
            # Built-in type names at value position (``isinstance(x,
            # int)`` already folds compile-time; this covers the
            # residual ``obj_type = int`` / ``self.ty = str`` uses).
            # pcc-native callable values must stay native under
            # --python-libpython=off; real type-object identity/equality
            # cases are handled before name lowering by type-tag compare
            # fast paths.
            if expr.ident in _CPY_BUILTIN_TYPE_NAMES:
                native_callable = self._emit_native_builtin_callable_value(expr.ident, expr)
                if native_callable is not None:
                    return native_callable
                return self._load_cpython_builtin(expr.ident)
            if self._name_returns_native_builtin_callable_value(expr.ident):
                native_callable = self._emit_native_builtin_callable_value(expr.ident, expr)
                if native_callable is not None:
                    return native_callable
            builtin_value = self._native_builtin_value_for_name(expr.ident)
            if builtin_value == "tempfile.TemporaryDirectory":
                return self._emit_owned_namespace_runtime_value("py_tempdir_type", (), expr)
            if builtin_value in ("os.fdopen", "os.open", "os.close", "os.mkdir", "os.pwrite", "os.ftruncate"):
                provider = {"os.fdopen": "py_file_fdopen_function", "os.open": "py_os_open_function",
                            "os.close": "py_os_close_function", "os.mkdir": "py_os_mkdir_function",
                            "os.pwrite": "py_os_pwrite_function", "os.ftruncate": "py_os_ftruncate_function"}[builtin_value]
                return self._emit_owned_namespace_runtime_value(provider, (), expr)
            if builtin_value is not None and builtin_value.startswith("os.O_"):
                flag = native_os_descriptor_constant(builtin_value[3:], self._target_sys_platform_text())
                if flag is not None:
                    return self._emit_native_module_constant({"value_kind": "int", "value": flag})
            if builtin_value == "tempfile.mkdtemp":
                return self._emit_owned_namespace_runtime_value("py_tempfile_mkdtemp_function", (), expr)
            if builtin_value == "tempfile.NamedTemporaryFile":
                return self._emit_owned_namespace_runtime_value("py_namedtempfile_function", (), expr)
            if builtin_value == "os.name":
                return self._emit_str_literal("nt" if self._target_sys_platform_text() == "win32" else "posix")
            if builtin_value == "os.sep":
                return self._emit_str_literal("\\" if self._target_sys_platform_text() == "win32" else "/")
            if builtin_value == "os.linesep":
                return self._emit_str_literal("\r\n" if self._target_sys_platform_text() == "win32" else "\n")
            if builtin_value == "os.altsep":
                return self._emit_str_literal("/") if self._target_sys_platform_text() == "win32" else self._emit_none_literal()
            if builtin_value == "os.pathsep":
                return self._emit_str_literal(";" if self._target_sys_platform_text() == "win32" else ":")
            if builtin_value in ("sys.prefix", "sys.base_prefix"):
                return self._emit_owned_namespace_runtime_value(
                    "py_sys_prefix_str",
                    (ir.Constant(_I64, 1 if builtin_value == "sys.base_prefix" else 0),),
                    expr,
                )
            if builtin_value == "pcc.optional_import_missing.None":
                return self._emit_none_literal()
            # Module-level constant? Emit a load of the global.
            module_globals = self._module_globals
            if expr.ident in module_globals:
                gv, _declared_ty = module_globals[expr.ident]
                # Names that can be unbound have a separate initialized bit;
                # ordinary globals keep a plain load with no added branch.
                if self._module_global_needs_bound_check(expr.ident):
                    self._emit_module_global_bound_check(expr.ident, expr)
                if self._is_valueclass_payload_type(_declared_ty) and isinstance(gv.value_type, ir.LiteralStructType):
                    return self._load_valueclass_payload(gv, _declared_ty, module_source=True)
                val = self.builder.load(
                    gv,
                    name=self._fresh(expr.ident),
                )
                if self._cpy_module_flags.get(expr.ident, False):
                    if not hasattr(self, "_cpy_values"):
                        self._cpy_values = set()
                    self._cpy_values.add(val)
                return val
            # User class reference at value position — load the class
            # global so ``ClassName.ATTR`` and similar look-ups work.
            if (
                hasattr(self, "class_lowering")
                and expr.ident in self.class_lowering.classes
            ):
                info = self.class_lowering.classes[expr.ident]
                return self.builder.load(
                    info.global_var,
                    name=self._fresh(f"cls.{expr.ident}"),
                )
            native_alias_module = getattr(
                self,
                "_native_module_aliases",
                {},
            ).get(expr.ident)
            native_ext_gv = getattr(self, "_native_extension_module_env", {}).get(
                expr.ident
            )
            if native_alias_module is not None:
                # Compiled sibling imports keep both a static alias (for
                # direct ``mod.attr`` lowering) and the real live module
                # object.  In value position Python passes the object, not
                # the historical module-name string placeholder.
                if (native_ext_gv is not None
                        or native_alias_module in getattr(self, "_sibling_module_inits", ())):
                    return self._emit_owned_module_name_lookup(expr)
                return self._emit_native_module_placeholder(native_alias_module)
            native_constant = getattr(
                self,
                "_native_module_constant_bindings",
                {},
            ).get(expr.ident)
            if native_constant is not None:
                return self._emit_native_module_constant(native_constant)
            if native_ext_gv is not None:
                return self._emit_owned_module_name_lookup(expr)
            # Fall back to the module-wide CPython import registry for
            # ``from os import sep`` / ``import sys`` style bindings.
            cpy_gv = getattr(self, "_cpy_module_env", {}).get(expr.ident)
            if cpy_gv is not None:
                val = self.builder.load(cpy_gv, name=self._fresh(f"cpy.{expr.ident}"))
                if not hasattr(self, "_cpy_values"):
                    self._cpy_values = set()
                self._cpy_values.add(val)
                return val
            builtin_module = self._native_builtin_module_for_name(expr.ident)
            if builtin_module is not None:
                return self._emit_cpython_module_value(builtin_module)
            star_val = self._load_from_native_extension_star_imports(expr.ident)
            if star_val is not None:
                return star_val
            star_val = self._load_from_cpy_star_imports(expr.ident)
            if star_val is not None:
                return star_val
            # User FuncDef at value position: wrap the pcc function
            # pointer as a CPython PyCFunction so it can be passed to
            # ``re.sub(pat, <repl>, text)`` / ``am.register(KEY, <fn>)``
            # / ``{c_ast.FileAST: _children_FileAST}`` / any other
            # CPython API that consumes a callable. Covers 1 / 2 / 3
            # arg DynType-in / DynType-out. Higher arity still falls
            # through.
            resolved_name = expr.ident
            fn_ir = self.functions.get(expr.ident)
            if fn_ir is None:
                direct_hoist = f"__nested_{expr.ident}"
                if direct_hoist in self.functions:
                    resolved_name = direct_hoist
                    fn_ir = self.functions[direct_hoist]
                else:
                    matches = [
                        name
                        for name in self.functions
                        if _is_nested_hoist_collision_name(name, direct_hoist)
                    ]
                    if len(matches) == 1:
                        resolved_name = matches[0]
                        fn_ir = self.functions[resolved_name]
            # Adapter-wrap path: the ident may originally have been
            # a nested def flagged for captures-via-globals wrap.
            # ``rename_map`` at hoist time remapped the original name
            # to the hoisted one already, but the metadata dict is
            # keyed on the original name. Try both.
            adapter_entry = None
            for candidate in (expr.ident, resolved_name):
                adapter_entry = getattr(
                    self,
                    "_hoist_wrap_caps",
                    {},
                ).get(candidate)
                if adapter_entry is not None:
                    break
            if fn_ir is None and adapter_entry is not None:
                hoisted_name = adapter_entry.get("hoisted_name")
                if hoisted_name:
                    fn_ir = self.functions.get(hoisted_name)
                    resolved_name = hoisted_name
            if (
                adapter_entry is None
                and fn_ir is not None
                and resolved_name != expr.ident
            ):
                free_names = getattr(
                    self,
                    "_hoisted_capture_params",
                    {},
                ).get(resolved_name, ())
                if free_names:
                    fnty = getattr(fn_ir, "function_type", None)
                    total_arity = len(getattr(fnty, "args", ()))
                    adapter_entry = {
                        "original_arity": max(total_arity - len(free_names), 0),
                        "free_names": tuple(free_names),
                        "hoisted_name": resolved_name,
                        "original_name": expr.ident,
                    }
            if fn_ir is not None:
                native_free_names = getattr(
                    self,
                    "_hoisted_capture_params",
                    {},
                ).get(resolved_name)
                if native_free_names is not None or getattr(
                    self,
                    "_prefer_native_callable_values",
                    False,
                ):
                    return self._emit_native_func_value(
                        expr.ident,
                        resolved_name,
                        fn_ir,
                        tuple(native_free_names or ()),
                    )
                fnty = getattr(fn_ir, "function_type", None)
                all_ptr_args = fnty is not None and all(
                    isinstance(a, ir.PointerType) for a in fnty.args
                )
                ret_ok = fnty is not None and isinstance(
                    fnty.return_type, ir.PointerType
                )
                ret_void = fnty is not None and isinstance(
                    fnty.return_type, ir.VoidType
                )
                ret_int_width = (
                    fnty.return_type.width
                    if fnty is not None
                    and isinstance(
                        fnty.return_type,
                        ir.IntType,
                    )
                    else 0
                )
                wrap_helper = None
                arity = None
                if all_ptr_args and (ret_ok or ret_void or ret_int_width in (1, 64)):
                    arity = len(fnty.args)
                    # Captures-adapter has original arity.
                    if adapter_entry is not None and adapter_entry.get("hoisted_name"):
                        arity = adapter_entry["original_arity"]
                    wrap_helper = {
                        0: "py_cpy_wrap_pcc_0arg",
                        1: "py_cpy_wrap_pcc_1arg",
                        2: "py_cpy_wrap_pcc_2arg",
                        3: "py_cpy_wrap_pcc_3arg",
                        4: "py_cpy_wrap_pcc_4arg",
                        5: "py_cpy_wrap_pcc_5arg",
                        6: "py_cpy_wrap_pcc_6arg",
                        7: "py_cpy_wrap_pcc_7arg",
                        8: "py_cpy_wrap_pcc_8arg",
                        9: "py_cpy_wrap_pcc_9arg",
                    }.get(arity)
                if wrap_helper is not None:
                    target_fn = fn_ir
                    if adapter_entry is not None and adapter_entry.get("free_names"):
                        # Hoisted-captures adapter. ``_emit_hoist_adapter``
                        # already boxes non-ptr returns internally.
                        target_fn = self._emit_hoist_adapter(
                            expr.ident,
                            fn_ir,
                            adapter_entry,
                        )
                    elif not ret_ok:
                        # Standalone adapter for a value-position ref to
                        # a pcc FuncDef whose return is void / bool / int.
                        # Box the result via the appropriate py_* helper.
                        adapter_name = f"{fn_ir.name}_v2pyobj_{arity}"
                        existing_adapter = self.module.globals.get(adapter_name)
                        if isinstance(existing_adapter, ir.Function):
                            target_fn = existing_adapter
                        else:
                            adapter_fnty = ir.FunctionType(
                                _CSTR,
                                [_CSTR] * arity,
                            )
                            target_fn = ir.Function(
                                self.module,
                                adapter_fnty,
                                name=adapter_name,
                            )
                            target_fn.linkage = "internal"
                            ab = target_fn.append_basic_block("entry")
                            ab_b = ir.IRBuilder(ab)
                            if ret_void:
                                ab_b.call(fn_ir, list(target_fn.args))
                                py_none_gv = declare_runtime_global(
                                    self.module,
                                    "py_None",
                                )
                                ab_b.ret(ab_b.load(py_none_gv, name="none"))
                            elif ret_int_width == 1:
                                raw = ab_b.call(
                                    fn_ir,
                                    list(target_fn.args),
                                    name="raw",
                                )
                                bit = ab_b.zext(raw, _I32, name="b2i32")
                                boxed = ab_b.call(
                                    self.runtime["py_bool_from_bit"],
                                    [bit],
                                    name="boxed",
                                )
                                ab_b.ret(boxed)
                            else:
                                # ret_int_width == 64
                                raw = ab_b.call(
                                    fn_ir,
                                    list(target_fn.args),
                                    name="raw",
                                )
                                boxed = ab_b.call(
                                    self.runtime["py_int_from_i64"],
                                    [raw],
                                    name="boxed",
                                )
                                ab_b.ret(boxed)
                    fn_ptr = self.builder.bitcast(
                        target_fn,
                        _CSTR,
                        name=self._fresh(f"{expr.ident}.fnptr"),
                    )
                    result = self.builder.call(
                        self.runtime[wrap_helper],
                        [fn_ptr],
                        name=self._fresh(f"cpy.{expr.ident}"),
                    )
                    return self._mark_owned_cpy_value(result)
            # Ordinary lexical/import/class/function bindings above outrank
            # the builtin. Literal ``...`` took the unshadowable entry path.
            if expr.ident == "Ellipsis":
                return self._emit_ellipsis_literal(expr)
            # ``globals()[dynamic_name] = value`` writes the shared module
            # namespace even when no statically declared LLVM global exists.
            # Python's LOAD_GLOBAL observes those writes before consulting
            # builtins / raising NameError, so make the dynamic namespace the
            # final lookup before the existing missing-name error.
            if getattr(self, "_freestanding_module", False) or getattr(
                self, "_runtime_port_module", False
            ):
                # Runtime modules have no module namespace, so this lookup
                # could only ever raise NameError when the line runs.  That
                # hid real bugs: a telemetry query calling an unimported
                # helper allocated a NameError on every call and returned 0.
                raise NameError(
                    "runtime module "
                    + (self.ast_module.name or "<module>")
                    + ": name '"
                    + expr.ident
                    + "' is not defined (import it or declare it with extern())"
                )
            return self._emit_owned_module_name_lookup(expr)
        alloca, ir_ty, declared_ty = slot
        module_binding = self._module_globals.get(expr.ident)
        if self._is_valueclass_payload_type(declared_ty) and isinstance(ir_ty, ir.LiteralStructType):
            module_source = module_binding is not None and module_binding[0] is alloca
            if module_source and self._module_global_needs_bound_check(expr.ident):
                self._emit_module_global_bound_check(expr.ident, expr)
            return self._load_valueclass_payload(alloca, declared_ty, module_source=module_source)
        if (
            module_binding is not None
            and module_binding[0] is alloca
            and self._module_global_needs_bound_check(expr.ident)
        ):
            # Module-body bindings also live in env. Their slot can remain
            # empty after a zero-iteration loop or be cleared by del; a bare
            # expression read needs the same check as a call operand.
            self._emit_module_global_bound_check(expr.ident, expr)
        if (
            expr.ident in getattr(self, "_gc_rooted_local_names", set())
            and isinstance(ir_ty, ir.PointerType)
            and self._ir_type_matches(ir_ty, _CSTR)
        ):
            load_name = "pcc_gc_load_ptr"
            if expr.ident in getattr(
                self, "_current_param_names", set()
            ) or expr.ident in getattr(self, "_borrowed_gc_rooted_local_names", set()):
                load_name = "pcc_gc_load_borrowed_ptr"
            val = self.builder.call(
                self.runtime[load_name],
                [
                    ir.Constant(_CSTR, None),
                    self._as_gc_ptr(
                        alloca,
                        name=self._fresh(expr.ident + ".gc.slot"),
                    ),
                ],
                name=self._fresh(expr.ident),
            )
        else:
            val = self.builder.load(alloca, name=self._fresh(expr.ident))
        # Re-tag as a CPython value when the binding was recorded as
        # one. Without this, downstream coercions see a bare DynType
        # and route through the pcc (non-CPython) unbox path.
        if getattr(self, "_cpy_env_flags", {}).get(expr.ident, False):
            # J2': inside a generator, cpy loop targets live in their
            # slot as CpyHandle boxes (frame-safe). Unbox here — every
            # downstream consumer keeps receiving the raw cpy pointer.
            gen_stack = getattr(self, "_generator_ctx_stack", ())
            if len(gen_stack) > 0 and expr.ident in gen_stack[-1].get(
                "cpy_boxed_names", ()
            ):
                val = self.builder.call(
                    self.runtime["py_cpy_handle_get"],
                    [val],
                    name=self._fresh(expr.ident + ".cpy.unbox"),
                )
            if not hasattr(self, "_cpy_values"):
                self._cpy_values = set()
            self._cpy_values.add(val)
        return val
