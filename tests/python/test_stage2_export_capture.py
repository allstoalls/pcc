"""Retention contracts; real native execution remains the ordinary CI Stage2."""

import pytest

from pcc.frontends.python import pipeline_frontend_parallel as parallel
from pcc.frontends.python import pipeline_frontend_worker_execution as execution
from pcc.frontends.python import pipeline_frontend_workers as workers
from scripts import bootstrap_platform as bootstrap


ENV = "PCC_STAGE2_EXPORT_EVIDENCE_DIR"
TARGET = "pcc.frontends.c.parse.c_parsetab"


def make_manifest(tmp_path, monkeypatch):
    directory = tmp_path / "evidence"
    monkeypatch.setenv(ENV, str(directory))
    names = ["fixture.module_" + str(i) for i in range(460)]
    names[137] = TARGET
    paths = [str(tmp_path / ("source_" + str(i) + ".py")) for i in range(460)]
    manifest_path = tmp_path / "export_137.manifest"
    result_path = tmp_path / "export_137.tsv"
    workers.write_worker_manifest(
        str(manifest_path), str(result_path), str(tmp_path), "",
        str(tmp_path / "ast"), paths, names, [137],
        entry_module="fixture.module_0", sibling_inits=[],
        libpython_mode="off", ir_scaffold_mode="on", verbose=False,
        job_kind="export",
    )
    return directory, manifest_path, result_path, names


def test_manifest_copy_preserves_full_input_and_first_claim(tmp_path, monkeypatch):
    directory, path, result, names = make_manifest(tmp_path, monkeypatch)
    original = path.read_bytes()
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    retained = directory / "selected.manifest"
    assert retained.read_bytes() == original
    manifest = workers.read_worker_manifest(str(retained))
    assert manifest["module_names"] == names
    assert len(manifest["src_paths"]) == 460
    assert manifest["assigned_indices"] == [137]
    assert manifest["result_path"] == str(result)
    path.write_text("not a second manifest", encoding="utf-8")
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    assert retained.read_bytes() == original


@pytest.mark.parametrize("assigned,prefix", [
    ([137], ["python", "-m", "pcc"]),
    ([136], ["pcc1"]),
    ([136, 137], ["pcc1"]),
])
def test_capture_does_not_change_selection_or_batching(tmp_path, monkeypatch, assigned, prefix):
    directory, path, _, names = make_manifest(tmp_path, monkeypatch)
    original = path.read_bytes()
    parallel._retain_stage2_export_manifest(str(path), names, assigned, prefix)
    assert not directory.exists()
    assert path.read_bytes() == original


def test_exact_module_name_is_required(tmp_path, monkeypatch):
    directory, path, _, names = make_manifest(tmp_path, monkeypatch)
    names[137] = TARGET + "_extra"
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    assert not directory.exists()


def test_retention_io_failure_does_not_raise(tmp_path, monkeypatch):
    directory, path, result, names = make_manifest(tmp_path, monkeypatch)
    directory.write_text("not a directory", encoding="utf-8")
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    execution._retain_stage2_export_result(TARGET, str(result))
    assert directory.read_text(encoding="utf-8") == "not a directory"


def test_result_copy_keeps_export_row_bytes_and_first_result(tmp_path, monkeypatch):
    directory, path, result, names = make_manifest(tmp_path, monkeypatch)
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    row = b"EXPORT\texports.json\treexports.json\t17\n"
    result.write_bytes(row)
    execution._retain_stage2_export_result(TARGET, str(result))
    assert (directory / "result.tsv").read_bytes() == row
    result.write_bytes(b"EXPORT\tdifferent\tdifferent\t1\n")
    execution._retain_stage2_export_result(TARGET, str(result))
    assert (directory / "result.tsv").read_bytes() == row
    assert not (directory / "result.tsv.pending").exists()


def test_failed_manifest_publication_cannot_claim_a_result(tmp_path, monkeypatch):
    directory, path, result, names = make_manifest(tmp_path, monkeypatch)

    def interrupted_publish(source, destination):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(parallel.os, "replace", interrupted_publish)
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    assert not (directory / "selected.manifest").exists()
    result.write_text("EXPORT\tx\ty\t17\n", encoding="utf-8")
    execution._retain_stage2_export_result(TARGET, str(result))
    assert not (directory / "result.tsv").exists()


def test_missing_or_unmatched_result_is_not_success(tmp_path, monkeypatch):
    directory, path, result, names = make_manifest(tmp_path, monkeypatch)
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    execution._retain_stage2_export_result(TARGET, str(result))
    assert not (directory / "result.tsv").exists()
    other = tmp_path / "other.tsv"
    other.write_text("EXPORT\tx\ty\t2\n", encoding="utf-8")
    execution._retain_stage2_export_result(TARGET, str(other))
    assert not (directory / "result.tsv").exists()


@pytest.mark.parametrize("stage,gc,timing,enabled", [
    (1, "0", "1", False),
    (2, "0", "1", True),
    (2, "0", "true", True),
    (2, "1", "1", False),
    (3, "0", "1", False),
    (2, "0", "0", False),
])
def test_stage_scope_preserves_environment(tmp_path, stage, gc, timing, enabled):
    original = {ENV: "inherited-must-not-leak", "PCC_PY_FRONTEND_WORKER_TIMING": timing,
                "PCC_HOST_PYTHON": "original-host", "KEEP": "unchanged"}
    selected = bootstrap._stage_compiler_environment(original, tmp_path, stage, gc)
    assert (ENV in selected) is enabled
    if enabled:
        assert selected[ENV] == str(tmp_path / "stage2-export-evidence")
    assert selected["PCC_HOST_PYTHON"] == (
        str(tmp_path / "forbidden-host-python") if stage > 1 else "original-host"
    )
    assert selected["KEEP"] == "unchanged"
    assert original[ENV] == "inherited-must-not-leak"


def test_export_callback_error_keeps_original_failure(tmp_path, monkeypatch):
    directory, path, _, names = make_manifest(tmp_path, monkeypatch)
    parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
    manifest = workers.read_worker_manifest(str(path))
    original = RuntimeError("original export failure")

    def fail(*args, **kwargs):
        assert kwargs["allow_local_int_abi_proofs"] is False
        raise original

    def unused(*args, **kwargs):
        pytest.fail("callback after the original export failure")

    with pytest.raises(RuntimeError) as caught:
        execution._run_export_worker(
            manifest, worker_timing_enabled=lambda: True,
            build_closed_world_context=fail, write_ast_wire=unused,
            closed_world_reexport_edges=unused, closed_world_module_dependencies=unused,
            mark_function_object_exports=unused, write_native_exports_wire=unused,
            write_reexport_edges_wire=unused,
        )
    assert caught.value is original
    assert not (directory / "result.tsv").exists()


def test_stage_environment_keeps_worker_timing_and_clears_inherited_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    monkeypatch.setenv(ENV, "inherited")
    environment = bootstrap._stage_environment("0")
    assert ENV not in environment
    selected = bootstrap._stage_compiler_environment(environment, tmp_path, 2, "0")
    assert selected["PCC_PY_FRONTEND_WORKER_TIMING"] == "1"
    prefix = workers.worker_env_prefix(
        timing_enabled=workers.worker_timing_enabled(selected["PCC_PY_FRONTEND_WORKER_TIMING"])
    )
    assert "PCC_PY_FRONTEND_WORKER_TIMING=1" in prefix


@pytest.mark.parametrize("kind", ["manifest", "result"])
def test_flush_failure_does_not_publish_evidence(tmp_path, monkeypatch, kind):
    directory, path, result, names = make_manifest(tmp_path, monkeypatch)
    original_open = open

    class FlushFailure:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, payload):
            return self.stream.write(payload)

        def flush(self):
            raise OSError("synthetic flush failure")

    def failing_open(name, mode="r", *args, **kwargs):
        stream = original_open(name, mode, *args, **kwargs)
        if str(name).endswith(".pending") and mode == "wb":
            return FlushFailure(stream)
        return stream

    if kind == "manifest":
        monkeypatch.setattr(parallel, "open", failing_open, raising=False)
        parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
        assert not (directory / "selected.manifest").exists()
    else:
        parallel._retain_stage2_export_manifest(str(path), names, [137], ["pcc1"])
        row = b"EXPORT\tx\ty\t17\n"
        result.write_bytes(row)
        monkeypatch.setattr(execution, "open", failing_open, raising=False)
        execution._retain_stage2_export_result(TARGET, str(result))
        assert result.read_bytes() == row
        assert not (directory / "result.tsv").exists()
