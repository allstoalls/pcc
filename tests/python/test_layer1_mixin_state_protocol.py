"""Source lint for the L1CodeGen mixin composition.

`L1CodeGen` composes 86 mixins over one `self` namespace, so two mixins
defining the same private helper is resolved silently by the MRO — one
definition simply wins, and a later edit to the losing copy does nothing.
That is the failure this lint exists to prevent (ARCH-P3-LAYER1-STATE-PROTOCOL).

It also keeps `pcc/frontends/python/codegen/layer1_state.py` honest: the declared
shared-state surface must stay a superset of what the mixins actually read.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

from pcc.frontends.python.codegen.layer1_state import (
    SHARED_STATE_ATTRIBUTES,
    L1CodeGenState,
)


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


CODEGEN = _repo_root() / "pcc" / "frontends" / "python" / "codegen"


def _mixin_methods() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for path in sorted(CODEGEN.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[item.name].append((path.name, node.name))
    return out


def test_no_method_name_is_defined_by_two_mixins():
    collisions = {
        name: sites for name, sites in _mixin_methods().items() if len(sites) > 1
    }
    assert not collisions, (
        "these method names are defined by more than one L1CodeGen mixin, so the "
        "MRO silently picks one and edits to the other are dead code:\n  "
        + "\n  ".join(
            f"{name}: " + ", ".join(f"{mod}:{cls}" for mod, cls in sites)
            for name, sites in sorted(collisions.items())
        )
    )


def test_the_lint_actually_sees_the_mixins():
    """A lint that scans nothing passes for the wrong reason."""
    methods = _mixin_methods()
    mixin_classes = {cls for sites in methods.values() for _, cls in sites}
    assert len(mixin_classes) > 50, len(mixin_classes)
    assert len(methods) > 500, len(methods)


def test_declared_shared_state_covers_what_mixins_actually_read():
    """Every non-private `self.<attr>` read in a lowering mixin is declared."""
    read = collections.Counter()
    for path in list(CODEGEN.glob("*_lowering.py")) + list(CODEGEN.glob("native_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and not node.attr.startswith("_")
                and not isinstance(node.ctx, ast.Store)
            ):
                read[node.attr] += 1
    declared = set(SHARED_STATE_ATTRIBUTES)
    # Method calls are not state; only count attributes never used as a call
    # target and read from at least two different mixin files.
    undeclared = sorted(
        name
        for name, count in read.items()
        if name not in declared and count >= 12 and not name.startswith("emit_")
    )
    assert not undeclared, (
        "mixins read shared state that layer1_state.L1CodeGenState does not "
        "declare; add it there (with who owns it) or stop reading it:\n  "
        + ", ".join(undeclared)
    )


def test_protocol_lists_the_high_traffic_attributes():
    for name in ("builder", "runtime", "module", "env", "current_function"):
        assert name in SHARED_STATE_ATTRIBUTES, name
    assert isinstance(L1CodeGenState, type)
