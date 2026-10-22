"""Observed automatic caps preserve explicit and external guard contracts."""
import importlib.util
import json
from pathlib import Path

import pytest

from scripts import bootstrap
from scripts import run_pcc_stage1_build as stage1
from scripts import run_process_tree_sample as guard


GIB = 1024**3


def darwin_observation(**changes):
    observation = {
        "platform": "darwin", "reclaimable_bytes": 306 * GIB // 10,
        "disk_free_bytes": 100 * GIB, "swap_total_bytes": 8 * GIB,
        "swap_used_bytes": 6 * GIB, "swap_free_bytes": 2 * GIB,
    }
    observation.update(changes)
    return observation


def test_pressured_darwin_auto_cap_is_admitted_by_unchanged_policy():
    observation = darwin_observation()
    selected = guard.select_tree_memory_budget(0, observation=observation)
    assert selected["max_tree_rss_bytes"] == observation["reclaimable_bytes"] // 2 - 8 * GIB
    assert selected["selection_kind"] == "automatic"
    assert selected["resource_preflight"]["reserve_bytes"] == 8 * GIB
    assert selected["resource_preflight"]["swap_pressure_waived_by_reclaimable"]


@pytest.mark.parametrize("cap", [8 * GIB, 16 * GIB])
def test_explicit_darwin_cap_is_refused_without_reselection(cap):
    with pytest.raises(guard.ProcessTreeSampleError, match="swap is already pressured"):
        guard.select_tree_memory_budget(cap, observation=darwin_observation())


def test_darwin_auto_also_obeys_disk_reserve():
    selected = guard.select_tree_memory_budget(0, observation=darwin_observation(
        reclaimable_bytes=80 * GIB, disk_free_bytes=10 * GIB,
    ))
    assert selected["max_tree_rss_bytes"] == 2 * GIB


def test_darwin_auto_refuses_when_reserve_cannot_fit():
    with pytest.raises(guard.ProcessTreeSampleError, match="positive automatic"):
        guard.select_tree_memory_budget(0, observation=darwin_observation(disk_free_bytes=8 * GIB))


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_use_available_ram_without_darwin_disk_swap_policy(platform):
    selected = guard.select_tree_memory_budget(0, observation={
        "platform": platform, "available_bytes": 6 * GIB,
        "cgroup_limits_verified": True,
    })
    assert selected["max_tree_rss_bytes"] == 3 * GIB
    assert selected["resource_preflight"] is None


def test_external_guard_adoption_is_exact_and_conflicts_fail_before_work():
    selected = guard.select_tree_memory_budget(0, external_budget=6 * GIB)
    assert selected["max_tree_rss_bytes"] == 6 * GIB
    assert selected["selection_kind"] == "external_guard"
    assert guard.select_tree_memory_budget(6 * GIB, external_budget=6 * GIB)["max_tree_rss_bytes"] == 6 * GIB
    for cap in (4 * GIB, 8 * GIB):
        with pytest.raises(guard.ProcessTreeSampleError, match="restart under a matching guard"):
            guard.select_tree_memory_budget(cap, external_budget=6 * GIB)


def make_linux_proc(tmp_path, *, version=2, child_limit="max"):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    mount = tmp_path / "cgroup"
    child = mount / "worker"
    child.mkdir(parents=True)
    (proc / "meminfo").write_text("MemTotal: 268435456 kB\nMemAvailable: 104857600 kB\n")
    if version == 2:
        (proc / "self/cgroup").write_text("0::/worker\n")
        (proc / "self/mountinfo").write_text(f"20 1 0:2 / {mount} rw - cgroup2 cgroup rw\n")
        (mount / "memory.max").write_text(str(8 * GIB))
        (mount / "memory.current").write_text(str(6 * GIB))
        (child / "memory.max").write_text(str(child_limit))
        (child / "memory.current").write_text(str(GIB))
    else:
        (proc / "self/cgroup").write_text("5:memory:/worker\n")
        (proc / "self/mountinfo").write_text(f"20 1 0:2 / {mount} rw - cgroup cgroup rw,memory\n")
        (mount / "memory.limit_in_bytes").write_text(str(8 * GIB))
        (mount / "memory.usage_in_bytes").write_text(str(6 * GIB))
        (child / "memory.limit_in_bytes").write_text(str(16 * GIB))
        (child / "memory.usage_in_bytes").write_text(str(GIB))
    return proc


@pytest.mark.parametrize("version", [1, 2])
def test_linux_auto_accounts_for_constrained_ancestor_not_machine_ram(tmp_path, version):
    observation = guard._linux_memory_observation(make_linux_proc(tmp_path, version=version))
    assert observation["mem_available_bytes"] == 100 * GIB
    assert observation["available_bytes"] == 2 * GIB
    assert observation["cgroup_limits_verified"]
    assert guard.select_tree_memory_budget(0, observation=observation)["max_tree_rss_bytes"] == GIB


def test_linux_unavailable_cgroup_mount_does_not_mean_unlimited(tmp_path):
    proc = make_linux_proc(tmp_path)
    (proc / "self/mountinfo").write_text("")
    observation = guard._linux_memory_observation(proc)
    assert not observation["cgroup_limits_verified"]
    with pytest.raises(guard.ProcessTreeSampleError, match="cgroup memory limits"):
        guard.select_tree_memory_budget(0, observation=observation)


def test_stage1_and_bootstrap_default_share_darwin_admission(monkeypatch):
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    monkeypatch.setattr(guard, "_host_memory_observation", darwin_observation)
    expected = darwin_observation()["reclaimable_bytes"] // 2 - 8 * GIB
    assert stage1._host_memory_budget_bytes(0) == expected
    options = bootstrap.Options({})
    bootstrap._resolve_tree_memory_budget(options)
    assert options.max_tree_rss_bytes == expected
    assert options.memory_budget_selection["selection_kind"] == "automatic"


def test_stage2_default_is_distinct_from_explicit_eight_gib(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("stage2_memory_selection", root / "scripts/run_pcc_stage2_from_receipt.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    monkeypatch.setattr(tool.memory_guard, "_host_memory_observation", darwin_observation)
    args = tool._parser().parse_args(["--stage1-dir", "a", "--output-dir", "b"])
    assert args.max_tree_rss_bytes == 0
    selected = tool._select_stage2_memory_budget(args)
    assert selected["selection_kind"] == "automatic"
    assert selected["max_tree_rss_bytes"] == darwin_observation()["reclaimable_bytes"] // 2 - 8 * GIB


def test_external_bootstrap_requires_a_known_cap():
    options = bootstrap.Options({"PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD": "1"})
    with pytest.raises(bootstrap.BootstrapError, match="explicit or inherited"):
        bootstrap._resolve_tree_memory_budget(options)


def test_bootstrap_revalidates_inherited_cap_and_honors_existing_override():
    environment = {"PCC_WORKER_TREE_BUDGET_BYTES": str(32 * GIB)}
    options = bootstrap.Options(environment)
    with pytest.raises(bootstrap.BootstrapError, match="unsafe bootstrap resource limit"):
        bootstrap._resolve_tree_memory_budget(options)
    environment["PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS"] = "1"
    options = bootstrap.Options(environment)
    bootstrap._resolve_tree_memory_budget(options)
    assert options.max_tree_rss_bytes == 32 * GIB
    assert options.memory_budget_selection["external_guard_bytes"] == 32 * GIB


@pytest.mark.parametrize("raw", ["0", "-1", "wrong", "8.5"])
def test_shared_reserve_parser_rejects_invalid_or_nonpositive_values(raw):
    environment = {"PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES": raw}
    with pytest.raises(guard.ProcessTreeSampleError, match="invalid bootstrap resource limit"):
        guard.configured_host_memory_reserve_bytes(environment)
    with pytest.raises(bootstrap.BootstrapError, match="invalid bootstrap resource limit"):
        bootstrap.Options(environment)


def test_shared_reserve_default_and_unsafe_upper_gate():
    assert guard.configured_host_memory_reserve_bytes({}) == 8 * GIB
    environment = {"PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES": str(16 * GIB)}
    with pytest.raises(guard.ProcessTreeSampleError, match="unsafe bootstrap resource limit"):
        guard.configured_host_memory_reserve_bytes(environment)
    environment["PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS"] = "1"
    assert guard.configured_host_memory_reserve_bytes(environment) == 16 * GIB


@pytest.mark.parametrize("reserve,available", [(8 * GIB, 306 * GIB // 10), (GIB // 2, 10 * GIB)])
def test_configured_reserve_parity_across_bootstrap_stage1_stage2(monkeypatch, reserve, available):
    key = "PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES"
    monkeypatch.setenv(key, str(reserve))
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    observation = darwin_observation(reclaimable_bytes=available)
    monkeypatch.setattr(guard, "_host_memory_observation", lambda: observation)
    monkeypatch.setattr(guard, "_darwin_resource_observation", lambda: observation)
    expected = available // 2 - reserve
    selection = stage1._host_memory_budget_selection(0)
    assert selection["max_tree_rss_bytes"] == expected
    assert selection["configured_host_memory_reserve_bytes"] == reserve
    options = bootstrap.Options({key: str(reserve)})
    bootstrap._resolve_tree_memory_budget(options)
    assert options.max_tree_rss_bytes == expected
    assert options.memory_budget_selection["resource_preflight"]["reserve_bytes"] == reserve

    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("stage2_reserve_parity", root / "scripts/run_pcc_stage2_from_receipt.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool.memory_guard, "_host_memory_observation", lambda: observation)
    monkeypatch.setattr(tool.memory_guard, "_darwin_resource_observation", lambda: observation)
    args = tool._parser().parse_args(["--stage1-dir", "a", "--output-dir", "b"])
    selected = tool._select_stage2_memory_budget(args)
    assert selected["max_tree_rss_bytes"] == expected
    assert selected["configured_host_memory_reserve_bytes"] == reserve
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    assert tool._stage2_resource_preflight(expected)["reserve_bytes"] == reserve
    values = {key: value for key, value in observation.items() if key != "platform"}
    assert tool._validate_resource_observation(max_tree_rss_bytes=expected, **values)["reserve_bytes"] == reserve


def test_options_and_help_do_not_probe_resources(monkeypatch):
    def unexpected_observation():
        raise AssertionError("availability read before admission")
    monkeypatch.setattr(guard, "_host_memory_observation", unexpected_observation)
    options = bootstrap.Options({})
    assert options.host_memory_reserve_bytes == 8 * GIB
    with pytest.raises(SystemExit) as caught:
        bootstrap.parse_args(["--help"], options)
    assert caught.value.code == 0
    with pytest.raises(SystemExit) as caught:
        stage1.main(["--help"])
    assert caught.value.code == 0


def mac_ci_observation(**changes):
    # Synthetic headroom on a 7 GB runner, not a measurement from failed CI.
    return darwin_observation(
        **{"reclaimable_bytes": 3 * GIB, "swap_total_bytes": 0,
           "swap_used_bytes": 0, "swap_free_bytes": 0, **changes}
    )


def mac_ci_options(tmp_path):
    return bootstrap.validate_settings(bootstrap.Options({
        "PCC_BOOTSTRAP_OUT_DIR": str(tmp_path),
        "PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES": str(4 * GIB),
        "PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES": str(2 * GIB),
        "PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES": str(GIB // 2),
    }))


@pytest.mark.parametrize("available,expected", [(3 * GIB, 5 * GIB // 2), (7 * GIB, 4 * GIB)])
def test_mac_ci_auto_budget_uses_observed_headroom_not_runner_total(available, expected):
    observation = mac_ci_observation(reclaimable_bytes=available)
    selected = guard.select_tree_memory_budget(
        0, default_ceiling=4 * GIB, minimum_bytes=2 * GIB,
        reserve_bytes=GIB // 2, observation=observation,
    )
    assert selected["max_tree_rss_bytes"] == expected
    assert selected["observation"] == observation
    assert selected["resource_preflight"]["reserve_bytes"] == GIB // 2
    assert selected["resource_preflight"]["required_reclaimable_and_disk_free_bytes"] == expected + GIB // 2


@pytest.mark.parametrize("changes", [
    {"reclaimable_bytes": 2 * GIB},
    {"disk_free_bytes": 2 * GIB},
    {"swap_total_bytes": 2 * GIB, "swap_used_bytes": 3 * GIB // 2,
     "swap_free_bytes": GIB // 2},
])
def test_mac_ci_below_minimum_fails_closed_without_rounding_up(changes):
    observation = mac_ci_observation(**changes)
    with pytest.raises(guard.ProcessTreeSampleError, match="minimum tree budget") as caught:
        guard.select_tree_memory_budget(
            0, default_ceiling=4 * GIB, minimum_bytes=2 * GIB,
            reserve_bytes=GIB // 2, observation=observation,
        )
    selected = caught.value.memory_budget_selection
    assert selected["max_tree_rss_bytes"] < 2 * GIB
    assert selected["observation"] == observation
    assert selected["configured_host_memory_reserve_bytes"] == GIB // 2


def test_mac_ci_explicit_four_gib_keeps_the_original_rejection(monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard, "_host_memory_observation", mac_ci_observation)
    with pytest.raises(guard.ProcessTreeSampleError, match="insufficient reclaimable") as caught:
        guard.select_tree_memory_budget(4 * GIB, reserve_bytes=GIB // 2)
    selected = caught.value.memory_budget_selection
    assert selected["max_tree_rss_bytes"] == 4 * GIB
    assert selected["resource_preflight"]["reclaimable_bytes"] == 3 * GIB


def test_mac_ci_bootstrap_persists_admission_and_propagates_exact_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_host_memory_observation", mac_ci_observation)
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    options = mac_ci_options(tmp_path)
    entered = []

    def stage(stage, out_exe, cmd, options):
        entered.append(stage)
        expected = 5 * GIB // 2
        assert options.max_tree_rss_bytes == expected
        environment = bootstrap.stage_environment(stage, options)
        assert environment["PCC_WORKER_TREE_BUDGET_BYTES"] == str(expected)
        command = bootstrap._posix_guard_command(tmp_path / "guard", options, ["compiler"])
        assert command[command.index("--auto-tree-rss-ceiling-bytes") + 1] == str(expected)
        assert command[command.index("--min-tree-rss-bytes") + 1] == str(2 * GIB)
        assert command[command.index("--darwin-preflight-reserve-bytes") + 1] == str(GIB // 2)

    monkeypatch.setattr(bootstrap, "_run_stage", stage)
    bootstrap.run_stage(1, tmp_path / "pcc1", ["compiler"], options)
    assert entered == [1]
    receipt = json.loads((tmp_path / "stage1.memory-budget.json").read_text())
    assert receipt["status"] == "ADMITTED"
    assert receipt["memory_budget_selection"] == options.memory_budget_selection
    assert receipt["memory_budget_selection"]["observation"] == mac_ci_observation()


def test_mac_ci_bootstrap_rejection_records_observation_and_never_starts_stage(tmp_path, monkeypatch):
    observation = mac_ci_observation(reclaimable_bytes=2 * GIB)
    monkeypatch.setattr(guard, "_host_memory_observation", lambda: observation)
    options = mac_ci_options(tmp_path)

    def forbidden_stage(*args):
        pytest.fail("compiler stage launched after failed memory admission")

    monkeypatch.setattr(bootstrap, "_run_stage", forbidden_stage)
    with pytest.raises(bootstrap.BootstrapError, match="minimum tree budget"):
        bootstrap.run_stage(1, tmp_path / "pcc1", ["compiler"], options)
    receipt = json.loads((tmp_path / "stage1.memory-budget.json").read_text())
    assert receipt["status"] == "PREFLIGHT_REJECTED"
    assert receipt["memory_budget_selection"]["observation"] == observation
    assert receipt["memory_budget_selection"]["minimum_tree_rss_bytes"] == 2 * GIB


@pytest.mark.parametrize("final_available,expected_code", [(29 * GIB // 10, 0), (2 * GIB, 2)])
def test_mac_ci_final_guard_reselects_after_startup_and_records_final_observation(
    tmp_path, monkeypatch, final_available, expected_code,
):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    observations = iter([mac_ci_observation(), mac_ci_observation(reclaimable_bytes=final_available)])
    monkeypatch.setattr(guard, "_host_memory_observation", lambda: next(observations))
    options = mac_ci_options(tmp_path)
    bootstrap._resolve_tree_memory_budget(options)
    initial_cap = options.max_tree_rss_bytes
    assert initial_cap == 5 * GIB // 2
    launched = []

    class Child:
        pid = 123
        returncode = 0

        def poll(self):
            return self.returncode

    def launch(command, **kwargs):
        launched.append(kwargs["env"])
        return Child()

    def run_guard(command, **kwargs):
        assert command[command.index("--auto-tree-rss-ceiling-bytes") + 1] == str(initial_cap)
        return guard.subprocess.CompletedProcess(command, guard.main(command[2:]))

    monkeypatch.setattr(guard.subprocess, "Popen", launch)
    monkeypatch.setattr(bootstrap.subprocess, "run", run_guard)
    monkeypatch.setattr(guard, "_process_identity", lambda pid: guard._ProcessIdentity(pid, 1, (1, 0)))
    tables = iter([({123: (1, 1024, "synthetic-worker")}, 0), ({}, 0)])
    monkeypatch.setattr(guard, "_process_table", lambda **kwargs: next(tables))
    code, directory = bootstrap._run_guarded(
        ["synthetic-worker"], options, 1, bootstrap.stage_environment(1, options),
    )
    assert code == expected_code
    receipt = json.loads((tmp_path / "stage1.memory-budget.json").read_text())
    final = receipt["memory_budget_selection"]
    assert final["observation"]["reclaimable_bytes"] == final_available
    assert final["default_ceiling_bytes"] == initial_cap
    assert receipt["admission_owner"] == "process_tree_guard"
    if expected_code:
        assert receipt["status"] == "PREFLIGHT_REJECTED"
        assert launched == []
    else:
        expected_cap = final_available - GIB // 2
        assert receipt["status"] == "ADMITTED"
        assert options.max_tree_rss_bytes == expected_cap < initial_cap
        assert len(launched) == 1
        assert launched[0]["PCC_WORKER_TREE_BUDGET_BYTES"] == str(expected_cap)
        state = Path(launched[0]["PCC_WORKER_TREE_STATE_PATH"]).read_text().splitlines()
        assert int(state[2]) == expected_cap
        assert "PCC_WORKER_TREE_STATE_PATH" not in bootstrap.stage_environment(1, options)


@pytest.mark.parametrize("failure", [OSError("vm_stat unavailable"), guard.subprocess.TimeoutExpired("vm_stat", 5)])
def test_mac_ci_unavailable_observation_records_rejection_before_stage(tmp_path, monkeypatch, failure):
    def unavailable():
        raise failure

    def forbidden_stage(*args):
        pytest.fail("stage launched without a resource observation")

    monkeypatch.setattr(guard, "_host_memory_observation", unavailable)
    monkeypatch.setattr(bootstrap, "_run_stage", forbidden_stage)
    options = mac_ci_options(tmp_path)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.run_stage(1, tmp_path / "pcc1", ["compiler"], options)
    receipt = json.loads((tmp_path / "stage1.memory-budget.json").read_text())
    assert receipt["status"] == "PREFLIGHT_REJECTED"
    assert str(failure) in receipt["error"]
    assert receipt["memory_budget_selection"] == {}


def mock_final_guard_output(monkeypatch, raw_receipt, returncode):
    directories = []

    def run_guard(command, **kwargs):
        result = Path(command[command.index("--result") + 1])
        directories.append(result.parent)
        if raw_receipt is not None:
            result.write_text(raw_receipt)
        (result.parent / "target.stdout").write_text("compiler stdout before guard exit\n")
        (result.parent / "target.stderr").write_text("compiler stderr before guard exit\n")
        return bootstrap.subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(bootstrap.subprocess, "run", run_guard)
    return directories


# POSIX guard receipt replay
@pytest.mark.pcc_gate(probe=lambda: not (bootstrap.os.name == "nt"))
@pytest.mark.parametrize("raw_receipt,diagnostic", [
    pytest.param(None, "missing", id="no-receipt"),
    pytest.param(json.dumps({"status": "RUNNING", "max_tree_rss_bytes": 0}),
                 "incomplete", id="interrupted-before-selection"),
    pytest.param(json.dumps({"status": "RUNNING", "memory_budget_selection": {
        "max_tree_rss_bytes": 2 * GIB}}), "incomplete", id="interrupted-after-selection"),
    pytest.param("{}", "incomplete", id="no-status"),
    pytest.param(json.dumps({"status": "COMPLETE"}), "incomplete", id="no-selection"),
    pytest.param(json.dumps({"status": "COMPLETE", "memory_budget_selection": {
        "selection_kind": "automatic"}}), "incomplete", id="no-selected-cap"),
    pytest.param("{", "invalid", id="malformed-json"),
    pytest.param("[]", "invalid", id="non-object-receipt"),
    pytest.param(json.dumps({"status": "COMPLETE", "memory_budget_selection": []}),
                 "invalid", id="non-object-selection"),
    pytest.param(json.dumps({"status": "COMPLETE", "memory_budget_selection": {
        "max_tree_rss_bytes": GIB}}), "invalid", id="below-minimum"),
    pytest.param(json.dumps({"status": "COMPLETE", "memory_budget_selection": {
        "max_tree_rss_bytes": 4 * GIB}}), "invalid", id="above-admitted-ceiling"),
    pytest.param(json.dumps({"status": "COMPLETE", "memory_budget_selection": {
        "max_tree_rss_bytes": float(2 * GIB)}}), "invalid", id="non-integer-cap"),
])
def test_mac_ci_guard_receipt_failure_preserves_compiler_output(
    tmp_path, monkeypatch, capsys, raw_receipt, diagnostic,
):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    options = mac_ci_options(tmp_path)
    options.max_tree_rss_bytes = 3 * GIB
    initial_selection = {"selection_kind": "automatic", "max_tree_rss_bytes": 3 * GIB}
    options.memory_budget_selection = initial_selection.copy()
    directories = mock_final_guard_output(monkeypatch, raw_receipt, -9)

    with pytest.raises(bootstrap.BootstrapError, match=diagnostic + " final guard memory receipt") as caught:
        bootstrap._run_guarded(["synthetic-worker"], options, 1, {})

    captured = capsys.readouterr()
    assert captured.out.count("compiler stdout before guard exit\n") == 1
    assert captured.err == "compiler stderr before guard exit\n"
    assert "compiler stderr" not in captured.out
    assert "guard returncode=-9" in str(caught.value)
    assert str(directories[0] / "result.json") in str(caught.value)
    assert options.max_tree_rss_bytes == 3 * GIB
    assert options.memory_budget_selection == initial_selection
    assert not (tmp_path / "stage1.memory-budget.json").exists()
    if raw_receipt is not None:
        assert (directories[0] / "result.json").read_text() == raw_receipt


# POSIX guard receipt replay
@pytest.mark.pcc_gate(probe=lambda: not (bootstrap.os.name == "nt"))
@pytest.mark.parametrize("returncode", [0, 7])
def test_mac_ci_terminal_guard_receipt_preserves_output_and_returncode(
    tmp_path, monkeypatch, capsys, returncode,
):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    options = mac_ci_options(tmp_path)
    options.max_tree_rss_bytes = 3 * GIB
    options.memory_budget_selection = {"selection_kind": "automatic"}
    selection = {"selection_kind": "automatic", "max_tree_rss_bytes": 5 * GIB // 2}
    directories = mock_final_guard_output(monkeypatch, json.dumps({
        "status": "COMPLETE", "returncode": returncode,
        "memory_budget_selection": selection,
    }), returncode)

    actual_code, directory = bootstrap._run_guarded(["synthetic-worker"], options, 1, {})

    assert actual_code == returncode
    assert directory == directories[0]
    captured = capsys.readouterr()
    assert captured.out.count("compiler stdout before guard exit\n") == 1
    assert captured.err == "compiler stderr before guard exit\n"
    assert options.max_tree_rss_bytes == selection["max_tree_rss_bytes"]
    receipt = json.loads((tmp_path / "stage1.memory-budget.json").read_text())
    assert receipt["status"] == "ADMITTED"
    assert receipt["guard_status"] == "COMPLETE"
    assert receipt["memory_budget_selection"] == selection


@pytest.mark.parametrize("kind,checkpoint", [("explicit", None), ("external_guard", None), ("automatic", "checkpoint")])
def test_mac_ci_guard_preserves_exact_explicit_inherited_and_checkpoint_caps(tmp_path, monkeypatch, kind, checkpoint):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    options = mac_ci_options(tmp_path)
    options.max_tree_rss_bytes = 3 * GIB
    options.memory_budget_selection = {"selection_kind": kind}
    options.stage1_checkpoint = checkpoint
    command = bootstrap._posix_guard_command(tmp_path / "guard", options, ["compiler"])
    assert "--auto-tree-rss-ceiling-bytes" not in command
    assert command[command.index("--max-tree-rss-bytes") + 1] == str(3 * GIB)
