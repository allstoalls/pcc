from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def _kill_process_group(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # A signal handler can reap the group leader before the outer cleanup
        # path repeats killpg().  Treat EPERM as benign only after the owned
        # parent is observably gone; a live process must still surface it.
        if proc.poll() is None:
            raise


def _finish_after_process_group_kill(
    proc: subprocess.Popen[str],
    *,
    cmd: Sequence[str],
    timeout: float,
    reason: str,
) -> subprocess.CompletedProcess[str]:
    _kill_process_group(proc, signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc, signal.SIGKILL)
        stdout, stderr = proc.communicate()
    stderr = (stderr or "") + (
        f"\n[{reason}] killed process group for {' '.join(cmd)} "
        f"after {timeout:.1f}s\n"
    )
    return subprocess.CompletedProcess(
        list(cmd), 124, stdout=stdout or "", stderr=stderr
    )


def run_process_group_timeout(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float,
    cwd: str | Path | None = None,
    stderr_path: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a captured command whose process group is killed at the timeout.

    With ``stderr_path`` the child's stderr goes straight into that file while
    it runs, so a caller killed by an outer deadline still leaves the partial
    output on disk. The file then also receives the timeout/interrupt note,
    and the result's ``stderr`` is read back from it.
    """
    if stderr_path is None:
        return _run_process_group(cmd, env=env, timeout=timeout, cwd=cwd,
                                  stderr_sink=subprocess.PIPE)
    path = Path(stderr_path)
    with open(path, "wb") as sink:
        result = _run_process_group(cmd, env=env, timeout=timeout, cwd=cwd,
                                    stderr_sink=sink)
    if result.stderr:
        with open(path, "a", encoding="utf-8") as sink:
            sink.write(result.stderr)
    return subprocess.CompletedProcess(
        result.args, result.returncode, stdout=result.stdout,
        stderr=path.read_text(encoding="utf-8", errors="replace"),
    )


def _run_process_group(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    timeout: float,
    cwd: str | Path | None,
    stderr_sink,
) -> subprocess.CompletedProcess[str]:
    if os.name != "posix":
        return subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=stderr_sink,
            text=True,
            # Decode child output as UTF-8 explicitly: pcc/clang emit UTF-8
            # diagnostics (em-dashes etc.). Relying on the parent locale makes
            # this crash with UnicodeDecodeError under LC_ALL=C. errors=replace
            # keeps a stray non-UTF-8 byte from masking the real exit status.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
        )

    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=stderr_sink,
        text=True,
        # See note above: UTF-8 child output must not be decoded with the
        # parent's (possibly ascii, under LC_ALL=C) locale.
        encoding="utf-8",
        errors="replace",
        env=dict(env) if env is not None else None,
        cwd=str(cwd) if cwd is not None else None,
        start_new_session=True,
    )
    old_handlers: dict[int, object] = {}

    def _interrupt_handler(signum: int, _frame) -> None:
        _kill_process_group(proc, signal.SIGTERM)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    if threading_is_main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _interrupt_handler)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            list(cmd), proc.returncode, stdout=stdout, stderr=stderr
        )
    except subprocess.TimeoutExpired:
        return _finish_after_process_group_kill(
            proc,
            cmd=cmd,
            timeout=timeout,
            reason="TIMEOUT",
        )
    except BaseException:
        result = _finish_after_process_group_kill(
            proc,
            cmd=cmd,
            timeout=timeout,
            reason="INTERRUPT",
        )
        # The caller never receives this result. Preserve the drained evidence
        # in its captured streams before propagating the original exception.
        for stream, output in ((sys.stdout, result.stdout), (sys.stderr, result.stderr)):
            try:
                stream.write(output)
                stream.flush()
            except BaseException:
                # A broken capture sink must neither replace the interruption
                # nor prevent the other stream from being published.
                pass
        raise
    finally:
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)


def threading_is_main_thread() -> bool:
    try:
        import threading
    except ImportError:  # pragma: no cover
        return False
    return threading.current_thread() is threading.main_thread()


def run_process_tree_timeout(
    cmd: Sequence[str],
    *,
    evidence: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    check: bool = False,
    text: bool = True,
    capture_output: bool = True,
    max_output_bytes: int = 1024 * 1024,
) -> subprocess.CompletedProcess[str]:
    """Capture a package command without abandoning its POSIX descendants.

    Reuse the sampler's kernel birth identities and bounded TERM/KILL cleanup;
    nested native workers create their own sessions, beyond a root killpg().
    Each stream is retained on disk up to the explicit byte limit. Overflow
    fails instead of returning truncated output as a successful package plan.
    Windows retains subprocess.run's existing behavior; this is a POSIX fix.
    """
    if not text or not capture_output:
        raise ValueError("process-tree capture requires text=True, capture_output=True")
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("timeout and output limit must be positive")
    if os.name != "posix":
        return subprocess.run(
            list(cmd), check=check, text=True, capture_output=True,
            timeout=timeout, env=env, cwd=cwd,
        )

    import json
    import selectors
    import time

    from scripts import run_process_tree_sample as sampler

    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=False)
    output_paths = {name: evidence / (name + ".log") for name in ("stdout", "stderr")}
    observed = {}
    process = None
    error = None
    cleanup_errors = []
    old_handlers = {}
    outputs = {name: bytearray() for name in output_paths}
    started = time.monotonic()
    deadline = started + timeout

    def interrupt(signum, _frame):
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    try:
        with output_paths["stdout"].open("xb") as stdout_log, output_paths["stderr"].open(
            "xb"
        ) as stderr_log, selectors.DefaultSelector() as selector:
            logs = {"stdout": stdout_log, "stderr": stderr_log}

            def drain(wait):
                # At most one chunk per ready stream: noisy children cannot starve
                # the work deadline or descendant discovery.
                for key, _events in selector.select(wait):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    retained = chunk[:max_output_bytes - len(outputs[name])]
                    logs[name].write(retained)
                    logs[name].flush()
                    outputs[name].extend(retained)
                    if len(retained) != len(chunk):
                        raise RuntimeError(f"{name} exceeded {max_output_bytes} captured bytes")

            try:
                if not threading_is_main_thread():
                    raise RuntimeError("process-tree timeout must run on the main thread")
                process = subprocess.Popen(
                    list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=dict(env) if env is not None else None,
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=True,
                )
                identity = sampler._process_identity(process.pid)
                if identity is None:
                    raise RuntimeError("cannot identify the launched package process")
                observed[process.pid] = identity
                for name in output_paths:
                    pipe = getattr(process, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                for signum in (signal.SIGINT, signal.SIGTERM):
                    old_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, interrupt)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(list(cmd), timeout)
                    states = sampler._termination_process_states(min(0.25, remaining))
                    if states is None:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(list(cmd), timeout)
                        raise RuntimeError("cannot observe the owned package process tree")
                    # Discover before poll() reaps the root and before any signal.
                    sampler._owned_process_pids(observed, states)
                    drain(0)
                    if process.poll() is not None:
                        break
                    drain(min(0.05, max(0.0, deadline - time.monotonic())))
            except BaseException as exc:
                error = exc
            finally:
                # Finish the same bounded cleanup even after a second interrupt.
                for signum in old_handlers:
                    try:
                        signal.signal(signum, signal.SIG_IGN)
                    except BaseException as exc:
                        cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                try:
                    if process is not None:
                        try:
                            sampler._terminate_owned_processes(process, observed)
                            states = sampler._termination_process_states(0.25)
                            if states is None or any(
                                not states[pid].startswith("Z")
                                for pid in sampler._owned_process_pids(observed, states)
                            ):
                                raise RuntimeError("owned package process cleanup is unverified")
                        except BaseException as exc:
                            cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                        drain_deadline = time.monotonic() + 0.25
                        try:
                            while selector.get_map() and time.monotonic() < drain_deadline:
                                drain(min(0.05, max(0.0, drain_deadline - time.monotonic())))
                            if selector.get_map():
                                raise RuntimeError("package output pipes did not reach EOF")
                        except BaseException as exc:
                            cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                        for name in output_paths:
                            pipe = getattr(process, name)
                            if pipe is not None:
                                try:
                                    pipe.close()
                                except BaseException as exc:
                                    cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                finally:
                    for signum, handler in old_handlers.items():
                        try:
                            signal.signal(signum, handler)
                        except BaseException as exc:
                            cleanup_errors.append(f"{type(exc).__name__}: {exc}")

    except BaseException as exc:
        # A log close/flush failure must not replace the command interruption.
        if error is None:
            error = exc
        else:
            cleanup_errors.append(f"{type(exc).__name__}: {exc}")

    stdout = outputs["stdout"].decode("utf-8", "replace")
    stderr = outputs["stderr"].decode("utf-8", "replace")
    if isinstance(error, subprocess.TimeoutExpired):
        # subprocess.run retains byte output on TimeoutExpired even in text mode.
        error.output = bytes(outputs["stdout"])
        error.stderr = bytes(outputs["stderr"])
    if error is None and cleanup_errors:
        error = RuntimeError("; ".join(cleanup_errors))
    if error is None and check and process.returncode:
        error = subprocess.CalledProcessError(process.returncode, list(cmd), stdout, stderr)
    receipt = {
        "command": list(cmd), "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "timeout_s": timeout, "elapsed_s": time.monotonic() - started,
        "returncode": process.returncode if process is not None else None,
        "status": "TIMEOUT" if isinstance(error, subprocess.TimeoutExpired)
        else "FAILED" if error is not None else "COMPLETE",
        "error": type(error).__name__ + ": " + str(error) if error is not None else None,
        "cleanup_errors": cleanup_errors,
        "max_output_bytes_per_stream": max_output_bytes,
        "captured_bytes": {name: len(value) for name, value in outputs.items()},
        "artifacts": {name: str(path) for name, path in output_paths.items()},
    }
    try:
        (evidence / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    except BaseException as exc:
        if error is None:
            error = exc
        else:
            error.add_note(f"Could not save process receipt: {exc}")
    if error is not None:
        error.add_note(f"Process artifacts: {evidence}; cleanup errors: {cleanup_errors}")
        raise error
    return subprocess.CompletedProcess(list(cmd), process.returncode, stdout, stderr)
