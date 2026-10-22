"""Real-runtime pending-verdict controls; no automatic runtime provisioning.

Synthetic candidates make finalizer ordering deterministic. These cases do
not substitute for the original typed-loop-unbox workload or a Darwin run.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.owned_runtime_c_fixture import admitted_runtime, link_c_harness
from tests.python.process_timeout import run_process_group_timeout


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests/fixtures/native/gc_tracing_pending_verdict.c"
MODES = (
    (1, "live-transitive", (0, 0, 0)),
    (2, "dead-self-cycle", (1, 1, 0)),
    (3, "self-resurrection", (1, 0, 0)),
    (4, "resurrected-finalizer-batch", (2, 0, 0)),
    (5, "gc-start-callback", (0, 0, 2)),
    (6, "nonpositive-budget", (0, 0, 0)),
    (7, "partial-budget-finalizer-once", (2, 2, 0)),
)


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@pytest.fixture(scope="module")
def _verdict_binary(tmp_path_factory):
    # This generic owned C helper requires an explicit archive and verifies
    # runtime source/codegen/configuration, and denies external build tools.
    archive = admitted_runtime()
    provenance = Path(str(archive) + ".provenance.json")
    manifest = json.loads(provenance.read_text(encoding="utf-8"))
    assert manifest["members"]
    assert all(member["runtime_build_config"]["threads"] is True
               and member["runtime_build_config"]["refcount"] == "atomic"
               for member in manifest["members"])
    directory = tmp_path_factory.mktemp("gc_tracing_sweep_verdict")
    binary = directory / "sweep_verdict.out"
    source_sha = _digest(HARNESS)
    archive_sha = _digest(archive)
    provenance_sha = _digest(provenance)
    link_c_harness(HARNESS, binary)
    assert _digest(HARNESS) == source_sha
    assert _digest(archive) == archive_sha
    assert _digest(provenance) == provenance_sha
    identity = {
        "schema": "pcc.tracing-sweep-verdict.v1",
        "compiler": "host-pcc0",
        "backend": "self",
        "source_sha256": source_sha,
        "runtime_sha256": archive_sha,
        "runtime_provenance_sha256": provenance_sha,
        "binary_sha256": _digest(binary),
        "target_triple": manifest["target_triple"],
        "synthetic_pending_verdict": True,
        "runtime_provisioning": False,
    }
    (directory / "build.json").write_text(json.dumps(identity, indent=2) + "\n")
    return binary, identity


@pytest.mark.integration
@pytest.mark.parametrize("mode,name,counts", MODES, ids=[mode[1] for mode in MODES])
@pytest.mark.parametrize("backend", (2, 1, 3, 4), ids=lambda backend: f"gc{backend}")
def test_pending_verdict_native(_verdict_binary, backend, mode, name, counts):
    binary, identity = _verdict_binary
    environment = dict(os.environ, PCC_GC_BACKEND=str(backend),
                       PCC_GC_DEBT_THRESHOLD="1099511627776",
                       PCC_TEST_NO_NATIVE_PROVISIONING="1", PCC_NO_AUTO_PCC1="1")
    environment.pop("LC_ALL", None)
    result = run_process_group_timeout([str(binary), str(mode)], env=environment, timeout=15)
    receipt = dict(identity, gc_backend=backend, mode=mode, case=name,
                   command=[str(binary), str(mode)], returncode=result.returncode,
                   stdout=result.stdout, stderr=result.stderr)
    (binary.parent / f"gc{backend}-{name}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    assert result.returncode == 0, receipt
    assert result.stderr == "", receipt
    finalizers, weak_callbacks, gc_callbacks = counts
    assert result.stdout == (
        f"sweep-verdict:ok backend={backend} mode={mode} finalizers={finalizers} "
        f"weak_callbacks={weak_callbacks} gc_callbacks={gc_callbacks}\n"
    ), receipt
