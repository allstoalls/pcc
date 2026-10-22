"""New multiplication members participate in the maintained runtime contract."""
from pathlib import Path

import pytest

from pcc.frontends.python import (
    owned_runtime_build,
)
from pcc.frontends.python.codegen import (
    runtime_abi,
)


ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize('target', (
    'x86_64-unknown-linux-gnu',
    'aarch64-unknown-linux-gnu',
    'arm64-apple-darwin',
    'x86_64-pc-windows-msvc',
))
@pytest.mark.parametrize('threads', (False, True))
def test_repetition_inventory_requires_both_members_in_every_runtime(target, threads):
    runtime = ROOT / 'pcc/runtime'
    names = owned_runtime_build.runtime_modules(str(runtime), target, threads)
    required = ('py_binary_repeat_runtime', 'freestanding_sequence_snapshot')
    assert len(names) == len(set(names))
    for name in required:
        assert names.count(name) == 1
        assert (runtime / 'py' / (name + '.py')).is_file()
    config = {'threads': threads, 'refcount': 'atomic'}
    manifest = {'target_triple': target, 'members': [
        {'member': name + '.o', 'runtime_build_config': dict(config)} for name in names
    ]}
    assert owned_runtime_build._manifest_matches_config(manifest, str(runtime), target, config)
    manifest['members'] = [member for member in manifest['members']
                           if member['member'] not in {name + '.o' for name in required}]
    assert not owned_runtime_build._manifest_matches_config(manifest, str(runtime), target, config)


def test_snapshot_kernel_remains_freestanding_and_semantic_entry_is_runtime_port():
    runtime = ROOT / 'pcc/runtime/py'
    assert '__pcc_freestanding__ = True' in (runtime / 'freestanding_sequence_snapshot.py').read_text()
    assert '__pcc_runtime_port__ = True' in (runtime / 'py_binary_repeat_runtime.py').read_text()


def test_snapshot_uses_the_existing_exact_split_root_copy_abi():
    check = runtime_abi.is_freestanding_gc_cross_object_runtime_import
    symbol = 'pcc_gc_root_copy_lease_prepare_locked'
    assert check(symbol, '(c_ptr,c_ptr,c_int64,c_ptr)', 'c_int64')
    assert not check(symbol, '(c_ptr,c_ptr,c_ptr)', 'c_int64')
    assert not check(symbol, '(c_ptr,c_ptr,c_ptr,c_ptr)', 'c_int64')
    assert not check(symbol, '(c_ptr,c_ptr,c_int64,c_ptr)', 'c_ptr')
    assert not check('pcc_gc_copy_unknown_pointer', '(c_ptr,c_ptr,c_ptr,c_ptr)', 'c_int64')


def test_retired_heap_copy_has_no_runtime_definition_or_import_admission():
    symbol = 'pcc_gc_copy_ptr_lease_commit_locked'
    assert symbol not in (ROOT / 'pcc/runtime/py/py_obj.py').read_text()
    assert symbol not in runtime_abi.RUNTIME_SIGNATURES
    assert symbol not in (ROOT / 'pcc/runtime/src/py_internal.h').read_text()
    assert symbol not in (ROOT / 'pcc/runtime/include/py_runtime.h').read_text()
    assert not runtime_abi.is_freestanding_gc_cross_object_runtime_import(
        symbol, '(c_ptr,c_ptr,c_ptr,c_ptr)', 'c_int64',
    )
