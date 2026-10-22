from pcc.frontends.c.passes import PassContext, PassPipeline
from pcc.frontends.c.passes.context import AllocStrategy
from pcc.frontends.c.passes.ir_metadata import (
    AlignPass,
    FuncAttrPass,
    LoopMetadataPass,
    NoaliasPass,
    RangeMetadataPass,
)
from pcc.frontends.c.passes.propagation import SROAPass
from pcc.frontends.c.parse.c_parser import CParser


def test_align_pass_adds_natural_alignment_to_plain_load_and_store():
    ir_text = """
define i32 @main(i32* %p) {
entry:
  %x = load i32, i32* %p
  store i32 %x, i32* %p
  ret i32 %x
}
""".strip()

    out = AlignPass().run(ir_text, PassContext())

    assert "load i32, i32* %p, align 4" in out
    assert "store i32 %x, i32* %p, align 4" in out


def test_noalias_pass_annotates_restrict_parameter():
    ir_text = """
define i32 @"sum"(i32* %"p") {
entry:
  %x = load i32, i32* %"p"
  ret i32 %x
}
""".strip()
    ctx = PassContext()
    ctx.get_func("sum").restrict_params.add("p")

    out = NoaliasPass().run(ir_text, ctx)

    assert 'define i32 @"sum"(noalias i32* %"p")' in out


def test_loop_metadata_pass_records_branch_opportunities():
    ir_text = """
define i32 @main(i1 %cond) {
entry:
  br i1 %cond, label %loop, label %exit
loop:
  br label %loop
exit:
  ret i32 0
}
""".strip()
    ctx = PassContext()

    out = LoopMetadataPass().run(ir_text, ctx)

    assert out == ir_text
    assert ctx.stats["loop_metadata.branches"] == 2


def test_range_metadata_pass_records_known_range_variables():
    ctx = PassContext()
    var = ctx.get_var("main", "x")
    var.range_min = 0
    var.range_max = 10

    out = RangeMetadataPass().run("define i32 @main() { ret i32 0 }", ctx)

    assert out == "define i32 @main() { ret i32 0 }"
    assert ctx.stats["range_metadata.opportunities"] == 1


def test_func_attr_pass_adds_conservative_leaf_attributes():
    ir_text = """
define i32 @"leaf"(i32 %".1") {
entry:
  ret i32 %".1"
}
""".strip()
    ctx = PassContext()
    info = ctx.get_func("leaf")
    info.is_leaf = True
    info.has_setjmp = False
    info.has_goto = False
    info.max_loop_depth = 0

    out = FuncAttrPass().run(ir_text, ctx)

    assert "nounwind" in out
    assert "nofree" in out
    assert "willreturn" in out


def test_func_attr_pass_skips_non_leaf_function():
    ir_text = """
define i32 @"caller"(i32 %".1") {
entry:
  %".2" = call i32 @"callee"(i32 %".1")
  ret i32 %".2"
}
""".strip()
    ctx = PassContext()
    info = ctx.get_func("caller")
    info.is_leaf = False

    out = FuncAttrPass().run(ir_text, ctx)

    assert "nounwind" not in out
    assert "nofree" not in out
    assert "willreturn" not in out


def test_sroa_marks_struct_field_only_local_as_candidate():
    ast = CParser().parse(
        """
        struct S { int x; int y; };
        int main(void) {
            struct S s;
            s.x = 1;
            s.y = 2;
            return s.x + s.y;
        }
        """
    )
    ctx = PassContext()
    ctx.get_var("main", "s").escapes = False

    SROAPass().run(ast, ctx)

    assert ctx.stats["sroa.candidates"] == 1


def test_sroa_skips_struct_used_as_whole_object():
    ast = CParser().parse(
        """
        struct S { int x; int y; };
        int consume(struct S s);
        int main(void) {
            struct S s;
            s.x = 1;
            return consume(s);
        }
        """
    )
    ctx = PassContext()
    ctx.get_var("main", "s").escapes = False

    SROAPass().run(ast, ctx)

    assert "sroa.candidates" not in ctx.stats


def test_alloc_decision_promotes_scalar_single_def_to_ssa_for_mem2reg_style_pipeline():
    ast = CParser().parse(
        """
        int main(void) {
            int x = 5;
            return x;
        }
        """
    )
    ctx = PassContext()
    pipeline = PassPipeline.minimal()

    pipeline.run_high_tier(ast, ctx)

    assert ctx.get_var("main", "x").alloc_strategy == AllocStrategy.SSA
