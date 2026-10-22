"""Native ``str.format()`` with explicit field indices in no-libpython mode.

Auto fields (``{}``) and format specs (``{:.2f}``) were already native; this
adds explicit index fields (``{0}``, ``{1}``, with reuse/reorder and specs),
parsed at compile time in ``format_lowering.py`` (no runtime change).  Named
``{name}`` (kwargs) is still a follow-up.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_structured_spec_helper_exports_typed_auto_index():
    from pcc.frontends.python.export_meta import decode_type
    from pcc.frontends.python.pipeline import build_closed_world_context
    from pcc.frontends.python.py_ast import IntType, TupleType

    source = Path.cwd() / "pcc" / "frontends" / "python" / "codegen" / "format_lowering.py"
    _modules, exports, _derived = build_closed_world_context(
        [str(source)],
        ["pcc.frontends.python.codegen.format_lowering"],
        merge_exports=False,
    )
    methods = exports["pcc.frontends.python.codegen.format_lowering"]["FormatLoweringMixin"][
        "methods"
    ]
    helper = next(
        method for method in methods if method["name"] == "_emit_structured_spec_obj"
    )

    return_ty = decode_type(helper["return_ty"])
    auto_idx_ty = decode_type(helper["param_types"][-1])
    assert isinstance(return_ty, TupleType)
    assert len(return_ty.elems) == 2
    assert isinstance(return_ty.elems[1], IntType)
    assert isinstance(auto_idx_ty, IntType)


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )


def test_str_format_explicit_index_matches_cpython(tmp_path, monkeypatch):
    src = tmp_path / "fmt.py"
    exe = tmp_path / "fmt.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("{}-{}".format(1, 2))
            print("{0} {1} {0}".format("a", "b"))
            print("{1}{0}".format("x", "y"))
            print("{0:.2f}".format(3.14159))
            print("{0:>6}|".format("hi"))
            print("{:05d}".format(42))
            print("{2} {0} {1}".format("a", "b", "c"))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_starred_positional_matches_cpython(tmp_path, monkeypatch):
    src = tmp_path / "fmt_star.py"
    exe = tmp_path / "fmt_star.out"
    program = textwrap.dedent("""
        def main() -> None:
            stat = ["p0", "p1", "p2", "p3", "p4", "p5"]
            print("DIRECT: {5} ({1},{3}) PROXY: {4} ({0},{2})".format(*stat))
            pair = ("left", "right")
            print("{0} / {1}".format(*pair))
            dtypes = ("left", "right")
            print("ufunc {!r} cannot use {!r} and {!r}".format("add", *dtypes))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_named_matches_cpython(tmp_path, monkeypatch):
    # Named ``{name}`` fields (kwargs), incl. specs and mixing with positional
    # index fields.  Required letting ``format`` through the str-method kwargs
    # gate and threading kwargs into the compile-time format parser.
    src = tmp_path / "fmtn.py"
    exe = tmp_path / "fmtn.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("{name}={val}".format(name="x", val=5))
            print("{greeting}, {name}!".format(greeting="Hi", name="bob"))
            print("{name:>8}|".format(name="hi"))
            print("{n:05d}".format(n=42))
            print("{0} {name}".format("pos", name="kw"))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_name_bound_literal_takes_native_path(tmp_path, monkeypatch):
    """A function-local ``fmt = "<literal>"; fmt.format(name=...)``
    must take the same native compile-time-parsed path as a literal
    call site.

    Before this support the call fell through ``_maybe_emit_literal_str_format``
    (which only accepted ``StrLit`` at ``attr.obj``) into the libpython
    fallback. The fallback returned a CPython ``PyStr`` whose layout
    pcc's print path doesn't natively decode — visible as
    ``<object tag=N>`` when the result was stored in a pcc list / dict
    and later printed via multi-arg ``print``. The bug appears in
    ``numpy/__init__.py:663`` as
    ``{n: _msg.format(n=n, extended_msg=extended_msg) for n, extended_msg in _type_info}``;
    see docs/investigations/python-cpy-call-kw-null-kwarg-segfault-diagnostic.md.
    """
    src = tmp_path / "name_fmt.py"
    exe = tmp_path / "name_fmt.out"
    program = textwrap.dedent("""
        def main() -> None:
            items = [("a", "msg_a"), ("b", "msg_b")]
            fmt = "{n}={extended_msg}"
            d = {}
            for n, extended_msg in items:
                d[n] = fmt.format(n=n, extended_msg=extended_msg)
            for k in sorted(d.keys()):
                print(k, d[k])
            # Also exercise the dict-comprehension shape (matches the
            # exact numpy/__init__.py:663 idiom).
            d2 = {n: fmt.format(n=n, extended_msg=extended_msg)
                  for n, extended_msg in items}
            print("---")
            for k in sorted(d2.keys()):
                print(k, d2[k])

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_nested_field_spec_matches_cpython(tmp_path, monkeypatch):
    """A nested replacement field inside the format spec (``"{:>{}}"``), where
    the width / precision is supplied by another argument.

    Before this support ``format_lowering._parse_auto_format_literal`` bailed to
    ``None`` whenever the spec contained ``{`` or ``}`` (and the field scanner
    stopped at the first ``}``, mis-parsing the outer field), so any
    ``str.format`` with a runtime width/precision forced the libpython
    fallback — a hard error under ``--python-libpython=off``. CPython assigns
    auto field numbers in textual order: the outer field first, then the
    nested spec fields.
    """
    src = tmp_path / "fmt_nested.py"
    exe = tmp_path / "fmt_nested.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("[{:>{}}]".format("x", 5))
            n = 6
            print("[{:>{}}]".format(42, n))
            print("[{:>{w}}]".format("hi", w=8))
            print("[{:.{}f}]".format(3.14159, 2))
            p = 3
            print("[{:.{}f}]".format(3.14159, p))
            print("[{0:>{1}}]".format("z", 4))
            print("[{:>{}}|{:<{}}]".format("a", 3, "b", 4))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_fill_char_and_default_align_matches_cpython(tmp_path, monkeypatch):
    """String format spec ``[[fill]align]`` and the str default (left) align.

    The runtime ``format_string_builtin`` (py_format.c, a C-only helper) parsed
    a lone alignment char but not the optional preceding fill character, so
    ``'{:*^11}'`` / ``format('hi', '->6')`` raised instead of padding with the
    fill; and it defaulted str alignment to right (``>``) whereas CPython
    defaults strings to left (``<``). Covers ``format()``, ``str.format``, and
    f-strings since all route through the same runtime helper.
    """
    src = tmp_path / "fmt_fill.py"
    exe = tmp_path / "fmt_fill.out"
    program = textwrap.dedent("""
        def main() -> None:
            print(format('hi', '*^11'))
            print(format('hi', '->6'))
            print(f"{'hi':_<8}")
            print('[{:5}]'.format('hi'))
            print('[{:>5}]'.format('x'))
            print(format('ab', '*^7'))
            print('{:.<10}'.format('xy'))
            print(format('hello', '*^9.3'))
            # int / float fill chars (same [[fill]align] mini-language)
            print(format(42, '*>8'))
            print(f"{42:_^7}")
            print(format(42, '->6'))
            print(format(3.14, '*^10'))
            print(f"{255:*>8x}")
            print('[{:>8}]'.format(7))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_module_const_literal_takes_native_path(tmp_path, monkeypatch):
    """A module-level ``_MSG = "<literal>"`` referenced inside a function
    or a top-level dict-comprehension must also take the native fast
    path. Closes the exact ``numpy/__init__.py:637-663`` shape:

        _msg = ("module 'numpy' has no attribute '{n}'.\\n"
                "extended: {extended_msg}")
        __former_attrs__ = {
            n: _msg.format(n=n, extended_msg=extended_msg)
            for n, extended_msg in _type_info
        }

    The Assign is inside the top-level ``else:`` of
    ``if __NUMPY_SETUP__:``, so it's not in any FuncDef body.
    ``_resolve_str_literal_value`` must walk the ast_module.body
    (descending into top-level If/Try/With) when no current function
    binding exists.
    """
    src = tmp_path / "mod_fmt.py"
    exe = tmp_path / "mod_fmt.out"
    program = textwrap.dedent("""
        # Module-level constant; referenced inside the comprehension at
        # module init. The if-else wrapping matches numpy's exact shape.
        if not False:
            _MSG = (
                "key '{n}'\\n"
                "extended: {extended_msg}")
            _items = [("a", "msg_a"), ("b", "msg_b")]
            _d = {n: _MSG.format(n=n, extended_msg=extended_msg)
                  for n, extended_msg in _items}
            for k in sorted(_d.keys()):
                print(k)
                print(_d[k])
                print("---")
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython


def test_str_format_uses_dominating_literal_after_sibling_rebind(tmp_path, monkeypatch):
    src = tmp_path / "dominating_fmt.py"
    exe = tmp_path / "dominating_fmt.out"
    program = textwrap.dedent("""
        try:
            raise RuntimeError("first")
        except RuntimeError as exc:
            msg = "earlier: " + str(exc)

        if True:
            msg = "selected: {}"
            print(msg.format("ok"))
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "selected: ok\n"


def test_str_format_conversion_fields_matches_cpython(tmp_path, monkeypatch):
    """``{!r}``/``{!s}``/``{!a}`` conversion fields (also with index/name/spec).

    The conversion is parsed off the field in ``format_lowering.py`` and
    applied via ``py_obj_repr``/``py_obj_str``/``py_obj_ascii`` before the
    format spec runs (frontend-only change; both runtime tiers already ship
    those helpers).
    """
    src = tmp_path / "fmt_conv.py"
    exe = tmp_path / "fmt_conv.out"
    program = textwrap.dedent("""
        def main() -> None:
            print("{!r}".format("hi"))
            print("{!s}".format("hi"))
            print("{0!r} {0!s}".format("x"))
            print("{!r}".format(42))
            print("{name!r}".format(name="abc"))
            print("{!r:>8}".format("hi"))
            print("{0!r}-{1!s}".format([1, 2], 3.5))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
