"""One bounded CI host-list comparison; excluded from local pure controls.

This synthetic envelope is not the compiler planner or a representative module.
Source extraction and fixture construction happen before tracing. Only this
single test starts a tracer; no compiler, subprocess, native operation or FFI is
used. Checkpoint and poison replays are separate from the peak measurement.
"""

import ast
import copy
from dataclasses import dataclass
import hashlib
import json
import platform
from pathlib import Path
import sys
from types import SimpleNamespace

from tests.python import test_managed_scratch_retirement as controls


FANIN = 65
VALUE_COUNT = 66
BLOCK_COUNT = 69
CALL_COUNT = 133
INCOMING_COUNT = 66
REPORT_NAME = "managed-scratch-host-memory.json"


def _malloc_host_trap(*args, **kwargs):
    # This is the actual CompilerIntArena.__init__ CPython fallback contract.
    raise NotImplementedError("host-only malloc trap")


def _real_namespace(*, original, observer=None):
    namespace = dict(
        valueclass=lambda cls: dataclass(frozen=True)(cls),
        _fail=controls._fail,
        BackendUnavailable=ValueError,
        IndexedFunctionKernel=type("ExactKernel", (), {}),
        PARSED_INSTRUCTION_KIND_CALL=1,
        PARSED_INSTRUCTION_KIND_BR=2,
        PARSED_INSTRUCTION_KIND_BR_COND=3,
        PARSED_INSTRUCTION_KIND_SWITCH=4,
        CALL_FLAG_FRAME_PROTOCOL=8,
        TYPE_KIND_PTR=3,
        POINTER_SIZE=8,
        NO_OFFSET=-(1 << 62),
        get_indexed_function_kernel=lambda func: func.kernel,
    )
    arena_tree = ast.parse(controls.ARENA.read_text())
    namespace.update({
        (alias.asname or alias.name): controls._forbidden
        for node in arena_tree.body if isinstance(node, ast.ImportFrom)
        for alias in node.names
    })
    namespace.update(malloc=_malloc_host_trap, _py_bytes_new=controls._forbidden)
    classes = [copy.deepcopy(node) for node in arena_tree.body
               if isinstance(node, ast.ClassDef) and node.name in
               ("CompilerInt2", "CompilerInt3", "CompilerInt4", "CompilerIntArena")]
    controls._exec(classes, namespace)
    stackmap_tree = ast.parse(controls.SOURCE.read_text())
    constants = [copy.deepcopy(node) for node in stackmap_tree.body
                 if isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name)
                     and target.id.startswith(("_ORIGIN_", "_TRANSFER_"))
                     for target in node.targets)]
    classes = [copy.deepcopy(node) for node in stackmap_tree.body
               if isinstance(node, ast.ClassDef) and node.name in
               ("PackedManagedLiveness", "PackedPointerAliases", "PackedManagedOrigins")]
    native = controls._function("_native_managed_liveness", original)
    if observer is not None:
        body = []
        for node in native.body:
            if (isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name) and node.target.id == "result"):
                body.extend(ast.parse(
                    '_observe("before-packed", {"uses": uses, "definitions": definitions, '
                    '"live_out": live_out, "live_in": live_in, '
                    '"tracked_values": tracked_values, "tracked_index": tracked_index, '
                    '"scratch": scratch})'
                ).body)
            body.append(node)
        native.body = body
        namespace["_observe"] = observer
    controls._exec([*constants, *classes, native,
                    controls._function("_managed_live_after", True)], namespace)
    _install_envelope(namespace, original=original, observer=observer)
    return namespace


def _install_envelope(namespace, *, original, observer=None):
    # Use exactly the production planner's successful-origins boundary. The
    # fixture's helper setup intentionally replaces root/planner construction.
    planner = controls._function("_build_function_stack_map_plan_native", original)
    start = next(index for index, node in enumerate(planner.body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "origins"
                         for target in node.targets))
    end = next(index for index, node in enumerate(planner.body)
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "live_after"
                       for target in node.targets))
    boundary = copy.deepcopy(planner.body[start + 1:end])
    expected = [] if original else [
        "aliases.close()", "origins.transfers.close()",
        "origins.transfer_ids.close()", "origins.proposal.close()",
        "roots.retire_construction_state()",
    ]
    assert [ast.unparse(node) for node in boundary] == expected
    # This old envelope intentionally models aliases/origins only. Root-plane
    # lifetime is covered by the real planner/PCO controls, not this memory ratio.
    boundary = [node for node in boundary
                if ast.unparse(node) != "roots.retire_construction_state()"]
    source = '''
def envelope(func, kernel):
    value_count = len(kernel.value_names)
    aliases = PackedPointerAliases(value_count)
    for value_id in range(value_count):
        aliases.set_alias(value_id, -1, 0)
    origins = PackedManagedOrigins(value_count)
    for value_id in range(value_count - 1):
        aliases.resolve(value_id)
        origins.set_state(value_id, _ORIGIN_MANAGED, -8,
                          aliases.result.get_unchecked(1))
    joined = CompilerIntArena()
    for value_id in range(value_count):
        joined.append(value_id)
    origins.set_join(value_count - 1, joined)
    joined.close()
    origins.converge()
    _successful_origins_boundary()
    live = _native_managed_liveness(func, origins, kernel)
    return aliases, origins, live
'''
    function = ast.parse(source).body[0]
    body = []
    for node in function.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_successful_origins_boundary"):
            body.extend(boundary)
            if observer is not None:
                body.extend(ast.parse(
                    '_observe("before-liveness", {"aliases.records": aliases.records, '
                    '"aliases.result": aliases.result, "origins.states": origins.states, '
                    '"origins.transfers": origins.transfers, '
                    '"origins.transfer_ids": origins.transfer_ids, '
                    '"origins.proposal": origins.proposal})'
                ).body)
                namespace["_observe"] = observer
        else:
            body.append(node)
    function.body = body
    controls._exec([function], namespace)


def _fixture(namespace):
    kernel = controls.Kernel(namespace, controls._phi_blocks(FANIN, True), VALUE_COUNT)
    assert len(kernel.block_names) == BLOCK_COUNT
    assert len(kernel.value_names) == VALUE_COUNT
    assert len(kernel.call_states) == CALL_COUNT
    assert len(kernel.incoming) == INCOMING_COUNT
    assert type(kernel.instruction_overflow_use_ids._values) is list
    return kernel, SimpleNamespace(kernel=kernel, name="synthetic-host-memory", frame_size=1024)


def _finish(namespace, kernel, result):
    aliases, origins, packed = result
    decoded = controls._decode(kernel, packed)
    states = tuple(origins.states.get_unchecked(index) for index in range(VALUE_COUNT * 3))
    assert all(origins.is_tracked(value) for value in range(VALUE_COUNT))
    assert all(states[index * 3:index * 3 + 3] == (namespace["_ORIGIN_MANAGED"], -8, 0)
               for index in range(VALUE_COUNT))
    assert not packed.states._closed and not packed.overflow_ids._closed
    aliases.close()
    origins.close()
    packed.close()
    kernel.instruction_overflow_use_ids.close()
    return decoded, states


def _oracle():
    namespace = _real_namespace(original=True)
    kernel, func = _fixture(namespace)
    packed = namespace["_managed_live_after"](func, frozenset(range(VALUE_COUNT)))
    decoded = controls._decode(kernel, packed)
    packed.close()
    kernel.instruction_overflow_use_ids.close()
    return decoded


def _checkpoint_replay(*, original):
    snapshots = {}

    def observe(stage, arenas):
        # Record scalar measurements only; do not keep arenas or lists alive.
        snapshots[stage] = {
            name: {"closed": arena._closed, "logical_slots": len(arena),
                   "list_length": len(arena._values),
                   "shallow_host_list_bytes": sys.getsizeof(arena._values)}
            for name, arena in arenas.items()
        }

    namespace = _real_namespace(original=original, observer=observe)
    kernel, func = _fixture(namespace)
    decoded, states = _finish(namespace, kernel, namespace["envelope"](func, kernel))
    return snapshots, decoded, states


def _poison_replay(*, original):
    namespace = controls._namespace(original=original, checkpoint=True)
    _install_envelope(namespace, original=original)
    # Poison lists deliberately retain their owner through the tracking graph.
    # This replay never contributes any traced peak.
    kernel = controls.Kernel(namespace, controls._phi_blocks(FANIN, True), VALUE_COUNT)
    func = SimpleNamespace(kernel=kernel, name="synthetic-poison", frame_size=1024)
    result = namespace["envelope"](func, kernel)
    aliases, origins, packed = result
    assert aliases.records._closed == (not original)
    assert aliases.result._closed == (not original)
    assert all(arena._closed == (not original) for arena in
               (origins.transfers, origins.transfer_ids, origins.proposal))
    decoded, states = _finish(namespace, kernel, result)
    labelled = {arena.label: arena for arena in namespace["TrackingArena"].instances}
    for name in controls.MATRICES:
        assert labelled[name].releases == 1
        assert labelled[name].close_calls == (1 if original else 2)
    retired = (aliases.records, aliases.result, origins.transfers,
               origins.transfer_ids, origins.proposal)
    assert all(arena.releases == 1 and arena.close_calls == (1 if original else 2)
               for arena in retired)
    assert len(namespace["checkpoints"]) == 1
    return decoded, states


def test_host_list_retirement_memory_comparison(tmp_path):
    # Imported here so merely collecting/local source controls cannot activate
    # a profiler. An existing tracer belongs to its caller and is untouched.
    import tracemalloc

    report = {
        "schema": 1,
        "scope": "synthetic source-extracted aliases/origins/converge/liveness/packed host-list envelope",
        "not_measured": ["full compiler planner", "representative Mac module", "native allocator", "RSS", "CPU time"],
        "fixture": {"blocks": BLOCK_COUNT, "values": VALUE_COUNT, "tracked": VALUE_COUNT,
                    "calls": CALL_COUNT, "phi_incoming": INCOMING_COUNT, "origin_join_sources": VALUE_COUNT},
        "runtime": {"implementation": platform.python_implementation(), "version": sys.version,
                    "platform": sys.platform, "machine": platform.machine()},
        "source_sha256": {"stackmaps": hashlib.sha256(controls.SOURCE.read_bytes()).hexdigest(),
                          "arena": hashlib.sha256(controls.ARENA.read_bytes()).hexdigest(),
                          "frozen_reference": hashlib.sha256(controls.REFERENCE.read_bytes()).hexdigest(),
                          "fixture_controls": hashlib.sha256(Path(controls.__file__).read_bytes()).hexdigest(),
                          "test": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "measurement": {"status": "pending", "order": ["old", "new", "new", "old"],
                        "tracemalloc_frames": 1, "runs": []},
    }
    report_path = tmp_path / REPORT_NAME
    try:
        if tracemalloc.is_tracing():
            # Do not stop or reset another tracer. No transparency claim is
            # made: even this test's normal Python allocations may be observed.
            report["status"] = "not_measured"
            report["measurement"]["status"] = "not_measured_existing_tracer"
            report["assessment"] = "no demonstrated peak improvement; existing tracer preserved"
            return
        assert platform.python_implementation() == "CPython"
        namespaces = {name: _real_namespace(original=(name == "old")) for name in ("old", "new")}
        expected = _oracle()
        expected_states = None
        # Fixed untraced warmup in both variants, then fresh fixtures per run.
        for name in ("old", "new"):
            namespace = namespaces[name]
            kernel, func = _fixture(namespace)
            decoded, states = _finish(namespace, kernel, namespace["envelope"](func, kernel))
            assert decoded == expected
            if expected_states is None:
                expected_states = states
            assert states == expected_states
        for position, name in enumerate(report["measurement"]["order"]):
            namespace = namespaces[name]
            kernel, func = _fixture(namespace)
            # No tracking instances/events, wrappers or checkpoint allocation
            # participate in these four whole-envelope peaks.
            assert "TrackingArena" not in namespace and "_observe" not in namespace
            assert not tracemalloc.is_tracing()
            tracemalloc.start(1)
            try:
                result = namespace["envelope"](func, kernel)
                current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            decoded, states = _finish(namespace, kernel, result)
            del result
            assert decoded == expected and states == expected_states
            report["measurement"]["runs"].append({
                "position": position, "variant": name,
                "current_after_packed_bytes": current, "whole_envelope_peak_bytes": peak,
                "decoded_output_sha256": hashlib.sha256(json.dumps(decoded).encode()).hexdigest(),
            })
        runs = report["measurement"]["runs"]
        report["measurement"]["paired_peak_deltas_old_minus_new_bytes"] = [
            runs[0]["whole_envelope_peak_bytes"] - runs[1]["whole_envelope_peak_bytes"],
            runs[3]["whole_envelope_peak_bytes"] - runs[2]["whole_envelope_peak_bytes"],
        ]
        demonstrated = all(delta > 0 for delta in
                           report["measurement"]["paired_peak_deltas_old_minus_new_bytes"])
        report["assessment"] = (
            "lower whole-envelope traced peak in both serial orders for this synthetic host fixture"
            if demonstrated else "no demonstrated whole-envelope peak improvement")
        report["measurement"]["status"] = "measured"
        checkpoints = {}
        for name in ("old", "new"):
            snapshots, decoded, states = _checkpoint_replay(original=(name == "old"))
            assert decoded == expected and states == expected_states
            checkpoints[name] = snapshots
            assert _poison_replay(original=(name == "old")) == (expected, expected_states)
        report["separate_checkpoint_replay"] = checkpoints
        report["separate_poison_replay"] = "passed"
        old, new = checkpoints["old"], checkpoints["new"]
        scratch = ("aliases.records", "aliases.result", "origins.transfers",
                   "origins.transfer_ids", "origins.proposal")
        before_liveness = sum(old["before-liveness"][key]["logical_slots"]
                              - new["before-liveness"][key]["logical_slots"] for key in scratch)
        before_packed = sum(old["before-packed"][key]["logical_slots"]
                            - new["before-packed"][key]["logical_slots"] for key in controls.MATRICES)
        assert before_liveness == 7 * VALUE_COUNT + VALUE_COUNT + 5
        assert before_packed == 3 * BLOCK_COUNT * ((VALUE_COUNT + 29) // 30)
        report["logical_slots_retired"] = {"before_liveness": before_liveness,
                                          "additional_before_packed": before_packed}
        report["checkpoint_size_scope"] = "sys.getsizeof of actual host backing lists; excludes referenced integer objects, allocator/RSS, and peak attribution"
        report["status"] = "passed"
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        # Existing preflight artifact upload includes JSON beneath --basetemp.
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
