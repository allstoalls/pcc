from __future__ import annotations

import textwrap

from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.py_ast import FloatType, FuncDef, FuncType, Return
from pcc.frontends.python.type_infer import infer_module


def test_extern_double_restype_flows_to_call_expression() -> None:
    module = parse_and_lift(
        textwrap.dedent("""
            from pcc.extern import c_double as f64, extern as declare

            floor_c: "extern" = declare("floor", (f64,), f64)

            def floor_value(value: float) -> float:
                return floor_c(value)
            """).lstrip(),
        "extern_double.py",
        "extern_double",
    )

    typed = infer_module(module)
    declaration = typed.body[1]
    assert isinstance(declaration.targets[0].ty, FuncType)
    assert isinstance(declaration.targets[0].ty.ret, FloatType)

    function = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    returned = next(stmt for stmt in function.body if isinstance(stmt, Return))
    assert returned.value is not None
    assert isinstance(returned.value.ty, FloatType)
