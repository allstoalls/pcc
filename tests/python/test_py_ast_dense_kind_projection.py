from __future__ import annotations

from dataclasses import fields, replace

from pcc.frontends.python import py_ast
from pcc.frontends.python.codegen import expr_dispatch_lowering
from pcc.frontends.python.codegen import stmt_dispatch_lowering
from pcc.frontends.python.codegen.expr_dispatch_lowering import ExprDispatchLoweringMixin
from pcc.frontends.python.codegen.stmt_dispatch_lowering import StmtDispatchLoweringMixin
from pcc.frontends.python.pipeline_ast_wire import _py_ast_from_wire, _py_ast_to_wire


def _span() -> py_ast.SourceSpan:
    return py_ast.SourceSpan("dense.py", 1, 0, 1, 1)


def _int_lit(value: int = 1) -> py_ast.IntLit:
    return py_ast.IntLit(
        span=_span(),
        ty=py_ast.IntType(name="int"),
        value=value,
    )


def test_expr_stmt_dataclasses_keep_only_semantic_fields() -> None:
    literal = _int_lit()
    statement = py_ast.Pass(span=_span())
    assert tuple(field.name for field in fields(py_ast.IntLit)) == (
        "span",
        "ty",
        "value",
    )
    assert tuple(field.name for field in fields(py_ast.Pass)) == ("span",)
    assert not hasattr(literal, "kind_id")
    assert not hasattr(statement, "kind_id")
    assert replace(literal, value=2).value == 2
    assert _int_lit() == py_ast.IntLit(
        span=_span(), ty=py_ast.IntType(name="int"), value=1
    )


def test_ast_wire_roundtrip_has_no_dispatch_metadata() -> None:
    statement = py_ast.Assign(
        span=_span(),
        targets=(
            py_ast.Name(
                span=_span(),
                ty=py_ast.IntType(name="int"),
                ident="value",
            ),
        ),
        value=_int_lit(7),
    )
    encoded = _py_ast_to_wire(statement)

    assert "kind_id" not in repr(encoded)
    decoded = _py_ast_from_wire(encoded)
    assert decoded == statement
    assert not hasattr(decoded, "kind_id")
    assert not hasattr(decoded.value, "kind_id")


def test_exact_stmt_dispatch_uses_semantic_classifier(monkeypatch) -> None:
    class Probe(StmtDispatchLoweringMixin):
        def __init__(self) -> None:
            self._generator_ctx_stack = []
            self.seen = None

        def _emit_assign(self, statement) -> None:
            self.seen = statement

    statement = py_ast.Assign(
        span=_span(),
        targets=(),
        value=_int_lit(),
    )
    classified = []
    original = stmt_dispatch_lowering._stmt_is_assign

    def classify(candidate):
        classified.append(candidate)
        return original(candidate)

    monkeypatch.setattr(stmt_dispatch_lowering, "_stmt_is_assign", classify)
    probe = Probe()
    probe._emit_stmt_impl(statement)
    assert probe.seen is statement
    assert classified == [statement]


def test_unknown_stmt_keeps_structural_slow_path() -> None:
    class DuckAssign:
        targets = ()
        value = _int_lit()

    class Probe(StmtDispatchLoweringMixin):
        def __init__(self) -> None:
            self._generator_ctx_stack = []
            self.seen = None

        def _emit_assign(self, statement) -> None:
            self.seen = statement

    statement = DuckAssign()
    probe = Probe()
    probe._emit_stmt_impl(statement)
    assert probe.seen is statement


def test_exact_expr_dispatch_uses_semantic_classifier(monkeypatch) -> None:
    class Probe(ExprDispatchLoweringMixin):
        def _emit_list_literal(self, expression):
            return expression

    expression = py_ast.ListExpr(
        span=_span(),
        ty=py_ast.ListType(name="list", elem=py_ast.IntType(name="int")),
        elems=(),
    )
    classified = []
    original = expr_dispatch_lowering._expr_is_list

    def classify(candidate, kind):
        classified.append(candidate)
        return original(candidate, kind)

    monkeypatch.setattr(expr_dispatch_lowering, "_expr_is_list", classify)
    assert Probe()._emit_expr_impl(expression) is expression
    assert classified == [expression]


def test_unknown_expr_keeps_structural_slow_path() -> None:
    class DuckType:
        name = "list"

    class DuckList:
        elems = ()
        ty = DuckType()

    class Probe(ExprDispatchLoweringMixin):
        def _emit_list_literal(self, expression):
            return expression

    expression = DuckList()
    assert Probe()._emit_expr_impl(expression) is expression
