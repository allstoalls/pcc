"""Owned parser source-coordinate preservation, not a native debugger claim."""
import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend.self_backend_debug import debug_attachment
from pcc.backend.self_backend_indexed_codec import (
    decode_indexed_module_file,
    encode_indexed_module_file,
)
from pcc.backend.self_backend_parse import parse_self_backend_module


METADATA = r'''
!llvm.dbg.cu = !{!1}
!0 = !DIFile(filename: "debuggee.py", directory: "/tmp/source")
!1 = distinct !DICompileUnit(language: DW_LANG_Python, file: !0, producer: "pcc", isOptimized: false, runtimeVersion: 0, emissionKind: FullDebug)
!2 = distinct !DISubprogram(name: "add", file: !0, line: 1, unit: !1, scope: !0)
!3 = !DILocation(line: 2, column: 4, scope: !2)
!4 = !DILocation(line: 3, column: 4, scope: !2)
'''


def program(body=None, metadata=METADATA, header='!dbg !2'):
    body = body or 'entry:\n  %sum = add i64 %a, %b, !dbg !3\n  ret i64 %sum, !dbg !4\n'
    return ('target triple = "arm64-apple-darwin"\n'
            + 'define i64 @add(i64 %a, i64 %b) ' + header + ' {\n'
            + body + '}\n' + metadata)


def info(text):
    module = parse_self_backend_module(text)
    return module, module.type_context.debug_info.functions['add']


def test_original_instruction_and_return_locations_survive_owned_parse():
    module, function = info(program())
    assert function.name == 'add'
    assert function.source.filename == 'debuggee.py'
    assert function.source.directory == '/tmp/source'
    assert function.line == 1
    assert len(function.blocks) == 1
    block = function.blocks[0]
    assert block.name == 'entry'
    assert [(p.line, p.column) for p in block.instructions] == [(2, 4)]
    assert (block.terminator.line, block.terminator.column) == (3, 4)
    assert block.instructions[0].source is function.source
    assert module.functions[0].indexed_kernel is not None


def test_lexical_file_discriminator_and_original_utf8_escape():
    metadata = METADATA.replace('scope: !2)', 'scope: !7)') + r'''
!5 = !DIFile(filename: "caf\C3\A9.py", directory: "/tmp/other")
!6 = distinct !DILexicalBlock(scope: !2, file: !0, line: 2, column: 4)
!7 = !DILexicalBlockFile(scope: !6, file: !5, discriminator: 9)
!8 = !DIExpression(DW_OP_deref)
'''
    module, function = info(program(metadata=metadata))
    location = function.blocks[0].instructions[0]
    assert (location.source.filename, location.source.directory) == ('café.py', '/tmp/other')
    assert location.discriminator == 9
    assert location.scope == 7
    assert module.type_context.debug_info.nodes[8].fields == {'$raw': 'DW_OP_deref'}


def test_reachable_block_remapping_does_not_move_source_lines():
    _, function = info(program('''entry:
  br i1 true, label %live, label %dead, !dbg !3
dead:
  ret i64 8, !dbg !4
live:
  %sum = add i64 %a, %b, !dbg !3
  ret i64 %sum, !dbg !4
'''))
    assert [b.name for b in function.blocks] == ['entry', 'live']
    assert function.blocks[0].instructions == []
    assert function.blocks[0].terminator.line == 2
    assert function.blocks[1].instructions[0].line == 2
    assert function.blocks[1].terminator.line == 3


def test_multiline_switch_location_stays_with_terminator():
    _, function = info(program('''entry:
  switch i64 %a, label %other [
    i64 0, label %zero
  ], !dbg !3
zero:
  ret i64 0, !dbg !4
other:
  ret i64 1, !dbg !4
'''))
    assert function.blocks[0].instructions == []
    assert function.blocks[0].terminator.line == 2


def test_phi_source_attachment_is_not_an_emitted_instruction():
    _, function = info(program('''entry:
  %which = icmp eq i64 %a, 0, !dbg !3
  br i1 %which, label %left, label %right, !dbg !3
left:
  br label %join, !dbg !4
right:
  br label %join, !dbg !4
join:
  %value = phi i64 [ %a, %left ], [ %b, %right ], !dbg !3
  ret i64 %value, !dbg !4
'''))
    join = next(b for b in function.blocks if b.name == 'join')
    assert join.instructions == []
    assert join.terminator.line == 3


@pytest.mark.parametrize('text', [
    'call void @f(ptr c"literal !dbg !4")',
    'ret i64 0 ; !dbg !4 is a comment',
    'call void @"quoted!dbg"()',
])
def test_quoted_or_commented_debug_text_is_never_removed(text):
    assert debug_attachment(text) == (text, -1)


def test_only_actual_debug_attachment_is_removed():
    assert debug_attachment('ret i64 1, !dbg !4') == ('ret i64 1', 4)
    assert debug_attachment('define i64 @f() !dbg !2 {') == ('define i64 @f() {', 2)
    assert debug_attachment('ret i64 1, !dbg !4, !prof !9') == ('ret i64 1, !prof !9', 4)
    with pytest.raises(BackendUnavailable, match='duplicate source debug attachment'):
        debug_attachment('ret i64 1, !dbg !4, !dbg !4')


@pytest.mark.parametrize(('before', 'after', 'diagnostic'), [
    ('scope: !2)', 'scope: !99)', 'missing source debug scope'),
    ('file: !0, line: 1', 'file: !99, line: 1', 'requires DIFile'),
    ('line: 2, column: 4', 'line: -2, column: 4', 'nonnegative integer'),
    ('column: 4, scope: !2)', 'column: 4, scope: !2, inlinedAt: !3)', 'inline call chains'),
    ('filename: "debuggee.py"', 'filename: "bad\\00.py"', 'contains NUL'),
])
def test_malformed_or_unproved_metadata_keeps_named_boundaries(before, after, diagnostic):
    with pytest.raises(BackendUnavailable, match=diagnostic):
        parse_self_backend_module(program(metadata=METADATA.replace(before, after)))


def test_cyclic_lexical_scope_is_rejected():
    metadata = METADATA.replace('scope: !2)', 'scope: !5)') + '!5 = !DILexicalBlock(scope: !5, file: !0)\n'
    with pytest.raises(BackendUnavailable, match='cyclic source debug scope'):
        parse_self_backend_module(program(metadata=metadata))


def test_same_cached_location_cannot_belong_to_another_function():
    extra = '''define i64 @other() !dbg !5 {
entry:
  ret i64 1, !dbg !3
}
!5 = distinct !DISubprogram(name: "other", file: !0, line: 8, unit: !1, scope: !0)
'''
    with pytest.raises(BackendUnavailable, match='belongs to another function'):
        parse_self_backend_module(program() + extra)


def test_source_coordinates_are_not_dropped_on_other_native_targets():
    text = program().replace('arm64-apple-darwin', 'x86_64-unknown-linux-gnu')
    with pytest.raises(BackendUnavailable, match='DWARF emission is not connected for this target'):
        emit_owned_object(text, 'x86_64-unknown-linux-gnu')


def test_normal_owned_output_does_not_gain_or_lose_metadata():
    text = program('entry:\n  %sum = add i64 %a, %b\n  ret i64 %sum\n', metadata='', header='')
    module = parse_self_backend_module(text)
    assert module.type_context.debug_info is None
    # An unused file record does not assert that executable code is debugged.
    assert emit_owned_object(text, 'arm64-apple-darwin') == emit_owned_object(text + METADATA, 'arm64-apple-darwin')


def test_actual_python_frontend_locations_reach_owned_parser(tmp_path):
    # The existing CLI fixture produces actual frontend metadata without
    # passing through clang or llvmlite's object emitter.
    from tests.python.test_py_debug_info import _compile_ll
    module = parse_self_backend_module(_compile_ll(tmp_path, debug=True))
    assert module.type_context.debug_info is not None
    found = [f for f in module.type_context.debug_info.functions.values() if f.name == 'add']
    assert len(found) == 1
    function = found[0]
    lines = {location.line for block in function.blocks
             for location in [*block.instructions, block.terminator] if location is not None}
    assert {2, 3} <= lines
    assert function.source.filename == 'dbg_on.py'
    assert function.source.directory == str(tmp_path)
    columns = {location.column for block in function.blocks
               for location in [*block.instructions, block.terminator]
               if location is not None and location.line in (2, 3)}
    # py_lift currently supplies line-only spans with column 1. Preserve
    # that original value; this is not a claim of precise token columns.
    assert columns == {1}


def test_original_module_span_is_not_replaced_by_import_name():
    from pcc.frontends.python.codegen.debug_info_lowering import debug_source_path
    from pcc.frontends.python.py_lift import parse_and_lift
    module = parse_and_lift("def f():\n    return 1\n", "/tmp/pkg/actual.py", "logical.module")
    assert debug_source_path(module) == "/tmp/pkg/actual.py"
    assert debug_source_path(module, "/chosen/actual.py") == "/chosen/actual.py"


def test_innermost_discriminator_survives_nested_scope_files():
    metadata = METADATA.replace('scope: !2)', 'scope: !7)') + '\n'.join([
        '!5 = !DILexicalBlockFile(scope: !2, file: !0, discriminator: 3)',
        '!7 = !DILexicalBlockFile(scope: !5, file: !0, discriminator: 9)',
    ])
    _, function = info(program(metadata=metadata))
    assert function.blocks[0].instructions[0].discriminator == 9


@pytest.mark.parametrize("body", [None, """entry:
  switch i64 %a, label %other [
    i64 0, label %zero
  ], !dbg !3
zero:
  ret i64 0, !dbg !4
other:
  ret i64 1, !dbg !4
"""])
def test_indexed_debug_transport_retains_original_graph_and_dense_bindings(tmp_path, body):
    original, _ = info(program(body))
    first, second = tmp_path / 'first.pidx', tmp_path / 'second.pidx'
    encode_indexed_module_file(str(first), original)
    restored = decode_indexed_module_file(str(first))
    encode_indexed_module_file(str(second), restored)
    assert first.read_bytes() == second.read_bytes()
    before, after = original.type_context.debug_info, restored.type_context.debug_info
    assert after.nodes == before.nodes
    assert after.functions == before.functions
    assert after.locations == before.locations
    assert all(f.type_context is restored.type_context for f in restored.functions)
    assert after.functions['add'].blocks[0].terminator.identity in (3, 4)


def test_invalid_indexed_debug_binding_preserves_previous_output(tmp_path):
    original, function = info(program())
    function.blocks.clear()
    destination = tmp_path / 'existing.pidx'
    destination.write_bytes(b'previous accepted output')
    with pytest.raises(ValueError, match='block count differs'):
        encode_indexed_module_file(str(destination), original)
    assert destination.read_bytes() == b'previous accepted output'


@pytest.mark.parametrize('mutation, diagnostic', [
    ('version', 'version mismatch'),
    ('profile', 'profile mismatch'),
    ('instruction-count', 'block differs'),
    ('block-name', 'block differs'),
    ('function-symbol', 'function binding'),
    ('unknown-location', 'requires DILocation'),
    ('boolean-identity', 'invalid metadata identity'),
    ('duplicate-node', 'duplicated'),
])
def test_indexed_debug_reader_rejects_wrong_graph_or_kernel_binding(tmp_path, mutation, diagnostic):
    import json
    from pcc.backend.self_backend_indexed_codec import _MAGIC
    original, _ = info(program())
    path = tmp_path / 'source.pidx'
    encode_indexed_module_file(str(path), original)
    with path.open('rb') as stream:
        assert stream.readline() == _MAGIC
        size = int(stream.readline())
        header = json.loads(stream.read(size))
        payload = stream.read()
    source = header['source_debug']
    binding = source['functions'][0]
    if mutation == 'version':
        header['schema'] = 'pcc.self-backend.indexed-module.v1'
    elif mutation == 'profile':
        source['profile'] = 'unknown'
    elif mutation == 'instruction-count':
        binding[2][0][1].append(-1)
    elif mutation == 'block-name':
        binding[2][0][0] = 'wrong'
    elif mutation == 'function-symbol':
        binding[0] = 'another'
    elif mutation == 'unknown-location':
        binding[2][0][1][0] = 999
    elif mutation == 'boolean-identity':
        binding[2][0][1][0] = True
    elif mutation == 'duplicate-node':
        source['nodes'].append(source['nodes'][0])
    encoded = json.dumps(header).encode()
    path.write_bytes(_MAGIC + str(len(encoded)).encode() + b'\n' + encoded + payload)
    with pytest.raises(ValueError, match=diagnostic):
        decode_indexed_module_file(str(path))


def test_in_process_host_selection_binds_debug_backend_module():
    from pcc.frontends.python.pipeline_self_backend_emit import emit_in_process
    from pcc.backend.self_backend_parse import parse_self_backend_target_triple
    from pcc.backend.self_backend_target_match import is_aarch64_darwin_triple
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm
    text = program()
    text = text.replace('arm64-apple-darwin', 'unknown-unknown-unknown')
    result = emit_in_process(text, parse_target_triple=parse_self_backend_target_triple,
                             host_target_triple=lambda: 'arm64-apple-darwin',
                             target_supported=is_aarch64_darwin_triple,
                             emit_asm=emit_aarch64_darwin_asm)
    assert result[0] == 'self-aarch64-darwin-v0'
    assert '.section __DWARF,__debug_line,regular,debug' in result[1]


def test_in_process_explicit_target_does_not_inherit_host():
    from pcc.frontends.python.pipeline_self_backend_emit import emit_in_process
    from pcc.backend.self_backend_parse import parse_self_backend_target_triple
    from pcc.backend.self_backend_target_match import is_aarch64_darwin_triple
    text = program()
    def no_host():
        raise AssertionError('explicit source target must not be replaced')
    result = emit_in_process(text, parse_target_triple=parse_self_backend_target_triple,
                             host_target_triple=no_host, target_supported=is_aarch64_darwin_triple,
                             emit_asm=lambda source, optimize: source)
    assert result == ('self-aarch64-darwin-v0', text)


def test_in_process_missing_target_remains_refused():
    from pcc.frontends.python.pipeline_self_backend_emit import emit_in_process
    from pcc.backend.self_backend_parse import parse_self_backend_target_triple
    text = program().split('\n', 1)[1]
    def unexpected(*args):
        raise AssertionError('missing target must fail before selection/emission')
    with pytest.raises(BackendUnavailable, match='requires a target triple'):
        emit_in_process(text, parse_target_triple=parse_self_backend_target_triple,
                        host_target_triple=unexpected, target_supported=unexpected,
                        emit_asm=unexpected)
