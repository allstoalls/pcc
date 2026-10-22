#!/usr/bin/env python3
"""Profile one native pcc compile by phase: a busy worker, then the coordinator.

Runs the compile under scripts/run_process_tree_sample.py (the gate's guard,
so worker admission matches) inside /usr/bin/time -l, then samples with
scripts/pcc_flamegraph.py:

* worker phase: once a frontend/emit worker is running, the busiest worker;
* coordinator phase: once no worker remains, the compiler process itself.

The compiler process is found by its executable path (argv[0] equals the
compiler), never by matching command-line text: the guard's own argv embeds
the compiler command and would match first.

Writes <out>/{worker,link}.folded, inclusive-by-function summaries
(<out>/{worker,link}.inclusive.txt), the guard receipt, compile stderr (with
the /usr/bin/time CPU totals) and summary.json.

    scripts/profile_compile_phases.py --compiler build/bootstrap/pcc1 \\
        --out /tmp/prof --env PCC_RUNTIME_ARCHIVE=... -- app.py -o app

Reports sampled weights, not wall-time attribution for the whole build; a
loaded machine stretches every phase alike.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
WORKER_ARGS = ("--pcc-python-multi-codegen-worker", "--pcc-self-backend-emit-worker",
               "--pcc-self-backend-emit-batch-worker", "--pcc-self-backend-indexed-emit-worker")


def _processes():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True,
                         check=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _descendants(root, rows):
    children = collections.defaultdict(list)
    for pid, ppid, args in rows:
        children[ppid].append((pid, args))
    found, pending = [], [root]
    while pending:
        for pid, args in children.get(pending.pop(), ()):
            found.append((pid, args))
            pending.append(pid)
    return found


def _compiler_pid(guard_pid, compiler):
    for pid, args in _descendants(guard_pid, _processes()):
        if args.split(" ", 1)[0] == compiler and not any(arg in args for arg in WORKER_ARGS):
            return pid
    return 0


def _workers(guard_pid):
    return [pid for pid, args in _descendants(guard_pid, _processes())
            if any(arg in args for arg in WORKER_ARGS)]


def _sample(pid, seconds, folded, exact):
    command = [sys.executable, str(ROOT / "scripts/pcc_flamegraph.py"), "cpu", str(pid), str(seconds),
               "--folded", str(folded), "-o", str(folded.with_suffix(".svg")),
               "--evidence-dir", str(folded.with_suffix(".evidence")), "--top", "20"]
    if exact:
        command.append("--exact-pid")
    result = subprocess.run(command, capture_output=True, text=True)
    folded.with_suffix(".report.txt").write_text(result.stdout + result.stderr)
    return result.returncode == 0 and folded.exists()


def _inclusive(folded, top=40):
    totals = collections.Counter()
    samples = 0
    for line in folded.read_text().splitlines():
        stack, _, weight = line.rpartition(" ")
        weight = int(weight)
        samples += weight
        seen = set()
        for frame in stack.split(";"):
            if frame not in seen:
                totals[frame] += weight
                seen.add(frame)
    lines = ["samples " + str(samples)]
    for frame, weight in totals.most_common(top):
        lines.append(f"{100 * weight / max(samples, 1):5.1f}% {frame}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compiler", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE for the compile")
    parser.add_argument("--worker-delay", type=float, default=30.0,
                        help="seconds into the worker phase before sampling")
    parser.add_argument("--worker-seconds", type=int, default=25)
    parser.add_argument("--link-seconds", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tree-rss-bytes", type=int, default=16 << 30)
    parser.add_argument("compile_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    compiler = str(Path(args.compiler).resolve())
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    compile_args = args.compile_args[1:] if args.compile_args[:1] == ["--"] else args.compile_args
    env = dict(os.environ)
    for item in args.env:
        key, _, value = item.partition("=")
        env[key] = value
    guard = [sys.executable, str(ROOT / "scripts/run_process_tree_sample.py"),
             "--cwd", os.getcwd(), "--timeout", str(args.timeout),
             "--max-tree-rss-bytes", str(args.max_tree_rss_bytes),
             "--result", str(out / "result.json"), "--samples", str(out / "samples.tsv"),
             "--stdout", str(out / "compile.stdout"), "--stderr", str(out / "compile.stderr"),
             "--", "/usr/bin/time", "-l", compiler, *compile_args]
    started = time.monotonic()
    process = subprocess.Popen(guard, env=env, stdout=open(out / "guard.log", "w"), stderr=subprocess.STDOUT)
    summary = {"compiler": compiler, "args": compile_args, "env": args.env}
    pid = 0
    while pid == 0 and process.poll() is None:
        time.sleep(0.5)
        pid = _compiler_pid(process.pid, compiler)
    summary["compiler_pid"] = pid
    worker_seen = 0.0
    while process.poll() is None and not worker_seen:
        if _workers(process.pid):
            worker_seen = time.monotonic()
        else:
            time.sleep(0.5)
    if worker_seen:
        time.sleep(args.worker_delay)
        if process.poll() is None and _workers(process.pid):
            summary["worker_sampled_at_s"] = round(time.monotonic() - started, 1)
            if _sample(pid, args.worker_seconds, out / "worker.folded", exact=False):
                (out / "worker.inclusive.txt").write_text(_inclusive(out / "worker.folded"))
    while process.poll() is None and _workers(process.pid):
        time.sleep(0.5)
    if process.poll() is None and pid:
        summary["link_sampled_at_s"] = round(time.monotonic() - started, 1)
        if _sample(pid, args.link_seconds, out / "link.folded", exact=True):
            (out / "link.inclusive.txt").write_text(_inclusive(out / "link.folded"))
    summary["guard_returncode"] = process.wait()
    summary["wall_s"] = round(time.monotonic() - started, 1)
    stderr = (out / "compile.stderr").read_text(errors="replace") if (out / "compile.stderr").exists() else ""
    timing = re.search(r"([\d.]+) real\s+([\d.]+) user\s+([\d.]+) sys", stderr)
    if timing:
        summary.update(real_s=float(timing.group(1)), user_s=float(timing.group(2)), sys_s=float(timing.group(3)))
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary["guard_returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
