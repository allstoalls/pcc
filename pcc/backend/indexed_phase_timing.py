"""Bounded diagnostics for the isolated frontend worker's indexed path.

Enabled only by the caller's existing worker-timing policy.  The emitter never
reads an environment variable or retains an IR/function object for these totals.
Indexes are stable; assembly children 13..15 belong to inclusive parent 9.
"""

import sys
import time

from pcc.extern import c_int64, extern


_pcc_gc_backend: "extern" = extern("pcc_gc_backend", (), c_int64)
_pcc_gc_telemetry: "extern" = extern("pcc_gc_telemetry", (c_int64,), c_int64)

# These are existing O(1) scalar getters, never heap/root inventories. The
# first eight counters are cumulative; the remaining values are end gauges.
# Only work steps and the recorded pause count/sum/max have 64-bit storage.
_GC_COUNTER_IDS = (5, 32, 33, 0, 1, 2, 3, 15, 7, 42, 57, 59, 60, 66, 68, 70)
_GC_DELTA_NAMES = (
    "work_steps", "recorded_pause_count", "recorded_pause_us", "allocations",
    "write_barriers", "read_barriers", "safepoints", "relocation_forwards",
)
_GC_GAUGE_NAMES = (
    "max_recorded_pause_us_since_reset_end", "store_buffer_entries_end",
    "store_buffer_high_water_end", "store_buffer_owner_fanout_high_water_end",
    "store_buffer_owner_count_high_water_end", "remembered_entries_end",
    "remembered_high_water_end", "store_buffer_medium_pending_end",
)


_PHASE_NAMES = (
    "frontend_generate_ns",          # 0
    "frontend_release_pre_ns",       # 1
    "backend_call_inclusive_ns",     # 2: contains 3..7 and 12
    "prepare_verify_ns",             # 3
    "prepare_layout_ns",             # 4
    "stackmap_plan_ns",              # 5
    "function_emit_inclusive_ns",    # 6: contains 12
    "backend_finalize_ns",           # 7
    "frontend_release_post_ns",      # 8
    "object_assemble_ns",            # 9
    "object_encode_ns",              # 10
    "object_write_ns",               # 11
    "function_setup_ns",             # 12: child of 6
    "assembly_parse_ns",             # 13: child of 9
    "assembly_measure_ns",           # 14: child of 9
    "assembly_emit_finalize_ns",     # 15: child of 9, not pure encoding
)


class ModulePhaseTiming:
    """Sixteen scalar aggregates and three coarse assembly boundary pairs."""

    def __init__(self, module_name: str):
        self.module_name = module_name
        self.target = "unselected"
        self.route = "not-direct"
        self.totals_ns = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        self.failed = False
        self.reported = False

    def start(self) -> int:
        if self.failed:
            return 0
        try:
            started_ns = time.perf_counter_ns()
            # Owned native clock failure uses zero rather than raising.
            if started_ns <= 0:
                self.failed = True
                return 0
            return started_ns
        except BaseException:
            self.failed = True
            return 0

    def add(self, phase: int, started_ns: int) -> None:
        if self.failed or started_ns == 0:
            return
        try:
            elapsed_ns = time.perf_counter_ns() - started_ns
            if elapsed_ns < 0 or phase < 0 or phase >= 16:
                self.failed = True
                return
            self.totals_ns[phase] += elapsed_ns
        except BaseException:
            self.failed = True

    def begin_assembly(self, phase: int) -> int:
        # These three boundaries run once each, outside all instruction loops.
        try:
            if phase < 13 or phase > 15:
                return 0
            started_ns = self.start()
            _write_event(
                "assembly", _PHASE_NAMES[phase][:-3], "object_assemble",
                "begin", not self.failed, 0,
                _bounded_module_name(self.module_name),
            )
            return started_ns
        except BaseException:
            return 0

    def complete_assembly(self, phase: int, started_ns: int) -> None:
        try:
            if phase < 13 or phase > 15:
                return
            before = self.totals_ns[phase]
            self.add(phase, started_ns)
            _write_event(
                "assembly", _PHASE_NAMES[phase][:-3], "object_assemble",
                "complete", not self.failed and started_ns > 0,
                self.totals_ns[phase] - before,
                _bounded_module_name(self.module_name),
            )
        except BaseException:
            pass

    def report(self, stream, complete: bool) -> None:
        # Reporting happens after the worker's existing codegen timer stops,
        # including on its normal exception path.  A killed process may emit no
        # summary; incomplete scopes are deliberately not invented or estimated.
        if self.reported:
            return
        self.reported = True
        try:
            parts = [
                "pcc indexed phase totals",
                "module=" + self.module_name,
                "target=" + self.target,
                "route=" + self.route,
                "codegen_complete=" + str(int(complete)),
                "clock_ok=" + str(int(not self.failed)),
            ]
            index = 0
            while index < 16:
                parts.append(_PHASE_NAMES[index] + "=" + str(self.totals_ns[index]))
                index += 1
            stream.write(" ".join(parts) + "\n")
        except BaseException:
            # Optional diagnostics must not replace a compiler result/error.
            pass


# A fixed vocabulary keeps coordinator receipts bounded and prevents accidental
# source, command, path, or environment text from entering this diagnostic sink.
_COORDINATOR_PARENTS = {
    "result_read": "none",
    "runtime_verify": "none",
    "artifact_link": "none",
    "elf_stage_inputs": "artifact_link",
    "elf_link_image": "artifact_link",
    "elf_image_select_inputs": "elf_link_image",
    "elf_image_classify_layout": "elf_link_image",
    "elf_image_allocate_copy": "elf_link_image",
    "elf_image_resolve_relocate": "elf_link_image",
    "elf_image_finalize": "elf_link_image",
    "elf_verify_inputs": "artifact_link",
    "elf_write_image": "artifact_link",
    "executable_publish": "artifact_link",
    "artifact_cleanup": "none",
}


def _bounded_module_name(module_name: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
    return "".join(char if char in allowed else "_" for char in module_name[:160])


def _write_event(scope, phase, parent, event, clock_ok, duration_ns, module_name="",
                 gc_fields=()):
    try:
        parts = [
            "pcc coarse phase", "scope=" + scope, "phase=" + phase,
            "parent=" + parent, "event=" + event,
            "clock_ok=" + str(int(clock_ok)),
        ]
        if module_name:
            parts.append("module=" + module_name)
        # A begin receipt is intentionally incomplete. Neither an exception nor
        # watchdog termination fabricates a completion or elapsed duration.
        if event == "complete" and clock_ok:
            parts.append("duration_ns=" + str(duration_ns))
        parts.extend(gc_fields)
        sys.stderr.write(" ".join(parts) + "\n")
        sys.stderr.flush()
    except BaseException:
        # Even reporting failure during unwinding cannot replace the operation's
        # original exception (including KeyboardInterrupt/SystemExit).
        pass


class CoarsePhaseTiming:
    """At most fourteen fixed coordinator boundary pairs in the direct build path.

    Native GC readings cover this compiler process only. They include nested
    recorded collector work and diagnostic allocation, are not atomic, and do
    not cover all GC cost (for example mutator barrier/free-time scans). Parent
    and child deltas overlap and must not be added. Host GC is never substituted.
    """

    def __init__(self):
        self.failed = False
        self.gc_unavailable = False
        self.gc_starts = {}

    def _gc_snapshot(self):
        if self.gc_unavailable:
            return []
        try:
            # This exact native-owner guard is folded by the closed-world
            # frontend. CPython never substitutes its unrelated GC counters.
            if sys.implementation.name != "pcc":
                self.gc_unavailable = True
                return []
            # Annotated externs lower to direct native runtime calls.
            backend = _pcc_gc_backend()
            if backend < 0 or backend > 4:
                self.gc_unavailable = True
                return []
            values = [backend]
            for metric in _GC_COUNTER_IDS:
                values.append(_pcc_gc_telemetry(metric))
            return values
        except BaseException:
            self.gc_unavailable = True
            return []

    def _gc_begin(self, phase, started_ns):
        try:
            snapshot = self._gc_snapshot()
            if snapshot:
                self.gc_starts[phase] = (started_ns, snapshot)
                return ["gc_available=1", "gc_backend=" + str(snapshot[0])]
        except BaseException:
            pass
        return ["gc_available=0"]

    def _gc_complete(self, phase, started_ns):
        try:
            start = self.gc_starts.pop(phase, None)
            snapshot = self._gc_snapshot()
            if not snapshot:
                return ["gc_available=0"]
            parts = [
                "gc_available=1", "gc_backend=" + str(snapshot[0]),
                "gc_scope=process", "gc_snapshot=non_atomic",
                "gc_recorded_work=inclusive", "gc_event_counter_bits=32",
                "gc_accounting_counter_bits=64",
            ]
            valid = (
                start is not None and start[0] == started_ns
                and start[1][0] == snapshot[0]
            )
            # Detect ordinary wrap/reset cases instead of publishing negative
            # work. Existing 32-bit event counters still cannot exclude a full
            # wrap between snapshots. No telemetry reset is ever performed.
            wide_valid = valid
            event_valid = valid
            index = 0
            while index < 8:
                counter_valid = valid and (
                    start[1][index + 1] >= 0
                    and snapshot[index + 1] >= start[1][index + 1]
                )
                if index < 3:
                    wide_valid = wide_valid and counter_valid
                else:
                    event_valid = event_valid and counter_valid
                index += 1
            parts.append("gc_wide_delta_ok=" + str(int(wide_valid)))
            parts.append("gc_event_delta_ok=" + str(int(event_valid)))
            index = 0
            while index < 8:
                if (index < 3 and wide_valid) or (index >= 3 and event_valid):
                    parts.append("gc_" + _GC_DELTA_NAMES[index] + "_delta="
                                 + str(snapshot[index + 1] - start[1][index + 1]))
                index += 1
            index = 0
            while index < 8:
                if snapshot[index + 9] >= 0:
                    parts.append("gc_" + _GC_GAUGE_NAMES[index] + "="
                                 + str(snapshot[index + 9]))
                index += 1
            return parts
        except BaseException:
            return ["gc_available=0"]

    def begin(self, phase: str) -> int:
        try:
            if phase not in _COORDINATOR_PARENTS:
                return 0
            started_ns = 0
            if not self.failed:
                try:
                    started_ns = time.perf_counter_ns()
                    if started_ns <= 0:
                        self.failed = True
                        started_ns = 0
                except BaseException:
                    self.failed = True
            _write_event(
                "coordinator", phase, _COORDINATOR_PARENTS[phase],
                "begin", not self.failed, 0,
                gc_fields=self._gc_begin(phase, started_ns),
            )
            return started_ns
        except BaseException:
            return 0

    def complete(self, phase: str, started_ns: int) -> None:
        try:
            if phase not in _COORDINATOR_PARENTS:
                return
            elapsed_ns = 0
            if not self.failed and started_ns > 0:
                try:
                    elapsed_ns = time.perf_counter_ns() - started_ns
                    if elapsed_ns < 0:
                        self.failed = True
                except BaseException:
                    self.failed = True
            _write_event(
                "coordinator", phase, _COORDINATOR_PARENTS[phase],
                "complete", not self.failed and started_ns > 0, elapsed_ns,
                gc_fields=self._gc_complete(phase, started_ns),
            )
        except BaseException:
            pass


    def elf_classification_skips(self, completed_sections: int, total_sections: int,
                                 skipped_sections: int, skipped_relocations: int) -> bool:
        """One fixed-field snapshot; caller coalesces at most eight quantiles.

        Sections are unweighted. Skipped relocation rows are a lower bound on
        avoided classification work, not total rows, elapsed time, or a speedup.
        """
        try:
            values = (completed_sections, total_sections, skipped_sections, skipped_relocations)
            if any(type(value) is not int or value < 0 for value in values):
                return False
            if not 0 < completed_sections <= total_sections or skipped_sections > completed_sections:
                return False
            parts = [
                "pcc elf classification skips",
                "completed_sections=" + str(completed_sections),
                "total_sections=" + str(total_sections),
                "skipped_sections=" + str(skipped_sections),
                "skipped_relocations_lower_bound=" + str(skipped_relocations),
            ]
            sys.stderr.write(" ".join(parts) + "\n")
            sys.stderr.flush()
            return True
        except BaseException:
            return False


    def elf_relocation_progress(self, event, completed_sections, total_sections,
                                completed_relocations, total_relocations) -> bool:
        """At most one scale, one GOT boundary and eight completed-row quantiles.

        R includes NONE rows. Section progress credits their initial resident
        row weights, not actual applied rows, resolution calls, throughput or time. Unknown/custom scale cannot publish a partial total.
        """
        try:
            if type(event) is not str or event not in ("scale", "scale_unknown", "got_complete", "sections"):
                return False
            limit = ((1 << 63) - 1) // 8
            values = (completed_sections, total_sections, completed_relocations, total_relocations)
            if any(type(value) is not int or value < 0 or value > limit for value in values):
                return False
            if event == "scale_unknown":
                if any(values):
                    return False
                parts = ["pcc elf relocation progress", "event=scale", "total_relocations_known=0"]
            else:
                if completed_sections > total_sections or completed_relocations > total_relocations:
                    return False
                if event == "sections":
                    if not completed_sections or not completed_relocations:
                        return False
                elif completed_sections or completed_relocations:
                    return False
                parts = ["pcc elf relocation progress", "event=" + event]
                if event == "scale":
                    parts.extend(("count_basis=initial_resident", "total_relocations_known=1",
                                  "total_relocations=" + str(total_relocations)))
                elif event == "sections":
                    parts.extend(("count_basis=initial_resident", "completed_sections=" + str(completed_sections),
                                  "total_sections=" + str(total_sections),
                                  "completed_section_initial_relocations=" + str(completed_relocations),
                                  "total_relocations=" + str(total_relocations)))
            sys.stderr.write(" ".join(parts) + "\n")
            sys.stderr.flush()
            return True
        except BaseException:
            return False
