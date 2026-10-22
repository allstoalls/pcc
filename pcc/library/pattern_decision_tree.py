from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternCase:
    tag: str
    target: str


@dataclass(frozen=True)
class DecisionNode:
    tag: str
    target: str


def build_decision_tree(cases: list[PatternCase], *, default: str | None = None) -> list[DecisionNode]:
    seen: set[str] = set()
    nodes: list[DecisionNode] = []
    for case in cases:
        if case.tag not in seen:
            seen.add(case.tag)
            nodes.append(DecisionNode(case.tag, case.target))
    if default is not None:
        nodes.append(DecisionNode("_", default))
    return nodes
