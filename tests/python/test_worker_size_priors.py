"""Empirical priors are soft reservations; actual RSS remains authoritative."""

import copy
import shlex
import sys

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_resource_plan as policy


MIB = 1024 * 1024


def test_failed_process_fixture_keeps_bounded_synthetic_protocol_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.python import test_worker_size_prior_processes as fixture

    report = policy.RESOURCE_REPORT_SCHEMA + "\n123\nleader-grown\n300\n400\nattempt-token\n"

    def failed_child(command, *, env, timeout):
        assert timeout == 20 and env["PCC_WORKER_TREE_BUDGET_BYTES"] == str(1024 * MIB)
        root = fixture.Path(command[-1])
        (root / "rss0").write_text(report)
        (root / "started.0.1").write_text("123\tattempt-token\tcpython\t0")
        (root / "admission.tsv").write_text("failure-budget\t-1\t0\t900\t800\t0\t1\n")
        (root / "complete.0.1").write_text("x" * 9000)
        (root / "unrelated-secret").write_text("must not be copied")
        assert env.get("PCC_TEST_WINDOWS_STAGE1_PHASE", "") in ("", "1")
        if env.get("PCC_TEST_WINDOWS_STAGE1_PHASE") == "1":
            (root / "windows-stage1-phase").write_text("model\n")
        return SimpleNamespace(returncode=1, stdout="original stdout", stderr="original failure")

    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(1024 * MIB))
    monkeypatch.setenv("PCC_TEST_WINDOWS_STAGE1_PHASE", "ambient-must-not-enable")
    monkeypatch.setattr(fixture, "run_process_group_timeout", failed_child)
    with pytest.raises(AssertionError) as caught:
        fixture._run(["unused"], "growth", tmp_path / "growth", "cpython", 0)
    record = fixture.json.loads((tmp_path / "growth" / "execution.json").read_text())
    assert (record["returncode"], record["stdout"], record["stderr"]) == (
        1, "original stdout", "original failure",
    )
    assert "original failure" in str(caught.value)
    evidence = record["failure_evidence"]
    assert set(evidence) == {"rss0", "started.0.1", "admission.tsv", "complete.0.1"}
    assert evidence["rss0"] == {"text": report, "truncated": False}
    assert evidence["complete.0.1"] == {"text": "x" * 8192, "truncated": True}
    assert record["windows_stage1_phase_witness"] is False
    for index, (target, compiler, owner, collector, case, enabled) in enumerate((
        ("win32", "pcc0", "pcc", 0, "width", True),
        ("win32", "pcc1", "pcc", 0, "width", False),
        ("win32", "pcc0", "pcc", 1, "width", False),
        ("win32", "pcc0", "pcc", 0, "growth", False),
        ("win32", "pcc0", "cpython", 0, "width", False),
        ("linux", "pcc0", "pcc", 0, "width", False),
        ("darwin", "pcc0", "pcc", 0, "width", False),
    )):
        monkeypatch.setattr(fixture, "sys", SimpleNamespace(platform=target))
        root = tmp_path / ("phase-" + str(index))
        with pytest.raises(AssertionError):
            fixture._run(["unused"], case, root, owner, collector, compiler)
        result = fixture.json.loads((root / "execution.json").read_text())
        assert result["windows_stage1_phase_witness"] is enabled
        assert ("windows-stage1-phase" in result["failure_evidence"]) is enabled
        if enabled:
            assert result["failure_evidence"]["windows-stage1-phase"] == {
                "text": "model\n", "truncated": False,
            }

    # The source witness uses one truncating file and fixed short literals.
    # It does not include paths, command lines, environment values or user data.
    import ast
    tree = ast.parse(fixture.DRIVER.read_text())
    phase = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_windows_stage1_phase")
    opens = [node for node in ast.walk(phase) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "open"]
    assert len(opens) == 1 and ast.literal_eval(opens[0].args[1]) == "w"
    assert isinstance(opens[0].args[0], ast.BinOp)
    assert ast.literal_eval(opens[0].args[0].right) == "/windows-stage1-phase"
    run_case = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "run_case")
    boundaries = [ast.literal_eval(node.value.args[1]) for node in run_case.body
                  if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                  and isinstance(node.value.func, ast.Name)
                  and node.value.func.id == "_windows_stage1_phase"]
    assert boundaries == ["owner", "model", "rss-current", "rss-peak", "command", "budget"]
    command = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "command")
    command_boundaries = [ast.literal_eval(node.value.args[1]) for node in command.body
                          if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                          and isinstance(node.value.func, ast.Name)
                          and node.value.func.id == "_windows_stage1_phase"]
    assert command_boundaries == ["abspath", "argv", "quote"]
    assert max(len(value) + 1 for value in boundaries + command_boundaries) <= 12


@pytest.mark.parametrize("valid_token", [False, True])
def test_failed_pool_evidence_uses_latest_accepted_peer_charge(tmp_path, monkeypatch, valid_token):
    import subprocess
    from types import SimpleNamespace
    from pcc.frontends.python import worker_process_pool as pool

    tasks = [task("host-export-v1", source=1, ast=0, exports=0),
             task("host-export-v1", source=0, ast=0, exports=0)]
    for index, item in enumerate(tasks):
        item["report_path"] = str(tmp_path / ("rss" + str(index)))
        item["class"] += ":" + str(index)
    priors = [policy.task_startup_prior_bytes(item) for item in tasks]
    available = sum(priors) + 64 * MIB
    owner = 32 * MIB
    live, tokens, stopped = set(), {}, []
    observations = []
    clock = SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda seconds: None)

    def write_report(index, peak, token):
        with open(tasks[index]["report_path"], "w") as stream:
            stream.write(policy.RESOURCE_REPORT_SCHEMA + "\n" + str(100 + index)
                         + "\nallocated\n" + str(peak) + "\n" + str(peak) + "\n" + token + "\n")

    def start(specs, index):
        tokens[index] = next(value.split("=", 1)[1] for value in specs[index][1]
                             if value.startswith(policy.RESOURCE_TOKEN_ENV + "="))
        live.add(100 + index)
        write_report(index, 16 * MIB, tokens[index])
        if index == 1:
            write_report(0, 320 * MIB, tokens[0] if valid_token else "stale-token")
        return 100 + index

    def stop(pid):
        stopped.append(pid)
        live.remove(pid)

    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "0")
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: owner)
    monkeypatch.setattr(pool, "time", clock)
    monkeypatch.setattr(policy, "time", clock)
    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", lambda pid: 7 if pid == 101 else pool._WORKER_RUNNING)
    monkeypatch.setattr(pool, "_retire_resource_worker", live.remove)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    trace = tmp_path / "admission.tsv"
    with pytest.raises(subprocess.CalledProcessError) as caught:
        pool.run_resource_worker_processes(["leader", "peer"], tasks, 2,
            owner + policy.RSS_HEADROOM_BYTES + available,
            observations=observations, trace_path=str(trace))
    assert caught.value.returncode == 7 and caught.value.cmd == "peer"
    rows = [line.split("\t") for line in trace.read_text().splitlines()]
    assert all(len(row) == 7 for row in rows)
    accepted_peak = (320 if valid_token else 16) * MIB
    charge = max(priors[0], policy.peak_reservation(accepted_peak))
    peer = next(row for row in rows if row[0] == "failure-active")
    assert peer[1:6] == ["0", "100", str(charge), str(available), str(accepted_peak)]
    budget = next(row for row in rows if row[0] == "failure-budget")
    assert budget[1:6] == ["-1", "0", str(charge + priors[1]), str(available), "0"]
    assert (charge + priors[1] > available) == valid_token
    assert not live and stopped == [100] and observations == []
    assert not any(row[0] in ("cancel", "retire") for row in rows)


@pytest.mark.parametrize("mode", [
    "venv", "venv-case", "other-python", "wrapper", "native-command",
    "non-windows", "system-python", "no-base", "other-owner",
])
def test_resource_start_owns_windows_venv_interpreter_without_changing_environment(monkeypatch, mode):
    import ntpath
    from types import SimpleNamespace
    from pcc.frontends.python import worker_process_pool as pool

    executable = r"C:\project venv\Scripts\python.exe"
    base = r"C:\Python313\python.exe"
    requested = executable
    platform = "linux" if mode == "non-windows" else "win32"
    owner = "pypy" if mode == "other-owner" else "cpython"
    if mode == "venv-case":
        requested = executable.upper()
    elif mode == "other-python":
        requested = r"C:\other venv\Scripts\python.exe"
    elif mode == "wrapper":
        requested = "wrapper.exe"
    elif mode == "native-command":
        requested = r"C:\project\pcc1.exe"
    elif mode == "system-python":
        base = executable
    system = SimpleNamespace(platform=platform, executable=executable,
                             implementation=SimpleNamespace(name=owner))
    if mode != "no-base":
        system._base_executable = base
    monkeypatch.setattr(pool, "sys", system)
    monkeypatch.setattr(pool, "os", SimpleNamespace(path=ntpath))
    monkeypatch.setattr(pool, "_HOST_WORKERS", {})
    argv = [requested, "-B", "worker with spaces.py", "", "quote'word"]
    environment = {"PCC_TEST_VALUE": "value=with spaces",
                   policy.RESOURCE_REPORT_ENV: r"C:\reports\worker.rss",
                   policy.RESOURCE_TOKEN_ENV: "a" * 64,
                   "__PYVENV_LAUNCHER__": "inherited-launcher"}
    specs = [(argv, [key + "=" + value for key, value in environment.items()])]
    original = copy.deepcopy(specs)
    captured = []
    process = SimpleNamespace(pid=1234)

    def popen(arguments, **options):
        captured.append((arguments, options))
        return process

    monkeypatch.setattr(pool.subprocess, "Popen", popen)
    assert pool._start_resource_worker(specs, 0) == process.pid
    assert pool._HOST_WORKERS == {process.pid: process}
    assert specs == original
    rewritten = mode in ("venv", "venv-case")
    expected = dict(environment)
    if rewritten:
        expected["__PYVENV_LAUNCHER__"] = executable
    options = {"env": expected}
    options.update({"creationflags": 512} if platform == "win32" else {"process_group": 0})
    assert captured == [([base if rewritten else requested, *argv[1:]], options)]


def task(model="host-indexed-frontend-v1", source=100, ast=200, exports=300):
    return {
        "class": "private-run:host", "estimate_bytes": 0,
        "inputs": [source, source, ast, ast, exports, 1],
        "startup_prior_model": model, "report_path": "unused", "restartable": True,
        "diagnostic_phase": "codegen", "diagnostic_indices": [0],
    }


@pytest.mark.parametrize("model,working", [
    ("host-export-v1", 160 * MIB + 160 * 100),
    ("host-summary-v1", 192 * MIB + 5 * (200 + 300)),
    ("host-codegen-v1", 128 * MIB + 224 * (100 + 200) + 43 * 300),
    ("host-indexed-frontend-v1", 128 * MIB + 80 * (100 + 200) + 11 * 300),
])
def test_phase_models_use_bytes_and_apply_existing_margin_once(model, working):
    item = task(model)
    assert policy.task_startup_prior_bytes(item) == policy.peak_reservation(working)
    assert policy.estimated_task_bytes(item, []) == policy.peak_reservation(working)
    assert policy.minimum_task_bytes(item) == 0


@pytest.mark.parametrize("count,exports", [(2, 1), (2, 24327614), (3, 7), (7, 123)])
def test_codegen_batch_prior_is_exact_sum_of_all_singleton_margins(count, exports):
    members = [task("host-codegen-v1", source=13 + index * 19,
                    ast=5 + index * 7, exports=exports) for index in range(count)]
    combined = task("host-codegen-v1", exports=exports)
    combined["inputs"] = [
        sum(item["inputs"][0] for item in members),
        max(item["inputs"][0] for item in members),
        sum(item["inputs"][2] for item in members),
        max(item["inputs"][2] for item in members), exports, count,
    ]
    assert policy.task_startup_prior_bytes(combined) == sum(
        policy.task_startup_prior_bytes(item) for item in members)
    assert policy.minimum_task_bytes(combined) == 0
    assert combined["estimate_bytes"] == 0


def test_backend_prior_requires_ready_verified_complete_file_size():
    item = task("host-indexed-backend-v1")
    item["inputs"] = [999999] * 47
    assert policy.task_startup_prior_bytes(item) == 0
    item["input_ready"] = True
    with pytest.raises(policy.WorkerMemoryError, match="verified packed bytes"):
        policy.task_startup_prior_bytes(item)
    item["prior_input_bytes"] = 12345
    assert policy.task_startup_prior_bytes(item) == policy.peak_reservation(64 * MIB + 7 * 12345)
    # The header and arena counts are not substituted for whole-file bytes.
    item["inputs"] = [1] * 47
    assert policy.task_startup_prior_bytes(item) == policy.peak_reservation(64 * MIB + 7 * 12345)


# Sealed kernel shape and measured peak of the backend that compiled
# pcc.frontends.python.codegen.call_expression_lowering on a macOS CI Stage1
# (run 38164415315): its largest function drove the peak past the v1
# forecast's margin, the worst of 5,052 recorded backend completions.
CALL_DISPATCH_SHAPE = [
    15296891, 54, 2912, 2057152, 1721696, 1830700, 1523284, 467416, 403380,
    2269628, 1922784, 567407, 480696, 2269628, 1922784, 300644, 257102, 1202576,
    1028408, 3976, 2870, 13920, 10768, 3588, 2351, 0, 0, 934832, 806760, 233708,
    201690, 7112, 5924, 7112, 5924, 0, 0, 233708, 201690, 0, 0, 2699360, 2259488,
    337420, 282436, 592677, 504695,
]
CALL_DISPATCH_PEAK = 1622556672
CALL_DISPATCH_PIDX_BYTES = 15296891 + 8 * sum(CALL_DISPATCH_SHAPE[3::2])


def test_backend_prior_v2_reads_total_and_largest_function_arena_entries():
    item = task("host-indexed-backend-v2")
    item["inputs"] = [100, 3, 2] + [10, 4] * 22
    assert policy.task_startup_prior_bytes(item) == 0
    item["input_ready"] = True
    with pytest.raises(policy.WorkerMemoryError, match="verified packed bytes"):
        policy.task_startup_prior_bytes(item)
    item["prior_input_bytes"] = 100 + 8 * 220
    assert policy.task_startup_prior_bytes(item) == policy.peak_reservation(
        64 * MIB + 40 * 220 + 48 * 88)
    # Header bytes, function and global counts are not arena entries.
    item["inputs"] = [10 ** 9, 10 ** 6, 10 ** 6] + [10, 4] * 22
    assert policy.task_startup_prior_bytes(item) == policy.peak_reservation(
        64 * MIB + 40 * 220 + 48 * 88)


@pytest.mark.parametrize("bad", [[1] * 46, [1, 1], [1, 1, 1, -1, 0], [1, 1, 1, True, 0], [1, 1, 1, 1.5, 0]])
def test_backend_prior_v2_rejects_an_unsealed_shape(bad):
    item = task("host-indexed-backend-v2")
    item.update(input_ready=True, prior_input_bytes=4096, inputs=bad)
    with pytest.raises(policy.WorkerMemoryError, match="sealed kernel shape"):
        policy.task_startup_prior_bytes(item)


def test_backend_prior_v2_covers_the_large_function_module_v1_missed():
    item = task("host-indexed-backend-v2")
    item.update(input_ready=True, prior_input_bytes=CALL_DISPATCH_PIDX_BYTES,
                inputs=list(CALL_DISPATCH_SHAPE))
    assert policy.task_startup_prior_bytes(item) >= CALL_DISPATCH_PEAK
    old = dict(item, startup_prior_model="host-indexed-backend-v1")
    assert policy.task_startup_prior_bytes(old) < CALL_DISPATCH_PEAK


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "123"])
def test_backend_prior_rejects_unverified_byte_types(bad):
    item = task("host-indexed-backend-v1")
    item.update(input_ready=True, prior_input_bytes=bad)
    with pytest.raises(policy.WorkerMemoryError, match="verified packed bytes"):
        policy.task_startup_prior_bytes(item)


@pytest.mark.parametrize("bad", [[1], [1] * 7, [-1, 0, 0, 0, 0, 1], [True, 0, 0, 0, 0, 1]])
def test_frontend_prior_rejects_unknown_or_negative_dimensions(bad):
    item = task()
    item["inputs"] = bad
    with pytest.raises(policy.WorkerMemoryError, match="source/AST/export bytes"):
        policy.task_startup_prior_bytes(item)


def test_unrecognized_model_fails_closed_and_absent_model_keeps_legacy_calibration():
    item = task("unknown-owner-v1")
    with pytest.raises(policy.WorkerMemoryError, match="unknown worker startup prior"):
        policy.estimated_task_bytes(item, [])
    del item["startup_prior_model"]
    assert policy.estimated_task_bytes(item, []) == 0
    assert policy.choose_task([0], [item], [], [], 3, 1024 * MIB) == (0, 1024 * MIB, True)


def test_prior_never_replaces_explicit_or_incomplete_lower_bound():
    item = task()
    prior = policy.task_startup_prior_bytes(item)
    item["estimate_bytes"] = prior + 5
    item["incomplete_peak_bytes"] = prior + 7
    assert policy.minimum_task_bytes(item) == prior + 7
    assert policy.estimated_task_bytes(item, []) == policy.peak_reservation(prior + 7)
    assert item["estimate_bytes"] == prior + 5


def test_covering_observations_still_use_the_unchanged_full_maximum():
    item = task()
    observations = [
        (item["class"], list(item["inputs"]), 900 * MIB),
        (item["class"], [2 * value for value in item["inputs"]], 10 * MIB),
        ("other-owner", [3 * value for value in item["inputs"]], 2000 * MIB),
    ]
    assert policy.estimated_task_bytes(item, observations) == policy.peak_reservation(900 * MIB)
    assert len(observations) == 3


def test_completed_modeled_observation_binds_original_prior_and_copies_inputs():
    item = task()
    prior = policy.task_startup_prior_bytes(item)
    sample = policy.completed_task_observation(item, 400 * MIB)
    assert sample == (item["class"], item["inputs"], 400 * MIB,
                      (item["startup_prior_model"], prior))
    item["inputs"][0] += 1
    assert sample[1][0] == 100
    del item["startup_prior_model"]
    assert policy.completed_task_observation(item, 400 * MIB) == (
        item["class"], item["inputs"], 400 * MIB)


def test_unprepared_backend_cannot_publish_a_modeled_completion():
    with pytest.raises(policy.WorkerMemoryError, match="no startup prior"):
        policy.completed_task_observation(task("host-indexed-backend-v1"), MIB)


def test_largest_completed_task_does_not_transfer_absolute_peak_to_small_input():
    large = task(source=10 * MIB, ast=0, exports=0)
    small = task(source=1, ast=0, exports=0)
    sample = policy.completed_task_observation(large, 600 * MIB)
    assert sample[3][1] > policy.peak_reservation(sample[2])
    assert policy.estimated_task_bytes(small, [sample]) == policy.task_startup_prior_bytes(small)
    assert policy.estimated_task_bytes(small, [sample]) < policy.peak_reservation(sample[2])


def test_underestimated_completion_raises_size_scale_for_small_and_large_tasks():
    measured = task(source=2, ast=0, exports=0)
    sample = policy.completed_task_observation(measured, 600 * MIB)
    for size in (1, 2, 10 * MIB):
        item = task(source=size, ast=0, exports=0)
        prior = policy.task_startup_prior_bytes(item)
        expected = (prior * policy.peak_reservation(sample[2]) + sample[3][1] - 1) // sample[3][1]
        assert policy.estimated_task_bytes(item, [sample]) == expected > prior
    assert policy.estimated_task_bytes(measured, [sample]) == policy.peak_reservation(600 * MIB)


def test_upward_scale_never_reduces_same_task_explicit_or_cancelled_peak_floor():
    item = task()
    sample = policy.completed_task_observation(task(source=10 * MIB), MIB)
    item["estimate_bytes"] = 900 * MIB
    item["incomplete_peak_bytes"] = 1000 * MIB
    assert policy.minimum_task_bytes(item) == 1000 * MIB
    assert policy.estimated_task_bytes(item, [sample]) == policy.peak_reservation(1000 * MIB)


def test_modeled_and_legacy_samples_coexist_without_cross_class_pollution():
    item = task()
    same = policy.completed_task_observation(item, 400 * MIB)
    foreign = ("other", list(item["inputs"]), 999999999999999, ("other-model", 0))
    legacy = (item["class"], [2 * value for value in item["inputs"]], 700 * MIB)
    assert policy.estimated_task_bytes(item, [same, foreign, legacy]) == policy.peak_reservation(700 * MIB)
    del item["startup_prior_model"]
    assert policy.estimated_task_bytes(item, [same]) == policy.peak_reservation(400 * MIB)


@pytest.mark.parametrize("binding", [None, (), ("host-indexed-frontend-v1",),
    ("host-indexed-frontend-v1", 0), ("host-indexed-frontend-v1", -1),
    ("host-indexed-frontend-v1", True), ("host-indexed-frontend-v1", 1.5),
    ("host-indexed-frontend-v1", "1"), ("other-model", 1)])
def test_invalid_same_class_prior_sample_fails_closed(binding):
    item = task()
    with pytest.raises(policy.WorkerMemoryError, match="prior binding differs"):
        policy.estimated_task_bytes(item, [(item["class"], item["inputs"], MIB, binding)])


def test_scale_uses_exact_integer_ceil_beyond_machine_word_and_float_precision():
    item = task(source=10**40, ast=0, exports=0)
    prior = policy.task_startup_prior_bytes(item)
    denominator, peak = 2**100 + 17, 2**104 + 23
    sample = (item["class"], item["inputs"], peak, (item["startup_prior_model"], denominator))
    numerator = prior * policy.peak_reservation(peak)
    quotient, remainder = divmod(numerator, denominator)
    assert remainder != 0
    assert policy.estimated_task_bytes(item, [sample]) == quotient + 1


def test_calibrated_forecast_above_available_drains_for_exclusive_retry_space():
    tasks = [task(source=2, ast=0, exports=0), task(source=1, ast=0, exports=0)]
    sample = policy.completed_task_observation(tasks[0], 900 * MIB)
    available = policy.task_startup_prior_bytes(tasks[0]) + MIB
    assert policy.task_startup_prior_bytes(tasks[0]) < available
    assert policy.choose_task([0, 1], tasks, [sample], [MIB], 3, available, True) == (-1, 0, False)
    index, demand, exclusive = policy.choose_task([0, 1], tasks, [sample], [], 3, available, True)
    assert index == 0 and exclusive and demand > available


def test_largest_first_host_order_has_no_small_cohort_barrier_and_stable_ties():
    tasks = [task(source=size, ast=0, exports=0) for size in (1, 30, 30, 2)]
    before = copy.deepcopy(tasks)
    order, bands = policy.resource_task_order(tasks)
    assert order == [1, 2, 3, 0]
    assert bands == []
    ready = [0, 2]
    assert policy.ready_resource_cohort(ready, order, [1], bands) is ready
    assert tasks == before


def test_unknown_host_priors_fill_width_without_exclusive_calibration():
    tasks = [task(source=size, ast=0, exports=0) for size in (1, 30, 2)]
    for item in tasks:
        item["calibrate_before_peers"] = True
    pending = [0, 1, 2]
    active = []
    expected = [1, 2, 0]
    available = sum(policy.task_startup_prior_bytes(item) for item in tasks)
    for index in expected:
        selected, demand, exclusive = policy.choose_task(pending, tasks, [], active, 3, available, True)
        assert selected == index and not exclusive
        assert demand == policy.task_startup_prior_bytes(tasks[index])
        active.append(demand)
        pending.remove(index)
    assert sum(active) == available
    assert policy.choose_task([0], tasks, [], active, 3, available, True) == (-1, 0, False)


def test_largest_fitting_ready_prior_does_not_lower_a_live_reservation():
    tasks = [task(source=size, ast=0, exports=0) for size in (3000000, 100, 100000)]
    live = 600 * MIB
    small = policy.task_startup_prior_bytes(tasks[1])
    available = live + small
    selected, demand, exclusive = policy.choose_task([0, 1, 2], tasks, [], [live], 3, available, True)
    assert (selected, demand, exclusive) == (1, small, False)
    assert policy.choose_task([0, 2], tasks, [], [live, small], 3, available, True) == (-1, 0, False)


def test_prior_above_available_drains_then_uses_guarded_exclusive_without_a_hard_floor():
    tasks = [task(source=1000000, ast=0, exports=0), task(source=1, ast=0, exports=0)]
    prior = policy.task_startup_prior_bytes(tasks[0])
    available = prior - 1
    assert policy.minimum_task_bytes(tasks[0]) == 0
    assert policy.choose_task([1, 0], tasks, [], [MIB], 3, available, True) == (-1, 0, False)
    assert policy.choose_task([1, 0], tasks, [], [], 3, available, True) == (0, prior, True)
    assert tasks[0]["estimate_bytes"] == 0


def test_retry_still_drains_and_precedes_larger_priors():
    tasks = [task(source=4000000), task(source=1)]
    tasks[1]["retry_calibration"] = True
    tasks[1]["incomplete_peak_bytes"] = 50 * MIB
    available = 2 * 1024 * MIB
    assert policy.choose_task([0, 1], tasks, [], [MIB], 3, available, True) == (-1, 0, False)
    assert policy.choose_task([0, 1], tasks, [], [], 3, available, True) == (1, available, True)


def test_newly_materialized_backend_size_is_ranked_after_readiness():
    frontend = task(source=1, ast=1, exports=1)
    backend = task("host-indexed-backend-v1")
    backend["inputs"] = [0] * 47
    tasks = [frontend, backend]
    assert policy.resource_task_order(tasks)[0] == [0, 1]
    backend.update(input_ready=True, prior_input_bytes=50 * MIB)
    selected, demand, exclusive = policy.choose_task([0, 1], tasks, [], [], 3, 2 * 1024 * MIB, True)
    assert selected == 1 and demand == policy.task_startup_prior_bytes(backend) and not exclusive


@pytest.mark.parametrize("kind", ["export", "summary", "codegen"])
@pytest.mark.parametrize("owner", ["host", "host-env", "native", "wrapper", "different-python", "host-extra"])
def test_only_the_actual_cpython_worker_prefix_receives_a_host_prior(tmp_path, monkeypatch, kind, owner):
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint

    manifest = dict(assigned_indices=[0], job_kind=kind, ast_dir="",
                    exports_path="", src_paths=["one.py"], module_names=["pkg.one"])
    monkeypatch.setattr(workers, "read_worker_manifest", lambda path: manifest)
    monkeypatch.setattr(workers, "_artifact_size", lambda path: 123)
    monkeypatch.setattr(checkpoint, "file_sha256", lambda path: "a" * 64)
    prefix = {
        "host": [sys.executable, "-m", "pcc"],
        "host-env": ["PCC_EXAMPLE=value", sys.executable, "-m", "pcc"],
        "native": ["/private/native-pcc1"],
        "wrapper": ["wrapper", sys.executable, "-m", "pcc"],
        "different-python": ["/private/python", "-m", "pcc"],
        "host-extra": [sys.executable, "-B", "-m", "pcc"],
    }[owner]
    command = shlex.join([*prefix, "--pcc-python-multi-codegen-worker", str(tmp_path / "manifest")])
    item = workers.resource_tasks_for_commands([command])[0]
    assert item["estimate_bytes"] == 0
    if owner in ("host", "host-env"):
        assert item["startup_prior_model"] == "host-" + kind + "-v1"
        assert policy.task_startup_prior_bytes(item) > 0
    else:
        assert "startup_prior_model" not in item
        assert policy.task_startup_prior_bytes(item) == 0


def test_own_worker_prefixes_name_only_the_running_compiler():
    assert workers.own_worker_prefixes("cpython", "/usr/bin/python3", "x") == [["/usr/bin/python3", "-m", "pcc"]]
    assert workers.own_worker_prefixes("cpython", "", "x") == []
    assert workers.own_worker_prefixes("pcc", "/opt/pcc1", "/opt/pcc1") == [["/opt/pcc1"]]
    assert workers.own_worker_prefixes("pcc", "/opt/pcc1", "pcc1") == [["/opt/pcc1"], ["pcc1"]]
    assert workers.own_worker_prefixes("pypy", "/opt/pypy", "x") == []


@pytest.mark.parametrize("kind", ["export", "summary", "codegen"])
@pytest.mark.parametrize("owner", ["self", "self-argv", "self-env", "other-native", "wrapper", "host"])
def test_native_compiler_workers_share_the_phase_priors(tmp_path, monkeypatch, kind, owner):
    """A native pcc's own workers get the same models; nothing else does."""
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint

    manifest = dict(assigned_indices=[0], job_kind=kind, ast_dir="",
                    exports_path="", src_paths=["one.py"], module_names=["pkg.one"])
    monkeypatch.setattr(workers, "read_worker_manifest", lambda path: manifest)
    monkeypatch.setattr(workers, "_artifact_size", lambda path: 123)
    monkeypatch.setattr(checkpoint, "file_sha256", lambda path: "a" * 64)
    own = workers.own_worker_prefixes("pcc", "/private/native-pcc1", "./pcc1")
    monkeypatch.setattr(workers, "own_worker_prefixes", lambda: own)
    prefix = {
        "self": ["/private/native-pcc1"],
        "self-argv": ["./pcc1"],
        "self-env": ["PCC_EXAMPLE=value", "/private/native-pcc1"],
        "other-native": ["/private/other-pcc1"],
        "wrapper": ["wrapper", "/private/native-pcc1"],
        "host": [sys.executable, "-m", "pcc"],
    }[owner]
    command = shlex.join([*prefix, "--pcc-python-multi-codegen-worker", str(tmp_path / "manifest")])
    item = workers.resource_tasks_for_commands([command])[0]
    assert item["estimate_bytes"] == 0
    if owner in ("self", "self-argv", "self-env"):
        assert item["startup_prior_model"] == "host-" + kind + "-v1"
        assert policy.task_startup_prior_bytes(item) > 0
    else:
        assert "startup_prior_model" not in item
        assert policy.task_startup_prior_bytes(item) == 0


def test_codegen_batches_do_not_share_completed_envelopes_with_singletons(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint

    base = dict(job_kind="codegen", ast_dir="", exports_path="",
                src_paths=["one.py", "two.py", "three.py"],
                module_names=["pkg.one", "pkg.two", "pkg.three"])
    manifests = {str(tmp_path / ("manifest" + str(count))): dict(base, assigned_indices=list(range(count)))
                 for count in (1, 2, 3)}
    monkeypatch.setattr(workers, "read_worker_manifest", lambda path: manifests[path])
    monkeypatch.setattr(workers, "_artifact_size", lambda path: 123)
    monkeypatch.setattr(checkpoint, "file_sha256", lambda path: "a" * 64)
    commands = [shlex.join([sys.executable, "-m", "pcc", "--pcc-python-multi-codegen-worker", path])
                for path in manifests]
    items = workers.resource_tasks_for_commands(commands)
    assert len({item["class"] for item in items}) == 3
    assert items[1]["class"].endswith("|host-codegen-batch:2")
    assert items[2]["class"].endswith("|host-codegen-batch:3")
    for count, item in enumerate(items, 1):
        assert item["inputs"][5] == count
        assert policy.task_startup_prior_bytes(item) == count * policy.task_startup_prior_bytes(items[0])
        assert policy.estimated_task_bytes(item, [
            ("unrelated", [999999999] * 6, 999999999),
        ]) == policy.task_startup_prior_bytes(item)


def test_backend_prior_byte_binding_is_exact_and_detects_stale_task_mutation():
    # Reuse the existing synthetic-wire contract fixture. This proves binding,
    # not codec/emitter execution; real split parity remains an integration gate.
    from tests.python import test_pipeline_indexed_handoff as fixture

    case = fixture.HandoffContractTests()
    case.setUp()
    try:
        case.task["startup_prior_model"] = "host-indexed-backend-v1"
        seal = case.publish()
        fixture.handoff.prepare_handoff_task(case.task, fixture._FRONTEND_TOKEN)
        assert case.task["prior_input_bytes"] == seal["pidx_size"] == case.sidecar.stat().st_size
        assert policy.task_startup_prior_bytes(case.task) == policy.peak_reservation(
            64 * MIB + 7 * seal["pidx_size"])
        case.task["prior_input_bytes"] += 1
        with pytest.raises(fixture.handoff.HandoffError, match="prepared task binding mismatch"):
            fixture.handoff.prepare_handoff_task(case.task, fixture._FRONTEND_TOKEN)
        assert case.sidecar.exists() and case.seal.exists()
    finally:
        case.doCleanups()


@pytest.mark.parametrize("frontend_host,backend_host", [(False, False), (False, True), (True, False), (True, True)])
def test_split_models_classify_frontend_and_backend_prefixes_independently(tmp_path, monkeypatch, frontend_host, backend_host):
    from tests.python import test_host_indexed_process_split as fixture
    from pcc.frontends.python import pipeline_frontend_indexed_stage as stage
    from pcc.frontends.python import worker_process_pool as pool

    prepared = fixture._stage_fixture(tmp_path, monkeypatch, count=1)
    if frontend_host:
        prepared["frontend_tasks"][0]["startup_prior_model"] = "host-codegen-v1"
    captured = []

    def observe(commands, tasks, width, budget, **kwargs):
        captured.extend(tasks)
        for request in prepared["requests"].values():
            with open(request["output_path"], "wb") as stream:
                stream.write(b"synthetic completed object")

    monkeypatch.setattr(pool, "run_resource_worker_processes", observe)
    prefix = [sys.executable, "-m", "pcc"] if backend_host else ["wrapper", sys.executable, "-m", "pcc"]
    stage.run_host_indexed_stage(
        prepared["commands"], prepared["manifests"], prepared["results"],
        prepared["chunks"], prepared["names"], prepared["ir_dir"], prefix, 1,
        "", shlex.quote,
    )
    assert len(captured) == 2
    assert captured[0].get("startup_prior_model") == ("host-indexed-frontend-v1" if frontend_host else None)
    assert captured[1].get("startup_prior_model") == ("host-indexed-backend-v2" if backend_host else None)
    assert prepared["frontend_tasks"][0].get("startup_prior_model") == ("host-codegen-v1" if frontend_host else None)
