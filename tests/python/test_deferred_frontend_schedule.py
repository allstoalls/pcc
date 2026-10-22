from itertools import combinations
import shlex

import pytest

from pcc.frontends.python import deferred_frontend_schedule as scheduler
from pcc.frontends.python import pipeline_frontend_workers as worker_policy


def test_call_node_score_matches_wire_marker_count():
    for payload in (
        b"",
        b'"Call"',
        b'prefix"Call"middle"Call"suffix',
        b'"Caller"\\"Call"\xff"Call"',
    ):
        assert scheduler._call_node_score(payload) == payload.count(b'"Call"')


@pytest.mark.integration
def test_call_node_score_executes_natively_under_all_collectors(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
):
    import inspect
    import os
    import subprocess

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    payloads = (
        b"",
        b'"Call"',
        b'prefix"Call"middle"Call"suffix',
        b'"Caller"\\"Call"\xff"Call"',
    )
    source = tmp_path / "ast_score.py"
    cases = repr(tuple((payload, payload.count(b'"Call"')) for payload in payloads))
    source.write_text(
        inspect.getsource(scheduler._call_node_score)
        + "\nfor payload, expected in " + cases + ":\n"
        + "    assert _call_node_score(payload) == expected\n"
        + "print('ast-score-ok')\n"
    )
    binary = tmp_path / "ast_score"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        run = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout.strip() == "ast-score-ok"


@pytest.mark.parametrize("budget_gib", [1, 2, 4, 8, 16])
@pytest.mark.parametrize("cpus", [1, 2, 4, 12, 64])
@pytest.mark.parametrize("phase", ["frontend", "pco0", "pco1", "pco2", "pco3", "pco4"])
def test_every_admitted_window_fits_the_budget(budget_gib, cpus, phase):
    sizes = [0, 30000, 200000, 1000000, 1900000, 3000000, 7000000, 14000000, 50000000]
    budget = budget_gib * 1073741824
    if phase == "frontend":
        floor = scheduler.indexed_frontend_floor_bytes
        groups = scheduler.frontend_groups(sizes, budget, cpus)
    else:
        gc_backend = int(phase[-1])
        floor = lambda size: scheduler.indexed_pco_floor_bytes(size, gc_backend)
        groups = scheduler.pco_groups(sizes, budget, cpus, gc_backend)
    assert sorted(index for indices, _ in groups for index in indices) == list(range(len(sizes)))
    for indices, width in groups:
        assert 1 <= width <= min(cpus, 12)
        # Check every possible active set, not only the first launch batch.
        if width > 1:
            for window in combinations(indices, min(len(indices), width)):
                charged = sum(floor(sizes[i]) for i in window)
                assert charged <= budget - 1073741824


def test_small_frontend_jobs_are_not_limited_by_combined_emit_estimates():
    groups = scheduler.frontend_groups([100000] * 30, 8 * 1073741824, 12)
    assert groups == [(list(range(30)), 9)]
    assert scheduler.frontend_groups([100000] * 30, 8 * 1073741824, 2) == [(list(range(30)), 2)]


def test_other_collectors_keep_the_legacy_frontend_reservation():
    size = 13_216_000
    legacy = 3 * 1073741824 // 4 + size * (19 * 1073741824 // 100) // 1000000
    for gc_backend in (1, 2, 3, 4, -1):
        assert scheduler.indexed_frontend_floor_bytes(size, gc_backend) == legacy


def test_pco_phase_uses_its_own_budget_instead_of_serial_safe_lane():
    sizes = [60000000, 14000000, 1000000, 1000000]
    assert scheduler.pco_groups(sizes, 6 * 1073741824, 12) == [([0], 3), ([1], 8), ([2, 3], 12)]
    for gc_backend in range(1, 5):
        assert scheduler.pco_groups(sizes, 6 * 1073741824, 12, gc_backend) == [([0], 1), ([1], 2), ([2, 3], 12)]
    assert scheduler.pco_groups([1000000] * 30, 6 * 1073741824, 2) == [(list(range(30)), 2)]


@pytest.mark.parametrize("raw,budget", [("2", ""), ("", ""), ("auto", ""), ("auto", "invalid")])
@pytest.mark.parametrize("phase", ["frontend", "pco"])
def test_unbudgeted_calls_preserve_conservative_policy(monkeypatch, raw, budget, phase):
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", raw)
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", budget)
    calls = []
    monkeypatch.setattr(scheduler, "run_worker_processes", lambda commands, width: calls.append((commands, width)))
    run = scheduler.run_frontend_commands if phase == "frontend" else scheduler.run_pco_commands
    run(["a", "b", "c"], ["unused"] * 3, 1, 2)
    assert calls == [(["a"], 1), (["b", "c"], 2)]


def _capture_resource_pool(monkeypatch):
    calls = []

    def capture(commands, tasks, width, budget, *, trace_path):
        calls.append((list(commands), tasks, width, budget, trace_path))

    def old_pool(*_args, **_kwargs):
        pytest.fail("budgeted commands reached a retired static pool")

    monkeypatch.setattr(scheduler, "run_resource_worker_processes", capture)
    monkeypatch.setattr(scheduler, "run_worker_processes", old_pool)
    monkeypatch.setattr(scheduler, "run_weighted_worker_processes", old_pool)
    monkeypatch.setattr(scheduler, "run_chained_worker_processes", old_pool)
    return calls


def _frontend_commands(tmp_path, sizes, exports=None):
    """Use the maintained command/manifest protocol at the scheduling boundary."""
    if exports is None:
        exports = tmp_path / "exports.json"
        exports.write_text("{}\n", encoding="utf-8")
    sources = []
    names = []
    for index, size in enumerate(sizes):
        source = tmp_path / ("source_" + str(index) + ".py")
        source.write_text("value = " + str(index) + "\n", encoding="utf-8")
        sources.append(str(source))
        names.append("pkg.module_" + str(index))
        with (tmp_path / ("module_" + str(index) + ".json")).open("wb") as stream:
            stream.truncate(size)
    manifests = []
    commands = []
    for index in range(len(sizes)):
        path = tmp_path / (str(index) + ".manifest")
        worker_policy.write_worker_manifest(
            str(path), str(tmp_path / (str(index) + ".result")),
            str(tmp_path / "ir"), str(exports), str(tmp_path), sources, names, [index],
            entry_module=names[0], sibling_inits=(), libpython_mode="off",
            ir_scaffold_mode="on", verbose=False,
        )
        manifests.append(str(path))
        commands.append(shlex.join(["pcc1", "--pcc-python-multi-codegen-worker", str(path)]))
    return commands, manifests


@pytest.mark.parametrize("raw,width", [("2", 2), ("", 12), ("auto", 12)])
def test_explicit_budget_keeps_shared_admission_for_numeric_and_auto_widths(
    tmp_path, monkeypatch, raw, width,
):
    commands, manifests = _frontend_commands(tmp_path, [30, 20, 10])
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", raw)
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "8589934592")
    monkeypatch.setattr(scheduler, "parallel_cpu_budget", lambda: 12)
    calls = _capture_resource_pool(monkeypatch)
    scheduler.run_frontend_commands(commands, manifests, 1, 2)
    selected_commands, tasks, selected_width, budget, trace = calls[0]
    assert len(calls) == 1 and selected_commands == commands
    assert selected_width == width and budget == 8 * 1073741824
    assert trace == manifests[0] + ".admission.tsv"
    assert len(tasks) == 3 and all(task["estimate_bytes"] == 0 for task in tasks)


def test_budgeted_frontend_rejects_unknown_commands_before_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "8589934592")
    calls = _capture_resource_pool(monkeypatch)
    with pytest.raises(ValueError, match="requires frontend worker manifests"):
        scheduler.run_frontend_commands(["unknown-command"], ["unused"], 0, 2)
    assert not calls


@pytest.mark.parametrize("gc_backend", ["0", "1", "2", "3", "4", "unknown"])
def test_auto_pco_describes_lazy_inputs_and_preserves_command_ownership(tmp_path, monkeypatch, gc_backend):
    sizes = [1000000, 60000000, 14000000, 1000000]
    sidecars = []
    for index, size in enumerate(sizes):
        path = tmp_path / (str(index) + ".pidx")
        with path.open("wb") as stream:
            stream.truncate(size)
        sidecars.append(str(path))
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "auto")
    monkeypatch.setenv("PCC_GC_BACKEND", gc_backend)
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(6 * 1073741824))
    monkeypatch.setattr(scheduler, "parallel_cpu_budget", lambda: 12)
    calls = _capture_resource_pool(monkeypatch)
    scheduler.run_pco_commands(["a", "b", "c", "d"], sidecars, 2, 1)
    commands, tasks, width, budget, trace = calls[0]
    assert len(calls) == 1 and commands == ["a", "b", "c", "d"]
    assert width == 12 and budget == 6 * 1073741824
    assert trace == sidecars[0] + ".pco-admission.tsv"
    assert [task["input_path"] for task in tasks] == sidecars
    assert all(not task["input_ready"] and task["depends_on"] == -1 for task in tasks)
    assert all(task["report_path"] == path + ".pco-rss" for task, path in zip(tasks, sidecars))
    expected_cost = ([scheduler._PCO_BASE, scheduler._PCO_PER_SIDECAR_MB]
                     if gc_backend == "0" else
                     [scheduler._PCO_LEGACY_BASE, scheduler._PCO_LEGACY_PER_SIDECAR_MB])
    assert all(task["input_cost"] == expected_cost for task in tasks)
    assert all(task["class"].endswith("|" + gc_backend) for task in tasks)
    calls.clear()
    with pytest.raises(ValueError, match="inventory mismatch"):
        scheduler.run_pco_commands(["a"], sidecars, 0, 1)
    assert not calls


def test_pco_missing_input_refuses_before_any_actual_process_launch(tmp_path, monkeypatch):
    from pcc.frontends.python import worker_process_pool

    sidecar = tmp_path / "present.pidx"
    sidecar.write_bytes(b"input")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "8589934592")
    monkeypatch.delenv("PCC_WORKER_TREE_STATE_PATH", raising=False)
    monkeypatch.setattr(
        worker_process_pool, "_start_resource_worker",
        lambda *_args: pytest.fail("a process launched before the missing input was rejected"),
    )
    with pytest.raises(OSError):
        scheduler.run_pco_commands(["a", "b"], [str(sidecar), str(tmp_path / "missing")], 0, 1)


def test_gc0_reservations_cover_all_392_system_peak_measurements():
    import json
    from pathlib import Path

    corpus = json.loads((Path(__file__).parents[1] / "data/pco_gc0_worker_peaks.json").read_text())
    assert corpus["gc_backend"] == 0
    assert len(corpus["workers"]) == 392
    for row in corpus["workers"]:
        peak = max(row["max_rss_bytes"], row["peak_footprint_bytes"])
        required = (peak * 5 + 3) // 4 + 128 * 1048576
        assert scheduler.indexed_pco_floor_bytes(row["input_bytes"]) >= required, row["index"]


def test_gc0_frontend_reservations_cover_all_392_system_peak_measurements():
    import json
    from pathlib import Path

    corpus = json.loads((Path(__file__).parents[1] / "data/frontend_gc0_worker_peaks.json").read_text())
    assert corpus["gc_backend"] == 0
    assert len(corpus["workers"]) == 392
    assert [row["index"] for row in corpus["workers"]] == list(range(392))
    for row in corpus["workers"]:
        peak = max(row["max_rss_bytes"], row["peak_footprint_bytes"])
        required = (peak * 5 + 3) // 4 + 128 * 1048576
        assert scheduler.indexed_frontend_floor_bytes(row["input_bytes"]) >= required, row["index"]


def test_gc0_closure_reservations_cover_all_392_system_peak_measurements():
    import json
    from pathlib import Path

    data = Path(__file__).parents[1] / "data"
    corpus = json.loads((data / "frontend_gc0_worker_peaks.json").read_text())
    closures = json.loads((data / "frontend_gc0_worker_closures.json").read_text())["modules"]
    assert len(closures) == 392
    for row in corpus["workers"]:
        peak = max(row["max_rss_bytes"], row["peak_footprint_bytes"])
        required = (peak * 5 + 3) // 4 + 128 * 1048576
        floor = scheduler.indexed_frontend_floor_bytes(
            row["input_bytes"], 0, closures[row["module"]],
        )
        assert floor >= required, row["index"]
        # The closure term only ever tightens the AST-only reservation's
        # 768 MiB base; it is not a second budget.
        assert floor <= scheduler.indexed_frontend_floor_bytes(row["input_bytes"]) * 2


def test_closure_floor_applies_to_gc0_only():
    assert scheduler.indexed_frontend_floor_bytes(1000000, 0, 0) == (
        482 * 1048576 + 125 * 1048576
    )
    for gc_backend in (1, 2, 3, 4, -1):
        assert scheduler.indexed_frontend_floor_bytes(1000000, gc_backend, 0) == (
            scheduler.indexed_frontend_floor_bytes(1000000, gc_backend)
        )


def _write_indexed_exports(path, payloads, dependencies):
    import json

    from pcc.frontends.python import pipeline_exports

    rows = [pipeline_exports._NATIVE_EXPORT_INDEXED_SCHEMA]
    for name, payload in payloads.items():
        rows.append("M\t" + name + "\t" + payload)
        rows.append("U\t" + name + "\t[[], []]")
    rows.append("P\t" + json.dumps(pipeline_exports._native_export_to_wire(dependencies)))
    for tag, value in (("D", {}), ("F", ()), ("T", ()), ("G", ())):
        rows.append(tag + "\t" + json.dumps(pipeline_exports._native_export_to_wire(value)))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_export_closure_bytes_follow_dependency_closure(tmp_path):
    exports = tmp_path / "native_exports.json"
    _write_indexed_exports(
        exports,
        {"a": "x" * 10, "b": "y" * 100, "c": "z" * 1000, "d": "w" * 5},
        {"a": ("b",), "b": ("c",), "c": (), "d": ("a", "c")},
    )
    assert scheduler.export_closure_bytes(str(exports), ["a", "b", "c", "d"]) == [
        1110, 1100, 1000, 1115,
    ]
    assert scheduler.export_closure_bytes(str(exports), ["missing"]) is None
    assert scheduler.export_closure_bytes(str(tmp_path / "absent"), ["a"]) is None
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}\n", encoding="utf-8")
    assert scheduler.export_closure_bytes(str(legacy), ["a"]) is None


@pytest.mark.parametrize("gc_backend", ["0", "3"])
def test_legacy_frontend_cost_model_reads_export_closure(tmp_path, monkeypatch, gc_backend):
    exports = tmp_path / "native_exports.json"
    names = ["root", "leaf"]
    _write_indexed_exports(
        exports, {"root": "r" * 3000000, "leaf": "l" * 2000000},
        {"root": ("leaf",), "leaf": ()},
    )
    sizes = [100000, 200000]
    manifests = []
    for index, size in enumerate(sizes):
        ast = tmp_path / ("module_" + str(index) + ".json")
        with ast.open("wb") as stream:
            stream.truncate(size)
        manifest = tmp_path / (str(index) + ".manifest")
        manifest.write_text("\n".join(
            ["pcc.frontends.python.codegen_worker.v4", "result", "out", str(exports),
             "codegen", str(tmp_path), "root", "off", "on", "0", "0", str(len(names))]
            + [str(i) + "\t" + name + "\tsrc.py" for i, name in enumerate(names)]
            + ["1", str(index)]
        ) + "\n")
        manifests.append(str(manifest))
    monkeypatch.setenv("PCC_GC_BACKEND", gc_backend)
    floors, _order = scheduler._frontend_floors_and_order(["a", "b"], manifests)
    if gc_backend == "0":
        expected = [
            scheduler.indexed_frontend_floor_bytes(sizes[0], 0, 5000000),
            scheduler.indexed_frontend_floor_bytes(sizes[1], 0, 2000000),
        ]
    else:
        expected = [scheduler.indexed_frontend_floor_bytes(size, -1) for size in sizes]
    assert sorted(floors) == sorted(expected)


@pytest.mark.parametrize("gc_backend", ["0", "1", "2", "3", "4", "unknown"])
def test_auto_describes_each_assigned_ast_for_measured_pool(tmp_path, monkeypatch, gc_backend):
    sizes = [1400, 700, 10, 10]
    commands, manifests = _frontend_commands(tmp_path, sizes)
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "auto")
    monkeypatch.setenv("PCC_GC_BACKEND", gc_backend)
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "8589934592")
    monkeypatch.setattr(scheduler, "parallel_cpu_budget", lambda: 12)
    calls = _capture_resource_pool(monkeypatch)
    scheduler.run_frontend_commands(commands, manifests, 2, 2)
    selected_commands, tasks, width, budget, trace = calls[0]
    assert len(calls) == 1 and selected_commands == commands
    assert width == 12 and budget == 8 * 1073741824
    assert trace == manifests[0] + ".admission.tsv"
    for index, task in enumerate(tasks):
        assert task["inputs"][2:4] == [sizes[index], sizes[index]]
        assert task["inputs"][-2:] == [(tmp_path / "exports.json").stat().st_size, 1]
        assert "|module-indexed|" + gc_backend + "|" in task["class"]
        assert task["estimate_bytes"] == 0 and task["source_identity"]
        assert task["report_path"] == manifests[index] + ".rss"


def test_frontend_and_pco_admission_execute_natively_under_all_collectors(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
):
    import inspect
    import os
    import subprocess

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    sizes = [0, 129227, 1000000, 6851869, 11263590, 14911544, 60000000]
    cases = []
    frontend_cases = []
    for budget_gib in (1, 6, 8):
        for cpus in (1, 12, 64):
            budget = budget_gib * 1073741824
            for gc_backend in range(5):
                cases.append((budget, cpus, gc_backend, scheduler.pco_groups(sizes, budget, cpus, gc_backend)))
                frontend_cases.append((
                    budget, cpus, gc_backend,
                    scheduler.frontend_groups(sizes, budget, cpus, gc_backend),
                ))
    source = tmp_path / "pco_admission.py"
    constants = "\n".join(
        name + " = " + repr(getattr(scheduler, name))
        for name in ("_GIB", "_MIB", "_DRIVER_RESERVE", "_FRONTEND_BASE",
                     "_FRONTEND_GC0_PER_AST_MB", "_FRONTEND_LEGACY_PER_AST_MB",
                     "_FRONTEND_GC0_CLOSURE_BASE", "_FRONTEND_GC0_CLOSURE_PER_AST_MB",
                     "_FRONTEND_GC0_PER_CLOSURE_MB",
                     "_PCO_BASE", "_PCO_PER_SIDECAR_MB",
                     "_PCO_LEGACY_BASE", "_PCO_LEGACY_PER_SIDECAR_MB", "_PCO_CAP", "_MAX_WIDTH")
    )
    source.write_text(constants + "\n\n" + "\n\n".join(
        inspect.getsource(function) for function in (
            scheduler.indexed_frontend_floor_bytes, scheduler.indexed_pco_floor_bytes,
            scheduler._admission_groups, scheduler.frontend_groups, scheduler.pco_groups,
        )
    ) + "\nsizes = " + repr(sizes) + "\ncases = " + repr(cases) + "\nfrontend_cases = " + repr(frontend_cases) + '''
def main():
    for budget, cpus, gc_backend, expected in cases:
        assert pco_groups(sizes, budget, cpus, gc_backend) == expected
    for budget, cpus, gc_backend, expected in frontend_cases:
        assert frontend_groups(sizes, budget, cpus, gc_backend) == expected
    print("admission-ok")
main()
''')
    binary = tmp_path / "pco_admission"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout.strip() == "admission-ok"


@pytest.mark.parametrize("gc_backend", ["0", "3"])
@pytest.mark.parametrize("raw,width", [("auto", 12), ("2", 2)])
def test_budgeted_chain_keeps_each_pco_dependent_on_its_frontend(
    tmp_path, monkeypatch, gc_backend, raw, width,
):
    """A missing PCO input remains deferred until its own producer completes."""
    commands, manifests = _frontend_commands(tmp_path, [100, 1400, 700])
    sidecars = [str(tmp_path / ("s" + str(index) + ".pidx")) for index in range(3)]
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", raw)
    monkeypatch.setenv("PCC_GC_BACKEND", gc_backend)
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "8589934592")
    monkeypatch.setattr(scheduler, "parallel_cpu_budget", lambda: 12)
    calls = _capture_resource_pool(monkeypatch)
    scheduler.run_frontend_pco_commands(
        commands, manifests, ["p0", "p1", "p2"], sidecars, 0, 2,
    )
    selected_commands, tasks, selected_width, budget, trace = calls[0]
    assert len(calls) == 1 and selected_commands == commands + ["p0", "p1", "p2"]
    assert selected_width == width and budget == 8 * 1073741824
    assert trace == manifests[0] + ".chained-admission.tsv"
    assert len(tasks) == 6
    assert all(task.get("depends_on", -1) == -1 for task in tasks[:3])
    assert [task["depends_on"] for task in tasks[3:]] == [0, 1, 2]
    assert [task["input_path"] for task in tasks[3:]] == sidecars
    assert all(not task["input_ready"] for task in tasks[3:])
    assert all(task["class"].endswith("|" + gc_backend) for task in tasks[3:])


def test_unbudgeted_chained_call_keeps_the_two_phases_in_order(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "2")
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    calls = []
    monkeypatch.setattr(scheduler, "run_worker_processes", lambda commands, width: calls.append((commands, width)))
    scheduler.run_frontend_pco_commands(
        ["f0", "f1"], ["unused"] * 2, ["p0", "p1"], ["s0", "s1"], 1, 2,
    )
    assert calls == [(["f0"], 1), (["f1"], 2), (["p0"], 1), (["p1"], 2)]


def test_chain_switch_off_keeps_the_phases_sequential(monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "auto")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "8589934592")
    monkeypatch.setenv("PCC_FRONTEND_PCO_CHAIN", "0")
    calls = []
    monkeypatch.setattr(scheduler, "run_frontend_commands", lambda *a: calls.append(("frontend", a[0])))
    monkeypatch.setattr(scheduler, "run_pco_commands", lambda *a: calls.append(("pco", a[0])))
    monkeypatch.setattr(
        scheduler, "run_chained_worker_processes",
        lambda *a: pytest.fail("chaining ran with PCC_FRONTEND_PCO_CHAIN=0"),
    )
    scheduler.run_frontend_pco_commands(["f"], ["m"], ["p"], ["s"], 0, 2)
    assert calls == [("frontend", ["f"]), ("pco", ["p"])]
