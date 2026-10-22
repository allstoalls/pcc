"""Real pcc0 split/unsplit PCO parity; no native-runtime or timing claim.

Run explicitly in the guarded Darwin ARM CI job. The sole call-boundary
observer delegates unchanged to the production pool and retains ephemeral
evidence. It does not replace exports, workers, admission, or compilation.
"""

import hashlib
import json
import os
from pathlib import Path
import platform
import sys

import pytest


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _compiler_sources(root):
    digest = hashlib.sha256()
    paths = sorted(root.rglob("*.py"))
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    return dict(py_files=len(paths), path_and_file_sha256=digest.hexdigest())


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin" and platform.machine() == "arm64")
def test_real_split_and_unsplit_match_complete_objects(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline
    from pcc.frontends.python import pipeline_frontend_workers as workers
    from pcc.frontends.python import worker_process_pool as pool
    from pcc.frontends.python import worker_resource_plan as policy
    from pcc.frontends.python.pipeline_targets import host_target_triple

    assert sys.implementation.name == "cpython"
    assert sys.platform == "darwin" and platform.machine() == "arm64"
    assert not os.environ.get("PCC_TEST_COMPILER"), "host pcc0 is required"
    # Keep the enclosing watchdog and its synchronized admission budget intact.
    budget = int(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "0"))
    state_path = os.environ.get(policy.TREE_STATE_ENV, "")
    assert budget > 0 and state_path and Path(state_path).is_file()
    target = host_target_triple()
    assert target == "arm64-apple-darwin"
    prefix = pipeline._python_frontend_worker_command_prefix()
    assert prefix == [sys.executable, "-m", "pcc"]

    root = tmp_path.resolve() / "indexed-process-split"
    root.mkdir()  # Never reuse or remove another test's output.
    source_dir = root / "source"
    source_dir.mkdir()
    names = ["indexed_split_leaf", "indexed_split_entry"]
    texts = [
        "def bump(value: int) -> int:\n    return value + 1\n",
        "from indexed_split_leaf import bump\n\n"
        "def answer() -> int:\n    return bump(41)\n",
    ]
    sources = [source_dir / (name + ".py") for name in names]
    for path, source in zip(sources, texts):
        path.write_text(source, encoding="utf-8")
    source_hashes = [_sha256(path) for path in sources]
    options = dict(jobs=1, entry_module=names[1], sibling_inits=(names[0],),
                   libpython_mode="off", ir_scaffold_mode="on", verbose=False)
    environment = {
        "PCC_NO_AUTO_PCC1": "1", "PCC_PY_FRONTEND_JOBS": "1",
        "PCC_PYTHON_IR_PASSES": "off", "PCC_BACKEND": "self",
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
        "PCC_DIRECT_INDEXED_NATIVE_OBJECT": "1", "PYTHONHASHSEED": "0",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    for key in (
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_SIDECAR", "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN",
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN", "PCC_STAGE1_CHECKPOINT_DIR",
        "PCC_INDEXED_HANDOFF_REQUEST", "PCC_INDEXED_HANDOFF_SEAL_SHA256",
        "PCC_TARGET_TRIPLE", "PCC_TARGET_ARCH",
    ):
        monkeypatch.delenv(key, raising=False)

    compiler_root = Path(pipeline.__file__).resolve().parents[2]
    compiler_sources = _compiler_sources(compiler_root)
    original_pool = pool.run_resource_worker_processes
    evidence, contexts, seen_pids = {}, {}, set()
    mode = ""
    object_bytes, object_receipts = {}, {}
    receipt = dict(
        schema="pcc.host-indexed-process-split.real.v1", status="RUNNING",
        scope="host pcc0/self/off ARM PCO parity and process retirement; passes off; no runtime execution",
        target=target, compiler_prefix=prefix, host_python_sha256=_sha256(sys.executable),
        compiler_sources=compiler_sources, options=options, environment=environment,
        tree_budget_bytes=budget,
        modules=[dict(index=index, module=name, source_sha256=source_hashes[index])
                 for index, name in enumerate(names)],
        contexts=contexts, objects=object_receipts, pools=evidence,
    )

    def save_receipt():
        (root / "indexed-process-split.json").write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
        )

    save_receipt()

    def observe_pool(commands, tasks, width, tree_budget, **kwargs):
        assert width == 1 and tree_budget == budget
        manifests = {}
        for index, command in enumerate(commands):
            argv, _env = pool._command_spec(command)
            assert argv[:3] == prefix
            if argv[3] == "--pcc-python-multi-codegen-worker":
                manifest = workers.read_worker_manifest(argv[4])
                manifests[index] = (_sha256(argv[4]), manifest)
                if manifest["job_kind"] == "codegen":
                    for ordinal in manifest["assigned_indices"]:
                        assert manifest["src_paths"] == [str(path) for path in sources]
                        assert manifest["module_names"] == names
                        contexts[mode][ordinal] = {
                            "source_sha256": _sha256(sources[ordinal]),
                            "ast_sha256": _sha256(Path(manifest["ast_dir"]) /
                                                   ("module_" + str(ordinal) + ".json")),
                            "exports_sha256": _sha256(manifest["exports_path"]),
                        }
        result = original_pool(commands, tasks, width, tree_budget, **kwargs)
        trace = [line.split("\t") for line in
                 Path(kwargs["trace_path"]).read_text(encoding="utf-8").splitlines()]
        reports = [Path(task["report_path"]).read_text(encoding="utf-8").splitlines()
                   for task in tasks]
        retained = []
        evidence[mode].append(dict(trace=trace, rss_reports=reports, workers=retained))
        save_receipt()  # Preserve real ephemeral outputs even if a later assertion fails.
        assert not any(row[0] in ("failed", "cancel", "unverified-retire") for row in trace)
        for index, task in enumerate(tasks):
            starts = [row for row in trace if row[0] in ("start", "calibrate")
                      and int(row[1]) == index]
            retires = [row for row in trace if row[0] == "retire" and int(row[1]) == index]
            assert len(starts) == len(retires) == 1
            pid = int(starts[0][2])
            assert pid > 0 and pid != os.getpid() and pid not in seen_pids
            seen_pids.add(pid)
            assert int(retires[0][2]) == pid and trace.index(starts[0]) < trace.index(retires[0])
            assert pid not in pool._HOST_WORKERS
            with pytest.raises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)  # The real pool has already reaped it.
            report = reports[index]
            assert len(report) == 6 and report[0] == policy.RESOURCE_REPORT_SCHEMA
            assert int(report[1]) == pid and report[2] == "complete"
            assert int(report[4]) >= int(report[3]) > 0 and len(report[5]) == 64
            row = dict(phase=task["diagnostic_phase"], modules=task["diagnostic_modules"],
                       indices=task["diagnostic_indices"], pid=pid, exit_code=0, reaped=True,
                       resource_token=report[5])
            if "handoff_slot" in task:
                slot = task["handoff_slot"]
                row["handoff_slot"] = slot
                assert task["depends_on"] == (-1 if index == slot else slot)
            if task.get("handoff_request"):
                request_path = Path(task["handoff_request"])
                request = json.loads(request_path.read_text(encoding="utf-8"))
                receipt_path = Path(request["receipt_path"])
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                assert request["target"] == target and request["passes"] == []
                assert request["module"] == names[request["index"]]
                assert request["manifest_sha256"] == manifests[task["depends_on"]][0]
                assert task["input_ready"] is True and len(task["inputs"]) == 47
                assert request["source_identity"] == task["handoff_source_identity"]
                assert receipt["schema"] == "pcc.indexed-handoff.receipt.v1"
                assert receipt["request_sha256"] == _sha256(request_path)
                assert receipt["seal_sha256"] == task["handoff_seal_sha256"]
                assert receipt["pidx_sha256"] == task["source_identity"]
                assert receipt["pidx_size"] > 0 and receipt["backend_token"] == report[5]
                output = Path(request["output_path"])
                assert receipt["output_sha256"] == _sha256(output)
                assert receipt["output_size"] == output.stat().st_size > 0
                assert not Path(request["sidecar_path"]).exists()
                assert not Path(request["seal_path"]).exists()
                assert request_path.is_file() and receipt_path.is_file() and output.is_file()
                row.update(request=request, receipt=receipt,
                           pidx_retired=True, seal_retired=True, receipt_retained=True)
            retained.append(row)
        save_receipt()
        return result  # Preserve the exact production return object.

    monkeypatch.setattr(pool, "run_resource_worker_processes", observe_pool)
    for mode in ("unsplit", "split"):
        evidence[mode], contexts[mode] = [], {}
        artifacts = root / mode
        artifacts.mkdir()
        monkeypatch.setenv("PCC_HOST_INDEXED_PROCESS_SPLIT", "1" if mode == "split" else "0")
        result = pipeline._compile_python_multi_codegen_parallel(
            [str(path) for path in sources], names, artifact_dir=str(artifacts), **options,
        )
        assert result is not None and len(result) == 8
        assert [name for name, _ir in result[0]] == names
        assert result[1] is False and result[4] == [] and result[5] is None
        assert [(name, kind) for name, kind, _path in result[7]] == [(name, "PCO") for name in names]
        assert result[6] == [(name, path) for name, _kind, path in result[7]]
        object_bytes[mode], object_receipts[mode] = [], []
        for index, (name, path) in enumerate(result[6]):
            path = Path(path)
            assert path.parent == artifacts and path.name == "module_" + str(index) + ".direct.pco"
            data = path.read_bytes()
            assert data.startswith(b"PCCNOBJ\x01")
            object_bytes[mode].append(data)
            object_receipts[mode].append(dict(index=index, module=name, size=len(data),
                                               sha256=hashlib.sha256(data).hexdigest()))
        assert [_sha256(path) for path in sources] == source_hashes
        assert os.environ[policy.TREE_STATE_ENV] == state_path
        assert int(os.environ["PCC_WORKER_TREE_BUDGET_BYTES"]) == budget
        assert _compiler_sources(compiler_root) == compiler_sources
        save_receipt()

    assert contexts["unsplit"] == contexts["split"] and set(contexts["split"]) == {0, 1}
    assert object_bytes["unsplit"] == object_bytes["split"]  # Complete ordered bytes, not only hashes.
    for mode in evidence:
        assert any(worker["phase"] == "export" for batch in evidence[mode] for worker in batch["workers"])
    split_batches = [batch for batch in evidence["split"]
                     if any(worker["phase"] == "indexed-backend" for worker in batch["workers"])]
    assert len(split_batches) == 1
    batch = split_batches[0]
    assert [worker["phase"] for worker in batch["workers"]] == ["indexed-frontend", "indexed-backend"] * 2
    assert sorted(worker["indices"][0] for worker in batch["workers"][::2]) == [0, 1]
    # jobs=1 must finish and reap each FE -> BE before the next module starts.
    lifecycle = [(row[0], int(row[1])) for row in batch["trace"]
                 if row[0] in ("start", "calibrate", "retire")]
    assert [("start" if event == "calibrate" else event, index) for event, index in lifecycle] == [
        (event, index) for index in range(4) for event in ("start", "retire")
    ]
    assert [worker["handoff_slot"] for worker in batch["workers"]] == [0, 0, 2, 2]
    assert len(names) > options["jobs"]
    occupied, maximum = set(), 0
    for event, index in lifecycle:
        slot = batch["workers"][index]["handoff_slot"]
        if event in ("start", "calibrate"):
            if index == slot:
                assert slot not in occupied and len(occupied) < options["jobs"]
                occupied.add(slot)
                maximum = max(maximum, len(occupied))
            else:
                assert slot in occupied
        elif index != slot:
            occupied.remove(slot)
    assert not occupied and maximum == options["jobs"]
    # Pool success and the receipt checks above verify every BE retirement;
    # the original trace alone does not report the later cleanup timestamp.
    batch["handoff_slot_reuse"] = dict(modules=len(names), width=options["jobs"],
                                       maximum_inflight=maximum)
    assert not any(worker["phase"].startswith("indexed-")
                   for batch in evidence["unsplit"] for worker in batch["workers"])
    receipt["status"] = "PASS"
    save_receipt()
