from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.global_dce import GlobalDCEPass


def _transformed_ast(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    transformed = GlobalDCEPass().run(ast, ctx) or ast
    return transformed


def _function_names(ast):
    return [
        ext.decl.name
        for ext in ast.ext
        if isinstance(ext, c_ast.FuncDef) and getattr(ext, "decl", None) is not None
    ]


def test_global_dce_removes_unused_static_function():
    ast = _transformed_ast(
        """
        static int dead(void) { return 1; }
        int main(void) { return 0; }
        """
    )

    assert _function_names(ast) == ["main"]


def test_global_dce_removes_unreachable_static_call_chain():
    ast = _transformed_ast(
        """
        static int leaf(void) { return 1; }
        static int mid(void) { return leaf(); }
        int main(void) { return 0; }
        """
    )

    assert _function_names(ast) == ["main"]


def test_global_dce_keeps_static_function_reachable_from_main():
    ast = _transformed_ast(
        """
        static int helper(void) { return 1; }
        int main(void) { return helper(); }
        """
    )

    assert _function_names(ast) == ["helper", "main"]


def test_global_dce_keeps_static_function_referenced_in_global_initializer():
    ast = _transformed_ast(
        """
        static int helper(void) { return 1; }
        static int (*fp)(void) = helper;
        int main(void) { return fp(); }
        """
    )

    assert "helper" in _function_names(ast)
