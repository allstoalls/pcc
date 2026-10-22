"""``self.runtime["NAME"]`` must remain a real pcc runtime lookup.

An earlier ON-mode scaffold optimization replaced this pattern with a
direct native reference to ``@py_cpy_*`` runtime symbols. That reduced
fallback counts, but it also made self-hosted compiler binaries link
against target-program runtime symbols. The safe boundary is stricter:
``self.runtime`` is a pcc-native dict, so string-keyed lookup should
compile as a normal pcc object/dict access and must not materialize a
native ``@py_cpy_*`` function address.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


# Source mimics the layer1 idiom: a class with a self.runtime dict and
# a method that looks up an entry by string-literal key. The
# ``call`` invocation is what gives layer1 its long tail of
# py_cpy_getattr / py_cpy_getitem before the actual extern call.
_RUNTIME_LOOKUP_PROGRAM = textwrap.dedent(
    """
    class Codegen:
        def __init__(self):
            self.runtime = {}

        def use_lookup(self):
            return self.runtime["py_cpy_call_noargs"]
    """
)


def test_on_mode_does_not_emit_direct_cpython_runtime_symbol():
    ir_text = _compile_to_ll(
        _RUNTIME_LOOKUP_PROGRAM, "rt_lookup_on", mode="on",
    )
    body = _function_body(ir_text, "use_lookup")
    assert body is not None
    assert "@py_cpy_call_noargs" not in body, (
        "ON mode must not turn self.runtime[...] into a direct "
        "native py_cpy function reference:\n" + body
    )


def test_on_mode_use_lookup_body_uses_pcc_dict_access():
    ir_on = _compile_to_ll(_RUNTIME_LOOKUP_PROGRAM, "rt_on", mode="on")
    body_on = _function_body(ir_on, "use_lookup")
    assert body_on is not None
    assert re.search(r"@py_(?:dict|obj)_getitem\b", body_on), (
        "ON body should keep self.runtime[...] as a pcc-native "
        "raising dict/object subscript lookup; got:\n" + body_on
    )
    assert "@py_cpy_call_noargs" not in body_on, (
        "ON body must not directly reference @py_cpy_call_noargs:\n"
        + body_on
    )


def test_on_mode_does_not_return_direct_function_pointer():
    ir_on = _compile_to_ll(_RUNTIME_LOOKUP_PROGRAM, "rt_direct", mode="on")
    body = _function_body(ir_on, "use_lookup")
    assert body is not None
    assert not re.search(
        r"ret\s+(?:ptr|i8\s*\*)\s+@py_cpy_call_noargs\b", body,
    ), (
        "ON body must not return a direct @py_cpy_call_noargs address; "
        "got:\n" + body
    )


def test_on_mode_unknown_runtime_key_falls_through():
    """If the dict key isn't a known runtime symbol, scaffold returns
    None and the existing dynamic path runs."""
    program = textwrap.dedent(
        """
        class Bag:
            def __init__(self):
                self.runtime = {}

            def lookup(self):
                return self.runtime["not_a_runtime_function"]
        """
    )
    ir_text = _compile_to_ll(program, "rt_unknown", mode="on")
    body = _function_body(ir_text, "lookup")
    assert body is not None
    # Unknown key → scaffold returned None → dynamic dispatch via
    # the typed-dict ``py_obj_getitem`` (or py_cpy_getitem if the
    # receiver had been DynType). Either way, NO direct extern
    # reference to "@not_a_runtime_function".
    assert "@not_a_runtime_function" not in body, (
        "Unknown key must NOT yield a direct function reference "
        "(would be malformed since no such extern exists):\n" + body
    )


def test_on_mode_non_string_index_falls_through():
    """Non-literal index (variable, int) doesn't match the pattern."""
    program = textwrap.dedent(
        """
        class Bag:
            def __init__(self):
                self.runtime = {}

            def lookup(self, k):
                return self.runtime[k]
        """
    )
    ir_text = _compile_to_ll(program, "rt_var_idx", mode="on")
    body = _function_body(ir_text, "lookup")
    assert body is not None
    assert re.search(r"\bcall\b", body), (
        "Variable-index lookup must keep dynamic dispatch:\n" + body
    )


def test_on_mode_only_self_runtime_matches():
    """Pattern is ``self.runtime["X"]``, not ``other.runtime["X"]``."""
    program = textwrap.dedent(
        """
        class Bag:
            def __init__(self, other):
                self.other = other

            def lookup(self):
                return self.other.runtime["py_cpy_call_noargs"]
        """
    )
    ir_text = _compile_to_ll(program, "rt_other", mode="on")
    body = _function_body(ir_text, "lookup")
    assert body is not None
    # Should fall through; other.runtime["..."] doesn't match the
    # ``self.runtime[...]`` pattern.
    assert not re.search(
        r"ret\s+(?:ptr|i8\s*\*)\s+@py_cpy_call_noargs", body,
    ), "non-self.runtime pattern must not yield a direct ref:\n" + body
