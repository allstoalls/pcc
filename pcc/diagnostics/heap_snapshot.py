
"""Heap snapshot graph utilities for pcc roadmap debugger tools.

The runtime can emit snapshots as node/edge JSON.  This module performs the
offline analysis needed by ``pcc-heap-snapshot``: reachability, strongly
connected components, and likely cycle leaks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class HeapNode:
    id: str
    type_name: str
    size: int = 0
    root: bool = False


@dataclass(frozen=True)
class HeapEdge:
    src: str
    dst: str
    label: str = ""


@dataclass
class HeapSnapshot:
    nodes: dict[str, HeapNode] = field(default_factory=dict)
    edges: list[HeapEdge] = field(default_factory=list)

    @staticmethod
    def from_json(data: dict) -> "HeapSnapshot":
        nodes = {
            str(n["id"]): HeapNode(
                id=str(n["id"]),
                type_name=str(n.get("type", n.get("type_name", "object"))),
                size=int(n.get("size", 0) or 0),
                root=bool(n.get("root", False)),
            )
            for n in data.get("nodes", [])
        }
        edges = [
            HeapEdge(
                src=str(e["src"]),
                dst=str(e["dst"]),
                label=str(e.get("label", "")),
            )
            for e in data.get("edges", [])
        ]
        return HeapSnapshot(nodes=nodes, edges=edges)

    def to_json(self) -> dict:
        return {
            "nodes": [
                {"id": n.id, "type": n.type_name, "size": n.size, "root": n.root}
                for n in self.nodes.values()
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "label": e.label}
                for e in self.edges
            ],
        }

    def adjacency(self) -> dict[str, list[str]]:
        adj = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            adj.setdefault(edge.src, []).append(edge.dst)
            adj.setdefault(edge.dst, [])
        return adj

    def reachable_from_roots(self) -> set[str]:
        adj = self.adjacency()
        work = [node.id for node in self.nodes.values() if node.root]
        seen: set[str] = set()
        while work:
            cur = work.pop()
            if cur in seen:
                continue
            seen.add(cur)
            work.extend(adj.get(cur, []))
        return seen

    def unreachable_nodes(self) -> set[str]:
        return set(self.nodes) - self.reachable_from_roots()

    def strongly_connected_components(self) -> list[set[str]]:
        adj = self.adjacency()
        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        comps: list[set[str]] = []

        def visit(v: str) -> None:
            nonlocal index
            indices[v] = index
            low[v] = index
            index += 1
            stack.append(v)
            on_stack.add(v)
            for w in adj.get(v, []):
                if w not in indices:
                    visit(w)
                    low[v] = min(low[v], low[w])
                elif w in on_stack:
                    low[v] = min(low[v], indices[w])
            if low[v] == indices[v]:
                comp: set[str] = set()
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    comp.add(w)
                    if w == v:
                        break
                comps.append(comp)

        for node_id in self.nodes:
            if node_id not in indices:
                visit(node_id)
        return comps

    def likely_cycle_leaks(self) -> list[set[str]]:
        unreachable = self.unreachable_nodes()
        out: list[set[str]] = []
        edge_pairs = {(e.src, e.dst) for e in self.edges}
        for comp in self.strongly_connected_components():
            if not comp <= unreachable:
                continue
            if len(comp) > 1:
                out.append(comp)
            else:
                only = next(iter(comp))
                if (only, only) in edge_pairs:
                    out.append(comp)
        return out
