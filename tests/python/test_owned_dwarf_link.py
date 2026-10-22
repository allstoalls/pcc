"""Cross-object source/address checks through both owned Mach-O merge paths."""
from __future__ import annotations

import re
import shutil
import struct
import subprocess

import pytest

from pcc.backend import macho_host_prepare as host
from pcc.backend import native_object as native
from pcc.backend.macho_dwarf import DwarfLinkError, patch_dwarf_info
from pcc.backend.macho_link import LinkError, link_relocatable, link_relocatable_native
from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
from pcc.backend.self_backend_parse import parse_self_backend_module
from tests.python.test_macho_windowed_parity import _budget, _prepared_state, _write_pcos
from tests.python.test_owned_dwarf import _dump, _rows
from tests.python.test_owned_source_debug_info import program


def _object(symbol, filename, line):
    text = program().replace('@add(', '@' + symbol + '(')
    text = text.replace('name: "add"', 'name: "' + symbol + '"')
    text = text.replace('debuggee.py', filename)
    text = text.replace('line: 2,', 'line: ' + str(line) + ',')
    text = text.replace('line: 3,', 'line: ' + str(line + 1) + ',')
    transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(text))
    try:
        sections, undefined = transport.assemble_sections()
        return native.NativeObject.from_sections(sections, undefined=undefined)
    finally:
        if transport.encoded_line_records is not None:
            transport.encoded_line_records.close()


def _assert_sources_and_addresses(tmp_path, data, count=2):
    dump = _dump(tmp_path, data)
    assert dump.count('DW_TAG_compile_unit') == count
    assert dump.count('DW_AT_decl_file\t("/tmp/source/one.py")') == 1
    assert dump.count('DW_AT_decl_file\t("/tmp/source/two.py")') == 1
    rows = _rows(dump)
    from pcc.backend.macho_spec import parse_object
    symbols = {symbol['name']: symbol['n_value'] for symbol in parse_object(data).symbols()}
    first_address = next(row[0] for row in rows if row[1] == 2)
    relative = first_address - symbols['_first']
    second_address = symbols['_second'] + relative
    assert any(row[0] == second_address and row[1] == 22 for row in rows)
    if count == 3:
        assert dump.count('DW_AT_decl_file\t("/tmp/source/three.py")') == 1
        assert any(row[0] == symbols['_third'] + relative and row[1] == 42 for row in rows)
    debugger = shutil.which('lldb')
    if debugger is None:
        pytest.skip('LLDB unavailable')
    result = subprocess.run([
        debugger, '-b', '-o', 'target create ' + str(tmp_path / 'debug.o'),
        '-o', 'breakpoint set --file two.py --line 22',
        '-o', 'image lookup -v -a ' + hex(second_address),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no locations' not in result.stdout
    assert 'two.py:22:4' in result.stdout
    assert re.search(r'Breakpoint 1:.*address = 0x0*' + format(second_address, 'x'), result.stdout)


@pytest.mark.parametrize('kind', ['macho', 'native', 'packed'])
def test_debug_merge_rebases_sections_and_survives_partial_relink(tmp_path, kind):
    objects = [_object('first', 'one.py', 2), _object('second', 'two.py', 22)]
    if kind == 'macho':
        inputs = [obj.to_macho() for obj in objects]
    elif kind == 'packed':
        inputs = [native.decode_packed_native_object(native.encode_native_object(obj)) for obj in objects]
    else:
        inputs = objects
    merged = link_relocatable_native(inputs)
    _assert_sources_and_addresses(tmp_path, merged.to_macho())
    again = link_relocatable([merged.to_macho(), _object('third', 'three.py', 42).to_macho()])
    _assert_sources_and_addresses(tmp_path, again, 3)


@pytest.mark.parametrize('staged', [False, True])
def test_windowed_debug_merge_matches_ordinary_and_releases_budget(tmp_path, staged):
    objects = [_object('first', 'one.py', 2), _object('second', 'two.py', 22)]
    paths = _write_pcos(tmp_path, objects)
    expected = link_relocatable_native(objects, _source_view=True)
    budget = _budget()
    result = host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=staged)
    try:
        assert _prepared_state(result) == _prepared_state(expected)
        # The ordinary merge has the identical bytes/rows just compared.
        _assert_sources_and_addresses(tmp_path, link_relocatable(objects))
        assert budget.retained_bytes == 0
    finally:
        host.close_staged_source_rows(result)


def test_debug_merge_fails_on_out_of_range_line_offset_without_mutating_input():
    value = _object('first', 'one.py', 2)
    # Mutate only an independent Mach-O copy of the admitted object.
    from pcc.backend.macho_spec import parse_object
    original = value.to_macho()
    data = bytearray(original)
    info = next(section for section in parse_object(original).sections() if section['sectname_str'] == '__debug_info')
    # Read the actual inline CU fields, then corrupt stmt_list specifically.
    offset = info['offset'] + 12
    offset = data.index(0, offset) + 1 + 2  # producer, language
    offset = data.index(0, offset) + 1      # name
    offset = data.index(0, offset) + 1      # comp_dir
    struct.pack_into('<I', data, offset, 0xFFFFFF00)
    before = bytes(data)
    with pytest.raises(LinkError, match='outside input __debug_line'):
        link_relocatable([bytes(data)])
    assert bytes(data) == before
    assert value.to_macho() == original


@pytest.mark.parametrize('mutation, diagnostic', [
    ('version', 'requires DWARF4'), ('length', 'unit length'),
    ('abbrev', 'outside input __debug_abbrev'), ('code', 'unknown abbreviation'),
])
def test_debug_offset_reader_rejects_malformed_units(mutation, diagnostic):
    obj = _object('first', 'one.py', 2)
    data = obj.to_macho()
    from pcc.backend.macho_spec import parse_object
    obj_sections = parse_object(data).sections()
    payloads = {section['sectname_str']: data[section['offset']:section['offset'] + section['size']]
                for section in obj_sections if section['segname_str'] == '__DWARF'}
    payload = bytearray(payloads['__debug_info'])
    if mutation == 'version':
        struct.pack_into('<H', payload, 4, 5)
    elif mutation == 'length':
        struct.pack_into('<I', payload, 0, len(payload))
    elif mutation == 'abbrev':
        struct.pack_into('<I', payload, 6, len(payloads['__debug_abbrev']))
    else:
        payload[11] = 33
    with pytest.raises(DwarfLinkError, match=diagnostic):
        patch_dwarf_info(payload, payloads['__debug_abbrev'],
                         {name: 0 for name in payloads},
                         {name: len(raw) for name, raw in payloads.items()})
