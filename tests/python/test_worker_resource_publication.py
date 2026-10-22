"""Resource snapshots remain atomic across Windows reader sharing conflicts."""

import errno
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_resource_plan as policy


def windows_denial(code):
    error = PermissionError(errno.EACCES, "Windows file sharing conflict")
    error.winerror = code
    return error


@pytest.fixture
def report(tmp_path, monkeypatch):
    path = tmp_path / "worker.rss"
    monkeypatch.setenv(policy.RESOURCE_REPORT_ENV, str(path))
    monkeypatch.setenv(policy.RESOURCE_TOKEN_ENV, "attempt-token")
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 100)
    monkeypatch.setattr(workers, "_worker_peak_rss_bytes", lambda: 120)
    policy.publish_worker_resource("allocated")
    return path


def test_publication_waits_for_open_reader_without_partial_report(report, monkeypatch):
    """Use real Windows sharing rules there; emulate only the OS denial elsewhere."""
    original_replace = policy._replace_worker_resource
    reader_open = threading.Event()
    first_denial = threading.Event()
    errors = []
    calls = []

    def replace(source, destination):
        calls.append((source, destination))
        try:
            if os.name != "nt" and reader_open.is_set():
                raise windows_denial(5)
            return original_replace(source, destination)
        except PermissionError:
            first_denial.set()
            raise

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    before = report.read_bytes()

    def publish():
        try:
            policy.publish_worker_resource("complete")
        except BaseException as error:
            errors.append(error)

    writer = threading.Thread(target=publish)
    try:
        with open(report, "r", encoding="utf-8") as reader:
            reader_open.set()
            writer.start()
            assert first_denial.wait(2), "replacement did not encounter the held reader"
            assert reader.read().splitlines()[2] == "allocated"
            assert report.read_bytes() == before
            assert policy.read_worker_resource(str(report), os.getpid(), "attempt-token") == (
                "allocated", 100, 120,
            )
        reader_open.clear()
    finally:
        reader_open.clear()
        writer.join(3)
    assert not writer.is_alive()
    assert errors == []
    assert len(calls) >= 2
    assert policy.read_worker_resource(str(report), os.getpid(), "attempt-token") == (
        "complete", 100, 120,
    )
    assert policy.read_worker_resource(str(report), os.getpid(), "other-token") is None
    assert policy.read_worker_resource(str(report), os.getpid() + 1, "attempt-token") is None
    assert not Path(str(report) + ".tmp").exists()


def test_production_reader_allows_first_replace_and_retains_old_snapshot(report, monkeypatch):
    """Real production opener: Win32 delete sharing is exercised on Windows."""
    before = report.read_text(encoding="utf-8")
    original_replace = policy._replace_worker_resource
    calls = []

    def replace(source, destination):
        calls.append((source, destination))
        return original_replace(source, destination)

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    monkeypatch.setattr(policy, "time", SimpleNamespace(
        monotonic=lambda: 0,
        sleep=lambda _seconds: pytest.fail("production reader must not require a retry"),
    ))
    with policy._open_worker_resource(str(report)) as old_reader:
        assert not os.get_inheritable(old_reader.fileno())
        # Do not pre-read: this must remain the old underlying file snapshot,
        # rather than bytes buffered before replacement.
        policy.publish_worker_resource("complete")
        assert calls == [(str(report) + ".tmp", str(report))]
        assert old_reader.read() == before
        with policy._open_worker_resource(str(report)) as new_reader:
            rows = new_reader.read().splitlines()
        assert rows == [policy.RESOURCE_REPORT_SCHEMA, str(os.getpid()), "complete",
                        "100", "120", "attempt-token"]
        assert policy.read_worker_resource(str(report), os.getpid(), "attempt-token") == (
            "complete", 100, 120,
        )
        old_reader.seek(0)
        assert old_reader.read() == before
    assert old_reader.closed and new_reader.closed
    assert not Path(str(report) + ".tmp").exists()


@pytest.mark.parametrize("code", [5, 32])
def test_persistent_windows_denial_is_bounded_and_preserves_old_report(report, monkeypatch, code):
    before = report.read_bytes()
    clock = [0.0]
    attempts = []
    sleeps = []
    failure = windows_denial(code)

    def replace(*_args):
        attempts.append(clock[0])
        raise failure

    def sleep(seconds):
        assert seconds > 0
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    with pytest.raises(PermissionError) as caught:
        policy.publish_worker_resource("complete")
    assert caught.value is failure
    assert 1 < len(attempts) <= 102
    assert 0 < clock[0] <= 1.01
    assert sum(sleeps) <= 1.01
    assert report.read_bytes() == before
    assert not Path(str(report) + ".tmp").exists()


@pytest.mark.parametrize("failure", [
    PermissionError(errno.EACCES, "POSIX access denied"),
    windows_denial(87),
    FileNotFoundError(errno.ENOENT, "missing directory"),
    OSError(errno.ENOSPC, "full filesystem"),
])
def test_unrelated_replace_errors_are_not_retried(report, monkeypatch, failure):
    before = report.read_bytes()
    calls = []

    def replace(*_args):
        calls.append(1)
        raise failure

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    monkeypatch.setattr(policy, "time", SimpleNamespace(
        monotonic=lambda: 0,
        sleep=lambda _seconds: pytest.fail("unrelated error must not retry"),
    ))
    with pytest.raises(type(failure)) as caught:
        policy.publish_worker_resource("complete")
    assert caught.value is failure
    assert calls == [1]
    assert report.read_bytes() == before
    assert not Path(str(report) + ".tmp").exists()


def test_interrupted_retry_preserves_previous_report_and_cleans_temporary(report, monkeypatch):
    before = report.read_bytes()

    def replace(*_args):
        raise windows_denial(32)

    def interrupt(_seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: 0, sleep=interrupt))
    with pytest.raises(KeyboardInterrupt):
        policy.publish_worker_resource("complete")
    assert report.read_bytes() == before
    assert not Path(str(report) + ".tmp").exists()


def test_cleanup_failure_does_not_hide_publication_error(report, monkeypatch):
    before = report.read_bytes()
    failure = PermissionError(errno.EACCES, "publication denied")

    def replace(*_args):
        raise failure

    def unlink(_path):
        raise PermissionError(errno.EACCES, "cleanup denied")

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    monkeypatch.setattr(policy.os, "unlink", unlink)
    with pytest.raises(PermissionError) as caught:
        policy.publish_worker_resource("complete")
    assert caught.value is failure
    assert report.read_bytes() == before


def test_windows_ci_checks_real_reader_contention_independently_of_bootstrap():
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/pcc1-package-parity.yml"
    text = workflow.read_text(encoding="utf-8")
    publication = text.split("  windows-rss-publication:\n", 1)[1].split("  other-platform-native-wheel:\n", 1)[0]
    bootstrap = text.split("  other-platform-native-wheel:\n", 1)[1].split("  pcc1-package-parity:\n", 1)[0]
    assert "runs-on: windows-2022" in publication
    assert "timeout-minutes: 10" in publication and "timeout-minutes: 2" in publication
    assert 'python -m pytest --noconftest -o "addopts=" -x -n6 --dist=loadgroup -vv --tb=short' in publication
    assert "tests/python/test_worker_resource_publication.py" in publication
    assert "tests/python/test_worker_resource_reader.py" in publication
    assert "tests/python/test_worker_resource_publisher.py" in publication
    assert "tests/python/test_worker_resource_native_windows.py" in publication
    assert "tests/python/test_worker_resource_windows_filesystem.py" in publication
    assert "--phase stage1" not in publication
    assert "Verify Windows RSS publication" not in bootstrap
    assert "--phase stage1" in bootstrap
    assert "needs:" not in bootstrap + publication
    assert text.count('      - "tests/python/test_worker_resource_publication.py"') == 2
    assert text.count('      - "tests/python/test_worker_resource_publisher.py"') == 2


@pytest.mark.parametrize("platform_name,retried", (("win32", True), ("linux", False)))
def test_errno_only_denial_retries_only_on_windows(report, monkeypatch, platform_name, retried):
    """The pcc runtime reports a Windows sharing violation as EACCES, no winerror.

    Only that runtime lacks winerror; under CPython an errno-only
    PermissionError stays unrelated (test_unrelated_replace_errors_are_not_retried).
    """
    import sys

    attempts = []

    def replace(source, destination):
        attempts.append(source)
        if len(attempts) == 1:
            raise PermissionError(errno.EACCES, "Permission denied")
        os.replace(source, destination)

    monkeypatch.setattr(policy, "_replace_worker_resource", replace)
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(sys, "implementation", SimpleNamespace(name="pcc"))
    if retried:
        policy.publish_worker_resource("complete")
        assert len(attempts) == 2
        assert report.read_text().splitlines()[2] == "complete"
    else:
        with pytest.raises(PermissionError):
            policy.publish_worker_resource("complete")
        assert len(attempts) == 1
        assert report.read_text().splitlines()[2] == "allocated"
