from __future__ import annotations

import os
import sys

"""Asm-first self backend bootstrap for AArch64 Darwin.

This backend consumes current LLVM IR text as a bootstrap input and lowers a
bounded but growing truthful subset to native AArch64 Darwin assembly.

Supported slice today:
- scalar integer types (`i1`, `i8`, `i16`, `i32`, `i64`)
- pointer scalars (`T*`, including pointer args/returns/local slots)
- `void` functions / calls / returns
- local `alloca`, `load`, `store`
- direct calls
- integer arithmetic / compares / branches / phi / simple loops
- scalar casts: `zext`, `sext`, `trunc`, `bitcast`, `ptrtoint`, `inttoptr`

Unsupported shapes still raise ``BackendUnavailable`` instead of guessing.
"""
from . import BackendUnavailable
from .arm64_asm_driver import AArch64ModuleBuilder, StructuredAArch64Module
from .arm64_encode import (
    EMITTED_INSTRUCTION_CALL,
    EMITTED_INSTRUCTION_FALLBACK,
    EMITTED_INSTRUCTION_MOVE,
    EMITTED_INSTRUCTION_SCALAR,
    EMITTED_INSTRUCTION_UNSCALED,
    STRUCTURED_FIXUP_CALL,
    append_emitted_instruction_record,
    intern_emitted_symbol,
)
from .code_profile import apply_function_order_profile
from .self_backend_aarch64_darwin_abi import (
    aggregate_returned_indirect as _aggregate_returned_indirect,
    aggregate_returned_indirect_indexed as _aggregate_returned_indirect_indexed,
)
from .self_backend_aarch64_darwin_compute import (
    emit_compute_instruction_by_id as _compute_emit_instruction,
)
from .self_backend_aarch64_darwin_data import emit_globals as _emit_globals
from .self_backend_aarch64_darwin_flow import (
    plan_aarch64_canonical_error_fallthroughs,
)
from .self_backend_aarch64_darwin_memory import (
    adjacent_gep_memory_address,
    emit_memory_instruction_by_id as _memory_emit_instruction,
)
from .self_backend_aarch64_darwin_mem import (
    DIRECT_INSTRUCTION_PLACEHOLDER,
    begin_direct_instruction_capture,
    borrow_direct_instruction_records,
    borrow_direct_instruction_symbol_names,
    end_direct_instruction_capture,
    direct_instruction_capture_active,
    require_direct_instruction_capture_idle,
)
from .self_backend_aarch64_darwin_symbols import (
    asm_symbol as _asm_symbol,
    asm_symbol_prevalidated,
    block_label as _block_label,
)
from .self_backend_aarch64_darwin_prologue import (
    emit_function_prologue as _prologue_emit_function_prologue,
)
from .self_backend_aarch64_darwin_tail_calls import (
    aarch64_tail_call_id_for_block,
    plan_aarch64_tail_calls,
)
from .self_backend_aarch64_darwin_terminators import (
    emit_branch_terminator as _terms_emit_branch_terminator,
    emit_cond_branch_terminator as _terms_emit_cond_branch_terminator,
    emit_cond_branch_terminator_indexed as _terms_emit_cond_branch_terminator_indexed,
    emit_epilogue as _terms_emit_epilogue,
    emit_tail_epilogue as _terms_emit_tail_epilogue,
    emit_inline_error_edge_indexed as _terms_emit_inline_error_edge_indexed,
    emit_inline_error_stub_indexed as _terms_emit_inline_error_stub_indexed,
    emit_switch_terminator as _terms_emit_switch_terminator,
    emit_switch_terminator_indexed as _terms_emit_switch_terminator_indexed,
    emit_unreachable_terminator as _terms_emit_unreachable_terminator,
)
from .self_backend_aarch64_darwin_returns import (
    emit_return_terminator as _rets_emit_return_terminator,
    emit_return_terminator_indexed as _rets_emit_return_terminator_indexed,
)
from .self_backend_emit import emit_function_blocks, emit_indexed_function_blocks
from .self_backend_instruction_dispatch import (
    emit_instruction_dispatch_parts,
)
from .self_backend_kernel import IndexedFunctionKernel, get_indexed_function_kernel
from .self_backend_ir import (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_ATOMICRMW,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_CMPXCHG,
    PARSED_INSTRUCTION_KIND_FENCE,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_STORE_ATOMIC,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
    PARSED_INSTRUCTION_KINDS,
    ParsedFunction,
    ParsedInstr,
    _PARSED_INSTRUCTION_KIND_IDS,
)
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_prepare import (
    PreparedSelfBackendModule,
    prepare_module_for_target,
    prepare_parsed_module_for_target,
)
from .self_backend_precise_stackmaps import (
    FunctionStackMapPlan,
    build_aarch64_stack_map_section,
    build_function_stack_map_plan,
    build_stack_map_plans,
    render_aarch64_stack_map_section,
)
from .self_backend_target_passes import (
    AARCH64_MEMORY_PAIR_BARRIER_BEGIN,
    AARCH64_MEMORY_PAIR_BARRIER_END,
    advance_aarch64_memory_pair_barrier,
    pair_adjacent_aarch64_64bit_memory_ops,
    plan_aarch64_madd_fusions,
    require_closed_aarch64_memory_pair_barrier,
    run_self_target_memory_pass_pipeline,
)
from .self_backend_target_match import is_aarch64_darwin_triple
from .self_backend_terminator_dispatch import emit_terminator_dispatch
from .self_backend_value_arena import CompilerInt2, CompilerInt4, CompilerIntArena
from .self_backend_aarch64_fragments import (
    AArch64EmissionFragments,
    EMISSION_RECORD_LABEL,
)

_MODULE_SYMBOLS = PreparedModuleSymbols(
    internal_prefix="",
    defined_symbols=frozenset(),
    internal_symbols=frozenset(),
)


class _NativeAArch64Emission:
    """One module emission scope over the canonical incremental assembler.

    Producer helpers still return per-function/fragments lists in this slice.
    This owner consumes them immediately, reusing the captured word arena per
    function and retaining no module instruction slots or coordinate remaps.
    """

    def __init__(self) -> None:
        self.symbol_names: list[str] = []
        self.symbol_ids: dict[str, int] = {}
        self.builder = AArch64ModuleBuilder(self.symbol_names)
        self.direct_records: CompilerIntArena = borrow_direct_instruction_records()
        self.direct_symbols: list[str] = borrow_direct_instruction_symbol_names()
        try:
            self.scratch: CompilerIntArena = CompilerIntArena()
            try:
                self.fragments: AArch64EmissionFragments = AArch64EmissionFragments()
            except Exception:
                self.scratch.close()
                raise
        except Exception:
            self.builder.close()
            raise
        self.direct_index: int = 0
        self.structured_count: int = 0
        self.direct_count: int = 0
        self.unscaled_count: int = 0
        self.move_count: int = 0
        self.call_count: int = 0
        self.barrier_depth: int = 0
        self.fragment_record_count: int = 0
        self.fallback_lines: list[str] = []

    def close(self) -> None:
        self.builder.close()
        self.scratch.close()
        self.fragments.close()

    def publish_fragment(self, fragment: CompilerInt2) -> None:
        fragments: AArch64EmissionFragments = self.fragments
        records: CompilerIntArena = fragments.records
        fragments.start_cursor(fragment)
        record_count = len(records) // 4
        record_id = fragments.next_record_id()
        while record_id >= 0:
            if record_id >= record_count:
                raise BackendUnavailable("emission fragment record ID is invalid")
            record: CompilerInt4 = records.get4_unchecked(record_id)
            family = record.second
            if family == EMISSION_RECORD_LABEL:
                if record.first < 0 or record.first >= len(fragments.symbol_names):
                    raise BackendUnavailable("emission fragment label ID is invalid")
                self.builder.append_label(fragments.symbol_names[record.first])
            else:
                if record.third != 0 or record.fourth != -1:
                    raise BackendUnavailable("emission fragment relocation is unsupported")
                if family not in (
                    EMITTED_INSTRUCTION_SCALAR,
                    EMITTED_INSTRUCTION_MOVE,
                    EMITTED_INSTRUCTION_UNSCALED,
                ):
                    raise BackendUnavailable("emission fragment instruction family is invalid")
                self.builder.append_encoded(record.first, 0, -1)
                self.structured_count += 1
                self.direct_count += 1
                if family == EMITTED_INSTRUCTION_MOVE:
                    self.move_count += 1
                elif family == EMITTED_INSTRUCTION_UNSCALED:
                    self.unscaled_count += 1
            self.fragment_record_count += 1
            record_id = fragments.next_record_id()

    def extend(self, lines: list[str]) -> None:
        for raw in lines:
            if "\n" in raw or "\r" in raw:
                for line in raw.splitlines():
                    self.append(line)
            else:
                self.append(raw)

    def release_captured_function(self) -> None:
        require_closed_aarch64_memory_pair_barrier(self.barrier_depth)
        if self.direct_index * 4 != len(self.direct_records):
            raise BackendUnavailable("direct instruction transport has trailing records")
        self.direct_records.clear()
        self.direct_index = 0
        self.fragments.reset()

    def append(self, line: str) -> None:
        family = EMITTED_INSTRUCTION_FALLBACK
        if line == DIRECT_INSTRUCTION_PLACEHOLDER:
            if self.direct_index * 4 >= len(self.direct_records):
                raise BackendUnavailable("direct instruction transport is truncated")
            record: CompilerInt4 = self.direct_records.get4_unchecked(self.direct_index)
            self.direct_index += 1
            self.direct_count += 1
            word = record.first
            family = record.second
            kind = record.third
            symbol_id = record.fourth
            if family == EMITTED_INSTRUCTION_CALL:
                word = 0x94000000
                kind = STRUCTURED_FIXUP_CALL
            elif kind < 0 and kind not in (-26, -19):
                raise BackendUnavailable("direct instruction relocation kind is invalid")
            if kind != 0:
                if symbol_id < 0 or symbol_id >= len(self.direct_symbols):
                    raise BackendUnavailable("direct instruction relocation symbol is invalid")
                symbol_id = intern_emitted_symbol(
                    self.direct_symbols[symbol_id], self.symbol_ids, self.symbol_names,
                )
            self.builder.append_encoded(word, kind, symbol_id)
        else:
            if line == AARCH64_MEMORY_PAIR_BARRIER_BEGIN:
                self.barrier_depth = advance_aarch64_memory_pair_barrier(
                    self.barrier_depth, True,
                )
                return
            if line == AARCH64_MEMORY_PAIR_BARRIER_END:
                self.barrier_depth = advance_aarch64_memory_pair_barrier(
                    self.barrier_depth, False,
                )
                return
            stripped = line.strip()
            current = self.builder.current
            if (
                current is None or not current.is_text or not stripped
                or stripped.startswith(".") or stripped.endswith(":")
            ):
                self.builder.append_chunk(line)
                return
            self.scratch.clear()
            family = append_emitted_instruction_record(
                line, 0, 0, -1, None, self.scratch,
                self.symbol_ids, self.symbol_names,
            )
            if family == EMITTED_INSTRUCTION_FALLBACK:
                self.fallback_lines.append(line)
                self.builder.append_chunk(line)
                return
            if len(self.scratch) != 4:
                raise BackendUnavailable("emitted instruction must publish one word")
            encoded: CompilerInt4 = self.scratch.get4_unchecked(0)
            self.builder.append_encoded(encoded.second, encoded.third, encoded.fourth)
        self.structured_count += 1
        if family == EMITTED_INSTRUCTION_UNSCALED:
            self.unscaled_count += 1
        elif family == EMITTED_INSTRUCTION_MOVE:
            self.move_count += 1
        elif family == EMITTED_INSTRUCTION_CALL:
            self.call_count += 1
        elif family != EMITTED_INSTRUCTION_SCALAR:
            raise BackendUnavailable("direct memory instruction family is invalid")


def _function_symbol(name: str) -> str:
    """Resolve one function symbol without a non-self-hostable lambda."""
    return _asm_symbol(name, _MODULE_SYMBOLS)

# SDK mach-o/compact_unwind_encoding.h: standard FP/LR frame, no saved pairs.
_UNWIND_ARM64_MODE_FRAME = 0x04000000

_MEMORY_PAIR_BARRIER_KINDS = (
    "load_atomic",
    "store_atomic",
    "atomicrmw",
    "cmpxchg",
    "fence",
)

_MEMORY_PAIR_BARRIER_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    PARSED_INSTRUCTION_KIND_STORE_ATOMIC,
    PARSED_INSTRUCTION_KIND_ATOMICRMW,
    PARSED_INSTRUCTION_KIND_CMPXCHG,
    PARSED_INSTRUCTION_KIND_FENCE,
)

_INDEXED_FIXED_PAYLOAD_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_GEP,
)


def _append_compact_unwind(
    lines: list[str],
    functions: list[ParsedFunction],
    module_symbols: PreparedModuleSymbols,
    target_offsets: dict[str, int] | None = None,
    target_text_size: int = 0,
) -> list[str]:
    """Append one exact frame-mode compact-unwind row per function.

    Every function emitted by this backend immediately saves FP/LR and makes
    x29 the frame pointer.  Function sizes are measured from the final text,
    after the peephole passes, so removed instructions cannot leave stale
    unwind ranges.
    """
    if not functions:
        return lines

    symbols = [_asm_symbol(func.name, module_symbols) for func in functions]
    wanted = set(symbols)
    offsets: dict[str, int] = {} if target_offsets is None else target_offsets
    text_offset = target_text_size
    in_text = False
    for raw in lines:
        line = raw.strip()
        if line.startswith(".section "):
            section = line[len(".section ") :].strip()
            in_text = section.startswith("__TEXT,__text,")
            continue
        if not in_text or not line:
            continue
        if line.endswith(":"):
            label = line[:-1].strip()
            if label in wanted:
                if label in offsets:
                    raise BackendUnavailable(
                        f"duplicate self-backend function label {label!r}"
                    )
                offsets[label] = text_offset
            continue
        if line.startswith("."):
            continue
        text_offset += 4

    missing = [symbol for symbol in symbols if symbol not in offsets]
    if missing:
        raise BackendUnavailable(
            "self backend could not locate function labels for compact "
            f"unwind: {missing!r}"
        )

    starts = sorted((offsets[symbol], symbol) for symbol in symbols)
    result = list(lines)
    result.append(".section __LD,__compact_unwind,regular,debug")
    result.append(".p2align 3")
    for index, (start, symbol) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else text_offset
        length = end - start
        if length <= 0 or length % 4 != 0 or length > 0xFFFFFFFF:
            raise BackendUnavailable(
                f"bad compact-unwind range for {symbol!r}: {start}..{end}"
            )
        result.append(f"  .quad {symbol}")
        result.append(f"  .long {length}")
        result.append(f"  .long {_UNWIND_ARM64_MODE_FRAME}")
        result.append("  .quad 0")
        result.append("  .quad 0")
    return result


def _emit_trace(message: str) -> None:
    """Coarse phase marker for diagnosing self-compiled backend failures.

    pcc1 raises runtime-generated exceptions with no message, so a failing
    stage inside the emitter is otherwise invisible: the only surface is
    `PCC-PY-COMPILE-001 ... exception_type=Exception`.  Gated on
    PCC_DEBUG_SELF_BACKEND_TRACE so it costs one env probe otherwise.
    """
    if os.environ.get("PCC_DEBUG_SELF_BACKEND_TRACE"):
        sys.stderr.write("[self.emit] " + message + "\n")


def emit_aarch64_darwin_asm(ir_text: str, optimize: bool = True) -> str:
    # ``ir_text`` is a borrowed function parameter.  The self-compiled path
    # forwards it through the prepare/parser stack.  Keep this wrapper short:
    # the prepared module crosses the next call as an owned return value, while
    # the large source string no longer shares a frame with every emit pass.
    owned_ir_text = ir_text + ""
    _emit_trace("prepare begin bytes=" + str(len(owned_ir_text)))
    prepared = prepare_module_for_target(
        owned_ir_text,
        aggregate_returned_indirect=_aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=(
            _aggregate_returned_indirect_indexed
        ),
        materialize_legacy_slots=False,
    )
    _emit_trace("prepare end")
    return _emit_prepared_aarch64_darwin_module(
        prepared,
        optimize,
        profile_ir_text=owned_ir_text,
    )


def emit_aarch64_darwin_indexed_module(module, optimize: bool = True) -> str:
    """Emit an already-indexed module through the ordinary verified backend."""
    prepared = prepare_parsed_module_for_target(
        module,
        aggregate_returned_indirect=_aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=(
            _aggregate_returned_indirect_indexed
        ),
        materialize_legacy_slots=False,
    )
    return _emit_prepared_aarch64_darwin_module(
        prepared,
        optimize,
        profile_ir_text="",
    )


def emit_aarch64_darwin_indexed_lines(
    module,
    optimize: bool = True,
) -> list[str]:
    """Emit the indexed module without materializing a joined text string."""

    prepared = prepare_parsed_module_for_target(
        module,
        aggregate_returned_indirect=_aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=(
            _aggregate_returned_indirect_indexed
        ),
        materialize_legacy_slots=False,
    )
    return _emit_prepared_aarch64_darwin_lines(
        prepared,
        optimize,
        profile_ir_text="",
    )


def emit_aarch64_darwin_indexed_transport(
    module,
    optimize: bool = True,
    structured_instructions: bool = True,
) -> StructuredAArch64Module:
    """Emit indexed code plus final structured section payloads."""

    prepared = prepare_parsed_module_for_target(
        module,
        aggregate_returned_indirect=_aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=(
            _aggregate_returned_indirect_indexed
        ),
        materialize_legacy_slots=False,
    )
    structured_sections = []
    native_text = structured_instructions and not optimize
    encoded_line_records = None if native_text else CompilerIntArena()
    structured_symbol_names: list[str] = []
    structured_counts: list[int] = []
    native_undefined: list[str] = []
    native_fallback_lines: list[str] = []
    lines = _emit_prepared_aarch64_darwin_lines(
        prepared,
        optimize,
        profile_ir_text="",
        structured_sections=structured_sections,
        encoded_line_records=(
            encoded_line_records if structured_instructions else None
        ),
        structured_symbol_names=(
            structured_symbol_names if structured_instructions else None
        ),
        structured_counts=(
            structured_counts if structured_instructions else None
        ),
        native_undefined=native_undefined if native_text else None,
        native_fallback_lines=native_fallback_lines if native_text else None,
    )
    if not structured_instructions:
        structured_counts = [0, 0, 0, 0, 0, 0, 0]
    elif len(structured_counts) != 7:
        raise BackendUnavailable("structured instruction inventory is missing")
    return StructuredAArch64Module(
        lines,
        tuple(structured_sections),
        encoded_line_records,
        tuple(structured_symbol_names),
        structured_counts[0],
        structured_counts[1],
        structured_counts[2],
        structured_counts[3],
        structured_counts[4],
        structured_counts[5],
        native_text,
        tuple(native_undefined),
        tuple(native_fallback_lines),
        structured_counts[6],
    )


_AARCH64_EMISSION_ACTIVE = False


def _emit_prepared_aarch64_darwin_lines(
    prepared: PreparedSelfBackendModule,
    optimize: bool = True,
    *,
    profile_ir_text: str = "",
    close_native_tables: bool = True,
    structured_sections=None,
    encoded_line_records: CompilerIntArena | None = None,
    structured_symbol_names: list[str] | None = None,
    structured_counts: list[int] | None = None,
    native_undefined: list[str] | None = None,
    native_fallback_lines: list[str] | None = None,
) -> list[str]:
    global _AARCH64_EMISSION_ACTIVE
    direct_instruction_capture = (
        encoded_line_records is not None or native_undefined is not None
    ) and not optimize
    # A nested entry must fail before it can close or contaminate its caller's
    # arena. The ordinary ASM path must not inherit a structured caller either.
    if _AARCH64_EMISSION_ACTIVE:
        raise BackendUnavailable("AArch64 emission is already active")
    _AARCH64_EMISSION_ACTIVE = True
    native_sink: _NativeAArch64Emission | None = None
    try:
        if direct_instruction_capture:
            begin_direct_instruction_capture()
        else:
            require_direct_instruction_capture_idle()
        try:
            if native_undefined is not None:
                native_sink = _NativeAArch64Emission()
            return _emit_prepared_aarch64_darwin_lines_active(
                prepared,
                optimize,
                profile_ir_text=profile_ir_text,
                close_native_tables=close_native_tables,
                structured_sections=structured_sections,
                encoded_line_records=encoded_line_records,
                structured_symbol_names=structured_symbol_names,
                structured_counts=structured_counts,
                native_sink=native_sink,
                native_undefined=native_undefined,
                native_fallback_lines=native_fallback_lines,
            )
        finally:
            if native_sink is not None:
                native_sink.close()
            if direct_instruction_capture:
                end_direct_instruction_capture()
    finally:
        _AARCH64_EMISSION_ACTIVE = False


def _emit_prepared_aarch64_darwin_lines_active(
    prepared: PreparedSelfBackendModule,
    optimize: bool = True,
    *,
    profile_ir_text: str = "",
    close_native_tables: bool = True,
    structured_sections=None,
    encoded_line_records: CompilerIntArena | None = None,
    structured_symbol_names: list[str] | None = None,
    structured_counts: list[int] | None = None,
    native_sink: _NativeAArch64Emission | None = None,
    native_undefined: list[str] | None = None,
    native_fallback_lines: list[str] | None = None,
) -> list[str]:
    global _MODULE_SYMBOLS
    direct_instruction_capture = encoded_line_records is not None and not optimize
    triple = prepared.triple
    if triple != "unknown-unknown-unknown" and not is_aarch64_darwin_triple(triple):
        raise BackendUnavailable(
            f"self backend asm MVP only supports AArch64 Darwin, got {triple!r}"
        )
    if (
        str(os.environ.get("PCC_SELF_TARGET_PASS_TRANSPORT", "") or "").strip().lower()
        == "memory"
    ):
        prepared = run_self_target_memory_pass_pipeline(
            prepared,
            "self-aarch64-darwin-v0",
            raw_passes=None,
            raw_transport="memory",
        )

    globals_ = prepared.globals_
    _emit_trace("order profile begin")
    functions, _profile_decision = apply_function_order_profile(
        prepared.functions,
        ir_text=profile_ir_text,
        target=triple,
    )
    _MODULE_SYMBOLS = prepared.module_symbols
    # Reused parsed modules must start from complete ordinary-call plans;
    # eligibility is proved again for this target/optimization invocation.
    for func in functions:
        func.aarch64_tail_call_ids = []
    _emit_trace("stack map plans begin funcs=" + str(len(functions)))
    stack_map_plans = {
        plan.function_name: plan
        for plan in build_stack_map_plans(
            functions,
            globals_,
            target="aarch64-darwin",
            function_symbol=_function_symbol,
        )
    }

    _emit_trace("stack map plans end")
    lines = _emit_globals(globals_, _MODULE_SYMBOLS)
    if native_sink is not None:
        native_sink.extend(lines)
        lines = []
    _emit_trace("globals end")
    cold_fallthrough_edges: list[tuple[str, str, str]] = []
    if functions:
        if native_sink is None:
            lines.append(".section __TEXT,__text,regular,pure_instructions")
        else:
            native_sink.append(".section __TEXT,__text,regular,pure_instructions")
        for func in functions:
            # Plan before the prologue invokes block-local allocation.  The
            # allocator extends the delayed multiply operands through the
            # consumer and discards any plan whose stack fallback was reused.
            _emit_trace("func begin " + str(func.name))
            plan_aarch64_tail_calls(func, stack_map_plans[func.name], enabled=optimize)
            if func.aarch64_tail_call_ids:
                previous_plan = stack_map_plans[func.name]
                stack_map_plans[func.name] = build_function_stack_map_plan(
                    func, globals_, target="aarch64-darwin",
                    identity_name=_function_symbol(func.name),
                )
                if previous_plan.packed_records is not None:
                    previous_plan.packed_records.close()
            plan_aarch64_madd_fusions(func, enabled=optimize)
            plan_aarch64_canonical_error_fallthroughs(
                func,
                enabled=optimize,
            )
            for source_block, error_block, success_block in (
                func.aarch64_cold_fallthrough_edges
            ):
                cold_fallthrough_edges.append(
                    (
                        _block_label(func.name, source_block),
                        _block_label(func.name, error_block),
                        _block_label(func.name, success_block),
                    )
                )
            if native_sink is None:
                lines.extend(_emit_function(func, stack_map_plans[func.name]))
            else:
                _emit_function(func, stack_map_plans[func.name], native_sink=native_sink)
                native_sink.release_captured_function()
    if native_sink is not None:
        if (
            optimize or structured_sections is None or structured_counts is None
            or native_undefined is None or native_fallback_lines is None
        ):
            raise BackendUnavailable("native text emission sinks are incomplete")
        target_offsets = native_sink.builder.text_label_offsets()
        if functions:
            ordered_native_plans = tuple(stack_map_plans[func.name] for func in functions)
            structured_sections.append(build_aarch64_stack_map_section(
                [], ordered_native_plans,
                function_symbol=_function_symbol, block_label=_block_label,
                target_offsets=target_offsets,
            ))
            if close_native_tables:
                for func in functions:
                    get_indexed_function_kernel(func).close_native_tables()
        native_sink.extend(_append_compact_unwind(
            [], functions, _MODULE_SYMBOLS, target_offsets,
            native_sink.builder.text_size(),
        ))
        native_sink.append(".subsections_via_symbols")
        sections, undefined = native_sink.builder.finish(structured_sections)
        structured_sections.clear()
        structured_sections.extend(sections)
        native_undefined.extend(undefined)
        native_fallback_lines.extend(native_sink.fallback_lines)
        structured_counts.extend([
            native_sink.structured_count, len(native_sink.fallback_lines),
            native_sink.unscaled_count, native_sink.move_count,
            native_sink.call_count, native_sink.direct_count,
            native_sink.fragment_record_count,
        ])
        return []
    if optimize:
        lines = _forward_adjacent_stack_store_load(lines)
        lines = _forward_one_intervening_stack_store_load(lines)
        lines = _fold_zero_store_source(lines)
        lines = _fold_mov_store_source(lines)
        lines = _fold_zero_compare_immediate(lines)
        lines = _fold_mov_compare_source(lines)
        lines = _fold_mov_zero_branch_source(lines)
        lines = _fold_mov_arith_self_update(lines)
        lines = _fold_mov_mov_chain(lines)
        lines = _fold_zero_test_branch(lines)
        lines = _fold_forwarded_cset_branch(lines)
        lines = _fold_cset_zero_branch(lines)
        lines = _drop_dead_cset_branch_stores(lines)
        metadata_labels: set[str] = set()
        for func in functions:
            plan = stack_map_plans[func.name]
            packed = plan.packed_records
            if packed is not None:
                exceptional_blocks = packed.exceptional_blocks
            else:
                exceptional_blocks = [record.exceptional_block for record in plan.records]
            for exceptional_block in exceptional_blocks:
                if exceptional_block:
                    metadata_labels.add(_block_label(func.name, exceptional_block))
        lines = _thread_trampoline_branches(lines, metadata_labels)
        lines = _fold_cond_branch_to_fallthrough(
            lines,
            cold_fallthrough_edges,
        )
        lines = _drop_fallthrough_uncond_branches(lines)
        lines = _drop_unreferenced_empty_local_labels(lines, metadata_labels)
    # Run after register allocation and every instruction-deleting peephole,
    # but before compact-unwind sizing.  Even with optimization disabled this
    # finalizer removes the source-semantics barrier pseudo-directives.
    lines = pair_adjacent_aarch64_64bit_memory_ops(lines, enabled=optimize)
    if functions:
        ordered_stack_map_plans = tuple(
            stack_map_plans[func.name] for func in functions
        )
        if structured_sections is None:
            lines.extend(render_aarch64_stack_map_section(
                lines,
                ordered_stack_map_plans,
                function_symbol=_function_symbol,
                block_label=_block_label,
            ))
        else:
            structured_sections.append(build_aarch64_stack_map_section(
                lines,
                ordered_stack_map_plans,
                function_symbol=_function_symbol,
                block_label=_block_label,
            ))
        if close_native_tables:
            for func in functions:
                get_indexed_function_kernel(func).close_native_tables()
    lines = _append_compact_unwind(lines, functions, _MODULE_SYMBOLS)
    direct_instruction_records = None
    direct_instruction_symbols = None
    if direct_instruction_capture:
        direct_instruction_records = borrow_direct_instruction_records()
        direct_instruction_symbols = borrow_direct_instruction_symbol_names()
    if lines:
        lines.append(".subsections_via_symbols")
    if encoded_line_records is not None:
        if structured_symbol_names is None:
            raise BackendUnavailable("structured symbol-name sink is missing")
        structured_symbol_ids: dict[str, int] = {}
        structured_instruction_count = 0
        fallback_instruction_count = 0
        structured_unscaled_count = 0
        structured_move_count = 0
        structured_call_count = 0
        direct_instruction_index = 0
        in_text = False
        line_index = 0
        while line_index < len(lines):
            line = lines[line_index]
            if (
                direct_instruction_records is not None
                and line == DIRECT_INSTRUCTION_PLACEHOLDER
            ):
                if direct_instruction_index * 4 >= len(direct_instruction_records):
                    raise BackendUnavailable(
                        "direct instruction transport is truncated"
                    )
                direct_record = direct_instruction_records.get4_unchecked(
                    direct_instruction_index
                )
                direct_instruction_index += 1
                word = direct_record.first
                relocation_kind = direct_record.third
                final_symbol_id = direct_record.fourth
                if direct_record.second == EMITTED_INSTRUCTION_CALL:
                    word = 0x94000000
                    relocation_kind = STRUCTURED_FIXUP_CALL
                    structured_call_count += 1
                elif relocation_kind < 0 and relocation_kind not in (-26, -19):
                    raise BackendUnavailable(
                        "direct instruction relocation kind is invalid"
                    )
                if relocation_kind != 0:
                    if (
                        direct_instruction_symbols is None
                        or final_symbol_id < 0
                        or final_symbol_id >= len(direct_instruction_symbols)
                    ):
                        raise BackendUnavailable(
                            "direct instruction relocation symbol is invalid"
                        )
                    final_symbol_id = intern_emitted_symbol(
                        direct_instruction_symbols[final_symbol_id],
                        structured_symbol_ids,
                        structured_symbol_names,
                    )
                encoded_line_records.append4(
                    line_index, word, relocation_kind, final_symbol_id,
                )
                if direct_record.second == EMITTED_INSTRUCTION_UNSCALED:
                    structured_unscaled_count += 1
                elif direct_record.second == EMITTED_INSTRUCTION_MOVE:
                    structured_move_count += 1
                elif direct_record.second not in (
                    EMITTED_INSTRUCTION_CALL,
                    EMITTED_INSTRUCTION_SCALAR,
                ):
                    raise BackendUnavailable(
                        "direct memory instruction family is invalid"
                    )
                lines[line_index] = ""
                structured_instruction_count += 1
                line_index += 1
                continue
            stripped = line.strip()
            if stripped.startswith(".section "):
                in_text = stripped[len(".section "):].startswith(
                    "__TEXT,__text,"
                )
                line_index += 1
                continue
            if not in_text or not stripped:
                line_index += 1
                continue
            if stripped.endswith(":"):
                line_index += 1
                continue
            if stripped.startswith("."):
                line_index += 1
                continue

            instruction_family = append_emitted_instruction_record(
                line,
                line_index,
                0,
                -1,
                None,
                encoded_line_records,
                structured_symbol_ids,
                structured_symbol_names,
            )
            if instruction_family == EMITTED_INSTRUCTION_UNSCALED:
                structured_unscaled_count += 1
            elif instruction_family == EMITTED_INSTRUCTION_MOVE:
                structured_move_count += 1
            elif instruction_family == EMITTED_INSTRUCTION_CALL:
                structured_call_count += 1
            if instruction_family != EMITTED_INSTRUCTION_FALLBACK:
                lines[line_index] = ""
                structured_instruction_count += 1
            else:
                fallback_instruction_count += 1
            line_index += 1
        if (
            direct_instruction_records is not None
            and direct_instruction_index * 4 != len(direct_instruction_records)
        ):
            raise BackendUnavailable(
                "direct instruction transport has trailing records"
            )
        if structured_counts is None:
            raise BackendUnavailable(
                "structured instruction count sink is missing"
            )
        structured_counts.append(structured_instruction_count)
        structured_counts.append(fallback_instruction_count)
        structured_counts.append(structured_unscaled_count)
        structured_counts.append(structured_move_count)
        structured_counts.append(structured_call_count)
        structured_counts.append(direct_instruction_index)
        structured_counts.append(0)
    return lines


def _emit_prepared_aarch64_darwin_module(
    prepared: PreparedSelfBackendModule,
    optimize: bool = True,
    *,
    profile_ir_text: str = "",
    close_native_tables: bool = True,
) -> str:
    lines = _emit_prepared_aarch64_darwin_lines(
        prepared,
        optimize,
        profile_ir_text=profile_ir_text,
        close_native_tables=close_native_tables,
    )
    return "\n".join(lines) + "\n"


def _parse_stack_transfer(line: str, opcode: str) -> tuple[str, str] | None:
    prefix = f"  {opcode} "
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix) :]
    try:
        reg, addr = rest.split(", ", 1)
    except ValueError:
        return None
    if not addr.startswith("[x29, #-") or not addr.endswith("]"):
        return None
    return reg, addr[len("[x29, #-") : -1]


def _forward_move(dest_reg: str, src_reg: str) -> str | None:
    if dest_reg == src_reg:
        return ""
    if len(dest_reg) < 2 or len(src_reg) < 2 or dest_reg[0] != src_reg[0]:
        return None
    if dest_reg[0] in ("d", "s"):
        return f"  fmov {dest_reg}, {src_reg}"
    if dest_reg[0] in ("x", "w"):
        return f"  mov {dest_reg}, {src_reg}"
    return None


def _register_alias_key(reg: str) -> str:
    if len(reg) < 2:
        return reg
    prefix = reg[0]
    index = reg[1:]
    if prefix in ("w", "x"):
        return "gpr:" + index
    if prefix in ("s", "d"):
        return "fp:" + index
    return reg


def _forward_stack_load_move(
    load_opcode: str, dest_reg: str, src_reg: str
) -> str | None:
    if load_opcode == "ldurb":
        if not dest_reg.startswith("w") or not src_reg.startswith("w"):
            return None
        return f"  and {dest_reg}, {src_reg}, #0xff"
    return _forward_move(dest_reg, src_reg)


def _forward_adjacent_stack_store_load(lines: list[str]) -> list[str]:
    out: list[str] = []
    pairs = (("stur", "ldur"), ("sturb", "ldurb"))
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            for store_opcode, load_opcode in pairs:
                store = _parse_stack_transfer(lines[index], store_opcode)
                load = _parse_stack_transfer(lines[index + 1], load_opcode)
                if store is None or load is None:
                    continue
                store_reg, store_offset = store
                load_reg, load_offset = load
                move = _forward_stack_load_move(load_opcode, load_reg, store_reg)
                if store_offset == load_offset and move is not None:
                    out.append(lines[index])
                    if move:
                        out.append(move)
                    index += 2
                    break
            else:
                out.append(lines[index])
                index += 1
                continue
            continue
        out.append(lines[index])
        index += 1
    return out


def _parse_any_stack_load(line: str) -> tuple[str, str] | None:
    for opcode in ("ldur", "ldurb"):
        parsed = _parse_stack_transfer(line, opcode)
        if parsed is not None:
            return parsed
    return None


def _forward_one_intervening_stack_store_load(lines: list[str]) -> list[str]:
    out: list[str] = []
    pairs = (("stur", "ldur"), ("sturb", "ldurb"))
    index = 0
    while index < len(lines):
        if index + 2 < len(lines):
            middle_load = _parse_any_stack_load(lines[index + 1])
            if middle_load is not None:
                middle_reg, middle_offset = middle_load
                for store_opcode, load_opcode in pairs:
                    store = _parse_stack_transfer(lines[index], store_opcode)
                    load = _parse_stack_transfer(lines[index + 2], load_opcode)
                    if store is None or load is None:
                        continue
                    store_reg, store_offset = store
                    load_reg, load_offset = load
                    move = _forward_stack_load_move(load_opcode, load_reg, store_reg)
                    if (
                        store_offset == load_offset
                        and middle_offset != store_offset
                        and _register_alias_key(middle_reg)
                        != _register_alias_key(store_reg)
                        and move is not None
                    ):
                        out.append(lines[index])
                        out.append(lines[index + 1])
                        if move:
                            out.append(move)
                        index += 3
                        break
                else:
                    out.append(lines[index])
                    index += 1
                    continue
                continue
        out.append(lines[index])
        index += 1
    return out


def _parse_cset(line: str) -> tuple[str, str] | None:
    prefix = "  cset "
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix) :]
    reg, sep, cond = rest.partition(", ")
    if not sep or not reg.startswith("w"):
        return None
    return reg, cond


def _parse_and_byte_forward(line: str) -> tuple[str, str] | None:
    prefix = "  and "
    if not line.startswith(prefix) or not line.endswith(", #0xff"):
        return None
    rest = line[len(prefix) : -len(", #0xff")]
    try:
        dest_reg, src_reg = rest.split(", ", 1)
    except ValueError:
        return None
    if not dest_reg.startswith("w") or not src_reg.startswith("w"):
        return None
    return dest_reg, src_reg


def _parse_cond_zero_branch(line: str) -> tuple[str, str, str] | None:
    if line.startswith("  cbz "):
        opcode = "cbz"
        rest = line[len("  cbz ") :]
    elif line.startswith("  cbnz "):
        opcode = "cbnz"
        rest = line[len("  cbnz ") :]
    else:
        return None
    try:
        reg, target = rest.split(", ", 1)
    except ValueError:
        return None
    if not reg.startswith("w"):
        return None
    return opcode, reg, target


def _fold_forwarded_cset_branch(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 3 < len(lines):
            cset = _parse_cset(lines[index])
            store = _parse_stack_transfer(lines[index + 1], "sturb")
            forwarded = _parse_and_byte_forward(lines[index + 2])
            branch = _parse_cond_zero_branch(lines[index + 3])
            if (
                cset is not None
                and store is not None
                and forwarded is not None
                and branch is not None
            ):
                cset_reg, _cset_cond = cset
                store_reg, _store_offset = store
                forwarded_reg, forwarded_src = forwarded
                opcode, branch_reg, branch_target = branch
                if (
                    store_reg == cset_reg
                    and forwarded_src == cset_reg
                    and branch_reg == forwarded_reg
                ):
                    out.append(lines[index])
                    out.append(lines[index + 1])
                    out.append(f"  {opcode} {cset_reg}, {branch_target}")
                    index += 4
                    continue
        out.append(lines[index])
        index += 1
    return out


def _parse_zero_movz(line: str) -> str | None:
    prefix = "  movz "
    if not line.startswith(prefix):
        return None
    parts = line[len(prefix) :].split(", ")
    if len(parts) not in (2, 3):
        return None
    reg = parts[0]
    if not reg.startswith(("w", "x")):
        return None
    if parts[1] != "#0":
        return None
    if len(parts) == 3 and parts[2] != "lsl #0":
        return None
    return reg


def _is_aarch64_scratch_reg(reg: str) -> bool:
    if len(reg) < 2 or reg[0] not in ("w", "x"):
        return False
    try:
        index = int(reg[1:])
    except ValueError:
        return False
    return 9 <= index <= 15


def _zero_reg_for(reg: str) -> str | None:
    if reg.startswith("w"):
        return "wzr"
    if reg.startswith("x"):
        return "xzr"
    return None


def _parse_store_source(line: str) -> tuple[str, str, str] | None:
    stripped = line[2:] if line.startswith("  ") else ""
    for opcode in ("stur", "str", "sturb", "strb"):
        prefix = f"{opcode} "
        if not stripped.startswith(prefix):
            continue
        rest = stripped[len(prefix) :]
        try:
            reg, addr = rest.split(", ", 1)
        except ValueError:
            return None
        return opcode, reg, addr
    return None


def _replace_store_source(line: str, source_reg: str) -> str | None:
    parsed = _parse_store_source(line)
    if parsed is None:
        return None
    opcode, _old_reg, addr = parsed
    return f"  {opcode} {source_reg}, {addr}"


def _tokens_for_reg_scan(text: str) -> list[str]:
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    return "".join(cleaned).split()


def _reg_aliases(reg: str) -> tuple[str, ...]:
    if len(reg) < 2 or reg[0] not in ("w", "x"):
        return (reg,)
    number = reg[1:]
    if not number.isdigit():
        return (reg,)
    if reg.startswith("w"):
        return (reg, "x" + number)
    return (reg, "w" + number)


def _line_defines_reg(line: str, reg: str) -> bool:
    if not line.startswith("  "):
        return False
    stripped = line[2:]
    if not stripped or stripped.startswith(("b ", "b.", "bl ", "cbz ", "cbnz ")):
        return False
    opcode, sep, rest = stripped.partition(" ")
    if not sep:
        return False
    if opcode in (
        "cmp",
        "fcmp",
        "ret",
        "stur",
        "str",
        # Release stores consume their first register operand just like the
        # ordinary store forms above.  Treating ``stlr w10, [x9]`` as a
        # definition of w10 lets the move/store peepholes delete the move
        # which produced w10, leaving the atomic store with a stale value.
        "stlr",
        "sturb",
        "strb",
        "stlrb",
        "sturh",
        "strh",
        "stlrh",
        "stp",
    ):
        return False
    dest, _sep, _tail = rest.partition(", ")
    return dest == reg


def _line_uses_reg(line: str, reg: str) -> bool:
    if not line.startswith("  "):
        return False
    stripped = line[2:]
    opcode, sep, rest = stripped.partition(" ")
    if not sep:
        return reg in _tokens_for_reg_scan(stripped)
    if _line_defines_reg(line, reg):
        _dest, sep2, tail = rest.partition(", ")
        return bool(sep2) and reg in _tokens_for_reg_scan(tail)
    return reg in _tokens_for_reg_scan(rest)


def _line_defines_reg_alias(line: str, reg: str) -> bool:
    return any(_line_defines_reg(line, alias) for alias in _reg_aliases(reg))


def _line_uses_reg_alias(line: str, reg: str) -> bool:
    return any(_line_uses_reg(line, alias) for alias in _reg_aliases(reg))


def _can_drop_zero_mov_after_store(
    lines: list[str], start_index: int, reg: str
) -> bool:
    index = start_index
    while index < len(lines):
        line = lines[index]
        if (
            _local_label_name(line) is not None
            or _is_function_label(line)
            or line.startswith(".")
            or line.startswith(("  b ", "  b.", "  bl ", "  cbz ", "  cbnz ", "  ret"))
        ):
            return False
        if _line_uses_reg_alias(line, reg):
            return False
        if _line_defines_reg_alias(line, reg):
            return True
        index += 1
    return True


def _fold_zero_store_source(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            zero_reg = _parse_zero_movz(lines[index])
            store = _parse_store_source(lines[index + 1])
            if zero_reg is not None and store is not None:
                _opcode, store_reg, _addr = store
                replacement_reg = _zero_reg_for(zero_reg)
                if (
                    replacement_reg is not None
                    and store_reg == zero_reg
                    and _is_aarch64_scratch_reg(zero_reg)
                    and _can_drop_zero_mov_after_store(lines, index + 2, zero_reg)
                ):
                    out.append(
                        _replace_store_source(lines[index + 1], replacement_reg)
                        or lines[index + 1]
                    )
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _parse_reg_mov(line: str) -> tuple[str, str] | None:
    prefix = "  mov "
    if not line.startswith(prefix):
        return None
    try:
        dest, src = line[len(prefix) :].split(", ", 1)
    except ValueError:
        return None
    if not dest.startswith(("w", "x")) or not src.startswith(("w", "x")):
        return None
    if dest[0] != src[0] or dest == src:
        return None
    return dest, src


def _fold_mov_store_source(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            move = _parse_reg_mov(lines[index])
            store = _parse_store_source(lines[index + 1])
            if move is not None and store is not None:
                dest_reg, src_reg = move
                _opcode, store_reg, _addr = store
                if (
                    store_reg == dest_reg
                    and _is_aarch64_scratch_reg(dest_reg)
                    and _can_drop_zero_mov_after_store(lines, index + 2, dest_reg)
                ):
                    out.append(
                        _replace_store_source(lines[index + 1], src_reg)
                        or lines[index + 1]
                    )
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _parse_cmp_reg(line: str) -> tuple[str, str] | None:
    prefix = "  cmp "
    if not line.startswith(prefix):
        return None
    try:
        lhs, rhs = line[len(prefix) :].split(", ", 1)
    except ValueError:
        return None
    if not lhs.startswith(("w", "x")) or not rhs.startswith(("w", "x")):
        return None
    if lhs[0] != rhs[0]:
        return None
    return lhs, rhs


def _fold_zero_compare_immediate(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            zero_reg = _parse_zero_movz(lines[index])
            cmp_regs = _parse_cmp_reg(lines[index + 1])
            if zero_reg is not None and cmp_regs is not None:
                lhs, rhs = cmp_regs
                # Folding away the `movz reg, #0` is only sound when nothing
                # after the compare still reads that register. A min/max
                # intrinsic emits `movz w10,#0; cmp w9,w10; csel w11,w9,w10,cc`
                # — dropping the movz here would leave the csel reading an
                # undefined w10. Mirror the liveness guard used by
                # _fold_mov_compare_source.
                if (
                    rhs == zero_reg
                    and lhs != zero_reg
                    and _can_drop_zero_mov_after_store(lines, index + 2, zero_reg)
                ):
                    out.append(f"  cmp {lhs}, #0")
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _fold_mov_compare_source(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            move = _parse_reg_mov(lines[index])
            cmp_regs = _parse_cmp_reg(lines[index + 1])
            if move is not None and cmp_regs is not None:
                dest_reg, src_reg = move
                lhs, rhs = cmp_regs
                if (
                    _is_aarch64_scratch_reg(dest_reg)
                    and (lhs == dest_reg or rhs == dest_reg)
                    and _can_drop_zero_mov_after_store(lines, index + 2, dest_reg)
                ):
                    if lhs == dest_reg:
                        lhs = src_reg
                    if rhs == dest_reg:
                        rhs = src_reg
                    out.append(f"  cmp {lhs}, {rhs}")
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _branch_false_path_does_not_use_reg(lines: list[str], start_index: int) -> bool:
    index = start_index
    while index < len(lines) and not lines[index]:
        index += 1
    if index >= len(lines):
        return True
    return lines[index].startswith(("  b ", "  ret"))


def _fold_mov_zero_branch_source(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            move = _parse_reg_mov(lines[index])
            branch = _parse_cond_zero_branch(lines[index + 1])
            if move is not None and branch is not None:
                dest_reg, src_reg = move
                opcode, branch_reg, target = branch
                if (
                    branch_reg == dest_reg
                    and dest_reg.startswith("w")
                    and src_reg.startswith("w")
                    and _is_aarch64_scratch_reg(dest_reg)
                    and _branch_false_path_does_not_use_reg(lines, index + 2)
                ):
                    out.append(f"  {opcode} {src_reg}, {target}")
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _parse_three_operand_arith(line: str) -> tuple[str, str, str, str] | None:
    if not line.startswith("  "):
        return None
    stripped = line[2:]
    opcode, sep, rest = stripped.partition(" ")
    if not sep or opcode not in ("add", "sub"):
        return None
    try:
        dest, lhs, rhs = rest.split(", ", 2)
    except ValueError:
        return None
    if not dest.startswith(("w", "x")) or not lhs.startswith(("w", "x")):
        return None
    if dest[0] != lhs[0]:
        return None
    return opcode, dest, lhs, rhs


def _fold_mov_arith_self_update(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            move = _parse_reg_mov(lines[index])
            arith = _parse_three_operand_arith(lines[index + 1])
            if move is not None and arith is not None:
                scratch, src_reg = move
                opcode, dest, lhs, rhs = arith
                if (
                    dest == scratch
                    and lhs == scratch
                    and _is_aarch64_scratch_reg(scratch)
                    and scratch not in _tokens_for_reg_scan(rhs)
                ):
                    out.append(f"  {opcode} {dest}, {src_reg}, {rhs}")
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _fold_mov_mov_chain(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            first = _parse_reg_mov(lines[index])
            second = _parse_reg_mov(lines[index + 1])
            if first is not None and second is not None:
                scratch, src_reg = first
                dst_reg, second_src = second
                if (
                    second_src == scratch
                    and _is_aarch64_scratch_reg(scratch)
                    and _can_drop_zero_mov_after_store(lines, index + 2, scratch)
                ):
                    replacement = _forward_move(dst_reg, src_reg)
                    if replacement is not None:
                        if replacement:
                            out.append(replacement)
                        index += 2
                        continue
        out.append(lines[index])
        index += 1
    return out


def _fold_zero_test_branch(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines):
            zero_reg = _parse_zero_movz(lines[index])
            branch = _parse_cond_zero_branch(lines[index + 1])
            if zero_reg is not None and branch is not None:
                opcode, branch_reg, target = branch
                if branch_reg == zero_reg:
                    if opcode == "cbz":
                        out.append(f"  b {target}")
                    else:
                        out.append(lines[index])
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _invert_aarch64_cc(cond: str) -> str | None:
    mapping = {
        "eq": "ne",
        "ne": "eq",
        "lt": "ge",
        "le": "gt",
        "gt": "le",
        "ge": "lt",
        "lo": "hs",
        "ls": "hi",
        "hi": "ls",
        "hs": "lo",
        "mi": "pl",
        "pl": "mi",
        "vs": "vc",
        "vc": "vs",
    }
    return mapping.get(cond)


def _fold_cset_zero_branch(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 3 < len(lines) and (
            lines[index].startswith("  cmp ") or lines[index].startswith("  fcmp ")
        ):
            cset = _parse_cset(lines[index + 1])
            store = _parse_stack_transfer(lines[index + 2], "sturb")
            branch = _parse_cond_zero_branch(lines[index + 3])
            if cset is not None and store is not None and branch is not None:
                cset_reg, cset_cond = cset
                store_reg, _store_offset = store
                opcode, branch_reg, branch_target = branch
                branch_cond = cset_cond
                if opcode == "cbz":
                    branch_cond = _invert_aarch64_cc(cset_cond) or ""
                if branch_cond and store_reg == cset_reg and branch_reg == cset_reg:
                    out.append(lines[index])
                    out.append(lines[index + 1])
                    out.append(lines[index + 2])
                    out.append(f"  b.{branch_cond} {branch_target}")
                    index += 4
                    continue
        out.append(lines[index])
        index += 1
    return out


def _parse_direct_cond_branch(line: str) -> tuple[str, str] | None:
    prefix = "  b."
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix) :]
    cond, sep, target = rest.partition(" ")
    if not sep or not cond or not target:
        return None
    return cond, target


def _parse_uncond_branch(line: str) -> str | None:
    prefix = "  b "
    if not line.startswith(prefix):
        return None
    target = line[len(prefix) :]
    if not target:
        return None
    return target


def _branch_target(line: str) -> str | None:
    zero_branch = _parse_cond_zero_branch(line)
    if zero_branch is not None:
        return zero_branch[2]
    target = _parse_uncond_branch(line)
    if target is not None:
        return target
    parsed_cond = _parse_direct_cond_branch(line)
    if parsed_cond is not None:
        _cond, target = parsed_cond
        return target
    return None


def _retarget_branch(line: str, target: str) -> str:
    target_text = str(target)
    zero_branch = _parse_cond_zero_branch(line)
    if zero_branch is not None:
        opcode_text = str(zero_branch[0])
        reg_text = str(zero_branch[1])
        return "  " + opcode_text + " " + reg_text + ", " + target_text
    if _parse_uncond_branch(line) is not None:
        return "  b " + target_text
    parsed_cond = _parse_direct_cond_branch(line)
    if parsed_cond is not None:
        cond_text = str(parsed_cond[0])
        return "  b." + cond_text + " " + target_text
    return line


def _is_function_label(line: str) -> bool:
    return bool(line) and not line.startswith((" ", ".", "L_")) and line.endswith(":")


def _drop_dead_cset_branch_stores_in_function(lines: list[str]) -> list[str]:
    loaded_offsets = {
        offset
        for line in lines
        for parsed in [_parse_stack_transfer(line, "ldurb")]
        if parsed is not None
        for _reg, offset in [parsed]
    }
    if not loaded_offsets:
        loaded_offsets = set()
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 3 < len(lines) and (
            lines[index].startswith("  cmp ") or lines[index].startswith("  fcmp ")
        ):
            cset = _parse_cset(lines[index + 1])
            store = _parse_stack_transfer(lines[index + 2], "sturb")
            branch = _parse_direct_cond_branch(lines[index + 3])
            if cset is not None and store is not None and branch is not None:
                cset_reg, _cset_cond = cset
                store_reg, store_offset = store
                if store_reg == cset_reg and store_offset not in loaded_offsets:
                    out.append(lines[index])
                    out.append(lines[index + 3])
                    index += 4
                    continue
        out.append(lines[index])
        index += 1
    return out


def _drop_dead_cset_branch_stores(lines: list[str]) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    in_function = False

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        out.extend(_drop_dead_cset_branch_stores_in_function(current))
        current = []

    for line in lines:
        if _is_function_label(line):
            if in_function:
                flush_current()
            else:
                out.extend(current)
                current = []
                in_function = True
            current.append(line)
            continue
        current.append(line)

    if in_function:
        flush_current()
    else:
        out.extend(current)
    return out


def _local_label_name(line: str) -> str | None:
    if line.startswith("L_") and line.endswith(":"):
        return line[:-1]
    return None


def _trampoline_targets(lines: list[str]) -> dict[str, str]:
    targets: dict[str, str] = {}
    index = 0
    while index < len(lines):
        label = _local_label_name(lines[index])
        if label is None:
            index += 1
            continue
        # Target-final stack-map anchors can legitimately be followed by a
        # single branch, but they describe a safepoint PC rather than an IR
        # trampoline block.  Threading/removing them loses the metadata record.
        if label.startswith("L_pcc_smap"):
            index += 1
            continue
        body: list[str] = []
        j = index + 1
        while j < len(lines):
            nxt = lines[j]
            if _local_label_name(nxt) is not None or _is_function_label(nxt):
                break
            if nxt.startswith("."):
                break
            if nxt:
                body.append(nxt)
            j += 1
        if len(body) == 1:
            target = _parse_uncond_branch(body[0])
            if target is not None and target != label:
                targets[label] = target
        index += 1
    return targets


def _resolve_trampoline_target(
    target: str,
    trampolines: dict[str, str],
) -> str:
    seen: set[str] = set()
    current = target
    while current in trampolines and current not in seen:
        seen.add(current)
        current = trampolines[current]
    return current


def _thread_trampoline_branches(
    lines: list[str], metadata_labels: set[str] | None = None,
) -> list[str]:
    trampolines = _trampoline_targets(lines)
    if not trampolines:
        return lines

    label_index: dict[str, int] = {}
    for index, line in enumerate(lines):
        label = _local_label_name(line)
        if label is not None:
            label_index[label] = index

    rewritten: list[str] = []
    for index, line in enumerate(lines):
        target = _branch_target(line)
        if target is None:
            rewritten.append(line)
            continue
        resolved = _resolve_trampoline_target(target, trampolines)
        if resolved == target:
            rewritten.append(line)
            continue
        # Range guard: cbz/cbnz reach +/-32KB (8192 instructions) and
        # b.cond +/-1MB; threading a short trampoline hop into a direct
        # far branch overflows the fixup in huge functions ("fixup value
        # out of range"). Line distance conservatively over-approximates
        # instruction distance (labels/directives are 0 bytes), so skip
        # the rewrite and keep the trampoline when the resolved target is
        # too far. Unconditional `b` reaches +/-128MB and never needs the
        # guard.
        limit = 0
        if _parse_cond_zero_branch(line) is not None:
            limit = 6000
        elif _parse_direct_cond_branch(line) is not None:
            limit = 200000
        if limit:
            resolved_index = label_index.get(resolved)
            if resolved_index is None or abs(resolved_index - index) > limit:
                rewritten.append(line)
                continue
        rewritten.append(_retarget_branch(line, resolved))

    referenced = {
        target
        for line in rewritten
        for target in [_branch_target(line)]
        if target is not None
    }
    if metadata_labels is not None:
        referenced.update(metadata_labels)
    out: list[str] = []
    index = 0
    while index < len(rewritten):
        label = _local_label_name(rewritten[index])
        if label is not None and label in trampolines and label not in referenced:
            j = index + 1
            while j < len(rewritten):
                nxt = rewritten[j]
                if _local_label_name(nxt) is not None or _is_function_label(nxt):
                    break
                if nxt.startswith("."):
                    break
                j += 1
            index = j
            continue
        out.append(rewritten[index])
        index += 1
    return out


def _drop_fallthrough_uncond_branches(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        target = _parse_uncond_branch(lines[index])
        if target is None or not target.startswith("L_"):
            out.append(lines[index])
            index += 1
            continue
        j = index + 1
        while j < len(lines) and not lines[j]:
            j += 1
        if j < len(lines) and _local_label_name(lines[j]) == target:
            index += 1
            continue
        out.append(lines[index])
        index += 1
    return out


def _fold_cond_branch_to_fallthrough(
    lines: list[str],
    eligible_edges: list[tuple[str, str, str]],
) -> list[str]:
    """Invert only IR-proven canonical error checks around a next block.

    ``eligible_edges`` comes from
    ``plan_aarch64_canonical_error_fallthroughs``.  Keeping that proof input
    mandatory prevents this textual peephole from changing ordinary branches
    that happen to have the same two-instruction assembly shape.
    """
    label_index: dict[str, int] = {}
    for idx, line in enumerate(lines):
        label = _local_label_name(line)
        if label is not None:
            label_index[label] = idx
    out: list[str] = []
    index = 0
    current_error_target = ""
    current_success_target = ""
    while index < len(lines):
        local_label = _local_label_name(lines[index])
        if local_label is not None:
            # Stack-map anchors identify safepoint PCs inside an IR block;
            # they do not begin a new control-flow block and therefore must
            # not discard the proof attached to the containing block.
            if not local_label.startswith("L_pcc_smap"):
                current_error_target = ""
                current_success_target = ""
                for source_label, error_target, success_target in eligible_edges:
                    if local_label == source_label:
                        current_error_target = error_target
                        current_success_target = success_target
                        break
        elif _is_function_label(lines[index]):
            current_error_target = ""
            current_success_target = ""
        if current_success_target and index + 1 < len(lines):
            else_target = _parse_uncond_branch(lines[index + 1])
            direct_branch = _parse_direct_cond_branch(lines[index])
            zero_branch = _parse_cond_zero_branch(lines[index])
            if (
                else_target is not None
                and (direct_branch is not None or zero_branch is not None)
            ):
                then_target = ""
                replacement = ""
                range_limit = 0
                if direct_branch is not None:
                    cond, then_target = direct_branch
                    inverse = _invert_aarch64_cc(cond)
                    if inverse is not None:
                        replacement = f"  b.{inverse} {else_target}"
                        range_limit = 200000
                elif zero_branch is not None:
                    opcode, reg, then_target = zero_branch
                    inverse_opcode = "cbnz" if opcode == "cbz" else "cbz"
                    replacement = f"  {inverse_opcode} {reg}, {else_target}"
                    range_limit = 200000
                j = index + 2
                while j < len(lines) and not lines[j]:
                    j += 1
                # Range guard: this rewrites a +/-128MB `b` into an imm19
                # b.cond or cbz/cbnz (both +/-1MB).  Line distance
                # conservatively over-approximates instruction distance.
                else_index = label_index.get(else_target)
                else_in_range = (
                    else_index is not None
                    and range_limit > 0
                    and abs(else_index - index) <= range_limit
                )
                if (
                    replacement
                    and then_target.startswith("L_")
                    and else_target.startswith("L_")
                    and then_target == current_success_target
                    and else_target == current_error_target
                    and else_in_range
                    and j < len(lines)
                    and _local_label_name(lines[j]) == then_target
                ):
                    out.append(replacement)
                    index += 2
                    continue
        out.append(lines[index])
        index += 1
    return out


def _drop_unreferenced_empty_local_labels(
    lines: list[str], metadata_labels: set[str] | None = None,
) -> list[str]:
    referenced = {
        target
        for line in lines
        for target in [_branch_target(line)]
        if target is not None
    }
    if metadata_labels is not None:
        referenced.update(metadata_labels)
    out: list[str] = []
    index = 0
    while index < len(lines):
        label = _local_label_name(lines[index])
        # Precise-stackmap labels are consumed after all peepholes when final
        # PCs are calculated; they intentionally have no branch reference.
        # Treat them as externally referenced metadata anchors even when two
        # safepoints collapse to the same target-final instruction boundary.
        is_stack_map_anchor = label is not None and label.startswith(
            "L_pcc_smap"
        )
        if (
            label is not None
            and label not in referenced
            and not is_stack_map_anchor
        ):
            j = index + 1
            while j < len(lines) and not lines[j]:
                j += 1
            if j < len(lines) and (
                _local_label_name(lines[j]) is not None or _is_function_label(lines[j])
            ):
                index += 1
                continue
        out.append(lines[index])
        index += 1
    return out


def _emit_function(
    func: ParsedFunction,
    stack_map_plan: FunctionStackMapPlan,
    native_sink: _NativeAArch64Emission | None = None,
) -> list[str]:
    lines = _prologue_emit_function_prologue(func, _MODULE_SYMBOLS)
    if native_sink is not None:
        native_sink.extend(lines)
        lines = []
    kernel = get_indexed_function_kernel(func)
    if stack_map_plan.packed_records is not None and (
        not func.blocks or direct_instruction_capture_active()
    ):
        # AArch64 plans are packed even for parsed/oracle inputs. Emit reloads
        # at their final position, never through an eagerly rendered line index.
        if native_sink is None:
            lines.extend(_emit_dense_indexed_function_blocks(func, kernel, stack_map_plan))
        else:
            _emit_dense_indexed_function_blocks(
                func, kernel, stack_map_plan, native_sink=native_sink,
            )
    elif not func.blocks:
        require_direct_instruction_capture_idle()
        lines.extend(
            emit_indexed_function_blocks(
                func,
                indexed_kernel=kernel,
                block_label=_block_label,
                emit_indexed_instruction=_emit_dense_indexed_instruction_parts,
                emit_indexed_terminator=_emit_dense_indexed_terminator,
                emit_indexed_error_edge=_emit_dense_indexed_error_edge,
                stack_map_plan=stack_map_plan,
            )
        )
    else:
        require_direct_instruction_capture_idle()
        blocks = func.aarch64_block_layout or func.blocks
        lines.extend(
            emit_function_blocks(
                func,
                block_label=_block_label,
                emit_instruction=_emit_instruction,
                emit_terminator=_emit_terminator,
                blocks=blocks,
                stack_map_plan=stack_map_plan,
                indexed_kernel=kernel,
                emit_indexed_instruction=_emit_indexed_instruction_parts,
                emit_indexed_terminator=_emit_indexed_terminator,
            )
        )
    if native_sink is None:
        lines.append(stack_map_plan.end_label + ":")
    else:
        native_sink.append(stack_map_plan.end_label + ":")
    return lines


def _emit_instruction(
    func: ParsedFunction, block: ParsedBlock, instr: ParsedInstr
) -> list[str]:
    return _emit_instruction_parts(
        func, block, instr.kind, instr.data, instr.is_volatile
    )


def _emit_instruction_parts(
    func: ParsedFunction,
    block: ParsedBlock,
    kind: str,
    data: tuple,
    is_volatile: bool,
) -> list[str]:
    lines = emit_instruction_dispatch_parts(
        func,
        block,
        kind,
        data,
        emit_memory=_emit_memory_with_symbols,
        emit_compute=_emit_compute_with_symbols,
    )
    if is_volatile or kind in _MEMORY_PAIR_BARRIER_KINDS:
        return [
            AARCH64_MEMORY_PAIR_BARRIER_BEGIN,
            *lines,
            AARCH64_MEMORY_PAIR_BARRIER_END,
        ]
    return lines


def _emit_indexed_instruction_core(
    func: ParsedFunction,
    block_name: str,
    indexed_kernel: IndexedFunctionKernel,
    block_id: int,
    instruction_index: int,
    instruction_id: int,
    kind_id: int,
    data: tuple,
    is_volatile: bool,
) -> list[str]:
    instruction_fact: CompilerInt4 = indexed_kernel.instruction_fact_by_id(
        instruction_id,
    )
    pointer_override_ref = -1
    pointer_override_type = -1
    memory_offset = 0
    if kind_id == PARSED_INSTRUCTION_KIND_GEP:
        address: CompilerInt4 = adjacent_gep_memory_address(indexed_kernel, block_id, instruction_index)
        if address.first >= 0:
            return []
    elif kind_id in (PARSED_INSTRUCTION_KIND_LOAD, PARSED_INSTRUCTION_KIND_STORE) and not is_volatile:
        address: CompilerInt4 = adjacent_gep_memory_address(indexed_kernel, block_id, instruction_index - 1)
        if address.first >= 0:
            pointer_override_ref = address.second
            pointer_override_type = address.third
            memory_offset = address.fourth
    lines = _memory_emit_instruction(
        func,
        kind_id,
        data,
        _MODULE_SYMBOLS,
        indexed_kernel=indexed_kernel,
        block_id=block_id,
        instruction_index=instruction_index,
        indexed_dest_id=instruction_fact.first,
        pointer_override_ref=pointer_override_ref,
        pointer_override_type=pointer_override_type,
        memory_offset=memory_offset,
    )
    if lines is None:
        lines = _compute_emit_instruction(
            func,
            kind_id,
            data,
            _MODULE_SYMBOLS,
            indexed_kernel=indexed_kernel,
            block_id=block_id,
            instruction_index=instruction_index,
            indexed_dest_id=instruction_fact.first,
            indexed_use_count=instruction_fact.second,
            indexed_use0=instruction_fact.third,
            indexed_use_tail=instruction_fact.fourth,
        )
    if lines is None:
        kind = PARSED_INSTRUCTION_KINDS[kind_id]
        raise BackendUnavailable(
            f"self backend hit unknown instruction kind in {func.name!r}/{block_name!r}: {kind}"
        )
    if is_volatile or kind_id in _MEMORY_PAIR_BARRIER_KIND_IDS:
        return [
            AARCH64_MEMORY_PAIR_BARRIER_BEGIN,
            *lines,
            AARCH64_MEMORY_PAIR_BARRIER_END,
        ]
    return lines


def _emit_indexed_instruction_parts(
    func: ParsedFunction,
    block: ParsedBlock,
    indexed_kernel: IndexedFunctionKernel,
    block_id: int,
    instruction_index: int,
    instruction_id: int,
    kind_id: int,
    data: tuple,
    is_volatile: bool,
) -> list[str]:
    return _emit_indexed_instruction_core(
        func,
        block.name,
        indexed_kernel,
        block_id,
        instruction_index,
        instruction_id,
        kind_id,
        data,
        is_volatile,
    )


def _emit_dense_indexed_instruction_parts(
    func: ParsedFunction,
    indexed_kernel: IndexedFunctionKernel,
    block_id: int,
    instruction_index: int,
    instruction_id: int,
    kind_id: int,
    data: tuple,
    is_volatile: bool,
) -> list[str]:
    return _emit_indexed_instruction_core(
        func,
        indexed_kernel.block_names[block_id],
        indexed_kernel,
        block_id,
        instruction_index,
        instruction_id,
        kind_id,
        data,
        is_volatile,
    )


def _emit_dense_indexed_error_edge(
    func: ParsedFunction,
    indexed_kernel: IndexedFunctionKernel,
    edge_id: int,
    cold_lines: list[str] | None = None,
    defer_cold_stub: bool = False,
) -> list[str]:
    return _terms_emit_inline_error_edge_indexed(
        func,
        kernel=indexed_kernel,
        edge_id=edge_id,
        module_symbols=_MODULE_SYMBOLS,
        cold_lines=cold_lines,
        defer_cold_stub=defer_cold_stub,
    )


def _emit_memory_with_symbols(func: ParsedFunction, kind: str, data) -> list[str]:
    return _memory_emit_instruction(
        func,
        _PARSED_INSTRUCTION_KIND_IDS[kind],
        data,
        _MODULE_SYMBOLS,
    )


def _emit_compute_with_symbols(func: ParsedFunction, kind: str, data) -> list[str]:
    return _compute_emit_instruction(
        func,
        _PARSED_INSTRUCTION_KIND_IDS[kind],
        data,
        _MODULE_SYMBOLS,
    )


def _emit_return_with_symbols(
    func: ParsedFunction,
    ret_type,
    value,
) -> list[str]:
    return _rets_emit_return_terminator(
        func,
        ret_type=ret_type,
        value=value,
        module_symbols=_MODULE_SYMBOLS,
    )


def _emit_branch_with_symbols(
    func: ParsedFunction,
    source_block: str,
    target: str,
) -> list[str]:
    return _terms_emit_branch_terminator(
        func,
        source_block=source_block,
        target=target,
        module_symbols=_MODULE_SYMBOLS,
    )


def _emit_cond_branch_with_symbols(
    func: ParsedFunction,
    block_name: str,
    cond_name: str,
    true_target: str,
    false_target: str,
) -> list[str]:
    return _terms_emit_cond_branch_terminator(
        func,
        block_name=block_name,
        cond_name=cond_name,
        true_target=true_target,
        false_target=false_target,
        module_symbols=_MODULE_SYMBOLS,
    )


def _emit_switch_with_symbols(
    func: ParsedFunction,
    block_name: str,
    value_type,
    value: str,
    default_target: str,
    cases,
) -> list[str]:
    return _terms_emit_switch_terminator(
        func,
        block_name=block_name,
        value_type=value_type,
        value=value,
        default_target=default_target,
        cases=cases,
        module_symbols=_MODULE_SYMBOLS,
    )


def _emit_terminator(
    func: ParsedFunction, block: ParsedBlock, term: ParsedInstr
) -> list[str]:
    return emit_terminator_dispatch(
        func,
        block,
        term,
        emit_ret_void=_terms_emit_epilogue,
        emit_ret=_emit_return_with_symbols,
        emit_br=_emit_branch_with_symbols,
        emit_br_cond=_emit_cond_branch_with_symbols,
        emit_switch=_emit_switch_with_symbols,
        emit_unreachable=_terms_emit_unreachable_terminator,
    )


def _emit_indexed_terminator_core(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    block_id: int,
    term: ParsedInstr | None,
    use_value_id: int,
) -> list[str]:
    header: CompilerInt4 = kernel.terminator_header(block_id)
    span: CompilerInt4 = kernel.terminator_span(block_id)
    kind_id = header.first
    if kind_id in (PARSED_INSTRUCTION_KIND_RET, PARSED_INSTRUCTION_KIND_RET_VOID):
        tail_call_id = aarch64_tail_call_id_for_block(func, block_id)
        if tail_call_id >= 0:
            tail_header: CompilerInt4 = kernel.call_header(tail_call_id)
            return _terms_emit_tail_epilogue(
                func, asm_symbol_prevalidated(kernel.call_texts[tail_header.second], _MODULE_SYMBOLS),
            )
    if kind_id == PARSED_INSTRUCTION_KIND_RET:
        return _rets_emit_return_terminator_indexed(
            func,
            kernel=kernel,
            type_id=header.second,
            value_ref=header.third,
            module_symbols=_MODULE_SYMBOLS,
        )
    if kind_id == PARSED_INSTRUCTION_KIND_BR_COND:
        return _terms_emit_cond_branch_terminator_indexed(
            func,
            kernel=kernel,
            block_id=block_id,
            condition_ref=header.third,
            true_target_id=header.fourth,
            false_target_id=span.first,
            module_symbols=_MODULE_SYMBOLS,
        )
    if kind_id == PARSED_INSTRUCTION_KIND_BR:
        return _terms_emit_branch_terminator(
            func,
            source_block=kernel.block_names[block_id],
            target=kernel.block_names[header.fourth],
            module_symbols=_MODULE_SYMBOLS,
        )
    if kind_id == PARSED_INSTRUCTION_KIND_RET_VOID:
        return _terms_emit_epilogue(func)
    if kind_id == PARSED_INSTRUCTION_KIND_UNREACHABLE:
        return _terms_emit_unreachable_terminator()
    if kind_id == PARSED_INSTRUCTION_KIND_SWITCH:
        return _terms_emit_switch_terminator_indexed(
            func,
            kernel=kernel,
            block_id=block_id,
            value_type_id=header.second,
            value_ref=header.third,
            default_target_id=header.fourth,
            case_start=span.second,
            case_count=span.third,
            module_symbols=_MODULE_SYMBOLS,
        )
    kind = PARSED_INSTRUCTION_KINDS[kind_id]
    raise BackendUnavailable(
        f"self backend hit unknown terminator kind in {func.name!r}/"
        f"{kernel.block_names[block_id]!r}: {kind}"
    )


def _emit_indexed_terminator(
    func: ParsedFunction,
    block: ParsedBlock,
    term: ParsedInstr | None,
    use_value_id: int,
) -> list[str]:
    kernel = get_indexed_function_kernel(func)
    block_id = kernel.block_id(block.name)
    return _emit_indexed_terminator_core(
        func,
        kernel,
        block_id,
        term,
        use_value_id,
    )


def _emit_dense_indexed_terminator(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    block_id: int,
    use_value_id: int,
) -> list[str]:
    return _emit_indexed_terminator_core(
        func,
        kernel,
        block_id,
        None,
        use_value_id,
    )


def _emit_dense_indexed_function_blocks(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    stack_map_plan: FunctionStackMapPlan,
    native_sink: _NativeAArch64Emission | None = None,
) -> list[str]:
    """Emit the packed AArch64 path without dynamic callback adapters."""

    records = stack_map_plan.packed_records
    if records is None:
        raise BackendUnavailable("packed AArch64 emit requires packed stack maps")
    lines: list[str] = []
    layout_count = len(kernel.block_layout_ids)
    block_position = 0
    block_count = layout_count if layout_count else len(kernel.block_names)
    while block_position < block_count:
        block_id = (
            kernel.block_layout_ids.get_unchecked(block_position)
            if layout_count
            else block_position
        )
        block_name = kernel.block_names[block_id]
        if block_position == 0:
            if native_sink is None:
                lines.append(_block_label(func.name, block_name) + ":")
            else:
                native_sink.append(_block_label(func.name, block_name) + ":")
        else:
            if native_sink is None:
                lines.append("")
            else:
                native_sink.append("")
            if native_sink is None:
                lines.append(_block_label(func.name, block_name) + ":")
            else:
                native_sink.append(_block_label(func.name, block_name) + ":")
        if native_sink is None:
            stack_map_plan.append_packed_entry_lines(lines, block_id)
        else:
            fragment: CompilerInt2 = native_sink.fragments.new_fragment()
            stack_map_plan.append_packed_entry_span(
                native_sink.fragments, fragment, block_id,
            )
            native_sink.publish_fragment(fragment)

        block: CompilerInt4 = kernel.block_fact(block_id)
        error_span = kernel.inline_error_edge_span(block_id)
        error_edge_offset = 0
        suffix_route_index = 0
        suffix_route_end = 0
        span_offset = block_id * 2
        suffix_route_start = records.suffix_route_spans.get_unchecked(span_offset)
        suffix_route_count = records.suffix_route_spans.get_unchecked(
            span_offset + 1
        )
        if suffix_route_start >= 0:
            suffix_route_index = suffix_route_start
            suffix_route_end = suffix_route_start + suffix_route_count

        instruction_index = 0
        while instruction_index < block.second:
            instruction_id = block.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            if kind_id in _INDEXED_FIXED_PAYLOAD_KIND_IDS:
                instruction_data = metadata.second
            else:
                instruction_data = kernel.instruction_data(
                    block_id,
                    instruction_index,
                )
            if native_sink is None:
                lines.extend(
                    _emit_dense_indexed_instruction_parts(
                        func,
                        kernel,
                        block_id,
                        instruction_index,
                        instruction_id,
                        kind_id,
                        instruction_data,
                        bool(metadata.third),
                    )
                )
            else:
                native_sink.extend(
                    _emit_dense_indexed_instruction_parts(
                        func,
                        kernel,
                        block_id,
                        instruction_index,
                        instruction_id,
                        kind_id,
                        instruction_data,
                        bool(metadata.third),
                    )
                )
            while suffix_route_index < suffix_route_end:
                route_scalar = suffix_route_index * 3
                route_instruction = records.suffix_routes.get_unchecked(
                    route_scalar + 1
                )
                if route_instruction > instruction_index:
                    break
                if route_instruction == instruction_index:
                    record_index = records.suffix_routes.get_unchecked(
                        route_scalar + 2
                    )
                    if native_sink is None:
                        stack_map_plan.append_packed_record_lines(
                            lines,
                            record_index,
                        )
                    else:
                        fragment: CompilerInt2 = native_sink.fragments.new_fragment()
                        stack_map_plan.append_packed_record_span(
                            native_sink.fragments, fragment, record_index,
                        )
                        native_sink.publish_fragment(fragment)
                suffix_route_index += 1
            while error_edge_offset < error_span.second:
                edge_id = error_span.first + error_edge_offset
                trigger = kernel.inline_error_edge_trigger(edge_id)
                if trigger > instruction_index:
                    break
                if trigger < instruction_index:
                    raise BackendUnavailable(
                        "inline error edges are not trigger-ordered"
                    )
                if native_sink is None:
                    lines.extend(
                        _emit_dense_indexed_error_edge(
                            func,
                            kernel,
                            edge_id,
                            None,
                            True,
                        )
                    )
                else:
                    native_sink.extend(
                        _emit_dense_indexed_error_edge(
                            func,
                            kernel,
                            edge_id,
                            None,
                            True,
                        )
                    )
                error_edge_offset += 1
            instruction_index += 1

        if error_edge_offset != error_span.second:
            raise BackendUnavailable(
                "inline error edge trigger exceeds source block"
            )

        if native_sink is None:
            stack_map_plan.append_packed_terminator_lines(lines, block_id)
        else:
            fragment: CompilerInt2 = native_sink.fragments.new_fragment()
            stack_map_plan.append_packed_terminator_span(
                native_sink.fragments, fragment, block_id,
            )
            native_sink.publish_fragment(fragment)
        terminator_use_id = -1
        if block.third:
            terminator_use_id = block.fourth
        if native_sink is None:
            lines.extend(
                _emit_dense_indexed_terminator(
                    func,
                    kernel,
                    block_id,
                    terminator_use_id,
                )
            )
        else:
            native_sink.extend(
                _emit_dense_indexed_terminator(
                    func,
                    kernel,
                    block_id,
                    terminator_use_id,
                )
            )
        block_position += 1
    # Defer construction as well as placement: producer records and line
    # placeholders must enter their streams in the same final order. Reuse
    # the kernel's edge IDs instead of building a second cold line container.
    if len(kernel.error_edge_scalars):
        block_position = 0
        while block_position < block_count:
            block_id = (
                kernel.block_layout_ids.get_unchecked(block_position)
                if layout_count
                else block_position
            )
            cold_span = kernel.inline_error_edge_span(block_id)
            edge_offset = 0
            while edge_offset < cold_span.second:
                if native_sink is None:
                    lines.extend(_terms_emit_inline_error_stub_indexed(
                        func, kernel, cold_span.first + edge_offset,
                    ))
                else:
                    native_sink.extend(_terms_emit_inline_error_stub_indexed(
                        func, kernel, cold_span.first + edge_offset,
                    ))
                edge_offset += 1
            block_position += 1
    return lines
