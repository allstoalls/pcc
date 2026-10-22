"""Issue 11.B.2: graceful codegen-failure fallback for recursive
stdlib import.

When the closure walker pulls in a module that pcc can parse but
not fully codegen (e.g. uses unsupported decorator syntax), that
module must be EXCLUDED from the native compile and its importers
must fall back to ``py_cpy_import`` for THAT module specifically —
not crash the whole compile.

Concrete scenario: ``import shlex`` → walker pulls shlex.py and its
transitive dep collections.py. collections.py uses
``@property``/``@staticmethod``-style decorators on methods that pcc
codegen doesn't support yet. Without graceful fallback, the entire
compile errors out.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def test_shlex_compile_succeeds_despite_collections_codegen_failure():
    """User code that imports shlex should compile cleanly even
    though shlex's transitive dep collections.py fails pcc codegen.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / "fb_shlex.py"
    src.write_text(textwrap.dedent(
        """
        import shlex

        def f(s: str) -> None:
            pass
        """
    ), encoding="utf-8")
    out = _BUILD / "fb_shlex.ll"
    # Must NOT raise NotImplementedError or similar codegen errors
    # from the transitively-pulled collections module.
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        recursive_stdlib=True,
    )
    text = out.read_text(encoding="utf-8")
    assert len(text) > 0


def test_simple_module_still_native():
    """Regression check: keyword (which compiles cleanly) still gets
    the native treatment after the fallback path was added."""
    from pcc.frontends.python.pipeline import compile_python
    import re

    src = _BUILD / "fb_keyword.py"
    src.write_text(textwrap.dedent(
        """
        import keyword

        def f(s: str) -> None:
            pass
        """
    ), encoding="utf-8")
    out = _BUILD / "fb_keyword.ll"
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        recursive_stdlib=True,
    )
    text = out.read_text(encoding="utf-8")
    # keyword is fully compilable, should NOT pull libpython for it
    pattern = re.compile(
        r"%\.\w+\s*=\s*getelementptr[^\n]+@\.cpy\.mod\.keyword"
        r"\b[^\n]*\n[^\n]*=\s*call[^\n]+@py_cpy_import",
        re.MULTILINE,
    )
    assert not pattern.search(text), "keyword should still be native"
