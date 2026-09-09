"""pcc.unsafe intrinsic lowering helpers for L1CodeGen."""
from __future__ import annotations

import platform
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir

from .freestanding_abi_constants import ABI_CONSTANTS

from ..py_ast import (
    Assign,
    AugAssign,
    BinOp,
    ClassDef,
    DynType,
    BoolType,
    Call,
    Expr,
    FloatType,
    FuncDef,
    Global,
    IntLit,
    IntType,
    Name,
    Stmt,
    StrLit,
    UnaryOp,
)


_VOID = ir.VoidType()
_I8 = ir.IntType(8)
_I16 = ir.IntType(16)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = _I8.as_pointer()


UNSAFE_INTRINSICS = frozenset(
    {
        "malloc",
        "cstr",
        "global_addr",
        "function_addr",
        "global_load_ptr",
        "global_store_ptr",
        "abi_constant",
        "define_global_i8",
        "define_global_i32",
        "define_global_i64",
        "define_global_header",
        "define_global_ptr_null",
        "define_thread_local_ptr_null",
        "define_thread_local_i32",
        "define_global_ptr_to_global",
        "define_global_cstr",
        "define_global_ptr_array",
        "define_global_null_ptr_array",
        "define_global_i32_array",
        "define_global_i64_array",
        "define_global_struct_words",
        "calloc",
        "realloc",
        "free",
        "ptr_add",
        "ptr_diff",
        "int_to_ptr",
        "ptr_to_int",
        "wrapping_mul_i64",
        "logical_shift_right_i64",
        "logical_shift_left_i64",
        "unsigned_div_i64",
        "unsigned_rem_i64",
        "unsigned_greater_i64",
        "mul_overflow_i64",
        "float_to_i64",
        "i64_to_float",
        "f64_div",
        "f64_signbit",
        "f64_bits",
        "f64_pair_make",
        "f64_pair_first",
        "f64_pair_second",
        "null",
        "ptr_eq",
        "ptr_is_null",
        "is_tagged_int",
        "tag_int",
        "untag_int",
        "load_i64",
        "load_i64x4",
        "load_i64x4_strided",
        "load_i32",
        "load_i8",
        "load_ptr",
        "load_f64",
        "store_i64",
        "store_i32",
        "store_i8",
        "store_ptr",
        "store_f64",
        "memset",
        "memcpy",
        "memmove",
        "write",
        "read",
        "close",
        "seek_file",
        "open_readonly",
        "darwin_current_rss_bytes",
        "darwin_peak_rss_bytes",
        "open_file",
        "rename_file",
        "chmod_file",
        "sync_file",
        "socket_open",
        "socket_connect",
        "socket_bind",
        "socket_listen",
        "socket_setsockopt",
        "socket_getsockopt",
        "fd_control",
        "eventfd_create",
        "socket_send",
        "socket_recv",
        "socket_accept",
        "socket_shutdown",
        "socket_sockname",
        "socket_peername",
        "poll_fd",
        "poll_readable_pair",
        "getpid",
        "getcwd",
        "readlink",
        "mkdir",
        "unlinkat",
        "uname",
        "uname_field",
        "cpu_query",
        "clock_gettime",
        "nanosleep",
        "waitpid",
        "kill",
        "process_exit",
        "spawn_process",
        "spawn_process_pipe",
        "stack_alloc",
        "strlen",
        "getenv",
        "setenv",
        "unsetenv",
        "initial_environ",
        "access",
        "stat_kind",
        "stat_mtime",
        "target_sys_platform",
        "target_platform_machine",
        "darwin_errno_location",
        "call_ptr1",
        "call_void_ptr0",
        "call_ptr0",
        "call_void_ptr1",
        "call_void_ptr2",
        "call_void_ptr_i64_ptr",
        "call_ptr2",
        "call_ptr_ptr_i64",
        "call_ptr_i64_i64",
        "call_ptr_ptr_ptr_i64_ptr",
        "call_ptr_ptr_ptr_i32",
        "call_ptr4",
        "call_ptr3",
        "call_i64_i64",
        "call_i64_i64_ptr",
        "call_i32_ptr1",
        "call_i32_ptr_i64",
        "call_i32_ptr_i32",
        "call_i32_ptr_i32_i32",
        "call_i32_ptr_i32_i32_i32",
        "call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32",
        "call_i32_i32_ptr_i64",
        "call_i32_i64_i64_ptr",
        "call_i32_i64_i32_i64",
        "call_i64_ptr1",
        "call_i64_ptr_i64",
        "call_i64_ptr4_i64_i64",
        "call_i64_ptr3_i64_i64_i64",
        "call_i64_ptr_ptr_ptr_i64",
        "call_i64_ptr2",
        "call_i64_ptr3",
        "call_i64_i64_i64_ptr",
        "call_i64_ptr_i64_ptr",
        "call_i64_ptr_i64_i64",
        "call_variadic_i64_ptr_i64_ptr",
        "call_variadic_i64_ptr_i64_i64",
        "call_variadic_i32_ptr_i32_ptr",
        "call_variadic_i32_ptr_i32_i64",
        "call_i64_ptr_i64_ptr_i64",
        "call_i64_ptr_i64_i64_ptr",
        "call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool",
        "call_i64_ptr_ptr_ptr_ptr_ptr_bool",
        "dynamic_library_open",
        "dynamic_library_open_global",
        "dynamic_library_symbol",
        "darwin_libsystem_symbol",
        "dynamic_library_close",
        "kqueue_create",
        "kevent_call",
        "epoll_create1",
        "epoll_ctl",
        "epoll_wait",
        "thread_safepoint",
        "gc_backend_current",
        "atomic_load_i32",
        "atomic_load_i64",
        "atomic_store_i32",
        "atomic_store_i64",
        "atomic_rmw_i32",
        "atomic_rmw_i64",
        "atomic_cas_i32",
        "atomic_cas_i64",
        "atomic_fence",
        "atomic_test_and_set",
        "atomic_clear",
        "syscall6",
        "page_alloc",
        "page_free",
        "va_start",
        "va_arg_i64",
        "va_arg_i32",
        "va_arg_u32",
        "va_arg_ptr",
        "va_arg_f64",
        "va_cursor",
        "va_end",
    }
)


# pcc.unsafe spells orderings the way musl/GCC __atomic_* does; LLVM spells
# relaxed as monotonic. Every atomic intrinsic requires a literal from this
# table — a dynamic ordering would defeat the point of stating it in source.
_ATOMIC_ORDER_TO_LLVM = {
    "relaxed": "monotonic",
    "acquire": "acquire",
    "release": "release",
    "acq_rel": "acq_rel",
    "seq_cst": "seq_cst",
}

_ATOMIC_RMW_OPS = ("add", "sub", "and", "or", "xchg")


# Keep the giant unsafe dispatcher split at Python function boundaries.
# The self-backend IR splitter cannot divide one LLVM function, so each
# family stays below the residual oversized-worker threshold.  Tuple
# membership uses equality rather than native string hashes.
_UNSAFE_INTRINSIC_FAMILIES = (
    (
        'va_start',
        'va_arg_i64',
        'va_arg_i32',
        'va_arg_u32',
        'va_arg_ptr',
        'va_arg_f64',
        'va_cursor',
        'va_end',
        'wrapping_mul_i64',
        'logical_shift_right_i64',
        'logical_shift_left_i64',
        'unsigned_div_i64',
        'unsigned_rem_i64',
        'unsigned_greater_i64',
        'mul_overflow_i64',
        'float_to_i64',
        'i64_to_float',
        'f64_div',
        'f64_signbit',
        'f64_bits',
        'f64_pair_make',
        'f64_pair_first',
        'f64_pair_second',
    ),
    (
        'page_alloc',
        'page_free',
        'syscall6',
        'cstr',
        'target_sys_platform',
        'target_platform_machine',
        'darwin_errno_location',
        'abi_constant',
        'global_addr',
        'function_addr',
        'int_to_ptr',
        'ptr_to_int',
        'global_load_ptr',
        'global_store_ptr',
        'malloc',
        'calloc',
        'realloc',
        'free',
        'ptr_add',
        'ptr_diff',
    ),
    (
        'null',
        'ptr_eq',
        'ptr_is_null',
        'is_tagged_int',
        'tag_int',
        'untag_int',
        'load_i64',
        'load_i64x4',
        'load_i64x4_strided',
        'load_i32',
        'load_i8',
        'load_ptr',
        'load_f64',
        'store_i64',
        'store_i32',
        'store_i8',
        'store_ptr',
        'store_f64',
        'memset',
        'memcpy',
        'memmove',
    ),
    (
        'read',
        'write',
        'close',
        'seek_file',
        'open_readonly',
        'darwin_current_rss_bytes',
        'darwin_peak_rss_bytes',
    ),
    (
        'open_file',
        'rename_file',
        'chmod_file',
        'sync_file',
    ),
    (
        'socket_open',
        'socket_connect',
        'socket_bind',
        'socket_listen',
        'socket_setsockopt',
        'socket_getsockopt',
        'fd_control',
        'eventfd_create',
    ),
    (
        'socket_send',
        'socket_recv',
        'socket_accept',
        'socket_shutdown',
        'socket_sockname',
        'socket_peername',
        'poll_fd',
        'poll_readable_pair',
    ),
    (
        'getpid',
        'getcwd',
        'readlink',
        'mkdir',
        'unlinkat',
        'uname',
        'uname_field',
        'cpu_query',
    ),
    (
        'clock_gettime',
        'nanosleep',
        'waitpid',
        'kill',
        'process_exit',
    ),
    (
        'spawn_process_pipe',
    ),
    (
        'spawn_process',
    ),
    (
        'stack_alloc',
        'strlen',
        'getenv',
        'setenv',
        'unsetenv',
        'initial_environ',
        'access',
        'stat_kind',
        'stat_mtime',
    ),
    (
        'call_ptr1',
        'call_ptr0',
        'call_void_ptr0',
        'call_void_ptr1',
        'call_void_ptr2',
        'call_void_ptr_i64_ptr',
        'call_ptr2',
        'call_ptr_ptr_i64',
        'call_ptr_ptr_ptr_i64_ptr',
        'call_ptr_ptr_ptr_i32',
        'call_ptr3',
        'call_ptr4',
        'call_i64_i64',
        'call_i64_i64_ptr',
        'call_i32_ptr1',
        'call_i32_ptr_i64',
        'call_i32_ptr_i32',
        'call_i32_ptr_i32_i32',
        'call_i32_ptr_i32_i32_i32',
        'call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32',
        'call_i32_i32_ptr_i64',
        'call_i32_i64_i64_ptr',
        'call_i32_i64_i32_i64',
    ),
    (
        'call_i64_ptr1',
        'call_i64_ptr_ptr_ptr_i64',
        'call_i64_ptr_i64',
        'call_i64_ptr2',
        'call_i64_ptr4_i64_i64',
        'call_i64_ptr3_i64_i64_i64',
        'call_ptr_i64_i64',
        'call_i64_ptr3',
        'call_i64_i64_i64_ptr',
        'call_variadic_i64_ptr_i64_ptr',
        'call_variadic_i64_ptr_i64_i64',
        'call_variadic_i32_ptr_i32_ptr',
        'call_variadic_i32_ptr_i32_i64',
        'call_i64_ptr_i64_ptr',
        'call_i64_ptr_i64_i64',
        'call_i64_ptr_i64_ptr_i64',
        'call_i64_ptr_i64_i64_ptr',
        'call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool',
        'call_i64_ptr_ptr_ptr_ptr_ptr_bool',
    ),
    (
        'dynamic_library_open',
        'dynamic_library_open_global',
        'dynamic_library_symbol',
        'darwin_libsystem_symbol',
        'dynamic_library_close',
        'kqueue_create',
        'kevent_call',
        'epoll_create1',
        'epoll_ctl',
        'epoll_wait',
        'thread_safepoint',
        'gc_backend_current',
    ),
)


def _unsafe_intrinsic_family(intrinsic: str) -> int:
    family = 0
    while family < len(_UNSAFE_INTRINSIC_FAMILIES):
        if intrinsic in _UNSAFE_INTRINSIC_FAMILIES[family]:
            return family
        family += 1
    return -1


class UnsafeIntrinsicMixin:
    _UNSAFE_INTRINSICS = UNSAFE_INTRINSICS

    def _is_unsafe_intrinsic(self, name: str) -> bool:
        return name in self._UNSAFE_INTRINSICS

    def _unsafe_intrinsic_for_name(self, name: str) -> Optional[str]:
        if name in self.env:
            return None
        if name in getattr(self, "_module_globals", {}):
            return None
        return getattr(self, "_unsafe_bindings", {}).get(name)

    def _unsafe_void_result(self) -> ir.Value:
        """Represent a side-effect-only intrinsic without escaping raw mode."""
        if getattr(self, "_freestanding_module", False):
            return ir.Constant(_CSTR, None)
        return self._emit_none_literal()

    def _unsafe_expect_arity(
        self,
        intrinsic: str,
        expr: Call,
        n_args: int,
    ) -> None:
        if expr.kwargs or len(expr.args) != n_args:
            raise NotImplementedError(
                f"pcc.unsafe.{intrinsic} expects {n_args} positional args"
            )

    def _unsafe_dynamic_library_target_matches(
        self,
        intrinsic: str,
        expr: Call,
        n_args: int,
    ) -> bool:
        """Validate an optional compile-time target-platform guard.

        Unguarded calls retain the cross-platform dynamic-loader contract.
        A guarded call is emitted only when its literal platform matches the
        current compilation target; a mismatch has no loader dependency.
        """
        if expr.kwargs or len(expr.args) not in {n_args, n_args + 1}:
            raise NotImplementedError(
                f"pcc.unsafe.{intrinsic} expects {n_args} positional args "
                "and one optional target-platform literal"
            )
        if len(expr.args) == n_args:
            return True
        platform_expr = expr.args[n_args]
        if not isinstance(platform_expr, StrLit) or platform_expr.value not in {
            "darwin",
            "freebsd",
            "linux",
            "win32",
        }:
            raise NotImplementedError(
                f"pcc.unsafe.{intrinsic} target platform must be a supported "
                "string literal"
            )
        return platform_expr.value == self._target_sys_platform_text()

    def _raw_addresses_are_ints(self) -> bool:
        """Application modules type raw addresses as ``int``.

        Freestanding kernels and runtime ports (``__pcc_runtime_port__``)
        keep raw pointers in the pointer lane because they build objects out
        of raw memory; see ``pipeline_freestanding``.
        """
        return not (
            getattr(self, "_freestanding_module", False)
            or getattr(self, "_runtime_port_module", False)
        )

    def _unsafe_ptr_arg(self, expr: Expr) -> ir.Value:
        ptr = self._emit_expr(expr)
        if isinstance(ptr.type, ir.PointerType):
            if not self._ir_type_matches(ptr.type, _CSTR):
                ptr = self.builder.bitcast(
                    ptr,
                    _CSTR,
                    name=self._fresh("unsafe.ptr.cast"),
                )
            return ptr
        # Raw addresses are ``int`` outside runtime-port mode (see
        # type_infer._POINTER_INTRINSICS); an integer-typed operand is the
        # address itself.  Float/bool operands remain a compile-time error.
        if isinstance(ptr.type, ir.IntType):
            expr_ty = getattr(expr, "ty", None)
            if isinstance(expr_ty, (FloatType, BoolType)):
                raise NotImplementedError(
                    "pcc.unsafe pointer argument must be a pointer or address value"
                )
            if ptr.type.width == 64:
                return self.builder.inttoptr(ptr, _CSTR)
            if ptr.type.width < 64:
                return self.builder.inttoptr(self.builder.zext(ptr, _I64), _CSTR)
            return self.builder.inttoptr(self.builder.trunc(ptr, _I64), _CSTR)
        raise NotImplementedError(
            "pcc.unsafe pointer argument must be a pointer-typed value"
        )

    def _unsafe_address_arg(self, expr: Expr) -> ir.Value:
        """Base-address operand of a raw-memory intrinsic (load/store/atomic/
        memcpy/free/ptr_add).

        Outside pointer-lane modules a raw address is an ``int``; a dynamic
        value (an untyped helper parameter, a container element) may carry it
        as a tagged small int.  A tagged value can never be a valid memory
        address, so it is untagged at run time.  The result is rebuilt from
        integer bits with ``inttoptr`` so the precise stack-map analysis sees a
        raw address, never a managed SSA value of ambiguous provenance; a real
        object pointer round-trips through the same bits unchanged.  Value
        operands (``is_tagged_int``, ``ptr_eq``, ``store_ptr`` payloads, object
        arguments) must NOT use this: for them a tagged small int is the value
        itself.
        """
        ptr = self._unsafe_ptr_arg(expr)
        expr_ty = getattr(expr, "ty", None)
        if not (isinstance(expr_ty, DynType) and self._raw_addresses_are_ints()):
            return ptr
        bits = self.builder.ptrtoint(ptr, _I64, name=self._fresh("unsafe.dyn.bits"))
        tag = self.builder.and_(bits, ir.Constant(_I64, 1), name=self._fresh("unsafe.dyn.tag"))
        is_tagged = self.builder.icmp_unsigned(
            "!=", tag, ir.Constant(_I64, 0), name=self._fresh("unsafe.dyn.is_tagged")
        )
        untagged = self.builder.ashr(bits, ir.Constant(_I64, 1), name=self._fresh("unsafe.dyn.untag"))
        address_bits = self.builder.select(
            is_tagged, untagged, bits, name=self._fresh("unsafe.dyn.addr.bits")
        )
        return self.builder.inttoptr(address_bits, _CSTR, name=self._fresh("unsafe.dyn.addr"))

    def _unsafe_i64_arg(self, expr: Expr) -> ir.Value:
        return self._to_int64(self._emit_expr(expr), expr.ty)

    def _unsafe_i32_arg(self, expr: Expr) -> ir.Value:
        return self.builder.trunc(
            self._unsafe_i64_arg(expr),
            _I32,
            name=self._fresh("unsafe.i64.to.i32"),
        )

    def _unsafe_darwin_errno_result(
        self, raw: ir.Value, label: str
    ) -> ir.Value:
        """Normalize a Darwin libc -1 result to the shared negative errno ABI."""
        if isinstance(raw.type, ir.IntType) and raw.type.width < 64:
            raw = self.builder.sext(
                raw, _I64, name=self._fresh("unsafe." + label + ".raw")
            )
        error_fn = self._declare_external_function("__error", _CSTR, [])
        error_ptr = self.builder.call(
            error_fn, [], name=self._fresh("unsafe." + label + ".errno_ptr")
        )
        typed_error_ptr = self.builder.bitcast(
            error_ptr,
            _I32.as_pointer(),
            name=self._fresh("unsafe." + label + ".errno_typed"),
        )
        error_i32 = self.builder.load(
            typed_error_ptr, name=self._fresh("unsafe." + label + ".errno_i32")
        )
        error = self.builder.sext(
            error_i32, _I64, name=self._fresh("unsafe." + label + ".errno")
        )
        negative_error = self.builder.sub(
            ir.Constant(_I64, 0),
            error,
            name=self._fresh("unsafe." + label + ".negative_errno"),
        )
        failed = self.builder.icmp_signed(
            "<",
            raw,
            ir.Constant(_I64, 0),
            name=self._fresh("unsafe." + label + ".failed"),
        )
        return self.builder.select(
            failed,
            negative_error,
            raw,
            name=self._fresh("unsafe." + label + ".result"),
        )

    def _unsafe_f64_arg(self, expr: Expr) -> ir.Value:
        return self._to_double(self._emit_expr(expr), expr.ty)

    def _unsafe_f64_pair_arg(self, expr: Expr) -> ir.Value:
        value = self._emit_expr(expr)
        value_type = value.type
        if not isinstance(value_type, ir.LiteralStructType):
            raise NotImplementedError(
                "pcc.unsafe f64-pair argument must be a {f64,f64} aggregate"
            )
        elements = value_type.elements
        if (
            len(elements) != 2
            or not isinstance(elements[0], ir.DoubleType)
            or not isinstance(elements[1], ir.DoubleType)
        ):
            raise NotImplementedError(
                "pcc.unsafe f64-pair argument must be a {f64,f64} aggregate"
            )
        return value

    def _unsafe_cstr_literal_arg(self, expr: Expr) -> ir.Value:
        if not isinstance(expr, StrLit):
            raise NotImplementedError("pcc.unsafe.cstr only accepts a string literal")
        gv = self._cstr_global(
            expr.value,
            self._fresh(".unsafe.cstr"),
        )
        return self._ptr_to_cstr(gv)

    def _unsafe_symbol_literal_arg(self, expr: Expr) -> str:
        if not isinstance(expr, StrLit):
            raise NotImplementedError(
                "pcc.unsafe global intrinsics require a string literal symbol"
            )
        symbol = expr.value
        if not symbol:
            raise NotImplementedError("empty external symbol name")

        def is_alpha_code(c: int) -> bool:
            return (97 <= c <= 122) or (65 <= c <= 90)

        def is_alnum_code(c: int) -> bool:
            return is_alpha_code(c) or (48 <= c <= 57)

        first = ord(symbol[0])
        if not (first == 95 or is_alpha_code(first)):
            raise NotImplementedError(f"invalid external symbol {symbol!r}")
        for ch in symbol[1:]:
            c = ord(ch)
            if not (c == 95 or is_alnum_code(c)):
                raise NotImplementedError(f"invalid external symbol {symbol!r}")
        return symbol

    def _unsafe_const_i64_arg(self, expr: Expr) -> int:
        if isinstance(expr, IntLit):
            return int(expr.value)
        if (
            isinstance(expr, UnaryOp)
            and expr.op == "-"
            and isinstance(expr.operand, IntLit)
        ):
            return -int(expr.operand.value)
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and self._unsafe_intrinsic_for_name(expr.func.ident) == "abi_constant"
        ):
            return self._unsafe_abi_constant_value(expr)
        if isinstance(expr, Name):
            info = getattr(
                self,
                "_native_module_constant_bindings",
                {},
            ).get(expr.ident)
            if (
                isinstance(info, dict)
                and info.get("kind") == "constant"
                and info.get("value_kind") in {"int", "bool"}
            ):
                value = info.get("value")
                if type(value) is int or type(value) is bool:
                    return int(value)
            module_value = self._module_scope_int_literal_binding(expr.ident)
            if module_value is not None:
                return module_value
        if isinstance(expr, BinOp) and expr.op in ("+", "-", "*", "//", "%", "<<", ">>", "&", "|", "^"):
            # Constant arithmetic over statically known integers, e.g.
            # ``stack_alloc(_MAX_NAME + 1)``.  Each operand must itself be a
            # constant by the rules above; division/modulo by zero and shifts
            # by negative counts fail closed like any non-constant operand.
            lhs = self._unsafe_const_i64_arg(expr.lhs)
            rhs = self._unsafe_const_i64_arg(expr.rhs)
            op = expr.op
            if op == "+":
                return lhs + rhs
            if op == "-":
                return lhs - rhs
            if op == "*":
                return lhs * rhs
            if op == "//" and rhs != 0:
                return lhs // rhs
            if op == "%" and rhs != 0:
                return lhs % rhs
            if op == "<<" and 0 <= rhs < 64:
                return lhs << rhs
            if op == ">>" and 0 <= rhs < 64:
                return lhs >> rhs
            if op == "&":
                return lhs & rhs
            if op == "|":
                return lhs | rhs
            if op == "^":
                return lhs ^ rhs
        raise NotImplementedError(
            "pcc.unsafe global definition intrinsics require integer literals "
            "or statically imported integer constants"
        )

    def _module_scope_int_literal_binding(self, ident: str):
        """Value of a module-scope ``NAME = <int literal>`` binding.

        Accepted only when the module assigns ``NAME`` exactly once, at module
        scope, to an integer literal (optionally negated), never augments it,
        and no function or class body declares it ``global``.  Anything else
        fails closed so a rebinding can never be folded away.  This lets an
        out-of-tree package use its own size constants for ``stack_alloc`` and
        the global-definition intrinsics the way pcc-owned modules do through
        their native export tables.
        """
        body = getattr(self, "_ast_body", None)
        if body is None:
            module = getattr(self, "ast_module", None)
            body = getattr(module, "body", None)
        if not body:
            return None
        value = None
        bindings = 0
        for stmt in body:
            if isinstance(stmt, Assign):
                for target in stmt.targets:
                    if isinstance(target, Name) and target.ident == ident:
                        bindings += 1
                        literal = stmt.value
                        if isinstance(literal, IntLit):
                            value = int(literal.value)
                        elif (
                            isinstance(literal, UnaryOp)
                            and literal.op == "-"
                            and isinstance(literal.operand, IntLit)
                        ):
                            value = -int(literal.operand.value)
                        else:
                            value = None
            elif isinstance(stmt, AugAssign):
                target = stmt.target
                if isinstance(target, Name) and target.ident == ident:
                    return None
        if bindings != 1 or value is None:
            return None
        # A ``global NAME`` anywhere in the module means the binding is not
        # constant.  Walk nested bodies iteratively (no closure conversion
        # needed under self-host).
        stack = list(body)
        while stack:
            node = stack.pop()
            if isinstance(node, Global):
                if ident in tuple(getattr(node, "names", ())):
                    return None
                continue
            if isinstance(node, (FuncDef, ClassDef)):
                stack.extend(getattr(node, "body", ()))
                continue
            for child_name in ("body", "orelse", "finalbody"):
                children = getattr(node, child_name, None)
                if isinstance(children, (list, tuple)):
                    for child in children:
                        if isinstance(child, Stmt):
                            stack.append(child)
            handlers = getattr(node, "handlers", None)
            if isinstance(handlers, (list, tuple)):
                for handler in handlers:
                    for child in getattr(handler, "body", ()):
                        if isinstance(child, Stmt):
                            stack.append(child)
        return value

    def _unsafe_abi_constant_value(self, expr: Call) -> int:
        self._unsafe_expect_arity("abi_constant", expr, 1)
        name_expr = expr.args[0]
        if not isinstance(name_expr, StrLit):
            raise NotImplementedError(
                "pcc.unsafe.abi_constant requires a string literal"
            )
        if name_expr.value not in ABI_CONSTANTS:
            raise NotImplementedError(
                "unknown freestanding ABI constant " + repr(name_expr.value)
            )
        return int(ABI_CONSTANTS[name_expr.value])

    def _define_global(
        self,
        name: str,
        value_ty: ir.Type,
        initializer: ir.Constant,
    ) -> ir.GlobalVariable:
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.GlobalVariable):
            gv = existing
            gv.linkage = ""
        elif existing is None:
            gv = ir.GlobalVariable(self.module, value_ty, name=name)
        else:
            raise NotImplementedError(f"{name!r} already declared as non-global")
        gv.initializer = initializer
        return gv

    def _get_or_declare_global_for_initializer(
        self,
        name: str,
    ) -> ir.GlobalVariable:
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.GlobalVariable):
            return existing
        if existing is not None:
            raise NotImplementedError(f"{name!r} already declared as non-global")
        gv = ir.GlobalVariable(self.module, _I8, name=name)
        gv.linkage = "external"
        return gv

    def _define_unsafe_global_intrinsic(
        self,
        intrinsic: str,
        expr: Call,
    ) -> bool:
        if intrinsic == "define_global_i8":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            value = self._unsafe_const_i64_arg(expr.args[1])
            self._define_global(
                name,
                _I8,
                ir.Constant(_I8, value & 0xFF),
            )
            return True
        if intrinsic == "define_global_i32":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            value = self._unsafe_const_i64_arg(expr.args[1])
            self._define_global(name, _I32, ir.Constant(_I32, value))
            return True
        if intrinsic == "define_global_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            value = self._unsafe_const_i64_arg(expr.args[1])
            self._define_global(name, _I64, ir.Constant(_I64, value))
            return True
        if intrinsic == "define_global_header":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            refcount = self._unsafe_const_i64_arg(expr.args[1])
            type_tag = self._unsafe_const_i64_arg(expr.args[2])
            flags = self._unsafe_const_i64_arg(expr.args[3])
            header_ty = ir.LiteralStructType([_I64, _I32, _I32])
            init = ir.Constant(
                header_ty,
                [
                    ir.Constant(_I64, refcount),
                    ir.Constant(_I32, type_tag),
                    ir.Constant(_I32, flags),
                ],
            )
            self._define_global(name, header_ty, init)
            return True
        if intrinsic == "define_global_ptr_null":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            self._define_global(name, _CSTR, ir.Constant(_CSTR, None))
            return True
        if intrinsic == "define_thread_local_ptr_null":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            gv = self._define_global(name, _CSTR, ir.Constant(_CSTR, None))
            gv.storage_class = "thread_local"
            return True
        if intrinsic == "define_thread_local_i32":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            value = self._unsafe_const_i64_arg(expr.args[1])
            gv = self._define_global(name, _I32, ir.Constant(_I32, value))
            gv.storage_class = "thread_local"
            return True
        if intrinsic == "define_global_ptr_to_global":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            target = self._unsafe_symbol_literal_arg(expr.args[1])
            target_gv = self._get_or_declare_global_for_initializer(target)
            self._define_global(name, _CSTR, ir.Constant(_CSTR, target_gv))
            return True
        if intrinsic == "define_global_cstr":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            if not isinstance(expr.args[1], StrLit):
                raise NotImplementedError(
                    "pcc.unsafe.define_global_cstr requires a string literal"
                )
            payload = self._utf8_byte_values(expr.args[1].value) + [0]
            arr_ty = ir.ArrayType(_I8, len(payload))
            init = ir.Constant(arr_ty, [ir.Constant(_I8, b) for b in payload])
            self._define_global(name, arr_ty, init)
            return True
        if intrinsic == "define_global_ptr_array":
            if expr.kwargs or len(expr.args) < 1:
                raise NotImplementedError(
                    "pcc.unsafe.define_global_ptr_array expects a name"
                )
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            targets = [self._unsafe_symbol_literal_arg(arg) for arg in expr.args[1:]]
            arr_ty = ir.ArrayType(_CSTR, len(targets))
            init = ir.Constant(
                arr_ty,
                [
                    ir.Constant(
                        _CSTR,
                        self._get_or_declare_global_for_initializer(t),
                    )
                    for t in targets
                ],
            )
            self._define_global(name, arr_ty, init)
            return True
        if intrinsic == "define_global_null_ptr_array":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            n_items = self._unsafe_const_i64_arg(expr.args[1])
            arr_ty = ir.ArrayType(_CSTR, n_items)
            init = ir.Constant(
                arr_ty, [ir.Constant(_CSTR, None) for _ in range(n_items)]
            )
            self._define_global(name, arr_ty, init)
            return True
        if intrinsic == "define_global_i32_array":
            if expr.kwargs or len(expr.args) < 1:
                raise NotImplementedError(
                    "pcc.unsafe.define_global_i32_array expects a name"
                )
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            values = [self._unsafe_const_i64_arg(arg) for arg in expr.args[1:]]
            arr_ty = ir.ArrayType(_I32, len(values))
            init = ir.Constant(arr_ty, [ir.Constant(_I32, v) for v in values])
            self._define_global(name, arr_ty, init)
            return True
        if intrinsic == "define_global_i64_array":
            if expr.kwargs or len(expr.args) < 1:
                raise NotImplementedError(
                    "pcc.unsafe.define_global_i64_array expects a name"
                )
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            values = [self._unsafe_const_i64_arg(arg) for arg in expr.args[1:]]
            arr_ty = ir.ArrayType(_I64, len(values))
            init = ir.Constant(arr_ty, [ir.Constant(_I64, v) for v in values])
            self._define_global(name, arr_ty, init)
            return True
        if intrinsic == "define_global_struct_words":
            # Raw-layout struct global with mixed i64/ptr fields.  The
            # element list is built dynamically, so the closed-world
            # LiteralStructType path uses the dyn variant (same pattern as
            # builder.call_dyn) instead of requiring a literal list at the
            # call site.
            if expr.kwargs or len(expr.args) < 1:
                raise NotImplementedError(
                    "pcc.unsafe.define_global_struct_words expects a name"
                )
            name = self._unsafe_symbol_literal_arg(expr.args[0])
            field_tys = []
            field_vals = []
            for arg in expr.args[1:]:
                if isinstance(arg, StrLit):
                    target = self._unsafe_symbol_literal_arg(arg)
                    target_gv = self._get_or_declare_global_for_initializer(
                        target
                    )
                    field_tys.append(_CSTR)
                    field_vals.append(ir.Constant(_CSTR, target_gv))
                else:
                    word = self._unsafe_const_i64_arg(arg)
                    field_tys.append(_I64)
                    field_vals.append(ir.Constant(_I64, word))
            struct_ty = ir.LiteralStructType(field_tys)
            init = ir.Constant(struct_ty, field_vals)
            self._define_global(name, struct_ty, init)
            return True
        return False

    def _maybe_define_unsafe_global_stmt(self, stmt: "ExprStmt") -> bool:
        expr = stmt.expr
        if not isinstance(expr, Call) or not isinstance(expr.func, Name):
            return False
        intrinsic = self._unsafe_intrinsic_for_name(expr.func.ident)
        if intrinsic is None:
            return False
        return self._define_unsafe_global_intrinsic(intrinsic, expr)

    def _declare_external_global(
        self,
        name: str,
        value_ty: ir.Type,
    ) -> ir.GlobalVariable:
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.GlobalVariable):
            return existing
        if existing is not None:
            raise NotImplementedError(f"{name!r} already declared as non-global")
        gv = ir.GlobalVariable(self.module, value_ty, name=name)
        gv.linkage = "external"
        return gv

    def _unsafe_typed_addr(
        self,
        base: ir.Value,
        offset: ir.Value,
        pointee: ir.Type,
    ) -> ir.Value:
        byte_addr = self.builder.gep(
            base,
            [offset],
            name=self._fresh("unsafe.addr"),
        )
        ptr_ty = pointee.as_pointer()
        if self._ir_type_matches(byte_addr.type, ptr_ty):
            return byte_addr
        return self.builder.bitcast(
            byte_addr,
            ptr_ty,
            name=self._fresh("unsafe.addr.cast"),
        )

    def _unsafe_atomic_order_arg(
        self,
        intrinsic: str,
        expr: Expr,
        allowed: tuple,
    ) -> str:
        if not isinstance(expr, StrLit):
            raise NotImplementedError(
                f"pcc.unsafe.{intrinsic} memory ordering must be a string literal"
            )
        order = expr.value
        if order not in _ATOMIC_ORDER_TO_LLVM or order not in allowed:
            raise NotImplementedError(
                f"pcc.unsafe.{intrinsic} does not support ordering {order!r}; "
                f"allowed: {', '.join(allowed)}"
            )
        return _ATOMIC_ORDER_TO_LLVM[order]

    def _emit_unsafe_atomic_call(self, intrinsic: str, expr: Call) -> ir.Value:
        if intrinsic == "atomic_test_and_set":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            addr = self._unsafe_typed_addr(
                self._unsafe_address_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                _I8,
            )
            order = self._unsafe_atomic_order_arg(
                intrinsic,
                expr.args[2],
                ("relaxed", "acquire", "release", "acq_rel", "seq_cst"),
            )
            old = self.builder.atomic_rmw(
                "xchg",
                addr,
                ir.Constant(_I8, 1),
                order,
                name=self._fresh("unsafe.atomic.tas"),
            )
            was_set = self.builder.icmp_unsigned(
                "!=",
                old,
                ir.Constant(_I8, 0),
                name=self._fresh("unsafe.atomic.tas.was"),
            )
            return self.builder.zext(
                was_set, _I64, name=self._fresh("unsafe.atomic.tas.i64")
            )
        if intrinsic == "atomic_clear":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            addr = self._unsafe_typed_addr(
                self._unsafe_address_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                _I8,
            )
            order = self._unsafe_atomic_order_arg(
                intrinsic, expr.args[2], ("relaxed", "release", "seq_cst")
            )
            self.builder.store_atomic(ir.Constant(_I8, 0), addr, order, 1)
            return self._unsafe_void_result()
        if intrinsic == "atomic_fence":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            order = self._unsafe_atomic_order_arg(
                intrinsic,
                expr.args[0],
                ("acquire", "release", "acq_rel", "seq_cst"),
            )
            self.builder.fence(order)
            return self._unsafe_void_result()
        is_i32 = intrinsic.endswith("_i32")
        width_ty = _I32 if is_i32 else _I64
        align = 4 if is_i32 else 8
        if intrinsic in ("atomic_load_i32", "atomic_load_i64"):
            self._unsafe_expect_arity(intrinsic, expr, 3)
            addr = self._unsafe_typed_addr(
                self._unsafe_address_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                width_ty,
            )
            order = self._unsafe_atomic_order_arg(
                intrinsic, expr.args[2], ("relaxed", "acquire", "seq_cst")
            )
            raw = self.builder.load_atomic(
                addr, order, align, name=self._fresh("unsafe.atomic.load")
            )
            if is_i32:
                return self.builder.sext(
                    raw, _I64, name=self._fresh("unsafe.atomic.i32.to.i64")
                )
            return raw
        if intrinsic in ("atomic_store_i32", "atomic_store_i64"):
            self._unsafe_expect_arity(intrinsic, expr, 4)
            addr = self._unsafe_typed_addr(
                self._unsafe_ptr_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                width_ty,
            )
            value = (
                self._unsafe_i32_arg(expr.args[2])
                if is_i32
                else self._unsafe_i64_arg(expr.args[2])
            )
            order = self._unsafe_atomic_order_arg(
                intrinsic, expr.args[3], ("relaxed", "release", "seq_cst")
            )
            self.builder.store_atomic(value, addr, order, align)
            return self._unsafe_void_result()
        if intrinsic in ("atomic_rmw_i32", "atomic_rmw_i64"):
            self._unsafe_expect_arity(intrinsic, expr, 5)
            op_expr = expr.args[0]
            if not isinstance(op_expr, StrLit) or op_expr.value not in _ATOMIC_RMW_OPS:
                raise NotImplementedError(
                    f"pcc.unsafe.{intrinsic} op must be a string literal, one of: "
                    + ", ".join(_ATOMIC_RMW_OPS)
                )
            addr = self._unsafe_typed_addr(
                self._unsafe_ptr_arg(expr.args[1]),
                self._unsafe_i64_arg(expr.args[2]),
                width_ty,
            )
            value = (
                self._unsafe_i32_arg(expr.args[3])
                if is_i32
                else self._unsafe_i64_arg(expr.args[3])
            )
            order = self._unsafe_atomic_order_arg(
                intrinsic,
                expr.args[4],
                ("relaxed", "acquire", "release", "acq_rel", "seq_cst"),
            )
            old = self.builder.atomic_rmw(
                op_expr.value,
                addr,
                value,
                order,
                name=self._fresh("unsafe.atomic.rmw"),
            )
            if is_i32:
                return self.builder.sext(
                    old, _I64, name=self._fresh("unsafe.atomic.i32.to.i64")
                )
            return old
        if intrinsic in ("atomic_cas_i32", "atomic_cas_i64"):
            self._unsafe_expect_arity(intrinsic, expr, 6)
            addr = self._unsafe_typed_addr(
                self._unsafe_ptr_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                width_ty,
            )
            expected = (
                self._unsafe_i32_arg(expr.args[2])
                if is_i32
                else self._unsafe_i64_arg(expr.args[2])
            )
            desired = (
                self._unsafe_i32_arg(expr.args[3])
                if is_i32
                else self._unsafe_i64_arg(expr.args[3])
            )
            order = self._unsafe_atomic_order_arg(
                intrinsic,
                expr.args[4],
                ("relaxed", "acquire", "release", "acq_rel", "seq_cst"),
            )
            fail_order = self._unsafe_atomic_order_arg(
                intrinsic, expr.args[5], ("relaxed", "acquire", "seq_cst")
            )
            pair = self.builder.cmpxchg(
                addr,
                expected,
                desired,
                order,
                fail_order,
                name=self._fresh("unsafe.atomic.cas"),
            )
            old = self.builder.extract_value(
                pair, [0], name=self._fresh("unsafe.atomic.cas.old")
            )
            if is_i32:
                return self.builder.sext(
                    old, _I64, name=self._fresh("unsafe.atomic.i32.to.i64")
                )
            return old
        raise NotImplementedError(f"pcc.unsafe.{intrinsic} is not lowered yet")

    def _target_sys_platform_text(self) -> str:
        triple = getattr(self, "_target_triple", "") or ""
        triple = triple.lower()
        if triple:
            if "darwin" in triple or "apple" in triple:
                return "darwin"
            if "linux" in triple:
                return "linux"
            if "windows" in triple or "win32" in triple:
                return "win32"
            if "freebsd" in triple:
                return "freebsd"
            return "unknown"
        if sys.platform == "darwin":
            return "darwin"
        if sys.platform.startswith("linux"):
            return "linux"
        if sys.platform.startswith("win"):
            return "win32"
        if sys.platform.startswith("freebsd"):
            return "freebsd"
        return "unknown"

    def _target_machine_text(self) -> str:
        triple = getattr(self, "_target_triple", "") or ""
        triple = triple.lower()
        if triple:
            arch = triple.split("-", 1)[0]
            if arch == "aarch64":
                return "aarch64"
            if arch == "arm64":
                return "arm64"
            if arch == "x86_64" or arch == "amd64":
                return "x86_64"
            if arch == "i386" or arch == "i686":
                return "i386"
            if arch == "arm":
                return "arm"
            return arch
        machine = platform.machine()
        if machine == "aarch64":
            return "aarch64"
        if machine == "arm64":
            return "arm64"
        if machine == "x86_64" or machine == "AMD64":
            return "x86_64"
        if machine == "i386" or machine == "i686":
            return "i386"
        if machine == "arm":
            return "arm"
        if machine == "ppc64":
            return "ppc64"
        if machine == "ppc":
            return "ppc"
        return machine

    def _stat_layout(self) -> tuple[int, int, int, int]:
        """Return struct stat size/mode/mtime offsets for host libc.

        This is deliberately a codegen/platform intrinsic boundary: the
        pcc-Python runtime source stays structure-agnostic while the compiler
        owns the ABI constants that differ between Darwin and Linux.
        """
        machine = self._target_machine_text()
        platform_name = self._target_sys_platform_text()
        if platform_name == "darwin":
            return 144, 4, 48, 56
        if platform_name == "linux":
            if machine == "x86_64":
                return 144, 24, 88, 96
            # glibc aarch64 uses a different first-field order from x86_64
            # but keeps the POSIX timespec pair in the same trailing region.
            if machine == "aarch64" or machine == "arm64":
                return 128, 16, 88, 96
        return 144, 24, 88, 96

    def _emit_unsafe_stat_call(self, path: ir.Value) -> tuple[ir.Value, ir.Value]:
        size, _mode_off, _mtime_sec_off, _mtime_nsec_off = self._stat_layout()
        stat_ty = ir.ArrayType(_I8, size)
        slot = self.builder.alloca(stat_ty, name=self._fresh("unsafe.stat.buf"))
        zero = ir.Constant(_I64, 0)
        buf = self.builder.gep(
            slot,
            [zero, zero],
            inbounds=True,
            name=self._fresh("unsafe.stat.ptr"),
        )
        platform_name = self._target_sys_platform_text()
        machine = self._target_machine_text()
        if platform_name == "darwin":
            stat_fn = self._declare_external_function(
                "stat",
                _I32,
                [_CSTR, _CSTR],
            )
            rc = self.builder.call(
                stat_fn,
                [path, buf],
                name=self._fresh("unsafe.stat.rc"),
            )
            return buf, rc
        if platform_name == "linux" and machine == "x86_64":
            zero = ir.Constant(_I64, 0)
            path_i = self.builder.ptrtoint(
                path,
                _I64,
                name=self._fresh("unsafe.stat.path.i64"),
            )
            buf_i = self.builder.ptrtoint(
                buf,
                _I64,
                name=self._fresh("unsafe.stat.buf.i64"),
            )
            raw = self.builder.syscall6(
                ir.Constant(_I64, 4),
                path_i,
                buf_i,
                zero,
                zero,
                zero,
                zero,
                name=self._fresh("unsafe.stat.syscall"),
            )
            return (
                buf,
                self.builder.trunc(
                    raw,
                    _I32,
                    name=self._fresh("unsafe.stat.rc"),
                ),
            )
        raise NotImplementedError(
            "pcc.unsafe stat helpers support Darwin libSystem and Linux x86_64 raw syscalls"
        )

    def _emit_unsafe_stat_kind(self, expr: Call) -> ir.Value:
        self._unsafe_expect_arity("stat_kind", expr, 1)
        _size, mode_off, _mtime_sec_off, _mtime_nsec_off = self._stat_layout()
        path = self._unsafe_ptr_arg(expr.args[0])
        path_i = self.builder.ptrtoint(
            path,
            _I64,
            name=self._fresh("unsafe.stat.path.i"),
        )
        is_null = self.builder.icmp_unsigned(
            "==",
            path_i,
            ir.Constant(_I64, 0),
            name=self._fresh("unsafe.stat.path.null"),
        )
        buf, rc = self._emit_unsafe_stat_call(path)
        failed = self.builder.icmp_signed(
            "!=",
            rc,
            ir.Constant(_I32, 0),
            name=self._fresh("unsafe.stat.failed"),
        )
        bad = self.builder.or_(
            is_null,
            failed,
            name=self._fresh("unsafe.stat.bad"),
        )
        mode_addr = self._unsafe_typed_addr(
            buf,
            ir.Constant(_I64, mode_off),
            _I32,
        )
        mode = self.builder.load(
            mode_addr,
            name=self._fresh("unsafe.stat.mode"),
            align=1,
        )
        masked = self.builder.and_(
            mode,
            ir.Constant(_I32, 0o170000),
            name=self._fresh("unsafe.stat.mode.mask"),
        )
        is_reg = self.builder.icmp_signed(
            "==",
            masked,
            ir.Constant(_I32, 0o100000),
            name=self._fresh("unsafe.stat.isreg"),
        )
        is_dir = self.builder.icmp_signed(
            "==",
            masked,
            ir.Constant(_I32, 0o040000),
            name=self._fresh("unsafe.stat.isdir"),
        )
        dir_or_other = self.builder.select(
            is_dir,
            ir.Constant(_I64, 2),
            ir.Constant(_I64, 3),
            name=self._fresh("unsafe.stat.kind.dir"),
        )
        typed_kind = self.builder.select(
            is_reg,
            ir.Constant(_I64, 1),
            dir_or_other,
            name=self._fresh("unsafe.stat.kind.typed"),
        )
        return self.builder.select(
            bad,
            ir.Constant(_I64, 0),
            typed_kind,
            name=self._fresh("unsafe.stat.kind"),
        )

    def _emit_unsafe_stat_mtime(self, expr: Call) -> ir.Value:
        self._unsafe_expect_arity("stat_mtime", expr, 1)
        _size, _mode_off, mtime_sec_off, mtime_nsec_off = self._stat_layout()
        path = self._unsafe_ptr_arg(expr.args[0])
        path_i = self.builder.ptrtoint(
            path,
            _I64,
            name=self._fresh("unsafe.mtime.path.i"),
        )
        is_null = self.builder.icmp_unsigned(
            "==",
            path_i,
            ir.Constant(_I64, 0),
            name=self._fresh("unsafe.mtime.path.null"),
        )
        buf, rc = self._emit_unsafe_stat_call(path)
        failed = self.builder.icmp_signed(
            "!=",
            rc,
            ir.Constant(_I32, 0),
            name=self._fresh("unsafe.mtime.failed"),
        )
        bad = self.builder.or_(
            is_null,
            failed,
            name=self._fresh("unsafe.mtime.bad"),
        )
        sec_addr = self._unsafe_typed_addr(
            buf,
            ir.Constant(_I64, mtime_sec_off),
            _I64,
        )
        nsec_addr = self._unsafe_typed_addr(
            buf,
            ir.Constant(_I64, mtime_nsec_off),
            _I64,
        )
        sec = self.builder.load(
            sec_addr,
            name=self._fresh("unsafe.mtime.sec"),
            align=1,
        )
        nsec = self.builder.load(
            nsec_addr,
            name=self._fresh("unsafe.mtime.nsec"),
            align=1,
        )
        sec_f = self.builder.sitofp(
            sec,
            _DOUBLE,
            name=self._fresh("unsafe.mtime.sec.f"),
        )
        nsec_f = self.builder.sitofp(
            nsec,
            _DOUBLE,
            name=self._fresh("unsafe.mtime.nsec.f"),
        )
        frac = self.builder.fdiv(
            nsec_f,
            ir.Constant(_DOUBLE, 1.0e9),
            name=self._fresh("unsafe.mtime.frac"),
        )
        value = self.builder.fadd(
            sec_f,
            frac,
            name=self._fresh("unsafe.mtime.value"),
        )
        return self.builder.select(
            bad,
            ir.Constant(_DOUBLE, float("nan")),
            value,
            name=self._fresh("unsafe.mtime"),
        )

    def _emit_unsafe_intrinsic_call(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if self._define_unsafe_global_intrinsic(intrinsic, expr):
            return self._unsafe_void_result()
        if intrinsic.startswith("atomic_"):
            return self._emit_unsafe_atomic_call(intrinsic, expr)
        family = _unsafe_intrinsic_family(intrinsic)
        if family == 0:
            return self._emit_unsafe_va_numeric_f64(intrinsic, expr)
        if family == 1:
            return self._emit_unsafe_page_global_alloc(intrinsic, expr)
        if family == 2:
            return self._emit_unsafe_ptr_memory(intrinsic, expr)
        if family == 3:
            return self._emit_unsafe_read_write_rss(intrinsic, expr)
        if family == 4:
            return self._emit_unsafe_file_mutation(intrinsic, expr)
        if family == 5:
            return self._emit_unsafe_socket_control(intrinsic, expr)
        if family == 6:
            return self._emit_unsafe_socket_io_poll(intrinsic, expr)
        if family == 7:
            return self._emit_unsafe_system_info(intrinsic, expr)
        if family == 8:
            return self._emit_unsafe_time_process_control(intrinsic, expr)
        if family == 9:
            return self._emit_unsafe_spawn_process_pipe(intrinsic, expr)
        if family == 10:
            return self._emit_unsafe_spawn_process(intrinsic, expr)
        if family == 11:
            return self._emit_unsafe_env_access_stat(intrinsic, expr)
        if family == 12:
            return self._emit_unsafe_indirect_calls_a(intrinsic, expr)
        if family == 13:
            return self._emit_unsafe_indirect_calls_b(intrinsic, expr)
        if family == 14:
            return self._emit_unsafe_loader_waitset_gc(intrinsic, expr)
        raise NotImplementedError(
            f"unknown pcc.unsafe intrinsic {intrinsic!r}"
        )

    def _emit_unsafe_va_numeric_f64(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "va_start":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            if self.current_function is None or not getattr(
                self.current_function.function_type, "var_arg", False
            ):
                raise NotImplementedError(
                    "pcc.unsafe.va_start requires @c_abi_variadic_export"
                )
            storage_ty = ir.ArrayType(_I8, 32)
            storage = self.builder.alloca(
                storage_ty,
                name=self._fresh("unsafe.va.storage"),
            )
            zero = ir.Constant(_I64, 0)
            cursor = self.builder.gep(
                storage,
                [zero, zero],
                inbounds=True,
                name=self._fresh("unsafe.va.cursor"),
            )
            va_start_fn = self._declare_external_function(
                "llvm.va_start.p0",
                _VOID,
                [_CSTR],
            )
            self.builder.call(va_start_fn, [cursor])
            return cursor
        if intrinsic == "va_arg_i64":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            self._tmp_counter += 1
            helper = self._declare_external_function(
                "__pcc_va_arg_" + str(self._tmp_counter),
                _I64,
                [_CSTR],
            )
            return self.builder.call(
                helper,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.va.i64"),
            )
        if intrinsic in ("va_arg_i32", "va_arg_u32"):
            self._unsafe_expect_arity(intrinsic, expr, 1)
            self._tmp_counter += 1
            helper = self._declare_external_function(
                "__pcc_va_arg_" + str(self._tmp_counter),
                _I32,
                [_CSTR],
            )
            raw = self.builder.call(
                helper,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.va.i32.raw"),
            )
            if intrinsic == "va_arg_i32":
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.va.i32"),
                )
            return self.builder.zext(
                raw,
                _I64,
                name=self._fresh("unsafe.va.u32"),
            )
        if intrinsic in ("va_arg_ptr", "va_arg_f64"):
            self._unsafe_expect_arity(intrinsic, expr, 1)
            self._tmp_counter += 1
            result_ty = _CSTR if intrinsic == "va_arg_ptr" else _DOUBLE
            helper = self._declare_external_function(
                "__pcc_va_arg_" + str(self._tmp_counter),
                result_ty,
                [_CSTR],
            )
            return self.builder.call(
                helper,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.va.value"),
            )
        if intrinsic == "va_cursor":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            ap = self._unsafe_ptr_arg(expr.args[0])
            if self._target_sys_platform_text() == "darwin":
                slot = self.builder.alloca(
                    _CSTR,
                    name=self._fresh("unsafe.va.fixed.slot"),
                )
                self.builder.store(ap, slot)
                if self._ir_type_matches(slot.type, _CSTR):
                    return slot
                return self.builder.bitcast(
                    slot,
                    _CSTR,
                    name=self._fresh("unsafe.va.fixed.cursor"),
                )
            return ap
        if intrinsic == "va_end":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            va_end_fn = self._declare_external_function(
                "llvm.va_end.p0",
                _VOID,
                [_CSTR],
            )
            self.builder.call(
                va_end_fn,
                [self._unsafe_ptr_arg(expr.args[0])],
            )
            return self._unsafe_void_result()
        if intrinsic == "wrapping_mul_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            return self.builder.mul(
                self._unsafe_i64_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                name=self._fresh("unsafe.mul.i64"),
            )
        if intrinsic == "logical_shift_right_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            return self.builder.lshr(
                self._unsafe_i64_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                name=self._fresh("unsafe.lshr.i64"),
            )
        if intrinsic == "logical_shift_left_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            return self.builder.shl(
                self._unsafe_i64_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                name=self._fresh("unsafe.shl.i64"),
            )
        if intrinsic in ("unsigned_div_i64", "unsigned_rem_i64"):
            self._unsafe_expect_arity(intrinsic, expr, 2)
            value = self._unsafe_i64_arg(expr.args[0])
            divisor = self._unsafe_i64_arg(expr.args[1])
            if intrinsic == "unsigned_div_i64":
                return self.builder.udiv(
                    value,
                    divisor,
                    name=self._fresh("unsafe.udiv.i64"),
                )
            return self.builder.urem(
                value,
                divisor,
                name=self._fresh("unsafe.urem.i64"),
            )
        if intrinsic == "unsigned_greater_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            return self.builder.icmp_unsigned(
                ">",
                self._unsafe_i64_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                name=self._fresh("unsafe.ugt.i64"),
            )
        if intrinsic == "mul_overflow_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            pair_type = ir.LiteralStructType([_I64, ir.IntType(1)])
            intrinsic_name = "llvm.smul.with.overflow.i64"
            overflow_intrinsic = self.module.globals.get(intrinsic_name)
            if overflow_intrinsic is None:
                overflow_intrinsic = ir.Function(
                    self.module,
                    ir.FunctionType(pair_type, [_I64, _I64]),
                    name=intrinsic_name,
                )
            pair = self.builder.call(
                overflow_intrinsic,
                [
                    self._unsafe_i64_arg(expr.args[0]),
                    self._unsafe_i64_arg(expr.args[1]),
                ],
                name=self._fresh("unsafe.mul.overflow.pair"),
            )
            return self.builder.extract_value(
                pair,
                [1],
                name=self._fresh("unsafe.mul.overflow"),
            )
        if intrinsic == "float_to_i64":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            return self.builder.fptosi(
                self._unsafe_f64_arg(expr.args[0]),
                _I64,
                name=self._fresh("unsafe.f64.to.i64"),
            )
        if intrinsic == "i64_to_float":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            return self.builder.sitofp(
                self._unsafe_i64_arg(expr.args[0]),
                _DOUBLE,
                name=self._fresh("unsafe.i64.to.f64"),
            )
        if intrinsic == "f64_div":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            return self.builder.fdiv(
                self._unsafe_f64_arg(expr.args[0]),
                self._unsafe_f64_arg(expr.args[1]),
                name=self._fresh("unsafe.f64.div"),
            )
        if intrinsic in ("f64_signbit", "f64_bits"):
            self._unsafe_expect_arity(intrinsic, expr, 1)
            bits = self.builder.bitcast(
                self._unsafe_f64_arg(expr.args[0]),
                _I64,
                name=self._fresh("unsafe.f64.bits"),
            )
            if intrinsic == "f64_bits":
                return bits
            return self.builder.lshr(
                bits,
                ir.Constant(_I64, 63),
                name=self._fresh("unsafe.f64.signbit"),
            )
        if intrinsic == "f64_pair_make":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            pair_type = ir.LiteralStructType([_DOUBLE, _DOUBLE])
            pair_slot = self.builder.alloca(
                pair_type,
                name=self._fresh("unsafe.f64.pair.slot"),
            )
            zero = ir.Constant(_I32, 0)
            first_ptr = self.builder.gep(
                pair_slot,
                [zero, zero],
                inbounds=True,
                name=self._fresh("unsafe.f64.pair.first.ptr"),
            )
            second_ptr = self.builder.gep(
                pair_slot,
                [zero, ir.Constant(_I32, 1)],
                inbounds=True,
                name=self._fresh("unsafe.f64.pair.second.ptr"),
            )
            self.builder.store(self._unsafe_f64_arg(expr.args[0]), first_ptr)
            self.builder.store(
                self._unsafe_f64_arg(expr.args[1]),
                second_ptr,
            )
            return self.builder.load(
                pair_slot,
                name=self._fresh("unsafe.f64.pair"),
            )
        if intrinsic in ("f64_pair_first", "f64_pair_second"):
            self._unsafe_expect_arity(intrinsic, expr, 1)
            return self.builder.extract_value(
                self._unsafe_f64_pair_arg(expr.args[0]),
                0 if intrinsic == "f64_pair_first" else 1,
                name=self._fresh("unsafe." + intrinsic),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_page_global_alloc(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "page_alloc":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            size = self._unsafe_i64_arg(expr.args[0])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                mmap_fn = self._declare_external_function(
                    "mmap",
                    _CSTR,
                    [_CSTR, _I64, _I32, _I32, _I32, _I64],
                )
                mapped = self.builder.call(
                    mmap_fn,
                    [
                        ir.Constant(_CSTR, None),
                        size,
                        ir.Constant(_I32, 3),
                        ir.Constant(_I32, 0x1002),
                        ir.Constant(_I32, -1),
                        ir.Constant(_I64, 0),
                    ],
                    name=self._fresh("unsafe.page.mmap"),
                )
                mapped_i = self.builder.ptrtoint(
                    mapped,
                    _I64,
                    name=self._fresh("unsafe.page.mmap.i64"),
                )
                failed = self.builder.icmp_signed(
                    "==",
                    mapped_i,
                    ir.Constant(_I64, -1),
                    name=self._fresh("unsafe.page.mmap.failed"),
                )
                return self.builder.select(
                    failed,
                    ir.Constant(_CSTR, None),
                    mapped,
                    name=self._fresh("unsafe.page.mmap.result"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                raw = self.builder.syscall6(
                    ir.Constant(_I64, 9),
                    zero,
                    size,
                    ir.Constant(_I64, 3),
                    ir.Constant(_I64, 0x22),
                    ir.Constant(_I64, -1),
                    zero,
                    name=self._fresh("unsafe.page.mmap.syscall"),
                )
                failed = self.builder.icmp_signed(
                    "<",
                    raw,
                    zero,
                    name=self._fresh("unsafe.page.mmap.failed"),
                )
                mapped = self.builder.inttoptr(
                    raw,
                    _CSTR,
                    name=self._fresh("unsafe.page.mmap.ptr"),
                )
                return self.builder.select(
                    failed,
                    ir.Constant(_CSTR, None),
                    mapped,
                    name=self._fresh("unsafe.page.mmap.result"),
                )
            raise NotImplementedError(
                "pcc.unsafe.page_alloc supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "page_free":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            ptr = self._unsafe_ptr_arg(expr.args[0])
            size = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                munmap_fn = self._declare_external_function(
                    "munmap",
                    _I32,
                    [_CSTR, _I64],
                )
                status = self.builder.call(
                    munmap_fn,
                    [ptr, size],
                    name=self._fresh("unsafe.page.munmap"),
                )
                return self.builder.sext(
                    status,
                    _I64,
                    name=self._fresh("unsafe.page.munmap.i64"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                ptr_i = self.builder.ptrtoint(
                    ptr,
                    _I64,
                    name=self._fresh("unsafe.page.munmap.ptr"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 11),
                    ptr_i,
                    size,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.page.munmap.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.page_free supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "syscall6":
            self._unsafe_expect_arity(intrinsic, expr, 7)
            if (
                self._target_sys_platform_text() != "linux"
                or self._target_machine_text() != "x86_64"
            ):
                raise NotImplementedError(
                    "pcc.unsafe.syscall6 is Linux x86_64 only; Darwin raw "
                    "syscalls are unsupported by policy (use named libSystem "
                    "externs)"
                )
            args = []
            for arg_expr in expr.args:
                value = self._emit_expr(arg_expr)
                if isinstance(value.type, ir.PointerType):
                    value = self.builder.ptrtoint(
                        value, _I64, name=self._fresh("unsafe.syscall.ptr")
                    )
                else:
                    value = self._to_int64(value, arg_expr.ty)
                args.append(value)
            return self.builder.syscall6(
                args[0],
                args[1],
                args[2],
                args[3],
                args[4],
                args[5],
                args[6],
                name=self._fresh("unsafe.syscall6"),
            )
        if intrinsic == "cstr":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            return self._unsafe_cstr_literal_arg(expr.args[0])
        if intrinsic == "target_sys_platform":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            gv, _n = self._cstr_literal(self._target_sys_platform_text())
            return self._ptr_to_cstr(gv)
        if intrinsic == "target_platform_machine":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            gv, _n = self._cstr_literal(self._target_machine_text())
            return self._ptr_to_cstr(gv)
        if intrinsic == "darwin_errno_location":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            if self._target_sys_platform_text() != "darwin":
                return ir.Constant(_CSTR, None)
            error_fn = self._declare_external_function("__error", _CSTR, [])
            return self.builder.call(
                error_fn,
                [],
                name=self._fresh("unsafe.darwin.errno.location"),
            )
        if intrinsic == "abi_constant":
            return ir.Constant(_I64, self._unsafe_abi_constant_value(expr))
        if intrinsic == "global_addr":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            symbol = self._unsafe_symbol_literal_arg(expr.args[0])
            gv = self._declare_external_global(symbol, _I8)
            if self._ir_type_matches(gv.type, _CSTR):
                return gv
            return self.builder.bitcast(
                gv,
                _CSTR,
                name=self._fresh("unsafe.global.addr"),
            )
        if intrinsic == "function_addr":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            symbol = self._unsafe_symbol_literal_arg(expr.args[0])
            fn = self.module.globals.get(symbol)
            current_module_functions = getattr(self, "_funcdef_functions", {}).values()
            owned_forward_declaration = False
            for candidate in current_module_functions:
                if candidate is fn:
                    owned_forward_declaration = True
                    break
            if not isinstance(fn, ir.Function) or (
                fn.is_declaration and not owned_forward_declaration
            ):
                raise NotImplementedError(
                    "pcc.unsafe.function_addr requires a function defined "
                    "in the current module: " + repr(symbol)
                )
            return self.builder.bitcast(
                fn,
                _CSTR,
                name=self._fresh("unsafe.function.addr"),
            )
        if intrinsic == "int_to_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            return self.builder.inttoptr(
                self._unsafe_i64_arg(expr.args[0]),
                _CSTR,
                name=self._fresh("unsafe.int.to.ptr"),
            )
        if intrinsic == "ptr_to_int":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            return self.builder.ptrtoint(
                self._unsafe_ptr_arg(expr.args[0]),
                _I64,
                name=self._fresh("unsafe.ptr.to.int"),
            )
        if intrinsic == "global_load_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            symbol = self._unsafe_symbol_literal_arg(expr.args[0])
            gv = self._declare_external_global(symbol, _CSTR)
            return self.builder.load(
                gv,
                name=self._fresh("unsafe.global.load.ptr"),
            )
        if intrinsic == "global_store_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            symbol = self._unsafe_symbol_literal_arg(expr.args[0])
            gv = self._declare_external_global(symbol, _CSTR)
            self.builder.store(self._unsafe_ptr_arg(expr.args[1]), gv)
            return self._unsafe_void_result()
        if intrinsic == "malloc":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            malloc_fn = self._declare_external_function(
                "malloc",
                _CSTR,
                [_I64],
            )
            return self.builder.call(
                malloc_fn,
                [self._unsafe_i64_arg(expr.args[0])],
                name=self._fresh("unsafe.malloc"),
            )
        if intrinsic == "calloc":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            calloc_fn = self._declare_external_function(
                "calloc",
                _CSTR,
                [_I64, _I64],
            )
            return self.builder.call(
                calloc_fn,
                [
                    self._unsafe_i64_arg(expr.args[0]),
                    self._unsafe_i64_arg(expr.args[1]),
                ],
                name=self._fresh("unsafe.calloc"),
            )
        if intrinsic == "realloc":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            realloc_fn = self._declare_external_function(
                "realloc",
                _CSTR,
                [_CSTR, _I64],
            )
            return self.builder.call(
                realloc_fn,
                [
                    self._unsafe_ptr_arg(expr.args[0]),
                    self._unsafe_i64_arg(expr.args[1]),
                ],
                name=self._fresh("unsafe.realloc"),
            )
        if intrinsic == "free":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            free_fn = self._declare_external_function(
                "free",
                _VOID,
                [_CSTR],
            )
            self.builder.call(
                free_fn,
                [self._unsafe_address_arg(expr.args[0])],
            )
            return self._unsafe_void_result()
        if intrinsic == "ptr_add":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            return self.builder.gep(
                self._unsafe_address_arg(expr.args[0]),
                [self._unsafe_i64_arg(expr.args[1])],
                name=self._fresh("unsafe.ptr.add"),
            )
        if intrinsic == "ptr_diff":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            # These are addresses, like ptr_add's base. Untyped application
            # parameters can carry them as tagged integers; subtract the
            # decoded addresses rather than their object representation.
            # Runtime-port pointer lanes remain unchanged by this helper.
            lhs_i = self.builder.ptrtoint(
                self._unsafe_address_arg(expr.args[0]),
                _I64,
                name=self._fresh("unsafe.ptr.diff.l"),
            )
            rhs_i = self.builder.ptrtoint(
                self._unsafe_address_arg(expr.args[1]),
                _I64,
                name=self._fresh("unsafe.ptr.diff.r"),
            )
            return self.builder.sub(
                lhs_i,
                rhs_i,
                name=self._fresh("unsafe.ptr.diff"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_ptr_memory(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "null":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            return ir.Constant(_CSTR, None)
        if intrinsic in ("ptr_eq", "ptr_is_null", "is_tagged_int"):
            if intrinsic == "ptr_eq":
                self._unsafe_expect_arity(intrinsic, expr, 2)
                lhs_i = self.builder.ptrtoint(
                    self._unsafe_ptr_arg(expr.args[0]),
                    _I64,
                    name=self._fresh("unsafe.ptr.eq.l"),
                )
                rhs_i = self.builder.ptrtoint(
                    self._unsafe_ptr_arg(expr.args[1]),
                    _I64,
                    name=self._fresh("unsafe.ptr.eq.r"),
                )
                return self.builder.icmp_unsigned(
                    "==",
                    lhs_i,
                    rhs_i,
                    name=self._fresh("unsafe.ptr.eq"),
                )
            self._unsafe_expect_arity(intrinsic, expr, 1)
            ptr_i = self.builder.ptrtoint(
                self._unsafe_ptr_arg(expr.args[0]),
                _I64,
                name=self._fresh(f"unsafe.{intrinsic}.i"),
            )
            if intrinsic == "ptr_is_null":
                return self.builder.icmp_unsigned(
                    "==",
                    ptr_i,
                    ir.Constant(_I64, 0),
                    name=self._fresh("unsafe.ptr.null"),
                )
            tagged = self.builder.and_(
                ptr_i,
                ir.Constant(_I64, 1),
                name=self._fresh("unsafe.tag.bit"),
            )
            return self.builder.icmp_unsigned(
                "==",
                tagged,
                ir.Constant(_I64, 1),
                name=self._fresh("unsafe.tagged"),
            )
        if intrinsic == "tag_int":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            value = self._unsafe_i64_arg(expr.args[0])
            shifted = self.builder.shl(
                value,
                ir.Constant(_I64, 1),
                name=self._fresh("unsafe.tag.int.shift"),
            )
            tagged = self.builder.or_(
                shifted,
                ir.Constant(_I64, 1),
                name=self._fresh("unsafe.tag.int.bits"),
            )
            return self.builder.inttoptr(
                tagged,
                _CSTR,
                name=self._fresh("unsafe.tag.int.ptr"),
            )
        if intrinsic == "untag_int":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            ptr_i = self.builder.ptrtoint(
                self._unsafe_ptr_arg(expr.args[0]),
                _I64,
                name=self._fresh("unsafe.untag.int.bits"),
            )
            return self.builder.ashr(
                ptr_i,
                ir.Constant(_I64, 1),
                name=self._fresh("unsafe.untag.int"),
            )
        if intrinsic in (
            "load_i64",
            "load_i64x4",
            "load_i64x4_strided",
            "load_i32",
            "load_i8",
            "load_ptr",
            "load_f64",
        ):
            self._unsafe_expect_arity(
                intrinsic,
                expr,
                3 if intrinsic == "load_i64x4_strided" else 2,
            )
            base = self._unsafe_address_arg(expr.args[0])
            offset = self._unsafe_i64_arg(expr.args[1])
            if intrinsic == "load_i64":
                addr = self._unsafe_typed_addr(base, offset, _I64)
                return self.builder.load(
                    addr,
                    name=self._fresh("unsafe.load.i64"),
                    align=1,
                )
            if intrinsic in ("load_i64x4", "load_i64x4_strided"):
                quad_type = ir.LiteralStructType([_I64, _I64, _I64, _I64])
                quad_slot = self.builder.alloca(
                    quad_type,
                    name=self._fresh("unsafe.load.i64x4.slot"),
                )
                zero = ir.Constant(_I32, 0)
                stride = ir.Constant(_I64, 8)
                if intrinsic == "load_i64x4_strided":
                    stride = self._unsafe_i64_arg(expr.args[2])
                lane_index = 0
                while lane_index < 4:
                    lane_offset = offset
                    if lane_index:
                        delta = self.builder.mul(
                            stride,
                            ir.Constant(_I64, lane_index),
                            name=self._fresh("unsafe.load.i64x4.delta"),
                        )
                        lane_offset = self.builder.add(
                            offset,
                            delta,
                            name=self._fresh("unsafe.load.i64x4.offset"),
                        )
                    lane_addr = self._unsafe_typed_addr(base, lane_offset, _I64)
                    lane = self.builder.load(
                        lane_addr,
                        name=self._fresh("unsafe.load.i64x4.lane"),
                        align=1,
                    )
                    lane_ptr = self.builder.gep(
                        quad_slot,
                        [zero, ir.Constant(_I32, lane_index)],
                        inbounds=True,
                        name=self._fresh("unsafe.load.i64x4.lane.ptr"),
                    )
                    self.builder.store(lane, lane_ptr)
                    lane_index += 1
                return self.builder.load(
                    quad_slot,
                    name=self._fresh("unsafe.load.i64x4.value"),
                )
            if intrinsic == "load_i32":
                addr = self._unsafe_typed_addr(base, offset, _I32)
                raw = self.builder.load(
                    addr,
                    name=self._fresh("unsafe.load.i32"),
                    align=1,
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.i32.to.i64"),
                )
            if intrinsic == "load_i8":
                addr = self._unsafe_typed_addr(base, offset, _I8)
                raw = self.builder.load(
                    addr,
                    name=self._fresh("unsafe.load.i8"),
                    align=1,
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.i8.to.i64"),
                )
            if intrinsic == "load_f64":
                addr = self._unsafe_typed_addr(base, offset, _DOUBLE)
                return self.builder.load(
                    addr,
                    name=self._fresh("unsafe.load.f64"),
                    align=1,
                )
            addr = self._unsafe_typed_addr(base, offset, _CSTR)
            return self.builder.load(
                addr,
                name=self._fresh("unsafe.load.ptr"),
                align=1,
            )
        if intrinsic in (
            "store_i64",
            "store_i32",
            "store_i8",
            "store_ptr",
            "store_f64",
        ):
            self._unsafe_expect_arity(intrinsic, expr, 3)
            base = self._unsafe_address_arg(expr.args[0])
            offset = self._unsafe_i64_arg(expr.args[1])
            if intrinsic == "store_i64":
                addr = self._unsafe_typed_addr(base, offset, _I64)
                self.builder.store(
                    self._unsafe_i64_arg(expr.args[2]),
                    addr,
                    align=1,
                )
                return self._unsafe_void_result()
            if intrinsic == "store_i32":
                addr = self._unsafe_typed_addr(base, offset, _I32)
                raw = self.builder.trunc(
                    self._unsafe_i64_arg(expr.args[2]),
                    _I32,
                    name=self._fresh("unsafe.i64.to.i32"),
                )
                self.builder.store(raw, addr, align=1)
                return self._unsafe_void_result()
            if intrinsic == "store_i8":
                addr = self._unsafe_typed_addr(base, offset, _I8)
                raw = self.builder.trunc(
                    self._unsafe_i64_arg(expr.args[2]),
                    _I8,
                    name=self._fresh("unsafe.i64.to.i8"),
                )
                self.builder.store(raw, addr, align=1)
                return self._unsafe_void_result()
            if intrinsic == "store_f64":
                addr = self._unsafe_typed_addr(base, offset, _DOUBLE)
                self.builder.store(
                    self._unsafe_f64_arg(expr.args[2]),
                    addr,
                    align=1,
                )
                return self._unsafe_void_result()
            addr = self._unsafe_typed_addr(base, offset, _CSTR)
            self.builder.store(
                self._unsafe_ptr_arg(expr.args[2]),
                addr,
                align=1,
            )
            return self._unsafe_void_result()
        if intrinsic == "memset":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            memset_fn = self._declare_external_function(
                "memset",
                _CSTR,
                [_CSTR, _I32, _I64],
            )
            return self.builder.call(
                memset_fn,
                [
                    self._unsafe_address_arg(expr.args[0]),
                    self._unsafe_i32_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.memset"),
            )
        if intrinsic in ("memcpy", "memmove"):
            self._unsafe_expect_arity(intrinsic, expr, 3)
            copy_fn = self._declare_external_function(
                intrinsic,
                _CSTR,
                [_CSTR, _CSTR, _I64],
            )
            return self.builder.call(
                copy_fn,
                [
                    self._unsafe_address_arg(expr.args[0]),
                    self._unsafe_address_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                ],
                name=self._fresh(f"unsafe.{intrinsic}"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_read_write_rss(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic in ("read", "write"):
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd = self._unsafe_i32_arg(expr.args[0])
            buf = self._unsafe_ptr_arg(expr.args[1])
            size = self._unsafe_i64_arg(expr.args[2])
            if self._target_sys_platform_text() == "darwin":
                io_fn = self._declare_external_function(
                    intrinsic,
                    _I64,
                    [_I32, _CSTR, _I64],
                )
                return self.builder.call(
                    io_fn,
                    [fd, buf, size],
                    name=self._fresh("unsafe." + intrinsic),
                )
            if (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                fd_i64 = self.builder.sext(
                    fd,
                    _I64,
                    name=self._fresh("unsafe." + intrinsic + ".fd"),
                )
                buf_i64 = self.builder.ptrtoint(
                    buf,
                    _I64,
                    name=self._fresh("unsafe." + intrinsic + ".buf"),
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 0 if intrinsic == "read" else 1),
                    fd_i64,
                    buf_i64,
                    size,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe." + intrinsic + ".syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe."
                + intrinsic
                + " supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "close":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            fd = self._unsafe_i32_arg(expr.args[0])
            if self._target_sys_platform_text() == "darwin":
                close_fn = self._declare_external_function("close", _I32, [_I32])
                raw = self.builder.call(
                    close_fn,
                    [fd],
                    name=self._fresh("unsafe.close.i32"),
                )
                return self.builder.sext(raw, _I64, name=self._fresh("unsafe.close"))
            if (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                fd_i64 = self.builder.sext(
                    fd, _I64, name=self._fresh("unsafe.close.fd")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 3),
                    fd_i64,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.close.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.close supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "seek_file":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd = self._unsafe_i32_arg(expr.args[0])
            offset = self._unsafe_i64_arg(expr.args[1])
            whence = self._unsafe_i32_arg(expr.args[2])
            if self._target_sys_platform_text() == "darwin":
                seek_fn = self._declare_external_function(
                    "lseek", _I64, [_I32, _I64, _I32]
                )
                return self.builder.call(
                    seek_fn,
                    [fd, offset, whence],
                    name=self._fresh("unsafe.seek_file"),
                )
            if (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                fd_i64 = self.builder.sext(
                    fd, _I64, name=self._fresh("unsafe.seek_file.fd")
                )
                whence_i64 = self.builder.sext(
                    whence,
                    _I64,
                    name=self._fresh("unsafe.seek_file.whence"),
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 8),
                    fd_i64,
                    offset,
                    whence_i64,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.seek_file.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.seek_file supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "open_readonly":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            path = self._unsafe_ptr_arg(expr.args[0])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                open_fn = self._declare_external_function(
                    "open", _I32, [_CSTR, _I32], var_arg=True
                )
                raw = self.builder.call(
                    open_fn,
                    [path, ir.Constant(_I32, 0)],
                    name=self._fresh("unsafe.open_readonly.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "open_readonly")
            if platform_name == "linux" and machine == "x86_64":
                path_i = self.builder.ptrtoint(
                    path, _I64, name=self._fresh("unsafe.open_readonly.path")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 2),
                    path_i,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.open_readonly.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.open_readonly supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "darwin_current_rss_bytes":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            if self._target_sys_platform_text() != "darwin":
                return ir.Constant(_I64, -1)
            machine = self._target_machine_text()
            if machine not in {"arm64", "aarch64", "x86_64"}:
                raise NotImplementedError(
                    "pcc.unsafe.darwin_current_rss_bytes requires a 64-bit Darwin target"
                )
            # mach_task_basic_info is 12 natural_t words.  Allocate it as six
            # i64s so resident_size (the second i64) is naturally aligned,
            # then expose the ABI's integer_t* view to task_info.
            info_ty = ir.ArrayType(_I64, 6)
            info = self.builder.alloca(
                info_ty,
                name=self._fresh("unsafe.rss.mach.info"),
            )
            zero = ir.Constant(_I64, 0)
            info_i64 = self.builder.gep(
                info,
                [zero, zero],
                inbounds=True,
                name=self._fresh("unsafe.rss.mach.info.ptr"),
            )
            info_i32 = self.builder.bitcast(
                info_i64,
                _I32.as_pointer(),
                name=self._fresh("unsafe.rss.mach.info.i32"),
            )
            count = self.builder.alloca(
                _I32,
                name=self._fresh("unsafe.rss.mach.count"),
            )
            self.builder.store(ir.Constant(_I32, 12), count)
            task_global = self._declare_external_global("mach_task_self_", _I32)
            task = self.builder.load(
                task_global,
                name=self._fresh("unsafe.rss.mach.task"),
            )
            task_info = self._declare_external_function(
                "task_info",
                _I32,
                [_I32, _I32, _I32.as_pointer(), _I32.as_pointer()],
            )
            rc = self.builder.call(
                task_info,
                [task, ir.Constant(_I32, 20), info_i32, count],
                name=self._fresh("unsafe.rss.mach.rc"),
            )
            resident_ptr = self.builder.gep(
                info,
                [zero, ir.Constant(_I64, 1)],
                inbounds=True,
                name=self._fresh("unsafe.rss.mach.resident.ptr"),
            )
            resident = self.builder.load(
                resident_ptr,
                name=self._fresh("unsafe.rss.mach.resident"),
            )
            return self.builder.select(
                self.builder.icmp_signed("==", rc, ir.Constant(_I32, 0)),
                resident,
                ir.Constant(_I64, -1),
                name=self._fresh("unsafe.rss.mach.result"),
            )
        if intrinsic == "darwin_peak_rss_bytes":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            if self._target_sys_platform_text() != "darwin":
                return ir.Constant(_I64, -1)
            machine = self._target_machine_text()
            if machine not in {"arm64", "aarch64", "x86_64"}:
                raise NotImplementedError(
                    "pcc.unsafe.darwin_peak_rss_bytes requires a 64-bit Darwin target"
                )
            # Darwin struct rusage is 18 machine longs: two timevals followed
            # by ru_maxrss and thirteen more counters.  ru_maxrss is word 4.
            usage_ty = ir.ArrayType(_I64, 18)
            usage = self.builder.alloca(
                usage_ty,
                name=self._fresh("unsafe.rss.rusage"),
            )
            zero = ir.Constant(_I64, 0)
            usage_i64 = self.builder.gep(
                usage,
                [zero, zero],
                inbounds=True,
                name=self._fresh("unsafe.rss.rusage.ptr"),
            )
            usage_bytes = self.builder.bitcast(
                usage_i64,
                _CSTR,
                name=self._fresh("unsafe.rss.rusage.bytes"),
            )
            getrusage = self._declare_external_function(
                "getrusage",
                _I32,
                [_I32, _CSTR],
            )
            rc = self.builder.call(
                getrusage,
                [ir.Constant(_I32, 0), usage_bytes],
                name=self._fresh("unsafe.rss.rusage.rc"),
            )
            peak_ptr = self.builder.gep(
                usage,
                [zero, ir.Constant(_I64, 4)],
                inbounds=True,
                name=self._fresh("unsafe.rss.rusage.max.ptr"),
            )
            peak = self.builder.load(
                peak_ptr,
                name=self._fresh("unsafe.rss.rusage.max"),
            )
            return self.builder.select(
                self.builder.icmp_signed("==", rc, ir.Constant(_I32, 0)),
                peak,
                ir.Constant(_I64, -1),
                name=self._fresh("unsafe.rss.rusage.result"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_file_mutation(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "open_file":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            path = self._unsafe_ptr_arg(expr.args[0])
            access = self._unsafe_i32_arg(expr.args[1])
            disposition = self._unsafe_i32_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            access_flags = access
            is_truncate = self.builder.icmp_signed(
                "==",
                disposition,
                ir.Constant(_I32, 1),
                name=self._fresh("unsafe.open_file.truncate"),
            )
            is_append = self.builder.icmp_signed(
                "==",
                disposition,
                ir.Constant(_I32, 2),
                name=self._fresh("unsafe.open_file.append"),
            )
            is_exclusive = self.builder.icmp_signed(
                "==",
                disposition,
                ir.Constant(_I32, 3),
                name=self._fresh("unsafe.open_file.exclusive"),
            )
            if platform_name == "darwin":
                create_flags = self.builder.select(
                    is_truncate,
                    ir.Constant(_I32, 0x0200 | 0x0400),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.open_file.create.truncate"),
                )
                append_flags = self.builder.select(
                    is_append,
                    ir.Constant(_I32, 0x0200 | 0x0008),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.open_file.create.append"),
                )
                exclusive_flags = self.builder.select(
                    is_exclusive,
                    ir.Constant(_I32, 0x0200 | 0x0800),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.open_file.create.exclusive"),
                )
                flags = self.builder.or_(
                    access_flags,
                    self.builder.or_(
                        create_flags,
                        self.builder.or_(append_flags, exclusive_flags),
                    ),
                    name=self._fresh("unsafe.open_file.flags"),
                )
                open_fn = self._declare_external_function(
                    "open",
                    _I32,
                    [_CSTR, _I32],
                    var_arg=True,
                )
                raw = self.builder.call(
                    open_fn,
                    [path, flags, ir.Constant(_I32, 0o666)],
                    name=self._fresh("unsafe.open_file.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "open_file")
            if platform_name == "linux" and machine == "x86_64":
                create_flags = self.builder.select(
                    is_truncate,
                    ir.Constant(_I32, 64 | 512),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.open_file.create.truncate"),
                )
                append_flags = self.builder.select(
                    is_append,
                    ir.Constant(_I32, 64 | 1024),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.open_file.create.append"),
                )
                exclusive_flags = self.builder.select(
                    is_exclusive,
                    ir.Constant(_I32, 64 | 128),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.open_file.create.exclusive"),
                )
                flags = self.builder.or_(
                    access_flags,
                    self.builder.or_(
                        create_flags,
                        self.builder.or_(append_flags, exclusive_flags),
                    ),
                    name=self._fresh("unsafe.open_file.flags"),
                )
                path_i = self.builder.ptrtoint(
                    path,
                    _I64,
                    name=self._fresh("unsafe.open_file.path"),
                )
                flags_i = self.builder.zext(
                    flags,
                    _I64,
                    name=self._fresh("unsafe.open_file.flags.i64"),
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 257),
                    ir.Constant(_I64, -100),
                    path_i,
                    flags_i,
                    ir.Constant(_I64, 0o666),
                    zero,
                    zero,
                    name=self._fresh("unsafe.open_file.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.open_file supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "rename_file":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            source = self._unsafe_ptr_arg(expr.args[0])
            destination = self._unsafe_ptr_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                rename_fn = self._declare_external_function(
                    "rename", _I32, [_CSTR, _CSTR]
                )
                raw = self.builder.call(
                    rename_fn,
                    [source, destination],
                    name=self._fresh("unsafe.rename_file.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "rename_file")
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 82),
                    self.builder.ptrtoint(source, _I64),
                    self.builder.ptrtoint(destination, _I64),
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.rename_file.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.rename_file supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "chmod_file":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            path = self._unsafe_ptr_arg(expr.args[0])
            mode = self._unsafe_i32_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                chmod_fn = self._declare_external_function(
                    "chmod", _I32, [_CSTR, _I32]
                )
                raw = self.builder.call(
                    chmod_fn,
                    [path, mode],
                    name=self._fresh("unsafe.chmod_file.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "chmod_file")
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 90),
                    self.builder.ptrtoint(path, _I64),
                    self.builder.zext(mode, _I64),
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.chmod_file.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.chmod_file supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "sync_file":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            fd = self._unsafe_i32_arg(expr.args[0])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                fsync_fn = self._declare_external_function("fsync", _I32, [_I32])
                raw = self.builder.call(
                    fsync_fn,
                    [fd],
                    name=self._fresh("unsafe.sync_file.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "sync_file")
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 74),
                    self.builder.sext(fd, _I64),
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.sync_file.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.sync_file supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_socket_control(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "socket_open":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            family = self._unsafe_i64_arg(expr.args[0])
            socket_type = self._unsafe_i64_arg(expr.args[1])
            protocol = self._unsafe_i64_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                socket_fn = self._declare_external_function(
                    "socket", _I32, [_I32, _I32, _I32]
                )
                raw = self.builder.call(
                    socket_fn,
                    [
                        self.builder.trunc(family, _I32),
                        self.builder.trunc(socket_type, _I32),
                        self.builder.trunc(protocol, _I32),
                    ],
                    name=self._fresh("unsafe.socket_open.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "socket_open")
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 41),
                    family,
                    socket_type,
                    protocol,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.socket_open.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.socket_open supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "socket_connect":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd = self._unsafe_i64_arg(expr.args[0])
            address = self._unsafe_ptr_arg(expr.args[1])
            address_len = self._unsafe_i64_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                connect_fn = self._declare_external_function(
                    "connect", _I32, [_I32, _CSTR, _I32]
                )
                raw = self.builder.call(
                    connect_fn,
                    [
                        self.builder.trunc(fd, _I32),
                        address,
                        self.builder.trunc(address_len, _I32),
                    ],
                    name=self._fresh("unsafe.socket_connect.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "socket_connect")
            if platform_name == "linux" and machine == "x86_64":
                address_i = self.builder.ptrtoint(
                    address, _I64, name=self._fresh("unsafe.socket_connect.address")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 42),
                    fd,
                    address_i,
                    address_len,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.socket_connect.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.socket_connect supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "socket_bind":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd = self._unsafe_i64_arg(expr.args[0])
            address = self._unsafe_ptr_arg(expr.args[1])
            address_len = self._unsafe_i64_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                bind_fn = self._declare_external_function(
                    "bind", _I32, [_I32, _CSTR, _I32]
                )
                raw = self.builder.call(
                    bind_fn,
                    [
                        self.builder.trunc(fd, _I32),
                        address,
                        self.builder.trunc(address_len, _I32),
                    ],
                    name=self._fresh("unsafe.socket_bind.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "socket_bind")
            if platform_name == "linux" and machine == "x86_64":
                address_i = self.builder.ptrtoint(
                    address, _I64, name=self._fresh("unsafe.socket_bind.address")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 49),
                    fd,
                    address_i,
                    address_len,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.socket_bind.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.socket_bind supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "socket_listen":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            fd = self._unsafe_i64_arg(expr.args[0])
            backlog = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                listen_fn = self._declare_external_function(
                    "listen", _I32, [_I32, _I32]
                )
                raw = self.builder.call(
                    listen_fn,
                    [self.builder.trunc(fd, _I32), self.builder.trunc(backlog, _I32)],
                    name=self._fresh("unsafe.socket_listen.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "socket_listen")
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 50),
                    fd,
                    backlog,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.socket_listen.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.socket_listen supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "socket_setsockopt":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fd = self._unsafe_i64_arg(expr.args[0])
            level = self._unsafe_i64_arg(expr.args[1])
            option = self._unsafe_i64_arg(expr.args[2])
            value = self._unsafe_ptr_arg(expr.args[3])
            value_len = self._unsafe_i64_arg(expr.args[4])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                setsockopt_fn = self._declare_external_function(
                    "setsockopt", _I32, [_I32, _I32, _I32, _CSTR, _I32]
                )
                raw = self.builder.call(
                    setsockopt_fn,
                    [
                        self.builder.trunc(fd, _I32),
                        self.builder.trunc(level, _I32),
                        self.builder.trunc(option, _I32),
                        value,
                        self.builder.trunc(value_len, _I32),
                    ],
                    name=self._fresh("unsafe.socket_setsockopt.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "socket_setsockopt")
            if platform_name == "linux" and machine == "x86_64":
                value_i = self.builder.ptrtoint(
                    value, _I64, name=self._fresh("unsafe.socket_setsockopt.value")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 54),
                    fd,
                    level,
                    option,
                    value_i,
                    value_len,
                    zero,
                    name=self._fresh("unsafe.socket_setsockopt.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.socket_setsockopt supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "socket_getsockopt":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fd = self._unsafe_i64_arg(expr.args[0])
            level = self._unsafe_i64_arg(expr.args[1])
            option = self._unsafe_i64_arg(expr.args[2])
            value = self._unsafe_ptr_arg(expr.args[3])
            value_capacity = self._unsafe_i64_arg(expr.args[4])
            length_ptr = self.builder.alloca(
                _I32, name=self._fresh("unsafe.socket_getsockopt.length")
            )
            self.builder.store(self.builder.trunc(value_capacity, _I32), length_ptr)
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                getsockopt_fn = self._declare_external_function(
                    "getsockopt",
                    _I32,
                    [_I32, _I32, _I32, _CSTR, _I32.as_pointer()],
                )
                raw_i32 = self.builder.call(
                    getsockopt_fn,
                    [
                        self.builder.trunc(fd, _I32),
                        self.builder.trunc(level, _I32),
                        self.builder.trunc(option, _I32),
                        value,
                        length_ptr,
                    ],
                    name=self._fresh("unsafe.socket_getsockopt.i32"),
                )
                result = self._unsafe_darwin_errno_result(
                    raw_i32, "socket_getsockopt"
                )
            elif platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                result = self.builder.syscall6(
                    ir.Constant(_I64, 55),
                    fd,
                    level,
                    option,
                    self.builder.ptrtoint(value, _I64),
                    self.builder.ptrtoint(length_ptr, _I64),
                    zero,
                    name=self._fresh("unsafe.socket_getsockopt.syscall"),
                )
            else:
                raise NotImplementedError(
                    "pcc.unsafe.socket_getsockopt supports Darwin libSystem and Linux x86_64 raw syscalls"
                )
            length = self.builder.zext(
                self.builder.load(length_ptr),
                _I64,
                name=self._fresh("unsafe.socket_getsockopt.result.length"),
            )
            return self.builder.select(
                self.builder.icmp_signed("<", result, ir.Constant(_I64, 0)),
                result,
                length,
                name=self._fresh("unsafe.socket_getsockopt.result"),
            )
        if intrinsic == "fd_control":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd = self._unsafe_i64_arg(expr.args[0])
            command = self._unsafe_i64_arg(expr.args[1])
            value = self._unsafe_i64_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                fcntl_fn = self._declare_external_function(
                    "fcntl", _I32, [_I32, _I32], var_arg=True
                )
                raw = self.builder.call(
                    fcntl_fn,
                    [
                        self.builder.trunc(fd, _I32),
                        self.builder.trunc(command, _I32),
                        self.builder.trunc(value, _I32),
                    ],
                    name=self._fresh("unsafe.fd_control.i32"),
                )
                return self._unsafe_darwin_errno_result(raw, "fd_control")
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 72),
                    fd,
                    command,
                    value,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.fd_control.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.fd_control supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "eventfd_create":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            if not (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                return ir.Constant(_I64, -38)
            zero = ir.Constant(_I64, 0)
            return self.builder.syscall6(
                ir.Constant(_I64, 290),
                self._unsafe_i64_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                zero,
                zero,
                zero,
                zero,
                name=self._fresh("unsafe.eventfd_create.syscall"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_socket_io_poll(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic in ("socket_send", "socket_recv"):
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fd = self._unsafe_i64_arg(expr.args[0])
            buffer = self._unsafe_ptr_arg(expr.args[1])
            size = self._unsafe_i64_arg(expr.args[2])
            flags = self._unsafe_i64_arg(expr.args[3])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            symbol = "send" if intrinsic == "socket_send" else "recv"
            if platform_name == "darwin":
                io_fn = self._declare_external_function(
                    symbol, _I64, [_I32, _CSTR, _I64, _I32]
                )
                raw = self.builder.call(
                    io_fn,
                    [
                        self.builder.trunc(fd, _I32),
                        buffer,
                        size,
                        self.builder.trunc(flags, _I32),
                    ],
                    name=self._fresh("unsafe." + intrinsic + ".i64"),
                )
                return self._unsafe_darwin_errno_result(raw, intrinsic)
            if platform_name == "linux" and machine == "x86_64":
                buffer_i = self.builder.ptrtoint(
                    buffer, _I64, name=self._fresh("unsafe." + intrinsic + ".buffer")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 44 if intrinsic == "socket_send" else 45),
                    fd,
                    buffer_i,
                    size,
                    flags,
                    zero,
                    zero,
                    name=self._fresh("unsafe." + intrinsic + ".syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe."
                + intrinsic
                + " supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic in ("socket_accept", "socket_shutdown"):
            expected = 1 if intrinsic == "socket_accept" else 2
            self._unsafe_expect_arity(intrinsic, expr, expected)
            fd = self._unsafe_i64_arg(expr.args[0])
            how = ir.Constant(_I64, 0)
            if intrinsic == "socket_shutdown":
                how = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                if intrinsic == "socket_accept":
                    fn = self._declare_external_function(
                        "accept", _I32, [_I32, _CSTR, _CSTR]
                    )
                    raw = self.builder.call(
                        fn,
                        [
                            self.builder.trunc(fd, _I32),
                            ir.Constant(_CSTR, None),
                            ir.Constant(_CSTR, None),
                        ],
                        name=self._fresh("unsafe.socket_accept.i32"),
                    )
                else:
                    fn = self._declare_external_function(
                        "shutdown", _I32, [_I32, _I32]
                    )
                    raw = self.builder.call(
                        fn,
                        [self.builder.trunc(fd, _I32), self.builder.trunc(how, _I32)],
                        name=self._fresh("unsafe.socket_shutdown.i32"),
                    )
                return self._unsafe_darwin_errno_result(raw, intrinsic)
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 43 if intrinsic == "socket_accept" else 48),
                    fd,
                    how if intrinsic == "socket_shutdown" else zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe." + intrinsic + ".syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe."
                + intrinsic
                + " supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic in ("socket_sockname", "socket_peername"):
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd = self._unsafe_i64_arg(expr.args[0])
            address = self._unsafe_ptr_arg(expr.args[1])
            capacity = self._unsafe_i64_arg(expr.args[2])
            length_ptr = self.builder.alloca(
                _I32, name=self._fresh("unsafe." + intrinsic + ".length")
            )
            self.builder.store(self.builder.trunc(capacity, _I32), length_ptr)
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            symbol = "getsockname" if intrinsic == "socket_sockname" else "getpeername"
            if platform_name == "darwin":
                fn = self._declare_external_function(
                    symbol, _I32, [_I32, _CSTR, _I32.as_pointer()]
                )
                raw_i32 = self.builder.call(
                    fn,
                    [self.builder.trunc(fd, _I32), address, length_ptr],
                    name=self._fresh("unsafe." + intrinsic + ".i32"),
                )
                result = self._unsafe_darwin_errno_result(raw_i32, intrinsic)
            elif platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                result = self.builder.syscall6(
                    ir.Constant(_I64, 51 if intrinsic == "socket_sockname" else 52),
                    fd,
                    self.builder.ptrtoint(address, _I64),
                    self.builder.ptrtoint(length_ptr, _I64),
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe." + intrinsic + ".syscall"),
                )
            else:
                raise NotImplementedError(
                    "pcc.unsafe."
                    + intrinsic
                    + " supports Darwin libSystem and Linux x86_64 raw syscalls"
                )
            length = self.builder.zext(
                self.builder.load(length_ptr),
                _I64,
                name=self._fresh("unsafe." + intrinsic + ".result.length"),
            )
            return self.builder.select(
                self.builder.icmp_signed("<", result, ir.Constant(_I64, 0)),
                result,
                length,
                name=self._fresh("unsafe." + intrinsic + ".result"),
            )
        if intrinsic in ("poll_fd", "poll_readable_pair"):
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fd0 = self._unsafe_i64_arg(expr.args[0])
            pair = intrinsic == "poll_readable_pair"
            second = self._unsafe_i64_arg(expr.args[1])
            timeout_arg = expr.args[2] if pair else expr.args[2]
            timeout_ms = self._unsafe_i64_arg(timeout_arg)
            records = self.builder.alloca(
                ir.ArrayType(_I64, 2 if pair else 1),
                name=self._fresh("unsafe." + intrinsic + ".records"),
            )
            records_ptr = self.builder.bitcast(records, _CSTR)
            fd0_ptr = self._unsafe_typed_addr(records_ptr, ir.Constant(_I64, 0), _I32)
            events0_ptr = self._unsafe_typed_addr(records_ptr, ir.Constant(_I64, 4), _I16)
            revents0_ptr = self._unsafe_typed_addr(records_ptr, ir.Constant(_I64, 6), _I16)
            self.builder.store(self.builder.trunc(fd0, _I32), fd0_ptr)
            events0 = ir.Constant(_I16, 1) if pair else self.builder.trunc(second, _I16)
            self.builder.store(events0, events0_ptr)
            self.builder.store(ir.Constant(_I16, 0), revents0_ptr)
            revents1_ptr = None
            if pair:
                fd1_ptr = self._unsafe_typed_addr(records_ptr, ir.Constant(_I64, 8), _I32)
                events1_ptr = self._unsafe_typed_addr(records_ptr, ir.Constant(_I64, 12), _I16)
                revents1_ptr = self._unsafe_typed_addr(records_ptr, ir.Constant(_I64, 14), _I16)
                self.builder.store(self.builder.trunc(second, _I32), fd1_ptr)
                self.builder.store(ir.Constant(_I16, 1), events1_ptr)
                self.builder.store(ir.Constant(_I16, 0), revents1_ptr)
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            count = ir.Constant(_I64, 2 if pair else 1)
            if platform_name == "darwin":
                fn = self._declare_external_function("poll", _I32, [_CSTR, _I32, _I32])
                raw_i32 = self.builder.call(
                    fn,
                    [records_ptr, ir.Constant(_I32, 2 if pair else 1), self.builder.trunc(timeout_ms, _I32)],
                    name=self._fresh("unsafe." + intrinsic + ".i32"),
                )
                result = self._unsafe_darwin_errno_result(raw_i32, intrinsic)
            elif platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                result = self.builder.syscall6(
                    ir.Constant(_I64, 7),
                    self.builder.ptrtoint(records_ptr, _I64),
                    count,
                    timeout_ms,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe." + intrinsic + ".syscall"),
                )
            else:
                raise NotImplementedError(
                    "pcc.unsafe."
                    + intrinsic
                    + " supports Darwin libSystem and Linux x86_64 raw syscalls"
                )
            ready0 = self.builder.icmp_signed(
                "!=", self.builder.load(revents0_ptr), ir.Constant(_I16, 0)
            )
            value = self.builder.zext(self.builder.load(revents0_ptr), _I64)
            if pair:
                ready1 = self.builder.icmp_signed(
                    "!=", self.builder.load(revents1_ptr), ir.Constant(_I16, 0)
                )
                value = self.builder.or_(
                    self.builder.select(ready0, ir.Constant(_I64, 1), ir.Constant(_I64, 0)),
                    self.builder.select(ready1, ir.Constant(_I64, 2), ir.Constant(_I64, 0)),
                )
            return self.builder.select(
                self.builder.icmp_signed("<", result, ir.Constant(_I64, 0)),
                result,
                value,
                name=self._fresh("unsafe." + intrinsic + ".result"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_system_info(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "getpid":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            if self._target_sys_platform_text() == "darwin":
                getpid_fn = self._declare_external_function("getpid", _I32, [])
                raw = self.builder.call(
                    getpid_fn,
                    [],
                    name=self._fresh("unsafe.getpid.i32"),
                )
                return self.builder.sext(raw, _I64, name=self._fresh("unsafe.getpid"))
            if (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 39),
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.getpid.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.getpid supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "getcwd":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            buffer = self._unsafe_ptr_arg(expr.args[0])
            size = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                getcwd_fn = self._declare_external_function(
                    "getcwd",
                    _CSTR,
                    [_CSTR, _I64],
                )
                return self.builder.call(
                    getcwd_fn,
                    [buffer, size],
                    name=self._fresh("unsafe.getcwd"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                buffer_i = self.builder.ptrtoint(
                    buffer,
                    _I64,
                    name=self._fresh("unsafe.getcwd.buffer"),
                )
                raw = self.builder.syscall6(
                    ir.Constant(_I64, 79),
                    buffer_i,
                    size,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.getcwd.syscall"),
                )
                failed = self.builder.icmp_signed(
                    "<",
                    raw,
                    zero,
                    name=self._fresh("unsafe.getcwd.failed"),
                )
                return self.builder.select(
                    failed,
                    ir.Constant(_CSTR, None),
                    buffer,
                    name=self._fresh("unsafe.getcwd.result"),
                )
            raise NotImplementedError(
                "pcc.unsafe.getcwd supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "readlink":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            path = self._unsafe_ptr_arg(expr.args[0])
            buffer = self._unsafe_ptr_arg(expr.args[1])
            size = self._unsafe_i64_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                readlink_fn = self._declare_external_function(
                    "readlink",
                    _I64,
                    [_CSTR, _CSTR, _I64],
                )
                return self.builder.call(
                    readlink_fn,
                    [path, buffer, size],
                    name=self._fresh("unsafe.readlink"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                path_i = self.builder.ptrtoint(
                    path,
                    _I64,
                    name=self._fresh("unsafe.readlink.path"),
                )
                buffer_i = self.builder.ptrtoint(
                    buffer,
                    _I64,
                    name=self._fresh("unsafe.readlink.buffer"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 89),
                    path_i,
                    buffer_i,
                    size,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.readlink.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.readlink supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "mkdir":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            path = self._unsafe_ptr_arg(expr.args[0])
            mode = self._unsafe_i32_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                mkdir_fn = self._declare_external_function(
                    "mkdir",
                    _I32,
                    [_CSTR, _I32],
                )
                raw = self.builder.call(
                    mkdir_fn,
                    [path, mode],
                    name=self._fresh("unsafe.mkdir.i32"),
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.mkdir"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                path_i = self.builder.ptrtoint(
                    path,
                    _I64,
                    name=self._fresh("unsafe.mkdir.path"),
                )
                mode_i = self.builder.zext(
                    mode,
                    _I64,
                    name=self._fresh("unsafe.mkdir.mode"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 83),
                    path_i,
                    mode_i,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.mkdir.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.mkdir supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "unlinkat":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            path = self._unsafe_ptr_arg(expr.args[0])
            remove_directory = self._unsafe_i32_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                flags = self.builder.select(
                    self.builder.icmp_signed(
                        "!=",
                        remove_directory,
                        ir.Constant(_I32, 0),
                        name=self._fresh("unsafe.unlinkat.directory"),
                    ),
                    ir.Constant(_I32, 0x80),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.unlinkat.flags"),
                )
                unlinkat_fn = self._declare_external_function(
                    "unlinkat",
                    _I32,
                    [_I32, _CSTR, _I32],
                )
                raw = self.builder.call(
                    unlinkat_fn,
                    [ir.Constant(_I32, -2), path, flags],
                    name=self._fresh("unsafe.unlinkat.i32"),
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.unlinkat"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                path_i = self.builder.ptrtoint(
                    path,
                    _I64,
                    name=self._fresh("unsafe.unlinkat.path"),
                )
                flags = self.builder.select(
                    self.builder.icmp_signed(
                        "!=",
                        remove_directory,
                        ir.Constant(_I32, 0),
                        name=self._fresh("unsafe.unlinkat.directory"),
                    ),
                    ir.Constant(_I32, 0x200),
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.unlinkat.flags"),
                )
                flags_i = self.builder.zext(
                    flags,
                    _I64,
                    name=self._fresh("unsafe.unlinkat.flags.i64"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 263),
                    ir.Constant(_I64, -100),
                    path_i,
                    flags_i,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.unlinkat.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.unlinkat supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "uname":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            buffer = self._unsafe_ptr_arg(expr.args[0])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                uname_fn = self._declare_external_function("uname", _I32, [_CSTR])
                raw = self.builder.call(
                    uname_fn,
                    [buffer],
                    name=self._fresh("unsafe.uname.i32"),
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.uname"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                buffer_i = self.builder.ptrtoint(
                    buffer,
                    _I64,
                    name=self._fresh("unsafe.uname.buffer"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 63),
                    buffer_i,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.uname.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.uname supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "uname_field":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            buffer = self._unsafe_ptr_arg(expr.args[0])
            index = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                width = 256
            elif platform_name == "linux" and machine == "x86_64":
                width = 65
            else:
                raise NotImplementedError(
                    "pcc.unsafe.uname_field supports Darwin and Linux x86_64 utsname layouts"
                )
            offset = self.builder.mul(
                index,
                ir.Constant(_I64, width),
                name=self._fresh("unsafe.uname_field.offset"),
            )
            return self.builder.gep(
                buffer,
                [offset],
                inbounds=True,
                name=self._fresh("unsafe.uname_field"),
            )
        if intrinsic == "cpu_query":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            buffer = self._unsafe_ptr_arg(expr.args[0])
            size = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            zero = ir.Constant(_I64, 0)
            if platform_name == "darwin":
                size_slot = self.builder.alloca(
                    _I64,
                    name=self._fresh("unsafe.cpu_query.size"),
                )
                self.builder.store(ir.Constant(_I64, 4), size_slot)
                name_gv, _name_len = self._cstr_literal("hw.logicalcpu")
                sysctl_fn = self._declare_external_function(
                    "sysctlbyname",
                    _I32,
                    [_CSTR, _CSTR, _CSTR, _CSTR, _I64],
                )
                status = self.builder.call(
                    sysctl_fn,
                    [
                        self._ptr_to_cstr(name_gv),
                        buffer,
                        self.builder.bitcast(size_slot, _CSTR),
                        ir.Constant(_CSTR, None),
                        zero,
                    ],
                    name=self._fresh("unsafe.cpu_query.status"),
                )
                count_ptr = self.builder.bitcast(
                    buffer,
                    _I32.as_pointer(),
                    name=self._fresh("unsafe.cpu_query.count_ptr"),
                )
                count_i32 = self.builder.load(
                    count_ptr,
                    name=self._fresh("unsafe.cpu_query.count_i32"),
                )
                count = self.builder.sext(
                    count_i32,
                    _I64,
                    name=self._fresh("unsafe.cpu_query.count"),
                )
                ok_status = self.builder.icmp_signed(
                    "==",
                    status,
                    ir.Constant(_I32, 0),
                    name=self._fresh("unsafe.cpu_query.ok_status"),
                )
                positive = self.builder.icmp_signed(
                    ">",
                    count,
                    zero,
                    name=self._fresh("unsafe.cpu_query.positive"),
                )
                ok = self.builder.and_(
                    ok_status,
                    positive,
                    name=self._fresh("unsafe.cpu_query.ok"),
                )
                negative = self.builder.sub(
                    zero,
                    count,
                    name=self._fresh("unsafe.cpu_query.direct"),
                )
                return self.builder.select(
                    ok,
                    negative,
                    zero,
                    name=self._fresh("unsafe.cpu_query.result"),
                )
            if platform_name == "linux" and machine == "x86_64":
                buffer_i = self.builder.ptrtoint(
                    buffer,
                    _I64,
                    name=self._fresh("unsafe.cpu_query.buffer"),
                )
                raw = self.builder.syscall6(
                    ir.Constant(_I64, 204),
                    zero,
                    size,
                    buffer_i,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.cpu_query.syscall"),
                )
                failed = self.builder.icmp_signed(
                    "<",
                    raw,
                    zero,
                    name=self._fresh("unsafe.cpu_query.failed"),
                )
                return self.builder.select(
                    failed,
                    zero,
                    raw,
                    name=self._fresh("unsafe.cpu_query.result"),
                )
            raise NotImplementedError(
                "pcc.unsafe.cpu_query supports Darwin sysctlbyname and Linux x86_64 sched_getaffinity"
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_time_process_control(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "clock_gettime":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            logical_kind = self._unsafe_const_i64_arg(expr.args[0])
            if logical_kind not in (0, 1):
                raise NotImplementedError(
                    "pcc.unsafe.clock_gettime kind must be literal 0 or 1"
                )
            buffer = self._unsafe_ptr_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                clock_id = 0 if logical_kind == 0 else 6
                clock_fn = self._declare_external_function(
                    "clock_gettime",
                    _I32,
                    [_I32, _CSTR],
                )
                raw = self.builder.call(
                    clock_fn,
                    [ir.Constant(_I32, clock_id), buffer],
                    name=self._fresh("unsafe.clock_gettime.i32"),
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.clock_gettime"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                buffer_i = self.builder.ptrtoint(
                    buffer,
                    _I64,
                    name=self._fresh("unsafe.clock_gettime.buffer"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 228),
                    ir.Constant(_I64, logical_kind),
                    buffer_i,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.clock_gettime.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.clock_gettime supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "nanosleep":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            request = self._unsafe_ptr_arg(expr.args[0])
            remaining = self._unsafe_ptr_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                sleep_fn = self._declare_external_function(
                    "nanosleep",
                    _I32,
                    [_CSTR, _CSTR],
                )
                raw = self.builder.call(
                    sleep_fn,
                    [request, remaining],
                    name=self._fresh("unsafe.nanosleep.i32"),
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.nanosleep"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                request_i = self.builder.ptrtoint(
                    request,
                    _I64,
                    name=self._fresh("unsafe.nanosleep.request"),
                )
                remaining_i = self.builder.ptrtoint(
                    remaining,
                    _I64,
                    name=self._fresh("unsafe.nanosleep.remaining"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 35),
                    request_i,
                    remaining_i,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.nanosleep.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.nanosleep supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "waitpid":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            pid = self._unsafe_i64_arg(expr.args[0])
            status = self._unsafe_ptr_arg(expr.args[1])
            options = self._unsafe_i64_arg(expr.args[2])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                pid_i32 = self.builder.trunc(
                    pid, _I32, name=self._fresh("unsafe.waitpid.pid")
                )
                options_i32 = self.builder.trunc(
                    options, _I32, name=self._fresh("unsafe.waitpid.options")
                )
                wait_fn = self._declare_external_function(
                    "waitpid", _I32, [_I32, _CSTR, _I32]
                )
                raw_i32 = self.builder.call(
                    wait_fn,
                    [pid_i32, status, options_i32],
                    name=self._fresh("unsafe.waitpid.i32"),
                )
                raw = self.builder.sext(
                    raw_i32, _I64, name=self._fresh("unsafe.waitpid")
                )
                error_fn = self._declare_external_function("__error", _CSTR, [])
                error_ptr = self.builder.call(
                    error_fn, [], name=self._fresh("unsafe.waitpid.errno_ptr")
                )
                typed_error_ptr = self.builder.bitcast(
                    error_ptr,
                    _I32.as_pointer(),
                    name=self._fresh("unsafe.waitpid.errno_typed"),
                )
                error_i32 = self.builder.load(
                    typed_error_ptr, name=self._fresh("unsafe.waitpid.errno_i32")
                )
                error = self.builder.sext(
                    error_i32, _I64, name=self._fresh("unsafe.waitpid.errno")
                )
                negative_error = self.builder.sub(
                    ir.Constant(_I64, 0),
                    error,
                    name=self._fresh("unsafe.waitpid.negative_errno"),
                )
                failed = self.builder.icmp_signed(
                    "<",
                    raw,
                    ir.Constant(_I64, 0),
                    name=self._fresh("unsafe.waitpid.failed"),
                )
                return self.builder.select(
                    failed,
                    negative_error,
                    raw,
                    name=self._fresh("unsafe.waitpid.result"),
                )
            if platform_name == "linux" and machine == "x86_64":
                status_i = self.builder.ptrtoint(
                    status, _I64, name=self._fresh("unsafe.waitpid.status")
                )
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 61),
                    pid,
                    status_i,
                    options,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.wait4.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.waitpid supports Darwin libSystem and Linux x86_64 wait4 syscall"
            )
        if intrinsic == "kill":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            pid = self._unsafe_i64_arg(expr.args[0])
            signal_number = self._unsafe_i64_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                pid_i32 = self.builder.trunc(
                    pid, _I32, name=self._fresh("unsafe.kill.pid")
                )
                signal_i32 = self.builder.trunc(
                    signal_number, _I32, name=self._fresh("unsafe.kill.signal")
                )
                kill_fn = self._declare_external_function(
                    "kill", _I32, [_I32, _I32]
                )
                raw_i32 = self.builder.call(
                    kill_fn,
                    [pid_i32, signal_i32],
                    name=self._fresh("unsafe.kill.i32"),
                )
                raw = self.builder.sext(
                    raw_i32, _I64, name=self._fresh("unsafe.kill")
                )
                error_fn = self._declare_external_function("__error", _CSTR, [])
                error_ptr = self.builder.call(
                    error_fn, [], name=self._fresh("unsafe.kill.errno_ptr")
                )
                typed_error_ptr = self.builder.bitcast(
                    error_ptr,
                    _I32.as_pointer(),
                    name=self._fresh("unsafe.kill.errno_typed"),
                )
                error_i32 = self.builder.load(
                    typed_error_ptr, name=self._fresh("unsafe.kill.errno_i32")
                )
                error = self.builder.sext(
                    error_i32, _I64, name=self._fresh("unsafe.kill.errno")
                )
                negative_error = self.builder.sub(
                    ir.Constant(_I64, 0),
                    error,
                    name=self._fresh("unsafe.kill.negative_errno"),
                )
                failed = self.builder.icmp_signed(
                    "<",
                    raw,
                    ir.Constant(_I64, 0),
                    name=self._fresh("unsafe.kill.failed"),
                )
                return self.builder.select(
                    failed,
                    negative_error,
                    raw,
                    name=self._fresh("unsafe.kill.result"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                return self.builder.syscall6(
                    ir.Constant(_I64, 62),
                    pid,
                    signal_number,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.kill.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.kill supports Darwin libSystem and Linux x86_64 raw syscall"
            )
        if intrinsic == "process_exit":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            status = self._unsafe_i64_arg(expr.args[0])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                status_i32 = self.builder.trunc(
                    status, _I32, name=self._fresh("unsafe.process_exit.status")
                )
                exit_fn = self._declare_external_function("_exit", _VOID, [_I32])
                self.builder.call(exit_fn, [status_i32])
                return self._unsafe_void_result()
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                self.builder.syscall6(
                    ir.Constant(_I64, 231),
                    status,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.exit_group.syscall"),
                )
                return self._unsafe_void_result()
            raise NotImplementedError(
                "pcc.unsafe.process_exit supports Darwin libSystem and Linux x86_64 exit_group"
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_spawn_process_pipe(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "spawn_process_pipe":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            path = self._unsafe_ptr_arg(expr.args[0])
            argv = self._unsafe_ptr_arg(expr.args[1])
            envp = self._unsafe_ptr_arg(expr.args[2])
            parent_reads = self._unsafe_i64_arg(expr.args[3])
            fd_out = self._unsafe_ptr_arg(expr.args[4])
            fd_out_i32 = self.builder.bitcast(
                fd_out,
                _I32.as_pointer(),
                name=self._fresh("unsafe.spawn_pipe.fd_out"),
            )
            fds_ty = ir.ArrayType(_I32, 2)
            fds = self.builder.alloca(
                fds_ty,
                name=self._fresh("unsafe.spawn_pipe.fds"),
            )
            zero_i64 = ir.Constant(_I64, 0)
            fd0_addr = self.builder.gep(
                fds,
                [zero_i64, zero_i64],
                inbounds=True,
                name=self._fresh("unsafe.spawn_pipe.fd0.addr"),
            )
            fd1_addr = self.builder.gep(
                fds,
                [zero_i64, ir.Constant(_I64, 1)],
                inbounds=True,
                name=self._fresh("unsafe.spawn_pipe.fd1.addr"),
            )
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                pipe_fn = self._declare_external_function("pipe", _I32, [_CSTR])
                close_fn = self._declare_external_function("close", _I32, [_I32])
                actions_init_fn = self._declare_external_function(
                    "posix_spawn_file_actions_init", _I32, [_CSTR]
                )
                actions_destroy_fn = self._declare_external_function(
                    "posix_spawn_file_actions_destroy", _I32, [_CSTR]
                )
                actions_adddup2_fn = self._declare_external_function(
                    "posix_spawn_file_actions_adddup2",
                    _I32,
                    [_CSTR, _I32, _I32],
                )
                actions_addclose_fn = self._declare_external_function(
                    "posix_spawn_file_actions_addclose",
                    _I32,
                    [_CSTR, _I32],
                )
                spawn_fn = self._declare_external_function(
                    "posix_spawn",
                    _I32,
                    [_CSTR, _CSTR, _CSTR, _CSTR, _CSTR, _CSTR],
                )
                pid_slot = self.builder.alloca(
                    _I32, name=self._fresh("unsafe.spawn_pipe.pid")
                )
                actions_slot = self.builder.alloca(
                    _CSTR, name=self._fresh("unsafe.spawn_pipe.actions")
                )
                self.builder.store(ir.Constant(_CSTR, None), actions_slot)
                actions_ptr = self.builder.bitcast(actions_slot, _CSTR)

                finish_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.finish")
                )
                init_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.init")
                )
                pipe_fail_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.pipe_fail")
                )
                pipe_rc = self.builder.call(
                    pipe_fn,
                    [self.builder.bitcast(fds, _CSTR)],
                    name=self._fresh("unsafe.spawn_pipe.pipe"),
                )
                self.builder.cbranch(
                    self.builder.icmp_signed("==", pipe_rc, ir.Constant(_I32, 0)),
                    init_block,
                    pipe_fail_block,
                )

                self.builder.position_at_end(pipe_fail_block)
                self.builder.branch(finish_block)
                pipe_fail_end = self.builder.block

                self.builder.position_at_end(init_block)
                fd0 = self.builder.load(fd0_addr)
                fd1 = self.builder.load(fd1_addr)
                reads = self.builder.icmp_signed(
                    "!=", parent_reads, ir.Constant(_I64, 0)
                )
                parent_fd = self.builder.select(reads, fd0, fd1)
                child_fd = self.builder.select(reads, fd1, fd0)
                child_target = self.builder.select(
                    reads, ir.Constant(_I32, 1), ir.Constant(_I32, 0)
                )
                actions_rc = self.builder.call(
                    actions_init_fn,
                    [actions_ptr],
                    name=self._fresh("unsafe.spawn_pipe.actions_init"),
                )
                setup_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.setup")
                )
                init_fail_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.init_fail")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(
                        "==", actions_rc, ir.Constant(_I32, 0)
                    ),
                    setup_block,
                    init_fail_block,
                )

                self.builder.position_at_end(init_fail_block)
                self.builder.call(close_fn, [fd0])
                self.builder.call(close_fn, [fd1])
                init_error = self.builder.neg(self.builder.sext(actions_rc, _I64))
                self.builder.branch(finish_block)
                init_fail_end = self.builder.block

                self.builder.position_at_end(setup_block)
                dup_rc = self.builder.call(
                    actions_adddup2_fn,
                    [actions_ptr, child_fd, child_target],
                    name=self._fresh("unsafe.spawn_pipe.adddup2"),
                )
                close_parent_rc = self.builder.call(
                    actions_addclose_fn,
                    [actions_ptr, parent_fd],
                    name=self._fresh("unsafe.spawn_pipe.addclose_parent"),
                )
                close_child_rc = self.builder.call(
                    actions_addclose_fn,
                    [actions_ptr, child_fd],
                    name=self._fresh("unsafe.spawn_pipe.addclose_child"),
                )
                first_close_error = self.builder.select(
                    self.builder.icmp_signed(
                        "!=", close_parent_rc, ir.Constant(_I32, 0)
                    ),
                    close_parent_rc,
                    close_child_rc,
                )
                setup_error = self.builder.select(
                    self.builder.icmp_signed("!=", dup_rc, ir.Constant(_I32, 0)),
                    dup_rc,
                    first_close_error,
                )
                spawn_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.spawn")
                )
                setup_fail_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.setup_fail")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(
                        "==", setup_error, ir.Constant(_I32, 0)
                    ),
                    spawn_block,
                    setup_fail_block,
                )

                self.builder.position_at_end(setup_fail_block)
                self.builder.call(actions_destroy_fn, [actions_ptr])
                self.builder.call(close_fn, [fd0])
                self.builder.call(close_fn, [fd1])
                setup_error_i64 = self.builder.neg(
                    self.builder.sext(setup_error, _I64)
                )
                self.builder.branch(finish_block)
                setup_fail_end = self.builder.block

                self.builder.position_at_end(spawn_block)
                spawn_rc = self.builder.call(
                    spawn_fn,
                    [
                        self.builder.bitcast(pid_slot, _CSTR),
                        path,
                        actions_ptr,
                        ir.Constant(_CSTR, None),
                        argv,
                        envp,
                    ],
                    name=self._fresh("unsafe.spawn_pipe.spawn_call"),
                )
                self.builder.call(actions_destroy_fn, [actions_ptr])
                success_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.success")
                )
                spawn_fail_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.spawn_fail")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(
                        "==", spawn_rc, ir.Constant(_I32, 0)
                    ),
                    success_block,
                    spawn_fail_block,
                )

                self.builder.position_at_end(spawn_fail_block)
                self.builder.call(close_fn, [fd0])
                self.builder.call(close_fn, [fd1])
                spawn_error_i64 = self.builder.neg(
                    self.builder.sext(spawn_rc, _I64)
                )
                self.builder.branch(finish_block)
                spawn_fail_end = self.builder.block

                self.builder.position_at_end(success_block)
                self.builder.call(close_fn, [child_fd])
                self.builder.store(parent_fd, fd_out_i32)
                pid_i64 = self.builder.sext(self.builder.load(pid_slot), _I64)
                self.builder.branch(finish_block)
                success_end = self.builder.block

                self.builder.position_at_end(finish_block)
                result = self.builder.phi(
                    _I64, name=self._fresh("unsafe.spawn_pipe.result")
                )
                result.add_incoming(ir.Constant(_I64, -1), pipe_fail_end)
                result.add_incoming(init_error, init_fail_end)
                result.add_incoming(setup_error_i64, setup_fail_end)
                result.add_incoming(spawn_error_i64, spawn_fail_end)
                result.add_incoming(pid_i64, success_end)
                return result
            if platform_name == "linux" and machine == "x86_64":
                finish_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.finish")
                )
                fork_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.fork")
                )
                pipe_fail_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.pipe_fail")
                )
                fds_i64 = self.builder.ptrtoint(
                    self.builder.bitcast(fds, _CSTR), _I64
                )
                pipe_rc = self.builder.syscall6(
                    ir.Constant(_I64, 293),
                    fds_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    name=self._fresh("unsafe.spawn_pipe.pipe2"),
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(">=", pipe_rc, zero_i64),
                    fork_block,
                    pipe_fail_block,
                )
                self.builder.position_at_end(pipe_fail_block)
                self.builder.branch(finish_block)
                pipe_fail_end = self.builder.block

                self.builder.position_at_end(fork_block)
                fd0 = self.builder.load(fd0_addr)
                fd1 = self.builder.load(fd1_addr)
                reads = self.builder.icmp_signed(
                    "!=", parent_reads, zero_i64
                )
                parent_fd = self.builder.select(reads, fd0, fd1)
                child_fd = self.builder.select(reads, fd1, fd0)
                child_target = self.builder.select(
                    reads, ir.Constant(_I64, 1), zero_i64
                )
                fd0_i64 = self.builder.sext(fd0, _I64)
                fd1_i64 = self.builder.sext(fd1, _I64)
                parent_fd_i64 = self.builder.sext(parent_fd, _I64)
                child_fd_i64 = self.builder.sext(child_fd, _I64)
                fork_result = self.builder.syscall6(
                    ir.Constant(_I64, 57),
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    name=self._fresh("unsafe.spawn_pipe.fork_call"),
                )
                child_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.child")
                )
                parent_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.parent")
                )
                fork_fail_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.fork_fail")
                )
                fork_nonnegative = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn_pipe.fork_nonnegative")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(">=", fork_result, zero_i64),
                    fork_nonnegative,
                    fork_fail_block,
                )
                self.builder.position_at_end(fork_nonnegative)
                self.builder.cbranch(
                    self.builder.icmp_signed("==", fork_result, zero_i64),
                    child_block,
                    parent_block,
                )

                self.builder.position_at_end(fork_fail_block)
                self.builder.syscall6(
                    ir.Constant(_I64, 3), fd0_i64, zero_i64, zero_i64,
                    zero_i64, zero_i64, zero_i64,
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 3), fd1_i64, zero_i64, zero_i64,
                    zero_i64, zero_i64, zero_i64,
                )
                self.builder.branch(finish_block)
                fork_fail_end = self.builder.block

                self.builder.position_at_end(child_block)
                self.builder.syscall6(
                    ir.Constant(_I64, 33), child_fd_i64, child_target,
                    zero_i64, zero_i64, zero_i64, zero_i64,
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 3), fd0_i64, zero_i64, zero_i64,
                    zero_i64, zero_i64, zero_i64,
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 3), fd1_i64, zero_i64, zero_i64,
                    zero_i64, zero_i64, zero_i64,
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 59),
                    self.builder.ptrtoint(path, _I64),
                    self.builder.ptrtoint(argv, _I64),
                    self.builder.ptrtoint(envp, _I64),
                    zero_i64,
                    zero_i64,
                    zero_i64,
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 231),
                    ir.Constant(_I64, 127),
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                    zero_i64,
                )
                self.builder.unreachable()

                self.builder.position_at_end(parent_block)
                self.builder.syscall6(
                    ir.Constant(_I64, 3), child_fd_i64, zero_i64, zero_i64,
                    zero_i64, zero_i64, zero_i64,
                )
                self.builder.store(parent_fd, fd_out_i32)
                self.builder.branch(finish_block)
                parent_end = self.builder.block

                self.builder.position_at_end(finish_block)
                result = self.builder.phi(
                    _I64, name=self._fresh("unsafe.spawn_pipe.result")
                )
                result.add_incoming(pipe_rc, pipe_fail_end)
                result.add_incoming(fork_result, fork_fail_end)
                result.add_incoming(fork_result, parent_end)
                return result
            raise NotImplementedError(
                "pcc.unsafe.spawn_process_pipe supports Darwin posix_spawn and Linux x86_64 raw syscalls"
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_spawn_process(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "spawn_process":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            path = self._unsafe_ptr_arg(expr.args[0])
            argv = self._unsafe_ptr_arg(expr.args[1])
            envp = self._unsafe_ptr_arg(expr.args[2])
            capture = self._unsafe_i64_arg(expr.args[3])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                pid_slot = self.builder.alloca(
                    _I32, name=self._fresh("unsafe.spawn.pid")
                )
                actions_slot = self.builder.alloca(
                    _CSTR, name=self._fresh("unsafe.spawn.actions")
                )
                attr_slot = self.builder.alloca(
                    _CSTR, name=self._fresh("unsafe.spawn.attr")
                )
                self.builder.store(ir.Constant(_CSTR, None), actions_slot)
                self.builder.store(ir.Constant(_CSTR, None), attr_slot)

                actions_init_fn = self._declare_external_function(
                    "posix_spawn_file_actions_init", _I32, [_CSTR]
                )
                actions_destroy_fn = self._declare_external_function(
                    "posix_spawn_file_actions_destroy", _I32, [_CSTR]
                )
                actions_addopen_fn = self._declare_external_function(
                    "posix_spawn_file_actions_addopen",
                    _I32,
                    [_CSTR, _I32, _CSTR, _I32, _I32],
                )
                attr_init_fn = self._declare_external_function(
                    "posix_spawnattr_init", _I32, [_CSTR]
                )
                attr_destroy_fn = self._declare_external_function(
                    "posix_spawnattr_destroy", _I32, [_CSTR]
                )
                attr_setpgroup_fn = self._declare_external_function(
                    "posix_spawnattr_setpgroup", _I32, [_CSTR, _I32]
                )
                attr_setflags_fn = self._declare_external_function(
                    "posix_spawnattr_setflags", _I32, [_CSTR, _I16]
                )
                spawn_fn = self._declare_external_function(
                    "posix_spawn",
                    _I32,
                    [_CSTR, _CSTR, _CSTR, _CSTR, _CSTR, _CSTR],
                )

                actions_rc = self.builder.call(
                    actions_init_fn,
                    [self.builder.bitcast(actions_slot, _CSTR)],
                    name=self._fresh("unsafe.spawn.actions_init"),
                )
                attr_rc = self.builder.call(
                    attr_init_fn,
                    [self.builder.bitcast(attr_slot, _CSTR)],
                    name=self._fresh("unsafe.spawn.attr_init"),
                )
                actions_ok = self.builder.icmp_signed(
                    "==", actions_rc, ir.Constant(_I32, 0)
                )
                attr_ok = self.builder.icmp_signed(
                    "==", attr_rc, ir.Constant(_I32, 0)
                )
                both_ok = self.builder.and_(actions_ok, attr_ok)
                setup_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.setup")
                )
                init_failure_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.init_failure")
                )
                finish_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.finish")
                )
                self.builder.cbranch(both_ok, setup_block, init_failure_block)

                self.builder.position_at_end(init_failure_block)
                first_init_error = self.builder.select(
                    self.builder.icmp_signed(
                        "!=", actions_rc, ir.Constant(_I32, 0)
                    ),
                    actions_rc,
                    attr_rc,
                )
                negative_init_error = self.builder.neg(
                    self.builder.sext(first_init_error, _I64)
                )
                cleanup_actions_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.cleanup_actions")
                )
                after_actions_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.after_actions")
                )
                self.builder.cbranch(
                    actions_ok, cleanup_actions_block, after_actions_block
                )
                self.builder.position_at_end(cleanup_actions_block)
                self.builder.call(
                    actions_destroy_fn,
                    [self.builder.bitcast(actions_slot, _CSTR)],
                )
                self.builder.branch(after_actions_block)
                self.builder.position_at_end(after_actions_block)
                cleanup_attr_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.cleanup_attr")
                )
                init_done_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.init_done")
                )
                self.builder.cbranch(attr_ok, cleanup_attr_block, init_done_block)
                self.builder.position_at_end(cleanup_attr_block)
                self.builder.call(
                    attr_destroy_fn,
                    [self.builder.bitcast(attr_slot, _CSTR)],
                )
                self.builder.branch(init_done_block)
                self.builder.position_at_end(init_done_block)
                self.builder.branch(finish_block)

                self.builder.position_at_end(setup_block)
                capture_needed = self.builder.icmp_signed(
                    "!=", capture, ir.Constant(_I64, 0)
                )
                capture_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.capture")
                )
                after_capture_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.after_capture")
                )
                self.builder.cbranch(
                    capture_needed, capture_block, after_capture_block
                )
                self.builder.position_at_end(capture_block)
                devnull = self._ptr_to_cstr(
                    self._cstr_global(
                        "/dev/null", "unsafe.spawn.devnull"
                    )
                )
                open_stdout = self.builder.call(
                    actions_addopen_fn,
                    [
                        self.builder.bitcast(actions_slot, _CSTR),
                        ir.Constant(_I32, 1),
                        devnull,
                        ir.Constant(_I32, 1),
                        ir.Constant(_I32, 0),
                    ],
                    name=self._fresh("unsafe.spawn.open_stdout"),
                )
                open_stderr = self.builder.call(
                    actions_addopen_fn,
                    [
                        self.builder.bitcast(actions_slot, _CSTR),
                        ir.Constant(_I32, 2),
                        devnull,
                        ir.Constant(_I32, 1),
                        ir.Constant(_I32, 0),
                    ],
                    name=self._fresh("unsafe.spawn.open_stderr"),
                )
                capture_error = self.builder.select(
                    self.builder.icmp_signed(
                        "!=", open_stdout, ir.Constant(_I32, 0)
                    ),
                    open_stdout,
                    open_stderr,
                )
                self.builder.branch(after_capture_block)
                capture_end_block = self.builder.block
                self.builder.position_at_end(after_capture_block)
                setup_error = self.builder.phi(
                    _I32, name=self._fresh("unsafe.spawn.capture_error")
                )
                setup_error.add_incoming(ir.Constant(_I32, 0), setup_block)
                setup_error.add_incoming(capture_error, capture_end_block)
                pgroup_rc = self.builder.call(
                    attr_setpgroup_fn,
                    [
                        self.builder.bitcast(attr_slot, _CSTR),
                        ir.Constant(_I32, 0),
                    ],
                    name=self._fresh("unsafe.spawn.setpgroup"),
                )
                flags_rc = self.builder.call(
                    attr_setflags_fn,
                    [
                        self.builder.bitcast(attr_slot, _CSTR),
                        ir.Constant(_I16, 2),
                    ],
                    name=self._fresh("unsafe.spawn.setflags"),
                )
                setup_or_pgroup = self.builder.select(
                    self.builder.icmp_signed(
                        "!=", setup_error, ir.Constant(_I32, 0)
                    ),
                    setup_error,
                    pgroup_rc,
                )
                full_setup_error = self.builder.select(
                    self.builder.icmp_signed(
                        "!=", setup_or_pgroup, ir.Constant(_I32, 0)
                    ),
                    setup_or_pgroup,
                    flags_rc,
                )
                spawn_do_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.do")
                )
                spawn_skip_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.skip")
                )
                spawn_join_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.join")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(
                        "==", full_setup_error, ir.Constant(_I32, 0)
                    ),
                    spawn_do_block,
                    spawn_skip_block,
                )
                self.builder.position_at_end(spawn_do_block)
                spawned_rc = self.builder.call(
                    spawn_fn,
                    [
                        self.builder.bitcast(pid_slot, _CSTR),
                        path,
                        self.builder.bitcast(actions_slot, _CSTR),
                        self.builder.bitcast(attr_slot, _CSTR),
                        argv,
                        envp,
                    ],
                    name=self._fresh("unsafe.spawn.call"),
                )
                self.builder.branch(spawn_join_block)
                spawn_end_block = self.builder.block
                self.builder.position_at_end(spawn_skip_block)
                self.builder.branch(spawn_join_block)
                spawn_skip_end_block = self.builder.block
                self.builder.position_at_end(spawn_join_block)
                spawn_rc = self.builder.phi(
                    _I32, name=self._fresh("unsafe.spawn.error")
                )
                spawn_rc.add_incoming(spawned_rc, spawn_end_block)
                spawn_rc.add_incoming(full_setup_error, spawn_skip_end_block)
                self.builder.call(
                    actions_destroy_fn,
                    [self.builder.bitcast(actions_slot, _CSTR)],
                )
                self.builder.call(
                    attr_destroy_fn,
                    [self.builder.bitcast(attr_slot, _CSTR)],
                )
                pid_i32 = self.builder.load(
                    pid_slot, name=self._fresh("unsafe.spawn.pid_value")
                )
                pid = self.builder.sext(
                    pid_i32, _I64, name=self._fresh("unsafe.spawn.pid_i64")
                )
                spawn_error_i64 = self.builder.sext(spawn_rc, _I64)
                setup_result = self.builder.select(
                    self.builder.icmp_signed(
                        "==", spawn_rc, ir.Constant(_I32, 0)
                    ),
                    pid,
                    self.builder.neg(spawn_error_i64),
                    name=self._fresh("unsafe.spawn.result"),
                )
                self.builder.branch(finish_block)
                setup_done_block = self.builder.block

                self.builder.position_at_end(finish_block)
                result = self.builder.phi(
                    _I64, name=self._fresh("unsafe.spawn.final")
                )
                result.add_incoming(negative_init_error, init_done_block)
                result.add_incoming(setup_result, setup_done_block)
                return result
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                fork_result = self.builder.syscall6(
                    ir.Constant(_I64, 57),
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.fork"),
                )
                child_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.child")
                )
                parent_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.parent")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(
                        "==", fork_result, ir.Constant(_I64, 0)
                    ),
                    child_block,
                    parent_block,
                )
                self.builder.position_at_end(child_block)
                self.builder.syscall6(
                    ir.Constant(_I64, 109),
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.setpgid"),
                )
                capture_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.child_capture")
                )
                exec_block = self.current_function.append_basic_block(
                    self._fresh("unsafe.spawn.child_exec")
                )
                self.builder.cbranch(
                    self.builder.icmp_signed(
                        "!=", capture, ir.Constant(_I64, 0)
                    ),
                    capture_block,
                    exec_block,
                )
                self.builder.position_at_end(capture_block)
                devnull = self._ptr_to_cstr(
                    self._cstr_global(
                        "/dev/null", "unsafe.spawn.linux.devnull"
                    )
                )
                devnull_i = self.builder.ptrtoint(devnull, _I64)
                null_fd = self.builder.syscall6(
                    ir.Constant(_I64, 2),
                    devnull_i,
                    ir.Constant(_I64, 1),
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.open_devnull"),
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 33),
                    null_fd,
                    ir.Constant(_I64, 1),
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.dup_stdout"),
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 33),
                    null_fd,
                    ir.Constant(_I64, 2),
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.dup_stderr"),
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 3),
                    null_fd,
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.close_devnull"),
                )
                self.builder.branch(exec_block)
                self.builder.position_at_end(exec_block)
                self.builder.syscall6(
                    ir.Constant(_I64, 59),
                    self.builder.ptrtoint(path, _I64),
                    self.builder.ptrtoint(argv, _I64),
                    self.builder.ptrtoint(envp, _I64),
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.execve"),
                )
                self.builder.syscall6(
                    ir.Constant(_I64, 231),
                    ir.Constant(_I64, 127),
                    zero,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.spawn.child_exit"),
                )
                self.builder.unreachable()

                self.builder.position_at_end(parent_block)
                return fork_result
            raise NotImplementedError(
                "pcc.unsafe.spawn_process supports Darwin posix_spawn and Linux x86_64 raw process syscalls"
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_env_access_stat(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "stack_alloc":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            size = self._unsafe_const_i64_arg(expr.args[0])
            if size <= 0 or size > 1048576:
                raise NotImplementedError(
                    "pcc.unsafe.stack_alloc requires a literal size in 1..1048576"
                )
            slot_ty = ir.ArrayType(_I8, size)
            slot = self.builder.alloca(
                slot_ty,
                name=self._fresh("unsafe.stack"),
            )
            zero = ir.Constant(_I64, 0)
            return self.builder.gep(
                slot,
                [zero, zero],
                inbounds=True,
                name=self._fresh("unsafe.stack.ptr"),
            )
        if intrinsic == "strlen":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            strlen_fn = self._declare_external_function(
                "strlen",
                _I64,
                [_CSTR],
            )
            return self.builder.call(
                strlen_fn,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.strlen"),
            )
        if intrinsic == "getenv":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            getenv_fn = self._declare_external_function(
                "getenv",
                _CSTR,
                [_CSTR],
            )
            return self.builder.call(
                getenv_fn,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.getenv"),
            )
        if intrinsic == "setenv":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            setenv_fn = self._declare_external_function(
                "setenv",
                _I32,
                [_CSTR, _CSTR, _I32],
            )
            raw = self.builder.call(
                setenv_fn,
                [
                    self._unsafe_ptr_arg(expr.args[0]),
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i32_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.setenv.i32"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.setenv"),
            )
        if intrinsic == "unsetenv":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            unsetenv_fn = self._declare_external_function(
                "unsetenv",
                _I32,
                [_CSTR],
            )
            raw = self.builder.call(
                unsetenv_fn,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.unsetenv.i32"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.unsetenv"),
            )
        if intrinsic == "initial_environ":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                environ_global = self._declare_external_global("environ", _CSTR)
                return self.builder.load(
                    environ_global,
                    name=self._fresh("unsafe.environ"),
                )
            if platform_name == "linux" and machine == "x86_64":
                envp_global = self.module.globals.get("pcc_initial_envp")
                if not isinstance(envp_global, ir.GlobalVariable):
                    envp_global = self._declare_external_global(
                        "pcc_initial_envp", _CSTR
                    )
                return self.builder.load(
                    envp_global,
                    name=self._fresh("unsafe.environ"),
                )
            raise NotImplementedError(
                "pcc.unsafe.initial_environ supports Darwin libSystem and Linux x86_64 startup envp"
            )
        if intrinsic == "access":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            path = self._unsafe_ptr_arg(expr.args[0])
            mode = self._unsafe_i32_arg(expr.args[1])
            platform_name = self._target_sys_platform_text()
            machine = self._target_machine_text()
            if platform_name == "darwin":
                access_fn = self._declare_external_function(
                    "access",
                    _I32,
                    [_CSTR, _I32],
                )
                raw = self.builder.call(
                    access_fn,
                    [path, mode],
                    name=self._fresh("unsafe.access.i32"),
                )
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.access"),
                )
            if platform_name == "linux" and machine == "x86_64":
                zero = ir.Constant(_I64, 0)
                path_i = self.builder.ptrtoint(
                    path,
                    _I64,
                    name=self._fresh("unsafe.access.path"),
                )
                mode_i = self.builder.sext(
                    mode,
                    _I64,
                    name=self._fresh("unsafe.access.mode"),
                )
                return self.builder.syscall6(
                    ir.Constant(_I64, 21),
                    path_i,
                    mode_i,
                    zero,
                    zero,
                    zero,
                    zero,
                    name=self._fresh("unsafe.access.syscall"),
                )
            raise NotImplementedError(
                "pcc.unsafe.access supports Darwin libSystem and Linux x86_64 raw syscalls"
            )
        if intrinsic == "stat_kind":
            return self._emit_unsafe_stat_kind(expr)
        if intrinsic == "stat_mtime":
            return self._emit_unsafe_stat_mtime(expr)
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_indirect_calls_a(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "call_ptr1":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            fnty = ir.FunctionType(_CSTR, [_CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr1.fn"),
            )
            return self.builder.call(
                callee,
                [self._unsafe_ptr_arg(expr.args[1])],
                name=self._fresh("unsafe.call.ptr1"),
            )
        if intrinsic == "call_ptr0":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            fnty = ir.FunctionType(_CSTR, [])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr0.fn"),
            )
            return self.builder.call(
                callee,
                [],
                name=self._fresh("unsafe.call.ptr0"),
            )
        if intrinsic == "call_void_ptr0":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            fnty = ir.FunctionType(ir.VoidType(), [])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.void.ptr0.fn"),
            )
            return self.builder.call(callee, [])
        if intrinsic == "call_void_ptr1":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            fnty = ir.FunctionType(ir.VoidType(), [_CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.void.ptr1.fn"),
            )
            return self.builder.call(
                callee,
                [self._unsafe_ptr_arg(expr.args[1])],
            )
        if intrinsic == "call_void_ptr2":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(ir.VoidType(), [_CSTR, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.void.ptr2.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                ],
            )
        if intrinsic == "call_void_ptr_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(ir.VoidType(), [_CSTR, _I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.void.ptr.i64.ptr.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                ],
            )
        if intrinsic == "call_ptr2":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr2.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.ptr2"),
            )
        if intrinsic == "call_ptr_ptr_i64":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr.ptr.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.ptr.ptr.i64"),
            )
        if intrinsic == "call_ptr_ptr_ptr_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR, _I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr.ptr.ptr.i64.ptr.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_i64_arg(expr.args[3]),
                    self._unsafe_ptr_arg(expr.args[4]),
                ],
                name=self._fresh("unsafe.call.ptr.ptr.ptr.i64.ptr"),
            )
        if intrinsic == "call_ptr_ptr_ptr_i32":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR, _I32])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr.ptr.ptr.i32.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_i32_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.ptr.ptr.ptr.i32"),
            )
        if intrinsic == "call_ptr3":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr3.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.ptr3"),
            )
        if intrinsic == "call_ptr4":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _CSTR, _CSTR, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr4.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_ptr_arg(expr.args[4]),
                ],
                name=self._fresh("unsafe.call.ptr4"),
            )
        if intrinsic == "call_i64_i64":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            fnty = ir.FunctionType(_I64, [_I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.i64.fn"),
            )
            return self.builder.call(
                callee,
                [self._unsafe_i64_arg(expr.args[1])],
                name=self._fresh("unsafe.call.i64.i64"),
            )
        if intrinsic == "call_i64_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_I64, [_I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.i64.ptr.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_i64_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.i64.i64.ptr"),
            )
        if intrinsic == "call_i32_ptr1":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            fnty = ir.FunctionType(_I32, [_CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.ptr1.fn"),
            )
            raw = self.builder.call(
                callee,
                [self._unsafe_ptr_arg(expr.args[1])],
                name=self._fresh("unsafe.call.i32.ptr1.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.ptr1"),
            )
        if intrinsic == "call_i32_ptr_i64":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_I32, [_CSTR, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.ptr.i64.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.i32.ptr.i64.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.ptr.i64"),
            )
        if intrinsic == "call_i32_ptr_i32":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_I32, [_CSTR, _I32])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.ptr.i32.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i32_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.i32.ptr.i32.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.ptr.i32"),
            )
        if intrinsic == "call_i32_ptr_i32_i32":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I32, [_CSTR, _I32, _I32])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.ptr.i32.i32.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i32_arg(expr.args[2]),
                    self._unsafe_i32_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i32.ptr.i32.i32.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.ptr.i32.i32"),
            )
        if intrinsic == "call_i32_ptr_i32_i32_i32":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fnty = ir.FunctionType(_I32, [_CSTR, _I32, _I32, _I32])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.ptr.i32.i32.i32.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i32_arg(expr.args[2]),
                    self._unsafe_i32_arg(expr.args[3]),
                    self._unsafe_i32_arg(expr.args[4]),
                ],
                name=self._fresh("unsafe.call.i32.ptr.i32.i32.i32.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.ptr.i32.i32.i32"),
            )
        if intrinsic == "call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32":
            self._unsafe_expect_arity(intrinsic, expr, 9)
            fnty = ir.FunctionType(
                _I32,
                [_CSTR, _I32, _I32, _I32, _I32, _I32, _CSTR, _I32],
            )
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.deflate.init2.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i32_arg(expr.args[2]),
                    self._unsafe_i32_arg(expr.args[3]),
                    self._unsafe_i32_arg(expr.args[4]),
                    self._unsafe_i32_arg(expr.args[5]),
                    self._unsafe_i32_arg(expr.args[6]),
                    self._unsafe_ptr_arg(expr.args[7]),
                    self._unsafe_i32_arg(expr.args[8]),
                ],
                name=self._fresh("unsafe.call.i32.deflate.init2.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.deflate.init2"),
            )
        if intrinsic == "call_i32_i32_ptr_i64":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I32, [_I32, _CSTR, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.i32.ptr.i64.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_i32_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_i64_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i32.i32.ptr.i64.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.i32.ptr.i64"),
            )
        if intrinsic == "call_i32_i64_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I32, [_I64, _I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.i64.i64.ptr.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_i64_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i32.i64.i64.ptr.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.i64.i64.ptr"),
            )
        if intrinsic == "call_i32_i64_i32_i64":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I32, [_I64, _I32, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i32.i64.i32.i64.fn"),
            )
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_i64_arg(expr.args[1]),
                    self._unsafe_i32_arg(expr.args[2]),
                    self._unsafe_i64_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i32.i64.i32.i64.raw"),
            )
            return self.builder.sext(
                raw,
                _I64,
                name=self._fresh("unsafe.call.i32.i64.i32.i64"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_indirect_calls_b(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "call_i64_ptr1":
            self._unsafe_expect_arity(intrinsic, expr, 2)
            fnty = ir.FunctionType(_I64, [_CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr1.fn"),
            )
            return self.builder.call(
                callee,
                [self._unsafe_ptr_arg(expr.args[1])],
                name=self._fresh("unsafe.call.i64.ptr1"),
            )
        if intrinsic == "call_i64_ptr_ptr_ptr_i64":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fnty = ir.FunctionType(_I64, [_CSTR, _CSTR, _CSTR, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr.ptr.ptr.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_i64_arg(expr.args[4]),
                ],
                name=self._fresh("unsafe.call.i64.ptr.ptr.ptr.i64"),
            )
        if intrinsic == "call_i64_ptr_i64":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_I64, [_CSTR, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.i64.ptr.i64"),
            )
        if intrinsic == "call_i64_ptr2":
            self._unsafe_expect_arity(intrinsic, expr, 3)
            fnty = ir.FunctionType(_I64, [_CSTR, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr2.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                ],
                name=self._fresh("unsafe.call.i64.ptr2"),
            )
        if intrinsic == "call_i64_ptr4_i64_i64":
            self._unsafe_expect_arity(intrinsic, expr, 7)
            fnty = ir.FunctionType(_I64, [_CSTR, _CSTR, _CSTR, _CSTR, _I64, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr4.i64.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_ptr_arg(expr.args[4]),
                    self._unsafe_i64_arg(expr.args[5]),
                    self._unsafe_i64_arg(expr.args[6]),
                ],
                name=self._fresh("unsafe.call.i64.ptr4.i64.i64"),
            )
        if intrinsic == "call_i64_ptr3_i64_i64_i64":
            self._unsafe_expect_arity(intrinsic, expr, 7)
            fnty = ir.FunctionType(_I64, [_CSTR, _CSTR, _CSTR, _I64, _I64, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr3.i64.i64.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_i64_arg(expr.args[4]),
                    self._unsafe_i64_arg(expr.args[5]),
                    self._unsafe_i64_arg(expr.args[6]),
                ],
                name=self._fresh("unsafe.call.i64.ptr3.i64.i64.i64"),
            )
        if intrinsic == "call_ptr_i64_i64":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_CSTR, [_CSTR, _I64, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.ptr.i64.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_i64_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.ptr.i64.i64"),
            )
        if intrinsic == "call_i64_ptr3":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I64, [_CSTR, _CSTR, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr3.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i64.ptr3"),
            )
        if intrinsic == "call_i64_i64_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I64, [_I64, _I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.i64.i64.ptr.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_i64_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i64.i64.i64.ptr"),
            )
        if (
            intrinsic == "call_variadic_i64_ptr_i64_ptr"
            or intrinsic == "call_variadic_i64_ptr_i64_i64"
            or intrinsic == "call_variadic_i32_ptr_i32_ptr"
            or intrinsic == "call_variadic_i32_ptr_i32_i64"
        ):
            # Real C variadic callee: the fixed prototype form puts the
            # trailing value in a register, but the Apple arm64 ABI passes
            # unnamed arguments on the stack.  Declaring var_arg lets the
            # backend place the value per the callee's ABI.
            self._unsafe_expect_arity(intrinsic, expr, 4)
            # Dynamic params/flag form on purpose: the literal-list +
            # literal-True shape lowers to ir.FunctionType.__init__2_varargs,
            # which the self-host scaffold never defines (stage1 link error).
            # The dynamic form routes through FunctionType___init___dyn,
            # the shape user_function_decl_lowering already links against.
            exact_i32_shape = (
                intrinsic == "call_variadic_i32_ptr_i32_ptr"
                or intrinsic == "call_variadic_i32_ptr_i32_i64"
            )
            variadic_return = _I64
            variadic_fixed_int = _I64
            if exact_i32_shape:
                variadic_return = _I32
                variadic_fixed_int = _I32
            variadic_params = [_CSTR, variadic_fixed_int]
            variadic_flag = True
            fnty = ir.FunctionType(
                variadic_return,
                variadic_params,
                var_arg=variadic_flag,
            )
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.variadic.fn"),
            )
            if (
                intrinsic == "call_variadic_i64_ptr_i64_ptr"
                or intrinsic == "call_variadic_i32_ptr_i32_ptr"
            ):
                trailing = self._unsafe_ptr_arg(expr.args[3])
            else:
                trailing = self._unsafe_i64_arg(expr.args[3])
            if exact_i32_shape:
                fixed_int = self._unsafe_i32_arg(expr.args[2])
            else:
                fixed_int = self._unsafe_i64_arg(expr.args[2])
            raw = self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    fixed_int,
                    trailing,
                ],
                name=self._fresh("unsafe.call.variadic.raw"),
            )
            if exact_i32_shape:
                return self.builder.sext(
                    raw,
                    _I64,
                    name=self._fresh("unsafe.call.variadic.i32"),
                )
            return raw
        if intrinsic == "call_i64_ptr_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I64, [_CSTR, _I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr.i64.ptr.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i64.ptr.i64.ptr"),
            )
        if intrinsic == "call_i64_ptr_i64_i64":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            fnty = ir.FunctionType(_I64, [_CSTR, _I64, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr.i64.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_i64_arg(expr.args[3]),
                ],
                name=self._fresh("unsafe.call.i64.ptr.i64.i64"),
            )
        if intrinsic == "call_i64_ptr_i64_ptr_i64":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fnty = ir.FunctionType(_I64, [_CSTR, _I64, _CSTR, _I64])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr.i64.ptr.i64.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_i64_arg(expr.args[4]),
                ],
                name=self._fresh("unsafe.call.i64.ptr.i64.ptr.i64"),
            )
        if intrinsic == "call_i64_ptr_i64_i64_ptr":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            fnty = ir.FunctionType(_I64, [_CSTR, _I64, _I64, _CSTR])
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.ptr.i64.i64.ptr.fn"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_i64_arg(expr.args[3]),
                    self._unsafe_ptr_arg(expr.args[4]),
                ],
                name=self._fresh("unsafe.call.i64.ptr.i64.i64.ptr"),
            )
        if intrinsic == "call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool":
            self._unsafe_expect_arity(intrinsic, expr, 8)
            fnty = ir.FunctionType(
                _I64,
                [_CSTR, _I64, _CSTR, _CSTR, _CSTR, _CSTR, ir.IntType(1)],
            )
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.source.bridge.fn"),
            )
            wait = self.builder.trunc(
                self._unsafe_i64_arg(expr.args[7]),
                ir.IntType(1),
                name=self._fresh("unsafe.call.i64.source.bridge.wait"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_i64_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_ptr_arg(expr.args[4]),
                    self._unsafe_ptr_arg(expr.args[5]),
                    self._unsafe_ptr_arg(expr.args[6]),
                    wait,
                ],
                name=self._fresh("unsafe.call.i64.source.bridge"),
            )
        if intrinsic == "call_i64_ptr_ptr_ptr_ptr_ptr_bool":
            self._unsafe_expect_arity(intrinsic, expr, 7)
            fnty = ir.FunctionType(
                _I64,
                [_CSTR, _CSTR, _CSTR, _CSTR, _CSTR, ir.IntType(1)],
            )
            callee = self.builder.bitcast(
                self._unsafe_ptr_arg(expr.args[0]),
                fnty.as_pointer(),
                name=self._fresh("unsafe.call.i64.library.bridge.fn"),
            )
            wait = self.builder.trunc(
                self._unsafe_i64_arg(expr.args[6]),
                ir.IntType(1),
                name=self._fresh("unsafe.call.i64.library.bridge.wait"),
            )
            return self.builder.call(
                callee,
                [
                    self._unsafe_ptr_arg(expr.args[1]),
                    self._unsafe_ptr_arg(expr.args[2]),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self._unsafe_ptr_arg(expr.args[4]),
                    self._unsafe_ptr_arg(expr.args[5]),
                    wait,
                ],
                name=self._fresh("unsafe.call.i64.library.bridge"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )

    def _emit_unsafe_loader_waitset_gc(
        self,
        intrinsic: str,
        expr: Call,
    ) -> ir.Value:
        if intrinsic == "dynamic_library_open":
            if not self._unsafe_dynamic_library_target_matches(
                intrinsic, expr, 1
            ):
                return ir.Constant(_CSTR, None)
            platform_text = self._target_sys_platform_text()
            if platform_text not in {"darwin", "linux"}:
                raise NotImplementedError(
                    "pcc.unsafe.dynamic_library_open supports Darwin and Linux"
                )
            open_fn = self._declare_external_function(
                "dlopen", _CSTR, [_CSTR, _I32]
            )
            # RTLD_NOW is 2 on both supported targets; RTLD_LOCAL is 4 on
            # Darwin and zero on glibc/musl Linux.
            flags = 2 | (4 if platform_text == "darwin" else 0)
            return self.builder.call(
                open_fn,
                [self._unsafe_ptr_arg(expr.args[0]), ir.Constant(_I32, flags)],
                name=self._fresh("unsafe.dynamic.library.open"),
            )
        if intrinsic == "dynamic_library_open_global":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            platform_text = self._target_sys_platform_text()
            if platform_text not in {"darwin", "linux"}:
                raise NotImplementedError(
                    "pcc.unsafe.dynamic_library_open_global supports Darwin and Linux"
                )
            open_fn = self._declare_external_function(
                "dlopen", _CSTR, [_CSTR, _I32]
            )
            # RTLD_NOW is 2. RTLD_GLOBAL is 8 on Darwin and 0x100 on Linux.
            flags = 2 | (8 if platform_text == "darwin" else 0x100)
            return self.builder.call(
                open_fn,
                [self._unsafe_ptr_arg(expr.args[0]), ir.Constant(_I32, flags)],
                name=self._fresh("unsafe.dynamic.library.open.global"),
            )
        if intrinsic == "dynamic_library_symbol":
            if not self._unsafe_dynamic_library_target_matches(
                intrinsic, expr, 2
            ):
                return ir.Constant(_CSTR, None)
            symbol_fn = self._declare_external_function(
                "dlsym", _CSTR, [_CSTR, _CSTR]
            )
            return self.builder.call(
                symbol_fn,
                [
                    self._unsafe_ptr_arg(expr.args[0]),
                    self._unsafe_ptr_arg(expr.args[1]),
                ],
                name=self._fresh("unsafe.dynamic.library.symbol"),
            )
        if intrinsic == "darwin_libsystem_symbol":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            if self._target_sys_platform_text() != "darwin":
                return ir.Constant(_CSTR, None)

            cache_name = "__pcc_unsafe_darwin_libsystem_handle"
            existing_cache = self.module.globals.get(cache_name)
            if isinstance(existing_cache, ir.GlobalVariable):
                cache_gv = existing_cache
            elif existing_cache is None:
                # Store pointer bits as i64: the supported 64-bit self
                # backends already implement i64 atomics, while pointer-typed
                # atomics would add an otherwise identical backend-only form.
                cache_gv = ir.GlobalVariable(self.module, _I64, name=cache_name)
                cache_gv.linkage = "internal"
                cache_gv.initializer = ir.Constant(_I64, 0)
            else:
                raise NotImplementedError(
                    "Darwin libSystem handle cache collides with a non-global"
                )

            cached_bits = self.builder.load_atomic(
                cache_gv,
                "acquire",
                8,
                name=self._fresh("unsafe.libsystem.handle.cached_bits"),
            )
            cached = self.builder.inttoptr(
                cached_bits,
                _CSTR,
                name=self._fresh("unsafe.libsystem.handle.cached"),
            )
            has_cached = self.builder.icmp_unsigned(
                "!=",
                cached_bits,
                ir.Constant(_I64, 0),
                name=self._fresh("unsafe.libsystem.handle.has_cached"),
            )
            check_bb = self.builder.block
            open_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.handle.open"),
            )
            ready_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.handle.ready"),
            )
            self.builder.cbranch(has_cached, ready_bb, open_bb)

            self.builder.position_at_end(open_bb)
            open_fn = self._declare_external_function(
                "dlopen", _CSTR, [_CSTR, _I32]
            )
            path_gv, _path_len = self._cstr_literal(
                "/usr/lib/libSystem.B.dylib"
            )
            opened = self.builder.call(
                open_fn,
                [self._ptr_to_cstr(path_gv), ir.Constant(_I32, 2 | 4)],
                name=self._fresh("unsafe.libsystem.handle.opened"),
            )
            opened_bits = self.builder.ptrtoint(
                opened,
                _I64,
                name=self._fresh("unsafe.libsystem.handle.opened_bits"),
            )
            published = self.builder.cmpxchg(
                cache_gv,
                ir.Constant(_I64, 0),
                opened_bits,
                "acq_rel",
                "acquire",
                name=self._fresh("unsafe.libsystem.handle.publish"),
            )
            previous_bits = self.builder.extract_value(
                published,
                [0],
                name=self._fresh("unsafe.libsystem.handle.previous_bits"),
            )
            previous = self.builder.inttoptr(
                previous_bits,
                _CSTR,
                name=self._fresh("unsafe.libsystem.handle.previous"),
            )
            lost_publish = self.builder.icmp_unsigned(
                "!=",
                previous_bits,
                ir.Constant(_I64, 0),
                name=self._fresh("unsafe.libsystem.handle.lost_publish"),
            )
            opened_valid = self.builder.icmp_unsigned(
                "!=",
                opened,
                ir.Constant(_CSTR, None),
                name=self._fresh("unsafe.libsystem.handle.opened_valid"),
            )
            close_duplicate = self.builder.and_(
                lost_publish,
                opened_valid,
                name=self._fresh("unsafe.libsystem.handle.close_duplicate"),
            )
            close_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.handle.close"),
            )
            publish_done_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.handle.publish_done"),
            )
            self.builder.cbranch(close_duplicate, close_bb, publish_done_bb)

            self.builder.position_at_end(close_bb)
            close_fn = self._declare_external_function(
                "dlclose", _I32, [_CSTR]
            )
            self.builder.call(
                close_fn,
                [opened],
                name=self._fresh("unsafe.libsystem.handle.close_duplicate"),
            )
            self.builder.branch(publish_done_bb)

            self.builder.position_at_end(publish_done_bb)
            selected = self.builder.select(
                lost_publish,
                previous,
                opened,
                name=self._fresh("unsafe.libsystem.handle.selected"),
            )
            open_exit = self.builder.block
            self.builder.branch(ready_bb)

            self.builder.position_at_end(ready_bb)
            handle = self.builder.phi(
                _CSTR,
                name=self._fresh("unsafe.libsystem.handle"),
            )
            handle.add_incoming(cached, check_bb)
            handle.add_incoming(selected, open_exit)
            has_handle = self.builder.icmp_unsigned(
                "!=",
                handle,
                ir.Constant(_CSTR, None),
                name=self._fresh("unsafe.libsystem.handle.valid"),
            )
            lookup_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.symbol.lookup"),
            )
            fail_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.symbol.fail"),
            )
            done_bb = self.current_function.append_basic_block(
                name=self._fresh("unsafe.libsystem.symbol.done"),
            )
            self.builder.cbranch(has_handle, lookup_bb, fail_bb)

            self.builder.position_at_end(lookup_bb)
            symbol_fn = self._declare_external_function(
                "dlsym", _CSTR, [_CSTR, _CSTR]
            )
            resolved = self.builder.call(
                symbol_fn,
                [handle, self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.libsystem.symbol.resolved"),
            )
            lookup_exit = self.builder.block
            self.builder.branch(done_bb)

            self.builder.position_at_end(fail_bb)
            fail_exit = self.builder.block
            self.builder.branch(done_bb)

            self.builder.position_at_end(done_bb)
            result = self.builder.phi(
                _CSTR,
                name=self._fresh("unsafe.libsystem.symbol"),
            )
            result.add_incoming(resolved, lookup_exit)
            result.add_incoming(ir.Constant(_CSTR, None), fail_exit)
            return result
        if intrinsic == "dynamic_library_close":
            if not self._unsafe_dynamic_library_target_matches(
                intrinsic, expr, 1
            ):
                return ir.Constant(_I64, 0)
            close_fn = self._declare_external_function("dlclose", _I32, [_CSTR])
            result = self.builder.call(
                close_fn,
                [self._unsafe_ptr_arg(expr.args[0])],
                name=self._fresh("unsafe.dynamic.library.close.i32"),
            )
            return self.builder.sext(
                result,
                _I64,
                name=self._fresh("unsafe.dynamic.library.close"),
            )
        if intrinsic == "kqueue_create":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            if self._target_sys_platform_text() != "darwin":
                return ir.Constant(_I64, -38)
            create_fn = self._declare_external_function("kqueue", _I32, [])
            raw = self.builder.call(
                create_fn, [], name=self._fresh("unsafe.kqueue.create.i32")
            )
            return self._unsafe_darwin_errno_result(raw, "kqueue_create")
        if intrinsic == "kevent_call":
            self._unsafe_expect_arity(intrinsic, expr, 6)
            if self._target_sys_platform_text() != "darwin":
                return ir.Constant(_I64, -38)
            event_fn = self._declare_external_function(
                "kevent", _I32, [_I32, _CSTR, _I32, _CSTR, _I32, _CSTR]
            )
            raw = self.builder.call(
                event_fn,
                [
                    self.builder.trunc(self._unsafe_i64_arg(expr.args[0]), _I32),
                    self._unsafe_ptr_arg(expr.args[1]),
                    self.builder.trunc(self._unsafe_i64_arg(expr.args[2]), _I32),
                    self._unsafe_ptr_arg(expr.args[3]),
                    self.builder.trunc(self._unsafe_i64_arg(expr.args[4]), _I32),
                    self._unsafe_ptr_arg(expr.args[5]),
                ],
                name=self._fresh("unsafe.kevent.call.i32"),
            )
            return self._unsafe_darwin_errno_result(raw, "kevent_call")
        if intrinsic == "epoll_create1":
            self._unsafe_expect_arity(intrinsic, expr, 1)
            if not (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                return ir.Constant(_I64, -38)
            zero = ir.Constant(_I64, 0)
            return self.builder.syscall6(
                ir.Constant(_I64, 291),
                self._unsafe_i64_arg(expr.args[0]),
                zero,
                zero,
                zero,
                zero,
                zero,
                name=self._fresh("unsafe.epoll_create1.syscall"),
            )
        if intrinsic == "epoll_ctl":
            self._unsafe_expect_arity(intrinsic, expr, 5)
            if not (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                return ir.Constant(_I64, -38)
            event_storage = self.builder.alloca(
                ir.ArrayType(_I8, 12),
                name=self._fresh("unsafe.epoll_ctl.event"),
            )
            zero = ir.Constant(_I64, 0)
            event = self.builder.gep(
                event_storage,
                [zero, zero],
                inbounds=True,
                name=self._fresh("unsafe.epoll_ctl.event.ptr"),
            )
            events_ptr = self._unsafe_typed_addr(event, zero, _I32)
            token_low_ptr = self._unsafe_typed_addr(
                event, ir.Constant(_I64, 4), _I32
            )
            token_high_ptr = self._unsafe_typed_addr(
                event, ir.Constant(_I64, 8), _I32
            )
            self.builder.store(
                self.builder.trunc(self._unsafe_i64_arg(expr.args[3]), _I32),
                events_ptr,
            )
            token = self._unsafe_i64_arg(expr.args[4])
            self.builder.store(self.builder.trunc(token, _I32), token_low_ptr)
            self.builder.store(
                self.builder.trunc(
                    self.builder.lshr(token, ir.Constant(_I64, 32)), _I32
                ),
                token_high_ptr,
            )
            return self.builder.syscall6(
                ir.Constant(_I64, 233),
                self._unsafe_i64_arg(expr.args[0]),
                self._unsafe_i64_arg(expr.args[1]),
                self._unsafe_i64_arg(expr.args[2]),
                self.builder.ptrtoint(event, _I64),
                zero,
                zero,
                name=self._fresh("unsafe.epoll_ctl.syscall"),
            )
        if intrinsic == "epoll_wait":
            self._unsafe_expect_arity(intrinsic, expr, 4)
            if not (
                self._target_sys_platform_text() == "linux"
                and self._target_machine_text() == "x86_64"
            ):
                return ir.Constant(_I64, -38)
            events = self._unsafe_ptr_arg(expr.args[1])
            zero = ir.Constant(_I64, 0)
            return self.builder.syscall6(
                ir.Constant(_I64, 232),
                self._unsafe_i64_arg(expr.args[0]),
                self.builder.ptrtoint(events, _I64),
                self._unsafe_i64_arg(expr.args[2]),
                self._unsafe_i64_arg(expr.args[3]),
                zero,
                zero,
                name=self._fresh("unsafe.epoll_wait.syscall"),
            )
        if intrinsic == "thread_safepoint":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            safepoint_fn = self._declare_external_function(
                "pcc_thread_safepoint", _VOID, []
            )
            self.builder.call(safepoint_fn, [])
            return self._unsafe_void_result()
        if intrinsic == "gc_backend_current":
            self._unsafe_expect_arity(intrinsic, expr, 0)
            backend_fn = self._declare_external_function(
                "pcc_gc_backend", _I64, []
            )
            return self.builder.call(
                backend_fn,
                [],
                name=self._fresh("unsafe.gc.backend.current"),
            )
        raise NotImplementedError(
            "pcc.unsafe internal intrinsic family mismatch"
        )
