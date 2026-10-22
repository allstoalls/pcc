"""Restore the exact pre-x86-classification owner without repinning old proofs."""

import __future__
import ast
import hashlib
from pathlib import Path

from tests.elf_arm_branch_slot_reference import without_arm_branch_slot_defaults


BASE_OWNER_SHA256 = "7b204f9eb3af55fdb3e1f2ab8f72e8a5cf965ba9b9ea0c845b541a85f09d212c"
OWNER_REVERSE_EDITS = (
    (
        'def _x86_packed_classification_rows(relocations):\n'
        '    """Keep the decoder\'s generator owner without constructing public records."""\n'
        '    for position in range(relocations.offset, relocations.offset + relocations.size, _RELA.size):\n'
        '        _offset, info, addend = _RELA.unpack_from(relocations.payload, position)\n'
        '        yield info >> 32, info & 0xFFFFFFFF, addend\n'
        '\n\n',
        '',
    ),
    (
        '        elif (type(machine) is int and machine == EM_X86_64\n'
        '                and type(relocations) is _PackedElfRelocations\n'
        '                and type(getattr(relocations, "payload", None)) is bytes\n'
        '                and type(relocations.offset) is int and type(relocations.size) is int\n'
        '                and type(_RELA) is struct.Struct and _RELA.format == "<QQq"):\n'
        '            # Keep a generator as the decoder-error owner, including if a\n'
        '            # symbol callback changes a later payload. Body errors release it.\n'
        '            relocation_sources = [relocations]\n'
        '            del relocations\n'
        '            for symbol_index, relocation_type, addend in _x86_packed_classification_rows(relocation_sources.pop()):\n'
        '                # Match assignment of the first projected row before callbacks.\n'
        '                relocation = None\n'
        '                if relocation_type not in got_reloc_types:\n'
        '                    continue\n'
        '                identity = _symbol_identity(objects, definitions, object_index, symbol_index)\n'
        '                kind = "tpoff" if relocation_type == R_X86_64_GOTTPOFF else "address"\n'
        '                key = (identity, kind)\n'
        '                if key not in got_seen:\n'
        '                    got_seen.add(key)\n'
        '                    got_identities.append(key)\n'
        '            del relocation_sources\n',
        '',
    ),
)


def without_x86_packed_classification(source):
    source = without_arm_branch_slot_defaults(source)
    for changed, original in reversed(OWNER_REVERSE_EDITS):
        assert source.count(changed) == 1
        source = source.replace(changed, original)
    assert hashlib.sha256(source.encode()).hexdigest() == BASE_OWNER_SHA256
    return source


def original_linker(elf):
    root = Path(__file__).resolve().parents[1]
    owner = root / "pcc/backend/elf_x86_64.py"
    assert Path(elf.__file__).resolve() == owner.resolve()
    source = owner.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert "\r" not in source
    source = without_x86_packed_classification(source)
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_link_static_executable_image"]
    assert len(functions) == 1
    function = functions[0]
    body = "".join(source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno])
    namespace = dict(vars(elf))
    exec(compile(body, "<frozen pre-x86 packed classification linker>", "exec",
                 flags=__future__.annotations.compiler_flag, dont_inherit=True),
         namespace, namespace)
    return namespace[function.name]
