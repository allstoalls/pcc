"""Shared test-suite fixtures.

The global ``Path.resolve`` / ``os.path.dirname`` shim that used to live here
is gone (TEST-P2-REMOVE-LEGACY-PATH-SHIM): every test under ``tests/{c,python}``
now computes its own directory and the repo root explicitly, so path arithmetic
means what it says and no longer depends on a process-wide monkeypatch.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys as _gate_sys
import tempfile
import textwrap
import time as _gate_time
from pathlib import Path
import os


if os.environ.get("PCC_TEST_COMPILER"):
    # Installed before any test module binds ``compile_python``; see
    # tests/pcc1_route.py.
    from tests.pcc1_route import install as _install_pcc1_route

    _install_pcc1_route()


_SELF_HOST_WARMUP_NODEID = (
    "tests/python/test_self_host_oracle_diff.py::"
    "test_000_self_host_oracle_stage_cache_warmup"
)


# --- pcc_gate: opt-in/hardware gates are deselected, never skipped ---------
#
# Policy: a test either runs and verifies, or it is not part of the run.
# ``skip`` is reserved for genuine mid-run environmental aborts. Tests marked
# ``@pytest.mark.pcc_gate(...)`` are deselected at collection when their gate
# is unmet; when explicitly selected with an unmet gate they must fail, so the
# in-test branches behind these gates use pytest.fail, not pytest.skip.

_TSAN_PROBE_CACHE: dict[str, str | None] = {}


import pytest  # noqa: E402

from tests.xdist_cpu_budget import (  # noqa: E402, F401
    pytest_configure,
    pytest_configure_node,
    pytest_runtest_setup,
    pytest_xdist_node_collection_finished,
)
from tests.runtime_fixture_provenance import _verified_test_runtime_archive  # noqa: E402
from tests.runtime_build_cache import (  # noqa: E402
    cached_pcc_python_runtime,
    cached_threaded_pcc_python_runtime,
)
from tests.native_provisioning import require_native_provisioning_allowed  # noqa: E402


@pytest.fixture(scope="session")
def pcc_runtime_archive(tmp_path_factory):
    """Return the immutable pcc-Python archive required by pcc1 tests.

    Consumers pass this path through ``PCC_RUNTIME_ARCHIVE``.  The fixture
    never rebuilds the repository's shared ``libpy_runtime_pcc_py.a`` under
    xdist.
    """

    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE")
    if explicit:
        archive, _manifest = _verified_test_runtime_archive(explicit)
        return archive

    require_native_provisioning_allowed()
    if _gate_sys.platform.startswith("linux") or _gate_sys.platform == "win32":
        from pcc.frontends.python.owned_runtime_build import ensure_target_runtime
        from pcc.frontends.python.pipeline_targets import host_target_triple
        runtime_dir = str(Path(__file__).resolve().parents[1] / "pcc" / "runtime")
        return Path(ensure_target_runtime(runtime_dir, host_target_triple()))
    del tmp_path_factory
    return cached_pcc_python_runtime() / "libpy_runtime_pcc_py.a"


@pytest.fixture(scope="session")
def threaded_pcc_runtime_archive() -> Path:
    """Return the ``PCC_WITH_THREADS=1`` pcc-Python archive.

    ``PCC_THREADED_RUNTIME_ARCHIVE`` names a prebuilt one; otherwise one
    content-addressed build is shared by every worker.
    """

    explicit = os.environ.get("PCC_THREADED_RUNTIME_ARCHIVE")
    if explicit:
        archive, _manifest = _verified_test_runtime_archive(explicit, threads=True)
        return archive
    require_native_provisioning_allowed()
    return cached_threaded_pcc_python_runtime() / "libpy_runtime_pcc_py.a"


@pytest.fixture(scope="session")
def pcc_stackmap_runtime_archive() -> Path:
    """The pcc-Python archive built with precise stack-map tables.

    Runtime frames (the collector's among them) carry records only in this
    build. Its codegen identity includes the table policy, so it has its own
    cache entry next to ``pcc_runtime_archive``'s.
    """

    require_native_provisioning_allowed()
    previous = os.environ.get("PCC_PRECISE_STACKMAP_TABLES")
    os.environ["PCC_PRECISE_STACKMAP_TABLES"] = "1"
    try:
        return cached_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    finally:
        if previous is None:
            os.environ.pop("PCC_PRECISE_STACKMAP_TABLES", None)
        else:
            os.environ["PCC_PRECISE_STACKMAP_TABLES"] = previous


@pytest.fixture
def precise_stackmap_tables(monkeypatch):
    """Ask emitted objects for the serialized precise stack-map table.

    Objects carry it only on request (``precise_stackmap_tables_enabled``);
    tests of the table's producers, merges and consumers opt in with this.
    """
    monkeypatch.setenv("PCC_PRECISE_STACKMAP_TABLES", "1")


def _tsan_unavailable_reason() -> str | None:
    cc = os.environ.get("CC", "clang")
    if cc not in _TSAN_PROBE_CACHE:
        _TSAN_PROBE_CACHE[cc] = _probe_tsan(cc)
    return _TSAN_PROBE_CACHE[cc]


def _probe_tsan(cc: str) -> str | None:
    if shutil.which(cc) is None:
        return f"compiler {cc!r} not found"
    with tempfile.TemporaryDirectory(prefix="pcc-tsan-probe-") as tmpdir:
        probe = os.path.join(tmpdir, "tsan_probe.c")
        exe = os.path.join(tmpdir, "tsan_probe.out")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(
                textwrap.dedent(
                    r"""
                    #include <pthread.h>

                    static void *worker(void *arg) {
                        (void)arg;
                        return 0;
                    }

                    int main(void) {
                        pthread_t thread;
                        if (pthread_create(&thread, 0, worker, 0) != 0) return 1;
                        return pthread_join(thread, 0) == 0 ? 0 : 2;
                    }
                    """
                ).lstrip()
            )
        try:
            build = subprocess.run(
                [cc, "-std=c11", "-pthread", "-fsanitize=thread", probe, "-o", exe],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"ThreadSanitizer probe could not run: {exc}"
        if build.returncode != 0:
            return "ThreadSanitizer runtime is not available for this compiler"
        # macOS TSan startup crashes are ASLR-dependent and nondeterministic;
        # one clean run proves nothing. Any crash in 3 runs = unreliable
        # toolchain, and an unreliable TSan cannot certify anything.
        for _ in range(3):
            run = subprocess.run([exe], capture_output=True, text=True, timeout=30)
            if run.returncode != 0:
                return (
                    "ThreadSanitizer runtime crashes before pcc code runs "
                    f"(exit {run.returncode})"
                )
    return None


_METAL_PROBE_RESULT: list[str | None] = []


def _metal_unavailable_reason() -> str | None:
    """Real-Metal gate: device via MTLCreateSystemDefaultDevice + metal CLI."""
    if not _METAL_PROBE_RESULT:
        _METAL_PROBE_RESULT.append(_probe_metal())
    return _METAL_PROBE_RESULT[0]


def _probe_metal() -> str | None:
    if _gate_sys.platform != "darwin":
        return "real-Metal gates require Darwin"
    try:
        import ctypes
        import ctypes.util

        lib_path = ctypes.util.find_library("Metal")
        if lib_path is None:
            return "Metal framework not found"
        lib = ctypes.CDLL(lib_path)
        lib.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
        if not lib.MTLCreateSystemDefaultDevice():
            return "MTLCreateSystemDefaultDevice returned nil"
    except OSError as exc:
        return f"Metal framework probe failed: {exc}"
    finder = subprocess.run(
        ["xcrun", "--find", "metal"], capture_output=True, text=True, timeout=30
    )
    if finder.returncode != 0:
        return "metal compiler not found via xcrun"
    return None


_PCC1_PROVISIONED = False


def _provision_pcc1() -> None:
    """Ensure a fresh stage1 pcc1 exists before pcc1-consumer tests run.

    ``pcc_gate(probe="pcc1")`` never deselects: instead of "stale pcc1 ->
    skip", the session rebuilds pcc1 (content-hash cached: ~16s warm, minutes
    after a pcc/ source change). Build failure is printed loudly and the
    consumer tests then fail on their own asserts — never silently skipped.

    Auto-provisioning is not Darwin-only, but it resolves the host's owned
    self-backend target first: on a host the self backend cannot target the
    gate reports the reason instead of starting a long build.
    ``PCC_NO_AUTO_PCC1=1`` opts out (CI that stages its own binaries) and
    ``PCC_PCC1_PROVISION_TIMEOUT`` bounds the build.
    """
    global _PCC1_PROVISIONED
    if _PCC1_PROVISIONED or os.environ.get("PCC_NO_AUTO_PCC1", "").strip():
        return
    require_native_provisioning_allowed()
    _PCC1_PROVISIONED = True
    repo = Path(__file__).resolve().parent.parent
    if str(repo) not in _gate_sys.path:
        _gate_sys.path.insert(0, str(repo))
    from scripts.file_lock import exclusive_file_lock

    try:
        from tests.python.pcc1_gate import (
            _provision_timeout_seconds,
            host_stage1_support,
        )
    except Exception as exc:  # noqa: BLE001 - report and skip auto-provisioning
        _gate_sys.stderr.write(
            f"[pcc_gate] cannot resolve the stage1 host support: {exc}\n"
        )
        return
    supported, detail = host_stage1_support()
    if not supported:
        _gate_sys.stderr.write(
            "[pcc_gate] this host has no owned self-backend stage1 target "
            f"({detail}); set PCC_NO_AUTO_PCC1=1 and stage pcc1 out of band\n"
        )
        return

    lock_path = os.path.join(tempfile.gettempdir(), "pcc-pytest-pcc1-provision.lock")
    with exclusive_file_lock(lock_path) as lockfile:
        lockfile.seek(0)
        stamp = lockfile.read().decode("utf-8", "replace").strip()
        now = _gate_time.time()
        if stamp:
            try:
                if now - float(stamp) < 300:
                    return  # another worker provisioned moments ago
            except ValueError:
                pass
        _gate_sys.stderr.write(
            "[pcc_gate] ensuring fresh stage1 pcc1 "
            "(scripts/bootstrap.py --stage 1; ~16s cached, minutes cold; "
            f"timeout {_provision_timeout_seconds():.0f}s, "
            "PCC_PCC1_PROVISION_TIMEOUT overrides)\n"
        )
        env = os.environ.copy()
        env.pop("LC_ALL", None)
        proc = subprocess.run(
            [_gate_sys.executable, str(repo / "scripts" / "bootstrap.py"), "--stage", "1"],
            capture_output=True,
            text=True,
            timeout=_provision_timeout_seconds(),
            cwd=str(repo),
            env=env,
        )
        if proc.returncode != 0:
            _gate_sys.stderr.write(
                "[pcc_gate] pcc1 auto-build FAILED; pcc1-consumer tests "
                "will fail loudly:\n"
                + proc.stdout[-2000:]
                + proc.stderr[-2000:]
                + "\n"
            )
        else:
            lockfile.seek(0)
            lockfile.truncate()
            lockfile.write(str(now).encode("ascii"))
            lockfile.flush()


def _pcc_gate_blocked_reason(item) -> str | None:
    for marker in item.iter_markers("pcc_gate"):
        unavailable = marker.kwargs.get("unavailable")
        if unavailable:
            return str(unavailable)
        env = marker.kwargs.get("env")
        if env and not os.environ.get(env, "").strip():
            return f"env {env} unset"
        dep = marker.kwargs.get("dep")
        if dep and importlib.util.find_spec(dep) is None:
            return f"dependency {dep!r} not installed"
        probe = marker.kwargs.get("probe")
        if callable(probe):
            available = probe()
            if not isinstance(available, bool):
                raise TypeError("pcc_gate callable probe must return bool")
            if not available:
                return "callable probe returned False"
        elif probe == "tsan":
            reason = _tsan_unavailable_reason()
            if reason:
                return reason
        elif probe == "metal":
            reason = _metal_unavailable_reason()
            if reason:
                return reason
        elif probe == "pcc1":
            _provision_pcc1()  # provisioning step, never a deselect reason
    return None


def pytest_collection_modifyitems(config, items):
    """Deselect unmet pcc_gate items; order the self-host warmup first."""

    deselected = [
        item for item in items if _pcc_gate_blocked_reason(item) is not None
    ]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        dropped = {id(item) for item in deselected}
        items[:] = [item for item in items if id(item) not in dropped]

    warmup = [item for item in items if item.nodeid == _SELF_HOST_WARMUP_NODEID]
    if not warmup:
        return
    remaining = [item for item in items if item not in warmup]
    items[:] = warmup + remaining


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """With PCC_TEST_COMPILER, report native compiles against host fallbacks."""
    if not os.environ.get("PCC_TEST_COMPILER") or hasattr(config, "workerinput"):
        return
    from tests.pcc1_route import LOG_ENV, summarize

    summary = summarize(os.environ.get(LOG_ENV, ""))
    counts = summary["counts"]
    terminalreporter.write_sep("=", "pcc1 compile routing")
    terminalreporter.write_line(
        f"native: {counts.get('native', 0)}  host fallback: {counts.get('host', 0)}"
        f"  (log: {os.environ.get(LOG_ENV, '')})"
    )
    for reason, count in sorted(summary["host_reasons"].items(), key=lambda kv: -kv[1]):
        terminalreporter.write_line(f"  {count:6d}  {reason}")


def pytest_sessionfinish(session, exitstatus):
    """PCC_TEST_COMPILER_STRICT=1: a host fallback fails the session."""
    config = session.config
    if not os.environ.get("PCC_TEST_COMPILER") or hasattr(config, "workerinput"):
        return
    from tests.pcc1_route import LOG_ENV, STRICT_ENV, summarize

    if os.environ.get(STRICT_ENV) == "1":
        if summarize(os.environ.get(LOG_ENV, ""))["counts"].get("host", 0):
            session.exitstatus = 1
