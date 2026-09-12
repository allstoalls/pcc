from __future__ import annotations

import os
import sys

"""Direct llvm_capi builder records -> final self-backend kernel adapter.

The canonical text records remain the diagnostic/oracle projection.  When
direct capture is enabled, builder operations publish their already-structured
operands into the final seed's compact arenas.  One integer on the existing
``InstructionRecord`` preserves final insertion order; no per-instruction
tuple/dataclass mirror is introduced.
"""

from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_call_flags import classify_call_flags
from pcc.backend.self_backend_ir import (
    ArgInfo,
    I1,
    IndexedCallPlane,
    ParsedFunction,
    ParsedModule,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
    PARSED_INSTRUCTION_KINDS,
    TypeDesc,
    _PARSED_INSTRUCTION_KIND_IDS,
    aggregate_member_info,
)
from pcc.backend.self_backend_kernel import (
    INLINE_ERROR_EDGE_WIDTH,
    IndexedFunctionSeed,
    get_indexed_function_kernel,
)
from pcc.backend.self_backend_literals import const_int_from_value
from pcc.backend.self_backend_parse import (
    build_indexed_function_seed_from_block_lines,
    check_simple_symbol_name,
    decode_global_name,
    decode_ssa_name,
    decode_value_token,
    parse_ir_type,
    parse_self_backend_module,
)
from pcc.backend.self_backend_value_arena import CompilerInt2, CompilerInt4, CompilerIntArena

_DIRECT_RECORD_FLAG_VOLATILE = 1
_DIRECT_RECORD_KIND_PHI = -1
_DIRECT_FIXED_PAYLOAD_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_SELECT,
)
_DIRECT_VOID_TYPE = TypeDesc("void")
_DIRECT_OPAQUE_PTR_TYPE = TypeDesc("ptr", pointee=_DIRECT_VOID_TYPE)


class DirectIndexedFunctionBuilder:
    """Compact construction owner populated while llvm_capi operands live."""

    function: _ir.Function | None
    seed: IndexedFunctionSeed
    record_metadata: CompilerIntArena
    record_use_spans: CompilerIntArena
    record_use_ids: CompilerIntArena
    fuse_uses: bool
    terminator_scalars: CompilerIntArena
    terminator_case_scalars: CompilerIntArena
    phi_scalars: CompilerIntArena
    phi_incoming_scalars: CompilerIntArena
    error_edge_scalars: CompilerIntArena
    error_edge_heads: CompilerIntArena
    error_edge_tails: CompilerIntArena
    error_edge_next: CompilerIntArena
    error_landing_scalars: CompilerIntArena
    record_arithmetic_flags: dict[int, tuple[str, ...]]
    type_cache: dict[int, tuple[object, TypeDesc]]
    type_id_cache: dict[int, tuple[object, int]]
    type_desc_ids: dict[int, tuple[TypeDesc, int]]
    derived_pointer_type_ids: dict[int, tuple[TypeDesc, int]]
    opaque_pointer_type_id: int
    target_names: list[str]
    target_name_ids: dict[str, int]
    callee_cache: dict[str, tuple[str, int, int]]
    forward_value_ids: dict[str, int]
    supported_records: int
    fallback_records: int

    __slots__ = (
        "function",
        "seed",
        "record_metadata",
        "record_use_spans",
        "record_use_ids",
        "fuse_uses",
        "terminator_scalars",
        "terminator_case_scalars",
        "phi_scalars",
        "phi_incoming_scalars",
        "error_edge_scalars",
        "error_edge_heads",
        "error_edge_tails",
        "error_edge_next",
        "error_landing_scalars",
        "record_arithmetic_flags",
        "type_cache",
        "type_id_cache",
        "type_desc_ids",
        "derived_pointer_type_ids",
        "opaque_pointer_type_id",
        "target_names",
        "target_name_ids",
        "callee_cache",
        "forward_value_ids",
        "supported_records",
        "fallback_records",
    )

    def __init__(self, function=None) -> None:
        self.function = function
        value_capacity_hint = 64 if function is not None else 0
        self.seed = IndexedFunctionSeed(0, value_capacity_hint)
        # kind ID, payload ID, destination value ID, flags.
        self.record_metadata = CompilerIntArena()
        self.record_use_spans = CompilerIntArena()
        self.record_use_ids = CompilerIntArena()
        self.fuse_uses = str(
            os.environ.get("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        self.terminator_scalars = CompilerIntArena()
        self.terminator_case_scalars = CompilerIntArena()
        self.phi_scalars = CompilerIntArena()
        self.phi_incoming_scalars = CompilerIntArena()
        # Raw construction record: source-name ID, trigger record index,
        # condition value ID, error-name ID, source line, cleanup-plan ID.
        # Per-name linked heads preserve source order without a Python
        # list/dict projection per block.
        self.error_edge_scalars = CompilerIntArena()
        self.error_edge_heads = CompilerIntArena()
        self.error_edge_tails = CompilerIntArena()
        self.error_edge_next = CompilerIntArena()
        # (landing target-name ID, payload slot value ID) pairs.
        self.error_landing_scalars = CompilerIntArena()
        self.record_arithmetic_flags: dict[int, tuple[str, ...]] = {}
        # id() keys pin their source object beside the converted descriptor.
        self.type_cache: dict[int, tuple[object, TypeDesc]] = {}
        self.type_id_cache: dict[int, tuple[object, int]] = {}
        self.type_desc_ids: dict[int, tuple[TypeDesc, int]] = {}
        # GEP derives the same typed-pointer projection repeatedly.  Pin the
        # canonical pointee beside its final type ID so an id() key cannot go
        # stale, and skip rebuilding/equality-scanning an equal TypeDesc.
        self.derived_pointer_type_ids: dict[int, tuple[TypeDesc, int]] = {}
        self.opaque_pointer_type_id = -1
        self.target_names: list[str] = []
        self.target_name_ids: dict[str, int] = {}
        self.callee_cache: dict[str, tuple[str, int, int]] = {}
        self.forward_value_ids: dict[str, int] = {}
        self.supported_records = 0
        self.fallback_records = 0
        if function is not None:
            for argument in function.args:
                argument_id = self.seed.append_proven_new_value(
                    decode_ssa_name(argument._ref)
                )
                self.seed.define_value_id(
                    argument_id,
                    -2,
                    self._type_desc(argument.type),
                    -1,
                )
                argument._direct_value_id = argument_id

    def _type_desc(self, value_type) -> TypeDesc:
        # llvm_capi tracks a compile-time pointee on each PointerType wrapper,
        # while the self backend consumes LLVM's opaque ``ptr`` projection.
        # Every wrapper therefore maps to the same immutable descriptor; do
        # not pin and re-compare one identity per emitted pointer operation.
        if isinstance(value_type, _ir.PointerType):
            return _DIRECT_OPAQUE_PTR_TYPE
        identity = id(value_type)
        cached = self.type_cache.get(identity)
        if cached is not None and cached[0] is value_type:
            return cached[1]
        if isinstance(value_type, _ir.VoidType):
            result = _DIRECT_VOID_TYPE
        elif isinstance(value_type, _ir.IntType):
            result = TypeDesc("int", int(value_type.width))
        elif isinstance(value_type, _ir.HalfType):
            result = TypeDesc("fp", 16)
        elif isinstance(value_type, _ir.FloatType):
            result = TypeDesc("fp", 32)
        elif isinstance(value_type, _ir.DoubleType):
            result = TypeDesc("fp", 64)
        elif isinstance(value_type, _ir.ArrayType):
            result = TypeDesc(
                "array",
                count=int(value_type.count),
                elem=self._type_desc(value_type.element),
            )
        elif isinstance(value_type, _ir.BaseStructType):
            fields = []
            for field_type in value_type.elements:
                fields.append(self._type_desc(field_type))
            result = TypeDesc(
                "struct",
                name=(
                    str(value_type.name)
                    if isinstance(value_type, _ir.IdentifiedStructType)
                    else ""
                ),
                fields=tuple(fields),
            )
        else:
            raise BackendUnavailable(
                "direct indexed builder does not support llvm_capi type "
                + str(type(value_type).__name__)
            )
        self.type_cache[identity] = (value_type, result)
        return result

    def _intern_type_desc(self, value_type: TypeDesc) -> int:
        identity = id(value_type)
        cached = self.type_desc_ids.get(identity)
        if cached is not None and cached[0] is value_type:
            return cached[1]
        type_id = self.seed.intern_type(value_type)
        self.type_desc_ids[identity] = (value_type, type_id)
        return type_id

    def _type_id(self, value_type) -> int:
        # All llvm_capi pointer wrappers have one opaque projection.  Avoid
        # retaining one source-wrapper entry per SSA pointer value.
        if isinstance(value_type, _ir.PointerType):
            type_id = self.opaque_pointer_type_id
            if type_id < 0:
                type_id = self.seed.intern_type(_DIRECT_OPAQUE_PTR_TYPE)
                self.opaque_pointer_type_id = type_id
                self.type_desc_ids[id(_DIRECT_OPAQUE_PTR_TYPE)] = (
                    _DIRECT_OPAQUE_PTR_TYPE,
                    type_id,
                )
            return type_id
        identity = id(value_type)
        cached = self.type_id_cache.get(identity)
        if cached is not None and cached[0] is value_type:
            return cached[1]
        value_desc = self._type_desc(value_type)
        type_id = self.seed.intern_type(value_desc)
        self.type_id_cache[identity] = (value_type, type_id)
        self.type_desc_ids[id(value_desc)] = (value_desc, type_id)
        return type_id

    def _intern_derived_pointer_type(self, pointee: TypeDesc) -> int:
        identity = id(pointee)
        cached = self.derived_pointer_type_ids.get(identity)
        if cached is not None and cached[0] is pointee:
            return cached[1]
        type_id = self._intern_type_desc(TypeDesc("ptr", pointee=pointee))
        self.derived_pointer_type_ids[identity] = (pointee, type_id)
        return type_id

    def _value_ref_text(self, value_ref: _ir.Value) -> str:
        return value_ref._ref

    def _dest_value_id(self, dest_ref: _ir.Value) -> int:
        existing_id = dest_ref._direct_value_id
        if existing_id >= 0:
            return existing_id
        raw_ref = self._value_ref_text(dest_ref)
        name = dest_ref._direct_name
        if not name:
            name = decode_ssa_name(raw_ref)
        value_id = -1
        if self.forward_value_ids:
            value_id = self.forward_value_ids.pop(name, -1)
        if value_id < 0:
            value_id = self.seed.append_proven_new_value(name)
        dest_ref._direct_value_id = value_id
        return value_id

    def _dest_text_id(self, dest_ref: str) -> int:
        return self.seed.intern_value(decode_ssa_name(dest_ref))

    def _operand_value_ref(self, value_ref: _ir.Value) -> int:
        raw_ref = self._value_ref_text(value_ref)
        if raw_ref.startswith("%"):
            existing_id = value_ref._direct_value_id
            if existing_id >= 0:
                return existing_id
            name = value_ref._direct_name
            if not name:
                name = decode_ssa_name(raw_ref)
            value_id = self.seed.intern_value(name)
            self.forward_value_ids[name] = value_id
            value_ref._direct_value_id = value_id
            return value_id
        decoded = decode_value_token(raw_ref)
        return self.seed.operand_ref(decoded)

    def _operand_text_ref(self, value_ref: str) -> int:
        return self.seed.operand_ref(decode_value_token(value_ref))

    def _target_id(self, block_name: str) -> int:
        existing = self.target_name_ids.get(block_name)
        if existing is not None:
            return existing
        target_id = len(self.target_names)
        self.target_names.append(block_name)
        self.target_name_ids[block_name] = target_id
        self.error_edge_heads.append(-1)
        self.error_edge_tails.append(-1)
        return target_id

    def _callee_record(
        self,
        callee_ref: str,
        is_indirect: bool,
        is_vararg: bool,
    ) -> tuple[str, int, int]:
        if not is_vararg:
            cached = self.callee_cache.get(callee_ref)
            if cached is not None:
                return cached
        decoded_callee = (
            decode_ssa_name(callee_ref)
            if is_indirect
            else decode_global_name(callee_ref)
        )
        if not is_indirect:
            check_simple_symbol_name(decoded_callee)
            if (
                decoded_callee.startswith("py_cpy_")
                and self.function is not None
                and not self.function._direct_first_libpython_callee
            ):
                self.function._direct_first_libpython_callee = decoded_callee
        result = (
            decoded_callee,
            self.seed.intern_text(decoded_callee),
            classify_call_flags(decoded_callee, is_indirect, is_vararg),
        )
        if not is_vararg:
            self.callee_cache[callee_ref] = result
        return result

    def _append_metadata(
        self,
        kind: str,
        payload_id: int,
        dest_value_id: int,
        flags: int = 0,
        use0: int = -1,
        use1: int = -1,
        use2: int = -1,
        use_start: int = -1,
        use_count: int = -1,
    ) -> int:
        kind_id = (
            _DIRECT_RECORD_KIND_PHI
            if kind == "phi"
            else _PARSED_INSTRUCTION_KIND_IDS.get(kind)
        )
        if kind_id is None:
            raise BackendUnavailable(
                "direct indexed builder has unknown record kind " + kind
            )
        record_id = self.record_metadata._length // 4
        self.record_metadata.append4(
            kind_id,
            payload_id,
            dest_value_id,
            flags,
        )
        if self.fuse_uses:
            if use_start < 0:
                use_start = self.record_use_ids._length
                use_count = 0
                if use0 >= 0:
                    self.record_use_ids.append(use0)
                    use_count += 1
                if use1 >= 0:
                    self.record_use_ids.append(use1)
                    use_count += 1
                if use2 >= 0:
                    self.record_use_ids.append(use2)
                    use_count += 1
            self.record_use_spans.append2(use_start, use_count)
        self.supported_records += 1
        return record_id

    def publish_alloca(self, dest_ref: _ir.Value, allocated_type) -> int:
        dest_id = self._dest_value_id(dest_ref)
        allocated_type_id = self._type_id(allocated_type)
        self.seed.publish_alloca_type_id(dest_id, allocated_type_id)
        self.seed.publish_value_type_id(
            dest_id,
            self._intern_derived_pointer_type(self._type_desc(allocated_type)),
        )
        return self._append_metadata("alloca", dest_id, dest_id)

    def publish_store(
        self,
        value_type,
        value_ref: _ir.Value,
        ptr_type,
        ptr_ref: _ir.Value,
        is_volatile: bool = False,
    ) -> int:
        record_id = self.seed.instruction_record_scalars._length // 4
        value_operand = self._operand_value_ref(value_ref)
        ptr_operand = self._operand_value_ref(ptr_ref)
        self.seed.instruction_record_scalars.append4(
            self._type_id(value_type),
            value_operand,
            self._type_id(ptr_type),
            ptr_operand,
        )
        self.seed.instruction_record_dest_ids.append(-1)
        return self._append_metadata(
            "store",
            record_id,
            -1,
            _DIRECT_RECORD_FLAG_VOLATILE if is_volatile else 0,
            value_operand,
            ptr_operand,
        )

    def publish_load(
        self,
        dest_ref: _ir.Value,
        value_type,
        ptr_type,
        ptr_ref: _ir.Value,
        is_volatile: bool = False,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        value_type_id = self._type_id(value_type)
        ptr_operand = self._operand_value_ref(ptr_ref)
        record_id = self.seed.instruction_record_scalars._length // 4
        self.seed.instruction_record_scalars.append4(
            value_type_id,
            self._type_id(ptr_type),
            ptr_operand,
            0,
        )
        self.seed.instruction_record_dest_ids.append(dest_id)
        self.seed.publish_value_type_id(dest_id, value_type_id)
        return self._append_metadata(
            "load",
            record_id,
            dest_id,
            _DIRECT_RECORD_FLAG_VOLATILE if is_volatile else 0,
            ptr_operand,
        )

    def publish_binop(
        self,
        op: str,
        dest_ref: _ir.Value,
        value_type,
        lhs_ref: _ir.Value,
        rhs_ref: _ir.Value,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        value_type_id = self._type_id(value_type)
        lhs_operand = self._operand_value_ref(lhs_ref)
        rhs_operand = self._operand_value_ref(rhs_ref)
        payload_id = self.seed.instruction_record_scalars._length // 4
        self.seed.instruction_record_scalars.append4(
            self.seed.intern_text(op),
            value_type_id,
            lhs_operand,
            rhs_operand,
        )
        self.seed.instruction_record_dest_ids.append(dest_id)
        self.seed.publish_value_type_id(dest_id, value_type_id)
        return self._append_metadata(
            "binop", payload_id, dest_id, 0, lhs_operand, rhs_operand
        )

    def publish_icmp(
        self,
        predicate: str,
        dest_ref: _ir.Value,
        value_type,
        lhs_ref: _ir.Value,
        rhs_ref: _ir.Value,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        value_desc = self._type_desc(value_type)
        value_type_id = self._intern_type_desc(value_desc)
        result_desc = (
            TypeDesc("array", count=value_desc.count, elem=I1)
            if value_desc.is_array and value_desc.elem is not None
            else I1
        )
        payload_id = self.seed.instruction_record_scalars._length // 4
        lhs_operand = self._operand_value_ref(lhs_ref)
        rhs_operand = self._operand_value_ref(rhs_ref)
        self.seed.instruction_record_scalars.append4(
            self.seed.intern_text(predicate),
            value_type_id,
            lhs_operand,
            rhs_operand,
        )
        self.seed.instruction_record_dest_ids.append(dest_id)
        self.seed.publish_value_type_id(
            dest_id,
            self._intern_type_desc(result_desc),
        )
        return self._append_metadata(
            "icmp", payload_id, dest_id, 0, lhs_operand, rhs_operand
        )

    def publish_fbinop(
        self,
        op: str,
        dest_ref: _ir.Value,
        value_type,
        lhs_ref: _ir.Value,
        rhs_ref: _ir.Value,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        dest_name = self.seed.value_names[dest_id]
        value_desc = self._type_desc(value_type)
        lhs_operand = self._operand_value_ref(lhs_ref)
        rhs_operand = self._operand_value_ref(rhs_ref)
        payload_id = self.seed.append_cold_instruction_data(
            (
                op,
                dest_name,
                value_desc,
                decode_value_token(self._value_ref_text(lhs_ref)),
                decode_value_token(self._value_ref_text(rhs_ref)),
            )
        )
        self.seed.publish_value_type_id(
            dest_id,
            self._intern_type_desc(value_desc),
        )
        return self._append_metadata(
            "fbinop",
            payload_id,
            dest_id,
            0,
            lhs_operand,
            rhs_operand,
        )

    def publish_fcmp(
        self,
        predicate: str,
        dest_ref: _ir.Value,
        value_type,
        lhs_ref: _ir.Value,
        rhs_ref: _ir.Value,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        dest_name = self.seed.value_names[dest_id]
        value_desc = self._type_desc(value_type)
        lhs_operand = self._operand_value_ref(lhs_ref)
        rhs_operand = self._operand_value_ref(rhs_ref)
        payload_id = self.seed.append_cold_instruction_data(
            (
                predicate,
                dest_name,
                value_desc,
                decode_value_token(self._value_ref_text(lhs_ref)),
                decode_value_token(self._value_ref_text(rhs_ref)),
            )
        )
        self.seed.publish_value_type_id(
            dest_id,
            self._intern_type_desc(I1),
        )
        return self._append_metadata(
            "fcmp",
            payload_id,
            dest_id,
            0,
            lhs_operand,
            rhs_operand,
        )

    def publish_fneg(
        self,
        dest_ref: _ir.Value,
        value_type,
        value_ref: _ir.Value,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        dest_name = self.seed.value_names[dest_id]
        value_desc = self._type_desc(value_type)
        source_operand = self._operand_value_ref(value_ref)
        payload_id = self.seed.append_cold_instruction_data(
            (
                dest_name,
                value_desc,
                decode_value_token(self._value_ref_text(value_ref)),
            )
        )
        self.seed.publish_value_type_id(
            dest_id,
            self._intern_type_desc(value_desc),
        )
        return self._append_metadata(
            "fneg",
            payload_id,
            dest_id,
            0,
            source_operand,
        )

    def publish_cast(
        self,
        op: str,
        dest_ref: _ir.Value,
        src_type,
        value_ref: _ir.Value,
        dst_type,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        dst_type_id = self._type_id(dst_type)
        source_operand = self._operand_value_ref(value_ref)
        payload_id = self.seed.instruction_record_scalars._length // 4
        self.seed.instruction_record_scalars.append4(
            self.seed.intern_text(op),
            self._type_id(src_type),
            source_operand,
            dst_type_id,
        )
        self.seed.instruction_record_dest_ids.append(dest_id)
        self.seed.publish_value_type_id(dest_id, dst_type_id)
        return self._append_metadata(
            "cast", payload_id, dest_id, 0, source_operand
        )

    def publish_select(
        self,
        dest_ref: _ir.Value,
        result_type,
        cond_ref: _ir.Value,
        true_ref: _ir.Value,
        false_ref: _ir.Value,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        result_type_id = self._type_id(result_type)
        cond_operand = self._operand_value_ref(cond_ref)
        true_operand = self._operand_value_ref(true_ref)
        false_operand = self._operand_value_ref(false_ref)
        payload_id = self.seed.instruction_record_scalars._length // 4
        self.seed.instruction_record_scalars.append4(
            result_type_id,
            cond_operand,
            true_operand,
            false_operand,
        )
        self.seed.instruction_record_dest_ids.append(dest_id)
        self.seed.publish_value_type_id(dest_id, result_type_id)
        return self._append_metadata(
            "select",
            payload_id,
            dest_id,
            0,
            cond_operand,
            true_operand,
            false_operand,
        )

    def publish_extractvalue(
        self,
        dest_ref: _ir.Value,
        aggregate_type,
        aggregate_ref: _ir.Value,
        indices,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        dest_name = self.seed.value_names[dest_id]
        aggregate_desc = self._type_desc(aggregate_type)
        aggregate_operand = self._operand_value_ref(aggregate_ref)
        index_values = []
        for index in indices:
            index_values.append(int(index))
        index_tuple = tuple(index_values)
        result_type, offset = aggregate_member_info(
            aggregate_desc,
            index_tuple,
        )
        self.seed.publish_value_type_id(
            dest_id,
            self._intern_type_desc(result_type),
        )
        payload_id = self.seed.append_cold_instruction_data(
            (
                dest_name,
                aggregate_desc,
                decode_value_token(self._value_ref_text(aggregate_ref)),
                index_tuple,
                result_type,
                offset,
            )
        )
        return self._append_metadata(
            "extractvalue",
            payload_id,
            dest_id,
            0,
            aggregate_operand,
        )

    def publish_gep(
        self,
        dest_ref: _ir.Value,
        base_type,
        ptr_type,
        ptr_ref: _ir.Value,
        indices,
    ) -> int:
        dest_id = self._dest_value_id(dest_ref)
        base_desc = self._type_desc(base_type)
        current_type = base_desc
        index_start = self.seed.gep_index_scalars._length // 2
        index_count = 0
        use_start = self.record_use_ids._length
        use_count = 0
        ptr_operand = self._operand_value_ref(ptr_ref)
        if self.fuse_uses and ptr_operand >= 0:
            self.record_use_ids.append(ptr_operand)
            use_count += 1
        for index_value in indices:
            index_ref = decode_value_token(_ir._value_ref(index_value))
            index_operand = self._operand_value_ref(index_value)
            self.seed.gep_index_scalars.append2(
                self._type_id(index_value.type),
                index_operand,
            )
            if self.fuse_uses and index_operand >= 0:
                self.record_use_ids.append(index_operand)
                use_count += 1
            if index_count > 0:
                if current_type.is_array:
                    if current_type.elem is None:
                        raise BackendUnavailable("direct GEP array has no element")
                    current_type = current_type.elem
                elif current_type.is_struct:
                    field_index = const_int_from_value(index_ref)
                    if field_index is None:
                        raise BackendUnavailable(
                            "direct struct GEP requires constant field index"
                        )
                    current_type = current_type.field_type(field_index)
                else:
                    raise BackendUnavailable(
                        "direct GEP cannot index scalar pointee"
                    )
            index_count += 1
        if index_count == 0:
            raise BackendUnavailable("direct GEP requires at least one index")
        result_type_id = self._intern_derived_pointer_type(current_type)
        payload_id = self.seed.gep_scalars._length // 8
        self.seed.gep_scalars.append4(
            self._intern_type_desc(base_desc),
            self._type_id(ptr_type),
            ptr_operand,
            index_start,
        )
        self.seed.gep_scalars.append4(
            index_count,
            result_type_id,
            dest_id,
            0,
        )
        self.seed.publish_value_type_id(dest_id, result_type_id)
        return self._append_metadata(
            "gep",
            payload_id,
            dest_id,
            use_start=use_start,
            use_count=use_count,
        )

    def publish_call(
        self,
        dest_ref: _ir.Value | None,
        ret_type,
        callee_ref: str,
        is_indirect: bool,
        args,
        expected_arg_types,
        fixed_arg_count: int,
        is_vararg: bool,
    ) -> int:
        arg_start = self.seed.args._length // 4
        use_start = self.record_use_ids._length
        use_count = 0
        if is_indirect:
            callee_operand = self._operand_text_ref(callee_ref)
            if self.fuse_uses and callee_operand >= 0:
                self.record_use_ids.append(callee_operand)
                use_count += 1
        arg_count = 0
        while arg_count < len(args):
            arg = args[arg_count]
            arg_type = (
                expected_arg_types[arg_count]
                if arg_count < fixed_arg_count
                else arg.type
            )
            operand_ref = self._operand_value_ref(arg)
            self.seed.args.append4(
                self._type_id(arg_type),
                operand_ref if operand_ref >= 0 else -1,
                -operand_ref - 1 if operand_ref < 0 else -1,
                0,
            )
            if self.fuse_uses and operand_ref >= 0:
                self.record_use_ids.append(operand_ref)
                use_count += 1
            arg_count += 1
        _decoded_callee, callee_text_id, call_flags = self._callee_record(
            callee_ref,
            is_indirect,
            is_vararg,
        )
        dest_id = (
            -1 if dest_ref is None else self._dest_value_id(dest_ref)
        )
        ret_type_id = self._type_id(ret_type)
        call_id = self.seed.records._length // 8
        self.seed.records.append4(
            ret_type_id,
            callee_text_id,
            call_flags,
            arg_start,
        )
        self.seed.records.append4(
            arg_count,
            fixed_arg_count,
            dest_id,
            0,
        )
        if dest_id >= 0:
            self.seed.publish_value_type_id(dest_id, ret_type_id)
        return self._append_metadata(
            "call",
            call_id,
            dest_id,
            use_start=use_start,
            use_count=use_count,
        )

    def publish_raw_call(
        self,
        dest_ref: str | None,
        ret_type,
        callee_ref: str,
        arg_type_texts,
        arg_ref_texts,
    ) -> int:
        arg_start = self.seed.args._length // 4
        use_start = self.record_use_ids._length
        use_count = 0
        arg_count = 0
        while arg_count < len(arg_ref_texts):
            arg_desc = parse_ir_type(str(arg_type_texts[arg_count]))
            arg_ref = decode_value_token(str(arg_ref_texts[arg_count]))
            if arg_desc.is_int and arg_desc.width == 1:
                if arg_ref == "false":
                    arg_ref = "0"
                elif arg_ref == "true":
                    arg_ref = "1"
            self.seed.append_arg(arg_desc, arg_ref, 0)
            operand_ref = self._operand_text_ref(str(arg_ref_texts[arg_count]))
            if self.fuse_uses and operand_ref >= 0:
                self.record_use_ids.append(operand_ref)
                use_count += 1
            arg_count += 1
        is_indirect = callee_ref.startswith("%")
        decoded_callee = (
            decode_ssa_name(callee_ref)
            if is_indirect
            else decode_global_name(callee_ref)
        )
        if not is_indirect:
            check_simple_symbol_name(decoded_callee)
            if (
                decoded_callee.startswith("py_cpy_")
                and self.function is not None
                and not self.function._direct_first_libpython_callee
            ):
                self.function._direct_first_libpython_callee = decoded_callee
        else:
            callee_operand = self._operand_text_ref(callee_ref)
            if self.fuse_uses and callee_operand >= 0:
                # Indirect callee precedes arguments in ordinary use order.
                self.record_use_ids.append(callee_operand)
                use_count += 1
        dest_id = -1 if dest_ref is None else self._dest_text_id(dest_ref)
        ret_type_id = self._type_id(ret_type)
        call_id = self.seed.records._length // 8
        self.seed.records.append4(
            ret_type_id,
            self.seed.intern_text(decoded_callee),
            classify_call_flags(decoded_callee, is_indirect, False),
            arg_start,
        )
        self.seed.records.append4(
            arg_count,
            arg_count,
            dest_id,
            0,
        )
        if dest_id >= 0:
            self.seed.publish_value_type_id(dest_id, ret_type_id)
        return self._append_metadata(
            "call",
            call_id,
            dest_id,
            use_start=use_start,
            use_count=use_count,
        )

    def publish_phi(self, dest_ref: _ir.Value, value_type) -> int:
        dest_id = self._dest_value_id(dest_ref)
        value_type_id = self._type_id(value_type)
        self.seed.publish_value_type_id(dest_id, value_type_id)
        phi_id = self.phi_scalars._length // 4
        self.phi_scalars.append4(
            dest_id,
            value_type_id,
            self.phi_incoming_scalars._length // 2,
            0,
        )
        return self._append_metadata("phi", phi_id, dest_id)

    def append_phi_incoming(
        self,
        record_id: int,
        value_ref: _ir.Value,
        block_name: str,
    ) -> None:
        metadata: CompilerInt4 = self.record_metadata.get4_unchecked(record_id)
        phi_id = metadata.second
        phi: CompilerInt4 = self.phi_scalars.get4_unchecked(phi_id)
        self.phi_incoming_scalars.append2(
            self._operand_value_ref(value_ref),
            self._target_id(block_name),
        )
        self.phi_scalars.set_unchecked(phi_id * 4 + 3, phi.fourth + 1)

    def publish_terminator(
        self,
        kind: str,
        value_type=None,
        value_ref: _ir.Value | None = None,
        target0: str = "",
        target1: str = "",
    ) -> int:
        terminator_id = self.terminator_scalars._length // 8
        kind_id = _PARSED_INSTRUCTION_KIND_IDS[kind]
        type_id = (
            -1
            if value_type is None
            else self._type_id(value_type)
        )
        operand = (
            -1 if value_ref is None else self._operand_value_ref(value_ref)
        )
        target0_id = -1 if target0 == "" else self._target_id(target0)
        target1_id = -1 if target1 == "" else self._target_id(target1)
        self.terminator_scalars.append4(kind_id, type_id, operand, target0_id)
        self.terminator_scalars.append4(
            target1_id,
            self.terminator_case_scalars._length // 2,
            0,
            0,
        )
        return self._append_metadata(
            kind,
            terminator_id,
            -1,
            0,
            operand,
        )

    def append_switch_case(
        self,
        record_id: int,
        value: int,
        target_name: str,
    ) -> None:
        metadata: CompilerInt4 = self.record_metadata.get4_unchecked(record_id)
        terminator_id = metadata.second
        span: CompilerInt4 = self.terminator_scalars.get4_unchecked(
            terminator_id * 2 + 1
        )
        self.terminator_case_scalars.append2(
            int(value),
            self._target_id(target_name),
        )
        self.terminator_scalars.set_unchecked(
            terminator_id * 8 + 6,
            span.third + 1,
        )

    def publish_inline_error_edge(
        self,
        source_block_name: str,
        condition,
        error_block_name: str,
        source_line: int,
        cleanup_plan_id: int,
        payload: int = -1,
    ) -> None:
        """Publish ``if condition: goto error_block`` right after ``condition``.

        The trigger is the condition's own defining instruction.  Its block
        position is resolved at finalization from the published definition
        rather than snapshotted here: the frontend inserts allocas, hoisted
        constants and root stores ahead of already-emitted records in the same
        block (``position_at_start`` / ``position_before``), so any index taken
        now drifts before the kernel is finalized.
        """
        condition_id = self._operand_value_ref(condition)
        if condition_id < 0:
            raise BackendUnavailable(
                "direct inline error edge requires an SSA condition"
            )
        source_id = self._target_id(source_block_name)
        edge_id = len(self.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH
        self.error_edge_scalars.append4(
            source_id,
            -1,
            condition_id,
            self._target_id(error_block_name),
        )
        self.error_edge_scalars.append4(
            int(source_line),
            int(cleanup_plan_id),
            int(payload),
            0,
        )
        self.error_edge_next.append(-1)
        previous_tail = self.error_edge_tails.get_unchecked(source_id)
        if previous_tail < 0:
            self.error_edge_heads.set_unchecked(source_id, edge_id)
        else:
            self.error_edge_next.set_unchecked(previous_tail, edge_id)
        self.error_edge_tails.set_unchecked(source_id, edge_id)

    def publish_inline_error_landing(self, block_name: str, slot) -> None:
        """Declare ``block_name`` as a shared frame landing reading ``slot``.

        Every inline error edge that targets the block carries a payload
        index; the emitter's cold stub stores it into ``slot`` (an i32 entry
        alloca) before jumping, and the landing's first load reads it back.
        """
        slot_id = self._operand_value_ref(slot)
        if slot_id < 0:
            raise BackendUnavailable(
                "direct inline error landing requires an alloca slot value"
            )
        self.error_landing_scalars.append2(self._target_id(block_name), slot_id)

    def set_arithmetic_flags(self, record_id: int, flags) -> None:
        values = tuple(flags)
        if values:
            self.record_arithmetic_flags[record_id] = values
        elif record_id in self.record_arithmetic_flags:
            del self.record_arithmetic_flags[record_id]

    def diagnostic_record_text(self, record_id: int) -> str:
        metadata: CompilerInt4 = self.record_metadata.get4_unchecked(record_id)
        kind = _record_kind(metadata)
        prefix = ""
        if metadata.third >= 0:
            dest_name = self.seed.value_names[metadata.third]
            prefix = (
                dest_name if dest_name.startswith("%") else "%" + dest_name
            ) + " = "
        if kind == "call":
            header: CompilerInt4 = self.seed.records.get4_unchecked(
                metadata.second * 2
            )
            callee_name = self.seed.texts[header.second]
            callee_ref = (
                (callee_name if callee_name.startswith("%") else "%" + callee_name)
                if header.third & 1
                else "@" + callee_name
            )
            return prefix + "call " + callee_ref
        if kind == "ret_void":
            return "ret void"
        if kind == "br" or kind == "br_cond":
            return "br label"
        if kind == "switch":
            return "switch i1"
        if kind == "unreachable":
            return "unreachable"
        return prefix + kind

    def release_construction_state(self) -> None:
        """Drop builder-only planes after the kernel adopts final arenas."""
        self.record_metadata.close()
        self.record_use_spans.close()
        self.record_use_ids.close()
        self.terminator_scalars.close()
        self.terminator_case_scalars.close()
        self.phi_scalars.close()
        self.phi_incoming_scalars.close()
        self.error_edge_scalars.close()
        self.error_edge_heads.close()
        self.error_edge_tails.close()
        self.error_edge_next.close()
        self.error_landing_scalars.close()
        self.record_arithmetic_flags.clear()
        self.type_cache.clear()
        self.type_id_cache.clear()
        self.type_desc_ids.clear()
        self.derived_pointer_type_ids.clear()
        self.opaque_pointer_type_id = -1
        self.target_names = []
        self.target_name_ids.clear()
        self.callee_cache.clear()
        self.forward_value_ids.clear()


def _record_metadata(
    builder: DirectIndexedFunctionBuilder,
    record: _ir.InstructionRecord,
) -> CompilerInt4:
    return builder.record_metadata.get4_unchecked(record._direct_record_id)


def _record_kind(metadata: CompilerInt4) -> str:
    if metadata.first == _DIRECT_RECORD_KIND_PHI:
        return "phi"
    return PARSED_INSTRUCTION_KINDS[metadata.first]


def _function_fallback_record_count(function: _ir.Function) -> int:
    count = 0
    report = str(
        os.environ.get("PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "")
        or ""
    ).strip().lower() in ("1", "true", "yes", "on")
    reported = 0
    for block in function.blocks:
        for record in block._instrs:
            if record._direct_record_id < 0:
                count += 1
                if report and reported < 8:
                    sys.stderr.write(
                        "pcc direct indexed fallback function="
                        + str(function.name)
                        + " block="
                        + str(block.name)
                        + " opname="
                        + str(record.opname)
                        + " text="
                        + str(record.text)[:160]
                        + "\n"
                    )
                    reported += 1
    return count


def _direct_terminator_index(
    builder: DirectIndexedFunctionBuilder,
    records: list[_ir.InstructionRecord],
) -> int:
    if not records:
        raise BackendUnavailable("direct indexed block has no terminator")
    index = 0
    last_index = len(records) - 1
    while index < last_index:
        if (
            _record_metadata(builder, records[index]).first
            == PARSED_INSTRUCTION_KIND_UNREACHABLE
        ):
            return index
        index += 1
    return last_index


def _direct_successors(
    builder: DirectIndexedFunctionBuilder,
    function: _ir.Function,
    old_block_id: int,
    name_to_old: dict[str, int],
    terminator_index: int,
) -> list[int]:
    records = function.blocks[old_block_id]._instrs
    metadata = _record_metadata(builder, records[terminator_index])
    kind_id = metadata.first
    terminator_count = len(builder.terminator_scalars) // 8
    if metadata.second < 0 or metadata.second >= terminator_count:
        raise BackendUnavailable(
            "direct indexed final record is not a terminator: function="
            + str(function.name)
            + " block="
            + str(function.blocks[old_block_id].name)
            + " kind="
            + _record_kind(metadata)
            + " payload="
            + str(metadata.second)
            + " terminators="
            + str(terminator_count)
        )
    term: CompilerInt4 = builder.terminator_scalars.get4_unchecked(
        metadata.second * 2
    )
    span: CompilerInt4 = builder.terminator_scalars.get4_unchecked(
        metadata.second * 2 + 1
    )
    out = []
    if (
        kind_id == PARSED_INSTRUCTION_KIND_BR
        or kind_id == PARSED_INSTRUCTION_KIND_BR_COND
        or kind_id == PARSED_INSTRUCTION_KIND_SWITCH
    ):
        if term.fourth < 0 or term.fourth >= len(builder.target_names):
            raise BackendUnavailable(
                "direct indexed terminator target is out of range: "
                + str(term.fourth)
            )
        out.append(name_to_old[builder.target_names[term.fourth]])
    if kind_id == PARSED_INSTRUCTION_KIND_BR_COND:
        out.append(name_to_old[builder.target_names[span.first]])
    elif kind_id == PARSED_INSTRUCTION_KIND_SWITCH:
        case_index = 0
        while case_index < span.third:
            case: CompilerInt2 = builder.terminator_case_scalars.get2_unchecked(
                span.second + case_index
            )
            out.append(name_to_old[builder.target_names[case.second]])
            case_index += 1
    return out


def _publish_fused_instruction_uses(
    builder: DirectIndexedFunctionBuilder,
    seed: IndexedFunctionSeed,
    record: _ir.InstructionRecord,
    block_id: int,
    instruction_index: int,
    instruction_start: int,
    first_use_blocks: list[int],
    last_use_positions: list[int],
    use_crosses_blocks: list[bool],
) -> None:
    use_span_index = record._direct_record_id * 2
    use_start = builder.record_use_spans.get_unchecked(use_span_index)
    use_count = builder.record_use_spans.get_unchecked(use_span_index + 1)
    first_use_id = -1
    second_use_or_overflow = -1
    use_index = 0
    while use_index < use_count:
        value_id = builder.record_use_ids.get_unchecked(use_start + use_index)
        indexed_use_id = _record_fused_use(
            seed,
            value_id,
            block_id,
            instruction_index,
            first_use_blocks,
            last_use_positions,
            use_crosses_blocks,
        )
        if use_index == 0:
            first_use_id = indexed_use_id
        elif use_index == 1:
            second_use_or_overflow = indexed_use_id
        elif use_index == 2:
            overflow_start = len(seed.instruction_overflow_use_ids)
            seed.instruction_overflow_use_ids.append(second_use_or_overflow)
            seed.instruction_overflow_use_ids.append(indexed_use_id)
            second_use_or_overflow = -overflow_start - 2
        else:
            seed.instruction_overflow_use_ids.append(indexed_use_id)
        use_index += 1
    global_instruction_id = instruction_start + instruction_index
    seed.instruction_facts.set3_unchecked(
        global_instruction_id * 4 + 1,
        use_count,
        first_use_id,
        second_use_or_overflow,
    )
    seed.instruction_use_total += use_count


def _publish_fused_terminator_use(
    builder: DirectIndexedFunctionBuilder,
    seed: IndexedFunctionSeed,
    record: _ir.InstructionRecord,
    block_id: int,
    instruction_count: int,
    first_use_blocks: list[int],
    last_use_positions: list[int],
    use_crosses_blocks: list[bool],
) -> None:
    span_index = record._direct_record_id * 2
    use_start = builder.record_use_spans.get_unchecked(span_index)
    use_count = builder.record_use_spans.get_unchecked(span_index + 1)
    if use_count > 1:
        raise BackendUnavailable(
            "direct indexed terminator has more than one SSA use"
        )
    use_id = -1
    if use_count == 1:
        use_id = _record_fused_use(
            seed,
            builder.record_use_ids.get_unchecked(use_start),
            block_id,
            instruction_count,
            first_use_blocks,
            last_use_positions,
            use_crosses_blocks,
        )
    seed.block_facts.set2_unchecked(
        block_id * 4 + 2,
        use_count,
        use_id,
    )


def _record_fused_use(
    seed: IndexedFunctionSeed,
    value_id: int,
    block_id: int,
    position: int,
    first_use_blocks: list[int],
    last_use_positions: list[int],
    use_crosses_blocks: list[bool],
) -> int:
    seed.value_is_used_flags[value_id] = True
    seed.used_value_ids_in_order.append(value_id)
    previous_block = first_use_blocks[value_id]
    if previous_block < 0:
        first_use_blocks[value_id] = block_id
        last_use_positions[value_id] = position
    elif previous_block != block_id:
        use_crosses_blocks[value_id] = True
    elif position > last_use_positions[value_id]:
        last_use_positions[value_id] = position
    return value_id


def _finalize_structured_seed(
    function: _ir.Function,
    builder: DirectIndexedFunctionBuilder,
) -> IndexedFunctionSeed:
    seed: IndexedFunctionSeed = builder.seed
    trace = str(
        os.environ.get("PCC_DEBUG_DIRECT_INDEXED_FINALIZE", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on")
    trace_block = str(
        os.environ.get("PCC_DEBUG_DIRECT_INDEXED_FINALIZE_BLOCK", "") or ""
    )
    if trace:
        sys.stderr.write(
            "pcc direct finalize start function=" + str(function.name) + "\n"
        )
    block_names = [str(block.name) for block in function.blocks]
    terminator_indexes: list[int] = []
    pruned_target_names: list[str] = []
    for block in function.blocks:
        terminator_index = _direct_terminator_index(builder, block._instrs)
        terminator_indexes.append(terminator_index)
        metadata = _record_metadata(builder, block._instrs[terminator_index])
        pruned_target = ""
        if metadata.first == PARSED_INSTRUCTION_KIND_BR:
            term = builder.terminator_scalars.get4_unchecked(metadata.second * 2)
            span = builder.terminator_scalars.get4_unchecked(metadata.second * 2 + 1)
            # A folded cbranch keeps its discarded target in the otherwise
            # unused second-target slot. Remove only that edge's PHI input;
            # unrelated malformed PHIs must still reach the verifier.
            if span.first >= 0 and span.first != term.fourth:
                pruned_target = builder.target_names[span.first]
        pruned_target_names.append(pruned_target)
    name_to_old = {}
    for block_id, block_name in enumerate(block_names):
        name_to_old[block_name] = block_id
    reachable = [False] * len(block_names)
    pending = [0]
    while pending:
        block_id = pending.pop()
        if reachable[block_id]:
            continue
        reachable[block_id] = True
        for successor in _direct_successors(
            builder,
            function,
            block_id,
            name_to_old,
            terminator_indexes[block_id],
        ):
            if not reachable[successor]:
                pending.append(successor)
        source_target_id = builder.target_name_ids.get(block_names[block_id])
        edge_id = (
            -1
            if source_target_id is None
            else builder.error_edge_heads.get_unchecked(source_target_id)
        )
        while edge_id >= 0:
            base = edge_id * INLINE_ERROR_EDGE_WIDTH
            error_target_id = builder.error_edge_scalars.get_unchecked(base + 3)
            if error_target_id < 0 or error_target_id >= len(builder.target_names):
                raise BackendUnavailable(
                    "direct inline error edge has an invalid target name ID"
                )
            error_name = builder.target_names[error_target_id]
            if error_name not in name_to_old:
                raise BackendUnavailable(
                    "direct inline error edge targets missing block "
                    + repr(error_name)
                )
            error_old = name_to_old[error_name]
            if not reachable[error_old]:
                pending.append(error_old)
            edge_id = builder.error_edge_next.get_unchecked(edge_id)
    if trace:
        sys.stderr.write(
            "pcc direct finalize reachable function="
            + str(function.name)
            + " blocks="
            + str(len(block_names))
            + "\n"
        )
    reachable_old_ids = []
    for block_id, is_reachable in enumerate(reachable):
        if is_reachable:
            reachable_old_ids.append(block_id)
            seed.register_block(block_names[block_id])
    old_to_new = [-1] * len(block_names)
    for new_id, old_id in enumerate(reachable_old_ids):
        old_to_new[old_id] = new_id

    block_phi_counts = [0] * len(block_names)
    block_instruction_counts = [0] * len(block_names)
    for old_block_id in reachable_old_ids:
        records = function.blocks[old_block_id]._instrs
        terminator_index = terminator_indexes[old_block_id]
        phi_count = 0
        while (
            phi_count < terminator_index
            and _record_metadata(builder, records[phi_count]).first
            == _DIRECT_RECORD_KIND_PHI
        ):
            phi_count += 1
        block_phi_counts[old_block_id] = phi_count
        block_instruction_counts[old_block_id] = terminator_index - phi_count

    first_use_blocks = (
        [-1] * len(seed.value_names) if builder.fuse_uses else []
    )
    last_use_positions = (
        [-1] * len(seed.value_names) if builder.fuse_uses else []
    )
    use_crosses_blocks = (
        [False] * len(seed.value_names) if builder.fuse_uses else []
    )

    for new_block_id, old_block_id in enumerate(reachable_old_ids):
        if trace:
            sys.stderr.write(
                "pcc direct finalize publish function="
                + str(function.name)
                + " block="
                + str(function.blocks[old_block_id].name)
                + "\n"
            )
        records = function.blocks[old_block_id]._instrs
        terminator_index = terminator_indexes[old_block_id]
        instruction_start = len(seed.instruction_metadata) // 4
        instruction_count = 0
        phi_start = len(seed.phi_scalars) // 4
        phi_count = 0
        record_index = 0
        while record_index < terminator_index:
            record = records[record_index]
            metadata = _record_metadata(builder, record)
            kind_id = metadata.first
            if trace and (not trace_block or trace_block == str(function.blocks[old_block_id].name)):
                sys.stderr.write(
                    "pcc direct finalize record block="
                    + str(function.blocks[old_block_id].name)
                    + " index="
                    + str(record_index)
                    + " kind="
                    + _record_kind(metadata)
                    + " payload="
                    + str(metadata.second)
                    + " dest="
                    + str(metadata.third)
                    + "\n"
                )
            if kind_id == _DIRECT_RECORD_KIND_PHI:
                raw_phi: CompilerInt4 = builder.phi_scalars.get4_unchecked(
                    metadata.second
                )
                seed.define_value_id(
                    raw_phi.first,
                    new_block_id,
                    None,
                    -1,
                )
                incoming_start = len(seed.phi_incoming_scalars) // 2
                incoming_count = 0
                incoming_index = 0
                while incoming_index < raw_phi.fourth:
                    incoming: CompilerInt2 = builder.phi_incoming_scalars.get2_unchecked(
                        raw_phi.third + incoming_index
                    )
                    predecessor_old = name_to_old[
                        builder.target_names[incoming.second]
                    ]
                    predecessor_new = old_to_new[predecessor_old]
                    if predecessor_new >= 0 and pruned_target_names[predecessor_old] != block_names[old_block_id]:
                        seed.phi_incoming_scalars.append2(
                            incoming.first,
                            predecessor_new,
                        )
                        if builder.fuse_uses and incoming.first >= 0:
                            _record_fused_use(
                                seed,
                                incoming.first,
                                predecessor_new,
                                block_instruction_counts[predecessor_old],
                                first_use_blocks,
                                last_use_positions,
                                use_crosses_blocks,
                            )
                        incoming_count += 1
                    incoming_index += 1
                seed.phi_scalars.append4(
                    raw_phi.first,
                    raw_phi.second,
                    incoming_start,
                    incoming_count,
                )
                phi_count += 1
                record_index += 1
                continue
            if metadata.third >= 0:
                seed.define_value_id(
                    metadata.third,
                    new_block_id,
                    None,
                    instruction_count,
                )
            flags = builder.record_arithmetic_flags.get(record._direct_record_id, ())
            seed.append_instruction_kind_id(
                kind_id,
                metadata.second,
                metadata.third,
                bool(metadata.fourth & _DIRECT_RECORD_FLAG_VOLATILE),
                flags,
            )
            if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                seed.records.set_unchecked(
                    metadata.second * 8 + 6,
                    metadata.third,
                )
            if builder.fuse_uses:
                _publish_fused_instruction_uses(
                    builder,
                    seed,
                    record,
                    new_block_id,
                    instruction_count,
                    instruction_start,
                    first_use_blocks,
                    last_use_positions,
                    use_crosses_blocks,
                )
            instruction_count += 1
            record_index += 1
        seed.block_phi_facts.append2(phi_start, phi_count)
        seed.append_block_fact(
            instruction_start,
            instruction_count,
            0,
            -1,
        )

        term_metadata = _record_metadata(builder, records[terminator_index])
        if trace and (not trace_block or trace_block == str(function.blocks[old_block_id].name)):
            sys.stderr.write(
                "pcc direct finalize term block="
                + str(function.blocks[old_block_id].name)
                + " kind="
                + _record_kind(term_metadata)
                + " payload="
                + str(term_metadata.second)
                + "\n"
            )
        term_kind_id = term_metadata.first
        raw_term: CompilerInt4 = builder.terminator_scalars.get4_unchecked(
            term_metadata.second * 2
        )
        raw_span: CompilerInt4 = builder.terminator_scalars.get4_unchecked(
            term_metadata.second * 2 + 1
        )
        target0 = (
            old_to_new[name_to_old[builder.target_names[raw_term.fourth]]]
            if term_kind_id == PARSED_INSTRUCTION_KIND_BR
            or term_kind_id == PARSED_INSTRUCTION_KIND_BR_COND
            or term_kind_id == PARSED_INSTRUCTION_KIND_SWITCH
            else -1
        )
        target1 = (
            old_to_new[name_to_old[builder.target_names[raw_span.first]]]
            if term_kind_id == PARSED_INSTRUCTION_KIND_BR_COND
            else -1
        )
        case_start = len(seed.terminator_case_scalars) // 2
        case_index = 0
        while case_index < raw_span.third:
            raw_case: CompilerInt2 = builder.terminator_case_scalars.get2_unchecked(
                raw_span.second + case_index
            )
            seed.terminator_case_scalars.append2(
                raw_case.first,
                old_to_new[name_to_old[builder.target_names[raw_case.second]]],
            )
            case_index += 1
        seed.terminator_scalars.append4(
            raw_term.first,
            raw_term.second,
            raw_term.third,
            target0,
        )
        seed.terminator_scalars.append4(
            target1,
            case_start,
            raw_span.third,
            0,
        )
        if builder.fuse_uses:
            _publish_fused_terminator_use(
                builder,
                seed,
                records[terminator_index],
                new_block_id,
                instruction_count,
                first_use_blocks,
                last_use_positions,
                use_crosses_blocks,
            )

    for new_block_id, old_block_id in enumerate(reachable_old_ids):
        edge_start = len(seed.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH
        edge_count = 0
        source_name = block_names[old_block_id]
        source_target_id = builder.target_name_ids.get(source_name)
        edge_id = (
            -1
            if source_target_id is None
            else builder.error_edge_heads.get_unchecked(source_target_id)
        )
        # Edges are published in construction order, but a record inserted
        # ahead of an earlier edge's condition leaves the list out of block
        # order.  Resolve every trigger from its condition's final definition
        # position, then publish the block's edges sorted by that position so
        # the verifier's monotonic-trigger contract and the emitter's in-order
        # walk both hold.
        ordered_edge_ids: list[int] = []
        ordered_triggers: list[int] = []
        while edge_id >= 0:
            base = edge_id * INLINE_ERROR_EDGE_WIDTH
            if builder.error_edge_scalars.get_unchecked(base) != source_target_id:
                raise BackendUnavailable(
                    "direct inline error-edge source index is inconsistent"
                )
            condition_id = builder.error_edge_scalars.get_unchecked(base + 2)
            if condition_id < 0 or condition_id >= len(seed.value_names):
                raise BackendUnavailable(
                    "direct inline error edge has an invalid condition value"
                )
            trigger_instruction = seed.definition_positions[condition_id]
            if (
                seed.definition_blocks[condition_id] != new_block_id
                or trigger_instruction < 0
                or trigger_instruction >= block_instruction_counts[old_block_id]
            ):
                raise BackendUnavailable(
                    "direct inline error edge condition "
                    + repr(seed.value_names[condition_id])
                    + " is not defined by an instruction in source block "
                    + repr(source_name)
                )
            insert_at = len(ordered_triggers)
            while (
                insert_at > 0
                and ordered_triggers[insert_at - 1] > trigger_instruction
            ):
                insert_at -= 1
            ordered_edge_ids.insert(insert_at, edge_id)
            ordered_triggers.insert(insert_at, trigger_instruction)
            edge_id = builder.error_edge_next.get_unchecked(edge_id)
        ordered_index = 0
        while ordered_index < len(ordered_edge_ids):
            edge_id = ordered_edge_ids[ordered_index]
            trigger_instruction = ordered_triggers[ordered_index]
            ordered_index += 1
            base = edge_id * INLINE_ERROR_EDGE_WIDTH
            condition_id = builder.error_edge_scalars.get_unchecked(base + 2)
            error_target_id = builder.error_edge_scalars.get_unchecked(base + 3)
            if error_target_id < 0 or error_target_id >= len(builder.target_names):
                raise BackendUnavailable(
                    "direct inline error edge has an invalid target name ID"
                )
            error_name = builder.target_names[error_target_id]
            if error_name not in name_to_old:
                raise BackendUnavailable(
                    "direct inline error edge targets missing block "
                    + repr(error_name)
                )
            error_old = name_to_old[error_name]
            error_new = old_to_new[error_old]
            if error_new < 0:
                raise BackendUnavailable(
                    "direct inline error edge targets an unreachable block"
                )
            seed.error_edge_scalars.append4(
                new_block_id,
                trigger_instruction,
                condition_id,
                error_new,
            )
            seed.error_edge_scalars.append4(
                builder.error_edge_scalars.get_unchecked(base + 4),
                builder.error_edge_scalars.get_unchecked(base + 5),
                builder.error_edge_scalars.get_unchecked(base + 6),
                0,
            )
            if builder.fuse_uses:
                _record_fused_use(
                    seed,
                    condition_id,
                    new_block_id,
                    trigger_instruction + 1,
                    first_use_blocks,
                    last_use_positions,
                    use_crosses_blocks,
                )
                seed.instruction_use_total += 1
            edge_count += 1
        seed.error_edge_spans.append2(edge_start, edge_count)

    landing_index = 0
    landing_count = len(builder.error_landing_scalars) // 2
    while landing_index < landing_count:
        landing: CompilerInt2 = builder.error_landing_scalars.get2_unchecked(
            landing_index
        )
        landing_index += 1
        landing_name = builder.target_names[landing.first]
        if landing_name not in name_to_old:
            raise BackendUnavailable(
                "direct inline error landing names missing block "
                + repr(landing_name)
            )
        landing_new = old_to_new[name_to_old[landing_name]]
        if landing_new < 0:
            continue
        if landing.second < 0 or landing.second >= len(seed.value_names):
            raise BackendUnavailable(
                "direct inline error landing has an invalid slot value"
            )
        seed.error_landing_scalars.append2(landing_new, landing.second)

    seed.phi_records_complete = True
    seed.terminator_records_complete = True
    if builder.fuse_uses:
        seed.value_last_use_positions = [-1] * len(seed.value_names)
        value_id = 0
        while value_id < len(seed.value_names):
            use_block = first_use_blocks[value_id]
            if (
                use_block >= 0
                and not use_crosses_blocks[value_id]
                and seed.definition_blocks[value_id] == use_block
            ):
                seed.value_last_use_positions[value_id] = last_use_positions[
                    value_id
                ]
            value_id += 1
        seed.use_records_complete = True
    instruction_id = 0
    while instruction_id * 4 < len(seed.instruction_metadata):
        metadata: CompilerInt4 = seed.instruction_metadata.get4_unchecked(
            instruction_id
        )
        if (
            metadata.first not in _DIRECT_FIXED_PAYLOAD_KIND_IDS
            and metadata.second >= 0
        ):
            raise BackendUnavailable(
                "direct indexed non-fixed instruction lacks cold payload: id="
                + str(instruction_id)
                + " kind="
                + PARSED_INSTRUCTION_KINDS[metadata.first]
                + " payload="
                + str(metadata.second)
            )
        instruction_id += 1
    seed.finish()
    if trace:
        sys.stderr.write(
            "pcc direct finalize finish function=" + str(function.name) + "\n"
        )
    return seed


def publish_exact_call_fixed(
    builder: DirectIndexedFunctionBuilder,
    dest_ref: _ir.Value | None,
    function: _ir.Function,
    arg_count: int,
    arg0=None,
    arg1=None,
) -> int:
    """Publish an exact arity-0/1/2 call without growing the builder class ABI."""
    if arg_count < 0 or arg_count > 2:
        raise BackendUnavailable("exact fixed call arity is outside 0..2")
    function_type = function.ftype
    fixed_arg_count = len(function_type.args)
    seed = builder.seed
    arg_start = seed.args._length // 4
    use_start = builder.record_use_ids._length
    use_count = 0
    if arg_count > 0:
        arg0_type = (
            function_type.args[0] if fixed_arg_count > 0 else arg0.type
        )
        arg0_ref = DirectIndexedFunctionBuilder._operand_value_ref(builder, arg0)
        seed.args.append4(
            DirectIndexedFunctionBuilder._type_id(builder, arg0_type),
            arg0_ref if arg0_ref >= 0 else -1,
            -arg0_ref - 1 if arg0_ref < 0 else -1,
            0,
        )
        if builder.fuse_uses and arg0_ref >= 0:
            builder.record_use_ids.append(arg0_ref)
            use_count += 1
    if arg_count > 1:
        arg1_type = (
            function_type.args[1] if fixed_arg_count > 1 else arg1.type
        )
        arg1_ref = DirectIndexedFunctionBuilder._operand_value_ref(builder, arg1)
        seed.args.append4(
            DirectIndexedFunctionBuilder._type_id(builder, arg1_type),
            arg1_ref if arg1_ref >= 0 else -1,
            -arg1_ref - 1 if arg1_ref < 0 else -1,
            0,
        )
        if builder.fuse_uses and arg1_ref >= 0:
            builder.record_use_ids.append(arg1_ref)
            use_count += 1
    callee_ref = "@" + str(function.name)
    _decoded_callee, callee_text_id, call_flags = (
        DirectIndexedFunctionBuilder._callee_record(
            builder,
            callee_ref,
            False,
            bool(function_type.var_arg),
        )
    )
    dest_id = (
        -1
        if dest_ref is None
        else DirectIndexedFunctionBuilder._dest_value_id(builder, dest_ref)
    )
    ret_type_id = DirectIndexedFunctionBuilder._type_id(
        builder,
        function_type.return_type,
    )
    call_id = seed.records._length // 8
    seed.records.append4(
        ret_type_id,
        callee_text_id,
        call_flags,
        arg_start,
    )
    seed.records.append4(
        arg_count,
        fixed_arg_count,
        dest_id,
        0,
    )
    if dest_id >= 0:
        seed.publish_value_type_id(dest_id, ret_type_id)
    return DirectIndexedFunctionBuilder._append_metadata(
        builder,
        "call",
        call_id,
        dest_id,
        use_start=use_start,
        use_count=use_count,
    )


def build_direct_indexed_function(function: _ir.Function) -> ParsedFunction:
    """Build one final-kernel function without module/function/block text scan."""
    cached = function._direct_indexed_function_cache
    if cached is not None:
        return cached
    if not function.blocks:
        raise ValueError("direct indexed function requires a definition")

    args: list[ArgInfo] = []
    index = 0
    while index < len(function.args):
        argument = function.args[index]
        args.append(
            ArgInfo(
                decode_ssa_name(argument._ref),
                parse_ir_type(str(argument.type)),
            )
        )
        index += 1

    builder = function._direct_indexed_builder
    fallback_count = _function_fallback_record_count(function)
    if builder is not None:
        builder.fallback_records = fallback_count
    if builder is not None and fallback_count == 0:
        seed = _finalize_structured_seed(function, builder)
    else:
        block_names: list[str] = []
        block_lines: list[list[str]] = []
        index = 0
        while index < len(function.blocks):
            block = function.blocks[index]
            block_names.append(str(block.name))
            lines: list[str] = []
            line_index = 0
            while line_index < len(block._text_lines):
                lines.append(block._text_lines[line_index])
                line_index += 1
            block_lines.append(lines)
            index += 1
        seed = build_indexed_function_seed_from_block_lines(
            str(function.name),
            args,
            block_names,
            block_lines,
        )
    parsed = ParsedFunction(
        name=str(function.name),
        ret_type=parse_ir_type(str(function.ftype.return_type)),
        args=args,
        is_global=str(function.linkage or "") != "internal",
        is_vararg=bool(function.ftype.var_arg),
        blocks=[],
        value_types={},
        value_slots={},
        value_slot_buckets={},
        alloca_slots={},
        alloca_slot_buckets={},
        block_map={},
        used_values=[],
        value_registers={},
        aarch64_madd_fusions=[],
        aarch64_block_layout=[],
        aarch64_cold_fallthrough_edges=[],
        hidden_sret_slot=None,
        frame_size=0,
        indexed_kernel=None,
        indexed_seed=seed,
        indexed_slot_projection=False,
    )
    try:
        get_indexed_function_kernel(parsed)
    except Exception as exc:
        raise BackendUnavailable(
            "direct indexed kernel adoption failed for function "
            + str(function.name)
            + ": "
            + type(exc).__name__
            + ": "
            + (str(exc) or type(exc).__name__)
        ) from exc
    if builder is not None:
        builder.release_construction_state()
    function._direct_indexed_function_cache = parsed
    return parsed


def build_direct_indexed_module(module: _ir.Module) -> ParsedModule:
    """Build direct functions while retaining globals/types as text oracle.

    Module/function/block topology and every function body bypass the text
    scanner.  Named type declarations and global initializers remain the
    explicitly counted transitional projection in this slice.
    """
    prelude: list[str] = [
        'target triple = "' + str(module.triple) + '"',
        'target datalayout = "' + str(module.data_layout) + '"',
        "",
    ]
    identified = list(module.context.identified_types.values())
    index = 0
    while index < len(identified):
        prelude.append(identified[index].get_declaration())
        index += 1
    if identified:
        prelude.append("")
    index = 0
    while index < len(module._globals):
        prelude.append(module._globals[index].render())
        index += 1
    skeleton = "\n".join(prelude)
    parsed_skeleton = parse_self_backend_module(skeleton)

    functions: list[ParsedFunction] = []
    module._direct_indexed_supported_records = 0
    module._direct_indexed_fallback_records = 0
    index = 0
    while index < len(module._functions):
        function = module._functions[index]
        if function.blocks:
            try:
                functions.append(build_direct_indexed_function(function))
            except Exception as exc:
                raise BackendUnavailable(
                    "direct indexed finalize failed for function "
                    + str(function.name)
                    + ": "
                    + type(exc).__name__
                    + ": "
                    + (str(exc) or type(exc).__name__)
                ) from exc
            builder = function._direct_indexed_builder
            if builder is None:
                for block in function.blocks:
                    module._direct_indexed_fallback_records += len(block._instrs)
            else:
                module._direct_indexed_supported_records += (
                    builder.supported_records
                )
                module._direct_indexed_fallback_records += (
                    builder.fallback_records
                )
        index += 1
    return ParsedModule(
        triple=parsed_skeleton.triple,
        globals_=parsed_skeleton.globals_,
        functions=tuple(functions),
    )


def direct_indexed_module_first_libpython_edge(module: ParsedModule) -> str:
    """Describe the first reachable structured ``py_cpy_*`` call edge."""
    for function in module.functions:
        kernel = get_indexed_function_kernel(function)
        instruction_id = 0
        instruction_count = len(kernel.instruction_metadata) // 4
        while instruction_id < instruction_count:
            metadata = kernel.instruction_metadata_by_id(instruction_id)
            if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                header = kernel.call_header(metadata.second)
                callee = kernel.call_texts[header.second]
                if callee.startswith("py_cpy_"):
                    return str(function.name) + " -> " + callee
            instruction_id += 1
    return ""


def direct_indexed_module_needs_libpython(module: ParsedModule) -> bool:
    """Read the structured call plane for a direct ``py_cpy_*`` edge."""
    return bool(direct_indexed_module_first_libpython_edge(module))


def direct_indexed_module_cfg_stats(module: ParsedModule) -> dict[str, int]:
    """Size the final CFG of a direct module for representation receipts.

    Counts functions, blocks, instructions and inline error edges, plus the
    three generated post-call block families the inline plane replaces
    (``call.cont``, ``call.err.cleanup``, ``err.frame``) by block-name prefix.
    """
    functions = 0
    blocks = 0
    instructions = 0
    inline_error_edges = 0
    call_cont = 0
    call_err_cleanup = 0
    err_frame = 0
    for function in module.functions:
        kernel = get_indexed_function_kernel(function)
        functions += 1
        blocks += len(kernel.block_names)
        instructions += len(kernel.instruction_metadata) // 4
        inline_error_edges += (
            len(kernel.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH
        )
        for name in kernel.block_names:
            if name.startswith("call.cont"):
                call_cont += 1
            elif name.startswith("call.err.cleanup"):
                call_err_cleanup += 1
            elif name.startswith("err.frame"):
                err_frame += 1
    return {
        "functions": functions,
        "blocks": blocks,
        "instructions": instructions,
        "inline_error_edges": inline_error_edges,
        "call_cont_blocks": call_cont,
        "call_err_cleanup_blocks": call_err_cleanup,
        "err_frame_blocks": err_frame,
    }


def direct_indexed_module_cfg_stats_text(stats: dict[str, int]) -> str:
    """Render ``direct_indexed_module_cfg_stats`` as one ``key=value`` line.

    Keys are spelled out in a fixed order: iterating or sorting the dict
    would need a libpython fallback inside the compiled stage1 worker.
    """
    return (
        "blocks=" + str(stats["blocks"])
        + " call_cont_blocks=" + str(stats["call_cont_blocks"])
        + " call_err_cleanup_blocks=" + str(stats["call_err_cleanup_blocks"])
        + " err_frame_blocks=" + str(stats["err_frame_blocks"])
        + " functions=" + str(stats["functions"])
        + " inline_error_edges=" + str(stats["inline_error_edges"])
        + " instructions=" + str(stats["instructions"])
    )


# See ir.py's matching tail import.  Direct-plane definitions are complete
# before importing ir, so both ``ir -> direct`` and ``direct -> ir`` orders are
# safe; postponed annotations do not read _ir during class/function creation.
from . import ir as _ir


__all__ = [
    "DirectIndexedFunctionBuilder",
    "publish_exact_call_fixed",
    "build_direct_indexed_function",
    "build_direct_indexed_module",
    "direct_indexed_module_cfg_stats",
    "direct_indexed_module_cfg_stats_text",
    "direct_indexed_module_first_libpython_edge",
    "direct_indexed_module_needs_libpython",
]
