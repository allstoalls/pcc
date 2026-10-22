"""Libc import ratchet: pcc1 must not grow new libc/libSystem imports.

The LIBC track replaces pcc's own libc closure with pcc-Python
implementations (translation references pinned in the baseline JSON:
musl primary, llvm-libc for the overlay model and math, apple-libc for
Darwin semantics). This gate is the ratchet: the stage1 binary's undefined
symbols must stay a subset of the recorded baseline. Shrinking is progress
and is recorded by deliberately regenerating the baseline with the smaller
set; growth fails here and must be argued, not slipped in.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pcc1_gate import find_current_pcc1, repo_root

REPO = repo_root()
BASELINE = REPO / "tests" / "libc_import_baseline.json"

_PLATFORM_REASON = (
    None
    if sys.platform == "darwin"
    else "baseline is recorded for darwin-arm64 mach-o imports"
)


@pytest.mark.pcc_gate(unavailable=_PLATFORM_REASON)
@pytest.mark.pcc_gate(probe="pcc1")
def test_pcc1_libc_imports_stay_within_baseline():
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        pytest.fail(
            "no fresh pcc1 even after auto-provisioning; "
            "run scripts/bootstrap.py --stage 1 and read its output"
        )
    out = subprocess.run(
        ["nm", "-u", str(pcc1)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    imports = {
        line.strip()[1:] if line.strip().startswith("_") else line.strip()
        for line in out.stdout.splitlines()
        if line.strip()
    }
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    allowed = set(baseline["symbols"])
    new = sorted(imports - allowed)
    assert not new, (
        "pcc1 grew NEW libc imports not present in the ratchet baseline "
        f"({BASELINE.name}):\n  " + "\n  ".join(new) + "\n"
        "Either implement them in pcc-Python (LIBC track) or argue the "
        "addition and regenerate the baseline deliberately."
    )
    if len(imports) < len(allowed):
        sys.stderr.write(
            f"[libc-ratchet] imports shrank: {len(imports)} < baseline "
            f"{len(allowed)} — tighten {BASELINE.name}\n"
        )


BASELINE_THREADS = REPO / "tests" / "libc_import_baseline_threads.json"
# Outside the build/bootstrap-* glob so find_current_pcc1 never
# selects the threads binary for the threads-off ratchet.
_THREADS_PCC1 = REPO / "build" / "libc-ratchet-threads" / "pcc1"


@pytest.mark.integration
@pytest.mark.pcc_gate(unavailable=_PLATFORM_REASON)
def test_threads_pcc1_libc_imports_stay_within_baseline():
    """PCC_WITH_THREADS=1 variant of the ratchet (LIBC-P1-IMPORT-RATCHET).

    Builds (or reuses, when fresher than every pcc source) a threads-on
    stage1 pcc1 and holds its undefined symbols to the recorded baseline:
    the threads build may only add the six pthread condvar/mutex symbols
    over the threads-off set.
    """
    from pcc1_gate import pcc1_freshness_cutoff

    if (
        not _THREADS_PCC1.is_file()
        or _THREADS_PCC1.stat().st_mtime < pcc1_freshness_cutoff(REPO)
    ):
        import os
        import shutil
        import tempfile

        # Isolate the threads build behind a private runtime-dir copy:
        # PCC_WITH_THREADS=1 rebuilds runtime archives, and doing that in
        # the shared pcc/runtime tree taints every later threads-off
        # binary (this bit the threads-off ratchet on 2026-08-01).
        with tempfile.TemporaryDirectory(prefix="pcc-threads-runtime-") as tmp:
            runtime_copy = Path(tmp) / "py_runtime"
            source_runtime = REPO / "pcc" / "runtime"
            runtime_copy.mkdir()
            # vendor/ carries the musl sources pcc compiles in place of
            # libSystem imports; omitting it silently re-imports them.
            for entry in ("src", "py", "include", "Makefile", "vendor"):
                source_entry = source_runtime / entry
                if source_entry.is_dir():
                    shutil.copytree(source_entry, runtime_copy / entry)
                elif source_entry.is_file():
                    shutil.copy2(source_entry, runtime_copy / entry)
            build = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "bootstrap.py"), "--stage", "1"],
                capture_output=True,
                text=True,
                timeout=880,
                cwd=str(REPO),
                env={
                    **{k: v for k, v in os.environ.items() if k != "LC_ALL"},
                    "PCC_WITH_THREADS": "1",
                    "PCC_RUNTIME_DIR": str(runtime_copy),
                    "PCC_BOOTSTRAP_OUT_DIR": str(_THREADS_PCC1.parent),
                },
            )
            assert build.returncode == 0, (
                build.stdout[-1500:] + build.stderr[-1500:]
            )
    out = subprocess.run(
        ["nm", "-u", str(_THREADS_PCC1)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    imports = {
        line.strip()[1:] if line.strip().startswith("_") else line.strip()
        for line in out.stdout.splitlines()
        if line.strip()
    }
    baseline = json.loads(BASELINE_THREADS.read_text(encoding="utf-8"))
    allowed = set(baseline["symbols"])
    new = sorted(imports - allowed)
    assert not new, (
        "threads-on pcc1 grew NEW libc imports beyond "
        f"{BASELINE_THREADS.name}:\n  " + "\n  ".join(new)
    )
    threads_off = json.loads(BASELINE.read_text(encoding="utf-8"))
    pthread_delta = sorted(allowed - set(threads_off["symbols"]))
    assert all(s.startswith("pthread_") for s in pthread_delta), pthread_delta


BASELINE_LINUX = REPO / "tests" / "libc_import_baseline_linux.json"
_LINUX_HARNESS = REPO / "scripts" / "run_self_backend_linux_x86_64_docker.py"
_LINUX_PCC1 = REPO / "build" / "libc-ratchet-linux" / "pcc1"


def _docker_available() -> bool:
    import shutil as _shutil

    docker = _shutil.which("docker")
    if docker is None or not _LINUX_HARNESS.is_file():
        return False
    probe = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return probe.returncode == 0


@pytest.mark.integration
def test_linux_pcc1_libc_imports_stay_within_baseline():
    """Linux twin of the darwin ratchet (LIBC-P1-IMPORT-RATCHET).

    Reuses the docker harness the self-backend linux suites already use. The
    image must carry clang >= 15: bookworm's clang-14 rejects pcc's emitted
    IR (opaque `ptr`) with "expected type", which is what made the first
    three attempts at this baseline fail.
    """
    import os as _os

    if not _LINUX_PCC1.is_file():
        if not _docker_available():
            pytest.fail(
                "linux ratchet needs either a prebuilt build/libc-ratchet-linux/pcc1 "
                "or a reachable docker daemon; see the row's evidence for the build command"
            )
        build = subprocess.run(
            [
                sys.executable,
                str(_LINUX_HARNESS),
                "bash",
                "-c",
                "set -e; RT=/workspace/build/linux_rt; rm -rf $RT; mkdir -p $RT; "
                "cp -r /workspace/pcc/runtime/src /workspace/pcc/runtime/py "
                "/workspace/pcc/runtime/include /workspace/pcc/runtime/Makefile $RT/; "
                "cp -r /workspace/pcc/runtime/vendor $RT/ 2>/dev/null || true; "
                "cd $RT && make PYTHON=python3 'PCC=python3 -m pcc' "
                "PCC_REPO_ROOT=/workspace libpy_runtime_pcc_py.a >/dev/null 2>&1; "
                "cd /workspace; "
                "PCC_RUNTIME_ARCHIVE=$RT/libpy_runtime_pcc_py.a PCC_RUNTIME_DIR=$RT "
                "PCC_BOOTSTRAP_OUT_DIR=/workspace/build/libc-ratchet-linux "
                "python3 scripts/bootstrap.py --backend llvm --stage 1",
            ],
            capture_output=True,
            text=True,
            timeout=2400,
            cwd=str(REPO),
            env={k: v for k, v in _os.environ.items() if k != "LC_ALL"},
        )
        assert build.returncode == 0, build.stdout[-2000:] + build.stderr[-2000:]
        assert _LINUX_PCC1.is_file(), "linux stage1 reported success but produced no pcc1"

    out = subprocess.run(
        [
            "bash",
            str(_LINUX_HARNESS),
            "bash",
            "-c",
            "nm -u --format=posix /workspace/build/libc-ratchet-linux/pcc1 "
            "| awk '{print $1}' | sed 's/@.*//' | sort -u",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO),
    )
    assert out.returncode == 0, out.stderr
    imports = {
        line.strip()
        for line in out.stdout.splitlines()
        if line.strip() and not line.startswith("nm:")
    }
    assert imports, "nm returned no undefined symbols for the linux pcc1"

    baseline = json.loads(BASELINE_LINUX.read_text(encoding="utf-8"))
    allowed = set(baseline["symbols"])
    new = sorted(imports - allowed)
    assert not new, (
        "linux pcc1 grew NEW libc imports beyond "
        f"{BASELINE_LINUX.name}:\n  " + "\n  ".join(new)
    )
    if len(imports) < len(allowed):
        sys.stderr.write(
            f"[libc-ratchet-linux] imports shrank: {len(imports)} < baseline "
            f"{len(allowed)} — tighten {BASELINE_LINUX.name}\n"
        )


def test_linux_baseline_is_platform_labeled_and_distinct_from_darwin():
    """The two baselines must not be confused for each other."""
    linux = json.loads(BASELINE_LINUX.read_text(encoding="utf-8"))
    darwin = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert linux["platform"] == "linux-x86_64"
    assert darwin["platform"] == "darwin-arm64"
    linux_syms, darwin_syms = set(linux["symbols"]), set(darwin["symbols"])
    # Platform machinery that cannot appear on the other side.
    assert {"__libc_start_main", "__errno_location"} <= linux_syms
    darwin_only = {"__chkstk_darwin", "_tlv_bootstrap"}
    assert darwin_only <= darwin_syms
    assert not (darwin_only & linux_syms)
    assert not ({"__libc_start_main", "__errno_location"} & darwin_syms)
