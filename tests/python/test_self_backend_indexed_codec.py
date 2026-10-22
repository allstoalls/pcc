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
from pcc.ir import ir


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
@pytest.mark.parametrize("telemetry", [False, True])
def test_indexed_module_fresh_emit_matches_the_text_assembly_oracle(
    tmp_path,
    monkeypatch,
    optimize,
    telemetry,
):
    if telemetry:
        monkeypatch.setenv("PCC_WORKER_RESOURCE_REPORT", str(tmp_path / "rss"))
        monkeypatch.setenv("PCC_WORKER_RESOURCE_TOKEN", "telemetry-identity")
    else:
        monkeypatch.delenv("PCC_WORKER_RESOURCE_REPORT", raising=False)
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
    if telemetry:
        from pcc.frontends.python.worker_resource_plan import read_worker_resource
        import os
        observed = read_worker_resource(str(tmp_path / "rss"), os.getpid(), "telemetry-identity")
        assert observed is not None and observed[0] == "complete"
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


def test_indexed_restore_closes_temporary_value_columns_after_copying(
    tmp_path, monkeypatch,
):
    module = _direct_module(monkeypatch)
    path = tmp_path / "adopt.pidx"
    encode_indexed_module_file(str(path), module)
    read_arena = indexed_codec._read_host_arena
    restored_arenas = []

    def capture(stream, count):
        arena = read_arena(stream, count)
        restored_arenas.append(arena)
        return arena

    monkeypatch.setattr(indexed_codec, "_read_host_arena", capture)
    restored = decode_indexed_module_file(str(path))
    for index, function in enumerate(restored.functions):
        arenas = {name: restored_arenas[index * len(_ARENA_FIELDS) + offset]
                  for offset, (name, _field, _seed) in enumerate(_ARENA_FIELDS)}
        kernel = get_indexed_function_kernel(function)
        original = get_indexed_function_kernel(module.functions[index])
        for name in ("value_scalars", "definition_positions", "used_value_ids"):
            assert getattr(kernel, name) is not arenas[name]
            assert getattr(kernel, name).diagnostic_values() == getattr(original, name).diagnostic_values()
            with pytest.raises(RuntimeError, match="closed"):
                arenas[name].diagnostic_values()
        assert not kernel.definition_blocks
        assert not kernel.value_type_ids
        kernel.close_native_tables()
        for name in ("value_scalars", "definition_positions", "used_value_ids"):
            with pytest.raises(RuntimeError, match="closed"):
                arenas[name].diagnostic_values()
        kernel.close_native_tables()  # Closing again must not free final storage twice.


@pytest.mark.parametrize("lane", [2, 3, 5])
def test_indexed_restore_rejects_prepared_value_columns(tmp_path, monkeypatch, lane):
    import json
    import struct

    path = tmp_path / "prepared.pidx"
    encode_indexed_module_file(str(path), _direct_module(monkeypatch))
    with path.open("rb") as stream:
        stream.readline()
        size = int(stream.readline())
        header = json.loads(stream.read(size))
        start = stream.tell()
    for name, count in header["functions"][0]["arenas"]:
        if name == "value_scalars":
            break
        start += count * 8
    else:
        raise AssertionError("missing value plane")
    raw = bytearray(path.read_bytes())
    struct.pack_into("<q", raw, start + lane * 8, 0)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="value state is already prepared"):
        decode_indexed_module_file(str(path))


def test_indexed_restore_preserves_boolean_lane_normalization(tmp_path, monkeypatch):
    import json
    import struct

    path = tmp_path / "boolean.pidx"
    encode_indexed_module_file(str(path), _direct_module(monkeypatch))
    with path.open("rb") as stream:
        stream.readline()
        size = int(stream.readline())
        header = json.loads(stream.read(size))
        start = stream.tell()
    for name, count in header["functions"][0]["arenas"]:
        if name == "value_scalars":
            break
        start += count * 8
    raw = bytearray(path.read_bytes())
    struct.pack_into("<q", raw, start + 7 * 8, -7)
    path.write_bytes(raw)
    restored = decode_indexed_module_file(str(path))
    assert get_indexed_function_kernel(restored.functions[0]).value_scalars.get_unchecked(7) == 1


def _executable_direct_module(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    module = ir.Module(name="restore_exec")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    helper = ir.Function(module, ir.FunctionType(i64, [i64]), name="helper")
    helper.args[0].name = "value"
    entry = helper.append_basic_block("entry")
    yes = helper.append_basic_block("yes")
    no = helper.append_basic_block("no")
    join = helper.append_basic_block("join")
    builder = ir.IRBuilder(entry)
    slot = builder.alloca(i64, name="slot")
    builder.store(helper.args[0], slot)
    loaded = builder.load(slot, name="loaded")
    condition = builder.icmp_signed(">", loaded, ir.Constant(i64, 39), name="condition")
    builder.cbranch(condition, yes, no)
    builder.position_at_end(yes)
    good = builder.add(loaded, ir.Constant(i64, 2), name="good")
    builder.branch(join)
    builder.position_at_end(no)
    bad = builder.sub(loaded, ir.Constant(i64, 2), name="bad")
    builder.branch(join)
    builder.position_at_end(join)
    result = builder.phi(i64, name="result")
    result.add_incoming(good, yes)
    result.add_incoming(bad, no)
    builder.ret(result)
    main = ir.Function(module, ir.FunctionType(i64, []), name="main")
    builder = ir.IRBuilder(main.append_basic_block("entry"))
    builder.ret(builder.call(helper, [ir.Constant(i64, 40)], name="answer"))
    empty = ir.Function(module, ir.FunctionType(ir.VoidType(), []), name="empty")
    ir.IRBuilder(empty.append_basic_block("entry")).ret_void()
    return module.direct_indexed_module()


def test_indexed_restore_handles_a_zero_value_function(tmp_path, monkeypatch):
    path = tmp_path / "empty.pidx"
    module = _executable_direct_module(monkeypatch)
    encode_indexed_module_file(str(path), module)
    restored = decode_indexed_module_file(str(path))
    empty = next(function for function in restored.functions if function.name == "empty")
    kernel = get_indexed_function_kernel(empty)
    assert len(kernel.value_names) == len(kernel.value_scalars) == 0
    assert emit_aarch64_darwin_indexed_module(restored, optimize=False) == emit_aarch64_darwin_indexed_module(module, optimize=False)


@pytest.mark.integration
def test_restored_value_arenas_survive_native_collection_and_roundtrip(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
):
    import os
    import subprocess
    import importlib.util
    from pathlib import Path
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.native_object import decode_packed_native_object

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    sidecar = tmp_path / "input.pidx"
    encode_indexed_module_file(str(sidecar), _executable_direct_module(monkeypatch))
    expected_pco = tmp_path / "expected.pco"
    emit_indexed_module_file(str(sidecar), str(expected_pco), "PCO", optimize=False)
    # Use the real compiler package closure, as the production worker does.
    # An unrelated top-level script does not admit private backend imports.
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "pidx_stage1_tool", repo / "scripts/run_pcc_stage1_build.py",
    )
    stage1_tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage1_tool)
    ab = stage1_tool._load_ab_tool()
    snapshot = tmp_path / "source"
    stage1_tool._snapshot_sources(repo, stage1_tool.source_manifest(repo, ab), snapshot, ab)
    source = snapshot / "pcc/backend/restore_native.py"
    source.parent.chmod(0o755)
    source.write_text('''import gc
import sys
from pcc.backend.self_backend_indexed_codec import decode_indexed_module_file, encode_indexed_module_file
from pcc.backend.self_backend_kernel import get_indexed_function_kernel

def main():
    module = decode_indexed_module_file(sys.argv[1])
    gc.collect()
    gc.collect()
    gc.collect()
    assert len(module.functions) == 3
    for function in module.functions:
        kernel = get_indexed_function_kernel(function)
        assert len(kernel.value_scalars) == len(kernel.value_names) * 8
        assert len(kernel.definition_positions) == len(kernel.value_names)
        assert not kernel.definition_blocks
        assert not kernel.value_type_ids
    encode_indexed_module_file(sys.argv[2], module)
    gc.collect()
    for function in module.functions:
        kernel = get_indexed_function_kernel(function)
        kernel.close_native_tables()
        kernel.close_native_tables()
    print("restore-native-ok")
main()
''')
    source.chmod(0o444)
    source.parent.chmod(0o555)
    binary = tmp_path / "restore_native"
    monkeypatch.chdir(snapshot)
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        restored = tmp_path / ("restored-" + str(backend) + ".pidx")
        ran = subprocess.run([str(binary), str(sidecar), str(restored)],
                             capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "restore-native-ok"
        assert restored.read_bytes() == sidecar.read_bytes(), backend
        pco = tmp_path / ("restored-" + str(backend) + ".pco")
        emit_indexed_module_file(str(restored), str(pco), "PCO", optimize=False)
        assert pco.read_bytes() == expected_pco.read_bytes(), backend
        executable = tmp_path / ("program-" + str(backend))
        executable.write_bytes(link_executable([decode_packed_native_object(pco.read_bytes())]))
        executable.chmod(0o755)
        executed = subprocess.run([str(executable)], capture_output=True, timeout=10)
        assert executed.returncode == 42, (backend, executed.stderr)
