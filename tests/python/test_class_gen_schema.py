from __future__ import annotations

from pathlib import Path

from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.py_ast import Assign, Attr, ClassDef, FuncDef, Name


def test_class_info_declares_extern_method_defs_in_init_schema():
    """ClassInfo.extern_method_defs is read by pcc-native bootstrap code.

    It must be part of the declared instance layout rather than a CPython-style
    dynamic post-init attribute.
    """
    src = Path("pcc/frontends/python/codegen/class_gen.py")
    mod = parse_and_lift(
        src.read_text(encoding="utf-8"),
        str(src),
        "pcc.frontends.python.codegen.class_gen",
    )

    init_fn = None
    for stmt in mod.body:
        if isinstance(stmt, ClassDef) and stmt.name == "ClassInfo":
            for body_stmt in stmt.body:
                if isinstance(body_stmt, FuncDef) and body_stmt.name == "__init__":
                    init_fn = body_stmt
                    break
            break
    assert init_fn is not None

    fields = set()
    for stmt in init_fn.body:
        if not isinstance(stmt, Assign):
            continue
        for target in stmt.targets:
            if (
                isinstance(target, Attr)
                and isinstance(target.obj, Name)
                and target.obj.ident == "self"
            ):
                fields.add(target.name)

    assert "extern_method_defs" in fields
