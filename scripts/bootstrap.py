#!/usr/bin/env python3
"""Portable self-host bootstrap: host pcc0 -> pcc1 -> pcc2 -> pcc3.

Usage:
  scripts/bootstrap.py [--stage N] [--from-stage N] [--reuse-stage1]
                       [--stage1-checkpoint DIR] [--out-dir DIR] [--clean]

  scripts/bootstrap.py --stage 1
      Build pcc1.  ``--stage 3 --reuse-stage1`` reuses an existing
      OUT_DIR/pcc1 and runs only stage2/stage3 + the fixed-point check.
  scripts/bootstrap.py --from-stage 3 --stage 3 --reuse-stage1
      Run only stage3 + the pcc2/pcc3 comparison against an existing pcc2.
  scripts/bootstrap.py --clean
      Remove every stage artifact under the output directory.

Runtime defaults for every stage:
  PCC_BOOTSTRAP_RUNTIME_CC=pcc
  PCC_BOOTSTRAP_RUNTIME_HIGH=py
  PCC_BOOTSTRAP_PYTHON_LIBPYTHON=off
  PCC_BOOTSTRAP_PYTHON_IR_PASSES=${PCC_PYTHON_IR_PASSES:-off}
  PCC_BOOTSTRAP_PY_FRONTEND_JOBS=${PCC_PY_FRONTEND_JOBS:-auto} for stage2+
  PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS=${PCC_PY_FRONTEND_JOBS:-auto}
  PCC_BOOTSTRAP_SELF_BACKEND_JOBS=${PCC_SELF_BACKEND_JOBS:-2}
  PCC_BOOTSTRAP_MACHO_LINK_JOBS=${PCC_MACHO_LINK_JOBS:-8}
  PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES=17179869184
  PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES=17179869184 (when no explicit cap)
  PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES=1
  PCC_BOOTSTRAP_STAGE_TIMEOUT=1800

This is the whole build entry in Python (it replaces ``bootstrap.sh``): no
bash/sh, no ``uname``, no ``codesign``, no ``cmp``, no ``stat``, no external
tool of any kind.  The owned ``self`` backend is the only backend: the
previous shell default silently selected the LLVM oracle on anything that was
not Darwin/arm64, and the oracle route is gone with the llvmlite dependency
(``--backend llvm`` now fails closed).  The native execution gate (run the
freshly built compiler, then compile and run a smoke program with it) runs on
every host, and the pcc2/pcc3 fixed-point check requires byte-identical
images on every platform and format -- no metadata-normalized acceptance.

The coordinator's Stage2+ auto default keeps the combined codegen pool at
two workers.  After it exits, deferred frontend-only workers use their
indexed AST memory estimates within the same tree budget.  A numeric override
above two requires PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS=1; ordinary agents,
tests and performance runners must never set that escape hatch.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAIN_PY = ROOT / "pcc" / "__main__.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_GIB = 1024 * 1024 * 1024
SAFE_MAX_JOBS = 2
SAFE_MAX_LINK_JOBS = 8
SAFE_MAX_TREE_RSS_BYTES = 16 * _GIB
# Permit the explicit two-hour CI Stage1 allowance without changing the
# local default or any memory limit (scripts/ci_worker_size_prior_gate.py).
SAFE_MAX_STAGE_TIMEOUT = 7200
SAFE_MAX_HOST_MEMORY_RESERVE_BYTES = 8 * _GIB

_USAGE = __doc__ or ""


class BootstrapError(RuntimeError):
    """A bootstrap usage/validation failure with a process exit code."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _configured_host_memory_reserve_bytes(environment):
    from scripts.run_process_tree_sample import (
        ProcessTreeSampleError,
        configured_host_memory_reserve_bytes,
    )

    try:
        return configured_host_memory_reserve_bytes(environment)
    except ProcessTreeSampleError as exc:
        raise BootstrapError(str(exc)) from exc


class Options:
    """Resolved bootstrap settings (environment defaults + CLI overrides)."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = dict(env)
        self.repo_root = ROOT
        self.out_dir = Path(
            env.get("PCC_BOOTSTRAP_OUT_DIR") or (ROOT / "build" / "bootstrap")
        )
        self.runtime_cc = env.get("PCC_BOOTSTRAP_RUNTIME_CC") or "pcc"
        self.runtime_high = env.get("PCC_BOOTSTRAP_RUNTIME_HIGH") or "py"
        self.python_libpython = env.get("PCC_BOOTSTRAP_PYTHON_LIBPYTHON") or "off"
        self.python_ir_passes = (
            env.get("PCC_BOOTSTRAP_PYTHON_IR_PASSES")
            or env.get("PCC_PYTHON_IR_PASSES")
            or "off"
        )
        self.py_frontend_jobs = (
            env.get("PCC_BOOTSTRAP_PY_FRONTEND_JOBS")
            or env.get("PCC_PY_FRONTEND_JOBS")
            or "auto"
        )
        self.stage1_py_frontend_jobs = (
            env.get("PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS")
            or env.get("PCC_PY_FRONTEND_JOBS")
            or "auto"
        )
        self.self_backend_jobs = (
            env.get("PCC_BOOTSTRAP_SELF_BACKEND_JOBS")
            or env.get("PCC_SELF_BACKEND_JOBS")
            or "2"
        )
        self.macho_link_jobs = (
            env.get("PCC_BOOTSTRAP_MACHO_LINK_JOBS")
            or env.get("PCC_MACHO_LINK_JOBS")
            or "8"
        )
        # ``auto`` for these two lanes means the documented safe default; the
        # frontend lane keeps ``auto`` for the compiler to resolve.
        if self.self_backend_jobs == "auto":
            self.self_backend_jobs = "2"
        if self.macho_link_jobs == "auto":
            self.macho_link_jobs = "8"
        self.max_tree_rss_bytes = _env_int(
            env, "PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES", SAFE_MAX_TREE_RSS_BYTES
        )
        self.requested_tree_rss_bytes = (
            self.max_tree_rss_bytes if env.get("PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES") else 0
        )
        self.auto_tree_rss_ceiling_bytes = _env_int(
            env, "PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES", SAFE_MAX_TREE_RSS_BYTES
        )
        self.min_tree_rss_bytes = _env_int(env, "PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES", 1)
        self.memory_budget_selection = {}
        self.stage_timeout = _env_int(env, "PCC_BOOTSTRAP_STAGE_TIMEOUT", 1800)
        self.smoke_refcount_audit = (
            env.get("PCC_BOOTSTRAP_SMOKE_REFCOUNT_AUDIT") or "1"
        )
        self.smoke_refcount_probe_mode = (
            env.get("PCC_BOOTSTRAP_SMOKE_REFCOUNT_PROBE_MODE") or "2"
        )
        self.host_memory_reserve_bytes = _configured_host_memory_reserve_bytes(env)
        self.external_memory_guard = (
            env.get("PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD") or "0"
        )
        self.in_process_codegen = env.get("PCC_BOOTSTRAP_IN_PROCESS_CODEGEN") or "0"
        self.defer_frontend_codegen = (
            env.get("PCC_BOOTSTRAP_DEFER_FRONTEND_CODEGEN") or "1"
        )
        self.defer_self_link = env.get("PCC_BOOTSTRAP_DEFER_SELF_LINK") or "1"
        self.profile_dir = env.get("PCC_BOOTSTRAP_PROFILE_DIR") or ""
        self.stage_exec_delay = env.get("PCC_BOOTSTRAP_STAGE_EXEC_DELAY") or "0.10"
        self.keep_process_samples = bool(
            env.get("PCC_BOOTSTRAP_KEEP_PROCESS_SAMPLES")
        )
        self.reuse_stage1 = _is_truthy(env.get("PCC_BOOTSTRAP_REUSE_STAGE1", "0"))
        self.backend = "self"
        self.backend_explicit = False
        self.start_stage = 1
        self.stage_limit = 3
        self.clean = False
        self.stage1_checkpoint = None
        self.checkpoint_attempt = None
        self.checkpoint_guard_dir = None
        self.executable_suffix = ".exe" if os.name == "nt" else ""

    def stage_output(self, stage: int) -> Path:
        return self.out_dir / ("pcc" + str(stage) + self.executable_suffix)

    def child_env(self) -> dict[str, str]:
        """Environment for child processes: never inherit LC_ALL."""

        environment = dict(self.env)
        environment.pop("LC_ALL", None)
        return environment


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise BootstrapError(f"invalid bootstrap resource limit: {key}={raw}") from exc


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _validate_worker_budget(label: str, value: str, safe_max: int, env) -> None:
    # Mirrors the shell guard: only a numeric value above the safe maximum is
    # rejected.  A non-numeric value (``auto``) is the compiler's own choice.
    if value.isdigit() and int(value) > safe_max and env.get(
        "PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS"
    ) != "1":
        raise BootstrapError(
            f"unsafe bootstrap worker budget: {label}={value} exceeds {safe_max}; "
            "set PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS=1 only for an explicitly "
            "isolated machine"
        )


def _validate_resource_limit(
    label: str, value: int, safe_max: int, env
) -> None:
    if value < 1:
        raise BootstrapError(f"invalid bootstrap resource limit: {label}={value}")
    if value > safe_max and env.get("PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS") != "1":
        raise BootstrapError(
            f"unsafe bootstrap resource limit: {label}={value} exceeds {safe_max}"
        )


def validate_settings(options: Options) -> Options:
    """Fail closed before any compilation starts (also on ``--help``)."""

    env = options.env
    _validate_worker_budget(
        "PCC_BOOTSTRAP_PY_FRONTEND_JOBS", options.py_frontend_jobs, SAFE_MAX_JOBS, env
    )
    _validate_worker_budget(
        "PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS",
        options.stage1_py_frontend_jobs,
        SAFE_MAX_JOBS,
        env,
    )
    _validate_worker_budget(
        "PCC_SELF_BACKEND_JOBS", options.self_backend_jobs, SAFE_MAX_JOBS, env
    )
    _validate_worker_budget(
        "PCC_MACHO_LINK_JOBS", options.macho_link_jobs, SAFE_MAX_LINK_JOBS, env
    )
    _validate_resource_limit(
        "PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES",
        options.max_tree_rss_bytes,
        SAFE_MAX_TREE_RSS_BYTES,
        env,
    )
    _validate_resource_limit(
        "PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES",
        options.auto_tree_rss_ceiling_bytes,
        SAFE_MAX_TREE_RSS_BYTES,
        env,
    )
    if not 0 < options.min_tree_rss_bytes <= options.auto_tree_rss_ceiling_bytes:
        raise BootstrapError("minimum bootstrap tree budget must fit the automatic ceiling")
    _validate_resource_limit(
        "PCC_BOOTSTRAP_STAGE_TIMEOUT",
        options.stage_timeout,
        SAFE_MAX_STAGE_TIMEOUT,
        env,
    )
    _validate_resource_limit(
        "PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES",
        options.host_memory_reserve_bytes,
        SAFE_MAX_HOST_MEMORY_RESERVE_BYTES,
        env,
    )
    return options


def parse_args(argv: list[str], options: Options) -> Options:
    parser = argparse.ArgumentParser(
        prog="scripts/bootstrap.py",
        description="Portable self-host bootstrap: pcc0 -> pcc1 -> pcc2 -> pcc3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_USAGE.split("Runtime defaults", 1)[0],
    )
    parser.add_argument("--stage", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument(
        "--from-stage", "--start-stage", dest="start_stage", type=int, choices=(1, 2, 3),
        default=1,
    )
    parser.add_argument("--reuse-stage1", action="store_true")
    parser.add_argument(
        "--stage1-checkpoint", metavar="DIR",
        help="durably checkpoint host Stage1 objects; resume only an identical build",
    )
    parser.add_argument(
        "--backend",
        choices=("self",),
        default=None,
        help=(
            "owned backend; only 'self' exists. The LLVM oracle route was "
            "removed when llvmlite stopped being a dependency, so passing "
            "'llvm' fails here instead of silently selecting a dead path."
        ),
    )
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)

    options.stage_limit = args.stage
    options.start_stage = args.start_stage
    options.clean = args.clean
    if args.out_dir:
        options.out_dir = Path(args.out_dir)
    if args.reuse_stage1:
        options.reuse_stage1 = True
    if args.stage1_checkpoint:
        options.stage1_checkpoint = Path(args.stage1_checkpoint)
        if options.reuse_stage1 or options.start_stage != 1 or options.clean:
            raise BootstrapError(
                "--stage1-checkpoint cannot combine with --reuse-stage1, "
                "--clean or --from-stage > 1"
            )
    if args.backend is not None:
        options.backend = args.backend
        options.backend_explicit = True
    return options


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def banner(title: str) -> None:
    print("")
    print("=" * 62)
    print(" " + title)
    print("=" * 62)


def _children_cpu_seconds() -> float | None:
    """CPU time of reaped children, or None where ``resource`` is absent."""

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime) + float(usage.ru_stime)


def _children_cpu_split() -> tuple[float, float] | None:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime), float(usage.ru_stime)


def cache_identity_environment(options: Options, environment: dict[str, str]) -> None:
    """Derive content-addressed cache namespaces when the caller did not."""

    if (
        environment.get("PCC_PY_FRONTEND_IR_CACHE_IDENTITY")
        and environment.get("PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY")
    ):
        pass
    else:
        command = ["-m", "pcc.driver.bootstrap_cache_identity"]
        python = _host_python_command()
        completed = subprocess.run(
            [*python, *command],
            cwd=str(ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = completed.stdout.splitlines() if completed.returncode == 0 else []
        if len(lines) >= 2 and lines[0] and lines[1]:
            environment.setdefault("PCC_PY_FRONTEND_IR_CACHE_IDENTITY", lines[0])
            environment.setdefault("PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY", lines[1])
    environment.setdefault(
        "PCC_SELF_BACKEND_OBJECT_CACHE_DIR",
        str(ROOT / "build" / "bootstrap-pytest-object-cache"),
    )


def _host_python_command() -> list[str]:
    """The interpreter used for stage1 / host-side helpers."""

    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "python"]
    return [sys.executable]


def stage_environment(stage: int, options: Options) -> dict[str, str]:
    environment = options.child_env()
    if stage != 1 or options.checkpoint_attempt is None:
        environment = _without_stage1_checkpoint(environment)
    frontend_jobs = (
        options.stage1_py_frontend_jobs
        if stage == 1
        else options.py_frontend_jobs
    )
    environment.update(
        {
            "PCC_RUNTIME_CC": options.runtime_cc,
            "PCC_RUNTIME_HIGH": options.runtime_high,
            "PCC_PYTHON_IR_PASSES": options.python_ir_passes,
            "PCC_PY_FRONTEND_JOBS": frontend_jobs,
            "PCC_SELF_BACKEND_JOBS": options.self_backend_jobs,
            "PCC_MACHO_LINK_JOBS": options.macho_link_jobs,
            "PCC_WORKER_TREE_BUDGET_BYTES": str(options.max_tree_rss_bytes),
        }
    )
    indexed_emit = environment.get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    indexed_capture = environment.get("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    inline_error_edges = environment.get("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE", "1")
    environment.update(
        {
            "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": indexed_capture,
            "PCC_DIRECT_INDEXED_KERNEL_EMIT": indexed_emit,
            "PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE": inline_error_edges,
            "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
            "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
            "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
        }
    )
    return environment


def _without_stage1_checkpoint(environment: dict[str, str]) -> dict[str, str]:
    return {
        name: value for name, value in environment.items()
        if not name.startswith("PCC_STAGE1_CHECKPOINT_")
    }


def _plan_paths(stage: int, out_exe: Path, options: Options) -> tuple[str, str, str]:
    """Return ``(codegen_plan, deferred_plan, error)`` for this stage."""

    if stage == 1:
        return "", "", ""
    codegen_plan = ""
    deferred_plan = ""
    if options.defer_frontend_codegen == "1":
        codegen_plan = str(out_exe) + ".pcc-codegen-plan"
        for suffix in ("", ".internal-inputs", ".link-profile.json", ".result.json"):
            Path(codegen_plan + suffix).unlink(missing_ok=True)
    if options.defer_self_link == "1":
        deferred_plan = str(out_exe) + ".pcc-link-plan"
        for suffix in ("", ".inputs", ".profile.json", ".result.json"):
            Path(deferred_plan + suffix).unlink(missing_ok=True)
    if codegen_plan:
        for value in (
            options.child_env().get("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1"),
            options.child_env().get("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1"),
        ):
            if not _is_truthy(value):
                return (
                    codegen_plan,
                    deferred_plan,
                    "native deferred codegen requires "
                    "PCC_DIRECT_INDEXED_KERNEL_CAPTURE=1 and "
                    "PCC_DIRECT_INDEXED_KERNEL_EMIT=1",
                )
    return codegen_plan, deferred_plan, ""


_OWNERSHIP_FAULT_MESSAGES = (
    "refcount operation on an unmanaged pointer",
    "object cell freed twice",
)


def audit_rerun_returncode(
    returncode: int, stderr_text: str, *, audit: str = "1"
) -> int:
    """Reclassify an otherwise-green smoke that reported an ownership fault.

    A segfault or link failure keeps its own code: those are a different class
    from the refcount-provenance audit, and stage gating tells them apart by
    exit code.  A passing smoke that reported a fault becomes 97.
    """

    if audit == "0":
        return returncode
    if any(message in stderr_text for message in _OWNERSHIP_FAULT_MESSAGES):
        return 97 if returncode == 0 else returncode
    return returncode


def _smoke_runtime_archive(options: Options) -> str:
    """The runtime archive the native smoke compile must link against.

    A compiled stage binary cannot rebuild the runtime archive (the rebuild
    needs host Python), so it must be handed an explicit archive: without one
    the native compiler treats the archive as unproved, enters the
    ``PCC_RUNTIME_CC``/host-Python rebuild path, and fails closed with
    "no-libpython function unavailable: ..._acquire_runtime_build_lock" —
    even when the archive on disk is current for the host.  That made the
    stage barrier depend on the archive's freshness state instead of on the
    freshly built compiler.
    """

    explicit = (options.env.get("PCC_RUNTIME_ARCHIVE") or "").strip()
    if explicit:
        return explicit
    candidates = []
    runtime_dir = (options.env.get("PCC_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "libpy_runtime_pcc_py.a")
    candidates.append(ROOT / "pcc" / "runtime" / "libpy_runtime_pcc_py.a")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def stage_exec_barrier(out_exe: Path, stage: int, options: Options) -> int:
    """Native execution gate: run the built compiler and a smoke program.

    The gate is not Darwin-only.  Every host runs ``--help`` and then compiles
    and executes a smoke program with the freshly built compiler; on Darwin the
    previous implementation additionally waited for the code-signature cache,
    which is now only the short delay below.
    """

    subprocess.run(
        [str(out_exe), "--help"],
        cwd=str(ROOT),
        env=_without_stage1_checkpoint(options.child_env()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if options.stage_exec_delay and options.stage_exec_delay != "0":
        time.sleep(float(options.stage_exec_delay))
    smoke_dir = Path(tempfile.mkdtemp(prefix="stage-smoke.", dir=str(options.out_dir)))
    try:
        smoke_src = smoke_dir / "smoke.py"
        smoke_out = smoke_dir / "smoke"
        smoke_err = smoke_dir / "smoke.stderr"
        smoke_src.write_text(
            "def main() -> int:\n    return 0\n\nmain()\n", encoding="utf-8"
        )
        # The smoke compile doubles as a refcount-provenance audit: in probe
        # mode 2 the runtime reports the first refcount operation that reaches
        # a pointer which is not a managed object (a freed cell, a raw value).
        # Without it the stage's own compile would still "pass" and corrupt the
        # object free list much later in the allocator.  Set
        # PCC_BOOTSTRAP_SMOKE_REFCOUNT_AUDIT=0 to run the smoke without it.
        environment = _without_stage1_checkpoint(options.child_env())
        environment.update(
            {
                "PCC_RUNTIME_CC": options.runtime_cc,
                "PCC_RUNTIME_HIGH": options.runtime_high,
                "PCC_GC_REFCOUNT_PROVENANCE_PROBE": options.smoke_refcount_probe_mode,
            }
        )
        smoke_archive = _smoke_runtime_archive(options)
        if smoke_archive:
            environment["PCC_RUNTIME_ARCHIVE"] = smoke_archive
        smoke_commands = (
            (
                "compile",
                [
                    str(out_exe),
                    "--ir-scaffold=on",
                    "--backend", options.backend,
                    "--python-libpython", options.python_libpython,
                    str(smoke_src),
                    "-o", str(smoke_out),
                ],
            ),
            ("execution", [str(smoke_out)]),
        )
        for phase, command in smoke_commands:
            try:
                with smoke_err.open("wb") as error_stream:
                    completed = subprocess.run(
                        command,
                        cwd=str(ROOT),
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=error_stream,
                        check=False,
                    )
            except OSError as error:
                print(f"stage smoke {phase} failed: {error}", file=sys.stderr)
                return 127
            returncode = completed.returncode
            stderr_text = ""
            try:
                stderr_text = smoke_err.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stderr_text = ""
            if options.smoke_refcount_audit != "0" and any(
                message in stderr_text for message in _OWNERSHIP_FAULT_MESSAGES
            ):
                print(f"stage smoke {phase}: ownership audit failed", file=sys.stderr)
                audit_lines = sorted(
                    {
                        line.replace("pcc runtime: ", "  ", 1)
                        for line in stderr_text.splitlines()
                        if line.startswith("pcc runtime: ")
                    }
                )
                for line in audit_lines:
                    print(line, file=sys.stderr)
                failing_command = out_exe if phase == "compile" else smoke_out
                print(
                    f"  rerun {failing_command} with PCC_GC_REFCOUNT_PROVENANCE_PROBE=3 "
                    "to abort at the site",
                    file=sys.stderr,
                )
            returncode = audit_rerun_returncode(
                returncode, stderr_text, audit=options.smoke_refcount_audit
            )
            if returncode != 0:
                if stderr_text:
                    print(f"stage smoke {phase} stderr:", file=sys.stderr)
                    for line in stderr_text.splitlines()[-20:]:
                        print(line, file=sys.stderr)
                return returncode
        return 0
    finally:
        shutil.rmtree(smoke_dir, ignore_errors=True)


def write_stage_result_json(
    stage: int,
    out_exe: Path,
    options: Options,
    *,
    compile_elapsed_ms: int,
    barrier_elapsed_ms: int,
    stage_elapsed_ms: int,
    returncode: int,
    barrier_returncode: int,
    cpu_split: tuple[float, float] | None,
) -> None:
    if not options.profile_dir:
        return
    path = Path(options.profile_dir) / f"stage{stage}.result.json"
    payload = {
        "schema": "pcc.bootstrap_stage_result.v1",
        "stage": int(stage),
        "output": str(out_exe),
        "backend": options.backend,
        "memory_budget_selection": options.memory_budget_selection,
        "compile_wall_ms": int(compile_elapsed_ms),
        "publish_barrier_ms": int(barrier_elapsed_ms),
        "wall_ms": int(stage_elapsed_ms),
        "returncode": int(returncode),
        "publish_barrier_returncode": int(barrier_returncode),
        "metric_scopes": {
            "compile_wall_ms": "end_to_end_elapsed",
            "compile_time_real_ms": "end_to_end_elapsed",
            "compile_user_ms": "timed_command_plus_waited_children_cpu",
            "compile_sys_ms": "timed_command_plus_waited_children_cpu",
            "publish_barrier_ms": "end_to_end_elapsed",
            "wall_ms": "end_to_end_elapsed_including_publish_barrier",
        },
        "comparison_contract": {
            "primary_compute_metrics": ["compile_user_ms", "compile_sys_ms"],
            "wall_metric_role": "paired_end_to_end_observation",
            "required_comparison": "adjacent_alternating_same_environment_pairs",
            "single_wall_verdict_allowed": False,
        },
    }
    if cpu_split is not None:
        payload["compile_user_ms"] = int(cpu_split[0] * 1000)
        payload["compile_sys_ms"] = int(cpu_split[1] * 1000)
        payload["compile_time_real_ms"] = int(compile_elapsed_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_stage_time_file(
    stage: int, options: Options, compile_elapsed_ms: int, cpu_split
) -> None:
    if not options.profile_dir:
        return
    if cpu_split is None:
        body = f"real_s={compile_elapsed_ms / 1000.0:.3f}\n"
    else:
        body = (
            f"real_s={compile_elapsed_ms / 1000.0:.3f}\n"
            f"user_s={cpu_split[0]:.3f}\n"
            f"sys_s={cpu_split[1]:.3f}\n"
        )
    path = Path(options.profile_dir) / f"stage{stage}.time"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _posix_guard_command(
    guard_dir: Path, options: Options, target: list[str]
) -> list[str]:
    command = [
        sys.executable,
        str(SCRIPTS / "run_process_tree_sample.py"),
        "--result", str(guard_dir / "result.json"),
        "--samples", str(guard_dir / "samples.tsv"),
        "--stdout", str(guard_dir / "target.stdout"),
        "--stderr", str(guard_dir / "target.stderr"),
        "--cwd", str(ROOT),
        "--timeout", str(options.stage_timeout),
        "--interval", "0.25",
        "--progress-interval", "30",
        "--no-performance-lock",
    ]
    if _guard_reselects_memory(options):
        # Final admission belongs to the enforcement process: its startup may
        # have reduced headroom since Bootstrap's planning observation.
        command += [
            "--auto-tree-rss-ceiling-bytes", str(options.max_tree_rss_bytes),
            "--min-tree-rss-bytes", str(options.min_tree_rss_bytes),
        ]
    else:
        command += ["--max-tree-rss-bytes", str(options.max_tree_rss_bytes)]
    if sys.platform == "darwin":
        command += [
            "--darwin-preflight-reserve-bytes",
            str(options.host_memory_reserve_bytes),
        ]
    command += ["--", *target]
    return command


def _guard_reselects_memory(options: Options) -> bool:
    return (sys.platform == "darwin"
            and options.stage1_checkpoint is None
            and options.memory_budget_selection.get("selection_kind") == "automatic")


def _write_memory_admission(stage: int, options: Options, status: str, **details) -> None:
    path = options.out_dir / f"stage{stage}.memory-budget.json"
    payload = {
        "schema": "pcc.bootstrap_memory_admission.v1", "stage": stage,
        "status": status, "memory_budget_selection": options.memory_budget_selection,
        **details,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_final_guard_memory_budget(
    guard_dir: Path, options: Options, stage: int, *, guard_returncode: int | None = None,
) -> None:
    path = guard_dir / "result.json"
    context = str(path)
    if guard_returncode is not None:
        context += f" (guard returncode={guard_returncode})"
    try:
        receipt = json.loads(path.read_text())
        if not isinstance(receipt, dict):
            raise ValueError("receipt must be a JSON object")
        status = receipt.get("status")
        if status in (None, "", "RUNNING"):
            raise BootstrapError(
                "incomplete final guard memory receipt: " + context
                + f": status={status!r}; guard did not publish a final result"
            )
        if not isinstance(status, str):
            raise ValueError("guard status must be a string")
        selection = receipt.get("memory_budget_selection")
        if selection is None:
            selection = {}
        if not isinstance(selection, dict):
            raise ValueError("memory budget selection must be a JSON object")
        if status == "PREFLIGHT_REJECTED":
            options.memory_budget_selection = selection
            _write_memory_admission(
                stage, options, "PREFLIGHT_REJECTED", admission_owner="process_tree_guard",
                error=receipt.get("error", ""), guard_receipt=str(path),
            )
            return
        if "max_tree_rss_bytes" not in selection:
            raise BootstrapError(
                "incomplete final guard memory receipt: " + context
                + f": status={status!r}; final memory selection has no max_tree_rss_bytes"
            )
        selected = selection["max_tree_rss_bytes"]
        if type(selected) is not int:
            raise ValueError("final automatic cap must be an integer")
        if not options.min_tree_rss_bytes <= selected <= options.max_tree_rss_bytes:
            raise ValueError("final automatic cap lies outside the admitted bounds")
    except FileNotFoundError as exc:
        raise BootstrapError("missing final guard memory receipt: " + context) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BootstrapError(
            "invalid final guard memory receipt: " + context + ": " + str(exc)
        ) from exc
    options.max_tree_rss_bytes = selected
    options.memory_budget_selection = selection
    _write_memory_admission(
        stage, options, "ADMITTED", admission_owner="process_tree_guard",
        guard_receipt=str(path), guard_status=receipt["status"],
    )


def _windows_guarded_run(
    guard_dir: Path, options: Options, target: list[str], environment: dict[str, str]
) -> int:
    """Job-object supervised run for Windows (the ``ps`` sampler is POSIX)."""

    from scripts.platform_process_watchdog import run as watchdog_run

    log_path = guard_dir / "target.log"
    status = "OK"
    record: dict[str, object] = {}
    returncode = 0
    try:
        record = watchdog_run(
            target,
            cwd=ROOT,
            env=environment,
            log_path=log_path,
            timeout=options.stage_timeout,
            rss_limit=options.max_tree_rss_bytes,
        )
        returncode = 0
    except TimeoutError:
        status, returncode = "TIMEOUT", 124
    except subprocess.CalledProcessError as exc:
        status, returncode = "FAILED", int(exc.returncode)
    except RuntimeError as exc:
        if "RSS cap" in str(exc):
            status, returncode = "MEMORY_LIMIT", 125
        else:
            raise
    (guard_dir / "result.json").write_text(
        json.dumps({"status": status, "returncode": returncode, **record}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    try:
        sys.stdout.write(log_path.read_text(encoding="utf-8", errors="replace"))
        sys.stdout.flush()
    except OSError:
        pass
    return returncode


def _run_guarded(
    target: list[str], options: Options, stage: int, environment: dict[str, str]
) -> tuple[int, Path | None]:
    if options.external_memory_guard == "1":
        return (
            subprocess.run(
                target, cwd=str(ROOT), env=environment, check=False
            ).returncode,
            None,
        )
    if options.checkpoint_guard_dir is not None:
        guard_dir = Path(options.checkpoint_guard_dir)
        guard_dir.mkdir(parents=True, exist_ok=False)
    else:
        guard_dir = Path(
            tempfile.mkdtemp(prefix=f"stage{stage}.process.", dir=str(options.out_dir))
        )
    print(
        f"process-tree guard: {guard_dir}/result.json "
        f"cap={options.max_tree_rss_bytes} timeout={options.stage_timeout}s"
    )
    if os.name == "nt":  # pragma: no cover - Windows host
        return _windows_guarded_run(guard_dir, options, target, environment), guard_dir
    if options.checkpoint_attempt is not None:
        options.checkpoint_attempt.request_guard_launch()
    completed = subprocess.run(
        _posix_guard_command(guard_dir, options, target),
        cwd=str(ROOT),
        env=environment,
        check=False,
    )
    # Replay compiler diagnostics even if the guard died before publishing its
    # final receipt. Receipt validation still fails closed after output replay.
    for name, stream in (("target.stdout", sys.stdout), ("target.stderr", sys.stderr)):
        path = guard_dir / name
        if path.is_file() and path.stat().st_size:
            stream.write(path.read_text(encoding="utf-8", errors="replace"))
            stream.flush()
    if _guard_reselects_memory(options):
        _read_final_guard_memory_budget(
            guard_dir, options, stage, guard_returncode=completed.returncode,
        )
    return completed.returncode, guard_dir


def _resolve_tree_memory_budget(options: Options) -> None:
    from scripts.run_process_tree_sample import (
        ProcessTreeSampleError,
        select_tree_memory_budget,
    )

    external = options.env.get("PCC_WORKER_TREE_BUDGET_BYTES")
    if options.external_memory_guard == "1" and not external and not options.requested_tree_rss_bytes:
        raise BootstrapError("external memory guard needs an explicit or inherited tree cap")
    try:
        selection = select_tree_memory_budget(
            options.requested_tree_rss_bytes,
            default_ceiling=options.auto_tree_rss_ceiling_bytes,
            minimum_bytes=options.min_tree_rss_bytes,
            reserve_bytes=options.host_memory_reserve_bytes, external_budget=external,
        )
    except (ProcessTreeSampleError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        options.memory_budget_selection = getattr(exc, "memory_budget_selection", None) or {}
        raise BootstrapError(str(exc)) from exc
    selection["external_memory_guard"] = options.external_memory_guard == "1"
    options.memory_budget_selection = selection
    options.max_tree_rss_bytes = selection["max_tree_rss_bytes"]
    validate_settings(options)
    print("PCC_BOOTSTRAP_MEMORY_BUDGET " + json.dumps(selection, sort_keys=True))


def run_stage(stage: int, out_exe: Path, cmd: list[str], options: Options) -> None:
    """Compile one stage, gate it natively, and report its receipt."""

    # Preserve launch admission evidence even when no compiler child can start.
    try:
        _resolve_tree_memory_budget(options)
    except BootstrapError as exc:
        _write_memory_admission(
            stage, options, "PREFLIGHT_REJECTED", admission_owner="bootstrap", error=str(exc),
        )
        raise
    _write_memory_admission(stage, options, "ADMITTED", admission_owner="bootstrap")
    if stage == 1 and options.stage1_checkpoint is not None:
        from scripts.bootstrap_stage1_checkpoint import (
            CheckpointError,
            run_checkpointed_stage,
        )
        try:
            return run_checkpointed_stage(stage, out_exe, cmd, options, _run_stage)
        except (CheckpointError, OSError, subprocess.TimeoutExpired) as exc:
            raise BootstrapError("Stage1 checkpoint: " + str(exc)) from exc
    return _run_stage(stage, out_exe, cmd, options)


def _run_stage(stage: int, out_exe: Path, cmd: list[str], options: Options) -> None:

    if options.profile_dir:
        Path(options.profile_dir).mkdir(parents=True, exist_ok=True)
    # Never let a failed/short-circuited compile leave a previous run's stage
    # binary in place; stage3 must not execute a stale pcc2.
    out_exe.unlink(missing_ok=True)
    Path(str(out_exe) + ".tmp").unlink(missing_ok=True)

    codegen_plan, deferred_plan, plan_error = _plan_paths(stage, out_exe, options)
    if plan_error:
        raise BootstrapError(plan_error)

    environment = stage_environment(stage, options)
    backend_args = ["--backend", options.backend] if options.backend else []
    backend_label = options.backend if options.backend_explicit else f"{options.backend} (default)"
    frontend_jobs = (
        options.stage1_py_frontend_jobs if stage == 1 else options.py_frontend_jobs
    )

    command = list(cmd)
    if options.profile_dir:
        command += ["--profile-json", str(Path(options.profile_dir) / f"stage{stage}.json")]
    command += backend_args
    command += [
        "--python-libpython", options.python_libpython,
        str(MAIN_PY), "-o", str(out_exe),
    ]

    if stage != 1:
        if options.in_process_codegen == "1":
            environment["PCC_PY_FRONTEND_IN_PROCESS_CODEGEN"] = "1"
        if codegen_plan:
            runtime_archive = environment.get(
                "PCC_RUNTIME_ARCHIVE",
                str(ROOT / "pcc" / "runtime" / "libpy_runtime_pcc_py.a"),
            )
            if not Path(runtime_archive).is_file():
                raise BootstrapError(
                    "native deferred codegen requires a runtime archive before "
                    "compilation; build stage1 or set PCC_RUNTIME_ARCHIVE to its "
                    "runtime archive"
                )
            environment["PCC_RUNTIME_ARCHIVE"] = runtime_archive
            environment["PCC_DEFER_FRONTEND_CODEGEN_PLAN"] = codegen_plan
            environment["PCC_DEFER_FRONTEND_OUTPUT"] = str(out_exe)
        if deferred_plan:
            environment["PCC_DEFER_SELF_LINK_PLAN"] = deferred_plan

    target = command
    if deferred_plan or codegen_plan:
        target = [
            sys.executable,
            str(SCRIPTS / "run_pcc_native_deferred.py"),
            command[0],
            codegen_plan,
            deferred_plan,
            "--",
            *command,
        ]

    banner(f"stage {stage}: backend {backend_label}: {' '.join(cmd)}")
    print(f"input: {MAIN_PY}")
    print(f"output: {out_exe}")
    print(
        "runtime: "
        f"PCC_RUNTIME_CC={options.runtime_cc} "
        f"PCC_RUNTIME_HIGH={options.runtime_high} "
        f"PCC_PYTHON_IR_PASSES={options.python_ir_passes} "
        f"PCC_PY_FRONTEND_JOBS={frontend_jobs} "
        f"PCC_SELF_BACKEND_JOBS={options.self_backend_jobs} "
        f"PCC_MACHO_LINK_JOBS={options.macho_link_jobs} "
        f"--python-libpython {options.python_libpython}"
    )
    if options.profile_dir:
        print(f"profile: {options.profile_dir}/stage{stage}.json")

    stage_start_ms = now_ms()
    compile_start_ms = stage_start_ms
    cpu_before = _children_cpu_split()
    stage_returncode, guard_dir = _run_guarded(target, options, stage, environment)
    compile_end_ms = now_ms()
    cpu_after = _children_cpu_split()
    cpu_split = None
    if cpu_before is not None and cpu_after is not None:
        cpu_split = (
            max(0.0, cpu_after[0] - cpu_before[0]),
            max(0.0, cpu_after[1] - cpu_before[1]),
        )

    compile_elapsed_ms = compile_end_ms - compile_start_ms
    barrier_start_ms = compile_end_ms
    if stage_returncode == 0:
        if not out_exe.is_file() or out_exe.stat().st_size == 0:
            print(
                f"FAIL — stage {stage} did not produce executable {out_exe}; "
                "refusing stale stage artifact.",
                file=sys.stderr,
            )
            stage_returncode = 127
        elif os.name != "nt" and not os.access(out_exe, os.X_OK):
            print(
                f"FAIL — stage {stage} did not produce executable {out_exe}; "
                "refusing stale stage artifact.",
                file=sys.stderr,
            )
            stage_returncode = 127
    barrier_returncode = 0
    if stage_returncode == 0:
        if options.checkpoint_attempt is not None:
            options.checkpoint_attempt.verify_inputs()
        barrier_returncode = stage_exec_barrier(out_exe, stage, options)
        if barrier_returncode != 0:
            stage_returncode = barrier_returncode
        elif options.checkpoint_attempt is not None:
            options.checkpoint_attempt.verify_inputs()
    barrier_end_ms = now_ms()
    barrier_elapsed_ms = barrier_end_ms - barrier_start_ms
    stage_elapsed_ms = barrier_end_ms - stage_start_ms

    _write_stage_time_file(stage, options, compile_elapsed_ms, cpu_split)
    write_stage_result_json(
        stage,
        out_exe,
        options,
        compile_elapsed_ms=compile_elapsed_ms,
        barrier_elapsed_ms=barrier_elapsed_ms,
        stage_elapsed_ms=stage_elapsed_ms,
        returncode=stage_returncode,
        barrier_returncode=barrier_returncode,
        cpu_split=cpu_split,
    )

    # A failed stage must not emit a success-shaped result line: anyone
    # grepping PCC_BOOTSTRAP_STAGE_RESULT reads a timing and an output path.
    if stage_returncode != 0:
        # Keep the process-tree sample on failure: result.json/samples.tsv are
        # the only record of what the killed stage was doing.
        print(
            f"PCC_BOOTSTRAP_STAGE_FAILED stage={stage} "
            f"elapsed_ms={stage_elapsed_ms} rc={stage_returncode} output=<none>"
        )
        raise BootstrapError("<stage failed>", exit_code=int(stage_returncode))
    if guard_dir is not None and not options.keep_process_samples:
        shutil.rmtree(guard_dir, ignore_errors=True)
    if not out_exe.is_file() or out_exe.stat().st_size == 0:
        print(
            f"PCC_BOOTSTRAP_STAGE_FAILED stage={stage} "
            f"elapsed_ms={stage_elapsed_ms} rc=0 output=<missing:{out_exe}>"
        )
        raise BootstrapError("<stage produced no artifact>", exit_code=1)
    if options.checkpoint_attempt is not None:
        # Checkpoint success is published only after durable module validation,
        # immutable attempt accounting and the final receipt have all succeeded.
        return
    print(
        f"PCC_BOOTSTRAP_STAGE_RESULT stage={stage} "
        f"elapsed_ms={stage_elapsed_ms} output={out_exe} rc=0"
    )
    # Wall time alone cannot distinguish "the compiler did more work" from "the
    # host was busy".  Print the timed-command CPU next to it so a stage1
    # comparison is diagnosable without a profile directory.
    if cpu_split is not None:
        user_ms = int(cpu_split[0] * 1000)
        sys_ms = int(cpu_split[1] * 1000)
        print(
            f"PCC_BOOTSTRAP_STAGE_CPU stage={stage} user_ms={user_ms} "
            f"sys_ms={sys_ms} cpu_ms={user_ms + sys_ms} "
            f"wall_ms={compile_elapsed_ms} "
            f"driver_and_barrier_ms={stage_elapsed_ms - compile_elapsed_ms}"
        )


def verify_fixed_point(options: Options) -> int:
    """Require pcc2 == pcc3 byte-for-byte, on every platform and image format.

    There is no metadata-normalized acceptance path: an ELF build-id note, a
    PE timestamp/checksum or a Mach-O UUID difference is a real difference in
    artifact identity, so a normalized "pass" would be a weaker claim than the
    gate advertises.  A byte mismatch is classified for diagnosis -- size drift
    (structural) versus same-size drift (metadata suspected) -- and both fail.
    """

    second = options.stage_output(2)
    third = options.stage_output(3)
    banner("verify: compare pcc2 pcc3")
    left = second.read_bytes()
    right = third.read_bytes()
    if left == right:
        print("OK — pcc2 and pcc3 are byte-identical. Self-host gate passed.")
        return 0
    print(f"pcc2 size: {len(left)}")
    print(f"pcc3 size: {len(right)}")
    if len(left) != len(right):
        print(
            "FAIL — pcc2 / pcc3 differ in size: the stages are not "
            "reproducing each other.",
            file=sys.stderr,
        )
        return 1
    print(
        "FAIL — pcc2 / pcc3 have equal size but differ in bytes; classify the "
        "drift (embedded uuid / signature / timestamp / build-id), then make "
        "the emission deterministic -- there is no normalized acceptance path.",
        file=sys.stderr,
    )
    return 2


def run_chain(options: Options) -> int:
    from scripts.file_lock import exclusive_file_lock

    # Receipt-bound callers already supervise and lock the complete tree.
    guard = contextlib.nullcontext() if options.external_memory_guard == "1" else exclusive_file_lock(ROOT / "build" / ".pcc-performance.lock", blocking=False)
    with guard:
        return _run_locked_chain(options)


def _run_locked_chain(options: Options) -> int:
    options.out_dir.mkdir(parents=True, exist_ok=True)
    environment = options.child_env()
    cache_identity_environment(options, environment)
    options.env = environment

    if options.start_stage <= 1 <= options.stage_limit:
        pcc1 = options.stage_output(1)
        if options.reuse_stage1 and pcc1.is_file() and pcc1.stat().st_size:
            banner(f"stage 1: reuse existing {pcc1} (--reuse-stage1)")
            print(f"PCC_BOOTSTRAP_STAGE_RESULT stage=1 elapsed_ms=0 output={pcc1} reused=1")
        else:
            run_stage(1, pcc1, [*_host_python_command(), "-m", "pcc"], options)

    if options.start_stage <= 2 <= options.stage_limit:
        run_stage(2, options.stage_output(2), [str(options.stage_output(1))], options)

    if options.start_stage <= 3 <= options.stage_limit:
        run_stage(3, options.stage_output(3), [str(options.stage_output(2))], options)
        return verify_fixed_point(options)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Stage output is watched live (CI logs, long local runs): keep the banner
    # and receipt lines visible while the guarded child is still sampling.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):  # pragma: no cover - exotic stream
            pass
    try:
        options = validate_settings(Options(dict(os.environ)))
        options = parse_args(argv, options)
        if options.clean:
            shutil.rmtree(options.out_dir, ignore_errors=True)
            print(f"cleaned {options.out_dir}")
            return 0
        return run_chain(options)
    except BootstrapError as exc:
        if str(exc) != "<stage failed>" and str(exc) != "<stage produced no artifact>":
            print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
