#!/usr/bin/env python3
"""Measure the owned value-array kernel against labeled host/C references.

New runs use the owned self backend. Historical v1 LLVM observations remain
recorded evidence and are not relabeled as measurements of this pipeline.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_SOURCE = REPO_ROOT / "benchmarks/python/scenarios/value_array_c_like.py"
C_SOURCE = REPO_ROOT / "benchmarks/c/value_array_c_like.c"
HEAD_TRUTH = REPO_ROOT / "docs/goal/head-truth-manifest.json"
ROUNDS_MARKER = "1_000_000))  # PCC_M3_BENCHMARK_ROUNDS"
EXPECTED_SLOW_PATH = ["0.25", "index-error", "overflow-error", "True"]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def child_env(**updates: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.update(updates)
    return env


def run_checked(
    command: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env or child_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        rendered = " ".join(command)
        detail = (result.stderr or result.stdout)[-4000:]
        raise RuntimeError(f"command failed ({result.returncode}): {rendered}\n{detail}")
    return result


def timed_run(command: list[str], expected: str, timeout: int) -> int:
    started = time.perf_counter_ns()
    result = run_checked(command, timeout=timeout)
    elapsed = time.perf_counter_ns() - started
    if result.stdout != expected:
        raise RuntimeError(
            f"output mismatch for {' '.join(command)}:\n"
            f"expected={expected!r}\nactual={result.stdout!r}"
        )
    return elapsed


def links_libpython(path: Path) -> bool:
    if sys.platform == "darwin":
        command = ["otool", "-L", str(path)]
    elif sys.platform.startswith("linux"):
        command = ["ldd", str(path)]
    else:
        raise RuntimeError(f"unsupported link inspection platform: {sys.platform}")
    result = run_checked(command, timeout=20)
    text = result.stdout + result.stderr
    return "libpython" in text or "Python.framework" in text


def hot_ir(ir_text: str) -> str:
    marker = "@user_value_array_c_like_hot("
    start = ir_text.index(marker)
    start = ir_text.rfind("define ", 0, start)
    end = ir_text.index("\n}", start) + 2
    return ir_text[start:end]


def ir_shape(ir_text: str) -> dict[str, Any]:
    body = hot_ir(ir_text)
    forbidden = ("@py_list_new", "@py_instance_new", "@py_valuebox_new")
    found = [name for name in forbidden if name in body]
    if found:
        raise RuntimeError(f"hot function contains object allocation calls: {found}")
    signature = body.splitlines()[0]
    aggregate = "{ { double, double }, { double, double } }"
    if aggregate not in signature:
        raise RuntimeError(f"hot signature lost typed-array aggregate ABI: {signature}")
    if "@py_int_add" not in body:
        raise RuntimeError("Python-int overflow slow path disappeared from hot function")
    return {
        "function_signature": signature,
        "hot_ir_sha256": sha256_bytes(body.encode()),
        "direct_aggregate_abi": True,
        "object_allocation_calls": found,
        "slow_path_python_int_add_retained": True,
        "instruction_counts": {
            "extractvalue": body.count("extractvalue"),
            "fadd": body.count("fadd double"),
            "fmul": body.count("fmul double"),
            "fsub": body.count("fsub double"),
        },
    }


def allocation_events(log_path: Path) -> list[dict[str, Any]]:
    return [
        event
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for event in [json.loads(line)]
        if event.get("category") == "alloc" and event.get("event") == "alloc_object"
    ]


def allocation_probe(tmp: Path, pcc_prefix: list[str]) -> dict[str, Any]:
    source = PY_SOURCE.read_text(encoding="utf-8")
    if source.count(ROUNDS_MARKER) != 1:
        raise RuntimeError("benchmark rounds marker is missing or ambiguous")
    observations: dict[str, Any] = {}
    for rounds in (0, 1000):
        replacement = f"{rounds}))  # PCC_M3_BENCHMARK_ROUNDS"
        probe_source = source.replace(ROUNDS_MARKER, replacement)
        src = tmp / f"value_array_alloc_{rounds}.py"
        exe = tmp / f"value_array_alloc_{rounds}"
        log = tmp / f"value_array_alloc_{rounds}.jsonl"
        src.write_text(probe_source, encoding="utf-8")
        run_checked(
            [
                *pcc_prefix,
                "--backend",
                "self",
                "--python-libpython=off",
                "--ir-scaffold=on",
                str(src),
                "-o",
                str(exe),
            ],
            timeout=120,
        )
        run_checked(
            [str(exe)],
            timeout=30,
            env=child_env(
                PCC_LOG="alloc",
                PCC_LOG_FORMAT="json",
                PCC_LOG_FILE=str(log),
            ),
        )
        events = allocation_events(log)
        histogram = collections.Counter(int(event.get("value1", 0)) for event in events)
        observations[str(rounds)] = {
            "alloc_object_count": len(events),
            "type_tag_histogram": {str(key): histogram[key] for key in sorted(histogram)},
        }
    if observations["0"] != observations["1000"]:
        raise RuntimeError(f"hot loop allocation delta is non-zero: {observations}")
    return {
        "mode": "self/no-libpython",
        "rounds": [0, 1000],
        "observations": observations,
        "hot_loop_alloc_object_delta": 0,
    }


def mode_result(
    label: str,
    samples: list[int],
    *,
    links_python: bool | str,
    command: list[str],
) -> dict[str, Any]:
    return {
        "label": label,
        "command": command,
        "links_libpython": links_python,
        "samples_ns": samples,
        "median_ns": int(statistics.median(samples)),
    }


def build_manifest(runs: int, warmups: int, timeout: int) -> dict[str, Any]:
    # Benchmark the public host compiler entrypoint.  ``python -m pcc`` is the
    # bootstrap-stage CLI compiled into pcc1/pcc2/pcc3; it intentionally emits
    # a different scaffold boundary and is not an alias for the installed
    # ``pcc`` command.  Running the launcher as a module keeps the subprocess
    # tied to this interpreter/environment without depending on PATH.
    pcc_prefix = [sys.executable, "-m", "pcc.driver.cli_launcher"]
    cc = shutil.which(os.environ.get("CC", "clang"))
    if cc is None:
        raise RuntimeError("C compiler not found")
    source_text = PY_SOURCE.read_text(encoding="utf-8")
    if source_text.count(ROUNDS_MARKER) != 1:
        raise RuntimeError("pinned source rounds marker is missing or ambiguous")
    head_truth = json.loads(HEAD_TRUTH.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="pcc_m3_c_like_") as tmp_name:
        tmp = Path(tmp_name)
        ir_path = tmp / "value_array_c_like.ll"
        self_exe = tmp / "value_array_c_like_self"
        c_exe = tmp / "value_array_c_like_c"
        common = ["--python-libpython=off", "--ir-scaffold=on"]
        run_checked(
            [
                *pcc_prefix,
                "--backend",
                "self",
                *common,
                f"--emit-llvm={ir_path}",
                str(PY_SOURCE),
            ],
            timeout=60,
        )
        for backend, output in (("self", self_exe),):
            run_checked(
                [
                    *pcc_prefix,
                    "--backend",
                    backend,
                    *common,
                    str(PY_SOURCE),
                    "-o",
                    str(output),
                ],
                timeout=120,
            )
        c_compile = [cc, "-O3", "-std=c11", str(C_SOURCE), "-o", str(c_exe)]
        run_checked(c_compile, timeout=60)

        host = run_checked([sys.executable, str(PY_SOURCE)], timeout=timeout)
        expected_python = host.stdout
        lines = expected_python.strip().splitlines()
        if len(lines) != 5 or lines[1:] != EXPECTED_SLOW_PATH:
            raise RuntimeError(f"slow-path oracle mismatch: {lines}")
        checksum = float(lines[0])
        for executable in (self_exe,):
            result = run_checked([str(executable)], timeout=timeout)
            if result.stdout != expected_python:
                raise RuntimeError(f"backend output mismatch: {executable}")
        native = run_checked([str(c_exe)], timeout=timeout)
        native_checksum = float(native.stdout.strip())
        if native_checksum != checksum:
            raise RuntimeError(
                f"native C checksum mismatch: python={checksum} c={native_checksum}"
            )

        commands = {
            "cpython-host": [sys.executable, str(PY_SOURCE)],
            "self/no-libpython": [str(self_exe)],
            "native-c/clang-O3": [str(c_exe)],
        }
        expected = {
            "cpython-host": expected_python,
            "self/no-libpython": expected_python,
            "native-c/clang-O3": native.stdout,
        }
        samples = {name: [] for name in commands}
        names = list(commands)
        for _ in range(warmups):
            for name in names:
                timed_run(commands[name], expected[name], timeout)
        for sample_index in range(runs):
            offset = sample_index % len(names)
            for name in names[offset:] + names[:offset]:
                samples[name].append(timed_run(commands[name], expected[name], timeout))

        modes = {
            "cpython_host": mode_result(
                "CPython-host",
                samples["cpython-host"],
                links_python="host-runtime",
                command=["python", "benchmarks/python/scenarios/value_array_c_like.py"],
            ),
            "self_no_libpython": mode_result(
                "self/no-libpython",
                samples["self/no-libpython"],
                links_python=links_libpython(self_exe),
                command=["value_array_c_like_self"],
            ),
            "native_c": mode_result(
                "native-C/clang-O3",
                samples["native-c/clang-O3"],
                links_python=False,
                command=["value_array_c_like_c"],
            ),
        }
        c_median = modes["native_c"]["median_ns"]
        host_median = modes["cpython_host"]["median_ns"]
        for key in ("self_no_libpython",):
            median = modes[key]["median_ns"]
            modes[key]["ratio_vs_cpython"] = median / host_median
            modes[key]["ratio_vs_native_c"] = median / c_median
        self_mode = modes["self_no_libpython"]
        if self_mode["ratio_vs_native_c"] > 2.0:
            raise RuntimeError(
                "self/no-libpython missed the C-like band: "
                f"{self_mode['ratio_vs_native_c']:.3f}x native C"
            )
        if self_mode["ratio_vs_cpython"] > 0.2:
            raise RuntimeError(
                "self/no-libpython missed the CPython speedup: "
                f"{self_mode['ratio_vs_cpython']:.3f}x CPython"
            )

        ir_text = ir_path.read_text(encoding="utf-8")
        cc_version = run_checked([cc, "--version"], timeout=20).stdout.splitlines()[0]
        return {
            "schema": "pcc.m3_c_like.value_array.v2",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "claim": {
                "name": "fixed value-array float recurrence",
                "boundary": (
                    "One fixed pcc.array[Sample,2] kernel uses an allocation-free "
                    "specialized hot path with Python semantic slow paths retained."
                ),
                "measured_policy": {
                    "self_no_libpython_ratio_vs_native_c_max": 2.0,
                    "self_no_libpython_ratio_vs_cpython_max": 0.2,
                },
                "does_not_claim": [
                    "arbitrary dynamic Python is C-speed",
                    "compile or process startup is allocation-free",
                    "long-running GC pause, RSS, or fragmentation performance",
                ],
            },
            "source_identity": {
                "repository_base_commit": head_truth["source"]["commit"],
                "worktree_dirty": True,
                "binding": "base commit plus exact content and emitted-IR hashes",
                "python_path": str(PY_SOURCE.relative_to(REPO_ROOT)),
                "python_sha256": sha256_path(PY_SOURCE),
                "native_c_path": str(C_SOURCE.relative_to(REPO_ROOT)),
                "native_c_sha256": sha256_path(C_SOURCE),
                "frontend_ir_sha256": sha256_bytes(ir_text.encode()),
            },
            "environment": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "c_compiler": cc_version,
            },
            "workload": {
                "rounds": 1_000_000,
                "recurrences_per_round": 16,
                "runtime_samples": runs,
                "warmups": warmups,
                "checksum": checksum,
                "native_c_checksum": native_checksum,
            },
            "ir_shape": ir_shape(ir_text),
            "correctness": {
                "python_stdout": expected_python,
                "host_self_exact_match": True,
                "native_c_hot_checksum_match": True,
                "slow_path_lines": lines[1:],
            },
            "allocation_probe": allocation_probe(tmp, pcc_prefix),
            "modes": modes,
            "compile_commands": {
                "self": ["pcc", "--backend", "self", *common],
                "native_c": [Path(cc).name, "-O3", "-std=c11"],
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--run-timeout", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.runs < 3:
        parser.error("--runs must be at least 3")
    manifest = build_manifest(args.runs, args.warmups, args.run_timeout)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
