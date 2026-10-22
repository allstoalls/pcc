"""Normal-CI-only real-kernel receipt within the existing PHI oracle call.

No extra pytest invocation, subprocess, compiler, job, or profiler is added.
This is a synthetic host observation, not a production throughput benchmark.
"""

import ast
import copy
import gc
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

from tests.managed_liveness_dirty_rows_reference import REFERENCE_SHA256, reference_function


def record_real_kernel_overhead(tmp_path, phi_source):
    # Real imports intentionally stay behind the existing CI-only parser test.
    from pcc.backend import self_backend_precise_stackmaps as stackmaps
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_value_arena import CompilerIntArena

    source_path = Path(stackmaps.__file__)
    tree = ast.parse(source_path.read_text())
    current = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_native_managed_liveness")
    old = reference_function()

    def load(function, observers=None):
        namespace = dict(vars(stackmaps))
        if observers is not None:
            namespace.update(observers)
        module = ast.Module(body=[ast.ImportFrom(module="__future__",
                            names=[ast.alias(name="annotations")], level=0), copy.deepcopy(function)],
                            type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "<dirty-row-real-CI-reference>", "exec"), namespace)
        return namespace["_native_managed_liveness"]

    functions = {"old": load(old), "new": load(current)}
    arguments = ", ".join("ptr %unused" + str(index) for index in range(65))
    blocks = ["b" + str(index) + ":\n  br label %b" + str(index + 1) for index in range(127)]
    blocks.append("b127:\n  ret void")
    zero_source = ('target triple = "arm64-apple-darwin23.6.0"\n'
                   'define void @zero_change(' + arguments + ') {\n' + '\n'.join(blocks) + '\n}\n')
    shapes = {"phi-cycle": (phi_source, ["p" + str(index) for index in range(65)] + ["chosen"]),
              "positive-width-zero-change": (zero_source, ["unused" + str(index) for index in range(65)])}

    def fixture(shape):
        source, names = shapes[shape]
        func = parse_self_backend_module(source).functions[0]
        kernel = get_indexed_function_kernel(func)
        assert type(kernel) is stackmaps.IndexedFunctionKernel
        cfg = (kernel.terminator_scalars, kernel.terminator_case_scalars,
               kernel.error_edge_spans, kernel.error_edge_scalars)
        assert all(type(arena) is CompilerIntArena and type(arena._values) is list
                   and arena._address == 0 for arena in cfg)
        assert stackmaps.CompilerIntArena is CompilerIntArena
        origins = stackmaps.PackedManagedOrigins(len(kernel.value_names))
        for name in names:
            value = kernel.value_id(name)
            assert value >= 0
            origins.set_state(value, stackmaps._ORIGIN_MANAGED)
        return func, origins, kernel

    def finish(result, origins, kernel):
        record = {"states": tuple(result.states._values), "overflow": tuple(result.overflow_ids._values),
                  "calls": tuple(kernel.call_scalars._values)}
        result.close()
        origins.close()
        kernel.close_native_tables()
        return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()

    report = {"schema": 1, "scope": "ordinary real parsed kernels and unpatched production host-list arenas",
              "assessment": "synthetic host costs only; no production throughput or native allocator claim",
              "runtime": {"implementation": platform.python_implementation(), "version": platform.python_version(),
                          "platform": sys.platform, "machine": platform.machine()},
              "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
              "old_reference_sha256": REFERENCE_SHA256,
              "function_ast_sha256": {name: hashlib.sha256(ast.dump(function).encode()).hexdigest()
                                      for name, function in (("old", old), ("new", current))},
              "fixture_sha256": {name: hashlib.sha256(source.encode()).hexdigest()
                                 for name, (source, _) in shapes.items()},
              "measurement_order": ["old", "new", "new", "old"],
              "whole_columns": ["position", "variant", "wall_ns", "process_cpu_ns"],
              "gate_columns": ["wall_ns", "process_cpu_ns"], "shapes": {}}
    report_path = tmp_path / "managed-liveness-dirty-rows-host-overhead.json"
    try:
        for shape in shapes:
            observed = {"gate_cells": 0, "scheduler_entries": 0, "changed_rows": 0}

            def cell():
                observed["gate_cells"] += 1

            def entered():
                observed["scheduler_entries"] += 1

            def changed_row():
                observed["changed_rows"] += 1

            # Separate un-timed source-body observers establish that THIS
            # ordinary kernel reaches the optimized path and the gate really
            # visits S payload cells. Timed bodies contain neither observer.
            inspected = copy.deepcopy(current)
            gate = next(node for node in inspected.body if isinstance(node, ast.FunctionDef)
                        and node.name == "cfg_arena_is_plain")
            loop = next(node for node in ast.walk(gate) if isinstance(node, ast.While))
            loop.body.insert(0, ast.parse("_ci_gate_cell()").body[0])
            branch = next(node for node in inspected.body if isinstance(node, ast.If)
                          and ast.unparse(node.test) == "dirty_rows_enabled"
                          and any(isinstance(child, ast.Try) for child in node.body))
            branch.body.insert(0, ast.parse("_ci_scheduler_entered()").body[0])
            for conditional in ast.walk(branch):
                if (isinstance(conditional, ast.If) and isinstance(conditional.test, ast.Call)
                        and isinstance(conditional.test.func, ast.Attribute)
                        and conditional.test.func.attr == "converge_liveness_row_unchecked"):
                    conditional.body.insert(0, ast.parse("_ci_changed_row()").body[0])
            inspected_function = load(inspected, {"_ci_gate_cell": cell,
                                                  "_ci_scheduler_entered": entered,
                                                  "_ci_changed_row": changed_row})
            func, origins, kernel = fixture(shape)
            cfg_arenas = (kernel.terminator_scalars, kernel.terminator_case_scalars,
                          kernel.error_edge_spans, kernel.error_edge_scalars)
            storage_cells = sum(len(arena) for arena in cfg_arenas)
            block_count = len(kernel.block_names)
            edge_count = sum(kernel.cfg_successor_count(block) for block in range(block_count))
            expected = finish(inspected_function(func, origins, kernel), origins, kernel)
            assert observed["gate_cells"] == storage_cells and observed["scheduler_entries"] == 1
            assert (observed["changed_rows"] == 0) == (shape == "positive-width-zero-change")
            shape_report = {"B": block_count, "E": edge_count,
                            "W": (len(shapes[shape][1]) + 29) // 30,
                            "tracked_values": len(shapes[shape][1]),
                            "changed_rows_observed": observed["changed_rows"],
                            "result_sha256": expected,
                            "S_actual_gate_cell_visits": observed["gate_cells"],
                            "scheduler_entries_observed": observed["scheduler_entries"],
                            "old_new_whole_liveness": [], "gate_only": []}
            report["shapes"][shape] = shape_report
            # Identical one-pass warmup and gc.collect policy for both arms.
            for name in ("old", "new"):
                func, origins, kernel = fixture(shape)
                gc.collect()
                assert finish(functions[name](func, origins, kernel), origins, kernel) == expected
            # Also exercise the imported production function unchanged.
            func, origins, kernel = fixture(shape)
            assert finish(stackmaps._native_managed_liveness(func, origins, kernel), origins, kernel) == expected
            for position, name in enumerate(report["measurement_order"]):
                func, origins, kernel = fixture(shape)
                gc.collect()
                cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
                result = functions[name](func, origins, kernel)
                wall_ns, cpu_ns = time.perf_counter_ns() - wall_start, time.process_time_ns() - cpu_start
                digest = finish(result, origins, kernel)
                assert digest == expected
                shape_report["old_new_whole_liveness"].append(
                    [position, name, wall_ns, cpu_ns])
            # Time the exact gate helper on the same ordinary arenas, outside
            # the whole-function samples. This does not estimate native cost.
            gate_namespace = dict(vars(stackmaps))
            gate_module = ast.Module(body=[ast.ImportFrom(module="__future__",
                                names=[ast.alias(name="annotations")], level=0),
                                copy.deepcopy(next(node for node in current.body
                                                   if isinstance(node, ast.FunctionDef)
                                                   and node.name == "cfg_arena_is_plain"))], type_ignores=[])
            exec(compile(ast.fix_missing_locations(gate_module), "<dirty-row-gate-CI-reference>", "exec"), gate_namespace)
            helper = gate_namespace["cfg_arena_is_plain"]
            func, origins, kernel = fixture(shape)
            arenas = (kernel.terminator_scalars, kernel.terminator_case_scalars,
                      kernel.error_edge_spans, kernel.error_edge_scalars)
            assert (helper(kernel.terminator_scalars)
                    and helper(kernel.terminator_case_scalars)
                    and helper(kernel.error_edge_spans)
                    and helper(kernel.error_edge_scalars))
            for _ in range(4):
                gc.collect()
                cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
                accepted = (helper(kernel.terminator_scalars)
                            and helper(kernel.terminator_case_scalars)
                            and helper(kernel.error_edge_spans)
                            and helper(kernel.error_edge_scalars))
                wall_ns, cpu_ns = time.perf_counter_ns() - wall_start, time.process_time_ns() - cpu_start
                assert accepted
                shape_report["gate_only"].append([wall_ns, cpu_ns])
            origins.close()
            kernel.close_native_tables()
        report["status"] = "measured"
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        # Existing basetemp JSON artifact collection owns this receipt.
        payload = json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n"
        if report["status"] == "measured":
            assert len(payload.encode()) <= 4096
        report_path.write_text(payload)
