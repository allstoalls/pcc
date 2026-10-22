"""Wire encoding and class layout consume one semantic AST field contract.

Previously separate field tables drifted for SetType and ValueArrayType
(AUD-P2-SELF-MODULE-SPECIAL-CASES-IN-CODEGEN). Keep the shared contract complete
and equal to the actual AST definitions, including their inherited fields.
"""

from __future__ import annotations

import dataclasses
import inspect

import pcc.frontends.python.py_ast as py_ast
import pcc.frontends.python.pipeline as pipeline
import pcc.frontends.python.pipeline_ast_wire as pipeline_ast_wire
from pcc.frontends.python.py_ast_contract import PY_AST_FIELD_NAME_OVERRIDES


def _ast_dataclasses():
    return {
        name: obj
        for name, obj in vars(py_ast).items()
        if inspect.isclass(obj)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == py_ast.__name__
    }


def test_wire_and_layout_share_the_field_order_contract():
    assert pipeline_ast_wire._PY_AST_FIELD_NAME_OVERRIDES is PY_AST_FIELD_NAME_OVERRIDES
    assert (
        pipeline._PY_AST_FIELD_NAME_OVERRIDES
        is pipeline_ast_wire._PY_AST_FIELD_NAME_OVERRIDES
    )


def test_layout_and_wire_tables_use_the_same_semantic_fields():
    assert not hasattr(pipeline_ast_wire, "_PY_AST_WIRE_FIELD_NAME_OVERRIDES")
    for field_names in PY_AST_FIELD_NAME_OVERRIDES.values():
        assert "kind_id" not in field_names


def test_every_pinned_order_matches_the_real_dataclass():
    classes = _ast_dataclasses()
    mismatches = []
    for name, pinned in PY_AST_FIELD_NAME_OVERRIDES.items():
        cls = classes.get(name)
        if cls is None:
            mismatches.append(f"{name}: pinned but no such dataclass in py_ast")
            continue
        actual = tuple(f.name for f in dataclasses.fields(cls))
        if actual != tuple(pinned):
            mismatches.append(f"{name}: dataclass {actual}, pinned {tuple(pinned)}")
    assert not mismatches, "\n  ".join(["pinned field order is wrong:"] + mismatches)


def test_every_ast_node_is_pinned():
    """An unpinned node takes the inferred path on one side and the pinned path
    on the other — which is exactly how SetType and ValueArrayType drifted."""
    unpinned = sorted(set(_ast_dataclasses()) - set(PY_AST_FIELD_NAME_OVERRIDES))
    assert not unpinned, (
        "these py_ast nodes have no pinned field order; add them to "
        "py_ast_contract.py:\n  " + ", ".join(unpinned)
    )
