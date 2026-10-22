from pcc.frontends.python.export_meta import decode_type, encode_type
from pcc.frontends.python.py_ast import ClassType, DynType, IntType, ListType, SetType


def test_decode_type_caches_tuple_descriptors():
    desc = (
        "list",
        (
            "class",
            "Node",
            "pkg.model",
            (("value", ("int", 64, True)),),
            (),
        ),
    )

    first = decode_type(desc)
    second = decode_type(desc)

    assert first is second
    assert isinstance(first, ListType)
    assert isinstance(first.elem, ClassType)
    assert first.elem.name == "Node"
    assert first.elem.fields == (("value", IntType(name="int", width=64, signed=True)),)


def test_decode_type_preserves_existing_type_passthrough():
    ty = IntType(name="int", width=32, signed=False)

    assert decode_type(ty) is ty


def test_set_type_export_metadata_roundtrip():
    ty = SetType(name="set", elem=DynType(name="dyn"))

    recovered = decode_type(encode_type(ty))

    assert recovered == ty
    assert isinstance(recovered, SetType)
