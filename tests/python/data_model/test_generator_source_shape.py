from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import FuncDef, Return, Name


def test_generator_function_source_shape_is_visible_to_frontend():
    mod = parser.parse(
        "def g():\n"
        "    yield 1\n"
        "    return 2\n",
        "generator_probe.py",
    )

    fn = mod.body[0]
    assert isinstance(fn, FuncDef)
    # Parser currently represents unsupported yield as a sentinel call in many
    # paths; this test locks that the function body is still available for the
    # generator-lowering pass rather than erased.
    assert len(fn.body) == 2
    assert isinstance(fn.body[1], Return)
