from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pcc.frontends.python.codegen import runtime_abi
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
    RUNTIME_SIGNATURES,
)


SOURCE = (
    Path(__file__).resolve().parents[2]
    / "pcc" / "frontends" / "python"
    / "codegen"
    / "runtime_abi.py"
)


def _chunk_literals(prefix: str) -> list[ast.Dict]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    literals: list[ast.Dict] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith(prefix):
            continue
        returns = [stmt for stmt in node.body if isinstance(stmt, ast.Return)]
        assert len(returns) == 1
        value = returns[0].value
        assert isinstance(value, ast.Dict)
        literals.append(value)
    return literals


def _chunk_keys(prefix: str) -> tuple[list[str], list[int]]:
    keys: list[str] = []
    sizes: list[int] = []
    for value in _chunk_literals(prefix):
        sizes.append(len(value.keys))
        for key in value.keys:
            assert isinstance(key, ast.Constant) and isinstance(key.value, str)
            keys.append(key.value)
    return keys, sizes


def _assemble_chunks(prefix: str) -> dict:
    parts = []
    for name, value in vars(runtime_abi).items():
        if name.startswith(prefix):
            parts.append((int(name.rsplit("_", 1)[1]), value))
    assembled = {}
    for _index, part in sorted(parts):
        assembled.update(part())
    return assembled


def test_runtime_signature_literals_are_bounded_function_chunks() -> None:
    keys, sizes = _chunk_keys("_runtime_signatures_part_")

    assert sizes
    assert max(sizes) <= 50
    assert len(keys) == len(set(keys))
    assert _assemble_chunks("_runtime_signatures_part_") == RUNTIME_SIGNATURES
    assert keys == list(RUNTIME_SIGNATURES)


def test_cross_object_signature_literals_are_bounded_function_chunks() -> None:
    keys, sizes = _chunk_keys("_cross_object_signatures_part_")

    assert sizes
    assert max(sizes) <= 50
    assembled = _assemble_chunks("_cross_object_signatures_part_")
    assert assembled == FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
    assert list(assembled) == list(FREESTANDING_GC_CROSS_OBJECT_SIGNATURES)


@pytest.mark.parametrize(
    ("prefix", "signatures"),
    [
        ("_runtime_signatures_part_", RUNTIME_SIGNATURES),
        ("_cross_object_signatures_part_", FREESTANDING_GC_CROSS_OBJECT_SIGNATURES),
    ],
    ids=["runtime", "cross_object"],
)
def test_signature_chunks_preserve_flat_literal_values_and_order(
    prefix: str, signatures: dict
) -> None:
    # A single literal supplies the reference semantics independently of the
    # chunk calls and update sequence, including last-write-wins values and
    # first-insertion positions for duplicate names in the cross-object table.
    literals = _chunk_literals(prefix)
    flattened = ast.Dict(
        keys=[key for literal in literals for key in literal.keys],
        values=[value for literal in literals for value in literal.values],
    )
    expression = ast.fix_missing_locations(ast.Expression(body=flattened))
    expected = eval(compile(expression, str(SOURCE), "eval"), vars(runtime_abi))

    assert list(signatures.items()) == list(expected.items())
