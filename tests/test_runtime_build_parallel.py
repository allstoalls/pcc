"""Runtime modules compile in parallel workers into the same archive bytes.

The owned runtime build compiled its 181 modules one after another in one
process (184 s wall at 97% of one core on an M-series host), so every
codegen edit cost minutes before a native test could run. Workers now
compile module batches through the shared compiler worker pool; the parent
assembles members in the canonical module order, so the archive, its C-API
inventory and its provenance do not depend on the worker count.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import owned_runtime_build as build
from pcc.frontends.python.pipeline_targets import host_target_triple


ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "freestanding_platform_system",
    "py_capi_stdio_runtime",
    "freestanding_platform_io",
    "py_capi_private_runtime",
    "py_int_bigint_convert",
]


def _build(tmp_path, monkeypatch, jobs, label, modules=MODULES):
    monkeypatch.setattr(build, "runtime_modules", lambda runtime_dir, target, threads=False: list(modules))
    monkeypatch.setattr(build, "require_native_provisioning_allowed", lambda: None)
    monkeypatch.setenv("PCC_RUNTIME_BUILD_JOBS", str(jobs))
    archive = tmp_path / label / "libpy_runtime_pcc_py.a"
    archive.parent.mkdir()
    build.build_runtime_archive(str(ROOT / "pcc" / "runtime"), str(archive), host_target_triple())
    return archive


def test_parallel_workers_produce_the_serial_archive(tmp_path, monkeypatch):
    launched = []
    real_pool = build.run_worker_processes

    def recording_pool(commands, width):
        launched.append((len(commands), width))
        return real_pool(commands, width)

    monkeypatch.setattr(build, "run_worker_processes", recording_pool)
    serial = _build(tmp_path, monkeypatch, 1, "serial")
    assert launched == []
    parallel = _build(tmp_path, monkeypatch, 3, "parallel")
    assert launched and launched[0][1] == 3 and launched[0][0] >= 3
    assert parallel.read_bytes() == serial.read_bytes()
    for suffix in (".capi_syms", ".provenance.json"):
        assert Path(str(parallel) + suffix).read_bytes() == Path(str(serial) + suffix).read_bytes()


def test_worker_failure_publishes_no_archive(tmp_path, monkeypatch):
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        _build(tmp_path, monkeypatch, 2, "broken", modules=MODULES[:2] + ["no_such_runtime_module"])
    assert not (tmp_path / "broken" / "libpy_runtime_pcc_py.a").exists()


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_invalid_job_count_is_rejected(monkeypatch, value):
    monkeypatch.setenv("PCC_RUNTIME_BUILD_JOBS", value)
    with pytest.raises(ValueError):
        build.runtime_build_jobs(10)
