"""Host-only checkpoint scheduling and worker-wire contracts.

These checks do not compile or execute native code. The cold/resumed executable
differential remains a separate integration gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline_frontend_parallel as parallel
from pcc.frontends.python import pipeline_frontend_worker_execution as execution
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint
from pcc.frontends.python import pipeline
from pcc.ir.compat import ir


def _write_manifest(tmp_path, assigned=(0, 1, 2), skipped=(0, 2), **overrides):
    options = {
        "entry_module": "pkg.a",
        "sibling_inits": ("pkg.b", "pkg.c"),
        "libpython_mode": "off",
        "ir_scaffold_mode": "on",
        "verbose": False,
        "checkpoint_root": str(tmp_path / "checkpoint"),
        "checkpoint_build_digest": "a" * 64,
        "checkpoint_graph_digest": "b" * 64,
        "checkpoint_skip_indices": skipped,
    }
    options.update(overrides)
    path = tmp_path / "worker.manifest"
    workers.write_worker_manifest(
        str(path), str(tmp_path / "result"), str(tmp_path / "ir"),
        str(tmp_path / "exports"), str(tmp_path / "ast"),
        ["/src/a.py", "/src/b.py", "/src/c.py"],
        ["pkg.a", "pkg.b", "pkg.c"], list(assigned), **options,
    )
    return path


def test_v5_keeps_original_assignments_separate_from_skip_mask(tmp_path):
    path = _write_manifest(tmp_path)
    assert path.read_text().splitlines()[0] == workers.WORKER_MANIFEST_V5
    manifest = workers.read_worker_manifest(str(path))
    assert manifest["assigned_indices"] == [0, 1, 2]
    assert manifest["checkpoint_skip_indices"] == [0, 2]
    assert manifest["module_names"] == ["pkg.a", "pkg.b", "pkg.c"]
    assert manifest["sibling_inits"] == ("pkg.b", "pkg.c")


@pytest.mark.parametrize("skipped", [(3,), (-1,), (1, 1), (True,)])
def test_v5_rejects_invalid_skip_masks(tmp_path, skipped):
    with pytest.raises(workers.FrontendWorkerContractError, match="skip mask"):
        _write_manifest(tmp_path, skipped=skipped)


@pytest.mark.parametrize("assigned", [(0, 0), (), (0, 3), (False, 1)])
def test_v5_rejects_invalid_original_assignments(tmp_path, assigned):
    with pytest.raises(workers.FrontendWorkerContractError, match="assignment"):
        _write_manifest(tmp_path, assigned=assigned, skipped=())


def test_v5_rejects_truncation_and_trailing_records(tmp_path):
    path = _write_manifest(tmp_path)
    original = path.read_text()
    path.write_text("\n".join(original.splitlines()[:-1]) + "\n")
    with pytest.raises(workers.FrontendWorkerContractError, match="truncated"):
        workers.read_worker_manifest(str(path))
    path.write_text(original + "extra\n")
    with pytest.raises(workers.FrontendWorkerContractError, match="trailing"):
        workers.read_worker_manifest(str(path))


def test_checkpoint_off_retains_v4_wire(tmp_path):
    path = _write_manifest(
        tmp_path, checkpoint_root="", checkpoint_build_digest="",
        checkpoint_graph_digest="", checkpoint_skip_indices=(),
    )
    assert path.read_text().splitlines()[0] == workers.WORKER_MANIFEST_V4
    manifest = workers.read_worker_manifest(str(path))
    assert manifest["checkpoint_root"] == ""
    assert manifest["checkpoint_skip_indices"] == []


def test_native_worker_rejects_checkpoint_before_loading_graph(tmp_path):
    manifest = workers.read_worker_manifest(str(_write_manifest(tmp_path)))
    with pytest.raises(Exception, match="owner or identity"):
        execution._validate_stage1_checkpoint_worker(manifest, lambda: "/pcc1")


def test_shared_named_types_prevent_selective_skip(tmp_path, monkeypatch):
    context = ir.Context()
    context.get_identified_type("state_from_an_earlier_module")
    monkeypatch.setattr(ir, "global_context", context)
    with pytest.raises(Exception, match="identified-type context"):
        execution._validate_stage1_checkpoint_module_context({}, {}, 0)


def test_restored_tsv_keeps_link_metadata_and_does_not_repeat_timing(tmp_path):
    record = {
        "needs_libpython": False,
        "needs_native_extension_exports": True,
        "ir_bytes_before_passes": 123,
        "artifact_path": str(tmp_path / "module_7.direct.pco"),
        "parse_ms": 17,
        "infer_ms": 23,
        "codegen_ms": 999,
    }
    line = execution._stage1_checkpoint_result_line(record, 7, "pkg.leaf", str(tmp_path), True)
    assert line.split("\t") == [
        "OK", "7", "pkg.leaf", "0", "1", "123",
        str(tmp_path / "module_7.ll"), "0", "0", "0", "PCO",
        record["artifact_path"], "REUSED", "1",
    ]
    assert (tmp_path / "module_7.ll").read_bytes() == b""


def test_worker_skips_hit_without_switching_to_singleton_exports(tmp_path, monkeypatch):
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    manifest = workers.read_worker_manifest(str(_write_manifest(
        tmp_path, assigned=(0, 1), skipped=(0,),
    )))
    events = []
    monkeypatch.setattr(execution, "_validate_stage1_checkpoint_worker", lambda *_args: {})
    monkeypatch.setattr(execution, "_validate_stage1_checkpoint_module_context", lambda *_args: None)
    monkeypatch.setattr(execution, "_freeze_worker_survivors", lambda: None)
    monkeypatch.setattr(checkpoint, "load_module", lambda *_args: {
        "needs_libpython": False, "needs_native_extension_exports": True,
        "ir_bytes_before_passes": 123, "artifact_path": str(tmp_path / "reused.pco"),
    })
    Path(manifest["ir_dir"]).mkdir()

    def read_exports(_path):
        events.append("full exports")
        return {"pkg.a": {}, "pkg.b": {}, "pkg.c": {}}, {}

    def read_ast(path):
        events.append(Path(path).name)
        return object()

    def infer(ast, **_kwargs):
        events.append("infer pending")
        return ast

    def codegen(*_args):
        events.append("codegen pending")
        raise RuntimeError("stop at the pending module without compiling native code")

    monkeypatch.setattr(type_infer, "infer_module", infer)
    monkeypatch.setattr(layer1, "L1CodeGen", codegen)
    result = execution.run_codegen_worker(
        str(tmp_path / "worker.manifest"), read_manifest=lambda _path: manifest,
        run_export_worker_callback=lambda *_args: pytest.fail("not an export job"),
        run_summary_worker_callback=lambda *_args: pytest.fail("not a summary job"),
        worker_timing_enabled=lambda: False, native_worker_executable=lambda: "",
        read_native_exports_wire=read_exports,
        read_native_exports_wire_for_module=lambda *_args: pytest.fail("singleton reader changed context"),
        read_ast_wire=read_ast,
        build_closed_world_context=lambda *_args, **_kwargs: pytest.fail("unexpected context rebuild"),
        module_imports_native_extension=lambda *_args, **_kwargs: False,
        contextual_host_params_for_module=lambda *_args: {},
        module_uses_default_native_exports=lambda *_args: False,
        copy_native_module_exports=lambda value: value,
        closed_world_function_object_exports=lambda *_args: {},
        log=lambda *_args: None, ir_needs_libpython=lambda *_args: False,
        safe_exception_text=str, write_worker_error=workers.write_worker_error,
        pipeline_error=RuntimeError,
    )
    assert result == 1
    assert events == ["full exports", "module_1.json", "infer pending", "codegen pending"]
    assert "stop at the pending module" in Path(manifest["result_path"]).read_text()


@pytest.mark.parametrize("change", [None, "source", "object", "runtime", "metadata", "order"])
def test_link_boundary_rechecks_bytes_and_restored_metadata(tmp_path, monkeypatch, change):
    runtime = tmp_path / "runtime.a"
    source = tmp_path / "source.py"
    artifact = tmp_path / "module_0.direct.pco"
    runtime.write_bytes(b"runtime")
    source.write_text("pass\n")
    artifact.write_bytes(b"object validated by mocked receipt owner")
    graph = {
        "runtime_path": str(runtime), "runtime_sha256": checkpoint.file_sha256(str(runtime)),
        "module_names": ["pkg.main"],
        "sources": [{"source_path": str(source), "source_sha256": checkpoint.file_sha256(str(source))}],
    }
    summary = {
        "missing_modules": 0, "rejected_modules": 0, "unique_completed_modules": 1,
        "module_count": 1, "graph_digest": "g" * 64,
        "modules": [{
            "index": 0, "module_name": "pkg.main",
            "object_sha256": checkpoint.file_sha256(str(artifact)),
            "needs_libpython": False, "needs_native_extension_exports": True,
            "ir_bytes_before_passes": 123,
        }],
    }
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_DIR", str(tmp_path / "checkpoint"))
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_BUILD", "b" * 64)
    monkeypatch.setenv("PCC_STAGE1_CHECKPOINT_STAGE", "1")
    monkeypatch.setattr(pipeline, "_python_frontend_worker_executable", lambda: "")
    monkeypatch.setattr(checkpoint, "summarize", lambda *_args: summary)
    monkeypatch.setattr(checkpoint, "load_graph", lambda *_args: graph)
    if change == "source":
        source.write_text("changed = True\n")
    elif change == "object":
        artifact.write_bytes(b"same path, changed bytes")
    elif change == "runtime":
        runtime.write_bytes(b"same path, changed runtime")
    name = "other.module" if change == "order" else "pkg.main"
    args = ([(name, "PCO", str(artifact))], str(runtime), False, change != "metadata", 123)
    if change is None:
        pipeline._validate_stage1_checkpoint_link(*args)
    else:
        with pytest.raises(pipeline.PyPipelineError, match="Stage1 checkpoint"):
            pipeline._validate_stage1_checkpoint_link(*args)


def test_scheduler_preserves_full_chunk_when_only_one_module_remains(tmp_path, monkeypatch):
    root = tmp_path / "checkpoint"
    root.mkdir()
    payload = {
        "stage": 1, "producer_role": "host-pcc0", "owner": "cpython",
        # Checkpoints exist only for x86_64 Linux Stage1 builds; this
        # scheduling fixture compiles nothing, so any host can run it.
        "backend": "self", "target": "x86_64-unknown-linux-gnu",
    }
    build_digest = checkpoint.canonical_digest(payload)
    (root / "build.json").write_text(json.dumps({
        "schema": checkpoint.BUILD_SCHEMA,
        "identity_sha256": build_digest, "payload": payload,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"runtime identity fixture, not executable")
    sources = []
    for index in range(3):
        source = tmp_path / ("input_" + str(index) + ".py")
        source.write_text("pass\n")
        sources.append(str(source))
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    for key, value in {
        "PCC_STAGE1_CHECKPOINT_DIR": str(root),
        "PCC_STAGE1_CHECKPOINT_BUILD": build_digest,
        "PCC_STAGE1_CHECKPOINT_STAGE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_NATIVE_OBJECT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
        "PCC_PYTHON_IR_PASSES": "off",
        "PCC_RUNTIME_ARCHIVE": str(runtime),
    }.items():
        monkeypatch.setenv(key, value)
    # The graph is stamped with the host triple; act as the x86_64 Linux host.
    import pcc.frontends.python.pipeline_targets as targets
    monkeypatch.setattr(targets, "host_target_triple", lambda: "x86_64-unknown-linux-gnu")
    manifests = []
    counters = {}

    def exports(tmp, _sources, _names, _chunks, _prefix, **kwargs):
        for index in range(3):
            (Path(kwargs["ast_dir"]) / ("module_" + str(index) + ".json")).write_text("{}")
        path = Path(tmp) / "exports"
        path.write_text("full graph fixture")
        return str(path)

    def load(_root, _build, _graph, index, _name, assigned, _artifacts):
        assert assigned == [0, 1, 2]
        return {"fixture": True} if index in (0, 2) else None

    def write_manifest(*args, **kwargs):
        workers.write_worker_manifest(*args, **kwargs)
        manifests.append(workers.read_worker_manifest(args[0]))

    def run_commands(_commands, **_kwargs):
        assert len(manifests) == 1
        manifest = manifests[0]
        assert manifest["assigned_indices"] == [0, 1, 2]
        assert manifest["checkpoint_skip_indices"] == [0, 2]
        assert len(manifest["module_names"]) == 3
        lines = []
        for index, name in enumerate(manifest["module_names"]):
            path = artifact_dir / ("module_" + str(index) + ".direct.pco")
            path.write_bytes(b"scheduling fixture, not an object")
            line = "OK\t" + str(index) + "\t" + name + "\t0\t1\t0\tunused\tPCO\t" + str(path)
            if index in manifest["checkpoint_skip_indices"]:
                line += "\tREUSED\t1"
            lines.append(line)
        Path(manifest["result_path"]).write_text("\n".join(lines) + "\n")

    monkeypatch.setattr(checkpoint, "load_module", load)
    result = parallel.compile_parallel_uncached(
        sources, ["pkg.a", "pkg.b", "pkg.c"], jobs=1,
        entry_module="pkg.a", sibling_inits=("pkg.b", "pkg.c"),
        libpython_mode="off", ir_scaffold_mode="on", verbose=False,
        artifact_dir=str(artifact_dir), can_spawn_worker=lambda: True,
        worker_command_prefix=lambda: ["python", "-m", "pcc"],
        chunk_count_for_workers=lambda *_args: 1,
        codegen_chunks=lambda *_args: [[0, 1, 2]],
        ast_wire_enabled=lambda: True, build_shared_exports_callback=exports,
        write_manifest=write_manifest, shell_quote_arg=lambda text: text,
        worker_arg="--worker", worker_env_prefix=lambda: "",
        join_strings=lambda values, separator: separator.join(values),
        run_worker_commands=run_commands, profiled_gc_collect=lambda *_a, **_k: None,
        read_worker_ir=lambda *_args: pytest.fail("direct objects must not read IR"),
        profile_begin=lambda *_args: 0, profile_end=lambda *_args: None,
        profile_counter=lambda _profile, name, value: counters.update({name: value}),
        pipeline_error=RuntimeError,
    )
    assert [row[0] for row in result[7]] == ["pkg.a", "pkg.b", "pkg.c"]
    assert result[2] is True
    assert counters["multi_frontend_checkpoint_hits"] == 2
    assert counters["multi_frontend_checkpoint_compiled"] == 1
