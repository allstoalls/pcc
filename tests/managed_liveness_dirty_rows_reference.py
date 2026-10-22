"""Reusable frozen pre-dirty-row source oracle; no production module imports."""

import ast
import copy
import hashlib
import json
from pathlib import Path

REFERENCE = Path(__file__).with_suffix(".json")
REFERENCE_SHA256 = "69ba8c9f267fe8a7d0b0ec1c65e897fe594067f1f412b3db6b3d771da718aa1d"


def reference_function():
    raw = REFERENCE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == REFERENCE_SHA256
    return ast.parse(json.loads(raw)["functions"]["_native_managed_liveness"]).body[0]


def without_dirty_row_schedule(function):
    """Bind the exact fallback and restore only the new scheduling region."""
    result = copy.deepcopy(function)
    old = reference_function()
    first = next(i for i, node in enumerate(result.body)
                 if isinstance(node, ast.FunctionDef) and node.name == "cfg_arena_is_plain")
    last = next(i for i, node in enumerate(result.body)
                if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "dirty_rows_enabled")
    original_loop = next(node for node in old.body
                         if isinstance(node, ast.While) and ast.unparse(node.test) == "changed")
    branch = result.body[last]
    assert len(branch.orelse) == 1
    assert ast.dump(branch.orelse[0]) == ast.dump(original_loop)
    old_start = next(i for i, node in enumerate(old.body)
                     if isinstance(node, ast.Assign) and ast.unparse(node) == "changed = True")
    old_end = old.body.index(original_loop)
    result.body[first:last + 1] = copy.deepcopy(old.body[old_start:old_end + 1])
    return result


def without_plane_fold(node):
    """Undo only the three-plane allocation/lifetime transformation."""
    import copy
    node = copy.deepcopy(node)
    body = []
    for statement in node.body:
        text = ast.unparse(statement)
        if text.startswith('live_out = CompilerIntArena('):
            body.append(ast.parse('live_in = CompilerIntArena(max(1, matrix_size))').body[0])
        if text == 'live_out.append_zeros(matrix_size)':
            body.append(ast.parse('live_in.append_zeros(matrix_size)').body[0])
        if (isinstance(statement, ast.If) and ast.unparse(statement.test) == 'dirty_rows_enabled'
                and len(statement.body) == 1 and ast.unparse(statement.body[0]) == 'live_in = uses'):
            assert [ast.unparse(n) for n in statement.orelse] == [
                'live_in = CompilerIntArena(max(1, matrix_size))', 'live_in.append_zeros(matrix_size)']
            continue
        if text == 'if not dirty_rows_enabled:\n    uses.close()':
            body.append(ast.parse('uses.close()').body[0])
        else:
            body.append(statement)
    node.body = body
    return node
