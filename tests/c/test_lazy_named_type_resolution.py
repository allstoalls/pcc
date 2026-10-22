"""Unused named syntax does not request unsupported target value layouts."""

import subprocess

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend import self_backend_parse as parser


TARGETS = (
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "arm64-apple-darwin", "x86_64-pc-windows-msvc",
)
UNSUPPORTED = ("half", "bfloat", "fp128", "ppc_fp128", "x86_amx")
# The module parser's legalization stores half as its i16 encoding
# (self_backend_half); only the raw type parser still rejects it.
UNSUPPORTED_WHEN_USED = tuple(leaf for leaf in UNSUPPORTED if leaf != "half")
ATTRIBUTES = ("byval", "byref", "sret", "inalloca", "preallocated", "elementtype")


@pytest.fixture(autouse=True)
def forbid_external_compilers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external compiler process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _module(declarations="", body=None, target=TARGETS[0]):
    if body is None:
        body = "define i32 @probe() {\nentry:\n  ret i32 7\n}\n"
    return 'target triple = "' + target + '"\n' + declarations + "\n" + body


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("leaf", UNSUPPORTED + ("x86_fp80",))
def test_unused_unsupported_nested_type_preserves_owned_object(target, leaf):
    declarations = "%Unused = type { i8, [2 x { " + leaf + " }] }\n"
    text = _module(declarations, target=target)
    module = parser.parse_self_backend_module(text)
    assert "%Unused" not in module.type_context.named_types
    assert leaf not in module.type_context.type_cache
    assert emit_owned_object(text, target) == emit_owned_object(_module(target=target), target)


def _named_layout_use(leaf, site):
    declarations = "%T = type { " + leaf + " }\n"
    if site == "global":
        return declarations, _module(declarations + "@value = global %T zeroinitializer\n")
    if site == "signature":
        return declarations, _module(declarations, "define void @probe(%T %value) {\nentry:\n  ret void\n}\n")
    if site == "alloca":
        return declarations, _module(declarations, "define void @probe() {\nentry:\n  %value = alloca %T\n  ret void\n}\n")
    return declarations, _module(declarations, "define ptr @probe(ptr %address) {\nentry:\n  %value = getelementptr %T, ptr %address, i32 0, i32 0\n  ret ptr %value\n}\n")


@pytest.mark.parametrize("site", ["global", "signature", "alloca", "gep"])
def test_used_half_layout_is_stored_as_its_binary16_encoding(site):
    _declarations, text = _named_layout_use("half", site)
    module = parser.parse_self_backend_module(text)
    layout = parser.parse_ir_type("%T", type_context=module.type_context)
    assert [field.describe() for field in layout.fields] == ["i16"]
    assert (layout.slot_size, layout.align) == (2, 2)


@pytest.mark.parametrize("leaf", UNSUPPORTED_WHEN_USED)
@pytest.mark.parametrize("site", ["global", "signature", "alloca", "gep"])
def test_used_unsupported_named_layout_fails_and_does_not_leak_cache(leaf, site):
    declarations, text = _named_layout_use(leaf, site)
    with pytest.raises(BackendUnavailable):
        parser.parse_self_backend_module(text)
    context = parser._parse_named_types(declarations)
    with pytest.raises(BackendUnavailable):
        parser.parse_ir_type("%T", type_context=context)
    assert "%T" not in context.named_types


@pytest.mark.parametrize("attribute", ATTRIBUTES)
@pytest.mark.parametrize("site", ["declaration", "definition", "indirect-call"])
def test_typed_abi_attribute_requests_layout_before_it_is_discarded(attribute, site):
    declarations = "%T = type { x86_fp80 }\n"
    argument = "ptr " + attribute + "(%T)"
    if site == "declaration":
        text = _module(declarations + "declare void @external(" + argument + ")\n")
    elif site == "definition":
        text = _module(declarations, "define void @probe(" + argument + " %value) {\nentry:\n  ret void\n}\n")
    else:
        text = _module(declarations, "define void @probe(ptr %callee) {\nentry:\n  call void %callee(" + argument + " null)\n  ret void\n}\n")
    with pytest.raises(BackendUnavailable, match="x86_fp80"):
        parser.parse_self_backend_module(text)


@pytest.mark.parametrize("body", [
    "{ i32,, i8 }", "{ [2 x i8} }", "{ i32, }", "{ mystery }",
    "{ %Missing }", "{ i32 i8 }",
])
def test_malformed_unused_struct_is_still_rejected(body):
    with pytest.raises(BackendUnavailable):
        parser.parse_self_backend_module(_module("%T = type " + body + "\n"))


def test_forward_nested_and_recursive_pointer_types_resolve_on_use():
    module = parser.parse_self_backend_module(_module(
        "%Envelope = type { [2 x %Pair], %Node* }\n"
        "%Node = type { i32, %Node* }\n"
        "%Pair = type { i16, i16 }\n",
        "define %Envelope @identity(%Envelope %value) {\nentry:\n  ret %Envelope %value\n}\n",
    ))
    envelope = parser.parse_ir_type("%Envelope", type_context=module.type_context)
    assert envelope.fields[0].elem.name == "%Pair"
    assert envelope.fields[0].elem.fields[0].width == 16
    node = parser.parse_ir_type("%Node", type_context=module.type_context)
    assert node.fields[1].pointee.name == "%Node"
    assert parser.parse_ir_type("%Envelope", type_context=module.type_context) is envelope


def test_ssa_name_does_not_request_same_named_type():
    text = _module("%T = type { x86_fp80 }\n", "define i32 @probe(i32 %T) {\nentry:\n  ret i32 %T\n}\n")
    assert emit_owned_object(text, TARGETS[0])
    assert "%T" not in parser.parse_self_backend_module(text).type_context.named_types


def test_named_bodies_do_not_leak_between_modules():
    parser.parse_self_backend_module(_module("%T = type { i32 }\n"))
    with pytest.raises(BackendUnavailable, match="could not split global type") as error:
        parser.parse_self_backend_module(_module(body="@item = global %T zeroinitializer\n"))
    assert isinstance(error.value.__cause__, BackendUnavailable)
    assert "named LLVM type" in str(error.value.__cause__)


def test_supported_typed_abi_metadata_and_ordinary_metadata_stay_accepted():
    module = parser.parse_self_backend_module(_module(
        "%T = type { i64 }\n"
        "declare void @external(ptr byval(%T))\n"
        "declare void @llvm.dbg.value(metadata, metadata, metadata)\n",
        "define void @probe(ptr %value) {\nentry:\n"
        "  call void @external(ptr byval(%T) %value)\n  ret void\n}\n",
    ))
    assert module.functions[0].name == "probe"
    assert parser.parse_ir_type("%T", type_context=module.type_context).fields[0].width == 64


def test_typed_abi_resolution_uses_lazy_cursor_not_diagnostic_token_list(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("normal ABI parsing materialized diagnostic token list")
    text = _module(
        "%T = type { i64 }\n"
        "declare void @external(ptr byval(%T))\n",
        "define void @probe(ptr %value) {\nentry:\n"
        "  call void @external(ptr byval(%T) %value)\n  ret void\n}\n",
    )
    with monkeypatch.context() as guard:
        guard.setattr(parser, "_tokenize_ir_type", forbidden)
        context = parser._parse_named_types("%T = type { i64 }")
        parser._resolve_declaration_type_attributes("declare void @external(ptr byval(%T))", type_context=context)
        parser._resolve_typed_abi_attributes("byval(%T) %value", type_context=context)
    assert emit_owned_object(text, TARGETS[0])
