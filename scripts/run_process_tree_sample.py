#!/usr/bin/env python3
"""Run one command with process-tree RSS sampling and a hard watchdog.

The target runs in a fresh process group.  Every sample reconstructs its
descendants from ``ps`` and records synchronized aggregate RSS; stdout/stderr
remain durable separate artifacts.  The optional repository performance lock
uses the same implementation as PCC's A/B tools.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import run_pcc_compile_ab as compile_ab


_INTERRUPT_REQUESTED = False
_PROCESS_TABLE_TIMEOUTS_S = (5.0, 20.0)
_SAFETY_PROCESS_TABLE_TIMEOUTS_S = (1.0,)
_GIB = 1024 * 1024 * 1024
_MIN_PRESSURED_SWAP_FREE_BYTES = 4 * _GIB
# On a large-RAM / small-swap host (e.g. 96 GiB RAM with a 4 GiB dynamic swap)
# ``vm.swapusage`` always looks "pressured" relative to the tiny swap file even
# though the machine has tens of GiB of reclaimable physical memory and the
# capped tree (<= max_tree_rss + reserve) will never thrash.  The swap-pressure
# refusal is waived only when reclaimable physical memory comfortably clears
# this multiple of the required budget; the hard reclaimable floor below still
# fails closed on a genuinely memory-starved host.
_SWAP_PRESSURE_RECLAIMABLE_MARGIN = 2


def _request_interrupt(_signum, _frame) -> None:
    global _INTERRUPT_REQUESTED
    _INTERRUPT_REQUESTED = True


class ProcessTreeSampleError(RuntimeError):
    def __init__(self, message: str, *, retry_count: int = 0) -> None:
        super().__init__(message)
        self.retry_count = retry_count


def _persist(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _process_table(
    *,
    timeouts_s: tuple[float, ...] = _PROCESS_TABLE_TIMEOUTS_S,
    include_command: bool = True,
) -> tuple[dict[int, tuple[int, int, str]], int]:
    run = None
    retry_count = 0
    for timeout_s in timeouts_s:
        try:
            run = subprocess.run(
                (
                    ["ps", "-ww", "-Ao", "pid=,ppid=,rss=,command="]
                    if include_command
                    else ["ps", "-Ao", "pid=,ppid=,rss="]
                ),
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout_s,
            )
            break
        except subprocess.TimeoutExpired as exc:
            retry_count += 1
            if timeout_s == timeouts_s[-1]:
                raise ProcessTreeSampleError(
                    "ps timed out after bounded process-table retries: "
                    + ",".join(str(value) for value in timeouts_s)
                    + " seconds",
                    retry_count=retry_count,
                ) from exc
    if run is None:
        raise ProcessTreeSampleError("ps produced no process-table result")
    if run.returncode != 0:
        raise ProcessTreeSampleError("ps failed: " + run.stderr.strip())
    rows: dict[int, tuple[int, int, str]] = {}
    for raw in run.stdout.splitlines():
        fields = raw.strip().split(None, 3)
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            rss_bytes = int(fields[2]) * 1024
        except ValueError:
            continue
        command = fields[3] if len(fields) >= 4 else ""
        rows[pid] = (ppid, rss_bytes, command)
    return rows, retry_count


def _process_command(pid: int, *, timeout_s: float = 0.25) -> str:
    """Read one argv after the safety-critical lean RSS table succeeds."""

    try:
        run = subprocess.run(
            ["ps", "-ww", "-p", str(int(pid)), "-o", "command="],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if run.returncode != 0:
        return ""
    return run.stdout.strip()


def _tree_rows(
    root_pid: int,
    table: dict[int, tuple[int, int, str]],
) -> dict[int, tuple[int, int, str]]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _rss, _command) in table.items():
        children.setdefault(ppid, []).append(pid)
    selected: dict[int, tuple[int, int, str]] = {}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in selected:
            continue
        row = table.get(pid)
        if row is None:
            continue
        selected[pid] = row
        pending.extend(children.get(pid, ()))
    return selected


def _terminate_owned_processes(
    process: subprocess.Popen[bytes],
    known_pids: set[int],
) -> None:
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        for pid in sorted(known_pids):
            if pid == os.getpid():
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        for pid in sorted(known_pids):
            if pid == os.getpid():
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


def _recorded_environment(environment: dict[str, str]) -> dict[str, str]:
    fixed = {
        "HOME",
        "LANG",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONPYCACHEPREFIX",
        "TMPDIR",
        "XDG_CACHE_HOME",
    }
    return {
        key: environment[key]
        for key in sorted(environment)
        if key.startswith("PCC_") or key in fixed
    }


def _parse_scaled_bytes(raw: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)", raw.strip())
    if match is None:
        raise ProcessTreeSampleError("cannot parse resource size: " + raw)
    scale = {
        "": 1,
        "K": 1024,
        "M": 1024 * 1024,
        "G": _GIB,
        "T": 1024 * _GIB,
    }[match.group(2)]
    return int(float(match.group(1)) * scale)


def _parse_vm_stat_reclaimable(raw: str) -> int:
    header = re.search(r"page size of ([0-9]+) bytes", raw)
    if header is None:
        raise ProcessTreeSampleError("vm_stat did not report its page size")
    page_size = int(header.group(1))
    pages: dict[str, int] = {}
    for line in raw.splitlines():
        name, separator, value = line.partition(":")
        if separator != ":":
            continue
        digits = value.strip().rstrip(".")
        if digits.isdigit():
            pages[name.strip()] = int(digits)
    reclaimable_names = (
        "Pages free",
        "Pages inactive",
        "Pages speculative",
        "Pages purgeable",
    )
    if not any(name in pages for name in reclaimable_names):
        raise ProcessTreeSampleError("vm_stat has no reclaimable-page counters")
    return sum(pages.get(name, 0) for name in reclaimable_names) * page_size


def _parse_swapusage(raw: str) -> tuple[int, int, int]:
    values = {}
    for name in ("total", "used", "free"):
        match = re.search(r"\b" + name + r"\s*=\s*([0-9.]+[KMGT]?)", raw)
        if match is None:
            raise ProcessTreeSampleError("vm.swapusage is missing " + name)
        values[name] = _parse_scaled_bytes(match.group(1))
    return values["total"], values["used"], values["free"]


def _darwin_resource_preflight(
    *,
    max_tree_rss_bytes: int,
    reserve_bytes: int,
) -> dict[str, int]:
    if sys.platform != "darwin":
        raise ProcessTreeSampleError(
            "Darwin memory preflight is unavailable on " + sys.platform
        )
    vm_stat = subprocess.run(
        ["/usr/bin/vm_stat"],
        check=False,
        text=True,
        capture_output=True,
        timeout=5,
    )
    if vm_stat.returncode != 0:
        raise ProcessTreeSampleError("vm_stat failed: " + vm_stat.stderr.strip())
    swap = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
        check=False,
        text=True,
        capture_output=True,
        timeout=5,
    )
    if swap.returncode != 0:
        raise ProcessTreeSampleError(
            "vm.swapusage failed: " + swap.stderr.strip()
        )
    reclaimable_bytes = _parse_vm_stat_reclaimable(vm_stat.stdout)
    swap_total_bytes, swap_used_bytes, swap_free_bytes = _parse_swapusage(
        swap.stdout
    )
    required_bytes = int(max_tree_rss_bytes) + int(reserve_bytes)
    disk_free_bytes = shutil.disk_usage("/").free
    if reclaimable_bytes < required_bytes:
        raise ProcessTreeSampleError(
            "insufficient reclaimable memory for guarded process tree"
        )
    if disk_free_bytes < required_bytes:
        raise ProcessTreeSampleError(
            "insufficient disk space for guarded process tree and swap reserve"
        )
    ample_physical_headroom = (
        reclaimable_bytes
        >= required_bytes * _SWAP_PRESSURE_RECLAIMABLE_MARGIN
    )
    if (
        swap_total_bytes > 0
        and swap_used_bytes * 2 > swap_total_bytes
        and swap_free_bytes < _MIN_PRESSURED_SWAP_FREE_BYTES
        and not ample_physical_headroom
    ):
        raise ProcessTreeSampleError(
            "swap is already pressured; refusing guarded process tree"
        )
    return {
        "max_tree_rss_bytes": int(max_tree_rss_bytes),
        "reserve_bytes": int(reserve_bytes),
        "required_reclaimable_and_disk_free_bytes": required_bytes,
        "reclaimable_bytes": reclaimable_bytes,
        "disk_free_bytes": disk_free_bytes,
        "swap_total_bytes": swap_total_bytes,
        "swap_used_bytes": swap_used_bytes,
        "swap_free_bytes": swap_free_bytes,
        "swap_pressure_waived_by_reclaimable": bool(ample_physical_headroom),
    }


def _process_snapshot(
    tree: dict[int, tuple[int, int, str]],
    command_cache: dict[int, str] | None = None,
) -> list[dict[str, object]]:
    ordered = sorted(
        tree.items(),
        key=lambda item: (-item[1][1], item[0]),
    )
    return [
        {
            "pid": pid,
            "ppid": row[0],
            "rss_bytes": row[1],
            "command": (
                row[2]
                if row[2]
                else "" if command_cache is None else command_cache.get(pid, "")
            ),
            "manifest_paths": _command_manifest_paths(
                row[2]
                if row[2]
                else "" if command_cache is None else command_cache.get(pid, "")
            ),
        }
        for pid, row in ordered
    ]


def _command_manifest_paths(command: str) -> list[str]:
    paths = []
    for token in str(command).split():
        candidate = token.strip("'\"")
        if candidate.endswith(".manifest") and candidate not in paths:
            paths.append(candidate)
    return paths


def _run(args: argparse.Namespace) -> dict[str, object]:
    global _INTERRUPT_REQUESTED
    _INTERRUPT_REQUESTED = False
    if args.timeout <= 0 or args.interval <= 0 or args.progress_interval <= 0:
        raise ProcessTreeSampleError("timeouts and intervals must be positive")
    if args.max_tree_rss_bytes < 0:
        raise ProcessTreeSampleError("max tree RSS must be zero or positive")
    if args.darwin_preflight_reserve_bytes < 0:
        raise ProcessTreeSampleError("preflight reserve must be zero or positive")
    if args.darwin_preflight_reserve_bytes and args.max_tree_rss_bytes <= 0:
        raise ProcessTreeSampleError("Darwin preflight requires a positive RSS cap")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ProcessTreeSampleError("missing command after --")

    result_path = Path(args.result).expanduser().absolute()
    samples_path = Path(args.samples).expanduser().absolute()
    stdout_path = Path(args.stdout).expanduser().absolute()
    stderr_path = Path(args.stderr).expanduser().absolute()
    for path in (result_path, samples_path, stdout_path, stderr_path):
        if path.exists():
            raise ProcessTreeSampleError("refusing existing output: " + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(args.cwd).expanduser().resolve()
    environment = os.environ.copy()
    started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    started = time.monotonic()
    payload: dict[str, object] = {
        "schema": "pcc.process_tree_sample.v1",
        "status": "RUNNING",
        "started_at_utc": started_utc,
        "command": command,
        "cwd": str(cwd),
        "environment": _recorded_environment(environment),
        "timeout_s": args.timeout,
        "interval_s": args.interval,
        "max_tree_rss_bytes": args.max_tree_rss_bytes,
        "darwin_preflight_reserve_bytes": args.darwin_preflight_reserve_bytes,
    }
    _persist(result_path, payload)
    if args.darwin_preflight_reserve_bytes:
        try:
            payload["resource_preflight"] = _darwin_resource_preflight(
                max_tree_rss_bytes=args.max_tree_rss_bytes,
                reserve_bytes=args.darwin_preflight_reserve_bytes,
            )
            _persist(result_path, payload)
        except BaseException as exc:
            payload.update(
                {
                    "status": "PREFLIGHT_REJECTED",
                    "completed_at_utc": dt.datetime.now(
                        dt.timezone.utc
                    ).isoformat(),
                    "elapsed_s": time.monotonic() - started,
                    "error": type(exc).__name__ + ": " + str(exc),
                }
            )
            _persist(result_path, payload)
            if isinstance(exc, ProcessTreeSampleError):
                raise
            raise ProcessTreeSampleError(
                "resource preflight failed: " + type(exc).__name__ + ": " + str(exc)
            ) from exc

    lock_context = (
        compile_ab._performance_lock()
        if args.performance_lock
        else contextlib.nullcontext()
    )
    samples: list[dict[str, object]] = []
    known_pids: set[int] = set()
    peak_tree_rss = 0
    peak_process_count = 0
    process_table_retry_count = 0
    timed_out = False
    interrupted = False
    memory_limited = False
    sampler_error = ""
    largest_process_observed: dict[str, object] = {
        "pid": 0,
        "ppid": 0,
        "rss_bytes": 0,
        "command": "",
        "manifest_paths": [],
        "elapsed_s": 0.0,
    }
    terminal_processes: list[dict[str, object]] = []
    command_cache: dict[int, str] = {}
    command_lookup_failure_count = 0
    process_table_timeouts = (
        _SAFETY_PROCESS_TABLE_TIMEOUTS_S
        if args.max_tree_rss_bytes > 0
        else _PROCESS_TABLE_TIMEOUTS_S
    )
    with lock_context:
        with stdout_path.open("wb") as stdout_stream, stderr_path.open(
            "wb"
        ) as stderr_stream, samples_path.open("w", encoding="utf-8") as samples_stream:
            samples_stream.write(
                "elapsed_s\ttree_rss_bytes\tprocess_count\tlargest_pid\t"
                "largest_rss_bytes\tlargest_command\n"
            )
            samples_stream.flush()
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
            )
            deadline = started + args.timeout
            next_progress = started
            try:
                while True:
                    table, retries = _process_table(
                        timeouts_s=process_table_timeouts,
                        include_command=args.max_tree_rss_bytes <= 0,
                    )
                    process_table_retry_count += retries
                    tree = _tree_rows(process.pid, table)
                    known_pids.update(tree)
                    tree_rss = sum(row[1] for row in tree.values())
                    process_count = len(tree)
                    peak_tree_rss = max(peak_tree_rss, tree_rss)
                    peak_process_count = max(peak_process_count, process_count)
                    largest_pid = 0
                    largest_rss = 0
                    largest_command = ""
                    for pid, (_ppid, rss_bytes, command_name) in tree.items():
                        if command_name:
                            command_cache[pid] = command_name
                        if rss_bytes > largest_rss:
                            largest_pid = pid
                            largest_rss = rss_bytes
                            largest_command = command_name
                    if largest_pid and not largest_command:
                        largest_command = command_cache.get(largest_pid, "")
                        if not largest_command:
                            largest_command = _process_command(largest_pid)
                            if largest_command:
                                command_cache[largest_pid] = largest_command
                            else:
                                command_lookup_failure_count += 1
                    elapsed = time.monotonic() - started
                    terminal_processes = _process_snapshot(tree, command_cache)
                    if largest_rss > int(
                        largest_process_observed["rss_bytes"]
                    ):
                        largest_process_observed = {
                            "pid": largest_pid,
                            "ppid": tree.get(largest_pid, (0, 0, ""))[0],
                            "rss_bytes": largest_rss,
                            "command": largest_command,
                            "manifest_paths": _command_manifest_paths(
                                largest_command
                            ),
                            "elapsed_s": round(elapsed, 6),
                        }
                    sample = {
                        "elapsed_s": round(elapsed, 6),
                        "tree_rss_bytes": tree_rss,
                        "process_count": process_count,
                        "largest_pid": largest_pid,
                        "largest_rss_bytes": largest_rss,
                        "largest_command": largest_command,
                    }
                    samples.append(sample)
                    safe_largest_command = largest_command.replace(
                        "\t", " "
                    ).replace("\n", " ")
                    samples_stream.write(
                        str(sample["elapsed_s"])
                        + "\t"
                        + str(tree_rss)
                        + "\t"
                        + str(process_count)
                        + "\t"
                        + str(largest_pid)
                        + "\t"
                        + str(largest_rss)
                        + "\t"
                        + safe_largest_command
                        + "\n"
                    )
                    samples_stream.flush()
                    now = time.monotonic()
                    if now >= next_progress:
                        print(
                            "elapsed={:.1f}s processes={} tree_rss={} peak_rss={}".format(
                                elapsed,
                                process_count,
                                tree_rss,
                                peak_tree_rss,
                            ),
                            flush=True,
                        )
                        next_progress = now + args.progress_interval
                        payload.update(
                            {
                                "elapsed_s": elapsed,
                                "sample_count": len(samples),
                                "peak_tree_rss_bytes": peak_tree_rss,
                                "peak_process_count": peak_process_count,
                                "process_table_retry_count": (
                                    process_table_retry_count
                                ),
                                "command_lookup_failure_count": (
                                    command_lookup_failure_count
                                ),
                                "largest_process_observed": (
                                    largest_process_observed
                                ),
                            }
                        )
                        _persist(result_path, payload)
                    if (
                        args.max_tree_rss_bytes > 0
                        and tree_rss > args.max_tree_rss_bytes
                    ):
                        memory_limited = True
                        _terminate_owned_processes(process, known_pids)
                        returncode = process.returncode
                        break
                    returncode = process.poll()
                    if returncode is not None:
                        # A wrapper can exit while one of its children keeps
                        # running in a different process group. Only select
                        # recorded PIDs still in our private session; this
                        # excludes reused PIDs and unrelated sessions.
                        after_exit, retries = _process_table(
                            timeouts_s=process_table_timeouts,
                            include_command=False,
                        )
                        process_table_retry_count += retries
                        remaining = set()
                        for pid in after_exit:
                            if pid == process.pid:
                                continue
                            with contextlib.suppress(ProcessLookupError):
                                if os.getsid(pid) == process.pid:
                                    remaining.add(pid)
                        if remaining:
                            known_pids.update(remaining)
                            payload["post_exit_cleanup_pids"] = sorted(remaining)
                            _terminate_owned_processes(process, remaining)
                        break
                    if _INTERRUPT_REQUESTED:
                        interrupted = True
                        _terminate_owned_processes(process, known_pids)
                        returncode = process.returncode
                        break
                    if now >= deadline:
                        timed_out = True
                        _terminate_owned_processes(process, known_pids)
                        returncode = process.returncode
                        break
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                _terminate_owned_processes(process, known_pids)
                interrupted = True
                returncode = process.returncode
            except BaseException as exc:
                _terminate_owned_processes(process, known_pids)
                process_table_retry_count += int(
                    getattr(exc, "retry_count", 0) or 0
                )
                sampler_error = type(exc).__name__ + ": " + str(exc)
                returncode = process.returncode

    payload.update(
        {
            "status": (
                "SAMPLER_ERROR"
                if sampler_error
                else (
                    "MEMORY_LIMIT"
                    if memory_limited
                    else (
                        "INTERRUPTED"
                        if interrupted
                        else ("TIMEOUT" if timed_out else "COMPLETE")
                    )
                )
            ),
            "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_s": time.monotonic() - started,
            "returncode": returncode,
            "sample_count": len(samples),
            "process_table_retry_count": process_table_retry_count,
            "command_lookup_failure_count": command_lookup_failure_count,
            "process_table_timeouts_s": list(process_table_timeouts),
            "peak_tree_rss_bytes": peak_tree_rss,
            "peak_process_count": peak_process_count,
            "known_process_count": len(known_pids),
            "largest_process_observed": largest_process_observed,
            "terminal_processes": terminal_processes,
            "artifacts": {
                "samples": str(samples_path),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
        }
    )
    if sampler_error:
        payload["error"] = sampler_error
    _persist(result_path, payload)
    if sampler_error:
        raise ProcessTreeSampleError(sampler_error)
    return payload


def run(args: argparse.Namespace) -> dict[str, object]:
    """Run with the same cooperative SIGINT contract when imported or invoked."""
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _request_interrupt)
    try:
        return _run(args)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--progress-interval", type=float, default=30.0)
    parser.add_argument(
        "--max-tree-rss-bytes",
        type=int,
        default=0,
        help="terminate the owned process group when aggregate RSS exceeds this cap",
    )
    parser.add_argument(
        "--darwin-preflight-reserve-bytes",
        type=int,
        default=0,
        help="before launch, require RSS cap plus this much reclaimable memory",
    )
    parser.add_argument(
        "--performance-lock",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(_parser().parse_args(argv))
    except (OSError, ValueError, ProcessTreeSampleError) as exc:
        print("process-tree sample error: " + str(exc), file=sys.stderr)
        return 2
    if result["status"] == "TIMEOUT":
        return 124
    if result["status"] == "MEMORY_LIMIT":
        return 125
    if result["status"] == "INTERRUPTED":
        return 130
    return int(result["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
