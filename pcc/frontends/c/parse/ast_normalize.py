"""Stable serialization of ``pcc.frontends.c.ast.c_ast`` trees for differential
testing of C parsers.

The normalized form:
- Each node is a dict ``{"_t": "NodeTypeName", <attr_name>: <value>, ...}``
- Attributes listed in ``node.attr_names`` are serialized by value
- Children from ``node.children()`` are serialized recursively
- Source coordinates are **dropped** (they're formatting-dependent and
  diverge between parsers that keep different spans)
- Node identity (id(node), weakref targets) is dropped

The output is JSON-compatible — list/dict/str/int/float/None — so any
two implementations of a C parser producing structurally equal trees
will serialize byte-identical.
"""
from __future__ import annotations

import json
from typing import Any

from pcc.frontends.c.ast import c_ast


def normalize(node: Any) -> Any:
    """Serialize a c_ast subtree (or list of subtrees) to a JSON-safe
    nested dict/list, dropping coordinates and identity info."""
    if node is None:
        return None
    if isinstance(node, list):
        return [normalize(x) for x in node]
    if isinstance(node, tuple):
        return [normalize(x) for x in node]
    if isinstance(node, c_ast.Node):
        out: dict = {"_t": type(node).__name__}
        # c_ast nodes use __slots__, so __dict__ isn't available.
        # Reading by attr_name is the documented contract; this tool
        # is host-only (not in the self-host compile target).
        attr_names = type(node).attr_names if hasattr(type(node), "attr_names") else ()
        for name in attr_names or ():
            v = getattr(node, name, None)
            # Attr values may themselves be nodes (e.g. bit-field size
            # expressions) or nested lists — recurse.
            out[name] = normalize(v)
        # Children are sub-nodes (may appear multiple times for list fields).
        children_by_name: dict = {}
        for child_name, child in node.children():
            # child_name for list-valued fields is like ``body[0]`` —
            # split off the base name so we group them back into a list.
            base = child_name.split("[", 1)[0]
            if base not in children_by_name:
                children_by_name[base] = []
            children_by_name[base].append(normalize(child))
        for base, kids in children_by_name.items():
            # Preserve the original single-vs-list shape: if the source
            # had ``x[0]`` style it's a list; else scalar.
            if len(kids) == 1 and not any(
                f"{base}[" in c for c, _ in node.children()
            ):
                out[base] = kids[0]
            else:
                out[base] = kids
        return out
    # Leaf values (str/int/float/bool/None) — JSON handles these.
    return node


def to_json(node: Any, *, indent: int = 2) -> str:
    """Serialize to deterministic JSON text. Keys are sorted so
    dict-insertion-order quirks don't cause false diffs."""
    return json.dumps(normalize(node), indent=indent, sort_keys=True)


def diff(a: Any, b: Any, *, path: str = "") -> list[str]:
    """Return a list of human-readable diff messages between two
    normalized trees. Empty list means identical. Cheap eager compare;
    stops walking a branch once a mismatch is found to keep output
    small on large trees."""
    out: list[str] = []
    _diff_walk(a, b, path, out)
    return out


def _diff_walk(a: Any, b: Any, path: str, out: list) -> None:
    if type(a) is not type(b):
        out.append(f"{path}: type {type(a).__name__} vs {type(b).__name__}")
        return
    if isinstance(a, dict):
        keys = set(a) | set(b)
        for k in sorted(keys):
            if k not in a:
                out.append(f"{path}.{k}: missing in LHS")
                continue
            if k not in b:
                out.append(f"{path}.{k}: missing in RHS")
                continue
            _diff_walk(a[k], b[k], f"{path}.{k}", out)
        return
    if isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: len {len(a)} vs {len(b)}")
            return
        for i, (ai, bi) in enumerate(zip(a, b)):
            _diff_walk(ai, bi, f"{path}[{i}]", out)
        return
    if a != b:
        out.append(f"{path}: {a!r} vs {b!r}")
