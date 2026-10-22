"""Host-only identity, durable attempts and accounting for Stage1 checkpoints.

The compiler's receipt codec deliberately does not import this module.  Input
files must remain frozen throughout each attempt; endpoint validation detects
drift, but cannot prove that a file was not changed and restored in between.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid


BUILD_SCHEMA = "pcc.stage1-checkpoint.build.v1"
TARGET = "x86_64-unknown-linux-gnu"
PREFIX = "PCC_STAGE1_CHECKPOINT_"
BYTECODE_POLICY = "fresh-empty-pycache-prefix-with-writes-disabled.v1"
# Only settings with a bounded, non-secret purpose are recorded in clear text.
# Unknown PCC variables still invalidate identity, without persisting values.
SAFE_SETTINGS = frozenset({
    "PCC_RUNTIME_CC", "PCC_RUNTIME_HIGH", "PCC_RUNTIME_BUILD",
    "PCC_PYTHON_IR_PASSES", "PCC_PY_FRONTEND_JOBS", "PCC_SELF_BACKEND_JOBS",
    "PCC_MACHO_LINK_JOBS", "PCC_WORKER_TREE_BUDGET_BYTES", "PCC_GC_BACKEND",
    "PCC_REFCOUNT_KIND", "PCC_WITH_THREADS", "PCC_IR_SCAFFOLD",
    "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
    "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
    "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
})
NONSEMANTIC_SETTINGS = frozenset({
    "PCC_BOOTSTRAP_OUT_DIR", "PCC_BOOTSTRAP_PROFILE_DIR",
    "PCC_BOOTSTRAP_KEEP_PROCESS_SAMPLES", "PCC_PROFILE_JSON",
})
# Proven diagnostic writers, not build inputs. Keep their destination settings
# in effective_config, but never inventory bytes created/updated at the paths.
OUTPUT_PATH_SETTINGS = frozenset({
    "PCC_LOG_FILE",  # pipeline_dependency_closure._pcc_emit_import_log (append)
    "PCC_HOIST_PROFILE_PATH",  # codegen.hoist_analysis.write_hoist_profile (write)
    "PCC_COMPILE_PROGRESS_FILE",  # worker/function markers append PID suffixes
    "PCC_STMT_PROGRESS_FILE",  # user_function_lowering._prologue_mark (PID suffix)
    "PCC_PYTHON_IR_PASS_TELEMETRY_PATH",  # ir_pass_pipeline.run_python_ir_pass_pipeline
    "PCC_PASSES_EXPLAIN_PATH",  # diagnostics.wiring._write_optional_report
})
PYTHON_SETTINGS = (
    "PYTHONHASHSEED", "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8",
    "PYTHONNOUSERSITE", "PYTHONSAFEPATH", "PYTHONOPTIMIZE", "PYTHONWARNINGS",
    "LANG", "LC_CTYPE", "LC_COLLATE", "LC_TIME", "TZ", "SOURCE_DATE_EPOCH",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONDONTWRITEBYTECODE",
)
_EXCLUDED_DIRS = frozenset({
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "build", "build_owned", "build_py", "build_pcc", "build_libpython",
})
_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".c", ".h", ".cc", ".cpp", ".hpp", ".s", ".S",
    ".inc", ".def", ".json", ".toml", ".yaml", ".yml", ".txt", ".cfg",
    ".ini", ".csv", ".tsv", ".typed", ".pth", ".zip", ".so",
})


class CheckpointError(ValueError):
    """An unsupported route, changed identity or incomplete durable write."""


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("duplicate checkpoint JSON field: " + key)
        result[key] = value
    return result


def read_json(path: Path):
    def invalid_constant(value):
        raise CheckpointError("non-finite checkpoint JSON number")
    try:
        return json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_json_pairs, parse_constant=invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckpointError("cannot read checkpoint JSON: " + str(path)) from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_immutable_json(path: Path, payload) -> None:
    """Publish once; an existing receipt is never replaced, even on a retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def file_identity(path: Path, *, label: str | None = None) -> dict:
    path = Path(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise CheckpointError("checkpoint input is not a regular file: " + str(path))
    before = resolved.stat()
    hasher = hashlib.sha256()
    with resolved.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    after = resolved.stat()
    if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns,
        before.st_ctime_ns) != (after.st_ino, after.st_dev, after.st_size,
                              after.st_mtime_ns, after.st_ctime_ns):
        raise CheckpointError("checkpoint input changed while hashing: " + str(path))
    return {"path": label if label is not None else str(path.absolute()),
            "realpath": str(resolved), "kind": "symlink" if path.is_symlink() else "file",
            "size": after.st_size, "sha256": hasher.hexdigest()}


def source_inventory(root: Path) -> list[dict]:
    """Cover compiler, all backends/runtimes, stdlib, helpers and config inputs."""
    records = []
    for name in ("pcc", "scripts", "utils"):
        directory = root / name
        if not directory.is_dir():
            continue
        for parent, directories, files in os.walk(directory):
            # A symlinked directory could conceal an unrecorded import closure.
            for child in directories:
                if (Path(parent) / child).is_symlink():
                    raise CheckpointError("symlinked source directory is unsupported: " + str(Path(parent) / child))
            directories[:] = sorted(d for d in directories if d not in _EXCLUDED_DIRS)
            for filename in sorted(files):
                path = Path(parent) / filename
                if path.suffix == ".pyc" and not path.with_suffix(".py").is_file():
                    raise CheckpointError("sourceless bytecode source is unsupported: " + str(path))
                if path.suffix in _SOURCE_SUFFIXES or filename in ("Makefile", "CMakeLists.txt"):
                    records.append(file_identity(path, label=str(path.relative_to(root))))
    for filename in ("pyproject.toml", "uv.lock", ".python-version", "hatch_build.py", "run.py"):
        path = root / filename
        if path.exists():
            records.append(file_identity(path, label=filename))
    return sorted(records, key=lambda row: row["path"])


def settings_identity(environment: dict[str, str]) -> dict:
    values = {}
    for name in sorted(environment):
        if not name.startswith("PCC_") or name.startswith(PREFIX) or name in NONSEMANTIC_SETTINGS:
            continue
        value = environment[name]
        values[name] = ({"value": value} if name in SAFE_SETTINGS else
                        {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()})
    return {"pcc": values, "process": {
        name: None if name not in environment else {
            "sha256": hashlib.sha256(environment[name].encode("utf-8")).hexdigest()
        } for name in PYTHON_SETTINGS
    }}


def recorded_guard_environment(environment: dict[str, str]) -> dict[str, str]:
    """Safe watchdog view; preserve unset/set and secret-sensitive changes."""
    return {name: (value if name in SAFE_SETTINGS else
                   "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest())
            for name, value in sorted(environment.items())
            if name.startswith("PCC_") or name in PYTHON_SETTINGS}


def strip_checkpoint_environment(environment: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in environment.items() if not name.startswith(PREFIX)}


def resolve_hash_seed(environment: dict[str, str]) -> str:
    value = environment.get("PYTHONHASHSEED") or "0"
    if (not value.isascii() or not value.isdigit() or len(value) > 10
            or int(value) > 4294967295):
        raise CheckpointError("Stage1 checkpoint requires a deterministic PYTHONHASHSEED integer in 0..4294967295")
    return value


def fresh_bytecode_environment(environment: dict[str, str], prefix: Path) -> dict[str, str]:
    """Only a newly created empty prefix is admitted; never reuse pyc inputs."""
    if not prefix.is_absolute() or not prefix.is_dir() or any(prefix.iterdir()):
        raise CheckpointError("checkpoint bytecode prefix must be a fresh empty directory")
    result = dict(environment)
    result["PYTHONPYCACHEPREFIX"] = str(prefix)
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


def validate_route(options, command: list[str], environment: dict[str, str]) -> None:
    resolve_hash_seed(environment)
    if (sys.implementation.name != "cpython" or sys.platform != "linux"
            or platform.machine().lower() not in ("x86_64", "amd64")):
        raise CheckpointError("Stage1 checkpoint requires host CPython on x86_64 Linux")
    if options.backend != "self" or options.python_libpython != "off":
        raise CheckpointError("Stage1 checkpoint requires self backend and --python-libpython off")
    if options.runtime_cc != "pcc" or options.runtime_high != "py":
        raise CheckpointError("Stage1 checkpoint requires the pcc-Python runtime")
    if options.reuse_stage1 or options.start_stage != 1 or options.clean:
        raise CheckpointError("Stage1 checkpoint cannot combine with --reuse-stage1, --clean or --from-stage > 1")
    if (options.external_memory_guard != "0" or options.stage_timeout > 2400
            or options.max_tree_rss_bytes > 16 * 1024 ** 3
            or environment.get("PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS") == "1"):
        raise CheckpointError("Stage1 checkpoint requires the normal lock/watchdog and safe resource limits")
    if command[-2:] != ["-m", "pcc"]:
        raise CheckpointError("Stage1 checkpoint requires the host source compiler command")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND"):
        if environment.get(name, "").lower() not in ("1", "true", "yes", "on"):
            raise CheckpointError("Stage1 checkpoint requires " + name + "=1")
    if environment.get("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "1").lower() not in ("1", "true", "yes", "on"):
        raise CheckpointError("Stage1 checkpoint requires native-object emission")
    for name in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                 "PCC_DIRECT_INDEXED_SIDECAR"):
        if environment.get(name, "").strip().lower() in ("1", "true", "yes", "on"):
            raise CheckpointError("unsupported checkpoint memory-expanding route setting: " + name)
    for name in ("PCC_DEFER_FRONTEND_CODEGEN_PLAN", "PCC_DEFER_SELF_LINK_PLAN",
                 "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN"):
        if environment.get(name, "") not in ("", "0"):
            raise CheckpointError("unsupported checkpoint route setting: " + name)
    if not environment.get("PCC_RUNTIME_ARCHIVE", "").strip():
        raise CheckpointError("Stage1 checkpoint requires an explicit validated PCC_RUNTIME_ARCHIVE")
    if environment.get("PCC_RUNTIME_BUILD", "owned") not in ("", "owned"):
        raise CheckpointError("Stage1 checkpoint requires the owned runtime archive lane")
    if environment.get("LD_PRELOAD"):
        raise CheckpointError("Stage1 checkpoint does not support LD_PRELOAD")


def _dependency_inventory(roots: list[str], source_root: Path) -> list[dict]:
    """Bounded conservative import-root inventory, including package metadata.

    Source-backed caches cannot be read under the enforced fresh-prefix policy.
    Sourceless bytecode and symlinked import roots are rejected.
    A larger/custom installation must be reduced explicitly, not partly hashed.
    """
    seen = set()
    records = []
    total_bytes = 0
    for entry in roots:
        root = Path(entry)
        if root == source_root or root == source_root / "scripts":
            continue
        if not root.exists():
            records.append({"path": str(root), "kind": "absent"})
            continue
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = []
            for parent, directories, files in os.walk(root):
                directories[:] = sorted(d for d in directories if d not in _EXCLUDED_DIRS)
                for name in directories:
                    if (Path(parent) / name).is_symlink():
                        raise CheckpointError("symlinked dependency directory is unsupported: " + str(Path(parent) / name))
                for name in sorted(files):
                    path = Path(parent) / name
                    if path.suffix == ".pyc":
                        if not path.with_suffix(".py").exists():
                            raise CheckpointError("sourceless bytecode dependency is unsupported: " + str(path))
                    elif (path.suffix in _SOURCE_SUFFIXES or ".so." in name
                          or path.parent.name.endswith((".dist-info", ".egg-info"))):
                        candidates.append(path)
        else:
            raise CheckpointError("unsupported interpreter import root: " + str(root))
        for path in candidates:
            if str(path) in seen:
                continue
            seen.add(str(path))
            total_bytes += path.stat().st_size
            if len(seen) > 50000 or total_bytes > 2 * 1024 ** 3:
                raise CheckpointError("dependency inventory exceeds supported bounds (50000 files / 2 GiB)")
            records.append(file_identity(path))
    return sorted(records, key=lambda row: row["path"])


def probe_identity(source_root: str) -> dict:
    """Run in the selected Stage1 interpreter, under the actual stage settings."""
    root = Path(source_root).resolve()
    if (not sys.dont_write_bytecode or not sys.pycache_prefix
            or sys.pycache_prefix != os.environ.get("PYTHONPYCACHEPREFIX")
            or any(Path(sys.pycache_prefix).iterdir())):
        raise CheckpointError("identity probe requires the fresh bytecode-prefix policy")
    if sys.implementation.name != "cpython" or platform.machine().lower() not in ("x86_64", "amd64") or sys.platform != "linux":
        raise CheckpointError("selected worker interpreter is not CPython on x86_64 Linux")
    from pcc.backend.elf_x86_64 import parse_relocatable, read_archive_payloads
    from pcc.driver.paths import host_python_command
    from pcc.frontends.python.owned_runtime_build import ensure_target_runtime
    from pcc.frontends.python.pipeline_targets import host_target_triple
    target = host_target_triple()
    if target != TARGET:
        raise CheckpointError("unsupported checkpoint target: " + target)
    worker = host_python_command(str(root), str(root))
    worker_path = Path(shutil.which(worker) or worker).resolve(strict=True)
    if worker_path != Path(sys.executable).resolve():
        raise CheckpointError("Stage1 source workers must use the selected host interpreter")
    archive = Path(ensure_target_runtime(str(root / "pcc/runtime"), target,
                   explicit_archive=os.environ["PCC_RUNTIME_ARCHIVE"])).resolve()
    # Explicit selection cannot rebuild. The ordinary owned provenance and
    # source/config validation above is supplemented with per-member ELF checks.
    members = []
    for name, data in read_archive_payloads(archive.read_bytes()):
        obj = parse_relocatable(data, compact_relocations=True)
        if obj.machine != 62:
            raise CheckpointError("runtime archive contains a non-x86_64 object")
        members.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    if not members:
        raise CheckpointError("runtime archive has no objects")
    sidecars = []
    for suffix in (".provenance.json", ".capi_syms", ".target", ".wheel"):
        path = Path(str(archive) + suffix)
        if path.exists():
            if suffix == ".target" and path.read_text().strip() != "linux:x86_64:" + target:
                raise CheckpointError("runtime target stamp mismatch")
            sidecars.append(file_identity(path))
    roots = list(dict.fromkeys(str(Path(entry or os.getcwd()).resolve()) for entry in sys.path))
    dependencies = _dependency_inventory(roots, root)
    inventoried = {row.get("realpath") for row in dependencies}
    for name, module in sorted(sys.modules.copy().items()):
        location = getattr(module, "__file__", None)
        if location and Path(location).is_file():
            path = Path(location).resolve()
            if str(path) not in inventoried and not path.is_relative_to(root):
                dependencies.append(file_identity(path))
                inventoried.add(str(path))
    dependencies.sort(key=lambda row: row["path"])
    # Record loaded ELF shared libraries, including the interpreter's libc,
    # libpython and cryptographic implementation, without invoking ldd or cc.
    libraries = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith("/"):
            path = Path(fields[5])
            if ".so" in path.name and path.is_file():
                libraries.add(str(path))
    return {
        "target": target,
        "host_interpreter": {"executable": file_identity(Path(sys.executable)),
            "implementation": sys.implementation.name, "version": sys.version,
            "cache_tag": sys.implementation.cache_tag, "soabi": sysconfig.get_config_var("SOABI"),
            "platform": sys.platform, "machine": platform.machine(),
            "pointer_bits": struct.calcsize("P") * 8, "import_roots": roots,
            "worker_executable": file_identity(worker_path),
            "shared_libraries": [file_identity(Path(path)) for path in sorted(libraries)]},
        "dependencies": dependencies,
        "runtime": {"archive": file_identity(archive), "sidecars": sidecars,
                    "members": members, "target": target, "validation": "owned-explicit-provenance-and-elf"},
    }


def build_identity(options, command: list[str], environment: dict[str, str]) -> dict:
    environment = dict(environment)
    environment["PYTHONHASHSEED"] = resolve_hash_seed(environment)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    validate_route(options, command, environment)
    root = Path(options.repo_root).resolve()
    host_command = command[:-2]
    probe_command = [*host_command, "-c",
        "import json,sys; from scripts.bootstrap_stage1_checkpoint import probe_identity; "
        "print(json.dumps(probe_identity(sys.argv[1]),sort_keys=True))", str(root)]
    # Checkpoint support is Linux-only. Keep probe caches outside source inputs
    # even when the caller's TMPDIR points into the checkout or an old cache.
    with tempfile.TemporaryDirectory(prefix="pcc-stage1-probe-pycache-", dir="/tmp") as temporary:
        prefix = Path(temporary)
        probe_environment = fresh_bytecode_environment(environment, prefix)
        completed = subprocess.run(probe_command, cwd=str(root), env=probe_environment,
                                   capture_output=True, text=True, timeout=120, check=False)
        if any(prefix.iterdir()):
            raise CheckpointError("identity probe populated its forbidden bytecode cache")
    if completed.returncode:
        # Child diagnostics may contain private environment values. The bounded
        # exception class is enough here; the normal archive check is reproducible.
        raise CheckpointError("checkpoint interpreter/runtime identity probe failed (exit " + str(completed.returncode) + ")")
    try:
        probe = json.loads(completed.stdout, object_pairs_hook=_json_pairs)
    except (ValueError, TypeError) as exc:
        raise CheckpointError("invalid checkpoint interpreter identity response") from exc
    launcher = shutil.which(host_command[0], path=environment.get("PATH")) or host_command[0]
    external_inputs = []
    for name, value in sorted(environment.items()):
        if (name.startswith("PCC_") and not name.startswith(PREFIX)
                and name not in NONSEMANTIC_SETTINGS and name not in OUTPUT_PATH_SETTINGS
                and value and len(value) < 4096):
            try:
                path = Path(value)
                is_file = path.is_file()
            except (OSError, ValueError):
                is_file = False
            if is_file:
                info = file_identity(path)
                external_inputs.append({"setting": name, "size": info["size"],
                                        "sha256": info["sha256"],
                                        "realpath_sha256": hashlib.sha256(info["realpath"].encode()).hexdigest()})
    return {
        "checkpoint_protocol_version": 1, "stage": 1, "producer_role": "host-pcc0",
        "owner": "cpython", "backend": "self", "target": probe["target"],
        "bytecode_policy": BYTECODE_POLICY,
        "source_root": str(root), "working_directory": str(root),
        "command": [*command, "--backend", "self", "--python-libpython", "off",
                    str(root / "pcc/__main__.py"), "-o", str(options.stage_output(1))],
        "entry_path": str(root / "pcc/__main__.py"), "output_role": "pcc1",
        "compiler_sources": source_inventory(root),
        "host_command": {"argv": host_command, "launcher": file_identity(Path(launcher))},
        "host_interpreter": probe["host_interpreter"], "dependencies": probe["dependencies"],
        "effective_config": settings_identity(environment), "runtime": probe["runtime"],
        "external_inputs": external_inputs,
        "execution_context": {"frontend_jobs": options.stage1_py_frontend_jobs,
            "self_backend_jobs": options.self_backend_jobs, "cpu_count": os.cpu_count(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "stage_timeout_s": options.stage_timeout, "tree_rss_cap_bytes": options.max_tree_rss_bytes,
            "chunk_assignments": "bound by graph identity after full frontend pre-pass"},
        "wire_contracts": {"worker": "pcc.frontends.python.codegen_worker.v5", "ast": "pcc.frontends.python.py_ast.v1",
            "exports": "pcc.frontends.python.native_exports.v1",
            "indexed_exports": "pcc.frontends.python.native_exports.indexed.v1",
            "effect_summary": "pcc.vthread.effect-summary.v1",
            "artifact": "ELF64-LE-ET_REL", "machine": 62},
    }


def ensure_build(root: Path, payload: dict) -> str:
    expected = {"schema": BUILD_SCHEMA, "identity_sha256": digest(payload), "payload": payload}
    path = root / "build.json"
    if path.exists():
        actual = read_json(path)
        if (not isinstance(actual, dict) or set(actual) != set(expected)
                or actual.get("schema") != BUILD_SCHEMA or not isinstance(actual.get("payload"), dict)
                or digest(actual["payload"]) != actual.get("identity_sha256")):
            raise CheckpointError("invalid checkpoint build identity")
        if actual != expected:
            changed = sorted(name for name in set(payload) | set(actual["payload"])
                             if payload.get(name) != actual["payload"].get(name))
            raise CheckpointError("checkpoint build identity mismatch: " + ", ".join(changed)
                                  + "; choose a new checkpoint directory")
    else:
        if any(path.name not in (".lock",) for path in root.iterdir()):
            raise CheckpointError("checkpoint directory has data but no build identity")
        write_immutable_json(path, expected)
    return expected["identity_sha256"]


def _cpu() -> tuple[float, float]:
    """CPU charged to this launcher and child chains that were actually reaped.

    Killed/orphaned descendants may never be charged to RUSAGE_CHILDREN.
    A terminal watchdog receipt alone does not establish complete CPU accounting.
    """
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + children.ru_utime, own.ru_stime + children.ru_stime


def _finite_nonnegative(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


_TERMINAL_GUARD_STATES = frozenset({
    "COMPLETE", "TIMEOUT", "MEMORY_LIMIT", "INTERRUPTED", "SAMPLER_ERROR",
    "PREFLIGHT_REJECTED", "LOCK_REJECTED",
})


def _terminal_guard(record) -> bool:
    return (isinstance(record, dict)
            and record.get("schema") == "pcc.process_tree_sample.v1"
            and record.get("status") in _TERMINAL_GUARD_STATES
            and _finite_nonnegative(record.get("elapsed_s")))


def check_prior_attempts(root: Path) -> None:
    """A lost watchdog is an explicit recovery boundary, never a cache hit."""
    for path in sorted((root / "attempts").glob("*.started.json")):
        started = read_json(path)
        attempt_id = path.name.removesuffix(".started.json")
        finish_path = path.with_name(attempt_id + ".finished.json")
        if not isinstance(started, dict) or not isinstance(started.get("guard_result"), str):
            raise CheckpointError("invalid checkpoint attempt start record")
        if finish_path.exists():
            finished = read_json(finish_path)
            if not isinstance(finished, dict):
                raise CheckpointError("invalid checkpoint attempt finish record")
            if finished.get("guard_launch_requested") is False:
                continue
        guard_path = Path(started["guard_result"])
        expected_guard = root / "attempts" / attempt_id / "guard/result.json"
        if guard_path != expected_guard or guard_path.resolve() != expected_guard.absolute():
            raise CheckpointError("checkpoint attempt guard path escapes store")
        if not guard_path.exists() or not _terminal_guard(read_json(guard_path)):
            raise CheckpointError("checkpoint resume blocked by unresolved watchdog: " + str(guard_path)
                                  + "; await its terminal receipt or use explicit recovery")


def summarize_attempts(root: Path) -> dict:
    attempts = []
    wall = cpu = 0.0
    unknown_wall = unknown_cpu = False
    first_start = None
    last_end = None
    for path in sorted((root / "attempts").glob("*.started.json")):
        started = read_json(path)
        attempt_id = path.name.removesuffix(".started.json")
        finish_path = path.with_name(attempt_id + ".finished.json")
        first_start = min(first_start or started["started_at_utc"], started["started_at_utc"])
        if finish_path.exists():
            finished = read_json(finish_path)
            elapsed = finished.get("active_wall_ms")
            used_cpu = finished.get("total_cpu_ms_lower_bound", finished.get("total_cpu_ms"))
            status = finished.get("status")
            unknown_wall = unknown_wall or finished.get("active_wall_exact") is False
            # Legacy launched receipts incorrectly claimed exact CPU when only
            # their guard was terminal. Keep their value as a lower bound.
            unknown_cpu = unknown_cpu or (finished.get("total_cpu_exact") is not True
                                          or finished.get("guard_launch_requested") is not False)
            last_end = max(last_end or finished["finished_at_utc"], finished["finished_at_utc"])
        else:
            status, elapsed, used_cpu = "UNFINISHED", None, None
            guard_path = Path(started["guard_result"])
            # A terminal guard proves only its compile interval, not the lost
            # launcher/barrier interval. RUNNING samples never prove exact time.
            if guard_path.exists():
                guard = read_json(guard_path)
                if _terminal_guard(guard):
                    wall += float(guard["elapsed_s"]) * 1000
                    status = "UNFINISHED_GUARD_" + str(guard["status"])
        if _finite_nonnegative(elapsed):
            wall += elapsed
        else:
            unknown_wall = True
        if _finite_nonnegative(used_cpu):
            cpu += used_cpu
        else:
            unknown_cpu = True
        attempts.append({"attempt_id": attempt_id, "status": status,
                         "started_receipt": str(path), "finished_receipt": str(finish_path) if finish_path.exists() else None,
                         "guard_result": started["guard_result"]})
    span = None
    if first_start and last_end:
        span = (dt.datetime.fromisoformat(last_end) - dt.datetime.fromisoformat(first_start)).total_seconds() * 1000
    return {"attempt_count": len(attempts), "attempts": attempts,
        "cumulative_active_wall_ms_lower_bound": int(wall),
        "cumulative_active_wall_ms": None if unknown_wall else int(wall),
        "cumulative_cpu_ms_lower_bound": int(cpu), "cumulative_cpu_ms": None if unknown_cpu else int(cpu),
        "unknown_wall_intervals": unknown_wall, "unknown_cpu_intervals": unknown_cpu,
        "first_start_to_last_finish_wall_ms": span,
        "uninterrupted_performance_acceptance": False}


class Attempt:
    def __init__(self, root: Path, identity: str, options, command, environment, payload,
                 *, started=None, cpu_started=None, started_at=None):
        self.root, self.identity = root, identity
        self.options, self.command, self.environment, self.payload = options, command, environment, payload
        self.guard_launch_requested = False
        self.id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex[:12]
        self.directory = root / "attempts" / self.id
        self.directory.mkdir(parents=True)
        self.guard_dir = self.directory / "guard"
        self.started = time.monotonic() if started is None else started
        self.cpu_started = _cpu() if cpu_started is None else cpu_started
        write_immutable_json(root / "attempts" / (self.id + ".started.json"), {
            "schema": "pcc.stage1-checkpoint.attempt-start.v1", "attempt_id": self.id,
            "build_identity_sha256": identity, "started_at_utc": started_at or dt.datetime.now(dt.timezone.utc).isoformat(),
            "monotonic_anchor_s": self.started, "launcher_pid": os.getpid(),
            "command": [*command, "--profile-json", str(self.directory / "profile/stage1.json"),
                        "--backend", options.backend, "--python-libpython", options.python_libpython,
                        str(Path(options.repo_root) / "pcc/__main__.py"), "-o", str(options.stage_output(1))],
            "guard_result": str(self.guard_dir / "result.json"),
            "stage_timeout_s": options.stage_timeout, "tree_rss_cap_bytes": options.max_tree_rss_bytes,
            "profile_dir": str(self.directory / "profile"),
        })

    def verify_inputs(self) -> None:
        pycache = self.directory / "pycache"
        if pycache.exists() and any(pycache.iterdir()):
            raise CheckpointError("checkpoint compiler populated its forbidden bytecode cache")
        current = build_identity(self.options, self.command, self.environment)
        if current != self.payload:
            changed = sorted(name for name in set(current) | set(self.payload)
                             if current.get(name) != self.payload.get(name))
            raise CheckpointError("checkpoint inputs changed during attempt: " + ", ".join(changed))

    def request_guard_launch(self) -> None:
        # Publish before spawning: a lost launcher without a terminal guard is
        # unresolved. A normal pre-launch failure may finish with false below.
        write_immutable_json(self.directory / "guard.launch-requested.json", {
            "schema": "pcc.stage1-checkpoint.guard-launch.v1", "attempt_id": self.id,
            "guard_result": str(self.guard_dir / "result.json"),
        })
        self.guard_launch_requested = True

    def finish(self, status: str, returncode: int, output: Path, *, modules=None) -> dict:
        result_path = self.directory / "profile/stage1.result.json"
        stage_result = read_json(result_path) if result_path.exists() else None
        if modules is None:
            try:
                modules = _module_summary(self.root, self.identity, self.id)
            except (ValueError, OSError) as exc:
                modules = {"validation": "unavailable", "error_type": type(exc).__name__}
        output_identity = file_identity(output) if status == "COMPLETE" else None
        accounting_complete = not self.guard_launch_requested
        if self.guard_launch_requested:
            try:
                accounting_complete = _terminal_guard(read_json(self.guard_dir / "result.json"))
            except CheckpointError:
                accounting_complete = False
        cpu_now = _cpu()
        elapsed = int((time.monotonic() - self.started) * 1000)
        cpu_lower_bound = int(max(0, sum(cpu_now) - sum(self.cpu_started)) * 1000)
        # No existing guard field proves that every descendant was reaped.
        # Preserve exact wall time independently of this CPU coverage gap.
        cpu_complete = not self.guard_launch_requested
        result = {"schema": "pcc.stage1-checkpoint.attempt-finish.v1", "attempt_id": self.id,
            "build_identity_sha256": self.identity, "status": status, "returncode": returncode,
            "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "active_wall_ms": elapsed,
            "total_cpu_ms": cpu_lower_bound if cpu_complete else None,
            "total_cpu_ms_lower_bound": cpu_lower_bound,
            "active_wall_exact": accounting_complete, "total_cpu_exact": cpu_complete,
            "cpu_scope": "launcher_and_reaped_children_including_guard_and_barrier",
            "measurement_end": "after_input_object_and_output_validation_before_ledger_publication",
            "guard_launch_requested": self.guard_launch_requested,
            "modules": modules,
            "stage_result": stage_result, "guard_result": str(self.guard_dir / "result.json"),
            "output": output_identity}
        write_immutable_json(self.root / "attempts" / (self.id + ".finished.json"), result)
        return summarize_attempts(self.root)


def _module_summary(root: Path, identity: str, attempt_id: str) -> dict:
    from pcc.frontends.python.pipeline_stage1_checkpoint import (
        CheckpointError as ReceiptError,
        summarize,
    )
    try:
        return summarize(str(root), identity, attempt_id)
    except ReceiptError as exc:
        raise CheckpointError(str(exc)) from exc


def run_checkpointed_stage(stage, output, command, options, run_stage_impl) -> None:
    from scripts import bootstrap
    from scripts.file_lock import exclusive_file_lock
    root = Path(options.stage1_checkpoint).expanduser().resolve()
    if root == Path(options.repo_root).resolve() or root == options.out_dir.resolve():
        raise CheckpointError("checkpoint store must have its own directory")
    if any(root.is_relative_to(Path(options.repo_root).resolve() / name)
           for name in ("pcc", "scripts", "utils")):
        raise CheckpointError("checkpoint store cannot be inside compiler source inputs")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("build.json", "graph.json", "attempts", "objects", "modules", "final.json", ".lock"):
        if (root / name).is_symlink():
            raise CheckpointError("checkpoint store contains a symlink: " + name)
    with exclusive_file_lock(root / ".lock", blocking=False):
        check_prior_attempts(root)
        started, cpu_started = time.monotonic(), _cpu()
        started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        environment = bootstrap.stage_environment(1, options)
        environment["PYTHONHASHSEED"] = resolve_hash_seed(environment)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        payload = build_identity(options, command, environment)
        identity = ensure_build(root, payload)
        prior = summarize_attempts(root)
        prior_wall = prior["cumulative_active_wall_ms"]
        print("PCC_STAGE1_CHECKPOINT_START previous_attempts=" + str(prior["attempt_count"])
              + " prior_cumulative_active_wall_ms=" + ("unknown" if prior_wall is None else str(prior_wall))
              + " prior_active_wall_ms_lower_bound=" + str(prior["cumulative_active_wall_ms_lower_bound"])
              + " prior_unknown_wall_intervals=" + str(int(prior["unknown_wall_intervals"]))
              + " current_attempt_limit_s=" + str(options.stage_timeout)
              + " uninterrupted_performance_acceptance=0")
        # A new attempt must not leave a prior success-shaped current receipt.
        # The old immutable attempts/<id>/final.json remains preserved.
        if (root / "final.json").exists():
            (root / "final.json").unlink()
            _fsync_directory(root)
        attempt = Attempt(root, identity, options, command, environment, payload,
                          started=started, cpu_started=cpu_started, started_at=started_at)
        selected = copy.copy(options)
        selected.env = dict(options.env)
        selected.env["PYTHONHASHSEED"] = environment["PYTHONHASHSEED"]
        pycache = attempt.directory / "pycache"
        pycache.mkdir()
        selected.env = fresh_bytecode_environment(selected.env, pycache)
        selected.env.update({PREFIX + "DIR": str(root), PREFIX + "BUILD": identity,
                             PREFIX + "STAGE": "1", PREFIX + "ATTEMPT": attempt.id})
        selected.profile_dir = str(attempt.directory / "profile")
        selected.keep_process_samples = True
        selected.checkpoint_attempt = attempt
        selected.checkpoint_guard_dir = attempt.guard_dir
        try:
            run_stage_impl(stage, output, command, selected)
            modules = _module_summary(root, identity, attempt.id)
            if (modules.get("module_count", 0) < 1 or modules.get("missing_modules")
                    or modules.get("rejected_modules")
                    or modules.get("unique_completed_modules") != modules.get("module_count")):
                raise CheckpointError("successful Stage1 has incomplete or invalid durable module receipts")
        except BaseException as exc:
            code = getattr(exc, "exit_code", 130 if isinstance(exc, KeyboardInterrupt) else 1)
            status = "TIMEOUT" if code == 124 else "INTERRUPTED" if code == 130 else "FAILED"
            summary = attempt.finish(status, code, output)
            print("PCC_STAGE1_CHECKPOINT_ACCOUNTING " + json.dumps(summary, sort_keys=True))
            raise
        summary = attempt.finish("COMPLETE", 0, output, modules=modules)
        final = {"schema": "pcc.stage1-checkpoint.final.v1", "build_identity_sha256": identity,
                 "attempt_id": attempt.id, "status": "complete_resumed" if summary["attempt_count"] > 1 else "complete_cold",
                 "output": file_identity(output), "runtime": payload["runtime"],
                 "modules": modules,
                 "smoke": {"returncode": 0, "receipt": str(attempt.directory / "profile/stage1.result.json")},
                 "accounting": summary}
        # Keep every successful publication; final.json is a convenience pointer
        # to the latest immutable final receipt, never evidence by itself.
        final_path = attempt.directory / "final.json"
        write_immutable_json(final_path, final)
        temporary = root / (".final." + attempt.id + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(canonical_bytes(final) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "final.json")
        _fsync_directory(root)
        print("PCC_STAGE1_CHECKPOINT_ACCOUNTING " + json.dumps(summary, sort_keys=True))
        print("PCC_BOOTSTRAP_STAGE_CHECKPOINT_RESULT stage=1 elapsed_ms="
              + str(read_json(attempt.directory / "profile/stage1.result.json")["wall_ms"])
              + " output=" + str(output) + " rc=0 checkpoint=" + final["status"])
