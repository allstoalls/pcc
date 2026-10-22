from __future__ import annotations

"""Target-neutral value/liveness analysis helpers for the self backend."""

from . import BackendUnavailable
from .self_backend_ir import (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_ATOMICRMW,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_CMPXCHG,
    PARSED_INSTRUCTION_KIND_EXTRACTELEMENT,
    PARSED_INSTRUCTION_KIND_EXTRACTVALUE,
    PARSED_INSTRUCTION_KIND_FBINOP,
    PARSED_INSTRUCTION_KIND_FCMP,
    PARSED_INSTRUCTION_KIND_FNEG,
    PARSED_INSTRUCTION_KIND_FREEZE,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_INSERTELEMENT,
    PARSED_INSTRUCTION_KIND_INSERTVALUE,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_STORE_ATOMIC,
    PARSED_INSTRUCTION_KIND_SYSCALL6,
    PARSED_INSTRUCTION_KIND_VA_ARG,
    PARSED_INSTRUCTION_KINDS,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    _dot_numeric_text_key_id,
    text_key_names_equal,
)
from .self_backend_literals import (
    const_int_from_value,
    is_aggregate_literal_value,
    is_float_literal,
    is_hex_literal,
)


# Classification is a pure function of the operand spelling, and a module's
# operands repeat heavily: planning one oversized shard called the classifier
# 346732 times over roughly 35000 distinct strings.  Answering from a memo
# turns ten predicate checks plus three literal parsers into one lookup.  The
# key space is bounded by the operand names actually present in the compiled
# input, and every entry stays valid for the whole process because the answer
# depends on nothing but the string.
_LOCAL_VALUE_REF_CACHE: dict[str, bool] = {}

# Bounded on purpose.  The memo lives for the process and an emit worker handles
# several shards in a row, so an unbounded dict would pin every operand string
# of every module it ever saw -- in a worker that already runs at ~10 GB that is
# not an acceptable risk for a pure-function cache.  One module's distinct
# operand set is well under this cap; crossing it means the working set changed
# shape, and starting over costs only recomputation.
_LOCAL_VALUE_REF_CACHE_CAP = 1 << 17


def is_local_value_ref(value: str) -> bool:
    cached = _LOCAL_VALUE_REF_CACHE.get(value)
    if cached is not None:
        return cached
    computed = _is_local_value_ref_uncached(value)
    if len(_LOCAL_VALUE_REF_CACHE) >= _LOCAL_VALUE_REF_CACHE_CAP:
        _LOCAL_VALUE_REF_CACHE.clear()
    _LOCAL_VALUE_REF_CACHE[value] = computed
    return computed


def _is_local_value_ref_uncached(value: str) -> bool:
    # llvmlite names unnamed SSA results ``%.6``, decoded here as ``.6``.
    # That spelling also matches our permissive float-literal regex, but LLVM
    # canonical floating constants include a leading digit.  Classify the
    # dot-plus-digits spelling as SSA before consulting the literal helpers.
    if len(value) > 1 and value.startswith(".") and value[1:].isdigit():
        return True
    return not (
        value == "null"
        or value == "poison"
        or value == "undef"
        or value == "zeroinitializer"
        or is_aggregate_literal_value(value)
        or value.startswith("@")
        or value.startswith(
            (
                "gep0:",
                "gepconst:",
                "cexpr:",
                "negconst:",
                "addconst:",
                "ptrtointconst:",
                "inttoptrconst:",
            )
        )
        or const_int_from_value(value) is not None
        or is_hex_literal(value)
        or is_float_literal(value)
    )


def _instruction_defined_value_from_parts(kind: str, data: tuple) -> str | None:
    if kind in {"binop", "fbinop", "icmp", "fcmp", "cast"}:
        # These instruction tuples start with an opcode/predicate; their SSA
        # destination is the second field.  Treating the opcode as the
        # definition silently disables block-local liveness and every target
        # combine that depends on it.
        return data[1]
    if kind in {
        "alloca",
        "load",
        "load_atomic",
        "atomicrmw",
        "cmpxchg",
        "syscall6",
        "fneg",
        "select",
        "freeze",
        "insertelement",
        "extractelement",
        "shufflevector",
        "extractvalue",
        "insertvalue",
        "va_arg",
        "gep",
    }:
        return data[0]
    if kind == "call":
        return data[0]
    return None


def _instruction_defined_value_from_id_parts(
    kind_id: int,
    data: tuple,
) -> str | None:
    if kind_id in (
        PARSED_INSTRUCTION_KIND_BINOP,
        PARSED_INSTRUCTION_KIND_FBINOP,
        PARSED_INSTRUCTION_KIND_ICMP,
        PARSED_INSTRUCTION_KIND_FCMP,
        PARSED_INSTRUCTION_KIND_CAST,
    ):
        return data[1]
    if kind_id in (
        PARSED_INSTRUCTION_KIND_ALLOCA,
        PARSED_INSTRUCTION_KIND_LOAD,
        PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
        PARSED_INSTRUCTION_KIND_ATOMICRMW,
        PARSED_INSTRUCTION_KIND_CMPXCHG,
        PARSED_INSTRUCTION_KIND_SYSCALL6,
        PARSED_INSTRUCTION_KIND_FNEG,
        PARSED_INSTRUCTION_KIND_SELECT,
        PARSED_INSTRUCTION_KIND_FREEZE,
        PARSED_INSTRUCTION_KIND_INSERTELEMENT,
        PARSED_INSTRUCTION_KIND_EXTRACTELEMENT,
        PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
        PARSED_INSTRUCTION_KIND_EXTRACTVALUE,
        PARSED_INSTRUCTION_KIND_INSERTVALUE,
        PARSED_INSTRUCTION_KIND_VA_ARG,
        PARSED_INSTRUCTION_KIND_GEP,
        PARSED_INSTRUCTION_KIND_CALL,
    ):
        return data[0]
    return None


def instruction_defined_value(instr: ParsedInstr) -> str | None:
    if instr.kind in {"binop", "fbinop", "icmp", "fcmp", "cast"}:
        # These instruction tuples start with an opcode/predicate; their SSA
        # destination is the second field.  Treating the opcode as the
        # definition silently disables block-local liveness and every target
        # combine that depends on it.
        return instr.data[1]
    if instr.kind in {
        "alloca",
        "load",
        "load_atomic",
        "atomicrmw",
        "cmpxchg",
        "syscall6",
        "fneg",
        "select",
        "freeze",
        "insertelement",
        "extractelement",
        "shufflevector",
        "extractvalue",
        "insertvalue",
        "va_arg",
        "gep",
    }:
        return instr.data[0]
    if instr.kind == "call":
        return instr.data[0]
    return None


def _instruction_used_values_from_parts(kind: str, data: tuple) -> list[str]:
    values: list[str] = []
    if kind == "store":
        values = [data[1], data[3]]
    elif kind == "load":
        values = [data[3]]
    elif kind == "load_atomic":
        values = [data[3]]
    elif kind == "store_atomic":
        values = [data[1], data[3]]
    elif kind == "atomicrmw":
        values = [data[3], data[5]]
    elif kind == "cmpxchg":
        values = [data[3], data[5], data[6]]
    elif kind == "syscall6":
        values = list(data[1])
    elif kind == "va_arg":
        values = [data[2]]
    elif kind in {"binop", "fbinop", "icmp", "fcmp"}:
        values = [data[3], data[4]]
    elif kind == "fneg":
        values = [data[2]]
    elif kind == "cast":
        values = [data[3]]
    elif kind == "select":
        values = [data[2], data[3], data[4]]
    elif kind == "freeze":
        values = [data[2]]
    elif kind == "insertelement":
        values = [data[2], data[4], data[5]]
    elif kind == "extractelement":
        values = [data[2], data[3]]
    elif kind == "shufflevector":
        values = [data[2], data[3], data[5]]
    elif kind == "extractvalue":
        values = [data[2]]
    elif kind == "insertvalue":
        values = [data[2], data[4]]
    elif kind == "gep":
        values = [data[3]]
        for index_pair in data[4]:
            values.append(index_pair[1])
    elif kind == "call":
        if data[3]:
            values.append(data[2])
        for argument in data[4]:
            values.append(argument[1])
    return [value for value in values if is_local_value_ref(value)]


def _instruction_used_values_from_id_parts(
    kind_id: int,
    data: tuple,
) -> list[str]:
    values: list[str] = []
    if kind_id == PARSED_INSTRUCTION_KIND_STORE:
        values = [data[1], data[3]]
    elif kind_id == PARSED_INSTRUCTION_KIND_LOAD:
        values = [data[3]]
    elif kind_id == PARSED_INSTRUCTION_KIND_LOAD_ATOMIC:
        values = [data[3]]
    elif kind_id == PARSED_INSTRUCTION_KIND_STORE_ATOMIC:
        values = [data[1], data[3]]
    elif kind_id == PARSED_INSTRUCTION_KIND_ATOMICRMW:
        values = [data[3], data[5]]
    elif kind_id == PARSED_INSTRUCTION_KIND_CMPXCHG:
        values = [data[3], data[5], data[6]]
    elif kind_id == PARSED_INSTRUCTION_KIND_SYSCALL6:
        values = list(data[1])
    elif kind_id == PARSED_INSTRUCTION_KIND_VA_ARG:
        values = [data[2]]
    elif kind_id in (
        PARSED_INSTRUCTION_KIND_BINOP,
        PARSED_INSTRUCTION_KIND_FBINOP,
        PARSED_INSTRUCTION_KIND_ICMP,
        PARSED_INSTRUCTION_KIND_FCMP,
    ):
        values = [data[3], data[4]]
    elif kind_id == PARSED_INSTRUCTION_KIND_FNEG:
        values = [data[2]]
    elif kind_id == PARSED_INSTRUCTION_KIND_CAST:
        values = [data[3]]
    elif kind_id == PARSED_INSTRUCTION_KIND_SELECT:
        values = [data[2], data[3], data[4]]
    elif kind_id == PARSED_INSTRUCTION_KIND_FREEZE:
        values = [data[2]]
    elif kind_id == PARSED_INSTRUCTION_KIND_INSERTELEMENT:
        values = [data[2], data[4], data[5]]
    elif kind_id == PARSED_INSTRUCTION_KIND_EXTRACTELEMENT:
        values = [data[2], data[3]]
    elif kind_id == PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR:
        values = [data[2], data[3], data[5]]
    elif kind_id == PARSED_INSTRUCTION_KIND_EXTRACTVALUE:
        values = [data[2]]
    elif kind_id == PARSED_INSTRUCTION_KIND_INSERTVALUE:
        values = [data[2], data[4]]
    elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
        values = [data[3]]
        for index_pair in data[4]:
            values.append(index_pair[1])
    elif kind_id == PARSED_INSTRUCTION_KIND_CALL:
        if data[3]:
            values.append(data[2])
        for argument in data[4]:
            values.append(argument[1])
    return [value for value in values if is_local_value_ref(value)]


def instruction_used_values(instr: ParsedInstr) -> list[str]:
    kind = instr.kind
    data = instr.data
    values: list[str] = []
    if kind == "store":
        _value_type, value, _ptr_type, ptr_name = data
        values = [value, ptr_name]
    elif kind == "load":
        _dest, _value_type, _ptr_type, ptr_name = data
        values = [ptr_name]
    elif kind == "load_atomic":
        values = [data[3]]
    elif kind == "store_atomic":
        values = [data[1], data[3]]
    elif kind == "atomicrmw":
        _dest, _op, _ptr_type, ptr_name, _value_type, value, _ordering = data
        values = [ptr_name, value]
    elif kind == "cmpxchg":
        (
            _dest,
            _pair_type,
            _ptr_type,
            ptr_name,
            _value_type,
            expected,
            desired,
            _success,
            _failure,
        ) = data
        values = [ptr_name, expected, desired]
    elif kind == "syscall6":
        _dest, arg_values = data
        values = list(arg_values)
    elif kind == "va_arg":
        _dest, _ap_type, ptr_name, _value_type = data
        values = [ptr_name]
    elif kind in {"binop", "fbinop", "icmp", "fcmp"}:
        _op, _dest, _value_type, lhs, rhs = data
        values = [lhs, rhs]
    elif kind == "fneg":
        _dest, _value_type, value = data
        values = [value]
    elif kind == "cast":
        _op, _dest, _src_type, value, _dst_type = data
        values = [value]
    elif kind == "select":
        _dest, _value_type, cond, true_value, false_value = data
        values = [cond, true_value, false_value]
    elif kind == "freeze":
        _dest, _value_type, value = data
        values = [value]
    elif kind == "insertelement":
        _dest, _vector_type, vector_value, _elem_type, elem_value, index_value = data
        values = [vector_value, elem_value, index_value]
    elif kind == "extractelement":
        _dest, _vector_type, vector_value, index_value, _elem_type = data
        values = [vector_value, index_value]
    elif kind == "shufflevector":
        _dest, _vector_type, lhs, rhs, _mask_type, mask_value = data
        values = [lhs, rhs, mask_value]
    elif kind == "extractvalue":
        _dest, _aggregate_type, value, _indices, _result_type, _offset = data
        values = [value]
    elif kind == "insertvalue":
        (
            _dest,
            _aggregate_type,
            aggregate_value,
            _elem_type,
            elem_value,
            _indices,
            _offset,
        ) = data
        values = [aggregate_value, elem_value]
    elif kind == "gep":
        _dest, _base_type, _ptr_type, ptr_value, indices = data
        values = [ptr_value, *[index_value for _index_type, index_value in indices]]
    elif kind == "call":
        (
            _dest,
            _ret_type,
            callee,
            is_indirect,
            args,
            _fixed_arg_count,
            _is_vararg,
            _arg_alignments,
        ) = data
        if is_indirect:
            values.append(callee)
        values.extend(arg_value for _arg_type, arg_value in args)
    return [value for value in values if is_local_value_ref(value)]


def terminator_used_values(term: ParsedInstr) -> list[str]:
    values: list[str] = []
    if term.kind == "ret":
        _ret_type, value = term.data
        values = [value]
    elif term.kind == "br_cond":
        cond_name, _true_target, _false_target = term.data
        values = [cond_name]
    elif term.kind == "switch":
        _value_type, value, _default_target, _cases = term.data
        values = [value]
    return [value for value in values if is_local_value_ref(value)]


def _stable_text_bucket_key(text: str) -> int:
    numeric_id = _dot_numeric_text_key_id(text)
    if numeric_id >= 0:
        # Negative keys reserve a collision-free lane for the canonical
        # ``.N``/``%.N`` aliases while ordinary polynomial keys stay >= 0.
        return -numeric_id - 1
    # The owned builder's function-wide name allocator gives every generated value and
    # block a unique decimal suffix (``name.N``).  The suffix is already the
    # dense provenance key; hashing the often-long diagnostic prefix one
    # character at a time merely repeats work.  Equal suffixes remain safe:
    # the open-addressed tables compare the full spelling before accepting an
    # entry, exactly as they do for ordinary polynomial collisions.
    suffix_at = text.rfind(".")
    if 0 <= suffix_at < len(text) - 1:
        suffix = text[suffix_at + 1 :]
        if suffix.isdecimal():
            return -int(suffix) - 1
    # 1099511627776 is 2**40, so the modulo is a mask.  A `%` per character is
    # the single most expensive operation in this loop, and under pcc1 an
    # arbitrary-precision modulo is worse still; masking is bit-identical.
    mask = 1099511627775
    value = 0
    index = 0
    while index < len(text):
        value = (value * 131 + ord(text[index])) & mask
        index += 1
    return value


def _record_definition(
    buckets: dict[int, list[tuple[str, str]]],
    value_name: str,
    block_name: str,
) -> None:
    bucket = buckets.setdefault(_stable_text_bucket_key(value_name), [])
    for index, (existing_name, _existing_block) in enumerate(bucket):
        if text_key_names_equal(existing_name, value_name):
            bucket[index] = (existing_name, block_name)
            return
    bucket.append((value_name, block_name))


def _record_block_length(
    buckets: dict[int, list[tuple[str, int]]],
    block_name: str,
    length: int,
) -> None:
    bucket = buckets.setdefault(_stable_text_bucket_key(block_name), [])
    for index, (existing_name, _existing_length) in enumerate(bucket):
        if text_key_names_equal(existing_name, block_name):
            bucket[index] = (existing_name, length)
            return
    bucket.append((block_name, length))


def _block_length_get(
    buckets: dict[int, list[tuple[str, int]]],
    block_name: str,
) -> int | None:
    for existing_name, length in buckets.get(
        _stable_text_bucket_key(block_name), []
    ):
        if text_key_names_equal(existing_name, block_name):
            return length
    return None


def _canonical_block_key(name: str):
    """Key on which ``text_key_names_equal`` is plain equality."""
    name_id = _dot_numeric_text_key_id(name)
    if name_id >= 0:
        return name_id
    return name


def _record_use_position(
    buckets: dict[
        int,
        list[tuple[str, list[tuple[str, int]]]],
    ],
    value_name: str,
    block_name: str,
    position: int,
) -> None:
    bucket = buckets.setdefault(_stable_text_bucket_key(value_name), [])
    use_sites = None
    for existing_name, existing_sites in bucket:
        if text_key_names_equal(existing_name, value_name):
            use_sites = existing_sites
            break
    if use_sites is None:
        use_sites = {}
        bucket.append((value_name, use_sites))
    # Keyed on the canonical block key rather than scanned: a value used across
    # B blocks paid B comparisons per record, so one giant function made this
    # quadratic.  `text_key_names_equal` is plain equality on this key, so the
    # dict lookup is exact -- see _canonical_block_key.
    key = _canonical_block_key(block_name)
    existing = use_sites.get(key)
    if existing is None or position > existing[1]:
        use_sites[key] = (existing[0] if existing is not None else block_name,
                          position if existing is None or position > existing[1]
                          else existing[1])


def _use_sites_get(
    buckets: dict[
        int,
        list[tuple[str, list[tuple[str, int]]]],
    ],
    value_name: str,
) -> list[tuple[str, int]] | None:
    for existing_name, use_sites in buckets.get(
        _stable_text_bucket_key(value_name), []
    ):
        if text_key_names_equal(existing_name, value_name):
            # Insertion order is preserved by dict, so the single-use case the
            # caller checks sees the same entry it saw when this was a list.
            return list(use_sites.values())
    return None


def _parsed_block_terminator_used_values(
    func: ParsedFunction,
    block_id: int,
    block: ParsedBlock,
) -> list[str]:
    if block.terminator is not None:
        return terminator_used_values(block.terminator)
    seed = func.indexed_seed
    if seed is None:
        return []
    value = seed.terminator_used_value(block_id)
    return [] if value is None else [value]


def collect_block_local_last_uses(func: ParsedFunction) -> dict[str, dict[str, int]]:
    """Return last-use positions only for values confined to one block.

    Native bootstrap can produce equal text names with inconsistent hashes.
    Stable integer buckets plus equality checks keep a hidden cross-block use
    from being mistaken for a local interval, which would otherwise let an
    AArch64 caller-saved register escape its proven block/call boundary.
    """

    if func.indexed_kernel is not None:
        return func.indexed_kernel.legacy_block_last_uses()

    def_buckets: dict[int, list[tuple[str, str]]] = {}
    block_length_buckets: dict[int, list[tuple[str, int]]] = {}
    for block_id, block in enumerate(func.blocks):
        _record_block_length(
            block_length_buckets, block.name, len(block.instructions)
        )
        for phi in block.phis:
            _record_definition(def_buckets, phi.dest, block.name)
        instructions = block.instructions
        kind_ids = instructions._kind_ids
        instruction_index = 0
        instruction_count = len(kind_ids)
        while instruction_index < instruction_count:
            kind_id = kind_ids[instruction_index]
            if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
                raise BackendUnavailable(
                    f"corrupt parsed-instruction kind id {kind_id}"
                )
            kind = PARSED_INSTRUCTION_KINDS[kind_id]
            data = instructions.instruction_data(instruction_index)
            dest = _instruction_defined_value_from_parts(kind, data)
            if dest is not None:
                _record_definition(def_buckets, dest, block.name)
            instruction_index += 1

    use_buckets: dict[
        int,
        list[tuple[str, list[tuple[str, int]]]],
    ] = {}
    for block_id, block in enumerate(func.blocks):
        term_pos = len(block.instructions)
        for phi in block.phis:
            for incoming in phi.incoming:
                if not is_local_value_ref(incoming.value):
                    continue
                incoming_position = _block_length_get(
                    block_length_buckets, incoming.label
                )
                assert incoming_position is not None
                _record_use_position(
                    use_buckets,
                    incoming.value,
                    incoming.label,
                    incoming_position,
                )
        instructions = block.instructions
        kind_ids = instructions._kind_ids
        instruction_index = 0
        instruction_count = len(kind_ids)
        while instruction_index < instruction_count:
            kind_id = kind_ids[instruction_index]
            if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
                raise BackendUnavailable(
                    f"corrupt parsed-instruction kind id {kind_id}"
                )
            kind = PARSED_INSTRUCTION_KINDS[kind_id]
            data = instructions.instruction_data(instruction_index)
            for value in _instruction_used_values_from_parts(kind, data):
                _record_use_position(use_buckets, value, block.name, instruction_index)
            instruction_index += 1
        for value in _parsed_block_terminator_used_values(func, block_id, block):
            _record_use_position(use_buckets, value, block.name, term_pos)

    block_local_last_uses: dict[str, dict[str, int]] = {}
    # Resolve the per-block dict through a stable int bucket, NOT by looking up
    # `block_local_last_uses` itself.  That dict grows inside this very loop, so
    # a text-keyed lookup on it re-entered the false-hash-miss fallback, whose
    # incremental index materialised `list(mapping)` on every growth: one
    # 28 MB shard turned into O(definitions x blocks) pointer copies plus a
    # fresh multi-hundred-KB list per definition -- measured at 57+ minutes of
    # 100% CPU and a 54.4 GB physical footprint for a single emit worker, with
    # `collect_block_local_last_uses` -> `text_key_mapping_get` holding
    # 6545 of 6573 samples.  Bucketing is the same technique the rest of this
    # function already uses, and equality is still checked explicitly, so
    # inconsistent native str hashing remains handled.
    mapping_buckets: dict[int, list[tuple[str, dict[str, int]]]] = {}
    for definition_bucket in def_buckets.values():
        for value, block_name in definition_bucket:
            use_sites = _use_sites_get(use_buckets, value)
            if (
                use_sites is None
                or len(use_sites) != 1
                or not text_key_names_equal(use_sites[0][0], block_name)
            ):
                continue
            bucket = mapping_buckets.setdefault(
                _stable_text_bucket_key(block_name), []
            )
            block_mapping = None
            for existing_name, candidate in bucket:
                if text_key_names_equal(existing_name, block_name):
                    block_mapping = candidate
                    break
            if block_mapping is None:
                block_mapping = {}
                bucket.append((block_name, block_mapping))
                block_local_last_uses[block_name] = block_mapping
            block_mapping[value] = use_sites[0][1]
    return block_local_last_uses


def collect_used_values(func: ParsedFunction) -> list[str]:
    # Keep the authoritative use collection hash-free. Native bootstrap can
    # produce equal SSA-name strings whose hashes disagree; set.add/update can
    # then lose the equality relationship that stack-slot assignment needs.
    if func.indexed_kernel is not None:
        return func.indexed_kernel.legacy_used_values()

    used: list[str] = []
    for block_id, block in enumerate(func.blocks):
        for phi in block.phis:
            for incoming in phi.incoming:
                if is_local_value_ref(incoming.value):
                    used.append(incoming.value)
        instructions = block.instructions
        kind_ids = instructions._kind_ids
        instruction_index = 0
        instruction_count = len(kind_ids)
        while instruction_index < instruction_count:
            kind_id = kind_ids[instruction_index]
            if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
                raise BackendUnavailable(
                    f"corrupt parsed-instruction kind id {kind_id}"
                )
            kind = PARSED_INSTRUCTION_KINDS[kind_id]
            data = instructions.instruction_data(instruction_index)
            used.extend(_instruction_used_values_from_parts(kind, data))
            instruction_index += 1
        used.extend(_parsed_block_terminator_used_values(func, block_id, block))
    return used


def value_has_uses(func: ParsedFunction, value_name: str) -> bool:
    if func.indexed_kernel is not None:
        value_id = func.indexed_kernel.value_id(value_name)
        return value_id >= 0 and func.indexed_kernel.value_is_used(value_id)
    if value_name in collect_used_values(func):
        return True
    for block_id, block in enumerate(func.blocks):
        for phi in block.phis:
            for incoming in phi.incoming:
                if incoming.value == value_name:
                    return True
        for instr in block.instructions:
            if value_name in instruction_used_values(instr):
                return True
        if value_name in _parsed_block_terminator_used_values(
            func,
            block_id,
            block,
        ):
            return True
    return False
