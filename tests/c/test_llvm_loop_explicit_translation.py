from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.llvm_loop_explicit import (
    IndVarsPass,
    LICMPass,
    LoopDeletionPass,
    LoopFullUnrollPass,
    LoopRotatePass,
    LoopUnrollPass,
    SimpleLoopUnswitchPass,
)


def _transform(code: str, pass_):
    ast = CParser().parse(code)
    ctx = PassContext()
    transformed = pass_.run(ast, ctx) or ast
    return transformed


def _main_body(ast):
    func = next(
        ext for ext in ast.ext
        if isinstance(ext, c_ast.FuncDef) and ext.decl.name == "main"
    )
    return func.body


def test_loop_unroll_full_rewrites_small_local_iv_for_loop():
    ast = _transform(
        """
        int main(void) {
            int sum = 0;
            for (int i = 0; i < 3; ++i) {
                sum += i;
            }
            return sum;
        }
        """,
        LoopFullUnrollPass(),
    )

    body = _main_body(ast)
    assert not any(isinstance(item, c_ast.For) for item in body.block_items or ())
    assigns = [item for item in body.block_items or () if isinstance(item, c_ast.Assignment)]
    assert len(assigns) == 3
    constants = [assign.rvalue.value for assign in assigns]
    assert constants == ["0", "1", "2"]


def test_loop_unroll_full_keeps_loop_with_break():
    ast = _transform(
        """
        int main(void) {
            int sum = 0;
            for (int i = 0; i < 3; ++i) {
                if (i == 1) break;
                sum += i;
            }
            return sum;
        }
        """,
        LoopFullUnrollPass(),
    )

    body = _main_body(ast)
    assert any(isinstance(item, c_ast.For) for item in body.block_items or ())


def test_simple_loop_unswitch_hoists_invariant_if_around_loop():
    ast = _transform(
        """
        int main(void) {
            int flag = 1;
            int sum = 0;
            for (int i = 0; i < 3; ++i) {
                if (flag)
                    sum += i;
                else
                    sum += 1;
            }
            return sum;
        }
        """,
        SimpleLoopUnswitchPass(),
    )

    body = _main_body(ast)
    outer_if = next(item for item in body.block_items or () if isinstance(item, c_ast.If))
    assert isinstance(outer_if.iftrue, c_ast.For)
    assert isinstance(outer_if.iffalse, c_ast.For)


def test_simple_loop_unswitch_skips_value_assigned_in_for_condition():
    ast = _transform(
        """
        int main(void) {
            const unsigned char *z = "'x';";
            int i, c;
            int delim = z[0];

            for (i = 1; (c = z[i]) != 0; i++) {
                if (c == delim)
                    return i;
            }

            return i;
        }
        """,
        SimpleLoopUnswitchPass(),
    )

    body = _main_body(ast)
    loops = [item for item in body.block_items or () if isinstance(item, c_ast.For)]

    assert len(loops) == 1
    assert not any(isinstance(item, c_ast.If) for item in body.block_items or ())


def test_loop_deletion_removes_empty_local_iv_loop():
    ast = _transform(
        """
        int main(void) {
            for (int i = 0; i < 4; ++i) {
            }
            return 0;
        }
        """,
        LoopDeletionPass(),
    )

    body = _main_body(ast)
    assert not any(isinstance(item, c_ast.For) for item in body.block_items or ())


def test_licm_hoists_simple_invariant_assignment():
    ast = _transform(
        """
        int main(void) {
            int flag = 3;
            int sum = 0;
            int t = 0;
            for (int i = 0; i < 2; ++i) {
                t = flag + 1;
                sum += t + i;
            }
            return sum;
        }
        """,
        LICMPass(),
    )

    body = _main_body(ast)
    assert isinstance(body.block_items[3], c_ast.Assignment)
    assert isinstance(body.block_items[4], c_ast.For)
    loop_body = body.block_items[4].stmt
    assert isinstance(loop_body, c_ast.Compound)
    assert len(loop_body.block_items or ()) == 1


def test_licm_keeps_assignment_to_loop_local_variable_in_scope():
    ast = _transform(
        """
        int main(void) {
            for (;;) {
                int max = 0;
                max = 1;
                break;
            }
            return 0;
        }
        """,
        LICMPass(),
    )

    body = _main_body(ast)
    loop = next(item for item in body.block_items or () if isinstance(item, c_ast.For))
    loop_body = loop.stmt
    assert isinstance(loop_body, c_ast.Compound)
    assert isinstance(loop_body.block_items[0], c_ast.Decl)
    assert isinstance(loop_body.block_items[1], c_ast.Assignment)


def test_licm_keeps_loop_state_reset_when_target_changes_later():
    ast = _transform(
        """
        int main(void) {
            int *p = 0;
            int *base = 0;
            for (int i = 0; i < 2; ++i) {
                p = base;
                ++p;
            }
            return 0;
        }
        """,
        LICMPass(),
    )

    body = _main_body(ast)
    loop = next(item for item in body.block_items or () if isinstance(item, c_ast.For))
    loop_body = loop.stmt
    assert isinstance(loop_body, c_ast.Compound)
    assert isinstance(loop_body.block_items[0], c_ast.Assignment)


def test_loop_unroll_full_keeps_loop_with_top_level_body_declaration():
    ast = _transform(
        """
        int main(void) {
            int sum = 0;
            for (int i = 0; i < 3; ++i) {
                int tmp = i;
                sum += tmp;
            }
            return sum;
        }
        """,
        LoopFullUnrollPass(),
    )

    body = _main_body(ast)
    assert any(isinstance(item, c_ast.For) for item in body.block_items or ())


def test_loop_unroll_rewrites_medium_small_local_iv_loop():
    ast = _transform(
        """
        int main(void) {
            int sum = 0;
            for (int i = 0; i < 6; ++i) {
                sum += i;
            }
            return sum;
        }
        """,
        LoopUnrollPass(),
    )

    body = _main_body(ast)
    assert not any(isinstance(item, c_ast.For) for item in body.block_items or ())
    assigns = [item for item in body.block_items or () if isinstance(item, c_ast.Assignment)]
    assert len(assigns) == 6


def test_loop_rotate_rewrites_while_into_if_do_while():
    ast = _transform(
        """
        int main(void) {
            int i = 0;
            while (i < 3) {
                i++;
            }
            return i;
        }
        """,
        LoopRotatePass(),
    )

    body = _main_body(ast)
    outer_if = next(item for item in body.block_items or () if isinstance(item, c_ast.If))
    assert isinstance(outer_if.iftrue, c_ast.DoWhile)


def test_indvars_normalizes_assignment_step_to_increment():
    ast = _transform(
        """
        int main(void) {
            int sum = 0;
            for (int i = 0; i < 4; i = i + 1) {
                sum += i;
            }
            return sum;
        }
        """,
        IndVarsPass(),
    )

    body = _main_body(ast)
    loop = next(item for item in body.block_items or () if isinstance(item, c_ast.For))
    assert isinstance(loop.next, c_ast.UnaryOp)
    assert loop.next.op == "p++"
