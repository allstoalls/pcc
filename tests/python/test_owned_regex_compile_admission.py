"""Owned regex compile admission and verbose lexical semantics."""

import ast
from pathlib import Path
import re

import pytest


def _normalizer():
    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_re_engine_runtime.py"
    tree = ast.parse(source.read_text())
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("_normalize_verbose_pattern", "_normalize_compile_pattern")]
    namespace = {}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_normalize_compile_pattern"]


@pytest.mark.parametrize("pattern, flags, texts", [
    ("a b # comment\n c", re.VERBOSE, ["abc", "a b c", "xabc"]),
    (r"[ #]+ \# ", re.VERBOSE, [" ##", "# #", "###"]),
    (r"a\ b", re.VERBOSE, ["a b", "ab"]),
    (r"[^^]+ # caret class", re.VERBOSE, ["abc", "^^"]),
    (r"[] #]+", re.VERBOSE, ["] #", "abc"]),
    (r"a{ 2 }", re.VERBOSE, ["a{2}", "aa", "a{ 2 }"]),
    (r"a{2,3} b", re.VERBOSE, ["aab", "aaab", "ab"]),
    (r"a{,} b", re.VERBOSE, ["b", "ab", "aaaab"]),
    (r"(?x)a b", 0, ["ab", "a b"]),
    (r"(?im)^a b$", re.VERBOSE, ["AB", "abc\nAB"]),
    (r"^a.b$", re.DOTALL, ["a\nb", "axb"]),
])
def test_owned_verbose_projection_matches_cpython(pattern, flags, texts):
    normalized, engine_flags = _normalizer()(pattern, int(flags))
    expected = re.compile(pattern, flags)
    projected = re.compile(normalized, engine_flags)
    for text in texts:
        assert projected.findall(text) == expected.findall(text)


@pytest.mark.parametrize("pattern", ["(? :a)", "a* ?", "a+ # comment\n ?"])
def test_verbose_projection_fails_explicitly_before_joining_invalid_tokens(pattern):
    with pytest.raises(re.error):
        re.compile(pattern, re.VERBOSE)
    with pytest.raises(NotImplementedError, match="separated"):
        _normalizer()(pattern, 64)


def test_toml_regexes_use_the_same_general_verbose_projection():
    from pcc.stdlib.tomllib import _re

    for pattern in (_re.RE_NUMBER.pattern, _re.RE_DATETIME.pattern, _re.RE_LOCALTIME.pattern):
        normalized, flags = _normalizer()(pattern, 64)
        expected = re.compile(pattern, re.VERBOSE)
        projected = re.compile(normalized, flags)
        for text in ("12:34:56.123456789", "1979-05-27T07:32:00Z", "-1_234.5e-2", "0xBAD", "no match"):
            assert projected.findall(text) == expected.findall(text)


def test_keyword_and_dynamic_regex_compile_emit_owned_calls(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "regex_compile.py"
    source.write_text(
        "import re\n"
        "def positional(): return re.compile('a b', re.VERBOSE)\n"
        "def keyword(): return re.compile('a b', flags=re.VERBOSE)\n"
        "def all_keywords(): return re.compile(flags=re.VERBOSE, pattern='a b')\n"
        "def dynamic(pattern: str, flags: int): return re.compile(pattern, flags=flags)\n"
        "def unsupported(): return re.compile('(?<=a)b')\n"
    )
    output = tmp_path / "regex_compile.ll"
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self")
    text = output.read_text()
    for name in ("positional", "keyword", "all_keywords", "dynamic", "unsupported"):
        body = re.search(r"^define[^\n]*@user_regex_compile_" + name + r"\(.*?^}", text, re.M | re.S)
        assert body is not None, name
        assert "@py_re_compile_obj" in body.group(), name
        assert "@py_cpy_" not in body.group(), name
        assert "strict.nolib.stub" not in body.group(), name
