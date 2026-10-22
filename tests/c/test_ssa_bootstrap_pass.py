from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass
from pcc.frontends.c.ssa import SSAPhi


_PARSER = CParser(lex_optimize=True, yacc_debug=False, yacc_optimize=True)


def test_ssa_bootstrap_pass_builds_supported_functions():
    ast = _PARSER.parse(
        """
        int choose(int x, int a, int b) {
            int y = a;
            if (x < 0) {
                y = a + 1;
            } else {
                y = b + 2;
            }
            return y;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    ssa_func = ctx.ssa_functions["choose"]
    assert any(
        isinstance(instr, SSAPhi)
        for block in ssa_func.blocks
        for instr in block.instructions
    )
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "built"
        and entry.target == "choose"
        for entry in ctx.log
    )


def test_ssa_bootstrap_pass_records_skipped_unsupported_functions():
    # Use a goto/label pattern the SSA builder explicitly rejects;
    # Switch is now lowered through SSA.
    ast = _PARSER.parse(
        """
        int dispatch(int n) {
            if (n) goto out;
        out:
            return n;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.ssa_functions == {}
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "skip_function"
        and entry.target == "dispatch"
        for entry in ctx.log
    )


def test_ssa_bootstrap_pass_skips_functions_with_static_local_storage():
    ast = _PARSER.parse(
        """
        int flip(void) {
            static int x = 0;
            x = !x;
            return x;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.ssa_functions == {}
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "skip_function"
        and entry.target == "flip"
        and "storage class static" in entry.detail
        for entry in ctx.log
    )


def test_ssa_bootstrap_pass_skips_functions_with_nonlocal_assignment_targets():
    ast = _PARSER.parse(
        """
        int g;

        int check(void) {
            g = 0;
            if (g) {
                return 1;
            }
            return 0;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.ssa_functions == {}
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "skip_function"
        and entry.target == "check"
        and "non-local assignment 'g'" in entry.detail
        for entry in ctx.log
    )


def test_ssa_bootstrap_pass_builds_short_circuit_condition_functions():
    ast = _PARSER.parse(
        """
        int helper(void);

        int check(int x) {
            if (x && helper()) {
                return 1;
            }
            return 0;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert "check" in ctx.ssa_functions
    assert any(
        block.name.startswith("if.cond.rhs.")
        for block in ctx.ssa_functions["check"].blocks
    )


def test_ssa_bootstrap_pass_builds_short_circuit_value_expressions():
    ast = _PARSER.parse(
        """
        int helper(void);

        int check(int x) {
            int y = x && helper();
            return y;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert "check" in ctx.ssa_functions
    assert any(
        block.name.startswith("logic.end.")
        for block in ctx.ssa_functions["check"].blocks
    )


def test_ssa_bootstrap_pass_builds_local_struct_with_field_access():
    """Phase 4 MVP: local struct-value with field access builds via stack alloca."""
    ast = _PARSER.parse(
        """
        struct S { int arr[10]; };
        int f(int n) {
            struct S s;
            s.arr[0] = n;
            return s.arr[0];
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert "f" in ctx.ssa_functions


def test_ssa_bootstrap_pass_rejects_struct_value_call_argument():
    """Phase 4 MVP: passing a struct-alloca local by value to a call."""
    ast = _PARSER.parse(
        """
        struct S { int v; };
        int helper(struct S s);
        int f(int n) {
            struct S s;
            s.v = n;
            return helper(s);
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "skip_function"
        and entry.target == "f"
        and "struct-value argument" in entry.detail
        for entry in ctx.log
    )


def test_ssa_bootstrap_pass_builds_scalar_struct_copy_assignment():
    """Phase 4 MVP: scalar-only struct-to-struct copy (`s2 = s1`) is now
    supported as a field-by-field load/store chain."""
    ast = _PARSER.parse(
        """
        struct S { int v; };
        int f(int n) {
            struct S s1;
            struct S s2;
            s1.v = n;
            s2 = s1;
            return s2.v;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert "f" in ctx.ssa_functions


def test_ssa_bootstrap_pass_builds_array_field_struct_copy():
    """Phase 4 MVP: struct-to-struct copy now extends to array fields
    via unrolled indexed load/store."""
    ast = _PARSER.parse(
        """
        struct S { int arr[3]; };
        int f(int n) {
            struct S s1;
            struct S s2;
            s1.arr[0] = n;
            s2 = s1;
            return s2.arr[0];
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.success"] == 1
    assert "f" in ctx.ssa_functions


def test_ssa_bootstrap_pass_rejects_struct_value_return():
    """Phase 4 MVP: `return s` on a struct-alloca local needs value copy
    we don't yet model, so reject."""
    ast = _PARSER.parse(
        """
        struct S { int v; };
        struct S f(int n) {
            struct S s;
            s.v = n;
            return s;
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "skip_function"
        and entry.target == "f"
        and "struct-value return" in entry.detail
        for entry in ctx.log
    )


def test_ssa_bootstrap_pass_skips_nested_short_circuit_value_expressions():
    ast = _PARSER.parse(
        """
        int helper(void);

        int check(int x) {
            return 1 + (x && helper());
        }
        """
    )
    ctx = PassContext()

    out = SSABootstrapPass().run(ast, ctx)

    assert out is None
    assert ctx.ssa_functions == {}
    assert ctx.stats["ssa.bootstrap.skipped"] == 1
    assert any(
        entry.pass_name == "ssa-bootstrap"
        and entry.action == "skip_function"
        and entry.target == "check"
        and "short-circuit expression '&&' in nested value position" in entry.detail
        for entry in ctx.log
    )
