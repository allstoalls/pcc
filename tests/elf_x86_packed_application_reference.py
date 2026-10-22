"""Exact immediate pre-x86-application owner for host differential controls.

This helper only removes the new specialization. Earlier reference transforms
can call it on the already restored owner without changing their pinned hashes.
It imports no production module and performs no link or compiler run on import.
"""

import __future__
import ast
import hashlib
from pathlib import Path


BASE_OWNER_SHA256 = '3b85ba9dde549bb6b2a1d272657a6efc02c58277d8059c7a77df120871c66929'
OWNER_REVERSE_EDITS = (('def _x86_packed_application_rows(relocations):\n    """Decode application fields without projecting a public relocation record."""\n    for position in range(relocations.offset, relocations.offset + relocations.size, _RELA.size):\n        offset, info, addend = _RELA.unpack_from(relocations.payload, position)\n        yield offset, info >> 32, info & 0xFFFFFFFF, addend\n\n\n', ''), ('        elif (type(machine) is int and machine == EM_X86_64\n                and type(relocations) is _PackedElfRelocations\n                and type(getattr(relocations, "payload", None)) is bytes\n                and type(relocations.offset) is int and type(relocations.size) is int\n                and type(_RELA) is struct.Struct and _RELA.format == "<QQq"):\n            # Keep decoding in its generator: a decoder exception retains its\n            # source, but an application exception must release the iterator.\n            relocation_sources = [relocations]\n            del relocations\n            for relocation_offset, symbol_index, relocation_type, addend in _x86_packed_application_rows(relocation_sources.pop()):\n                # As in the generic loop, decode before releasing a prior row.\n                relocation = None\n                if relocation_type == R_X86_64_NONE:\n                    continue\n                patch = placement.file_offset + relocation_offset\n                place = placement.address + relocation_offset\n                symbol_address, _symbol = resolve_symbol(object_index, symbol_index)\n                if relocation_type == R_X86_64_64:\n                    _write_int(image, patch, symbol_address + addend, 64, False)\n                elif relocation_type in (R_X86_64_PC32, R_X86_64_PLT32):\n                    _write_int(image, patch, symbol_address + addend - place, 32, True)\n                elif relocation_type == R_X86_64_32:\n                    _write_int(image, patch, symbol_address + addend, 32, False)\n                elif relocation_type == R_X86_64_32S:\n                    _write_int(image, patch, symbol_address + addend, 32, True)\n                elif relocation_type in (\n                    R_X86_64_GOTPCREL,\n                    R_X86_64_GOTPCRELX,\n                    R_X86_64_REX_GOTPCRELX,\n                    R_X86_64_GOTTPOFF,\n                ):\n                    identity = _symbol_identity(objects, definitions, object_index, symbol_index)\n                    kind = "tpoff" if relocation_type == R_X86_64_GOTTPOFF else "address"\n                    slot = got_address + got_offsets[(identity, kind)]\n                    _write_int(image, patch, slot + addend - place, 32, True)\n                elif relocation_type == R_X86_64_TPOFF32:\n                    value = tls_offset(object_index, symbol_index)\n                    _write_int(image, patch, value + addend, 32, True)\n                else:\n                    raise ElfError(f"relocation type {relocation_type} has no link rule")\n            del relocation_sources\n', ''))


def without_x86_packed_application(source):
    if hashlib.sha256(source.encode()).hexdigest() == BASE_OWNER_SHA256:
        return source
    for changed, original in reversed(OWNER_REVERSE_EDITS):
        assert source.count(changed) == 1
        source = source.replace(changed, original)
    assert hashlib.sha256(source.encode()).hexdigest() == BASE_OWNER_SHA256
    return source


def original_linker(elf):
    owner = Path(__file__).resolve().parents[1] / "pcc/backend/elf_x86_64.py"
    assert Path(elf.__file__).resolve() == owner.resolve()
    source = without_x86_packed_application(owner.read_text(encoding="utf-8"))
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_link_static_executable_image")
    body = "".join(source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno])
    namespace = dict(vars(elf))
    exec(compile(body, "<frozen pre-x86 packed application linker>", "exec",
                 flags=__future__.annotations.compiler_flag, dont_inherit=True),
         namespace, namespace)
    return namespace[function.name]
