import pytest

from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend import self_backend_precise_stackmaps as stackmaps
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from tests.python.test_x86_64_emission_lifetime import (
    ROOTED_PROGRAM, _assert_tables_closed, _emit, _indexed_module,
)


@pytest.mark.parametrize('packed', [False, True])
def test_finished_kernel_is_retired_before_next_stackmap_plan(tmp_path, monkeypatch, packed):
    monkeypatch.delenv('PCC_CODE_PROFILE', raising=False)
    monkeypatch.setenv('PCC_SELF_TARGET_PASSES', 'none')
    monkeypatch.setenv('PCC_SELF_TARGET_PASS_TRANSPORT', 'text')
    module = _indexed_module(tmp_path, ROOTED_PROGRAM)
    seen = []
    original = stackmaps.build_function_stack_map_plan

    def checked(function, globals_, **kwargs):
        for previous in seen:
            _assert_tables_closed(get_indexed_function_kernel(previous))
            assert not previous.value_slots
            assert not previous.value_slot_buckets
            assert not previous.alloca_slots
            assert not previous.alloca_slot_buckets
            assert not previous.value_types
        result = original(function, globals_, **kwargs)
        seen.append(function)
        return result

    monkeypatch.setattr(stackmaps, 'build_function_stack_map_plan', checked)
    if hasattr(emitter, 'build_function_stack_map_plan'):
        monkeypatch.setattr(emitter, 'build_function_stack_map_plan', checked)
    assembly, encoded = _emit(module, packed)
    assert seen == list(module.functions)
    assert len(seen) > 1
    assert assembly and encoded.startswith(b'\x7fELF')
