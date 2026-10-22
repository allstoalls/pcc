import gc
import weakref

import pytest

from pcc.backend import self_backend_ir as ir
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend import elf_x86_64 as elf


def test_retired_mapping_drops_cached_key_owners():
    class Key(str):
        pass
    key = Key('retired_lookup_key')
    reference = weakref.ref(key)
    mapping = {key: 123}
    assert ir.text_key_mapping_get(mapping, 'absent') is None
    assert id(mapping) in ir._TEXT_KEY_INDEX_CACHE
    del key
    ir.clear_text_key_mapping(mapping)
    assert not mapping
    assert id(mapping) not in ir._TEXT_KEY_INDEX_CACHE
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\v', '\f', '\x1c', '\x85', '\u2028'])
@pytest.mark.parametrize('chunk_size', [1, 7, 65536])
def test_line_separators_and_chunking_preserve_object_bytes(monkeypatch, separator, chunk_size):
    lines = ['.intel_syntax noprefix', '.text', '.globl probe', '.type probe, @function', 'probe:', '  mov eax, 42  ', '\tcall external', '  ret', '.size probe, .-probe']
    text = separator.join(lines) + separator
    monkeypatch.setattr(assembler, '_ASSEMBLY_LINE_CHUNK_CHARS', chunk_size)
    obj = assembler.assemble_file(text)
    assert obj.sections[0].data == bytes.fromhex('b8 2a 00 00 00 e8 00 00 00 00 c3')
    expected = elf.emit_relocatable(assembler.assemble_file('\n'.join(lines) + '\n'))
    assert elf.emit_relocatable(obj) == expected


def test_long_instruction_line_assembles_untruncated():
    line = 'mov' + ' ' * 65536 + 'eax, 42'
    text = '.intel_syntax noprefix\n.text\nprobe:\n' + line + '\nret\n'
    obj = assembler.assemble_file(text)
    assert obj.sections[0].data == bytes.fromhex('b8 2a 00 00 00 c3')


@pytest.mark.parametrize('line', ['unsupported eax', 'mov eax', 'mov [rax], 1', 'jmp'])
def test_invalid_instruction_fails_closed(line):
    text = '.intel_syntax noprefix\n.text\nprobe:\n  nop\n  ' + line + '\n  ret\n'
    with pytest.raises(assembler.X86EncodeError):
        assembler.assemble_file(text)
