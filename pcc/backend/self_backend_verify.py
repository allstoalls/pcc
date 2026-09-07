from __future__ import annotations

"""Fail-closed verifier for the parsed self-backend LLVM-IR subset.

The textual parser deliberately accepts only a finite LLVM-IR surface, but a
successfully decoded instruction is not by itself proof that its SSA graph is
valid.  This verifier runs before stack-slot assignment or target lowering and
checks the invariants that the emitters otherwise have to assume.

It is diagnostics-only: it never rewrites IR, repairs PHIs, or widens the
accepted instruction set.  Check names are stable so a malformed program is
reported at this boundary instead of becoming a target-specific miscompile.
"""

from . import BackendUnavailable
from .self_backend_analysis import (
    is_local_value_ref,
    terminator_used_values,
)
from .self_backend_ir import (
    I1,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_ATOMICRMW,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
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
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_STORE_ATOMIC,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_SYSCALL6,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
    PARSED_INSTRUCTION_KIND_VA_ARG,
    PARSED_INSTRUCTION_KINDS,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    ParsedModule,
    TypeDesc,
    text_key_names_equal,
)
from .self_backend_kernel import (
    INLINE_ERROR_EDGE_WIDTH,
    IndexedFunctionKernel,
    TYPE_KIND_ARRAY,
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    TYPE_KIND_STRUCT,
    TYPE_KIND_VOID,
    get_indexed_function_kernel,
)
from .self_backend_value_arena import (
    CompilerInt2,
    CompilerInt4,
)


def _stable_text_key(text: str) -> int:
    """Hash text without relying on the native-bootstrap string hash cache."""

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


def _fail(check: str, func: ParsedFunction, detail: str) -> None:
    raise BackendUnavailable(
        f"self IR verifier [{check}] in {func.name!r}: {detail}"
    )


def _types_match(left: TypeDesc, right: TypeDesc) -> bool:
    # Opaque and typed LLVM pointers share one machine representation in the
    # current subset.  The parser intentionally retains a pointee when one was
    # written, but that spelling is not an operand mismatch under opaque ptr.
    if left.is_ptr and right.is_ptr:
        return True
    if left.kind != right.kind:
        return False
    if left.is_void:
        return True
    if left.is_int or left.is_fp:
        return left.width == right.width
    if left.is_array:
        return (
            left.count == right.count
            and left.elem is not None
            and right.elem is not None
            and _types_match(left.elem, right.elem)
        )
    if left.is_struct:
        if left.name or right.name:
            return left.name == right.name
        return len(left.fields) == len(right.fields) and all(
            _types_match(left_field, right_field)
            for left_field, right_field in zip(left.fields, right.fields)
        )
    return False


def _type_ids_match(
    kernel: IndexedFunctionKernel, left_id: int, right_id: int
) -> bool:
    if left_id == right_id:
        return True
    left: CompilerInt4 = kernel.type_header(left_id)
    right: CompilerInt4 = kernel.type_header(right_id)
    return left.first == TYPE_KIND_PTR and right.first == TYPE_KIND_PTR


def _type_id_is_int(kernel: IndexedFunctionKernel, type_id: int) -> bool:
    header: CompilerInt4 = kernel.type_header(type_id)
    return header.first == TYPE_KIND_INT


def _type_id_is_int_lane(kernel: IndexedFunctionKernel, type_id: int) -> bool:
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first == TYPE_KIND_INT:
        return True
    if header.first != TYPE_KIND_ARRAY or header.fourth < 0:
        return False
    child: CompilerInt4 = kernel.type_header(header.fourth)
    return child.first == TYPE_KIND_INT


def _type_id_is_fp_lane(kernel: IndexedFunctionKernel, type_id: int) -> bool:
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first == TYPE_KIND_FP:
        return True
    if header.first != TYPE_KIND_ARRAY or header.fourth < 0:
        return False
    child: CompilerInt4 = kernel.type_header(header.fourth)
    return child.first == TYPE_KIND_FP


def _type_id_is_i1(kernel: IndexedFunctionKernel, type_id: int) -> bool:
    header: CompilerInt4 = kernel.type_header(type_id)
    return header.first == TYPE_KIND_INT and header.second == 1


def _is_i1(value_type: TypeDesc) -> bool:
    return value_type.is_int and value_type.width == 1


def _is_int(value_type: TypeDesc) -> bool:
    return value_type.is_int


def _is_int_lane(value_type: TypeDesc) -> bool:
    return value_type.is_int or (
        value_type.is_array
        and value_type.elem is not None
        and value_type.elem.is_int
    )


def _is_fp_lane(value_type: TypeDesc) -> bool:
    return value_type.is_fp or (
        value_type.is_array
        and value_type.elem is not None
        and value_type.elem.is_fp
    )


def _block_index(
    blocks: list[ParsedBlock],
    buckets: dict[int, list[int]],
    name: str,
) -> int:
    for index in buckets.get(_stable_text_key(name), []):
        if text_key_names_equal(blocks[index].name, name):
            return index
    return -1


def _definition_get(
    kernel: IndexedFunctionKernel,
    definitions: IndexedFunctionKernel,
    name: str,
) -> CompilerInt4:
    return _definition_by_id(kernel, definitions, kernel.value_id(name))


def _definition_by_id(
    kernel: IndexedFunctionKernel,
    definitions: IndexedFunctionKernel,
    value_id: int,
) -> CompilerInt4:
    if value_id < 0 or value_id >= len(kernel.value_names):
        return CompilerInt4(-1, -1, -1, 0)
    position = definitions.definition_position(value_id)
    if position == -3:
        return CompilerInt4(-1, -1, -1, 0)
    header: CompilerInt4 = kernel.value_header(value_id)
    # type id, definition block, definition position, present flag.
    return CompilerInt4(header.second, header.first, position, 1)


def _instruction_result_type_parts(kind_id: int, data: tuple) -> TypeDesc | None:
    if kind_id == PARSED_INSTRUCTION_KIND_ALLOCA:
        return data[1].ptr()
    if kind_id in (
        PARSED_INSTRUCTION_KIND_LOAD,
        PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    ):
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_ATOMICRMW:
        return data[4]
    if kind_id == PARSED_INSTRUCTION_KIND_CMPXCHG:
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_SYSCALL6:
        return TypeDesc("int", 64)
    if kind_id in (
        PARSED_INSTRUCTION_KIND_BINOP,
        PARSED_INSTRUCTION_KIND_FBINOP,
    ):
        return data[2]
    if kind_id == PARSED_INSTRUCTION_KIND_FNEG:
        return data[1]
    if kind_id in (
        PARSED_INSTRUCTION_KIND_ICMP,
        PARSED_INSTRUCTION_KIND_FCMP,
    ):
        value_type = data[2]
        if value_type.is_array and value_type.elem is not None:
            return TypeDesc("array", count=value_type.count, elem=I1)
        return I1
    if kind_id == PARSED_INSTRUCTION_KIND_CAST:
        return data[4]
    if kind_id in (
        PARSED_INSTRUCTION_KIND_SELECT,
        PARSED_INSTRUCTION_KIND_FREEZE,
    ):
        return data[1]
    if kind_id in (
        PARSED_INSTRUCTION_KIND_INSERTELEMENT,
        PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
    ):
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_EXTRACTELEMENT:
        return data[4]
    if kind_id == PARSED_INSTRUCTION_KIND_EXTRACTVALUE:
        return data[4]
    if kind_id == PARSED_INSTRUCTION_KIND_INSERTVALUE:
        return data[1]
    if kind_id == PARSED_INSTRUCTION_KIND_VA_ARG:
        return data[3]
    if kind_id == PARSED_INSTRUCTION_KIND_GEP:
        return data[2]
    if kind_id == PARSED_INSTRUCTION_KIND_CALL:
        return data[1]
    return None


def _successor_names(term: ParsedInstr) -> tuple[str, ...]:
    if term.kind == "br":
        return (term.data[0],)
    if term.kind == "br_cond":
        return (term.data[1], term.data[2])
    if term.kind == "switch":
        return (
            term.data[2],
            *(target for _case_value, target in term.data[3]),
        )
    return ()


def _build_cfg(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
) -> tuple[dict[int, list[int]], list[list[int]], list[list[int]]]:
    block_names = kernel.block_names
    block_buckets: dict[int, list[int]] = {}
    for index, block_name in enumerate(block_names):
        bucket = block_buckets.setdefault(_stable_text_key(block_name), [])
        for existing_index in bucket:
            if text_key_names_equal(block_names[existing_index], block_name):
                _fail(
                    "terminator",
                    func,
                    f"duplicate basic block label {block_name!r}",
                )
        bucket.append(index)

    predecessors: list[list[int]] = [[] for _name in block_names]
    successors: list[list[int]] = [[] for _name in block_names]
    for index, block_name in enumerate(block_names):
        successor_count = kernel.cfg_successor_count(index)
        successor_index = 0
        while successor_index < successor_count:
            target_index = kernel.cfg_successor_id(
                index,
                successor_index,
            )
            if target_index < 0:
                target_name = "<invalid-inline-or-terminator-target>"
                _fail(
                    "terminator",
                    func,
                    f"block {block_name!r} branches to missing block {target_name!r}",
                )
            if target_index not in successors[index]:
                successors[index].append(target_index)
            if index not in predecessors[target_index]:
                predecessors[target_index].append(index)
            successor_index += 1
    return block_buckets, predecessors, successors


def _compute_dominators(
    predecessors: list[list[int]],
    successors: list[list[int]],
) -> tuple[list[int], list[int]]:
    """Dominator-tree preorder intervals via Cooper-Harvey-Kennedy idoms.

    Returns (dom_in, dom_out); block d dominates block b iff
    dom_in[d] <= dom_in[b] <= dom_out[d]. Memory stays O(blocks): dense
    per-block dominator sets are O(blocks^2), and a 72k-block generated
    module top needs hundreds of GiB that way.
    """
    block_count = len(predecessors)
    postorder: list[int] = []
    visited = [False] * block_count
    visited[0] = True
    stack: list[tuple[int, int]] = [(0, 0)]
    while stack:
        block, edge = stack[-1]
        if edge < len(successors[block]):
            stack[-1] = (block, edge + 1)
            target = successors[block][edge]
            if not visited[target]:
                visited[target] = True
                stack.append((target, 0))
        else:
            stack.pop()
            postorder.append(block)
    rpo: list[int] = []
    index = len(postorder) - 1
    while index >= 0:
        rpo.append(postorder[index])
        index -= 1
    rpo_number = [-1] * block_count
    for order, block in enumerate(rpo):
        rpo_number[block] = order
    idom = [-1] * block_count
    idom[0] = 0
    changed = True
    while changed:
        changed = False
        for block in rpo:
            if block == 0:
                continue
            new_idom = -1
            for pred in predecessors[block]:
                if idom[pred] < 0:
                    continue
                if new_idom < 0:
                    new_idom = pred
                    continue
                finger1 = pred
                finger2 = new_idom
                while finger1 != finger2:
                    while rpo_number[finger1] > rpo_number[finger2]:
                        finger1 = idom[finger1]
                    while rpo_number[finger2] > rpo_number[finger1]:
                        finger2 = idom[finger2]
                new_idom = finger1
            if new_idom >= 0 and idom[block] != new_idom:
                idom[block] = new_idom
                changed = True
    children: list[list[int]] = [[] for _index in range(block_count)]
    for block in rpo:
        if block != 0 and idom[block] >= 0:
            children[idom[block]].append(block)
    dom_in = [-1] * block_count
    dom_out = [-1] * block_count
    counter = 1
    dom_in[0] = 0
    walk: list[tuple[int, int]] = [(0, 0)]
    while walk:
        block, child = walk[-1]
        if child < len(children[block]):
            walk[-1] = (block, child + 1)
            target = children[block][child]
            dom_in[target] = counter
            counter += 1
            walk.append((target, 0))
        else:
            walk.pop()
            dom_out[block] = counter
            counter += 1
    # parse_self_backend_module filters unreachable blocks, so only malformed
    # input still contains one; such a block dominates only itself.
    for block in range(block_count):
        if dom_in[block] < 0:
            dom_in[block] = counter
            dom_out[block] = counter + 1
            counter += 2
    return dom_in, dom_out


def _block_dominates(
    dominators: tuple[list[int], list[int]],
    dom_block: int,
    block: int,
) -> bool:
    dom_in, dom_out = dominators
    return dom_in[dom_block] <= dom_in[block] <= dom_out[dom_block]


def _build_definitions(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
) -> None:
    kernel = get_indexed_function_kernel(func)
    duplicate_id = kernel.first_duplicate_definition_value_id
    if duplicate_id >= 0:
        _fail(
            "ssa-definition",
            func,
            f"value {kernel.value_name(duplicate_id)!r} has more than one definition",
        )
    block_index = 0
    while block_index < len(kernel.block_names):
        block_fact: CompilerInt4 = kernel.block_fact(block_index)
        position = 0
        while position < block_fact.second:
            instruction_id = block_fact.first + position
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                instruction_id
            )
            dest_id = instruction_fact.first
            if dest_id < 0:
                position += 1
                continue
            dest = kernel.value_name(dest_id)
            payload_id = metadata.second
            if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                call_header: CompilerInt4 = kernel.call_header(payload_id)
                result_type_id = call_header.first
            elif kind_id == PARSED_INSTRUCTION_KIND_ALLOCA:
                allocated_type_id = kernel.alloca_type_id(dest_id)
                result_type_id = kernel.intern_type(
                    kernel.types[allocated_type_id].ptr()
                )
            elif (
                kind_id == PARSED_INSTRUCTION_KIND_LOAD
                or kind_id == PARSED_INSTRUCTION_KIND_BINOP
                or kind_id == PARSED_INSTRUCTION_KIND_ICMP
                or kind_id == PARSED_INSTRUCTION_KIND_CAST
                or kind_id == PARSED_INSTRUCTION_KIND_SELECT
            ):
                record: CompilerInt4 = kernel.instruction_record(
                    payload_id
                )
                if (
                    kind_id == PARSED_INSTRUCTION_KIND_LOAD
                    or kind_id == PARSED_INSTRUCTION_KIND_SELECT
                ):
                    result_type_id = record.first
                elif kind_id == PARSED_INSTRUCTION_KIND_BINOP:
                    result_type_id = record.second
                elif kind_id == PARSED_INSTRUCTION_KIND_ICMP:
                    compared: CompilerInt4 = kernel.type_header(record.second)
                    compared_kind: int = compared.first
                    compared_count: int = compared.third
                    compared_child_id: int = compared.fourth
                    if (
                        compared_kind == TYPE_KIND_ARRAY
                        and compared_child_id >= 0
                    ):
                        result_type_id = kernel.intern_type(
                            TypeDesc("array", count=compared_count, elem=I1)
                        )
                    else:
                        result_type_id = kernel.intern_type(I1)
                else:
                    result_type_id = record.fourth
            elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                gep_span: CompilerInt4 = kernel.gep_span(
                    payload_id
                )
                result_type_id = gep_span.second
            else:
                data = kernel.instruction_data(block_index, position)
                result_type = _instruction_result_type_parts(kind_id, data)
                result_type_id = (
                    -1 if result_type is None else kernel.intern_type(result_type)
                )
            if result_type_id < 0:
                kind = PARSED_INSTRUCTION_KINDS[kind_id]
                _fail(
                    "operand-type",
                    func,
                    f"instruction {kind!r} defines {dest!r} without a value type",
                )
            result_type_header: CompilerInt4 = kernel.type_header(
                result_type_id
            )
            if result_type_header.first == TYPE_KIND_VOID:
                kind = PARSED_INSTRUCTION_KINDS[kind_id]
                _fail(
                    "operand-type",
                    func,
                    f"instruction {kind!r} defines {dest!r} without a value type",
                )
            kernel.publish_value_type_id(dest_id, result_type_id)
            position += 1
        block_index += 1


def _require_local_type(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    value: str,
    expected: TypeDesc,
    *,
    context: str,
) -> None:
    kernel = get_indexed_function_kernel(func)
    _require_local_type_id(
        func,
        definitions,
        value,
        kernel.intern_type(expected),
        context=context,
    )


def _require_local_type_id(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    value: str,
    expected_type_id: int,
    *,
    context: str,
) -> None:
    if not is_local_value_ref(value):
        return
    kernel = get_indexed_function_kernel(func)
    definition = _definition_get(kernel, definitions, value)
    if definition.fourth == 0:
        _fail("ssa-dominance", func, f"{context} uses undefined value {value!r}")
    if not _type_ids_match(kernel, definition.first, expected_type_id):
        _fail(
            "operand-type",
            func,
            f"{context} expects {kernel.type_desc(expected_type_id).describe()} "
            f"for {value!r}, got "
            + kernel.type_desc(definition.first).describe(),
        )


def _require_local_type_ref(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    kernel: IndexedFunctionKernel,
    value_ref: int,
    expected_type_id: int,
    *,
    context: str,
) -> None:
    if value_ref < 0:
        return
    definition: CompilerInt4 = _definition_by_id(
        kernel, definitions, value_ref
    )
    if definition.fourth == 0:
        value = kernel.value_name(value_ref)
        _fail("ssa-dominance", func, f"{context} uses undefined value {value!r}")
    if not _type_ids_match(kernel, definition.first, expected_type_id):
        value = kernel.value_name(value_ref)
        _fail(
            "operand-type",
            func,
            f"{context} expects {kernel.type_desc(expected_type_id).describe()} "
            f"for {value!r}, got "
            + kernel.type_desc(definition.first).describe(),
        )


def _require_local_int(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    value: str,
    *,
    context: str,
) -> None:
    if not is_local_value_ref(value):
        return
    kernel = get_indexed_function_kernel(func)
    definition = _definition_get(kernel, definitions, value)
    if definition.fourth == 0:
        _fail("ssa-dominance", func, f"{context} uses undefined value {value!r}")
    if not _type_id_is_int(kernel, definition.first):
        _fail(
            "operand-type",
            func,
            f"{context} expects an integer for {value!r}, got "
            + kernel.type_desc(definition.first).describe(),
        )


def _verify_instruction_types_parts(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    block_name: str,
    kind_id: int,
    data: tuple,
) -> None:
    # This is the explicit cold/long-tail verifier path.  Packed call/fixed/GEP
    # records are handled above without projecting their opcode IDs.
    kind = PARSED_INSTRUCTION_KINDS[kind_id]
    context = f"{block_name!r}/{kind}"
    if kind == "store":
        value_type, value, ptr_type, ptr = data
        if not ptr_type.is_ptr:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type(func, definitions, value, value_type, context=context)
        _require_local_type(func, definitions, ptr, ptr_type, context=context)
    elif kind in {"load", "load_atomic"}:
        _dest, _value_type, ptr_type, ptr, *_rest = data
        if not ptr_type.is_ptr:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type(func, definitions, ptr, ptr_type, context=context)
    elif kind == "store_atomic":
        value_type, value, ptr_type, ptr, _ordering = data
        if not ptr_type.is_ptr:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type(func, definitions, value, value_type, context=context)
        _require_local_type(func, definitions, ptr, ptr_type, context=context)
    elif kind == "atomicrmw":
        _dest, _op, ptr_type, ptr, value_type, value, _ordering = data
        if not ptr_type.is_ptr:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type(func, definitions, ptr, ptr_type, context=context)
        _require_local_type(func, definitions, value, value_type, context=context)
    elif kind == "cmpxchg":
        (
            _dest,
            _pair_type,
            ptr_type,
            ptr,
            value_type,
            expected,
            desired,
            _success,
            _failure,
        ) = data
        if not ptr_type.is_ptr:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type(func, definitions, ptr, ptr_type, context=context)
        _require_local_type(func, definitions, expected, value_type, context=context)
        _require_local_type(func, definitions, desired, value_type, context=context)
    elif kind == "syscall6":
        _dest, args = data
        i64 = TypeDesc("int", 64)
        for value in args:
            _require_local_type(func, definitions, value, i64, context=context)
    elif kind == "va_arg":
        _dest, ap_type, ap, _value_type = data
        if not ap_type.is_ptr:
            _fail("operand-type", func, f"{context} va_list type is not a pointer")
        _require_local_type(func, definitions, ap, ap_type, context=context)
    elif kind in {"binop", "fbinop", "icmp", "fcmp"}:
        _op, _dest, value_type, lhs, rhs = data
        if kind == "binop" and not _is_int_lane(value_type):
            _fail("operand-type", func, f"{context} expects integer operands")
        if kind in {"fbinop", "fcmp"} and not _is_fp_lane(value_type):
            _fail("operand-type", func, f"{context} expects floating operands")
        if kind == "icmp" and not (
            _is_int_lane(value_type) or value_type.is_ptr
        ):
            _fail("operand-type", func, f"{context} expects integer/pointer operands")
        _require_local_type(func, definitions, lhs, value_type, context=context)
        _require_local_type(func, definitions, rhs, value_type, context=context)
    elif kind == "fneg":
        _dest, value_type, value = data
        if not _is_fp_lane(value_type):
            _fail("operand-type", func, f"{context} expects a floating operand")
        _require_local_type(func, definitions, value, value_type, context=context)
    elif kind == "cast":
        _op, _dest, src_type, value, _dst_type = data
        _require_local_type(func, definitions, value, src_type, context=context)
    elif kind == "select":
        _dest, value_type, cond, true_value, false_value = data
        if is_local_value_ref(cond):
            kernel = get_indexed_function_kernel(func)
            cond_def = _definition_get(kernel, definitions, cond)
            if cond_def.fourth == 0:
                _fail("ssa-dominance", func, f"{context} uses undefined value {cond!r}")
            cond_type: CompilerInt4 = kernel.type_header(cond_def.first)
            value_type_id = kernel.intern_type(value_type)
            value_type_header: CompilerInt4 = kernel.type_header(value_type_id)
            vector_i1 = (
                cond_type.first == TYPE_KIND_ARRAY
                and cond_type.fourth >= 0
                and _type_id_is_i1(kernel, cond_type.fourth)
                and value_type_header.first == TYPE_KIND_ARRAY
                and cond_type.third == value_type_header.third
            )
            if not (_type_id_is_i1(kernel, cond_def.first) or vector_i1):
                _fail(
                    "operand-type",
                    func,
                    f"{context} condition {cond!r} is not i1/vector-i1",
                )
        _require_local_type(func, definitions, true_value, value_type, context=context)
        _require_local_type(func, definitions, false_value, value_type, context=context)
    elif kind == "freeze":
        _dest, value_type, value = data
        _require_local_type(func, definitions, value, value_type, context=context)
    elif kind == "insertelement":
        _dest, vector_type, vector, elem_type, elem, index = data
        _require_local_type(func, definitions, vector, vector_type, context=context)
        _require_local_type(func, definitions, elem, elem_type, context=context)
        _require_local_int(func, definitions, index, context=context)
    elif kind == "extractelement":
        _dest, vector_type, vector, index, _elem_type = data
        _require_local_type(func, definitions, vector, vector_type, context=context)
        _require_local_int(func, definitions, index, context=context)
    elif kind == "shufflevector":
        _dest, result_type, lhs, rhs, mask_type, mask = data
        for value in (lhs, rhs):
            if not is_local_value_ref(value):
                continue
            kernel = get_indexed_function_kernel(func)
            definition = _definition_get(kernel, definitions, value)
            if definition.fourth == 0:
                _fail("ssa-dominance", func, f"{context} uses undefined value {value!r}")
            definition_type: CompilerInt4 = kernel.type_header(
                definition.first
            )
            result_type_id = kernel.intern_type(result_type)
            result_header: CompilerInt4 = kernel.type_header(result_type_id)
            if (
                definition_type.first != TYPE_KIND_ARRAY
                or definition_type.fourth < 0
                or result_header.first != TYPE_KIND_ARRAY
                or result_header.fourth < 0
                or not _type_ids_match(
                    kernel,
                    definition_type.fourth,
                    result_header.fourth,
                )
            ):
                _fail("operand-type", func, f"{context} has incompatible vector operand")
        _require_local_type(func, definitions, mask, mask_type, context=context)
    elif kind == "extractvalue":
        _dest, aggregate_type, value, _indices, _result_type, _offset = data
        _require_local_type(func, definitions, value, aggregate_type, context=context)
    elif kind == "insertvalue":
        (
            _dest,
            aggregate_type,
            aggregate_value,
            elem_type,
            elem_value,
            _indices,
            _offset,
        ) = data
        _require_local_type(
            func, definitions, aggregate_value, aggregate_type, context=context
        )
        _require_local_type(func, definitions, elem_value, elem_type, context=context)
    elif kind == "gep":
        _dest, _base_type, ptr_type, ptr, indices = data
        if not ptr_type.is_ptr:
            _fail("operand-type", func, f"{context} base type is not a pointer")
        _require_local_type(func, definitions, ptr, ptr_type, context=context)
        for index_type, index_value in indices:
            _require_local_type(
                func, definitions, index_value, index_type, context=context
            )
    elif kind == "call":
        (
            _dest,
            _ret_type,
            callee,
            is_indirect,
            args,
            fixed_arg_count,
            is_vararg,
            arg_alignments,
        ) = data
        if len(arg_alignments) != len(args):
            _fail(
                "operand-type",
                func,
                f"{context} call argument alignment count does not match operands",
            )
        for alignment in arg_alignments:
            if alignment < 0 or (alignment and alignment & (alignment - 1)):
                _fail(
                    "operand-type",
                    func,
                    f"{context} call argument has invalid alignment {alignment}",
                )
        if is_indirect:
            _require_local_type(
                func,
                definitions,
                callee,
                TypeDesc("ptr", pointee=TypeDesc("void")),
                context=context,
            )
        if is_vararg and len(args) < fixed_arg_count:
            _fail("operand-type", func, f"{context} has too few fixed arguments")
        if fixed_arg_count and not is_vararg and len(args) != fixed_arg_count:
            _fail("operand-type", func, f"{context} argument count disagrees with signature")
        for arg_type, arg_value in args:
            _require_local_type(
                func, definitions, arg_value, arg_type, context=context
            )


def _definition_dominates_use(
    definition: CompilerInt4,
    block_index: int,
    position: int,
    dominators: tuple[list[int], list[int]],
) -> bool:
    if definition.second < 0:
        return True
    if definition.second == block_index:
        return definition.third < position
    return _block_dominates(dominators, definition.second, block_index)


def _verify_call_instruction_types_indexed(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    block_name: str,
    kernel: IndexedFunctionKernel,
    call_id: int,
) -> None:
    context = f"{block_name!r}/call"
    header: CompilerInt4 = kernel.call_header(call_id)
    span: CompilerInt4 = kernel.call_span(call_id)
    arg_count = span.first
    fixed_arg_count = span.second
    is_indirect = bool(header.third & 1)
    is_vararg = bool(header.third & 2)
    if is_indirect:
        callee = kernel.call_texts[header.second]
        definition = _definition_get(kernel, definitions, callee)
        if definition.fourth == 0:
            _fail(
                "ssa-dominance",
                func,
                f"{context} uses undefined value {callee!r}",
            )
        definition_type: CompilerInt4 = kernel.type_header(
            definition.first
        )
        if definition_type.first != TYPE_KIND_PTR:
            _fail(
                "operand-type",
                func,
                f"{context} expects a pointer for {callee!r}",
            )
    if is_vararg and arg_count < fixed_arg_count:
        _fail("operand-type", func, f"{context} has too few fixed arguments")
    if fixed_arg_count and not is_vararg and arg_count != fixed_arg_count:
        _fail(
            "operand-type",
            func,
            f"{context} argument count disagrees with signature",
        )
    arg_index = 0
    while arg_index < arg_count:
        raw: CompilerInt4 = kernel.call_arg(header.fourth + arg_index)
        alignment = raw.fourth
        if alignment < 0 or (alignment and alignment & (alignment - 1)):
            _fail(
                "operand-type",
                func,
                f"{context} call argument has invalid alignment {alignment}",
            )
        _require_local_type_ref(
            func,
            definitions,
            kernel,
            raw.second,
            raw.first,
            context=context,
        )
        arg_index += 1


def _verify_fixed_instruction_types_indexed(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    block_name: str,
    kernel: IndexedFunctionKernel,
    kind_id: int,
    record_id: int,
) -> None:
    if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
        kind_name = "load"
    elif kind_id == PARSED_INSTRUCTION_KIND_STORE:
        kind_name = "store"
    elif kind_id == PARSED_INSTRUCTION_KIND_BINOP:
        kind_name = "binop"
    elif kind_id == PARSED_INSTRUCTION_KIND_ICMP:
        kind_name = "icmp"
    elif kind_id == PARSED_INSTRUCTION_KIND_CAST:
        kind_name = "cast"
    else:
        kind_name = "select"
    context = f"{block_name!r}/{kind_name}"
    raw: CompilerInt4 = kernel.instruction_record(record_id)

    if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
        ptr_type_id = raw.second
        ptr_type: CompilerInt4 = kernel.type_header(ptr_type_id)
        if ptr_type.first != TYPE_KIND_PTR:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type_ref(
            func,
            definitions,
            kernel,
            raw.third,
            ptr_type_id,
            context=context,
        )
        return

    if kind_id == PARSED_INSTRUCTION_KIND_STORE:
        value_type_id = raw.first
        ptr_type_id = raw.third
        ptr_type: CompilerInt4 = kernel.type_header(ptr_type_id)
        if ptr_type.first != TYPE_KIND_PTR:
            _fail("operand-type", func, f"{context} address type is not a pointer")
        _require_local_type_ref(
            func, definitions, kernel, raw.second, value_type_id, context=context
        )
        _require_local_type_ref(
            func, definitions, kernel, raw.fourth, ptr_type_id, context=context
        )
        return

    if (
        kind_id == PARSED_INSTRUCTION_KIND_BINOP
        or kind_id == PARSED_INSTRUCTION_KIND_ICMP
    ):
        value_type_id = raw.second
        if kind_id == PARSED_INSTRUCTION_KIND_BINOP and not _type_id_is_int_lane(
            kernel, value_type_id
        ):
            _fail("operand-type", func, f"{context} expects integer operands")
        value_type_header: CompilerInt4 = kernel.type_header(value_type_id)
        if kind_id == PARSED_INSTRUCTION_KIND_ICMP and not (
            _type_id_is_int_lane(kernel, value_type_id)
            or value_type_header.first == TYPE_KIND_PTR
        ):
            _fail("operand-type", func, f"{context} expects integer/pointer operands")
        _require_local_type_ref(
            func, definitions, kernel, raw.third, value_type_id, context=context
        )
        _require_local_type_ref(
            func, definitions, kernel, raw.fourth, value_type_id, context=context
        )
        return

    if kind_id == PARSED_INSTRUCTION_KIND_CAST:
        src_type_id = raw.second
        _require_local_type_ref(
            func, definitions, kernel, raw.third, src_type_id, context=context
        )
        return

    value_type_id = raw.first
    value_type: CompilerInt4 = kernel.type_header(value_type_id)
    if raw.second >= 0:
        cond = kernel.value_name(raw.second)
        cond_def = _definition_by_id(kernel, definitions, raw.second)
        if cond_def.fourth == 0:
            _fail("ssa-dominance", func, f"{context} uses undefined value {cond!r}")
        cond_type: CompilerInt4 = kernel.type_header(cond_def.first)
        vector_i1 = (
            cond_type.first == TYPE_KIND_ARRAY
            and cond_type.fourth >= 0
            and _type_id_is_i1(kernel, cond_type.fourth)
            and value_type.first == TYPE_KIND_ARRAY
            and cond_type.third == value_type.third
        )
        if not (_type_id_is_i1(kernel, cond_def.first) or vector_i1):
            _fail(
                "operand-type",
                func,
                f"{context} condition {cond!r} is not i1/vector-i1",
            )
    _require_local_type_ref(
        func, definitions, kernel, raw.third, value_type_id, context=context
    )
    _require_local_type_ref(
        func, definitions, kernel, raw.fourth, value_type_id, context=context
    )


def _verify_gep_instruction_types_indexed(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    block_name: str,
    kernel: IndexedFunctionKernel,
    record_id: int,
) -> None:
    context = f"{block_name!r}/gep"
    header: CompilerInt4 = kernel.gep_header(record_id)
    span: CompilerInt4 = kernel.gep_span(record_id)
    ptr_type_id = header.second
    ptr_type: CompilerInt4 = kernel.type_header(ptr_type_id)
    if ptr_type.first != TYPE_KIND_PTR:
        _fail("operand-type", func, f"{context} base type is not a pointer")
    _require_local_type_ref(
        func, definitions, kernel, header.third, ptr_type_id, context=context
    )
    index = 0
    while index < span.first:
        raw: CompilerInt2 = kernel.gep_index(header.fourth + index)
        _require_local_type_ref(
            func,
            definitions,
            kernel,
            raw.second,
            raw.first,
            context=context,
        )
        index += 1


def _verify_ordinary_uses(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    dominators: tuple[list[int], list[int]],
) -> None:
    kernel = get_indexed_function_kernel(func)
    for block_index, block_name in enumerate(kernel.block_names):
        block_fact: CompilerInt4 = kernel.block_fact(block_index)
        position = 0
        while position < block_fact.second:
            instruction_id = block_fact.first + position
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                instruction_id
            )
            payload_id = metadata.second
            if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                _verify_call_instruction_types_indexed(
                    func,
                    definitions,
                    block_name,
                    kernel,
                    payload_id,
                )
            elif (
                kind_id == PARSED_INSTRUCTION_KIND_LOAD
                or kind_id == PARSED_INSTRUCTION_KIND_STORE
                or kind_id == PARSED_INSTRUCTION_KIND_BINOP
                or kind_id == PARSED_INSTRUCTION_KIND_ICMP
                or kind_id == PARSED_INSTRUCTION_KIND_CAST
                or kind_id == PARSED_INSTRUCTION_KIND_SELECT
            ):
                _verify_fixed_instruction_types_indexed(
                    func,
                    definitions,
                    block_name,
                    kernel,
                    kind_id,
                    payload_id,
                )
            elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                _verify_gep_instruction_types_indexed(
                    func,
                    definitions,
                    block_name,
                    kernel,
                    payload_id,
                )
            elif kind_id != PARSED_INSTRUCTION_KIND_ALLOCA:
                data = kernel.instruction_data(block_index, position)
                _verify_instruction_types_parts(
                    func,
                    definitions,
                    block_name,
                    kind_id,
                    data,
                )
            use_index = 0
            use_count = instruction_fact.second
            while use_index < use_count:
                if use_index == 0:
                    value_id = instruction_fact.third
                elif use_count == 2:
                    value_id = instruction_fact.fourth
                else:
                    overflow_start = -instruction_fact.fourth - 2
                    value_id = kernel.instruction_overflow_use_ids.get_unchecked(
                        overflow_start + use_index - 1
                    )
                value = kernel.value_name(value_id)
                definition = _definition_by_id(kernel, definitions, value_id)
                if definition.fourth == 0:
                    kind_name = PARSED_INSTRUCTION_KINDS[kind_id]
                    _fail(
                        "ssa-dominance",
                        func,
                        f"{block_name!r}/{kind_name} uses undefined value {value!r}",
                    )
                if not _definition_dominates_use(
                    definition, block_index, position, dominators
                ):
                    kind_name = PARSED_INSTRUCTION_KINDS[kind_id]
                    _fail(
                        "ssa-dominance",
                        func,
                        f"definition of {value!r} does not dominate "
                        f"{block_name!r}/{kind_name}",
                    )
                use_index += 1
            position += 1
        term_position = block_fact.second
        term_use_index = 0
        while term_use_index < block_fact.third:
            value_id = block_fact.fourth
            value = kernel.value_name(value_id)
            definition = _definition_by_id(kernel, definitions, value_id)
            if definition.fourth == 0:
                _fail(
                    "ssa-dominance",
                    func,
                    f"terminator in {block_name!r} uses undefined value {value!r}",
                )
            if not _definition_dominates_use(
                definition, block_index, term_position, dominators
            ):
                _fail(
                    "ssa-dominance",
                    func,
                    f"definition of {value!r} does not dominate terminator "
                    f"in {block_name!r}",
                )
            term_use_index += 1


def _verify_phis(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    predecessors: list[list[int]],
    dominators: tuple[list[int], list[int]],
    block_buckets: dict[int, list[int]],
) -> None:
    kernel = get_indexed_function_kernel(func)
    block_names = kernel.block_names
    for block_index, block_name in enumerate(block_names):
        expected = set(predecessors[block_index])
        phi_fact: CompilerInt2 = kernel.block_phi_fact(block_index)
        expected_counts: dict[int, int] = {}
        if phi_fact.second:
            # Dominance uses a graph of unique predecessor blocks, whereas a
            # PHI has one incoming per CFG edge. A switch (or a conditional
            # branch with equal targets) can contribute several such edges.
            for pred_index in predecessors[block_index]:
                edge_count = 0
                successor_index = 0
                successor_count = kernel.cfg_successor_count(pred_index)
                while successor_index < successor_count:
                    if kernel.cfg_successor_id(pred_index, successor_index) == block_index:
                        edge_count += 1
                    successor_index += 1
                expected_counts[pred_index] = edge_count
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(
                phi_fact.first + phi_index
            )
            phi_name = kernel.value_name(phi.first)
            if not expected:
                _fail(
                    "phi-predecessors",
                    func,
                    f"phi {phi_name!r} appears in predecessor-free block {block_name!r}",
                )
            actual: set[int] = set()
            actual_counts: dict[int, int] = {}
            first_values: dict[int, int] = {}
            incoming_index = 0
            while incoming_index < phi.fourth:
                incoming: CompilerInt2 = kernel.phi_incoming(
                    phi.third + incoming_index
                )
                pred_index = incoming.second
                incoming_label = (
                    "<invalid>"
                    if pred_index < 0
                    else kernel.block_names[pred_index]
                )
                if pred_index < 0 or pred_index not in expected:
                    _fail(
                        "phi-predecessors",
                        func,
                        f"phi {phi_name!r} in {block_name!r} names non-predecessor "
                        f"{incoming_label!r}",
                    )
                actual_count = actual_counts.get(pred_index, 0) + 1
                if actual_count > expected_counts[pred_index]:
                    _fail(
                        "phi-predecessors",
                        func,
                        f"phi {phi_name!r} in {block_name!r} has excess edges from "
                        f"{incoming_label!r}",
                    )
                if pred_index in first_values and first_values[pred_index] != incoming.first:
                    _fail(
                        "phi-predecessors",
                        func,
                        f"phi {phi_name!r} in {block_name!r} has different values "
                        f"for parallel edges from {incoming_label!r}",
                    )
                first_values[pred_index] = incoming.first
                actual_counts[pred_index] = actual_count
                actual.add(pred_index)
                if incoming.first < 0:
                    incoming_index += 1
                    continue
                incoming_value = kernel.value_name(incoming.first)
                definition = _definition_by_id(
                    kernel, definitions, incoming.first
                )
                if definition.fourth == 0:
                    _fail(
                        "ssa-dominance",
                        func,
                        f"phi {phi_name!r} uses undefined value {incoming_value!r}",
                    )
                if not _type_ids_match(
                    kernel,
                    definition.first,
                    phi.second,
                ):
                    _fail(
                        "operand-type",
                        func,
                        f"phi {phi_name!r} expects "
                        f"{kernel.type_desc(phi.second).describe()} for "
                        f"{incoming_value!r}, got "
                        + kernel.type_desc(definition.first).describe(),
                    )
                # A PHI use occurs on the incoming predecessor edge.  A local
                # definition in that predecessor is before its terminator;
                # otherwise the defining block must dominate the predecessor.
                if definition.second >= 0 and not (
                    definition.second == pred_index
                    or _block_dominates(
                        dominators, definition.second, pred_index
                    )
                ):
                    _fail(
                        "ssa-dominance",
                        func,
                        f"definition of {incoming_value!r} does not dominate "
                        f"phi edge {incoming_label!r} -> {block_name!r}",
                    )
                incoming_index += 1
            if actual != expected:
                missing = [
                    block_names[index] for index in sorted(expected - actual)
                ]
                _fail(
                    "phi-predecessors",
                    func,
                    f"phi {phi_name!r} in {block_name!r} is missing "
                    f"predecessors {missing!r}",
                )
            if actual_counts != expected_counts:
                _fail(
                    "phi-predecessors",
                    func,
                    f"phi {phi_name!r} in {block_name!r} has missing parallel edges",
                )
            phi_index += 1


def _verify_terminator_types(
    func: ParsedFunction,
    definitions: IndexedFunctionKernel,
    kernel: IndexedFunctionKernel,
) -> None:
    for block_id, block_name in enumerate(kernel.block_names):
        header: CompilerInt4 = kernel.terminator_header(block_id)
        span: CompilerInt4 = kernel.terminator_span(block_id)
        kind_id = header.first
        context = f"terminator in {block_name!r}"
        if kind_id == PARSED_INSTRUCTION_KIND_RET_VOID:
            if not func.ret_type.is_void:
                _fail("terminator", func, f"{context} returns void from non-void function")
        elif kind_id == PARSED_INSTRUCTION_KIND_RET:
            expected_type_id = kernel.intern_type(func.ret_type)
            if not _type_ids_match(kernel, header.second, expected_type_id):
                _fail(
                    "terminator",
                    func,
                    f"{context} return type does not match function return type",
                )
            _require_local_type_ref(
                func,
                definitions,
                kernel,
                header.third,
                header.second,
                context=context,
            )
        elif kind_id == PARSED_INSTRUCTION_KIND_BR_COND:
            _require_local_type_ref(
                func,
                definitions,
                kernel,
                header.third,
                kernel.intern_type(I1),
                context=context,
            )
        elif kind_id == PARSED_INSTRUCTION_KIND_SWITCH:
            _require_local_type_ref(
                func,
                definitions,
                kernel,
                header.third,
                header.second,
                context=context,
            )
            seen_values: set[int] = set()
            case_index = 0
            while case_index < span.third:
                case: CompilerInt2 = kernel.terminator_case(
                    span.second + case_index
                )
                case_value = case.first
                if case_value in seen_values:
                    _fail(
                        "terminator",
                        func,
                        f"{context} repeats switch case {case_value}",
                    )
                seen_values.add(case_value)
                case_index += 1
        elif kind_id in (
            PARSED_INSTRUCTION_KIND_BR,
            PARSED_INSTRUCTION_KIND_UNREACHABLE,
        ):
            continue
        else:
            kind = PARSED_INSTRUCTION_KINDS[kind_id]
            _fail("terminator", func, f"{context} has unknown kind {kind!r}")


def verify_parsed_function(func: ParsedFunction) -> None:
    kernel = get_indexed_function_kernel(func)
    if not kernel.block_names:
        _fail("terminator", func, "function has no basic blocks")
    _verify_inline_error_edge_shape(func, kernel)
    block_buckets, predecessors, successors = _build_cfg(func, kernel)
    # CFG target diagnostics have now consumed the only information that may
    # need the original terminator spelling.  From here onward verifier,
    # stackprep and both indexed target paths use the authoritative kernel
    # records, so release scalar PHI/terminator compatibility projections at
    # their last semantic use instead of retaining them until stackprep.
    kernel.release_scalar_phi_projections(func)
    kernel.release_terminator_projections(func)
    dominators = _compute_dominators(predecessors, successors)
    definitions = kernel
    _build_definitions(func, definitions)
    _verify_inline_error_edges(func, kernel, definitions, dominators)
    _verify_phis(
        func,
        definitions,
        predecessors,
        dominators,
        block_buckets,
    )
    _verify_ordinary_uses(func, definitions, dominators)
    _verify_terminator_types(func, definitions, kernel)


def _verify_inline_error_edge_shape(func: ParsedFunction, kernel) -> None:
    count = len(kernel.error_edge_scalars)
    if count % INLINE_ERROR_EDGE_WIDTH != 0:
        _fail("terminator", func, "inline error-edge plane is truncated")
    edge_total = count // INLINE_ERROR_EDGE_WIDTH
    expected_start = 0
    block_id = 0
    while block_id < len(kernel.block_names):
        span: CompilerInt2 = kernel.inline_error_edge_span(block_id)
        if span.first != expected_start or span.second < 0:
            _fail("terminator", func, "inline error-edge spans are inconsistent")
        edge_offset = 0
        previous_trigger = -1
        while edge_offset < span.second:
            edge_id = span.first + edge_offset
            if edge_id < 0 or edge_id >= edge_total:
                _fail("terminator", func, "inline error-edge span exceeds plane")
            source_block = kernel.inline_error_edge_source_block(edge_id)
            trigger = kernel.inline_error_edge_trigger(edge_id)
            condition_value = kernel.inline_error_edge_condition(edge_id)
            error_block = kernel.inline_error_edge_target(edge_id)
            source_line = kernel.inline_error_edge_source_line(edge_id)
            cleanup_plan = kernel.inline_error_edge_cleanup_plan(edge_id)
            if source_block != block_id:
                _fail("terminator", func, "inline error edge has the wrong source")
            if (
                trigger < 0
                or trigger >= kernel.instruction_count(block_id)
                or trigger <= previous_trigger
            ):
                _fail("terminator", func, "inline error edge has an invalid trigger")
            if error_block < 0 or error_block >= len(kernel.block_names):
                _fail("terminator", func, "inline error edge has an invalid target")
            if condition_value < 0 or condition_value >= len(kernel.value_names):
                _fail("terminator", func, "inline error edge has an invalid condition")
            if source_line < 0 or cleanup_plan != 0:
                _fail("terminator", func, "inline error edge has an unsupported plan")
            payload = kernel.inline_error_edge_payload(edge_id)
            landing_slot = kernel.inline_error_landing_slot(error_block)
            if landing_slot >= 0:
                if payload < 0:
                    _fail(
                        "terminator",
                        func,
                        "inline error edge into frame landing "
                        + repr(kernel.block_names[error_block])
                        + " carries no payload",
                    )
                if landing_slot >= len(kernel.value_names):
                    _fail("terminator", func, "inline error landing slot is invalid")
            elif payload != -1:
                _fail(
                    "terminator",
                    func,
                    "inline error edge carries a payload into "
                    + repr(kernel.block_names[error_block])
                    + ", which is not a frame landing",
                )
            previous_trigger = trigger
            edge_offset += 1
        expected_start += span.second
        block_id += 1
    if expected_start != edge_total:
        _fail("terminator", func, "inline error-edge spans do not cover plane")


def _verify_inline_error_edges(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    definitions: IndexedFunctionKernel,
    dominators: tuple[list[int], list[int]],
) -> None:
    edge_total = len(kernel.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH
    edge_id = 0
    while edge_id < edge_total:
        source_block = kernel.inline_error_edge_source_block(edge_id)
        trigger = kernel.inline_error_edge_trigger(edge_id)
        condition_value = kernel.inline_error_edge_condition(edge_id)
        error_block = kernel.inline_error_edge_target(edge_id)
        context = (
            "inline error edge in "
            + repr(kernel.block_names[source_block])
            + " after instruction "
            + str(trigger)
        )
        _require_local_type_ref(
            func,
            definitions,
            kernel,
            condition_value,
            kernel.intern_type(I1),
            context=context,
        )
        definition = _definition_by_id(kernel, definitions, condition_value)
        if not _definition_dominates_use(
            definition,
            source_block,
            trigger + 1,
            dominators,
        ):
            _fail(
                "ssa-dominance",
                func,
                "definition of "
                + repr(kernel.value_name(condition_value))
                + " does not dominate "
                + context,
            )
        target_phi: CompilerInt2 = kernel.block_phi_fact(error_block)
        if target_phi.second:
            _fail(
                "phi-predecessors",
                func,
                "inline error-edge target contains unsupported PHI records",
            )
        edge_id += 1


def verify_parsed_module(module: ParsedModule) -> None:
    for func in module.functions:
        verify_parsed_function(func)


__all__ = ["verify_parsed_function", "verify_parsed_module"]
