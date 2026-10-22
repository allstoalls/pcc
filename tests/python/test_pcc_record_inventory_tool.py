from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "pcc_record_inventory.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("pcc_record_inventory", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_data_plane_class_contract_is_fail_closed_for_new_backend_classes() -> None:
    tool = _load_tool()
    report = tool.data_plane_class_contract_report()
    site_report = tool.diagnostic_projection_site_contract_report()

    assert report["unclassified"] == []
    assert report["stale_classifications"] == []
    assert report["invalid_classifications"] == []
    assert report["discovered_class_count"] == report["classified_class_count"]
    assert report["by_classification"]["native_arena"] >= 1
    assert report["by_classification"]["native_value_record"] >= 1
    assert report["by_classification"]["diagnostic_projection"] >= 1
    assert tool.DATA_PLANE_CLASS_CONTRACT[
        "arm64_encode.py:PackedAArch64TextBuilder"
    ] == "native_arena"
    assert tool.DATA_PLANE_CLASS_CONTRACT[
        "arm64_asm_driver.py:AArch64ModuleBuilder"
    ] == "phase_shell"
    assert tool.DATA_PLANE_CLASS_CONTRACT[
        "self_backend_aarch64_darwin.py:_NativeAArch64Emission"
    ] == "phase_shell"
    assert site_report["unclassified_sites"] == []
    assert site_report["stale_sites"] == []
    assert site_report["count_mismatches"] == []
    assert site_report["invalid_policies"] == []
    assert site_report["sites"][
        "self_backend_precise_stackmaps.py:_frame_protocol_enter_group:_RootGroup"
    ] == {"count": 1, "policy": "legacy_or_unsupported"}


def test_legalization_phase_shells_remain_visible_to_record_inventory() -> None:
    from pcc.backend.self_backend_half import _HalfFunction, _HalfModule
    from pcc.backend.self_backend_wide_int import _WideFunction, _WideModule

    tool = _load_tool()
    for filename, module_type, function_type in (
        ("self_backend_half.py", _HalfModule, _HalfFunction),
        ("self_backend_wide_int.py", _WideModule, _WideFunction),
    ):
        module = module_type("")
        function = function_type(module, ".inventory")
        function.emit("ret void")
        payload = tool._graph_inventory((function,))

        for family in (module_type, function_type):
            assert tool.DATA_PLANE_CLASS_CONTRACT[
                f"{filename}:{family.__name__}"
            ] == "phase_shell"
            assert payload["families"][family.__name__]["unique_objects"] == 1

        list_owners = {
            row["owner"] for row in payload["container_primary_owners"]["list"]
        }
        dict_owners = {
            row["owner"] for row in payload["container_primary_owners"]["dict"]
        }
        assert f"{function_type.__name__}.out" in list_owners
        assert f"{module_type.__name__}.lines" in list_owners
        assert f"{module_type.__name__}.named_types" in dict_owners


def test_source_debug_sidecars_remain_visible_to_record_inventory() -> None:
    from pcc.backend.self_backend_debug import (
        DebugBlock,
        DebugLocation,
        DebugMetadata,
        DebugNode,
        DebugSource,
        FunctionDebugInfo,
    )
    from pcc.backend.self_backend_dwarf import DebugFunctionPlan

    # Construct the actual sidecar owners without invoking the IR parser,
    # unsafe arenas, codec or emitter. Shared coordinates must remain counted
    # once by identity, along with every retained metadata/row container.
    source = DebugSource("example.py", "/fixture")
    location = DebugLocation(source, 7, 3, 0, 2, 3)
    block = DebugBlock("entry", [location], location)
    function = FunctionDebugInfo("sample", "sample", source, 6, 2, 1, [block])
    node = DebugNode("DILocation", {"line": "7", "column": "3", "scope": "!2"})
    metadata = DebugMetadata({3: node})
    metadata.functions[function.symbol] = function
    metadata.locations[3] = location
    metadata.location_owners[3] = function.scope
    metadata.sources[0] = source
    plan = DebugFunctionPlan(metadata, function, "sample", "sample_end")
    assert plan.mark(0, 0) == "L_pcc_dbg_sample_0:"
    assert plan.rows == [("L_pcc_dbg_sample_0", location)]

    tool = _load_tool()
    payload = tool._graph_inventory((plan,))
    for family, category in (
        (DebugNode, "semantic_record"),
        (DebugSource, "semantic_record"),
        (DebugLocation, "semantic_record"),
        (DebugBlock, "phase_shell"),
        (FunctionDebugInfo, "phase_shell"),
        (DebugMetadata, "phase_shell"),
        (DebugFunctionPlan, "phase_shell"),
    ):
        filename = family.__module__.rsplit(".", 1)[1] + ".py"
        assert tool.DATA_PLANE_CLASS_CONTRACT[filename + ":" + family.__name__] == category
        assert payload["families"][family.__name__]["unique_objects"] == 1
        assert payload["families"][family.__name__]["references"] >= 1

    assert payload["containers"] == {"dict": 6, "list": 3, "tuple": 1}
    assert {row["owner"] for row in payload["container_primary_owners"]["list"]} == {
        "DebugBlock.instructions", "FunctionDebugInfo.blocks", "DebugFunctionPlan.rows",
    }
    assert {row["owner"] for row in payload["container_primary_owners"]["dict"]} == {
        "DebugNode.fields", "DebugMetadata.nodes", "DebugMetadata.functions",
        "DebugMetadata.locations", "DebugMetadata.location_owners", "DebugMetadata.sources",
    }


def test_data_plane_class_contract_reports_an_unclassified_new_class(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    (tmp_path / "self_backend_future.py").write_text(
        "class FutureHotRecord:\n"
        "    pass\n\n"
        "def future_hot_path():\n"
        "    return ParsedInstr('future', ())\n",
        encoding="utf-8",
    )
    (tmp_path / "arm64_future.py").write_text(
        "class FutureInstructionRecord:\n    pass\n", encoding="utf-8",
    )

    report = tool.data_plane_class_contract_report(tmp_path)
    site_report = tool.diagnostic_projection_site_contract_report(tmp_path)

    assert report["unclassified"] == [
        "arm64_future.py:FutureInstructionRecord",
        "self_backend_future.py:FutureHotRecord"
    ]
    assert site_report["unclassified_sites"] == [
        "self_backend_future.py:future_hot_path:ParsedInstr"
    ]


def test_record_inventory_counts_identity_free_type_and_slot_records() -> None:
    tool = _load_tool()
    payload = tool.inventory_ir_text(
        """
target triple = "arm64-apple-darwin23.6.0"

define i64 @sum(i64 %left, i64 %right) {
entry:
  %result = add i64 %left, %right
  ret i64 %result
}
""".lstrip()
    )

    assert payload["schema"] == "pcc.compiler-record-inventory.v1"
    assert payload["shape"]["functions"] == 1
    assert payload["shape"]["blocks"] == 1
    assert payload["shape"]["instructions"] == 1
    assert payload["shape"]["kernel_types"] >= 1
    assert payload["shape"]["block_name_index_capacity"] >= 8
    assert payload["shape"]["value_name_index_capacity"] >= 8
    assert payload["shape"]["value_slots"] >= 1
    assert payload["shape"]["legacy_value_slot_map_entries"] == 0
    assert payload["shape"]["legacy_alloca_slot_map_entries"] == 0
    assert payload["instruction_payloads"]["instructions"] == 1
    assert payload["instruction_payloads"]["tuple_references"] == 0
    assert payload["instruction_payloads"]["packed_fixed_records"] == 1
    assert payload["parsed_instruction_payloads"]["instructions"] == 1
    assert payload["parsed_instruction_payloads"]["tuple_references"] == 0
    assert payload["parsed_instruction_payloads"]["packed_payloads"] == 1
    assert payload["parsed_instruction_payloads"]["by_kind"]["binop"][
        "tuple_references"
    ] == 0
    assert payload["instruction_payloads"]["by_kind"]["binop"][
        "instructions"
    ] == 1
    assert payload["shape"]["call_diagnostic_projections"] == 0
    assert payload["shape"]["instruction_diagnostic_projections"] == 0
    assert payload["instruction_projection_by_stage"] == {
        "emitted": 0,
        "parsed": 0,
        "stack_prepared": 0,
        "stackmap_planned": 0,
        "verified": 0,
    }
    assert set(payload["graph_by_stage"]) == {
        "emitted",
        "parsed",
        "stack_prepared",
        "stackmap_planned",
        "verified",
    }
    assert set(payload["type_projection_by_stage"]) == {
        "emitted",
        "parsed",
        "verified",
        "stack_prepared",
        "stackmap_planned",
    }
    assert all(
        all(count == 0 for count in stage.values())
        for stage in payload["record_projection_by_stage"].values()
    )
    for family in (
        "ParsedBlock",
        "CompactParsedInstrArena",
        "ParsedInstr",
        "PhiInstr",
        "PhiIncoming",
        "PlannedRootLocation",
        "PlannedManagedReload",
        "PlannedSafepoint",
        "_PointerOrigin",
        "_RootGroup",
        "_ManagedValueOrigin",
    ):
        assert payload["graph_by_stage"]["parsed"]["families"][family][
            "unique_objects"
        ] == 0
    assert payload["graph"]["families"]["TypeDesc"]["unique_objects"] >= 1
    assert payload["shape"]["legacy_slot_projections"] == 0
    assert payload["stackmap_construction"] == {
        "avoidable_main_active_dict_constructions": 0,
        "avoidable_main_active_dict_group_insertions": 0,
        "blocks": 1,
        "current_main_active_dict_constructions": 0,
        "current_main_active_dict_group_insertions": 0,
        "eager_baseline_main_active_dict_constructions": 1,
        "eager_baseline_main_active_dict_group_insertions": 0,
        "empty_entry_blocks": 0,
        "empty_entry_protocol_blocks": 0,
        "entry_state_contents": 1,
        "entry_state_identities": 0,
        "entry_identity_cache_avoided_group_scans": 0,
        "entry_identity_cache_hits": 0,
        "entry_identity_cache_misses": 0,
        "entry_identity_cache_scanned_group_references": 0,
        "lazy_main_active_dict_constructions": 0,
        "lazy_main_active_dict_group_insertions": 0,
        "max_state_groups": 0,
        "max_state_locations": 0,
        "native_location_records": 0,
        "native_state_ids": 1,
        "nonempty_entry_blocks": 1,
        "protocol_blocks": 0,
        "protocol_calls": 0,
        "reachable_entry_blocks": 1,
        "root_group_identities": 0,
        "root_location_identities": 0,
        "state_group_references": 0,
    }
    assert payload["graph"]["families"]["SlotInfo"]["unique_objects"] == 0
    assert payload["graph"]["families"]["AllocaInfo"]["unique_objects"] == 0
    for kind, count in payload["graph"]["containers"].items():
        assert sum(
            row["unique_objects"]
            for row in payload["graph"]["container_primary_owners"][kind]
        ) == count


def test_record_inventory_counts_parser_published_call_payload() -> None:
    tool = _load_tool()
    payload = tool.inventory_ir_text(
        """
target triple = "arm64-apple-darwin23.6.0"

declare i64 @identity(i64)

define i64 @run(i64 %value) {
entry:
  %result = call i64 @identity(i64 %value)
  ret i64 %result
}
""".lstrip()
    )

    parsed = payload["parsed_instruction_payloads"]
    assert parsed["instructions"] == 1
    assert parsed["tuple_references"] == 0
    assert parsed["packed_payloads"] == 1
    assert parsed["by_kind"]["call"]["packed_payloads"] == 1
    assert parsed["by_kind"]["call"]["top_level_fields"] == 0
    assert payload["shape"]["call_diagnostic_projections"] == 0


def test_parsed_graph_uses_packed_terminators_and_phis() -> None:
    tool = _load_tool()
    payload = tool.inventory_ir_text(
        """
target triple = "arm64-apple-darwin23.6.0"

define i64 @choose(i1 %cond) {
entry:
  br i1 %cond, label %left, label %right
left:
  br label %done
right:
  br label %done
done:
  %value = phi i64 [ 1, %left ], [ 2, %right ]
  ret i64 %value
}
""".lstrip()
    )

    parsed_owners = {
        row["owner"]
        for row in payload["graph_by_stage"]["parsed"][
            "container_primary_owners"
        ]["tuple"]
    }
    verified_owners = {
        row["owner"]
        for row in payload["graph_by_stage"]["verified"][
            "container_primary_owners"
        ]["tuple"]
    }
    assert "ParsedInstr.data" not in parsed_owners
    assert "PhiInstr.incoming" not in parsed_owners
    assert "ParsedBlock.phis" not in parsed_owners
    assert "ParsedInstr.data" not in verified_owners
    assert "PhiInstr.incoming" not in verified_owners
    assert "ParsedBlock.phis" not in verified_owners


def test_stackprep_uses_function_kind_plane_not_blocks_or_per_block_arenas() -> None:
    source = (
        REPO / "pcc/backend/self_backend_stackprep.py"
    ).read_text(encoding="utf-8")
    start = source.index("def assign_stack_slots(")
    body = source[start:]
    assert "for current_block_id, block in enumerate(func.blocks)" not in body
    assert "kernel.instruction_arenas[current_block_id]" not in body
    assert "block_fact: CompilerInt4 = kernel.block_fact(current_block_id)" in body
    assert "kernel.instruction_kind_id_by_id(" in body

    kernel_source = (
        REPO / "pcc/backend/self_backend_kernel.py"
    ).read_text(encoding="utf-8")
    assert "instruction_kind_ids: CompilerIntArena" in kernel_source


def test_verifier_reuses_kernel_definition_position_records() -> None:
    verifier_source = (
        REPO / "pcc/backend/self_backend_verify.py"
    ).read_text(encoding="utf-8")
    kernel_source = (
        REPO / "pcc/backend/self_backend_kernel.py"
    ).read_text(encoding="utf-8")

    assert "class _Definition" not in verifier_source
    assert "_new_definition_table" not in verifier_source
    assert "definitions = kernel" in verifier_source
    assert "definitions.definition_position(value_id)" in verifier_source
    assert "definition_positions: CompilerIntArena" in kernel_source


def test_fused_block_plane_phi_count_reads_the_scalar_lane_directly() -> None:
    source = (
        REPO / "pcc/backend/self_backend_parse.py"
    ).read_text(encoding="utf-8")
    start = source.index("    def phi_count(")
    end = source.index("\n    def ", start + 1)
    body = source[start:end]
    assert "self.block_phi_facts.get_unchecked(block_id * 2 + 1)" in body
    assert "self.phi_fact(block_id).second" not in body
    plane_start = source.index("class _FunctionBlockPlane:")
    plane_end = source.index("\ndef _parse_blocks(", plane_start)
    plane = source[plane_start:plane_end]
    for aggregate_helper in (
        "header",
        "span",
        "case",
        "phi_fact",
        "phi_record",
        "phi_incoming_record",
    ):
        assert f"self.{aggregate_helper}(" not in plane


def test_self_backend_never_reads_a_lane_from_an_inline_aggregate_call() -> None:
    violations: list[str] = []
    for path in sorted((REPO / "pcc/backend").glob("self_backend*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"first", "second", "third", "fourth"}
                and isinstance(node.value, ast.Call)
            ):
                violations.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert violations == []


def test_indexed_emit_consumes_suffix_routes_with_one_monotonic_cursor() -> None:
    source = (REPO / "pcc/backend/self_backend_emit.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def emit_indexed_function_blocks(")
    body = source[start:]
    assert "append_packed_suffix_lines(" not in body
    assert "route_scalar = suffix_route_index * 3" in body
    assert "suffix_route_index += 1" in body

    stackmap_source = (
        REPO / "pcc/backend/self_backend_precise_stackmaps.py"
    ).read_text(encoding="utf-8")
    start = stackmap_source.index("    def append_packed_suffix_lines(")
    end = stackmap_source.index("\n    def ", start + 1)
    suffix_body = stackmap_source[start:end]
    assert "suffix_routes.get3_unchecked(" not in suffix_body


def test_native_stackmap_hot_paths_keep_call_kind_as_an_integer_id() -> None:
    source = (
        REPO / "pcc/backend/self_backend_precise_stackmaps.py"
    ).read_text(encoding="utf-8")
    for function_name in (
        "_native_root_states",
        "_native_managed_liveness",
        "_build_function_stack_map_plan_native",
    ):
        start = source.index(f"def {function_name}(")
        end = source.find("\ndef ", start + 1)
        body = source[start:] if end < 0 else source[start:end]
        assert 'PARSED_INSTRUCTION_KINDS[metadata.first] == "call"' not in body
        assert 'PARSED_INSTRUCTION_KINDS[metadata.first] != "call"' not in body
    liveness_start = source.index("def _native_managed_liveness(")
    liveness_end = source.index("\ndef ", liveness_start + 1)
    liveness = source[liveness_start:liveness_end]
    assert "live_state_id" not in liveness
    assert "current_live_state = CompilerIntArena(1)" in liveness


def test_packed_aarch64_emit_uses_static_instruction_dispatch() -> None:
    source = (
        REPO / "pcc/backend/self_backend_aarch64_darwin.py"
    ).read_text(encoding="utf-8")
    emit_function_start = source.index("def _emit_function(")
    emit_function_end = source.index("\ndef ", emit_function_start + 1)
    emit_function = source[emit_function_start:emit_function_end]
    assert "_emit_dense_indexed_function_blocks(" in emit_function

    packed_start = source.index("def _emit_dense_indexed_function_blocks(")
    packed_end = source.find("\ndef ", packed_start + 1)
    packed = source[packed_start:] if packed_end < 0 else source[packed_start:packed_end]
    assert "_emit_dense_indexed_instruction_parts(" in packed
    assert "_emit_dense_indexed_terminator(" in packed
    assert "emit_indexed_function_blocks(" not in packed


def test_record_inventory_attributes_nested_containers_to_named_owner() -> None:
    tool = _load_tool()

    @dataclass
    class Root:
        rows: list[tuple[int, int]]
        lookup: dict[str, list[int]]

    payload = tool._graph_inventory(
        (Root(rows=[(1, 2), (3, 4)], lookup={"answer": [42]}),)
    )

    assert payload["containers"] == {"dict": 1, "list": 2, "tuple": 2}
    assert [
        (row["owner"], row["unique_objects"])
        for row in payload["container_primary_owners"]["dict"]
    ] == [("Root.lookup", 1)]
    list_owners = {
        row["owner"]: row["unique_objects"]
        for row in payload["container_primary_owners"]["list"]
    }
    assert list_owners == {"Root.lookup": 1, "Root.rows": 1}
    assert [
        (row["owner"], row["unique_objects"])
        for row in payload["container_primary_owners"]["tuple"]
    ] == [("Root.rows", 2)]
