from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pcc.backend import BackendUnavailable, self_backend_parse as parser
from pcc.backend.self_backend_module_symbols import prepare_module_symbols
from pcc.backend import self_backend_x86_64_linux_data as x86_data
from pcc.backend import self_backend_aarch64_darwin_data as arm_data


def module(first, second):
    return ('target triple = "x86_64-unknown-linux-gnu"\n'
            '%T = type { ' + first + ', ' + second + ' }\n'
            '@storage = global %T zeroinitializer\n'
            'define %T @identity(%T %value) {\nentry:\n  ret %T %value\n}\n')


A = module('i8', 'i64')
B = module('i16', 'i16')


def widths(context):
    return tuple(field.width for field in parser.parse_ir_type('%T', type_context=context).fields)


def test_interleaved_modules_keep_lazy_resolution_and_caches_owned():
    a = parser.parse_self_backend_module(A.replace('@storage = global %T zeroinitializer\n', '').replace('define %T @identity(%T %value) {\nentry:\n  ret %T %value\n}', 'define i32 @probe() {\nentry:\n  ret i32 0\n}'))
    assert '%T' not in a.type_context.named_types
    b = parser.parse_self_backend_module(B)
    assert widths(a.type_context) == (8, 64)
    assert widths(b.type_context) == (16, 16)
    a_type = parser.parse_ir_type('%T*', type_context=a.type_context)
    assert a_type is parser.parse_ir_type('%T*', type_context=a.type_context)
    assert a_type is not parser.parse_ir_type('%T*', type_context=b.type_context)
    with pytest.raises(BackendUnavailable, match='named LLVM type'):
        parser.parse_ir_type('%T')


@pytest.mark.parametrize('boundary', ['_parse_functions', '_resolve_named_type', '_canonical_leaf_type'])
def test_nested_module_parse_cannot_replace_outer_resolution(monkeypatch, boundary):
    original = getattr(parser, boundary)
    active = False
    entered = False

    def nested(*args, **kwargs):
        nonlocal active, entered
        if not active and not entered:
            active = entered = True
            try:
                b = parser.parse_self_backend_module(B)
                assert widths(b.type_context) == (16, 16)
            finally:
                active = False
        return original(*args, **kwargs)

    monkeypatch.setattr(parser, boundary, nested)
    a = parser.parse_self_backend_module(A)
    assert entered
    assert tuple(field.width for field in a.functions[0].ret_type.fields) == (8, 64)
    assert widths(a.type_context) == (8, 64)


def test_concurrent_module_parses_keep_distinct_type_contexts(monkeypatch):
    original = parser._parse_functions
    barrier = Barrier(2)

    def rendezvous(text, **kwargs):
        barrier.wait(timeout=5)
        return original(text, **kwargs)

    monkeypatch.setattr(parser, '_parse_functions', rendezvous)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = list(executor.map(parser.parse_self_backend_module, [A, B]))
    assert widths(a.type_context) == (8, 64)
    assert widths(b.type_context) == (16, 16)


def test_failed_resolution_is_confined_to_its_own_module():
    a = parser.parse_self_backend_module(A)
    b = parser.parse_self_backend_module(B.replace('%T = type { i16, i16 }', '%T = type { bfloat }').replace('@storage = global %T zeroinitializer\n', '').replace('define %T @identity(%T %value) {\nentry:\n  ret %T %value\n}', 'define i32 @probe() {\nentry:\n  ret i32 0\n}'))
    with pytest.raises(BackendUnavailable, match='bfloat'):
        parser.parse_ir_type('%T', type_context=b.type_context)
    assert not b.type_context.named_types
    assert widths(a.type_context) == (8, 64)


def test_late_constant_gep_uses_the_explicit_module_layout():
    a = parser.parse_self_backend_module(A)
    b = parser.parse_self_backend_module(B)
    expression = 'getelementptr (%T, ptr @storage, i32 0, i32 1)'
    assert parser.parse_constant_gep(expression, type_context=a.type_context) == ('storage', 8)
    assert parser.parse_constant_gep(expression, type_context=b.type_context) == ('storage', 2)


@pytest.mark.parametrize('emitter,target', [(x86_data, 'x86_64-unknown-linux-gnu'), (arm_data, 'arm64-apple-darwin')])
def test_late_backend_initializer_retains_its_module_context(emitter, target):
    a = parser.parse_self_backend_module(A)
    symbols = prepare_module_symbols(A, list(a.globals_), list(a.functions), target)
    parser.parse_self_backend_module(B)
    expression = 'getelementptr (%T, ptr @storage, i32 0, i32 1)'
    lines = emitter.emit_scalar_initializer(parser.parse_ir_type('ptr'), expression, 'address', symbols)
    assert any('8' in line and 'storage' in line for line in lines), lines


def test_recursive_forward_reference_is_private_to_module():
    a = parser.parse_self_backend_module('target triple = "x86_64-unknown-linux-gnu"\n%Node = type { i64, %Node* }\n')
    b = parser.parse_self_backend_module('target triple = "x86_64-unknown-linux-gnu"\n%Node = type { i8, %Node* }\n')
    node_a = parser.parse_ir_type('%Node', type_context=a.type_context)
    node_b = parser.parse_ir_type('%Node', type_context=b.type_context)
    assert node_a.fields[0].width == 64 and node_b.fields[0].width == 8
    assert node_a.fields[1].pointee.name == node_b.fields[1].pointee.name == '%Node'
    assert node_a.fields[1].pointee is not node_b.fields[1].pointee


def test_direct_indexed_module_resolves_its_own_named_signature(monkeypatch):
    from pcc.ir import ir
    from pcc.ir.direct_indexed_kernel import build_direct_indexed_module

    monkeypatch.setenv('PCC_DIRECT_INDEXED_KERNEL_CAPTURE', '1')
    context = ir.Context()
    record = context.get_identified_type('T')
    record.set_body([ir.IntType(8), ir.IntType(64)])
    source = ir.Module(name='direct', context=context)
    source.triple = 'x86_64-unknown-linux-gnu'
    function = ir.Function(source, ir.FunctionType(record, [record]), name='identity')
    ir.IRBuilder(function.append_basic_block('entry')).ret(function.args[0])
    parser.parse_self_backend_module(B)
    result = build_direct_indexed_module(source)
    assert widths(result.type_context) == (8, 64)
    assert tuple(field.width for field in result.functions[0].ret_type.fields) == (8, 64)
    assert result.functions[0].type_context is result.type_context


def test_indexed_sidecar_retains_lazy_declarations_for_late_materialization(tmp_path):
    from pcc.backend.self_backend_indexed_codec import (
        decode_indexed_module_file, encode_indexed_module_file,
    )

    text = ('target triple = "x86_64-unknown-linux-gnu"\n'
            '%T = type { i8, i64 }\n%Unused = type { x86_fp80 }\n'
            '@storage = global %T zeroinitializer\n'
            '@address = global ptr getelementptr (%T, ptr @storage, i32 0, i32 1)\n')
    a = parser.parse_self_backend_module(text)
    path = tmp_path / 'module.pidx'
    encode_indexed_module_file(str(path), a)
    parser.parse_self_backend_module(B)
    restored = decode_indexed_module_file(str(path))
    assert '%Unused' not in restored.type_context.named_types
    assert widths(restored.type_context) == (8, 64)
    symbols = prepare_module_symbols(text, list(restored.globals_), [], restored.triple)
    address = restored.globals_[1]
    lines = x86_data.emit_scalar_initializer(address.type, address.initializer, address.name, symbols)
    assert any('storage' in line and '8' in line for line in lines), lines
    assert all(item.type_context is restored.type_context for item in restored.globals_)


def test_call_signature_cache_cannot_hide_another_modules_unsupported_abi():
    supported = parser._parse_named_types('%T = type { i32 }\n')
    unsupported = parser._parse_named_types('%T = type { x86_fp80 }\n')
    assert parser._parse_call_signature('(%T)', type_context=supported) == (1, False)
    with pytest.raises(BackendUnavailable, match='x86_fp80'):
        parser._parse_call_signature('(%T)', type_context=unsupported)


def test_reinitializing_a_context_invalidates_its_abi_signature_cache():
    context = parser._parse_named_types('%T = type { i32 }\n')
    assert parser._parse_call_signature('(%T)', type_context=context) == (1, False)
    parser._parse_named_types('%T = type { x86_fp80 }\n', type_context=context)
    with pytest.raises(BackendUnavailable, match='x86_fp80'):
        parser._parse_call_signature('(%T)', type_context=context)
