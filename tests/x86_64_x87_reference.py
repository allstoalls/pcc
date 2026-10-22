"""Frozen 39256 x87 prefix; source-only helpers do not import PCC.

The old/new comparison deliberately shares the unchanged suffix. The prefix
is the exact pre-hoist source, not rebuilt from the new global dictionaries.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENCODER = ROOT / "pcc/backend/x86_64_encode.py"
PREIMAGE_PREFIX = r'''def _encode_instruction_uncached(
    line: str,
    *,
    pc: int,
    labels: dict[str, tuple[str, int]],
    section_name: str,
    operand_cache: _OperandParseCache | None = None,
) -> EncodedInstruction:
    """Encode one normalized Intel-syntax instruction at ``pc``."""
    stripped = line.strip()
    if not stripped:
        return EncodedInstruction(b"")
    mnemonic, _, rest = stripped.partition(" ")
    mnemonic = mnemonic.lower()
    # x87 register-stack operations have implicit operands.  Keep the owned
    # dialect finite: these exact spellings are emitted by the fp80 lowering.
    x87_fixed = {
        "faddp": b"\xde\xc1", "fsubp": b"\xde\xe9",
        "fmulp": b"\xde\xc9", "fdivp": b"\xde\xf9",
        "fchs": b"\xd9\xe0", "fabs": b"\xd9\xe1",
        "fsqrt": b"\xd9\xfa", "fprem": b"\xd9\xf8",
        "frndint": b"\xd9\xfc", "fldz": b"\xd9\xee",
    }
    if mnemonic in x87_fixed:
        if rest.strip():
            raise X86EncodeError(f"{mnemonic} takes no operands in the owned dialect")
        return EncodedInstruction(x87_fixed[mnemonic])
    x87_stack = {
        ("fxch", "st(1)"): b"\xd9\xc9",
        ("fld", "st(0)"): b"\xd9\xc0",
        ("fstp", "st(0)"): b"\xdd\xd8",
        ("fstp", "st(1)"): b"\xdd\xd9",
        ("fucomip", "st(0), st(1)"): b"\xdf\xe9",
        ("fnstsw", "ax"): b"\xdf\xe0",
    }
    if (mnemonic, rest.strip()) in x87_stack:
        return EncodedInstruction(x87_stack[(mnemonic, rest.strip())])
'''


def source_tree():
    return ast.parse(ENCODER.read_text(encoding="utf-8"))


def leaf(tree):
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == "_encode_instruction_uncached")


def prefix_length(function):
    return next(index for index, node in enumerate(function.body)
                if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "mnemonic"
                and any(isinstance(value, ast.Constant) and value.value == "lock"
                        for value in node.test.comparators))


def old_function(tree):
    function = copy.deepcopy(leaf(tree))
    prefix = ast.parse(PREIMAGE_PREFIX).body[0]
    function.body = prefix.body + function.body[prefix_length(function):]
    return function


def table_nodes(tree):
    result = [copy.deepcopy(node) for node in tree.body if isinstance(node, ast.Assign)
              and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in ("_X87_FIXED", "_X87_STACK")]
    assert [node.targets[0].id for node in result] == ["_X87_FIXED", "_X87_STACK"]
    return result


def old_encoder(namespace):
    """CI-only real preimage-prefix oracle, using unchanged production leaves."""
    scope = dict(namespace)
    function = old_function(source_tree())
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, "<frozen-x87-preimage>", "exec", flags=0, dont_inherit=True), scope)
    return scope["_encode_instruction_uncached"]
