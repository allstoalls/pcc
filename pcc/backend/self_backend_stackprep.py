from __future__ import annotations

"""Target-neutral stack-slot preparation for parsed self-backend functions."""

from typing import Callable

from . import BackendUnavailable
from .self_backend_kernel import (
    TYPE_KIND_ARRAY,
    TYPE_KIND_STRUCT,
    get_indexed_function_kernel,
)
from .self_backend_ir import (
    AllocaInfo,
    I1,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_ATOMICRMW,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CMPXCHG,
    PARSED_INSTRUCTION_KIND_EXTRACTELEMENT,
    PARSED_INSTRUCTION_KIND_EXTRACTVALUE,
    PARSED_INSTRUCTION_KIND_FBINOP,
    PARSED_INSTRUCTION_KIND_FCMP,
    PARSED_INSTRUCTION_KIND_FNEG,
    PARSED_INSTRUCTION_KIND_FREEZE,
    PARSED_INSTRUCTION_KIND_INSERTELEMENT,
    PARSED_INSTRUCTION_KIND_INSERTVALUE,
    PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
    PARSED_INSTRUCTION_KIND_SYSCALL6,
    PARSED_INSTRUCTION_KIND_VA_ARG,
    PARSED_INSTRUCTION_KINDS,
    ParsedFunction,
    SlotInfo,
    TypeDesc,
    _align_to,
    parsed_function_publish_alloca_slot,
    parsed_function_publish_value_slot,
)
from .self_backend_value_arena import CompilerInt2, CompilerInt4


def _legacy_instruction_result_type(kind_id: int, data) -> TypeDesc | None:
    if kind_id == PARSED_INSTRUCTION_KIND_LOAD_ATOMIC:
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_ATOMICRMW:
        return data[4]
    if kind_id == PARSED_INSTRUCTION_KIND_CMPXCHG:
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_SYSCALL6:
        return TypeDesc("int", 64)
    if kind_id == PARSED_INSTRUCTION_KIND_FBINOP:
        return data[2]
    if kind_id == PARSED_INSTRUCTION_KIND_FNEG:
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_FCMP:
        value_type = data[2]
        return (
            TypeDesc("array", count=value_type.count, elem=I1)
            if value_type.is_array and value_type.elem is not None
            else I1
        )
    if kind_id in (
        PARSED_INSTRUCTION_KIND_FREEZE,
        PARSED_INSTRUCTION_KIND_INSERTELEMENT,
        PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
        PARSED_INSTRUCTION_KIND_INSERTVALUE,
    ):
        return data[1]
    if (
        kind_id == PARSED_INSTRUCTION_KIND_EXTRACTELEMENT
        or kind_id == PARSED_INSTRUCTION_KIND_EXTRACTVALUE
    ):
        return data[4]
    if kind_id == PARSED_INSTRUCTION_KIND_VA_ARG:
        return data[3]
    return None


def assign_stack_slots(
    func: ParsedFunction,
    *,
    aggregate_returned_indirect: Callable[[TypeDesc], bool],
    aggregate_returned_indirect_indexed=None,
    materialize_legacy_slots: bool = True,
) -> None:
    offset = 0
    kernel = get_indexed_function_kernel(func)
    func.indexed_slot_projection = not materialize_legacy_slots
    # Block-local slot reuse.  Free slots are bucketed by type so an
    # allocation takes the earliest freed slot of its type without scanning
    # every free slot, and ``active_position`` maps a value ID to its index in
    # the active lists so a use never scans them: the direct inline error
    # plane makes blocks several times longer, and both scans were quadratic
    # in block length (cProfile: maybe_free_local_value +4.4x calls, value_id
    # +27x, assign_stack_slots +0.25s on class_gen).  Reuse order is unchanged.
    free_slot_ids_by_type: dict[int, list[int]] = {}
    active_local_value_ids: list[int] = []
    active_local_slot_ids: list[int] = []
    active_position: list[int] = [-1] * len(kernel.value_names)
    legacy_slot_infos_by_id: list[SlotInfo] = []

    def intern_slot_id(slot_offset: int, type_id: int) -> int:
        return kernel.intern_slot_type_id(slot_offset, type_id)

    def legacy_slot_info(slot_id: int) -> SlotInfo:
        if slot_id < len(legacy_slot_infos_by_id):
            return legacy_slot_infos_by_id[slot_id]
        if slot_id != len(legacy_slot_infos_by_id):
            raise BackendUnavailable("indexed slot table is not dense")
        result = SlotInfo(
            kernel.slot_offset(slot_id),
            kernel.type_desc(kernel.slot_type_id(slot_id)),
        )
        legacy_slot_infos_by_id.append(result)
        return result

    def publish_value_slot(value_id: int, value_name: str, slot_id: int) -> None:
        kernel.publish_value_slot_id(value_id, slot_id)
        if materialize_legacy_slots:
            parsed_function_publish_value_slot(
                func,
                value_name,
                legacy_slot_info(slot_id),
            )

    def publish_alloca(
        value_id: int,
        value_name: str,
        slot_offset: int,
        allocated_type_id: int,
    ) -> None:
        kernel.publish_alloca_type_id(
            value_id,
            slot_offset,
            allocated_type_id,
        )
        if materialize_legacy_slots:
            parsed_function_publish_alloca_slot(
                func,
                value_name,
                AllocaInfo(
                    slot_offset,
                    kernel.type_desc(allocated_type_id),
                ),
            )

    def alloc(size: int, align: int) -> int:
        nonlocal offset
        # Offsets name object starts at FP-offset, so align the resulting
        # descending address, not the previous allocation's upper boundary.
        offset = _align_to(offset + size, align)
        return offset

    def alloc_value_slot(
        value_id: int,
        value_type_id: int,
    ) -> int:
        value_layout: CompilerInt4 = kernel.type_layout(value_type_id)
        if kernel.last_use(current_block_id, value_id) is None:
            return intern_slot_id(
                alloc(value_layout.first, value_layout.second),
                value_type_id,
            )
        bucket = free_slot_ids_by_type.get(value_type_id)
        if bucket:
            slot_id = bucket.pop(0)
        else:
            slot_id = intern_slot_id(
                alloc(value_layout.first, value_layout.second),
                value_type_id,
            )
        active_position[value_id] = len(active_local_value_ids)
        active_local_value_ids.append(value_id)
        active_local_slot_ids.append(slot_id)
        return slot_id

    def returned_indirect(type_id: int) -> bool:
        if (
            not materialize_legacy_slots
            and aggregate_returned_indirect_indexed is not None
        ):
            return aggregate_returned_indirect_indexed(kernel, type_id)
        return aggregate_returned_indirect(kernel.types[type_id])

    def publish_object_type_if_required(value_name: str, type_id: int) -> None:
        # This compatibility side table remains authoritative for emit helpers
        # that have not migrated to type IDs yet.  Keep it complete until every
        # reader is converted, then delete it atomically; partially clearing it
        # changes register/peephole decisions and therefore assembly shape.
        func.value_types[value_name] = kernel.types[type_id]

    def maybe_free_local_value(value_id: int, position: int) -> None:
        if value_id < 0 or value_id >= len(active_position):
            return
        active_index = active_position[value_id]
        if active_index < 0:
            return
        if kernel.last_use(current_block_id, value_id) != position:
            return
        slot_id = active_local_slot_ids[active_index]
        slot_type_id = kernel.slot_type_id(slot_id)
        bucket = free_slot_ids_by_type.get(slot_type_id)
        if bucket is None:
            bucket = []
            free_slot_ids_by_type[slot_type_id] = bucket
        bucket.append(slot_id)
        last_index = len(active_local_value_ids) - 1
        if active_index != last_index:
            moved_value_id = active_local_value_ids[last_index]
            active_local_value_ids[active_index] = moved_value_id
            active_local_slot_ids[active_index] = active_local_slot_ids[last_index]
            active_position[moved_value_id] = active_index
        active_local_value_ids.pop()
        active_local_slot_ids.pop()
        active_position[value_id] = -1

    for arg in func.args:
        arg_value_id = kernel.value_id(arg.name)
        if (
            arg.type.is_void
            or arg_value_id < 0
            or not kernel.value_is_used(arg_value_id)
        ):
            continue
        arg_type_id = kernel.intern_type(arg.type)
        publish_object_type_if_required(arg.name, arg_type_id)
        arg_layout: CompilerInt4 = kernel.type_layout(arg_type_id)
        publish_value_slot(
            arg_value_id,
            arg.name,
            intern_slot_id(
                alloc(arg_layout.first, arg_layout.second),
                arg_type_id,
            ),
        )

    return_type_id = kernel.intern_type(func.ret_type)
    if returned_indirect(return_type_id):
        hidden_sret_type_id = kernel.intern_type(
            TypeDesc("ptr", pointee=TypeDesc("void"))
        )
        kernel.hidden_sret_slot_id = intern_slot_id(
            alloc(8, 8),
            hidden_sret_type_id,
        )
        if materialize_legacy_slots:
            func.hidden_sret_slot = legacy_slot_info(
                kernel.hidden_sret_slot_id
            )

    current_block_id = 0
    while current_block_id < len(kernel.block_names):
        phi_fact = kernel.block_phi_fact(current_block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi = kernel.phi_record(phi_fact.first + phi_index)
            phi_value_id = phi.first
            phi_name = kernel.value_name(phi_value_id)
            if phi_value_id < 0:
                raise BackendUnavailable(
                    f"indexed kernel is missing phi value {phi_name!r}"
                )
            phi_type_id = phi.second
            kernel.publish_value_type_id(phi_value_id, phi_type_id)
            publish_object_type_if_required(phi_name, phi_type_id)
            if kernel.value_is_used(phi_value_id):
                publish_value_slot(
                    phi_value_id,
                    phi_name,
                    alloc_value_slot(phi_value_id, phi_type_id),
                )
            phi_index += 1
        block_fact: CompilerInt4 = kernel.block_fact(current_block_id)
        # An inline error edge uses its condition at trigger + 1, a position
        # no instruction use list names.  Free it there like any other last
        # use; otherwise every condition stayed active for the rest of the
        # function, its slot was never reused, and each block end re-sorted
        # all of them (quadratic in the number of call sites).
        edge_span: CompilerInt2 = kernel.inline_error_edge_span(current_block_id)
        edge_cursor = 0
        instruction_index = 0
        instruction_count = block_fact.second
        while instruction_index < instruction_count:
            kind_id = kernel.instruction_kind_id_by_id(
                block_fact.first + instruction_index
            )
            if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
                raise BackendUnavailable(
                    f"corrupt parsed-instruction kind id {kind_id}"
                )
            dest_value_id = kernel.defined_value_id(
                current_block_id,
                instruction_index,
            )
            if dest_value_id >= 0:
                dest = kernel.value_name(dest_value_id)
                result_type_id = kernel.value_type_id(dest_value_id)
                if result_type_id < 0:
                    legacy_type = _legacy_instruction_result_type(
                        kind_id,
                        kernel.instruction_data(
                            current_block_id,
                            instruction_index,
                        ),
                    )
                    if legacy_type is None:
                        raise BackendUnavailable(
                            f"indexed kernel is missing result type for {dest!r}"
                        )
                    result_type_id = kernel.publish_value_type(
                        dest_value_id,
                        legacy_type,
                    )
                publish_object_type_if_required(dest, result_type_id)
                if kind_id == PARSED_INSTRUCTION_KIND_ALLOCA:
                    allocated_type_id = kernel.alloca_type_id(dest_value_id)
                    if kernel.value_is_used(dest_value_id):
                        allocated_layout: CompilerInt4 = kernel.type_span(
                            allocated_type_id
                        )
                        publish_alloca(
                            dest_value_id,
                            dest,
                            alloc(
                                allocated_layout.third,
                                # Raw byte buffers can hold machine pointer
                                # slots. Keep their nominal span unchanged,
                                # but provide at least word-aligned storage.
                                max(allocated_layout.fourth, 8),
                            ),
                            allocated_type_id,
                        )
                elif kernel.value_is_used(dest_value_id) or (
                    kind_id == PARSED_INSTRUCTION_KIND_CALL
                    and returned_indirect(result_type_id)
                ):
                    publish_value_slot(
                        dest_value_id,
                        dest,
                        alloc_value_slot(
                            dest_value_id,
                            result_type_id,
                        ),
                    )
            use_index = 0
            use_count = kernel.instruction_use_count(
                current_block_id, instruction_index
            )
            while use_index < use_count:
                maybe_free_local_value(
                    kernel.instruction_use_id(
                        current_block_id, instruction_index, use_index
                    ),
                    instruction_index,
                )
                use_index += 1
            # Edges are published in trigger order within their block.
            while (
                edge_cursor < edge_span.second
                and kernel.inline_error_edge_trigger(edge_span.first + edge_cursor) + 1
                <= instruction_index
            ):
                edge_id = edge_span.first + edge_cursor
                maybe_free_local_value(
                    kernel.inline_error_edge_condition(edge_id),
                    kernel.inline_error_edge_trigger(edge_id) + 1,
                )
                edge_cursor += 1
            instruction_index += 1
        term_pos = instruction_count
        # Same order as before (by value name); the IDs are already at hand,
        # so no name -> ID re-lookup.
        remaining_values: list[str] = []
        remaining_ids_by_name: dict[str, int] = {}
        for active_value_id in active_local_value_ids:
            remaining_name = kernel.value_name(active_value_id)
            remaining_values.append(remaining_name)
            remaining_ids_by_name[remaining_name] = active_value_id
        remaining_values.sort()
        for remaining_value in remaining_values:
            maybe_free_local_value(remaining_ids_by_name[remaining_value], term_pos)
        current_block_id += 1

    func.frame_size = _align_to(offset, 16)
    kernel.finish_slot_interning()
    if not materialize_legacy_slots:
        if kernel.supported_object_projection_is_closed(func):
            func.value_types.clear()
            func.block_map.clear()
            kernel.release_block_projections(func)
