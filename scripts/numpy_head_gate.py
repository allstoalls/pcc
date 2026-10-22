#!/usr/bin/env python3
"""Current-source NumPy _core build/link/PEP 489 milestone gate.

The historical NumPy evidence was assembled by hand in temporary directories.
This gate rebuilds the same 137-file compile surface from Meson's recorded build
graph, links the actual 136-object ``_multiarray_umath`` closure, and executes
the extension through pcc's strict self/no-libpython loader.  Generated Meson
sources are inputs; object files are always rebuilt under the requested output
directory and are never reused from the NumPy project tree.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pcc.package.build_exec import (  # noqa: E402
    _STALE_TOOLCHAIN_FLAG_REPLACEMENTS,
    _materialize_pcc_capi_include,
    _normalize_stale_toolchain_flags,
    _pcc_runtime_include_dir,
    _redirect_pcc_native_includes,
)
from pcc.package.metadata import current_platform_tag  # noqa: E402
from pcc.package.schema import pcc_native_extension_suffix  # noqa: E402
from scripts.numpy_first_blocker import evaluate_result  # noqa: E402

SCHEMA = "pcc.numpy-head-gate.v1"
PASS = "PASS"
FAIL = "FAIL"
EXPECTED_NAME = "numpy"
EXPECTED_VERSION = "2.4.4"
EXPECTED_COMPILE_SURFACE = 137
EXPECTED_LINK_CLOSURE = 136
MODULE_NAME = "_multiarray_umath"
MODULE_QUALIFIED_NAME = "numpy._core._multiarray_umath"
MODULE_GRAPH_SEEDS = (
    "math",
    "sys",
    "time",
    "gc",
    "copy",
    "os",
    "numpy.exceptions",
    "numpy._globals",
    "numpy._core._exceptions",
    "numpy._core.printoptions",
    "numpy.dtypes",
)

_BUILD_EDGE_RE = re.compile(r"^build\s+(.+?):\s+(\S+)(?:\s+(.*))?$")
_MODULE_NOT_FOUND_RE = re.compile(r"module not found:\s*([^\s]+)")
_PYINIT_SYMBOL_RE = re.compile(r"\b(_?PyInit_[A-Za-z0-9_]+)\b")
_DEPENDENCY_FLAGS = {"-MD", "-MMD", "-MP"}
_DEPENDENCY_VALUE_FLAGS = {"-MF", "-MQ", "-MT"}
# _STALE_TOOLCHAIN_FLAG_REPLACEMENTS / _normalize_stale_toolchain_flags are the
# single source of truth in pcc.package.build_exec (imported above), so the
# package build path and this gate can never drift on deprecated-flag handling.


class GateError(RuntimeError):
    """A deterministic gate precondition or execution failure."""


@dataclass(frozen=True)
class CompileAction:
    source: Path
    original_output: str
    command: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class GatePlan:
    source_root: Path
    meson_build: Path
    module_target: str
    compile_surface: tuple[CompileAction, ...]
    link_closure_outputs: tuple[str, ...]
    source_name: str
    source_version: str
    source_sha256: str


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read JSON input {path}: {exc}") from exc


def _logical_ninja_lines(path: Path) -> list[str]:
    try:
        physical = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError(f"cannot read Ninja graph {path}: {exc}") from exc
    logical: list[str] = []
    pending = ""
    for line in physical:
        current = pending + line.lstrip() if pending else line
        if current.endswith("$"):
            pending = current[:-1]
            continue
        logical.append(current)
        pending = ""
    if pending:
        logical.append(pending)
    return logical


def _ninja_edges(path: Path) -> dict[str, tuple[str, ...]]:
    edges: dict[str, tuple[str, ...]] = {}
    for line in _logical_ninja_lines(path):
        match = _BUILD_EDGE_RE.match(line)
        if match is None:
            continue
        outputs = shlex.split(match.group(1))
        inputs = tuple(
            token
            for token in shlex.split(match.group(3) or "")
            if token not in {"|", "||"}
        )
        for output in outputs:
            edges[output] = inputs
    return edges


def _module_target(intro_path: Path, meson_build: Path) -> str:
    entries = _read_json(intro_path)
    if not isinstance(entries, list):
        raise GateError(f"Meson target list is not an array: {intro_path}")
    candidates: list[Path] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "shared module":
            continue
        if not str(entry.get("name", "")).startswith(MODULE_NAME + "."):
            continue
        filenames = entry.get("filename")
        if isinstance(filenames, list):
            candidates.extend(Path(str(item)) for item in filenames)
    if len(candidates) != 1:
        raise GateError(
            f"expected one Meson {MODULE_NAME} shared module, got {candidates}"
        )
    try:
        return str(candidates[0].resolve().relative_to(meson_build.resolve()))
    except ValueError as exc:
        raise GateError(
            f"module target escapes Meson build root: {candidates[0]}"
        ) from exc


def _collect_object_closure(
    target: str, edges: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    objects: list[str] = []
    seen_objects: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for dependency in edges.get(node, ()):
            if dependency.endswith(".o"):
                if dependency not in seen_objects:
                    seen_objects.add(dependency)
                    objects.append(dependency)
            elif dependency in edges:
                visit(dependency)

    if target not in edges:
        raise GateError(f"module target is missing from build.ninja: {target}")
    visit(target)
    return tuple(objects)


def _metadata_identity(pkg_info: Path) -> tuple[str, str]:
    name = ""
    version = ""
    try:
        lines = pkg_info.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError(f"cannot read {pkg_info}: {exc}") from exc
    for line in lines:
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
    return name, version


def _resolve_source(entry: dict[str, object], meson_build: Path) -> Path:
    source = Path(str(entry.get("file") or ""))
    if not source.is_absolute():
        source = meson_build / source
    return source.resolve()


def _target_component(output: str) -> str:
    parts = Path(output).parts
    if len(parts) < 3 or parts[:2] != ("numpy", "_core"):
        return ""
    return parts[2]


def _is_historical_core_surface(output: str) -> bool:
    target = _target_component(output)
    if not target or "test" in target:
        return False
    # _simd is a separate extension.  Its baseline dispatch object remains in
    # the historical _core compile surface, while its two module-body objects
    # do not.  This is the recorded 113 C + 24 C++ = 137-file boundary.
    return not target.startswith("_simd.")


def _rebase_absolute_token(
    token: str,
    *,
    recorded_build: Path | None,
    recorded_source: Path | None,
    meson_build: Path,
    source_root: Path,
) -> str:
    pairs = (
        (recorded_build, meson_build),
        (recorded_source, source_root),
    )
    for old, new in pairs:
        if old is None:
            continue
        old_text = str(old)
        if token == old_text:
            return str(new)
        if token.startswith(old_text + os.sep):
            return str(new) + token[len(old_text) :]
        for prefix in ("-I", "-F", "-L", "-idirafter"):
            combined = prefix + old_text
            if token == combined or token.startswith(combined + os.sep):
                return prefix + str(new) + token[len(combined) :]
    return token


def _entry_command(
    entry: dict[str, object], source_root: Path, meson_build: Path
) -> tuple[str, ...]:
    raw = str(entry.get("command") or "")
    arguments = entry.get("arguments")
    if isinstance(arguments, list):
        tokens = [str(item) for item in arguments]
    else:
        tokens = shlex.split(raw)
    if not tokens:
        raise GateError(f"empty compile command for {entry.get('file')}")
    recorded_raw = str(entry.get("directory") or "")
    recorded_build = Path(recorded_raw) if recorded_raw else None
    recorded_source = (
        recorded_build.parent.parent.parent if recorded_build is not None else None
    )
    return tuple(
        _rebase_absolute_token(
            token,
            recorded_build=recorded_build,
            recorded_source=recorded_source,
            meson_build=meson_build,
            source_root=source_root,
        )
        for token in tokens
    )


def _source_digest(
    source_root: Path,
    scaffold_paths: Iterable[Path],
    actions: Sequence[CompileAction],
) -> str:
    digest = hashlib.sha256()
    paths = set(scaffold_paths)
    paths.update(action.source for action in actions)
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            label = str(path.resolve().relative_to(source_root.resolve()))
        except ValueError:
            label = str(path.resolve())
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise GateError(f"source identity input is missing: {path}: {exc}") from exc
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def build_plan(source_root: Path) -> GatePlan:
    source_root = source_root.resolve()
    meson_build = source_root / "build" / "pcc-package" / "meson-build"
    compile_commands = meson_build / "compile_commands.json"
    intro_targets = meson_build / "meson-info" / "intro-targets.json"
    build_ninja = meson_build / "build.ninja"
    entries = _read_json(compile_commands)
    if not isinstance(entries, list):
        raise GateError(f"compile_commands root is not an array: {compile_commands}")

    actions: list[CompileAction] = []
    by_output: dict[str, CompileAction] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        output = str(raw_entry.get("output") or "")
        if not _is_historical_core_surface(output):
            continue
        action = CompileAction(
            source=_resolve_source(raw_entry, meson_build),
            original_output=output,
            command=_entry_command(raw_entry, source_root, meson_build),
            cwd=meson_build,
        )
        if output in by_output:
            raise GateError(f"duplicate compile output in NumPy graph: {output}")
        by_output[output] = action
        actions.append(action)

    target = _module_target(intro_targets, meson_build)
    link_outputs = _collect_object_closure(target, _ninja_edges(build_ninja))
    missing = [output for output in link_outputs if output not in by_output]
    if missing:
        raise GateError(f"link closure has no compile commands: {missing[:5]}")

    name, version = _metadata_identity(source_root / "PKG-INFO")
    source_sha256 = _source_digest(
        source_root,
        (source_root / "PKG-INFO", compile_commands, intro_targets, build_ninja),
        actions,
    )
    return GatePlan(
        source_root=source_root,
        meson_build=meson_build,
        module_target=target,
        compile_surface=tuple(actions),
        link_closure_outputs=link_outputs,
        source_name=name,
        source_version=version,
        source_sha256=source_sha256,
    )


def validate_plan(plan: GatePlan) -> None:
    errors: list[str] = []
    if plan.source_name != EXPECTED_NAME:
        errors.append(f"expected source name {EXPECTED_NAME}, got {plan.source_name}")
    if plan.source_version != EXPECTED_VERSION:
        errors.append(
            f"expected source version {EXPECTED_VERSION}, got {plan.source_version}"
        )
    if len(plan.compile_surface) != EXPECTED_COMPILE_SURFACE:
        errors.append(
            f"expected {EXPECTED_COMPILE_SURFACE} compile actions, "
            f"got {len(plan.compile_surface)}"
        )
    if len(plan.link_closure_outputs) != EXPECTED_LINK_CLOSURE:
        errors.append(
            f"expected {EXPECTED_LINK_CLOSURE} link objects, "
            f"got {len(plan.link_closure_outputs)}"
        )
    missing_sources = [
        action.source for action in plan.compile_surface if not action.source.is_file()
    ]
    if missing_sources:
        errors.append(f"missing source inputs: {missing_sources[:5]}")
    if errors:
        raise GateError("; ".join(errors))


def _strip_dependency_flags(tokens: Sequence[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _DEPENDENCY_FLAGS:
            index += 1
            continue
        if token in _DEPENDENCY_VALUE_FLAGS and index + 1 < len(tokens):
            index += 2
            continue
        if any(
            token.startswith(prefix) and token != prefix
            for prefix in _DEPENDENCY_VALUE_FLAGS
        ):
            index += 1
            continue
        result.append(token)
        index += 1
    return result


def _replace_output(tokens: Sequence[str], output: Path) -> list[str]:
    result = list(tokens)
    for index, token in enumerate(result[:-1]):
        if token == "-o":
            result[index + 1] = str(output)
            return result
    result.extend(("-o", str(output)))
    return result


def _compile_command(
    action: CompileAction,
    *,
    output: Path,
    capi_dir: Path,
    runtime_include: Path,
) -> list[str]:
    tokens = list(action.command)
    compiler = shutil.which(tokens[0])
    if compiler is None:
        raise GateError(f"compiler not found: {tokens[0]}")
    tokens[0] = compiler
    tokens = _redirect_pcc_native_includes(tokens, capi_dir, runtime_include)
    tokens = _normalize_stale_toolchain_flags(tokens)
    tokens = _strip_dependency_flags(tokens)
    tokens = _replace_output(tokens, output)
    if "-fPIC" not in tokens:
        tokens.append("-fPIC")
    return tokens


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _artifact_path(build_root: Path, suffix: str) -> Path:
    parts = MODULE_QUALIFIED_NAME.split(".")
    return build_root / "site" / Path(*parts[:-1]) / f"{parts[-1]}{suffix}"


def _loader_package_site(build_root: Path, source_root: Path) -> str:
    """Return package roots in installed-artifact precedence order."""
    generated_root = source_root / "build" / "pcc-package" / "meson-build"
    return os.pathsep.join(
        (
            str((build_root / "site").resolve()),
            str(generated_root.resolve()),
            str(source_root.resolve()),
        )
    )


def _compile_all(
    plan: GatePlan,
    build_root: Path,
    *,
    jobs: int,
    timeout: int,
) -> tuple[dict[str, Path], list[dict[str, object]]]:
    capi_dir = _materialize_pcc_capi_include(build_root, execute=True)
    runtime_include = _pcc_runtime_include_dir()
    if capi_dir is None or runtime_include is None:
        raise GateError("PCC C-API/runtime include directories are unavailable")

    outputs: dict[str, Path] = {}
    failures: list[dict[str, object]] = []
    lock = threading.Lock()
    completed = 0

    def compile_one(
        action: CompileAction,
    ) -> tuple[CompileAction, Path, subprocess.CompletedProcess[str]]:
        output = build_root / "objects" / action.original_output
        output.parent.mkdir(parents=True, exist_ok=True)
        command = _compile_command(
            action,
            output=output,
            capi_dir=capi_dir,
            runtime_include=runtime_include,
        )
        process = _run_process(command, cwd=action.cwd, timeout=timeout)
        return action, output, process

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        pending = {
            executor.submit(compile_one, action) for action in plan.compile_surface
        }
        for future in as_completed(pending):
            action, output, process = future.result()
            with lock:
                completed += 1
                if process.returncode == 0 and output.is_file():
                    outputs[action.original_output] = output
                else:
                    failures.append(
                        {
                            "source": str(action.source),
                            "output": action.original_output,
                            "returncode": process.returncode,
                            "stdout": process.stdout[-4000:],
                            "stderr": process.stderr[-8000:],
                        }
                    )
                if (
                    completed == 1
                    or completed % 10 == 0
                    or completed == len(plan.compile_surface)
                ):
                    print(
                        f"[numpy-head] compile {completed}/{len(plan.compile_surface)} "
                        f"failures={len(failures)}",
                        flush=True,
                    )
    return outputs, failures


def _link_module(
    plan: GatePlan,
    outputs: dict[str, Path],
    artifact: Path,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    linker = shutil.which("c++") or shutil.which("clang++")
    if linker is None:
        raise GateError("C++ linker not found")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    command = [
        linker,
        "-bundle",
        "-Wl,-undefined,dynamic_lookup",
        *[str(outputs[name]) for name in plan.link_closure_outputs],
    ]
    if platform.system() == "Darwin":
        command.extend(("-framework", "Accelerate"))
    command.extend(("-lm", "-o", str(artifact)))
    return _run_process(command, cwd=ROOT, timeout=timeout)


def _dynamic_dependencies(
    path: Path,
) -> tuple[list[str], subprocess.CompletedProcess[str]]:
    command = (
        ["otool", "-L", str(path)]
        if platform.system() == "Darwin"
        else ["ldd", str(path)]
    )
    process = _run_process(command, cwd=ROOT, timeout=30)
    dependencies = [
        line.strip() for line in process.stdout.splitlines()[1:] if line.strip()
    ]
    return dependencies, process


def _exports_pyinit(path: Path) -> bool:
    command = (
        ["nm", "-gU", str(path)]
        if platform.system() == "Darwin"
        else ["nm", "-g", str(path)]
    )
    process = _run_process(command, cwd=ROOT, timeout=30)
    return process.returncode == 0 and f"PyInit_{MODULE_NAME}" in process.stdout


def classify_loader_output(
    returncode: int, stdout: str, stderr: str
) -> tuple[bool, bool, dict[str, object] | None]:
    """Classify the first post-link boundary without guessing past the log.

    A missing module from NumPy's multi-phase initialization path proves both
    that ``PyInit_*`` returned its module definition and that the loader entered
    ``Py_mod_exec``. Unclassified native failures do not receive that claim.
    """

    combined = (stdout or "") + (stderr or "")
    missing = _MODULE_NOT_FOUND_RE.search(combined)
    pyinit_symbol = _PYINIT_SYMBOL_RE.search(combined)
    if returncode == 0:
        return True, True, None
    if missing is not None:
        return (
            True,
            True,
            {
                "kind": "first_missing_module",
                "value": missing.group(1),
                "phase": "Py_mod_exec",
            },
        )
    if (
        "dlsym failed" in combined
        or "undefined symbol" in combined
        or "symbol not found" in combined
    ):
        symbol = (
            pyinit_symbol.group(1).removeprefix("_")
            if pyinit_symbol is not None
            else combined.strip().splitlines()[-1][-500:]
        )
        return (
            False,
            False,
            {
                "kind": "first_missing_symbol",
                "value": symbol,
                "phase": "extension_load_or_init",
            },
        )
    return (
        False,
        False,
        {
            "kind": "first_semantic_mismatch",
            "value": combined.strip()[-2000:],
            "phase": "extension_load_or_init",
        },
    )


def _loader_probe(
    build_root: Path,
    artifact: Path,
    *,
    source_root: Path,
    timeout: int,
) -> dict[str, object]:
    source = build_root / "loader_probe.py"
    executable = build_root / "loader_probe"
    profile_path = build_root / "loader_profile.json"
    source.write_text(
        "import math\n"
        "import sys\n"
        "import time\n"
        "import gc\n"
        "import copy\n"
        "import os\n"
        "import numpy.exceptions\n"
        "import numpy._globals\n"
        "import numpy._core._exceptions\n"
        "import numpy._core.printoptions\n"
        "import numpy.dtypes\n"
        f"import {MODULE_QUALIFIED_NAME}\n"
        "print('numpy-core-import-complete')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = _loader_package_site(build_root, source_root)
    env["PCC_RUNTIME_CC"] = "cc"
    env["PCC_RUNTIME_HIGH"] = "c"
    env["PCC_PY_FRONTEND_WORKER_TIMING"] = "1"
    compile_process = _run_process(
        (
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            "--profile-json",
            str(profile_path),
            str(source),
            "-o",
            str(executable),
        ),
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )
    if compile_process.returncode != 0:
        return {
            "compile_returncode": compile_process.returncode,
            "compile_stdout": compile_process.stdout[-4000:],
            "compile_stderr": compile_process.stderr[-8000:],
            "run_returncode": None,
            "entered_pyinit": False,
            "entered_py_mod_exec": False,
            "first_blocker": {
                "kind": "first_semantic_mismatch",
                "value": (
                    compile_process.stderr.strip()[-2000:]
                    or "strict loader compilation failed"
                ),
                "phase": "loader_compile",
            },
            "executable": str(executable),
            "module_graph_seeds": list(MODULE_GRAPH_SEEDS),
            "profile": str(profile_path),
        }

    dependencies, dep_process = _dynamic_dependencies(executable)
    lowered_dependencies = "\n".join(dependencies).lower()
    links_libpython = (
        "libpython" in lowered_dependencies
        or "python.framework" in lowered_dependencies
    )
    links_llvm = "libllvm" in lowered_dependencies
    run_process = _run_process((str(executable),), cwd=ROOT, env=env, timeout=60)
    entered_pyinit, entered_exec, blocker = classify_loader_output(
        run_process.returncode, run_process.stdout, run_process.stderr
    )
    return {
        "compile_returncode": compile_process.returncode,
        "compile_stdout": compile_process.stdout[-4000:],
        "compile_stderr": compile_process.stderr[-8000:],
        "run_returncode": run_process.returncode,
        "run_stdout": run_process.stdout,
        "run_stderr": run_process.stderr,
        "dependency_returncode": dep_process.returncode,
        "dependencies": dependencies,
        "links_libpython": links_libpython,
        "links_llvm": links_llvm,
        "entered_pyinit": entered_pyinit,
        "entered_py_mod_exec": entered_exec,
        "first_blocker": blocker,
        "executable": str(executable),
        "module_graph_seeds": list(MODULE_GRAPH_SEEDS),
        "profile": str(profile_path),
    }


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _write_result(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_gate(
    source_root: Path,
    build_root: Path,
    result_path: Path,
    *,
    jobs: int,
    compile_timeout: int,
    link_timeout: int,
    loader_timeout: int,
) -> dict[str, object]:
    source_root = source_root.resolve()
    build_root = build_root.resolve()
    result_path = result_path.resolve()
    started = time.monotonic()
    result: dict[str, object] = {
        "schema": SCHEMA,
        "status": FAIL,
        "mode": {
            "compiler": "host-pcc-current-source",
            "backend": "self",
            "python_abi": "pcc-native",
            "libpython": "off",
            "ir_scaffold": "on",
        },
        "failure": None,
    }
    try:
        plan = build_plan(source_root)
        validate_plan(plan)
        result["source"] = {
            "name": plan.source_name,
            "version": plan.source_version,
            "sha256": plan.source_sha256,
            "path": _relative(plan.source_root),
            "meson_build_graph": _relative(plan.meson_build),
        }
        result["compile"] = {
            "expected": EXPECTED_COMPILE_SURFACE,
            "planned": len(plan.compile_surface),
            "passed": 0,
            "failed": 0,
            "language_counts": {
                suffix: sum(
                    action.source.suffix == suffix for action in plan.compile_surface
                )
                for suffix in (".c", ".cpp", ".cc")
            },
            "toolchain_normalizations": dict(_STALE_TOOLCHAIN_FLAG_REPLACEMENTS),
        }
        result["link"] = {
            "expected_objects": EXPECTED_LINK_CLOSURE,
            "planned_objects": len(plan.link_closure_outputs),
            "returncode": None,
        }

        outputs, compile_failures = _compile_all(
            plan,
            build_root,
            jobs=jobs,
            timeout=compile_timeout,
        )
        result["compile"] = {
            **result["compile"],  # type: ignore[arg-type]
            "passed": len(outputs),
            "failed": len(compile_failures),
            "failures": compile_failures,
        }
        if compile_failures or len(outputs) != EXPECTED_COMPILE_SURFACE:
            raise GateError(
                f"NumPy compile surface is not green: {len(outputs)}/{EXPECTED_COMPILE_SURFACE}"
            )

        suffix = pcc_native_extension_suffix(current_platform_tag())
        artifact = _artifact_path(build_root, suffix)
        link_process = _link_module(
            plan,
            outputs,
            artifact,
            timeout=link_timeout,
        )
        dependencies, dep_process = (
            _dynamic_dependencies(artifact) if artifact.is_file() else ([], None)
        )
        dep_text = "\n".join(dependencies).lower()
        links_libpython = "libpython" in dep_text or "python.framework" in dep_text
        result["link"] = {
            **result["link"],  # type: ignore[arg-type]
            "returncode": link_process.returncode,
            "stdout": link_process.stdout[-4000:],
            "stderr": link_process.stderr[-8000:],
            "artifact": _relative(artifact),
            "artifact_sha256": (
                hashlib.sha256(artifact.read_bytes()).hexdigest()
                if artifact.is_file()
                else None
            ),
            "dependency_returncode": (
                dep_process.returncode if dep_process is not None else None
            ),
            "dependencies": dependencies,
            "links_libpython": links_libpython,
            "exports_pyinit": (
                _exports_pyinit(artifact) if artifact.is_file() else False
            ),
        }
        if link_process.returncode != 0 or not artifact.is_file():
            raise GateError(
                f"NumPy core link failed with exit {link_process.returncode}"
            )
        if links_libpython:
            raise GateError("NumPy pcc-native artifact links libpython")
        if result["link"]["exports_pyinit"] is not True:  # type: ignore[index]
            raise GateError(f"NumPy artifact does not export PyInit_{MODULE_NAME}")

        loader = _loader_probe(
            build_root,
            artifact,
            source_root=plan.source_root,
            timeout=loader_timeout,
        )
        result["loader"] = loader
        if loader.get("compile_returncode") != 0:
            raise GateError("strict self/no-libpython loader probe did not compile")
        if (
            loader.get("links_libpython") is not False
            or loader.get("links_llvm") is not False
        ):
            raise GateError("loader probe has a forbidden libpython/LLVM dependency")
        if (
            loader.get("entered_pyinit") is not True
            or loader.get("entered_py_mod_exec") is not True
        ):
            raise GateError("loader probe did not prove PyInit and Py_mod_exec entry")

        blocker_ratchet = evaluate_result(result, "numpy-core-head")
        result["first_blocker_ratchet"] = blocker_ratchet
        if blocker_ratchet.get("accepted") is not True:
            raise GateError(
                "NumPy first-blocker ratchet rejected the run: "
                + "; ".join(str(item) for item in blocker_ratchet.get("errors", []))
            )

        result["status"] = PASS
        result["artifacts"] = [
            _relative(artifact),
            _relative(Path(str(loader["executable"]))),
            _relative(result_path),
        ]
    except (GateError, OSError, subprocess.TimeoutExpired) as exc:
        result["failure"] = str(exc)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    _write_result(result_path, result)
    return result


def command_plan(args: argparse.Namespace) -> int:
    try:
        plan = build_plan(Path(args.source))
        validate_plan(plan)
    except GateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "source": {
                    "name": plan.source_name,
                    "version": plan.source_version,
                    "sha256": plan.source_sha256,
                },
                "compile_surface": len(plan.compile_surface),
                "link_closure": len(plan.link_closure_outputs),
                "module_target": plan.module_target,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    result = run_gate(
        Path(args.source),
        Path(args.build_root),
        Path(args.result),
        jobs=args.jobs,
        compile_timeout=args.compile_timeout,
        link_timeout=args.link_timeout,
        loader_timeout=args.loader_timeout,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == PASS else 1


def _artifact_from_result(result: dict[str, object]) -> Path:
    raw_path: object = None
    link = result.get("link")
    if isinstance(link, dict):
        raw_path = link.get("artifact")
    artifact = result.get("artifact")
    if raw_path is None and isinstance(artifact, dict):
        raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise GateError("existing NumPy result does not name an artifact")
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_file():
        raise GateError(f"existing NumPy artifact is missing: {path}")
    return path


def command_loader(args: argparse.Namespace) -> int:
    """Refresh only the strict loader frontier for an already-proven artifact."""
    started = time.monotonic()
    result_path = Path(args.result).resolve()
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise GateError("existing NumPy result root is not an object")
        source_root = Path(args.source).resolve()
        plan = build_plan(source_root)
        validate_plan(plan)
        artifact = _artifact_from_result(result)
        loader = _loader_probe(
            Path(args.build_root).resolve(),
            artifact,
            source_root=source_root,
            timeout=args.loader_timeout,
        )
        result["loader"] = loader
        result["status"] = FAIL
        result["failure"] = None
        if loader.get("compile_returncode") != 0:
            raise GateError("strict self/no-libpython loader probe did not compile")
        if (
            loader.get("links_libpython") is not False
            or loader.get("links_llvm") is not False
        ):
            raise GateError("loader probe has a forbidden libpython/LLVM dependency")
        if (
            loader.get("entered_pyinit") is not True
            or loader.get("entered_py_mod_exec") is not True
        ):
            raise GateError("loader probe did not prove PyInit and Py_mod_exec entry")
        blocker_ratchet = evaluate_result(result, args.lane)
        result["first_blocker_ratchet"] = blocker_ratchet
        if blocker_ratchet.get("accepted") is not True:
            raise GateError(
                "NumPy first-blocker ratchet rejected the loader refresh: "
                + "; ".join(str(item) for item in blocker_ratchet.get("errors", []))
            )
        result["status"] = PASS
    except (GateError, OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        if "result" not in locals() or not isinstance(result, dict):
            result = {"schema": "unknown", "status": FAIL}
        result["status"] = FAIL
        result["failure"] = str(exc)
    result["loader_refresh_seconds"] = round(time.monotonic() - started, 3)
    _write_result(result_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == PASS else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--source", default="projects/numpy-2.4.4")
    plan_parser.set_defaults(func=command_plan)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--source", default="projects/numpy-2.4.4")
    run_parser.add_argument("--build-root", default="build/head-truth/numpy-core")
    run_parser.add_argument(
        "--result", default="build/head-truth/numpy-core/result.json"
    )
    run_parser.add_argument("--jobs", type=int, default=min(12, os.cpu_count() or 1))
    run_parser.add_argument("--compile-timeout", type=int, default=300)
    run_parser.add_argument("--link-timeout", type=int, default=180)
    run_parser.add_argument("--loader-timeout", type=int, default=600)
    run_parser.set_defaults(func=command_run)

    loader_parser = subparsers.add_parser("loader")
    loader_parser.add_argument("--source", default="projects/numpy-2.4.4")
    loader_parser.add_argument("--build-root", default="build/head-truth/numpy-core")
    loader_parser.add_argument(
        "--result", default="build/head-truth/numpy-core/result.json"
    )
    loader_parser.add_argument("--lane", default="numpy-core-head")
    loader_parser.add_argument("--loader-timeout", type=int, default=120)
    loader_parser.set_defaults(func=command_loader)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
