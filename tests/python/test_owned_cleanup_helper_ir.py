"""Actual helper ABI, caller cleanup shape and precise root-state verification."""
from pathlib import Path
import re

import pytest

from pcc.backend.self_backend_call_flags import classify_call_flags
from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_modules
from tests.owned_ir_validation import verify_ir_text
from tests.python.test_slot_call_cleanup_reuse import setup_roots, IdentityOnly
from tests.python.test_slot_call_lexical_roots import _emit

ROOT = Path(__file__).resolve().parents[2]
ROOT_HELPER = 'py_cleanup_one_root_preserving_exception'
TWO_ROOT_HELPER = 'py_cleanup_two_roots_preserving_exception'
LEASE_HELPER = 'py_cleanup_one_lease_preserving_exception'


def verify_stackmaps(text):
    verify_ir_text(text)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)


@pytest.mark.parametrize('threads', ['0', '1'])
def test_actual_runtime_helper_abi_and_synchronous_body(tmp_path, monkeypatch, threads):
    monkeypatch.setenv('PCC_WITH_THREADS', threads)
    output = tmp_path / 'cleanup.ll'
    if threads == '0':
        with pytest.raises(ValueError, match='single-thread runtime was removed'):
            _compile_runtime_module('py_cleanup_runtime', str(ROOT / 'pcc/runtime/py/py_cleanup_runtime.py'),
                                    str(output), 'x86_64-unknown-linux-gnu')
        assert not output.exists()
        with pytest.raises(ValueError, match='single-thread runtime kernel was removed'):
            runtime_modules(str(ROOT / 'pcc/runtime'), 'x86_64-unknown-linux-gnu', threads=False)
        assert __import__('os').environ['PCC_WITH_THREADS'] == threads
        return
    _compile_runtime_module('py_cleanup_runtime', str(ROOT / 'pcc/runtime/py/py_cleanup_runtime.py'),
                            str(output), 'x86_64-unknown-linux-gnu')
    text = output.read_text()
    verify_stackmaps(text)
    for name, signature, disposal in (
        (ROOT_HELPER, r'ptr %slot', ['pcc_gc_store_root']),
        (TWO_ROOT_HELPER, r'ptr %first, ptr %second', ['pcc_gc_store_root'] * 2),
        (LEASE_HELPER, r'ptr %slot, i64 %acquired', ['pcc_gc_foreign_lease_release']),
    ):
        match = re.search(r'^define[^\n]*void @' + name + r'\(' + signature + r'\)[^\n]*\{\n(.*?)^}', text, re.M | re.S)
        assert match
        body = match.group(1)
        calls = re.findall(r'\bcall\b[^\n]*?@([\w.]+)\(', body)
        assert calls == ['pcc_gc_frame_enter_lifo', 'py_tls_exc_swap_slot'] + disposal + [
            'py_clear_exception', 'py_tls_exc_swap_slot', 'pcc_gc_frame_leave_lifo',
        ]
        if name == TWO_ROOT_HELPER:
            assert re.findall(r'@pcc_gc_store_root\(ptr (%\w+), ptr null\)', body) == [
                '%second', '%first',
            ]
            assert not re.search(r'load ptr, ptr %(?:first|second)\b', body)
        assert body.index('store ptr null') < body.index('@pcc_gc_frame_enter_lifo')
        assert 'alloca [8 x i8]' in body
        assert 'br ' not in body and 'Py_' not in body
        assert 'pcc_thread_' not in body
        # Ordinary safepoint-capable call; never a hidden caller-frame transition.
        assert classify_call_flags(name, False, False) == 0
    assert 'i64 %acquired)' in text
    assert 'py_cleanup_runtime' in runtime_modules(str(ROOT / 'pcc/runtime'), 'x86_64-unknown-linux-gnu', threads=True)
    assert re.search(r'py_cleanup_runtime.o:.*\n\t[^\n]*PCC_IMPLICIT_THREAD_SAFEPOINTS=0', (ROOT / 'pcc/runtime/Makefile').read_text())
    assert __import__('os').environ['PCC_WITH_THREADS'] == threads


def test_exact_single_shapes_have_no_caller_exception_alloca():
    model, first, second, target = setup_roots()
    model._slot_call_cleanup_block((first,), target)
    assert model.events == [(ROOT_HELPER, (first,)), ('leave', first), ('branch', target)]
    model.events.clear()
    token = IdentityOnly()
    model._slot_call_cleanup_block((), target, ((second, token),))
    assert model.events == [(LEASE_HELPER, (second, token)), ('branch', target)]


def test_exact_two_roots_keep_reverse_caller_retirement_visible():
    model, first, second, target = setup_roots()
    model._slot_call_cleanup_block((first, second), target)
    assert model.events == [
        (TWO_ROOT_HELPER, (first, second)),
        ('leave', second), ('leave', first), ('branch', target),
    ]


@pytest.mark.parametrize('annotation,value,error_return', [
    ('int', '17', 'ret ptr null'),
    ('i64', '17', 'ret i64 0'),
    ('float', '1.25', 'ret double 0x0000000000000000'),
    ('None', 'None', 'ret void'),
])
def test_two_root_helper_preserves_exceptional_return_convention(annotation, value, error_return):
    _codegen, text = _emit(
        'from pcc import i64\n'
        'def probe(f, x) -> ' + annotation + ':\n'
        '    f(x)\n    return ' + value + '\n',
    )
    verify_stackmaps(text)
    body = re.search(r'^define[^\n]*@user_lexical_probe\([^\n]*\).*?^}', text, re.M | re.S)[0]
    blocks = dict(re.findall(r'^([\w.]+):\n(.*?)(?=^[\w.]+:|^})', body, re.M | re.S))
    assert '@' + TWO_ROOT_HELPER + '(' in body
    assert blocks['err.finish'].strip().endswith(error_return)


@pytest.mark.parametrize('shape', [
    'three-roots', 'multiple-leases', 'mixed', 'two-roots-with-lease',
    'flagged', 'non-lifo', 'flagged-first', 'flagged-second',
    'non-lifo-first', 'non-lifo-second',
])
def test_ineligible_shapes_keep_inline_tls_protocol(shape):
    model, first, second, target = setup_roots()
    roots, leases = (first,), ()
    if shape == 'three-roots':
        third = IdentityOnly()
        model._slot_call_root_records.append((third, None, True))
        roots = (first, second, third)
    if shape == 'multiple-leases': roots, leases = (), ((first, IdentityOnly()), (second, IdentityOnly()))
    if shape == 'mixed': leases = ((second, IdentityOnly()),)
    if shape == 'two-roots-with-lease': roots, leases = (first, second), ((second, IdentityOnly()),)
    if shape == 'flagged': model._slot_call_root_records[0] = (first, IdentityOnly(), True)
    if shape == 'non-lifo': model._slot_call_root_records[0] = (first, None, False)
    if shape in ('flagged-first', 'flagged-second', 'non-lifo-first', 'non-lifo-second'):
        roots = (first, second)
        index = int(shape.endswith('second'))
        model._slot_call_root_records[index] = (
            roots[index], IdentityOnly() if shape.startswith('flagged') else None,
            shape.startswith('flagged'),
        )
    model._slot_call_cleanup_block(roots, target, leases)
    calls = [event[0] for event in model.events]
    assert calls.count('swap') == 2
    assert not any(helper in calls for helper in (ROOT_HELPER, TWO_ROOT_HELPER, LEASE_HELPER))


@pytest.mark.parametrize('source', [
    'def probe(f, x):\n    if x:\n        return f(x)\n    return f(None)\n',
    'def probe(f, values):\n    for x in values:\n        try:\n            try:\n                f(x)\n            except TypeError:\n                f(None)\n        except ValueError:\n            pass\n',
    'def probe(f, x):\n    try:\n        yield f(x)\n    except ValueError:\n        yield None\n',
])
def test_real_caller_cleanup_root_provenance_and_stackmaps(source):
    _, text = _emit(source)
    verify_stackmaps(text)
    blocks = re.findall(r'^call.slot.cleanup[^:]*:\n(.*?)(?=^[\w.]+:|^})', text, re.M | re.S)
    assert blocks
    for block in blocks:
        if '@' + ROOT_HELPER + '(' in block:
            assert '@pcc_gc_frame_leave_lifo(' in block
            assert block.index('@' + ROOT_HELPER) < block.index('@pcc_gc_frame_leave_lifo')
            assert '@py_tls_exc_swap_slot(' not in block
        if '@' + TWO_ROOT_HELPER + '(' in block:
            owners = re.search(
                r'@' + TWO_ROOT_HELPER + r'\(ptr (%[\w.]+), ptr (%[\w.]+)\)', block,
            )
            assert owners
            aliases = dict(re.findall(r'(%[\w.]+) = bitcast ptr (%[\w.]+) to ptr', block))
            resolve = lambda value: aliases.get(value, value)
            leaves = re.findall(r'@pcc_gc_frame_leave_lifo\(ptr (%[\w.]+)\)', block)
            assert list(map(resolve, leaves)) == list(map(resolve, reversed(owners.groups())))
            assert block.index('@' + TWO_ROOT_HELPER) < block.index('@pcc_gc_frame_leave_lifo')
            assert '@py_tls_exc_swap_slot(' not in block
            assert '@pcc_gc_store_root(' not in block
        if '@' + LEASE_HELPER + '(' in block:
            assert '@pcc_gc_frame_leave_lifo(' not in block
            assert '@pcc_gc_store_root(' not in block
