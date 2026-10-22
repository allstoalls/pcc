"""UNRUN host-pool cancellation/retry integration with simulated batch artifacts.

No compiler, object encoder, native FFI, or runtime build is exercised. The
configured pool budget stays fixed; this does not prove an external tree guard.
"""

import json
import os
from pathlib import Path
import shlex
import sys

import pytest


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: os.name == "posix" and sys.implementation.name == "cpython")
def test_cancelled_host_batch_retries_all_modules_before_atomic_acceptance(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline_frontend_workers as workers
    from pcc.frontends.python import worker_process_pool as pool
    from pcc.frontends.python import worker_resource_plan as policy
    from pcc.frontends.python.pipeline_frontend_host_batch import validate_host_batch_result

    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    source = Path(pool.__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(source)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    ))
    child = tmp_path / "batch_retry_child.py"
    child.write_text(r'''
import json
import os
from pathlib import Path
import sys
import time
from pcc.frontends.python.worker_resource_plan import (
    RESOURCE_REPORT_ENV, RESOURCE_TOKEN_ENV, publish_worker_resource,
)

assert sys.implementation.name == "cpython"
root = Path(sys.argv[1])
index, growth = int(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + 30
artifacts = root / "artifacts"
artifacts.mkdir(exist_ok=True)
result = artifacts / "result.tsv"
partial = artifacts / "result.tsv.batch.partial"

def atomic_text(path, text):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

def wait_for(predicate):
    while not predicate():
        assert time.monotonic() < deadline, (index, "event coordination timed out")
        time.sleep(0.005)

def seen(event, number):
    trace = root / "admission.tsv"
    return trace.exists() and any(
        line.split("\t")[:2] == [event, str(number)]
        for line in trace.read_text().splitlines()
    )

count = root / ("attempts." + str(index))
attempt = int(count.read_text()) + 1 if count.exists() else 1
atomic_text(count, str(attempt))
record = {
    "pid": os.getpid(), "token": os.environ[RESOURCE_TOKEN_ENV], "attempt": attempt,
    "budget": int(os.environ["PCC_WORKER_TREE_BUDGET_BYTES"]),
}
identity = str(record["pid"]) + "|" + record["token"] + "|" + str(attempt)
record["identity"] = identity
atomic_text(root / ("started." + str(index) + "." + str(attempt)), json.dumps(record))
payloads = [bytearray(4 * 1024 ** 2)]
publish_worker_resource("waiting")

if index == 0:
    wait_for(lambda: (root / "first-module-staged").exists())
    first = json.loads((root / "started.1.1").read_text())
    os.kill(first["pid"], 0)
    payloads.append(bytearray(growth))
    publish_worker_resource("leader-grown")
    # This event is emitted only after the pool stops and reaps the peer.
    wait_for(lambda: seen("cancel", 1))
    old = "OLD|" + first["identity"] + "|module:0\n"
    assert (artifacts / "module_0.ll").read_text() == old
    assert (artifacts / "module_0.direct.pco").read_bytes() == old.encode("ascii")
    assert not (artifacts / "module_1.direct.pco").exists()
    assert not result.exists()
    assert not (root / "complete.1.1").exists()
    assert len(partial.read_text().splitlines()) == 1
    atomic_text(root / "cancelled-report", (root / "rss1").read_text())
    atomic_text(root / "cancelled-first", json.dumps({
        "identity": first["identity"], "old_bytes": old,
        "success_tsv": False, "complete": False,
    }))
elif attempt == 1:
    old = "OLD|" + identity + "|module:0\n"
    (artifacts / "module_0.ll").write_text(old, encoding="utf-8")
    (artifacts / "module_0.direct.pco").write_bytes(old.encode("ascii"))
    partial.write_text("OK\t0\tmain\tINCOMPLETE\n", encoding="utf-8")
    publish_worker_resource("module0-staged")
    atomic_text(root / "first-module-staged", identity)
    wait_for(lambda: False)  # Only real cancellation can leave this branch.
else:
    assert attempt == 2
    assert (root / "cancelled-first").exists()
    assert not result.exists()
    first = json.loads((root / "started.1.1").read_text())
    assert first["pid"] != record["pid"] and first["token"] != record["token"]
    old = "OLD|" + first["identity"] + "|module:0\n"
    assert (artifacts / "module_0.direct.pco").read_bytes() == old.encode("ascii")
    rows, written = [], []
    for module_index, name in enumerate(("main", "sibling")):
        text = "NEW|" + identity + "|module:" + str(module_index) + "\n"
        stem = artifacts / ("module_" + str(module_index))
        ir_path, object_path = Path(str(stem) + ".ll"), Path(str(stem) + ".direct.pco")
        ir_path.write_text(text, encoding="utf-8")
        object_path.write_bytes(text.encode("ascii"))
        written.append(module_index)
        rows.append("\t".join(("OK", str(module_index), name, "0", "0",
                               str(len(text)), str(ir_path), "PCO", str(object_path))))
    partial.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert not result.exists()
    os.replace(partial, result)
    atomic_text(root / "retry-written", json.dumps(written))

publish_worker_resource("complete")
atomic_text(root / ("complete." + str(index) + "." + str(attempt)),
            Path(os.environ[RESOURCE_REPORT_ENV]).read_text())
''', encoding="utf-8")

    # Match the established progressive-retry test's startup envelope. The
    # leader's grown reservation fits alone but not beside the staged peer.
    mib = 1024 ** 2
    owner = workers._coordinator_rss_bytes()
    startup_peak = max(owner, workers._worker_peak_rss_bytes())
    growth = startup_peak + 128 * mib
    available = 2 * policy.peak_reservation(startup_peak + 32 * mib) + 32 * mib
    budget = owner + policy.RSS_HEADROOM_BYTES + available
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(budget))
    tasks = [{
        "report_path": str(tmp_path / ("rss" + str(index))),
        "estimate_bytes": 8 * mib, "inputs": [2 - index],
        "class": "host:batch-retry:" + str(index), "restartable": True,
    } for index in range(2)]
    commands = [shlex.join([sys.executable, "-B", str(child), str(tmp_path),
                           str(index), str(growth)]) for index in range(2)]
    trace = tmp_path / "admission.tsv"
    observations, reaped = [], set()
    original_retire = pool._retire_resource_worker

    def record_retire(pid):
        original_retire(pid)
        reaped.add(pid)

    monkeypatch.setattr(pool, "_retire_resource_worker", record_retire)
    assert pool._HOST_WORKERS == {}
    pool.run_resource_worker_processes(commands, tasks, 2, budget,
                                      observations=observations, trace_path=str(trace))

    rows = [line.split("\t") for line in trace.read_text().splitlines()]
    cancellations = [row for row in rows if row[0] == "cancel"]
    starts = [row for row in rows if row[:2] == ["start", "1"]]
    assert len(cancellations) == 1 and cancellations[0][1] == "1"
    assert len(starts) == 2
    leader_retired = next(position for position, row in enumerate(rows)
                          if row[:2] == ["retire", "0"])
    assert leader_retired < rows.index(starts[1])
    assert int(starts[1][3]) == int(starts[1][4])  # Exclusive retry, unchanged cap.
    assert [int((tmp_path / ("attempts." + str(index))).read_text())
            for index in range(2)] == [1, 2]
    first = json.loads((tmp_path / "started.1.1").read_text())
    retry = json.loads((tmp_path / "started.1.2").read_text())
    assert first["pid"] != retry["pid"] and first["token"] != retry["token"]
    assert int(cancellations[0][2]) == first["pid"]
    for path in tmp_path.glob("started.*"):
        record = json.loads(path.read_text())
        assert record["budget"] == budget
        assert record["pid"] in reaped
        with pytest.raises(ProcessLookupError):
            os.kill(record["pid"], 0)
    assert not (tmp_path / "complete.1.1").exists()
    cancelled = policy.read_worker_resource(str(tmp_path / "cancelled-report"),
                                          first["pid"], first["token"])
    assert cancelled is not None and cancelled[0] == "module0-staged"
    witness = json.loads((tmp_path / "cancelled-first").read_text())
    assert witness["identity"] == first["identity"]
    assert witness["success_tsv"] is witness["complete"] is False
    completed = policy.read_worker_resource(str(tmp_path / "complete.1.2"),
                                          retry["pid"], retry["token"])
    assert completed is not None and completed[0] == "complete"
    assert tasks[1]["estimate_bytes"] == 8 * mib
    assert tasks[1]["incomplete_peak_bytes"] == int(cancellations[0][5]) > 0
    assert tasks[1]["retry_calibration"] is False
    assert len(observations) == 2
    assert [sample[2] for sample in observations if sample[0] == tasks[1]["class"]] == [completed[2]]
    assert json.loads((tmp_path / "retry-written").read_text()) == [0, 1]
    artifacts = tmp_path / "artifacts"
    result = (artifacts / "result.tsv").read_text()
    validate_host_batch_result(result, [0, 1], ["main", "sibling"], str(artifacts))
    assert [line.split("\t")[:3] for line in result.splitlines()] == [
        ["OK", "0", "main"], ["OK", "1", "sibling"],
    ]
    assert not (artifacts / "result.tsv.batch.partial").exists()
    for index in (0, 1):
        expected = ("NEW|" + retry["identity"] + "|module:" + str(index) + "\n").encode("ascii")
        for suffix in (".ll", ".direct.pco"):
            assert (artifacts / ("module_" + str(index) + suffix)).read_bytes() == expected
    assert os.environ["PCC_WORKER_TREE_BUDGET_BYTES"] == str(budget)
    assert pool._HOST_WORKERS == {}
