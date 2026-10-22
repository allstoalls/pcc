"""Native module alias and import-from helper lowering."""

from __future__ import annotations

import os
from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.export_meta import decode_type
from pcc.frontends.python.import_alternatives import import_fallbacks, import_statement_key
from pcc.frontends.python.pipeline_import_policy import native_builtin_dispatch_owns
from pcc.frontends.python.py_ast import Attr, Call, ClassDef, DynType, Expr, For, FuncDef, If, Import, ImportFrom, IntType, IntLit, Lambda, Name, NoneLit, NoneType, SourceSpan, StrLit, Subscript, Try, While, With
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.cpy_import_state import (
    live_import_expr_binding,
    live_import_global_slot,
)
from pcc.frontends.python.codegen.native_os import native_os_descriptor_constant
from pcc.frontends.python.codegen.generator_lowering import emit_generator_may_park_call
from pcc.frontends.python.codegen.layer1_support import (
    _import_from_binds_nothing_at_runtime,
)
from pcc.frontends.python.codegen.hoist_analysis import (
    name_in,
)
from pcc.frontends.python.codegen.hoist_boxing import (
    function_import_conflict_names,
)
from pcc.frontends.python.codegen.vthread_effect_analysis import (
    vthread_proven_suspension_module_alias,
    vthread_proven_value_alias,
)

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()
_DOUBLE = ir.DoubleType()
_VOID = ir.VoidType()

_MATH_INF = 1e308 * 10.0
_MATH_NAN = (1e308 * 10.0) * 0.0


def _native_builtin_type_value_names():
    # The builtin type names that are lowered to native ``builtins.<name>``
    # value aliases rather than a runtime ``import builtins``.  Returned from a
    # module-level FUNCTION, not a module-level tuple/frozenset constant: a
    # self-hosted L1CodeGen mixin method cannot reliably read a module-global
    # data constant across the compiled-module boundary (it projects empty and
    # every membership test goes false, routing strict builds to a bogus
    # ``import builtins``), but a plain function call that reconstructs the
    # literal each time is reliable.  Single source of truth for the three
    # membership sites below.
    return (
        "staticmethod",
        "classmethod",
        "property",
        "bool",
        "bytes",
        "bytearray",
        "complex",
        "dict",
        "float",
        "int",
        "list",
        "memoryview",
        "object",
        "range",
        "set",
        "str",
        "tuple",
        "type",
    )


def _restore_alias_entry(mapping, key: str, present: bool, value) -> None:
    if present:
        mapping[key] = value
    else:
        mapping.pop(key, None)


def _native_star_export_items(exports: dict):
    all_info = exports.get("__all__")
    all_names = None
    if isinstance(all_info, dict):
        all_names = all_info.get("export_names")
    if all_names is not None:
        items = []
        for export_name in all_names:
            info = exports.get(export_name)
            if info is not None:
                items.append((export_name, info))
        return items
    items = []
    for export_name, info in exports.items():
        if export_name.startswith("_"):
            continue
        items.append((export_name, info))
    return items


def _string_constant_value(name: str) -> Optional[str]:
    if name == "ascii_lowercase":
        return "abcdefghijklmnopqrstuvwxyz"
    if name == "ascii_uppercase":
        return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if name == "ascii_letters":
        return "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if name == "digits":
        return "0123456789"
    if name == "hexdigits":
        return "0123456789abcdefABCDEF"
    if name == "octdigits":
        return "01234567"
    if name == "punctuation":
        return "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    if name == "whitespace":
        return " \t\n\r\x0b\x0c"
    if name == "printable":
        return (
            "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ \t\n\r\x0b\x0c"
        )
    return None


_VIRTUAL_THREAD_CONSTANTS = {
    "OUTCOME_PENDING": 0,
    "OUTCOME_RETURNED": 1,
    "OUTCOME_RAISED": 2,
    "OUTCOME_CANCELLED": 3,
    "RECV_VALUE": 1,
    "RECV_SENDER_CLOSED": 2,
    "RECV_RECEIVER_CLOSED": 3,
    "SELECT_LEFT": 0,
    "SELECT_RIGHT": 1,
}


def _is_virtual_thread_export(name: str) -> bool:
    return (
        name in _VIRTUAL_THREAD_CONSTANTS
        or name == "spawn"
        or name == "continuation"
        or name == "completed"
        or name == "continuation_factory"
        or name == "transparent_call"
        or name == "forward_call"
        or name == "call"
        or name == "join"
        or name == "cancel"
        or name == "mpsc"
        or name == "oneshot"
        or name == "sender_clone"
        or name == "send"
        or name == "recv"
        or name == "close_sender"
        or name == "close_receiver"
        or name == "select2"
        or name == "run"
        or name == "run_until_idle"
        or name == "carrier_pool_start"
        or name == "carrier_pool_stop"
        or name == "io_backend"
        or name == "current"
        or name == "yield_now"
        or name == "sleep_current"
        or name == "block_current_on_fd"
        or name == "readable"
        or name == "writable"
        or name == "tcp_listen"
        or name == "tcp_accept"
        or name == "tcp_connect"
        or name == "tcp_recv"
        or name == "tcp_send_all"
        or name == "tcp_close"
        or name == "result"
        or name == "exception"
        or name == "outcome"
        or name == "state"
        or name == "sleep"
        or name == "block_on_fd"
    )


_CODECS_BOM_CONSTANTS: dict[str, bytes] = {
    "BOM_UTF8": b"\xef\xbb\xbf",
    "BOM_UTF32_LE": b"\xff\xfe\x00\x00",
    "BOM_UTF32_BE": b"\x00\x00\xfe\xff",
    "BOM_LE": b"\xff\xfe",
    "BOM_BE": b"\xfe\xff",
}


# ``time`` functions with a native lowering (clock reads build NEW objects).
_NATIVE_TIME_CALLS = (
    "monotonic",
    "perf_counter",
    "time",
    "strftime",
    "sleep",
    "monotonic_ns",
    "perf_counter_ns",
    "time_ns",
)
# ``from time import ...`` names resolved natively (``sleep`` only as ``time.sleep``).
_NATIVE_TIME_IMPORTS = tuple(name for name in _NATIVE_TIME_CALLS if name != "sleep")

class NativeModuleAliasMixin:
    def _extern_user_function_return_ir_type(
        self,
        info: dict,
        *,
        box_int_abi: bool,
    ) -> ir.Type:
        # ``may_park`` is a source-level effect, not a semantic return type.
        # Its definition is lowered through the native generator ABI and thus
        # always returns the child continuation pointer, including source
        # functions annotated ``-> None``.
        if bool(info.get("may_park", False)):
            return _CSTR
        # The schema-carried ``returns_none`` bool survives the export round
        # trip even where type descriptors degrade to ("dyn",) under the
        # self-hosted compiler (encode_type's isinstance chain sees foreign
        # node identity, so ``-> None`` encodes as dyn; see
        # pipeline._export_returns_none and the class_gen method plans, which
        # already consume the bool). A dyn-shaped return here declares this
        # extern as returning a pointer against a ``ret void`` definition,
        # and the caller then roots/increfs whatever the callee left in x0
        # (pcc1-built pcc2 GC4 graph-lock deadlock).
        if "returns_none" in info and bool(info["returns_none"]):
            return _VOID
        ret_ty = decode_type(info["return_ty"])
        # Match by stable type name as well as identity: under pcc1 self-host
        # the decoded ``NoneType`` can come from a separately compiled copy of
        # the types module, so ``isinstance`` sees a foreign class identity for
        # a semantically identical None (same boundary that
        # user_function_decl_lowering._is_none_return documents). Identity-only
        # matching declared a ``-> None`` sibling as returning a pointer while
        # its definition emitted ``ret void``; the caller then rooted and
        # increfed whatever the callee left in x0.
        if isinstance(ret_ty, NoneType) or getattr(ret_ty, "name", "") == "None":
            return _VOID
        if ret_ty is None:
            ret_ty = DynType(name="dyn")
        return self._abi_ir_type(ret_ty, box_int_abi=box_int_abi)

    def _register_native_builtin_module_alias(
        self,
        local_name: str,
        module_name: str,
    ) -> None:
        self._native_builtin_module_aliases[local_name] = module_name

    def _clear_native_builtin_module_alias(self, local_name: str) -> None:
        aliases = self._native_builtin_module_aliases
        if aliases is not None:
            aliases.pop(local_name, None)

    def _register_native_builtin_value_alias(
        self,
        local_name: str,
        value_kind: str,
    ) -> None:
        self._native_builtin_value_aliases[local_name] = value_kind

    def _clear_native_builtin_value_alias(self, local_name: str) -> None:
        aliases = self._native_builtin_value_aliases
        if aliases is not None:
            aliases.pop(local_name, None)

    def _native_builtin_module_for_name(self, ident: str) -> Optional[str]:
        if ident in self.env:
            return None
        alias = self._native_builtin_module_aliases.get(ident)
        current_fd = getattr(self, "current_func_def", None)
        if (
            alias == "pcc.virtual_thread"
            and current_fd is not None
            and not vthread_proven_suspension_module_alias(
                self.ast_module,
                current_fd,
                ident,
                self._vthread_binding_cache,
            )
        ):
            return None
        if alias is not None:
            return alias
        if ident in self._module_globals:
            return None
        return None

    def _native_builtin_value_for_name(self, ident: str) -> Optional[str]:
        if ident in self.env:
            return None
        alias = self._native_builtin_value_aliases.get(ident)
        if alias is not None and alias.startswith("pcc.virtual_thread."):
            current_fd = getattr(self, "current_func_def", None)
            if current_fd is not None:
                export_name = alias.rsplit(".", 1)[1]
                if not vthread_proven_value_alias(
                    self.ast_module,
                    current_fd,
                    ident,
                    export_name,
                    self._vthread_binding_cache,
                ):
                    return None
        if alias is not None:
            return alias
        if ident in self._module_globals:
            return None
        if ident in _native_builtin_type_value_names():
            return "builtins." + ident
        return None

    def _native_builtin_value_kind_for_expr(self, expr: Expr) -> Optional[str]:
        if (isinstance(expr, Attr) and isinstance(expr.obj, Name)
                and self._native_builtin_module_for_name(expr.obj.ident) == "os"
                and expr.name in ("fdopen", "open", "close", "mkdir", "pwrite", "ftruncate")):
            return "os." + expr.name
        if isinstance(expr, Name):
            return self._native_builtin_value_for_name(expr.ident)
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "pcc"
            and expr.name
            in (
                "valueclass",
                "i64",
                "u64",
                "i64_buffer",
                "guarded_i64_dot",
                "guarded_loop_counter",
            )
        ):
            if expr.name in ("i64", "u64"):
                # The public marker is the host int callable as a value;
                # annotation lifting owns its explicit machine projection.
                return "builtins.int"
            return "pcc." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "weakref"
            and expr.name
            in (
                "ref",
                "proxy",
                "WeakValueDictionary",
                "WeakKeyDictionary",
            )
        ):
            return "weakref." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "copy"
            and expr.name in ("copy", "deepcopy")
        ):
            return "copy." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "functools"
            and expr.name in ("partial", "reduce", "update_wrapper")
        ):
            return "functools." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "contextvars"
            and expr.name == "ContextVar"
        ):
            return "contextvars.ContextVar"
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "tempfile"
            and expr.name in ("TemporaryDirectory", "mkdtemp", "NamedTemporaryFile")
        ):
            return "tempfile." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "pickle"
            and expr.name in ("dumps", "loads")
        ):
            return "pickle." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "traceback"
            and expr.name in ("format_exc", "print_exc")
        ):
            return "traceback." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident)
            == "pcc.virtual_thread"
            and _is_virtual_thread_export(expr.name)
        ):
            return "pcc.virtual_thread." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "threading"
            and expr.name
            in (
                "Thread",
                "Lock",
                "RLock",
                "Event",
                "Condition",
                "Semaphore",
                "BoundedSemaphore",
                "current_thread",
                "get_ident",
            )
        ):
            return "threading." + expr.name
        if (
            isinstance(expr, Attr)
            and expr.name == "path"
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "os"
        ):
            return "os.path"
        if (
            isinstance(expr, Attr)
            and expr.name == "urandom"
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "os"
        ):
            return "os.urandom"
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "os"
            and expr.name
            in (
                "_pcc_sha256_file_hex",
                "_pcc_sha256_file_hex_bounded",
            )
        ):
            return "os." + expr.name
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "time"
            and expr.name
            in _NATIVE_TIME_CALLS
        ):
            return "time." + expr.name
        if (
            isinstance(expr, Attr)
            and expr.name in ("exit", "getrecursionlimit", "setrecursionlimit")
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "sys"
        ):
            return "sys." + expr.name
        if (
            isinstance(expr, Attr)
            and expr.name == "dedent"
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "textwrap"
        ):
            return "textwrap.dedent"
        return None

    def _emit_native_builtin_value_call(
        self,
        builtin_value: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        expr: Call,
    ) -> Optional[ir.Value]:
        if builtin_value == "builtins.range":
            if kwargs:
                return None
            span = args[0].span if args else None
            return self._emit_range_value_call(
                Call(
                    span=span,
                    ty=DynType(name="dyn"),
                    func=Name(
                        span=span,
                        ty=DynType(name="dyn"),
                        ident="range",
                    ),
                    args=args,
                    kwargs=kwargs,
                )
            )
        if (
            builtin_value.startswith("time.")
            and builtin_value[len("time."):] in _NATIVE_TIME_CALLS
        ):
            return self._emit_native_time_call(
                builtin_value,
                args,
                kwargs,
                builtin_value + ".value",
                expr,
            )
        if builtin_value == "os.urandom":
            return self._emit_native_os_urandom_call(args, kwargs)
        if builtin_value == "textwrap.dedent":
            return self._emit_native_textwrap_dedent_call(args, kwargs)
        if builtin_value in (
            "copy.copy",
            "copy.deepcopy",
            "pickle.dumps",
            "pickle.loads",
        ):
            return self._emit_native_copy_pickle_call(
                builtin_value,
                args,
                kwargs,
                expr,
            )
        if builtin_value == "functools.partial":
            return self._emit_native_functools_partial_call(args, kwargs)
        if builtin_value == "functools.reduce":
            return self._emit_native_functools_reduce_call(args, kwargs)
        if builtin_value == "contextvars.ContextVar":
            return self._emit_native_contextvar_new(args, kwargs)
        if builtin_value in ("traceback.format_exc", "traceback.print_exc"):
            return self._emit_native_traceback_exc_call(builtin_value, args, kwargs)
        if builtin_value == "pcc.valueclass":
            if kwargs or len(args) != 1:
                return None
            return marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                self._emit_expr(args[0]),
                args[0].ty,
            )
        return None

    def _emit_native_contextvar_new(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if len(args) != 1 or not isinstance(args[0], StrLit):
            return None
        default_obj = ir.Constant(_CSTR, None)
        if kwargs:
            if len(kwargs) != 1 or kwargs[0][0] != "default":
                return None
            default_obj = self._emit_as_object(kwargs[0][1])
        name_ptr = self._pooled_cstr_ptr(args[0].value, ".contextvar.name")
        result = self.builder.call(
            self.runtime["PyContextVar_New"],
            [name_ptr, default_obj],
            name=self._fresh("contextvars.ContextVar"),
        )
        self._emit_post_call_err_check(args[0].span)
        return result

    def _emit_native_builtin_module_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        if not isinstance(attr.obj, Name):
            return None
        module_name = self._native_builtin_module_for_name(attr.obj.ident)
        if (
            module_name is None
            and attr.name in ("partial", "reduce")
            and attr.obj.ident == "functools"
            and attr.obj.ident in getattr(self, "_cpy_module_env", {})
        ):
            module_name = "functools"
        if module_name == "time" and attr.name in _NATIVE_TIME_CALLS:
            return self._emit_native_time_call(
                "time." + attr.name,
                expr.args,
                expr.kwargs,
                "time." + attr.name,
                expr,
            )
        if module_name == "os" and attr.name == "urandom":
            return self._emit_native_os_urandom_call(expr.args, expr.kwargs)
        if module_name == "copy" and attr.name in ("copy", "deepcopy"):
            return self._emit_native_copy_pickle_call(
                "copy." + attr.name,
                expr.args,
                expr.kwargs,
                expr,
            )
        if module_name == "pickle" and attr.name in ("dumps", "loads"):
            return self._emit_native_copy_pickle_call(
                "pickle." + attr.name,
                expr.args,
                expr.kwargs,
                expr,
            )
        if module_name == "functools" and attr.name == "partial":
            return self._emit_native_functools_partial_call(expr.args, expr.kwargs)
        if module_name == "functools" and attr.name == "reduce":
            return self._emit_native_functools_reduce_call(expr.args, expr.kwargs)
        if module_name == "functools" and attr.name == "update_wrapper":
            return self._emit_native_functools_update_wrapper_call(
                expr.args,
                expr.kwargs,
            )
        if module_name == "contextvars" and attr.name == "ContextVar":
            return self._emit_native_contextvar_new(expr.args, expr.kwargs)
        if module_name == "textwrap" and attr.name == "dedent":
            return self._emit_native_textwrap_dedent_call(expr.args, expr.kwargs)
        if module_name == "traceback" and attr.name in ("format_exc", "print_exc"):
            return self._emit_native_traceback_exc_call(
                "traceback." + attr.name,
                expr.args,
                expr.kwargs,
            )
        return None

    def _is_native_builtin_dynamic_module(self, module_name: str) -> bool:
        return module_name in (
            "math",
            "sys",
        )

    def _is_native_dynamic_module(self, module_name: str) -> bool:
        if self._is_native_builtin_dynamic_module(module_name):
            return True
        native_table = self._native_module_exports or {}
        return module_name in native_table

    def _native_builtin_module_has_attr(self, module_name: str, attr_name: str) -> bool:
        if module_name == "math":
            return attr_name in (
                "pi",
                "e",
                "tau",
                "inf",
                "nan",
                "floor",
                "copysign",
                "sqrt",
                "pow",
                "__dict__",
            )
        if module_name == "pcc":
            return attr_name in (
                "valueclass",
                "i64",
                "u64",
                "i64_buffer",
                "guarded_i64_dot",
                "guarded_loop_counter",
                "__dict__",
            )
        if module_name == "codecs":
            return attr_name in _CODECS_BOM_CONSTANTS
        if module_name == "sys":
            return attr_name in ("maxunicode", "maxsize", "modules")
        if module_name == "re":
            return attr_name in (
                "I",
                "IGNORECASE",
                "M",
                "MULTILINE",
                "S",
                "DOTALL",
                "X",
                "VERBOSE",
            )
        return False

    def _emit_native_builtin_module_dict(self, module_name: str) -> Optional[ir.Value]:
        if module_name != "math":
            return None
        d = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh("math.__dict__"),
        )
        entries = {
            "pi": 3.141592653589793,
            "e": 2.718281828459045,
            "tau": 6.283185307179586,
            "inf": _MATH_INF,
            "nan": _MATH_NAN,
        }
        for key, value in entries.items():
            self.builder.call(
                self.runtime["py_dict_set"],
                [
                    d,
                    self._emit_str_literal(key),
                    self._emit_native_module_constant(
                        {"value_kind": "float", "value": value},
                    ),
                ],
            )
        return d

    def _emit_native_builtin_module_attr(
        self,
        module_name: str,
        attr_name: str,
    ) -> Optional[ir.Value]:
        if module_name == "sys" and attr_name == "modules":
            return self._emit_owned_namespace_runtime_value("py_sys_modules", ())
        if module_name == "os":
            provider = {"fdopen": "py_file_fdopen_function", "open": "py_os_open_function",
                        "close": "py_os_close_function", "mkdir": "py_os_mkdir_function",
                        "pwrite": "py_os_pwrite_function", "ftruncate": "py_os_ftruncate_function"}.get(attr_name)
            if provider is not None:
                return self._emit_owned_namespace_runtime_value(provider, ())
            flag = native_os_descriptor_constant(attr_name, self._target_sys_platform_text())
            if flag is not None:
                if "os" in getattr(self, "_sibling_module_inits", ()):
                    value_slot = self._emit_slot_call_module_value("os", attr_name, None, "owned.module.constant")
                    return self._take_slot_call_root(value_slot)
                return self._emit_native_module_constant({"value_kind": "int", "value": flag})
        if module_name == "tempfile" and attr_name == "TemporaryDirectory":
            return self._emit_owned_namespace_runtime_value("py_tempdir_type", ())
        if module_name == "tempfile" and attr_name == "mkdtemp":
            return self._emit_owned_namespace_runtime_value("py_tempfile_mkdtemp_function", ())
        if module_name == "tempfile" and attr_name == "NamedTemporaryFile":
            return self._emit_owned_namespace_runtime_value("py_namedtempfile_function", ())
        if module_name == "math":
            constants = {
                "pi": 3.141592653589793,
                "e": 2.718281828459045,
                "tau": 6.283185307179586,
                "inf": _MATH_INF,
                "nan": _MATH_NAN,
            }
            if attr_name in constants:
                return self._emit_native_module_constant(
                    {"value_kind": "float", "value": constants[attr_name]},
                )
            if attr_name == "__dict__":
                return self._emit_native_builtin_module_dict(module_name)
        if module_name == "typing" and attr_name == "TYPE_CHECKING":
            return self._emit_native_module_constant(
                {"value_kind": "bool", "value": False},
            )
        if (
            module_name == "pcc.virtual_thread"
            and attr_name in _VIRTUAL_THREAD_CONSTANTS
        ):
            return self._emit_native_module_constant(
                {
                    "value_kind": "int",
                    "value": _VIRTUAL_THREAD_CONSTANTS[attr_name],
                },
            )
        if module_name == "codecs" and attr_name in _CODECS_BOM_CONSTANTS:
            return self._emit_native_module_constant(
                {"value_kind": "bytes", "value": _CODECS_BOM_CONSTANTS[attr_name]},
            )
        if module_name == "sys" and attr_name == "maxunicode":
            return self._emit_native_module_constant(
                {"value_kind": "int", "value": 0x10FFFF},
            )
        if module_name == "sys" and attr_name == "maxsize":
            return self._emit_native_module_constant(
                {"value_kind": "int", "value": ((0x7FFFFFFF << 32) | 0xFFFFFFFF)},
            )
        if module_name == "sys" and attr_name == "byteorder":
            # All currently supported pcc execution targets are little-endian;
            # this also mirrors pcc/stdlib/sys.py.
            return self._emit_native_module_constant(
                {"value_kind": "str", "value": "little"},
            )
        if module_name == "re":
            constants = {
                "I": 2,
                "IGNORECASE": 2,
                "M": 8,
                "MULTILINE": 8,
                "S": 16,
                "DOTALL": 16,
                "X": 64,
                "VERBOSE": 64,
            }
            if attr_name in constants:
                return self._emit_native_module_constant(
                    {"value_kind": "int", "value": constants[attr_name]},
                )
        return None

    def _emit_native_module_placeholder(self, module_name: str) -> ir.Value:
        module_dict = self._emit_native_builtin_module_dict(module_name)
        if module_dict is not None:
            return module_dict
        return self._emit_str_literal(module_name)

    def _emit_native_functools_partial_call(self, args, kwargs):
        if len(args) < 1:
            return None
        kwdict_unpack = self._split_starstar_kwargs_unpack(args)
        arg_exprs = args
        kwargs_expr = None
        if kwdict_unpack is not None:
            arg_exprs, kwargs_expr = kwdict_unpack
        if len(arg_exprs) < 1:
            return None
        old = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        try:
            fn = self._emit_as_object(arg_exprs[0])
        finally:
            self._prefer_native_callable_values = old
        bound = self._emit_dynamic_call_args_tuple(arg_exprs[1:])
        if kwargs or kwargs_expr is not None:
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                kwargs,
                kwargs_expr,
                arg_exprs[0].span,
            )
            result = self.builder.call(
                self.runtime["py_functools_partial_kw"],
                [fn, bound, kwargs_obj],
                name=self._fresh("functools.partial.kw"),
            )
            if kwargs:
                self._gc_release(kwargs_obj)
        else:
            result = self.builder.call(
                self.runtime["py_functools_partial"],
                [fn, bound],
                name=self._fresh("functools.partial"),
            )
        self._gc_release(bound)
        return result

    def _emit_native_functools_update_wrapper_call(self, args, kwargs):
        if kwargs or len(args) != 2:
            return None
        result = self.builder.call(
            self.runtime["py_functools_update_wrapper"],
            [self._emit_as_object(args[0]), self._emit_as_object(args[1])],
            name=self._fresh("functools.update_wrapper"),
        )
        self._emit_post_call_err_check(getattr(args[0], "span", None))
        return result

    def _emit_native_functools_reduce_call(self, args, kwargs):
        if kwargs or len(args) not in (2, 3):
            return None
        reducer = args[0]
        if (
            not isinstance(reducer, Lambda)
            or len(reducer.params) != 2
            or reducer.params[0].name == ""
            or reducer.params[1].name == ""
        ):
            return None

        src_expr = args[1]
        src_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            self._emit_expr(src_expr),
            src_expr.ty,
        )
        src_list = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("reduce.list"),
        )
        self._emit_list_append_via_iter(
            src_list,
            src_obj,
            getattr(src_expr, "span", None),
        )
        n_val = self.builder.call(
            self.runtime["py_list_len"],
            [src_list],
            name=self._fresh("reduce.len"),
        )

        acc_slot = self._alloca_in_entry(_CSTR, name="reduce.acc.addr")
        item_slot = self._alloca_in_entry(_CSTR, name="reduce.item.addr")
        idx_slot = self._alloca_in_entry(_I64, name="reduce.idx.addr")
        fn = self.current_function
        loop_cond = fn.append_basic_block(name=self._fresh("reduce.cond"))
        loop_body = fn.append_basic_block(name=self._fresh("reduce.body"))
        loop_step = fn.append_basic_block(name=self._fresh("reduce.step"))
        end_bb = fn.append_basic_block(name=self._fresh("reduce.end"))

        if len(args) == 3:
            init_obj = self._emit_expr_as_pcc_object(args[2])
            self.builder.store(init_obj, acc_slot)
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            self.builder.branch(loop_cond)
        else:
            is_empty = self.builder.icmp_signed(
                "==",
                n_val,
                ir.Constant(_I64, 0),
                name=self._fresh("reduce.empty"),
            )
            empty_bb = fn.append_basic_block(name=self._fresh("reduce.empty.bb"))
            seed_bb = fn.append_basic_block(name=self._fresh("reduce.seed"))
            self.builder.cbranch(is_empty, empty_bb, seed_bb)
            self.builder.position_at_end(empty_bb)
            self._emit_builtin_exception_and_branch(
                "TypeError",
                "reduce() of empty iterable with no initial value",
                getattr(src_expr, "span", None),
            )
            self.builder.position_at_end(seed_bb)
            first = self.builder.call(
                self.runtime["py_list_get"],
                [src_list, ir.Constant(_I64, 0)],
                name=self._fresh("reduce.first"),
            )
            self.builder.store(first, acc_slot)
            self.builder.store(ir.Constant(_I64, 1), idx_slot)
            self.builder.branch(loop_cond)

        self.builder.position_at_end(loop_cond)
        idx = self.builder.load(idx_slot, name=self._fresh("reduce.idx"))
        keep_going = self.builder.icmp_signed(
            "<",
            idx,
            n_val,
            name=self._fresh("reduce.keep"),
        )
        self.builder.cbranch(keep_going, loop_body, end_bb)

        self.builder.position_at_end(loop_body)
        item = self.builder.call(
            self.runtime["py_list_get"],
            [src_list, idx],
            name=self._fresh("reduce.item"),
        )
        self.builder.store(item, item_slot)
        p0 = reducer.params[0].name
        p1 = reducer.params[1].name
        old0 = self.env.get(p0)
        old1 = self.env.get(p1)
        self.env[p0] = (acc_slot, _CSTR, DynType(name="dyn"))
        self.env[p1] = (item_slot, _CSTR, DynType(name="dyn"))
        try:
            reduced = self._emit_expr_as_pcc_object(reducer.body)
        finally:
            if old0 is None:
                self.env.pop(p0, None)
            else:
                self.env[p0] = old0
            if old1 is None:
                self.env.pop(p1, None)
            else:
                self.env[p1] = old1
        self.builder.store(reduced, acc_slot)
        self.builder.branch(loop_step)

        self.builder.position_at_end(loop_step)
        next_idx = self.builder.add(
            idx,
            ir.Constant(_I64, 1),
            name=self._fresh("reduce.next"),
        )
        self.builder.store(next_idx, idx_slot)
        self.builder.branch(loop_cond)

        self.builder.position_at_end(end_bb)
        return self.builder.load(acc_slot, name=self._fresh("reduce.result"))

    def _emit_native_copy_pickle_call(
        self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        expr: Call,
    ) -> Optional[ir.Value]:
        if kwargs:
            return None
        if kind == "copy.copy" and len(args) == 1:
            # Preserve the original Call identity: its caller may already own
            # an output slot. The shared unary path leases the operand and
            # publishes the NEW result before any parking/error cleanup.
            return self._emit_owned_unary_runtime_call(expr, "py_copy_copy")
        if kind == "copy.deepcopy" and len(args) == 1:
            return self._emit_owned_unary_runtime_call(expr, "py_copy_deepcopy")
        if kind == "pickle.dumps" and 1 <= len(args) <= 2:
            # Owned operands and the NEW result published before cleanup, as
            # copy.copy: ``"".join(pickle.loads(pickle.dumps(it, proto)))``.
            protocol = args[1] if len(args) == 2 else NoneLit(span=expr.span, ty=NoneType(name="None"))
            return self._emit_owned_operand_runtime_call(
                expr, "py_pickle_dumps", 2, operand_exprs=(args[0], protocol),
            )
        if kind == "pickle.loads" and len(args) == 1:
            return self._emit_owned_unary_runtime_call(expr, "py_pickle_loads")
        return None

    def _emit_native_time_call(
        self,
        builtin_value: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        result_name: str,
        expr: Call,
    ) -> Optional[ir.Value]:
        if builtin_value == "time.sleep":
            if len(args) != 1 or kwargs:
                return None
            result = self.builder.call(
                self.runtime["py_time_sleep"],
                [self._emit_as_object(args[0])],
                name=self._fresh(result_name),
            )
            # A negative delay raises ValueError, so the error gate is load
            # bearing rather than defensive.
            self._emit_post_call_err_check(args[0].span)
            # `py_time_sleep` hands back the immortal None; it is borrowed, so
            # this must NOT be recorded as an owned value the way the three
            # clock reads below are.
            return result
        if builtin_value == "time.strftime":
            if len(args) != 1 or kwargs:
                return None
            return self._emit_owned_unary_runtime_call(expr, "py_time_strftime")
        if len(args) > 0 or len(kwargs) > 0:
            return None
        runtime_name = {
            "time.monotonic": "py_time_monotonic",
            "time.perf_counter": "py_time_perf_counter",
            "time.time": "py_time_time",
            "time.monotonic_ns": "py_time_monotonic_ns",
            "time.perf_counter_ns": "py_time_perf_counter_ns",
            "time.time_ns": "py_time_time_ns",
        }.get(builtin_value)
        if runtime_name is None:
            return None
        sink = self._slot_call_result_sink(expr)
        output = sink if sink is not None else self._new_slot_call_root(result_name + ".result")
        previous = self._current_try_err_block()
        saved_cpy = self._cpy_operand_cleanup_block
        target = previous if previous is not None else self._ensure_fn_err_exit()
        if sink is None:
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
        try:
            # Clock reads build NEW objects. Publish the actual producer's
            # result before any status, lease or operand cleanup can park.
            self._slot_call_runtime_call(runtime_name, (), result_slot=output, span=expr.span)
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh(result_name + ".current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_native_os_urandom_call(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if len(args) != 1 or len(kwargs) != 0:
            return None
        result = self.builder.call(
            self.runtime["py_os_urandom"],
            [self._emit_as_object(args[0])],
            name=self._fresh("os.urandom"),
        )
        self._emit_post_call_err_check(args[0].span)
        # `py_os_urandom` returns new bytes; see the clock note above.
        self._note_owned_object_value(result)
        return result

    def _emit_native_traceback_exc_call(
        self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        """traceback.format_exc() / traceback.print_exc().

        The exception being handled is tracked at codegen time in
        ``_active_handler_excs`` (its top is the innermost handler's
        retained exception; ``_rewrite_traceback_handler_bindings``
        guarantees handlers are retained in modules importing
        traceback). Outside a handler fall back to the TLS pending
        exception; the runtime helper formats NULL as
        "NoneType: None\\n" like CPython."""
        if args or kwargs:
            return None
        exc_val = self._active_handler_exception_for_current_function()
        if exc_val is None:
            exc_val = self.builder.call(
                self.runtime["py_current_exception"],
                [],
                name=self._fresh("traceback.cur_exc"),
            )
        if kind == "traceback.print_exc":
            self.builder.call(
                self.runtime["py_exc_traceback_print_exc"],
                [exc_val],
            )
            return self._emit_none_literal()
        result = self.builder.call(
            self.runtime["py_exc_traceback_format_exc"],
            [exc_val],
            name=self._fresh("traceback.format_exc"),
        )
        # This runtime formatter returns a new string, including the no-error
        # case. Record that producer at its return; no cleanup may intervene
        # before an enclosing operand consumer publishes the owned result.
        self._note_owned_object_value(result)
        return result

    def _module_imports_traceback(self) -> bool:
        # Worklist walk (module body + nested statement bodies) so a
        # function-local ``import traceback`` also opts the module in.
        pending = [self.ast_module.body]
        while pending:
            stmts = pending.pop()
            for stmt in stmts:
                if isinstance(stmt, Import):
                    for mod_name, _as_name in stmt.names:
                        if mod_name == "traceback":
                            return True
                    continue
                if isinstance(stmt, ImportFrom):
                    if stmt.module == "traceback":
                        for attr_name, _as_name in stmt.names:
                            if attr_name in ("format_exc", "print_exc"):
                                return True
                    continue
                if isinstance(stmt, Try):
                    pending.append(stmt.body)
                    pending.append(stmt.else_body)
                    pending.append(stmt.finally_body)
                    for h in stmt.handlers:
                        pending.append(h.body)
                    continue
                if isinstance(stmt, (If, While, For)):
                    pending.append(stmt.body)
                    pending.append(stmt.else_body)
                    continue
                if isinstance(stmt, (With, FuncDef, ClassDef)):
                    pending.append(stmt.body)
        return False

    def _rewrite_traceback_handler_bindings(self) -> None:
        """Synthesize an ``as``-binding on name-less ``except`` handlers
        in modules that import ``traceback``.

        CPython keeps the exception being handled alive for the whole
        handler body (thread-state exc_info), which is what
        ``traceback.format_exc()`` / ``print_exc()`` read. pcc's handler
        lowering clears the TLS slot at handler entry and only retains
        the exception (tracking it in ``_active_handler_excs``) when the
        handler is name-bound or its body re-raises — so in a plain
        ``except ValueError:`` handler the exception object is already
        freed when ``format_exc()`` runs. Renaming the handler to a
        reserved binding routes the existing retain + tracking path in
        exception lowering without touching its code, and moves the
        exception lifetime toward CPython semantics (alive until the
        handler ends)."""
        if not self._module_imports_traceback():
            return
        from dataclasses import replace as _replace

        def rewrite_stmts(stmts):
            # Avoid generator expressions — pcc-py self-host mis-hoists
            # ``for h in ...`` closures (see for-else tag_breaks).
            out = []
            changed = False
            for s in stmts:
                if isinstance(s, Try):
                    new_body = rewrite_stmts(s.body)
                    new_else = rewrite_stmts(s.else_body)
                    new_final = rewrite_stmts(s.finally_body)
                    handlers_changed = False
                    new_handlers_list = []
                    for h in s.handlers:
                        new_h_body = rewrite_stmts(h.body)
                        if h.name is None:
                            new_handlers_list.append(
                                _replace(
                                    h,
                                    name="__pcc_traceback_handled_exc",
                                    body=new_h_body,
                                )
                            )
                            handlers_changed = True
                        elif new_h_body is not h.body:
                            new_handlers_list.append(_replace(h, body=new_h_body))
                            handlers_changed = True
                        else:
                            new_handlers_list.append(h)
                    if (
                        handlers_changed
                        or new_body is not s.body
                        or new_else is not s.else_body
                        or new_final is not s.finally_body
                    ):
                        out.append(
                            _replace(
                                s,
                                body=new_body,
                                handlers=tuple(new_handlers_list),
                                else_body=new_else,
                                finally_body=new_final,
                            )
                        )
                        changed = True
                    else:
                        out.append(s)
                    continue
                if isinstance(s, (If, While, For)):
                    new_body = rewrite_stmts(s.body)
                    new_else = rewrite_stmts(s.else_body)
                    if new_body is not s.body or new_else is not s.else_body:
                        out.append(_replace(s, body=new_body, else_body=new_else))
                        changed = True
                    else:
                        out.append(s)
                    continue
                if isinstance(s, (With, FuncDef, ClassDef)):
                    new_body = rewrite_stmts(s.body)
                    if new_body is not s.body:
                        out.append(_replace(s, body=new_body))
                        changed = True
                    else:
                        out.append(s)
                    continue
                out.append(s)
            if changed:
                return tuple(out)
            return stmts

        new_module_body = rewrite_stmts(self.ast_module.body)
        if new_module_body is not self.ast_module.body:
            self.ast_module = _replace(self.ast_module, body=new_module_body)

    def _all_import_from_names_are_native_builtins(
        self,
        stmt: ImportFrom,
        import_module: str,
    ) -> bool:
        import os as _os
        import sys

        if _os.environ.get("PCC_PY_DEBUG_IMPORTS"):
            sys.stderr.write(
                f"[debug] _all_import_from_names_are_native_builtins mod={import_module!r} names={stmt.names!r}\n"
            )
        if import_module == "builtins":
            native_type_names = _native_builtin_type_value_names()
            for attr_name, _as_name in stmt.names:
                if (attr_name not in native_type_names
                        and self._builtin_function_wrapper(attr_name) is None):
                    return False
            return True
        if import_module == "sys":
            return all(
                attr_name
                in ("exit", "getrecursionlimit", "setrecursionlimit", "stdin", "stdout", "stderr", "prefix", "base_prefix")
                for attr_name, _as_name in stmt.names
            )
        if import_module == "os":
            return all(
                attr_name in ("path", "name", "sep", "linesep", "altsep", "pathsep", "urandom", "fdopen", "open", "close", "mkdir", "pwrite", "ftruncate")
                or native_os_descriptor_constant(attr_name, self._target_sys_platform_text()) is not None
                for attr_name, _as_name in stmt.names
            )
        if import_module == "time":
            return all(
                attr_name in _NATIVE_TIME_IMPORTS
                for attr_name, _as_name in stmt.names
            )
        if import_module == "string":
            return all(
                _string_constant_value(attr_name) is not None
                for attr_name, _as_name in stmt.names
            )
        if import_module == "tempfile":
            return all(attr_name in ("TemporaryDirectory", "mkdtemp", "NamedTemporaryFile") for attr_name, _as_name in stmt.names)
        if import_module == "dataclasses":
            return all(attr_name == "replace" for attr_name, _as_name in stmt.names)
        if import_module == "functools":
            return all(
                attr_name in ("partial", "reduce") for attr_name, _as_name in stmt.names
            )
        if import_module == "math":
            return all(
                attr_name
                in (
                    "floor",
                    "ceil",
                    "sqrt",
                    "trunc",
                    "gcd",
                    "factorial",
                    "isqrt",
                    "pi",
                    "e",
                    "tau",
                    "inf",
                    "nan",
                    "pow",
                )
                for attr_name, _as_name in stmt.names
            )
        if import_module == "pcc":
            return all(
                attr_name
                in (
                    "valueclass",
                    "virtual_thread",
                    "i64",
                    "u64",
                    "i64_buffer",
                    "guarded_i64_dot",
                    "guarded_loop_counter",
                )
                for attr_name, _as_name in stmt.names
            )
        if import_module == "re":
            return all(
                attr_name in ("match", "search", "fullmatch", "compile", "findall", "sub")
                for attr_name, _as_name in stmt.names
            )
        if import_module == "codecs":
            return all(
                attr_name in _CODECS_BOM_CONSTANTS for attr_name, _as_name in stmt.names
            )
        if import_module == "textwrap":
            return all(attr_name == "dedent" for attr_name, _as_name in stmt.names)
        if import_module == "traceback":
            return all(
                attr_name in ("format_exc", "print_exc")
                for attr_name, _as_name in stmt.names
            )
        if import_module == "gc":
            return all(
                attr_name
                in (
                    "collect",
                    "disable",
                    "enable",
                    "isenabled",
                    "is_tracked",
                    "is_finalized",
                    "get_count",
                    "get_threshold",
                    "set_threshold",
                    "get_stats",
                    "freeze",
                    "unfreeze",
                    "get_freeze_count",
                    "get_objects",
                    "get_referents",
                    "get_referrers",
                )
                for attr_name, _as_name in stmt.names
            )
        if import_module == "weakref":
            return all(
                attr_name in ("ref", "WeakValueDictionary", "WeakKeyDictionary")
                for attr_name, _as_name in stmt.names
            )
        if import_module == "copy":
            return all(
                attr_name in ("copy", "deepcopy") for attr_name, _as_name in stmt.names
            )
        if import_module == "pickle":
            return all(
                attr_name in ("dumps", "loads") for attr_name, _as_name in stmt.names
            )
        if import_module == "threading":
            return all(
                attr_name
                in (
                    "Thread",
                    "Lock",
                    "RLock",
                    "Event",
                    "Condition",
                    "Semaphore",
                    "BoundedSemaphore",
                    "current_thread",
                    "get_ident",
                )
                for attr_name, _as_name in stmt.names
            )
        if import_module == "pcc.virtual_thread":
            for attr_name, _as_name in stmt.names:
                if not _is_virtual_thread_export(attr_name):
                    return False
            return True
        if import_module == "contextlib":
            return all(
                attr_name == "contextmanager" for attr_name, _as_name in stmt.names
            )
        if import_module == "contextvars":
            return all(attr_name == "ContextVar" for attr_name, _as_name in stmt.names)
        if import_module == "enum":
            return all(
                attr_name in ("Enum", "IntEnum", "auto")
                for attr_name, _as_name in stmt.names
            )
        if import_module == "typing":
            return all(
                attr_name
                in (
                    "Generic",
                    "Protocol",
                    "TypeVar",
                    "runtime_checkable",
                    "get_origin",
                    "get_args",
                    "Optional",
                )
                for attr_name, _as_name in stmt.names
            )
        return False

    def _register_native_builtin_import_from_aliases(
        self,
        stmt: ImportFrom,
        import_module: str,
    ) -> bool:
        if not self._all_import_from_names_are_native_builtins(
            stmt,
            import_module,
        ):
            return False
        for attr_name, as_name in stmt.names:
            local_name = as_name or attr_name
            if (
                import_module == "builtins"
                and (attr_name in _native_builtin_type_value_names()
                     or self._builtin_function_wrapper(attr_name) is not None)
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "builtins." + attr_name,
                )
                continue
            if import_module == "tempfile" and attr_name in ("TemporaryDirectory", "mkdtemp", "NamedTemporaryFile"):
                self._register_native_builtin_value_alias(local_name, "tempfile." + attr_name)
                continue
            if attr_name == "path" and import_module == "os":
                self._register_native_builtin_value_alias(local_name, "os.path")
                continue
            if import_module == "os" and (attr_name in ("fdopen", "open", "close", "mkdir", "pwrite", "ftruncate")
                    or native_os_descriptor_constant(attr_name, self._target_sys_platform_text()) is not None):
                self._register_native_builtin_value_alias(local_name, "os." + attr_name)
                continue
            if attr_name == "urandom" and import_module == "os":
                self._register_native_builtin_value_alias(local_name, "os.urandom")
                continue
            if (
                attr_name in _NATIVE_TIME_IMPORTS
                and import_module == "time"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "time." + attr_name,
                )
                continue
            if attr_name == "partial" and import_module == "functools":
                self._register_native_builtin_value_alias(
                    local_name,
                    "functools.partial",
                )
                continue
            if attr_name == "reduce" and import_module == "functools":
                self._register_native_builtin_value_alias(
                    local_name,
                    "functools.reduce",
                )
                continue
            if (
                attr_name in ("format_exc", "print_exc")
                and import_module == "traceback"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "traceback." + attr_name,
                )
                continue
            if (
                attr_name in ("name", "sep", "linesep", "altsep", "pathsep")
                and import_module == "os"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "os." + attr_name,
                )
                continue
            string_value = _string_constant_value(attr_name)
            if string_value is not None and import_module == "string":
                self._register_native_module_constant(
                    local_name,
                    {
                        "kind": "constant",
                        "value_kind": "str",
                        "value": string_value,
                    },
                )
                continue
            if attr_name in ("exit", "getrecursionlimit", "setrecursionlimit") and import_module == "sys":
                self._register_native_builtin_value_alias(local_name, "sys." + attr_name)
                continue
            if attr_name == "replace" and import_module == "dataclasses":
                self._register_native_builtin_value_alias(
                    local_name,
                    "dataclasses.replace",
                )
                continue
            if (
                attr_name
                in (
                    "floor",
                    "ceil",
                    "copysign",
                    "sqrt",
                    "trunc",
                    "gcd",
                    "factorial",
                    "isqrt",
                )
                and import_module == "math"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "math." + attr_name,
                )
                continue
            if attr_name == "pow" and import_module == "math":
                self._register_native_builtin_value_alias(
                    local_name,
                    "math.pow",
                )
                continue
            if attr_name == "virtual_thread" and import_module == "pcc":
                # Match the existing ``import pcc.virtual_thread as name``
                # route, including a compiled sibling's initialization.
                if "pcc.virtual_thread" in getattr(self, "_sibling_module_inits", ()):
                    self._emit_compiled_module_ensure_initialized("pcc.virtual_thread")
                self._register_native_builtin_module_alias(
                    local_name,
                    "pcc.virtual_thread",
                )
                continue
            if attr_name == "valueclass" and import_module == "pcc":
                self._register_native_builtin_value_alias(
                    local_name,
                    "pcc.valueclass",
                )
                continue
            if attr_name in ("i64", "u64") and import_module == "pcc":
                self._register_native_builtin_value_alias(local_name, "builtins.int")
                continue
            if (
                attr_name
                in ("i64_buffer", "guarded_i64_dot", "guarded_loop_counter")
                and import_module == "pcc"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "pcc." + attr_name,
                )
                continue
            if (
                attr_name in ("pi", "e", "tau", "inf", "nan")
                and import_module == "math"
            ):
                self._register_native_module_constant(
                    local_name,
                    {
                        "kind": "constant",
                        "value_kind": "float",
                        "value": {
                            "pi": 3.141592653589793,
                            "e": 2.718281828459045,
                            "tau": 6.283185307179586,
                            "inf": _MATH_INF,
                            "nan": _MATH_NAN,
                        }[attr_name],
                    },
                )
                continue
            if attr_name in ("match", "search", "fullmatch", "compile", "findall", "sub") and import_module == "re":
                self._register_native_builtin_value_alias(
                    local_name,
                    "re." + attr_name,
                )
                continue
            if attr_name == "dedent" and import_module == "textwrap":
                self._register_native_builtin_value_alias(
                    local_name,
                    "textwrap.dedent",
                )
                continue
            if attr_name in _CODECS_BOM_CONSTANTS and import_module == "codecs":
                self._register_native_module_constant(
                    local_name,
                    {
                        "kind": "constant",
                        "value_kind": "bytes",
                        "value": _CODECS_BOM_CONSTANTS[attr_name],
                    },
                )
                continue
            if (
                attr_name
                in (
                    "collect",
                    "disable",
                    "enable",
                    "isenabled",
                    "is_tracked",
                    "is_finalized",
                    "get_count",
                    "get_threshold",
                    "set_threshold",
                    "get_stats",
                    "freeze",
                    "unfreeze",
                    "get_freeze_count",
                    "get_objects",
                    "get_referents",
                    "get_referrers",
                )
                and import_module == "gc"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "gc." + attr_name,
                )
                continue
            if (
                attr_name in ("ref", "WeakValueDictionary", "WeakKeyDictionary")
                and import_module == "weakref"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "weakref." + attr_name,
                )
                continue
            if attr_name in ("copy", "deepcopy") and import_module == "copy":
                self._register_native_builtin_value_alias(
                    local_name,
                    "copy." + attr_name,
                )
                continue
            if attr_name in ("dumps", "loads") and import_module == "pickle":
                self._register_native_builtin_value_alias(
                    local_name,
                    "pickle." + attr_name,
                )
                continue
            if (
                attr_name
                in (
                    "Thread",
                    "Lock",
                    "RLock",
                    "Event",
                    "Condition",
                    "Semaphore",
                    "BoundedSemaphore",
                    "current_thread",
                    "get_ident",
                )
                and import_module == "threading"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "threading." + attr_name,
                )
                continue
            if import_module == "pcc.virtual_thread" and _is_virtual_thread_export(
                attr_name
            ):
                if attr_name in _VIRTUAL_THREAD_CONSTANTS:
                    self._register_native_module_constant(
                        local_name,
                        {
                            "kind": "constant",
                            "value_kind": "int",
                            "value": _VIRTUAL_THREAD_CONSTANTS[attr_name],
                        },
                    )
                else:
                    self._register_native_builtin_value_alias(
                        local_name,
                        "pcc.virtual_thread." + attr_name,
                    )
                continue
            if attr_name == "contextmanager" and import_module == "contextlib":
                self._register_native_builtin_value_alias(
                    local_name,
                    "contextlib.contextmanager",
                )
                continue
            if attr_name == "ContextVar" and import_module == "contextvars":
                self._register_native_builtin_value_alias(
                    local_name,
                    "contextvars.ContextVar",
                )
                continue
            if attr_name in ("stdin", "stdout", "stderr") and import_module == "sys":
                self._register_native_builtin_value_alias(
                    local_name,
                    "sys." + attr_name,
                )
                continue
            if attr_name in ("prefix", "base_prefix") and import_module == "sys":
                self._register_native_builtin_value_alias(
                    local_name,
                    "sys." + attr_name,
                )
                continue
            if attr_name in ("Enum", "IntEnum", "auto") and import_module == "enum":
                self._register_native_builtin_value_alias(
                    local_name,
                    "enum." + attr_name,
                )
                continue
            if (
                attr_name
                in (
                    "Generic",
                    "Protocol",
                    "TypeVar",
                    "runtime_checkable",
                    "get_origin",
                    "get_args",
                    "Optional",
                )
                and import_module == "typing"
            ):
                self._register_native_builtin_value_alias(
                    local_name,
                    "typing." + attr_name,
                )
                continue
        return True

    def _note_function_import_alias(self, local_name: str) -> None:
        """Record a function-local import that overwrites a module alias.

        Like class method bodies, a function body must not leave another
        provider behind for a name the rest of the module already resolves.
        A name nothing else bound stays visible to later hoisted helpers.
        """
        if self.current_func_def is None:
            return
        semantic = self._cross_module_semantic_functions
        if local_name not in self.functions and local_name not in semantic:
            return
        undo = self._function_import_alias_undo
        for entry in undo:
            if entry[0] == local_name:
                return
        func_defs = self._cross_module_func_defs
        extension_modules = self._native_extension_modules()
        identity = self._cross_module_identity_decorators
        generators = getattr(self, "_generator_func_names", None)
        undo.append((
            local_name,
            local_name in self.functions,
            self.functions.get(local_name),
            local_name in func_defs,
            func_defs.get(local_name),
            local_name in semantic,
            semantic.get(local_name),
            local_name in extension_modules,
            extension_modules.get(local_name),
            local_name in identity,
            identity.get(local_name),
            generators is not None and local_name in generators,
        ))

    def _restore_function_import_aliases(self, undo: list) -> None:
        """Undo ``_note_function_import_alias`` records, newest first."""
        index = len(undo) - 1
        while index >= 0:
            entry = undo[index]
            index -= 1
            name = entry[0]
            _restore_alias_entry(self.functions, name, entry[1], entry[2])
            _restore_alias_entry(self._cross_module_func_defs, name, entry[3], entry[4])
            _restore_alias_entry(
                self._cross_module_semantic_functions, name, entry[5], entry[6],
            )
            _restore_alias_entry(
                self._native_extension_modules(), name, entry[7], entry[8],
            )
            _restore_alias_entry(
                self._cross_module_identity_decorators, name, entry[9], entry[10],
            )
            generators = getattr(self, "_generator_func_names", None)
            if generators is not None:
                if entry[11]:
                    generators.add(name)
                else:
                    generators.discard(name)

    def _bind_native_cross_module_export(
        self,
        *,
        local_name: str,
        src_module: str,
        attr_name: str,
        info: dict,
    ) -> bool:
        """Bind one export from a native sibling module.

        Used by both explicit imports and native star-import expansion. Returns
        False for export kinds that still need the caller's CPython fallback.
        """
        kind = info.get("kind")
        owning_module = info.get("owning_module", src_module)
        export_name = info.get("export_name", attr_name)
        if kind == "function":
            self._note_function_import_alias(local_name)
            semantic_functions = self._cross_module_semantic_functions
            if info.get("semantic_decorator"):
                semantic_functions[local_name] = (owning_module, export_name)
                gv = self._native_extension_module_global(local_name)
                self._native_extension_modules()[local_name] = gv
                self.functions.pop(local_name, None)
                self._cross_module_func_defs.pop(local_name, None)
                return True
            semantic_functions.pop(local_name, None)
            sanitised = self._module_symbol_suffix(owning_module)
            sym = f"user_{sanitised}_{export_name}"
            existing = self.module.globals.get(sym)
            if isinstance(existing, ir.Function):
                fn = existing
            else:
                box_int_abi = self._export_box_int_abi(info)
                param_tys = [
                    self._abi_ir_type(
                        decode_type(t),
                        box_int_abi=box_int_abi,
                    )
                    for t in info["param_types"]
                ]
                fnty = ir.FunctionType(
                    self._extern_user_function_return_ir_type(
                        info,
                        box_int_abi=box_int_abi,
                    ),
                    param_tys,
                )
                fn = ir.Function(self.module, fnty, name=sym)
                fn.linkage = "external"
            if (bool(info.get("manual_pointer_abi", False))
                    and info.get("manual_pointer_abi_symbol", "") == sym):
                self._manual_pointer_abi_functions.add(sym)
            self.functions[local_name] = fn
            self._cross_module_func_defs[local_name] = self._extern_info_to_funcdef(
                local_name,
                info,
            )
            if bool(info.get("may_park", False)):
                self._generator_func_names.add(local_name)
            identity_decorators = self._cross_module_identity_decorators
            if info.get("identity_decorator"):
                identity_decorators[local_name] = True
            else:
                identity_decorators.pop(local_name, None)
            # A published function object (needs_object) of the one module
            # definition carries ``<sym>_native_adapter`` as its entry.
            static_entries = self._cross_module_static_entries
            if (self.current_func_def is None
                    and bool(info.get("needs_object", False))
                    and bool(info.get("single_definition", False))
                    and not bool(info.get("manual_pointer_abi", False))
                    and not bool(info.get("is_async", False))
                    and not bool(info.get("may_park", False))):
                static_entries[local_name] = sym + "_native_adapter"
            elif self.current_func_def is None:
                static_entries.pop(local_name, None)
            return True
        if kind == "class":
            self._declare_native_module_extern_class(
                owning_module=owning_module,
                class_name=info["class_name"],
                field_names=info["field_names"],
                methods=info["methods"],
                local_name=local_name,
                base_names=info.get("base_names", ()),
            )
            return True
        if kind == "constant":
            self._register_native_module_constant(local_name, info)
            return True
        if kind == "module_global":
            self._declare_native_module_extern_global(
                local_name,
                owning_module,
                export_name,
                info,
            )
            return True
        if kind == "typing_metadata":
            self._typing_metadata_aliases.add(local_name)
            return True
        return False

    def _emit_native_default_func_ref(
        self,
        owning_module: str,
        export_name: str,
    ) -> ir.Value:
        native_table = self._native_module_exports
        if native_table is None or owning_module not in native_table:
            raise NotImplementedError(
                "native default function module not available: " + owning_module
            )
        exports = native_table[owning_module]
        info = exports.get(export_name)
        if not isinstance(info, dict) or info.get("kind") != "function":
            raise NotImplementedError(
                "native default function export not available: "
                + owning_module
                + "."
                + export_name
            )
        sanitised = self._module_symbol_suffix(owning_module)
        local_name = "__pcc_native_default_" + sanitised + "_" + export_name
        self._bind_native_cross_module_export(
            local_name=local_name,
            src_module=owning_module,
            attr_name=export_name,
            info=info,
        )
        fn = self.functions.get(local_name)
        if fn is None:
            raise NotImplementedError(
                "native default function failed to bind: "
                + owning_module
                + "."
                + export_name
            )
        return self._emit_native_func_value(
            export_name,
            local_name,
            fn,
            (),
        )

    def _emit_native_default_global_ref(
        self,
        owning_module: str,
        export_name: str,
        span,
    ):
        """Resolve a cross-module default-argument Name (``def f(x=CONST)``)
        through the defining module's exports. Returns None when the export
        is not a constant / module_global so the caller can fall back."""
        native_table = self._native_module_exports
        if native_table is None or owning_module not in native_table:
            return None
        info = native_table[owning_module].get(export_name)
        if not isinstance(info, dict):
            return None
        kind = info.get("kind")
        if kind == "constant":
            return self._emit_native_module_constant_or_override(
                owning_module,
                export_name,
                info,
            )
        if kind == "module_global":
            return self._emit_native_module_global_attr_load(
                owning_module,
                export_name,
                info,
                span,
            )
        return None

    def _import_fallback_shadowed_names(self, stmt: ImportFrom) -> set:
        """Fallback names whose ``try`` import binds from a compiled module.

        Only that try-body import can run, so the handler's alternative must
        not replace the static binding; its runtime import is still emitted.
        """
        if self._import_fallbacks is None:
            self._import_fallbacks = import_fallbacks(self.ast_module.body)
        alternatives = self._import_fallbacks.get(import_statement_key(stmt))
        shadowed: set = set()
        if not alternatives:
            return shadowed
        native_table = self._native_module_exports or {}
        for local_name, primary in alternatives.items():
            primary_module = self._resolve_relative_import(primary[0])
            if primary[1] in native_table.get(primary_module, {}):
                shadowed.add(local_name)
        return shadowed

    def _predeclare_native_cross_module(
        self,
        stmt: ImportFrom,
        src_module: str,
        exports: dict,
    ) -> None:
        """First-pass declaration for native cross-module imports:
        declare the extern function globals and bind them in
        ``self.functions`` so user-function bodies lowered in the
        same compilation unit can resolve the call. Class imports
        declare the class global + method externs via
        ``class_lowering.declare_extern_class``."""
        # A compile-time-only import (typing, abc, ...) executes no binding,
        # so a name without a static export must not become a runtime global:
        # its consumers would read that never-published global instead of
        # treating the name as compile-time metadata.
        runtime_binding = not _import_from_binds_nothing_at_runtime(stmt)
        shadowed = self._import_fallback_shadowed_names(stmt)
        for attr_name, as_name in stmt.names:
            if attr_name == "*":
                for export_name, export_info in _native_star_export_items(exports):
                    self._bind_native_cross_module_export(
                        local_name=export_name,
                        src_module=src_module,
                        attr_name=export_name,
                        info=export_info,
                    )
                continue
            local_name = as_name or attr_name
            if local_name in shadowed:
                continue
            info = exports.get(attr_name)
            if info is None:
                full_submodule = self._native_import_from_submodule(
                    src_module,
                    attr_name,
                )
                if full_submodule is not None:
                    self._register_native_module_alias(
                        local_name,
                        full_submodule,
                    )
                    continue
                if runtime_binding:
                    gv = self._native_extension_module_global(local_name)
                    self._native_extension_modules()[local_name] = gv
                continue
            if self._bind_native_cross_module_export(
                local_name=local_name,
                src_module=src_module,
                attr_name=attr_name,
                info=info,
            ):
                continue
            if runtime_binding:
                gv = self._native_extension_module_global(local_name)
                self._native_extension_modules()[local_name] = gv

    def _native_import_from_submodule(
        self,
        src_module: str,
        attr_name: str,
    ) -> Optional[str]:
        """Return ``pkg.attr`` when ``from pkg import attr`` names a
        native sibling submodule compiled in the same invocation."""
        if not src_module or attr_name == "*":
            return None
        full_name = f"{src_module}.{attr_name}"
        if full_name == getattr(self.ast_module, "name", None):
            return full_name
        native_table = self._native_module_exports
        if native_table is None:
            return None
        if full_name in native_table:
            return full_name
        return None

    def _has_native_import_from_targets(
        self,
        stmt: ImportFrom,
        src_module: str,
    ) -> bool:
        """Whether ``from src import ...`` touches any native sibling
        export or native sibling submodule."""
        native_table = self._native_module_exports
        if native_table is None:
            return False
        if src_module in native_table:
            return True
        for attr_name, _ in stmt.names:
            if self._native_import_from_submodule(src_module, attr_name):
                return True
        return False

    def _bind_native_cross_module_imports(
        self,
        stmt: ImportFrom,
        src_module: str,
        exports: dict,
    ) -> None:
        """For each name imported from a native sibling module,
        declare an extern function of matching signature and register
        in ``self.functions`` so subsequent calls resolve to it."""
        conflict_names = ()
        if self.current_func_def is not None:
            conflict_names = function_import_conflict_names(self.current_func_def)
        shadowed = self._import_fallback_shadowed_names(stmt)
        for attr_name, as_name in stmt.names:
            if attr_name == "*":
                for export_name, export_info in _native_star_export_items(exports):
                    self._bind_native_cross_module_export(
                        local_name=export_name,
                        src_module=src_module,
                        attr_name=export_name,
                        info=export_info,
                    )
                if self.builder is not None:
                    self._emit_compiled_module_import_star(src_module)
                continue
            local_name = as_name or attr_name
            info = exports.get(attr_name)
            if info is not None and local_name in shadowed:
                # The try-body import owns the static binding; this handler
                # only rebinds the module namespace if that import failed.
                self._emit_compiled_module_import_from(
                    src_module,
                    [(attr_name, as_name)],
                )
                continue
            if info is None:
                full_submodule = self._native_import_from_submodule(
                    src_module,
                    attr_name,
                )
                if full_submodule is not None:
                    self._register_native_module_alias(
                        local_name,
                        full_submodule,
                    )
                    continue
                if src_module in (self._native_module_exports or {}):
                    self._emit_compiled_module_import_from(
                        src_module,
                        [(attr_name, as_name)],
                    )
                else:
                    self._import_from_cpython_single(
                        stmt,
                        src_module,
                        attr_name,
                        as_name,
                    )
                continue
            if self.current_func_def is not None and (
                info.get("kind") in ("module_global", "constant")
                or (
                    info.get("kind") in ("function", "class")
                    and name_in(conflict_names, local_name)
                )
            ):
                # A from-import executes a binding in this function's scope.
                # Declaring a consumer module cell does not initialize a local
                # value, and a constant shortcut would ignore later rebinding.
                # A static function/class alias is flow-insensitive, so a
                # name with another provider or binding needs this value too.
                self._emit_compiled_module_import_from(
                    src_module,
                    [(attr_name, as_name)],
                )
                continue
            if self._bind_native_cross_module_export(
                local_name=local_name,
                src_module=src_module,
                attr_name=attr_name,
                info=info,
            ):
                if (
                    self.current_func_def is None
                    and info.get("kind") == "module_global"
                ):
                    value_ty = decode_type(
                        info.get("value_ty", ("dyn",))
                    ) or DynType(name="dyn")
                    provider_boxes_int = bool(
                        info.get("box_int_abi", self._should_box_python_ints())
                    )
                    if isinstance(value_ty, IntType):
                        source_ir_ty = self._abi_ir_type(value_ty, box_int_abi=provider_boxes_int)
                    elif self._is_object(value_ty):
                        source_ir_ty = _CSTR
                    else:
                        source_ir_ty = self._storage_ir_type(value_ty)
                    source_symbol = self._module_global_symbol_name(
                        info.get("owning_module", src_module),
                        info.get("export_name", attr_name),
                    )
                    source_gv = self.module.globals.get(source_symbol)
                    if not isinstance(source_gv, ir.GlobalVariable):
                        source_gv = ir.GlobalVariable(
                            self.module,
                            source_ir_ty,
                            name=source_symbol,
                        )
                        source_gv.linkage = "external"
                    local_gv, local_ty = self._module_globals[local_name]
                    imported_value = self.builder.load(
                        source_gv,
                        name=self._fresh("native.import.global." + local_name),
                    )
                    if not self._ir_type_matches(
                        local_gv.value_type,
                        source_gv.value_type,
                    ):
                        imported_value = self._bridge_cross_module_int_global(
                            imported_value,
                            local_gv.value_type,
                            src_module,
                            attr_name,
                        )
                    self._store_module_global_root_value(
                        local_gv,
                        imported_value,
                        declared_ty=local_ty,
                    )
                if (
                    self.current_func_def is None
                    and info.get("kind") != "typing_metadata"
                ):
                    # A statically bound direct-call/class/constant import is
                    # still an ordinary module-namespace binding.  Load the
                    # initialized sibling's stable object and publish it so
                    # module attributes and subsequent runtime star imports
                    # observe the same binding as the static fast path.
                    self._emit_compiled_module_import_from(
                        src_module,
                        [(attr_name, as_name)],
                    )
                continue
            if src_module in (self._native_module_exports or {}):
                self._emit_compiled_module_import_from(
                    src_module,
                    [(attr_name, as_name)],
                )
            else:
                self._import_from_cpython_single(
                    stmt,
                    src_module,
                    attr_name,
                    as_name,
                )

    def _register_native_module_constant(
        self,
        local_name: str,
        info: dict,
    ) -> None:
        """Bind ``from native_mod import CONST`` without materialising a
        CPython module object."""
        if not hasattr(self, "_native_module_constant_bindings"):
            self._native_module_constant_bindings = {}
        self._native_module_constant_bindings[local_name] = info

    def _emit_native_module_global_attr_load(
        self,
        module_name: str,
        attr_name: str,
        info: dict,
        span,
    ) -> ir.Value:
        """Load a sibling module's top-level variable via its extern global.

        Used by Attr lowering when ``mod.attr`` resolves to an export with
        ``kind == "module_global"`` (e.g. ``mod.__all__`` where the sibling
        defined ``__all__ = [...]``).  The ``.modvar.<mod>.<attr>`` extern
        is the same one the defining module's ``_pcc_py_module_top_<mod>()``
        populates at init time.  Without this helper the Attr lowering
        falls through to a generic ``py_obj_getattr`` on the module-name
        string and fails with ``AttributeError`` — see
        ``docs/investigations/python-native-module-alias-module-global-attr-attribute-error.md``.
        """
        value_ty = decode_type(info.get("value_ty", ("dyn",))) or DynType(name="dyn")
        provider_boxes_int = bool(
            info.get("box_int_abi", self._should_box_python_ints())
        )
        if isinstance(value_ty, IntType):
            ir_ty = self._abi_ir_type(value_ty, box_int_abi=provider_boxes_int)
        elif self._is_object(value_ty):
            ir_ty = _CSTR
        else:
            ir_ty = self._storage_ir_type(value_ty)
        sym = self._module_global_symbol_name(module_name, attr_name)
        existing = self.module.globals.get(sym)
        if existing is None:
            gv = ir.GlobalVariable(self.module, ir_ty, name=sym)
            gv.linkage = "external"
        else:
            gv = existing
        loaded = self.builder.load(
            gv,
            name=self._fresh(f"modvar.{attr_name}"),
        )
        if isinstance(value_ty, IntType):
            # The consumer may keep ints in the other representation than the
            # exporting module (raw i64 versus boxed object); bridge on load.
            consumer_ir_ty = (
                self._abi_ir_type(value_ty, box_int_abi=self._should_box_python_ints())
            )
            if not self._ir_type_matches(consumer_ir_ty, gv.value_type):
                loaded = self._bridge_cross_module_int_global(
                    loaded, consumer_ir_ty, module_name, attr_name
                )
        return loaded

    def _declare_native_module_extern_global(
        self,
        local_name: str,
        owning_module: str,
        attr_name: str,
        info: dict,
    ) -> None:
        value_ty = decode_type(info.get("value_ty", ("dyn",))) or DynType(name="dyn")
        provider_boxes_int = bool(
            info.get("box_int_abi", self._should_box_python_ints())
        )
        if isinstance(value_ty, IntType):
            ir_ty = self._abi_ir_type(value_ty, box_int_abi=provider_boxes_int)
        elif self._is_object(value_ty):
            ir_ty = _CSTR
        else:
            ir_ty = self._storage_ir_type(value_ty)
        sym = self._module_global_symbol_name(owning_module, attr_name)
        existing = self.module.globals.get(sym)
        if existing is None:
            gv = ir.GlobalVariable(self.module, ir_ty, name=sym)
            gv.linkage = "external"
        else:
            gv = existing
        local_gv, _local_ty = self._ensure_module_global_name(local_name, value_ty)
        if not self._ir_type_matches(local_gv.value_type, ir_ty):
            # Declaration time has no builder.  An int stored by the exporter
            # in the other representation (raw i64 versus boxed object) is
            # bridged at every load in _emit_native_module_global_attr_load;
            # anything else is a genuine mismatch and fails closed here.
            int_shaped = isinstance(
                local_gv.value_type, (ir.IntType, ir.PointerType)
            ) and isinstance(ir_ty, (ir.IntType, ir.PointerType))
            if not (isinstance(value_ty, IntType) and int_shaped):
                raise NotImplementedError(
                    "cross-module global representation mismatch for "
                    + owning_module
                    + "."
                    + attr_name
                )


    def _bridge_cross_module_int_global(
        self,
        value: ir.Value,
        local_ir_ty: ir.Type,
        owning_module: str,
        attr_name: str,
    ) -> ir.Value:
        """Convert a sibling module's int global between its raw ``i64`` and
        boxed-object representations.

        Modules decide their int representation independently (the raw-int
        scaffold follows ``pcc.*`` naming or a ``pcc.unsafe``/``pcc.extern``
        import), so an application package can import ``CONST = 7`` from a
        sibling that stores it as ``i64`` into a module that stores ints as
        objects, or the reverse.  Both directions are exact for the int lane;
        anything else is a genuine mismatch and fails closed.
        """
        value_ty = getattr(value, "type", None)
        if isinstance(value_ty, ir.IntType) and isinstance(local_ir_ty, ir.PointerType):
            if value_ty.width != 64:
                value = self.builder.sext(value, _I64, name=self._fresh("native.import.sext"))
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [value],
                name=self._fresh("native.import.box." + attr_name),
            )
        if isinstance(value_ty, ir.PointerType) and isinstance(local_ir_ty, ir.IntType):
            unboxed = self.builder.call(
                self.runtime["py_int_value_i64"],
                [value],
                name=self._fresh("native.import.unbox." + attr_name),
            )
            if local_ir_ty.width != 64:
                unboxed = self.builder.trunc(unboxed, local_ir_ty, name=self._fresh("native.import.trunc"))
            return unboxed
        raise NotImplementedError(
            "cross-module global representation mismatch for "
            + owning_module
            + "."
            + attr_name
        )

    def _register_native_module_alias(
        self,
        local_name: str,
        module_name: str,
    ) -> None:
        """Track ``from .pkg import submod as name`` bindings for the
        limited native-submodule path.

        This is intentionally narrow: it only exists so later
        ``name.fn(...)`` calls can resolve directly against a sibling
        submodule's exported functions without routing through
        ``py_cpy_import``."""
        self._native_module_aliases[local_name] = module_name

    def _register_native_module_object_alias(
        self,
        local_name: str,
        module_name: str,
    ) -> None:
        """Track ``x = __import__("native_sibling")``.

        Unlike ``_native_module_aliases``, this binding represents a value
        produced by the dynamic import API, not a source-level ``import mod``
        alias.  Keep it separate so value-position reads of ``x`` do not get
        classified as CPython-backed module objects.
        """
        self._native_module_object_aliases[local_name] = module_name

    def _clear_native_module_object_alias(self, local_name: str) -> None:
        aliases = self._native_module_object_aliases
        if aliases is not None:
            aliases.pop(local_name, None)

    def _native_module_object_for_name(self, local_name: str) -> Optional[str]:
        return self._native_module_object_aliases.get(local_name)

    def _native_module_attr_global(
        self,
        module_name: str,
        attr_name: str,
    ) -> ir.GlobalVariable:
        slots = getattr(self, "_native_module_attr_globals", None)
        if slots is None:
            self._native_module_attr_globals = {}
            slots = self._native_module_attr_globals
        key = (module_name, attr_name)
        existing = slots.get(key)
        if existing is not None:
            return existing
        mod_suffix = self._module_symbol_suffix(module_name)
        attr_suffix = attr_name.replace(".", "_").replace("-", "_")
        gv = ir.GlobalVariable(
            self.module,
            _CSTR,
            name=f".modattr.{mod_suffix}.{attr_suffix}",
        )
        gv.linkage = "internal"
        gv.initializer = ir.Constant(_CSTR, None)
        slots[key] = gv
        return gv

    def _native_module_attr_global_if_exists(
        self,
        module_name: str,
        attr_name: str,
    ) -> Optional[ir.GlobalVariable]:
        slots = getattr(self, "_native_module_attr_globals", None)
        if slots is None:
            return None
        return slots.get((module_name, attr_name))

    def _emit_native_module_attr_load(
        self,
        module_name: str,
        attr_name: str,
        span: Optional[SourceSpan],
    ) -> Optional[ir.Value]:
        gv = self._native_module_attr_global_if_exists(module_name, attr_name)
        if gv is None:
            return None
        value = self.builder.load(
            gv,
            name=self._fresh(f"modattr.{module_name}.{attr_name}"),
        )
        missing = self.builder.icmp_signed(
            "==",
            value,
            ir.Constant(value.type, None),
            name=self._fresh(f"modattr.{attr_name}.missing"),
        )
        err_bb = self.current_function.append_basic_block(
            name=self._fresh(f"modattr.{attr_name}.err"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh(f"modattr.{attr_name}.ok"),
        )
        self.builder.cbranch(missing, err_bb, ok_bb)
        self.builder.position_at_end(err_bb)
        self._emit_builtin_exception_and_branch(
            "AttributeError",
            f"module {module_name!r} has no attribute {attr_name!r}",
            span,
        )
        self.builder.position_at_end(ok_bb)
        return value

    def _emit_native_module_constant_or_override(
        self,
        module_name: str,
        attr_name: str,
        info: dict,
    ) -> ir.Value:
        if info.get("has_module_storage") and not self._freestanding_module:
            # A source literal owns a normal module-global cell. Read that
            # cell on every attribute access, including in other consumers
            # and after the provider itself assigns to its global name.
            owning_module = info.get("owning_module", module_name)
            export_name = info.get("export_name", attr_name)
            value_ty = decode_type(info["value_ty"]) or DynType(name="dyn")
            # Constant attributes have always exposed an object. Do not use
            # the ordinary module-global loader's consumer integer bridge:
            # a raw-int consumer must not narrow a provider's boxed bigint.
            if isinstance(value_ty, IntType):
                if "box_int_abi" not in info:
                    raise NotImplementedError("compiled module constant integer storage ABI is unknown")
                ir_ty = self._abi_ir_type(value_ty, box_int_abi=bool(info["box_int_abi"]))
            elif self._is_object(value_ty):
                ir_ty = _CSTR
            else:
                ir_ty = self._storage_ir_type(value_ty)
            symbol = self._module_global_symbol_name(owning_module, export_name)
            gv = self.module.globals.get(symbol)
            if gv is None:
                gv = ir.GlobalVariable(self.module, ir_ty, name=symbol)
                gv.linkage = "external"
            elif not self._ir_type_matches(gv.value_type, ir_ty):
                raise NotImplementedError("compiled module constant storage ABI mismatch: " + symbol)
            value = self.builder.load(gv, name=self._fresh("modvar.constant." + attr_name))
            if isinstance(ir_ty, ir.PointerType):
                return value
            return marshal.marshal_to_object(
                self.builder, self.module, self.runtime, value, value_ty,
            )
        gv = self._native_module_attr_global_if_exists(module_name, attr_name)
        if gv is None:
            return self._emit_native_module_constant(info)
        default_value = self._emit_native_module_constant(info)
        return self.builder.call(
            self.runtime["py_module_attr_value_or_default"],
            [gv, default_value],
        )

    def _native_module_alias_export_info(
        self,
        alias_name: str,
        attr_name: str,
    ) -> Optional[tuple[str, dict]]:
        """Return ``(module_name, export_info)`` for ``alias.attr`` when
        ``alias`` names a native sibling submodule or a literal dynamic
        import of one."""
        if live_import_global_slot(self, alias_name) is not None:
            return None
        module_name = self._native_module_aliases.get(alias_name)
        if module_name is None:
            module_name = self._native_module_object_for_name(alias_name)
        if module_name is None:
            return None
        native_table = self._native_module_exports
        if native_table is None:
            return None
        info = native_table.get(module_name, {}).get(attr_name)
        if info is None:
            return None
        return module_name, info

    def _native_builtin_compiled_export_info(
        self,
        alias_name: str,
        attr_name: str,
    ) -> Optional[tuple[str, dict]]:
        """Return a compiled export behind a builtin-module alias.

        Imports such as ``import re`` are registered as builtin aliases so
        dedicated native lowerings get first refusal. In a recursive
        closed-world compile the same module can also have a compiled export
        table. When a dedicated lowering cannot handle a call or attribute,
        consult that table before falling all the way back to libpython.
        """
        module_name = self._native_builtin_module_for_name(alias_name)
        if module_name is None:
            return None
        native_table = self._native_module_exports or {}
        info = native_table.get(module_name, {}).get(attr_name)
        if not isinstance(info, dict):
            return None
        return module_name, info

    def _native_module_expr_export_info(
        self,
        module_expr: Expr,
        attr_name: str,
    ) -> Optional[tuple[str, dict]]:
        """Return native export info for ``module_expr.attr``.

        Supports both the existing one-level shape
        ``import string; string.ascii_lowercase`` and the package shape
        ``import urllib.parse; urllib.parse.quote``. The public import
        spelling stays CPython-compatible; this only changes how pcc's
        native module alias table is queried.
        """
        if live_import_expr_binding(self, module_expr):
            return None
        if isinstance(module_expr, Name):
            return self._native_module_alias_export_info(
                module_expr.ident,
                attr_name,
            )
        if isinstance(module_expr, Attr) and isinstance(module_expr.obj, Name):
            base = module_expr.obj.ident
            child = module_expr.name
            module_name = self._native_module_aliases.get(base)
            if module_name is None:
                return None
            native_table = self._native_module_exports
            if native_table is None:
                return None
            if module_name == f"{base}.{child}":
                info = native_table.get(module_name, {}).get(attr_name)
                if info is not None:
                    return module_name, info
            full_name = f"{module_name}.{child}"
            info = native_table.get(full_name, {}).get(attr_name)
            if info is not None:
                return full_name, info
            return None
        # Deeper dotted package access: ``import a.b.c; a.b.c.X`` where the
        # module expression is a 3+-level Attr chain (``a.b.c``) whose ``.obj``
        # is itself an Attr, not a Name — so the one-level branch above does not
        # match.  Flatten the pure Name/Attr chain to its spelled dotted name
        # and look it up directly in the native export table, gated on the root
        # being a known native module alias (so a real object attribute chain
        # ``obj.x.y.z`` is left to ``py_obj_getattr``).  Without this,
        # ``a.b.c.X`` fell through to the runtime getattr chain and raised
        # ``AttributeError`` on the intermediate ``a.b``.  See investigation
        # docs/investigations/python-deep-dotted-package-attr-no-libpython.md
        parts = self._dotted_module_parts(module_expr)
        if parts is not None and len(parts) >= 2:
            if parts[0] in self._native_module_aliases:
                native_table = self._native_module_exports
                if native_table is not None:
                    spelled = ".".join(parts)
                    info = native_table.get(spelled, {}).get(attr_name)
                    if info is not None:
                        return spelled, info
        return None

    def _dotted_module_parts(self, expr: Expr) -> Optional[list]:
        """Flatten a pure ``a.b.c`` Name/Attr chain to ``["a", "b", "c"]``.

        Returns None if ``expr`` is not a chain of ``Attr`` nodes rooted at a
        ``Name`` (e.g. it contains a call or subscript), so non-module attribute
        chains are not mistaken for dotted module access.
        """
        parts: list = []
        cur = expr
        while isinstance(cur, Attr):
            parts.append(cur.name)
            cur = cur.obj
        if isinstance(cur, Name):
            parts.append(cur.ident)
            parts.reverse()
            return parts
        return None

    def _native_literal_dunder_import_module(self, expr: Expr) -> Optional[str]:
        """Return the native module named by literal one-arg ``__import__``."""
        if not isinstance(expr, Call) or expr.kwargs or len(expr.args) != 1:
            return None
        if isinstance(expr.func, Name) and expr.func.ident == "__import__":
            arg = expr.args[0]
            if isinstance(arg, StrLit) and self._is_native_dynamic_module(arg.value):
                return arg.value
        return None

    def _native_module_name_for_object_expr(self, expr: Expr) -> Optional[str]:
        if live_import_expr_binding(self, expr):
            return None
        if isinstance(expr, Name):
            module_name = self._native_module_object_for_name(expr.ident)
            if module_name is not None:
                return module_name
            module_name = self._native_module_aliases.get(expr.ident)
            if module_name is not None:
                return module_name
            return self._native_builtin_module_for_name(expr.ident)
        if isinstance(expr, Call):
            return self._native_literal_dunder_import_module(expr)
        # sys.modules is a live, mutable mapping. Even a literal key must use
        # its current entry; an export-table lookup would hide replacements,
        # deletions and None entries, and could initialize an absent module.
        return None

    def _native_module_object_export_info(
        self,
        module_expr: Expr,
        attr_name: str,
    ) -> Optional[tuple[str, dict]]:
        export = self._native_module_expr_export_info(module_expr, attr_name)
        if export is not None:
            return export
        module_name = self._native_module_name_for_object_expr(module_expr)
        if module_name is None:
            return None
        native_table = self._native_module_exports or {}
        info = native_table.get(module_name, {}).get(attr_name)
        if info is None:
            return None
        return module_name, info

    def _emit_native_module_export_value(
        self,
        module_name: str,
        attr_name: str,
        info: dict,
    ) -> ir.Value:
        kind = info.get("kind")
        if kind == "constant":
            return self._emit_native_module_constant_or_override(
                module_name,
                attr_name,
                info,
            )
        if kind == "module_global":
            owning_module = info.get("owning_module", module_name)
            export_name = info.get("export_name", attr_name)
            value_ty = decode_type(info.get("value_ty", ("dyn",))) or DynType(
                name="dyn"
            )
            provider_boxes_int = bool(
                info.get("box_int_abi", self._should_box_python_ints())
            )
            if isinstance(value_ty, IntType):
                ir_ty = self._abi_ir_type(value_ty, box_int_abi=provider_boxes_int)
            elif self._is_object(value_ty):
                ir_ty = _CSTR
            else:
                ir_ty = self._storage_ir_type(value_ty)
            sym = self._module_global_symbol_name(owning_module, export_name)
            existing = self.module.globals.get(sym)
            if existing is None:
                gv = ir.GlobalVariable(self.module, ir_ty, name=sym)
                gv.linkage = "external"
            else:
                gv = existing
            return self.builder.load(gv, name=self._fresh(f"modvar.{attr_name}"))
        if kind == "function":
            # A function used as ``module.fn`` in value position is a Python
            # function object, not the native entry-point address used by the
            # direct-call fast path.  The defining module publishes its stable
            # wrapper into the live namespace at initialization; load that
            # owned value so assignment, identity, metadata, and later dynamic
            # calls keep ordinary Python semantics.
            module_name_ptr = self._ptr_to_cstr(
                self._cstr_global(
                    module_name,
                    f".pcc.compiled.value.module.{module_name}",
                )
            )
            value = self.builder.call(
                self.runtime["py_module_attr_get"],
                [module_name_ptr, self._attr_name_ptr(attr_name)],
                name=self._fresh(f"compiled.module.value.{attr_name}"),
            )
            self._emit_attribute_error_if_null(value, attr_name, None)
            return value
        if kind == "class" and hasattr(self, "class_lowering"):
            owning_module = info.get("owning_module", module_name)
            export_name = info.get("export_name", attr_name)
            class_info = self.class_lowering.declare_extern_class(
                owning_module=owning_module,
                class_name=info["class_name"],
                field_names=info["field_names"],
                methods=info["methods"],
                local_name=export_name,
                field_types=info.get("field_types", ()),
            )
            if class_info is not None:
                return self.builder.load(
                    class_info.global_var,
                    name=self._fresh(f"cls.{attr_name}"),
                )
        raise NotImplementedError(
            f"native module export {module_name}.{attr_name} kind "
            f"{kind!r} is not value-lowerable"
        )

    def _maybe_emit_native_module_hasattr(self, expr: Call) -> Optional[ir.Value]:
        if len(expr.args) != 2 or expr.kwargs:
            return None
        if not isinstance(expr.args[1], StrLit):
            return None
        module_name = self._native_module_name_for_object_expr(expr.args[0])
        if module_name is None:
            return None
        native_table = self._native_module_exports or {}
        attr_name = expr.args[1].value
        present = attr_name in native_table.get(module_name, {})
        if not present:
            present = self._native_builtin_module_has_attr(module_name, attr_name)
        if present:
            return ir.Constant(_I1, 1)
        if module_name == "sys" and attr_name == "get_int_max_str_digits":
            return ir.Constant(_I1, 0)
        if module_name in native_table and not self._is_native_builtin_dynamic_module(
            module_name
        ):
            module_name_ptr = self._ptr_to_cstr(
                self._cstr_global(
                    module_name,
                    f".hasattr.module.{module_name}",
                )
            )
            value = self.builder.call(
                self.runtime["py_module_attr_get"],
                [module_name_ptr, self._attr_name_ptr(attr_name)],
                name=self._fresh("hasattr.module.value"),
            )
            # Do not rebind the earlier Python ``bool`` named ``present`` to
            # an LLVM ``i1`` value.  The host compiler tolerates that mixed
            # local, but a self-hosted pcc1 keeps the first projection and
            # can turn the returned IR value into ``None``.  That made a
            # successful runtime module lookup report false from ``hasattr``.
            runtime_present = self.builder.icmp_signed(
                "!=",
                value,
                ir.Constant(_CSTR, None),
                name=self._fresh("hasattr.module.present"),
            )
            self._gc_release(value)
            return runtime_present
        # Attribute is not in pcc's static native table.  Returning a
        # compile-time False here was the historic behavior, but that made
        # ``hasattr(os, "__all__")``, ``hasattr(os, "path")``,
        # ``hasattr(os, "__name__")`` and similar evaluate to False at
        # compile time even though the runtime can answer correctly
        # (either via the libpython fallback for cpython-backed modules
        # under ``--python-libpython=auto``, or via ``py_obj_getattr`` on
        # the underlying pcc-native module object).  Fall through
        # (return None) so the next lowering branch in
        # ``call_expression_lowering.py::_emit_hasattr`` can emit a runtime
        # hasattr: ``py_cpy_call(builtins.hasattr)`` when the object also
        # looks like a cpython value, or ``py_obj_getattr`` non-NULL
        # otherwise.  Surfaced by numpy/__init__.py:681 via
        # ``_core.__all__`` and minimally as
        # ``hasattr(os, "__all__") → False`` under
        # ``--python-libpython=auto``.  See
        # ``docs/investigations/python-hasattr-static-false-on-builtin-modules.md``.
        return None

    def _maybe_emit_native_module_getattr(self, expr: Call) -> Optional[ir.Value]:
        if not (2 <= len(expr.args) <= 3) or expr.kwargs:
            return None
        module_name = self._native_module_name_for_object_expr(expr.args[0])
        if module_name is None:
            if self._expr_looks_cpython(expr.args[0]):
                if len(expr.args) == 3:
                    return self._emit_as_object(expr.args[2])
                return None
            return None
        if not isinstance(expr.args[1], StrLit):
            module_name_ptr = self._ptr_to_cstr(
                self._cstr_global(
                    module_name,
                    f".getattr.module.{module_name}",
                )
            )
            attr_name_ptr = self._emit_attr_name_ptr_arg(
                expr.args[1],
                "getattr.module.name",
            )
            value = self.builder.call(
                self.runtime["py_module_attr_get"],
                [module_name_ptr, attr_name_ptr],
                name=self._fresh("getattr.module.value"),
            )
            if len(expr.args) == 2:
                self._emit_attribute_error_if_null(
                    value,
                    "dynamic module attribute",
                    expr.span,
                )
                return value
            default_obj = self._emit_as_object(expr.args[2])
            is_missing = self.builder.icmp_signed(
                "==",
                value,
                ir.Constant(_CSTR, None),
                name=self._fresh("getattr.module.missing"),
            )
            return self.builder.select(
                is_missing,
                default_obj,
                value,
                name=self._fresh("getattr.module.default"),
            )
        native_table = self._native_module_exports or {}
        info = native_table.get(module_name, {}).get(expr.args[1].value)
        if info is None:
            builtin_attr = self._emit_native_builtin_module_attr(
                module_name,
                expr.args[1].value,
            )
            if builtin_attr is not None:
                return builtin_attr
            if len(expr.args) == 3:
                return self._emit_as_object(expr.args[2])
            return None
        return self._emit_native_module_export_value(
            module_name,
            expr.args[1].value,
            info,
        )

    def _maybe_emit_native_module_type_name(self, expr: Attr) -> Optional[ir.Value]:
        if (
            expr.name != "__name__"
            or not isinstance(expr.obj, Call)
            or not isinstance(expr.obj.func, Name)
            or expr.obj.func.ident != "type"
            or len(expr.obj.args) != 1
        ):
            return None
        if self._native_module_name_for_object_expr(expr.obj.args[0]) is None:
            return None
        return self._emit_str_literal("module")

    def _inspect_module_call_name(self, expr: Call) -> Optional[str]:
        if not isinstance(expr.func, Attr) or expr.kwargs:
            return None
        attr = expr.func
        if not isinstance(attr.obj, Name):
            return None
        if (
            self._native_module_aliases.get(attr.obj.ident) != "inspect"
            and self._native_builtin_module_for_name(attr.obj.ident) != "inspect"
        ):
            return None
        return attr.name

    def _inspect_funcdef_for_expr(self, expr: Expr) -> Optional[FuncDef]:
        if not isinstance(expr, Name):
            return None
        try:
            return self._find_user_funcdef(expr.ident)
        except Exception:
            return None

    def _inspect_default_repr(self, expr: Optional[Expr]) -> Optional[str]:
        if expr is None:
            return None
        if isinstance(expr, IntLit):
            return str(expr.value)
        if isinstance(expr, StrLit):
            return repr(expr.value)
        if isinstance(expr, NoneLit):
            return "None"
        return "..."

    def _inspect_signature_metadata_for_funcdef(self, fd: FuncDef) -> dict:
        display_parts: list[str] = []
        params: list[str] = []
        varargs = None
        varkw = None
        for arg in fd.args:
            if not arg.name:
                continue
            if arg.kind == "**kwargs":
                varkw = arg.name
                display_parts.append("**" + arg.name)
                continue
            if arg.kind == "*args":
                varargs = arg.name
                display_parts.append("*" + arg.name)
                continue
            params.append(arg.name)
            part = arg.name
            default_repr = self._inspect_default_repr(arg.default)
            if default_repr is not None:
                part += "=" + default_repr
            display_parts.append(part)
        return {
            "params": params,
            "varargs": varargs,
            "varkw": varkw,
            "signature": "(" + ", ".join(display_parts) + ")",
        }

    def _inspect_signature_metadata_for_call(self, expr: Expr) -> Optional[dict]:
        if not isinstance(expr, Call):
            return None
        if self._inspect_module_call_name(expr) != "signature":
            return None
        if len(expr.args) != 1:
            return None
        fd = self._inspect_funcdef_for_expr(expr.args[0])
        if fd is None:
            return None
        return self._inspect_signature_metadata_for_funcdef(fd)

    def _inspect_fullargspec_metadata_for_call(self, expr: Expr) -> Optional[dict]:
        if not isinstance(expr, Call):
            return None
        if self._inspect_module_call_name(expr) != "getfullargspec":
            return None
        if len(expr.args) != 1:
            return None
        fd = self._inspect_funcdef_for_expr(expr.args[0])
        if fd is None:
            return None
        return self._inspect_signature_metadata_for_funcdef(fd)

    def _emit_string_list(self, values: list[str], name: str) -> ir.Value:
        out = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(name),
        )
        for value in values:
            item = self._emit_str_literal(value)
            self.builder.call(self.runtime["py_list_append"], [out, item])
        return out

    def _emit_optional_string(self, value: Optional[str]) -> ir.Value:
        if value is None:
            return self._emit_none_literal()
        return self._emit_str_literal(value)

    def _maybe_emit_inspect_alias_attr(self, expr: Attr) -> Optional[ir.Value]:
        if not isinstance(expr.obj, Name):
            return None
        signature = getattr(self, "_inspect_signature_aliases", {}).get(expr.obj.ident)
        if signature is not None and expr.name == "parameters":
            return self._emit_string_list(
                list(signature.get("params", ())),
                "inspect.signature.parameters",
            )
        fullargspec = getattr(self, "_inspect_fullargspec_aliases", {}).get(
            expr.obj.ident
        )
        if fullargspec is not None:
            if expr.name == "args":
                return self._emit_string_list(
                    list(fullargspec.get("params", ())),
                    "inspect.fullargspec.args",
                )
            if expr.name == "varargs":
                return self._emit_optional_string(fullargspec.get("varargs"))
            if expr.name == "varkw":
                return self._emit_optional_string(fullargspec.get("varkw"))
        return None

    def _emit_inspect_getsource(self, expr: Call) -> Optional[ir.Value]:
        if len(expr.args) != 1:
            return None
        target = expr.args[0]
        fd = self._inspect_funcdef_for_expr(target)
        if fd is None:
            return None
        src_path = fd.span.file or target.span.file
        if not src_path:
            return self._emit_str_literal("")
        try:
            with open(src_path, "r", encoding="utf-8") as f:
                return self._emit_str_literal(f.read())
        except OSError:
            return self._emit_str_literal("")

    def _emit_inspect_getmro(self, expr: Call) -> Optional[ir.Value]:
        if len(expr.args) != 1:
            return None
        target = expr.args[0]
        if not isinstance(target, Name):
            return None
        info = self.class_lowering.classes.get(target.ident)
        if info is None:
            return None
        cls_obj = self.builder.load(
            info.global_var,
            name=self._fresh(f"inspect.mro.{target.ident}"),
        )
        return self.builder.call(
            self.runtime["py_obj_getattr"],
            [cls_obj, self._attr_name_ptr("__mro__")],
            name=self._fresh("inspect.mro"),
        )

    def _maybe_emit_native_inspect_call(self, expr: Call) -> Optional[ir.Value]:
        if len(expr.args) != 1:
            return None
        attr_name = self._inspect_module_call_name(expr)
        if attr_name is None:
            return None
        if attr_name == "signature":
            metadata = self._inspect_signature_metadata_for_call(expr)
            if metadata is None:
                return None
            return self._emit_str_literal(str(metadata.get("signature", "()")))
        if attr_name == "getfullargspec":
            metadata = self._inspect_fullargspec_metadata_for_call(expr)
            if metadata is None:
                return None
            return self._emit_none_literal()
        if attr_name == "getsource":
            return self._emit_inspect_getsource(expr)
        if attr_name == "getmro":
            return self._emit_inspect_getmro(expr)
        if attr_name in ("isfunction", "isclass", "ismethod"):
            target = expr.args[0]
            if attr_name == "isfunction":
                is_func = self._inspect_funcdef_for_expr(target) is not None
                if not is_func and isinstance(target, Attr):
                    export = self._native_module_object_export_info(
                        target.obj,
                        target.name,
                    )
                    is_func = export is not None and export[1].get("kind") == "function"
                return self.builder.call(
                    self.runtime["py_bool_from_bit"],
                    [
                        ir.Constant(
                            _I32,
                            1 if is_func else 0,
                        )
                    ],
                    name=self._fresh("inspect.isfunction"),
                )
            if attr_name == "isclass":
                is_cls = (
                    isinstance(target, Name)
                    and target.ident in self.class_lowering.classes
                )
                return self.builder.call(
                    self.runtime["py_bool_from_bit"],
                    [ir.Constant(_I32, 1 if is_cls else 0)],
                    name=self._fresh("inspect.isclass"),
                )
            is_method = isinstance(target, Attr) and not isinstance(target.obj, Name)
            return self.builder.call(
                self.runtime["py_bool_from_bit"],
                [ir.Constant(_I32, 1 if is_method else 0)],
                name=self._fresh("inspect.ismethod"),
            )
        if attr_name not in ("getdoc", "isfunction"):
            return None
        target = expr.args[0]
        if not isinstance(target, Attr):
            return None
        export = self._native_module_object_export_info(target.obj, target.name)
        if export is None:
            return None
        _module_name, info = export
        is_func = info.get("kind") == "function"
        if attr_name == "isfunction":
            return ir.Constant(_I1, 1 if is_func else 0)
        if not is_func:
            return self._emit_none_literal()
        return self._emit_str_literal(str(info.get("docstring") or ""))

    def _emit_native_module_constant(self, info: dict) -> ir.Value:
        value_kind = info.get("value_kind", info.get("kind"))
        if self._freestanding_module:
            if value_kind == "int" and type(info.get("value")) is int:
                return ir.Constant(_I64, int(info["value"]))
            raise NotImplementedError(
                "freestanding compile-time scaffolds only support exact "
                "integer constants"
            )
        if value_kind == "str":
            return self._emit_str_literal(str(info.get("value", "")))
        if value_kind == "bytes":
            raw = info.get("value", b"")
            if isinstance(raw, str):
                raw = raw.encode("latin-1")
            return self._emit_bytes_literal(bytes(raw))
        if value_kind == "int":
            constant = int(info.get("value", 0))
            # Same treatment an ordinary int literal gets: a small value
            # materializes as a tagged constant instead of a call, so no
            # allocation and no GC barriers.  Scaffold constants are almost
            # all struct offsets and type tags, used as `load_i64(obj,
            # SOME_OFFSET)` — the object form is then immediately unboxed to
            # satisfy an i64 parameter.  Recording the literal lets that
            # unboxing fold too, leaving a compile-time integer where two
            # runtime calls used to be.
            boxed = self._emit_int_literal_object(constant)
            marshal.note_boxed_i64_constant(boxed, constant)
            return boxed
        if value_kind == "bool":
            return self.builder.call(
                self.runtime["py_bool_from_bit"],
                [ir.Constant(_I32, 1 if info.get("value") else 0)],
                name=self._fresh("native.const.bool"),
            )
        if value_kind == "float":
            value = info.get("value", 0.0)
            return self.builder.call(
                self.runtime["py_float_from_f64"],
                [ir.Constant(_DOUBLE, float(value))],
                name=self._fresh("native.const.float"),
            )
        if value_kind == "none":
            return self._emit_none_literal()
        raise NotImplementedError(
            f"unsupported native module constant kind {value_kind!r}"
        )

    def _ensure_native_module_alias_class_export(
        self,
        alias_name: str,
        attr_name: str,
    ):
        """Declare ``alias.Class`` as an extern class on demand.

        Keep the local registration keyed by the leaf class name so
        existing ``isinstance(..., mod.Class)`` logic, which only looks
        at the tail token, can stay unchanged. If the current module
        already owns a different class with that same leaf name, fall
        back to the CPython path instead of silently rebinding it.
        """
        export = self._native_module_alias_export_info(alias_name, attr_name)
        if export is None:
            return None
        module_name, info = export
        if info.get("kind") != "class":
            return None
        owning_module = info.get("owning_module", module_name)
        export_name = info.get("export_name", attr_name)
        # First, try to find a class that exactly matches the expected global name.
        expected_global = (
            ".class."
            + self._module_symbol_suffix(owning_module)
            + "."
            + export_name
        )

        # We need to find if this class is already registered under ANY key.
        class_info = None
        for registered in self.class_lowering.classes.values():
            if registered.global_var.name == expected_global:
                class_info = registered
                break

        if class_info is not None:
            return class_info

        # Not found: declare it. The registry will now use a qualified key internally.
        class_info = self._declare_native_module_extern_class(
            owning_module=owning_module,
            class_name=attr_name,
            field_names=info["field_names"],
            methods=info["methods"],
            local_name=attr_name,
            base_names=info.get("base_names", ()),
            field_types=info.get("field_types", ()),
        )

        # Record the qualified name as the hint for this assignment target.
        if hasattr(self, "env_class_hint"):
            # The caller (_emit_attr) will use this return value to
            # generate an assignment. We need to tell the generator
            # to use the qualified name for the hint.
            pass

        return class_info

    def _declare_native_module_extern_class(
        self,
        *,
        owning_module: str,
        class_name: str,
        field_names: tuple,
        methods: tuple,
        local_name: str,
        base_names: tuple = (),
        field_types: tuple = (),
        _seen: tuple = (),
    ):
        if not isinstance(base_names, tuple):
            if isinstance(base_names, list):
                base_names = tuple(base_names)
            else:
                base_names = ()
        if owning_module == "pcc.frontends.python.py_ast" and class_name not in _seen:
            module_exports = None
            if self._native_module_exports is not None:
                module_exports = self._native_module_exports.get(owning_module)
            if module_exports is not None:
                next_seen = _seen + (class_name,)
                for base_name in base_names:
                    base_info = module_exports.get(base_name)
                    if (
                        not isinstance(base_info, dict)
                        or base_info.get("kind") != "class"
                    ):
                        continue
                    self._declare_native_module_extern_class(
                        owning_module=owning_module,
                        class_name=base_info.get("class_name", base_name),
                        field_names=base_info.get("field_names", ()),
                        methods=base_info.get("methods", ()),
                        local_name=base_name,
                        base_names=base_info.get("base_names", ()),
                        field_types=base_info.get("field_types", ()),
                        _seen=next_seen,
                    )
        class_lowering = self.class_lowering
        return class_lowering.declare_extern_class(
            owning_module=owning_module,
            class_name=class_name,
            field_names=field_names,
            methods=methods,
            local_name=local_name,
            field_types=field_types,
        )

    def _native_class_method_def(
        self,
        class_info,
        method_name: str,
    ) -> Optional[FuncDef]:
        if class_info is not None:
            for candidate_name, candidate_def in class_info.method_defs:
                if candidate_name == method_name:
                    return candidate_def
        if class_info is not None and class_info.expanded_cd is not None:
            for s in class_info.expanded_cd.body:
                if getattr(s, "name", None) == method_name:
                    return s
        if class_info is not None and class_info.extern_method_defs:
            synth = class_info.extern_method_defs.get(method_name)
            if synth is not None:
                return synth
        if class_info is None:
            return None
        for stmt in self.ast_module.body:
            if (
                getattr(stmt, "body", None) is not None
                and getattr(stmt, "name", None) == class_info.name
            ):
                for s in stmt.body:
                    if getattr(s, "name", None) == method_name:
                        return s
        return None

    def _emit_native_class_instantiate(
        self,
        class_name: str,
        args: tuple,
        expr=None,
    ) -> ir.Value:
        class_info = self.class_lowering.classes.get(class_name)
        if class_info is None:
            raise NotImplementedError(
                f"instantiation: class {class_name!r} not found in module"
            )
        init_info = class_info
        init_fn = class_info.init_fn
        if init_fn is None:
            init_fn = class_info.methods.get("__init__")
        if init_fn is None:
            inherited = self._resolve_method_mro(class_name, "__init__")
            if inherited is not None:
                init_info = inherited
                init_fn = inherited.init_fn
                if init_fn is None:
                    init_fn = inherited.methods.get("__init__")

        span = None if expr is None else expr.span
        sink = None if expr is None else self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("native.constructor.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            cls = self._new_slot_call_root("native.constructor.class")
            roots.append(cls)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            # The module-qualified class comes from this provider's global;
            # a caller local with the same short name is a different binding.
            self._slot_call_copy_source(cls, class_info.global_var, span=span)
            # This runtime producer has an actual NEW managed-object result.
            # Publish it before lease release, errors, initializer evaluation,
            # or any argument/default/finalizer cleanup can safepoint.
            self._slot_call_runtime_call(
                "py_instance_new", (cls,), result_slot=output, span=span,
            )
            if init_fn is not None:
                token = self.builder.call(
                    self.runtime["pcc_gc_foreign_lease_acquire"],
                    [self._as_gc_ptr(output)],
                    name=self._fresh("native.constructor.lease"),
                )
                self._slot_call_check_status(token, "constructor receiver lease", span)
                self._try_err_block = self._slot_call_cleanup_block(
                    tuple(roots), target, ((output, token),),
                )
                self._cpy_operand_cleanup_block = self._try_err_block
                receiver = self.builder.load(output, name=self._fresh("native.constructor.receiver"))
                # Keep the provider's declared physical ABI. In particular,
                # Python int and runtime machine-int parameters are different
                # even when both source annotations spell int. The counted
                # receiver lease survives this helper's legacy pin cleanup.
                self._emit_direct_method_call(
                    init_fn, receiver, init_info, "__init__", args,
                )
                released = self.builder.call(
                    self.runtime["pcc_gc_foreign_lease_release"],
                    [self._as_gc_ptr(output), token],
                    name=self._fresh("native.constructor.release"),
                )
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_check_status(released, "constructor receiver lease release", span)
            elif args:
                raise L1CodegenError("constructor without an initializer requires the class binder")
            self._release_slot_call_roots((cls,))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("native.constructor.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_no_init_field_instance(
        self,
        class_name: str,
        args: tuple,
        kwargs: tuple,
        *,
        force: bool = False,
        expr=None,
    ) -> ir.Value | None:
        info = self.class_lowering.classes.get(class_name)
        if info is None or info.valueclass:
            return None
        if info.init_fn is not None and not force:
            return None
        field_names = tuple(info.field_names)
        if not field_names:
            return None
        if len(args) > len(field_names):
            raise NotImplementedError(
                f"class {class_name!r} has no __init__ for extra "
                "positional arguments"
            )
        seen = set(field_names[:len(args)])
        for kw_name, _kw_expr in kwargs:
            if kw_name in seen:
                raise NotImplementedError(
                    f"class {class_name!r} got multiple values for "
                    f"field {kw_name!r}"
                )
            seen.add(kw_name)

        span = None if expr is None else expr.span
        sink = None if expr is None else self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("constructor.fields.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            # Keep the class lookup before operand evaluation. A local class
            # may have a live binding; an imported class has its provider's
            # authoritative global. Neither is an owned raw SSA pointer.
            cls = self._new_slot_call_root("constructor.fields.class")
            roots.append(cls)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if info.owning_module:
                source = (info.global_var, False)
            else:
                source = self._slot_call_name_source(
                    Name(span=span, ty=DynType(name="dyn"), ident=class_name),
                )
            if source is None:
                raise L1CodegenError("field constructor class has no authoritative source")
            self._slot_call_copy_source(cls, source[0], source[1], span)
            operands = []
            if expr is not None and expr.operand_order:
                for kind, index in expr.operand_order:
                    if kind == "kw":
                        operands.append(kwargs[index])
                    else:
                        operands.append((field_names[index], args[index]))
            else:
                for index, argument in enumerate(args):
                    operands.append((field_names[index], argument))
                operands.extend(kwargs)
            fields = []
            # Finish every source expression before allocation or a setter
            # callback. Object operands use their exact object projection,
            # including arbitrary-precision integers; raw pointers fail closed.
            for field_name, argument in operands:
                item = self._emit_slot_call_operand(argument, "constructor.fields.argument")
                fields.append((field_name, item))
                roots.append(item)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_instance_new", (cls,), result_slot=output, span=span,
            )
            for field_name, item in fields:
                self._slot_call_runtime_call(
                    "py_obj_setattr", (output, item),
                    suffix_args=(self._attr_name_ptr(field_name),),
                    argument_order=(0, 2, 1), span=span,
                )
            retained = tuple(roots[1:]) if sink is None else tuple(roots)
            self._release_slot_call_roots(retained)
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("constructor.fields.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _declare_extern_user_function(
        self,
        owning_module: str,
        func_name: str,
        info: dict,
        *,
        bind_name: Optional[str] = None,
    ) -> ir.Function:
        """Declare (or reuse) the extern symbol for a native sibling
        function export, optionally binding it under ``bind_name``."""
        owning_module = info.get("owning_module", owning_module)
        func_name = info.get("export_name", func_name)
        sanitised = self._module_symbol_suffix(owning_module)
        sym = f"user_{sanitised}_{func_name}"
        existing = self.module.globals.get(sym)
        if isinstance(existing, ir.Function):
            fn = existing
        else:
            box_int_abi = self._export_box_int_abi(info)
            param_tys = [
                self._abi_ir_type(
                    decode_type(t),
                    box_int_abi=box_int_abi,
                )
                for t in info["param_types"]
            ]
            fnty = ir.FunctionType(
                self._extern_user_function_return_ir_type(
                    info,
                    box_int_abi=box_int_abi,
                ),
                param_tys,
            )
            fn = ir.Function(self.module, fnty, name=sym)
            fn.linkage = "external"
        if (bool(info.get("manual_pointer_abi", False))
                and info.get("manual_pointer_abi_symbol", "") == sym):
            self._manual_pointer_abi_functions.add(sym)
        if bind_name is not None:
            self.functions[bind_name] = fn
            self._cross_module_func_defs[bind_name] = self._extern_info_to_funcdef(
                bind_name, info
            )
            if bool(info.get("may_park", False)):
                self._generator_func_names.add(bind_name)
            identity_decorators = self._cross_module_identity_decorators
            if info.get("identity_decorator"):
                identity_decorators[bind_name] = True
            else:
                identity_decorators.pop(bind_name, None)
        return fn

    def _emit_compiled_module_object_call(
        self,
        module_name: str,
        attr: Attr,
        expr: Call,
    ) -> ir.Value:
        """Call an export through its already-published native module object.

        This preserves Python call binding when a direct cross-module ABI call
        cannot supply omitted defaults.  The compiled function object's native
        signature binder owns those defaults, so this remains no-libpython.
        """
        result = self._emit_slot_call_object(
            expr, "compiled.module.call." + attr.name, module_name, attr.name,
        )
        self._note_owned_dynamic_call_value(result)
        return self._dynamic_export_call_value(expr, result)

    def _dynamic_export_call_value(self, expr: Call, result: ir.Value) -> ir.Value:
        """Give a dynamically called export its inferred type's representation.

        The call returns a NEW object reference, while inference may type it as
        a native scalar (``math.sqrt`` -> float) that every consumer reads
        unboxed: ``print(N.sqrt(4.0))`` boxed the object pointer as a double
        and the self IR verifier refused the program. Unbox here and release
        the reference the scalar no longer needs. An enclosing object slot
        keeps the boxed value, as for runtime-bound user calls.
        """
        ty = getattr(expr, "ty", None)
        if (ty is None or not isinstance(result.type, ir.PointerType)
                or self._slot_call_result_sink(expr) is not None
                or not self._is_native_scalar_type(ty)
                or (isinstance(ty, IntType) and self._int_exprs_are_boxed())):
            return result
        value = self._coerce_from_object(result, ty)
        self._owned_dynamic_call_values.discard(result)
        self._gc_release(result, self._release_context_label("dynamic-export-call"))
        return value

    def _maybe_emit_native_module_alias_call(self, expr: Call) -> Optional[ir.Value]:
        """Lower ``submod.fn(...)`` when ``submod`` was imported from a
        native sibling module and ``fn`` is one of that submodule's
        exported top-level functions."""
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        # A call builtin dispatch owns although the module also has a
        # compiled provider -- a live import binding -- never reaches the
        # provider's definition (pipeline_import_policy).
        if isinstance(attr.obj, Name):
            builtin_module = self._native_builtin_module_for_name(attr.obj.ident)
            if (builtin_module is not None
                    and native_builtin_dispatch_owns(builtin_module, attr.name)):
                owned = self._emit_native_builtin_module_call(expr)
                if owned is not None:
                    return owned
        if live_import_expr_binding(self, attr.obj):
            # Imported computed values keep a dynamic type and a live binding.
            # Reuse ordinary dictionary method guards before materializing the
            # callable: they load that binding once and preserve normal method
            # lookup for a replacement or receiver that is not an exact dict.
            if isinstance(attr.obj.ty, DynType):
                native = self._maybe_emit_dict_method_via_dyn(expr)
                if native is not None:
                    return native
            return self._dynamic_export_call_value(
                expr, self._emit_slot_call_object(expr, "import.method." + attr.name),
            )
        export = self._native_module_expr_export_info(attr.obj, attr.name)
        if export is None:
            module_name = None
            if isinstance(attr.obj, Name):
                module_name = self._native_module_aliases.get(attr.obj.ident)
                if module_name is None:
                    module_name = self._native_module_object_for_name(attr.obj.ident)
            if module_name is None:
                return None
            return self._emit_compiled_module_object_call(
                module_name,
                attr,
                expr,
            )
        module_name, info = export
        kind = info.get("kind")
        if kind == "function":
            if info.get("semantic_decorator"):
                return self._emit_compiled_module_object_call(module_name, attr, expr)
            fn = self._declare_extern_user_function(
                module_name,
                attr.name,
                info,
            )
            ast_func_def = self._extern_info_to_funcdef(attr.name, info)
            if ast_func_def is None:
                return None
            if self._ordinary_call_needs_runtime_binding(
                expr, ast_func_def, False, self._slot_call_result_sink(expr) is not None,
            ):
                return self._emit_runtime_bound_user_call(expr, attr.name, fn)
            if self._call_would_use_callee_defaults(
                expr.args,
                expr.kwargs,
                ast_func_def.args,
            ):
                # Cross-module default expressions live in the callee's
                # module scope.  Invoke the published pcc-native function
                # object so its signature binder supplies them.  Falling
                # through here used to import the same compiled module via
                # CPython and broke strict no-libpython mode.
                return self._emit_compiled_module_object_call(
                    module_name,
                    attr,
                    expr,
                )
            if (
                bool(info.get("may_park", False))
                and len(getattr(self, "_generator_ctx_stack", ())) > 0
            ):
                effect_name = module_name + "." + attr.name
                return emit_generator_may_park_call(
                    self,
                    expr,
                    effect_name,
                    fn=fn,
                    ast_func_def=ast_func_def,
                    effect_proven=True,
                )
            if (bool(info.get("may_park", False))
                    and self._funcdef_is_continuation_factory(self.current_func_def)):
                raise L1CodegenError("factory methods must defer parking calls with continuation()")
            if ast_func_def.is_async:
                return self._emit_async_user_function_call(
                    attr.name,
                    fn,
                    ast_func_def,
                    expr.args,
                    expr.kwargs,
                )
            return self._emit_direct_user_function_call(
                display_name=attr.name,
                fn=fn,
                ast_func_def=ast_func_def,
                args=expr.args,
                kwargs=expr.kwargs,
            )
        if kind != "class":
            return None
        if not isinstance(attr.obj, Name):
            return None
        class_info = self._ensure_native_module_alias_class_export(
            attr.obj.ident,
            attr.name,
        )
        if class_info is None:
            return None
        if str(
            os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE", "") or ""
        ).strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            import sys

            sys.stderr.write(
                "debug: native_module_alias_class_call module="
                + module_name
                + " attr="
                + attr.name
                + " class_info="
                + str(class_info.name)
                + " args_len="
                + str(len(expr.args))
                + "\n"
            )
        if (
            module_name == "pcc.frontends.python.py_ast"
            and len(expr.args) + len(expr.kwargs)
            >= len(tuple(class_info.field_names))
        ):
            inst = self._emit_no_init_field_instance(
                class_info.name,
                expr.args,
                expr.kwargs,
                force=True,
                expr=expr,
            )
            if inst is not None:
                return inst
        new_owner = self._resolve_method_mro(class_info.name, "__new__")
        if new_owner is None and "__new__" in class_info.methods:
            new_owner = class_info
        if new_owner is not None:
            if class_info.valueclass:
                # Explicit payload constructors keep their existing ABI owner.
                return self.class_lowering.emit_instantiate(class_info.name, expr.args, self, kwargs=expr.kwargs)
            return self._emit_compiled_module_object_call(module_name, attr, expr)
        init_fd = self._native_class_method_def(class_info, "__init__")
        if (not class_info.valueclass
                and self._ordinary_call_needs_runtime_binding(expr, init_fd, True)):
            return self._emit_compiled_module_object_call(module_name, attr, expr)
        if (init_fd is not None
                and self.class_lowering.has_definition_defaults(class_info, "__init__")
                and self._call_would_use_callee_defaults(expr.args, expr.kwargs, init_fd.args[1:])):
            # Defaults belong to the defining class's published signature.
            # In particular, field factories run once per omitted argument;
            # shared default objects must never be re-evaluated in the caller.
            return self._emit_compiled_module_object_call(module_name, attr, expr)
        if (expr.kwargs or self._has_starred_unpack(expr.args)
                or self._split_starstar_kwargs_unpack(expr.args) is not None):
            # Preserve the Python call shape and source evaluation order.
            # Flattening keywords here both loses keyword-only provenance
            # and evaluates expressions in formal order. The published
            # native class binder owns binding exactly once, including
            # variadic operands and duplicate/positional-only diagnostics.
            return self._emit_compiled_module_object_call(module_name, attr, expr)
        if init_fd is None:
            inst = self._emit_no_init_field_instance(
                class_info.name,
                expr.args,
                expr.kwargs,
                expr=expr,
            )
            if inst is not None:
                return inst
            if expr.args:
                return self._emit_compiled_module_object_call(module_name, attr, expr)
        return self._emit_native_class_instantiate(
            class_info.name,
            expr.args,
            expr=expr,
        )

    def _maybe_emit_native_builtin_compiled_call(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """Use a recursively compiled stdlib export as the final native path.

        Dedicated module lowerings remain authoritative because callers invoke
        this only after those lowerings decline the expression. The temporary
        alias lets the existing cross-module ABI path perform the actual call
        without duplicating its function/class/default handling.
        """
        attr = expr.func
        if not isinstance(attr, Attr) or not isinstance(attr.obj, Name):
            return None
        alias_name = attr.obj.ident
        export = self._native_builtin_compiled_export_info(alias_name, attr.name)
        if export is None:
            return None
        module_name, _info = export
        aliases = self._native_module_aliases
        had_old = alias_name in aliases
        old_module = aliases.get(alias_name)
        aliases[alias_name] = module_name
        try:
            return self._maybe_emit_native_module_alias_call(expr)
        finally:
            if had_old:
                aliases[alias_name] = old_module
            else:
                aliases.pop(alias_name, None)


__all__ = ["NativeModuleAliasMixin"]
