"""Tests for the PCC pass framework."""

import unittest


from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes import (
    PassContext,
    PassPipeline,
    default_pass_groups,
    disable_pass_group,
    validate_default_pass_groups,
)
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import AllocStrategy, VarInfo
from pcc.frontends.c.passes.escape_analysis import EscapeAnalysisPass
from pcc.frontends.c.passes.alloc_decision import AllocDecisionPass
from pcc.frontends.c.passes.clang_compat import TailCallPass
from pcc.frontends.c.passes.canonicalize import CanonicalizerPass
from pcc.frontends.c.passes.nsw_inference import NSWInferencePass
from pcc.frontends.c.passes.propagation import CopyPropagationPass
from pcc.frontends.c.parse.c_parser import CParser


def _analyze(code):
    """Parse C code and run the default HighTier pipeline."""
    ast = CParser().parse(code)
    ctx = PassContext()
    pipeline = PassPipeline.default()
    pipeline.run_high_tier(ast, ctx)
    return ctx


class TestEscapeAnalysis(unittest.TestCase):

    def test_simple_local_no_escape(self):
        ctx = _analyze("int main() { int x = 5; return x; }")
        v = ctx.get_var("main", "x")
        assert not v.escapes
        assert v.single_def
        assert v.def_count == 1
        assert v.use_count == 1

    def test_address_taken_escapes(self):
        ctx = _analyze("int main() { int x = 5; int *p = &x; return *p; }")
        v = ctx.get_var("main", "x")
        assert v.address_taken
        assert v.escapes

    def test_passed_to_call_escapes(self):
        ctx = _analyze("""
            void foo(int *p) {}
            int main() { int x = 5; foo(&x); return x; }
        """)
        v = ctx.get_var("main", "x")
        assert v.passed_to_call
        assert v.escapes

    def test_loop_var_multi_def(self):
        ctx = _analyze("""
            int main() {
                int sum = 0;
                for (int i = 0; i < 10; i++) { sum += i; }
                return sum;
            }
        """)
        vi = ctx.get_var("main", "i")
        assert not vi.escapes
        assert not vi.single_def
        assert vi.def_count >= 2

        vs = ctx.get_var("main", "sum")
        assert not vs.escapes
        assert not vs.single_def

    def test_function_param(self):
        ctx = _analyze("int add(int a, int b) { return a + b; }")
        va = ctx.get_var("add", "a")
        assert va.is_param
        assert va.single_def
        assert not va.escapes

    def test_leaf_function(self):
        ctx = _analyze("int square(int x) { return x * x; }")
        f = ctx.get_func("square")
        assert f.is_leaf

    def test_non_leaf_function(self):
        ctx = _analyze("""
            int foo() { return 1; }
            int main() { return foo(); }
        """)
        f = ctx.get_func("main")
        assert not f.is_leaf

    def test_setjmp_detection(self):
        ctx = _analyze("""
            int main() {
                int x = 0;
                setjmp(0);
                return x;
            }
        """)
        f = ctx.get_func("main")
        assert f.has_setjmp

    def test_goto_detection(self):
        ctx = _analyze("""
            int main() {
                goto end;
                end:
                return 0;
            }
        """)
        f = ctx.get_func("main")
        assert f.has_goto


class TestAllocDecision(unittest.TestCase):

    def test_simple_scalar_ssa(self):
        ctx = _analyze("int main() { int x = 5; return x; }")
        v = ctx.get_var("main", "x")
        assert v.alloc_strategy == AllocStrategy.SSA

    def test_address_taken_stays_alloca(self):
        ctx = _analyze("int main() { int x = 5; int *p = &x; return *p; }")
        v = ctx.get_var("main", "x")
        assert v.alloc_strategy == AllocStrategy.ALLOCA

    def test_multi_def_register_hint(self):
        ctx = _analyze("""
            int main() {
                int x = 0;
                x = 1;
                x = 2;
                return x;
            }
        """)
        v = ctx.get_var("main", "x")
        assert v.alloc_strategy == AllocStrategy.REGISTER_HINT

    def test_struct_stays_alloca(self):
        ctx = _analyze("""
            struct S { int a; int b; };
            int main() { struct S s; s.a = 1; return s.a; }
        """)
        v = ctx.get_var("main", "s")
        assert v.alloc_strategy == AllocStrategy.ALLOCA

    def test_param_ssa(self):
        ctx = _analyze("int id(int x) { return x; }")
        v = ctx.get_var("id", "x")
        assert v.alloc_strategy == AllocStrategy.SSA

    def test_pointer_single_def_ssa(self):
        """Use minimal pipeline to test alloc decision without DCE interference."""
        ast = CParser().parse("""
            int main() {
                int arr[10];
                int *p = arr;
                return *p;
            }
        """)
        ctx = PassContext()
        pipeline = PassPipeline.minimal()
        pipeline.run_high_tier(ast, ctx)
        v = ctx.get_var("main", "p")
        assert v.alloc_strategy == AllocStrategy.SSA


class TestNSWInference(unittest.TestCase):

    def test_for_loop_bounded(self):
        ctx = _analyze("""
            int main() {
                int sum = 0;
                for (int i = 0; i < 100; i++) {
                    sum += i;
                }
                return sum;
            }
        """)
        v = ctx.get_var("main", "i")
        assert v.range_min == 0
        assert v.range_max == 100

    def test_unsigned_range(self):
        ctx = _analyze("""
            int main() {
                unsigned int x = 42;
                return x;
            }
        """)
        v = ctx.get_var("main", "x")
        assert v.range_min == 0
        assert v.range_max == (1 << 32) - 1


class TestPassPipeline(unittest.TestCase):

    def test_default_pipeline_describes(self):
        p = PassPipeline.default()
        desc = p.describe()
        assert "escape-analysis" in desc
        assert "alloc-decision" in desc
        assert "nsw-inference" in desc
        assert "llvm-o2-pipeline" not in desc

    def test_disabled_pipeline_is_noop(self):
        ctx = PassContext()
        ctx.enabled = False
        ast = CParser().parse("int main() { int x = 5; return x; }")
        pipeline = PassPipeline.default()
        pipeline.run_high_tier(ast, ctx)
        assert len(ctx.functions) == 0  # no analysis was run

    def test_stats_populated(self):
        ctx = _analyze("""
            int main() { int a = 1; int b = 2; return a + b; }
        """)
        assert ctx.stats.get("escape_analysis.functions_analyzed", 0) > 0
        assert ctx.stats.get("escape_analysis.vars_analyzed", 0) > 0

    def test_default_pass_groups_cover_default_pipeline(self):
        problems = validate_default_pass_groups()
        assert problems["missing"] == ()
        assert problems["extra"] == ()

        groups = default_pass_groups()
        grouped = [
            pass_name
            for pass_names in groups.values()
            for pass_name in pass_names
        ]
        assert len(grouped) == len(set(grouped))

    def test_disable_pass_group_marks_all_members_disabled(self):
        ctx = PassContext()
        disable_pass_group(ctx, "loop-inline")
        assert not ctx.is_pass_enabled("loop-opt")
        assert not ctx.is_pass_enabled("inline-opt")
        assert ctx.is_pass_enabled("canonicalize")

    def test_failing_pass_is_skipped_when_fail_open(self):
        class ExplodingPass(ASTPass):
            name = "explode"

            def run(self, ast, ctx):
                raise RuntimeError("boom")

        ctx = PassContext()
        ast = CParser().parse("int main() { return 0; }")
        pipeline = PassPipeline().add_high(ExplodingPass())
        out = pipeline.run_high_tier(ast, ctx)

        assert out is ast
        assert ctx.pass_metrics["explode"].failures == 1
        assert ctx.pass_metrics["explode"].runs == 0
        assert "RuntimeError: boom" in ctx.pass_metrics["explode"].last_detail

    def test_pass_report_round_trips_metrics(self):
        ctx = PassContext()
        ctx.note_pass_run("canonicalize", "high", 1.25)

        restored = PassContext.from_pass_report(ctx.pass_report())

        assert restored.pass_metrics["canonicalize"].tier == "high"
        assert restored.pass_metrics["canonicalize"].runs == 1
        assert restored.pass_metrics["canonicalize"].total_time_ms == 1.25



    def test_copy_propagation_skips_reassigned_function_pointer(self):
        ast = CParser().parse("""
            typedef int (*binop)(int, int);
            int add(int a, int b) { return a + b; }
            int sub(int a, int b) { return a - b; }
            int main() {
                binop f = add;
                int r1 = f(10, 3);
                f = sub;
                int r2 = f(10, 3);
                return r1 + r2;
            }
        """)
        ctx = PassContext()
        out = CopyPropagationPass().run(ast, ctx)
        if out is None:
            out = ast
        main = out.ext[-1]
        block_items = main.body.block_items
        call1 = block_items[1].init
        assign = block_items[2]
        call2 = block_items[3].init

        assert isinstance(call1, c_ast.FuncCall)
        assert isinstance(call1.name, c_ast.ID)
        assert call1.name.name == "f"

        assert isinstance(assign, c_ast.Assignment)
        assert isinstance(assign.lvalue, c_ast.ID)
        assert isinstance(assign.rvalue, c_ast.ID)
        assert assign.lvalue.name == "f"
        assert assign.rvalue.name == "sub"

        assert isinstance(call2, c_ast.FuncCall)
        assert isinstance(call2.name, c_ast.ID)
        assert call2.name.name == "f"

    def test_tail_call_skips_alloca_backed_argument(self):
        ir_text = """
define i32 @"f"(i32 %".1", i32* %".2")
{
.4:
  %"a" = alloca i32
  %"x" = alloca i32
  %"tmp" = sub nsw i32 %".1", 1
  %"calltmp" = call i32 @"f"(i32 %"tmp", i32* %"x")
  ret i32 %"calltmp"
}
"""
        out = TailCallPass().run(ir_text, PassContext())
        assert 'tail call' not in out

    def test_tail_call_skips_pointer_loaded_from_local_slot(self):
        # Regression: a pointer into a local buffer that reaches the returned
        # call via a pointer-typed local (pre-mem2reg: load from an alloca
        # slot) must NOT be marked tail. Marking it tail frees the caller frame
        # (and the buffer) before the callee dereferences it. This is the
        # py_int_to_str_obj `return py_str_new(p, ...)` shape that produced
        # str(int) == "\\0\\0" in the pcc-built runtime.
        ir_text = """
define ptr @"f"(i64 %".1")
{
.4:
  %"buf" = alloca [32 x i8]
  %"paddr" = alloca ptr
  %"end" = getelementptr i8, ptr %"buf", i64 32
  store ptr %"end", ptr %"paddr"
  %"p" = load ptr, ptr %"paddr"
  %"calltmp" = call ptr @"py_str_new"(ptr %"p", i64 2)
  ret ptr %"calltmp"
}
"""
        out = TailCallPass().run(ir_text, PassContext())
        assert 'tail call' not in out

    def test_tail_call_marks_returned_call_with_heap_pointer(self):
        # Control: a pointer that does NOT derive from a local (here from a
        # call result) is safe to tail-call, so the optimization still applies.
        ir_text = """
define ptr @"g"()
{
.3:
  %"heap" = call ptr @"make_heap"()
  %"calltmp" = call ptr @"py_str_new"(ptr %"heap", i64 2)
  ret ptr %"calltmp"
}
"""
        out = TailCallPass().run(ir_text, PassContext())
        assert 'tail call ptr @"py_str_new"' in out

    def test_tail_call_marks_simple_returned_call(self):
        ir_text = """
define i32 @"id"(i32 %".1")
{
.3:
  %"calltmp" = call i32 @"next"(i32 %".1")
  ret i32 %"calltmp"
}
"""
        out = TailCallPass().run(ir_text, PassContext())
        assert 'tail call i32 @"next"' in out

    def test_copy_propagation_treats_field_store_as_object_redefinition(self):
        ast = CParser().parse("""
            struct S { int x; int y; };
            struct S global_s = {1, 2};
            int main() {
                struct S s = global_s;
                if (s.x < 3) s.x = 3;
                return s.x;
            }
        """)
        ctx = PassContext()
        out = CopyPropagationPass().run(ast, ctx)
        if out is None:
            out = ast
        main = out.ext[-1]
        block_items = main.body.block_items
        if_stmt = block_items[1]
        ret = block_items[2]

        assert isinstance(if_stmt, c_ast.If)
        assert isinstance(if_stmt.cond.left, c_ast.StructRef)
        assert isinstance(if_stmt.cond.left.name, c_ast.ID)
        assert if_stmt.cond.left.name.name == "s"

        assign = if_stmt.iftrue
        assert isinstance(assign, c_ast.Assignment)
        assert isinstance(assign.lvalue, c_ast.StructRef)
        assert isinstance(assign.lvalue.name, c_ast.ID)
        assert assign.lvalue.name.name == "s"

        assert isinstance(ret, c_ast.Return)
        assert isinstance(ret.expr, c_ast.StructRef)
        assert isinstance(ret.expr.name, c_ast.ID)
        assert ret.expr.name.name == "s"

    def test_canonicalize_does_not_fold_float_self_subtraction(self):
        ast = CParser().parse("""
            int main() {
                double d = 1.0 / 0.0;
                return (d - d) != (d - d);
            }
        """)
        ctx = PassContext()
        out = CanonicalizerPass().run(ast, ctx)
        if out is None:
            out = ast
        ret = out.ext[-1].body.block_items[-1]

        assert isinstance(ret, c_ast.Return)
        assert isinstance(ret.expr, c_ast.BinaryOp)
        assert ret.expr.op == "!="
        assert isinstance(ret.expr.left, c_ast.BinaryOp)
        assert ret.expr.left.op == "-"
        assert isinstance(ret.expr.right, c_ast.BinaryOp)
        assert ret.expr.right.op == "-"

    def test_canonicalize_does_not_strength_reduce_float_multiplication(self):
        ast = CParser().parse("""
            int main() {
                double x = 3.5;
                return (2 * x) > 0.0;
            }
        """)
        ctx = PassContext()
        out = CanonicalizerPass().run(ast, ctx)
        if out is None:
            out = ast
        ret = out.ext[-1].body.block_items[-1]

        assert isinstance(ret, c_ast.Return)
        assert isinstance(ret.expr, c_ast.BinaryOp)
        assert ret.expr.op == ">"
        assert isinstance(ret.expr.left, c_ast.BinaryOp)
        assert ret.expr.left.op == "*"

    def test_canonicalize_keeps_large_negative_integer_literal_as_unary_op(self):
        ast = CParser().parse("""
            long long b = -754324895235774564;
            int main(void) { return b < 0 ? 0 : 1; }
        """)
        ctx = PassContext()
        out = CanonicalizerPass().run(ast, ctx)
        if out is None:
            out = ast
        decl = out.ext[0]

        assert isinstance(decl, c_ast.Decl)
        assert isinstance(decl.init, c_ast.UnaryOp)
        assert decl.init.op == "-"
        assert isinstance(decl.init.expr, c_ast.Constant)
        assert decl.init.expr.value == "754324895235774564"

    def test_canonicalize_keeps_large_bitnot_integer_literal_as_unary_op(self):
        ast = CParser().parse("""
            long long b = ~2147483648;
            int main(void) { return b == -2147483649LL ? 0 : 1; }
        """)
        ctx = PassContext()
        out = CanonicalizerPass().run(ast, ctx)
        if out is None:
            out = ast
        decl = out.ext[0]

        assert isinstance(decl, c_ast.Decl)
        assert isinstance(decl.init, c_ast.UnaryOp)
        assert decl.init.op == "~"
        assert isinstance(decl.init.expr, c_ast.Constant)
        assert decl.init.expr.value == "2147483648"

    def test_canonicalize_does_not_fold_octal_literal_as_decimal(self):
        ast = CParser().parse("""
            int main(void) {
                return 010 + 1;
            }
        """)
        ctx = PassContext()
        out = CanonicalizerPass().run(ast, ctx)
        if out is None:
            out = ast
        ret = out.ext[-1].body.block_items[-1]

        assert isinstance(ret, c_ast.Return)
        assert isinstance(ret.expr, c_ast.BinaryOp)
        assert ret.expr.op == "+"
        assert isinstance(ret.expr.left, c_ast.Constant)
        assert ret.expr.left.value == "010"


class TestIntegrationWithCodegen(unittest.TestCase):
    """Ensure the pass framework doesn't break actual compilation."""

    def test_basic_eval(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        assert e.evaluate("int main() { return 42; }") == 42

    def test_loop_eval(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate("""
            int main() {
                int s = 0;
                for (int i = 1; i <= 10; i++) s += i;
                return s;
            }
        """)
        assert r == 55

    def test_pointer_eval(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate("""
            int main() {
                int x = 99;
                int *p = &x;
                *p = 100;
                return x;
            }
        """)
        assert r == 100

    def test_recursive_eval(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate("""
            int fib(int n) {
                if (n <= 1) return n;
                return fib(n-1) + fib(n-2);
            }
            int main() { return fib(10); }
        """)
        assert r == 55

    def test_goto_label_eval_survives_default_pipeline(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate("""
            int main() {
            start:
                goto next;
                return 1;
            success:
                return 0;
            next:
            foo:
                goto success;
                return 1;
            }
        """)
        assert r == 0

    def test_sizeof_integer_promotion_survives_default_pipeline(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate(
            "int main() { return sizeof(((short)1) + 0); }",
            use_system_cpp=False,
            use_compile_cache=False,
        )
        assert r == 4

    def test_function_pointer_store_survives_default_pipeline(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate(
            """
            int add(int a, int b) { return a + b; }
            int main() {
                int (*fp)(int, int) = add;
                return fp(2, 3);
            }
            """,
            use_system_cpp=False,
            use_compile_cache=False,
        )
        assert r == 5

    def test_function_pointer_parameter_survives_default_pipeline(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate(
            """
            int add(int a, int b) { return a + b; }
            int call_cb(int (*cb)(int, int)) { return cb(2, 4); }
            int main() { return call_cb(add); }
            """,
            use_system_cpp=False,
            use_compile_cache=False,
        )
        assert r == 6

    def test_unsigned_ssa_promoted_locals_preserve_compare_semantics(self):
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        e = CEvaluator()
        r = e.evaluate(
            """
            int main(void) {
                int errors = 0;
                unsigned int a = 3u;
                unsigned int big = (1u << 31);
                unsigned int x = 0u;
                unsigned int y = x - 1u;

                if (a > big) errors++;
                if (y < x) errors++;

                return errors;
            }
            """,
            use_system_cpp=False,
            use_compile_cache=False,
        )
        assert r == 0


if __name__ == "__main__":
    unittest.main()
