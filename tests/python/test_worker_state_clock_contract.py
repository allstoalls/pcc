"""Execute clock selection and fail-closed worker-state freshness contracts.

The Darwin ABI double is deterministic, even on a Linux host that has never
suspended. It is not evidence of execution on an actual Darwin kernel. The
separate host-clock probe compares real emitted clock calls with CPython.
"""

from pathlib import Path
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = ROOT / "pcc/runtime/py/freestanding_platform_time.py"
NS = 1_000_000_000


def _write_state(path, sampled_at, *, budget=1000, owner=11):
    # All rows are synthetic. Derive distinct IDs even when the owner is
    # itself a small PID such as 10, 12, or 13 in an isolated process namespace.
    wrapper, worker, descendant = owner + 3, owner + 1, owner + 2
    path.write_text(
        policy.TREE_STATE_SCHEMA + "\n" + str(sampled_at) + "\n" + str(budget)
        + "\n" + str(wrapper) + "\t0\t50\n" + str(owner) + "\t" + str(wrapper) + "\t100\n"
        + str(worker) + "\t" + str(owner) + "\t200\n"
        + str(descendant) + "\t" + str(worker) + "\t30\n",
        encoding="utf-8",
    )


def _build_clock_probe(directory, *, mock_darwin):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin
    from pcc.frontends.python.pipeline_targets import host_target_triple

    directory.mkdir()
    llvm_ir = directory / "platform_time.ll"
    with pytest.MonkeyPatch.context() as patch:
        if mock_darwin:
            # Change only clock ABI selection, not the executable's host ABI.
            patch.setattr(UnsafeIntrinsicMixin, "_target_sys_platform_text",
                          lambda self: "darwin")
        pipeline.compile_python(
            str(PLATFORM_SOURCE), str(llvm_ir), emit_llvm_only=True,
            libpython_mode="off", python_library=True,
        )
    obj = directory / "platform_time.o"
    obj.write_bytes(emit_owned_object(llvm_ir.read_text(), host_target_triple()))
    harness = directory / "clock_probe.c"
    harness.write_text(
        r"""
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
int64_t pcc_platform_monotonic_us(void);
int64_t pcc_platform_monotonic_ns(void);
int64_t pcc_platform_wall_time_ns(void);
""" + (r"""
static int64_t awake_ns, suspended_ns, wall_ns;
int clock_gettime(clockid_t clock, struct timespec *value) {
    int64_t ns;
    if (clock == 0) ns = wall_ns;
    else if (clock == 8) ns = awake_ns;
    else if (clock == 6) ns = awake_ns + suspended_ns;
    else return -1;
    value->tv_sec = ns / 1000000000;
    value->tv_nsec = ns % 1000000000;
    return 0;
}
""" if mock_darwin else "") + r"""
int main(int argc, char **argv) {
""" + (r"""
    if (argc != 4) return 20;
    awake_ns = strtoll(argv[1], 0, 10);
    suspended_ns = strtoll(argv[2], 0, 10);
    wall_ns = strtoll(argv[3], 0, 10);
""" if mock_darwin else "") + r"""
    int64_t micros = pcc_platform_monotonic_us();
    int64_t nanos = pcc_platform_monotonic_ns();
    int64_t wall = pcc_platform_wall_time_ns();
    if (micros <= 0 || nanos <= 0 || wall <= 0) return 21;
    printf("%lld %lld %lld\n", (long long)micros, (long long)nanos,
           (long long)wall);
    return 0;
}
""", encoding="utf-8")
    compiler = shutil.which("cc") or shutil.which("clang")
    assert compiler, "a C harness compiler is required for the clock ABI oracle"
    executable = directory / "clock_probe"
    # PCC owns platform object emission; C is only the explicit test ABI
    # double/entrypoint and system linker, not a production runtime fallback.
    build = subprocess.run(
        [compiler, str(harness), str(obj), "-o", str(executable)],
        capture_output=True, text=True, timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return executable


@pytest.fixture(scope="module")
def clock_probes(tmp_path_factory):
    directory = tmp_path_factory.mktemp("worker_state_clocks")
    return (
        _build_clock_probe(directory / "host", mock_darwin=False),
        _build_clock_probe(directory / "darwin_abi_double", mock_darwin=True),
    )


def _read_probe(executable, *args):
    result = subprocess.run([str(executable), *map(str, args)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    return tuple(int(value) for value in result.stdout.split())


@pytest.mark.pcc_gate(probe=lambda: os.name == "posix")
def test_emitted_host_clock_shares_cpython_epoch_across_processes(clock_probes):
    before = time.monotonic_ns()
    wall_before = time.time_ns()
    micros, nanos, wall = _read_probe(clock_probes[0])
    wall_after = time.time_ns()
    after = time.monotonic_ns()
    # The microsecond API truncates at most 999ns. Neither an elapsed-only
    # assertion nor a same-process mock would catch a different clock epoch.
    assert before - 999 <= micros * 1000 <= after
    assert before <= nanos <= after
    assert wall_before <= wall <= wall_after


@pytest.mark.pcc_gate(probe=lambda: os.name == "posix")
@pytest.mark.parametrize("suspended_seconds", [0, 17924, 86400])
def test_darwin_clock_abi_matches_host_state_after_system_suspend(
    clock_probes, tmp_path, monkeypatch, suspended_seconds,
):
    state = tmp_path / "tree.tsv"
    _write_state(state, 100.0)
    awake = 100 * NS + 125_000_000
    # Deliberately unrelated wall time cannot substitute for the protocol.
    micros, nanos, wall = _read_probe(clock_probes[1], awake,
                                     suspended_seconds * NS, 123456789 * NS)
    assert nanos == awake and micros == awake // 1000
    assert wall == 123456789 * NS
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: nanos / NS))
    assert policy.read_tree_state(str(state), 1000, 11, [12], 100.0) == (50, {12: 230})
    # A long suspension does not spend Darwin awake-time TTL, but ordinary
    # awake time must still expire this unchanged snapshot.
    _, expired, _ = _read_probe(clock_probes[1], 102 * NS + 1_000_000,
                                suspended_seconds * NS, 1 * NS)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: expired / NS))
    assert policy.read_tree_state(str(state), 1000, 11, [12]) is None


def test_windows_clock_lowering_preserves_logical_kinds(tmp_path, monkeypatch):
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(UnsafeIntrinsicMixin, "_target_sys_platform_text",
                        lambda self: "win32")
    output = tmp_path / "windows_platform_time.ll"
    pipeline.compile_python(
        str(PLATFORM_SOURCE), str(output), emit_llvm_only=True,
        libpython_mode="off", python_library=True,
    )
    ir_text = output.read_text()
    calls = [line for line in ir_text.splitlines()
             if "call " in line and "@pcc_win_clock_gettime(" in line]
    assert len(calls) == 4
    assert sum("(i64 0," in line for line in calls) == 2
    assert sum("(i64 1," in line for line in calls) == 2


@pytest.mark.parametrize("sampled_at,now,not_before,usable", [
    (100.0, 100.0, 100.0, True),
    (100.0, 102.0, 100.0, True),
    (100.0, 102.000001, 100.0, False),
    (100.000001, 100.0, 0.0, False),
    (100.0, 100.1, 100.000001, False),
    # Reproduce incompatible clock domains in either direction. They remain
    # unavailable, never inferred as zero RSS or repaired with wall time.
    (100.0, 18024.0, 0.0, False),
    (18024.0, 100.0, 0.0, False),
    ("nan", 100.0, 0.0, False),
    ("inf", 100.0, 0.0, False),
    ("-inf", 100.0, 0.0, False),
    ("not-a-clock", 100.0, 0.0, False),
])
def test_tree_clock_bounds_are_fail_closed(
    tmp_path, monkeypatch, sampled_at, now, not_before, usable,
):
    state = tmp_path / "tree.tsv"
    _write_state(state, sampled_at)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: now))
    expected = (50, {12: 230}) if usable else None
    assert policy.read_tree_state(str(state), 1000, 11, [12], not_before) == expected


@pytest.mark.parametrize("bad_state", [
    "", "pcc.worker-tree-rss.v1\n100\n1000\n",  # Missing measured owner.
    "other-schema\n100\n1000\n11\t10\t100\n",
    "pcc.worker-tree-rss.v1\n100\n999\n11\t10\t100\n",
    "pcc.worker-tree-rss.v1\n100\nNaN\n11\t10\t100\n",
    "pcc.worker-tree-rss.v1\n100\n1000\n11\t10\t0\n",
    "pcc.worker-tree-rss.v1\n100\n1000\n11\t10\t-1\n",
    "pcc.worker-tree-rss.v1\n100\n1000\n11\t10\tNaN\n",
    "pcc.worker-tree-rss.v1\n100\n1000\n11\t10\t100\n11\t10\t100\n",
    "pcc.worker-tree-rss.v1\n100\n1000\n11\t10\t100\tignored\n",
])
def test_malformed_tree_state_never_supplies_zero_rss(tmp_path, monkeypatch, bad_state):
    state = tmp_path / "tree.tsv"
    state.write_text(bad_state, encoding="utf-8")
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: 100.5))
    assert policy.read_tree_state(str(state), 1000, 11, [12]) is None


@pytest.mark.parametrize("owner_pid", [10, 12345])
@pytest.mark.parametrize("publication_delay", [0.25, 1.75, 2.25, 4.0])
def test_sampler_retry_delay_does_not_relax_admission_freshness(
    tmp_path, monkeypatch, publication_delay, owner_pid,
):
    # Deterministic scheduling model: the coordinator starts just after the
    # previous snapshot. The next fresh snapshot is delayed by ps. This pins
    # down the existing separate initial 2s wait, including the 1+3s retry
    # risk, without running ps or silently inflating the snapshot TTL.
    class Clock:
        value = 100.0

        def monotonic(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds
            if self.value >= 100.0 + publication_delay:
                _write_state(state, self.value, budget=budget, owner=owner_pid)

    class Admitted(Exception):
        pass

    clock = Clock()
    state = tmp_path / "tree.tsv"
    budget = 512 * 1024 * 1024
    _write_state(state, 99.99, budget=budget, owner=owner_pid)
    monkeypatch.setenv(policy.TREE_STATE_ENV, str(state))
    # Make both the reported failure and ordinary-PID control deterministic;
    # do not alter the real pytest PID or the process-wide os module.
    monkeypatch.setattr(pool, "os", SimpleNamespace(
        getpid=lambda: owner_pid, environ=os.environ, path=os.path, unlink=os.unlink,
    ))
    monkeypatch.setattr(policy, "time", clock)
    monkeypatch.setattr(pool, "time", clock)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 32 * 1024 * 1024)
    assert policy.read_tree_state(str(state), budget, owner_pid, [owner_pid + 1]) == (
        50, {owner_pid + 1: 230},
    )

    def start(_specs, _index):
        assert clock.value >= 100.0 + publication_delay
        raise Admitted()

    monkeypatch.setattr(pool, "_start_resource_worker", start)
    item = {"report_path": str(tmp_path / "rss"), "estimate_bytes": 0,
            "inputs": [1], "class": "host:test", "restartable": True}
    if publication_delay < policy.STATE_MAX_AGE_SECONDS:
        with pytest.raises(Admitted):
            pool.run_resource_worker_processes(["never-spawn"], [item], 1, budget, [])
    else:
        with pytest.raises(policy.WorkerMemoryError, match="missing, stale, or incompatible"):
            pool.run_resource_worker_processes(["never-spawn"], [item], 1, budget, [])
        assert 102.0 <= clock.value < 102.02
