"""Issue 11.B.1 part 2: codegen-side wiring for recursive stdlib.

When ``recursive_stdlib=True`` causes the closure walker to pull
e.g. ``keyword`` into the multi-file compile set, the codegen for an
``import keyword`` statement in user code must:
  - NOT emit ``py_cpy_import("keyword")`` (that pulls libpython)
  - register keyword as a native module alias so subsequent
    ``keyword.X`` accesses route to native ``user_keyword_X`` symbols
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, recursive: bool) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        recursive_stdlib=recursive,
    )
    return out.read_text(encoding="utf-8")


def _count_py_cpy_import_for(ir_text: str, mod_name: str) -> int:
    """Count call sites that ``py_cpy_import(@.cpy.mod.<mod_name>)``."""
    # Match a call followed by a getelementptr pulling the module name
    # global. LLVM emits the GEP on the line just above the call.
    pattern = re.compile(
        r"%\.\w+\s*=\s*getelementptr[^\n]+@\.cpy\.mod\."
        + re.escape(mod_name)
        + r"\b[^\n]*\n[^\n]*=\s*call[^\n]+@py_cpy_import",
        re.MULTILINE,
    )
    return len(pattern.findall(ir_text))


def test_recursive_import_skips_py_cpy_import():
    """``import keyword`` with recursive_stdlib=True should NOT
    emit ``py_cpy_import("keyword")`` because keyword is now in the
    native compile closure."""
    program = textwrap.dedent("""
        import keyword
        def f(s: str):
            pass
        """)
    ir_text = _compile_to_ll(program, "rec_import_keyword_on", recursive=True)
    n = _count_py_cpy_import_for(ir_text, "keyword")
    assert n == 0, (
        f"recursive=True should produce ZERO py_cpy_import for keyword; "
        f"got {n} call sites"
    )


def test_pcc_stdlib_constant_import_stays_native():
    """``import string`` should use the pcc/stdlib port and resolve
    exported literal constants without CPython module fallback."""
    program = textwrap.dedent("""
        import string
        def f():
            return string.ascii_lowercase
        """)
    ir_text = _compile_to_ll(program, "rec_import_string_on", recursive=True)
    assert "call ptr @py_cpy_import" not in ir_text
    assert "@.cpy.mod.string" not in ir_text
    from pcc.backend.self_backend_parse import decode_llvm_c_string

    constants = re.findall(r'c"(?:[^"\\]|\\[0-9A-Fa-f]{2})*"', ir_text)
    assert b"abcdefghijklmnopqrstuvwxyz\0" in [
        decode_llvm_c_string(value) for value in constants
    ]


def test_pcc_stdlib_from_import_constant_stays_native():
    """``from string import CONST`` should bind the exported pcc/stdlib
    constant directly, using normal CPython spelling without a module
    fallback."""
    program = textwrap.dedent("""
        from string import ascii_lowercase as letters
        def f():
            return letters
        print(f())
        """)
    ir_text = _compile_to_ll(
        program,
        "rec_from_import_string_const_on",
        recursive=True,
    )
    assert "call ptr @py_cpy_import" not in ir_text
    assert "@.cpy.mod.string" not in ir_text
    assert "abcdefghijklmnopqrstuvwxyz" in ir_text


def test_dotted_pcc_stdlib_import_routes_to_native_submodule():
    """``import urllib.parse`` should bind the top-level CPython name
    while routing ``urllib.parse.fn`` to the native pcc/stdlib
    submodule."""
    program = textwrap.dedent("""
        import urllib.parse
        def f():
            return urllib.parse.quote("a/b", "/")
        print(f())
        """)
    ir_text = _compile_to_ll(
        program,
        "rec_import_urllib_parse_on",
        recursive=True,
    )
    assert "call ptr @py_cpy_import" not in ir_text
    assert "@.cpy.mod.urllib" not in ir_text
    assert "@user_urllib_parse_quote" in ir_text


def test_owned_urljoin_provider_and_result_handoff_ir(tmp_path, monkeypatch):
    """A wrapper result remains owned when passed to a dynamic slot call.

    This reproduces the local result handoff that failed for page_url. The
    callee is a generic callback, so no package installer special case applies.
    Only host IR emission is checked here; native execution is a separate gate.
    """
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")
    monkeypatch.setenv("PCC_PY_FRONTEND_IN_PROCESS_CODEGEN", "1")
    source = tmp_path / "urljoin_handoff.py"
    output = tmp_path / "urljoin_handoff.ll"
    source.write_text(
        "import urllib.parse\n"
        "def resolve(base, ref):\n"
        "    return urllib.parse.urljoin(base, ref)\n"
        "def invoke(callback, base, ref):\n"
        "    page_url = resolve(base, ref)\n"
        "    return callback(page_url)\n"
        "def main():\n"
        "    print(resolve('https://example.test/a/b', '../c?x#f'))\n"
        "main()\n",
        encoding="utf-8",
    )
    compile_python(
        str(source), str(output), emit_llvm_only=True,
        recursive_stdlib=True, libpython_mode="off", ir_scaffold_mode="on",
        backend="self",
    )
    text = output.read_text(encoding="utf-8")
    has_public_provider = "@user_urllib_parse_urljoin" in text
    has_resolution_helper = "@user_urllib_parse__urljoin_text" in text
    assert has_public_provider, "owned urljoin export is missing"
    assert has_resolution_helper, "owned URL resolution body is missing"
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
    assert "strict_nolib_unavailable" not in text
    invoke = re.search(r"^define [^\n]*@user_[^\n(]*_invoke\([^\n]*\).*?^}",
                       text, re.M | re.S)
    assert invoke is not None
    assert "@pcc_gc_root_copy_lease" in invoke.group(0)
    assert "@py_obj_call_slots" in invoke.group(0)


def test_native_sibling_import_alias_value_position_stays_native():
    """A function-local ``import pkg.sub as sub; return sub`` should not
    re-materialize the native sibling module through CPython fallback."""
    from pcc.frontends.python.pipeline import compile_python_multi

    root = _BUILD / "native_pkg_alias_value"
    pkg = root / "pkg_alias_value"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "def get_sub():\n" "    import pkg_alias_value.sub as sub\n" "    return sub\n",
        encoding="utf-8",
    )
    (pkg / "sub.py").write_text("VALUE = 7\n", encoding="utf-8")
    (root / "main.py").write_text(
        "import pkg_alias_value\n" "def main():\n" "    print('ok')\n" "main()\n",
        encoding="utf-8",
    )
    out = root / "out.ll"
    compile_python_multi(
        [
            str(root / "main.py"),
            str(pkg / "__init__.py"),
            str(pkg / "sub.py"),
        ],
        str(out),
        emit_llvm_only=True,
        entry_module="main",
        module_names=["main", "pkg_alias_value", "pkg_alias_value.sub"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    ir_text = out.read_text(encoding="utf-8")
    assert "call ptr @py_cpy_import" not in ir_text
    assert "@.cpy.mod.pkg_alias_value.sub" not in ir_text
    assert "pkg_alias_value.sub" in ir_text


def test_default_off_mode_auto_routes_available_native_stdlib():
    """With libpython off by default, an available pcc/stdlib provider
    should be selected automatically; users should not need a private
    ``std.*`` spelling or an explicit recursive_stdlib flag."""
    program = textwrap.dedent("""
        import string
        print(string.ascii_lowercase)
        """)
    ir_text = _compile_to_ll(
        program,
        "rec_import_string_auto_native",
        recursive=False,
    )
    assert "call ptr @py_cpy_import" not in ir_text
    assert "@.cpy.mod.string" not in ir_text
    assert "abcdefghijklmnopqrstuvwxyz" in ir_text


def test_recursive_off_mode_routes_warnings_filter_calls_native():
    """The closed stdlib graph must execute the pcc-owned warnings provider.

    Emit-only requests deliberately do not auto-close their stdlib graph, so
    request the recursive closure explicitly here.  Executable compilation
    enables the same closure automatically.
    """
    program = textwrap.dedent("""
        import warnings
        def f():
            warnings.warn("x", stacklevel=2)
            warnings.filterwarnings("ignore")
            warnings.simplefilter("default")
        """)
    ir_text = _compile_to_ll(
        program,
        "rec_import_warnings_auto_native",
        recursive=True,
    )
    assert "call ptr @py_cpy_import" not in ir_text
    assert "@.cpy.mod.warnings" not in ir_text
    assert "@user_warnings_warn" in ir_text
    assert "@user_warnings_filterwarnings" in ir_text
    assert "@user_warnings_simplefilter" in ir_text


def test_contextvar_constructor_and_sys_maxsize_stay_native():
    program = textwrap.dedent("""
        import sys
        from contextvars import ContextVar

        defaults = {"legacy": sys.maxsize}
        options = ContextVar("options", default=defaults)
        """)
    ir_text = _compile_to_ll(program, "native_contextvar_sys_maxsize", recursive=False)
    assert "call ptr @py_cpy_import" not in ir_text
    assert "call ptr @py_cpy_call" not in ir_text
    assert "call ptr @PyContextVar_New" in ir_text
    assert "9223372036854775807" in ir_text


def test_sys_byteorder_stays_native():
    program = textwrap.dedent("""
        import sys
        little_endian = sys.byteorder == "little"
        print(little_endian)
        """)
    ir_text = _compile_to_ll(program, "native_sys_byteorder", recursive=False)
    assert re.search(r"\bcall\b[^\n]*@py_cpy_import", ir_text) is None
    assert re.search(r"\bcall\b[^\n]*@py_cpy_getattr", ir_text) is None
    assert "little" in ir_text


def test_sys_implementation_stays_native():
    program = textwrap.dedent("""
        import sys
        cache_tag = sys.implementation.cache_tag
        implementation_name = sys.implementation.name
        print(cache_tag or implementation_name)
        """)
    ir_text = _compile_to_ll(
        program,
        "native_sys_implementation",
        recursive=False,
    )
    assert re.search(r"\bcall\b[^\n]*@py_cpy_import", ir_text) is None
    assert re.search(r"\bcall\b[^\n]*@py_cpy_getattr", ir_text) is None
    assert "pcc" in ir_text


def test_off_mode_preserves_py_cpy_import():
    """recursive_stdlib=False (default) keeps the historical
    ``py_cpy_import`` path."""
    program = textwrap.dedent("""
        import keyword
        def f(s: str):
            pass
        """)
    ir_text = _compile_to_ll(program, "rec_import_keyword_off", recursive=False)
    # Without recursive_stdlib, status quo: py_cpy_import is emitted.
    assert "@py_cpy_import" in ir_text


def test_owned_html_parser_subclass_constructor_and_callbacks_ir(tmp_path, monkeypatch):
    """Owned provider/export proof only; native callback execution is separate."""
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    monkeypatch.setenv('PCC_PY_FRONTEND_IN_PROCESS_CODEGEN', '1')
    source = tmp_path / 'html_subclass.py'
    output = tmp_path / 'html_subclass.ll'
    source.write_text("""from html.parser import HTMLParser
class Survey(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
    def handle_starttag(self, tag, attrs):
        self.items.append((tag, attrs))
    def handle_data(self, data):
        self.items.append(data)
def main():
    parser = Survey()
    parser.feed('<a h="a&amp;b">x</a>')
    parser.close()
    print(parser.items)
main()
""", encoding='utf-8')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   recursive_stdlib=True, libpython_mode='off',
                   ir_scaffold_mode='on', backend='self')
    text = output.read_text(encoding='utf-8')
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    assert 'strict_nolib_unavailable' not in text
    for method in ('__init__', 'feed', 'close', 'reset', 'parse_starttag', 'goahead'):
        assert re.search(r'^define [^\n]*@user_html_parser_HTMLParser_' + method + r'\(',
                         text, re.M), method
    constructor = re.search(r'^define [^\n]*@user_html_subclass_Survey___init__\([^\n]*\).*?^}',
                            text, re.M | re.S)
    assert constructor is not None
    assert 'HTMLParser.__init__.super' in constructor.group(0)
    parser = re.search(r'^define [^\n]*@user_html_parser_HTMLParser_parse_starttag\([^\n]*\).*?^}',
                       text, re.M | re.S)
    assert parser is not None
    assert '@py_obj_load_method' in parser.group(0)
    assert 'handle_starttag' in parser.group(0)
    assert '@py_obj_call_method' in parser.group(0)


def test_owned_html_parser_original_source_subclass_ir(tmp_path, monkeypatch):
    """Compile the unchanged original class body; full imported execution is separate."""
    import ast
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    monkeypatch.setenv('PCC_PY_FRONTEND_IN_PROCESS_CODEGEN', '1')
    acquire = (_REPO_ROOT / 'pcc/package/acquire.py').read_text(encoding='utf-8')
    node = next(node for node in ast.parse(acquire).body
                if isinstance(node, ast.ClassDef) and node.name == '_SimpleLinks')
    source = tmp_path / 'html_original_class.py'
    output = tmp_path / 'html_original_class.ll'
    source.write_text('from html.parser import HTMLParser\n' +
                      ast.get_source_segment(acquire, node) +
                      '\ndef main():\n    parser = _SimpleLinks()\n'
                      '    parser.feed(\'<a href="x&amp;y">\')\n'
                      '    parser.close()\n    print(parser.links)\nmain()\n',
                      encoding='utf-8')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   recursive_stdlib=True, libpython_mode='off',
                   ir_scaffold_mode='on', backend='self')
    text = output.read_text(encoding='utf-8')
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    assert 'strict_nolib_unavailable' not in text
    assert '@user_html_parser_HTMLParser___init__' in text
    assert '@user_html_original_class__SimpleLinks_handle_starttag' in text
