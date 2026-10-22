import weakref

import pytest

from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from tests.python.test_x86_64_emission_lifetime import ROOTED_PROGRAM, TARGET, _indexed_module


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


def _input(tmp_path):
    plans = []
    assembly = emit_indexed_assembly(_indexed_module(tmp_path, ROOTED_PROGRAM), stack_map_plans_out=plans)
    return assembly, plans


def test_explicit_consumer_releases_plans_before_finishing_sections_and_preserves_bytes(tmp_path, monkeypatch):
    # Packing needs every final .text offset, so it follows the one encoding
    # pass; the plans must be gone before any section payload is finished.
    assembly, retained = _input(tmp_path)
    expected = encode_assembly_object(assembly, TARGET, stack_map_plans=retained)
    assert retained and all(plan.records for plan in retained)
    assembly, plans = _input(tmp_path)
    references = [weakref.ref(plan) for plan in plans]
    packed = False
    sections_after_pack = 0
    real_pack = maps.build_x86_64_stack_map_payload
    real_finish = assembler._finish_section

    def observe_pack(*args, **kwargs):
        nonlocal packed
        result = real_pack(*args, **kwargs)
        packed = True
        return result

    def observe_finish(*args, **kwargs):
        nonlocal sections_after_pack
        assert packed
        assert not plans
        assert all(reference() is None for reference in references)
        sections_after_pack += 1
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(maps, 'build_x86_64_stack_map_payload', observe_pack)
    monkeypatch.setattr(assembler, '_finish_section', observe_finish)
    actual = encode_assembly_object(assembly, TARGET, stack_map_plans=plans, consume_stack_map_plans=True)
    assert packed and sections_after_pack
    assert actual == expected


def test_consumption_keeps_plans_if_final_offset_validation_fails(tmp_path):
    assembly, plans = _input(tmp_path)
    original = tuple(plans)
    assembly = assembly.replace('.section .pcc_stackmaps,"a",@progbits', '')
    with pytest.raises(assembler.X86EncodeError, match='empty section marker'):
        encode_assembly_object(assembly, TARGET, stack_map_plans=plans, consume_stack_map_plans=True)
    assert tuple(plans) == original


def test_consuming_path_rejects_immutable_plan_owner(tmp_path):
    assembly, plans = _input(tmp_path)
    with pytest.raises(assembler.X86EncodeError, match='mutable plan list'):
        assembler.assemble_file_with_stack_maps(
            assembly, tuple(plans), function_symbol=emitter._asm_symbol,
            block_label=emitter._block_label, consume_stack_map_plans=True,
        )


def test_real_backend_packed_stack_map_object_matches_two_pass_driver(tmp_path, monkeypatch):
    from tests.python.test_x86_64_parse_buffer_publication import _two_pass

    assembly, plans = _input(tmp_path)
    actual = encode_assembly_object(assembly, TARGET, stack_map_plans=plans)
    monkeypatch.setattr(assembler, 'assemble_file_with_stack_maps',
                        _two_pass().assemble_file_with_stack_maps)
    assert encode_assembly_object(assembly, TARGET, stack_map_plans=plans) == actual


def test_real_windows_packed_stack_map_object_matches_two_pass_driver(monkeypatch):
    from pcc.backend import coff_x86_64 as coff
    from tests.python.test_x86_64_parse_buffer_publication import _two_pass
    from tests.python.test_x86_64_windows_packed_stackmaps import _coff_fixture

    assembly, plans, _rendered = _coff_fixture()
    actual = coff.assemble_object(assembly, stack_map_plans=list(plans))
    monkeypatch.setattr(assembler, 'assemble_file_with_stack_maps_keeping_labels',
                        _two_pass().assemble_file_with_stack_maps_keeping_labels)
    assert coff.assemble_object(assembly, stack_map_plans=list(plans)) == actual
