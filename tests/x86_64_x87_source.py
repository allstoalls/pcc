"""Source-derived control for the two x87 opcode tables and their real reads.

Only the unchanged encoder prefix through the two x87 returns is included.
The full encoder and assembly/error-order differentials have separate tests.
No implementation of a changed branch is replaced with a test imitation.
The native program also executes the source-derived operand-name predicate;
that addition does not claim native execution of the complete operand parser.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.x86_64_operand_whitespace import native_whitespace_source

PRODUCTION_SOURCE = (
    Path(__file__).resolve().parents[1] / "pcc/backend/x86_64_encode.py"
)
X87_CASES = (
    ("faddp", b"\xde\xc1"), ("fsubp", b"\xde\xe9"),
    ("fmulp", b"\xde\xc9"), ("fdivp", b"\xde\xf9"),
    ("fchs", b"\xd9\xe0"), ("fabs", b"\xd9\xe1"),
    ("fsqrt", b"\xd9\xfa"), ("fprem", b"\xd9\xf8"),
    ("frndint", b"\xd9\xfc"), ("fldz", b"\xd9\xee"),
    ("fxch st(1)", b"\xd9\xc9"), ("fld st(0)", b"\xd9\xc0"),
    ("fstp st(0)", b"\xdd\xd8"), ("fstp st(1)", b"\xdd\xd9"),
    ("fucomip st(0), st(1)", b"\xdf\xe9"), ("fnstsw ax", b"\xdf\xe0"),
)


def x87_provider_source() -> str:
    """Copy exact production declarations, ABI and complete changed branches."""
    source = PRODUCTION_SOURCE.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    nodes = ast.parse(source, str(PRODUCTION_SOURCE)).body
    definitions = {
        node.name: node for node in nodes
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    tables = [
        node for node in nodes if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name)
                and target.id in ("_X87_FIXED", "_X87_STACK")
                for target in node.targets)
    ]
    assert len(tables) == 2
    assert [node.targets[0].id for node in tables] == ["_X87_FIXED", "_X87_STACK"]
    assert all(len(node.targets) == 1 and isinstance(node.value, ast.Dict)
               for node in tables)
    assert [len(node.value.keys) for node in tables] == [10, 6]

    def segment(node):
        first = min([node.lineno] + [item.lineno for item in
                                    getattr(node, "decorator_list", ())])
        return "".join(lines[first - 1:node.end_lineno])

    leaf = definitions["_encode_instruction_uncached"]
    boundaries = [
        node for node in leaf.body if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "mnemonic"
        and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "lock"
    ]
    assert len(boundaries) == 1, "the real post-x87 boundary changed"
    boundary = boundaries[0]
    prefix = "".join(lines[leaf.lineno - 1:boundary.lineno - 1])
    # The extracted function must still contain the actual two complete
    # membership/return branches, with no per-call table reconstruction.
    prefix_tree = ast.parse(prefix)
    references = [node.id for node in ast.walk(prefix_tree)
                  if isinstance(node, ast.Name)
                  and node.id in ("_X87_FIXED", "_X87_STACK")]
    assert references.count("_X87_FIXED") == 2
    assert references.count("_X87_STACK") == 2
    assert not any(isinstance(node, ast.Dict) for node in ast.walk(prefix_tree))
    table_returns = [
        node for node in ast.walk(prefix_tree) if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "EncodedInstruction"
        and node.value.args and isinstance(node.value.args[0], ast.Subscript)
        and isinstance(node.value.args[0].value, ast.Name)
        and node.value.args[0].value.id in ("_X87_FIXED", "_X87_STACK")
    ]
    assert len(table_returns) == 2
    prefix += (
        '    raise AssertionError("x87 source control reached omitted encoder path")\n'
    )
    prelude = (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n\n"
        "# Annotation-only owner: this control never creates or uses a cache.\n"
        "class _OperandParseCache:\n"
        "    pass\n"
    )
    copied = [segment(definitions[name]) for name in (
        "X86EncodeError", "EncodedRelocation", "EncodedInstruction",
    )]
    return "\n\n".join((prelude, *copied, *(segment(node) for node in tables), prefix))


def x87_native_program() -> str:
    """Exercise cold global ownership and retained results with real x87 reads."""
    checks = "".join(
        "    check(" + repr(line) + ", " + repr(expected) + ")\n"
        for line, expected in X87_CASES
    )
    return x87_provider_source() + native_whitespace_source() + '''
import gc

def check(line: str, expected: bytes):
    first = _encode_instruction_uncached(line, pc=0, labels={}, section_name=".text")
    second = _encode_instruction_uncached(line, pc=137, labels={}, section_name=".data")
    assert first.code == expected and second.code == expected
    assert first.relocations == () and second.relocations == ()
    assert first is not second

def main():
    # No test-held reference has read either table before this collection.
    # The production module bindings must own both dicts and their bytes.
    gc.collect()
    gc.collect()
    assert len(_X87_FIXED) == 10 and len(_X87_STACK) == 6
    fixed = _encode_instruction_uncached("faddp", pc=0, labels={}, section_name=".text")
    stack = _encode_instruction_uncached("fxch st(1)", pc=31, labels={}, section_name=".data")
''' + checks + '''    gc.collect()
    assert fixed.code == b"\\xde\\xc1" and fixed.relocations == ()
    assert stack.code == b"\\xd9\\xc9" and stack.relocations == ()
    check("  FADDP   ", b"\\xde\\xc1")
    check("  FXCH st(1)  ", b"\\xd9\\xc9")
    rejected = False
    try:
        _encode_instruction_uncached("faddp rax", pc=0, labels={}, section_name=".text")
    except X86EncodeError as error:
        assert str(error) == "faddp takes no operands in the owned dialect"
        rejected = True
    assert rejected
    gc.collect()
    check("faddp", b"\\xde\\xc1")
    check("fxch st(1)", b"\\xd9\\xc9")
    check_operand_whitespace()
    gc.collect()
    check_operand_whitespace()
    print("X87_TABLE_GLOBALS_OK")

main()
'''
