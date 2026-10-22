"""Preload aliases share serialization without changing structural type IDs."""

from pcc.frontends.python import type_infer
from pcc.frontends.python.py_ast import ClassType


def test_real_preload_serializes_local_and_qualified_alias_once(monkeypatch):
    exports = {"provider": {"Record": {
        "kind": "class", "class_name": "Record", "base_names": (),
        "field_names": ("value",),
        "field_types": (("value", ("int", 64, True)),),
    }}}
    original = type_infer.encode_type_memo
    calls = []

    def observe(ty, memo):
        calls.append(ty)
        return original(ty, memo)

    monkeypatch.setattr(type_infer, "encode_type_memo", observe)
    result = type_infer.build_unique_external_class_preload(exports)
    # Each alias once, sharing one descriptor; their order follows insertion.
    assert sorted(result["keys"]) == [("Record", 0), ("provider.Record", 0)]
    assert len(result["types"]) == 1
    assert len(calls) == 1


def test_identity_reuse_preserves_distinct_equal_types_and_key_order(monkeypatch):
    first = ClassType(name="Record", module="provider", fields=(), bases=())
    equal = ClassType(name="Record", module="provider", fields=(), bases=())
    assert first is not equal
    original = type_infer.encode_type_memo
    calls = []

    def preload(ctx):
        ctx.class_types.update({"first": first, "alias": first, "equal": equal})

    def observe(ty, memo):
        calls.append(ty)
        return original(ty, memo)

    monkeypatch.setattr(type_infer, "_preload_unique_external_classes", preload)
    monkeypatch.setattr(type_infer, "encode_type_memo", observe)
    result = type_infer.build_unique_external_class_preload({})
    assert result == {
        "types": (type_infer.encode_type(first),),
        "keys": (("first", 0), ("alias", 0), ("equal", 0)),
        "dependencies": (),
    }
    assert len(calls) == 2
    assert calls[0] is first and calls[1] is equal


def test_identity_key_collision_cannot_reuse_another_types_descriptor(monkeypatch):
    first = ClassType(name="First", module="provider", fields=(), bases=())
    second = ClassType(name="Second", module="provider", fields=(), bases=())

    def preload(ctx):
        ctx.class_types.update({"first": first, "second": second, "alias": first})

    monkeypatch.setattr(type_infer, "_preload_unique_external_classes", preload)
    monkeypatch.setattr(type_infer, "id", lambda _ty: 1, raising=False)
    result = type_infer.build_unique_external_class_preload({})
    assert result["keys"] == (("first", 0), ("second", 1), ("alias", 0))
    assert result["types"] == (type_infer.encode_type(first), type_infer.encode_type(second))
