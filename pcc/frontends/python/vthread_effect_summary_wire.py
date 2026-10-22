"""Deterministic line codec for compact virtual-thread effect summaries."""

from __future__ import annotations


SCHEMA = "pcc.vthread.effect-summary.v1"


def _validated_text(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.find("\t") >= 0
        or value.find("\n") >= 0
        or value.find("\r") >= 0
    ):
        raise ValueError("invalid vthread effect summary " + label)
    return value


def _validate_values(values: list[str], label: str) -> None:
    if not isinstance(values, list):
        raise ValueError("invalid vthread effect summary " + label)
    index = 0
    while index < len(values):
        _validated_text(values[index], label)
        index += 1


def write_summary(
    path: str,
    module_name: str,
    seeds: list[str],
    edges: list[str],
    publish: list[str],
) -> None:
    module_name = _validated_text(module_name, "module")
    _validate_values(seeds, "seed")
    _validate_values(edges, "edge")
    _validate_values(publish, "publish")
    if len(edges) % 2:
        raise ValueError("vthread effect summary edge payload is odd")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(SCHEMA + "\n")
        stream.write("M\t" + module_name + "\n")
        for key in seeds:
            stream.write("S\t" + key + "\n")
        index = 0
        while index < len(edges):
            stream.write("E\t" + edges[index] + "\t" + edges[index + 1] + "\n")
            index += 2
        for key in publish:
            stream.write("P\t" + key + "\n")


def read_summary(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    if not lines or lines[0] != SCHEMA:
        raise ValueError("invalid vthread effect summary wire schema")
    module_name = ""
    seeds: list[str] = []
    edges: list[str] = []
    publish: list[str] = []
    index = 1
    while index < len(lines):
        parts = lines[index].split("\t")
        if len(parts) == 2 and parts[0] == "M":
            if module_name:
                raise ValueError("duplicate vthread effect summary module")
            module_name = _validated_text(parts[1], "module")
        elif len(parts) == 2 and parts[0] == "S":
            seeds.append(_validated_text(parts[1], "seed"))
        elif len(parts) == 3 and parts[0] == "E":
            edges.append(_validated_text(parts[1], "edge caller"))
            edges.append(_validated_text(parts[2], "edge callee"))
        elif len(parts) == 2 and parts[0] == "P":
            publish.append(_validated_text(parts[1], "publish"))
        else:
            raise ValueError("invalid vthread effect summary wire row")
        index += 1
    if not module_name:
        raise ValueError("vthread effect summary module is missing")
    return {
        "module_name": module_name,
        "seeds": seeds,
        "edges": edges,
        "publish": publish,
    }


__all__ = ["SCHEMA", "read_summary", "write_summary"]
