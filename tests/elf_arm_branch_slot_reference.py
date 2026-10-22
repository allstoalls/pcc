"""Immediate pre-change ELF owner for tiny host-only branch-slot controls.

Keep the complete owner hash and exact inverse edits independent of the
candidate. No linker or compiler runs while this module is imported.
"""

import __future__
import ast
import hashlib
from pathlib import Path

from tests.elf_x86_packed_application_reference import without_x86_packed_application


BASE_OWNER_SHA256 = "411959c27c76d984d2ccff427fb821bfa7bbd003bcbbc56c1ecb6de2589eeeaf"
OWNER_REVERSE_EDITS = tuple(
    (
        indent + "slots = branch_slots.get((object_index, section_index))\n"
        + indent + "if slots is None:\n"
        + indent + "    slots = {}\n"
        + indent + "    branch_slots[(object_index, section_index)] = slots\n",
        indent + "slots = branch_slots.setdefault((object_index, section_index), {})\n",
    )
    for indent in (" " * 28, " " * 20)
)


def without_arm_branch_slot_defaults(source):
    source = without_x86_packed_application(source)
    for changed, original in OWNER_REVERSE_EDITS:
        assert source.count(changed) == 1
        source = source.replace(changed, original)
    assert hashlib.sha256(source.encode()).hexdigest() == BASE_OWNER_SHA256
    return source


def original_linker(elf):
    owner = Path(__file__).resolve().parents[1] / "pcc/backend/elf_x86_64.py"
    assert Path(elf.__file__).resolve() == owner.resolve()
    source = without_arm_branch_slot_defaults(owner.read_text())
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_link_static_executable_image")
    body = "".join(source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno])
    namespace = dict(vars(elf))
    exec(compile(body, "<frozen eager ARM branch-slot defaults>", "exec",
                 flags=__future__.annotations.compiler_flag, dont_inherit=True),
         namespace, namespace)
    return namespace[function.name]
