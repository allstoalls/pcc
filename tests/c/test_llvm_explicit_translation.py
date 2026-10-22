import pytest

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.passes import PassContext
from pcc.frontends.c.passes.llvm_explicit import (
    AlignmentFromAssumptionsPass,
    AnnotationRemarksPass,
    CGProfilePass,
    CHRPass,
    CoroAnnotationElidePass,
    CoroCleanupPass,
    CoroEarlyPass,
    CoroElidePass,
    CoroSplitPass,
    ConstMergePass,
    DivRemPairsPass,
    EEInstrumentPass,
    ExtraSimpleLoopUnswitchPass,
    FloatToIntPass,
    InvalidatePass,
    InjectTLIMappingsPass,
    LibcallsShrinkwrapPass,
    LowerConstantIntrinsicsPass,
    LoopDistributePass,
    LoopVectorizePass,
    MoveAutoInitPass,
    OpenMPCGSCCPass,
    OpenMPOptPass,
    RecomputeGlobalsAAPass,
    RelLookupTableConverterPass,
    RequirePass,
    SLPVectorizerPass,
    TransformWarningPass,
    VectorCombinePass,
    VerifyPass,
)


def _transformed_ast(code: str, pass_):
    ast = CParser().parse(code)
    ctx = PassContext()
    return pass_.run(ast, ctx) or ast


def _transformed_function(code: str, pass_):
    transformed = _transformed_ast(code, pass_)
    return next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))


def test_lower_constant_intrinsics_folds_builtin_constant_p_true():
    func = _transformed_function(
        """
        int f(void) {
            return __builtin_constant_p(1 + 2);
        }
        """,
        LowerConstantIntrinsicsPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.Constant)
    assert ret.expr.value == "1"


def test_lower_constant_intrinsics_folds_builtin_constant_p_false():
    func = _transformed_function(
        """
        int f(int x) {
            return __builtin_constant_p(x);
        }
        """,
        LowerConstantIntrinsicsPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.Constant)
    assert ret.expr.value == "0"


def test_alignment_from_assumptions_lowers_builtin_to_pointer_expr():
    func = _transformed_function(
        """
        int *f(int *p) {
            return __builtin_assume_aligned(p, 16);
        }
        """,
        AlignmentFromAssumptionsPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert ret.expr.name == "p"


def test_alignment_from_assumptions_keeps_call_when_alignment_arg_has_side_effect():
    func = _transformed_function(
        """
        unsigned long side_effect(void);
        int *f(int *p) {
            return __builtin_assume_aligned(p, side_effect());
        }
        """,
        AlignmentFromAssumptionsPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "__builtin_assume_aligned"


def test_float2int_folds_plain_constant_cast():
    func = _transformed_function(
        """
        int f(void) {
            return (int)3.75;
        }
        """,
        FloatToIntPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.Constant)
    assert ret.expr.value == "3"


def test_float2int_keeps_out_of_range_cast_intact():
    func = _transformed_function(
        """
        int f(void) {
            return (int)5000000000.0;
        }
        """,
        FloatToIntPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.Cast)


def test_verify_records_node_count_for_simple_ast():
    ast = CParser().parse("int main(void) { return 0; }")
    ctx = PassContext()

    VerifyPass().run(ast, ctx)

    assert ctx.stats["verify.nodes"] > 0


def test_verify_rejects_malformed_funcdef():
    ast = c_ast.FileAST(
        [
            c_ast.FuncDef(
                None,
                None,
                c_ast.Compound(block_items=[]),
            )
        ]
    )
    ctx = PassContext()

    with pytest.raises(ValueError, match="malformed FuncDef"):
        VerifyPass().run(ast, ctx)


def test_require_pass_records_module_counts():
    ast = CParser().parse(
        """
        int g;
        int f(void) { return g; }
        """
    )
    ctx = PassContext()

    RequirePass().run(ast, ctx)

    assert ctx.stats["require.modules"] == 1
    assert ctx.stats["require.functions_seen"] == 1
    assert ctx.stats["require.globals_seen"] == 1


def test_invalidate_pass_records_boundary_without_mutating_ast():
    ast = CParser().parse("int f(void) { return 0; }")
    ctx = PassContext()
    ctx.bump("probe.stat")
    ctx.get_func("f")

    result = InvalidatePass().run(ast, ctx)

    assert result is None
    assert ctx.stats["invalidate.boundaries"] == 1
    assert ctx.stats["invalidate.tracked_functions"] == 1
    assert any(entry.pass_name == "invalidate" for entry in ctx.log)


def test_recompute_globalsaa_records_global_buckets():
    ast = CParser().parse(
        """
        int g;
        static const int c = 1;
        int *p;
        int *addr(void) { return &g; }
        """
    )
    ctx = PassContext()

    RecomputeGlobalsAAPass().run(ast, ctx)

    assert ctx.stats["recompute_globalsaa.globals"] == 3
    assert ctx.stats["recompute_globalsaa.const_globals"] == 1
    assert ctx.stats["recompute_globalsaa.mutable_globals"] == 2
    assert ctx.stats["recompute_globalsaa.pointer_globals"] == 1
    assert ctx.stats["recompute_globalsaa.address_taken_globals"] == 1


def test_annotation_remarks_records_builtin_hints_and_restrict():
    ast = CParser().parse(
        """
        int f(int *restrict p, int x) {
            __builtin_assume_aligned(p, 16);
            return __builtin_constant_p(1) + __builtin_expect(x, 0);
        }
        """
    )
    ctx = PassContext()

    AnnotationRemarksPass().run(ast, ctx)

    assert ctx.stats["annotation_remarks.modules"] == 1
    assert ctx.stats["annotation_remarks.restrict_pointers"] == 1
    assert ctx.stats["annotation_remarks.builtin_assume_aligned"] == 1
    assert ctx.stats["annotation_remarks.builtin_constant_p"] == 1
    assert ctx.stats["annotation_remarks.builtin_expect"] == 1


def test_transform_warning_records_blocker_kinds():
    ast = CParser().parse(
        """
        int f(int (*fp)(void), int x) {
        top:
            if (x)
                goto top;
            switch (x) {
                case 1:
                    return fp();
                default:
                    return 0;
            }
        }
        """
    )
    ctx = PassContext()

    TransformWarningPass().run(ast, ctx)

    assert ctx.stats["transform_warning.modules"] == 1
    assert ctx.stats["transform_warning.functions_with_blockers"] == 1
    assert ctx.stats["transform_warning.blocker_kinds"] == 4
    assert any(
        entry.pass_name == "transform-warning" and "indirect-call" in entry.detail
        for entry in ctx.log
    )


def test_cg_profile_records_direct_indirect_and_recursive_calls():
    ast = CParser().parse(
        """
        int leaf(void) { return 1; }
        int recur(int n) {
            if (n <= 0)
                return 0;
            return recur(n - 1);
        }
        int caller(int (*fp)(void)) {
            return leaf() + fp();
        }
        """
    )
    ctx = PassContext()

    CGProfilePass().run(ast, ctx)

    assert ctx.stats["cg_profile.functions_seen"] == 3
    assert ctx.stats["cg_profile.direct_calls"] == 2
    assert ctx.stats["cg_profile.internal_calls"] == 2
    assert ctx.stats["cg_profile.indirect_calls"] == 1
    assert ctx.stats["cg_profile.recursive_calls"] == 1


def test_rel_lookup_table_converter_records_pointer_table_candidate():
    ast = CParser().parse(
        """
        typedef int (*fn)(void);
        int a(void) { return 1; }
        int b(void) { return 2; }
        static fn table[] = { a, b };
        """
    )
    ctx = PassContext()

    RelLookupTableConverterPass().run(ast, ctx)

    assert ctx.stats["rel_lookup_table_converter.modules"] == 1
    assert ctx.stats["rel_lookup_table_converter.candidates"] == 1
    assert ctx.stats["rel_lookup_table_converter.entries"] == 2


@pytest.mark.parametrize(
    ("pass_factory", "stat_prefix"),
    [
        (CoroEarlyPass, "coro_early"),
        (CoroElidePass, "coro_elide"),
        (CoroSplitPass, "coro_split"),
        (CoroAnnotationElidePass, "coro_annotation_elide"),
        (CoroCleanupPass, "coro_cleanup"),
    ],
)
def test_coro_passes_record_coroutine_builtin_inventory(pass_factory, stat_prefix):
    ast = CParser().parse(
        """
        void *f(void) {
            __builtin_coro_id(0, 0, 0, 0);
            return __builtin_coro_resume(0);
        }
        """
    )
    ctx = PassContext()

    pass_factory().run(ast, ctx)

    assert ctx.stats[f"{stat_prefix}.modules"] == 1
    assert ctx.stats[f"{stat_prefix}.sites"] == 2


def test_openmp_passes_record_runtime_hook_inventory():
    code = """
        int f(void) {
            __kmpc_fork_call(0, 0, 0);
            return omp_get_max_threads();
        }
    """
    ast = CParser().parse(code)

    ctx = PassContext()
    OpenMPOptPass().run(ast, ctx)
    assert ctx.stats["openmp_opt.modules"] == 1
    assert ctx.stats["openmp_opt.runtime_calls"] == 2

    ctx = PassContext()
    OpenMPCGSCCPass().run(ast, ctx)
    assert ctx.stats["openmp_opt_cgscc.modules"] == 1
    assert ctx.stats["openmp_opt_cgscc.runtime_calls"] == 2


def test_ee_instrument_records_hook_inventory():
    ast = CParser().parse(
        """
        void f(void) {
            __cyg_profile_func_enter(0, 0);
            __asan_report_store4(0);
        }
        """
    )
    ctx = PassContext()

    EEInstrumentPass().run(ast, ctx)

    assert ctx.stats["ee_instrument.modules"] == 1
    assert ctx.stats["ee_instrument.hook_sites"] == 2


def test_loop_distribute_splits_independent_assignments_into_multiple_loops():
    ast = CParser().parse(
        """
        int main(void) {
            int sum = 0;
            int prod = 0;
            for (int i = 0; i < 3; ++i) {
                sum += i;
                prod += i + 1;
            }
            return sum + prod;
        }
        """
    )
    ctx = PassContext()

    transformed = LoopDistributePass().run(ast, ctx) or ast
    func = next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))
    loops = [item for item in func.body.block_items or () if isinstance(item, c_ast.For)]

    assert len(loops) == 2
    for loop in loops:
        assert isinstance(loop.stmt, c_ast.Compound)
        assert len(loop.stmt.block_items or ()) == 1


def test_loop_distribute_keeps_dependent_statements_in_single_loop():
    ast = CParser().parse(
        """
        int main(void) {
            int sum = 0;
            int prod = 0;
            for (int i = 0; i < 3; ++i) {
                sum += i;
                prod += sum;
            }
            return sum + prod;
        }
        """
    )
    ctx = PassContext()

    transformed = LoopDistributePass().run(ast, ctx) or ast
    func = next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))
    loops = [item for item in func.body.block_items or () if isinstance(item, c_ast.For)]

    assert len(loops) == 1


def test_loop_vectorize_records_simple_vector_store_candidate():
    ast = CParser().parse(
        """
        int main(void) {
            int a[4], b[4], out[4];
            for (int i = 0; i < 4; ++i) {
                out[i] = a[i] + b[i];
            }
            return out[0];
        }
        """
    )
    ctx = PassContext()

    LoopVectorizePass().run(ast, ctx)

    assert ctx.stats["loop_vectorize.modules"] == 1
    assert ctx.stats["loop_vectorize.candidates"] == 1
    assert ctx.stats["loop_vectorize.vector_stmts"] == 1


def test_loop_vectorize_skips_loop_with_control_flow_in_body():
    ast = CParser().parse(
        """
        int main(void) {
            int a[4], out[4];
            for (int i = 0; i < 4; ++i) {
                if (a[i])
                    out[i] = a[i];
            }
            return out[0];
        }
        """
    )
    ctx = PassContext()

    LoopVectorizePass().run(ast, ctx)

    assert ctx.stats["loop_vectorize.modules"] == 1
    assert ctx.stats["loop_vectorize.candidates"] == 0


def test_extra_simple_loop_unswitch_rewrites_single_if_loop_body():
    ast = CParser().parse(
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
        """
    )
    ctx = PassContext()

    transformed = ExtraSimpleLoopUnswitchPass().run(ast, ctx) or ast
    func = next(ext for ext in transformed.ext if isinstance(ext, c_ast.FuncDef))
    outer_if = next(item for item in func.body.block_items or () if isinstance(item, c_ast.If))

    assert isinstance(outer_if.iftrue, c_ast.For)
    assert isinstance(outer_if.iffalse, c_ast.For)


def test_libcalls_shrinkwrap_drops_zero_length_memset_statement():
    func = _transformed_function(
        """
        void *memset(void *dst, int c, unsigned long n);
        int main(void) {
            char buf[4];
            memset(buf, 0, 0);
            return 0;
        }
        """,
        LibcallsShrinkwrapPass(),
    )

    calls = [
        item for item in func.body.block_items or ()
        if isinstance(item, c_ast.FuncCall)
    ]
    assert not calls


def test_libcalls_shrinkwrap_folds_zero_length_memcmp_to_zero():
    func = _transformed_function(
        """
        int memcmp(int *a, int *b, int n);
        int f(int *a, int *b) {
            return memcmp(a, b, 0);
        }
        """,
        LibcallsShrinkwrapPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.Constant)
    assert ret.expr.value == "0"


def test_move_auto_init_merges_adjacent_decl_and_store():
    func = _transformed_function(
        """
        int f(int v) {
            int x;
            x = v + 1;
            return x;
        }
        """,
        MoveAutoInitPass(),
    )

    decl = func.body.block_items[0]
    assert isinstance(decl, c_ast.Decl)
    assert isinstance(decl.init, c_ast.BinaryOp)
    assert isinstance(func.body.block_items[1], c_ast.Return)


def test_move_auto_init_keeps_static_local_assignment_separate():
    func = _transformed_function(
        """
        int f(void) {
            static int x;
            x = 1;
            return x;
        }
        """,
        MoveAutoInitPass(),
    )

    assert isinstance(func.body.block_items[0], c_ast.Decl)
    assert isinstance(func.body.block_items[1], c_ast.Assignment)


def test_move_auto_init_keeps_decl_store_separate_in_switch_case_block():
    func = _transformed_function(
        """
        int f(int x) {
            switch (x) {
                case 1: {
                    int shared;
                    shared = 7;
                case 2:
                    return shared;
                }
                default:
                    return 0;
            }
        }
        """,
        MoveAutoInitPass(),
    )

    switch = next(item for item in func.body.block_items or () if isinstance(item, c_ast.Switch))
    first_case = switch.stmt.block_items[0]
    assert isinstance(first_case, c_ast.Case)
    case_block = first_case.stmts[0]
    assert isinstance(case_block, c_ast.Compound)
    assert isinstance(case_block.block_items[0], c_ast.Decl)
    assert getattr(case_block.block_items[0], "init", None) is None
    assert isinstance(case_block.block_items[1], c_ast.Assignment)


def test_chr_factors_identical_trailing_return_out_of_if_else():
    func = _transformed_function(
        """
        int f(int c) {
            if (c) {
                c = c + 1;
                return c * 2;
            } else {
                c = c + 2;
                return c * 2;
            }
        }
        """,
        CHRPass(),
    )

    first = func.body.block_items[0]
    assert isinstance(first, c_ast.If)
    assert isinstance(func.body.block_items[1], c_ast.Return)


def test_chr_keeps_branch_with_break_untouched():
    func = _transformed_function(
        """
        int f(int c) {
            while (1) {
                if (c) {
                    break;
                    return 1;
                } else {
                    c = 3;
                    return 1;
                }
            }
            return 0;
        }
        """,
        CHRPass(),
    )

    loop = func.body.block_items[0]
    assert isinstance(loop, c_ast.While)
    inner_if = loop.stmt.block_items[0]
    assert isinstance(inner_if, c_ast.If)
    assert isinstance(inner_if.iftrue, c_ast.Compound)
    assert isinstance(inner_if.iftrue.block_items[-1], c_ast.Return)


def test_chr_keeps_common_tail_that_uses_branch_local_name_in_scope():
    func = _transformed_function(
        """
        int f(int c) {
            if (c) {
                int k = c + 1;
                return k;
            } else {
                int k = c + 2;
                return k;
            }
        }
        """,
        CHRPass(),
    )

    assert len(func.body.block_items or ()) == 1
    inner_if = func.body.block_items[0]
    assert isinstance(inner_if, c_ast.If)
    assert isinstance(inner_if.iftrue, c_ast.Compound)
    assert isinstance(inner_if.iffalse, c_ast.Compound)
    assert isinstance(inner_if.iftrue.block_items[-1], c_ast.Return)
    assert isinstance(inner_if.iffalse.block_items[-1], c_ast.Return)


def test_constmerge_rewrites_duplicate_static_const_scalar_uses():
    ast = _transformed_ast(
        """
        static const int alpha = 7;
        static const int beta = 7;
        int f(void) { return beta; }
        """,
        ConstMergePass(),
    )

    decls = [
        ext for ext in ast.ext
        if isinstance(ext, c_ast.Decl) and ext.name in {"alpha", "beta"}
    ]
    assert [decl.name for decl in decls] == ["alpha"]
    func = next(ext for ext in ast.ext if isinstance(ext, c_ast.FuncDef))
    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.ID)
    assert ret.expr.name == "alpha"


def test_constmerge_keeps_duplicate_when_address_is_taken():
    ast = _transformed_ast(
        """
        static const int alpha = 7;
        static const int beta = 7;
        static const int *ptr = &beta;
        int f(void) { return *ptr; }
        """,
        ConstMergePass(),
    )

    decls = [
        ext for ext in ast.ext
        if isinstance(ext, c_ast.Decl) and ext.name in {"alpha", "beta"}
    ]
    assert {decl.name for decl in decls} == {"alpha", "beta"}


def test_div_rem_pairs_rewrites_adjacent_remainder_to_use_quotient():
    func = _transformed_function(
        """
        int f(int a, int b) {
            int q = a / b;
            int r = a % b;
            return q + r;
        }
        """,
        DivRemPairsPass(),
    )

    q_decl = func.body.block_items[0]
    r_decl = func.body.block_items[1]
    assert isinstance(q_decl, c_ast.Decl)
    assert isinstance(r_decl, c_ast.Decl)
    assert isinstance(r_decl.init, c_ast.BinaryOp)
    assert r_decl.init.op == "-"
    assert isinstance(r_decl.init.right, c_ast.BinaryOp)
    assert r_decl.init.right.op == "*"
    assert isinstance(r_decl.init.right.left, c_ast.ID)
    assert r_decl.init.right.left.name == "q"


def test_div_rem_pairs_keeps_narrow_char_targets_unmodified():
    func = _transformed_function(
        """
        int f(char a, char b) {
            char q = a / b;
            char r = a % b;
            return q + r;
        }
        """,
        DivRemPairsPass(),
    )

    r_decl = func.body.block_items[1]
    assert isinstance(r_decl, c_ast.Decl)
    assert isinstance(r_decl.init, c_ast.BinaryOp)
    assert r_decl.init.op == "%"


def test_inject_tli_mappings_renames_builtin_strlen():
    func = _transformed_function(
        """
        int strlen(const char *);
        int f(void) {
            return __builtin_strlen("ok");
        }
        """,
        InjectTLIMappingsPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "strlen"


def test_inject_tli_mappings_lowers_builtin_bzero_to_memset():
    func = _transformed_function(
        """
        void *memset(void *, int, int);
        int f(char *p, int n) {
            __builtin_bzero(p, n);
            return 0;
        }
        """,
        InjectTLIMappingsPass(),
    )

    call = func.body.block_items[0]
    assert isinstance(call, c_ast.FuncCall)
    assert isinstance(call.name, c_ast.ID)
    assert call.name.name == "memset"
    args = list(call.args.exprs or ())
    assert len(args) == 3
    assert isinstance(args[1], c_ast.Constant)
    assert args[1].value == "0"


def test_inject_tli_mappings_truncates_builtin_memcpy_chk_args():
    func = _transformed_function(
        """
        void *memcpy(void *, const void *, int);
        int f(char *a, char *b) {
            __builtin___memcpy_chk(a, b, 4, 99);
            return 0;
        }
        """,
        InjectTLIMappingsPass(),
    )

    call = func.body.block_items[0]
    assert isinstance(call, c_ast.FuncCall)
    assert isinstance(call.name, c_ast.ID)
    assert call.name.name == "memcpy"
    assert len(call.args.exprs or ()) == 3


def test_inject_tli_mappings_keeps_builtin_chk_when_arglist_too_short():
    func = _transformed_function(
        """
        void *f(void *a, void *b) {
            return __builtin___memcpy_chk(a, b);
        }
        """,
        InjectTLIMappingsPass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.FuncCall)
    assert isinstance(ret.expr.name, c_ast.ID)
    assert ret.expr.name.name == "__builtin___memcpy_chk"


def test_vector_combine_merges_nested_bitwise_and_constants():
    func = _transformed_function(
        """
        int f(int x) {
            return (x & 12) & 10;
        }
        """,
        VectorCombinePass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.BinaryOp)
    assert ret.expr.op == "&"
    assert isinstance(ret.expr.left, c_ast.ID)
    assert ret.expr.left.name == "x"
    assert isinstance(ret.expr.right, c_ast.Constant)
    assert ret.expr.right.value == "8"


def test_vector_combine_keeps_nonconstant_nested_mask():
    func = _transformed_function(
        """
        int f(int x, int y) {
            return (x & y) & 10;
        }
        """,
        VectorCombinePass(),
    )

    ret = func.body.block_items[0]
    assert isinstance(ret, c_ast.Return)
    assert isinstance(ret.expr, c_ast.BinaryOp)
    assert isinstance(ret.expr.left, c_ast.BinaryOp)
    assert ret.expr.left.op == "&"


def test_slp_vectorizer_records_isomorphic_assignment_group():
    ast = CParser().parse(
        """
        int f(int a0, int a1, int b0, int b1) {
            int x0, x1;
            x0 = a0 + b0;
            x1 = a1 + b1;
            return x0 + x1;
        }
        """
    )
    ctx = PassContext()

    SLPVectorizerPass().run(ast, ctx)

    assert ctx.stats["slp_vectorizer.modules"] == 1
    assert ctx.stats["slp_vectorizer.groups"] == 1
    assert ctx.stats["slp_vectorizer.lanes"] == 2


def test_slp_vectorizer_skips_mixed_operator_sequence():
    ast = CParser().parse(
        """
        int f(int a0, int a1, int b0, int b1) {
            int x0, x1;
            x0 = a0 + b0;
            x1 = a1 - b1;
            return x0 + x1;
        }
        """
    )
    ctx = PassContext()

    SLPVectorizerPass().run(ast, ctx)

    assert ctx.stats["slp_vectorizer.modules"] == 1
    assert ctx.stats["slp_vectorizer.groups"] == 0
