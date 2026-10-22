from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.inline_opt import InlineOptPass


def _transformed_ast(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    transformed = InlineOptPass().run(ast, ctx) or ast
    return transformed


def _function(ast, name: str):
    return next(
        ext for ext in ast.ext
        if isinstance(ext, c_ast.FuncDef) and ext.decl.name == name
    )


def test_inline_opt_rewrites_eta_wrapper_call_to_direct_target():
    ast = _transformed_ast(
        """
        int add1(int x) { return x + 1; }
        int wrapper(int x) { return add1(x); }
        int main(void) { return wrapper(41); }
        """
    )

    main = _function(ast, "main")
    ret = main.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "add1"


def test_inline_opt_resolves_transitive_eta_wrappers():
    ast = _transformed_ast(
        """
        int leaf(int x) { return x + 1; }
        int mid(int x) { return leaf(x); }
        int top(int x) { return mid(x); }
        int main(void) { return top(7); }
        """
    )

    main = _function(ast, "main")
    ret = main.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "leaf"


def test_inline_opt_keeps_wrapper_when_param_types_do_not_match_target():
    ast = _transformed_ast(
        """
        int target(unsigned char x) { return x; }
        int wrapper(int x) { return target(x); }
        int main(void) { return wrapper(300); }
        """
    )

    main = _function(ast, "main")
    ret = main.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "wrapper"


def test_inline_opt_keeps_wrapper_when_return_types_do_not_match():
    ast = _transformed_ast(
        """
        long target(int x) { return x; }
        int wrapper(int x) { return target(x); }
        int main(void) { return wrapper(7); }
        """
    )

    main = _function(ast, "main")
    ret = main.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "wrapper"
