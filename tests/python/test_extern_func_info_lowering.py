from __future__ import annotations

from pcc.frontends.python.codegen.extern_func_info_lowering import (
    ExternFuncInfoLoweringMixin,
)


def test_find_user_funcdef_accepts_structural_ast_wire_node():
    foreign_funcdef_type = type("FuncDef", (), {})
    foreign = foreign_funcdef_type()
    foreign.name = "target"

    host = ExternFuncInfoLoweringMixin()
    module_type = type("Module", (), {})
    host.ast_module = module_type()
    host.ast_module.body = (foreign,)
    host._ast_body = ()
    host._cross_module_func_defs = {}
    host._module_block_func_defs = {}

    assert host._find_user_funcdef("target") is foreign
