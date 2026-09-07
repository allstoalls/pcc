from __future__ import annotations

"""Versioned process-boundary codec for direct indexed self-backend modules."""

import json
import os
import struct
from typing import cast

from .self_backend_ir import ArgInfo, GlobalDef, ParsedFunction, ParsedModule, TypeDesc
from .self_backend_kernel import (
    IndexedFunctionSeed,
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from .self_backend_value_arena import CompilerIntArena


# Corrupt sidecars are an input/value contract failure local to this codec.
# Keeping the builtin exception local prevents a cross-module exception-class
# lookup from replacing every strict no-libpython codec function with a stub.
BackendUnavailable = ValueError


def _wire_str(value, name: str) -> str:
    if not isinstance(value, str):
        raise BackendUnavailable("indexed module has invalid " + name)
    return value


def _wire_int(value, name: str) -> int:
    if not isinstance(value, int):
        raise BackendUnavailable("indexed module has invalid " + name)
    return value


def _wire_bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise BackendUnavailable("indexed module has invalid " + name)
    return value


_SCHEMA = "pcc.self-backend.indexed-module.v1"
_MAGIC = b"PCCIDXMOD1\n"
_MAX_HEADER_BYTES = 512 * 1024 * 1024

# (wire name, final-kernel field, reconstructed-seed field).  The order is
# part of v1 and is the raw payload order after the JSON header.
_ARENA_FIELDS = (
    ("call_records", "call_scalars", "records"),
    ("call_args", "call_arg_scalars", "args"),
    ("block_facts", "block_facts", "block_facts"),
    ("instruction_facts", "instruction_facts", "instruction_facts"),
    ("instruction_kinds", "instruction_kind_ids", "instruction_kind_ids"),
    ("instruction_metadata", "instruction_metadata", "instruction_metadata"),
    (
        "instruction_record_dests",
        "instruction_record_dest_ids",
        "instruction_record_dest_ids",
    ),
    (
        "instruction_record_scalars",
        "instruction_record_scalars",
        "instruction_record_scalars",
    ),
    ("gep_indices", "gep_index_scalars", "gep_index_scalars"),
    ("gep_scalars", "gep_scalars", "gep_scalars"),
    (
        "instruction_overflow_uses",
        "instruction_overflow_use_ids",
        "instruction_overflow_use_ids",
    ),
    ("terminator_cases", "terminator_case_scalars", "terminator_case_scalars"),
    ("terminators", "terminator_scalars", "terminator_scalars"),
    ("block_phi_facts", "block_phi_facts", "block_phi_facts"),
    ("phi_incoming", "phi_incoming_scalars", "phi_incoming_scalars"),
    ("phi_scalars", "phi_scalars", "phi_scalars"),
    ("error_edges", "error_edge_scalars", "error_edge_scalars"),
    ("error_edge_spans", "error_edge_spans", "error_edge_spans"),
    ("error_landings", "error_landing_scalars", "error_landing_scalars"),
    ("value_scalars", "value_scalars", ""),
    ("definition_positions", "definition_positions", ""),
    ("used_value_ids", "used_value_ids", ""),
)


def _type_to_wire(value: TypeDesc | None):
    if value is None:
        return None
    return [
        value.kind,
        value.width,
        _type_to_wire(value.pointee),
        value.count,
        _type_to_wire(value.elem),
        value.name,
        [_type_to_wire(field) for field in value.fields],
    ]


def _type_from_wire(value) -> TypeDesc | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 7:
        raise BackendUnavailable("indexed module has an invalid type record")
    fields = value[6]
    if not isinstance(fields, list):
        raise BackendUnavailable("indexed module has invalid type fields")
    pointee = _type_from_wire(value[2])
    elem = _type_from_wire(value[4])
    return TypeDesc(
        kind=_wire_str(value[0], "type kind"),
        width=_wire_int(value[1], "type width"),
        pointee=pointee,
        count=_wire_int(value[3], "type count"),
        elem=elem,
        name=_wire_str(value[5], "type name"),
        fields=tuple(_type_from_wire(field) for field in fields),
    )


def _cold_to_wire(value):
    if isinstance(value, TypeDesc):
        return {"t": _type_to_wire(value)}
    if isinstance(value, tuple):
        return {"q": [_cold_to_wire(item) for item in value]}
    if isinstance(value, list):
        return {"l": [_cold_to_wire(item) for item in value]}
    if isinstance(value, dict):
        return {
            "d": [
                [_cold_to_wire(key), _cold_to_wire(item)]
                for key, item in value.items()
            ]
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise BackendUnavailable(
        "indexed module cold payload is not serializable: "
        + type(value).__name__
    )


def _cold_from_wire(value):
    if not isinstance(value, dict):
        return value
    if len(value) != 1:
        raise BackendUnavailable("indexed module has an invalid cold payload")
    if "t" in value:
        result = _type_from_wire(value["t"])
        if result is None:
            raise BackendUnavailable("indexed module cold type is null")
        return result
    if "q" in value:
        items = value["q"]
        if not isinstance(items, list):
            raise BackendUnavailable("indexed module cold tuple is invalid")
        return tuple(_cold_from_wire(item) for item in items)
    if "l" in value:
        items = value["l"]
        if not isinstance(items, list):
            raise BackendUnavailable("indexed module cold list is invalid")
        return [_cold_from_wire(item) for item in items]
    if "d" in value:
        rows = value["d"]
        if not isinstance(rows, list):
            raise BackendUnavailable("indexed module cold dict is invalid")
        out = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise BackendUnavailable("indexed module cold dict row is invalid")
            out[_cold_from_wire(row[0])] = _cold_from_wire(row[1])
        return out
    raise BackendUnavailable("indexed module cold payload tag is invalid")


def _global_to_wire(value: GlobalDef):
    return [
        value.name,
        _type_to_wire(value.type),
        value.initializer,
        value.is_constant,
        value.is_internal,
        value.tls_model,
        value.alignment,
        value.ir_prefix,
        [item for item in value.trailing_attributes],
    ]


def _global_from_wire(value) -> GlobalDef:
    if not isinstance(value, list) or len(value) != 9:
        raise BackendUnavailable("indexed module has an invalid global record")
    value_type = _type_from_wire(value[1])
    if value_type is None:
        raise BackendUnavailable("indexed module global type is null")
    trailing = value[8]
    if not isinstance(trailing, list):
        raise BackendUnavailable("indexed module global attributes are invalid")
    return GlobalDef(
        name=_wire_str(value[0], "global name"),
        type=value_type,
        initializer=_wire_str(value[2], "global initializer"),
        is_constant=_wire_bool(value[3], "global constant flag"),
        is_internal=_wire_bool(value[4], "global internal flag"),
        tls_model=_wire_str(value[5], "global TLS model"),
        alignment=_wire_int(value[6], "global alignment"),
        ir_prefix=_wire_str(value[7], "global IR prefix"),
        trailing_attributes=tuple(
            _wire_str(item, "global trailing attribute") for item in trailing
        ),
    )


def _kernel_arenas(
    kernel: IndexedFunctionKernel,
) -> tuple[tuple[str, CompilerIntArena], ...]:
    out: list[tuple[str, CompilerIntArena]] = []
    for wire_name, kernel_field, _seed_field in _ARENA_FIELDS:
        arena = cast(CompilerIntArena, getattr(kernel, kernel_field))
        if not isinstance(arena, CompilerIntArena):
            raise BackendUnavailable(
                "indexed module kernel arena is invalid: " + wire_name
            )
        out.append((wire_name, arena))
    return tuple(out)


def _function_to_wire(function: ParsedFunction, kernel: IndexedFunctionKernel):
    value_count = len(kernel.value_names)
    if len(kernel.value_scalars) != value_count * 8:
        raise BackendUnavailable(
            "indexed module requires complete value scalar columns"
        )
    if len(kernel.definition_positions) != value_count:
        raise BackendUnavailable(
            "indexed module requires complete definition positions"
        )
    if len(kernel.slot_scalars) != 0 or len(kernel.block_layout_ids) != 0:
        raise BackendUnavailable(
            "indexed module sidecar must be written before stack preparation"
        )
    value_id = 0
    while value_id < value_count:
        if (
            kernel.value_scalars.get_unchecked(value_id * 8 + 2) != -1
            or kernel.value_scalars.get_unchecked(value_id * 8 + 3) != -1
            or kernel.value_scalars.get_unchecked(value_id * 8 + 5) != -1
        ):
            raise BackendUnavailable(
                "indexed module sidecar contains prepared value state"
            )
        value_id += 1
    arithmetic_flags = []
    for instruction_id, flags in kernel.instruction_arithmetic_flag_values.items():
        arithmetic_flags.append(
            [instruction_id, [flag for flag in flags]]
        )
    arena_lengths = []
    for wire_name, arena in _kernel_arenas(kernel):
        arena_lengths.append([wire_name, len(arena)])
    return {
        "name": function.name,
        "ret_type": _type_to_wire(function.ret_type),
        "args": [
            [arg.name, _type_to_wire(arg.type)] for arg in function.args
        ],
        "is_global": function.is_global,
        "is_vararg": function.is_vararg,
        "block_names": [name for name in kernel.block_names],
        "value_names": [name for name in kernel.value_names],
        "first_duplicate_definition_value_id": (
            kernel.first_duplicate_definition_value_id
        ),
        "instruction_use_total": kernel.instruction_use_total,
        "instruction_arithmetic_flags": arithmetic_flags,
        "cold_instruction_data": [
            _cold_to_wire(item) for item in kernel.cold_instruction_data
        ],
        "call_texts": [text for text in kernel.call_texts],
        "types": [_type_to_wire(item) for item in kernel.types],
        "arenas": arena_lengths,
    }


def _write_host_arena(stream, arena: CompilerIntArena) -> None:
    chunks = []
    index = 0
    while index < len(arena):
        chunks.append(struct.pack("<q", arena.get_unchecked(index)))
        index += 1
    stream.write(b"".join(chunks))


def encode_indexed_module_file(path: str, module: ParsedModule) -> None:
    """Write one direct module with raw scalar arenas and a JSON cold header."""

    functions: list[dict] = []
    raw_arenas: list[CompilerIntArena] = []
    total_scalars = 0
    module_functions: tuple[ParsedFunction, ...] = module.functions
    for function_value in module_functions:
        function: ParsedFunction = cast(ParsedFunction, function_value)
        # This is a process-boundary codec for an already-published direct
        # module, not another construction path.  Re-entering the lazy kernel
        # builder here both obscures the boundary and makes the pcc1 closure
        # project the cross-module call through py_cpy.  Require the producer's
        # exact final kernel instead.
        kernel_value = function.indexed_kernel
        if not isinstance(kernel_value, IndexedFunctionKernel):
            raise BackendUnavailable(
                "indexed module function has no published indexed kernel"
            )
        kernel: IndexedFunctionKernel = kernel_value
        functions.append(_function_to_wire(function, kernel))
        for _wire_name, arena in _kernel_arenas(kernel):
            raw_arenas.append(arena)
            total_scalars += len(arena)
    header = {
        "schema": _SCHEMA,
        "scalar_width": 8,
        "byte_order": "little",
        "triple": module.triple,
        "globals": [_global_to_wire(item) for item in module.globals_],
        "functions": functions,
        "total_scalars": total_scalars,
    }
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    if len(header_bytes) > _MAX_HEADER_BYTES:
        raise BackendUnavailable("indexed module header exceeds the size limit")
    with open(path, "wb") as stream:
        stream.write(_MAGIC)
        # Decimal digits plus newline have identical UTF-8 and ASCII bytes;
        # UTF-8 is the codec implemented by the strict pcc1 stdlib surface.
        stream.write((str(len(header_bytes)) + "\n").encode("utf-8"))
        stream.write(header_bytes)
        if not raw_arenas:
            return
        native = raw_arenas[0].uses_native_storage
        if native:
            stream.flush()
            fd = int(stream.fileno())
            for arena in raw_arenas:
                if not arena.uses_native_storage:
                    raise BackendUnavailable(
                        "indexed module mixes arena storage projections"
                    )
                arena.write_raw_fd(fd)
        else:
            for arena in raw_arenas:
                if arena.uses_native_storage:
                    raise BackendUnavailable(
                        "indexed module mixes arena storage projections"
                    )
                _write_host_arena(stream, arena)


def _read_host_arena(stream, count: int) -> CompilerIntArena:
    raw = stream.read(count * 8)
    if len(raw) != count * 8:
        raise BackendUnavailable("indexed module arena payload is truncated")
    arena = CompilerIntArena(count)
    index = 0
    while index < count:
        arena.append(struct.unpack_from("<q", raw, index * 8)[0])
        index += 1
    return arena


def _read_native_arena(fd: int, count: int) -> CompilerIntArena:
    arena = CompilerIntArena(count)
    arena.read_raw_fd(fd, count)
    return arena


def _replace_seed_arena(
    seed: IndexedFunctionSeed,
    seed_field: str,
    arena: CompilerIntArena,
) -> None:
    previous = getattr(seed, seed_field)
    if isinstance(previous, CompilerIntArena):
        previous.close()
    setattr(seed, seed_field, arena)


def _seed_from_wire(value) -> tuple[ParsedFunction, tuple[tuple[str, int], ...]]:
    if not isinstance(value, dict):
        raise BackendUnavailable("indexed module has an invalid function record")
    block_names = value.get("block_names")
    value_names = value.get("value_names")
    if not isinstance(block_names, list) or not isinstance(value_names, list):
        raise BackendUnavailable("indexed module function names are invalid")
    seed = IndexedFunctionSeed(len(block_names), len(value_names))
    for name in block_names:
        seed.register_block(_wire_str(name, "block name"))
    for name in value_names:
        seed._append_value_columns(_wire_str(name, "value name"))
    seed._ensure_value_name_index()
    seed.first_duplicate_definition_value_id = _wire_int(
        value.get("first_duplicate_definition_value_id", -1),
        "duplicate definition ID",
    )
    seed.instruction_use_total = _wire_int(
        value.get("instruction_use_total", 0), "instruction use total"
    )
    seed.instruction_arithmetic_flag_values = {}
    arithmetic_rows = value.get("instruction_arithmetic_flags")
    if not isinstance(arithmetic_rows, list):
        raise BackendUnavailable("indexed module arithmetic flags are invalid")
    for row in arithmetic_rows:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], list):
            raise BackendUnavailable("indexed module arithmetic flag row is invalid")
        instruction_id = _wire_int(row[0], "arithmetic instruction ID")
        seed.instruction_arithmetic_flag_values[instruction_id] = tuple(
            _wire_str(item, "arithmetic flag") for item in row[1]
        )
    cold = value.get("cold_instruction_data")
    if not isinstance(cold, list):
        raise BackendUnavailable("indexed module cold data is invalid")
    seed.cold_instruction_data = [_cold_from_wire(item) for item in cold]
    call_texts = value.get("call_texts")
    type_rows = value.get("types")
    if not isinstance(call_texts, list) or not isinstance(type_rows, list):
        raise BackendUnavailable("indexed module call tables are invalid")
    seed.texts = [_wire_str(item, "call text") for item in call_texts]
    seed.text_identity_ids = {
        id(item): index for index, item in enumerate(seed.texts)
    }
    seed.types = []
    for row in type_rows:
        item = _type_from_wire(row)
        if item is None:
            raise BackendUnavailable("indexed module type table contains null")
        seed.types.append(item)
    seed.type_identity_ids = {
        id(item): (index, item) for index, item in enumerate(seed.types)
    }
    seed.terminator_records_complete = True
    seed.phi_records_complete = True
    seed.use_records_complete = True
    seed.complete = True

    args_value = value.get("args")
    if not isinstance(args_value, list):
        raise BackendUnavailable("indexed module function args are invalid")
    args = []
    for row in args_value:
        if not isinstance(row, list) or len(row) != 2:
            raise BackendUnavailable("indexed module function arg is invalid")
        arg_type = _type_from_wire(row[1])
        if arg_type is None:
            raise BackendUnavailable("indexed module function arg type is null")
        args.append(ArgInfo(_wire_str(row[0], "argument name"), arg_type))
    ret_type = _type_from_wire(value.get("ret_type"))
    if ret_type is None:
        raise BackendUnavailable("indexed module function return type is null")
    arena_rows = value.get("arenas")
    if not isinstance(arena_rows, list) or len(arena_rows) != len(_ARENA_FIELDS):
        raise BackendUnavailable("indexed module arena inventory is invalid")
    arena_inventory = []
    index = 0
    while index < len(_ARENA_FIELDS):
        row = arena_rows[index]
        expected_name = _ARENA_FIELDS[index][0]
        if (
            not isinstance(row, list)
            or len(row) != 2
            or row[0] != expected_name
            or not isinstance(row[1], int)
            or row[1] < 0
        ):
            raise BackendUnavailable("indexed module arena order is invalid")
        arena_inventory.append((expected_name, row[1]))
        index += 1
    function = ParsedFunction(
        name=_wire_str(value.get("name", ""), "function name"),
        ret_type=ret_type,
        args=args,
        is_global=_wire_bool(value.get("is_global", False), "global flag"),
        is_vararg=_wire_bool(value.get("is_vararg", False), "vararg flag"),
        blocks=[],
        value_types={},
        value_slots={},
        value_slot_buckets={},
        alloca_slots={},
        alloca_slot_buckets={},
        block_map={},
        used_values=[],
        block_local_last_uses=None,
        value_registers={},
        aarch64_madd_fusions=[],
        aarch64_block_layout=[],
        aarch64_cold_fallthrough_edges=[],
        hidden_sret_slot=None,
        frame_size=0,
        indexed_kernel=None,
        indexed_seed=seed,
        indexed_slot_projection=False,
        aarch64_tail_call_ids=[],
    )
    return function, tuple(arena_inventory)


def decode_indexed_module_file(path: str) -> ParsedModule:
    """Read and fully validate one v1 direct indexed module sidecar."""

    with open(path, "rb") as stream:
        if stream.readline() != _MAGIC:
            raise BackendUnavailable("invalid indexed module file magic")
        size_line = stream.readline()
        try:
            header_size = int(size_line.decode("utf-8"))
        except Exception as exc:
            raise BackendUnavailable("invalid indexed module header size") from exc
        if header_size < 0 or header_size > _MAX_HEADER_BYTES:
            raise BackendUnavailable("indexed module header exceeds the size limit")
        header_raw = stream.read(header_size)
        if len(header_raw) != header_size:
            raise BackendUnavailable("indexed module header is truncated")
        try:
            header = json.loads(header_raw.decode("utf-8"))
        except Exception as exc:
            raise BackendUnavailable("indexed module header is invalid") from exc
        if (
            not isinstance(header, dict)
            or header.get("schema") != _SCHEMA
            or header.get("scalar_width") != 8
            or header.get("byte_order") != "little"
        ):
            raise BackendUnavailable("indexed module header contract mismatch")
        function_rows = header.get("functions")
        global_rows = header.get("globals")
        if not isinstance(function_rows, list) or not isinstance(global_rows, list):
            raise BackendUnavailable("indexed module header tables are invalid")
        functions = []
        inventories = []
        total_scalars = 0
        for row in function_rows:
            function, inventory = _seed_from_wire(row)
            functions.append(function)
            inventories.append(inventory)
            for _name, count in inventory:
                total_scalars += count
        header_total_scalars = _wire_int(
            header.get("total_scalars", -1), "total scalar count"
        )
        if total_scalars != header_total_scalars:
            raise BackendUnavailable("indexed module scalar total is inconsistent")
        payload_start = int(stream.tell())
        expected_size = payload_start + total_scalars * 8
        if os.path.getsize(path) != expected_size:
            raise BackendUnavailable("indexed module file size is inconsistent")
        probe = CompilerIntArena(0)
        native = probe.uses_native_storage
        probe.close()
        fd = -1
        if native and total_scalars:
            stream.seek(payload_start)
            fd = int(stream.fileno())
        function_index = 0
        while function_index < len(functions):
            function = functions[function_index]
            seed = function.indexed_seed
            if not isinstance(seed, IndexedFunctionSeed):
                raise BackendUnavailable("indexed module seed construction failed")
            inventory = inventories[function_index]
            temporary_arenas = {}
            arena_index = 0
            while arena_index < len(_ARENA_FIELDS):
                wire_name, _kernel_field, seed_field = _ARENA_FIELDS[arena_index]
                count = inventory[arena_index][1]
                arena = (
                    _read_native_arena(fd, count)
                    if native
                    else _read_host_arena(stream, count)
                )
                if seed_field:
                    _replace_seed_arena(seed, seed_field, arena)
                else:
                    temporary_arenas[wire_name] = arena
                arena_index += 1
            value_scalars = temporary_arenas.get("value_scalars")
            definition_positions = temporary_arenas.get("definition_positions")
            used_value_ids = temporary_arenas.get("used_value_ids")
            value_count = len(seed.value_names)
            if (
                not isinstance(value_scalars, CompilerIntArena)
                or not isinstance(definition_positions, CompilerIntArena)
                or not isinstance(used_value_ids, CompilerIntArena)
                or len(value_scalars) != value_count * 8
                or len(definition_positions) != value_count
            ):
                raise BackendUnavailable(
                    "indexed module value scalar columns are inconsistent"
                )
            seed.definition_blocks = []
            seed.definition_positions = []
            seed.value_type_ids = []
            seed.alloca_type_ids = []
            seed.value_is_used_flags = []
            seed.value_last_use_positions = []
            value_id = 0
            while value_id < value_count:
                seed.definition_blocks.append(
                    value_scalars.get_unchecked(value_id * 8)
                )
                seed.value_type_ids.append(
                    value_scalars.get_unchecked(value_id * 8 + 1)
                )
                if (
                    value_scalars.get_unchecked(value_id * 8 + 2) != -1
                    or value_scalars.get_unchecked(value_id * 8 + 3) != -1
                    or value_scalars.get_unchecked(value_id * 8 + 5) != -1
                ):
                    raise BackendUnavailable(
                        "indexed module value state is already prepared"
                    )
                seed.alloca_type_ids.append(
                    value_scalars.get_unchecked(value_id * 8 + 4)
                )
                seed.value_last_use_positions.append(
                    value_scalars.get_unchecked(value_id * 8 + 6)
                )
                seed.value_is_used_flags.append(
                    bool(value_scalars.get_unchecked(value_id * 8 + 7))
                )
                seed.definition_positions.append(
                    definition_positions.get_unchecked(value_id)
                )
                value_id += 1
            seed.used_value_ids_in_order = []
            used_index = 0
            while used_index < len(used_value_ids):
                seed.used_value_ids_in_order.append(
                    used_value_ids.get_unchecked(used_index)
                )
                used_index += 1
            get_indexed_function_kernel(function)
            value_scalars.close()
            definition_positions.close()
            used_value_ids.close()
            function_index += 1
    globals_ = tuple(_global_from_wire(row) for row in global_rows)
    return ParsedModule(
        triple=_wire_str(header.get("triple", ""), "target triple"),
        globals_=globals_,
        functions=tuple(functions),
    )


__all__ = ["encode_indexed_module_file", "decode_indexed_module_file"]
