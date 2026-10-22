"""General TOML and diagnostic parity with the selected CPython 3.15 host."""

from decimal import Decimal
from io import BytesIO, StringIO
import math
from pathlib import Path
import sys
import tomllib as host_tomllib
import warnings

import pytest

from pcc.stdlib import tomllib as owned_tomllib
from pcc.stdlib.tomllib import _parser as owned_parser


VALID_DOCUMENTS = [
    "",
    "# comment\n",
    'title = "α 😀"\nenabled = true\nempty = ""\n',
    "decimal = -1_234\nhex = 0xDEAD_BEEF\noctal = 0o755\nbinary = 0b1010\n",
    "huge = 12345678901234567890123456789012345678901234567890\n",
    "first = 1.25\nsecond = -2e+3\nthird = 1_0.0_5\n",
    '"key.with.dots" = 3\nplain."quoted part".last = "value"\n',
    "array = [1, 'two', true, [3, 4], {nested = 5},]\n",
    "[parent.child]\nname = 'first'\n[other]\nvalue = 2\n",
    "[[items]]\nname = 'first'\n[[items]]\nname = 'second'\n[items.detail]\nx = 3\n",
    'basic = "\\u03B1\\U0001F600"\nescape = "\\e\\x41"\n',
    "literal = 'C:\\users\\name'\nmultiline = '''\nfirst\nsecond'''\n",
    'multiline = """\nfirst\\\n    second"""\n',
    'x = """a\r\nb"""\r\nlist = [1, # comment\r\n2]\r\n',
    "date = 2024-02-29\ntime = 12:34:56.123456789\nshort_time = 12:34\n",
    "utc = 1979-05-27T07:32:00Z\noffset = 1979-05-27 07:32:00-07:00\n",
    "local = 1979-05-27t07:32:00.123456\n",
]


INVALID_DOCUMENTS = [
    "x = [",
    'x = "unterminated',
    "x = 1\nx = 2\n",
    "a.b = 1\na = 2\n",
    "[a]\n[a]\n",
    "x = {a = 1, a = 2}\n",
    "x = {a = 1}\nx.b = 2\n",
    "x = 01\n",
    "x = 0x_1\n",
    "x = True\n",
    'x = "\\q"\n',
    'x = "\\uD800"\n',
    'x = "\\U00110000"\n',
    "date = 2023-02-29\n",
    "x = 1 # bad\x00comment\n",
    "bad = [1\r\n",
]


def _error_record(module, document):
    with pytest.raises(module.TOMLDecodeError) as caught:
        module.loads(document)
    error = caught.value
    assert type(error) is module.TOMLDecodeError
    return (
        str(error), error.args, error.msg, error.doc,
        error.pos, error.lineno, error.colno,
    )


def test_owned_tomllib_exports_exception_identity():
    assert sys.version_info[:2] == (3, 15)
    assert owned_tomllib.__all__ == host_tomllib.__all__
    assert owned_tomllib.TOMLDecodeError is owned_parser.TOMLDecodeError
    assert issubclass(owned_tomllib.TOMLDecodeError, ValueError)
    assert owned_tomllib.TOMLDecodeError.__module__ == owned_tomllib.__name__


@pytest.mark.parametrize("document", VALID_DOCUMENTS)
def test_general_documents_match_cpython(document):
    assert owned_tomllib.loads(document) == host_tomllib.loads(document)


@pytest.mark.parametrize("document", INVALID_DOCUMENTS)
def test_decode_error_message_and_location_match_cpython(document):
    assert _error_record(owned_tomllib, document) == _error_record(host_tomllib, document)


@pytest.mark.parametrize("spelling", ["inf", "+inf", "-inf", "nan", "+nan", "-nan"])
def test_special_float_values_match_cpython(spelling):
    document = "number = " + spelling
    actual = owned_tomllib.loads(document)["number"]
    expected = host_tomllib.loads(document)["number"]
    if math.isnan(expected):
        assert math.isnan(actual)
    else:
        assert actual == expected
    assert math.copysign(1, actual) == math.copysign(1, expected)


def test_binary_file_load_and_custom_float_parser_match_cpython():
    document = b"array = [1.25, 1e2]\ninline = { value = 2.5 }\n"
    actual = owned_tomllib.load(BytesIO(document), parse_float=Decimal)
    expected = host_tomllib.load(BytesIO(document), parse_float=Decimal)
    assert actual == expected
    assert type(actual["array"][0]) is Decimal


def test_custom_float_parser_receives_original_number_spelling():
    document = "values = [1_0.0_5, -2e+3, +inf, -nan]"
    records = []
    for module in (owned_tomllib, host_tomllib):
        calls = []

        def parse_float(text):
            calls.append(text)
            return "number:" + text

        records.append((module.loads(document, parse_float=parse_float), calls))
    assert records[0] == records[1]


@pytest.mark.parametrize("result", [[], {}])
def test_custom_float_parser_cannot_return_tables_or_arrays(result):
    for module in (owned_tomllib, host_tomllib):
        with pytest.raises(ValueError, match="parse_float must not return dicts or lists") as caught:
            module.loads("x = 1.0", parse_float=lambda text: result)
        assert type(caught.value) is ValueError


def test_custom_float_parser_exception_is_preserved():
    class CallbackError(Exception):
        pass

    def parse_float(text):
        raise CallbackError(text)

    for module in (owned_tomllib, host_tomllib):
        with pytest.raises(CallbackError, match="1.5"):
            module.loads("x = 1.5", parse_float=parse_float)


@pytest.mark.parametrize("value", [b"x = 1", None, 42])
def test_loads_rejects_non_string_input_like_cpython(value):
    records = []
    for module in (owned_tomllib, host_tomllib):
        with pytest.raises(TypeError) as caught:
            module.loads(value)
        records.append(str(caught.value))
    assert records[0] == records[1]


def test_load_requires_binary_stream_and_preserves_utf8_errors():
    records = []
    for module in (owned_tomllib, host_tomllib):
        with pytest.raises(TypeError) as caught:
            module.load(StringIO("x = 1"))
        records.append(str(caught.value))
        with pytest.raises(UnicodeDecodeError):
            module.load(BytesIO(b"\xff"))
    assert records[0] == records[1]


def test_decode_error_constructor_locations_and_legacy_arguments():
    records = []
    for module in (owned_tomllib, host_tomllib):
        error = module.TOMLDecodeError("broken", "first\nsecond", 8)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            legacy = module.TOMLDecodeError("legacy", 17)
        records.append((
            str(error), error.args, error.msg, error.doc, error.pos,
            error.lineno, error.colno, str(legacy), legacy.args,
            [(warning.category, str(warning.message)) for warning in caught],
        ))
    assert records[0] == records[1]


def test_native_discovery_finds_owned_tomllib_package_and_parser():
    from pcc.frontends.python.pipeline_dependency_closure import (
        _locate_native_stdlib_module_source,
    )

    for module, path in (
        ("tomllib", Path(owned_tomllib.__file__)),
        ("tomllib._parser", Path(owned_parser.__file__)),
    ):
        source = _locate_native_stdlib_module_source(module)
        assert source is not None
        assert Path(source).resolve() == path.resolve()
