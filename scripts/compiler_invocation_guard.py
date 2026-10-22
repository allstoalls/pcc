#!/usr/bin/env python3
"""External, invocation-scoped cleanup for an unchanged gateway benchmark.

This is a benchmark containment workaround, not a native PCC lifecycle fix.
The named compiler stays the real executable in argv and benchmark identities.
No performance lock is acquired and inherited PCC_WORKER_TREE_* is unchanged.
Keep the original outer 2 GiB guard: it accounts for the benchmark and wrapper
overhead as well as the invocation tree checked here.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import threading
import time
import types


TIMEOUT_SECONDS = 300.0
TREE_BUDGET_BYTES = 2 * 1024 ** 3
TRANSPORT_KEYS = (
    "PCC_WORKER_TREE_STATE_PATH",
    "PCC_WORKER_TREE_BUDGET_BYTES",
    "PCC_WORKER_TREE_RECOVERY",
)


class CleanupError(RuntimeError):
    pass


def digest(path):
    state = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            state.update(block)
    return state.hexdigest()


def load_module(path, name):
    if Path(path).name == "run_process_tree_sample.py":
        # The sampler uses a relative import for its lock helper. Put
        # both in a private package so a benchmark's later ordinary import
        # still resolves to its real source tree and performance lock.
        package_name = "_pcc_invocation_guard_helpers_" + hashlib.sha256(
            str(Path(path).resolve().parent).encode()).hexdigest()[:16]
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(Path(path).resolve().parent)]
            sys.modules[package_name] = package
        name = package_name + "." + name
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load " + str(path))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(path).parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def resolve_executable(command, kwargs):
    if not isinstance(command, (tuple, list)) or not command:
        return None
    executable = os.fspath(command[0])
    environment = kwargs.get("env")
    environment = os.environ if environment is None else environment
    base = Path(kwargs.get("cwd") or os.getcwd()).resolve()
    if os.path.dirname(executable):
        return (base / executable).resolve()
    search = os.pathsep.join(str((base / (entry or ".")).resolve())
                            for entry in environment.get("PATH", os.defpath).split(os.pathsep))
    selected = shutil.which(executable, path=search)
    return Path(selected).resolve() if selected else None


def require_outer_guard(environment):
    """The CLI supplements, and must never replace, the combined-tree cap."""
    path = environment.get("PCC_WORKER_TREE_STATE_PATH", "")
    if not path or environment.get("PCC_WORKER_TREE_BUDGET_BYTES") != str(TREE_BUDGET_BYTES):
        raise RuntimeError("an active outer 2 GiB process-tree guard is required")
    recovery = environment.get("PCC_WORKER_TREE_RECOVERY", "").split("\t")
    try:
        if len(recovery) != 4 or recovery[0] != "pcc.worker-tree-recovery.v1":
            raise ValueError("invalid recovery schema")
        timeouts = [float(value) for value in recovery[1].split(",")]
        allowance, guard_deadline = float(recovery[2]), float(recovery[3])
        valid_recovery = (all(0 < value < float("inf") for value in timeouts)
                          and 0 < allowance < float("inf")
                          and time.monotonic() < guard_deadline < float("inf")
                          and allowance == sum(timeouts))
    except (ValueError, OverflowError):
        valid_recovery = False
    if not valid_recovery:
        raise RuntimeError("the outer process-tree guard deadline is missing or expired")
    # The outer guard launches its target before publishing its first table.
    # Allow that initial publication, without launching any compiler meanwhile.
    deadline = min(time.monotonic() + 1.0, guard_deadline)
    while True:
        try:
            lines = Path(path).read_text().splitlines()
            now = time.monotonic()
            rows = {}
            for line in lines[3:]:
                pid, parent, rss = (int(value) for value in line.split("\t"))
                if pid <= 0 or parent < 0 or rss < 0 or pid in rows:
                    raise ValueError("invalid process-tree row")
                rows[pid] = (parent, rss)
            owner = rows.get(os.getpid())
            if (len(lines) >= 4 and lines[0] == "pcc.worker-tree-rss.v1"
                    and int(lines[2]) == TREE_BUDGET_BYTES
                    and 0 <= now - float(lines[1]) <= 2.0
                    and now < guard_deadline
                    and owner is not None and owner[0] == os.getppid() and owner[1] > 0
                    and sum(row[1] for row in rows.values()) <= TREE_BUDGET_BYTES):
                return
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("the outer 2 GiB process-tree snapshot is missing or stale")
        time.sleep(0.02)


class CompilerGuard:
    def __init__(self, helper, compilers, evidence, *, outer_guard_required=False):
        if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("this containment adapter requires Linux process handles (pidfds)")
        descriptor = os.pidfd_open(os.getpid(), 0)
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
        self.helper = helper
        self.compilers = {Path(path).resolve(): digest(path) for path in compilers}
        self.evidence = Path(evidence).resolve()
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.calls = 0
        self.outer_guard_required = outer_guard_required

    def install(self, processes):
        original = processes.run_command

        def run_command(command, *, timeout, capture_output=False, check=False, **kwargs):
            if kwargs.get("executable") is not None:
                overridden = resolve_executable([kwargs["executable"]], kwargs)
                if overridden in self.compilers:
                    raise ValueError("compiler executable overrides are not supported")
            compiler = resolve_executable(command, kwargs)
            if compiler not in self.compilers:
                return original(command, timeout=timeout, capture_output=capture_output,
                                check=check, **kwargs)
            return self.run(command, compiler=compiler, timeout=timeout,
                            capture_output=capture_output, check=check, **kwargs)

        processes.run_command = run_command

    def _signal(self, pid, expected, number):
        """A failed handle lookup never falls back to a reusable numeric PID."""
        descriptor = None
        try:
            descriptor = os.pidfd_open(pid, 0)
            current = self.helper._process_identity(pid)
            if current is None or current.start_time != expected.start_time:
                return False
            signal.pidfd_send_signal(descriptor, number)
            return True
        except ProcessLookupError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _root_exited(self, process):
        # Retain the zombie/session anchor until the complete child tree is
        # empty. Popen.poll() here would release it before the final snapshot.
        return os.waitid(os.P_PID, process.pid,
                         os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    def _cleanup_barrier(self, process, observed):
        """Do not let identity checking overlap even a TERM-resistant worker."""
        if process.pid not in observed:
            # Popen still owns this unreaped child, but missing birth telemetry
            # prevents proving ownership of any descendants. Stop and fail closed.
            process.kill()
            process.wait(timeout=2)
            raise CleanupError("compiler birth identity missing; cannot certify descendants")
        term_deadline = time.monotonic() + 2.0
        deadline = term_deadline + 2.0
        signaled = set()
        empty_after_exit = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CleanupError("owned processes did not exit before the cleanup deadline")
            states = self.helper._termination_process_states(min(0.25, remaining))
            if states is None:
                raise CleanupError("cannot verify compiler cleanup; subsequent work is blocked")
            root_exited = self._root_exited(process)
            owned = self.helper._owned_process_pids(observed, states)
            live = [pid for pid in owned if not states[pid].startswith("Z")]
            if not live and root_exited:
                if empty_after_exit:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    return sorted(owned)
                # Require another complete sample while retaining the root,
                # so a final fork during the preceding ps snapshot is visible.
                empty_after_exit = True
            else:
                empty_after_exit = False
            number = signal.SIGTERM if time.monotonic() < term_deadline else signal.SIGKILL
            for pid in live:
                if number == signal.SIGKILL or pid not in signaled:
                    self._signal(pid, observed[pid], number)
                    signaled.add(pid)
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def run(self, command, *, compiler, timeout, capture_output=False,
            check=False, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("the invocation guard must run in the benchmark main thread")
        if timeout <= 0 or timeout > TIMEOUT_SECONDS:
            raise ValueError("compiler timeout must be positive and no greater than 300 seconds")
        if kwargs.get("shell") or "executable" in kwargs:
            raise ValueError("compiler shell/executable overrides are not supported")
        if "process_group" in kwargs or "start_new_session" in kwargs:
            raise ValueError("the invocation guard owns the compiler session")
        if kwargs.get("stdin") == subprocess.PIPE:
            raise ValueError("compiler stdin=PIPE is unsupported by this containment adapter")
        if capture_output:
            if "stdout" in kwargs or "stderr" in kwargs:
                raise ValueError("capture_output cannot be combined with stdout/stderr")
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if digest(compiler) != self.compilers[compiler]:
            raise RuntimeError("compiler identity changed before invocation")
        environment = kwargs.get("env")
        environment = os.environ if environment is None else environment
        if self.outer_guard_required:
            require_outer_guard(environment)
        self.calls += 1
        directory = self.evidence / ("call-%04d" % self.calls)
        directory.mkdir()
        receipt = {
            "schema": "pcc.compiler-invocation-containment.v1",
            "mode": "external host supervisor of the original compiler executable",
            "status": "RUNNING", "command": list(command),
            "compiler": str(compiler), "compiler_sha256": self.compilers[compiler],
            "cwd": str(Path(kwargs.get("cwd") or os.getcwd()).resolve()),
            "timeout_s": timeout, "max_tree_rss_bytes": TREE_BUDGET_BYTES,
            "inherited_worker_transport": {key: environment.get(key) for key in TRANSPORT_KEYS},
            "outer_guard_required_for_combined_tree_cap": True,
            "samples": [],
        }
        path = directory / "receipt.json"
        save(path, receipt)
        observed = {}
        process = None
        captured = {}
        stopped = []
        previous = {}
        status = "COMPLETE"
        failure = None
        started = time.monotonic()
        deadline = started + timeout
        outputs = {}

        def interrupted(signum, _frame):
            if not stopped:
                stopped.append(signum)

        with contextlib.ExitStack() as stack:
            for stream_name in ("stdout", "stderr"):
                if kwargs.get(stream_name) == subprocess.PIPE:
                    output_path = directory / (stream_name + ".bin")
                    stream = stack.enter_context(output_path.open("w+b"))
                    captured[stream_name] = stream
                    kwargs[stream_name] = stream
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, interrupted)
            try:
                process = subprocess.Popen(command, start_new_session=True, **kwargs)
                identity = self.helper._process_identity(process.pid)
                if identity is None:
                    raise CleanupError("cannot establish the compiler process birth identity")
                observed[process.pid] = identity
                receipt["root_pid"] = process.pid
                receipt["root_start_time"] = list(identity.start_time)
                while True:
                    remaining = deadline - time.monotonic()
                    if stopped:
                        status = "INTERRUPTED"
                        break
                    if remaining <= 0:
                        status = "TIMEOUT"
                        break
                    # Never reap here: a final fork can occur after this
                    # snapshot. The zombie remains an owned session anchor.
                    table, retries, sampled_at = self.helper._timed_process_table(
                        timeouts_s=(min(1.0, remaining),), include_command=False,
                    )
                    rows = self.helper._owned_tree_rows(table, observed)
                    rss = sum(row[1] for row in rows.values())
                    receipt["samples"].append({
                        "monotonic_s": sampled_at, "rss_bytes": rss,
                        "pids": sorted(rows), "process_table_retries": retries,
                    })
                    save(path, receipt)
                    if rss > TREE_BUDGET_BYTES:
                        status = "MEMORY_LIMIT"
                        break
                    if self._root_exited(process):
                        break
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            except BaseException as exc:
                if isinstance(exc, self.helper.ProcessTreeSampleError) and time.monotonic() >= deadline:
                    status = "TIMEOUT"
                    receipt["deadline_sampler_error"] = str(exc)
                else:
                    status = "ERROR"
                    failure = exc
            finally:
                try:
                    if process is not None:
                        receipt["cleanup_zombie_pids"] = self._cleanup_barrier(process, observed)
                        receipt["cleanup_verified"] = True
                except BaseException as exc:
                    receipt["cleanup_verified"] = False
                    receipt["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
                    status = "CLEANUP_ERROR"
                    # A failed barrier is fatal to the entire comparison,
                    # including code that catches an ordinary compile error.
                    failure = SystemExit("compiler cleanup could not be verified; comparison stopped")
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
                for stream_name, stream in captured.items():
                    stream.flush()
                    stream.seek(0)
                    outputs[stream_name] = stream.read()
                if stopped and status not in ("ERROR", "CLEANUP_ERROR"):
                    status = "INTERRUPTED"
                try:
                    receipt["compiler_sha256_after"] = digest(compiler)
                    if receipt["compiler_sha256_after"] != self.compilers[compiler]:
                        raise RuntimeError("compiler identity changed during invocation")
                except Exception as exc:
                    if failure is None:
                        status = "IDENTITY_ERROR"
                        failure = exc
                receipt.update(status=status, returncode=(process.returncode if process else None),
                               elapsed_s=time.monotonic() - started,
                               owned_processes={str(pid): list(identity.start_time)
                                                for pid, identity in observed.items()})
                if failure is not None:
                    receipt["error"] = type(failure).__name__ + ": " + str(failure)
                save(path, receipt)
        if failure is not None:
            raise failure
        if status == "INTERRUPTED":
            raise SystemExit(128 + stopped[0])
        if status == "TIMEOUT":
            raise subprocess.TimeoutExpired(command, timeout,
                                            output=outputs.get("stdout"), stderr=outputs.get("stderr"))
        stdout, stderr = outputs.get("stdout"), outputs.get("stderr")
        if process.text_mode:
            def decode(value):
                return (value.decode(process.encoding, process.errors or "strict")
                        .replace("\r\n", "\n").replace("\r", "\n")) if value is not None else None
            stdout, stderr = decode(stdout), decode(stderr)
        if status == "MEMORY_LIMIT":
            raise subprocess.CalledProcessError(125, command, output=stdout, stderr=stderr)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", action="append", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--processes", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide the unchanged benchmark script and its arguments after --")
    script = Path(command[0]).resolve()
    source = ast.parse(script.read_text())
    if not any(isinstance(node, ast.ImportFrom) and node.module == "processes"
               and node.level == 0 and any(name.name == "run_command" for name in node.names)
               for node in ast.walk(source)):
        parser.error("the benchmark must import run_command from its adjacent processes module")
    helper_path = Path(__file__).resolve().parent / "run_process_tree_sample.py"
    helper = load_module(helper_path, "pcc_external_invocation_tree_helpers")
    processes_path = (args.processes or script.parent / "processes.py").resolve()
    processes = load_module(processes_path, "processes")
    guard = CompilerGuard(helper, args.compiler, args.evidence, outer_guard_required=True)
    guard.install(processes)
    sys.modules["processes"] = processes
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *command[1:]]
    launcher = {
        "schema": "pcc.compiler-containment-launcher.v1",
        "benchmark": str(script), "benchmark_sha256": digest(script),
        "processes": str(processes_path), "processes_sha256": digest(processes_path),
        "helper_sha256": digest(helper_path),
        "helper_dependency_sha256": digest(helper_path.parent / "run_pcc_compile_ab.py"),
        "wrapper_sha256": digest(__file__),
        "real_compilers": {str(path): value for path, value in guard.compilers.items()},
        "scope": "external containment only; native worker lifecycle remains unresolved",
    }
    save(guard.evidence / "launcher.json", launcher)
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        launcher["guarded_invocations"] = guard.calls
        launcher["completed_at_monotonic_s"] = time.monotonic()
        save(guard.evidence / "launcher.json", launcher)


if __name__ == "__main__":
    main()
