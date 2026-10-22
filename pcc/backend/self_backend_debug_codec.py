"""Versioned original debug graph transport for owned indexed modules.

Only original metadata IDs cross the boundary. Re-resolve their file/scope
facts and bind dense block shapes to the decoded kernel before publishing.
"""
from __future__ import annotations

from . import BackendUnavailable
from .self_backend_debug import DebugMetadata, DebugNode
from .self_backend_ir import ParsedFunction
from .self_backend_kernel import IndexedFunctionKernel
from .self_backend_value_arena import CompilerInt4


_PROFILE = "pcc.source-debug.v1"


def _identity(value, *, missing: bool = False) -> int:
    if type(value) is not int or value < (-1 if missing else 0):
        raise BackendUnavailable("indexed source debug has an invalid metadata identity")
    return value


def debug_from_wire(value, functions: tuple[ParsedFunction, ...]) -> DebugMetadata:
    if not isinstance(value, dict) or value.get("profile") != _PROFILE:
        raise BackendUnavailable("indexed source debug profile mismatch")
    rows = value.get("nodes")
    bindings = value.get("functions")
    if not isinstance(rows, list) or not isinstance(bindings, list):
        raise BackendUnavailable("indexed source debug tables are invalid")
    nodes: dict[int, DebugNode] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise BackendUnavailable("indexed source debug node is invalid")
        identity = _identity(row[0])
        kind = row[1]
        fields = row[2]
        if (identity in nodes or not isinstance(kind, str) or not kind.startswith("DI")
                or not isinstance(fields, dict)):
            raise BackendUnavailable("indexed source debug node is invalid or duplicated")
        for name, field in fields.items():
            if not isinstance(name, str) or not isinstance(field, str):
                raise BackendUnavailable("indexed source debug field is invalid")
        nodes[identity] = DebugNode(kind, dict(fields))
    debug = DebugMetadata(nodes)
    owners = {function.name: function for function in functions}
    if len(owners) != len(functions):
        raise BackendUnavailable("indexed source debug function symbols are duplicated")
    scopes: set[int] = set()
    for binding in bindings:
        if not isinstance(binding, list) or len(binding) != 3:
            raise BackendUnavailable("indexed source debug function binding is invalid")
        symbol = binding[0]
        scope = _identity(binding[1])
        blocks = binding[2]
        if (not isinstance(symbol, str) or symbol not in owners
                or symbol in debug.functions or scope in scopes
                or not isinstance(blocks, list)):
            raise BackendUnavailable("indexed source debug function binding is invalid")
        scopes.add(scope)
        function = owners[symbol]
        kernel = function.indexed_kernel
        if not isinstance(kernel, IndexedFunctionKernel):
            raise BackendUnavailable("indexed source debug requires a published kernel")
        if len(blocks) != len(kernel.block_names):
            raise BackendUnavailable("indexed source debug block count differs from its kernel")
        info = debug.function(scope, symbol)
        for block_id, block in enumerate(blocks):
            if not isinstance(block, list) or len(block) != 3:
                raise BackendUnavailable("indexed source debug block is invalid")
            name, instructions, terminator = block
            block_fact: CompilerInt4 = kernel.block_fact(block_id)
            if (name != kernel.block_names[block_id] or not isinstance(instructions, list)
                    or len(instructions) != block_fact.second):
                raise BackendUnavailable("indexed source debug block differs from its kernel")
            instruction_ids = [_identity(item, missing=True) for item in instructions]
            debug.block(info, name, instruction_ids, [_identity(terminator, missing=True)])
    return debug


def debug_to_wire(debug: DebugMetadata, functions: tuple[ParsedFunction, ...]):
    bindings = []
    for symbol, function in debug.functions.items():
        if function.symbol != symbol:
            raise BackendUnavailable("indexed source debug function symbol differs")
        bindings.append([symbol, function.scope, [
            [block.name,
             [location.identity if location is not None else -1
              for location in block.instructions],
             block.terminator.identity if block.terminator is not None else -1]
            for block in function.blocks
        ]])
    wire = {
        "profile": _PROFILE,
        "nodes": [[identity, node.kind, dict(node.fields)]
                  for identity, node in sorted(debug.nodes.items())],
        "functions": bindings,
    }
    restored = debug_from_wire(wire, functions)
    if restored.functions != debug.functions:
        raise BackendUnavailable("indexed source debug coordinates differ from original metadata")
    return wire
