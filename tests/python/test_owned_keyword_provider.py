"""Compare the owned keyword provider with the selected CPython 3.15 host."""

import keyword as host_keyword
import re
import sys
from pathlib import Path

import pytest

from pcc.stdlib import keyword as owned_keyword


def _outcome(function, value):
    try:
        return ("value", function(value))
    except Exception as exc:
        return (type(exc), str(exc))


def test_keyword_tables_match_cpython_315():
    assert sys.version_info[:2] == (3, 15)
    assert owned_keyword.__all__ == host_keyword.__all__
    assert type(owned_keyword.kwlist) is list
    assert type(owned_keyword.softkwlist) is list
    assert owned_keyword.kwlist == host_keyword.kwlist
    assert owned_keyword.softkwlist == host_keyword.softkwlist


def test_native_discovery_selects_the_owned_keyword_source():
    from pcc.frontends.python.pipeline_dependency_closure import (
        _locate_native_stdlib_module_source,
    )

    source = _locate_native_stdlib_module_source("keyword")
    assert source is not None
    assert Path(source).resolve() == Path(owned_keyword.__file__).resolve()


@pytest.mark.parametrize("name", ["iskeyword", "issoftkeyword"])
def test_keyword_strings_match_cpython(name):
    owned = getattr(owned_keyword, name)
    host = getattr(host_keyword, name)
    values = host_keyword.kwlist + host_keyword.softkwlist + [
        "", "IF", "none", "identifier", " if", "if ", "é", "if\0",
    ]
    for value in values:
        assert owned(value) is host(value), repr(value)


@pytest.mark.parametrize("name", ["iskeyword", "issoftkeyword"])
@pytest.mark.parametrize("value", [
    None, False, True, 0, 3.25, b"if", ("if",), frozenset({"if"}),
    {"if"}, set(), [], {}, bytearray(b"if"),
])
def test_non_string_and_unhashable_membership_matches_cpython(name, value):
    assert _outcome(getattr(owned_keyword, name), value) == _outcome(
        getattr(host_keyword, name), value
    )


class _KeywordAlias:
    def __init__(self, word):
        self.word = word

    def __hash__(self):
        return hash(self.word)

    def __eq__(self, other):
        return self.word == other


class _UnhashableString(str):
    __hash__ = None


@pytest.mark.parametrize("name", ["iskeyword", "issoftkeyword"])
def test_membership_preserves_user_hash_and_equality(name):
    owned = getattr(owned_keyword, name)
    host = getattr(host_keyword, name)
    for word in ("if", "lazy", "identifier"):
        alias = _KeywordAlias(word)
        assert owned(alias) is host(alias)
        value = _UnhashableString(word)
        assert _outcome(owned, value) == _outcome(host, value)


@pytest.mark.parametrize("list_name, predicate, existing", [
    ("kwlist", "iskeyword", "if"),
    ("softkwlist", "issoftkeyword", "lazy"),
])
def test_public_list_mutation_does_not_change_membership(
    list_name, predicate, existing
):
    values = getattr(owned_keyword, list_name)
    original = values[:]
    try:
        values.remove(existing)
        values.append("new_keyword_for_test")
        check = getattr(owned_keyword, predicate)
        assert check(existing) is True
        assert check("new_keyword_for_test") is False
    finally:
        values[:] = original


@pytest.mark.parametrize("name", ["iskeyword", "issoftkeyword"])
def test_membership_predicates_require_one_positional_argument(name):
    for module in (owned_keyword, host_keyword):
        function = getattr(module, name)
        with pytest.raises(TypeError):
            function()
        with pytest.raises(TypeError):
            function("if", "else")
        with pytest.raises(TypeError):
            function(key="if")


def test_keyword_provider_emits_real_no_libpython_functions(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    output = tmp_path / "keyword.ll"
    compile_python(
        str(Path(owned_keyword.__file__)), str(output),
        emit_llvm_only=True, python_library=True,
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    text = output.read_text(encoding="utf-8")
    for name in ("iskeyword", "issoftkeyword"):
        body = re.search(
            r"^define[^\n]*@[^\n]*_" + name + r"\([^\n]*\{\n(.*?)^\}",
            text, re.MULTILINE | re.DOTALL,
        )
        assert body is not None, name
        assert "strict.nolib.stub" not in body.group(1)
        assert "@py_cpy_" not in body.group(1)


def test_imported_keyword_calls_use_the_owned_provider(tmp_path):
    from pcc.frontends.python.pipeline_context import (
        compile_contextual_per_module_fallback_counts,
    )

    source = tmp_path / "keyword_user.py"
    source.write_text(
        "import keyword\n"
        "def classify(value):\n"
        "    return keyword.iskeyword(value), keyword.issoftkeyword(value)\n",
        encoding="utf-8",
    )
    counts = compile_contextual_per_module_fallback_counts(
        [str(Path(owned_keyword.__file__)), str(source)],
        ["keyword", "keyword_user"], ["keyword_user"],
        ir_scaffold_mode="on", strict_no_libpython=True,
        emit_ir_dir=str(tmp_path), entry_module="keyword_user",
    )
    assert counts == {"keyword_user": 0}
    text = (tmp_path / "keyword_user.ll").read_text(encoding="utf-8")
    assert "@user_keyword_iskeyword" in text
    assert "@user_keyword_issoftkeyword" in text
    assert "strict.nolib.stub" not in text
