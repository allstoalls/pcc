from __future__ import annotations

import re

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.arm64_asm_driver import assemble_file, assemble_lines
from pcc.backend.native_object import NativeObject, encode_native_object
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_verify import verify_parsed_function
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_asm,
    emit_aarch64_darwin_indexed_lines,
    emit_aarch64_darwin_indexed_module,
    emit_aarch64_darwin_indexed_transport,
)
from pcc.llvm_capi import ir
from pcc.llvm_capi.direct_indexed_kernel import (
    build_direct_indexed_function,
    direct_indexed_module_cfg_stats,
    direct_indexed_module_first_libpython_edge,
    direct_indexed_module_needs_libpython,
)
from pcc.py_frontend.codegen.class_gen import _classgen_emit_dynamic_attr_value


_DIRECT_STATIC_METHOD_ABI = (
    "publish_inline_error_edge",
    "set_arithmetic_flags",
    "publish_alloca",
    "publish_store",
    "publish_load",
    "publish_binop",
    "publish_icmp",
    "publish_fbinop",
    "publish_fcmp",
    "publish_fneg",
    "publish_cast",
    "publish_select",
    "publish_extractvalue",
    "publish_call",
    "publish_gep",
    "publish_raw_call",
    "publish_phi",
    "append_phi_incoming",
    "publish_terminator",
    "append_switch_case",
    "diagnostic_record_text",
)


_ARENA_FIELDS = (
    "block_facts",
    "instruction_facts",
    "instruction_kind_ids",
    "instruction_metadata",
    "instruction_record_dest_ids",
    "gep_index_scalars",
    "gep_scalars",
    "instruction_overflow_use_ids",
    "terminator_case_scalars",
    "terminator_scalars",
    "block_phi_facts",
    "phi_incoming_scalars",
    "phi_scalars",
    "error_edge_scalars",
    "error_edge_spans",
    "error_landing_scalars",
    "definition_positions",
    "used_value_ids",
    "slot_scalars",
    "block_layout_ids",
)


def test_direct_inline_error_edge_plane_matches_explicit_cfg_oracle(monkeypatch):
    def build(*, direct: bool):
        if direct:
            monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
            monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
            monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")
        else:
            monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", raising=False)
            monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", raising=False)
            monkeypatch.delenv(
                "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES",
                raising=False,
            )
        module = ir.Module(name="inline-error-edge")
        module.triple = "arm64-apple-darwin23.6.0"
        i64 = ir.IntType(64)
        function = ir.Function(module, ir.FunctionType(i64, []), name="probe")
        entry = function.append_basic_block("entry")
        error = function.append_basic_block("error")
        builder = ir.IRBuilder(entry)
        condition = builder.icmp_signed(
            "!=",
            ir.Constant(i64, 1),
            ir.Constant(i64, 0),
            name="failed",
        )
        emitted_inline = ir.IRBuilder_try_inline_error_edge(
            builder,
            condition,
            error,
            42,
            0,
        )
        if emitted_inline:
            builder.ret(ir.Constant(i64, 1))
            builder.position_at_end(error)
            builder.ret(ir.Constant(i64, 0))
        else:
            normal = function.append_basic_block("normal")
            builder.cbranch(condition, error, normal)
            builder.position_at_end(error)
            builder.ret(ir.Constant(i64, 0))
            builder.position_at_end(normal)
            builder.ret(ir.Constant(i64, 1))
        return module

    monkeypatch.setattr(ir, "_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED", True)
    direct_source = build(direct=True)
    direct = direct_source.direct_indexed_module()
    kernel = get_indexed_function_kernel(direct.functions[0])
    edge = kernel.error_edge_scalars.diagnostic_values()

    assert len(edge) == 8
    assert edge[0] == 0
    assert edge[1] == 0
    assert edge[2] >= 0
    assert edge[3:] == [1, 42, 0, -1, 0]
    assert kernel.error_edge_spans.diagnostic_values() == [0, 1, 1, 0]
    assert kernel.block_names == ["entry", "error"]
    assert kernel.value_is_used(edge[2])
    assert kernel.last_use(0, edge[2]) == 1

    text_source = build(direct=False)
    parsed = parse_self_backend_module(str(text_source))
    assert get_indexed_function_kernel(
        parsed.functions[0]
    ).error_edge_scalars.diagnostic_values() == []
    direct_asm = emit_aarch64_darwin_indexed_module(direct, optimize=False)
    text_asm = emit_aarch64_darwin_asm(str(text_source), optimize=False)
    assert "  cbnz w9, L_probe_error" in direct_asm
    assert "L_probe_normal:" not in direct_asm
    assert "L_probe_normal:" in text_asm
    direct_lines = emit_aarch64_darwin_indexed_lines(
        build(direct=True).direct_indexed_module(),
        optimize=False,
    )
    assert direct_asm == "\n".join(direct_lines) + "\n"
    text_sections, text_undefined = assemble_file(direct_asm)
    transport = emit_aarch64_darwin_indexed_transport(
        build(direct=True).direct_indexed_module(),
        optimize=False,
    )
    structured_sections, structured_undefined = assemble_lines(
        transport.line_chunks,
        transport.structured_sections,
        transport.encoded_line_records,
        transport.structured_symbol_names,
    )
    assert transport.fallback_instruction_count == 0
    assert transport.native_finalized
    assert transport.encoded_line_records is None
    assert structured_sections == text_sections
    assert structured_undefined == text_undefined
    assert encode_native_object(NativeObject.from_sections(
        structured_sections,
        undefined=structured_undefined,
    )) == encode_native_object(NativeObject.from_sections(
        text_sections,
        undefined=text_undefined,
    ))

    bad = build(direct=True).direct_indexed_module()
    bad_kernel = get_indexed_function_kernel(bad.functions[0])
    bad_kernel.error_edge_scalars.set_unchecked(3, 99)
    with pytest.raises(BackendUnavailable, match="invalid target"):
        verify_parsed_function(bad.functions[0])


@pytest.mark.parametrize("shape", ["hfa", "cold_landing"])
def test_direct_transport_preserves_deferred_instruction_order(monkeypatch, tmp_path, shape):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")
    monkeypatch.setattr(ir, "_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED", True)

    def build():
        module = ir.Module(name="instruction-order-" + shape)
        module.triple = "arm64-apple-darwin23.6.0"
        if shape == "hfa":
            ty = ir.ArrayType(ir.DoubleType(), 2)
            function = ir.Function(module, ir.FunctionType(ty, [ty]), name="probe")
            builder = ir.IRBuilder(function.append_basic_block("entry"))
            builder.ret(function.args[0])
        else:
            i32 = ir.IntType(32)
            function = ir.Function(module, ir.FunctionType(i32, [i32]), name="probe")
            builder = ir.IRBuilder(function.append_basic_block("entry"))
            landing = function.append_basic_block("landing")
            slot = builder.alloca(i32, name="payload")
            assert ir.IRBuilder_declare_inline_error_landing(builder, landing, slot)
            condition = builder.icmp_signed("!=", function.args[0], ir.Constant(i32, 0))
            assert ir.IRBuilder_try_inline_error_edge(
                builder, condition, landing, 42, 0, 73,
            )
            builder.ret(ir.Constant(i32, 19))
            builder.position_at_end(landing)
            builder.ret(builder.load(slot))
        return module.direct_indexed_module()

    expected_asm = emit_aarch64_darwin_indexed_module(build(), optimize=False)
    expected = assemble_file(expected_asm)
    transport = emit_aarch64_darwin_indexed_transport(build(), optimize=False)
    try:
        actual = assemble_lines(
            transport.line_chunks, transport.structured_sections,
            transport.encoded_line_records, transport.structured_symbol_names,
        )
        assert actual == expected
    finally:
        assert transport.native_finalized
        assert transport.encoded_line_records is None

    # Explicit receipt-bound pcc1 replay: the default invocation remains a
    # seconds-only host regression, while a build qualification supplies its
    # exact compiler. This executes the changed emitter, not just --emit-llvm.
    import os
    import subprocess
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file

    compiler = os.environ.get("PCC_INDEXED_EMIT_TEST_COMPILER")
    if compiler:
        indexed_path = tmp_path / (shape + ".pidx")
        encode_indexed_module_file(str(indexed_path), build())
        expected_pco = encode_native_object(NativeObject.from_sections(
            expected[0], undefined=expected[1],
        ))
        for mode, oracle in (("ASM", expected_asm.encode()), ("PCO", expected_pco)):
            output = tmp_path / (shape + "." + mode.lower())
            run = subprocess.run(
                [compiler, "--pcc-self-backend-indexed-emit-worker",
                 str(indexed_path), str(output), mode],
                capture_output=True, text=True, timeout=30,
            )
            assert run.returncode == 0, run.stdout + run.stderr
            assert output.read_bytes() == oracle


def test_direct_transport_structures_unscaled_slot_load_store_family(
    monkeypatch,
):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")

    def build():
        module = ir.Module(name="structured-unscaled")
        module.triple = "arm64-apple-darwin23.6.0"
        i64 = ir.IntType(64)
        function = ir.Function(
            module,
            ir.FunctionType(i64, [i64]),
            name="probe_slot",
        )
        entry = function.append_basic_block("entry")
        builder = ir.IRBuilder(entry)
        slot = builder.alloca(i64, name="slot")
        builder.store(function.args[0], slot)
        loaded = builder.load(slot, name="loaded")
        builder.ret(loaded)
        return module.direct_indexed_module()

    oracle_asm = emit_aarch64_darwin_indexed_module(
        build(),
        optimize=False,
    )
    oracle_sections, oracle_undefined = assemble_file(oracle_asm)
    transport = emit_aarch64_darwin_indexed_transport(
        build(),
        optimize=False,
    )
    assert transport.structured_unscaled_count >= 2
    assert transport.direct_instruction_count >= 2
    # These three counters are selected migrated families, not an exhaustive
    # partition: the generic encoder also owns arithmetic/branch/etc. records.
    assert transport.structured_instruction_count >= (
        transport.structured_unscaled_count
        + transport.structured_move_count
        + transport.structured_call_count
    )
    assert transport.native_finalized
    assert transport.encoded_line_records is None
    assert transport.line_chunks == []
    sections, undefined = assemble_lines(
        transport.line_chunks,
        transport.structured_sections,
        transport.encoded_line_records,
        transport.structured_symbol_names,
    )
    assert sections == oracle_sections
    assert undefined == oracle_undefined
    host_transport = emit_aarch64_darwin_indexed_transport(
        build(),
        optimize=False,
        structured_instructions=False,
    )
    assert host_transport.structured_instruction_count == 0
    assert host_transport.encoded_line_records is not None
    assert len(host_transport.encoded_line_records) == 0
    host_sections, host_undefined = assemble_lines(
        host_transport.line_chunks,
        host_transport.structured_sections,
        host_transport.encoded_line_records,
        host_transport.structured_symbol_names,
    )
    host_transport.encoded_line_records.close()
    assert host_sections == oracle_sections
    assert host_undefined == oracle_undefined


def test_direct_transport_structures_recursive_cross_atom_and_external_calls(
    monkeypatch,
):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")

    def build():
        module = ir.Module(name="structured-calls")
        module.triple = "arm64-apple-darwin23.6.0"
        void_function = ir.FunctionType(ir.VoidType(), [])
        external = ir.Function(module, void_function, name="external_hook")
        helper = ir.Function(module, void_function, name="helper")
        helper_builder = ir.IRBuilder(helper.append_basic_block("entry"))
        helper_builder.ret_void()
        recursive = ir.Function(module, void_function, name="recursive")
        recursive_builder = ir.IRBuilder(
            recursive.append_basic_block("entry")
        )
        recursive_builder.call(recursive, [])
        recursive_builder.ret_void()
        entry = ir.Function(module, void_function, name="entry")
        entry_builder = ir.IRBuilder(entry.append_basic_block("entry"))
        entry_builder.call(helper, [])
        entry_builder.call(external, [])
        entry_builder.ret_void()
        return module.direct_indexed_module()

    oracle_asm = emit_aarch64_darwin_indexed_module(
        build(),
        optimize=False,
    )
    oracle_sections, oracle_undefined = assemble_file(oracle_asm)
    transport = emit_aarch64_darwin_indexed_transport(
        build(),
        optimize=False,
    )
    assert transport.structured_call_count == 3
    assert transport.native_finalized
    assert transport.encoded_line_records is None
    sections, undefined = assemble_lines(
        transport.line_chunks,
        transport.structured_sections,
        transport.encoded_line_records,
        transport.structured_symbol_names,
    )
    assert sections == oracle_sections
    assert undefined == oracle_undefined
    assert not any(
        relocation.symbol.endswith("recursive")
        for section in sections if section.sectname == "__text"
        for relocation in section.relocations
    )


def test_native_transport_finalizes_without_module_instruction_slots(monkeypatch):
    from pcc.backend import self_backend_aarch64_darwin as emitter

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    captured_sizes = []
    release = emitter._NativeAArch64Emission.release_captured_function
    native_function_results = []
    native_block_results = []
    emit_function = emitter._emit_function
    emit_blocks = emitter._emit_dense_indexed_function_blocks

    def observe_function(*args, **kwargs):
        if emitter.direct_instruction_capture_active():
            assert kwargs.get("native_sink") is not None
        result = emit_function(*args, **kwargs)
        if kwargs.get("native_sink") is not None:
            native_function_results.append(result)
        return result

    def observe_blocks(*args, **kwargs):
        result = emit_blocks(*args, **kwargs)
        if kwargs.get("native_sink") is not None:
            native_block_results.append(result)
        return result

    monkeypatch.setattr(emitter, "_emit_function", observe_function)
    monkeypatch.setattr(emitter, "_emit_dense_indexed_function_blocks", observe_blocks)

    def observe_release(sink):
        captured_sizes.append(len(sink.direct_records))
        release(sink)
        assert len(sink.direct_records) == 0

    monkeypatch.setattr(emitter._NativeAArch64Emission, "release_captured_function", observe_release)

    def build():
        module = ir.Module(name="native-module-buffer")
        module.triple = "arm64-apple-darwin23.6.0"
        i64 = ir.IntType(64)
        for index in range(3):
            function = ir.Function(module, ir.FunctionType(i64, [i64]), name=f"f{index}")
            builder = ir.IRBuilder(function.append_basic_block("entry"))
            yes = function.append_basic_block("yes")
            no = function.append_basic_block("no")
            value = builder.add(function.args[0], ir.Constant(i64, index))
            builder.cbranch(builder.icmp_signed(">", value, ir.Constant(i64, 0)), yes, no)
            builder.position_at_end(yes)
            builder.ret(value)
            builder.position_at_end(no)
            builder.ret(ir.Constant(i64, 0))
        return module.direct_indexed_module()

    expected = assemble_file(emit_aarch64_darwin_indexed_module(build(), optimize=False))
    result = emit_aarch64_darwin_indexed_transport(build(), optimize=False)
    assert result.native_finalized
    assert result.line_chunks == []
    assert result.encoded_line_records is None
    assert result.structured_instruction_count > 0
    assert result.direct_instruction_count > 0
    assert len(captured_sizes) == 3
    assert max(captured_sizes) < result.direct_instruction_count * 4
    assert native_function_results == [[], [], []]
    assert native_block_results == [[], [], []]
    assert result.assemble_sections() == expected


def test_direct_inline_error_edge_trigger_follows_records_inserted_ahead(
    monkeypatch,
):
    """Records inserted ahead of a published edge must not move the edge.

    The frontend hoists allocas, pooled constants and root stores to the start
    of a block after later records already carry inline edges
    (``position_at_start`` / ``position_before``).  The trigger is therefore
    resolved from the condition's final definition, and a block's edges are
    published in trigger order even when they were created out of order.
    """
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")
    monkeypatch.setattr(ir, "_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED", True)
    module = ir.Module(name="inline-error-edge-shift")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    function = ir.Function(module, ir.FunctionType(i64, [i64]), name="probe")
    entry = function.append_basic_block("entry")
    error = function.append_basic_block("error")
    builder = ir.IRBuilder(entry)
    arg = function.args[0]
    first = builder.icmp_signed("!=", arg, ir.Constant(i64, 0), name="first")
    assert ir.IRBuilder_try_inline_error_edge(builder, first, error, 10, 0)
    doubled = builder.add(arg, arg, name="doubled")
    second = builder.icmp_signed(
        "!=", doubled, ir.Constant(i64, 4), name="second"
    )
    assert ir.IRBuilder_try_inline_error_edge(builder, second, error, 11, 0)
    builder.position_at_start(entry)
    hoisted = builder.add(arg, ir.Constant(i64, 1), name="hoisted")
    early = builder.icmp_signed("==", hoisted, ir.Constant(i64, 7), name="early")
    assert ir.IRBuilder_try_inline_error_edge(builder, early, error, 9, 0)
    builder.position_at_end(entry)
    builder.ret(doubled)
    builder.position_at_end(error)
    builder.ret(ir.Constant(i64, 0))

    direct = module.direct_indexed_module()
    kernel = get_indexed_function_kernel(direct.functions[0])
    verify_parsed_function(direct.functions[0])
    edges = kernel.error_edge_scalars.diagnostic_values()
    assert len(edges) == 24
    # entry records: hoisted(0) early(1) first(2) doubled(3) second(4)
    assert [edges[index * 8 + 1] for index in range(3)] == [1, 2, 4]
    assert [edges[index * 8 + 4] for index in range(3)] == [9, 10, 11]
    assert kernel.error_edge_spans.diagnostic_values() == [0, 3, 3, 0]
    asm = emit_aarch64_darwin_indexed_module(direct, optimize=False)
    assert asm.count("L_probe_error") >= 3


_FRONTEND_INLINE_EDGE_SOURCE = (
    "def probe(value: str) -> int:\n"
    "    return int(value)\n"
    "\n"
    "\n"
    "class T:\n"
    "    def __init__(self, tag):\n"
    "        self.tag = tag\n"
    "\n"
    "\n"
    "def takes(a, b) -> int:\n"
    "    return len(b)\n"
    "\n"
    "\n"
    "def guarded(text: str) -> int:\n"
    "    try:\n"
    "        return int(text)\n"
    "    except ValueError:\n"
    "        return -1\n"
    "\n"
    "\n"
    "def temp_args(tag: str) -> int:\n"
    "    return takes(T(tag), [T(tag)])\n"
    "\n"
    "\n"
    "class Resolver:\n"
    "    def helper(self, node) -> int:\n"
    "        return len(str(node))\n"
    "\n"
    "    def resolve(self, node) -> int:\n"
    "        # Pinned receiver plus an owned attribute temporary: the\n"
    "        # error edge lands in a cleanup block that unpins ``self``\n"
    "        # after a release safepoint, so that block must own a real\n"
    "        # entry root state even though only the edge reaches it.\n"
    "        return self.helper(node.child) + self.helper(node.other)\n"
    "\n"
    "\n"
    "class Name:\n"
    "    def __init__(self, ident: str):\n"
    "        self.ident = ident\n"
    "\n"
    "\n"
    "class Finder:\n"
    "    def __init__(self):\n"
    "        self.classes = {'x': 1}\n"
    "\n"
    "    def alias(self, ident: str, cd) -> str:\n"
    "        return ident\n"
    "\n"
    "    def resolve_name(self, cd, value: Name) -> str:\n"
    "        # Typed attribute temporaries released on the return path\n"
    "        # after the frame roots are left: a cleanup block's unpin of\n"
    "        # ``self`` must only extend liveness up to the edge trigger.\n"
    "        if value.ident in self.classes:\n"
    "            return value.ident\n"
    "        return self.alias(value.ident, cd)\n"
)


def _generate_frontend_module(monkeypatch, *, direct: bool):
    """Lower the canary source through L1CodeGen in direct or text mode."""
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen import exception_lowering
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    if direct:
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
        monkeypatch.setattr(
            ir,
            "_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED",
            True,
        )
        monkeypatch.setattr(
            exception_lowering,
            "_DIRECT_INLINE_ERROR_EDGE_ENABLED",
            True,
        )
    else:
        monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", raising=False)
        monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", raising=False)
        monkeypatch.setattr(
            ir,
            "_DIRECT_INLINE_ERROR_EDGE_CAPTURE_ENABLED",
            False,
        )
        monkeypatch.setattr(
            exception_lowering,
            "_DIRECT_INLINE_ERROR_EDGE_ENABLED",
            False,
        )
    ast_module = parse_and_lift(
        _FRONTEND_INLINE_EDGE_SOURCE, "<inline-edge>", "inline_edge"
    )
    typed = type_infer.infer_module(ast_module)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    rendered = codegen.generate(typed)
    return codegen.module, rendered


def test_frontend_post_call_error_check_uses_inline_direct_edge(monkeypatch):
    def generate(*, direct: bool):
        return _generate_frontend_module(monkeypatch, direct=direct)

    direct_source, _direct_rendered = generate(direct=True)
    direct = direct_source.direct_indexed_module()
    stats = direct_indexed_module_cfg_stats(direct)
    assert stats["inline_error_edges"] > 0
    assert stats["call_cont_blocks"] == 0
    assert stats["call_err_cleanup_blocks"] > 0
    assert stats["functions"] == len(direct.functions)
    assert stats["blocks"] == sum(
        len(get_indexed_function_kernel(function).block_names)
        for function in direct.functions
    )
    for function in direct.functions:
        verify_parsed_function(function)
    direct_asm = emit_aarch64_darwin_indexed_module(direct, optimize=False)
    assert "  cbnz w9," in direct_asm

    _text_source, text = generate(direct=False)
    text_labels = re.findall(r"^([A-Za-z_.][^ :]*):", text, re.MULTILINE)
    assert any(label.startswith("call.cont") for label in text_labels)
    assert (
        sum(1 for label in text_labels if label.startswith("call.err.cleanup"))
        == stats["call_err_cleanup_blocks"]
    )
    assert emit_aarch64_darwin_asm(text, optimize=False)


def test_frontend_edge_only_cleanup_blocks_keep_safepoint_records(monkeypatch):
    """A block reached only through inline edges must still be planned.

    The root-state worklist once reused the previous block's edge span, so
    edge-only successors (every ``call.err.cleanup`` block) received no entry
    state and safepoint planning silently skipped them: their calls had no
    stack-map records at all.  Every planned call inside such a block must
    now own a suffix route.
    """
    from pcc.backend import self_backend_aarch64_darwin as aarch64
    from pcc.backend import self_backend_precise_stackmaps as precise_stackmaps

    direct_source, _rendered = _generate_frontend_module(monkeypatch, direct=True)
    direct = direct_source.direct_indexed_module()
    prepared = aarch64.prepare_parsed_module_for_target(
        direct,
        aggregate_returned_indirect=aarch64._aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=(
            aarch64._aggregate_returned_indirect_indexed
        ),
        materialize_legacy_slots=False,
    )
    plans = {
        plan.function_name: plan
        for plan in precise_stackmaps.build_stack_map_plans(
            prepared.functions, prepared.globals_, target="aarch64-darwin"
        )
    }
    checked_blocks = 0
    for function in prepared.functions:
        kernel = get_indexed_function_kernel(function)
        plan = plans[function.name]
        packed = plan.packed_records
        routed_blocks = set()
        for route_index in range(len(packed.suffix_routes) // 3):
            routed_blocks.add(packed.suffix_route(route_index).first)
        for block_id, name in enumerate(kernel.block_names):
            if not name.startswith("call.err.cleanup"):
                continue
            planned = False
            for instruction_index in range(kernel.instruction_count(block_id)):
                metadata = kernel.instruction_metadata_by_id(
                    kernel.block_fact(block_id).first + instruction_index
                )
                if metadata.first != precise_stackmaps.PARSED_INSTRUCTION_KIND_CALL:
                    continue
                if precise_stackmaps._native_record_kind(
                    kernel, metadata.second
                ).first >= 0:
                    planned = True
            if planned:
                assert block_id in routed_blocks, (function.name, name)
                checked_blocks += 1
    assert checked_blocks > 0


def test_frontend_ir_module_helpers_have_matching_static_exports():
    """Frontend uses of ``IRBuilder_*`` module helpers must be self-host resolvable.

    The compiled stage resolves a module-level ``pcc.llvm_capi.ir`` helper only
    when it is imported by name and listed in
    ``layer1_support._build_static_native_exports()`` with the real positional
    arity; the attribute form ``ir.IRBuilder_x(...)`` falls back to a dynamic
    ``ir`` lookup that has no runtime object (pcc1 class_gen worker:
    ``NameError: name 'ir' is not defined`` once the inline-edge flag made
    that branch execute).
    """
    import inspect
    from pathlib import Path

    from pcc.py_frontend.codegen import layer1_support

    codegen_dir = Path(ir.__file__).resolve().parents[1] / "py_frontend" / "codegen"
    attribute_form: dict[str, list[str]] = {}
    imported: set[str] = set()
    for source in codegen_dir.glob("*.py"):
        text = source.read_text()
        hits = re.findall(r"\bir\.(IRBuilder_[A-Za-z0-9_]+)\(", text)
        if hits:
            attribute_form[source.name] = sorted(set(hits))
        for block in re.findall(
            r"from pcc\.llvm_capi\.ir import \(([^)]*)\)", text, re.S
        ):
            imported.update(re.findall(r"\b(IRBuilder_[A-Za-z0-9_]+)", block))
        imported.update(
            re.findall(r"from pcc\.llvm_capi\.ir import (IRBuilder_[A-Za-z0-9_]+)\b", text)
        )
    assert not attribute_form, (
        "ir.IRBuilder_* attribute calls are not resolvable under pcc1; import "
        "the helper by name: " + repr(attribute_form)
    )
    assert imported, "expected IRBuilder_* helpers imported by name in the frontend"
    # A name-imported helper resolves as a direct call to its compiled
    # function, so it needs no static export entry (IRBuilder_publish_direct_raw_call
    # is such a case).  When a helper IS in the export table, its declared
    # positional arity must match the real function, or a call site with the
    # extra argument mis-types and falls back to the dynamic path.
    exports = layer1_support._build_static_native_exports()["pcc.llvm_capi.ir"]
    problems = []
    for name in sorted(imported):
        export = exports.get(name)
        if export is None:
            continue
        real_params = [
            param
            for param in inspect.signature(getattr(ir, name)).parameters.values()
            if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        ]
        if len(export["param_types"]) != len(real_params):
            problems.append(
                name
                + ": export lists "
                + str(len(export["param_types"]))
                + " positional parameters, the function has "
                + str(len(real_params))
            )
    assert not problems, problems


def _kernel_signature(function) -> dict:
    kernel = get_indexed_function_kernel(function)
    result = {
        "block_names": list(kernel.block_names),
        "value_names": list(kernel.value_names),
        "types": [value.describe() for value in kernel.types],
        "value_type_ids": list(kernel.value_type_ids),
        "alloca_type_ids": list(kernel.alloca_type_ids),
        "definition_blocks": list(kernel.definition_blocks),
        "definition_position_values": list(kernel.definition_position_values),
        "diagnostic_instructions": [],
    }
    block_id = 0
    while block_id < len(kernel.block_names):
        instruction_index = 0
        block_instructions = []
        while instruction_index < kernel.instruction_count(block_id):
            instruction = kernel.diagnostic_instruction(
                block_id,
                instruction_index,
            )
            block_instructions.append(
                (
                    instruction.kind,
                    instruction.data,
                    instruction.is_volatile,
                    instruction.arithmetic_flags,
                )
            )
            instruction_index += 1
        result["diagnostic_instructions"].append(block_instructions)
        block_id += 1
    for name in _ARENA_FIELDS:
        result[name] = getattr(kernel, name).diagnostic_values()
    return result


def test_direct_builder_topology_publishes_the_same_final_kernel_as_text(
    monkeypatch,
):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-kernel")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    callee = ir.Function(module, ir.FunctionType(i64, [i64]), name="callee")
    function = ir.Function(
        module,
        ir.FunctionType(i64, [i64, i64]),
        name="choose",
    )
    function.args[0].name = "left"
    function.args[1].name = "right"

    entry = function.append_basic_block("entry")
    true_block = function.append_basic_block("true")
    false_block = function.append_basic_block("false")
    merge = function.append_basic_block("merge")
    builder = ir.IRBuilder(entry)
    slot = builder.alloca(i64, name="slot")
    builder.store(function.args[0], slot)
    loaded = builder.load(slot, name="loaded")
    condition = builder.icmp_signed(">", loaded, function.args[1], name="cmp")
    builder.cbranch(condition, true_block, false_block)

    builder.position_at_end(true_block)
    called = builder.call(callee, [loaded], name="called")
    builder.branch(merge)
    true_end = builder.block

    builder.position_at_end(false_block)
    summed = builder.add(loaded, function.args[1], name="summed")
    builder.branch(merge)
    false_end = builder.block

    builder.position_at_end(merge)
    selected = builder.phi(i64, name="selected")
    ir.IRBuilder_add_incoming(selected, called, true_end)
    ir.IRBuilder_add_incoming(selected, summed, false_end)
    builder.ret(selected)

    direct = build_direct_indexed_function(function)
    parsed_module = parse_self_backend_module(str(module))
    parsed = next(value for value in parsed_module.functions if value.name == "choose")

    assert direct.blocks == []
    assert parsed.blocks == []
    assert _kernel_signature(direct) == _kernel_signature(parsed)
    assert direct.ret_type.describe() == parsed.ret_type.describe() == "i64"
    assert [(arg.name, arg.type.describe()) for arg in direct.args] == [
        (arg.name, arg.type.describe()) for arg in parsed.args
    ]

    direct_module = module.direct_indexed_module()
    assert module._direct_indexed_supported_records > 0
    assert module._direct_indexed_fallback_records == 0
    assert direct_module.triple == parsed_module.triple
    assert direct_module.globals_ == parsed_module.globals_
    assert [value.name for value in direct_module.functions] == ["choose"]
    assert _kernel_signature(direct_module.functions[0]) == _kernel_signature(parsed)
    assert emit_aarch64_darwin_indexed_module(
        direct_module, optimize=False
    ) == emit_aarch64_darwin_asm(str(module), optimize=False)


def test_exact_small_arity_calls_bypass_generic_lists_and_match_direct_kernel(
    monkeypatch,
):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setattr(ir, "_DEBUG_IR_CALL_TRACE_ENABLED", False)

    def build(use_fixed: bool):
        module = ir.Module(name="direct-fixed-call")
        module.triple = "arm64-apple-darwin23.6.0"
        i32 = ir.IntType(32)
        i64 = ir.IntType(64)
        call0 = ir.Function(
            module,
            ir.FunctionType(ir.VoidType(), []),
            name="call0",
        )
        call1 = ir.Function(
            module,
            ir.FunctionType(i64, [i32]),
            name="call1",
        )
        call2 = ir.Function(
            module,
            ir.FunctionType(i32, [i32], var_arg=True),
            name="call2",
        )
        caller = ir.Function(
            module,
            ir.FunctionType(ir.VoidType(), []),
            name="caller",
        )
        block = caller.append_basic_block("entry")
        builder = ir.IRBuilder(block)
        arg0 = ir.Constant(i32, 7)
        arg1 = ir.Constant(i64, 9)
        if use_fixed:
            ir.IRBuilder_call0(builder, call0)
            ir.IRBuilder_call1(builder, call1, arg0)
            ir.IRBuilder_call2(builder, call2, arg0, arg1)
        else:
            ir._irbuilder_call_from_args_list(builder, call0, [])
            ir._irbuilder_call_from_args_list(builder, call1, [arg0])
            ir._irbuilder_call_from_args_list(builder, call2, [arg0, arg1])
        builder.ret_void()
        return module.direct_indexed_module()

    generic = build(use_fixed=False)
    original_generic = ir._irbuilder_call_from_args_list

    def reject_generic(*_args, **_kwargs):
        raise AssertionError("fixed direct call entered generic list helper")

    monkeypatch.setattr(ir, "_irbuilder_call_from_args_list", reject_generic)
    fixed = build(use_fixed=True)
    monkeypatch.setattr(ir, "_irbuilder_call_from_args_list", original_generic)

    assert _kernel_signature(fixed.functions[0]) == _kernel_signature(
        generic.functions[0]
    )
    assert emit_aarch64_darwin_indexed_module(
        fixed, optimize=False
    ) == emit_aarch64_darwin_indexed_module(generic, optimize=False)


def test_direct_builder_float_records_match_text_and_assembly(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-float-kernel")
    module.triple = "arm64-apple-darwin23.6.0"
    double = ir.DoubleType()
    function = ir.Function(
        module,
        ir.FunctionType(ir.IntType(1), [double, double]),
        name="float_compare",
    )
    function.args[0].name = "left"
    function.args[1].name = "right"

    entry = function.append_basic_block("entry")
    builder = ir.IRBuilder(entry)
    summed = builder.fadd(function.args[0], function.args[1], name="summed")
    negated = builder.fneg(summed, name="negated")
    compared = builder.fcmp_ordered(
        ">=", negated, function.args[1], name="compared"
    )
    builder.ret(compared)

    direct = build_direct_indexed_function(function)
    parsed_module = parse_self_backend_module(str(module))
    parsed = next(
        value for value in parsed_module.functions if value.name == "float_compare"
    )

    assert _kernel_signature(direct) == _kernel_signature(parsed)
    direct_module = module.direct_indexed_module()
    assert module._direct_indexed_supported_records == 4
    assert module._direct_indexed_fallback_records == 0
    assert emit_aarch64_darwin_indexed_module(
        direct_module, optimize=False
    ) == emit_aarch64_darwin_asm(str(module), optimize=False)


def test_direct_gep_reuses_pinned_derived_pointer_type_id(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-gep-type-id")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    array_type = ir.ArrayType(i64, 4)
    function = ir.Function(
        module,
        ir.FunctionType(i64, []),
        name="read_two",
    )
    entry = function.append_basic_block("entry")
    builder = ir.IRBuilder(entry)
    slot = builder.alloca(array_type, name="slot")
    zero = ir.Constant(i64, 0)
    first = builder.gep(slot, [zero, ir.Constant(i64, 1)], name="first")
    direct_builder = function._direct_indexed_builder
    assert direct_builder is not None
    derived_type_count = len(direct_builder.derived_pointer_type_ids)
    second = builder.gep(slot, [zero, ir.Constant(i64, 2)], name="second")
    assert len(direct_builder.derived_pointer_type_ids) == derived_type_count
    loaded_first = builder.load(first, name="loaded_first")
    loaded_second = builder.load(second, name="loaded_second")
    builder.ret(builder.add(loaded_first, loaded_second, name="total"))

    direct_module = module.direct_indexed_module()
    parsed_module = parse_self_backend_module(str(module))
    assert _kernel_signature(direct_module.functions[0]) == _kernel_signature(
        parsed_module.functions[0]
    )
    assert emit_aarch64_darwin_indexed_module(
        direct_module, optimize=False
    ) == emit_aarch64_darwin_asm(str(module), optimize=False)


def test_direct_only_instruction_string_uses_static_opname(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    module = ir.Module(name="direct-record-opname")
    i64 = ir.IntType(64)
    function = ir.Function(
        module,
        ir.FunctionType(i64, []),
        name="read_record",
    )
    entry = function.append_basic_block("entry")
    builder = ir.IRBuilder(entry)
    builder.alloca(i64, name="slot")
    builder.ret(ir.Constant(i64, 1))

    assert [str(record) for record in entry.instructions] == ["alloca", "ret"]


def test_scaffold_switch_case_publishes_direct_edge(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-scaffold-switch")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    function = ir.Function(
        module,
        ir.FunctionType(i64, [i64]),
        name="choose_switch",
    )
    entry = function.append_basic_block("entry")
    default = function.append_basic_block("default")
    case = function.append_basic_block("case")
    builder = ir.IRBuilder(entry)
    switch = builder.switch(function.args[0], default)
    ir.scaffold_SwitchInstr_add_case_i64(switch, 1, case)
    builder.position_at_end(default)
    builder.ret(ir.Constant(i64, 0))
    builder.position_at_end(case)
    builder.ret(ir.Constant(i64, 1))

    direct_module = module.direct_indexed_module()
    parsed_module = parse_self_backend_module(str(module))

    assert _kernel_signature(direct_module.functions[0]) == _kernel_signature(
        parsed_module.functions[0]
    )
    assert emit_aarch64_darwin_indexed_module(
        direct_module, optimize=False
    ) == emit_aarch64_darwin_asm(str(module), optimize=False)


def test_direct_module_libpython_scan_reads_structured_calls(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-libpython-scan")
    i64 = ir.IntType(64)
    fallback = ir.Function(
        module,
        ir.FunctionType(i64, [i64]),
        name="py_cpy_probe",
    )
    function = ir.Function(
        module,
        ir.FunctionType(i64, [i64]),
        name="caller",
    )
    entry = function.append_basic_block("entry")
    builder = ir.IRBuilder(entry)
    result = builder.call(fallback, [function.args[0]], name="result")
    builder.ret(result)

    direct_module = module.direct_indexed_module()

    assert direct_indexed_module_first_libpython_edge(
        direct_module
    ) == "caller -> py_cpy_probe"
    assert direct_indexed_module_needs_libpython(direct_module)


def test_direct_module_libpython_scan_ignores_unreachable_call_records(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-unreachable-libpython-scan")
    i64 = ir.IntType(64)
    fallback = ir.Function(
        module,
        ir.FunctionType(i64, [i64]),
        name="py_cpy_probe",
    )
    function = ir.Function(
        module,
        ir.FunctionType(i64, [i64]),
        name="caller",
    )
    entry = function.append_basic_block("entry")
    dead = function.append_basic_block("dead")
    builder = ir.IRBuilder(entry)
    builder.ret(function.args[0])
    builder.position_at_end(dead)
    result = builder.call(fallback, [function.args[0]], name="result")
    builder.ret(result)

    direct_module = module.direct_indexed_module()

    assert direct_indexed_module_first_libpython_edge(direct_module) == ""
    assert not direct_indexed_module_needs_libpython(direct_module)


def test_classgen_dynamic_getattr_publishes_direct_raw_call(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="direct-classgen-getattr")
    ptr = ir.PointerType(ir.IntType(8))
    function = ir.Function(
        module,
        ir.FunctionType(ptr, [ptr]),
        name="read_attr",
    )
    entry = function.append_basic_block("entry")

    class Name:
        ident = "obj"

    class Attr:
        obj = Name()
        name = "value"
        span = None

    class Parent:
        def __init__(self):
            self.module = module
            self.builder = ir.IRBuilder(entry)
            self.env = {}
            self._module_globals = {}
            self._attr_pool = {}

        def _emit_name(self, _expr):
            return function.args[0]

        def _emit_expr(self, _expr):
            return function.args[0]

        def _emit_attribute_error_if_null(self, *_args):
            return None

    parent = Parent()
    result = _classgen_emit_dynamic_attr_value(parent, Attr())
    parent.builder.ret(result)

    direct_module = module.direct_indexed_module()

    assert module._direct_indexed_fallback_records == 0
    assert not direct_indexed_module_needs_libpython(direct_module)


def test_direct_publication_uses_exact_static_abi_in_stage1_context(tmp_path):
    """The compiled frontend must not bind direct-builder methods dynamically."""
    import re
    import os
    from pathlib import Path

    from pcc.py_frontend import pipeline

    repo = Path(__file__).absolute().parents[2]
    named_output = os.environ.get("PCC_CONTEXTUAL_IR_OUTPUT")
    if named_output:
        tmp_path = Path(named_output).resolve()
        assert not tmp_path.exists(), "contextual evidence output must be fresh"
        tmp_path.mkdir(parents=True)
    print("contextual IR output: " + str(tmp_path), flush=True)
    entry = str(repo / "pcc" / "__main__.py")
    srcs, mods = pipeline._collect_relative_module_closure(
        entry,
        include_same_package_absolute=True,
        recurse_same_package_absolute=True,
    )
    srcs, mods = pipeline._filter_ir_scaffold_closure(
        srcs,
        mods,
        ir_scaffold_mode="on",
    )
    seen = {module_name: source for source, module_name in zip(srcs, mods)}
    pipeline._expand_native_extension_module_object_ports(srcs, mods, seen)
    srcs, mods = pipeline._prepare_multi_source_compile_closure(
        srcs,
        mods,
        recursive_stdlib=True,
        ir_scaffold_mode="on",
    )
    assert len(srcs) == len(mods) == len(set(mods))
    # C compilation and linking now belong to the compiler closure. The old
    # 228-file snapshot predates those owners; keep the ABI assertions below
    # tied to the actual closure instead of an unrelated historical count.
    assert "pcc.evaluater.c_evaluator" in mods
    assert "pcc.backend.macho_exec" in mods
    assert "pcc.backend.self_backend_aarch64_fragments" in mods

    targets = {
        "pcc.backend.self_backend_aarch64_fragments",
        "pcc.backend.self_backend_aarch64_darwin",
        "pcc.backend.self_backend_aarch64_darwin_addr",
        "pcc.backend.self_backend_aarch64_darwin_branch_protection",
        "pcc.backend.self_backend_aarch64_darwin_calls",
        "pcc.backend.self_backend_aarch64_darwin_compute",
        "pcc.backend.self_backend_aarch64_darwin_flow",
        "pcc.backend.self_backend_aarch64_darwin_materialize",
        "pcc.backend.self_backend_aarch64_darwin_mem",
        "pcc.backend.self_backend_aarch64_darwin_memory",
        "pcc.backend.self_backend_aarch64_darwin_ops",
        "pcc.backend.self_backend_aarch64_darwin_prologue",
        "pcc.backend.self_backend_aarch64_darwin_regalloc",
        "pcc.backend.self_backend_aarch64_darwin_regs",
        "pcc.backend.self_backend_aarch64_darwin_slots",
        "pcc.backend.self_backend_aarch64_darwin_terminators",
        "pcc.backend.self_backend_analysis",
        "pcc.backend.self_backend_emit",
        "pcc.backend.self_backend_ir",
        "pcc.backend.self_backend_indexed_codec",
        "pcc.backend.self_backend_indexed_emit",
        "pcc.backend.self_backend_kernel",
        "pcc.backend.self_backend_precise_stackmaps",
        "pcc.backend.self_backend_stackprep",
        "pcc.backend.self_backend_verify",
        "pcc.backend.arm64_asm_driver",
        "pcc.backend.arm64_encode",
        "pcc.backend.macho_obj",
        "pcc.backend.macho_spec",
        "pcc.backend.native_object",
        "pcc.py_frontend.codegen.assignment_statement_lowering",
        "pcc.py_frontend.codegen.assignment_store_lowering",
        "pcc.py_frontend.codegen.class_gen",
        "pcc.py_frontend.codegen.comprehension_lowering",
        "pcc.py_frontend.codegen.exact_int_lowering",
        "pcc.py_frontend.codegen.generation_lowering",
        "pcc.py_frontend.codegen.hoist_boxing",
        "pcc.py_frontend.codegen.layer1_init",
        "pcc.py_frontend.codegen.module_global_lowering",
        "pcc.py_frontend.codegen.native_text_modules",
        "pcc.py_frontend.pipeline_frontend_worker_execution",
        "pcc.py_frontend.pipeline_frontend_parallel",
        "pcc.py_frontend.pipeline_self_link",
        "pcc.py_frontend.pipeline_self_backend_link",
        "pcc.py_frontend.pipeline",
        "pcc.llvm_capi.ir",
        "pcc.llvm_capi.direct_indexed_kernel",
    }
    assert targets.issubset(set(mods))
    counts = pipeline.compile_contextual_per_module_fallback_counts(
        srcs,
        mods,
        targets,
        ir_scaffold_mode="on",
        strict_no_libpython=True,
        emit_ir_dir=str(tmp_path),
    )
    assert counts == {module_name: 0 for module_name in targets}

    parallel_ir = (
        tmp_path / "pcc_py_frontend_pipeline_frontend_parallel.ll"
    ).read_text(encoding="utf-8")
    parallel_match = re.search(
        r"define external ptr "
        r"@user_pcc_py_frontend_pipeline_frontend_parallel_"
        r"compile_parallel_uncached\([^)]*\) \{(.+?)\n\}",
        parallel_ir,
        re.DOTALL,
    )
    assert parallel_match is not None
    assert "strict.nolib.stub" not in parallel_match.group(1)

    link_ir = (tmp_path / "pcc_py_frontend_pipeline_self_backend_link.ll").read_text()
    owned_link = re.search(
        r"define [^\n]+@user_pcc_py_frontend_pipeline_self_backend_link_"
        r"_owned_macho_link_in_process\([^\n]*\) \{\n(.*?)\n\}", link_ir, re.S,
    )
    assert owned_link is not None
    assert "strict.nolib.stub" not in owned_link.group(1)

    caller_ir = (tmp_path / "pcc_llvm_capi_ir.ll").read_text(encoding="utf-8")
    callee_ir = (
        tmp_path / "pcc_llvm_capi_direct_indexed_kernel.ll"
    ).read_text(encoding="utf-8")
    assembler_ir = (
        tmp_path / "pcc_backend_arm64_asm_driver.ll"
    ).read_text(encoding="utf-8")
    macho_obj_ir = (
        tmp_path / "pcc_backend_macho_obj.ll"
    ).read_text(encoding="utf-8")
    native_object_ir = (
        tmp_path / "pcc_backend_native_object.ll"
    ).read_text(encoding="utf-8")
    worker_pco_ir = [
        assembler_ir,
        (tmp_path / "pcc_backend_arm64_encode.ll").read_text(encoding="utf-8"),
        macho_obj_ir,
        (tmp_path / "pcc_backend_macho_spec.ll").read_text(encoding="utf-8"),
        native_object_ir,
        (tmp_path / "pcc_backend_self_backend_aarch64_fragments.ll").read_text(encoding="utf-8"),
    ]
    native_emitter_ir = (
        tmp_path / "pcc_backend_self_backend_aarch64_darwin.ll"
    ).read_text(encoding="utf-8")
    assert "strict.nolib.stub:" not in native_emitter_ir
    append_match = re.search(
        r"define [^\n]+@user_pcc_backend_self_backend_aarch64_darwin_"
        r"_NativeAArch64Emission_append\([^\n]*\) \{\n(.*?)\n\}",
        native_emitter_ir, re.S,
    )
    assert append_match is not None
    assert "dyn.attr.get4_unchecked" not in append_match.group(1)
    assert "@user_pcc_backend_self_backend_value_arena_CompilerIntArena_get4_unchecked" in append_match.group(1)
    for method in ("_define_label", "append_encoded"):
        method_match = re.search(
            r"define [^\n]+@user_pcc_backend_arm64_asm_driver_AArch64ModuleBuilder_"
            + method + r"\([^\n]*\) \{\n(.*?)\n\}", assembler_ir, re.S,
        )
        assert method_match is not None, method
        method_body = method_match.group(1)
        current_loads = re.findall(
            r"%self\.current\.[^\n]* = call ptr [^\n]*@py_instance_get_field\(",
            method_body,
        )
        assert len(current_loads) == 1, (method, len(current_loads))
        assert "@py_obj_getattr(" not in method_body, method
        assert "@py_obj_setattr(" not in method_body, method
        assert "@user_pcc_backend_arm64_asm_driver__SectionBuffer_is_text(" in method_body, method
        assert "strict.nolib.stub" not in method_body, method
    from pcc.ir_diff import IrSummary

    fragment_edges = (
        ("self_backend_aarch64_fragments", "AArch64EmissionFragments__append_record",
         "user_pcc_backend_self_backend_value_arena_CompilerIntArena_append4"),
        ("self_backend_aarch64_fragments", "AArch64EmissionFragments_new_fragment",
         "user_pcc_backend_self_backend_value_arena_CompilerRecordSpanArena_new_span"),
        ("self_backend_aarch64_darwin", "_NativeAArch64Emission_publish_fragment",
         "user_pcc_backend_self_backend_value_arena_CompilerIntArena_get4_unchecked"),
        ("self_backend_aarch64_darwin_slots", "append_slot_base_address_parts",
         "user_pcc_backend_self_backend_aarch64_darwin_regs_append_add_offset"),
        ("self_backend_precise_stackmaps", "FunctionStackMapPlan__append_reload_span_packed",
         "user_pcc_backend_self_backend_aarch64_darwin_slots_append_load_slot_to_reg_parts"),
    )
    for module_name, function_name, callee in fragment_edges:
        ir_text = (tmp_path / ("pcc_backend_" + module_name + ".ll")).read_text()
        symbol = "user_pcc_backend_" + module_name + "_" + function_name
        function = IrSummary.parse(ir_text).functions[symbol]
        assert callee in function.calls, symbol
        assert not any(call.startswith(("py_obj_call", "py_valuebox_")) for call in function.calls), symbol
    kernel_ir = (tmp_path / "pcc_backend_self_backend_kernel.ll").read_text(encoding="utf-8")
    for getter in (
        "block_fact", "block_phi_fact", "call_arg", "call_header", "call_span",
        "gep_header", "gep_index", "gep_span", "inline_error_edge_span",
        "instruction_fact", "instruction_fact_by_id", "instruction_metadata_by_id",
        "instruction_record", "phi_incoming", "phi_record", "terminator_case",
        "terminator_header", "terminator_span",
    ):
        getter_match = re.search(
            r"define [^\n]+@user_pcc_backend_self_backend_kernel_IndexedFunctionKernel_"
            + getter + r"\([^\n]*\) \{\n(.*?)\n\}", kernel_ir, re.S,
        )
        assert getter_match is not None, getter
        getter_body = getter_match.group(1)
        assert "@user_pcc_backend_self_backend_value_arena_CompilerIntArena_" in getter_body, getter
        assert "@py_obj_call" not in getter_body, getter
        assert "@py_valuebox_get_field" not in getter_body, getter
    pipeline_ir = (
        tmp_path / "pcc_py_frontend_pipeline.ll"
    ).read_text(encoding="utf-8")
    assert "classgen.arg.ARM64_RELOC_UNSIGNED" not in assembler_ir
    assert not re.search(
        r"py_obj_getattr\([^\n]*@\.pyattr\.ARM64_RELOC_UNSIGNED",
        assembler_ir,
    )
    assert re.search(r"call ptr .*@py_obj_and\(", assembler_ir)
    assert re.search(r"call ptr .*@py_int_shl\(", assembler_ir)
    assert not re.search(r"shl i64 1, %mul\.", assembler_ir)
    assert not re.search(
        r"define external void "
        r"@user_pcc_py_frontend_pipeline__record_macho_link_profile\([^\n]*\) "
        r"\{\nstrict\.nolib\.stub:",
        pipeline_ir,
    )
    assert "json.load.read" in pipeline_ir
    assert all("strict.nolib.stub:" not in module_ir for module_ir in worker_pco_ir)
    uint64_global = "@.modvar.pcc_backend_native_object._UINT64_MAX"
    assert uint64_global + " = global ptr null" in native_object_ir
    assert not re.search(
        r"store i64 [^\n]*, ptr " + re.escape(uint64_global),
        native_object_ir,
    )
    dynamic_names = "|".join(
        re.escape(method_name)
        for method_name in _DIRECT_STATIC_METHOD_ABI
    )
    assert not re.search(r"%dyn\.attr\.(?:" + dynamic_names + r")\.", caller_ir)

    method_prefix = (
        "user_pcc_llvm_capi_direct_indexed_kernel_"
        "DirectIndexedFunctionBuilder_"
    )
    for method_name in _DIRECT_STATIC_METHOD_ABI:
        method_symbol = method_prefix + method_name
        assert re.search(
            r"\bcall [^\n]*@" + re.escape(method_symbol) + r"\(",
            caller_ir,
        ), method_name
        assert "define external" in callee_ir
        assert "@" + method_symbol + "(" in callee_ir
    assert not re.search(
        r"define [^\n]*_direct_(?:publish|append)_.*_exact\(",
        callee_ir,
    )
