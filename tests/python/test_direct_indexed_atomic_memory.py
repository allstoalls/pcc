"""Atomic memory stays structured through capture, codec and target emission."""

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.arm64_asm_driver import assemble_file, assemble_lines
from pcc.backend.native_object import NativeObject, encode_native_object
from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_asm,
    emit_aarch64_darwin_indexed_module,
    emit_aarch64_darwin_indexed_transport,
)
from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file, decode_indexed_module_file
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_function
from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
from pcc.ir import ir


def _module(monkeypatch, *, width=32, load_order="acquire", store_order="release",
            alignment=16, direct=True, no_text=False):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1" if no_text else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")
    module = ir.Module(name="atomic-memory")
    module.triple = "arm64-apple-darwin23.6.0"
    value_type = ir.IntType(width)
    function = ir.Function(module, ir.FunctionType(value_type, [value_type.as_pointer(), value_type]), "exchange")
    function.args[0].name = "address"
    function.args[1].name = "value"
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    builder.store_atomic(function.args[1], function.args[0], store_order, alignment)
    value = builder.load_atomic(function.args[0], load_order, alignment, "observed", typ=value_type)
    builder.ret(value)
    return module


def _object(assembly):
    sections, undefined = assemble_file(assembly)
    return encode_native_object(NativeObject.from_sections(sections, undefined=undefined))


@pytest.mark.parametrize("width", [8, 16, 32, 64])
@pytest.mark.parametrize("load_order, store_order", [
    ("unordered", "unordered"), ("monotonic", "monotonic"),
    ("acquire", "release"), ("seq_cst", "seq_cst"),
])
def test_atomic_capture_text_codec_and_object_parity(monkeypatch, tmp_path, width, load_order, store_order):
    source = _module(monkeypatch, width=width, load_order=load_order, store_order=store_order)
    text = str(source)
    direct = source.direct_indexed_module()
    parsed = parse_self_backend_module(text)
    expected = []
    for module in (direct, parsed):
        kernel = get_indexed_function_kernel(module.functions[0])
        verify_parsed_function(module.functions[0])
        rows = [kernel.diagnostic_instruction(0, i).data for i in range(2)]
        assert rows[0][4:] == (store_order, 16)
        assert rows[1][4:] == (load_order, 16)
        assert rows[0][0].width == width
        assert rows[1][1].width == width
        expected.append(rows)
        kernel.diagnostic_projections = 0
    assert expected[0] == expected[1]

    direct_asm = emit_aarch64_darwin_indexed_module(direct, optimize=False)
    assert direct_asm == emit_aarch64_darwin_asm(text, optimize=False)
    assert all(get_indexed_function_kernel(fn).diagnostic_projections == 0 for fn in direct.functions)
    suffix = "b" if width == 8 else "h" if width == 16 else ""
    assert ("  ldar" + suffix + " " in direct_asm) is (load_order in ("acquire", "seq_cst"))
    assert ("  stlr" + suffix + " " in direct_asm) is (store_order in ("release", "seq_cst"))

    fresh = _module(monkeypatch, width=width, load_order=load_order, store_order=store_order,
                    no_text=True).direct_indexed_module()
    path = tmp_path / "atomic.pidx"
    encode_indexed_module_file(str(path), fresh)
    decoded = decode_indexed_module_file(str(path))
    decoded_kernel = get_indexed_function_kernel(decoded.functions[0])
    assert decoded_kernel.instruction_record_scalars.diagnostic_values() == get_indexed_function_kernel(fresh.functions[0]).instruction_record_scalars.diagnostic_values()
    decoded_asm = emit_aarch64_darwin_indexed_module(decoded, optimize=False)
    assert _object(decoded_asm) == _object(direct_asm)
    assert decoded_kernel.diagnostic_projections == 0


@pytest.mark.parametrize("width", [8, 16, 32, 64])
def test_no_text_atomic_memory_never_parses_or_projects(monkeypatch, width):
    import pcc.ir.direct_indexed_kernel as capture
    from pcc.backend.self_backend_kernel import IndexedFunctionKernel

    def forbidden(*args, **kwargs):
        raise AssertionError("atomic normal path attempted text/diagnostic fallback")

    source = _module(monkeypatch, width=width, no_text=True)
    function = source.functions[0]
    assert all(record._direct_record_id >= 0 and not record.text for record in function.blocks[0]._instrs)
    monkeypatch.setattr(capture, "build_indexed_function_seed_from_block_lines", forbidden)
    monkeypatch.setattr(IndexedFunctionKernel, "diagnostic_fixed_instruction_data", forbidden)
    monkeypatch.setattr(IndexedFunctionKernel, "diagnostic_instruction", forbidden)
    direct = source.direct_indexed_module()
    for function in direct.functions:
        verify_parsed_function(function)
    transport = emit_aarch64_darwin_indexed_transport(direct, optimize=False)
    assert transport.native_finalized
    assert transport.line_chunks == []
    assert transport.fallback_instruction_count == 0
    assert transport.fallback_instruction_lines == ()
    assert all(get_indexed_function_kernel(fn).diagnostic_projections == 0 for fn in direct.functions)
    sections, undefined = assemble_lines(transport.line_chunks, transport.structured_sections,
                                         transport.encoded_line_records, transport.structured_symbol_names)
    assert encode_native_object(NativeObject.from_sections(sections, undefined=undefined))


def test_x86_atomic_ordering_projection_keeps_seq_cst_xchg(monkeypatch):
    source = _module(monkeypatch, load_order="seq_cst", store_order="seq_cst")
    source.triple = "x86_64-unknown-linux-gnu"
    assembly = emit_x86_64_linux_asm(str(source))
    assert "xchg DWORD PTR" in assembly


def test_byte_release_store_preserves_width_without_text_fallback(monkeypatch):
    def build(no_text):
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1" if no_text else "0")
        module = ir.Module(name="atomic-byte")
        module.triple = "arm64-apple-darwin23.6.0"
        byte = ir.IntType(8)
        function = ir.Function(module, ir.FunctionType(byte, [byte.as_pointer(), byte]), "byte_store")
        builder = ir.IRBuilder(function.append_basic_block("entry"))
        builder.store_atomic(function.args[1], function.args[0], "release", 1)
        builder.ret(builder.load(function.args[0]))
        return module

    direct = build(True).direct_indexed_module()
    transport = emit_aarch64_darwin_indexed_transport(direct, optimize=False)
    assert transport.fallback_instruction_count == 0
    assert transport.fallback_instruction_lines == ()
    assert get_indexed_function_kernel(direct.functions[0]).diagnostic_projections == 0
    # Compare the release-store ISA word against the established ASM oracle.
    sections, undefined = assemble_lines(transport.line_chunks, transport.structured_sections,
                                         transport.encoded_line_records, transport.structured_symbol_names)
    encoded = encode_native_object(NativeObject.from_sections(sections, undefined=undefined))
    assert encoded == _object(emit_aarch64_darwin_asm(str(build(False)), optimize=False))


@pytest.mark.parametrize("width", [1, 128])
def test_unsupported_atomic_load_width_is_an_explicit_target_boundary(monkeypatch, width):
    source = _module(monkeypatch, width=width, alignment=2 if width == 16 else 1, no_text=True)
    with pytest.raises(BackendUnavailable, match="load_atomic|store_atomic"):
        emit_aarch64_darwin_indexed_module(source.direct_indexed_module(), optimize=False)


def test_existing_atomicrmw_capture_survives_atomic_memory_records(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    module = ir.Module(name="atomicrmw-regression")
    module.triple = "arm64-apple-darwin23.6.0"
    value_type = ir.IntType(64)
    function = ir.Function(module, ir.FunctionType(value_type, [value_type.as_pointer()]), "increment")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    prior = builder.atomic_rmw("add", function.args[0], ir.Constant(value_type, 3), "seq_cst")
    observed = builder.load_atomic(function.args[0], "acquire", 8)
    builder.ret(builder.add(prior, observed))
    direct = module.direct_indexed_module()
    verify_parsed_function(direct.functions[0])
    assert emit_aarch64_darwin_indexed_module(direct, optimize=False) == emit_aarch64_darwin_asm(str(module), optimize=False)


@pytest.mark.parametrize("field, value", [(0, -1), (1, -1), (1, 3)])
def test_atomic_record_metadata_validation_fails_closed(monkeypatch, field, value):
    direct = _module(monkeypatch, no_text=True).direct_indexed_module()
    kernel = get_indexed_function_kernel(direct.functions[0])
    # First atomic operation's second CompilerInt4 row holds order/align.
    kernel.instruction_record_scalars.set_unchecked(4 + field, value)
    with pytest.raises(BackendUnavailable, match="atomic-metadata"):
        verify_parsed_function(direct.functions[0])


def test_codec_reads_legacy_five_field_atomic_cold_payloads(monkeypatch, tmp_path):
    direct = _module(monkeypatch, no_text=True).direct_indexed_module()
    kernel = get_indexed_function_kernel(direct.functions[0])
    for index in range(2):
        legacy = kernel.diagnostic_instruction(0, index).data[:5]
        cold_id = len(kernel.cold_instruction_data)
        kernel.cold_instruction_data.append(legacy)
        kernel.instruction_metadata.set_unchecked(index * 4 + 1, -cold_id - 1)
    path = tmp_path / "legacy-atomic.pidx"
    encode_indexed_module_file(str(path), direct)
    restored = decode_indexed_module_file(str(path))
    verify_parsed_function(restored.functions[0])
    # Legacy files retain their existing tuple payloads; fresh producers use
    # positive fixed-record IDs and append alignment to diagnostic projections.
    legacy_kernel = get_indexed_function_kernel(restored.functions[0])
    assert all(len(payload) == 5 for payload in legacy_kernel.cold_instruction_data)
    assert legacy_kernel.instruction_metadata_by_id(0).second < 0
    legacy_asm = emit_aarch64_darwin_indexed_module(restored, optimize=False)
    fresh = _module(monkeypatch, no_text=True).direct_indexed_module()
    assert legacy_asm == emit_aarch64_darwin_indexed_module(fresh, optimize=False)


@pytest.mark.parametrize("operation, ordering", [("load", "release"), ("store", "acquire")])
def test_invalid_atomic_ordering_fails_closed(monkeypatch, operation, ordering):
    with pytest.raises(BackendUnavailable, match="ordering"):
        _module(monkeypatch, load_order=ordering if operation == "load" else "acquire",
                store_order=ordering if operation == "store" else "release", no_text=True)


@pytest.mark.parametrize("alignment", [0, 3])
def test_invalid_atomic_alignment_fails_closed(monkeypatch, alignment):
    with pytest.raises(BackendUnavailable, match="alignment"):
        _module(monkeypatch, alignment=alignment, no_text=True)
