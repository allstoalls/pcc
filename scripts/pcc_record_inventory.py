#!/usr/bin/env python3
"""Inventory compiler-internal record projections on one self-backend IR file."""

from __future__ import annotations

import argparse
import ast
from collections import Counter, deque
from dataclasses import fields, is_dataclass
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import run_pcc_compile_ab as compile_ab

from pcc.backend.self_backend_call_flags import CALL_FLAG_FRAME_PROTOCOL
from pcc.backend.self_backend_aarch64_darwin import (
    _emit_prepared_aarch64_darwin_module,
)
from pcc.backend.self_backend_aarch64_darwin_abi import (
    aggregate_returned_indirect,
    aggregate_returned_indirect_indexed,
)
from pcc.backend.self_backend_ir import (
    AllocaInfo,
    CompactParsedInstrArena,
    ParsedBlock,
    ParsedInstr,
    PhiIncoming,
    PhiInstr,
    SlotInfo,
    TypeDesc,
)
from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KINDS
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_prepare import (
    PreparedSelfBackendModule,
    prepare_parsed_functions,
)
from pcc.backend.self_backend_module_symbols import prepare_module_symbols
from pcc.backend.self_backend_precise_stackmaps import (
    PlannedManagedReload,
    PlannedRootLocation,
    PlannedSafepoint,
    _FRAME_PROTOCOL,
    _ManagedValueOrigin,
    _PointerOrigin,
    _RootGroup,
    _block_entry_states,
    _pointer_aliases,
    build_stack_map_plans,
)
from pcc.backend.self_backend_stackprep import assign_stack_slots
from pcc.backend.self_backend_verify import verify_parsed_module


SCHEMA = "pcc.compiler-record-inventory.v1"

# One fail-closed classification for every top-level class in the self-backend
# source family.  This is deliberately broader than the object families that
# happen to be reachable in today's inventory input: adding a new compiler
# record must first state whether it is native storage, a semantic/phase shell,
# a lazy diagnostic projection, or target/control machinery.  Otherwise the
# source-shape gate fails instead of silently reporting another false zero.
DATA_PLANE_CLASS_CONTRACT = {
    # Owned ELF linker source identities and validated layout metadata remain
    # resident; payloads belong to one explicitly bounded input-store cache.
    "owned_elf_inputs.py:_ClassificationProof": "semantic_record",
    "owned_elf_inputs.py:_InputSource": "semantic_record",
    "owned_elf_inputs.py:_InputSection": "phase_shell",
    "owned_elf_inputs.py:_InputObject": "phase_shell",
    "owned_elf_inputs.py:ElfInputStore": "phase_shell",
    "arm64_encode.py:EncodeError": "target_control",
    "arm64_encode.py:AssembledText": "phase_shell",
    # Packed instruction/relocation columns; names, inline data and the
    # explicitly counted text-oracle adapter remain visible side tables.
    "arm64_encode.py:PackedAArch64TextBuilder": "native_arena",
    "arm64_asm_driver.py:_SectionBuffer": "phase_shell",
    "arm64_asm_driver.py:StructuredAArch64Module": "phase_shell",
    # One append/finalize owner per module. It retains section/symbol state;
    # instruction storage belongs to its registered packed text builders.
    "arm64_asm_driver.py:AArch64ModuleBuilder": "phase_shell",
    # One native module emission scope. Borrowed capture/scratch arenas are
    # packed; only explicitly counted residual encoder lines may be retained.
    "self_backend_aarch64_darwin.py:_NativeAArch64Emission": "phase_shell",
    # One scope owner for raw records, span roots/cursor and traced symbols.
    "self_backend_aarch64_fragments.py:AArch64EmissionFragments": "native_arena",
    "self_backend_module_symbols.py:PreparedModuleSymbols": "phase_shell",
    "self_backend_prepare.py:PreparedSelfBackendModule": "phase_shell",
    # Text legalization owners: one module scope for lines/type/helper tables
    # and one function scope for temporary names and its rewrite buffer.
    "self_backend_half.py:_HalfModule": "phase_shell",
    "self_backend_half.py:_HalfFunction": "phase_shell",
    "self_backend_wide_int.py:_WideModule": "phase_shell",
    "self_backend_wide_int.py:_WideFunction": "phase_shell",
    "self_backend_parse.py:_FunctionBlockPlane": "native_arena",
    # Original source metadata is a retained semantic sidecar, not a lazy
    # instruction projection or packed native storage. Module/function/block
    # owners retain its ordinary Python records and coordinate collections.
    "self_backend_debug.py:DebugNode": "semantic_record",
    "self_backend_debug.py:DebugSource": "semantic_record",
    "self_backend_debug.py:DebugLocation": "semantic_record",
    "self_backend_debug.py:DebugBlock": "phase_shell",
    "self_backend_debug.py:FunctionDebugInfo": "phase_shell",
    "self_backend_debug.py:DebugMetadata": "phase_shell",
    # One emission plan per function owns the actual machine-label/source
    # rows, borrowing the semantic sidecar until DWARF sections are written.
    "self_backend_dwarf.py:DebugFunctionPlan": "phase_shell",
    "self_backend_targets.py:SelfBackendPlatformVerdict": "target_control",
    "self_backend_targets.py:SelfBackendTargetSpec": "target_control",
    "self_backend_ir.py:TypeDesc": "semantic_record",
    # Per-module lazy declarations and canonicalization ownership.
    "self_backend_ir.py:TypeParseContext": "phase_shell",
    "self_backend_ir.py:ArgInfo": "semantic_record",
    "self_backend_ir.py:PhiIncoming": "diagnostic_projection",
    "self_backend_ir.py:PhiInstr": "diagnostic_projection",
    "self_backend_ir.py:ParsedInstr": "diagnostic_projection",
    "self_backend_ir.py:CompactParsedInstrView": "diagnostic_projection",
    "self_backend_ir.py:IndexedCallPlane": "native_arena",
    "self_backend_ir.py:CompactParsedInstrArena": "native_arena",
    "self_backend_ir.py:ParsedBlock": "diagnostic_projection",
    "self_backend_ir.py:SlotInfo": "diagnostic_projection",
    "self_backend_ir.py:AllocaInfo": "diagnostic_projection",
    "self_backend_ir.py:GlobalDef": "semantic_record",
    "self_backend_ir.py:ParsedModule": "phase_shell",
    "self_backend_ir.py:ParsedFunction": "phase_shell",
    "self_backend_target_passes.py:AArch64MaddFusion": "target_control",
    "self_backend_target_passes.py:SelfTargetPassContext": "target_control",
    "self_backend_target_passes.py:SelfTargetPass": "target_control",
    "self_backend_target_passes.py:SelfTargetMemoryPass": "target_control",
    "self_backend_target_passes.py:StripTrailingWhitespacePass": "target_control",
    "self_backend_target_passes.py:FoldFrameAddressPass": "target_control",
    "self_backend_target_passes.py:FoldImmediatePass": "target_control",
    "self_backend_target_passes.py:VerifyPreparedModulePass": "target_control",
    "self_backend_kernel.py:IndexedFunctionSeed": "native_arena",
    "self_backend_kernel.py:IndexedFunctionKernel": "native_arena",
    "self_backend_value_arena.py:CompilerInt2": "native_value_record",
    "self_backend_value_arena.py:CompilerInt3": "native_value_record",
    "self_backend_value_arena.py:CompilerInt4": "native_value_record",
    "self_backend_value_arena.py:CompilerIntArena": "native_arena",
    # Scope-relative CompilerInt2 keys; mutable roots and immutable concat
    # nodes live in scalar arenas, including the non-recursive traversal stack.
    "self_backend_value_arena.py:CompilerRecordSpanArena": "native_arena",
    "self_backend_precise_stackmaps.py:PlannedRootLocation": (
        "diagnostic_projection"
    ),
    "self_backend_precise_stackmaps.py:PlannedManagedReload": (
        "diagnostic_projection"
    ),
    "self_backend_precise_stackmaps.py:PlannedSafepoint": (
        "diagnostic_projection"
    ),
    "self_backend_precise_stackmaps.py:PackedPlannedSafepoints": "native_arena",
    "self_backend_precise_stackmaps.py:PackedManagedLiveness": "native_arena",
    "self_backend_precise_stackmaps.py:PackedPointerAliases": "native_arena",
    "self_backend_precise_stackmaps.py:PackedRootStatePlane": "native_arena",
    "self_backend_precise_stackmaps.py:PackedManagedOrigins": "native_arena",
    "self_backend_precise_stackmaps.py:PackedReloadScratch": "native_arena",
    "self_backend_precise_stackmaps.py:FunctionStackMapPlan": "phase_shell",
    "self_backend_precise_stackmaps.py:_PointerOrigin": "diagnostic_projection",
    "self_backend_precise_stackmaps.py:_RootGroup": "diagnostic_projection",
    "self_backend_precise_stackmaps.py:_ManagedValueOrigin": (
        "diagnostic_projection"
    ),
}
DIAGNOSTIC_PROJECTION_SITE_CONTRACT: dict[str, tuple[int, str]] = {
    "self_backend_ir.py:CompactParsedInstrArena.__getitem__:CompactParsedInstrView": (
        2,
        "diagnostic_adapter",
    ),
    "self_backend_ir.py:CompactParsedInstrArena.__iter__:CompactParsedInstrView": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_ir.py:CompactParsedInstrView.materialize:ParsedInstr": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_ir.py:parsed_function_alloca_slot:AllocaInfo": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_ir.py:parsed_function_value_slot:SlotInfo": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_kernel.py:IndexedFunctionKernel.diagnostic_block:ParsedBlock": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_kernel.py:IndexedFunctionKernel.diagnostic_instruction:ParsedInstr": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_kernel.py:IndexedFunctionKernel.diagnostic_phi:PhiIncoming": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_kernel.py:IndexedFunctionKernel.diagnostic_phi:PhiInstr": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_kernel.py:IndexedFunctionKernel.diagnostic_terminator:ParsedInstr": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_parse.py:_call_instr_from_parts:ParsedInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_filter_reachable_blocks:PhiInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_filter_reachable_blocks_linear:PhiInstr": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_parse.py:_parse_binop_instruction:ParsedInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_block_structure:PhiIncoming": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_block_structure:PhiInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_extractvalue_instruction:ParsedInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_fcmp_instruction:ParsedInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_icmp_instruction:ParsedInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_insertvalue_instruction:ParsedInstr": (
        1,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_instruction:ParsedInstr": (
        21,
        "parse_construction",
    ),
    "self_backend_parse.py:_parse_terminator:ParsedInstr": (
        8,
        "parse_construction",
    ),
    "self_backend_precise_stackmaps.py:PackedPlannedSafepoints.materialize:PlannedSafepoint": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_precise_stackmaps.py:PackedPlannedSafepoints.record_locations:PlannedRootLocation": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_precise_stackmaps.py:PackedPlannedSafepoints.record_reloads:PlannedManagedReload": (
        1,
        "diagnostic_adapter",
    ),
    "self_backend_precise_stackmaps.py:_managed_value_origins.transferred:_ManagedValueOrigin": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:_managed_value_origins:_ManagedValueOrigin": (
        2,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:_planned_managed_reloads:PlannedManagedReload": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:_pointer_aliases:_PointerOrigin": (
        2,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:_resolve_pointer:_PointerOrigin": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:_root_group:PlannedRootLocation": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:_root_group:_RootGroup": (
        3,
        "legacy_or_unsupported",
    ),
    # The legacy x86_64 planner tracks local registry-frame lifetimes here;
    # the normal AArch64 path uses the packed native root-state plane.
    "self_backend_precise_stackmaps.py:_frame_protocol_enter_group:_RootGroup": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_precise_stackmaps.py:build_function_stack_map_plan.add_record:PlannedSafepoint": (
        1,
        "host_oracle",
    ),
    "self_backend_stackprep.py:assign_stack_slots.legacy_slot_info:SlotInfo": (
        1,
        "legacy_or_unsupported",
    ),
    "self_backend_stackprep.py:assign_stack_slots.publish_alloca:AllocaInfo": (
        1,
        "legacy_or_unsupported",
    ),
}
_DATA_PLANE_CLASSIFICATIONS = frozenset(
    (
        "native_value_record",
        "native_arena",
        "semantic_record",
        "phase_shell",
        "diagnostic_projection",
        "target_control",
    )
)
_DIAGNOSTIC_PROJECTION_SITE_POLICIES = frozenset(
    (
        "parse_construction",
        "diagnostic_adapter",
        "legacy_or_unsupported",
        "host_oracle",
    )
)


def _discover_self_backend_classes(backend_dir: Path | None = None) -> set[str]:
    if backend_dir is None:
        backend_dir = _SCRIPT_DIR.parent / "pcc" / "backend"
    discovered: set[str] = set()
    for path in sorted(set(backend_dir.glob("self_backend*.py")) | set(backend_dir.glob("arm64_*.py")) | set(backend_dir.glob("owned_elf_inputs.py"))):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                discovered.add(f"{path.name}:{node.name}")
    return discovered


def data_plane_class_contract_report(backend_dir: Path | None = None) -> dict:
    """Return the fail-closed source classification for self-backend classes."""

    discovered = _discover_self_backend_classes(backend_dir)
    classified = set(DATA_PLANE_CLASS_CONTRACT)
    invalid = sorted(
        key
        for key, classification in DATA_PLANE_CLASS_CONTRACT.items()
        if classification not in _DATA_PLANE_CLASSIFICATIONS
    )
    by_classification = Counter(DATA_PLANE_CLASS_CONTRACT.values())
    return {
        "discovered_class_count": len(discovered),
        "classified_class_count": len(classified),
        "unclassified": sorted(discovered - classified),
        "stale_classifications": sorted(classified - discovered),
        "invalid_classifications": invalid,
        "by_classification": dict(sorted(by_classification.items())),
    }


class _DiagnosticProjectionSiteVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, diagnostic_names: set[str]) -> None:
        self.filename = filename
        self.diagnostic_names = diagnostic_names
        self.scope: list[str] = []
        self.sites: Counter = Counter()

    def _visit_scope(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        target_name = ""
        if isinstance(target, ast.Name):
            target_name = target.id
        elif isinstance(target, ast.Attribute):
            target_name = target.attr
        if target_name in self.diagnostic_names:
            owner = ".".join(self.scope) if self.scope else "<module>"
            self.sites[f"{self.filename}:{owner}:{target_name}"] += 1
        self.generic_visit(node)


def _discover_diagnostic_projection_sites(
    backend_dir: Path | None = None,
) -> Counter:
    if backend_dir is None:
        backend_dir = _SCRIPT_DIR.parent / "pcc" / "backend"
    diagnostic_names = {
        key.split(":", 1)[1]
        for key, classification in DATA_PLANE_CLASS_CONTRACT.items()
        if classification == "diagnostic_projection"
    }
    sites: Counter = Counter()
    for path in sorted(set(backend_dir.glob("self_backend*.py")) | set(backend_dir.glob("arm64_*.py")) | set(backend_dir.glob("owned_elf_inputs.py"))):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _DiagnosticProjectionSiteVisitor(path.name, diagnostic_names)
        visitor.visit(tree)
        sites.update(visitor.sites)
    return sites


def diagnostic_projection_site_contract_report(
    backend_dir: Path | None = None,
) -> dict:
    """Find direct diagnostic-record construction outside declared seams."""

    discovered = _discover_diagnostic_projection_sites(backend_dir)
    classified = DIAGNOSTIC_PROJECTION_SITE_CONTRACT
    mismatches = []
    for key in sorted(set(discovered) & set(classified)):
        expected_count, _policy = classified[key]
        if discovered[key] != expected_count:
            mismatches.append(
                {
                    "site": key,
                    "expected": expected_count,
                    "actual": discovered[key],
                }
            )
    invalid_policies = sorted(
        key
        for key, (_count, policy) in classified.items()
        if policy not in _DIAGNOSTIC_PROJECTION_SITE_POLICIES
    )
    return {
        "discovered_site_count": len(discovered),
        "classified_site_count": len(classified),
        "unclassified_sites": sorted(set(discovered) - set(classified)),
        "stale_sites": sorted(set(classified) - set(discovered)),
        "count_mismatches": mismatches,
        "invalid_policies": invalid_policies,
        "sites": {
            key: {
                "count": discovered[key],
                "policy": classified[key][1] if key in classified else "",
            }
            for key in sorted(discovered)
        },
    }


def _contract_runtime_record_families() -> tuple[type, ...]:
    families: list[type] = []
    for key in DATA_PLANE_CLASS_CONTRACT:
        filename, class_name = key.split(":", 1)
        module_name = f"pcc.backend.{filename[:-3]}"
        module = importlib.import_module(module_name)
        family = getattr(module, class_name)
        # Non-runtime-checkable Protocol classes raise from isinstance().
        # Every concrete class, including phase/control shells, remains visible
        # so a misleading classification cannot hide a reachable object.
        if not getattr(family, "_is_protocol", False):
            families.append(family)
    return tuple(families)


_LEAF_TYPES = (str, bytes, bytearray, int, float, bool, type(None))
_STOP_TYPES = (
    type,
    types.BuiltinFunctionType,
    types.FunctionType,
    types.MethodType,
    types.ModuleType,
)
_RECORD_FAMILIES = _contract_runtime_record_families()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _slot_names(value) -> tuple[str, ...]:
    names: list[str] = []
    for cls in type(value).__mro__:
        slots = getattr(cls, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name not in ("__dict__", "__weakref__") and name not in names:
                names.append(name)
    return tuple(names)


def _child_edges(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield "dict.key", key
            yield "dict.value", item
        return
    if isinstance(value, list):
        for item in value:
            yield "list.item", item
        return
    if isinstance(value, tuple):
        for item in value:
            yield "tuple.item", item
        return
    if isinstance(value, set):
        for item in value:
            yield "set.item", item
        return
    if isinstance(value, frozenset):
        for item in value:
            yield "frozenset.item", item
        return
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield f"{type(value).__name__}.{field.name}", getattr(value, field.name)
        return
    mapping = getattr(value, "__dict__", None)
    if isinstance(mapping, dict):
        for name, item in mapping.items():
            yield f"{type(value).__name__}.{name}", item
    for name in _slot_names(value):
        try:
            yield f"{type(value).__name__}.{name}", getattr(value, name)
        except AttributeError:
            continue


def _container_kind(value) -> str | None:
    if isinstance(value, list):
        return "list"
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, set):
        return "set"
    if isinstance(value, frozenset):
        return "frozenset"
    return None


def _family_payload(objects, references: Counter, cls) -> dict:
    values = objects.get(cls, [])
    payload = {
        "unique_objects": len(values),
        "references": int(references.get(cls, 0)),
        "shallow_bytes": sum(sys.getsizeof(value) for value in values),
    }
    try:
        payload["structural_values"] = len(set(values))
    except TypeError:
        pass
    return payload


def _graph_inventory(roots) -> dict:
    seen: set[int] = set()
    objects: dict[type, list] = {}
    references: Counter = Counter()
    containers: Counter = Counter()
    container_owner_counts: Counter = Counter()
    container_owner_bytes: Counter = Counter()
    pending = deque(
        (root, f"<root:{type(root).__name__}>") for root in roots
    )
    while pending:
        value, primary_owner = pending.popleft()
        cls = type(value)
        if isinstance(value, _RECORD_FAMILIES):
            references[cls] += 1
        if isinstance(value, _LEAF_TYPES) or isinstance(value, _STOP_TYPES):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, _RECORD_FAMILIES):
            objects.setdefault(cls, []).append(value)
        container_kind = _container_kind(value)
        if container_kind is not None:
            containers[container_kind] += 1
            owner_key = (container_kind, primary_owner)
            container_owner_counts[owner_key] += 1
            container_owner_bytes[owner_key] += sys.getsizeof(value)
        for edge_name, child in _child_edges(value):
            child_owner = primary_owner if container_kind is not None else edge_name
            pending.append((child, child_owner))

    type_values = objects.get(TypeDesc, [])
    type_kinds_unique = Counter(value.kind for value in type_values)
    type_payload = _family_payload(objects, references, TypeDesc)
    type_payload["structural_values"] = len(set(type_values))
    type_payload["kind_unique_objects"] = dict(sorted(type_kinds_unique.items()))

    container_primary_owners: dict[str, list[dict[str, int | str]]] = {}
    for kind in sorted(containers):
        rows = []
        for (owner_kind, owner), count in container_owner_counts.items():
            if owner_kind != kind:
                continue
            rows.append(
                {
                    "owner": owner,
                    "unique_objects": int(count),
                    "shallow_bytes": int(container_owner_bytes[(owner_kind, owner)]),
                }
            )
        rows.sort(
            key=lambda row: (
                -int(row["unique_objects"]),
                -int(row["shallow_bytes"]),
                str(row["owner"]),
            )
        )
        container_primary_owners[kind] = rows

    return {
        "reachable_unique_objects": len(seen),
        "containers": dict(sorted(containers.items())),
        "container_primary_owner_rule": (
            "first breadth-first nearest non-container field; rows partition "
            "the unique container count"
        ),
        "container_primary_owners": container_primary_owners,
        "families": {
            family.__name__: (
                type_payload
                if family is TypeDesc
                else _family_payload(objects, references, family)
            )
            for family in _RECORD_FAMILIES
        },
    }


def _instruction_payload_inventory(functions) -> dict:
    by_kind: dict[str, dict] = {}
    total_instructions = 0
    total_tuple_references = 0
    total_list_references = 0
    nonempty_arithmetic_flags = 0
    arithmetic_flag_uses: Counter = Counter()
    packed_call_records = 0
    packed_alloca_records = 0
    packed_fixed_records = 0
    for func in functions:
        kernel = get_indexed_function_kernel(func)
        for block_id in range(len(kernel.block_names)):
            instruction_index = 0
            while instruction_index < kernel.instruction_count(block_id):
                kind_id = kernel.instruction_kind_id(
                    block_id,
                    instruction_index,
                )
                kind = PARSED_INSTRUCTION_KINDS[kind_id]
                data = kernel.instruction_record_id(
                    block_id,
                    instruction_index,
                )
                if data < 0:
                    data = kernel.instruction_data(block_id, instruction_index)
                row = by_kind.setdefault(
                    kind,
                    {
                        "instructions": 0,
                        "top_level_fields": 0,
                        "tuple_references": 0,
                        "list_references": 0,
                        "tuple_lengths": Counter(),
                    },
                )
                row["instructions"] += 1
                if (
                    kind in (
                        "call",
                        "alloca",
                        "load",
                        "store",
                        "cast",
                        "icmp",
                        "binop",
                        "select",
                        "gep",
                    )
                    and isinstance(data, int)
                ):
                    row["packed_records"] = row.get("packed_records", 0) + 1
                    if kind == "call":
                        packed_call_records += 1
                    elif kind == "alloca":
                        packed_alloca_records += 1
                    else:
                        packed_fixed_records += 1
                    pending = []
                else:
                    row["top_level_fields"] += len(data)
                    pending = [data]
                while pending:
                    value = pending.pop()
                    if isinstance(value, tuple):
                        row["tuple_references"] += 1
                        row["tuple_lengths"][len(value)] += 1
                        total_tuple_references += 1
                        pending.extend(value)
                    elif isinstance(value, list):
                        row["list_references"] += 1
                        total_list_references += 1
                        pending.extend(value)
                if kernel.instruction_has_arithmetic_flags(
                    block_id,
                    instruction_index,
                ):
                    nonempty_arithmetic_flags += 1
                    for flag in kernel.instruction_arithmetic_flags(
                        block_id,
                        instruction_index,
                    ):
                        arithmetic_flag_uses[flag] += 1
                total_instructions += 1
                instruction_index += 1

    rendered_by_kind = {}
    for kind, row in sorted(
        by_kind.items(),
        key=lambda item: (-item[1]["tuple_references"], item[0]),
    ):
        rendered = dict(row)
        rendered["tuple_lengths"] = {
            str(length): count
            for length, count in sorted(row["tuple_lengths"].items())
        }
        rendered_by_kind[kind] = rendered
    return {
        "instructions": total_instructions,
        "tuple_references": total_tuple_references,
        "list_references": total_list_references,
        "nonempty_arithmetic_flag_records": nonempty_arithmetic_flags,
        "packed_call_records": packed_call_records,
        "packed_alloca_records": packed_alloca_records,
        "packed_fixed_records": packed_fixed_records,
        "arithmetic_flag_uses": dict(sorted(arithmetic_flag_uses.items())),
        "by_kind": rendered_by_kind,
    }


def _parsed_instruction_payload_inventory(functions) -> dict:
    packed = _instruction_payload_inventory(functions)
    rendered_by_kind = {}
    total_packed_payloads = 0
    for kind, source in packed["by_kind"].items():
        row = dict(source)
        row["packed_payloads"] = row.pop("packed_records", 0)
        total_packed_payloads += row["packed_payloads"]
        rendered_by_kind[kind] = row
    return {
        "instructions": packed["instructions"],
        "tuple_references": packed["tuple_references"],
        "list_references": packed["list_references"],
        "packed_payloads": total_packed_payloads,
        "by_kind": rendered_by_kind,
    }


def _legacy_stackmap_construction_sizing(functions, globals_) -> dict:
    """Size the transient root-state plane before it is packed and released."""

    globals_by_name = {global_.name: global_ for global_ in globals_}
    blocks = 0
    reachable_entry_blocks = 0
    empty_entry_blocks = 0
    nonempty_entry_blocks = 0
    state_group_references = 0
    max_state_groups = 0
    max_state_locations = 0
    protocol_calls = 0
    protocol_entry_group_references = 0
    protocol_block_keys: set[tuple[int, int]] = set()
    empty_protocol_block_keys: set[tuple[int, int]] = set()
    state_identity_objects: dict[int, tuple] = {}
    state_contents: set[tuple[int, ...]] = set()
    group_identities: set[int] = set()
    location_identities: set[int] = set()
    entry_identity_cache_hits = 0
    entry_identity_cache_misses = 0
    entry_identity_cache_scanned_group_references = 0
    entry_identity_cache_avoided_group_scans = 0

    for function_index, func in enumerate(functions):
        kernel = get_indexed_function_kernel(func)
        aliases = _pointer_aliases(func)
        entries = _block_entry_states(func, globals_by_name, aliases)
        blocks += len(entries)
        for block_index, entry in enumerate(entries):
            if entry is None:
                continue
            reachable_entry_blocks += 1
            entry_identity = id(entry)
            identity_entry = state_identity_objects.get(entry_identity)
            if identity_entry is entry:
                entry_identity_cache_hits += 1
                entry_identity_cache_avoided_group_scans += len(entry)
            else:
                entry_identity_cache_misses += 1
                entry_identity_cache_scanned_group_references += len(entry)
                # Keep the exact key alive so id() reuse cannot false-hit.
                state_identity_objects[entry_identity] = entry
            state_contents.add(tuple(id(group) for group in entry))
            state_group_references += len(entry)
            max_state_groups = max(max_state_groups, len(entry))
            location_count = 0
            for group in entry:
                group_identities.add(id(group))
                location_count += len(group.locations)
                for location in group.locations:
                    location_identities.add(id(location))
            max_state_locations = max(max_state_locations, location_count)
            if entry:
                nonempty_entry_blocks += 1
            else:
                empty_entry_blocks += 1

            block_fact = kernel.block_fact(block_index)
            instruction_index = 0
            while instruction_index < block_fact.second:
                instruction_id = block_fact.first + instruction_index
                metadata = kernel.instruction_metadata_by_id(instruction_id)
                if PARSED_INSTRUCTION_KINDS[metadata.first] != "call":
                    instruction_index += 1
                    continue
                header = kernel.call_header(metadata.second)
                if not header.third & 1:
                    callee = kernel.call_texts[header.second]
                    if callee in _FRAME_PROTOCOL:
                        block_key = (function_index, block_index)
                        protocol_calls += 1
                        if block_key not in protocol_block_keys:
                            protocol_entry_group_references += len(entry)
                        protocol_block_keys.add(block_key)
                        if not entry:
                            empty_protocol_block_keys.add(block_key)
                instruction_index += 1

    protocol_blocks = len(protocol_block_keys)
    required_active_dict_blocks = protocol_blocks
    return {
        "blocks": blocks,
        "reachable_entry_blocks": reachable_entry_blocks,
        "empty_entry_blocks": empty_entry_blocks,
        "nonempty_entry_blocks": nonempty_entry_blocks,
        "protocol_calls": protocol_calls,
        "protocol_blocks": protocol_blocks,
        "empty_entry_protocol_blocks": len(empty_protocol_block_keys),
        "eager_baseline_main_active_dict_constructions": reachable_entry_blocks,
        "current_main_active_dict_constructions": required_active_dict_blocks,
        "lazy_main_active_dict_constructions": required_active_dict_blocks,
        "avoidable_main_active_dict_constructions": 0,
        "eager_baseline_main_active_dict_group_insertions": (
            state_group_references
        ),
        "current_main_active_dict_group_insertions": (
            protocol_entry_group_references
        ),
        "lazy_main_active_dict_group_insertions": (
            protocol_entry_group_references
        ),
        "avoidable_main_active_dict_group_insertions": 0,
        "entry_state_identities": len(state_identity_objects),
        "entry_state_contents": len(state_contents),
        "entry_identity_cache_hits": entry_identity_cache_hits,
        "entry_identity_cache_misses": entry_identity_cache_misses,
        "entry_identity_cache_scanned_group_references": (
            entry_identity_cache_scanned_group_references
        ),
        "entry_identity_cache_avoided_group_scans": (
            entry_identity_cache_avoided_group_scans
        ),
        "root_group_identities": len(group_identities),
        "root_location_identities": len(location_identities),
        "state_group_references": state_group_references,
        "max_state_groups": max_state_groups,
        "max_state_locations": max_state_locations,
    }


def _stackmap_construction_sizing(functions, plans) -> dict:
    blocks = 0
    protocol_calls = 0
    protocol_blocks = 0
    state_ids = 0
    location_records = 0
    max_state_locations = 0
    for func, plan in zip(functions, plans):
        kernel = get_indexed_function_kernel(func)
        blocks += len(kernel.block_names)
        seen_protocol_blocks: set[int] = set()
        block_id = 0
        while block_id < len(kernel.block_names):
            block = kernel.block_fact(block_id)
            instruction_index = 0
            while instruction_index < block.second:
                metadata = kernel.instruction_metadata_by_id(
                    block.first + instruction_index
                )
                if PARSED_INSTRUCTION_KINDS[metadata.first] == "call":
                    header = kernel.call_header(metadata.second)
                    if header.third & CALL_FLAG_FRAME_PROTOCOL:
                        protocol_calls += 1
                        seen_protocol_blocks.add(block_id)
                instruction_index += 1
            block_id += 1
        protocol_blocks += len(seen_protocol_blocks)
        packed = plan.packed_records
        if packed is not None:
            state_ids += len(packed.location_group_spans) // 2
            location_records += len(packed.location_scalars) // 2
            group_id = 0
            while group_id < len(packed.location_group_spans) // 2:
                group = packed.location_group_spans.get2_unchecked(group_id)
                max_state_locations = max(max_state_locations, group.second)
                group_id += 1
    return {
        "blocks": blocks,
        "reachable_entry_blocks": blocks,
        "empty_entry_blocks": 0,
        "nonempty_entry_blocks": blocks,
        "protocol_calls": protocol_calls,
        "protocol_blocks": protocol_blocks,
        "empty_entry_protocol_blocks": 0,
        "eager_baseline_main_active_dict_constructions": blocks,
        "current_main_active_dict_constructions": 0,
        "lazy_main_active_dict_constructions": 0,
        "avoidable_main_active_dict_constructions": 0,
        "eager_baseline_main_active_dict_group_insertions": 0,
        "current_main_active_dict_group_insertions": 0,
        "lazy_main_active_dict_group_insertions": 0,
        "avoidable_main_active_dict_group_insertions": 0,
        "entry_state_identities": 0,
        "entry_state_contents": state_ids,
        "entry_identity_cache_hits": 0,
        "entry_identity_cache_misses": 0,
        "entry_identity_cache_scanned_group_references": 0,
        "entry_identity_cache_avoided_group_scans": 0,
        "root_group_identities": 0,
        "root_location_identities": 0,
        "state_group_references": 0,
        "max_state_groups": 0,
        "max_state_locations": max_state_locations,
        "native_state_ids": state_ids,
        "native_location_records": location_records,
    }


def inventory_ir_text(ir_text: str) -> dict:
    module = parse_self_backend_module(ir_text)
    parsed_instruction_payloads = _parsed_instruction_payload_inventory(
        module.functions
    )
    graph_by_stage = {"parsed": _graph_inventory((module,))}
    parsed_call_projections = sum(
        func.indexed_kernel.call_diagnostic_projections
        for func in module.functions
    )
    parsed_instruction_projections = sum(
        func.indexed_kernel.diagnostic_projections
        for func in module.functions
    )
    parsed_type_projections = sum(
        func.indexed_kernel.type_object_projections
        for func in module.functions
    )
    parsed_record_projections = {
        "block": sum(
            func.indexed_kernel.block_diagnostic_projections
            for func in module.functions
        ),
        "instruction_arena": sum(
            func.indexed_kernel.instruction_arena_diagnostic_projections
            for func in module.functions
        ),
        "phi": sum(
            func.indexed_kernel.phi_diagnostic_projections
            for func in module.functions
        ),
        "terminator": sum(
            func.indexed_kernel.terminator_diagnostic_projections
            for func in module.functions
        ),
    }
    verify_parsed_module(module)
    call_projection_by_stage = {
        "parsed": parsed_call_projections,
        "verified": sum(
            func.indexed_kernel.call_diagnostic_projections
            for func in module.functions
        )
    }
    instruction_projection_by_stage = {
        "parsed": parsed_instruction_projections,
        "verified": sum(
            func.indexed_kernel.diagnostic_projections
            for func in module.functions
        )
    }
    type_projection_by_stage = {
        "parsed": parsed_type_projections,
        "verified": sum(
            func.indexed_kernel.type_object_projections
            for func in module.functions
        )
    }
    record_projection_by_stage = {
        "parsed": parsed_record_projections,
        "verified": {
            "block": sum(
                func.indexed_kernel.block_diagnostic_projections
                for func in module.functions
            ),
            "instruction_arena": sum(
                func.indexed_kernel.instruction_arena_diagnostic_projections
                for func in module.functions
            ),
            "phi": sum(
                func.indexed_kernel.phi_diagnostic_projections
                for func in module.functions
            ),
            "terminator": sum(
                func.indexed_kernel.terminator_diagnostic_projections
                for func in module.functions
            ),
        }
    }
    graph_by_stage["verified"] = _graph_inventory((module,))
    functions = list(module.functions)
    globals_ = list(module.globals_)
    prepare_parsed_functions(functions)
    for func in functions:
        assign_stack_slots(
            func,
            aggregate_returned_indirect=aggregate_returned_indirect,
            aggregate_returned_indirect_indexed=(
                aggregate_returned_indirect_indexed
            ),
            materialize_legacy_slots=False,
        )
    graph_by_stage["stack_prepared"] = _graph_inventory((globals_, functions))
    call_projection_by_stage["stack_prepared"] = sum(
        func.indexed_kernel.call_diagnostic_projections for func in functions
    )
    instruction_projection_by_stage["stack_prepared"] = sum(
        func.indexed_kernel.diagnostic_projections for func in functions
    )
    type_projection_by_stage["stack_prepared"] = sum(
        func.indexed_kernel.type_object_projections for func in functions
    )
    record_projection_by_stage["stack_prepared"] = {
        "block": sum(kernel.block_diagnostic_projections for kernel in (
            func.indexed_kernel for func in functions
        )),
        "instruction_arena": sum(
            func.indexed_kernel.instruction_arena_diagnostic_projections
            for func in functions
        ),
        "phi": sum(
            func.indexed_kernel.phi_diagnostic_projections for func in functions
        ),
        "terminator": sum(
            func.indexed_kernel.terminator_diagnostic_projections
            for func in functions
        ),
    }
    plans = build_stack_map_plans(
        functions,
        globals_,
        target="aarch64-darwin",
    )
    stackmap_construction = _stackmap_construction_sizing(functions, plans)
    kernels = [get_indexed_function_kernel(func) for func in functions]
    graph = _graph_inventory((globals_, functions, plans))
    graph_by_stage["stackmap_planned"] = graph
    call_projection_by_stage["stackmap_planned"] = sum(
        kernel.call_diagnostic_projections for kernel in kernels
    )
    instruction_projection_by_stage["stackmap_planned"] = sum(
        kernel.diagnostic_projections for kernel in kernels
    )
    type_projection_by_stage["stackmap_planned"] = sum(
        kernel.type_object_projections for kernel in kernels
    )
    record_projection_by_stage["stackmap_planned"] = {
        "block": sum(kernel.block_diagnostic_projections for kernel in kernels),
        "instruction_arena": sum(
            kernel.instruction_arena_diagnostic_projections for kernel in kernels
        ),
        "phi": sum(kernel.phi_diagnostic_projections for kernel in kernels),
        "terminator": sum(
            kernel.terminator_diagnostic_projections for kernel in kernels
        ),
    }
    module_symbols = prepare_module_symbols(ir_text, globals_, functions)
    assembly = _emit_prepared_aarch64_darwin_module(
        PreparedSelfBackendModule(
            triple=module.triple,
            globals_=globals_,
            functions=functions,
            module_symbols=module_symbols,
        ),
        profile_ir_text=ir_text,
        close_native_tables=False,
    )
    graph_by_stage["emitted"] = _graph_inventory((globals_, functions, plans))
    call_projection_by_stage["emitted"] = sum(
        kernel.call_diagnostic_projections for kernel in kernels
    )
    instruction_projection_by_stage["emitted"] = sum(
        kernel.diagnostic_projections for kernel in kernels
    )
    type_projection_by_stage["emitted"] = sum(
        kernel.type_object_projections for kernel in kernels
    )
    record_projection_by_stage["emitted"] = {
        "block": sum(kernel.block_diagnostic_projections for kernel in kernels),
        "instruction_arena": sum(
            kernel.instruction_arena_diagnostic_projections for kernel in kernels
        ),
        "phi": sum(kernel.phi_diagnostic_projections for kernel in kernels),
        "terminator": sum(
            kernel.terminator_diagnostic_projections for kernel in kernels
        ),
    }
    return {
        "schema": SCHEMA,
        "data_plane_class_contract": data_plane_class_contract_report(),
        "diagnostic_projection_site_contract": (
            diagnostic_projection_site_contract_report()
        ),
        "shape": {
            "functions": len(functions),
            "blocks": sum(len(kernel.block_names) for kernel in kernels),
            "instructions": sum(
                len(kernel.instruction_metadata) // 4 for kernel in kernels
            ),
            "value_slots": sum(
                sum(
                    1
                    for value_id in range(len(kernel.value_names))
                    if kernel.value_slot_id(value_id) >= 0
                )
                for kernel in kernels
            ),
            "alloca_slots": sum(
                sum(
                    1
                    for value_id in range(len(kernel.value_names))
                    if kernel.alloca_offset(value_id) >= 0
                )
                for kernel in kernels
            ),
            "legacy_value_slot_map_entries": sum(
                len(func.value_slots) for func in functions
            ),
            "legacy_alloca_slot_map_entries": sum(
                len(func.alloca_slots) for func in functions
            ),
            "kernel_values": sum(len(kernel.value_names) for kernel in kernels),
            "kernel_types": sum(len(kernel.types) for kernel in kernels),
            "block_name_index_capacity": sum(
                kernel.block_name_index_capacity for kernel in kernels
            ),
            "value_name_index_capacity": sum(
                kernel.value_name_index_capacity for kernel in kernels
            ),
            "legacy_slot_projections": sum(
                kernel.legacy_slot_projections for kernel in kernels
            ),
            "call_records": sum(
                len(kernel.call_scalars) // 8 for kernel in kernels
            ),
            "call_arg_records": sum(
                len(kernel.call_arg_scalars) // 4 for kernel in kernels
            ),
            "call_diagnostic_projections": sum(
                kernel.call_diagnostic_projections for kernel in kernels
            ),
            "instruction_diagnostic_projections": sum(
                kernel.diagnostic_projections for kernel in kernels
            ),
            "type_object_projections": sum(
                kernel.type_object_projections for kernel in kernels
            ),
            "block_diagnostic_projections": sum(
                kernel.block_diagnostic_projections for kernel in kernels
            ),
            "instruction_arena_diagnostic_projections": sum(
                kernel.instruction_arena_diagnostic_projections
                for kernel in kernels
            ),
            "phi_diagnostic_projections": sum(
                kernel.phi_diagnostic_projections for kernel in kernels
            ),
            "terminator_diagnostic_projections": sum(
                kernel.terminator_diagnostic_projections for kernel in kernels
            ),
            "stackmap_records": sum(
                len(plan.packed_records)
                if plan.packed_records is not None
                else len(plan.records)
                for plan in plans
            ),
        },
        "instruction_payloads": _instruction_payload_inventory(functions),
        "parsed_instruction_payloads": parsed_instruction_payloads,
        "stackmap_construction": stackmap_construction,
        "call_projection_by_stage": call_projection_by_stage,
        "instruction_projection_by_stage": instruction_projection_by_stage,
        "type_projection_by_stage": type_projection_by_stage,
        "record_projection_by_stage": record_projection_by_stage,
        "assembly_sha256": hashlib.sha256(assembly.encode()).hexdigest(),
        "graph": graph,
        "graph_by_stage": graph_by_stage,
    }


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ir", type=Path, help="canonical LLVM IR input")
    parser.add_argument("--output", type=Path, help="optional JSON artifact")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    ir_path = args.ir.expanduser().resolve()
    if not ir_path.is_file():
        raise SystemExit(f"IR input does not exist: {ir_path}")
    with compile_ab._performance_lock():
        payload = inventory_ir_text(ir_path.read_text(encoding="utf-8"))
    payload["input"] = {
        "path": str(ir_path),
        "sha256": _sha256(ir_path),
        "size_bytes": ir_path.stat().st_size,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().resolve()
        if output.exists():
            raise SystemExit(f"output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
