"""Bounded batching and complete-result contracts; draft execution UNRUN."""

import pytest

from pcc.frontends.python import pipeline_frontend_host_batch as batch


def _env():
    return {
        "PCC_HOST_CODEGEN_BATCH": "2",
        "PCC_WORKER_TREE_BUDGET_BYTES": str(8 * 1024 * 1024 * 1024),
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
    }


def _limit(env, **overrides):
    options = dict(native_worker=False, indexed_split=False, checkpoint_root="",
                   action_cache_plan=None, libpython_mode="off", artifact_dir="artifacts")
    options.update(overrides)
    return batch.host_codegen_batch_limit(env, **options)


def test_batching_is_explicit_and_exactly_two():
    assert _limit({}) == 1
    assert _limit({"PCC_HOST_CODEGEN_BATCH": "1"}) == 1
    assert _limit(_env()) == 2
    for budget in ("", "0", "-1", "invalid"):
        with pytest.raises(ValueError, match="requires measured tree admission"):
            _limit(dict(_env(), PCC_WORKER_TREE_BUDGET_BYTES=budget))
    for size in ("3", "4", "auto", "-1"):
        with pytest.raises(ValueError, match="must be 1 or 2"):
            _limit(dict(_env(), PCC_HOST_CODEGEN_BATCH=size))


@pytest.mark.parametrize("override", [
    {"native_worker": True}, {"indexed_split": True},
    {"checkpoint_root": "checkpoint"}, {"action_cache_plan": {}},
])
def test_batching_rejects_other_ownership_and_cache_protocols(override):
    with pytest.raises(ValueError, match="cannot share"):
        _limit(_env(), **override)


def test_batching_requires_direct_release_without_oracle():
    for name in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND", "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK"):
        env = _env()
        env[name] = "0"
        with pytest.raises(ValueError, match="requires " + name):
            _limit(env)
    for name in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_SIDECAR", "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN"):
        with pytest.raises(ValueError, match="does not support " + name):
            _limit(dict(_env(), **{name: "1"}))


def test_batch_pairs_have_a_real_cardinality_cap_and_leave_large_inputs_alone(tmp_path):
    source_paths = []
    ast_dir = tmp_path / "ast"
    ast_dir.mkdir()
    for index in range(7):
        source = tmp_path / (str(index) + ".py")
        source.write_text("#" * (200_000 if index == 2 else 10))
        source_paths.append(str(source))
        (ast_dir / ("module_" + str(index) + ".json")).write_text("[]")
    chunks = [[index] for index in range(7)]
    actual = batch.bounded_host_codegen_batches(source_paths, str(ast_dir), chunks)
    assert actual == [[0, 1], [2], [3, 4], [5, 6]]
    assert max(map(len, actual)) == 2
    assert [index for chunk in actual for index in chunk] == list(range(7))
    with pytest.raises(ValueError, match="singleton"):
        batch.bounded_host_codegen_batches(source_paths, str(ast_dir), [[0, 1]])
    with pytest.raises(ValueError, match="assignment"):
        batch.bounded_host_codegen_batches(source_paths, str(ast_dir), [[0], [0]])


def _rows(tmp_path):
    rows = []
    for index, name in enumerate(("first", "second")):
        ir = tmp_path / ("module_" + str(index) + ".ll")
        obj = tmp_path / ("module_" + str(index) + ".direct.pco")
        ir.write_text("")
        obj.write_bytes(b"placeholder")
        rows.append("\t".join(("OK", str(index), name, "0", "0", "0", str(ir), "PCO", str(obj))))
    return rows


def test_batch_result_accepts_only_the_complete_assigned_set(tmp_path):
    rows = _rows(tmp_path)
    batch.validate_host_batch_result("\n".join(rows), [0, 1], ["first", "second"], str(tmp_path))
    batch.validate_host_batch_result("\n".join(reversed(rows)), [0, 1], ["first", "second"], str(tmp_path))
    timed = [row.replace("\tPCO\t", "\t1\t2\t3\tPCO\t") for row in rows]
    batch.validate_host_batch_result("\n".join(timed), [0, 1], ["first", "second"], str(tmp_path))
    for bad_timing in ("-1", "invalid"):
        bad = timed[1].replace("\t1\t2\t3\tPCO\t", "\t" + bad_timing + "\t2\t3\tPCO\t")
        with pytest.raises(ValueError, match="timing"):
            batch.validate_host_batch_result(timed[0] + "\n" + bad, [0, 1], ["first", "second"], str(tmp_path))
    for bad in (rows[:1], [rows[0], rows[0]], [rows[0], rows[1].replace("\tsecond\t", "\tfirst\t")],
                [rows[0], rows[1].replace("\t1\tsecond\t", "\t9\tsecond\t")]):
        with pytest.raises(ValueError):
            batch.validate_host_batch_result("\n".join(bad), [0, 1], ["first", "second"], str(tmp_path))


def test_batch_result_preserves_worker_error_before_accepting_rows(tmp_path):
    rows = _rows(tmp_path)
    with pytest.raises(ValueError, match="injected failure"):
        batch.validate_host_batch_result(rows[0] + "\nERR\tinjected failure\n", [0, 1],
                                         ["first", "second"], str(tmp_path))


def test_batch_result_rejects_stale_other_module_artifact(tmp_path):
    rows = _rows(tmp_path)
    rows[1] = rows[1].replace("module_1.direct.pco", "module_0.direct.pco")
    with pytest.raises(ValueError, match="artifact"):
        batch.validate_host_batch_result("\n".join(rows), [0, 1], ["first", "second"], str(tmp_path))
