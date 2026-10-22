"""Independent DWARF consumer checks for owned AArch64 object generation."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_obj import emit_object
from pcc.backend.macho_spec import parse_object
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_indexed_module, emit_aarch64_darwin_indexed_transport,
)
from pcc.backend.self_backend_dwarf import DebugFunctionPlan, _final_rows
from pcc.backend.self_backend_indexed_codec import (
    decode_indexed_module_file, encode_indexed_module_file,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from tests.python.test_owned_source_debug_info import METADATA, program


def _dump(tmp_path, data):
    tool = shutil.which('dwarfdump')
    if tool is None:
        pytest.skip('independent dwarfdump consumer unavailable')
    path = tmp_path / 'debug.o'
    path.write_bytes(data)
    verify = subprocess.run([tool, '--verify', str(path)], capture_output=True, text=True, timeout=15)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert 'No errors.' in verify.stdout
    return subprocess.check_output([tool, '--debug-info', '--debug-line', str(path)], text=True, timeout=15)


def _sections(data):
    obj = parse_object(data)
    return {(section['segname_str'], section['sectname_str']): data[section["offset"]:section["offset"] + section["size"]]
            for section in obj.sections()}


def _rows(dump):
    return [tuple(int(field, 16 if i == 0 else 10) for i, field in enumerate(match.groups()))
            for match in re.finditer(r'^0x([0-9a-f]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', dump, re.M)]


@pytest.mark.parametrize('structured', [True, False])
@pytest.mark.parametrize('indexed_cache', [True, False])
def test_native_dwarf_tracks_final_instruction_addresses_and_cache(tmp_path, structured, indexed_cache):
    module = parse_self_backend_module(program())
    if indexed_cache:
        cache = tmp_path / 'module.pidx'
        encode_indexed_module_file(str(cache), module)
        module = decode_indexed_module_file(str(cache))
    transport = emit_aarch64_darwin_indexed_transport(module, optimize=True, structured_instructions=structured)
    try:
        sections, undefined = transport.assemble_sections()
        data = emit_object(sections, undefined=undefined)
    finally:
        if transport.encoded_line_records is not None:
            transport.encoded_line_records.close()
    dump = _dump(tmp_path, data)
    assert 'DW_LANG_Python' in dump
    assert 'DW_AT_name\t("add")' in dump
    assert 'DW_AT_decl_file\t("/tmp/source/debuggee.py")' in dump
    rows = _rows(dump)
    assert [(row[1], row[2]) for row in rows] == [(0, 0), (2, 4), (3, 4), (3, 4)]
    # The line-2 row names the actual emitted add, not an IR instruction count.
    code = _sections(data)['__TEXT', '__text']
    address = rows[1][0]
    assert int.from_bytes(code[address:address + 4], 'little') & 0xFFE0FC00 == 0x8B000000
    assert rows[-1][0] == len(code)
    assert rows[0][0] == 0 < rows[1][0] < rows[2][0] < rows[-1][0]
    # stp fp,lr,[sp,#-16]!; mov fp,sp: debug leaf frames remain walkable.
    frame_start = 4 if code[:4] == bytes.fromhex('3f2303d5') else 0  # optional PACIASP
    assert code[frame_start:frame_start + 8] == bytes.fromhex('fd7bbfa9fd030091')


def test_text_and_structured_debug_objects_have_identical_code_and_dwarf(tmp_path):
    text = program()
    structured = emit_owned_object(text, 'arm64-apple-darwin')
    assembly = emit_aarch64_darwin_indexed_module(parse_self_backend_module(text), optimize=True)
    sections, undefined = assemble_file(assembly)
    rendered = emit_object(sections, undefined=undefined)
    assert _sections(structured) == _sections(rendered)
    assert _dump(tmp_path, rendered)


def test_multiple_functions_lexical_files_discriminators_and_sequence_ends(tmp_path):
    metadata = METADATA.replace('scope: !2)', 'scope: !7)') + r'''
!5 = !DIFile(filename: "caf\C3\A9.py", directory: "/tmp/other")
!6 = distinct !DILexicalBlock(scope: !2, file: !0, line: 2, column: 4)
!7 = !DILexicalBlockFile(scope: !6, file: !5, discriminator: 9)
!10 = distinct !DICompileUnit(language: DW_LANG_Python, file: !5, producer: "pcc", isOptimized: false, runtimeVersion: 0, emissionKind: FullDebug)
!11 = distinct !DISubprogram(name: "second", file: !5, line: 20, unit: !10, scope: !5)
!12 = !DILocation(line: 22, column: 3, scope: !11)
'''
    source = program(metadata=metadata) + '''
define i64 @second() !dbg !11 {
entry:
  ret i64 7, !dbg !12
}
'''
    dump = _dump(tmp_path, emit_owned_object(source, 'arm64-apple-darwin'))
    assert dump.count('DW_TAG_compile_unit') == 2
    assert dump.count('end_sequence') == 2
    assert '/tmp/other' in dump
    assert 'caf' in dump
    rows = _rows(dump)
    assert [(row[1], row[2], row[5]) for row in rows if row[1] == 2] == [(2, 4, 9)]
    first_end = max(row[0] for row in rows if row[1] == 3)
    assert any(row[0] >= first_end and row[1] == 22 and row[2] == 3 for row in rows)


def test_reachable_blocks_and_multiline_switch_debug_dwarf(tmp_path):
    source = program('''entry:
  switch i64 %a, label %other [
    i64 0, label %zero
  ], !dbg !3
zero:
  ret i64 0, !dbg !4
other:
  ret i64 1, !dbg !4
''')
    dump = _dump(tmp_path, emit_owned_object(source, 'arm64-apple-darwin'))
    assert {row[1] for row in _rows(dump)} >= {2, 3}
    assert dump.count('end_sequence') == 1


def test_actual_python_frontend_debug_object_is_consumed_by_lldb(tmp_path):
    from tests.python.test_py_debug_info import _compile_ll
    source = _compile_ll(tmp_path, debug=True)
    data = emit_owned_object(source, 'arm64-apple-darwin')
    dump = _dump(tmp_path, data)
    assert str(tmp_path) in dump
    assert 'dbg_on.py' in dump
    assert {row[1] for row in _rows(dump)} >= {2, 3}
    debugger = shutil.which('lldb')
    if debugger is None:
        pytest.skip('independent LLDB consumer unavailable')
    line_address = next(row[0] for row in _rows(dump) if row[1] == 2)
    result = subprocess.run([
        debugger, '-b', '-o', 'target create ' + str(tmp_path / 'debug.o'),
        '-o', 'breakpoint set --file dbg_on.py --line 2',
        '-o', 'breakpoint list',
        '-o', 'image lookup -v -a ' + hex(line_address),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'no locations' not in result.stdout
    assert re.search(r'Breakpoint 1:.*(?:address|locations)', result.stdout)
    assert "file = 'dbg_on.py', line = 2" in result.stdout
    assert 'dbg_on.py:2:1' in result.stdout
    assert 'LineEntry:' in result.stdout


def test_final_layout_refuses_missing_or_reordered_debug_labels():
    from pcc.backend import BackendUnavailable
    module = parse_self_backend_module(program())
    debug = module.type_context.debug_info
    plan = DebugFunctionPlan(debug, debug.functions['add'], '_add', 'end')
    marker = plan.mark(0, 0)[:-1]
    with pytest.raises(BackendUnavailable, match='absent from final text'):
        _final_rows(plan, {'_add': 0, 'end': 12})
    with pytest.raises(BackendUnavailable, match='outside its final function'):
        _final_rows(plan, {'_add': 0, 'end': 12, marker: 16})
