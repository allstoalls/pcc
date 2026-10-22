"""Architecture guards for the nested-hoist lowering split."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).absolute().parents[2]
_CODEGEN = _ROOT / "pcc" / "frontends" / "python" / "codegen"


def _tree(name: str) -> ast.Module:
    return ast.parse((_CODEGEN / name).read_text(encoding="utf-8"))


def test_hoist_pass_is_composed_outside_layer1_mro():
    mixins = _tree("layer1_mixins.py")
    stack = next(
        node
        for node in mixins.body
        if isinstance(node, ast.ClassDef) and node.name == "L1CodeGenMixinStack"
    )
    base_names = {
        base.id for base in stack.bases if isinstance(base, ast.Name)
    }
    assert "HoistLoweringMixin" not in base_names

    generation = _tree("generation_lowering.py")
    calls = [
        node
        for node in ast.walk(generation)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hoist_nested_funcdefs"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 1
    assert isinstance(calls[0].args[0], ast.Name)
    assert calls[0].args[0].id == "self"

    from pcc.frontends.python.codegen.host_contract import (
        PROBE_POLICY_CONTEXTUAL_MIXIN,
        per_module_probe_policy,
    )

    for suffix in ("hoist_boxing", "hoist_free_names", "hoist_predicates"):
        assert (
            per_module_probe_policy("pcc.frontends.python.codegen." + suffix)
            == PROBE_POLICY_CONTEXTUAL_MIXIN
        )


def test_hoist_pass_keeps_analysis_and_boxing_out_of_orchestrator():
    lowering = _tree("hoist_lowering.py")
    pass_class = next(
        node
        for node in lowering.body
        if isinstance(node, ast.ClassDef) and node.name == "_HoistLoweringPass"
    )
    run = next(
        node
        for node in pass_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_hoist_nested_funcdefs"
    )

    # The pre-split method was about 2,890 lines. Keep the orchestration below
    # the post-split ceiling so pure analysis cannot silently migrate back in.
    assert run.end_lineno is not None
    assert run.end_lineno - run.lineno + 1 <= 1800

    nested_names = {
        node.name
        for node in ast.walk(run)
        if isinstance(node, ast.FunctionDef) and node is not run
    }
    assert nested_names.isdisjoint(
        {
            "box_expr",
            "box_stmts",
            "box_outer_body",
            "compute_free_names",
            "body_has_yield",
            "body_needs_nested_rewrite",
            "hoist_stmt_kind",
            "rewrite_yield_in_stmts",
        }
    )

    split_files = (
        "hoist_lowering.py",
        "hoist_analysis.py",
        "hoist_boxing.py",
        "hoist_free_names.py",
        "hoist_predicates.py",
    )
    combined_lines = sum(
        len((_CODEGEN / name).read_text(encoding="utf-8").splitlines())
        for name in split_files
    )
    # +35 over the post-split 4400: boxing now rewrites import bindings of
    # captured names into cell stores (``_box_import``).
    # +750: boxing binds for/with/except targets through temporaries,
    # rewrites raise/del/def-default/class-header reads, respects lambda and
    # comprehension scopes, boxes names a lambda sees rebound (the late-binding
    # scan), and gives lambda-captured comprehension targets per-run cells.
    # +24: a hoisted closure's captured ``self`` keeps the enclosing method's
    # inferred type (mixin methods read the host class layout).
    # +418: function-local from-import aliases are scoped (0f6fa1d9c), classes
    # nested in a class body are declared ahead of their outer class
    # (8c7f55e4c), nested-def decorators apply where the definition executes
    # (7c8158df1), the October remediation checkpoints (1a59d09df, 351c617eb,
    # 1a891b352), and the module-scope name scan moved out of the
    # orchestrator into ``_hoist_module_scope_names``.
    # +222: function-local class methods are closures over the definition's
    # cells (trailing capture parameters, one ``__class__`` cell per class
    # statement execution, run-time base classification) instead of the
    # hidden-instance-attribute approximation.
    # +15: a nested def's imported or builtin decorators apply where the def
    # executes too; which ones function declaration lowers itself is
    # decorator_lowering.lowered_at_declaration.
    assert combined_lines <= 5864


def test_hoist_exception_handlers_use_stage_safe_field_access():
    """Loop-projected handlers lose precise fields in compiled stages."""
    for name in (
        "hoist_analysis.py",
        "hoist_boxing.py",
        "hoist_free_names.py",
        "hoist_lowering.py",
        "hoist_predicates.py",
    ):
        tree = _tree(name)
        for loop in (node for node in ast.walk(tree) if isinstance(node, ast.For)):
            if not (
                isinstance(loop.target, ast.Name)
                and isinstance(loop.iter, ast.Attribute)
                and loop.iter.attr == "handlers"
            ):
                continue
            target_name = loop.target.id
            direct_handler_fields = {
                node.attr
                for node in ast.walk(loop)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == target_name
                and node.attr in {"body", "exc_type", "name"}
            }
            assert not direct_handler_fields, (name, direct_handler_fields)


def test_free_name_analysis_does_not_pass_analyzer_callbacks():
    tree = _tree("hoist_free_names.py")
    top_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    compute = top_functions["compute_free_names"]
    analyzer_names = {
        node.name
        for node in ast.walk(compute)
        if isinstance(node, ast.FunctionDef) and node is not compute
    }
    analyzer_names.update(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_walk_")
    )
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        passed_names = {
            node.id
            for arg in call.args
            for node in ast.walk(arg)
            if isinstance(node, ast.Name)
        }
        assert passed_names.isdisjoint(analyzer_names)
