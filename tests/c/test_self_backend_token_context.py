"""Simple operands must not allocate module type-layout caches."""

from unittest.mock import patch

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_parse as parser
from pcc.backend.self_backend_ir import TypeParseContext


@pytest.mark.parametrize("token, expected", [
    ("  null \n", "null"), ("poison", "poison"), ("undef", "undef"),
    ("false", "false"), ("true", "true"), ("zeroinitializer", "zeroinitializer"),
    ("%name", "name"), ('%"a b"', "a b"),
    ("@global", "@global"), ('@"a b"', "@a b"),
    ("0", "0"), ("123456789012345678901234567890", "123456789012345678901234567890"),
    ("-42", "-42"), ("1.25", "1.25"), ("-1.25", "-1.25"),
    ("0x3FF0000000000000", "0x3FF0000000000000"),
])
def test_simple_token_does_not_construct_context(token, expected):
    with patch.object(parser, "TypeParseContext", side_effect=AssertionError("unused context")):
        assert parser.decode_value_token(token) == expected


@pytest.mark.parametrize("token, expected", [
    ("(i32 -4)", "-4"),
    ("inttoptr (i64 1 to ptr)", "inttoptrconst:1"),
    ("nonnull inttoptr (i64 1 to ptr)", "inttoptrconst:1"),
    ("getelementptr ([4 x i8], ptr @data, i64 0, i64 2)", "gepconst:data:2"),
])
def test_complex_token_constructs_one_shared_context(token, expected):
    with patch.object(parser, "TypeParseContext", wraps=TypeParseContext) as constructor:
        assert parser.decode_value_token(token) == expected
        assert constructor.call_count == 1


def test_complex_token_preserves_supplied_named_layout_context():
    context = TypeParseContext(named_type_bodies={"%Pair": "{ i8, i64 }"})
    token = "getelementptr (%Pair, ptr @data, i64 0, i32 1)"
    with patch.object(parser, "TypeParseContext", side_effect=AssertionError("replaced context")):
        with patch.object(parser, "parse_constant_gep", wraps=parser.parse_constant_gep) as parse_gep:
            assert parser.decode_value_token(token, type_context=context) == "gepconst:data:8"
            assert parse_gep.call_args.kwargs["type_context"] is context
    assert context.named_type_bodies == {"%Pair": "{ i8, i64 }"}


def test_invalid_token_keeps_diagnostic():
    with pytest.raises(BackendUnavailable, match="unsupported value syntax for self backend"):
        parser.decode_value_token("???")
