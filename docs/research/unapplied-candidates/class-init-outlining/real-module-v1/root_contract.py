"""Source-only observer for the real class-initializer structural gate.

Use one RootContractObserver(stackmaps, HELPER_PREFIX, label=...) per independently parsed
and ordinarily prepared raw/default-mem2reg-SROA module. The driver must use
arm64-apple-darwin23.6.0 and build_function_stack_map_plan(...,
target="aarch64-darwin") for every prepared function. After each plan is used,
the driver closes plan.packed_records and its kernel, including on failure.
Call observer.assert_complete(expected_helpers=...) after planning/cleanup.

This module does not parse IR, prepare functions, invoke the compiler, or own
native arenas. The original planner still checks joins, provenance, and
liveness. The observer retains only ordinary Python diagnostics. In particular,
it neither closes nor retains the returned root plane or the borrowed aliases.

Lease coverage is deliberately limited: direct i64 producer results, immediate
signed-negative checks, exact SSA tokens, resolved static pointer aliases, and
an acyclic helper CFG. Other branches and switches are conservatively explored
on every edge. Inline-error capture must be disabled: nonzero inline error
edges are rejected. Unsupported shapes or a work
budget exhaustion are blockers, never passes. This verifies emitted cleanup
attempt coverage under the runtime ABI contract; zero tokens are valid no-op
tokens and release status may fail. It proves neither successful runtime
release nor complete GC correctness.
"""


HELPER_PREFIX = "__pcc_class_init_body_"
PRODUCERS = {
    "pcc_gc_foreign_lease_acquire": 1,
    "pcc_gc_root_copy_lease": 2,
    "pcc_gc_root_copy_borrowed_lease": 2,
}
CONSUMERS = {
    "pcc_gc_foreign_lease_release",
    "py_cleanup_one_lease_preserving_exception",
}


class ContractError(RuntimeError):
    """A violation or unsupported shape blocks this structural gate."""


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _reachable(kernel):
    _require(bool(kernel.block_names), "helper has no entry block")
    found = {0}
    pending = [0]
    for block_id in pending:
        for index in range(kernel.cfg_successor_count(block_id)):
            target = kernel.cfg_successor_id(block_id, index)
            _require(0 <= target < len(kernel.block_names), "invalid CFG edge")
            if target not in found:
                found.add(target)
                pending.append(target)
    return tuple(sorted(found))


def _root_returns(stackmaps, kernel, roots, reachable):
    rows = []
    for block_id in reachable:
        state_id = roots.entry_state_ids.get_unchecked(block_id)
        _require(state_id >= 0, "reachable block lacks an entry root state")
        block = kernel.block_fact(block_id)
        for index in range(block.second):
            metadata = kernel.instruction_metadata_by_id(block.first + index)
            if metadata.first == stackmaps.PARSED_INSTRUCTION_KIND_CALL:
                if kernel.call_flags(metadata.second) & stackmaps.CALL_FLAG_FRAME_PROTOCOL:
                    state_id = kernel.call_aux_state_id(metadata.second)
                    _require(state_id >= 0, "frame call lacks its post-root state")
        term = kernel.diagnostic_terminator(block_id)
        if term.kind == "ret":
            value_type, value = term.data
            _require(value_type.is_int and value_type.width == 32 and value in ("0", "1"),
                     "unsupported helper return: expected literal i32 0 or 1")
            # state_spans.fourth is ACTIVE GROUP COUNT. A group can own zero
            # locations, so state_location_spans.second is not sufficient.
            active_groups = roots.state_spans.get4_unchecked(state_id).fourth
            _require(active_groups == 0,
                     "active root groups at " + kernel.block_names[block_id]
                     + " ret " + value + ": " + str(active_groups))
            rows.append({"block": kernel.block_names[block_id], "return": int(value),
                         "state_id": state_id, "active_groups": active_groups})
        elif term.kind == "ret_void":
            raise ContractError("unsupported void helper return")
    _require(bool(rows), "helper has no reachable i32 status return")
    return rows


def verify_foreign_lease_cleanup_coverage(stackmaps, kernel, aliases, *,
                                          max_states=1000000):
    """Check only class-helper lease ABI paths over an already verified kernel.

    Existing PackedPointerAliases.resolve and _native_call_arg_ref establish
    slot (base, offset) identity. Kernel instruction_data supplies canonical
    call/icmp tuples; no textual IR recognizer is duplicated here. All unknown
    token uses, including PHIs, stores, selects, casts, and returns, are blocked.
    The budget is shared across all per-producer traversals in this helper.
    """
    _require(max_states > 0, "lease work budget must be positive")
    reachable = _reachable(kernel)
    reachable_set = set(reachable)
    successors = {}
    indegrees = {block: 0 for block in reachable}
    producers = {}
    releases = {}
    uses = {}

    def slot_origin(call_id, argument=0):
        header = kernel.call_header(call_id)
        raw = kernel.call_arg(header.fourth + argument)
        _require(kernel.type_desc(raw.first).is_ptr, "lease slot is not a pointer")
        aliases.resolve(stackmaps._native_call_arg_ref(raw))
        base = aliases.result.get_unchecked(0)
        offset = aliases.result.get_unchecked(1)
        if base < 0:
            _require(kernel.terminator_value(base).startswith("@"),
                     "unsupported non-global constant lease slot")
        else:
            _require(base < len(kernel.value_names), "invalid lease slot origin")
            _require(kernel.alloca_offset(base) >= 0
                     or kernel.value_header(base).first == -2,
                     "unsupported lease slot origin (not alloca, argument, or global)")
            _require(kernel.type_desc(kernel.value_type_id(base)).is_ptr,
                     "lease slot origin is not pointer-typed")
        return base, offset

    for block_id in reachable:
        targets = tuple(kernel.cfg_successor_id(block_id, index)
                        for index in range(kernel.cfg_successor_count(block_id)))
        successors[block_id] = targets
        for target in targets:
            indegrees[target] += 1
        _require(kernel.inline_error_edge_span(block_id).second == 0,
                 "unsupported inline-error capture: this gate requires fresh textual CFGs")
        for index in range(kernel.instruction_count(block_id)):
            point = (block_id, index)
            for use_index in range(kernel.instruction_use_count(block_id, index)):
                value_id = kernel.instruction_use_id(block_id, index, use_index)
                uses.setdefault(value_id, set()).add(point)
            if kernel.instruction_kind_id(block_id, index) != stackmaps.PARSED_INSTRUCTION_KIND_CALL:
                continue
            data = kernel.instruction_data(block_id, index)
            callee = data[2]
            if callee not in PRODUCERS and callee not in CONSUMERS:
                # Ordinary pcc_gc_release is refcount release, not a lease ABI.
                _require(not ("_lease" in callee and callee.startswith(("pcc_gc_", "py_cleanup_"))),
                         "unsupported lease ABI: " + callee)
                continue
            _require(not data[3], "indirect lease call is unsupported")
            call_id = kernel.instruction_call_id(block_id, index)
            if callee in PRODUCERS:
                _require(data[0] is not None and data[1].is_int and data[1].width == 64,
                         "lease producer must return an i64 SSA token")
                _require(len(data[4]) == PRODUCERS[callee], "unexpected lease producer arity")
                token_id = kernel.value_id(data[0])
                _require(token_id >= 0 and token_id not in producers, "invalid/redefined lease token")
                producers[token_id] = (point, slot_origin(call_id), callee)
            else:
                _require(len(data[4]) == 2 and data[4][1][0].is_int
                         and data[4][1][0].width == 64, "unexpected release signature")
                token_id = kernel.value_id(data[4][1][1])
                _require(token_id >= 0, "release does not use an exact producer SSA token")
                releases[point] = (token_id, slot_origin(call_id))

    # No dynamic iterations are modeled. Reject loops rather than confusing
    # one static producer instruction with multiple dynamic acquisitions.
    ready = [block for block in reachable if indegrees[block] == 0]
    for block_id in ready:
        for target in successors[block_id]:
            indegrees[target] -= 1
            if indegrees[target] == 0:
                ready.append(target)
    _require(len(ready) == len(reachable), "unsupported cyclic helper CFG")
    for point, (token_id, origin) in releases.items():
        _require(token_id in producers, "release token has no recognized producer")
        _require(origin == producers[token_id][1], "release slot alias origin differs from producer")

    token_ids = set(producers)
    for block_id in reachable:
        phi_span = kernel.block_phi_fact(block_id)
        for phi_id in range(phi_span.first, phi_span.first + phi_span.second):
            phi = kernel.phi_record(phi_id)
            for incoming_id in range(phi.third, phi.third + phi.fourth):
                _require(kernel.phi_incoming(incoming_id).first not in token_ids,
                         "unsupported lease token PHI")
        for use_index in range(kernel.terminator_use_count(block_id)):
            _require(kernel.terminator_use_id(block_id, use_index) not in token_ids,
                     "unsupported lease token terminator use")

    state_count = 0
    results = []
    for token_id, (producer_point, origin, callee) in producers.items():
        block_id, producer_index = producer_point
        compare_point = (block_id, producer_index + 1)
        _require(compare_point[1] < kernel.instruction_count(block_id)
                 and kernel.instruction_kind_id(*compare_point) == stackmaps.PARSED_INSTRUCTION_KIND_ICMP,
                 "lease producer lacks an immediate signed-negative check")
        compare = kernel.instruction_data(*compare_point)
        _require(compare[0] == "slt" and compare[2].is_int and compare[2].width == 64
                 and compare[3] == kernel.value_name(token_id)
                 and stackmaps.const_int_from_value(compare[4]) == 0,
                 "unsupported lease success guard: expected icmp slt i64 token, 0")
        term = kernel.diagnostic_terminator(block_id)
        _require(compare_point[1] + 1 == kernel.instruction_count(block_id)
                 and term.kind == "br_cond" and term.data[0] == compare[1],
                 "unsupported lease status-check control shape")
        failure = (kernel.terminator_successor_id(block_id, 0), 0)
        success = (kernel.terminator_successor_id(block_id, 1), 0)
        allowed_uses = {compare_point}
        allowed_uses.update(point for point, release in releases.items() if release[0] == token_id)
        _require(uses.get(token_id, set()) == allowed_uses,
                 "unsupported lease token transformation/use or missing cleanup attempt")

        # State -1 means the producer failed and acquired nothing. States 0/1
        # count attempts after success, including a legitimate zero token.
        pending = [(success[0], success[1], 0), (failure[0], failure[1], -1)]
        seen = set()
        successful_exits = set()
        failed_exits = set()
        for current_block, index, count in pending:
            state = (current_block, index, count)
            if state in seen:
                continue
            seen.add(state)
            state_count += 1
            _require(state_count <= max_states, "lease CFG work budget exhausted")
            _require(current_block in reachable_set, "lease walk left the reachable CFG")
            if index < kernel.instruction_count(current_block):
                point = (current_block, index)
                if point in releases and releases[point][0] == token_id:
                    _require(count == 0, "duplicate cleanup attempt or cleanup after failed acquire")
                    count = 1
                pending.append((current_block, index + 1, count))
                continue
            term = kernel.diagnostic_terminator(current_block)
            if term.kind == "ret":
                _require(count != 0, "successful lease reaches return without a matching cleanup attempt")
                (successful_exits if count == 1 else failed_exits).add(current_block)
                continue
            _require(term.kind in ("br", "br_cond", "switch"),
                     "unsupported lease exit/control terminator: " + term.kind)
            for successor_index in range(kernel.terminator_successor_count(current_block)):
                pending.append((kernel.terminator_successor_id(current_block, successor_index), 0, count))
        _require(bool(successful_exits) and bool(failed_exits),
                 "lease status branch lacks a reachable success/failure return")
        results.append({"token": kernel.value_name(token_id), "producer": callee,
                        "slot_origin": origin, "success_exit_count": len(successful_exits),
                        "failed_acquire_exit_count": len(failed_exits)})
    return {"status": "pass", "producer_count": len(results),
            "release_call_count": len(releases), "visited_states": state_count,
            "claim": "emitted exact-token cleanup-attempt coverage only", "tokens": results}


class RootContractObserver:
    """Temporarily observe the planner's root plane without taking ownership.

    Observation failures are saved until assert_complete: raising inside the
    hook would strand a returned plane before its normal planner owner adopts
    and closes it. Exceptions from the original implementation propagate
    unchanged. This hook is process-global and requires single-threaded use.
    """

    def __init__(self, stackmaps, prefix=HELPER_PREFIX, *, label="", max_lease_states=1000000):
        _require(prefix == HELPER_PREFIX, "observer is scoped only to class-outline helpers")
        self.stackmaps = stackmaps
        self.label = label
        self.max_lease_states = max_lease_states
        self.reports = []
        self.rows = self.reports
        self.failures = []
        self.original_call_count = 0
        self._original = None
        self._hook = self._observe
        self._installed = False

    def __enter__(self):
        _require(self._original is None, "observer instances cannot be reused")
        self._original = self.stackmaps._native_root_states
        self.stackmaps._native_root_states = self._hook
        self._installed = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.restore()
        return False

    def restore(self):
        """Idempotently restore the module hook; never close planner owners."""
        if not self._installed:
            return
        if self.stackmaps._native_root_states is self._hook:
            self.stackmaps._native_root_states = self._original
        else:
            self.failures.append("root-state hook changed while observer was active")
        self._installed = False

    def _observe(self, func, globals_by_name, aliases, kernel):
        self.original_call_count += 1
        roots = self._original(func, globals_by_name, aliases, kernel)
        if func.name.startswith(HELPER_PREFIX):
            report = {"function": func.name, "label": self.label, "status": "blocked"}
            self.reports.append(report)
            try:
                reachable = _reachable(kernel)
                report["root_returns"] = _root_returns(self.stackmaps, kernel, roots, reachable)
                report["lease_cleanup"] = verify_foreign_lease_cleanup_coverage(
                    self.stackmaps, kernel, aliases, max_states=self.max_lease_states)
                report["status"] = "pass"
            except Exception as error:
                report["error"] = type(error).__name__ + ": " + str(error)
                self.failures.append(func.name + ": " + report["error"])
        return roots

    def assert_complete(self, *, expected_helpers=None):
        _require(not self._installed, "finish planning and restore the hook before asserting")
        names = [report["function"] for report in self.reports]
        _require(bool(names), "no class-outline helpers were observed")
        _require(len(names) == len(set(names)), "a helper was planned more than once in this phase")
        if expected_helpers is not None:
            _require(len(names) == expected_helpers, "observed helper count differs from expected count")
        _require(not self.failures, "; ".join(self.failures))
        return tuple(self.reports)
