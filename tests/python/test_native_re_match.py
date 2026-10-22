"""Native ``re.match`` / ``re.search`` lowering for the no-libpython subset."""

from __future__ import annotations

import re
import subprocess
import textwrap
import os
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
        libpython_mode="off",
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


def test_re_match_dispatches_to_native_helper():
    program = textwrap.dedent("""
        import re
        from re import match, search

        def f() -> None:
            print(re.match("a+", "aa") is not None)
            print(match("\\\\d+", "123") is not None)
            print(re.match("abc", "ABC", re.I) is not None)
            print(search("b+", "aaBBBcc", re.I) is not None)
        """)

    ir_text = _compile_to_ll(program, "native_re_match_ir", mode="on")
    body = _function_body(ir_text, "f")

    assert body is not None
    assert "@py_re_match" in body, body
    assert "@py_re_match_flags" in body, body
    assert "@py_re_search_flags" in body, body
    assert "py_cpy_" not in body, body


def test_re_compile_bound_match_search_stay_native():
    program = textwrap.dedent("""
        import re

        has_word = re.compile("b+", re.I).search
        at_start = re.compile("a+", re.I).match
        words = re.compile(r"\\b[a-z][\\w$]*\\b", re.I).findall

        def f() -> None:
            print(has_word("xxBBB") is not None)
            print(at_start("AAAxxx") is not None)
            print("beta_2" in words("a + beta_2 + 3"))
        """)

    ir_text = _compile_to_ll(program, "native_re_compile_bound_ir", mode="on")
    assert "@py_re_compile_method" in ir_text, ir_text
    assert "@py_re_findall_flags" in ir_text, ir_text
    assert re.search(r"\bcall\b[^\n]*@py_cpy_", ir_text) is None, ir_text


def test_re_compile_literal_alias_methods_stay_native():
    program = textwrap.dedent("""
        import re

        word = re.compile(r"b+", re.I)
        pieces = re.compile(r"\\b[a-z][\\w$]*\\b", re.I)

        def f() -> None:
            print(word.match("BBB") is not None)
            print(word.search("xxBBB") is not None)
            print("beta_2" in pieces.findall("a + beta_2 + 3"))
        """)

    ir_text = _compile_to_ll(program, "native_re_compile_alias_ir", mode="on")
    assert "@py_re_match_flags" in ir_text, ir_text
    assert "@py_re_search_flags" in ir_text, ir_text
    assert "@py_re_findall_flags" in ir_text, ir_text
    assert "@.cpy.attr.compile" not in ir_text, ir_text
    assert re.search(r"\bcall\b[^\n]*@py_cpy_", ir_text) is None, ir_text


def test_re_compile_alias_value_use_keeps_fallback_boundary():
    program = textwrap.dedent("""
        import re

        word = re.compile(r"b+", re.I)

        def f():
            return word
        """)

    ir_text = _compile_to_ll(program, "native_re_compile_alias_value_ir", mode="on")
    # re.compile now produces a native pattern object (py_re_compile_obj); the
    # value-escape (returning ``word``) carries that native object, which is
    # safe because unsupported uses (e.g. re.split(obj, ...)) fall back to
    # CPython at the call site, not at compile.
    assert "@py_re_compile_obj" in ir_text, ir_text
    assert "@.cpy.attr.compile" not in ir_text, ir_text


def test_re_compile_local_alias_methods_stay_native_and_scoped():
    program = textwrap.dedent("""
        import re

        def f(text: str) -> bool:
            prune_file_pat = re.compile(r"(?:[~#]|\\\\.py[co]|\\\\.o)$")
            return prune_file_pat.search(text) is not None

        def g():
            prune_file_pat = re.compile(r"b+")
            return prune_file_pat
        """)

    ir_text = _compile_to_ll(program, "native_re_compile_local_alias_ir", mode="on")
    body_f = _function_body(ir_text, "f")
    body_g = _function_body(ir_text, "g")
    assert body_f is not None
    assert body_g is not None
    # f's local ``re.compile(...).search`` stays native via the pattern object
    # (py_re_compile_obj + object .search dispatch); no CPython fallback call.
    # Runtime output verified equal to CPython.
    assert "@py_re_compile_obj" in body_f, body_f
    assert re.search(r"\bcall\b[^\n]*@py_cpy_", body_f) is None, body_f
    assert "cpy.fn.compile" not in body_f, body_f
    # g's ``re.compile(...)`` now produces a native pattern object too (the
    # returned value is a native re object; previously this escaped to a CPython
    # compile fallback).
    assert "@py_re_compile_obj" in body_g, body_g
    assert "cpy.fn.compile" not in body_g, body_g


def test_re_compile_class_attr_re_split_stores_pattern_string():
    program = textwrap.dedent("""
        import re

        class Parser:
            sep = re.compile(r"\\s|,|([+-])")

            def f(self, text: str):
                return re.split(self.sep, text)
        """)

    ir_text = _compile_to_ll(program, "native_re_compile_class_split_ir", mode="on")
    assert "@.cpy.attr.compile" not in ir_text, ir_text
    assert "@.cpy.attr.split" in ir_text, ir_text


def test_re_compile_class_attr_method_use_keeps_compile_fallback():
    program = textwrap.dedent("""
        import re

        class Parser:
            pat = re.compile(r"^x+")

            def f(self, text: str) -> bool:
                return self.pat.match(text) is not None
        """)

    ir_text = _compile_to_ll(program, "native_re_compile_class_method_ir", mode="on")
    # ``pat = re.compile(...)`` class attr + ``self.pat.match(...)`` now stays
    # native: re.compile yields a native pattern object and .match dispatches to
    # the native engine (runtime output verified equal to CPython).
    assert "@py_re_compile_obj" in ir_text, ir_text
    assert "@.cpy.attr.compile" not in ir_text, ir_text


def test_re_split_literal_separator_stays_native():
    program = textwrap.dedent("""
        import re

        def f() -> None:
            parts = re.split("/", "alpha/beta/gamma", maxsplit=1)
            print(parts[1])
        """)

    ir_text = _compile_to_ll(program, "native_re_split_literal_ir", mode="on")
    body = _function_body(ir_text, "f")
    assert body is not None
    assert "@py_str_split_maxsplit" in body, body
    assert "py_cpy_" not in body, body


def test_native_re_match_runtime_matches_basic_prefixes(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent("""
            import re

            def main() -> None:
                print(re.match("a+", "aa") is not None)
                print(re.match("\\\\d+", "123abc") is not None)
                print(re.match("z+", "aa") is None)
                print(re.match("abc", "ABC", re.I) is not None)
                print(re.search("b+", "aaBBBcc", re.I) is not None)
                print(re.search("^b+", "aaBBBcc", re.I) is None)
                has_word = re.compile("b+", re.I).search
                at_start = re.compile("a+", re.I).match
                words = re.compile(r"\\b[a-z][\\w$]*\\b", re.I).findall
                print(has_word("xxBBB") is not None)
                print(at_start("AAAxxx") is not None)
                print("beta_2" in words("a + beta_2 + 3"))
                print("(x)" in re.findall(r"\\(.*?\\)", "a(x)b(y)"))
                limited = re.split("/", "alpha/beta/gamma", maxsplit=1)
                print(limited[0])
                print(limited[1])
                unlimited = re.split("/", "alpha/beta/gamma", maxsplit=0)
                print(unlimited[2])
                whitespace_match = re.compile(r"[ \\t\\n\\r]*").match
                offset_search = re.compile("b").search
                print(whitespace_match("x", 1).end())
                print(offset_search("ab", 1).start())

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )
    with mock.patch.dict(
        os.environ,
        {"PCC_RUNTIME_CC": "cc", "PCC_RUNTIME_HIGH": "c"},
    ):
        compile_python(
            str(src),
            str(exe),
            ir_scaffold_mode="on",
            libpython_mode="off",
        )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == (
        "True\nTrue\nTrue\nTrue\nTrue\nTrue\nTrue\nTrue\nTrue\nTrue\n"
        "alpha\nbeta/gamma\ngamma\n1\n1\n"
    )
