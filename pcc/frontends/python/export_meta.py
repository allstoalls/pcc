from __future__ import annotations

from pcc.frontends.python.py_ast import (
    RawPointerType,
    BoolType,
    ByteArrayType,
    BytesType,
    ClassType,
    ComplexType,
    DictType,
    DynType,
    FloatType,
    FuncType,
    IntType,
    ListType,
    MemoryViewType,
    NoneType,
    SetType,
    StrType,
    TupleType,
    Type,
    ValueClassType,
)


def _is_value_class_type(ty) -> bool:
    return isinstance(ty, ValueClassType) or (
        isinstance(ty, ClassType) and bool(getattr(ty, "valueclass", False))
    )


def encode_type(ty: Type | None):
    return _encode_type(ty, None)


def encode_type_memo(ty: Type | None, memo: dict):
    """``encode_type`` sharing encodings of repeated subtypes via ``memo``.

    A class encoding embeds its bases' and class-typed fields' encodings, so
    one preload build re-encoded the same subtypes about 4 million times.
    ``memo`` maps ``id(ty)`` to ``(ty, encoding)``; the identity check keeps a
    reused address from matching.  Callers keep every memoized type alive for
    the memo's lifetime.
    """
    return _encode_type(ty, memo)


def _encode_type(ty, memo):
    if ty is None:
        return None
    if memo is not None:
        cached = memo.get(id(ty))
        if cached is not None and cached[0] is ty:
            return cached[1]
        encoded = _encode_type_uncached(ty, memo)
        memo[id(ty)] = (ty, encoded)
        return encoded
    return _encode_type_uncached(ty, memo)


def _encode_type_uncached(ty, memo):
    if isinstance(ty, RawPointerType):
        return ("raw_pointer",)
    if isinstance(ty, IntType):
        return ("int", ty.width, ty.signed)
    if isinstance(ty, FloatType):
        return ("float", ty.width)
    if isinstance(ty, ComplexType):
        return ("complex",)
    if isinstance(ty, BoolType):
        return ("bool",)
    if isinstance(ty, NoneType):
        return ("none",)
    if isinstance(ty, StrType):
        return ("str",)
    if isinstance(ty, BytesType):
        return ("bytes",)
    if isinstance(ty, ByteArrayType):
        return ("bytearray",)
    if isinstance(ty, MemoryViewType):
        return ("memoryview",)
    if isinstance(ty, ListType):
        return ("list", _encode_type(ty.elem, memo))
    if isinstance(ty, SetType):
        return (ty.name, _encode_type(ty.elem, memo))
    if isinstance(ty, DictType):
        return ("dict", _encode_type(ty.key, memo), _encode_type(ty.value, memo))
    if isinstance(ty, TupleType):
        return ("tuple", tuple(_encode_type(t, memo) for t in ty.elems))
    if isinstance(ty, FuncType):
        return (
            "func",
            tuple(_encode_type(t, memo) for t in ty.params),
            _encode_type(ty.ret, memo),
        )
    if _is_value_class_type(ty):
        return (
            "valueclass",
            ty.name,
            ty.module,
            tuple((name, _encode_type(field_ty, memo)) for name, field_ty in ty.fields),
            tuple(_encode_type(base, memo) for base in ty.bases),
            tuple((name, _encode_type(prop_ty, memo)) for name, prop_ty in ty.properties),
            bool(getattr(ty, "flattened", True)),
            bool(getattr(ty, "nullable_fields", False)),
        )
    if isinstance(ty, ClassType):
        return (
            "class",
            ty.name,
            ty.module,
            tuple((name, _encode_type(field_ty, memo)) for name, field_ty in ty.fields),
            tuple(_encode_type(base, memo) for base in ty.bases),
        )
    if isinstance(ty, DynType):
        return ("dyn",)
    # Export metadata is an ABI hint for cross-module declarations.
    # Complex annotations that the lightweight exporter cannot encode
    # should degrade to dyn instead of blocking bootstrap.
    return ("dyn",)


_DECODE_TYPE_MISSING = object()
_DECODE_TYPE_CACHE = {}


def decode_type(desc):
    if desc is None or isinstance(desc, Type):
        return desc
    if not isinstance(desc, tuple) or len(desc) == 0:
        raise TypeError(f"invalid export type descriptor: {desc!r}")
    try:
        cached = _DECODE_TYPE_CACHE.get(desc, _DECODE_TYPE_MISSING)
    except TypeError:
        return _decode_type_uncached(desc)
    if cached is not _DECODE_TYPE_MISSING:
        return cached
    decoded = _decode_type_uncached(desc)
    try:
        _DECODE_TYPE_CACHE[desc] = decoded
    except TypeError:
        pass
    return decoded


def _decode_type_uncached(desc):
    tag = desc[0]
    if tag == "raw_pointer":
        return RawPointerType(name="pcc.extern.c_rawptr")
    if tag == "int":
        width = desc[1] if len(desc) > 1 else 64
        signed = desc[2] if len(desc) > 2 else True
        return IntType(name="int", width=width, signed=signed)
    if tag == "float":
        width = desc[1] if len(desc) > 1 else 64
        return FloatType(name="float", width=width)
    if tag == "complex":
        return ComplexType("complex")
    if tag == "bool":
        return BoolType("bool")
    if tag == "none":
        return NoneType("None")
    if tag == "str":
        return StrType("str")
    if tag == "bytes":
        return BytesType("bytes")
    if tag == "bytearray":
        return ByteArrayType("bytearray")
    if tag == "memoryview":
        return MemoryViewType("memoryview")
    if tag == "list":
        return ListType(name="list", elem=decode_type(desc[1]))
    if tag in ("set", "frozenset"):
        return SetType(name=tag, elem=decode_type(desc[1]))
    if tag == "dict":
        return DictType(
            name="dict",
            key=decode_type(desc[1]),
            value=decode_type(desc[2]),
        )
    if tag == "tuple":
        return TupleType(
            name="tuple",
            elems=tuple(decode_type(t) for t in desc[1]),
        )
    if tag == "func":
        return FuncType(
            name="callable",
            params=tuple(decode_type(t) for t in desc[1]),
            ret=decode_type(desc[2]),
        )
    if tag == "class":
        return ClassType(
            name=desc[1],
            module=desc[2],
            fields=tuple((name, decode_type(field_ty)) for name, field_ty in desc[3]),
            bases=tuple(decode_type(base) for base in desc[4]),
        )
    if tag == "valueclass":
        props = ()
        flattened = True
        nullable_fields = False
        if len(desc) > 5:
            props = tuple((name, decode_type(prop_ty)) for name, prop_ty in desc[5])
        if len(desc) > 6:
            flattened = bool(desc[6])
        if len(desc) > 7:
            nullable_fields = bool(desc[7])
        return ClassType(
            name=desc[1],
            module=desc[2],
            fields=tuple((name, decode_type(field_ty)) for name, field_ty in desc[3]),
            bases=tuple(decode_type(base) for base in desc[4]),
            properties=props,
            valueclass=True,
        )
    if tag == "dyn":
        return DynType("dyn")
    raise TypeError(f"unknown export type descriptor tag: {tag!r}")
