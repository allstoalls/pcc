"""Issue 11.B.1: ``compile_python_multi`` should recursively pull
imported pure-Python modules into the native compile set.

Goal: writing ``from dataclasses import dataclass`` in user code
should result in ``dataclasses.py`` being compiled natively (when
it's pure Python and pcc can parse it), rather than triggering a
``py_cpy_import`` call to libpython.

Each test sets up a small synthetic source that imports something,
compiles, and verifies the IR routing.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(
    source: str,
    name: str,
    *,
    recursive: bool = True,
    libpython_mode: str | None = None,
) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        recursive_stdlib=recursive,  # opt-in flag
        libpython_mode=libpython_mode,
    )
    return out.read_text(encoding="utf-8")


def test_pure_python_stdlib_pulled_into_closure_when_recursive(tmp_path):
    """When ``recursive_stdlib=True`` (Issue 11.B.1), the closure
    walker pulls a pure-Python stdlib module's source into the
    compile set.

    Verifies the WALKER piece (this is the foundation). Codegen-side
    routing — making ``import keyword`` in user code skip
    ``py_cpy_import`` once shlex is in the closure — is a separate
    integration step (TODO: B.1 part 2)."""
    from pcc.frontends.python.pipeline import (
        _collect_multi_source_relative_closure,
    )

    src = tmp_path / "u.py"
    src.write_text("import keyword\ndef f(s: str): return s\n", encoding="utf-8")
    srcs, mods = _collect_multi_source_relative_closure(
        [str(src)],
        ["u"],
        recursive_stdlib=True,
    )
    assert "keyword" in mods, f"keyword should be in recursive closure, got: {mods}"
    # The exact transitive set varies by Python version; just assert
    # a pure-Python stdlib module made it.


def test_recursive_off_does_not_expand(tmp_path):
    """recursive_stdlib=False (default) keeps closure shallow."""
    from pcc.frontends.python.pipeline import (
        _collect_multi_source_relative_closure,
    )

    src = tmp_path / "u.py"
    src.write_text("import shlex\ndef f(s): return shlex.split(s)\n", encoding="utf-8")
    srcs, mods = _collect_multi_source_relative_closure(
        [str(src)],
        ["u"],
        recursive_stdlib=False,
    )
    assert (
        "shlex" not in mods
    ), f"shlex should NOT be pulled when recursive=False, got: {mods}"


def test_recursive_stdlib_does_not_expand_native_textwrap(tmp_path):
    """The native ``textwrap.dedent`` surface must not pull the host
    ``textwrap.py`` implementation and its unsupported regex initialization
    into a strict recursive closure.
    """
    from pcc.frontends.python.pipeline import (
        _collect_multi_source_relative_closure,
    )

    src = tmp_path / "u.py"
    src.write_text(
        'import textwrap\nvalue = textwrap.dedent("  value\\n")\n',
        encoding="utf-8",
    )
    _, mods = _collect_multi_source_relative_closure(
        [str(src)],
        ["u"],
        recursive_stdlib=True,
    )
    assert "textwrap" not in mods


def test_recursive_off_keeps_existing_libpython_path():
    """Default (recursive=False) keeps the historical
    ``py_cpy_import`` path for non-native builtin stdlib imports."""
    program = textwrap.dedent("""
        import keyword
        def f(s: str):
            return s
        """)
    ir_text = _compile_to_ll(
        program,
        "rec_keyword_off",
        recursive=False,
        libpython_mode="auto",
    )
    # Without the flag, status quo: py_cpy_import path is exercised
    assert "@.cpy.mod.keyword" in ir_text


def test_plain_emit_only_does_not_auto_expand_stdlib_closure(tmp_path):
    """A module IR probe stays single-file unless closure output is requested."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "probe.py"
    out = tmp_path / "probe.ll"
    profile: dict[str, object] = {}
    src.write_text(
        "import re\n\ndef matches(text: str) -> bool:\n"
        "    return re.match('x', text) is not None\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        profile=profile,
    )

    counters = profile["counters"]
    assert counters["auto_files"] == 1
    assert "multi_files" not in counters
    assert "; ---- module:" not in out.read_text(encoding="utf-8")


def test_no_libpython_auto_recursive_stdlib_scans_multi_file_closure(tmp_path):
    """Strict multi-file compile should notice stdlib imports in siblings."""
    from pcc.frontends.python.pipeline import compile_python

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    main = pkg / "__main__.py"
    worker = pkg / "worker.py"
    main.write_text(
        "from .worker import run\n" "run()\n",
        encoding="utf-8",
    )
    worker.write_text(
        "import base64\n" "\n" "def run():\n" "    print(base64.b64encode(b'ab'))\n",
        encoding="utf-8",
    )
    out = tmp_path / "pkg.ll"
    compile_python(
        str(main),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")
    assert "; ---- module: base64 ----" in ir_text
    assert "@.cpy.mod.base64" not in ir_text


def test_recursive_stdlib_stops_at_function_bodies(tmp_path):
    """Only imports reachable while initializing a module are eager closure
    dependencies; imports in deferred function bodies remain deferred."""
    from pcc.frontends.python.pipeline import _stdlib_absolute_imports_in

    src = tmp_path / "module_scope.py"
    src.write_text(
        textwrap.dedent("""
            if True:
                import keyword

            class C:
                import textwrap

                def method(self):
                    import pydoc

            def later():
                import email
            """),
        encoding="utf-8",
    )

    imports = _stdlib_absolute_imports_in(str(src))

    assert "keyword" in imports
    assert "textwrap" in imports
    assert "pydoc" not in imports
    assert "email" not in imports


def test_fast_import_discovery_matches_initialization_boundary(tmp_path):
    from pcc.frontends.python import pipeline

    src = tmp_path / "initialization_scope.py"
    src.write_text(
        textwrap.dedent('''
            """
            import random
            """
            from dataclasses import dataclass, field
            if True: import base64

            class C:
                import textwrap

                def method(self):
                    import pydoc

            def later():
                import email
            '''),
        encoding="utf-8",
    )
    source = src.read_text(encoding="utf-8")

    eager = pipeline._source_absolute_imports_for_discovery(
        source,
        include_function_bodies=False,
    )
    all_imports = pipeline._source_absolute_imports_for_discovery(
        source,
        include_function_bodies=True,
    )

    assert set(eager) == {"base64", "textwrap"}
    assert set(all_imports) == {"base64", "textwrap", "pydoc", "email"}


def test_recursive_stdlib_filters_scaffold_before_dependency_expansion():
    from pcc.frontends.python import pipeline

    source = _REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "stmt_misc_lowering.py"
    _, modules = pipeline._prepare_multi_source_compile_closure(
        [str(source)],
        ["pcc.frontends.python.codegen.stmt_misc_lowering"],
        recursive_stdlib=True,
        ir_scaffold_mode="on",
    )

    assert "pcc.ir.ir" in modules
    assert "pcc.ir.compat" not in modules
    assert "pcc.ir.binding" not in modules
    assert "ctypes" not in modules
    assert "ctypes.util" not in modules


@pytest.mark.parametrize(
    "source",
    [
        "def load():\n    import base64\n",
        "def load():\n    from base64 import b64encode\n",
        "if True: import base64\n",
        "if True: from base64 import b64encode\n",
        "from base64 import (\n    b64encode,\n)\n",
    ],
)
def test_native_stdlib_discovery_does_not_invoke_full_parser(
    tmp_path, monkeypatch, source
):
    """Import discovery is lexical metadata, not a second frontend parse."""
    from pcc.frontends.python import py_lift
    from pcc.frontends.python import pipeline

    src = tmp_path / "native_import.py"
    src.write_text(source, encoding="utf-8")

    def reject_parse(*_args, **_kwargs):
        raise AssertionError("native stdlib discovery invoked the full parser")

    monkeypatch.setattr(py_lift, "parse_and_lift", reject_parse)

    assert pipeline._source_uses_native_stdlib(str(src))


@pytest.mark.parametrize(
    "source",
    [
        "# import base64\n",
        "text = 'import base64'\n",
        'text = """\nimport base64\n"""\n',
        "from .base64 import b64encode\n",
    ],
)
def test_native_stdlib_discovery_ignores_non_import_text(tmp_path, source):
    from pcc.frontends.python import pipeline

    src = tmp_path / "not_an_absolute_import.py"
    src.write_text(source, encoding="utf-8")

    assert not pipeline._source_uses_native_stdlib(str(src))


def test_recursive_stdlib_uses_compiled_export_after_native_lowering_declines(
    tmp_path,
):
    """A builtin module's specialized lowering gets first refusal, then its
    recursively compiled export is used instead of libpython."""
    from pcc.frontends.python.pipeline import compile_python_multi

    src = tmp_path / "class_body_regex.py"
    out = tmp_path / "class_body_regex.ll"
    src.write_text(
        textwrap.dedent(r"""
            import re

            class C:
                pattern = re.compile(r"[a-z]+", re.ASCII)

            print("ok")
            """),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(src), str(_REPO_ROOT / "pcc" / "ir" / "support" / "re.py")],
        str(out),
        emit_llvm_only=True,
        entry_module="class_body_regex",
        module_names=["class_body_regex", "re"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert "@user_re_compile" in ir_text
    assert "call ptr (ptr) @py_cpy_import" not in ir_text


def test_dynamic_getattr_on_compiled_module_uses_native_registry(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    provider = tmp_path / "provider.py"
    main = tmp_path / "main.py"
    out = tmp_path / "dynamic_getattr.ll"
    provider.write_text("VALUE = 'native'\n", encoding="utf-8")
    main.write_text(
        textwrap.dedent("""
            import provider

            name = "VALUE"
            print(getattr(provider, name))
            """),
        encoding="utf-8",
    )

    compile_python_multi(
        [str(main), str(provider)],
        str(out),
        emit_llvm_only=True,
        entry_module="main",
        module_names=["main", "provider"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert "call ptr (ptr, ptr) @py_module_attr_get" in ir_text
    assert "call ptr (ptr) @py_cpy_import" not in ir_text


def test_builtin_object_and_complex_type_values_stay_native(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "object_type_value.py"
    out = tmp_path / "object_type_value.ll"
    src.write_text(
        "ObjectType = object\nComplexType = complex\n"
        "print(ObjectType)\nprint(ComplexType)\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert "call ptr @py_builtin_type_for_tag(i64 -1)" in ir_text
    assert "call ptr @py_builtin_type_for_tag(i64 16)" in ir_text
    assert "call ptr (ptr) @py_cpy_import" not in ir_text


def test_builtin_super_type_value_stays_native(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "super_type_value.py"
    out = tmp_path / "super_type_value.ll"
    src.write_text(
        "registry = {super: 'registered'}\n" "print(registry[super])\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert "call ptr @py_builtin_type_for_tag(i64 -3)" in ir_text
    assert "call ptr (ptr) @py_cpy_import" not in ir_text


def test_strict_function_value_uses_native_function_object(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "native_function_value.py"
    out = tmp_path / "native_function_value.ll"
    src.write_text(
        textwrap.dedent("""
            def transform(value, suffix="!"):
                return str(value) + suffix

            callback = transform
            print(callback(3))
            """),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert "call ptr @py_func_new_named" in ir_text
    assert "call ptr @py_cpy_wrap_pcc_" not in ir_text


def test_os_curdir_default_value_stays_native(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "os_curdir_default.py"
    out = tmp_path / "os_curdir_default.ll"
    src.write_text(
        "import os\ndef open_here(path=os.curdir):\n    return path\n"
        "print(open_here())\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    ir_text = out.read_text(encoding="utf-8")
    assert 'c".\\00"' in ir_text
    assert "call ptr (ptr) @py_cpy_import" not in ir_text


def test_cycle_detection(tmp_path):
    """Modules that recursively import each other don't hang the
    compiler."""
    a = tmp_path / "mod_a.py"
    b = tmp_path / "mod_b.py"
    a.write_text("from mod_b import g\ndef f(): return g()\n", encoding="utf-8")
    b.write_text("from mod_a import f\ndef g(): return 42\n", encoding="utf-8")
    # Just shouldn't hang; doesn't need to fully succeed.
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        from pcc.frontends.python.pipeline import (
            _collect_multi_source_relative_closure,
        )

        srcs, mods = _collect_multi_source_relative_closure(
            [str(a)],
            ["mod_a"],
            recursive_stdlib=True,
        )
        # Both mod_a and mod_b should be in closure, no infinite loop.
        assert "mod_a" in mods
    finally:
        sys.path.remove(str(tmp_path))


def test_c_extension_falls_back():
    """A module that's a C extension (_socket, _struct, etc.) cannot
    be parsed by pcc; should fall back to py_cpy_import gracefully
    rather than crash."""
    program = textwrap.dedent("""
        import _socket
        def f():
            return None
        """)
    # Should compile without crashing; _socket import goes to py_cpy_*
    ir_text = _compile_to_ll(
        program,
        "rec_socket",
        recursive=True,
        libpython_mode="auto",
    )
    # Memory-transport pass sharding namespaces module-private symbols while
    # preserving the dynamic import edge.  Assert both supported symbol shapes
    # and, more importantly, the actual libpython fallback call.
    assert re.search(r"@(?:__pcp\d+_)?\.cpy\.mod\._socket\b", ir_text)
    assert re.search(r"\bcall ptr @py_cpy_import\(", ir_text)
