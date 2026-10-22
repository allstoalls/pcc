from __future__ import annotations

import re
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).absolute().parents[2]


def test_class_instance_is_pinned_across_init(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "constructor_root.py"
    out = tmp_path / "constructor_root.ll"
    src.write_text(
        textwrap.dedent("""
            class Holder:
                def __init__(self, value: str):
                    self.value = value

            def make(value: str) -> Holder:
                return Holder(value)
            """).lstrip(),
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )

    text = out.read_text(encoding="utf-8")
    body = text.split("define external ptr @user_constructor_root_make", 1)[1]
    body = body.split("\n}", 1)[0]
    new_pos = body.index("@py_instance_new")
    pin_pos = body.index("@pcc_gc_pin", new_pos)
    init_pos = body.index("@user_constructor_root_Holder___init__", pin_pos)
    unpin_pos = body.index("@pcc_gc_unpin", init_pos)
    assert new_pos < pin_pos < init_pos < unpin_pos


def test_classgen_typed_null_bool_literal_arg_uses_bool_fallback():
    from pcc.ir.compat import ir
    from pcc.frontends.python.codegen import class_gen
    from pcc.frontends.python.py_ast import BoolLit, BoolType

    class Parent:
        def _emit_expr(self, _expr):
            return ir.Value(ir.IntType(1), "null")

    expr = BoolLit(span=None, ty=BoolType(name="bool"), value=False)

    value = class_gen._classgen_emit_arg_expr(
        Parent(),
        expr,
        BoolType(name="bool"),
    )

    assert str(value.type) == "i1"
    assert class_gen._classgen_value_ref_text(value) == "false"


def test_classgen_bool_literal_ref_text_uses_i1_constant_text():
    from pcc.ir.compat import ir
    from pcc.frontends.python.codegen import class_gen
    from pcc.frontends.python.py_ast import BoolLit, BoolType

    expr = BoolLit(span=None, ty=BoolType(name="bool"), value=False)

    assert class_gen._classgen_bool_literal_ref_text(expr, ir.IntType(1)) == "false"


def test_classgen_typed_null_i1_ref_text_is_false():
    from pcc.ir.compat import ir
    from pcc.frontends.python.codegen import class_gen

    value = ir.Value(ir.IntType(1), "null")

    assert class_gen._classgen_value_ref_text(value) == "false"


def test_dataclass_constructor_attr_args_do_not_lower_to_null(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    out = tmp_path / "py_parse.ll"

    compile_python(
        str(REPO_ROOT / "pcc" / "frontends" / "python" / "py_parse.py"),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )

    text = out.read_text(encoding="utf-8")
    bad_name_ctor = re.compile(
        r"@user_pcc_frontends_python_py_parse__Name___init__" r"\(ptr [^,]+, ptr null, i64 null\)"
    )
    assert bad_name_ctor.search(text) is None
