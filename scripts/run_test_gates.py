#!/usr/bin/env python3
"""Repository closure/gate runners without a shell.

The per-area closure gates used to be one Bash script each (``run_b1_b6_closure_gate.sh``,
``run_d2_d6_closure_gate.sh``, ``run_final_language_closure_gate.sh``,
``run_coroutine_scheduler_roots_gate.sh``, ``run_gc_production_contract.sh``,
``gc_longrun.sh``, ``run_goal_closure_bundle_gate.sh``).  They were thin
``uv run pytest`` wrappers plus two small matrices, so they now live here with
one registry: no bash, and the same names work on Windows.

Usage:
  scripts/run_test_gates.py --list
  scripts/run_test_gates.py --gate b1-b6
  scripts/run_test_gates.py --gate d2-d6
  scripts/run_test_gates.py --gate final-language
  scripts/run_test_gates.py --gate coroutine-scheduler-roots
  scripts/run_test_gates.py --gate goal-closure-bundle
  scripts/run_test_gates.py --gate gc-production-contract [--backends "0 3 4"]
  scripts/run_test_gates.py --gate gc-longrun [--out-dir DIR] [--churn-rounds N]

Environment equivalents are preserved: ``GC_BACKENDS`` and
``GC_CONTRACT_SUITE`` select the GC production-contract matrix.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

_B1_B6 = (
    "tests/python/data_model/test_b1_b6_compiled_acceptance.py",
    "tests/python/data_model/test_b1_b6_runtime_wiring_regression.py",
    "tests/python/data_model/test_bytes_literal.py",
    "tests/python/test_runtime_type_builtin_native.py",
    "tests/python/data_model/test_classvar_runtime.py",
    "tests/python/data_model/test_user_dunder_runtime.py",
    "tests/python/data_model/test_exception_chaining_runtime.py",
    "tests/python/data_model/test_call_splat_runtime.py",
)
_D2_D6 = (
    "tests/python/data_model/test_d2_d6_compiled_acceptance.py",
    "tests/python/data_model/test_d2_d6_runtime_wiring_regression.py",
    "tests/python/test_generator_protocol.py",
    "tests/python/test_async_await.py",
    "tests/python/test_context_manager_full.py",
    "tests/python/test_protocol_edges.py",
    "tests/python/test_format_protocol.py",
)
_FINAL_LANGUAGE = (
    "tests/python/test_dynamic_import.py",
    "tests/python/test_inspect_protocol.py",
    "tests/python/test_pickle_copy.py",
    "tests/python/test_dataclasses_full.py",
    "tests/python/data_model/test_final_language_compiled_acceptance.py",
    "tests/python/data_model/test_t4_weakref_native_acceptance.py",
)
_COROUTINE_ROOTS = (
    "tests/python/test_gc_coroutine_roots.py",
    "tests/python/test_gc_coroutine_scheduler_roots_production.py",
)
_GOAL_CLOSURE_FILES = (
    "docs/research/c-extension-abi.md",
    "docs/reports/goal-final-evaluation-next-phase.md",
    "docs/investigations/default-backend-verdict.md",
    "docs/investigations/bootstrap-five-gc-matrix.md",
)
_LONGRUN_WORKLOADS = (
    ("churn", "benchmarks/python/longrun_churn.py", "churn_rounds"),
    ("growshrink", "benchmarks/python/longrun_growshrink.py", "gs_cycles"),
    ("finalizers", "benchmarks/python/longrun_finalizers.py", "fin_rounds"),
    ("pointer_mutator", "benchmarks/python/longrun_pointer_mutator.py", "pm_rounds"),
)


def _child_env() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    return environment


def _python_prefix() -> list[str]:
    uv = shutil.which("uv")
    return [uv, "run", "python"] if uv else [sys.executable]


def _pytest_prefix() -> list[str]:
    uv = shutil.which("uv")
    return [uv, "run", "pytest"] if uv else [sys.executable, "-m", "pytest"]


def run_command(command: list[str], *, env: dict[str, str] | None = None) -> int:
    print("+ " + " ".join(command), flush=True)
    completed = subprocess.run(
        command, cwd=str(ROOT), env=env or _child_env(), check=False
    )
    return completed.returncode


def run_pytest(tests: tuple[str, ...], *, extra_env: dict[str, str] | None = None) -> int:
    environment = _child_env()
    if extra_env:
        environment.update(extra_env)
    return run_command([*_pytest_prefix(), *tests, "-q", "-n0"], env=environment)


def gate_b1_b6(_args) -> int:
    return run_pytest(_B1_B6)


def gate_d2_d6(_args) -> int:
    return run_pytest(_D2_D6)


def gate_final_language(_args) -> int:
    ownership = run_command(
        [*_python_prefix(), "scripts/check_layer1_ownership.py"]
    )
    if ownership != 0:
        return ownership
    return run_pytest(_FINAL_LANGUAGE)


def gate_coroutine_scheduler_roots(_args) -> int:
    return run_pytest(_COROUTINE_ROOTS)


def gate_async_gateway(args) -> int:
    required = ("gateway", "runtime_archive", "source_manifest", "out_dir",
                "threads", "refcount")
    missing = [name.replace("_", "-") for name in required if getattr(args, name) is None]
    if missing:
        raise SystemExit("async-gateway requires --" + ", --".join(missing))
    command = [
        sys.executable, "-B", str(ROOT / "scripts/qualification/async_gateway.py"),
        "--core", str(ROOT), "--gateway", args.gateway,
        "--runtime-archive", args.runtime_archive,
        "--source-manifest", args.source_manifest, "--output", args.out_dir,
        "--threads", args.threads, "--refcount", args.refcount,
    ]
    for option in ("pcc0", "pcc1"):
        value = getattr(args, option)
        if value:
            command.extend(["--" + option, value])
    return run_command(command)


def gate_goal_closure_bundle(_args) -> int:
    for gate in (gate_b1_b6, gate_d2_d6, gate_final_language):
        code = gate(_args)
        if code != 0:
            return code
    missing = [name for name in _GOAL_CLOSURE_FILES if not (ROOT / name).is_file()]
    if missing:
        print("missing closure evidence: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("goal closure bundle: PASS")
    return 0


def gate_gc_production_contract(args) -> int:
    suite = args.suite or os.environ.get("GC_CONTRACT_SUITE") or (
        "tests/python/gc_production_contract"
    )
    backends = (
        args.backends
        or os.environ.get("GC_BACKENDS")
        or "0 1 2 3 4"
    ).split()
    if not (ROOT / suite).is_dir():
        print(f"error: contract suite dir not found: {suite}", file=sys.stderr)
        return 2
    failed: list[str] = []
    for backend in backends:
        print(f"=== PCC_GC_BACKEND={backend} production contract ({suite}) ===")
        if run_pytest((suite,), extra_env={"PCC_GC_BACKEND": backend}) != 0:
            print(
                f"!!! backend #{backend} FAILED the common production contract",
                file=sys.stderr,
            )
            failed.append(backend)
    if failed:
        print(
            "5-GC production contract: FAILED (a production backend is a "
            "release blocker): " + " ".join(failed),
            file=sys.stderr,
        )
        return 1
    print(f"5-GC production contract: PASS (all backends {' '.join(backends)})")
    return 0


def gate_gc_longrun(args) -> int:
    out_dir = Path(
        args.out_dir or f"/tmp/pcc-gc-longrun-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    rounds = {
        "churn_rounds": args.churn_rounds,
        "gs_cycles": args.gs_cycles,
        "fin_rounds": args.fin_rounds,
        "pm_rounds": args.pm_rounds,
    }
    print("[gc_longrun] building workloads ...", flush=True)
    binaries: dict[str, Path] = {}
    for name, source, _rounds_key in _LONGRUN_WORKLOADS:
        binary = out_dir / name
        code = run_command(
            [
                *_python_prefix(),
                "-m",
                "pcc",
                "--python-libpython=off",
                "--ir-scaffold=on",
                "--backend",
                "self",
                source,
                "-o",
                str(binary),
            ]
        )
        if code != 0:
            print(f"[gc_longrun] workload build failed: {name}", file=sys.stderr)
            return code
        binaries[name] = binary

    # A crashing (workload, backend) pair must not abort the matrix: record
    # per-series exit codes in status.tsv and continue.
    status_path = out_dir / "status.tsv"
    rows: list[str] = []
    for backend in ("0", "1", "2", "3", "4"):
        for name, _source, rounds_key in _LONGRUN_WORKLOADS:
            count = str(rounds[rounds_key])
            print(f"[gc_longrun] {name} backend={backend} rounds={count}", flush=True)
            environment = _child_env()
            environment["PCC_GC_BACKEND"] = backend
            csv_path = out_dir / f"{name}.gc{backend}.csv"
            with csv_path.open("wb") as stream:
                completed = subprocess.run(
                    [str(binaries[name]), count],
                    cwd=str(ROOT),
                    env=environment,
                    stdout=stream,
                    check=False,
                )
            rows.append(f"{name}\tgc{backend}\texit={completed.returncode}")
    status_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"[gc_longrun] done — series under {out_dir} (exit codes: {status_path})")
    return 0


_GATES = {
    "b1-b6": gate_b1_b6,
    "d2-d6": gate_d2_d6,
    "final-language": gate_final_language,
    "coroutine-scheduler-roots": gate_coroutine_scheduler_roots,
    "async-gateway": gate_async_gateway,
    "goal-closure-bundle": gate_goal_closure_bundle,
    "gc-production-contract": gate_gc_production_contract,
    "gc-longrun": gate_gc_longrun,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one repository closure gate (no shell required)."
    )
    parser.add_argument("--gate", choices=sorted(_GATES))
    parser.add_argument("--list", action="store_true", help="list gate names")
    parser.add_argument("--out-dir", help="gate evidence output directory")
    parser.add_argument("--gateway", help="immutable gateway source root for async-gateway")
    parser.add_argument("--runtime-archive", help="explicit matched runtime for async-gateway")
    parser.add_argument("--source-manifest", help="exact combined source inventories")
    parser.add_argument("--threads", choices=("0", "1"))
    parser.add_argument("--refcount", choices=("atomic", "local"))
    parser.add_argument("--pcc0", help="verified host PCC command for gateway tests")
    parser.add_argument("--pcc1", help="verified native PCC command for gateway tests")
    parser.add_argument("--churn-rounds", default=200000)
    parser.add_argument("--gs-cycles", default=4000)
    parser.add_argument("--fin-rounds", default=100000)
    parser.add_argument("--pm-rounds", default=200000)
    parser.add_argument("--backends", help="gc-production-contract backend subset")
    parser.add_argument("--suite", help="gc-production-contract suite directory")
    args = parser.parse_args(argv)
    if args.list or not args.gate:
        for name in sorted(_GATES):
            print(name)
        return 0
    return _GATES[args.gate](args)


if __name__ == "__main__":
    raise SystemExit(main())
