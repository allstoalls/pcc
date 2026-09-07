from __future__ import annotations

import hashlib

import pytest
from pcc.backend import self_backend_indexed_codec as indexed_codec
from pcc.backend import self_backend_indexed_emit as indexed_emit
from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_indexed_module,
)
from pcc.backend.self_backend_indexed_codec import (
    _ARENA_FIELDS,
    decode_indexed_module_file,
    encode_indexed_module_file,
)
from pcc.backend.self_backend_indexed_emit import emit_indexed_module_file
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import encode_native_object_from_sections
from pcc.llvm_capi import ir


def _arena_values(kernel, field: str):
    return getattr(kernel, field).diagnostic_values()


def test_indexed_emit_debug_phase_labels_native_memory_counters(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("PCC_DEBUG_INDEXED_EMIT", "1")

    indexed_emit._debug_phase("probe")

    assert capsys.readouterr().err == (
        "pcc indexed emit phase=probe rss_bytes=-1 "
        "heap_in_use_bytes=-1 heap_capacity_bytes=-1\n"
    )


def _assert_native_json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_native_json_value(item)
        return
    assert isinstance(value, dict), type(value).__name__
    for key, item in value.items():
        assert isinstance(key, str)
        _assert_native_json_value(item)


def _direct_module(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    module = ir.Module(name="codec")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    counter = ir.GlobalVariable(module, i64, "counter")
    counter.linkage = "internal"
    counter.initializer = ir.Constant(i64, 7)
    counter.align = 8
    callee = ir.Function(module, ir.FunctionType(i64, [i64]), name="callee")
    function = ir.Function(module, ir.FunctionType(i64, [i64]), name="run")
    function.args[0].name = "value"
    entry = function.append_basic_block("entry")
    yes = function.append_basic_block("yes")
    no = function.append_basic_block("no")
    builder = ir.IRBuilder(entry)
    slot = builder.alloca(i64, name="slot")
    builder.store(function.args[0], slot)
    loaded = builder.load(slot, name="loaded")
    summed = builder.add(loaded, ir.Constant(i64, 2), name="sum")
    called = builder.call(callee, [summed], name="called")
    ok = builder.icmp_signed(">", called, ir.Constant(i64, 0), name="ok")
    builder.cbranch(ok, yes, no)
    builder.position_at_end(yes)
    builder.ret(called)
    builder.position_at_end(no)
    builder.ret(ir.Constant(i64, 0))
    f64 = ir.DoubleType()
    float_function = ir.Function(
        module,
        ir.FunctionType(f64, [f64]),
        name="float_run",
    )
    float_function.args[0].name = "value"
    float_entry = float_function.append_basic_block("entry")
    float_builder = ir.IRBuilder(float_entry)
    float_sum = float_builder.fadd(
        float_function.args[0],
        ir.Constant(f64, 1.5),
        name="sum",
    )
    float_builder.ret(float_sum)
    return module.direct_indexed_module()


def test_indexed_module_codec_roundtrips_every_scalar_plane_and_assembly(
    tmp_path,
    monkeypatch,
):
    module = _direct_module(monkeypatch)
    path = tmp_path / "module.pidx"
    second = tmp_path / "module-second.pidx"
    host_json_dumps = indexed_codec.json.dumps

    def checked_json_dumps(value, **kwargs):
        _assert_native_json_value(value)
        return host_json_dumps(value, **kwargs)

    monkeypatch.setattr(indexed_codec.json, "dumps", checked_json_dumps)

    encode_indexed_module_file(str(path), module)
    encode_indexed_module_file(str(second), module)
    restored = decode_indexed_module_file(str(path))

    assert path.read_bytes() == second.read_bytes()
    assert restored.triple == module.triple
    assert restored.globals_ == module.globals_
    assert len(restored.functions) == len(module.functions) == 2
    for original_function, restored_function in zip(
        module.functions,
        restored.functions,
    ):
        assert (
            restored_function.name,
            restored_function.ret_type,
            restored_function.args,
            restored_function.is_global,
            restored_function.is_vararg,
        ) == (
            original_function.name,
            original_function.ret_type,
            original_function.args,
            original_function.is_global,
            original_function.is_vararg,
        )

        original_kernel = get_indexed_function_kernel(original_function)
        restored_kernel = get_indexed_function_kernel(restored_function)
        assert restored_kernel.block_names == original_kernel.block_names
        assert restored_kernel.value_names == original_kernel.value_names
        assert restored_kernel.call_texts == original_kernel.call_texts
        assert restored_kernel.types == original_kernel.types
        assert (
            restored_kernel.instruction_arithmetic_flag_values
            == original_kernel.instruction_arithmetic_flag_values
        )
        assert (
            restored_kernel.cold_instruction_data
            == original_kernel.cold_instruction_data
        )
        for _wire_name, kernel_field, _seed_field in _ARENA_FIELDS:
            assert _arena_values(restored_kernel, kernel_field) == _arena_values(
                original_kernel,
                kernel_field,
            )

    original_asm = emit_aarch64_darwin_indexed_module(module, optimize=False)
    restored_asm = emit_aarch64_darwin_indexed_module(restored, optimize=False)
    assert restored_asm == original_asm
    assert hashlib.sha256(restored_asm.encode()).digest() == hashlib.sha256(
        original_asm.encode()
    ).digest()


def test_indexed_module_codec_rejects_truncated_raw_arena_payload(
    tmp_path,
    monkeypatch,
):
    module = _direct_module(monkeypatch)
    path = tmp_path / "module.pidx"
    encode_indexed_module_file(str(path), module)
    raw = path.read_bytes()
    path.write_bytes(raw[:-1])

    with pytest.raises(ValueError, match="size is inconsistent"):
        decode_indexed_module_file(str(path))


def test_indexed_module_codec_requires_the_published_preparation_boundary(
    tmp_path,
    monkeypatch,
):
    module = _direct_module(monkeypatch)
    module.functions[0].indexed_kernel = None

    with pytest.raises(ValueError, match="no published indexed kernel"):
        encode_indexed_module_file(str(tmp_path / "module.pidx"), module)


@pytest.mark.parametrize("optimize", [False, True])
def test_indexed_module_fresh_emit_matches_the_text_assembly_oracle(
    tmp_path,
    monkeypatch,
    optimize,
):
    module = _direct_module(monkeypatch)
    sidecar = tmp_path / "module.pidx"
    output = tmp_path / "module.pco"
    encode_indexed_module_file(str(sidecar), module)

    oracle_asm = emit_aarch64_darwin_indexed_module(module, optimize=optimize)
    sections, undefined = assemble_file(oracle_asm)
    expected = encode_native_object_from_sections(
        sections,
        undefined=undefined,
    )

    emit_indexed_module_file(str(sidecar), str(output), "PCO", optimize=optimize)

    assert output.read_bytes() == expected
    assert not (tmp_path / "module.pco.tmp").exists()


def test_indexed_module_fresh_emit_preserves_the_assembly_lane(
    tmp_path,
    monkeypatch,
):
    module = _direct_module(monkeypatch)
    sidecar = tmp_path / "module.pidx"
    output = tmp_path / "module.s"
    encode_indexed_module_file(str(sidecar), module)
    expected = emit_aarch64_darwin_indexed_module(module, optimize=False)

    emit_indexed_module_file(str(sidecar), str(output), "ASM")

    assert output.read_text(encoding="utf-8") == expected
