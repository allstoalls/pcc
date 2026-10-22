"""Windows packed maps must preserve complete COFF, SEH and public ASM.

These are host object/dispatch controls, not native execution qualification.
"""

import ast
from dataclasses import replace
from pathlib import Path
import weakref

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import coff_x86_64 as coff
from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.precise_stackmap import (
    ARCH_X86_64,
    PreciseStackMapError,
    decode_stack_map,
)
from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
from pcc.backend.self_backend_indexed_emit import emit_indexed_module_file
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_target_match import (
    is_x86_64_linux_triple,
    is_x86_64_windows_triple,
)
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from pcc.backend.x86_64_encode import X86EncodeError
from tests.python.test_precise_stackmap_abi import _target_final_stackmap_ir
from tests.python.test_x86_64_packed_stackmaps import (
    MARKER,
    NOTE,
    _block,
    _fixture,
    _symbol,
)


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


TARGET = "x86_64-pc-windows-msvc"
ROOTED = _target_final_stackmap_ir(TARGET)
SOURCES = (
    ROOTED,
    f'target triple = "{TARGET}"\ndefine i64 @rax() {{\nentry:\n ret i64 42\n}}',
    f'target triple = "{TARGET}"\n@only_data = global i64 42',
)


@pytest.fixture(autouse=True)
def _fixed_emission_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")
    monkeypatch.setattr(emitter, "_MODULE_SYMBOLS", type(emitter._MODULE_SYMBOLS)(
        internal_prefix="", defined_symbols=frozenset(),
        internal_symbols=frozenset(), thread_local_symbols=frozenset(),
    ))


def _forbid_symbolic_maps(*_args, **_kwargs):
    raise AssertionError("Windows object route projected textual stack maps")


@pytest.mark.parametrize("source", SOURCES, ids=("rooted", "reserved-symbol", "data-only"))
@pytest.mark.parametrize("consume", (False, True))
def test_production_packed_coff_matches_complete_text_object(monkeypatch, source, consume):
    oracle_assembly = emit_indexed_assembly(parse_self_backend_module(source))
    expected = encode_assembly_object(oracle_assembly, TARGET)
    monkeypatch.setattr(emitter, "render_x86_64_stack_map_section", _forbid_symbolic_maps)
    monkeypatch.setattr(maps, "_validate_symbolic_plans", _forbid_symbolic_maps)
    monkeypatch.setattr(maps, "_stack_locations", _forbid_symbolic_maps)
    plans = []
    assembly = emit_indexed_assembly(parse_self_backend_module(source), stack_map_plans_out=plans)
    plan_count = len(plans)
    actual = encode_assembly_object(
        assembly, TARGET, stack_map_plans=plans, consume_stack_map_plans=consume,
    )
    assert actual == expected
    assert len(plans) == (0 if consume else plan_count)
    if plan_count:
        assert assembly.split(MARKER, 1)[1] == NOTE
        obj = coff.parse_object(actual)
        sections = {section.name: section for section in obj.sections}
        assert sections[".pdata"].data and sections[".xdata"].data
        stack_maps = sections[".pcc_stackmaps"]
        decoded = decode_stack_map(stack_maps.data, expected_arch=ARCH_X86_64)
        assert len(decoded.functions) == plan_count
        assert len(stack_maps.relocations) == plan_count
        assert all(rel.kind == coff.ADDR64 for rel in stack_maps.relocations)
        if source == ROOTED:
            assert any(record.locations for function in decoded.functions for record in function.records)


def _coff_fixture():
    assembly, plans = _fixture()
    assembly = assembly.replace("probe:\n", ".seh_proc probe\nprobe:\n.seh_endprologue\n", 1)
    assembly = assembly.replace(".Lprobe_end:\n", ".Lprobe_end:\n.seh_endproc\n", 1)
    assembly += ".data\n.globl payload\npayload:\n .quad external+8\n"
    rendered = maps.render_x86_64_stack_map_section(
        assembly.splitlines(), plans, function_symbol=_symbol, block_label=_block,
    )
    expected = coff.assemble_object(assembly + "\n".join(rendered) + "\n" + NOTE)
    return assembly + MARKER + NOTE, plans, expected


def test_all_record_kinds_high_ids_and_seh_markers_keep_complete_coff():
    assembly, plans, expected = _coff_fixture()
    actual = coff.assemble_object(assembly, stack_map_plans=plans)
    assert actual == expected
    obj = coff.parse_object(actual)
    sections = {section.name: section for section in obj.sections}
    decoded = decode_stack_map(sections[".pcc_stackmaps"].data, expected_arch=ARCH_X86_64)
    assert decoded.functions[0].records[0].safepoint_id == 0xFFFFFFFFFFFFFFFF
    assert decoded.functions[0].records[3].continuation_id == 0xFFFFFFFF
    assert len(sections[".pdata"].relocations) == 3
    assert all(rel.kind == coff.ADDR32NB for rel in sections[".pdata"].relocations)
    assert any(symbol.name.startswith(".Lpcc_seh_") for symbol in obj.symbols)
    data_relocation = sections[".data"].relocations[0]
    assert data_relocation.kind == coff.ADDR64
    assert obj.symbols[data_relocation.symbol].name == "external"
    assert int.from_bytes(sections[".data"].data, "little") == 8


@pytest.mark.parametrize("invalid", ("missing-marker", "nonempty-marker", "missing-label", "bad-root"))
def test_packed_coff_preserves_final_offset_and_schema_validation(invalid):
    assembly, plans, _expected = _coff_fixture()
    if invalid == "missing-marker":
        assembly = assembly.replace(MARKER, "")
        error = X86EncodeError
    elif invalid == "nonempty-marker":
        assembly = assembly.replace(MARKER, MARKER + ".byte 0\n")
        error = X86EncodeError
    elif invalid == "missing-label":
        assembly = assembly.replace(".Lprobe_call:\n", "")
        error = BackendUnavailable
    else:
        records = tuple(
            replace(record, locations=(maps.PlannedRootLocation(-(1 << 31) - 1, True),))
            if record.locations else record
            for record in plans[0].records
        )
        plans = (replace(plans[0], records=records),)
        error = PreciseStackMapError
    with pytest.raises(error):
        coff.assemble_object(assembly, stack_map_plans=plans)


def test_consumption_retires_plans_before_finishing_sections(monkeypatch):
    assembly, original, expected = _coff_fixture()
    plans = list(original)
    references = [weakref.ref(plan) for plan in original]
    del original
    real_pack = maps.build_x86_64_stack_map_payload
    real_finish = assembler._finish_section
    packed = []
    observed = []

    def pack(*args, **kwargs):
        result = real_pack(*args, **kwargs)
        packed.append(True)
        return result

    def finish(*args, **kwargs):
        assert packed
        assert plans == []
        assert all(reference() is None for reference in references)
        observed.append(True)
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(maps, "build_x86_64_stack_map_payload", pack)
    monkeypatch.setattr(assembler, "_finish_section", finish)
    assert coff.assemble_object(
        assembly, stack_map_plans=plans, consume_stack_map_plans=True,
    ) == expected
    assert packed == [True] and observed and plans == []


@pytest.mark.parametrize("artifact_kind", ("ASM", "PCO"))
def test_indexed_file_emitter_preserves_asm_and_packs_windows_pco(tmp_path, monkeypatch, artifact_kind):
    expected_assembly = emit_indexed_assembly(parse_self_backend_module(ROOTED))
    expected = (expected_assembly.encode("utf-8") if artifact_kind == "ASM"
                else encode_assembly_object(expected_assembly, TARGET))
    module = parse_self_backend_module(ROOTED)
    sidecar = tmp_path / "module.pidx"
    try:
        encode_indexed_module_file(str(sidecar), module)
    finally:
        for function in module.functions:
            get_indexed_function_kernel(function).close_native_tables()
    if artifact_kind == "PCO":
        monkeypatch.setattr(emitter, "render_x86_64_stack_map_section", _forbid_symbolic_maps)
    output = tmp_path / "module.output"
    emit_indexed_module_file(str(sidecar), str(output), artifact_kind)
    assert output.read_bytes() == expected
    assert not (tmp_path / "module.output.tmp").exists()


def test_worker_selects_packed_maps_only_for_eligible_x86_object_routes(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2]
    tree = ast.parse((source / "pcc/frontends/python/pipeline_frontend_worker_execution.py").read_text())
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "direct_packed_stack_maps"
                for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "bool"
    ]
    assert len(assignments) == 1
    expression = compile(ast.Expression(assignments[0].value), "worker-packed-maps", "eval")
    environment = {
        "bool": bool, "emit_direct": True, "indexed_sidecar_output": False,
        "native_object_output": True, "validate_direct": False,
        "emit_text_control": False,
        "is_x86_64_linux_triple": is_x86_64_linux_triple,
        "is_x86_64_windows_triple": is_x86_64_windows_triple,
    }
    for target in (TARGET, "x86_64-unknown-linux-gnu", "arm64-apple-darwin23.6.0"):
        environment["direct_target"] = target
        eligible = target != "arm64-apple-darwin23.6.0"
        assert eval(expression, {"__builtins__": {}}, environment) is eligible
        for name, blocked in (
            ("emit_direct", False), ("indexed_sidecar_output", True),
            ("native_object_output", False), ("validate_direct", True),
            ("emit_text_control", True),
        ):
            assert not eval(expression, {"__builtins__": {}}, {**environment, name: blocked})

    from pcc.frontends.python import pipeline
    from tests.python.test_direct_indexed_worker_validation import _validation_worker

    observed = []
    assemble = assembler.assemble_builder

    # The worker streams the module into its sections; packed maps reach the
    # owned packer with the unwind markers, after an empty stack-map section
    # and before the closing note, as the text route places them.
    def traced(builder, keep_labels, plans, **kwargs):
        stack_maps = builder.sections.get(".pcc_stackmaps")
        observed.append((len(plans), bool(keep_labels), tuple(builder.order[-2:]),
                         None if stack_maps is None else stack_maps.size))
        return assemble(builder, keep_labels, plans, **kwargs)

    with monkeypatch.context() as context:
        manifest, result = _validation_worker(tmp_path, context, TARGET, False, False)
        context.setattr(emitter, "render_x86_64_stack_map_section", _forbid_symbolic_maps)
        context.setattr(assembler, "assemble_builder", traced)
        assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    assert observed and observed[0][0] > 0 and observed[0][1]
    assert observed[0][2:] == ((".pcc_stackmaps", ".note.GNU-stack"), 0)
    packed = (tmp_path / "module_0.direct.pco").read_bytes()
    with monkeypatch.context() as context:
        manifest, result = _validation_worker(tmp_path, context, TARGET, True, False)
        assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    assert packed == encode_assembly_object(
        (tmp_path / "module_0.text.s").read_text(), TARGET,
    )


@pytest.mark.parametrize("enabled", (False, True))
def test_packed_coff_keeps_phase_timing_assembly_encode_boundary(enabled):
    assembly, plans, expected = _coff_fixture()

    class Probe:
        def __init__(self):
            self.ids = []

        def start(self):
            return 1

        def add(self, phase, started):
            assert started == 1
            self.ids.append(phase)

        def begin_assembly(self, phase):
            assert phase in (13, 14, 15)
            return self.start()

        def complete_assembly(self, phase, started):
            self.add(phase, started)

    timing = Probe() if enabled else None
    actual = encode_assembly_object(
        assembly, TARGET, stack_map_plans=plans, phase_timing=timing,
    )
    assert actual == expected
    if enabled:
        assert timing.ids == [13, 14, 15, 9, 10]
