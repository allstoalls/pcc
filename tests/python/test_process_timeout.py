"""Host-only evidence and cleanup controls for the captured-process helper."""

from __future__ import annotations

import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

import pytest

from tests.python import process_timeout


pytestmark = pytest.mark.pcc_gate(probe=lambda: os.name == "posix")

_ROOT = Path(__file__).resolve().parents[2]
_STDOUT = "child stdout: caf\u00e9\n"
_STDERR = "child stderr: \u2014 diagnostic\n"


def _handlers():
    return {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}


def _fake_process(monkeypatch, outcomes):
    class Process:
        pid = 12345
        returncode = -signal.SIGTERM

        def __init__(self):
            self.timeouts = []
            self.signals = []
            self.outcomes = iter(outcomes)

        def communicate(self, timeout=None):
            self.timeouts.append(timeout)
            outcome = next(self.outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    proc = Process()
    monkeypatch.setattr(process_timeout.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        process_timeout, "_kill_process_group", lambda proc, sig: proc.signals.append(sig)
    )
    return proc


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit(143), RuntimeError("communicate failed")],
    ids=["keyboard-interrupt", "system-exit", "ordinary-error"],
)
def test_interrupted_output_is_captured_before_reraising(monkeypatch, capsys, error):
    before = _handlers()
    proc = _fake_process(monkeypatch, [error, (_STDOUT, _STDERR)])

    with pytest.raises(type(error)) as raised:
        process_timeout.run_process_group_timeout(["owned-child"], timeout=9)

    assert raised.value is error
    assert _handlers() == before
    assert proc.timeouts == [9, 2.0]
    assert proc.signals == [signal.SIGTERM]
    captured = capsys.readouterr()
    assert captured.out == _STDOUT
    assert captured.err == _STDERR + (
        "\n[INTERRUPT] killed process group for owned-child after 9.0s\n"
    )


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize(
    "error, publication_error",
    [
        (KeyboardInterrupt(), BrokenPipeError("capture closed")),
        (SystemExit(143), KeyboardInterrupt()),
        (RuntimeError("communicate failed"), SystemExit(99)),
    ],
    ids=["keyboard-interrupt", "system-exit", "ordinary-error"],
)
def test_capture_failure_preserves_original_exception_and_other_stream(
    monkeypatch, capsys, stream_name, operation, error, publication_error
):
    class BrokenStream(io.StringIO):
        def write(self, text):
            if operation == "write":
                raise publication_error
            return super().write(text)

        def flush(self):
            if operation == "flush":
                raise publication_error
            return super().flush()

    before = _handlers()
    proc = _fake_process(monkeypatch, [error, (_STDOUT, _STDERR)])
    with monkeypatch.context() as capture_patch:
        capture_patch.setattr(sys, stream_name, BrokenStream())
        with pytest.raises(type(error)) as raised:
            process_timeout.run_process_group_timeout(["owned-child"], timeout=9)

    assert raised.value is error
    assert _handlers() == before
    assert proc.signals == [signal.SIGTERM]
    captured = capsys.readouterr()
    if stream_name == "stdout":
        assert captured.out == ""
        assert captured.err == _STDERR + (
            "\n[INTERRUPT] killed process group for owned-child after 9.0s\n"
        )
    else:
        assert captured.out == _STDOUT
        assert captured.err == ""


@pytest.mark.parametrize("returncode", [0, 7], ids=["success", "failure"])
def test_normal_return_does_not_publish_captured_output(capsys, returncode):
    before = _handlers()
    result = process_timeout.run_process_group_timeout(
        [
            sys.executable, "-c",
            f"import sys; print({_STDOUT!r}, end=''); "
            f"print({_STDERR!r}, end='', file=sys.stderr); sys.exit({returncode})",
        ],
        timeout=10,
    )

    assert result.returncode == returncode
    assert result.stdout == _STDOUT
    assert result.stderr == _STDERR
    assert _handlers() == before
    assert capsys.readouterr() == ("", "")


def test_timeout_return_does_not_publish_captured_output(monkeypatch, capsys):
    before = _handlers()
    proc = _fake_process(
        monkeypatch, [subprocess.TimeoutExpired(["owned-child"], 9), (_STDOUT, _STDERR)]
    )
    result = process_timeout.run_process_group_timeout(["owned-child"], timeout=9)

    assert result.returncode == 124
    assert result.stdout == _STDOUT
    assert result.stderr == _STDERR + (
        "\n[TIMEOUT] killed process group for owned-child after 9.0s\n"
    )
    assert proc.timeouts == [9, 2.0]
    assert proc.signals == [signal.SIGTERM]
    assert _handlers() == before
    assert capsys.readouterr() == ("", "")


def test_interruption_keeps_evidence_after_sigkill_escalation(monkeypatch, capsys):
    error = KeyboardInterrupt()
    before = _handlers()
    proc = _fake_process(
        monkeypatch,
        [error, subprocess.TimeoutExpired(["owned-child"], 2), (_STDOUT, _STDERR)],
    )
    with pytest.raises(KeyboardInterrupt) as raised:
        process_timeout.run_process_group_timeout(["owned-child"], timeout=9)

    assert raised.value is error
    assert proc.timeouts == [9, 2.0, None]
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
    assert _handlers() == before
    captured = capsys.readouterr()
    assert captured.out == _STDOUT
    assert captured.err == _STDERR + (
        "\n[INTERRUPT] killed process group for owned-child after 9.0s\n"
    )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_signal_preserves_output_exit_and_descendant_cleanup(tmp_path, signum):
    # The signal targets only this test's runner. All processes are tiny Python
    # children with finite lifetimes; no compiler or runtime is invoked.
    ready = tmp_path / "descendant.ready"
    descendant = textwrap.dedent(f"""
        import signal
        import time
        from pathlib import Path

        def terminate(signum, frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("DESCENDANT_TERMINATED", flush=True)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        Path({str(ready)!r}).touch()
        time.sleep(8)
        raise SystemExit(73)
    """)
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(f"""
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        descendant = subprocess.Popen([sys.executable, "-c", {descendant!r}])

        def terminate(signum, frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            status = descendant.wait(timeout=2)
            print("DESCENDANT_REAPED:" + str(status), flush=True)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        deadline = time.monotonic() + 5
        while not Path({str(ready)!r}).exists():
            if time.monotonic() >= deadline:
                descendant.kill()
                descendant.wait()
                raise SystemExit(74)
            time.sleep(0.01)
        print({_STDOUT!r}, end="", flush=True)
        print({_STDERR!r}, end="", file=sys.stderr, flush=True)
        os.kill(os.getppid(), {int(signum)})
        time.sleep(8)
    """), encoding="utf-8")
    runner = textwrap.dedent(f"""
        import signal
        import sys
        sys.path.insert(0, {str(_ROOT)!r})
        from tests.python.process_timeout import run_process_group_timeout

        def previous_handler(signum, frame):
            raise AssertionError("previous handler called inside helper")

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, previous_handler)
        try:
            run_process_group_timeout([sys.executable, {str(child)!r}], timeout=10)
        finally:
            assert signal.getsignal(signal.SIGINT) is previous_handler
            assert signal.getsignal(signal.SIGTERM) is previous_handler
            print("HANDLERS_RESTORED", file=sys.stderr, flush=True)
        raise AssertionError("interruption returned normally")
    """)
    result = subprocess.run(
        [sys.executable, "-c", runner],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )

    if signum == signal.SIGINT:
        assert result.returncode in (-signal.SIGINT, 128 + signal.SIGINT), result.stderr
        assert "KeyboardInterrupt" in result.stderr
    else:
        assert result.returncode == 128 + signal.SIGTERM, result.stderr
    assert result.stdout.count(_STDOUT) == 1
    assert result.stdout.count("DESCENDANT_TERMINATED\n") == 1
    assert result.stdout.count("DESCENDANT_REAPED:0\n") == 1
    assert result.stderr.count(_STDERR) == 1
    assert result.stderr.count("[INTERRUPT] killed process group") == 1
    assert result.stderr.count("HANDLERS_RESTORED\n") == 1


def test_stderr_path_is_on_disk_while_the_child_runs(tmp_path):
    """An outer deadline can kill the caller mid-run; the file must already
    hold what the child printed, not only what a finished run returns."""
    import threading
    import time

    sink = tmp_path / "child.stderr"
    release = tmp_path / "release"
    script = textwrap.dedent(f"""
        import os, sys, time
        sys.stderr.write("started\\n")
        sys.stderr.flush()
        deadline = time.monotonic() + 30
        while not os.path.exists({str(release)!r}) and time.monotonic() < deadline:
            time.sleep(0.05)
    """)
    results = []
    worker = threading.Thread(target=lambda: results.append(
        process_timeout.run_process_group_timeout(
            [sys.executable, "-c", script], timeout=60, stderr_path=sink,
        )
    ))
    worker.start()
    seen = ""
    deadline = time.monotonic() + 30
    while not seen and time.monotonic() < deadline:
        seen = sink.read_text(encoding="utf-8") if sink.exists() else ""
        time.sleep(0.05)
    release.write_text("go", encoding="utf-8")
    worker.join(60)
    assert seen == "started\n"
    assert len(results) == 1 and results[0].returncode == 0
    assert results[0].stderr == "started\n"


def test_stderr_path_keeps_partial_output_and_the_timeout_note(tmp_path):
    sink = tmp_path / "child.stderr"
    script = (
        "import sys, time; sys.stdout.write('out'); sys.stdout.flush(); "
        "sys.stderr.write('partial \\u2014 line\\n'); sys.stderr.flush(); time.sleep(30)"
    )
    result = process_timeout.run_process_group_timeout(
        [sys.executable, "-c", script], timeout=1.0, stderr_path=sink,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    text = sink.read_text(encoding="utf-8")
    assert result.returncode == 124
    assert result.stdout == "out"
    assert text.startswith("partial — line\n")
    assert "[TIMEOUT] killed process group" in text
    assert result.stderr == text
