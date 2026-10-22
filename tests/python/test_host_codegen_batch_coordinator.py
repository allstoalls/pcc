"""Draft callback-only host batch coordinator wiring contracts; all UNRUN.

Only placeholder source/AST/IR/object/TSV files are used. No worker process,
compiler, real object encoder, or measured-memory admission is exercised.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def coordinator_case(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline_frontend_parallel as parallel

    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "PCC_DIRECT_INDEXED_NATIVE_OBJECT",
    ):
        monkeypatch.setenv(name, "1")
    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_SIDECAR", "PCC_HOST_INDEXED_PROCESS_SPLIT",
        "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN",
    ):
        monkeypatch.setenv(name, "0")
    for name in (
        "PCC_STAGE1_CHECKPOINT_DIR", "PCC_STAGE1_CHECKPOINT_BUILD",
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN", "PCC_INDEXED_HANDOFF_REQUEST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PCC_HOST_CODEGEN_BATCH", "2")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    monkeypatch.setenv("PCC_BACKEND", "self")

    def make_case(corruption=""):
        root = tmp_path / (corruption or "valid")
        root.mkdir()
        names = ["m0", "m1", "m2"]
        sources = []
        for index, name in enumerate(names):
            source = root / (name + ".py")
            source.write_text("x = " + str(index) + "\n", encoding="utf-8")
            sources.append(str(source))
        state = SimpleNamespace(
            manifests=[], command_batches=[], completed=[], counters={},
            chunk_requests=[], export_chunks=[], names=names, artifacts=root / "artifacts",
        )

        def chunk_count(count, jobs, prefix):
            state.chunk_requests.append((count, jobs, prefix))
            return count

        def chunks(paths, count):
            assert paths == sources and count == 3
            return [[0], [1], [2]]

        def build_exports(tmp, paths, module_names, export_chunks, _prefix, **kwargs):
            assert paths == sources and module_names == names
            assert kwargs["max_parallel"] == kwargs["safe_parallel"] == 2
            state.export_chunks = [list(chunk) for chunk in export_chunks]
            for index in range(3):
                (Path(kwargs["ast_dir"]) / ("module_" + str(index) + ".json")).write_bytes(b"{}")
            exports = Path(tmp) / "exports.json"
            exports.write_text("{}", encoding="utf-8")
            return str(exports)

        def write_manifest(path, result, ir_dir, exports, ast_dir, paths,
                           module_names, assigned_indices, **kwargs):
            assert paths == sources and module_names == names
            assert Path(exports).is_file() and not kwargs["checkpoint_root"]
            assert all((Path(ast_dir) / ("module_" + str(index) + ".json")).is_file()
                       for index in assigned_indices)
            state.manifests.append(SimpleNamespace(
                path=path, result=result, ir_dir=ir_dir, indices=list(assigned_indices),
            ))

        def row(index, ir_dir):
            stem = Path(ir_dir) / ("module_" + str(index))
            ir_path = Path(str(stem) + ".ll")
            object_path = Path(str(stem) + ".direct.pco")
            text = "; " + names[index] + "\n"
            ir_path.write_text(text, encoding="utf-8")
            object_path.write_bytes(("object:" + str(index)).encode("ascii"))
            return "\t".join(("OK", str(index), names[index], "0", "0",
                               str(len(text)), str(ir_path), "PCO", str(object_path)))

        def run_commands(commands, *, max_parallel):
            state.command_batches.append((len(commands), max_parallel))
            selected = [manifest for manifest in state.manifests
                        if any(manifest.path in command for command in commands)]
            assert len(selected) == len(commands)
            # Reverse completion and in-batch rows; collection must restore input order.
            for manifest in reversed(selected):
                rows = [row(index, manifest.ir_dir) for index in reversed(manifest.indices)]
                if manifest.indices == [2]:
                    if corruption == "missing":
                        rows = []
                    elif corruption == "duplicate":
                        rows += rows
                    elif corruption == "outside-assignment":
                        rows = [row(0, manifest.ir_dir)]
                Path(manifest.result).write_text("\n".join(rows) + ("\n" if rows else ""),
                                                 encoding="utf-8")
                state.completed.append(manifest.indices)

        def unexpected_read(*_args):
            pytest.fail("PCO results must not reload IR text")

        def run():
            return parallel.compile_parallel_uncached(
                sources, names, jobs=2, entry_module="m0", sibling_inits=(),
                libpython_mode="off", ir_scaffold_mode="on", verbose=False,
                action_cache_plan=None, artifact_dir=str(state.artifacts),
                can_spawn_worker=lambda: True, worker_command_prefix=lambda: ["python3"],
                chunk_count_for_workers=chunk_count, codegen_chunks=chunks,
                ast_wire_enabled=lambda: True, build_shared_exports_callback=build_exports,
                write_manifest=write_manifest, shell_quote_arg=str, worker_arg="--worker",
                worker_env_prefix=lambda: "PCC_PY_FRONTEND_JOBS=1",
                join_strings=lambda values, sep: sep.join(values),
                run_worker_commands=run_commands,
                profiled_gc_collect=lambda *_args, **_kwargs: None,
                read_worker_ir=unexpected_read, profile_begin=lambda _profile: 0,
                profile_end=lambda *_args: None,
                profile_counter=lambda _profile, key, value: state.counters.update({key: value}),
                pipeline_error=RuntimeError,
            )

        state.run = run
        return state

    return make_case


def test_host_batch_coordinator_preserves_width_and_module_order(coordinator_case):
    case = coordinator_case()
    result = case.run()
    assert [manifest.indices for manifest in case.manifests] == [[0, 1], [2]]
    assert case.chunk_requests == [(3, 2, ["python3"])]
    assert case.export_chunks == [[0, 1, 2]]
    assert case.command_batches == [(2, 2)]
    assert case.completed == [[2], [0, 1]]
    assert case.counters["multi_frontend_worker_requested_concurrency"] == 2
    assert case.counters["multi_frontend_codegen_safe_jobs"] == 2
    assert result[0] == [(name, "") for name in case.names]
    expected_objects = [(name, str(case.artifacts / ("module_" + str(index) + ".direct.pco")))
                        for index, name in enumerate(case.names)]
    assert result[5] is None
    assert result[6] == expected_objects
    assert result[7] == [(name, "PCO", path) for name, path in expected_objects]
    for index, (_name, path) in enumerate(expected_objects):
        assert Path(path).read_bytes() == ("object:" + str(index)).encode("ascii")


def test_host_batch_coordinator_rejects_malformed_second_batch(coordinator_case):
    # Keep one node while independently exercising all three binding failures.
    for corruption in ("missing", "duplicate", "outside-assignment"):
        case = coordinator_case(corruption)
        with pytest.raises(RuntimeError, match="host codegen batch result (omitted|binding)"):
            case.run()
        assert [manifest.indices for manifest in case.manifests] == [[0, 1], [2]]
        assert case.command_batches == [(2, 2)]
        assert case.completed == [[2], [0, 1]]
