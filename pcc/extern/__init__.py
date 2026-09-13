"""pcc ``extern "C"`` FFI declarations (P6C.1).

The self-hosting roadmap (``docs/plans/python-frontend-plan.md``
§Phase 6C.1) requires that compiled Python be able to declare and
call arbitrary C library entry points directly, without going
through CPython or any other interpreter. This module hosts the
type markers and the ``extern()`` factory that pcc's codegen
recognizes.

Runtime status (2026-05-12): **codegen has landed**. The frontend
records ``extern()`` calls into ``L1CodeGen._extern_decls`` (see
``pcc/py_frontend/codegen/layer1.py::_emit_extern_call``); a call
through an extern lowers to a direct LLVM ``call`` to the named
external symbol with the declared C ABI. There is **no Python /
``py_obj_*`` trampoline** — the emitted asm is just ``bl <symbol>``
plus the int truncate/extend conversions performed by
``_coerce_to_extern``.

Known sharp edges still under P6C.1:

* ``c_str`` **argument**: pcc's ``str`` value is a ``PyStrObject*``,
  but the extern declaration expects a raw ``char*``. The current
  codegen passes the ``PyStrObject*`` through unchanged (see
  ``_coerce_to_extern`` line ~13483). For libc symbols that read
  bytes (``access``, ``getenv``, ...) this is wrong unless the
  callee happens to skip the header — most callers go through a
  helper that materialises a NUL-terminated buffer first.
* ``c_str`` **return**: extern functions returning ``char*`` (e.g.
  ``getenv``) hand back raw ``i8*``. There is no runtime helper
  yet that wraps the result into a ``PyStrObject``; the pcc-side
  caller currently has to raise ``NotImplementedError``. See
  ``pcc/py_stdlib/os.py::getenv`` for the canonical "blocked on
  string-return marshalling" stub.
* **Errno**: extern calls do not raise Python-level exceptions on
  failure; the C return contract is the only signal (``-1`` +
  ``errno`` for libc). Wrappers that want ``OSError`` semantics
  must inspect errno explicitly.

Example::

    from pcc.extern import extern, c_int, c_str, c_ptr

    printf: extern = extern(
        "printf",
        argtypes=(c_str,),
        restype=c_int,
        variadic=True,
    )

    def main() -> None:
        printf(c_str("hello\\n"))

    main()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class _CType:
    """Opaque marker for a C scalar type. Instances map one-to-one
    onto an LLVM IR type via the P6C.1 codegen rules."""

    __slots__ = ("name", "ir_name")

    def __init__(self, name: str, ir_name: str) -> None:
        self.name = name
        self.ir_name = ir_name

    def __repr__(self) -> str:
        return f"<CType {self.name}>"


c_void = _CType("void", "void")
c_bool = _CType("bool", "i1")
c_int8 = _CType("int8", "i8")
c_int16 = _CType("int16", "i16")
c_int32 = _CType("int32", "i32")
c_int64 = _CType("int64", "i64")
c_uint8 = _CType("uint8", "i8")
c_uint16 = _CType("uint16", "i16")
c_uint32 = _CType("uint32", "i32")
c_uint64 = _CType("uint64", "i64")

# Convenience aliases matching typical platform sizes.
c_int = c_int32
c_long = c_int64
c_size_t = c_uint64

c_float = _CType("float", "float")
c_double = _CType("double", "double")

# Pointers are all opaque ``ptr`` at the IR level; the semantic
# distinction (c_str vs generic c_ptr) is tracked for the frontend's
# type-inference layer.
c_ptr = _CType("ptr", "ptr")
c_str = _CType("cstr", "ptr")
# Return-position markers.  In normal (non-runtime-port) compilation a bare
# ``c_ptr``/``c_str`` return is rejected because the frontend cannot tell a
# PyObject* from raw memory: ``c_obj`` declares an owned Python object result and
# ``c_rawptr`` a raw address, which pcc types as ``int`` so it never enters the
# object refcount protocol.  Both lower to the same IR pointer type.
# A foreign function returning a borrowed object must retain it in its ABI
# adapter before exposing that result as c_obj.
c_obj = _CType("obj", "ptr")
c_rawptr = _CType("rawptr", "ptr")


@dataclass(frozen=True)
class ExternFn:
    """Declaration of an ``extern "C"`` function. The frontend treats
    instances as callables at the AST level; codegen lowers each call
    to a direct ``call @<symbol>(...)``."""

    symbol: str
    argtypes: tuple[_CType, ...]
    restype: _CType = c_void
    variadic: bool = False
    # Optional lib name for library-load hints. None means the symbol
    # is expected to resolve at static-link time.
    lib: str | None = None

    def __call__(self) -> Any:
        """Runtime trap. The frontend rewrites each call to a direct
        LLVM ``call`` before this method would fire — it only
        executes on CPython (i.e. during pcc's own interpreter-mode
        development) where we fall through to a NotImplementedError
        so misuse is loud.

        Kept arg-less on purpose: a ``*args`` signature would be a
        self-host audit blocker (pcc frontend doesn't lower variadic
        defs). The frontend lowers every extern call site before it
        ever reaches this method, so the runtime signature is moot —
        any caller that actually gets here is already going to
        ``raise``. The AttributeError / TypeError from argument-count
        mismatch lands at the same point as the NotImplementedError
        would have."""
        raise NotImplementedError(
            f"extern({self.symbol!r}) called from interpreted Python — "
            "this call site should have been lowered at compile time"
        )


def extern(
    symbol: str,
    argtypes: tuple[_CType, ...] = (),
    restype: _CType = c_void,
    variadic: bool = False,
    lib: str | None = None,
) -> ExternFn:
    """Factory for an extern-C function declaration.

    Instances are consumed by pcc's codegen via isinstance checks in
    the type-inference layer. Using :class:`ExternFn` as a call
    target triggers the direct-call lowering in P6C.1."""
    return ExternFn(
        symbol=symbol, argtypes=argtypes, restype=restype,
        variadic=variadic, lib=lib,
    )


def c_abi_export(symbol: str):
    """Decorator that forces pcc to emit the decorated function under
    the given unmangled C-ABI symbol name instead of the usual
    ``user_<module>_<name>`` mangling. Required when a pcc-Python
    function is meant to replace a py_runtime/*.c symbol at link time
    (Phase 4c runtime-high migration).

    Codegen recognizes ``@c_abi_export("name")`` by decorator name and
    uses the literal string argument as the LLVM symbol. The returned
    wrapper is a no-op at the Python level — it just round-trips the
    function through so the decorator syntax stays valid when the
    module is imported under CPython.
    """
    def _wrap(fn):
        fn.__pcc_c_abi_symbol__ = symbol
        return fn
    return _wrap


def c_abi_variadic_export(symbol: str):
    """Export a pcc-Python function as a variadic C-ABI symbol.

    The decorated function's declared parameters are the fixed C arguments;
    its unnamed arguments are consumed with ``pcc.unsafe.va_*`` intrinsics.
    Like :func:`c_abi_export`, this decorator is a CPython-level no-op whose
    meaning is consumed by pcc codegen.
    """
    def _wrap(fn):
        fn.__pcc_c_abi_symbol__ = symbol
        fn.__pcc_c_abi_variadic__ = True
        return fn
    return _wrap


def c_abi_typed_export(symbol: str, restype: str, argtypes: tuple[str, ...]):
    """Export a function with an exact C ABI signature.

    ``restype`` and ``argtypes`` use LLVM-sized names (``void``, ``ptr``,
    ``i8``/``i16``/``i32``/``i64``, ``f32`` and ``f64``), plus recursive
    structural aggregates such as ``{f64,f64}``.  This is intended for
    runtime ABI surfaces whose C representation differs from Python's native
    lanes; codegen consumes the literal signature from the decorator.
    """
    def _wrap(fn):
        fn.__pcc_c_abi_symbol__ = symbol
        fn.__pcc_c_abi_restype__ = restype
        fn.__pcc_c_abi_argtypes__ = argtypes
        return fn
    return _wrap


__all__ = [
    "ExternFn",
    "extern",
    "c_abi_export",
    "c_abi_variadic_export",
    "c_abi_typed_export",
    "c_void", "c_bool",
    "c_int8", "c_int16", "c_int32", "c_int64",
    "c_uint8", "c_uint16", "c_uint32", "c_uint64",
    "c_int", "c_long", "c_size_t",
    "c_float", "c_double",
    "c_ptr", "c_str", "c_obj", "c_rawptr",
]
