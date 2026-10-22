"""Self-backend single/batch/split worker protocol implementations."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from typing import Optional

from pcc.driver.paths import join_strings
from pcc.backend import worker_phase_observer as _worker_phase_observer


class SelfBackendEmitError(RuntimeError):
    """A self-backend emitter worker contract was invalid."""


def _worker_phase_timing_enabled() -> bool:
    return str(os.environ.get("PCC_PY_FRONTEND_WORKER_TIMING", "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _worker_phase_diagnostic(
    enabled: bool,
    event: str,
    phase: str,
    lane: str,
    wave: int,
    input_count: int,
    command_count: int,
    input_size_total: int,
    input_size_max: int,
    requested_jobs: int,
) -> None:
    """Best-effort coordinator boundaries; END means the pool returned normally."""
    if not enabled:
        return
    try:
        # Labels are internal constants, never commands, paths or IR. Sizes
        # retain the scheduler's units: unsplit text length or split file bytes.
        # -1 means unavailable. Requested jobs are not observed concurrency.
        if lane not in ("split", "oversized", "huge", "medium", "small"):
            lane = "other"
        message = (
            "pcc self backend phase event=" + event
            + " phase=" + phase + " lane=" + lane + " wave=" + str(wave)
            + " monotonic_s=" + str(time.monotonic())
            + " input_count=" + str(input_count)
            + " command_count=" + str(command_count)
            + " scheduler_input_size_total=" + str(input_size_total)
            + " scheduler_input_size_max=" + str(input_size_max)
            + " requested_jobs=" + str(requested_jobs)
        )
        sys.stderr.write(message[:1023] + "\n")
        sys.stderr.flush()
    except Exception:
        # Diagnostics must not block dispatch or replace its original error.
        pass


_worker_timing_item = 0


def _worker_item_timing_context() -> tuple[int, int]:
    """Process-local observation IDs, not input or compiler content hashes."""
    global _worker_timing_item
    pid = -1
    item = -1
    try:
        _worker_timing_item += 1
        item = _worker_timing_item
        pid = os.getpid()
    except BaseException:
        pass
    return pid, item


def _worker_item_phase_diagnostic(
    event: str, phase: str, pid: int, item: int, outcome: str = "none",
) -> None:
    """Bounded callback receipts; complete means returned, never artifact validation.

    PID/item correlate with the ordered worker manifest. Compiler/runtime hashes
    belong to the outer CI receipt. No paths, source, exception text or hashes are
    collected here, and no timer is passed into the backend's execution path.
    """
    try:
        _worker_phase_observer.write_event(event, phase, pid, item, outcome)
    except BaseException:
        pass


def debug_dump_ir_texts(ir_texts: list[str]) -> None:
    dump_dir = str(os.environ.get("PCC_DEBUG_SELF_IR_DUMP_DIR", "") or "").strip()
    if not dump_dir:
        return
    for index, ir_text in enumerate(ir_texts):
        path = str(os.path.join(dump_dir, f"self_backend_input_{index}.ll"))
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(str(ir_text))
        except OSError:
            return


def emit_in_process(
    ir_text: str,
    *,
    parse_target_triple,
    host_target_triple,
    target_supported,
    emit_asm,
) -> Optional[tuple[str, str]]:
    triple = parse_target_triple(ir_text)
    if triple == "unknown-unknown-unknown":
        triple = host_target_triple()
        # Target selection must reach the backend's module context, not just
        # its dispatch branch. The subprocess path already normalizes this
        # placeholder; keep in-process capability/ABI checks equally bound.
        from .pipeline_targets import self_backend_ir_text
        ir_text = self_backend_ir_text(ir_text, host_target_triple=triple)
    if target_supported(triple):
        return "self-aarch64-darwin-v0", emit_asm(ir_text, False)
    from pcc.backend.self_backend_targets import resolve_self_backend_target
    from pcc.backend import BackendUnavailable
    try:
        target = resolve_self_backend_target(triple)
    except BackendUnavailable:
        return None
    return target.identity, target.emit_asm(ir_text)


def emit_packed_object_in_process(
    ir_text: str,
    *,
    parse_target_triple,
    host_target_triple,
) -> Optional[tuple[str, bytes]]:
    """Keep Linux x86 stack-map plans owned until final object offsets exist."""
    from pcc.backend.self_backend_target_match import is_x86_64_linux_triple

    triple = parse_target_triple(ir_text)
    if triple == "unknown-unknown-unknown":
        triple = host_target_triple()
        # Target selection must reach the backend's module context, not just
        # its dispatch branch. The subprocess path already normalizes this
        # placeholder; keep in-process capability/ABI checks equally bound.
        from .pipeline_targets import self_backend_ir_text
        ir_text = self_backend_ir_text(ir_text, host_target_triple=triple)
    if not is_x86_64_linux_triple(triple):
        return None
    from pcc.backend.self_backend_x86_64_linux import _emit_x86_64_module
    from pcc.backend.target_objects import encode_assembly_object

    plans = []
    try:
        # Preserve the text emitter's passes and original IR profile identity.
        # Only stack-map rendering changes: the assembler packs those plans
        # after measuring final instruction offsets, then consumes their owner.
        assembly = _emit_x86_64_module(ir_text, stack_map_plans_out=plans)
        payload = encode_assembly_object(
            assembly, triple, stack_map_plans=plans,
            consume_stack_map_plans=True,
        )
        return "self-x86_64-linux-v0", payload
    finally:
        # Validation failures deliberately retain caller-owned plans in the
        # encoder. This worker owns its private list and must release it too.
        plans.clear()


def emit_via_host_python(
    ir_text: str,
    tmp_dir: str,
    index: int,
    *,
    emit_native,
    normalize_ir,
    host_python_command,
    host_code: str,
    pcc_source_root,
) -> tuple[str, str]:
    native_result = emit_native(ir_text)
    if native_result is not None:
        return native_result
    if sys.implementation.name == "pcc":
        raise SelfBackendEmitError("native self emission cannot invoke host Python for an unsupported target")
    ir_path = str(os.path.join(tmp_dir, f"self_backend_input_{index}.ll"))
    with open(ir_path, "w", encoding="utf-8") as stream:
        stream.write(normalize_ir(ir_text))
    try:
        output = str(
            subprocess.check_output(
                [
                    host_python_command(),
                    "-c",
                    host_code,
                    pcc_source_root(),
                    ir_path,
                ],
                text=True,
            )
        )
    except Exception as exc:
        raise SelfBackendEmitError(
            f"self backend native emission failed: {exc}"
        ) from exc
    lines = output.splitlines()
    if not lines:
        raise SelfBackendEmitError(
            "self backend native emission failed: host emitter produced no output"
        )
    return lines[0], "\n".join(lines[1:])


def run_emit_worker(
    ir_path: str,
    result_path: str,
    obj_path: str = "",
    cc: str = "",
    *,
    normalize_ir,
    emit_native,
    emit_native_object=None,
) -> int:
    timing_enabled = False
    try:
        timing_enabled = _worker_phase_timing_enabled()
    except BaseException:
        pass
    timing_phase = ""
    timing_context_open = False
    timing_mask_open = False
    if timing_enabled:
        timing_pid, timing_item = _worker_item_timing_context()
    elif _worker_phase_observer.active:
        timing_mask_context = _worker_phase_observer.mask_worker()
        timing_mask_open = True
    try:
        if cc and sys.implementation.name == "pcc":
            raise SelfBackendEmitError("native self emission cannot invoke an external assembler")
        if timing_enabled:
            timing_phase = "read_normalize"
            _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
        with open(ir_path, "r", encoding="utf-8") as stream:
            ir_text = normalize_ir(stream.read())
        if timing_enabled:
            _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
            timing_phase = ""
        packed_result = None
        if obj_path and not cc and obj_path.endswith(".pco") and emit_native_object is not None:
            if timing_enabled:
                timing_phase = "packed_emit_assemble"
                _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
            packed_result = emit_native_object(ir_text)
            if timing_enabled:
                _worker_item_phase_diagnostic(
                    "complete", timing_phase, timing_pid, timing_item,
                    "declined" if packed_result is None else "returned",
                )
                timing_phase = ""
        asm_text = ""
        if packed_result is None:
            if timing_enabled:
                timing_phase = "emit"
                _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
            if timing_enabled:
                timing_worker_context = _worker_phase_observer.open_worker(timing_pid, timing_item)
                timing_context_open = True
            native_result = emit_native(ir_text)
            if timing_enabled:
                _worker_phase_observer.close_worker(timing_worker_context, False)
                timing_context_open = False
            if native_result is None:
                raise SelfBackendEmitError(
                    "native emitter does not support the module target"
                )
            target_id, asm_text = native_result
            if timing_enabled:
                _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                timing_phase = ""
        else:
            target_id, payload = packed_result
        result_payload = asm_text
        if obj_path and not cc and obj_path.endswith(".pco"):
            # Owned link: assemble here, in parallel with the other emit
            # workers, and hand the linker a packed native object.  The link
            # used to assemble every module's text serially in one process;
            # under pcc1 that was most of a multi-module compile.
            if packed_result is None:
                from pcc.backend.arm64_asm_driver import assemble_file
                from pcc.backend.native_object import encode_native_object_from_sections

                if target_id in ("self-aarch64-linux-v0", "self-x86_64-linux-v0"):
                    from pcc.backend.owned_elf_link import assemble as assemble_elf
                    from pcc.backend.elf_x86_64 import emit_relocatable
                    triple = "aarch64-unknown-linux-gnu" if target_id == "self-aarch64-linux-v0" else "x86_64-unknown-linux-gnu"
                    if timing_enabled:
                        timing_phase = "assemble_encode"
                        _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
                    payload = emit_relocatable(assemble_elf(asm_text, triple))
                    if timing_enabled:
                        _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                        timing_phase = ""
                elif target_id == "self-x86_64-windows-v0":
                    from pcc.backend.coff_x86_64 import assemble_object
                    if timing_enabled:
                        timing_phase = "assemble_encode"
                        _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
                    payload = assemble_object(asm_text)
                    if timing_enabled:
                        _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                        timing_phase = ""
                else:
                    if timing_enabled:
                        timing_phase = "assemble"
                        _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
                    sections, undefined = assemble_file(asm_text)
                    if timing_enabled:
                        _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                        timing_phase = "encode"
                        _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
                    payload = encode_native_object_from_sections(sections, undefined=undefined)
                    if timing_enabled:
                        _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                        timing_phase = ""
            asm_text = ""
            if timing_enabled:
                timing_phase = "object_write"
                _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
            with open(obj_path, "wb") as stream:
                stream.write(payload)
            if timing_enabled:
                _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                timing_phase = ""
            result_payload = obj_path
        elif obj_path:
            asm_path = obj_path if not cc else result_path + ".s"
            if timing_enabled:
                timing_phase = "assembly_write"
                _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
            with open(asm_path, "w", encoding="utf-8") as stream:
                stream.write(asm_text)
            if timing_enabled:
                _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                timing_phase = ""
            if cc:
                if timing_enabled:
                    timing_phase = "external_assemble"
                    _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
                subprocess.run([cc, "-c", asm_path, "-o", obj_path], check=True)
                if timing_enabled:
                    _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
                    timing_phase = ""
            result_payload = obj_path
        if timing_enabled:
            timing_phase = "result_write"
            _worker_item_phase_diagnostic("begin", timing_phase, timing_pid, timing_item)
        with open(result_path, "w", encoding="utf-8") as stream:
            stream.write(target_id + "\n")
            stream.write(result_payload)
        if timing_enabled:
            _worker_item_phase_diagnostic("complete", timing_phase, timing_pid, timing_item)
            timing_phase = ""
        return 0
    except Exception as exc:
        sys.stderr.write("self backend emit worker failed: " + str(exc) + "\n")
        return 1
    finally:
        if timing_enabled and timing_context_open:
            _worker_phase_observer.close_worker(timing_worker_context, True)
        if timing_enabled and timing_phase:
            _worker_item_phase_diagnostic("failed", timing_phase, timing_pid, timing_item)
        if timing_mask_open:
            _worker_phase_observer.close_worker(timing_mask_context, False)


def run_emit_batch_worker(
    manifest_path: str,
    *,
    manifest_version: str,
    emit_worker,
) -> int:
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
        if not lines or lines[0] != manifest_version:
            raise SelfBackendEmitError(
                "self backend emit batch has an invalid manifest"
            )
        payload = lines[1:]
        if not payload or len(payload) % 4 != 0:
            raise SelfBackendEmitError(
                "self backend emit batch has invalid item fields"
            )
        index = 0
        while index < len(payload):
            status = emit_worker(
                payload[index],
                payload[index + 1],
                payload[index + 2],
                payload[index + 3],
            )
            if status != 0:
                return status
            index += 4
        return 0
    except Exception as exc:
        sys.stderr.write(
            "self backend emit batch worker failed: " + str(exc) + "\n"
        )
        return 1


def _item_byte_sizes(sized_items):
    """Return just the byte sizes from ``(input_bytes, worker_item)`` pairs.

    A comprehension here would bind a second `input_bytes` in this module, and
    the self-hosted frontend plans that name as an exact-int local elsewhere in
    the same function -- the two storage forms cannot coexist, and stage1 fails
    with "planned exact-int local does not have object storage: input_bytes".
    Keeping the unpack inside its own function sidesteps the name entirely.
    """
    sizes = []
    for sized_item in sized_items:
        sizes.append(sized_item[0])
    return sizes


def _pack_batches(worker_items, batch_max_items: int, item_bytes):
    """Group worker items into batches balanced by input BYTES, not count.

    A batch is emitted serially by one worker, so a batch's cost is the sum of
    its inputs and the pool's wall time is the heaviest batch.  Packing purely
    by item count put the four largest shards of a stage2 emit into one batch:

        batch 0   27 + 12 + 10 + 5 MB = 54 MB   <- decided the whole phase
        batch 1    4 +  4 +  4 + 2 MB = 14 MB
        batch 2    2 +  1 +  1      MB =  4 MB

    Two of the three workers finished early and idled while the first ground
    through 54 MB serially.  (The same batch is what a previous cold bootstrap
    was killed in.)  Longest-processing-time-first — take the largest item and
    put it in whichever batch is currently lightest — evens that out; the item
    count cap is kept so a batch never grows an unbounded manifest.

    ``item_bytes`` may be None when sizes are unknown, in which case this
    degrades to the original count-based chunking.
    """
    items = list(worker_items)
    if not items:
        return []
    if not item_bytes or len(item_bytes) != len(items):
        batches = []
        batch = []
        for item in items:
            batch.append(item)
            if len(batch) >= batch_max_items:
                batches.append(batch)
                batch = []
        if batch:
            batches.append(batch)
        return batches

    order = sorted(range(len(items)), key=lambda i: -int(item_bytes[i]))
    n_batches = (len(items) + batch_max_items - 1) // batch_max_items
    batches = []
    loads = []
    for _ in range(n_batches):
        batches.append([])
        loads.append(0)
    for index in order:
        target = -1
        for slot in range(n_batches):
            if len(batches[slot]) >= batch_max_items:
                continue
            if target < 0 or loads[slot] < loads[target]:
                target = slot
        if target < 0:
            target = 0
            batches.append([])
            loads.append(0)
            target = len(batches) - 1
        batches[target].append(items[index])
        loads[target] += int(item_bytes[index])
    return [batch for batch in batches if batch]


def pack_admission_waves(command_bytes, byte_cap: int, width: int):
    """Group per-command input-byte weights into memory-safe waves.

    First-fit-decreasing: commands sorted by weight descending, each placed
    into the first wave whose byte sum stays within ``byte_cap`` and whose
    width stays within ``width``; otherwise it opens a new wave.  Returns a
    list of waves, each a list of original command indices.

    Input bytes are the footprint proxy calibrated from the 2026-08-27
    oversized-lane receipts (~1.3-1.4 GB peak footprint per input MB, pair
    sums measured <= 7.2 GB under a 7 MB byte cap; see
    docs/goal/evidence/2026-08-27-oversized-lane-pairing-schedule-design.md).
    The general sliding window in run_worker_commands is better for many
    small children, but it cannot keep two specific giants from being
    co-resident; for a handful of multi-GiB workers the bounded barrier
    loss buys a hard memory guarantee.
    """
    order = sorted(
        range(len(command_bytes)),
        key=lambda index: (-int(command_bytes[index]), index),
    )
    waves: list[list[int]] = []
    wave_bytes: list[int] = []
    for index in order:
        weight = int(command_bytes[index])
        placed = False
        for wave_index in range(len(waves)):
            if placed:
                continue
            if len(waves[wave_index]) >= width:
                continue
            if wave_bytes[wave_index] + weight > byte_cap:
                continue
            waves[wave_index].append(index)
            wave_bytes[wave_index] = wave_bytes[wave_index] + weight
            placed = True
        if not placed:
            waves.append([index])
            wave_bytes.append(weight)
    return waves


def run_emit_worker_pool(
    worker_command_prefix: list[str],
    worker_items: list[tuple[str, str, str]],
    cc: str,
    tmp_dir: str,
    batch_label: str,
    max_parallel: int,
    *,
    batch_max_items: int,
    manifest_version: str,
    item_bytes=None,
    worker_arg: str,
    small_int_decimal,
    shell_quote_arg,
    run_worker_commands,
    admission_byte_cap: int = 0,
    admission_width: int = 2,
    diagnostic_input_size_total: int = -1,
    diagnostic_input_size_max: int = -1,
) -> int:
    if not worker_items:
        return 0
    timing_enabled = _worker_phase_timing_enabled()
    max_parallel = max(1, min(int(max_parallel), len(worker_items)))
    # An oversized input can retain several GiB in a native worker.  Never put
    # two such inputs in one process: even though the lane is serial, batching
    # kept every completed IR graph alive and one stage2 worker grew past
    # 12 GiB while processing its fourth shard.  A fresh process is the memory
    # lifetime boundary.  Callers can also select batch_max_items=1 for safe
    # native inputs; source workers retain byte-balanced startup amortization.
    effective_batch_max_items = (
        1 if batch_label == "oversized" else batch_max_items
    )
    batches = _pack_batches(
        worker_items,
        effective_batch_max_items,
        item_bytes,
    )
    # _pack_batches REORDERS by descending bytes (LPT), so the caller's
    # item_bytes list no longer aligns with the batch/command order.  Weigh
    # each command from its own batch contents instead.
    bytes_by_item = {}
    if item_bytes is not None and len(item_bytes) == len(worker_items):
        if timing_enabled:
            diagnostic_input_size_total = 0
            diagnostic_input_size_max = 0
        for item_index in range(len(worker_items)):
            bytes_by_item[worker_items[item_index]] = int(
                item_bytes[item_index]
            )
            if timing_enabled:
                input_size = bytes_by_item[worker_items[item_index]]
                diagnostic_input_size_total += input_size
                diagnostic_input_size_max = max(diagnostic_input_size_max, input_size)
    command_bytes: list[int] = []
    commands: list[str] = []
    for batch_index, batch in enumerate(batches):
        batch_weight = 0
        for batch_item in batch:
            batch_weight = batch_weight + bytes_by_item.get(batch_item, 0)
        command_bytes.append(batch_weight)
        manifest_path = str(
            os.path.join(
                tmp_dir,
                "self_backend_emit_"
                + batch_label
                + "_"
                + small_int_decimal(batch_index)
                + ".manifest",
            )
        )
        with open(manifest_path, "w", encoding="utf-8") as stream:
            stream.write(manifest_version + "\n")
            for result_path, obj_path, ir_path in batch:
                for field in (ir_path, result_path, obj_path, cc):
                    if "\n" in field or "\r" in field:
                        raise SelfBackendEmitError(
                            "self backend emit batch field contains a newline"
                        )
                    stream.write(field + "\n")
        command_parts = [shell_quote_arg(part) for part in worker_command_prefix]
        command_parts.extend(
            [shell_quote_arg(worker_arg), shell_quote_arg(manifest_path)]
        )
        commands.append(join_strings(command_parts, " "))
    if (
        admission_byte_cap > 0
        and batch_label == "oversized"
        and bytes_by_item
        and len(commands) > 1
        and len(command_bytes) == len(commands)
    ):
        # Memory-safe width-2 waves for the multi-GiB oversized workers.
        # Each wave's input-byte sum respects the cap, so two giants are
        # never co-resident; pools are sequential, so the wave peak is the
        # machine peak.  Serial behavior is recovered with cap 0.
        waves = pack_admission_waves(
            command_bytes,
            int(admission_byte_cap),
            int(admission_width),
        )
        for wave_index, wave in enumerate(waves):
            wave_input_size_total = 0
            wave_input_size_max = 0
            if timing_enabled:
                # Oversized batches contain exactly one input per command.
                for index in wave:
                    wave_input_size_total += command_bytes[index]
                    wave_input_size_max = max(wave_input_size_max, command_bytes[index])
            _worker_phase_diagnostic(
                timing_enabled, "START", "emit", "oversized", wave_index + 1,
                len(wave), len(wave), wave_input_size_total, wave_input_size_max, len(wave),
            )
            run_worker_commands(
                [commands[index] for index in wave],
                max_parallel=len(wave),
            )
            _worker_phase_diagnostic(
                timing_enabled, "END", "emit", "oversized", wave_index + 1,
                len(wave), len(wave), wave_input_size_total, wave_input_size_max, len(wave),
            )
        return len(commands)
    _worker_phase_diagnostic(
        timing_enabled, "START", "emit", batch_label, 1,
        len(worker_items), len(commands), diagnostic_input_size_total, diagnostic_input_size_max,
        min(max_parallel, len(commands)),
    )
    run_worker_commands(
        commands,
        max_parallel=min(max_parallel, len(commands)),
    )
    _worker_phase_diagnostic(
        timing_enabled, "END", "emit", batch_label, 1,
        len(worker_items), len(commands), diagnostic_input_size_total, diagnostic_input_size_max,
        min(max_parallel, len(commands)),
    )
    return len(commands)


def run_split_worker(
    ir_path: str,
    result_path: str,
    output_prefix: str,
    export_prefix: str,
    shard_bytes_text: str,
    *,
    split_ir_module,
    small_int_decimal,
) -> int:
    try:
        with open(ir_path, "r", encoding="utf-8") as stream:
            ir_text = stream.read()
        shard_bytes = int(shard_bytes_text)
        if shard_bytes <= 0:
            raise SelfBackendEmitError(
                "self backend split worker requires shard bytes"
            )
        shards = split_ir_module(
            ir_text,
            export_prefix=export_prefix,
            shard_bytes=shard_bytes,
        )
        shard_paths: list[str] = []
        for index, shard_text in enumerate(shards):
            shard_path = output_prefix + small_int_decimal(index) + ".ll"
            with open(shard_path, "w", encoding="utf-8") as stream:
                stream.write(shard_text)
            shard_paths.append(shard_path)
        with open(result_path, "w", encoding="utf-8") as stream:
            stream.write("pcc.self_backend.split.v1\n")
            for shard_path in shard_paths:
                stream.write(shard_path + "\n")
        return 0
    except Exception as exc:
        sys.stderr.write("self backend split worker failed: " + str(exc) + "\n")
        return 1


def emit_objects_many_in_process(
    ir_texts: list[str],
    tmp_dir: str,
    cc: str,
    *,
    split_large_modules: bool,
    profile: Optional[dict],
    internal_link: bool = False,
    parse_target_triple,
    host_target_triple,
    target_supported,
    native_worker_executable,
    split_large_ir_modules,
    source_workers_worthwhile,
    worker_command_prefix_for_frontend,
    split_threshold_bytes,
    split_shard_bytes,
    jobs_for_ir_texts,
    profile_counter,
    profiled_gc_collect,
    profile_begin,
    profile_end,
    run_worker_commands,
    small_int_decimal,
    shell_quote_arg,
    split_worker_arg: str,
    plan_cache,
    jobs,
    jobs_for_input_sizes,
    jobs_env: str,
    run_emit_worker_pool,
    publish_cache,
    maintain_cache,
    emit_in_process,
    join_strings,
    oversized_admission=None,
) -> Optional[list[tuple[str, str]]]:
    """Emit one target's modules through its owned in-process/worker route."""
    if not ir_texts:
        return []
    if not internal_link and sys.implementation.name == "pcc":
        raise SelfBackendEmitError("native self batch emission requires owned object emission")
    first_triple = parse_target_triple(ir_texts[0])
    if first_triple == "unknown-unknown-unknown":
        first_triple = host_target_triple()
    from pcc.backend.self_backend_targets import resolve_self_backend_target
    from pcc.backend import BackendUnavailable
    try:
        selected_target = resolve_self_backend_target(first_triple).identity
    except BackendUnavailable:
        return None
    # Cache plans and worker object formats are selected once for the batch.
    # Reject mixed targets before writing inputs or publishing any cache entry.
    for ir_text in ir_texts[1:]:
        triple = parse_target_triple(ir_text)
        if triple == "unknown-unknown-unknown":
            triple = host_target_triple()
        try:
            current_target = resolve_self_backend_target(triple).identity
        except BackendUnavailable as exc:
            raise SelfBackendEmitError("unsupported target in self emission batch: " + triple) from exc
        if current_target != selected_target:
            raise SelfBackendEmitError("self emission batch contains different target platforms")
    pairs: list[tuple[str, str]] = []
    native_worker = native_worker_executable()
    inputs = ir_texts
    if not native_worker and split_large_modules:
        inputs = split_large_ir_modules(ir_texts)
    if native_worker:
        worker_command_prefix = [native_worker]
    elif sys.implementation.name != "pcc" and source_workers_worthwhile(inputs):
        worker_command_prefix = worker_command_prefix_for_frontend()
    else:
        worker_command_prefix = []
    t = profile_begin(profile)
    if worker_command_prefix:
        timing_enabled = _worker_phase_timing_enabled()
        split_threshold = split_threshold_bytes()
        split_shard_bytes_value = split_shard_bytes()
        planned_inputs: list[tuple[str, str, int]] = []
        split_worker_commands: list[str] = []
        split_module_count = 0
        split_input_size_total = 0
        split_input_size_max = 0
        for index, ir_text in enumerate(inputs):
            ir_path = str(os.path.join(tmp_dir, f"self_backend_module_{index}.ll"))
            with open(ir_path, "w", encoding="utf-8") as f:
                f.write(ir_text)
            input_bytes = len(ir_text)
            if native_worker and split_large_modules and input_bytes >= split_threshold:
                if timing_enabled:
                    split_input_size_total += input_bytes
                    split_input_size_max = max(split_input_size_max, input_bytes)
                result_path = str(
                    os.path.join(tmp_dir, f"self_backend_split_{index}.result")
                )
                output_prefix = str(
                    os.path.join(tmp_dir, f"self_backend_split_{index}_shard_")
                )
                export_prefix = "__pco" + small_int_decimal(index) + "_"
                command_parts = []
                for prefix_part in worker_command_prefix:
                    command_parts.append(shell_quote_arg(prefix_part))
                command_parts.extend(
                    [
                        shell_quote_arg(split_worker_arg),
                        shell_quote_arg(ir_path),
                        shell_quote_arg(result_path),
                        shell_quote_arg(output_prefix),
                        shell_quote_arg(export_prefix),
                        shell_quote_arg(
                            small_int_decimal(split_shard_bytes_value)
                        ),
                    ]
                )
                split_worker_commands.append(join_strings(command_parts, " "))
                planned_inputs.append((result_path, output_prefix, -1))
                split_module_count += 1
            else:
                planned_inputs.append((ir_path, "", input_bytes))

        # Admit split workers only when there is split work. Without splits,
        # defer the batch admission until cache planning establishes work.
        split_jobs = (
            jobs_for_ir_texts(inputs, native_worker=bool(native_worker))
            if split_worker_commands
            else 0
        )
        profile_counter(profile, "link_self_native_split_jobs", split_jobs)
        if split_worker_commands:
            profiled_gc_collect(
                profile,
                "link_self_native_pre_split_collect",
                allocations_owned_by_current_process=False,
            )
            split_t = profile_begin(profile)
            _worker_phase_diagnostic(
                timing_enabled, "START", "split", "split", 1,
                split_module_count, len(split_worker_commands),
                split_input_size_total, split_input_size_max,
                min(2, split_jobs, len(split_worker_commands)),
            )
            run_worker_commands(
                split_worker_commands,
                max_parallel=min(
                    2,
                    split_jobs,
                    len(split_worker_commands),
                ),
            )
            _worker_phase_diagnostic(
                timing_enabled, "END", "split", "split", 1,
                split_module_count, len(split_worker_commands),
                split_input_size_total, split_input_size_max,
                min(2, split_jobs, len(split_worker_commands)),
            )
            profile_end(profile, "link_self_native_split_workers", split_t)
            profiled_gc_collect(
                profile,
                "link_self_native_post_split_collect",
                allocations_owned_by_current_process=False,
            )

        worker_inputs: list[tuple[str, int]] = []
        split_shard_count = 0
        for input_path, output_prefix, input_bytes in planned_inputs:
            if input_bytes >= 0:
                worker_inputs.append((input_path, input_bytes))
                continue
            with open(input_path, "r", encoding="utf-8") as f:
                manifest_lines = f.read().splitlines()
            if not manifest_lines or manifest_lines[0] != "pcc.self_backend.split.v1":
                raise SelfBackendEmitError(
                    "self backend split worker produced an invalid manifest"
                )
            shard_paths = manifest_lines[1:]
            if not shard_paths:
                raise SelfBackendEmitError("self backend split worker produced no shards")
            for shard_path in shard_paths:
                if not shard_path.startswith(output_prefix) or not os.path.isfile(
                    shard_path
                ):
                    raise SelfBackendEmitError(
                        "self backend split worker produced an invalid shard path"
                    )
                worker_inputs.append((shard_path, os.path.getsize(shard_path)))
                split_shard_count += 1
        profile_counter(profile, "link_self_native_split_modules", split_module_count)
        profile_counter(profile, "link_self_native_split_shards", split_shard_count)

        # A native worker parsing one multi-megabyte module can retain several
        # GiB.  Keep residual post-split inputs at or above the safety threshold
        # in their own serial lane, but do not let those few inputs force every
        # sub-threshold cache miss into that lane.  The safe lane can use the
        # normal bounded self-backend worker budget after the oversized lane
        # exits, so their peak resident compiler heaps never overlap.
        emit_threshold = split_threshold_bytes()

        worker_items: list[tuple[str, str, str]] = []
        worker_input_bytes: list[int] = []
        for index, worker_input in enumerate(worker_inputs):
            ir_path, input_bytes = worker_input
            artifact_suffix = ".pco" if internal_link else ".o"
            obj_path = str(os.path.join(
                tmp_dir,
                f"self_backend_native_{index}{artifact_suffix}",
            ))
            result_path = str(
                os.path.join(tmp_dir, f"self_backend_native_{index}.result")
            )
            worker_items.append((result_path, obj_path, ir_path))
            worker_input_bytes.append(input_bytes)

        cache_plan_t = profile_begin(profile)
        cache_plan = plan_cache(
            worker_items,
            selected_target,
            "pcc-native-pco-v1" if internal_link else cc,
            tmp_dir,
        )
        profile_end(profile, "link_self_native_object_cache_plan", cache_plan_t)

        oversized_worker_items: list[tuple[int, tuple[str, str, str]]] = []
        large_worker_items: list[tuple[int, tuple[str, str, str]]] = []
        small_worker_items: list[tuple[str, str, str]] = []
        small_input_size_total = 0
        small_input_size_max = 0
        safe_emit_input_sizes: list[int] = []
        for index, worker_item in enumerate(worker_items):
            input_bytes = worker_input_bytes[index]
            _cache_path, cache_status = cache_plan[index]
            if cache_status == "hit":
                continue
            if native_worker and input_bytes >= emit_threshold:
                insert_at = 0
                while (
                    insert_at < len(oversized_worker_items)
                    and oversized_worker_items[insert_at][0] >= input_bytes
                ):
                    insert_at += 1
                oversized_worker_items.insert(
                    insert_at,
                    (input_bytes, worker_item),
                )
            elif input_bytes >= 1_000_000:
                safe_emit_input_sizes.append(input_bytes)
                insert_at = 0
                while (
                    insert_at < len(large_worker_items)
                    and large_worker_items[insert_at][0] >= input_bytes
                ):
                    insert_at += 1
                large_worker_items.insert(insert_at, (input_bytes, worker_item))
            else:
                safe_emit_input_sizes.append(input_bytes)
                small_worker_items.append(worker_item)
                if timing_enabled:
                    small_input_size_total += input_bytes
                    small_input_size_max = max(small_input_size_max, input_bytes)
        cache_miss_count = len(oversized_worker_items) + len(safe_emit_input_sizes)
        batch_jobs = split_jobs
        if not split_worker_commands and cache_miss_count:
            # Keep the original batch admission when emission is needed. A
            # partial cache hit can leave one miss, whose lane width alone
            # takes the singleton path and would bypass that budget check.
            batch_jobs = jobs_for_ir_texts(inputs, native_worker=bool(native_worker))
        configured_emit_jobs = (
            jobs(cache_miss_count) if cache_miss_count else 0
        )
        safe_emit_jobs = (
            jobs_for_input_sizes(
                safe_emit_input_sizes,
                native_worker=bool(native_worker),
            )
            if safe_emit_input_sizes
            else 0
        )
        profile_counter(
            profile,
            "link_self_native_oversized_object_count",
            len(oversized_worker_items),
        )
        profile_counter(
            profile,
            "link_self_native_safe_object_count",
            len(large_worker_items) + len(small_worker_items),
        )
        oversized_emit_jobs = 0
        explicit_emit_jobs = str(
            os.environ.get(jobs_env, "") or ""
        ).strip()
        # Memory-aware width-2 waves for the oversized lane, calibrated by
        # the 2026-08-27 per-item receipts (input bytes track peak footprint
        # at ~1.3-1.4 GB/MB; a 7 MB concurrent-input cap held every measured
        # pair <= 7.2 GB while the serial lane cost 164.8 s).  Cap 0 keeps
        # the serial lane; an explicit jobs env keeps the legacy unbounded
        # override and disables admission rather than stacking both.
        oversized_admission_cap = 7_000_000
        admission_env = str(
            os.environ.get("PCC_SELF_BACKEND_OVERSIZED_BYTE_CAP", "") or ""
        ).strip()
        if admission_env:
            cap_valid = True
            for ch in admission_env:
                if ch < "0" or ch > "9":
                    cap_valid = False
            if cap_valid:
                oversized_admission_cap = int(admission_env)
            else:
                oversized_admission_cap = 0
        oversized_admission_width = 2
        if oversized_worker_items:
            oversized_emit_jobs = 1
            if explicit_emit_jobs:
                oversized_emit_jobs = jobs(
                    len(oversized_worker_items)
                )
                oversized_admission_cap = 0
            elif not admission_env and native_worker and oversized_admission is not None:
                # A declared worker tree budget replaces the fixed 7 MB /
                # width-2 waves with the measured compiled-worker peak model.
                admitted = oversized_admission(len(oversized_worker_items))
                if admitted is not None:
                    oversized_admission_width, oversized_admission_cap = admitted
                    oversized_emit_jobs = oversized_admission_width
        effective_emit_jobs = max(safe_emit_jobs, oversized_emit_jobs)
        profile_counter(
            profile,
            "link_self_native_configured_jobs",
            configured_emit_jobs,
        )
        profile_counter(
            profile,
            "link_self_native_safe_emit_jobs",
            safe_emit_jobs,
        )
        profile_counter(
            profile,
            "link_self_native_emit_jobs",
            effective_emit_jobs,
        )
        profile_counter(
            profile,
            "link_self_native_oversized_emit_jobs",
            oversized_emit_jobs,
        )
        profile_counter(
            profile,
            "link_self_native_large_ir_job_cap",
            (
                1
                if (batch_jobs > 0 and batch_jobs < jobs(len(inputs)))
                or bool(oversized_worker_items)
                else 0
            ),
        )
        oversized_pool_processes = 0
        if oversized_worker_items:
            oversized_emit_t = profile_begin(profile)
            oversized_pool_processes = run_emit_worker_pool(
                worker_command_prefix,
                [worker_item for _input_bytes, worker_item in oversized_worker_items],
                "" if internal_link else cc,
                tmp_dir,
                "oversized",
                oversized_emit_jobs,
                bool(native_worker),
                item_bytes=_item_byte_sizes(oversized_worker_items),
                admission_byte_cap=oversized_admission_cap,
                admission_width=oversized_admission_width,
            )
            profile_end(
                profile,
                "link_self_native_emit_oversized_workers",
                oversized_emit_t,
            )
        safe_pool_processes = 0
        safe_emit_t = profile_begin(profile)
        if large_worker_items:
            huge_items: list[tuple[str, str, str]] = []
            huge_bytes: list[int] = []
            medium_items: list[tuple[str, str, str]] = []
            medium_bytes: list[int] = []
            for input_bytes, worker_item in large_worker_items:
                if input_bytes >= 4_000_000:
                    huge_items.append(worker_item)
                    huge_bytes.append(input_bytes)
                else:
                    medium_items.append(worker_item)
                    medium_bytes.append(input_bytes)
            if huge_items:
                safe_pool_processes += run_emit_worker_pool(
                    worker_command_prefix,
                    huge_items,
                    "" if internal_link else cc,
                    tmp_dir,
                    "huge",
                    min(2, safe_emit_jobs, len(huge_items)),
                    bool(native_worker),
                    item_bytes=huge_bytes,
                )
            if medium_items:
                safe_pool_processes += run_emit_worker_pool(
                    worker_command_prefix,
                    medium_items,
                    "" if internal_link else cc,
                    tmp_dir,
                    "medium",
                    min(8, safe_emit_jobs, len(medium_items)),
                    bool(native_worker),
                    item_bytes=medium_bytes,
                )
        if small_worker_items:
            safe_pool_processes += run_emit_worker_pool(
                worker_command_prefix,
                small_worker_items,
                "" if internal_link else cc,
                tmp_dir,
                "small",
                min(12, safe_emit_jobs, len(small_worker_items)),
                bool(native_worker),
                # Keep item_bytes absent: supplying it would change packing.
                diagnostic_input_size_total=small_input_size_total if timing_enabled else -1,
                diagnostic_input_size_max=small_input_size_max if timing_enabled else -1,
            )
        if large_worker_items or small_worker_items:
            profile_end(
                profile,
                "link_self_native_emit_safe_workers",
                safe_emit_t,
            )
        emit_pool_processes = oversized_pool_processes + safe_pool_processes
        profile_counter(
            profile,
            "link_self_native_oversized_emit_pool_processes",
            oversized_pool_processes,
        )
        profile_counter(
            profile,
            "link_self_native_safe_emit_pool_processes",
            safe_pool_processes,
        )
        profile_counter(
            profile,
            "link_self_native_emit_pool_processes",
            emit_pool_processes,
        )
        native_object_cache_hits = 0
        native_object_cache_misses = 0
        native_object_cache_disabled = 0
        for worker_index, worker_item in enumerate(worker_items):
            result_path, obj_path, _ir_path = worker_item
            with open(result_path, "r", encoding="utf-8") as f:
                worker_result = f.read()
            worker_result_lines = worker_result.splitlines()
            target_id = worker_result_lines[0] if worker_result_lines else ""
            emitted_obj_path = (
                worker_result_lines[1] if len(worker_result_lines) >= 2 else ""
            )
            if (
                target_id != selected_target
                or emitted_obj_path.strip() != obj_path
                or not os.path.isfile(obj_path)
            ):
                raise SelfBackendEmitError(
                    "self backend emit worker produced an invalid result"
                )
            cache_status = (
                worker_result_lines[2]
                if len(worker_result_lines) >= 3
                else cache_plan[worker_index][1]
            )
            if cache_status == "hit":
                native_object_cache_hits += 1
            elif cache_status == "miss":
                native_object_cache_misses += 1
            else:
                native_object_cache_disabled += 1
            pairs.append((target_id, obj_path))
        cache_publish_t = profile_begin(profile)
        cache_publish_ok = publish_cache(
            worker_items,
            cache_plan,
            tmp_dir,
        )
        if cache_publish_ok:
            protected_cache_paths: list[str] = []
            for cache_path, cache_status in cache_plan:
                if cache_path and cache_status in ("hit", "miss"):
                    protected_cache_paths.append(cache_path)
            maintain_cache(protected_cache_paths)
        profile_end(profile, "link_self_native_object_cache_publish", cache_publish_t)
        profile_counter(
            profile,
            "link_self_native_object_cache_hits",
            native_object_cache_hits,
        )
        profile_counter(
            profile,
            "link_self_native_object_cache_misses",
            native_object_cache_misses,
        )
        profile_counter(
            profile,
            "link_self_native_object_cache_disabled",
            native_object_cache_disabled,
        )
        profile_counter(
            profile,
            "link_self_native_object_cache_publish_ok",
            1 if cache_publish_ok else 0,
        )
    else:
        for object_index, ir_text in enumerate(inputs):
            asm_path = str(
                os.path.join(tmp_dir, f"self_backend_native_{object_index}.s")
            )
            obj_path = str(os.path.join(
                tmp_dir,
                "self_backend_native_"
                + small_int_decimal(object_index)
                + (".s" if internal_link else ".o"),
            ))
            native_result = emit_in_process(ir_text)
            if native_result is None:
                return None
            target_id = native_result[0]
            if target_id != selected_target:
                raise SelfBackendEmitError("self emitter returned a different target platform")
            asm_text = native_result[1]
            with open(asm_path, "w", encoding="utf-8") as f:
                f.write(asm_text)
            if not internal_link:
                subprocess.run([cc, "-c", asm_path, "-o", obj_path], check=True)
            else:
                obj_path = asm_path
            pairs.append((target_id, obj_path))
            # Source-mode stage1 emits hundreds of shards in this process.
            if object_index % 4 == 3:
                gc.collect()
    profiled_gc_collect(
        profile,
        "link_self_emit_objects_collect",
        allocations_owned_by_current_process=not bool(worker_command_prefix),
    )
    profile_end(profile, "link_self_emit_objects_native", t)
    profile_counter(profile, "link_self_native_object_count", len(pairs))
    profile_counter(
        profile,
        "link_self_native_object_fastpath_inputs",
        len(pairs) if internal_link else 0,
    )
    return pairs


def emit_objects_many_via_host_python(
    ir_texts: list[str],
    tmp_dir: str,
    cc: str,
    *,
    split_large_modules: bool = False,
    profile: Optional[dict] = None,
    internal_link: bool = False,
    emit_in_process_many,
    profile_begin,
    profile_end,
    split_threshold_bytes,
    jobs_for_count,
    host_python_command,
    host_many_code: str,
    pcc_source_root,
    small_int_decimal,
    profile_counter,
) -> list[tuple[str, str]]:
    native_results = emit_in_process_many(
        ir_texts,
        tmp_dir,
        cc,
        split_large_modules=split_large_modules,
        profile=profile,
        internal_link=internal_link,
    )
    if native_results is not None:
        return native_results
    if sys.implementation.name == "pcc":
        raise SelfBackendEmitError(
            "native self batch emission cannot invoke host Python for an unsupported target"
        )
    ir_paths = []
    t = profile_begin(profile)
    for index, ir_text in enumerate(ir_texts):
        ir_path = str(os.path.join(tmp_dir, f"self_backend_input_{index}.ll"))
        with open(ir_path, "w", encoding="utf-8") as f:
            f.write(ir_text)
        ir_paths.append(ir_path)
    profile_end(profile, "link_self_write_object_inputs", t)

    t = profile_begin(profile)
    job_count_hint = len(ir_paths)
    if split_large_modules:
        threshold = split_threshold_bytes()
        for ir_text in ir_texts:
            if len(ir_text) >= threshold:
                job_count_hint = max(job_count_hint, os.cpu_count() or 1)
                break
    jobs = jobs_for_count(job_count_hint)
    host_py = host_python_command()
    result_path = str(os.path.join(tmp_dir, "self_backend_results.tsv"))
    profile_end(profile, "link_self_prepare_object_emit", t)
    try:
        t = profile_begin(profile)
        subprocess.run(
            [
                host_py,
                "-c",
                host_many_code,
                pcc_source_root(),
                small_int_decimal(jobs),
                cc,
                "1" if split_large_modules else "0",
                result_path,
                "1" if internal_link else "0",
            ]
            + ir_paths,
            check=True,
        )
        profile_end(profile, "link_self_object_emit_subprocess", t)
    except Exception as e:
        raise SelfBackendEmitError(f"self backend native emission failed: {e}") from e
    try:
        t = profile_begin(profile)
        with open(result_path, "r", encoding="utf-8") as f:
            out = f.read()
        profile_end(profile, "link_self_read_object_results", t)
    except OSError as e:
        raise SelfBackendEmitError(f"self backend native emission failed: {e}") from e

    parsed_results: list[tuple[int, str, str]] = []
    host_emit_sum_ms = 0
    host_emit_max_ms = 0
    host_cc_sum_ms = 0
    host_cc_max_ms = 0
    host_input_bytes = 0
    host_input_max_bytes = 0
    host_object_cache_hits = 0
    host_object_cache_misses = 0
    host_object_cache_disabled = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        idx_text = parts[0]
        target_id = parts[1]
        obj_path = parts[2]
        try:
            idx = int(idx_text)
        except ValueError:
            continue
        if idx < 0:
            continue
        if len(parts) >= 6:
            try:
                emit_ms = int(parts[3])
                cc_ms = int(parts[4])
                byte_len = int(parts[5])
            except ValueError:
                emit_ms = 0
                cc_ms = 0
                byte_len = 0
            host_emit_sum_ms += emit_ms
            host_cc_sum_ms += cc_ms
            host_input_bytes += byte_len
            if byte_len > host_input_max_bytes:
                host_input_max_bytes = byte_len
            if emit_ms > host_emit_max_ms:
                host_emit_max_ms = emit_ms
            if cc_ms > host_cc_max_ms:
                host_cc_max_ms = cc_ms
        if len(parts) >= 7:
            cache_status = parts[6]
            if cache_status == "hit":
                host_object_cache_hits += 1
            elif cache_status == "miss":
                host_object_cache_misses += 1
            elif cache_status == "off":
                host_object_cache_disabled += 1
        parsed_results.append((idx, target_id, obj_path))

    if not parsed_results:
        raise SelfBackendEmitError(
            "self backend native emission failed: missing module result"
        )
    pairs: list[tuple[str, str]] = []
    for _idx, target_id, obj_path in parsed_results:
        pairs.append((target_id, obj_path))
    profile_counter(
        profile,
        "link_self_host_emit_asm_sum_ms",
        host_emit_sum_ms,
    )
    profile_counter(
        profile,
        "link_self_host_emit_asm_max_ms",
        host_emit_max_ms,
    )
    profile_counter(profile, "link_self_host_cc_sum_ms", host_cc_sum_ms)
    profile_counter(profile, "link_self_host_cc_max_ms", host_cc_max_ms)
    profile_counter(profile, "link_self_host_input_bytes", host_input_bytes)
    profile_counter(
        profile,
        "link_self_host_input_max_bytes",
        host_input_max_bytes,
    )
    profile_counter(profile, "link_self_host_object_count", len(pairs))
    profile_counter(profile, "link_self_host_jobs", jobs)
    profile_counter(
        profile,
        "link_self_host_object_cache_hits",
        host_object_cache_hits,
    )
    profile_counter(
        profile,
        "link_self_host_object_cache_misses",
        host_object_cache_misses,
    )
    profile_counter(
        profile,
        "link_self_host_object_cache_disabled",
        host_object_cache_disabled,
    )
    profile_counter(
        profile,
        "link_self_native_object_fastpath_inputs",
        len(pairs) if internal_link else 0,
    )
    return pairs
