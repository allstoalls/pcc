import gc
import weakref

from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.self_backend_prepare import prepare_module_for_target
from tests.python.test_x86_64_emission_lifetime import ROOTED_PROGRAM


def test_record_chunks_retire_before_next_function_header(monkeypatch):
    prepared = prepare_module_for_target(ROOTED_PROGRAM, aggregate_returned_indirect=emitter._aggregate_returned_indirect)
    plans = maps.build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux', function_symbol=emitter._asm_symbol)
    offsets = {}
    position = 0
    for plan in plans:
        offsets[emitter._asm_symbol(plan.function_name)] = position
        for record in plan.records:
            position += 4
            offsets[record.label] = position
            if record.exceptional_block:
                offsets[emitter._block_label(plan.function_name, record.exceptional_block)] = position + 1
        position += 4
        offsets[plan.end_label] = position
    expected = maps.build_x86_64_stack_map_payload(plans, offsets, function_symbol=emitter._asm_symbol, block_label=emitter._block_label)
    record_codec = maps._STACK_MAP_RECORD_CODEC
    function_codec = maps._STACK_MAP_FUNCTION_CODEC
    references = []
    headers = []

    class Owner:
        pass

    class TrackedBytes(bytes):
        def __new__(cls, value):
            result = super().__new__(cls, value)
            result.owner = Owner()
            references.append(weakref.ref(result.owner))
            return result

    class RecordCodec:
        size = record_codec.size
        @staticmethod
        def pack(*args):
            return TrackedBytes(record_codec.pack(*args))

    class FunctionCodec:
        size = function_codec.size
        @staticmethod
        def pack(*args):
            assert all(reference() is None for reference in references)
            headers.append(args[0])
            return function_codec.pack(*args)

    monkeypatch.setattr(maps, '_STACK_MAP_RECORD_CODEC', RecordCodec())
    monkeypatch.setattr(maps, '_STACK_MAP_FUNCTION_CODEC', FunctionCodec())
    actual = maps.build_x86_64_stack_map_payload(plans, offsets, function_symbol=emitter._asm_symbol, block_label=emitter._block_label)
    assert len(headers) > 1 and references
    assert all(reference() is None for reference in references)
    assert actual == expected
