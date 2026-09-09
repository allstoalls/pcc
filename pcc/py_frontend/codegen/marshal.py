"""Marshalling helpers between native scalars and PyObject*.

Phase 2 of the Python frontend splits any expression into an L1 "native"
and an L2 "boxed PyObject*" form. Whenever a native-typed value has to
interoperate with a container element, a runtime-lib call, or a mixed-
arity print, we *marshal* it into ``PyObject*`` via :func:`marshal_to_object`.
The reverse direction — unpacking a ``PyObject*`` back into a native
scalar — lives in :func:`marshal_from_object`.

These helpers operate in terms of:

* an ``ir.IRBuilder`` at a valid insertion point,
* a ``runtime`` dict mapping runtime-lib names to ``ir.Function`` (same
  shape as :func:`pcc.py_frontend.codegen.runtime_abi.declare_runtime`
  returns),
* the source ``pcc_py`` :class:`Type`, and
* the target type description (either "PyObject*" for to_object, or a
  destination :class:`Type` for from_object).

They expect runtime declarations to have been set up by
:func:`declare_runtime` beforehand. The one module mutation they make is the
float-literal pool below: a constant needs an object to point at, and the
alternative is allocating one on every evaluation.

Mirrors the ABI documented in Section 3 of
``docs/plans/python-frontend-interfaces.md``.
"""

from __future__ import annotations

import struct
from typing import Mapping

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolType,
    ByteArrayType,
    BytesType,
    ClassType,
    ComplexType,
    DictType,
    FloatType,
    IntType,
    ListType,
    MemoryViewType,
    NoneType,
    SetType,
    StrType,
    TupleType,
    Type,
)
from .runtime_abi import declare_runtime_global

# -- Canonical IR types ------------------------------------------------------

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_PTR = _I8.as_pointer()  # PyObject*


def _type_name(ty: Type) -> str:
    name = getattr(ty, "name", "")
    if name is None:
        return ""
    return name


def _type_name_in(ty: Type, names: tuple[str, ...]) -> bool:
    return _type_name(ty) in names


def _ir_type_or_none(value):
    try:
        return value.type
    except AttributeError:
        return None


def _ir_type_text(ty) -> str:
    if ty is None:
        return ""
    try:
        return str(ty)
    except Exception:
        return ""


def _builder_function_name(builder) -> str:
    try:
        return str(builder._block.function.name)
    except Exception:
        return "<unknown>"


def _ir_int_width(ty) -> int:
    try:
        if isinstance(ty, ir.IntType):
            return int(ty.width)
    except Exception:
        pass
    try:
        return int(ty.width)
    except Exception:
        pass
    text = _ir_type_text(ty)
    if len(text) > 1 and text[0] == "i":
        digits = text[1:]
        if digits.isdigit():
            return int(digits)
    return -1


def _ir_is_pointer_type(ty) -> bool:
    try:
        if isinstance(ty, ir.PointerType):
            return True
    except Exception:
        pass
    text = _ir_type_text(ty)
    return text == "ptr" or text.endswith("*") or text.endswith(" ptr")


def _ir_is_double_type(ty) -> bool:
    try:
        if isinstance(ty, ir.DoubleType):
            return True
    except Exception:
        pass
    return _ir_type_text(ty) == "double"


def _tmp(builder: ir.IRBuilder, hint: str) -> str:
    """Make a unique SSA name by salting with the builder's block id."""
    return f"{hint}.{id(builder._block):x}"


def is_object_type(ty: Type) -> bool:
    """True if ``ty`` must be represented as ``PyObject*`` at runtime."""
    if _type_name_in(
        ty,
        (
            "str",
            "bytes",
            "bytearray",
            "memoryview",
            "list",
            "dict",
            "tuple",
            "set",
            "frozenset",
            "None",
            "complex",
        ),
    ):
        return True
    return isinstance(
        ty,
        (
            StrType,
            BytesType,
            ByteArrayType,
            MemoryViewType,
            ListType,
            DictType,
            TupleType,
            SetType,
            NoneType,
            ComplexType,
        ),
    )


def is_native_type(ty: Type) -> bool:
    """True if ``ty`` has a native LLVM-IR scalar representation."""
    if _type_name_in(ty, ("int", "float", "bool")):
        return True
    return isinstance(ty, (IntType, FloatType, BoolType))


# PyFloatObject is {i64 refcount, i32 type_tag, i32 flags, double value};
# PY_TYPE_FLOAT is 3 and PY_FLAG_IMMORTAL is 1.
_PY_TYPE_FLOAT = 3
_PY_FLAG_IMMORTAL = 1


def float_literal_pool(module: ir.Module) -> dict:
    """The module's `bits -> GlobalVariable` pool of static float literals.

    Exposed so the static-literal initializer can register these objects with
    the same one-shot pass it already runs over the string literal pool.
    """
    pool = getattr(module, "_pcc_float_literal_pool", None)
    if pool is None:
        pool = {}
        module._pcc_float_literal_pool = pool
    return pool


def _double_constant_value(value: ir.Value, value_ty) -> "float | None":
    """The literal double behind *value*, or None when it is not a constant."""
    if not isinstance(value, ir.Constant) or not isinstance(value_ty, ir.DoubleType):
        return None
    raw = getattr(value, "value", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _float_literal_object(module: ir.Module, value: float) -> ir.Value:
    """Return a statically initialized immortal float object for *value*.

    Boxing a float literal through `py_float_from_f64` allocates on every
    evaluation, and `marshal_to_object` cannot tell its callers whether it
    returned a fresh box or passed a pointer through, so nothing released
    them: `y = x - 0.5` leaked 24 bytes per iteration and
    `(x - 0.5) * 1000.0` leaked 48, with the box emitted inside the loop body.

    A literal has a constant value, so it does not need a fresh object. This
    mirrors `_emit_str_literal`: one immortal object per distinct value in the
    data segment, pooled, registered once with the collector. Refcount
    operations on an immortal are no-ops, so the ownership ambiguity that
    caused the leak becomes harmless rather than merely fixed at one caller,
    and every evaluation now costs a pointer instead of an allocation.
    """
    bits = struct.unpack("<q", struct.pack("<d", value))[0]
    pool = float_literal_pool(module)
    existing = pool.get(bits)
    if existing is not None:
        return existing
    obj_ty = ir.LiteralStructType([_I64, _I32, _I32, ir.DoubleType()])
    gv = ir.GlobalVariable(
        module, obj_ty, name=".pyfloat.obj." + str(len(pool))
    )
    gv.linkage = "internal"
    gv.global_constant = False
    gv.initializer = ir.Constant(
        obj_ty,
        [
            ir.Constant(_I64, 1),
            ir.Constant(_I32, _PY_TYPE_FLOAT),
            ir.Constant(_I32, _PY_FLAG_IMMORTAL),
            ir.Constant(ir.DoubleType(), value),
        ],
    )
    pool[bits] = gv
    return gv


def marshal_to_object(
    builder: ir.IRBuilder,
    module: ir.Module,
    runtime: Mapping[str, ir.Function],
    value: ir.Value,
    ty: Type,
) -> ir.Value:
    """Return a ``PyObject*`` representation of ``value`` (typed ``ty``).

    * ``int``   → ``py_int_from_i64``
    * ``float`` → ``py_float_from_f64``
    * ``bool``  → ``py_bool_from_bit`` (with a zext to i32)
    * ``None``  → the global ``py_None`` (borrowed, INCREF'd by runtime
      on use; we return the const pointer here)
    * ``str`` / ``list`` / ``dict`` / ``tuple`` → already a ``PyObject*``,
      pass through.

    Any other type raises :class:`NotImplementedError`; the L3 layer
    picks those up.
    """
    ty_name = _type_name(ty)
    value_ty = _ir_type_or_none(value)
    if isinstance(ty, IntType) or ty_name == "int":
        # bool backed as i1 also comes through here when common_type
        # coerces to int; guard width.
        v64 = value
        value_width = _ir_int_width(value_ty)
        if value_width != 64:
            if value_width > 0:
                if value_width == 64:
                    v64 = value
                else:
                    v64 = builder.sext(value, _I64, name="m.b2i64")
            elif _ir_is_pointer_type(value_ty):
                # int-annotated but held as PyObject* — the value came
                # from a CPython / dyn path that boxed it. Already a
                # PyObject*, pass through.
                return value
            elif value_ty is None:
                # Self-host cross-module IR values can lose their Python-side
                # ``.type`` metadata while still carrying an i64 SSA value.
                v64 = value
            elif _ir_type_text(value_ty).isdigit():
                # pcc2 can mis-read ``ir.Constant.type`` as the constant's
                # literal value when subclass field metadata drifts. The
                # source type is still an int here, so keep the native value.
                v64 = value
            else:
                raise NotImplementedError(
                    "marshal_to_object: unexpected IR type "
                    + _ir_type_text(value_ty)
                    + " for int"
                )
        return builder.call(runtime["py_int_from_i64"], [v64], name="m.int_box")
    if isinstance(ty, FloatType) or ty_name == "float":
        # If inference said float but the caller already has a
        # PyObject* (e.g. a CPython ``float(...)`` fallback returned
        # a ptr), pass through — the downstream DynType path handles
        # the object form.
        if _ir_is_pointer_type(value_ty):
            return value
        literal = _double_constant_value(value, value_ty)
        if literal is not None:
            return _float_literal_object(module, literal)
        return builder.call(runtime["py_float_from_f64"], [value], name="m.flt_box")
    if isinstance(ty, BoolType) or ty_name == "bool":
        # py_bool_from_bit takes i32.
        value_width = _ir_int_width(value_ty)
        if value_width == 1:
            bit = builder.zext(value, _I32, name="m.b2i32")
        elif _ir_is_pointer_type(value_ty):
            # Bool-typed value already boxed via a CPython/dynamic path.
            return value
        elif value_width > 0:
            if value_width == 32:
                bit = value
            elif value_width > 32:
                bit = builder.trunc(value, _I32, name="m.b2i32")
            else:
                bit = builder.zext(value, _I32, name="m.b2i32")
        else:
            raise NotImplementedError(
                "marshal_to_object: unexpected IR type "
                + _ir_type_text(value_ty)
                + " for bool"
            )
        return builder.call(runtime["py_bool_from_bit"], (bit,), name="m.bool_box")
    if isinstance(ty, NoneType) or ty_name == "None":
        # If caller already has a pointer (e.g. loaded py_None earlier),
        # return it; otherwise materialise a load of the global.
        if _ir_is_pointer_type(value_ty):
            return value
        gv = declare_runtime_global(module, "py_None")
        return builder.load(gv, name="m.none")
    if _type_name_in(
        ty,
        (
            "str",
            "bytes",
            "bytearray",
            "memoryview",
            "list",
            "dict",
            "tuple",
            "set",
            "frozenset",
            "complex",
        ),
    ) or isinstance(
        ty,
        (
            StrType,
            BytesType,
            ByteArrayType,
            MemoryViewType,
            ListType,
            DictType,
            TupleType,
            SetType,
            ComplexType,
        ),
    ):
        # Already a PyObject* in the common case. But an ``or`` /
        # ``and`` phi whose arms are str-typed can land as i1 in IR
        # (the truth value, not the actual string ptr). When that
        # happens the caller was wrong to keep the StrType tag — we
        # still need to hand the runtime a pointer. Box the bit as
        # a bool PyObject* so the runtime's str helpers see a
        # well-typed ptr. (The resulting object isn't a real str, but
        # py_str_* will raise cleanly or fall back.)
        if _ir_is_pointer_type(value_ty):
            return value
        value_width = _ir_int_width(value_ty)
        if value_width == 1:
            bit = builder.zext(value, _I32, name="m.str.b2i32")
            return builder.call(
                runtime["py_bool_from_bit"], (bit,), name="m.str.bool_box"
            )
        if value_width > 0:
            widened = (
                value
                if value_width == 64
                else builder.sext(value, _I64, name="m.str.sext64")
            )
            return builder.call(
                runtime["py_int_from_i64"], (widened,), name="m.str.int_box"
            )
        return value
    # DynType values are usually ``PyObject*`` at the IR level (class
    # instances, attribute loads, results of ``MyClass(args)``). But if
    # an earlier coercion already unboxed them to a native scalar (e.g.
    # an ``int + 1`` on a DynType attribute that went through
    # ``py_int_to_i64``), marshal back to ``PyObject*`` based on the
    # LLVM type we actually hold.
    from ..py_ast import DynType, FuncType  # local import to avoid cycle

    if isinstance(ty, FuncType):
        # FuncType values at runtime are PyObject* (either a CPython
        # callable wrapping a hoisted pcc FuncDef, or an
        # operator.attrgetter/itemgetter from a simple lambda lowering).
        # Identity marshal — already a pointer.
        return value
    if isinstance(ty, ClassType):
        # Class objects flow around as PyObject* values at runtime.
        # This shows up in pcc's own sources as tuples of type objects
        # such as ``(IntType, FloatType, BoolType, DynType)`` used for
        # ``isinstance`` family checks during self-host compilation.
        if _ir_is_pointer_type(value_ty) or _ir_type_text(value_ty) == "":
            return value
        raise NotImplementedError(
            "marshal_to_object: unexpected IR type "
            + _ir_type_text(value_ty)
            + " for class object"
        )
    if isinstance(ty, DynType) or ty_name == "dyn":
        # Self-host resilience: some stage2/stage3 paths can surface a
        # missing IR value for DynType expressions that semantically
        # degrade to ``None``. Keep codegen progressing by materialising
        # ``py_None`` instead of aborting the whole compile.
        if value is None or value_ty is None:
            gv = declare_runtime_global(module, "py_None")
            return builder.load(gv, name="m.dyn.none")
        if _ir_is_pointer_type(value_ty):
            return value
        value_width = _ir_int_width(value_ty)
        if value_width == 64:
            return builder.call(
                runtime["py_int_from_i64"], [value], name="m.dyn.int_box"
            )
        if value_width > 0:
            if value_width == 1:
                bit = builder.zext(value, _I32, name="m.dyn.b2i32")
                return builder.call(
                    runtime["py_bool_from_bit"], (bit,), name="m.dyn.bool_box"
                )
            widened = (
                value
                if value_width == 64
                else builder.sext(value, _I64, name="m.dyn.sext64")
            )
            return builder.call(
                runtime["py_int_from_i64"], (widened,), name="m.dyn.int_box"
            )
        if _ir_is_double_type(value_ty):
            return builder.call(
                runtime["py_float_from_f64"], [value], name="m.dyn.flt_box"
            )
        if _ir_type_text(value_ty) == "void":
            # A ``-> None``/unannotated callee lowers to ``ret void``. When
            # inference typed the call expression as dyn, the Python-level
            # value is None — materialize the singleton instead of trying to
            # marshal a nonexistent SSA value (cross-module return-ABI fix;
            # docs/investigations/libpy-runtime-pcc-archive-pure-c-chain-crashes.md).
            gv = declare_runtime_global(module, "py_None")
            return builder.load(gv, name="m.dyn.none")
        raise NotImplementedError(
            "marshal_to_object: DynType with IR "
            + _ir_type_text(value_ty)
            + " not supported in "
            + _builder_function_name(builder)
        )
    if isinstance(ty, Type) or ty_name == "Type":
        # ``Type`` is the base class for pcc's type-description objects.
        # In the self-hosted compiler those values flow at runtime as
        # regular PyObject* instances, for example when iterating AST or
        # frontend metadata containers whose element type is imprecise.
        if _ir_is_pointer_type(value_ty):
            return value
        raise NotImplementedError(
            "marshal_to_object: unexpected IR type "
            + _ir_type_text(value_ty)
            + " for Type object"
        )
    if _ir_is_pointer_type(value_ty):
        # Last-resort identity path for boxed values whose precise frontend
        # type object did not match this module's class objects. This happens
        # during bootstrap when type-description objects cross compiled
        # module boundaries. Scalars still fail below.
        return value
    raise NotImplementedError("marshal_to_object: unsupported type descriptor")


def marshal_from_object(
    builder: ir.IRBuilder,
    module: ir.Module,
    runtime: Mapping[str, ir.Function],
    pyobj: ir.Value,
    target_ty: Type,
) -> ir.Value:
    """Unpack a ``PyObject*`` into the native representation of ``target_ty``.

    * ``int``   → ``py_int_to_i64`` (overflow flag discarded at L1/L2 for
      now; Phase 3 propagates it to an exception).
    * ``float`` → ``py_float_to_f64``
    * ``bool``  → ``py_obj_truthy`` truncated to i1.
    * ``str`` / ``list`` / ``dict`` / ``tuple`` / ``None`` → pass through
      (they already live as ``PyObject*`` natively).
    """
    target_name = _type_name(target_ty)
    if isinstance(target_ty, IntType) or target_name == "int":
        if isinstance(pyobj.type, ir.IntType):
            if pyobj.type.width == 64:
                return pyobj
            if pyobj.type.width < 64:
                return builder.sext(pyobj, _I64, name="m.int_widen")
            return builder.trunc(pyobj, _I64, name="m.int_narrow")
        folded = _boxed_i64_constant(pyobj)
        if folded is not None:
            # `py_int_from_i64(C)` immediately unboxed back to i64.  Every
            # `load_i64(obj, SOME_OFFSET)` in the runtime ports hits this: the
            # module-level offset is a Python int constant, so it is boxed to
            # satisfy the object-typed name and unboxed again to satisfy the
            # i64 parameter — two runtime calls to pass a literal.  Those two
            # calls per constant use were the bulk of the int box/unbox
            # traffic in a self-hosted pcc1 compile.
            return folded
        # The overflow slot is written but not branched on.  Record the source
        # object so an immediate re-box can hand it back instead of a truncated
        # i64: above 2**63-1 `py_int_to_i64` yields 0.
        ov_slot = _stash_overflow_slot(builder)
        fn = builder._block.function
        if fn is None:
            return builder.call(
                runtime["py_int_to_i64"], [pyobj, ov_slot], name="m.int_unbox"
            )
        # A tagged small int is its own payload: shift the bits inline and
        # call the runtime only for a boxed (bignum) object.  Every indexed
        # load ``xs[i & 63]`` paid this call on a value the caller had just
        # produced as a tagged immediate.
        one = ir.Constant(_I64, 1)
        bits = builder.ptrtoint(pyobj, _I64, name="m.int.bits")
        is_tagged = builder.icmp_signed(
            "==", builder.and_(bits, one, name="m.int.low"), one, name="m.int.tagged"
        )
        # Block names must be unique per function here, not merely per
        # module scope: the self-hosted compiler does not rename duplicate
        # block labels the way the host builder does, and two unboxes in one
        # function then fail the self IR verifier (phi-predecessors /
        # ssa-dominance).  Value names are deduplicated on both sides.
        tag = str(len(fn.blocks))
        fast_bb = fn.append_basic_block(name="m.int.unbox.fast." + tag)
        slow_bb = fn.append_basic_block(name="m.int.unbox.slow." + tag)
        join_bb = fn.append_basic_block(name="m.int.unbox.join." + tag)
        builder.cbranch(is_tagged, fast_bb, slow_bb)
        builder.position_at_end(fast_bb)
        fast = builder.ashr(bits, one, name="m.int.untag")
        builder.branch(join_bb)
        builder.position_at_end(slow_bb)
        slow = builder.call(
            runtime["py_int_to_i64"], [pyobj, ov_slot], name="m.int_unbox.slow"
        )
        builder.branch(join_bb)
        builder.position_at_end(join_bb)
        unboxed = builder.phi(_I64, name="m.int_unbox")
        unboxed.add_incoming(fast, fast_bb)
        unboxed.add_incoming(slow, slow_bb)
        return unboxed
    if isinstance(target_ty, FloatType) or target_name == "float":
        if isinstance(pyobj.type, ir.DoubleType):
            return pyobj
        if isinstance(pyobj.type, ir.FloatType):
            return builder.fpext(pyobj, _DOUBLE, name="m.flt_widen")
        return builder.call(runtime["py_float_to_f64"], [pyobj], name="m.flt_unbox")
    if isinstance(target_ty, BoolType) or target_name == "bool":
        if isinstance(pyobj.type, ir.IntType) and pyobj.type.width == 1:
            return pyobj
        if isinstance(pyobj.type, ir.IntType):
            zero = ir.Constant(pyobj.type, 0)
            return builder.icmp_unsigned("!=", pyobj, zero, name="m.bool_native")
        i32 = builder.call(runtime["py_obj_truthy"], [pyobj], name="m.bool_unbox_i32")
        return builder.trunc(i32, _I1, name="m.bool_unbox")
    if _type_name_in(
        target_ty,
        (
            "str",
            "bytes",
            "bytearray",
            "memoryview",
            "list",
            "dict",
            "tuple",
            "set",
            "frozenset",
            "None",
            "complex",
        ),
    ) or isinstance(
        target_ty,
        (
            StrType,
            BytesType,
            ByteArrayType,
            MemoryViewType,
            ListType,
            DictType,
            TupleType,
            SetType,
            ClassType,
            NoneType,
            ComplexType,
        ),
    ):
        return pyobj
    from ..py_ast import DynType as _DynType, FuncType as _FuncType

    if isinstance(target_ty, (_DynType, _FuncType)) or target_name in (
        "dyn",
        "FuncType",
    ):
        # FuncType values at runtime are opaque PyObject* callables
        # (CPython PyCFunction wrappers produced by
        # ``py_cpy_wrap_pcc_Narg`` or user operator.getters). Identity
        # pass-through. DynType handled the same way — the caller
        # already has a PyObject*.
        return pyobj
    if isinstance(target_ty, Type) or target_name == "Type":
        return pyobj
    if isinstance(pyobj.type, ir.PointerType):
        # Same duplicate-type-object fallback as marshal_to_object: an
        # already boxed value can be carried through unchanged.
        return pyobj
    raise NotImplementedError("marshal_from_object: unsupported target type descriptor")


# Boxed integer constants, by identity of the emitted value.  The IR builder
# returns opaque handles with no operand access, so a value cannot be asked
# what it boxes after the fact; the producer records it here instead.  The
# value itself is kept in the entry so its ``id`` cannot be recycled onto a
# different object while the mapping is live.
_BOXED_I64_CONSTANTS: dict[int, tuple] = {}


# NOTE on `py_int_from_i64(py_int_to_i64(x))`: it is NOT an identity -- above
# 2**63-1 `py_int_to_i64` yields 0 and the re-box gives 0.  Recovering the source
# object here was implemented and MEASURED UNSOUND: retaining it leaks one
# reference per call whenever the source is an OWNED temp the ownership lowering
# already releases (26 MB at 20k iterations, 98 MB at 200k, against a flat 1 MB),
# and not retaining frees it early.  Attribute reads mix both kinds -- the
# `py_instance_get_field` result is borrowed while `py_obj_getattr` returns a new
# reference -- so an opt-in flag per call site is not enough either.
#
# The shape that IS fixed is `int(<str>)`, via an object-returning variant
# (`emit_int_builtin_as_object`) that never unboxes at all.  Doing the same for
# the remaining sites needs the recovered value REGISTERED with the ownership
# lowering as that call's owned result.  Tracked as M5-SELFHOST-BIG-INT-LITERAL.


def note_boxed_i64_constant(value: ir.Value, constant: int) -> None:
    """Record that *value* is ``py_int_from_i64`` of the literal *constant*."""
    _BOXED_I64_CONSTANTS[id(value)] = (value, int(constant))


def reset_boxed_i64_constants() -> None:
    """Drop the mapping between modules."""
    _BOXED_I64_CONSTANTS.clear()


def _boxed_i64_constant(pyobj: ir.Value) -> ir.Value | None:
    """Return the literal *pyobj* boxes, when it was recorded as boxing one."""
    entry = _BOXED_I64_CONSTANTS.get(id(pyobj))
    if entry is None:
        return None
    held, constant = entry
    if held is not pyobj:
        return None
    return ir.Constant(_I64, constant)


def _stash_overflow_slot(builder: ir.IRBuilder) -> ir.Value:
    """Allocate an int32 stack slot to receive an overflow flag.

    The slot is left uninitialised — :func:`py_int_to_i64` writes it
    unconditionally. We hoist it to the entry block so repeated use in a
    loop body does not grow the stack.

    NOTE: llvmlite's IRBuilder position is an integer ``_anchor`` into
    the block's instruction list. Inserting via a separate builder at
    the top of the entry block *does not* shift the primary builder's
    anchor, so subsequent emissions on the primary builder land at the
    wrong index and orphan previously-emitted instructions behind the
    terminator. We therefore temporarily repoint the caller's own
    builder to the allocation slot, emit, and restore the anchor.
    """
    fn = builder._block.function
    entry = fn.blocks[0]
    # Insert at the very start of the entry block instead of scanning for
    # the first non-alloca instruction: the scan walked the entry's alloca
    # prefix on every call (O(prefix) opname reads), and since
    # core_helpers._alloca_in_entry now parks one alloca per rooted call
    # site in that prefix, int-heavy functions turned the scan quadratic
    # under the self-hosted compiler. Ordering among entry allocas is
    # irrelevant — each executes once.
    saved_block = builder._block
    # Read _pos directly rather than via getattr-with-default: the
    # pcc-py self-host runtime can return the default for instance
    # attrs that ARE set (the same pattern that broke
    # _skip_program_main / current_class). Falling back to
    # ``builder._anchor`` exercises a Python ``@property`` descriptor
    # which the self-host runtime also doesn't reliably execute, so
    # the alloca insertion point ends up restored as the primary
    # cursor and later emissions land before their operands.
    saved_pos = builder._pos

    builder.position_at_start(entry)
    slot = builder.alloca(_I32, name="ov.flag")

    if saved_block is not None:
        builder._block = saved_block
        # Prefer the concrete pcc.llvm_capi insertion cursor. The
        # self-hosted compiler does not reliably execute Python
        # ``property`` descriptors, so depending on IRBuilder._anchor
        # can restore the cursor to the alloca insertion point and
        # emit later instructions before their operands.
        end_marker = getattr(builder, "_END", None)
        if saved_block is entry and saved_pos != end_marker:
            # The front insert shifted every index in the entry block.
            builder._pos = saved_pos + 1
        else:
            builder._pos = saved_pos
    return slot


__all__ = [
    "is_object_type",
    "is_native_type",
    "marshal_to_object",
    "marshal_from_object",
]
