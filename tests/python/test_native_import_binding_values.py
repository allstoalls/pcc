"""Import bindings keep their object in every value position.

An import inside a function binds a local.  When an escaping nested function
captures it, hoisting boxes the name into a one-element cell, and the import
statement must store into that cell: it never did, so the closure read None
("'NoneType' object is not callable").  A native sibling bound by
``import helper`` is also a real module object in value position; it used to
be the module-name placeholder string.
"""

import os
import subprocess
import sys

import pytest

from pcc.frontends.python.codegen.hoist_boxing import _box_stmts
from pcc.frontends.python.py_ast import (
    Assign,
    DynType,
    Import,
    ImportFrom,
    IntLit,
    IntType,
    Name,
    SourceSpan,
    Subscript,
)


HELPER = '''def double(value):
    return value * 2


def triple(value):
    return value * 3
'''

PROGRAM = '''import gc
import helper as module_helper

module_alias = module_helper
MODULE_ROW = (type(module_alias).__name__, module_alias.double(2))


def call_with(fn, value):
    return fn(value)


def from_import_escape(value):
    from helper import double

    def inner(item):
        return double(item)

    return call_with(inner, value)


def import_alias_escape(value):
    import helper as h

    def inner(item):
        return h.double(item)

    return call_with(inner, value)


def import_plain_escape(value):
    import helper

    def inner(item):
        return helper.triple(item)

    return call_with(inner, value)


def branch_import_escape(value, flag):
    if flag:
        from helper import triple as scale
    else:
        from helper import double as scale

    def inner(item):
        return scale(item)

    return call_with(inner, value)


def direct_closure(value):
    from helper import double

    def inner(item):
        return double(item) + 1

    return inner(value)


def rebinding_after_import(value):
    from helper import double

    def inner(item):
        return double(item)

    first = call_with(inner, value)
    double = lambda item: -item
    return first, call_with(inner, value)


def main():
    gc.collect()
    print(MODULE_ROW)
    print(from_import_escape(5), import_alias_escape(6), import_plain_escape(7))
    print(branch_import_escape(3, True), branch_import_escape(3, False))
    print(direct_closure(4))
    print(rebinding_after_import(9))


main()
'''


def test_escaping_closures_see_import_bindings_on_every_gc(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    (tmp_path / "helper.py").write_text(HELPER, encoding="utf-8")
    source = tmp_path / "import_bindings.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True,
        timeout=20, cwd=str(tmp_path),
    )
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout == "('module', 4)\n10 12 21\n9 6\n9\n(18, -9)\n"
    output = tmp_path / "import_bindings"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"


_SPAN = SourceSpan(file="t.py", line=7, col=4, end_line=7, end_col=20)
_DYN = DynType(name="dyn")


def _cell_store(bound, temp):
    return Assign(
        span=_SPAN,
        targets=(Subscript(
            span=_SPAN, ty=_DYN,
            obj=Name(span=_SPAN, ty=_DYN, ident=bound),
            idx=IntLit(span=_SPAN, ty=IntType(name="int"), value=0),
        ),),
        value=Name(span=_SPAN, ty=_DYN, ident=temp),
    )


@pytest.mark.parametrize("stmt,boxed,expected_names,stores", [
    (ImportFrom(span=_SPAN, module="helper", names=(("double", None), ("triple", "t"))),
     ("double",), (("double", "__pcc_boxed_import_double_7_4"), ("triple", "t")),
     (("double", "__pcc_boxed_import_double_7_4"),)),
    (Import(span=_SPAN, names=(("helper", None), ("pkg.leaf", "leaf"))),
     ("helper", "leaf"),
     (("helper", "__pcc_boxed_import_helper_7_4"), ("pkg.leaf", "__pcc_boxed_import_leaf_7_4")),
     (("helper", "__pcc_boxed_import_helper_7_4"), ("leaf", "__pcc_boxed_import_leaf_7_4"))),
])
def test_boxed_import_binds_a_temporary_then_stores_the_cell(
    stmt, boxed, expected_names, stores,
):
    rewritten = _box_stmts((stmt,), boxed)
    assert rewritten[0] == type(stmt)(**{**stmt.__dict__, "names": expected_names})
    assert rewritten[1:] == tuple(_cell_store(bound, temp) for bound, temp in stores)


@pytest.mark.parametrize("stmt", [
    ImportFrom(span=_SPAN, module="helper", names=(("double", None),)),
    ImportFrom(span=_SPAN, module="helper", names=(("*", None),)),
    Import(span=_SPAN, names=(("pkg.leaf", None),)),
])
def test_unboxed_star_and_unaliased_dotted_imports_are_left_alone(stmt):
    # ``pkg`` is boxed for the dotted form: its package object may not exist
    # natively, so that binding keeps its previous lowering.
    assert _box_stmts((stmt,), ("pkg", "other")) == (stmt,)
