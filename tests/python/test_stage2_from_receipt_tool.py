from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "run_pcc_stage2_from_receipt.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "pcc_stage2_from_receipt_test_module", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    scripts = str(TOOL.parent)
    sys.path.insert(0, scripts)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stage2_tool_validates_and_materializes_stage1_record(tmp_path: Path):
    tool = _load_tool()
    stage1_dir = tmp_path / "stage1"
    source = stage1_dir / "source-snapshot"
    runtime = stage1_dir / "runtime-bundle" / "libpy_runtime_pcc_py.a"
    compiler = stage1_dir / "pcc1"
    source.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    compiler.write_bytes(b"pcc1")
    compiler.chmod(0o755)
    runtime.write_bytes(b"runtime")
    (stage1_dir / "manifest.json").write_text(
        json.dumps({"status": "SUCCEEDED"}), encoding="utf-8"
    )
    receipt = {
        "status": "SUCCEEDED",
        "source_snapshot": "source-snapshot",
        "compiler_sha256": _sha256(compiler),
        "runtime_archive_sha256": _sha256(runtime),
    }
    (stage1_dir / "build-receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )

    class FakeAB:
        sha256_path = staticmethod(_sha256)

    checked = []

    class FakeStage1:
        @staticmethod
        def _require_immutable_source(path, _ab):
            checked.append(path)

    record = tool._stage1_record(stage1_dir, ab=FakeAB, stage1=FakeStage1)

    assert record == {
        "compiler": str(compiler.resolve()),
        "source_snapshot": str(source.resolve()),
        "receipt_path": str((stage1_dir / "build-receipt.json").resolve()),
    }
    assert checked == [source.resolve()]


def test_stage2_tool_rejects_compiler_hash_drift(tmp_path: Path):
    tool = _load_tool()
    stage1_dir = tmp_path / "stage1"
    source = stage1_dir / "source-snapshot"
    runtime = stage1_dir / "runtime-bundle" / "libpy_runtime_pcc_py.a"
    compiler = stage1_dir / "pcc1"
    source.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    compiler.write_bytes(b"changed")
    compiler.chmod(0o755)
    runtime.write_bytes(b"runtime")
    (stage1_dir / "manifest.json").write_text(
        json.dumps({"status": "SUCCEEDED"}), encoding="utf-8"
    )
    (stage1_dir / "build-receipt.json").write_text(
        json.dumps(
            {
                "status": "SUCCEEDED",
                "source_snapshot": "source-snapshot",
                "compiler_sha256": "not-the-current-hash",
                "runtime_archive_sha256": _sha256(runtime),
            }
        ),
        encoding="utf-8",
    )

    class FakeAB:
        sha256_path = staticmethod(_sha256)

    class FakeStage1:
        @staticmethod
        def _require_immutable_source(_path, _ab):
            raise AssertionError("hash drift must fail before source validation")

    try:
        tool._stage1_record(stage1_dir, ab=FakeAB, stage1=FakeStage1)
    except tool.Stage2ReceiptError as exc:
        assert "compiler hash" in str(exc)
    else:
        raise AssertionError("Stage2 tool accepted a drifted pcc1")


def test_stage2_tool_defaults_to_two_workers_and_automatic_cap():
    tool = _load_tool()
    args = tool._parser().parse_args(
        ["--stage1-dir", "/tmp/stage1", "--output-dir", "/tmp/stage2"]
    )

    assert args.self_backend_jobs == 2
    assert args.max_tree_rss_bytes == 0
    tool._validate_limits(args)

    args.max_tree_rss_bytes = 16 * 1024 * 1024 * 1024
    tool._validate_limits(args)
    args.max_tree_rss_bytes += 1
    with pytest.raises(tool.Stage2ReceiptError, match="16 GiB"):
        tool._validate_limits(args)
    args.max_tree_rss_bytes = 8 * 1024 * 1024 * 1024

    args.self_backend_jobs = 3
    with pytest.raises(tool.Stage2ReceiptError, match="1..2"):
        tool._validate_limits(args)


def _fake_prior_stage2(tmp_path: Path) -> Path:
    import os

    prior = tmp_path / "prior"
    stage2 = prior / "stage2"
    sources = tmp_path / "sources"
    sources.mkdir(parents=True)
    state = stage2 / "pcc2.pcc-codegen-plan.state.123"
    (state / "manifests").mkdir(parents=True)
    (state / "results").mkdir(parents=True)
    sizes = (100_000, 50_000, 30_000, 20_000)
    rows = []
    for index, size in enumerate(sizes):
        source = sources / f"m{index}.py"
        source.write_text("#" * size, encoding="utf-8")
        rows.append(f"{index}\tmod{index}\t{source}")
    manifest = state / "manifests" / "worker_0.manifest"
    manifest.write_text(
        "pcc.frontends.python.codegen_worker.v4\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    plan = stage2 / "pcc2.pcc-codegen-plan"
    plan.write_text("pcc.frontend-codegen-plan.v1\n", encoding="utf-8")
    base = 1_700_000_000.0
    os.utime(plan, (base, base))
    # The two largest modules completed: 150000 / 200000 = 75% of the bytes,
    # observed over 300s of lane wall.
    for offset, index in ((150.0, 0), (300.0, 1)):
        result = state / "results" / f"worker_{index}.tsv"
        result.write_text(f"OK\t{index}\tmod{index}\t0\t0\t0\tx\n", encoding="utf-8")
        os.utime(result, (base + offset, base + offset))
    return prior


def test_stage2_prediction_scales_lane_bytes_and_forbids_hopeless_runs(
    tmp_path: Path,
):
    tool = _load_tool()
    prior = _fake_prior_stage2(tmp_path)
    prediction = tool.predict_stage2_seconds(prior)
    assert prediction["completed_modules"] == 2
    assert prediction["total_modules"] == 4
    assert prediction["observed_lane_s"] == pytest.approx(300.0)
    # 300s / 0.75 byte fraction + measured checkpoint/link reserve.
    assert prediction["predicted_total_s"] == pytest.approx(
        400.0 + tool.STAGE2_CHECKPOINT_AND_LINK_RESERVE_S
    )
    with pytest.raises(tool.Stage2ReceiptError, match="prediction"):
        tool._validate_stage2_prediction(prediction, 600)
    tool._validate_stage2_prediction(prediction, 700)


def test_stage2_prediction_requires_at_least_one_completed_result(
    tmp_path: Path,
):
    tool = _load_tool()
    prior = _fake_prior_stage2(tmp_path)
    for result in (prior / "stage2" / "pcc2.pcc-codegen-plan.state.123" / "results").iterdir():
        result.unlink()
    with pytest.raises(tool.Stage2ReceiptError, match="completed"):
        tool.predict_stage2_seconds(prior)


def test_stage2_resource_preflight_parses_darwin_memory_and_swap():
    tool = _load_tool()
    vm_stat = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100.
Pages active:                             999.
Pages inactive:                           200.
Pages speculative:                         10.
Pages purgeable:                            20.
"""

    assert tool._parse_vm_stat_reclaimable(vm_stat) == 330 * 16384
    assert tool._parse_swapusage(
        "total = 16.00G  used = 2.00G  free = 14.00G  (encrypted)"
    ) == (16 * tool._GIB, 2 * tool._GIB, 14 * tool._GIB)


def test_stage2_resource_preflight_requires_host_and_swap_reserve():
    tool = _load_tool()
    cap = 8 * tool._GIB
    safe = tool._validate_resource_observation(
        max_tree_rss_bytes=cap,
        reclaimable_bytes=20 * tool._GIB,
        disk_free_bytes=40 * tool._GIB,
        swap_total_bytes=16 * tool._GIB,
        swap_used_bytes=2 * tool._GIB,
        swap_free_bytes=14 * tool._GIB,
    )
    assert safe["required_reclaimable_and_disk_free_bytes"] == 16 * tool._GIB

    with pytest.raises(tool.Stage2ReceiptError, match="reclaimable"):
        tool._validate_resource_observation(
            max_tree_rss_bytes=cap,
            reclaimable_bytes=15 * tool._GIB,
            disk_free_bytes=40 * tool._GIB,
            swap_total_bytes=0,
            swap_used_bytes=0,
            swap_free_bytes=0,
        )
    with pytest.raises(tool.Stage2ReceiptError, match="swap is already pressured"):
        tool._validate_resource_observation(
            max_tree_rss_bytes=cap,
            reclaimable_bytes=20 * tool._GIB,
            disk_free_bytes=40 * tool._GIB,
            swap_total_bytes=8 * tool._GIB,
            swap_used_bytes=6 * tool._GIB,
            swap_free_bytes=2 * tool._GIB,
        )


def test_stage2_runner_args_carry_every_resource_envelope_field():
    """The first completed capped Stage2 lost its receipt to a missing
    ``frontend_jobs`` in ``_resource_envelope``; the Namespace handed to the
    stage runner must satisfy the envelope without touching a real run."""
    tool = _load_tool()
    args = tool._parser().parse_args(
        ["--stage1-dir", "/tmp/stage1", "--output-dir", "/tmp/stage2"]
    )
    # run() resolves automatic selection before building this Namespace.
    args.max_tree_rss_bytes = 8 * 1024 * 1024 * 1024
    runner_args = tool._runner_args(args, {"PCC_GC_BACKEND": "3"})
    envelope = tool.stage_ab._resource_envelope(
        args=runner_args, environment={}, process={}
    )
    assert envelope["frontend_jobs"] == 2
    assert envelope["gc_backend"] == 3
    assert envelope["self_backend_jobs"] == 2
    assert envelope["max_tree_rss_bytes"] == 8 * 1024 * 1024 * 1024
    explicit = tool._parser().parse_args(
        ["--stage1-dir", "/tmp/s1", "--output-dir", "/tmp/s2", "--gc-backend", "1"]
    )
    assert tool._runner_args(explicit, {"PCC_GC_BACKEND": "4"}).gc_backend == 1
