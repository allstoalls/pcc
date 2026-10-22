"""Named pass groups for ablation and reporting."""

from __future__ import annotations


_DEFAULT_PASS_GROUPS: dict[str, tuple[str, ...]] = {
    "analysis": (
        "lower-expect",
        "escape-analysis",
        "alloc-decision",
        "nsw-inference",
        "deadargelim-analysis",
        "ssa-bootstrap",
        "ssa-gvn",
        "ssa-sccp",
        "ssa-loop-phi",
    ),
    "scalar": (
        "sroa",
        "local-value-numbering",
        "gvn",
        "ssa-sccp-rewrite",
        "ssa-gvn-rewrite",
        "ssa-dse",
        "ssa-adce",
        "expr-reassociation",
        "copy-propagation",
    ),
    "simplify-cfg": (
        "ssa-branch-prune",
        "lower-constant-intrinsics",
        "float2int",
        "alignment-from-assumptions",
        "canonicalize",
        "dce",
        "control-flow",
    ),
    "loop-inline": (
        "loop-opt",
        "licm",
        "indvars",
        "loop-rotate",
        "simple-loop-unswitch",
        "loop-deletion",
        "loop-unroll",
        "loop-unroll-full",
        "inline-opt",
        "global-dce",
        "elim-avail-extern-src",
    ),
    "specialization": (
        "scc-analysis",
        "loop-recognition",
        "recursive-unrolling",
        "assignment-conversion",
        "let-elevation",
        "primitive-specialization",
        "closure-lifting",
        "redundant-check",
        "float-unboxing",
    ),
    "ir-metadata": (
        "tail-call",
        "noundef",
        "memory-opt-ir",
        "noalias",
        "align",
        "nsw-annotation",
        "func-attr",
        "loop-metadata",
        "range-metadata",
    ),
    "llvm-explicit-early": (
        "coro-early",
        "ee-instrument",
        "openmp-opt",
        "require",
        "invalidate",
    ),
    "llvm-explicit-late": (
        "openmp-opt-cgscc",
        "libcalls-shrinkwrap",
        "extra-simple-loop-unswitch-passes",
        "coro-elide",
        "coro-split",
        "coro-annotation-elide",
        "recompute-globalsaa",
        "coro-cleanup",
        "loop-distribute",
        "inject-tli-mappings",
        "loop-vectorize",
        "vector-combine",
        "transform-warning",
        "annotation-remarks",
        "verify",
        "move-auto-init",
        "slp-vectorizer",
        "div-rem-pairs",
        "constmerge",
        "cg-profile",
        "rel-lookup-table-converter",
        "chr",
    ),
}


def default_pass_groups() -> dict[str, tuple[str, ...]]:
    """Return the ordered default pass-group mapping."""
    return dict(_DEFAULT_PASS_GROUPS)


def pass_group_names() -> tuple[str, ...]:
    """Return the ordered default pass-group names."""
    return tuple(_DEFAULT_PASS_GROUPS)


def passes_for_group(group_name: str) -> tuple[str, ...]:
    """Return the pass names belonging to a group."""
    try:
        return _DEFAULT_PASS_GROUPS[group_name]
    except KeyError as exc:
        raise ValueError(f"Unknown pass group: {group_name}") from exc


def disable_pass_group(ctx, group_name: str):
    """Disable all passes in a named group on a PassContext."""
    for pass_name in passes_for_group(group_name):
        ctx.disable_pass(pass_name)


def unique_default_pass_names() -> tuple[str, ...]:
    """Return the unique pass names in the default pipeline order."""
    from pcc.frontends.c.passes.base import PassPipeline

    names: list[str] = []
    for pass_ in PassPipeline.default().high_tier + PassPipeline.default().low_tier:
        if pass_.name not in names:
            names.append(pass_.name)
    return tuple(names)


def registered_llvm_alias_names() -> tuple[str, ...]:
    """Return explicit LLVM names that already have Python-side registrations."""
    from pcc.frontends.c.passes.registry import registered_llvm_alias_names as _registered

    return _registered()


def llvm_default_pass_names(opt_level: int = 2) -> tuple[str, ...]:
    """Return unique concrete LLVM pass names for default<O{level}>.

    This is opt-in metadata for the explicit-text-pipeline path. It may return
    an empty tuple when no matching external `opt` binary is installed.
    """
    # External LLVM pipelines are no longer executable product capabilities.
    return ()


def unique_managed_pass_names(
    opt_level: int = 2,
    *,
    include_llvm: bool = False,
) -> tuple[str, ...]:
    """Return repository-managed pass names, optionally including LLVM leaf passes."""
    names = list(unique_default_pass_names())
    for pass_name in registered_llvm_alias_names():
        if pass_name not in names:
            names.append(pass_name)
    if include_llvm:
        for pass_name in llvm_default_pass_names(opt_level):
            if pass_name not in names:
                names.append(pass_name)
    return tuple(names)


def validate_default_pass_groups() -> dict[str, tuple[str, ...]]:
    """Return missing or extra pass names relative to the default pipeline."""
    grouped = {
        pass_name
        for pass_names in _DEFAULT_PASS_GROUPS.values()
        for pass_name in pass_names
    }
    default = set(unique_default_pass_names())
    return {
        "missing": tuple(sorted(default - grouped)),
        "extra": tuple(sorted(grouped - default)),
    }
