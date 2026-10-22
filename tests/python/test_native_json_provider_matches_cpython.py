"""The compiled json provider must produce CPython's bytes.

``pcc/stdlib/json.py`` accepted ``indent`` and dropped it, emitted the
compact separators CPython uses only on request, supported no ``separators``
argument at all, and defined neither ``load`` nor ``dump`` -- while ``pcc/``
calls ``json.dumps`` 93 times, ``json.loads`` 50, ``json.load`` 13 and
``json.dump`` 10.  Nothing caught it because ``json`` was classified as
compiler-owned builtin dispatch without being registered as a module whose
compiled provider is required, so codegen never bound these names to it.

That matters beyond formatting: the runtime archive provenance manifests,
build receipts and benchmark reports are JSON written for later byte
comparison.  A manifest written by pcc1 and one written by host pcc have to
agree.

The decoder side is pinned too.  The native ``py_json_loads`` helper returned
NULL without setting an exception on a malformed document, so compiled
``json.loads('{"k":')`` answered ``<null>`` where CPython raises.  It now
raises; the class is ``ValueError`` rather than ``json.JSONDecodeError``
because the native helper cannot reach the provider's class, and trailing
data after a complete value stays tolerated by the helper's recorded
contract.  Both are asserted here so a change to either is deliberate.
"""

from __future__ import annotations

import importlib.util
import io
import json as host_json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = REPO_ROOT / "pcc" / "stdlib" / "json.py"

_OBJECTS = [
    {"b": 1, "a": [1, 2, {"c": True}], "d": "x"},
    {}, [], [[]], [{}], {"k": {}}, {"k": []},
    {"n": None, "t": True, "f": False, "i": -7, "fl": 1.5, "s": 'a"b\\c\nd'},
    [1, [2, [3, [4]]]],
    {"outer": {"mid": {"inner": [1, 2]}}},
    # ``ensure_ascii`` defaults to True and the provider used to emit every
    # non-ASCII code point raw, i.e. it always behaved as False.
    {"unicode": "héllo — 日本語 🚀", "key—é": "v"},
    # json spells the non-finite floats NaN/Infinity/-Infinity; ``repr``
    # spells them nan/inf/-inf, which is what the provider emitted.
    [float("inf"), float("-inf"), float("nan"), 1e308, -0.0, 3.14159],
]

_KWARGS = [
    {},
    {"sort_keys": True},
    {"indent": 2},
    {"indent": 2, "sort_keys": True},
    {"indent": 0},
    {"indent": "\t"},
    {"separators": (",", ":")},
    {"separators": (",", ":"), "sort_keys": True},
    {"indent": 2, "separators": (",", ":")},
    {"indent": 4, "sort_keys": True, "separators": (",", ": ")},
    {"ensure_ascii": False},
    {"ensure_ascii": False, "indent": 2},
    {"ensure_ascii": True},
    {"allow_nan": True},
]


def _provider():
    spec = importlib.util.spec_from_file_location("pcc_json_provider", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dumps_matches_cpython_across_the_matrix():
    provider = _provider()
    mismatches = []
    for obj in _OBJECTS:
        for kwargs in _KWARGS:
            expected = host_json.dumps(obj, **kwargs)
            actual = provider.dumps(obj, **kwargs)
            if actual != expected:
                mismatches.append((obj, kwargs, expected, actual))
    assert not mismatches, "\n".join(
        f"kwargs={kw} obj={o!r}\n  host    : {e!r}\n  provider: {a!r}"
        for o, kw, e, a in mismatches[:6]
    )


def test_dumps_round_trips_through_the_host_decoder():
    provider = _provider()
    for obj in _OBJECTS:
        if repr(obj).find("nan") >= 0:
            continue  # NaN never equals itself; covered by the byte match
        for kwargs in _KWARGS:
            assert host_json.loads(provider.dumps(obj, **kwargs)) == obj


def test_allow_nan_false_rejects_non_finite_floats():
    provider = _provider()
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            provider.dumps([value], allow_nan=False)
        with pytest.raises(ValueError):
            host_json.dumps([value], allow_nan=False)


def test_object_pairs_hook_sees_document_order_and_duplicates():
    provider = _provider()
    document = '{"b": 1, "a": 2, "b": 3}'
    assert provider.loads(document, object_pairs_hook=list) == host_json.loads(
        document, object_pairs_hook=list
    )
    assert provider.loads(document, object_pairs_hook=list) == [
        ("b", 1), ("a", 2), ("b", 3)
    ]
    assert provider.loads(document) == host_json.loads(document)


def test_file_helpers_exist_and_match():
    """``json.load``/``json.dump`` are used in the closure and were missing."""
    provider = _provider()
    obj = {"b": 1, "a": [1, 2, {"c": True}]}
    for kwargs in ({}, {"indent": 2}, {"sort_keys": True, "separators": (",", ":")}):
        host_stream, provider_stream = io.StringIO(), io.StringIO()
        host_json.dump(obj, host_stream, **kwargs)
        provider.dump(obj, provider_stream, **kwargs)
        assert provider_stream.getvalue() == host_stream.getvalue()
        assert provider.load(io.StringIO(host_stream.getvalue())) == obj


def test_decoder_raises_on_malformed_documents():
    provider = _provider()
    for text in ('{"k":', "[1,", "", "nul", '{"k" 1}'):
        with pytest.raises(ValueError):
            provider.loads(text)


def test_recorded_native_helper_divergences():
    """Pin the two accepted gaps so changing either is a deliberate act.

    The provider itself raises ``JSONDecodeError`` and rejects trailing data;
    the native ``py_json_loads`` helper that compiled code actually reaches
    raises plain ``ValueError`` and tolerates trailing data.  ``JSONDecodeError``
    subclassing ``ValueError`` is what keeps ``except ValueError`` working
    across both.
    """
    provider = _provider()
    assert issubclass(provider.JSONDecodeError, ValueError)
    with pytest.raises(provider.JSONDecodeError):
        provider.loads('{"k": 1} trailing')

    helper = (REPO_ROOT / "pcc" / "runtime" / "py" / "py_json_runtime.py").read_text()
    assert "py_raise_owned(" in helper, "the helper must leave a pending exception"
    assert "trailing non-whitespace" in helper, (
        "the trailing-data contract must stay recorded next to the code"
    )
