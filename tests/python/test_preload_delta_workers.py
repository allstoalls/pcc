"""Per-root preload deltas computed in worker processes build the serial index."""

import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap

import pytest

from pcc.frontends.python import (
    pipeline,
    pipeline_frontend_workers as workers,
    preload_delta_worker,
    type_infer,
    worker_process_pool as pool,
    worker_resource_plan as resources,
)
from pcc.frontends.python.pipeline_exports import (
    _native_export_to_wire,
    _write_native_exports_wire,
)


def _cls(name, owner, fields):
    return {
        "kind": "class", "class_name": name, "owning_module": owner,
        "field_names": tuple(field for field, _ in fields),
        "field_types": tuple(fields), "methods": (), "base_names": (),
    }


def _exports(module_count=20):
    # Removing either Shared owner makes the other one unique, so those two
    # roots add descriptors the global preload lacks; every m* root drops
    # its own unique class.
    exports = {
        "dup_a": {"Shared": _cls("Shared", "dup_a", (("x", ("int", 64, True)),))},
        "dup_b": {"Shared": _cls("Shared", "dup_b", (("y", ("str",)),))},
    }
    for index in range(module_count):
        name = "m%02d" % index
        exports[name] = {
            "C%d" % index: _cls("C%d" % index, name, (("v", ("int", 64, True)),)),
        }
    exports["plain"] = {}
    return exports


@pytest.fixture
def host_workers(monkeypatch):
    monkeypatch.setattr(
        pipeline, "_python_frontend_worker_command_prefix",
        lambda: [sys.executable, "-m", "pcc"],
    )
    calls = []
    original = pipeline._run_python_frontend_worker_commands

    def observe(commands, **kwargs):
        calls.append((list(commands), kwargs))
        return original(commands, **kwargs)

    monkeypatch.setattr(pipeline, "_run_python_frontend_worker_commands", observe)
    return calls


def test_worker_deltas_build_the_serial_index_byte_for_byte(
    monkeypatch, host_workers, tmp_path,
):
    exports = _exports()
    serial = type_infer.build_unique_external_class_preload_index(exports)
    # New descriptors get ids in root order: dup_a's before dup_b's.
    assert len(serial["types"]) == 22
    assert serial["roots"]["dup_a"] == ((), (("Shared", 20), ("dup_b.Shared", 20)))
    assert serial["roots"]["dup_b"] == ((), (("Shared", 21), ("dup_a.Shared", 21)))
    assert serial["roots"]["m07"] == (("C7", "m07.C7"), ())
    assert serial["roots"]["plain"] == ((), ())

    monkeypatch.setenv("PCC_PRELOAD_DELTA_JOBS", "3")
    parallel = pipeline._build_unique_external_class_preload_index(
        exports, str(tmp_path),
    )
    assert len(host_workers) == 1
    commands, kwargs = host_workers[0]
    assert len(commands) == 3 and kwargs == {"max_parallel": 3}
    assert all(preload_delta_worker.WORKER_ARG in command for command in commands)
    assert all(str(tmp_path / "preload_deltas") in command for command in commands)
    assert parallel == serial
    assert json.dumps(parallel) == json.dumps(serial)
    assert list(tmp_path.iterdir()) == []


def test_worker_result_missing_a_root_fails_closed(monkeypatch, host_workers, tmp_path):
    original = preload_delta_worker.read_result

    def drop_dup_b(out_path):
        deltas = original(out_path)
        deltas.pop("dup_b", None)
        return deltas

    monkeypatch.setenv("PCC_PRELOAD_DELTA_JOBS", "2")
    monkeypatch.setattr(preload_delta_worker, "read_result", drop_dup_b)
    with pytest.raises(pipeline.PyPipelineError, match="omitted root dup_b"):
        pipeline._build_unique_external_class_preload_index(_exports(), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("jobs,module_count,work_dir", [
    ("1", 20, True), ("4", 8, True), ("4", 20, False),
])
def test_one_job_few_roots_or_no_work_dir_stay_in_the_coordinator(
    monkeypatch, host_workers, tmp_path, jobs, module_count, work_dir,
):
    exports = _exports(module_count)
    monkeypatch.setenv("PCC_PRELOAD_DELTA_JOBS", jobs)
    result = pipeline._build_unique_external_class_preload_index(
        exports, str(tmp_path) if work_dir else "",
    )
    assert host_workers == []
    assert result == type_infer.build_unique_external_class_preload_index(exports)


def test_worker_cli_round_trip_and_unknown_root(tmp_path):
    exports = _exports()
    exports_path = tmp_path / "exports.json"
    _write_native_exports_wire(str(exports_path), exports, {})
    roots_path = tmp_path / "roots.txt"
    roots_path.write_text("dup_b\nm03\n", encoding="utf-8")
    out_path = tmp_path / "deltas.json"
    command = [sys.executable, "-m", "pcc", preload_delta_worker.WORKER_ARG]
    completed = subprocess.run(
        command + [str(exports_path), str(roots_path), str(out_path)],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    global_by_key = type_infer.preload_global_by_key(
        type_infer.build_unique_external_class_preload(exports)
    )
    assert preload_delta_worker.read_result(str(out_path)) == {
        root: type_infer.preload_root_delta(exports, root, global_by_key)
        for root in ("dup_b", "m03")
    }
    assert not (tmp_path / "deltas.json.partial").exists()

    roots_path.write_text("not_exported\n", encoding="utf-8")
    failed = subprocess.run(
        command + [str(exports_path), str(roots_path), str(tmp_path / "bad.json")],
        capture_output=True, text=True,
    )
    assert failed.returncode != 0
    assert not (tmp_path / "bad.json").exists()

    usage = subprocess.run(command + [str(exports_path)], capture_output=True, text=True)
    assert usage.returncode == 2
    assert "requires exports, roots and output paths" in usage.stderr


def test_worker_path_avoids_modules_pcc1_compiles_as_cpython():
    # pcc1 links tempfile and shutil as CPython modules; a function calling
    # them compiles to a no-libpython stub that raises in the native Stage2
    # coordinator, and only a full self-host run reaches it.
    for function in (
        pipeline._build_unique_external_class_preload_index,
        pipeline._preload_deltas_in_workers,
        preload_delta_worker.run,
        preload_delta_worker.read_result,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert not names & {"tempfile", "shutil"}, function.__name__


def _preload_command(tmp_path, roots=("dup_b", "m03")):
    exports_path = tmp_path / "exports.json"
    roots_path = tmp_path / "roots.txt"
    out_path = tmp_path / "deltas.json"
    _write_native_exports_wire(str(exports_path), _exports(), {})
    roots_path.write_text("\n".join(roots) + "\n", encoding="utf-8")
    command = shlex.join([
        sys.executable, "-B", "-m", "pcc", preload_delta_worker.WORKER_ARG,
        str(exports_path), str(roots_path), str(out_path),
    ])
    return command, exports_path, roots_path, out_path


def test_preload_resource_tasks_bind_complete_graph_roots_and_owner(tmp_path):
    command, exports_path, roots_path, out_path = _preload_command(tmp_path)
    task = workers.resource_tasks_for_commands([command])[0]
    identities = [hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (exports_path, roots_path)]
    assert task["inputs"] == [exports_path.stat().st_size, roots_path.stat().st_size, 2]
    assert task["estimate_bytes"] == 0
    assert task["report_path"] == str(out_path) + ".rss"
    assert task["restartable"] is True
    assert task["source_identity"] == "|".join(identities)
    assert "|preload-delta|full-graph|" in task["class"]
    assert sys.executable in task["class"] and identities[0] in task["class"]

    roots_path.write_text("dup_b\nm04\n", encoding="utf-8")
    changed = workers.resource_tasks_for_commands([command])[0]
    assert changed["inputs"] == task["inputs"]
    assert changed["source_identity"] != task["source_identity"]
    assert changed["class"] == task["class"]

    _write_native_exports_wire(str(exports_path), _exports(21), {})
    changed_graph = workers.resource_tasks_for_commands([command])[0]
    assert changed_graph["class"] != task["class"]


def test_known_mixed_worker_tasks_and_unknown_commands_are_distinct(tmp_path):
    command, exports_path, _roots_path, _out_path = _preload_command(tmp_path)
    source = tmp_path / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manifest = tmp_path / "manifest.txt"
    workers.write_worker_manifest(
        str(manifest), str(tmp_path / "result.json"), str(tmp_path),
        str(exports_path), "", [str(source)], ["module"], [0],
        entry_module="module", sibling_inits=[], libpython_mode="off",
        ir_scaffold_mode="on", verbose=False, job_kind="export",
    )
    codegen = shlex.join([
        sys.executable, "-B", "-m", "pcc",
        "--pcc-python-multi-codegen-worker", str(manifest),
    ])
    tasks = workers.resource_tasks_for_commands([command, codegen])
    assert len(tasks) == 2 and tasks[0]["class"] != tasks[1]["class"]
    assert tasks[0]["report_path"].endswith("deltas.json.rss")
    assert tasks[1]["report_path"] == str(manifest) + ".rss"
    unknown = "custom-worker --custom-worker-mode work"
    assert workers.resource_tasks_for_commands([unknown]) is None
    assert workers.resource_tasks_for_commands([command, unknown]) is None
    assert workers.resource_tasks_for_commands([unknown, command]) is None


@pytest.mark.parametrize("tail", [
    [], ["exports", "roots"], ["exports", "roots", "out", "extra"],
    ["exports", "roots", "out", "--pcc-preload-delta-worker"],
    ["--pcc-python-multi-codegen-worker", "manifest"],
])
def test_invalid_preload_command_never_falls_through_to_old_pool(monkeypatch, tail):
    monkeypatch.setenv(workers.WORKER_TREE_BUDGET_ENV, str(1024**3))
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid known command reached the old worker pool")
    monkeypatch.setattr(pool, "run_worker_processes", forbidden)
    command = shlex.join([sys.executable, preload_delta_worker.WORKER_ARG] + tail)
    with pytest.raises(workers.FrontendWorkerContractError):
        workers.run_worker_commands([command], max_parallel=2)


def test_preload_resource_task_missing_input_is_not_unknown(tmp_path):
    command, _exports_path, roots_path, _out_path = _preload_command(tmp_path)
    roots_path.unlink()
    with pytest.raises(FileNotFoundError):
        workers.resource_tasks_for_commands([command])


def test_preload_worker_publishes_graph_root_and_final_rss(tmp_path, monkeypatch):
    _command, exports_path, roots_path, out_path = _preload_command(tmp_path)
    report = tmp_path / "worker.rss"
    monkeypatch.setenv(resources.RESOURCE_REPORT_ENV, str(report))
    monkeypatch.setenv(resources.RESOURCE_TOKEN_ENV, "preload-attempt")
    phases = []
    original = resources.publish_worker_resource
    def observe(phase):
        original(phase)
        phases.append(resources.read_worker_resource(str(report), os.getpid(), "preload-attempt"))
    monkeypatch.setattr(resources, "publish_worker_resource", observe)
    assert preload_delta_worker.run(str(exports_path), str(roots_path), str(out_path)) == 0
    assert [row[0] for row in phases] == ["exports", "preload", "root:dup_b", "root:m03", "complete"]
    assert all(0 < row[1] <= row[2] for row in phases)
    assert phases[-1][2] >= phases[0][2]
    assert not Path(str(report) + ".tmp").exists()
    assert not Path(str(out_path) + ".partial").exists()


def test_budgeted_preload_cli_has_stable_shards_serial_bytes_and_measured_reports(
    monkeypatch, host_workers, tmp_path,
):
    exports = _exports(38)
    serial = type_infer.build_unique_external_class_preload_index(exports)
    global_by_key = type_infer.preload_global_by_key(
        type_infer.build_unique_external_class_preload(exports)
    )
    monkeypatch.setenv(workers.WORKER_TREE_BUDGET_ENV, str(1024**3))
    # The enclosing test guard still enforces its tree cap; this explicit
    # one-GiB scheduler budget measures the test owner and its CLI children.
    monkeypatch.delenv(resources.TREE_STATE_ENV, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("budgeted preload reached the old fixed-width pool")
    monkeypatch.setattr(pool, "run_worker_processes", forbidden)
    original = pool.run_resource_worker_processes
    records = []
    def observe(commands, tasks, width, budget, **kwargs):
        shards = []
        for command in commands:
            argv = shlex.split(command)
            position = argv.index(preload_delta_worker.WORKER_ARG)
            shards.append(Path(argv[position + 2]).read_text().splitlines())
        original(commands, tasks, width, budget, **kwargs)
        trace = Path(kwargs["trace_path"]).read_text().splitlines()
        reports = [Path(task["report_path"]).read_text().splitlines() for task in tasks]
        for command, roots in zip(commands, shards):
            out_path = Path(shlex.split(command)[-1])
            expected_rows = tuple((root, *type_infer.preload_root_delta(exports, root, global_by_key))
                                  for root in roots)
            assert out_path.read_bytes() == json.dumps(_native_export_to_wire(expected_rows)).encode()
        records.append((shards, trace, reports, width))
    monkeypatch.setattr(pool, "run_resource_worker_processes", observe)

    for jobs in (2, 6):
        work = tmp_path / str(jobs)
        work.mkdir()
        monkeypatch.setenv("PCC_PRELOAD_DELTA_JOBS", str(jobs))
        monkeypatch.setattr(pool, "_RESOURCE_OBSERVATIONS", [])
        result = pipeline._build_unique_external_class_preload_index(exports, str(work))
        assert json.dumps(result).encode() == json.dumps(serial).encode()
        assert list(result["roots"]) == list(serial["roots"])
        assert list(work.iterdir()) == []

    assert len(host_workers) == len(records) == 2
    assert records[0][0] == records[1][0]
    assert [len(chunk) for chunk in records[0][0]] == [8] * 5
    for shards, trace, reports, width in records:
        events = [row.split("\t") for row in trace]
        assert sum(row[0] == "calibrate" for row in events) == 1
        assert sum(row[0] == "start" for row in events) == 4
        assert sum(row[0] == "retire" for row in events) == len(shards)
        retired_pids = {int(row[2]) for row in events if row[0] == "retire"}
        assert {int(row[1]) for row in reports} == retired_pids
        assert all(row[0] == resources.RESOURCE_REPORT_SCHEMA and row[2] == "complete"
                   and 0 < int(row[3]) <= int(row[4]) and len(row[5]) == 64
                   for row in reports)
        assert len({row[5] for row in reports}) == len(reports)
        active = set()
        for row in events:
            if row[0] in ("start", "calibrate"):
                active.add(int(row[2]))
                assert len(active) <= width
            elif row[0] == "retire":
                active.remove(int(row[2]))
        assert not active
    # Keep the observed reports outside the production state directory so
    # the process test can prove both measurements and complete cleanup.
    (tmp_path / "observed-runs.json").write_text(json.dumps(records, indent=2) + "\n")


def test_budgeted_preload_failure_cleans_reports_and_state(monkeypatch, tmp_path):
    monkeypatch.setenv(workers.WORKER_TREE_BUDGET_ENV, str(1024**3))
    monkeypatch.delenv(resources.TREE_STATE_ENV, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("failed budgeted preload reached the old worker pool")
    monkeypatch.setattr(pool, "run_worker_processes", forbidden)
    with pytest.raises(subprocess.CalledProcessError):
        pipeline._preload_deltas_in_workers(
            _exports(), ["not_exported"] * 16,
            [sys.executable, "-B", "-m", "pcc"], 2, str(tmp_path),
        )
    assert list(tmp_path.iterdir()) == []
    assert pool._HOST_WORKERS == {}
