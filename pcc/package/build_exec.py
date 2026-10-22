"""Generic package build action execution for pcc.

This is a package-agnostic execution harness. NumPy can use it as one input
to a future build, but the module deliberately works from generic source
metadata and explicit toolchain paths rather than package-specific rules.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

from pcc.package.schema import PCC_CAPI_HEADERS, pcc_native_extension_suffix

from .build_plan import (
    BuildCommand,
    _compile_commands_path,
    load_compile_commands,
    build_plan_for_artifact,
    load_meson_introspection_commands,
)
from .linkage import linkage_report
from .metadata import current_platform_tag, inspect_artifact, is_build_relevant_path
from .toolchain import toolchain_report

_C_SUFFIXES = {".c"}
_FORTRAN_SUFFIXES = {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"}
_LIBRARY_SUFFIXES = (".so", ".dylib", ".a", ".dll")
_CYTHON_REQUIREMENT_RE = re.compile(
    r"cython\s*>=\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE
)
_GENERATED_TARGET_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".c++",
    ".h",
    ".hh",
    ".hpp",
    ".inc",
    ".py",
    ".pxd",
    ".pyx",
    ".json",
    ".txt",
}

# pcc's self-contained Python C-API headers. pcc's Python.h declares the pcc
# object model (PyObjectHeader/type_tag), NOT CPython's ABI; numpy compiled
# against these emits a pcc-native extension instead of a CPython-ABI one.
# Match a CPython header include dir (so it can be dropped in pcc-native mode):
# .../pythonX.Y, .../Python.framework/..., .../include/pythonX.Y[suffix].
_CPYTHON_INCLUDE_DIR_RE = re.compile(
    r"(?:^|/)(?:python3\.\d+|Python\.framework)(?:/|$)|/include/python3?\.?\d"
)
_STALE_TOOLCHAIN_FLAG_REPLACEMENTS = {
    "-D_LIBCPP_ENABLE_ASSERTIONS=1": (
        "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST"
    ),
}
_DEPENDENCY_FLAGS = {"-MD", "-MMD", "-MP"}
_DEPENDENCY_VALUE_FLAGS = {"-MF", "-MQ", "-MT"}
_NINJA_BUILD_RE = re.compile(r"^build\s+(.+?):\s+(\S+)(?:\s+(.*))?$")


def _repo_root() -> Path:
    # build_exec.py lives at <root>/pcc/package/build_exec.py.
    return Path(__file__).resolve().parents[2]


def _pcc_capi_source_dir() -> Path | None:
    candidate = _repo_root() / "utils" / "fake_libc_include"
    return candidate if (candidate / "Python.h").is_file() else None


def _pcc_runtime_include_dir() -> Path | None:
    candidate = _repo_root() / "pcc" / "runtime" / "include"
    return candidate if candidate.is_dir() else None


def _materialize_pcc_capi_include(build_dir: Path, *, execute: bool) -> Path | None:
    """Curated include dir holding ONLY pcc's Python C-API headers.

    Deliberately NOT the whole ``utils/fake_libc_include``: its stub ``math.h``
    / ``complex.h`` would shadow the system libm that numpy's C core needs.
    Mirrors the validated ``/tmp/pcc_capi`` probe shape (Python.h + the three
    directly-included helpers). Returns the dir path (always, so the include
    flag can be planned without executing); copies the headers only when
    ``execute`` is set.
    """
    src = _pcc_capi_source_dir()
    if src is None:
        return None
    dest = build_dir / "pcc-capi-include"
    if execute:
        dest.mkdir(parents=True, exist_ok=True)
        for header in PCC_CAPI_HEADERS:
            header_src = src / header
            if header_src.is_file():
                with open(header_src, "rb") as source:
                    contents = source.read()
                with open(dest / header, "wb") as output:
                    output.write(contents)
    return dest


def _is_cpython_include_dir(directory: str) -> bool:
    return bool(_CPYTHON_INCLUDE_DIR_RE.search(directory))


def _redirect_pcc_native_includes(
    command: list[str], capi_dir: Path, runtime_include: Path | None
) -> list[str]:
    """pcc-native include redirect for a single compile command.

    Drops CPython header include dirs (``-I``/``-isystem``) and appends pcc's
    C-API + runtime includes, so ``#include <Python.h>`` resolves to pcc's
    object model rather than CPython's ABI. System libc and the package's own
    include dirs are left untouched (pcc includes are appended last, so a real
    package header always wins; they only fill the Python.h gap left by the
    dropped CPython dirs). Generic — no package-specific rules.
    """
    if not command:
        return command
    out = [command[0]]
    i = 1
    while i < len(command):
        tok = command[i]
        if tok in ("-I", "-isystem") and i + 1 < len(command):
            if _is_cpython_include_dir(command[i + 1]):
                i += 2
                continue
            out.append(tok)
            out.append(command[i + 1])
            i += 2
            continue
        if tok.startswith("-I") and len(tok) > 2:
            if _is_cpython_include_dir(tok[2:]):
                i += 1
                continue
        out.append(tok)
        i += 1
    out.append("-I" + str(capi_dir))
    if runtime_include is not None:
        out.append("-I" + str(runtime_include))
    return out


def _normalize_stale_toolchain_flags(command: list[str]) -> list[str]:
    """Replay current Meson mappings for flags removed by newer toolchains."""

    return [_STALE_TOOLCHAIN_FLAG_REPLACEMENTS.get(token, token) for token in command]


def _strip_dependency_output_flags(command: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(command):
        token = command[i]
        if token in _DEPENDENCY_FLAGS:
            i += 1
            continue
        if token in _DEPENDENCY_VALUE_FLAGS and i + 1 < len(command):
            i += 2
            continue
        if any(
            token.startswith(prefix) and token != prefix
            for prefix in _DEPENDENCY_VALUE_FLAGS
        ):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def _replace_command_output(command: list[str], output: Path) -> list[str]:
    out = list(command)
    i = 0
    while i + 1 < len(out):
        if out[i] == "-o":
            out[i + 1] = str(output)
            return out
        i += 1
    out.extend(["-o", str(output)])
    return out


def _logical_ninja_lines(path: Path) -> list[str]:
    physical = path.read_text(encoding="utf-8").splitlines()
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


def _ninja_graph(build_dir: Path) -> dict[str, dict[str, object]]:
    path = build_dir / "build.ninja"
    if not path.is_file():
        return {}
    graph: dict[str, dict[str, object]] = {}
    current_outputs: list[str] = []
    for line in _logical_ninja_lines(path):
        match = _NINJA_BUILD_RE.match(line)
        if match is not None:
            current_outputs = shlex.split(match.group(1))
            inputs = tuple(
                token
                for token in shlex.split(match.group(3) or "")
                if token not in {"|", "||"}
            )
            for output in current_outputs:
                graph[output] = {
                    "inputs": inputs,
                    "rule": match.group(2),
                    "variables": {},
                }
            continue
        if current_outputs and line.startswith(" ") and "=" in line:
            name, value = line.strip().split("=", 1)
            for output in current_outputs:
                variables = graph[output]["variables"]
                if isinstance(variables, dict):
                    variables[name.strip()] = value.strip()
            continue
        if line and not line.startswith(" "):
            current_outputs = []
    return graph


def _relative_meson_output(output: str, build_dir: Path) -> str:
    path = Path(output)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(build_dir.resolve()))
        except ValueError:
            return str(path)
    return str(path)


def _meson_target_replay_plan(root: Path, target: str) -> dict[str, object]:
    build_dir = _meson_build_dir_from_intro_path(root, _meson_intro_targets_path(root))
    graph = _ninja_graph(build_dir)
    requested = _relative_meson_output(target, build_dir)
    if requested not in graph:
        candidates = [name for name in graph if Path(name).name == Path(requested).name]
        if len(candidates) == 1:
            requested = candidates[0]
        else:
            return {
                "ok": False,
                "target": target,
                "build_dir": str(build_dir),
                "objects": [],
                "link_args": [],
                "diagnostic": "PCC-PKG-MESON-TARGET-MISSING",
            }

    objects: list[str] = []
    seen_objects: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        edge = graph.get(node)
        if edge is None:
            return
        inputs = edge.get("inputs")
        if not isinstance(inputs, tuple):
            return
        for dependency in inputs:
            if dependency.endswith(".o"):
                if dependency not in seen_objects:
                    seen_objects.add(dependency)
                    objects.append(dependency)
            elif dependency in graph:
                visit(dependency)

    visit(requested)
    edge = graph[requested]
    object_sources: dict[str, str] = {}
    for object_output in objects:
        object_edge = graph.get(object_output)
        inputs = object_edge.get("inputs") if object_edge is not None else None
        if not isinstance(inputs, tuple) or not inputs:
            continue
        source_path = Path(inputs[0])
        if not source_path.is_absolute():
            source_path = build_dir / source_path
        object_sources[object_output] = str(source_path.resolve())
    variables = edge.get("variables")
    raw_link_args = (
        variables.get("LINK_ARGS", "") if isinstance(variables, dict) else ""
    )
    link_args = shlex.split(str(raw_link_args))
    recursive_nodes = visited - {requested}
    filtered_link_args: list[str] = []
    for token in link_args:
        relative = _relative_meson_output(token, build_dir)
        if relative in recursive_nodes:
            continue
        filtered_link_args.append(token)
    return {
        "ok": bool(objects),
        "target": requested,
        "build_dir": str(build_dir),
        "objects": objects,
        "object_sources": object_sources,
        "link_args": filtered_link_args,
        "rule": edge.get("rule"),
        "recursive_nodes": sorted(recursive_nodes),
        "diagnostic": None if objects else "PCC-PKG-MESON-TARGET-EMPTY",
    }


def _meson_intro_targets_path(root: Path) -> Path | None:
    candidates = (
        root / "meson-info" / "intro-targets.json",
        root / "build" / "meson-info" / "intro-targets.json",
        root
        / "build"
        / "pcc-package"
        / "meson-build"
        / "meson-info"
        / "intro-targets.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _meson_build_dir_from_intro_path(root: Path, intro_path: Path | None) -> Path:
    if intro_path is None:
        return root / "build" / "pcc-package" / "meson-build"
    if intro_path.parent.name == "meson-info":
        return intro_path.parent.parent
    return root / "build" / "pcc-package" / "meson-build"


def _meson_shared_extension_targets(root: Path) -> dict[str, str]:
    """Map import-qualified module names to Meson target paths."""

    intro_path = _meson_intro_targets_path(root)
    if intro_path is None:
        return {}
    build_dir = _meson_build_dir_from_intro_path(root, intro_path).resolve()
    try:
        entries = json.loads(intro_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(entries, list):
        return {}
    targets: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "shared module":
            continue
        filenames = entry.get("filename")
        if not isinstance(filenames, list):
            continue
        for raw_filename in filenames:
            filename = Path(str(raw_filename)).expanduser()
            if not filename.is_absolute():
                filename = build_dir / filename
            try:
                relative = filename.resolve().relative_to(build_dir)
            except ValueError:
                continue
            if relative.suffix.lower() not in {".so", ".pyd", ".dll", ".dylib"}:
                continue
            module_leaf = relative.stem.split(".", 1)[0]
            module = ".".join((*relative.parent.parts, module_leaf))
            targets[module] = relative.as_posix()
    return targets


def _module_source(
    root: Path, build_dir: Path, module: str
) -> tuple[Path | None, bool]:
    parts = module.split(".")
    for base in (build_dir, root):
        module_file = base.joinpath(*parts).with_suffix(".py")
        package_file = base.joinpath(*parts, "__init__.py")
        if module_file.is_file():
            return module_file, False
        if package_file.is_file():
            return package_file, True
    return None, False


def _module_scope_imports(statements: list[ast.stmt]):
    """Yield imports executed in module control flow, excluding lazy bodies."""

    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            yield statement
            continue
        if isinstance(
            statement,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        for _, value in ast.iter_fields(statement):
            if (
                isinstance(value, list)
                and value
                and all(isinstance(item, ast.stmt) for item in value)
            ):
                yield from _module_scope_imports(value)


def _import_candidates(
    node: ast.Import | ast.ImportFrom, package: str
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if node.level:
        parts = package.split(".") if package else []
        keep = max(0, len(parts) - node.level + 1)
        base = ".".join(parts[:keep])
        if node.module:
            base = ".".join(item for item in (base, node.module) if item)
    else:
        base = node.module or ""
    candidates = [base] if base else []
    candidates.extend(
        ".".join(item for item in (base, alias.name) if item)
        for alias in node.names
        if alias.name != "*"
    )
    return tuple(candidates)


def discover_eager_meson_extension_targets(
    name: str, path: str | Path
) -> tuple[dict[str, str], ...]:
    """Find Meson extensions reachable from package module-scope imports.

    This is deliberately package-neutral. Imports nested inside functions or
    classes are lazy and do not belong to the package's import-time extension
    closure.
    """

    root = Path(path).expanduser().resolve()
    intro_path = _meson_intro_targets_path(root)
    if intro_path is None:
        return ()
    build_dir = _meson_build_dir_from_intro_path(root, intro_path).resolve()
    targets = _meson_shared_extension_targets(root)
    if not targets:
        return ()
    normalized_name = name.lower().replace("-", "_")
    seeds: list[str] = []
    direct_packages: set[str] = set()
    for base in (build_dir, root):
        try:
            children = sorted(base.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and (child / "__init__.py").is_file():
                direct_packages.add(child.name)
    preferred = sorted(
        item
        for item in direct_packages
        if item.lower().replace("-", "_") == normalized_name
    )
    seeds.extend(preferred or sorted(direct_packages))

    seen: set[str] = set()
    queue = list(seeds)
    selected: list[dict[str, str]] = []
    suffix = pcc_native_extension_suffix(current_platform_tag())
    while queue:
        module = queue.pop(0)
        if module in seen:
            continue
        seen.add(module)
        target = targets.get(module)
        if target is not None:
            relative = Path(target)
            output = relative.with_name(relative.stem.split(".", 1)[0] + suffix)
            selected.append(
                {
                    "module": module,
                    "target": target,
                    "output": str((build_dir / output).resolve()),
                }
            )
            continue
        source, is_package = _module_source(root, build_dir, module)
        if source is None:
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        package = module if is_package else module.rpartition(".")[0]
        for import_node in _module_scope_imports(tree.body):
            for candidate in _import_candidates(import_node, package):
                if not any(
                    candidate == seed or candidate.startswith(seed + ".")
                    for seed in seeds
                ):
                    continue
                candidate_source, _ = _module_source(root, build_dir, candidate)
                if candidate not in seen and (
                    candidate in targets or candidate_source is not None
                ):
                    queue.append(candidate)
    return tuple(selected)


def _meson_ninja_target(build_dir: Path, raw_path: str) -> str:
    path = Path(raw_path)
    try:
        if path.is_absolute():
            return str(path.relative_to(build_dir))
    except ValueError:
        pass
    return str(path)


def _load_meson_generated_targets(root: Path) -> tuple[str, ...]:
    intro_path = _meson_intro_targets_path(root)
    if intro_path is None:
        return ()
    build_dir = _meson_build_dir_from_intro_path(root, intro_path)
    try:
        entries = json.loads(intro_path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    if not isinstance(entries, list):
        return ()
    targets: list[str] = []
    for target in entries:
        if not isinstance(target, dict):
            continue
        if str(target.get("type") or "").lower() == "custom":
            filenames = target.get("filename")
            if not isinstance(filenames, list):
                filenames = ()
            for filename in filenames:
                targets.append(_meson_ninja_target(build_dir, str(filename)))
        groups = target.get("target_sources")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            generated_sources = group.get("generated_sources")
            if not isinstance(generated_sources, list):
                generated_sources = ()
            for generated in generated_sources:
                targets.append(_meson_ninja_target(build_dir, str(generated)))
    return tuple(dict.fromkeys(targets))


def _is_file_custom_target(target: str) -> bool:
    """Return whether a Ninja custom target names a generated file.

    `ninja -t targets all` also exposes Meson control targets such as
    `meson-internal__test`. Those can depend on `all`, so executing them here
    accidentally turns "materialize generated inputs" into a full project
    build. Keep this filter file-shaped and package-agnostic.
    """
    if not target or target.startswith("meson-internal__"):
        return False
    if target in {"all", "test", "install", "benchmark", "clean", "uninstall"}:
        return False
    path = Path(target)
    return path.suffix.lower() in _GENERATED_TARGET_SUFFIXES


def _ninja_graph_host_python_command(build_dir: Path) -> str | None:
    """Return the first generated command that can start a host interpreter."""
    try:
        lines = (build_dir / "build.ninja").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("command ="):
            continue
        command = stripped[len("command =") :].strip()
        token_text = command
        for separator in ("&&", "||", ";", "|", "(", ")"):
            token_text = token_text.replace(separator, " ")
        tokens = token_text.split()
        for index, raw in enumerate(tokens):
            token = raw.strip("'\"` ,")
            base = Path(token).name.lower()
            is_python = base == "python" or base.startswith("python3")
            is_pypy = base == "pypy" or base.startswith("pypy3")
            is_cython = base == "cython" or base.startswith("cython3")
            is_env_python = base in {"$python", "${python}", "%python%"}
            is_uv_python = base in {"uv", "uvx"} and (
                base == "uvx"
                or (index + 1 < len(tokens) and tokens[index + 1].lower() == "run")
            )
            if is_python or is_pypy or is_cython or is_env_python or is_uv_python:
                return command
    return None


def _load_ninja_custom_targets(
    build_dir: Path,
    search_paths: list[str] | tuple[str, ...],
    timeout: int,
) -> tuple[str, ...]:
    ninja = _find_tool(("ninja",), search_paths) or "ninja"
    env = os.environ.copy()
    env["PATH"] = _path_env(search_paths)
    try:
        proc = subprocess.run(
            [ninja, "-C", str(build_dir), "-t", "targets", "all"],
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except Exception:
        return ()
    if proc.returncode != 0:
        return ()
    targets: list[str] = []
    for line in proc.stdout.splitlines():
        target, sep, kind = line.partition(":")
        target = target.strip()
        if (
            sep
            and kind.strip().startswith("CUSTOM_COMMAND")
            and _is_file_custom_target(target)
        ):
            targets.append(target)
    return tuple(dict.fromkeys(targets))


def _iter_source_files(root: Path, suffixes: set[str]) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in suffixes
        and is_build_relevant_path(root, path)
        and "__pycache__" not in path.parts
        and ".git" not in path.parts
    )


def _path_env(search_paths: list[str] | tuple[str, ...]) -> str:
    prefix = os.pathsep.join(str(Path(path).expanduser()) for path in search_paths)
    existing = os.environ.get("PATH", "")
    if prefix and existing:
        return prefix + os.pathsep + existing
    return prefix or existing


def _find_tool(
    names: tuple[str, ...], search_paths: list[str] | tuple[str, ...]
) -> str | None:
    path_env = _path_env(search_paths)
    for name in names:
        found = shutil.which(name, path=path_env)
        if found:
            return found
    return None


def _meson_setup_command(
    root: Path,
    build_dir: Path,
    search_paths: list[str] | tuple[str, ...],
) -> list[str]:
    meson_tool = _find_tool(("meson",), search_paths)
    if meson_tool is not None:
        return [meson_tool, "setup", str(build_dir), str(root)]
    vendored = root / "vendored-meson" / "meson" / "meson.py"
    if vendored.exists():
        return [sys.executable, str(vendored), "setup", str(build_dir), str(root)]
    return ["meson", "setup", str(build_dir), str(root)]


def _resolve_command_tool(token: str, search_paths: list[str] | tuple[str, ...]) -> str:
    if "/" in token or "\\" in token:
        return token
    found = shutil.which(token, path=_path_env(search_paths))
    return found or token


def _compiler_behind_launcher(command: list[str]) -> str | None:
    """Return the compiler executable from a possibly launcher-prefixed command."""

    if not command:
        return None
    launcher = Path(command[0]).name
    if launcher in {"ccache", "sccache", "distcc", "icecc"} and len(command) > 1:
        return command[1]
    return command[0]


def _run_action(
    *,
    kind: str,
    command: list[str],
    source: Path | None,
    output: Path | None = None,
    cwd: Path,
    execute: bool,
    search_paths: list[str] | tuple[str, ...],
    timeout: int,
) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": kind,
        "command": command,
        "output": str(output) if output is not None else None,
        "source": str(source) if source is not None else None,
        "status": "planned",
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    if not execute:
        return row
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = _path_env(search_paths)
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        row["status"] = "blocked"
        row["stderr"] = str(exc)
        return row
    except subprocess.TimeoutExpired as exc:
        row["status"] = "timeout"
        row["stdout"] = exc.stdout or ""
        row["stderr"] = exc.stderr or ""
        return row
    row["returncode"] = proc.returncode
    row["stdout"] = proc.stdout
    row["stderr"] = proc.stderr
    row["status"] = "passed" if proc.returncode == 0 else "failed"
    return row


def _library_names_for_request(name: str) -> tuple[str, ...]:
    lowered = name.lower()
    if lowered == "blas":
        return ("openblas", "blas")
    return (lowered,)


def _find_library_binding(
    request: str,
    library_dirs: list[str] | tuple[str, ...],
) -> dict[str, object]:
    names = _library_names_for_request(request)
    for directory in library_dirs:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_file():
                continue
            lower = child.name.lower()
            if not lower.endswith(_LIBRARY_SUFFIXES):
                continue
            for candidate in names:
                if lower.startswith("lib" + candidate) or lower.startswith(candidate):
                    stem = child.name
                    if stem.startswith("lib"):
                        stem = stem[3:]
                    for suffix in _LIBRARY_SUFFIXES:
                        if stem.endswith(suffix):
                            stem = stem[: -len(suffix)]
                            break
                    return {
                        "request": request,
                        "found": True,
                        "link_name": stem,
                        "path": str(child),
                    }
    return {
        "request": request,
        "found": False,
        "link_name": request,
        "path": None,
    }


def generated_c_provenance(root: str | Path) -> list[dict[str, object]]:
    source_root = Path(root).expanduser().resolve()
    rows: list[dict[str, object]] = []
    for pyx in _iter_source_files(source_root, {".pyx"}):
        generated = pyx.with_suffix(".c")
        status = "missing"
        if generated.exists():
            try:
                status = (
                    "up_to_date"
                    if generated.stat().st_mtime >= pyx.stat().st_mtime
                    else "stale"
                )
            except OSError:
                status = "unknown"
        rows.append(
            {
                "pyx": str(pyx),
                "generated_c": str(generated),
                "exists": generated.exists(),
                "status": status,
            }
        )
    return rows


def _cython_min_version(requires: tuple[str, ...]) -> str | None:
    for requirement in requires:
        match = _CYTHON_REQUIREMENT_RE.search(requirement)
        if match is not None:
            return match.group(1)
    return None


def _owned_compile_argv(command: BuildCommand, root: Path, output: Path, abi_mode: str) -> list[str]:
    """Translate a C compile action, never execute its recorded tool or shell."""
    cwd = Path(command.directory).resolve() if command.directory else root
    source = Path(command.file)
    if not source.is_absolute():
        source = cwd / source
    tokens = shlex.split(command.command)
    index = 1
    if tokens and Path(tokens[0]).name in {"ccache", "sccache"}:
        index = 2
    args = ["--backend=self", "--emit-obj", str(output)]
    while index < len(tokens):
        token = tokens[index]
        if token == "-c":
            index += 1
            continue
        if token == "-o":
            if index + 1 >= len(tokens):
                raise ValueError("missing output in compile command")
            index += 2
            continue
        if token.startswith("-o") and len(token) > 2:
            index += 1
            continue
        if token in {"-I", "-D", "-U"}:
            if index + 1 >= len(tokens):
                raise ValueError("missing value for " + token)
            value = tokens[index + 1]
            index += 2
        elif token.startswith(("-I", "-D", "-U")) and len(token) > 2:
            value = token[2:]
            token = token[:2]
            index += 1
        elif token in {"-O0", "-O1", "-O2", "-O3"}:
            args.append(token)
            index += 1
            continue
        elif not token.startswith("-"):
            candidate = Path(token)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if candidate.resolve() != source.resolve():
                raise ValueError("unsupported command operand: " + token)
            index += 1
            continue
        else:
            raise ValueError("unsupported owned C compile option: " + token)
        if token == "-I":
            directory = Path(value)
            if not directory.is_absolute():
                directory = cwd / directory
            if abi_mode == "pcc-native" and _is_cpython_include_dir(str(directory)):
                continue
            args.append("--cpp-arg=-I" + str(directory.resolve()))
        else:
            args.append("--cpp-arg=" + token + value)
    args.append(str(source.resolve()))
    return args


def _execute_owned_build_actions(
    name, path, *, include_dirs, execute, regenerate_cython, run_f2py,
    link_output, abi_mode, from_compile_commands, from_meson_introspection,
    meson_target, configure_meson, enforce_generated_c, jobs, timeout, owned_compiler,
    library_dirs, libraries,
):
    """Compile supported source actions through the actual owned C driver.

    The source/graph readers are shared with the explicit host executor. Only
    the execution boundary differs; arbitrary recorded commands are never run.
    A job count is an upper bound; this initial owned executor stays serial.
    Compiler children use the native timed process boundary, never cc/ninja.
    """
    import tempfile

    root = Path(path).expanduser().resolve()
    plan = build_plan_for_artifact(name, root)
    actions = []
    diagnostics = []
    replay = None
    replay_outputs = []
    generated = generated_c_provenance(root)

    def diagnose(code, message):
        diagnostics.append({"code": code, "message": message})

    compiler_command = []
    if sys.implementation.name == "pcc":
        if owned_compiler:
            compiler_command = [str(owned_compiler)]
        elif execute:
            diagnose("PCC-PKG-OWNED-COMPILER-REQUIRED", "native build replay requires its parent PCC compiler")
    else:
        compiler_command = [sys.executable, "-m", "pcc"]
    if not root.is_dir():
        diagnose("PCC-PKG-BUILD-PATH-MISSING", "package source directory is missing")
    if regenerate_cython:
        diagnose("PCC-PKG-OWNED-CYTHON-UNAVAILABLE", "Cython source generation has no owned execution provider")
    if run_f2py:
        diagnose("PCC-PKG-OWNED-F2PY-UNAVAILABLE", "f2py source generation has no owned execution provider")
    if libraries and link_output is None:
        diagnose("PCC-PKG-OWNED-LIBRARY-LINK-UNAVAILABLE", "requested libraries require an owned native link action")
    if link_output is not None:
        diagnose("PCC-PKG-OWNED-SHARED-LINK-UNAVAILABLE", "native package shared-library linking has no cross-platform owned provider; C object compilation remains available")
    if enforce_generated_c:
        for row in generated:
            if row["status"] in {"missing", "stale"}:
                diagnose("PCC-PKG-GENERATED-C-" + row["status"].upper(), str(row["generated_c"]))
    if configure_meson and not (_meson_build_dir_from_intro_path(root, _meson_intro_targets_path(root)) / "build.ninja").is_file():
        diagnose("PCC-PKG-OWNED-MESON-GRAPH-REQUIRED", "configure the source with the owned native Meson tool before target replay")

    commands = []
    if from_compile_commands or from_meson_introspection or meson_target:
        if from_meson_introspection:
            commands = list(load_meson_introspection_commands(root, root))
        elif from_compile_commands:
            compile_path = _compile_commands_path(root)
            commands = list(load_compile_commands(compile_path)) if compile_path else []
        else:
            commands = list(plan.commands)
        if not commands:
            diagnose("PCC-PKG-BUILD-COMMANDS-MISSING", "requested source graph has no compile actions")
        if meson_target:
            replay = _meson_target_replay_plan(root, meson_target)
            if not replay["ok"]:
                diagnose(str(replay["diagnostic"]), "requested Meson target has no owned object closure")
                commands = []
            else:
                selected = []
                for object_name in replay["objects"]:
                    expected_source = replay["object_sources"].get(object_name)
                    candidates = []
                    for command in commands:
                        base = Path(command.directory).resolve() if command.directory else root
                        source = Path(command.file)
                        if not source.is_absolute():
                            source = base / source
                        if expected_source and str(source.resolve()) == expected_source:
                            candidates.append(command)
                    if len(candidates) > 1:
                        expected_output = (Path(replay["build_dir"]) / object_name).resolve()
                        exact = []
                        for candidate in candidates:
                            if candidate.output:
                                base = Path(candidate.directory).resolve() if candidate.directory else root
                                candidate_output = Path(candidate.output)
                                if not candidate_output.is_absolute():
                                    candidate_output = base / candidate_output
                                if candidate_output.resolve() == expected_output:
                                    exact.append(candidate)
                        candidates = exact
                    if len(candidates) != 1:
                        diagnose("PCC-PKG-MESON-TARGET-COMPILE-ACTION-MISSING", "target object has no unique source action: " + object_name)
                    else:
                        selected.append(candidates[0])
                        replay_outputs.append(object_name)
                commands = selected
        build_root = _meson_build_dir_from_intro_path(root, _meson_intro_targets_path(root))
        if (build_root / "build.ninja").is_file():
            graph = _ninja_graph(build_root)
            nodes = list(graph) if replay is None else list(replay.get("objects", []))
            seen_nodes = set()
            while nodes:
                node = nodes.pop()
                if node in seen_nodes:
                    continue
                seen_nodes.add(node)
                edge = graph.get(node)
                if edge:
                    if str(edge["rule"]).startswith("CUSTOM_COMMAND"):
                        diagnose("PCC-PKG-OWNED-GENERATED-COMMAND-UNAVAILABLE", "generated source action has no owned provider: " + node)
                    nodes.extend(edge["inputs"])
    else:
        for source in _iter_source_files(root, _C_SUFFIXES | {".cc", ".cpp", ".cxx", ".c++"} | _FORTRAN_SUFFIXES):
            relative = source.relative_to(root)
            output = root / "build" / "pcc-package" / "owned" / (str(relative) + ".o")
            language = "c" if source.suffix.lower() == ".c" else "fortran" if source.suffix.lower() in _FORTRAN_SUFFIXES else "cxx"
            commands.append(BuildCommand(str(source), "cc -c " + shlex.quote(str(source)), str(root), language, "cc", str(output), (), (), (), (), ()))

    capi_dir = None
    runtime_include = None
    if abi_mode == "pcc-native" and commands:
        capi_dir = _materialize_pcc_capi_include(root / "build" / "pcc-package", execute=execute and not diagnostics)
        runtime_include = _pcc_runtime_include_dir()
        if capi_dir is None or runtime_include is None:
            diagnose("PCC-PKG-CAPI-INCLUDE-MISSING", "owned C build requires PCC C-API headers")
    for index, command in enumerate(commands):
        cwd = Path(command.directory).resolve() if command.directory else root
        source = Path(command.file)
        if not source.is_absolute():
            source = cwd / source
        output = Path(command.output) if command.output else root / "build" / "pcc-package" / "owned" / (source.name + "." + str(index) + ".o")
        if not output.is_absolute():
            output = cwd / output
        output = output.resolve()
        if replay_outputs:
            output = root / "build" / "pcc-package" / "owned-target" / (str(index) + "-" + Path(replay_outputs[index]).name)
        row = {"kind": "c_compile", "command": [], "source": str(source.resolve()), "output": str(output), "status": "planned", "returncode": None, "stdout": "", "stderr": "", "execution_owner": "pcc"}
        actions.append(row)
        if command.language != "c":
            row["status"] = "blocked"
            diagnose("PCC-PKG-OWNED-LANGUAGE-UNAVAILABLE", "owned package compilation does not yet support " + command.language)
            continue
        try:
            argv = _owned_compile_argv(command, root, output, abi_mode)
            for directory in include_dirs:
                include = Path(directory).expanduser()
                if not include.is_absolute():
                    include = root / include
                if abi_mode == "pcc-native" and _is_cpython_include_dir(str(include)):
                    continue
                argv.insert(-1, "--cpp-arg=-I" + str(include.resolve()))
            if capi_dir is not None:
                argv.insert(-1, "--cpp-arg=-I" + str(capi_dir))
                argv.insert(-1, "--cpp-arg=-I" + str(runtime_include))
            if source.resolve() == output:
                raise ValueError("compile output aliases its source")
            row["command"] = (compiler_command or ["pcc"]) + argv
        except ValueError as exc:
            row["status"] = "blocked"
            row["stderr"] = str(exc)
            diagnose("PCC-PKG-OWNED-COMPILE-OPTION-UNAVAILABLE", str(exc))
            continue
        if not execute:
            continue
        if diagnostics:
            row["status"] = "blocked"
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="pcc-owned-build-") as scratch:
                staged = os.path.join(scratch, "output.o")
                compile_argv = list(argv)
                compile_argv[compile_argv.index("--emit-obj") + 1] = staged
                row["executed_command"] = compiler_command + compile_argv
                process = subprocess.run(
                    compiler_command + compile_argv, capture_output=True,
                    text=True, timeout=timeout,
                )
                row["returncode"] = process.returncode
                row["stdout"] = process.stdout or ""
                row["stderr"] = process.stderr or ""
                if process.returncode == 0 and os.path.isfile(staged):
                    os.replace(staged, str(output))
                    row["status"] = "passed"
                else:
                    row["status"] = "failed"
        except subprocess.TimeoutExpired as exc:
            row["status"] = "timeout"
            row["stderr"] = str(exc)
        except Exception as exc:
            row["status"] = "failed"
            row["stderr"] = str(exc)
        if row["status"] != "passed":
            diagnose("PCC-PKG-BUILD-ACTION-FAILED", "owned C object action did not complete: " + str(source))
    ok = not diagnostics and all(row["status"] in {"passed", "planned"} for row in actions)
    return {"ok": ok, "name": name, "path": str(root), "execute": execute,
            "build_backend": "pcc-self", "build_mode_requested": "owned", "build_ownership": "owned",
            "host_assisted": False, "host_python": None,
            "host_free_build_claim": bool(sys.implementation.name == "pcc" and execute and ok and actions and all(row["status"] == "passed" for row in actions)),
            "compiler_execution": "native-pcc" if sys.implementation.name == "pcc" else "cpython-hosted",
            "include_dirs": [str(path) for path in include_dirs],
            "library_dirs": [str(path) for path in library_dirs], "libraries": list(libraries),
            "from_compile_commands": from_compile_commands,
            "from_meson_introspection": from_meson_introspection, "meson_target": meson_target,
            "meson_target_replay": replay, "configure_meson": configure_meson,
            "jobs": jobs, "effective_jobs": 1, "timeout": timeout,
            "timeout_enforcement": "owned-process-deadline",
            "generated_c_provenance": generated, "actions": actions, "diagnostics": diagnostics,
            "vendor_bindings": [], "linkage": None, "toolchain": {"owner": "pcc"}, "build_plan": plan.as_dict()}


def execute_build_actions(
    name: str,
    path: str | Path,
    *,
    search_paths: list[str] | tuple[str, ...] = (),
    include_dirs: list[str] | tuple[str, ...] = (),
    library_dirs: list[str] | tuple[str, ...] = (),
    execute: bool = False,
    regenerate_cython: bool = False,
    run_f2py: bool = False,
    link_output: str | Path | None = None,
    libraries: list[str] | tuple[str, ...] = (),
    abi_mode: str = "pcc-native",
    from_compile_commands: bool = False,
    from_meson_introspection: bool = False,
    meson_target: str | None = None,
    configure_meson: bool = False,
    enforce_generated_c: bool = False,
    jobs: int = 1,
    timeout: int = 30,
    build_mode: str = "owned",
    owned_compiler: str | None = None,
) -> dict[str, object]:
    if build_mode not in {"owned", "host"}:
        raise ValueError("build mode must be owned or host")
    if jobs < 1 or timeout < 1:
        raise ValueError("jobs and timeout must be positive")
    if build_mode == "owned":
        if search_paths:
            raise ValueError("tool search paths require --build-mode=host")
        return _execute_owned_build_actions(
            name, path, include_dirs=include_dirs, execute=execute,
            regenerate_cython=regenerate_cython, run_f2py=run_f2py,
            link_output=link_output, abi_mode=abi_mode,
            from_compile_commands=from_compile_commands,
            from_meson_introspection=from_meson_introspection,
            meson_target=meson_target, configure_meson=configure_meson,
            enforce_generated_c=enforce_generated_c, jobs=jobs, timeout=timeout,
            owned_compiler=owned_compiler, library_dirs=library_dirs, libraries=libraries,
        )
    root = Path(path).expanduser().resolve()
    metadata = inspect_artifact(name, root)
    plan = build_plan_for_artifact(name, root)
    actions: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    generated_c_rows = generated_c_provenance(root)
    native_fallbacks = set(metadata.native_library_fallbacks)

    toolchain = toolchain_report(
        search_paths=search_paths,
        library_dirs=library_dirs,
        require_fortran=bool(metadata.fortran_sources),
        require_cython=(regenerate_cython or configure_meson)
        and bool(metadata.cython_sources),
        require_f2py=run_f2py and bool(metadata.fortran_sources),
        require_blas=bool(metadata.blas_indicators) and "blas" not in native_fallbacks,
        require_lapack=bool(metadata.lapack_indicators)
        and "lapack" not in native_fallbacks,
        min_cython_version=_cython_min_version(metadata.pyproject_requires),
    )
    diagnostics.extend(toolchain["diagnostics"])  # type: ignore[arg-type]

    build_dir = root / "build" / "pcc-package"
    meson_build_dir = build_dir / "meson-build"
    if execute:
        build_dir.mkdir(parents=True, exist_ok=True)
    object_outputs: list[Path] = []

    cython_tool = _find_tool(("cython", "cython3"), search_paths)
    if (
        metadata.cython_sources
        and not metadata.generated_c_artifacts
        and not regenerate_cython
        and not from_compile_commands
        and not from_meson_introspection
    ):
        diagnostics.append(
            {
                "code": "PCC-PKG-CYTHON-REGENERATION-REQUIRED",
                "message": "cython sources exist without generated C; pass --regenerate-cython",
            }
        )
    if regenerate_cython:
        for rel in metadata.cython_sources:
            source = root / rel
            output = source.with_suffix(".c")
            if cython_tool is None:
                actions.append(
                    {
                        "kind": "cython_regenerate",
                        "command": ["cython", str(source), "-o", str(output)],
                        "source": str(source),
                        "status": "blocked" if execute else "planned",
                        "returncode": None,
                        "stdout": "",
                        "stderr": "cython executable not found" if execute else "",
                    }
                )
            else:
                actions.append(
                    _run_action(
                        kind="cython_regenerate",
                        command=[cython_tool, str(source), "-o", str(output)],
                        source=source,
                        output=output,
                        cwd=root,
                        execute=execute,
                        search_paths=search_paths,
                        timeout=timeout,
                    )
                )

    if regenerate_cython and execute:
        generated_c_rows = generated_c_provenance(root)
    if enforce_generated_c:
        for row in generated_c_rows:
            if row["status"] == "missing":
                diagnostics.append(
                    {
                        "code": "PCC-PKG-GENERATED-C-MISSING",
                        "message": "generated C artifact is missing for Cython source",
                        "pyx": str(row["pyx"]),
                        "generated_c": str(row["generated_c"]),
                    }
                )
            elif row["status"] == "stale":
                diagnostics.append(
                    {
                        "code": "PCC-PKG-GENERATED-C-STALE",
                        "message": "generated C artifact is older than its Cython source",
                        "pyx": str(row["pyx"]),
                        "generated_c": str(row["generated_c"]),
                    }
                )

    f2py_tool = _find_tool(("f2py", "f2py3"), search_paths)
    if run_f2py:
        for source in _iter_source_files(root, _FORTRAN_SUFFIXES):
            if f2py_tool is None:
                actions.append(
                    {
                        "kind": "f2py_build",
                        "command": ["f2py", "-c", str(source)],
                        "source": str(source),
                        "status": "blocked" if execute else "planned",
                        "returncode": None,
                        "stdout": "",
                        "stderr": "f2py executable not found" if execute else "",
                    }
                )
            else:
                actions.append(
                    _run_action(
                        kind="f2py_build",
                        command=[f2py_tool, "-c", str(source)],
                        source=source,
                        cwd=root,
                        execute=execute,
                        search_paths=search_paths,
                        timeout=timeout,
                    )
                )

    cc_tool = _find_tool(("cc", "clang", "gcc"), search_paths)
    cxx_tool = _find_tool(("c++", "clang++", "g++"), search_paths)
    fortran_tool = _find_tool(("gfortran", "flang", "ifx", "ifort"), search_paths)
    graph_commands = ()
    graph_kind_prefix = "compile_command_"
    meson_graph_pending = False
    target_replay: dict[str, object] | None = None
    replay_object_by_command: dict[BuildCommand, str] = {}
    if from_compile_commands and plan.commands:
        graph_commands = plan.commands
    elif from_meson_introspection:
        graph_commands = load_meson_introspection_commands(root)
        graph_kind_prefix = "meson_compile_"
        if not graph_commands and configure_meson:
            setup_action = _run_action(
                kind="meson_setup",
                command=_meson_setup_command(root, meson_build_dir, search_paths),
                source=None,
                output=meson_build_dir,
                cwd=root,
                execute=execute,
                search_paths=search_paths,
                timeout=timeout,
            )
            actions.append(setup_action)
            if execute and setup_action["status"] == "passed":
                graph_commands = load_meson_introspection_commands(
                    meson_build_dir, root
                )
        if not graph_commands:
            if not configure_meson or execute:
                diagnostics.append(
                    {
                        "code": "PCC-PKG-MESON-INTROSPECTION-MISSING",
                        "message": "Meson intro-targets.json did not provide C/C++/Fortran compile sources",
                    }
                )
            else:
                meson_graph_pending = True

    if meson_target is not None:
        if not from_compile_commands:
            diagnostics.append(
                {
                    "code": "PCC-PKG-MESON-TARGET-REQUIRES-COMPILE-COMMANDS",
                    "message": (
                        "--meson-target requires --from-compile-commands so "
                        "the Ninja target graph can be joined to exact compile actions"
                    ),
                }
            )
            graph_commands = ()
        else:
            target_replay = _meson_target_replay_plan(root, meson_target)
            if not target_replay["ok"]:
                diagnostics.append(
                    {
                        "code": target_replay["diagnostic"],
                        "message": f"Meson target could not be replayed: {meson_target}",
                    }
                )
                graph_commands = ()
            else:
                target_build_dir = Path(str(target_replay["build_dir"]))
                commands_by_output = {
                    _relative_meson_output(command.output, target_build_dir): command
                    for command in graph_commands
                    if command.output
                }
                commands_by_source: dict[str, list[BuildCommand]] = {}
                for command in graph_commands:
                    source_path = Path(command.file).expanduser()
                    if not source_path.is_absolute():
                        source_path = Path(command.directory or root) / source_path
                    commands_by_source.setdefault(
                        str(source_path.resolve()), []
                    ).append(command)
                selected_commands: list[BuildCommand] = []
                unresolved_objects: list[str] = []
                object_sources = target_replay.get("object_sources", {})
                for output in target_replay["objects"]:
                    command = commands_by_output.get(output)
                    if command is None and isinstance(object_sources, dict):
                        candidates = commands_by_source.get(
                            str(object_sources.get(output) or ""), []
                        )
                        if len(candidates) > 1:
                            object_parent = str(
                                (target_build_dir / Path(output).parent).resolve()
                            )
                            candidates = [
                                candidate
                                for candidate in candidates
                                if object_parent in candidate.command
                            ]
                        if len(candidates) == 1:
                            command = candidates[0]
                    if command is None:
                        unresolved_objects.append(output)
                        continue
                    selected_commands.append(command)
                    replay_object_by_command[command] = output
                if unresolved_objects:
                    diagnostics.append(
                        {
                            "code": "PCC-PKG-MESON-TARGET-COMPILE-ACTION-MISSING",
                            "message": (
                                "compile_commands.json does not cover the complete "
                                "Meson target object closure"
                            ),
                            "missing_objects": unresolved_objects,
                        }
                    )
                    graph_commands = ()
                else:
                    graph_commands = tuple(selected_commands)

    selected_cxx_linker: str | None = None
    if graph_commands:
        if from_meson_introspection or target_replay is not None:
            build_root = _meson_build_dir_from_intro_path(
                root, _meson_intro_targets_path(root)
            )
            generated_targets = _load_meson_generated_targets(root)
            if execute:
                generated_targets = tuple(
                    dict.fromkeys(
                        generated_targets
                        + _load_ninja_custom_targets(build_root, search_paths, timeout)
                    )
                )
            if generated_targets:
                ninja = _find_tool(("ninja",), search_paths) or "ninja"
                actions.append(
                    _run_action(
                        kind="meson_generated_targets",
                        command=[ninja, "-C", str(build_root), *generated_targets],
                        source=None,
                        output=None,
                        cwd=root,
                        execute=execute,
                        search_paths=search_paths,
                        timeout=timeout,
                    )
                )
        pcc_native_capi_dir = None
        pcc_native_runtime_inc = None
        if abi_mode == "pcc-native":
            pcc_native_capi_dir = _materialize_pcc_capi_include(
                build_dir, execute=execute
            )
            pcc_native_runtime_inc = _pcc_runtime_include_dir()
            if pcc_native_capi_dir is None:
                diagnostics.append(
                    {
                        "code": "PCC-PKG-CAPI-INCLUDE-MISSING",
                        "message": (
                            "pcc-native build could not locate pcc's Python C-API "
                            "headers (utils/fake_libc_include/Python.h); compiling "
                            "against the build's CPython includes instead."
                        ),
                    }
                )
        graph_action_specs: list[tuple[str, list[str], Path, Path | None, Path]] = []
        for build_command in graph_commands:
            try:
                command = shlex.split(build_command.command)
            except ValueError:
                command = build_command.command.split()
            if command:
                command[0] = _resolve_command_tool(command[0], search_paths)
            if command and build_command.language == "cxx":
                compiler = _compiler_behind_launcher(command)
                if compiler is not None:
                    selected_cxx_linker = _resolve_command_tool(compiler, search_paths)
            if pcc_native_capi_dir is not None and build_command.language in {
                "c",
                "cxx",
            }:
                command = _redirect_pcc_native_includes(
                    command, pcc_native_capi_dir, pcc_native_runtime_inc
                )
            command = _normalize_stale_toolchain_flags(command)
            cwd = (
                Path(build_command.directory).expanduser().resolve()
                if build_command.directory
                else root
            )
            source = Path(build_command.file).expanduser()
            if not source.is_absolute():
                source = cwd / source
            output = (
                Path(build_command.output).expanduser()
                if build_command.output
                else None
            )
            if output is not None and not output.is_absolute():
                output = cwd / output
            if output is not None and target_replay is not None:
                target_build_dir = Path(str(target_replay["build_dir"]))
                output_key = replay_object_by_command[build_command]
                output = build_dir / "pcc-native-target" / "objects" / output_key
                command = _replace_command_output(
                    _strip_dependency_output_flags(command), output
                )
                if "-fPIC" not in command:
                    command.append("-fPIC")
            if output is not None:
                object_outputs.append(output)
            graph_action_specs.append(
                (
                    graph_kind_prefix + build_command.language,
                    command,
                    source,
                    output,
                    cwd,
                )
            )

        def run_graph_action(
            spec: tuple[str, list[str], Path, Path | None, Path],
        ) -> dict[str, object]:
            kind, command, source, output, cwd = spec
            return _run_action(
                kind=kind,
                command=command,
                source=source,
                output=output,
                cwd=cwd,
                execute=execute,
                search_paths=search_paths,
                timeout=timeout,
            )

        if execute and target_replay is not None and jobs > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
                actions.extend(executor.map(run_graph_action, graph_action_specs))
        else:
            actions.extend(run_graph_action(spec) for spec in graph_action_specs)
    elif from_compile_commands or from_meson_introspection:
        pass
    else:
        static_include_dirs = [str(Path(path).expanduser()) for path in include_dirs]
        if abi_mode == "pcc-native":
            pcc_native_capi_dir = _materialize_pcc_capi_include(
                build_dir, execute=execute
            )
            pcc_native_runtime_inc = _pcc_runtime_include_dir()
            if pcc_native_capi_dir is None or pcc_native_runtime_inc is None:
                diagnostics.append(
                    {
                        "code": "PCC-PKG-CAPI-INCLUDE-MISSING",
                        "message": (
                            "pcc-native source build could not locate the curated "
                            "PCC Python C-API and runtime headers"
                        ),
                    }
                )
            else:
                static_include_dirs.extend(
                    [str(pcc_native_capi_dir), str(pcc_native_runtime_inc)]
                )
        include_flags = ["-I" + path for path in static_include_dirs]
        pic_flags = ["-fPIC"] if link_output is not None else []
        for source in _iter_source_files(root, _C_SUFFIXES):
            output = build_dir / (source.stem + ".o")
            object_outputs.append(output)
            command = [
                cc_tool or "cc",
                "-c",
                *pic_flags,
                *include_flags,
                str(source),
                "-o",
                str(output),
            ]
            actions.append(
                _run_action(
                    kind="c_compile",
                    command=command,
                    source=source,
                    output=output,
                    cwd=root,
                    execute=execute and cc_tool is not None,
                    search_paths=search_paths,
                    timeout=timeout,
                )
                if cc_tool is not None
                else {
                    "kind": "c_compile",
                    "command": command,
                    "output": str(output),
                    "source": str(source),
                    "status": "blocked" if execute else "planned",
                    "returncode": None,
                    "stdout": "",
                    "stderr": "C compiler not found" if execute else "",
                }
            )

        for source in _iter_source_files(root, _FORTRAN_SUFFIXES):
            output = build_dir / (source.stem + ".o")
            object_outputs.append(output)
            command = [fortran_tool or "gfortran", "-c", str(source), "-o", str(output)]
            actions.append(
                _run_action(
                    kind="fortran_compile",
                    command=command,
                    source=source,
                    output=output,
                    cwd=root,
                    execute=execute and fortran_tool is not None,
                    search_paths=search_paths,
                    timeout=timeout,
                )
                if fortran_tool is not None
                else {
                    "kind": "fortran_compile",
                    "command": command,
                    "output": str(output),
                    "source": str(source),
                    "status": "blocked" if execute else "planned",
                    "returncode": None,
                    "stdout": "",
                    "stderr": "Fortran compiler not found" if execute else "",
                }
            )

    vendor_bindings = [
        _find_library_binding(library, library_dirs) for library in libraries
    ]
    for binding in vendor_bindings:
        if not binding["found"]:
            diagnostics.append(
                {
                    "code": "PCC-PKG-MISSING-LIBRARY",
                    "message": f"requested package native library is missing: {binding['request']}",
                    "library": str(binding["request"]),
                }
            )

    link_report: dict[str, object] | None = None
    if link_output is not None:
        output_path = Path(link_output).expanduser()
        if not output_path.is_absolute():
            output_path = (root / output_path).resolve()
        if meson_graph_pending:
            actions.append(
                {
                    "kind": "native_link",
                    "command": [
                        "<after-meson-setup>",
                        "-shared",
                        "-o",
                        str(output_path),
                    ],
                    "output": str(output_path),
                    "source": None,
                    "status": "planned",
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                }
            )
        else:
            link_inputs = [str(path) for path in object_outputs]
            uses_cxx_linker = selected_cxx_linker is not None or (
                target_replay is not None
                and str(target_replay.get("rule") or "").startswith("cpp_")
            )
            linker_tool = (
                selected_cxx_linker or cxx_tool if uses_cxx_linker else cc_tool
            )
            if target_replay is not None:
                target_link_args = [
                    str(token) for token in target_replay.get("link_args", [])
                ]
                command = [linker_tool or ("c++" if uses_cxx_linker else "cc")]
                command.extend(link_inputs)
                if not any(
                    token in {"-shared", "-bundle", "-dynamiclib"}
                    for token in target_link_args
                ):
                    command.append("-shared")
                command.extend([*target_link_args, "-o", str(output_path)])
            else:
                command = [linker_tool or "cc", "-shared"]
                if sys.platform == "darwin":
                    command.extend(["-undefined", "dynamic_lookup"])
                command.extend([*link_inputs, "-o", str(output_path)])
            for directory in library_dirs:
                command.append("-L" + str(Path(directory).expanduser()))
            for binding in vendor_bindings:
                if binding["found"]:
                    command.append("-l" + str(binding["link_name"]))
            if linker_tool is None:
                actions.append(
                    {
                        "kind": "native_link",
                        "command": command,
                        "output": str(output_path),
                        "source": None,
                        "status": "blocked" if execute else "planned",
                        "returncode": None,
                        "stdout": "",
                        "stderr": "compiler/linker not found" if execute else "",
                    }
                )
            elif execute and any(action["status"] != "passed" for action in actions):
                actions.append(
                    {
                        "kind": "native_link",
                        "command": command,
                        "output": str(output_path),
                        "source": None,
                        "status": "blocked",
                        "returncode": None,
                        "stdout": "",
                        "stderr": "compile actions did not all pass",
                    }
                )
            else:
                actions.append(
                    _run_action(
                        kind="native_link",
                        command=command,
                        source=None,
                        output=output_path,
                        cwd=root,
                        execute=execute,
                        search_paths=search_paths,
                        timeout=timeout,
                    )
                )
            artifacts = [str(output_path)] if output_path.exists() else []
            link_report = linkage_report(
                artifacts=artifacts,
                commands=[" ".join(command)],
                abi_mode=abi_mode,
            )
            if not link_report["ok"]:
                diagnostics.extend(link_report["diagnostics"])  # type: ignore[arg-type]

    failed = [
        action
        for action in actions
        if action["status"] in ("blocked", "failed", "timeout")
    ]
    if failed:
        diagnostics.append(
            {
                "code": "PCC-PKG-BUILD-ACTION-FAILED",
                "message": "one or more package build actions did not complete",
            }
        )
    return {
        "ok": not failed
        and not toolchain["diagnostics"]
        and not [
            diag
            for diag in diagnostics
            if isinstance(diag, dict)
            and str(diag.get("code", "")).startswith("PCC-PKG")
        ],
        "build_backend": "host",
        "build_mode_requested": "host",
        "build_ownership": "host",
        "host_assisted": True,
        "host_free_build_claim": False,
        "host_python": sys.executable if sys.implementation.name != "pcc" else None,
        "name": metadata.name,
        "path": str(root),
        "execute": execute,
        "include_dirs": [str(Path(path).expanduser()) for path in include_dirs],
        "from_compile_commands": from_compile_commands,
        "from_meson_introspection": from_meson_introspection,
        "meson_target": meson_target,
        "meson_target_replay": target_replay,
        "configure_meson": configure_meson,
        "jobs": max(1, jobs),
        "generated_c_provenance": generated_c_rows,
        "actions": actions,
        "diagnostics": diagnostics,
        "vendor_bindings": vendor_bindings,
        "linkage": link_report,
        "toolchain": toolchain,
        "build_plan": plan.as_dict(),
    }


def execute_eager_meson_extensions(
    name: str,
    path: str | Path,
    *,
    execute: bool = False,
    jobs: int = 1,
    timeout: int = 30,
    build_mode: str = "owned",
    owned_compiler: str | None = None,
) -> dict[str, object]:
    """Configure Meson and replay the package's eager extension closure."""

    if build_mode not in {"owned", "host"}:
        return {
            "ok": False,
            "build_backend": "unresolved",
            "build_mode_requested": build_mode,
            "build_ownership": "unresolved",
            "host_assisted": False,
            "host_python": None,
            "host_free_build_claim": False,
            "actions": [],
            "targets": [],
            "diagnostics": [
                {
                    "code": "PCC-PKG-BUILD-MODE-INVALID",
                    "message": "build mode must be owned or host",
                }
            ],
        }
    owned = build_mode == "owned"
    backend = "pcc-native-build-exec" if owned else "host"
    ownership = "owned" if owned else "host"
    host_python = None if owned else sys.executable

    root = Path(path).expanduser().resolve()
    metadata = inspect_artifact(name, root)
    build_dir = root / "build" / "pcc-package" / "meson-build"
    actions: list[dict[str, object]] = []
    target_reports: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    wrappers = None
    search_paths: list[str] = []
    try:
        if execute and not owned:
            from .install import _build_requirement_tool_wrappers

            wrappers = _build_requirement_tool_wrappers(metadata.pyproject_requires)
            if wrappers is not None:
                search_paths.append(wrappers.name)
        if not (build_dir / "build.ninja").is_file():
            if owned:
                diagnostics.append(
                    {
                        "code": "PCC-PKG-OWNED-MESON-GRAPH-REQUIRED",
                        "message": (
                            "owned target replay requires a graph generated "
                            "by the native Meson tool"
                        ),
                    }
                )
                return {
                    "ok": False,
                    "build_backend": backend,
                    "build_mode_requested": build_mode,
                    "build_ownership": ownership,
                    "host_assisted": False,
                    "host_python": None,
                    "host_free_build_claim": False,
                    "selection": "module_scope_eager_import_closure",
                    "actions": actions,
                    "targets": target_reports,
                    "diagnostics": diagnostics,
                }
            setup_command = _meson_setup_command(root, build_dir, search_paths)
            setup = _run_action(
                kind="meson_setup",
                command=setup_command,
                source=None,
                output=build_dir,
                cwd=root,
                execute=execute,
                search_paths=search_paths,
                timeout=timeout,
            )
            setup["stdout"] = str(setup.get("stdout") or "")[-4000:]
            setup["stderr"] = str(setup.get("stderr") or "")[-4000:]
            actions.append(setup)
            if execute and setup["status"] != "passed":
                diagnostics.append(
                    {
                        "code": "PCC-PKG-MESON-SETUP-FAILED",
                        "message": "Meson setup failed before pcc-native target replay",
                    }
                )
                return {
                    "ok": False,
                    "build_backend": backend,
                    "build_mode_requested": build_mode,
                    "build_ownership": ownership,
                    "host_assisted": not owned,
                    "host_python": host_python,
                    "host_free_build_claim": False,
                    "selection": "module_scope_eager_import_closure",
                    "actions": actions,
                    "targets": target_reports,
                    "diagnostics": diagnostics,
                }
        if owned:
            host_command = _ninja_graph_host_python_command(build_dir)
            if host_command is not None:
                diagnostics.append(
                    {
                        "code": "PCC-PKG-OWNED-BUILD-GRAPH-HOST-PYTHON",
                        "message": (
                            "native Meson graph would execute a host Python "
                            "build command"
                        ),
                        "command": host_command,
                    }
                )
                return {
                    "ok": False,
                    "build_backend": backend,
                    "build_mode_requested": build_mode,
                    "build_ownership": ownership,
                    "host_assisted": False,
                    "host_python": None,
                    "host_free_build_claim": False,
                    "selection": "module_scope_eager_import_closure",
                    "actions": actions,
                    "targets": target_reports,
                    "diagnostics": diagnostics,
                }
        selected = discover_eager_meson_extension_targets(name, root)
        for row in selected:
            report = execute_build_actions(
                name,
                root,
                search_paths=search_paths,
                execute=execute,
                from_compile_commands=True,
                meson_target=row["target"],
                link_output=row["output"],
                abi_mode="pcc-native",
                build_mode=build_mode,
                owned_compiler=owned_compiler,
                jobs=max(1, jobs),
                timeout=timeout,
            )
            failed = [
                action
                for action in report["actions"]
                if action["status"] not in {"passed", "planned"}
            ]
            target_row: dict[str, object] = dict(row)
            target_row.update(
                {
                    "ok": report["ok"],
                    "host_free_build_claim": report["host_free_build_claim"],
                    "action_count": len(report["actions"]),
                    "failed_actions": [
                        {
                            "kind": action["kind"],
                            "source": action.get("source"),
                            "stderr": str(action.get("stderr") or "")[-4000:],
                        }
                        for action in failed[:3]
                    ],
                    "diagnostics": report["diagnostics"],
                    "linkage": report["linkage"],
                }
            )
            target_reports.append(target_row)
            actions.append(
                {
                    "kind": "pcc_native_meson_target",
                    "module": row["module"],
                    "target": row["target"],
                    "output": row["output"],
                    "status": "passed" if report["ok"] else "failed",
                    "action_count": len(report["actions"]),
                }
            )
        ok = all(bool(report["ok"]) for report in target_reports)
        if owned and not target_reports:
            ok = False
            diagnostics.append(
                {
                    "code": "PCC-PKG-OWNED-MESON-TARGETS-MISSING",
                    "message": "native Meson graph contains no eager extension targets",
                }
            )
        return {
            "ok": ok,
            "skipped": not bool(target_reports),
            "reason": None if target_reports else "no_eager_extension_targets",
            "build_backend": backend,
            "build_mode_requested": build_mode,
            "build_ownership": ownership,
            "host_assisted": not owned,
            "host_python": host_python,
            "host_free_build_claim": owned and execute and ok and all(bool(row["host_free_build_claim"]) for row in target_reports),
            "selection": "module_scope_eager_import_closure",
            "actions": actions,
            "targets": target_reports,
            "diagnostics": diagnostics,
        }
    finally:
        if wrappers is not None:
            wrappers.cleanup()


_BUILD_EXEC_HELP = """usage: pcc.package build-exec [name] --path PATH [OPTIONS]

Compile package source actions with PCC's owned backend by default.
Explicit --build-mode=host selects external compatibility tools.

Options:
  -h, --help                   Show this help
  --path PATH                  Source tree (required)
  --search-path PATH           Repeat compatibility-tool search paths
  --include-dir PATH           Repeat C include directories
  --library-dir PATH           Repeat library directories
  --library NAME               Repeat requested libraries
  --execute                    Execute the plan
  --regenerate-cython          Regenerate Cython sources
  --run-f2py                    Build Fortran wrappers
  --link-output PATH           Shared-library output
  --abi MODE                   Extension ABI (default: pcc-native)
  --from-compile-commands       Read compile_commands.json
  --from-meson-introspection    Read Meson source actions
  --meson-target TARGET         Select one graph object closure
  --configure-meson            Configure a missing graph
  --enforce-generated-c        Reject missing/stale generated C
  --jobs N                     Maximum action concurrency (default: 1)
  --timeout SECONDS            Per-action deadline (default: 30)
  --eager-meson-extensions      Select eager extension targets
  --build-mode owned|host       Execution owner (default: owned)
  --owned-compiler PATH         Parent native PCC compiler for replay tools
  --report PATH                 Write the JSON report
  --json                        Print the JSON report
"""


def _parse_build_exec_args(argv, owned_compiler=None):
    """A shared bounded parser, independent of host/native argparse coverage."""
    options = {
        "name": "package", "path": None, "search_path": [], "include_dir": [],
        "library_dir": [], "library": [], "execute": False,
        "regenerate_cython": False, "run_f2py": False, "link_output": None,
        "abi_mode": "pcc-native", "from_compile_commands": False,
        "from_meson_introspection": False, "meson_target": None,
        "configure_meson": False, "enforce_generated_c": False,
        "jobs": 1, "timeout": 30, "eager_meson_extensions": False,
        "build_mode": "owned", "owned_compiler": owned_compiler,
        "report": None, "json": False, "help": False,
    }
    values = {
        "--path": "path", "--search-path": "search_path", "--include-dir": "include_dir",
        "--library-dir": "library_dir", "--library": "library", "--link-output": "link_output",
        "--abi": "abi_mode", "--meson-target": "meson_target", "--jobs": "jobs",
        "--timeout": "timeout", "--build-mode": "build_mode", "--report": "report",
        "--owned-compiler": "owned_compiler",
    }
    flags = {
        "--execute": "execute", "--regenerate-cython": "regenerate_cython",
        "--run-f2py": "run_f2py", "--from-compile-commands": "from_compile_commands",
        "--from-meson-introspection": "from_meson_introspection",
        "--configure-meson": "configure_meson", "--enforce-generated-c": "enforce_generated_c",
        "--eager-meson-extensions": "eager_meson_extensions", "--json": "json",
        "--help": "help", "-h": "help",
    }
    repeated = ("search_path", "include_dir", "library_dir", "library")
    tokens = list(sys.argv[1:] if argv is None else argv)
    positional = False
    separator = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--" and not separator:
            separator = True
            index += 1
            continue
        if not separator and token.startswith("-"):
            option, equals, value = token.partition("=")
            if option in flags:
                if equals:
                    raise ValueError(option + " does not take a value")
                options[flags[option]] = True
            elif option in values:
                key = values[option]
                if not equals:
                    index += 1
                    if index >= len(tokens) or tokens[index].startswith("--"):
                        raise ValueError(option + " requires a value")
                    value = tokens[index]
                if not value:
                    raise ValueError(option + " requires a value")
                if key in ("jobs", "timeout"):
                    try:
                        integer = int(value)
                    except ValueError:
                        raise ValueError(option + " must be a positive integer")
                    if integer < 1:
                        raise ValueError(option + " must be a positive integer")
                    options[key] = integer
                elif key in repeated:
                    options[key].append(value)
                else:
                    options[key] = value
            else:
                raise ValueError("unrecognized argument: " + token)
        else:
            if positional:
                raise ValueError("unexpected package name: " + token)
            options["name"] = token
            positional = True
        index += 1
    if options["help"]:
        return options
    if options["path"] is None:
        raise ValueError("--path is required")
    if options["build_mode"] not in ("owned", "host"):
        raise ValueError("--build-mode must be owned or host")
    if options["build_mode"] == "owned" and options["search_path"]:
        raise ValueError("--search-path requires --build-mode=host")
    if options["from_compile_commands"] and options["from_meson_introspection"]:
        raise ValueError("select only one source command graph")
    if options["eager_meson_extensions"]:
        for key in ("search_path", "include_dir", "library_dir", "library", "link_output", "meson_target", "from_compile_commands", "from_meson_introspection", "configure_meson", "regenerate_cython", "run_f2py", "enforce_generated_c"):
            if options[key]:
                raise ValueError("--eager-meson-extensions does not accept --" + key.replace("_", "-"))
    return options


def main(argv: list[str] | None = None, *, owned_compiler: str | None = None) -> int:
    try:
        ns = _parse_build_exec_args(argv, owned_compiler)
    except ValueError as exc:
        sys.stderr.write("pcc.package build-exec: error: " + str(exc) + "\n")
        return 2
    if ns["help"]:
        print(_BUILD_EXEC_HELP, end="")
        return 0
    if ns["eager_meson_extensions"]:
        report = execute_eager_meson_extensions(
            ns["name"],
            ns["path"],
            execute=ns["execute"],
            jobs=ns["jobs"],
            timeout=ns["timeout"],
            build_mode=ns["build_mode"],
            owned_compiler=ns["owned_compiler"],
        )
    else:
        report = execute_build_actions(
            ns["name"],
            ns["path"],
            search_paths=ns["search_path"],
            include_dirs=ns["include_dir"],
            library_dirs=ns["library_dir"],
            execute=ns["execute"],
            regenerate_cython=ns["regenerate_cython"],
            run_f2py=ns["run_f2py"],
            link_output=ns["link_output"],
            libraries=ns["library"],
            abi_mode=ns["abi_mode"],
            from_compile_commands=ns["from_compile_commands"],
            from_meson_introspection=ns["from_meson_introspection"],
            meson_target=ns["meson_target"],
            configure_meson=ns["configure_meson"],
            enforce_generated_c=ns["enforce_generated_c"],
            build_mode=ns["build_mode"],
            owned_compiler=ns["owned_compiler"],
            jobs=ns["jobs"],
            timeout=ns["timeout"],
        )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if ns["report"]:
        report_path = Path(ns["report"]).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
