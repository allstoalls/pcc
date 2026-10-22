"""Owned C pass mappings, AST/SSA rewrites and explicit capability gaps.

LLVM names/source anchors label historical reference algorithms; this corpus
executes only pcc source implementations and never an external optimizer.
"""

import re

import pytest

from pcc.frontends.c.evaluator.c_evaluator import (
    _compile_preprocessed_translation_unit_artifact,
)
from pcc.frontends.c.passes.llvm_builtin_registry import LLVM_DEFAULT_PROFILE_PASSES
from pcc.frontends.c.passes import (
    PassPipeline,
    expand_registered_pass_name,
    llvm_python_translation,
    unique_default_pass_names,
    unique_managed_pass_names,
)


def _assert_owned_source_mapping(alias):
    entry = llvm_python_translation(alias)
    owned_names = set(unique_default_pass_names())
    if entry is None:
        # A direct pcc pass (DCE) need not have a reference-algorithm alias.
        assert alias in owned_names, alias
        assert expand_registered_pass_name(alias) == (alias,)
        return None
    assert entry.status == "deprecated-source-approximation", alias
    assert entry.implementation_tier == "ast", alias
    assert entry.ir_pass_class is None, alias
    assert entry.python_passes, alias
    assert set(entry.python_passes) <= owned_names, alias
    return entry


def _ir_needle_count(ir_text: str, needle: str) -> int:
    if needle == 'call i32 @"wrap"':
        pattern = re.compile(
            r'\b(?:tail\s+)?call\s+i32(?:\s+\([^@]*\))?\s+@(?:"wrap"|wrap)\('
        )
        return len(pattern.findall(ir_text))
    return ir_text.count(needle)


def _defined_function_ir(ir_text: str, name: str) -> str:
    pattern = re.compile(
        rf'^define\b[^\n]*@(?:"{re.escape(name)}"|{re.escape(name)})\([^\n]*\).*?^\}}$',
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(ir_text)
    assert match is not None, f"missing IR definition for {name}"
    return match.group(0)


def _pipeline_without_high_passes(*pass_names):
    pipeline = PassPipeline.default()
    disabled = set(pass_names)
    pipeline.high_tier = [
        pass_ for pass_ in pipeline.high_tier if pass_.name not in disabled
    ]
    return pipeline


@pytest.mark.parametrize(
    "alias",
    [
        'instsimplify',
        'dce',
        'adce',
        'sccp',
        'gvn',
        'reassociate',
        'dse',
        'early-cse',
        'globaldce',
        'elim-avail-extern',
        'annotation-remarks',
        'cg-profile',
        'ee-instrument',
        'coro-annotation-elide',
        'coro-cleanup',
        'coro-early',
        'coro-elide',
        'coro-split',
        'simplifycfg',
        'bdce',
        'instcombine',
        'callsite-splitting',
        'loop-rotate',
        'loop-simplifycfg',
        'loop-instsimplify',
        'extra-simple-loop-unswitch-passes',
        'loop-sink',
        'lower-expect',
        'inferattrs',
        'libcalls-shrinkwrap',
        'require',
        'invalidate',
        'verify',
        'openmp-opt',
        'openmp-opt-cgscc',
        'rel-lookup-table-converter',
        'transform-warning',
        'lower-constant-intrinsics',
        'infer-alignment',
        'annotation2metadata',
        'forceattrs',
        'inject-tli-mappings',
        'recompute-globalsaa',
        'aggressive-instcombine',
        'alignment-from-assumptions',
        'chr',
        'float2int',
        'loop-idiom',
        'memcpyopt',
        'move-auto-init',
        'speculative-execution',
        'tailcallelim',
    ],
)
def test_registry_does_not_claim_retired_ir_backings(alias):
    _assert_owned_source_mapping(alias)


def test_c_pass_registry_retains_source_mapping_and_reference_anchor():
    entry = _assert_owned_source_mapping("aggressive-instcombine")

    assert entry.python_passes == ("canonicalize", "expr-reassociation", "copy-propagation")
    assert entry.upstream_sources == (
        "llvm/lib/Transforms/AggressiveInstCombine/AggressiveInstCombine.cpp",
    )


@pytest.mark.parametrize(
    "llvm_name",
    [
        "aggressive-instcombine",
        "adce",
        "alignment-from-assumptions",
        "argpromotion",
        "annotation-remarks",
        "bdce",
        "cg-profile",
        "called-value-propagation",
        "callsite-splitting",
        "chr",
        "constraint-elimination",
        "constmerge",
        "correlated-propagation",
        "coro-annotation-elide",
        "coro-cleanup",
        "coro-early",
        "coro-elide",
        "coro-split",
        "deadargelim",
        "div-rem-pairs",
        "dse",
        "early-cse",
        "ee-instrument",
        "elim-avail-extern",
        "infer-alignment",
        "extra-simple-loop-unswitch-passes",
        "float2int",
        "forceattrs",
        "function-attrs",
        "globaldce",
        "globalopt",
        "gvn",
        "inferattrs",
        "invalidate",
        "inject-tli-mappings",
        "inline",
        "indvars",
        "instcombine",
        "instsimplify",
        "jump-threading",
        "libcalls-shrinkwrap",
        "licm",
        "loop-deletion",
        "loop-distribute",
        "loop-idiom",
        "loop-instsimplify",
        "loop-load-elim",
        "loop-rotate",
        "loop-sink",
        "loop-simplifycfg",
        "loop-unroll",
        "loop-unroll-full",
        "loop-vectorize",
        "lower-expect",
        "lower-constant-intrinsics",
        "mem2reg",
        "memcpyopt",
        "mldst-motion",
        "move-auto-init",
        "newgvn",
        "openmp-opt",
        "openmp-opt-cgscc",
        "recompute-globalsaa",
        "reassociate",
        "rel-lookup-table-converter",
        "require",
        "rpo-function-attrs",
        "sccp",
        "simplifycfg",
        "simple-loop-unswitch",
        "slp-vectorizer",
        "sroa",
        "speculative-execution",
        "always-inline",
        "tailcallelim",
        "transform-warning",
        "vector-combine",
        "verify",
        "annotation2metadata",
    ],
)
def test_selected_aliases_have_owned_source_approximations(llvm_name):
    _assert_owned_source_mapping(llvm_name)


def test_registered_pass_name_expands_to_owned_passes_and_alias_spelling():
    assert expand_registered_pass_name("function-attrs") == (
        "func-attr",
        "function-attrs",
    )
    assert expand_registered_pass_name("rpo-function-attrs") == (
        "func-attr",
        "rpo-function-attrs",
    )
    assert expand_registered_pass_name("tailcallelim") == (
        "tail-call",
        "tailcallelim",
    )
    assert expand_registered_pass_name("lower-expect") == ("lower-expect",)
    assert expand_registered_pass_name("globaldce") == (
        "global-dce",
        "globaldce",
    )
    assert expand_registered_pass_name("verify") == ("verify",)
    assert expand_registered_pass_name("lower-constant-intrinsics") == (
        "lower-constant-intrinsics",
    )
    assert expand_registered_pass_name("alignment-from-assumptions") == (
        "alignment-from-assumptions",
    )
    assert expand_registered_pass_name("forceattrs") == (
        "func-attr",
        "forceattrs",
    )
    assert expand_registered_pass_name("libcalls-shrinkwrap") == (
        "libcalls-shrinkwrap",
    )
    assert expand_registered_pass_name("instsimplify") == (
        "canonicalize",
        "instsimplify",
    )
    assert expand_registered_pass_name("reassociate") == (
        "expr-reassociation",
        "reassociate",
    )


@pytest.mark.parametrize(
    "alias",
    [
        'rpo-function-attrs',
        'callsite-splitting',
        'loop-rotate',
        'loop-simplifycfg',
        'loop-instsimplify',
        'loop-sink',
        'elim-avail-extern',
        'tailcallelim',
        'lower-expect',
        'inferattrs',
        'libcalls-shrinkwrap',
        'annotation-remarks',
        'cg-profile',
        'ee-instrument',
        'coro-annotation-elide',
        'coro-cleanup',
        'coro-early',
        'coro-elide',
        'coro-split',
        'lower-constant-intrinsics',
        'require',
        'invalidate',
        'verify',
        'openmp-opt',
        'openmp-opt-cgscc',
        'rel-lookup-table-converter',
        'extra-simple-loop-unswitch-passes',
        'simple-loop-unswitch',
        'loop-distribute',
        'loop-vectorize',
        'slp-vectorizer',
        'transform-warning',
        'aggressive-instcombine',
        'alignment-from-assumptions',
        'annotation2metadata',
        'chr',
        'float2int',
        'forceattrs',
        'infer-alignment',
        'inject-tli-mappings',
        'loop-idiom',
        'memcpyopt',
        'move-auto-init',
        'recompute-globalsaa',
        'speculative-execution',
    ],
)
def test_registered_aliases_report_owned_ast_availability(alias):
    _assert_owned_source_mapping(alias)


def test_unimplemented_loop_simplify_alias_is_rejected():
    from pcc.driver.cli_core import _pass_env_overrides

    assert llvm_python_translation("loop-simplify") is None
    assert "loop-simplify" not in unique_managed_pass_names(opt_level=0)
    with pytest.raises(ValueError, match="unknown pass name.*loop-simplify"):
        _pass_env_overrides(0, enabled_passes=("loop-simplify",))


@pytest.mark.parametrize(
    ("llvm_name", "expected"),
    [
        (
            "aggressive-instcombine",
            (
                "canonicalize",
                "expr-reassociation",
                "copy-propagation",
                "aggressive-instcombine",
            ),
        ),
        ("adce", ("ssa-adce", "dce", "adce")),
        ("argpromotion", ("inline-opt", "sroa", "alloc-decision", "argpromotion")),
        ("bdce", ("ssa-adce", "dce", "bdce")),
        (
            "called-value-propagation",
            ("inline-opt", "copy-propagation", "called-value-propagation"),
        ),
        ("callsite-splitting", ("inline-opt", "control-flow", "callsite-splitting")),
        ("constraint-elimination", ("control-flow", "canonicalize", "constraint-elimination")),
        ("correlated-propagation", ("control-flow", "canonicalize", "dce", "correlated-propagation")),
        ("deadargelim", ("deadargelim-analysis", "inline-opt", "dce", "deadargelim")),
        ("instcombine", ("canonicalize", "expr-reassociation", "copy-propagation", "instcombine")),
        ("early-cse", ("local-value-numbering", "copy-propagation", "early-cse")),
        ("dse", ("ssa-dse", "ssa-adce", "dce", "memory-opt-ir", "dse")),
        ("elim-avail-extern", ("elim-avail-extern-src", "elim-avail-extern")),
        ("globalopt", ("canonicalize", "dce", "inline-opt", "globalopt")),
        (
            "gvn",
            (
                "ssa-gvn",
                "ssa-gvn-rewrite",
                "gvn",
                "local-value-numbering",
                "copy-propagation",
            ),
        ),
        ("jump-threading", ("control-flow", "jump-threading")),
        ("instsimplify", ("canonicalize", "instsimplify")),
        ("indvars", ("indvars",)),
        ("loop-idiom", ("loop-opt", "loop-idiom")),
        ("loop-instsimplify", ("loop-opt", "canonicalize", "loop-instsimplify")),
        ("loop-rotate", ("loop-rotate",)),
        ("loop-load-elim", ("memory-opt-ir", "loop-load-elim")),
        ("loop-sink", ("loop-opt", "loop-sink")),
        ("loop-simplifycfg", ("control-flow", "dce", "loop-simplifycfg")),
        ("infer-alignment", ("align", "infer-alignment")),
        ("rpo-function-attrs", ("func-attr", "rpo-function-attrs")),
        ("forceattrs", ("func-attr", "forceattrs")),
        ("licm", ("licm",)),
        ("simple-loop-unswitch", ("simple-loop-unswitch",)),
        ("loop-deletion", ("loop-deletion",)),
        ("loop-unroll", ("loop-unroll",)),
        ("loop-unroll-full", ("loop-unroll-full",)),
        ("inline", ("inline-opt", "inline")),
        ("always-inline", ("inline-opt", "always-inline")),
        ("lower-expect", ("lower-expect",)),
        ("mem2reg", ("alloc-decision", "sroa", "mem2reg")),
        ("memcpyopt", ("memory-opt-ir", "memcpyopt")),
        ("mldst-motion", ("memory-opt-ir", "mldst-motion")),
        (
            "newgvn",
            (
                "ssa-gvn",
                "ssa-gvn-rewrite",
                "gvn",
                "local-value-numbering",
                "copy-propagation",
                "newgvn",
            ),
        ),
        ("ipsccp", ("canonicalize", "dce", "inline-opt", "ipsccp")),
        ("globaldce", ("global-dce", "globaldce")),
        ("libcalls-shrinkwrap", ("libcalls-shrinkwrap",)),
        ("openmp-opt", ("openmp-opt",)),
        ("float2int", ("float2int",)),
        ("div-rem-pairs", ("div-rem-pairs",)),
        ("constmerge", ("constmerge",)),
        ("reassociate", ("expr-reassociation", "reassociate")),
        (
            "sccp",
            (
                "ssa-sccp",
                "ssa-sccp-rewrite",
                "ssa-branch-prune",
                "canonicalize",
                "dce",
                "sccp",
            ),
        ),
        ("simplifycfg", ("canonicalize", "control-flow", "dce", "simplifycfg")),
        ("slp-vectorizer", ("slp-vectorizer",)),
        ("sroa", ("sroa",)),
        ("speculative-execution", ("control-flow", "canonicalize", "speculative-execution")),
    ],
)
def test_more_registered_alias_names_expand_to_owned_c_passes(
    llvm_name, expected
):
    assert expand_registered_pass_name(llvm_name) == expected


def test_unique_managed_pass_names_exposes_owned_c_aliases_at_o0():
    names = unique_managed_pass_names(opt_level=0, include_llvm=False)

    assert "function-attrs" in names
    assert "tailcallelim" in names
    assert "reassociate" in names
    assert "aggressive-instcombine" in names
    assert "adce" in names
    assert "argpromotion" in names
    assert "bdce" in names
    assert "called-value-propagation" in names
    assert "callsite-splitting" in names
    assert "constraint-elimination" in names
    assert "correlated-propagation" in names
    assert "deadargelim" in names
    assert "early-cse" in names
    assert "elim-avail-extern" in names
    assert "instcombine" in names
    assert "dse" in names
    assert "globalopt" in names
    assert "inline" in names
    assert "ipsccp" in names
    assert "jump-threading" in names
    assert "mem2reg" in names
    assert "memcpyopt" in names
    assert "mldst-motion" in names
    assert "globaldce" in names
    assert "loop-idiom" in names
    assert "loop-instsimplify" in names
    assert "loop-load-elim" in names
    assert "loop-sink" in names
    assert "loop-simplifycfg" in names
    assert "lower-expect" in names
    assert "lower-constant-intrinsics" in names
    assert "alignment-from-assumptions" in names
    assert "verify" in names
    assert "openmp-opt" in names
    assert "div-rem-pairs" in names
    assert "constmerge" in names
    assert "instsimplify" in names
    assert "sccp" in names
    assert "simplifycfg" in names
    assert "sroa" in names
    assert "speculative-execution" in names


def test_reference_profile_aliases_have_explicit_c_source_registrations():
    defaults = []
    for opt_level in sorted(LLVM_DEFAULT_PROFILE_PASSES):
        for pass_name in LLVM_DEFAULT_PROFILE_PASSES[opt_level]:
            if pass_name not in defaults:
                defaults.append(pass_name)

    missing = [
        pass_name
        for pass_name in defaults
        if llvm_python_translation(pass_name).python_passes == ()
    ]

    assert missing == []


def test_registered_disable_alias_can_turn_off_python_function_attrs(monkeypatch):
    code = "int leaf(int x){return x+1;} int main(void){return leaf(1);}"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert "nounwind" in default_artifact["ir_text"]
    assert "nofree" in default_artifact["ir_text"]
    assert "willreturn" in default_artifact["ir_text"]

    monkeypatch.setenv("PCC_DISABLE_PASSES", "function-attrs")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "nounwind" not in disabled_artifact["ir_text"]
    assert "nofree" not in disabled_artifact["ir_text"]
    assert "willreturn" not in disabled_artifact["ir_text"]
    metric = disabled_artifact["pass_report"]["passes"]["func-attr"]
    assert metric["skips"] >= 1


def test_registered_disable_alias_can_turn_off_python_forceattrs(monkeypatch):
    code = "int leaf(int x){return x+1;} int main(void){return leaf(1);}"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert "nounwind" in default_artifact["ir_text"]
    assert "nofree" in default_artifact["ir_text"]
    assert "willreturn" in default_artifact["ir_text"]

    monkeypatch.setenv("PCC_DISABLE_PASSES", "forceattrs")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "nounwind" not in disabled_artifact["ir_text"]
    assert "nofree" not in disabled_artifact["ir_text"]
    assert "willreturn" not in disabled_artifact["ir_text"]
    metric = disabled_artifact["pass_report"]["passes"]["func-attr"]
    assert metric["skips"] >= 1


def test_registered_disable_alias_can_turn_off_python_tail_call_marking(monkeypatch):
    code = "int leaf(int x){return x+1;} int main(void){return leaf(1);}"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert "tail call" in default_artifact["ir_text"]

    monkeypatch.setenv("PCC_DISABLE_PASSES", "tailcallelim")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "tail call" not in disabled_artifact["ir_text"]
    metric = disabled_artifact["pass_report"]["passes"]["tail-call"]
    assert metric["skips"] >= 1


@pytest.mark.parametrize(
    ("alias", "code", "needle", "default_count", "disabled_count", "metric_names"),
    [
        (
            "aggressive-instcombine",
            "int f(int x){ return (x + 0) + (2 + 3); }",
            " add ",
            1,
            3,
            ("canonicalize", "expr-reassociation", "copy-propagation"),
        ),
        (
            "adce",
            "int f(int a){ int x = a + 1; return a; }",
            " add ",
            0,
            1,
            ("ssa-adce", "dce"),
        ),
        (
            "argpromotion",
            (
                "int leaf(int x){ return x + 1; } "
                "int wrap(int x){ return leaf(x); } "
                "int f(void){ struct S { int a; int b; }; struct S s; "
                "s.a=7; s.b=8; int y=s.a; return wrap(y); }"
            ),
            'call i32 @"wrap"',
            0,
            1,
            ("inline-opt", "sroa", "alloc-decision"),
        ),
        (
            "bdce",
            "int f(int a){ int x = a + 1; return a; }",
            " add ",
            0,
            1,
            ("ssa-adce", "dce"),
        ),
        (
            "called-value-propagation",
            (
                "int id(int x){ return x; } "
                "int wrap(int x){ return id(x); } "
                "int f(void){ int y=7; int z=y; return wrap(z); }"
            ),
            'call i32 @"wrap"',
            0,
            1,
            ("inline-opt", "copy-propagation"),
        ),
        (
            "callsite-splitting",
            (
                "int id(int x){ return x; } "
                "int wrap(int x){ return id(x); } "
                "int f(int c,int x){ if (c) return wrap(x); return wrap(x + 1); }"
            ),
            'call i32 @"wrap"',
            0,
            2,
            ("inline-opt", "control-flow"),
        ),
        (
            "instcombine",
            "int f(int x){ return (x + 0) + (2 + 3); }",
            " add ",
            1,
            3,
            ("canonicalize", "expr-reassociation", "copy-propagation"),
        ),
        (
            "dse",
            "int f(int a){ int x=0; x=a+1; x=a+2; return x; }",
            "store ",
            2,
            4,
            ("ssa-dse", "ssa-adce", "dce", "memory-opt-ir"),
        ),
            (
                "globalopt",
                (
                    "static int id(int x){ return x; } "
                    "static int wrap(int x){ return id(x); } "
                    "int f(void){ int y=7; int z=y; return wrap(z); }"
                ),
                "alloca",
                1,
                3,
                ("canonicalize", "dce", "inline-opt"),
            ),
        (
            "ipsccp",
            (
                "int id(int x){ return x; } "
                "int wrap(int x){ return id(x); } "
                "int f(void){ return wrap(7); }"
            ),
            'call i32 @"wrap"',
            0,
            1,
            ("canonicalize", "dce", "inline-opt"),
        ),
        (
            "loop-instsimplify",
            "int f(int n){ int sum=0; for(int i=0;i<n;i++){ sum += i - 0; } return sum; }",
            " sub ",
            0,
            1,
            ("loop-opt", "canonicalize"),
        ),
        (
            "gvn",
            "int f(int x,int y){ int a=x+y; int b=x+y; return a==b; }",
            " add ",
            1,
            2,
            (
                "ssa-gvn",
                "ssa-gvn-rewrite",
                "gvn",
                "local-value-numbering",
                "copy-propagation",
            ),
        ),
        (
            "early-cse",
            "int f(int x,int y){ int a=x+y; int b=x+y; return a==b; }",
            " add ",
            1,
            2,
            ("local-value-numbering", "copy-propagation"),
        ),
        (
            "sccp",
            "int f(void){ int x = 4 - 4; if (x) return 7; return 9; }",
            " br i1 ",
            0,
            1,
            ("ssa-sccp", "ssa-sccp-rewrite", "ssa-branch-prune", "canonicalize", "dce"),
        ),
    ],
)
def test_registered_disable_alias_can_turn_off_selected_python_translations(
    monkeypatch, alias, code, needle, default_count, disabled_count, metric_names
):
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert _ir_needle_count(default_artifact["ir_text"], needle) == default_count

    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert _ir_needle_count(disabled_artifact["ir_text"], needle) == disabled_count
    for metric_name in metric_names:
        metric = disabled_artifact["pass_report"]["passes"][metric_name]
        assert metric["skips"] >= 1


def test_sccp_alias_controls_ssa_branch_prune_on_join_proven_constant(monkeypatch):
    code = """
    int f(int c) {
        int x = 0;
        int y = 0;
        if (c) {
            x = 1;
        } else {
            x = 1;
        }
        if (x) {
            y = 7;
            y = y + 1;
        } else {
            y = 9;
            y = y + 1;
        }
        return y;
    }
    """

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["ir_text"].count(" br i1 ") <= 1
    assert default_artifact["pass_report"]["stats"]["ssa_branch_prune.fold_true"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "sccp")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["ir_text"].count(" br i1 ") == 1
    assert "ssa_branch_prune.fold_true" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["ssa-sccp"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["ssa-sccp-rewrite"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["ssa-branch-prune"]["skips"] >= 1


def test_sccp_alias_controls_ssa_sccp_rewrite_on_join_constant_return(monkeypatch):
    code = """
    int f(int c) {
        int x = 0;
        if (c) {
            x = 7;
        } else {
            x = 7;
        }
        return x;
    }
    """

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    # Direct SSA lowering can now return the merged constant even without the
    # AST rewrite, so the alias-control check must key off pass stats, not
    # a specific final IR spelling.
    assert default_artifact["pass_report"]["stats"]["ssa_sccp_rewrite.rewrite_return"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "sccp")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "ssa_sccp_rewrite.rewrite_return" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["ssa-sccp"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["ssa-sccp-rewrite"]["skips"] >= 1


def test_gvn_alias_controls_ssa_gvn_rewrite_on_cross_block_return(monkeypatch):
    code = """
    int f(int a, int b, int flag) {
        int x = a + b;
        if (flag) {
            return a + b;
        }
        return x;
    }
    """

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    # Direct SSA lowering now bypasses the source rewrite when emitting the
    # final IR for this shape, so the stable signal here is that the rewrite
    # pass ran and recorded work.
    assert default_artifact["pass_report"]["stats"]["ssa_gvn_rewrite.rewrite_return"] >= 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "gvn")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "ssa_gvn_rewrite.rewrite_return" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["ssa-gvn"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["ssa-gvn-rewrite"]["skips"] >= 1


def test_adce_alias_controls_ssa_adce_on_dead_initializer_before_overwrite(
    monkeypatch,
):
    code = """
    int f(int a) {
        int x = a + 1;
        x = 0;
        return x;
    }
    """

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["pass_report"]["stats"]["ssa_adce.drop_init"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "adce")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "ssa_adce.drop_init" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["ssa-adce"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["dce"]["skips"] >= 1


def test_dse_alias_controls_ssa_dse_on_dead_effectful_assignment(monkeypatch):
    code = """
    int side_effect(void);
    int f(int a) {
        int x;
        x = side_effect();
        x = a + 2;
        return x;
    }
    """

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["pass_report"]["stats"]["ssa_dse.preserve_effect_assign"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "dse")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "ssa_dse.preserve_effect_assign" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["ssa-dse"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["ssa-adce"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["dce"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["memory-opt-ir"]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_argpromotion_boundary_analysis(
    monkeypatch,
):
    code = (
        "int leaf(int x){ return x + 1; } "
        "int wrap(int x){ return leaf(x); } "
        "int f(void){ struct S { int a; int b; }; struct S s; "
        "s.a=7; s.b=8; int y=s.a; return wrap(y); }"
    )

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["stats"]["sroa.candidates"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "argpromotion")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "sroa.candidates" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["inline-opt"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["sroa"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["alloc-decision"]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_deadargelim_boundary_analysis(
    monkeypatch,
):
    code = (
        "int callee(int x){ return x; } "
        "int wrapper(int live, int dead){ return callee(live); } "
        "int f(void){ return wrapper(7, 9); }"
    )

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["stats"]["deadargelim.dead_params"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "deadargelim")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "deadargelim.dead_params" not in disabled_artifact["pass_report"]["stats"]
    assert (
        disabled_artifact["pass_report"]["passes"]["deadargelim-analysis"]["skips"] >= 1
    )
    assert disabled_artifact["pass_report"]["passes"]["inline-opt"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["dce"]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_elim_avail_extern_boundary(
    monkeypatch,
):
    code = "extern int helper(void); int main(void){ return 0; }"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["stats"]["elim_avail_extern.removed"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "elim-avail-extern")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "elim_avail_extern.removed" not in disabled_artifact["pass_report"]["stats"]
    assert (
        disabled_artifact["pass_report"]["passes"]["elim-avail-extern-src"]["skips"] >= 1
    )


@pytest.mark.parametrize(
    ("alias", "code"),
    [
        (
            "simplifycfg",
            "int f(int c,int a,int b){ if (c) return a; return b; }",
        ),
        (
            "loop-simplifycfg",
            (
                "int f(int x){ int sum=0; "
                "for(int i=0;i<4;i++){ if (x) { if (x) sum += i; } } "
                "return sum; }"
            ),
        ),
    ],
)
def test_registered_disable_alias_can_turn_off_cfg_cleanup_translations(
    monkeypatch, alias, code
):
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["passes"]["control-flow"]["runs"] >= 1
    assert default_artifact["pass_report"]["passes"]["dce"]["runs"] >= 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["pass_report"]["passes"]["control-flow"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["dce"]["skips"] >= 1


@pytest.mark.parametrize(
    ("alias", "code", "disabled_needles", "metric_names"),
    [
        (
            "correlated-propagation",
            "int f(int x){ if (x) { if (x) return 1; } return 0; }",
            ("ret i32 0", "ret i32 1"),
            ("control-flow", "canonicalize", "dce"),
        ),
        (
            "jump-threading",
            "int f(int x){ if (x) return 1; if (x) return 2; return 0; }",
            ("ret i32 0", "ret i32 1", "ret i32 2"),
            ("control-flow",),
        ),
        (
            "constraint-elimination",
            "int f(int x){ if (x > 3) { if (x > 1) return 1; } return 0; }",
            ("ret i32 0", "ret i32 1"),
            ("control-flow", "canonicalize"),
        ),
    ],
)
def test_registered_disable_alias_can_turn_off_branch_refinement_translations(
    monkeypatch, alias, code, disabled_needles, metric_names
):
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    for needle in disabled_needles:
        assert _ir_needle_count(default_artifact["ir_text"], needle) == 0
        assert _ir_needle_count(disabled_artifact["ir_text"], needle) >= 1
    for metric_name in metric_names:
        assert disabled_artifact["pass_report"]["passes"][metric_name]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_speculation_friendly_translation(
    monkeypatch,
):
    code = "int f(int c, int x){ if (c) return x + 0; return 0; }"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["ir_text"].count(" phi ") == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "speculative-execution")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["ir_text"].count(" phi ") == 0
    assert disabled_artifact["pass_report"]["passes"]["control-flow"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["canonicalize"]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_analysis_only_loop_idiom_translation(
    monkeypatch,
):
    code = "void fill(int *p, int n){ for (int i=0; i<n; ++i) { p[i] = 0; } }"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["stats"]["loop_opt.memset_idiom_candidates"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "loop-idiom")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "loop_opt.memset_idiom_candidates" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["loop-opt"]["skips"] >= 1


@pytest.mark.parametrize(
    ("alias", "code", "stat_name"),
    [
        (
            "memcpyopt",
            (
                "void *memcpy(void*, const void*, unsigned long); "
                "void copy(char *dst, char *src){ memcpy(dst, src, 4); }"
            ),
            "memory_opt.memcpy_like_calls",
        ),
        (
            "mldst-motion",
            "int f(int *p){ int x=*p; int y=*p; return x+y; }",
            "memory_opt.load_load_elim",
        ),
        (
            "loop-load-elim",
            (
                "int f(int *p){ "
                "for(int i=0;i<3;i++){ int x=p[0]; int y=p[0]; p[0]=x+y; } "
                "return p[0]; }"
            ),
            "memory_opt.load_load_elim",
        ),
    ],
)
def test_registered_disable_alias_can_turn_off_analysis_only_memory_translations(
    monkeypatch, alias, code, stat_name
):
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["stats"][stat_name] >= 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert stat_name not in disabled_artifact["pass_report"]["stats"]
    metric = disabled_artifact["pass_report"]["passes"]["memory-opt-ir"]
    assert metric["skips"] >= 1


def test_mldst_motion_alias_controls_within_block_load_reuse(monkeypatch):
    code = "int f(int *p){ int x=*p; int y=*p; return x+y; }"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["ir_text"].count(" load ") == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "mldst-motion")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["ir_text"].count(" load ") >= 2
    assert disabled_artifact["pass_report"]["passes"]["memory-opt-ir"]["skips"] >= 1


def test_mldst_motion_alias_controls_bitcast_exact_slot_reuse(monkeypatch):
    code = "int f(int a){ int x = 0; void *p = &x; *(int*)p = a; return x; }"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["pass_report"]["stats"]["memory_opt.store_load_forward"] >= 3
    assert default_artifact["ir_text"].count(" load ") < 5

    monkeypatch.setenv("PCC_DISABLE_PASSES", "mldst-motion")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["ir_text"].count(" load ") >= 5
    assert "memory_opt.store_load_forward" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["memory-opt-ir"]["skips"] >= 1


def test_mldst_motion_alias_controls_zero_gep_exact_slot_reuse(monkeypatch):
    code = "int f(int a){ int x[1]; x[0] = a; return x[0]; }"

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["pass_report"]["stats"]["memory_opt.store_load_forward"] >= 2
    assert default_artifact["ir_text"].count(" load ") == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "mldst-motion")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["ir_text"].count(" load ") >= 3
    assert "memory_opt.store_load_forward" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["memory-opt-ir"]["skips"] >= 1


def test_loop_load_elim_alias_controls_within_block_loop_reload_reuse(monkeypatch):
    code = (
        "int f(int *p){ "
        "for(int i=0;i<3;i++){ int x=p[0]; int y=p[0]; p[0]=x+y; } "
        "return p[0]; }"
    )

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["ir_text"].count(" load ") == 8

    monkeypatch.setenv("PCC_DISABLE_PASSES", "loop-load-elim")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert disabled_artifact["ir_text"].count(" load ") >= 10
    assert disabled_artifact["pass_report"]["passes"]["memory-opt-ir"]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_analysis_only_sroa_translation(
    monkeypatch,
):
    code = (
        "struct S { int x; int y; }; "
        "int f(void){ struct S s; s.x=1; s.y=2; return s.x+s.y; }"
    )
    pipeline = _pipeline_without_high_passes("alloc-decision")

    default_artifact = _compile_preprocessed_translation_unit_artifact(
        "probe.c", code, pass_pipeline=pipeline
    )

    assert default_artifact["pass_report"]["stats"]["sroa.candidates"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "sroa")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact(
        "probe.c",
        code,
        pass_pipeline=_pipeline_without_high_passes("alloc-decision"),
    )

    assert "sroa.candidates" not in disabled_artifact["pass_report"]["stats"]
    metric = disabled_artifact["pass_report"]["passes"]["sroa"]
    assert metric["skips"] >= 1


@pytest.mark.parametrize(
    ('alias', 'code', 'needle', 'default_count', 'disabled_count'),
    [
        ('aggressive-instcombine', 'int f(int x){ return (x + 0) + (2 + 3); }', ' add ', 1, 3),
        ('instcombine', 'int f(int x){ return (x + 0) + (2 + 3); }', ' add ', 1, 3),
        ('adce', 'int f(int a){ int x = a + 1; return a; }', ' add ', 0, 1),
        ('bdce', 'int f(int a){ int x = a + 1; return a; }', ' add ', 0, 1),
        ('argpromotion', 'int leaf(int x){ return x + 1; } int wrap(int x){ return leaf(x); } int f(void){ struct S { int a; int b; }; struct S s; s.a=7; s.b=8; int y=s.a; return wrap(y); }', 'call i32 @"wrap"', 0, 1),
        ('called-value-propagation', 'int id(int x){ return x; } int wrap(int x){ return id(x); } int f(void){ int y=7; int z=y; return wrap(z); }', 'call i32 @"wrap"', 0, 1),
        ('callsite-splitting', 'int id(int x){ return x; } int wrap(int x){ return id(x); } int f(int c,int x){ if (c) return wrap(x); return wrap(x + 1); }', 'call i32 @"wrap"', 0, 2),
        ('early-cse', 'int f(int x,int y){ int a=x+y; int b=x+y; return a==b; }', ' add ', 1, 2),
        ('globalopt', 'static int id(int x){ return x; } static int wrap(int x){ return id(x); } int f(void){ int y=7; int z=y; return wrap(z); }', 'alloca', 1, 3),
        ('ipsccp', 'int id(int x){ return x; } int wrap(int x){ return id(x); } int f(void){ return wrap(7); }', 'call i32 @"wrap"', 0, 1),
        ('loop-instsimplify', 'int f(int n){ int sum=0; for(int i=0;i<n;i++){ sum += i - 0; } return sum; }', ' sub ', 0, 1),
        ('sccp', 'int f(void){ int x = 4 - 4; if (x) return 7; return 9; }', ' br i1 ', 0, 1),
    ],
)
def test_owned_c_translations_rewrite_focused_ir(monkeypatch, alias, code, needle, default_count, disabled_count):
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert _ir_needle_count(default_artifact["ir_text"], needle) == default_count
    assert _ir_needle_count(disabled_artifact["ir_text"], needle) == disabled_count


def test_owned_dse_removes_dead_store_before_parameter_assignment(monkeypatch):
    code = """
    int f(int a) {
        int x;
        x = 1;
        x = a;
        return x;
    }
    """
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    monkeypatch.setenv("PCC_DISABLE_PASSES", "dse")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert _defined_function_ir(default_artifact["ir_text"], "f").count("store ") == 2
    assert _defined_function_ir(disabled_artifact["ir_text"], "f").count("store ") == 3


@pytest.mark.parametrize("alias", ["gvn", "newgvn"])
def test_owned_gvn_family_removes_repeated_add(monkeypatch, alias):
    code = "int f(int x,int y){ int a=x+y; int b=x+y; return a==b; }"
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["ir_text"].count(" add ") == 1
    assert disabled_artifact["ir_text"].count(" add ") == 2
    assert disabled_artifact["pass_report"]["passes"]["ssa-gvn"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["ssa-gvn-rewrite"]["skips"] >= 1
    assert disabled_artifact["pass_report"]["passes"]["gvn"]["skips"] >= 1
    assert (
        disabled_artifact["pass_report"]["passes"]["local-value-numbering"]["skips"] >= 1
    )
    assert disabled_artifact["pass_report"]["passes"]["copy-propagation"]["skips"] >= 1


def test_registered_disable_alias_can_turn_off_analysis_only_loop_sink_translation(
    monkeypatch,
):
    code = (
        "int f(int *p, int cond){ int sum=0; "
        "for (int i=0; i<4; ++i) { int t = *p; if (cond) sum += t; } "
        "return sum; }"
    )

    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert default_artifact["pass_report"]["stats"]["loop_opt.sink_candidates"] == 1

    monkeypatch.setenv("PCC_DISABLE_PASSES", "loop-sink")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)

    assert "loop_opt.sink_candidates" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["loop-opt"]["skips"] >= 1


@pytest.mark.parametrize(
    ("alias", "code", "stat_name"),
    [
        (
            "memcpyopt",
            (
                "void *memcpy(void*, const void*, unsigned long); "
                "void copy(char *dst, char *src){ memcpy(dst, src, 4); }"
            ),
            "memory_opt.memcpy_like_calls",
        ),
        (
            "mldst-motion",
            "int f(int *p){ int x=*p; int y=*p; return x+y; }",
            "memory_opt.load_load_elim",
        ),
        (
            "loop-load-elim",
            (
                "int f(int *p){ "
                "for(int i=0;i<3;i++){ int x=p[0]; int y=p[0]; p[0]=x+y; } "
                "return p[0]; }"
            ),
            "memory_opt.load_load_elim",
        ),
    ],
)
def test_owned_memory_translations_report_analysis_boundaries(monkeypatch, alias, code, stat_name):
    default_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    monkeypatch.setenv("PCC_DISABLE_PASSES", alias)
    disabled_artifact = _compile_preprocessed_translation_unit_artifact("probe.c", code)
    assert default_artifact["pass_report"]["stats"][stat_name] >= 1
    assert stat_name not in disabled_artifact["pass_report"]["stats"]


def test_owned_sroa_reports_aggregate_candidate_without_ir_promotion_claim(monkeypatch):
    code = (
        "struct S { int x; int y; }; "
        "int f(void){ struct S s; s.x=1; s.y=2; return s.x+s.y; }"
    )
    pipeline = _pipeline_without_high_passes("alloc-decision")
    default_artifact = _compile_preprocessed_translation_unit_artifact(
        "probe.c", code, pass_pipeline=pipeline
    )
    monkeypatch.setenv("PCC_DISABLE_PASSES", "sroa")
    disabled_artifact = _compile_preprocessed_translation_unit_artifact(
        "probe.c",
        code,
        pass_pipeline=_pipeline_without_high_passes("alloc-decision"),
    )
    assert default_artifact["pass_report"]["stats"]["sroa.candidates"] == 1
    assert "sroa.candidates" not in disabled_artifact["pass_report"]["stats"]
    assert disabled_artifact["pass_report"]["passes"]["sroa"]["skips"] >= 1
    assert disabled_artifact["ir_text"].count("alloca") >= 1


def test_function_attrs_stay_conservative_for_leaf_loops():
    code = "int spin(void){ while (1) {} }"

    artifact = _compile_preprocessed_translation_unit_artifact("spin.c", code)
    ir_text = artifact["ir_text"]

    assert "nounwind" in ir_text
    assert "nofree" in ir_text
    assert "willreturn" not in ir_text
