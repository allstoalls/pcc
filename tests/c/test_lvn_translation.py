import pytest

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext, PassPipeline
from pcc.frontends.c.passes.propagation import LocalValueNumberingPass


def _transformed_function(code: str):
    ast = CParser().parse(code)
    ctx = PassContext()
    PassPipeline.minimal().run_high_tier(ast, ctx)
    transformed = LocalValueNumberingPass().run(ast, ctx) or ast
    return next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))


def test_lvn_reuses_prior_decl_expression_within_same_block():
    func = _transformed_function(
        """
        int f(int x, int y) {
            int a = x + y;
            int b = x + y;
            return b;
        }
        """
    )

    second_decl = func.body.block_items[1]
    assert isinstance(second_decl, c_ast.Decl)
    assert isinstance(second_decl.init, c_ast.ID)
    assert second_decl.init.name == "a"


def test_lvn_invalidates_when_operand_changes():
    func = _transformed_function(
        """
        int f(int x, int y) {
            int a = x + y;
            x = 3;
            int b = x + y;
            return b;
        }
        """
    )

    third_stmt = func.body.block_items[2]
    assert isinstance(third_stmt, c_ast.Decl)
    assert isinstance(third_stmt.init, c_ast.BinaryOp)
    assert third_stmt.init.op == "+"


def test_lvn_reuses_prior_assignment_expression():
    func = _transformed_function(
        """
        int f(int x, int y) {
            int a = 0;
            int b = 0;
            a = x + y;
            b = x + y;
            return b;
        }
        """
    )

    fourth_stmt = func.body.block_items[3]
    assert isinstance(fourth_stmt, c_ast.Assignment)
    assert isinstance(fourth_stmt.rvalue, c_ast.ID)
    assert fourth_stmt.rvalue.name == "a"


def test_lvn_does_not_cache_pointer_dereference_across_store():
    func = _transformed_function(
        """
        int f(int *p) {
            int x;
            int y;
            x = *p;
            *p = 0;
            y = *p;
            return x != y;
        }
        """
    )

    fifth_stmt = func.body.block_items[4]
    assert isinstance(fifth_stmt, c_ast.Assignment)
    assert isinstance(fifth_stmt.rvalue, c_ast.UnaryOp)
    assert fifth_stmt.rvalue.op == "*"


def test_lvn_invalidates_bindings_when_loop_body_mutates_dependency():
    func = _transformed_function(
        """
        int f(int *arr, int n) {
            int *p = arr;
            int *first = p;
            for (int i = 0; i < n; i++) p++;
            int *second = p;
            return first == second;
        }
        """
    )

    fourth_stmt = func.body.block_items[3]
    assert isinstance(fourth_stmt, c_ast.Decl)
    assert isinstance(fourth_stmt.init, c_ast.ID)
    assert fourth_stmt.init.name == "p"


def test_lvn_does_not_reuse_values_across_different_declared_types():
    func = _transformed_function(
        """
        typedef int T0;
        typedef long T1;
        int f(void *p) {
            T0 *p0 = p;
            T1 *p1 = p;
            return p0 == (void *) p1;
        }
        """
    )

    second_decl = func.body.block_items[1]
    assert isinstance(second_decl, c_ast.Decl)
    assert isinstance(second_decl.init, c_ast.ID)
    assert second_decl.init.name == "p"


def test_lvn_does_not_reuse_integer_constant_across_different_types():
    func = _transformed_function(
        """
        int f(void) {
            int a = 1;
            long b = 1;
            return a + (int) b;
        }
        """
    )

    second_decl = func.body.block_items[1]
    assert isinstance(second_decl, c_ast.Decl)
    assert isinstance(second_decl.init, c_ast.Constant)
    assert second_decl.init.value == "1"


def test_lvn_does_not_reuse_string_literal_as_an_array_initializer():
    func = _transformed_function(
        r'''
        int f(void) {
            char first[20] = "abcdefgh";
            char second[20] = "abcdefgh";
            return first[0] + second[0];
        }
        '''
    )

    second_decl = func.body.block_items[1]
    assert isinstance(second_decl, c_ast.Decl)
    assert isinstance(second_decl.init, c_ast.Constant)
    assert second_decl.init.type == "string"
    assert second_decl.init.value == '"abcdefgh"'


@pytest.mark.parametrize("effect", (
    "mutate(&cached);",
    "if (mutate(&cached)) {}",
    "*alias = 1;",
    "alias[0] = 1;",
    "(*alias)++;",
))
def test_lvn_invalidates_cached_result_after_aliased_write(effect):
    func = _transformed_function(
        "int mutate(long *p); int f(void) { long cached = 0; "
        "long *alias = &cached; " + effect +
        "long next = 0; return next; }"
    )
    next_decl = func.body.block_items[-2]
    assert isinstance(next_decl.init, c_ast.Constant)
    assert next_decl.init.value == "0"


@pytest.mark.parametrize("effect", (
    "mutate(&x);",
    "if (mutate(&x)) {}",
    "*alias = 1;",
    "alias[0] = 1;",
    "(*alias)++;",
))
def test_lvn_invalidates_expression_operand_after_aliased_write(effect):
    func = _transformed_function(
        "int mutate(int *p); int f(int x, int y) { int cached = x + y; "
        "int *alias = &x; " + effect +
        "int next = x + y; return next; }"
    )
    next_decl = func.body.block_items[-2]
    assert isinstance(next_decl.init, c_ast.BinaryOp)
    assert next_decl.init.op == "+"


@pytest.mark.integration
def test_lvn_aliased_call_and_store_preserve_native_values(tmp_path):
    import subprocess
    from tests.owned_runtime_c_fixture import link_c_harness

    source = tmp_path / "lvn_aliased_memory.c"
    source.write_text(
        "int mutate(long *p) { *p = 1; return 1; }\n"
        "int main(void) {\n"
        "  long ready = 0;\n"
        "  if (mutate(&ready) != 1) return 1;\n"
        "  long sent = 0;\n"
        "  if (sent != 0 || ready != 1) return 2;\n"
        "  long first = 7;\n"
        "  long *alias = &first;\n"
        "  *alias = 9;\n"
        "  long second = 7;\n"
        "  if (first != 9 || second != 7) return 3;\n"
        "  return 0;\n"
        "}\n"
    )
    executable = tmp_path / "lvn_aliased_memory"
    link_c_harness(source, executable)
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
