"""Project a uv lock into a transactional pcc-native package environment."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request

from pcc.package.acquire import requires_python_allows
from pcc.package.install import _artifact_compatibility_reason_from_name
from pcc.package.runtime_profile import (
    CapabilityArtifactError,
    read_capability_artifacts,
)
from pcc.package.environment import resolve_package_environment

UV_LOCK_ADAPTER_SCHEMA = "pcc.uv-lock-adapter.v1"
UV_LOCK_SUPPORTED_VERSION = 1
UV_LOCK_SUPPORTED_REVISIONS = (1, 2, 3)
SYNC_STATE_SCHEMA = "pcc.uv-locked-sync.v1"
_LOCAL_SOURCE_IGNORED_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


class UvLockSyncError(Exception):
    def __init__(self, code: str, message: str, **details: object):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "ok": False,
            "diagnostic": self.code,
            "error": self.message,
        }
        out.update(self.details)
        return out


def _normalized_name(value: object) -> str:
    return str(value or "").lower().replace("_", "-").replace(".", "-")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(
            part in _LOCAL_SOURCE_IGNORED_NAMES or part.startswith(".mesonpy-")
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(relative.as_posix().encode())
            digest.update(b"\0")
            digest.update(os.readlink(path).encode())
            digest.update(b"\0")
            continue
        if not path.is_file():
            continue
        digest.update(b"file\0")
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _snapshot_local_source(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> list[str]:
        return [
            name
            for name in names
            if name in _LOCAL_SOURCE_IGNORED_NAMES or name.startswith(".mesonpy-")
        ]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def _marker_environment(target_python: str, target_triple: str) -> dict[str, str]:
    triple = str(target_triple or "").lower()
    machine = triple.split("-", 1)[0]
    if "darwin" in triple or "apple" in triple:
        sys_platform = "darwin"
        platform_system = "Darwin"
    elif "windows" in triple or "mingw" in triple or "msvc" in triple:
        sys_platform = "win32"
        platform_system = "Windows"
    elif "linux" in triple:
        sys_platform = "linux"
        platform_system = "Linux"
    else:
        sys_platform = sys.platform
        platform_system = platform.system()
    version = str(target_python)
    parts = version.split(".")
    short_version = ".".join(parts[:2])
    full_version = ".".join((parts + ["0", "0"])[:3])
    return {
        "implementation_name": "pcc",
        "implementation_version": full_version,
        "os_name": "nt" if sys_platform == "win32" else "posix",
        "platform_machine": machine,
        "platform_python_implementation": "pcc",
        "platform_release": "",
        "platform_system": platform_system,
        "platform_version": "",
        "python_full_version": full_version,
        "python_version": short_version,
        "sys_platform": sys_platform,
        "extra": "",
    }


def _marker_value(node: ast.AST, environment: dict[str, str]) -> object:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in environment:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-MARKER-UNSUPPORTED",
                f"unsupported marker variable: {node.id}",
            )
        return environment[node.id]
    if isinstance(node, ast.List | ast.Tuple):
        return [_marker_value(item, environment) for item in node.elts]
    raise UvLockSyncError(
        "PCC-PKG-UVLOCK-MARKER-UNSUPPORTED",
        "unsupported marker expression",
        expression=ast.dump(node, include_attributes=False),
    )


def _evaluate_marker_node(node: ast.AST, environment: dict[str, str]) -> bool:
    if isinstance(node, ast.BoolOp):
        values = [_evaluate_marker_node(item, environment) for item in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate_marker_node(node.operand, environment)
    if isinstance(node, ast.Compare):
        left = _marker_value(node.left, environment)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _marker_value(comparator, environment)
            ordered_left = left
            ordered_right = right
            if isinstance(left, str) and isinstance(right, str):
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", left) and re.fullmatch(
                    r"[0-9]+(?:\.[0-9]+)*", right
                ):
                    left_parts = tuple(int(part) for part in left.split("."))
                    right_parts = tuple(int(part) for part in right.split("."))
                    width = max(len(left_parts), len(right_parts))
                    ordered_left = left_parts + (0,) * (width - len(left_parts))
                    ordered_right = right_parts + (0,) * (width - len(right_parts))
            if isinstance(operator, ast.Eq):
                result = left == right
            elif isinstance(operator, ast.NotEq):
                result = left != right
            elif isinstance(operator, ast.Lt):
                result = ordered_left < ordered_right  # type: ignore[operator]
            elif isinstance(operator, ast.LtE):
                result = ordered_left <= ordered_right  # type: ignore[operator]
            elif isinstance(operator, ast.Gt):
                result = ordered_left > ordered_right  # type: ignore[operator]
            elif isinstance(operator, ast.GtE):
                result = ordered_left >= ordered_right  # type: ignore[operator]
            elif isinstance(operator, ast.In):
                result = left in right  # type: ignore[operator]
            elif isinstance(operator, ast.NotIn):
                result = left not in right  # type: ignore[operator]
            else:
                raise UvLockSyncError(
                    "PCC-PKG-UVLOCK-MARKER-UNSUPPORTED",
                    "unsupported marker comparison operator",
                )
            if not result:
                return False
            left = right
        return True
    raise UvLockSyncError(
        "PCC-PKG-UVLOCK-MARKER-UNSUPPORTED",
        "unsupported marker expression",
        expression=ast.dump(node, include_attributes=False),
    )


def marker_applies(
    marker: object,
    environment: dict[str, str],
    *,
    extras: tuple[str, ...] = (),
) -> bool:
    text = str(marker or "").strip()
    if not text:
        return True
    try:
        expression = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-MARKER-UNSUPPORTED",
            f"invalid marker expression: {text}",
        ) from exc
    if "extra" not in text:
        return _evaluate_marker_node(expression.body, environment)
    values = extras or ("",)
    for extra in values:
        selected = dict(environment)
        selected["extra"] = extra
        if _evaluate_marker_node(expression.body, selected):
            return True
    return False


def _source_path(source: object, lock_dir: Path) -> Path | None:
    if not isinstance(source, dict):
        return None
    for key in ("directory", "editable", "virtual", "path"):
        value = source.get(key)
        if value is not None:
            return (lock_dir / str(value)).resolve()
    return None


def _dependency_rows(package: dict[str, object], key: str) -> list[dict[str, object]]:
    value = package.get(key, [])
    if not isinstance(value, list):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
            f"package {package.get('name')} has non-list {key}",
        )
    rows: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("name"):
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
                f"package {package.get('name')} has invalid dependency row",
            )
        rows.append(item)
    return rows


def _selected_root_edges(
    root: dict[str, object], groups: tuple[str, ...], extras: tuple[str, ...]
) -> list[dict[str, object]]:
    rows = _dependency_rows(root, "dependencies")
    group_table = root.get("dev-dependencies", {})
    if group_table is None:
        group_table = {}
    if not isinstance(group_table, dict):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
            "root dev-dependencies must be a table",
        )
    optional_table = root.get("optional-dependencies", {})
    if optional_table is None:
        optional_table = {}
    if not isinstance(optional_table, dict):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
            "root optional-dependencies must be a table",
        )
    for group in groups:
        if group not in group_table:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-GROUP-NOT-FOUND",
                f"uv lock does not define dependency group: {group}",
            )
        value = group_table[group]
        if not isinstance(value, list):
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
                f"dependency group {group} is not a list",
            )
        rows.extend(item for item in value if isinstance(item, dict))
    for extra in extras:
        if extra not in optional_table:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-EXTRA-NOT-FOUND",
                f"uv lock does not define project extra: {extra}",
            )
        value = optional_table[extra]
        if not isinstance(value, list):
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
                f"project extra {extra} is not a list",
            )
        rows.extend(item for item in value if isinstance(item, dict))
    return rows


def _select_dependency_package(
    edge: dict[str, object], packages_by_name: dict[str, list[dict[str, object]]]
) -> dict[str, object]:
    name = _normalized_name(edge.get("name"))
    candidates = list(packages_by_name.get(name, []))
    if edge.get("version") is not None:
        candidates = [
            item for item in candidates if item.get("version") == edge.get("version")
        ]
    if edge.get("source") is not None:
        candidates = [
            item for item in candidates if item.get("source") == edge.get("source")
        ]
    if len(candidates) != 1:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-GRAPH-AMBIGUOUS",
            f"locked dependency {name} resolves to {len(candidates)} package rows",
            package=name,
        )
    return candidates[0]


def _root_packages(
    packages: list[dict[str, object]], lock_dir: Path, project_root: Path
) -> list[dict[str, object]]:
    roots = []
    for package in packages:
        path = _source_path(package.get("source"), lock_dir)
        if path == project_root:
            roots.append(package)
    if not roots:
        local = [
            item for item in packages if _source_path(item.get("source"), lock_dir)
        ]
        if len(local) == 1:
            roots = local
    if not roots:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-PROJECT-NOT-FOUND",
            "uv lock has no local root package for this project",
        )
    return roots


def _artifact_for_package(
    package: dict[str, object], lock_dir: Path
) -> dict[str, object]:
    local_path = _source_path(package.get("source"), lock_dir)
    if local_path is not None:
        if not local_path.is_dir():
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-MISSING-ARTIFACT",
                f"locked local source is missing: {local_path}",
                package=package.get("name"),
            )
        return {
            "kind": "local-directory",
            "path": str(local_path),
            "url": None,
            "sha256": _tree_digest(local_path),
        }

    wheels = package.get("wheels", [])
    if not isinstance(wheels, list):
        wheels = []
    compatible_wheels = []
    for wheel in wheels:
        if not isinstance(wheel, dict) or not wheel.get("url"):
            continue
        filename = Path(urllib.parse.urlparse(str(wheel["url"])).path).name
        compatible, reason = _artifact_compatibility_reason_from_name(
            filename, abi="pcc-native"
        )
        if compatible:
            rank = 2 if reason == "pcc_native_wheel" else 1
            compatible_wheels.append((rank, filename, wheel, reason))
    if compatible_wheels:
        _, _, selected, reason = max(compatible_wheels)
        return _locked_remote_artifact(selected, reason)

    sdist = package.get("sdist")
    if isinstance(sdist, dict) and sdist.get("url"):
        return _locked_remote_artifact(sdist, "source-artifact")
    if wheels:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-INCOMPATIBLE-WHEEL",
            "lock contains wheels, but none match pcc-native or pure-Python ABI",
            package=package.get("name"),
            version=package.get("version"),
        )
    raise UvLockSyncError(
        "PCC-PKG-UVLOCK-MISSING-ARTIFACT",
        "lock contains no source, pure-Python wheel, or pcc-native artifact",
        package=package.get("name"),
        version=package.get("version"),
    )


def _locked_remote_artifact(row: dict[str, object], reason: str) -> dict[str, object]:
    hash_text = str(row.get("hash") or "")
    if not hash_text.startswith("sha256:") or len(hash_text) != 71:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-HASH-REQUIRED",
            "locked artifact requires an exact sha256 hash",
            url=row.get("url"),
        )
    return {
        "kind": reason,
        "path": None,
        "url": str(row["url"]),
        "sha256": hash_text.split(":", 1)[1],
    }


def project_uv_lock(
    lock_path: str | Path,
    *,
    project_root: str | Path | None = None,
    target_python: str,
    target_triple: str,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
) -> dict[str, object]:
    path = Path(lock_path).expanduser().resolve()
    if not path.is_file():
        raise UvLockSyncError("PCC-PKG-UVLOCK-NOT-FOUND", f"uv lock not found: {path}")
    lock_bytes = path.read_bytes()
    try:
        lock = tomllib.loads(lock_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA", "uv lock is not valid TOML"
        ) from exc
    version = lock.get("version")
    revision = lock.get("revision", 1)
    if (
        version != UV_LOCK_SUPPORTED_VERSION
        or revision not in UV_LOCK_SUPPORTED_REVISIONS
    ):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA",
            f"unsupported uv lock version/revision: {version}/{revision}",
            version=version,
            revision=revision,
        )
    requires_python = str(lock.get("requires-python") or "")
    if not requires_python or not requires_python_allows(
        requires_python, target_python
    ):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-TARGET-PYTHON-MISMATCH",
            f"target Python {target_python} is not allowed by {requires_python or '<missing>'}",
            target_python=target_python,
            requires_python=requires_python,
        )
    raw_packages = lock.get("package")
    if not isinstance(raw_packages, list):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA", "uv lock package table is missing"
        )
    packages = [item for item in raw_packages if isinstance(item, dict)]
    if len(packages) != len(raw_packages):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA", "uv lock has invalid package rows"
        )
    lock_dir = path.parent
    root_dir = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else lock_dir
    )
    roots = _root_packages(packages, lock_dir, root_dir)
    by_name: dict[str, list[dict[str, object]]] = {}
    for package in packages:
        by_name.setdefault(_normalized_name(package.get("name")), []).append(package)
    marker_environment = _marker_environment(target_python, target_triple)
    ordered: list[dict[str, object]] = []
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(package: dict[str, object], edges: list[dict[str, object]] | None = None):
        key = (_normalized_name(package.get("name")), str(package.get("version") or ""))
        if key in visited:
            return
        if key in visiting:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-GRAPH-CYCLE",
                f"dependency cycle includes {key[0]}",
            )
        visiting.add(key)
        package_edges = (
            _dependency_rows(package, "dependencies") if edges is None else edges
        )
        for edge in package_edges:
            if not marker_applies(
                edge.get("marker"), marker_environment, extras=extras
            ):
                continue
            visit(_select_dependency_package(edge, by_name))
        visiting.remove(key)
        visited.add(key)
        if package not in roots:
            ordered.append(package)

    for root in roots:
        visit(root, _selected_root_edges(root, groups, extras))

    projected = []
    for package in ordered:
        artifact = _artifact_for_package(package, lock_dir)
        projected.append(
            {
                "name": _normalized_name(package.get("name")),
                "version": str(package.get("version") or "0"),
                "source": package.get("source"),
                "artifact": artifact,
                "dependencies": [
                    _normalized_name(item.get("name"))
                    for item in _dependency_rows(package, "dependencies")
                    if marker_applies(
                        item.get("marker"), marker_environment, extras=extras
                    )
                ],
            }
        )
    return {
        "schema": UV_LOCK_ADAPTER_SCHEMA,
        "lock_path": str(path),
        "lock_sha256": _sha256_bytes(lock_bytes),
        "uv_lock_version": version,
        "uv_lock_revision": revision,
        "requires_python": requires_python,
        "target_python": target_python,
        "target_triple": target_triple,
        "groups": list(groups),
        "extras": list(extras),
        "root_packages": [_normalized_name(item.get("name")) for item in roots],
        "packages": projected,
    }


def _materialize_remote_artifact(
    artifact: dict[str, object], cache_root: Path
) -> tuple[Path, bool]:
    digest = str(artifact["sha256"])
    url = str(artifact["url"])
    filename = Path(urllib.parse.urlparse(url).path).name or "artifact"
    destination_dir = cache_root / "locked" / "sha256" / digest
    destination = destination_dir / filename
    if destination.is_file():
        if _sha256_file(destination) != digest:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-CACHE-CORRUPT",
                f"cached artifact hash mismatch: {destination}",
            )
        return destination, False
    destination_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=destination_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            with temp_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        actual = _sha256_file(temp_path)
        if actual != digest:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-HASH-MISMATCH",
                f"locked artifact hash mismatch for {url}",
                expected_sha256=digest,
                actual_sha256=actual,
            )
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination, True


def _find_pcc1(explicit: str | None = None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_value = str(os.environ.get("PCC_SYNC_PCC1") or "").strip()
    if env_value:
        candidates.append(Path(env_value).expanduser())
    virtual_env = str(os.environ.get("VIRTUAL_ENV") or "").strip()
    if virtual_env:
        candidates.append(Path(virtual_env) / "bin" / "pcc1")
    found = shutil.which("pcc1")
    if found:
        candidates.append(Path(found))
    candidates.append(
        Path(__file__).resolve().parents[2] / "build" / "bootstrap" / "pcc1"
    )
    for candidate in candidates:
        path = candidate.resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise UvLockSyncError(
        "PCC-PKG-UVLOCK-PCC1-NOT-FOUND",
        "pcc sync requires the native pcc1 shipped with the installed wheel",
    )


def _build_key(package: dict[str, object], environment: dict[str, object]) -> str:
    artifact = package["artifact"]
    payload = {
        "artifact_sha256": artifact["sha256"],  # type: ignore[index]
        "package": package["name"],
        "version": package["version"],
        "python_semantic_target": environment["python_semantic_target"],
        "pcc_native_abi": environment["pcc_native_abi"],
        "package_abi_mode": environment["package_abi_mode"],
        "target_triple": environment["target_triple"],
        "build_options": {"backend": "pcc1", "libpython": "off"},
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return _sha256_bytes(data)


def _sync_key(projection: dict[str, object], environment: dict[str, object]) -> str:
    payload = {
        "adapter": UV_LOCK_ADAPTER_SCHEMA,
        "lock_sha256": projection["lock_sha256"],
        "groups": projection["groups"],
        "extras": projection["extras"],
        "python_semantic_target": environment["python_semantic_target"],
        "compatibility_tag": environment["compatibility_tag"],
        "package_abi_mode": environment["package_abi_mode"],
    }
    return _sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )


def _install_with_pcc1(
    pcc1: Path,
    artifact: Path,
    stage_site: Path,
    cache_root: Path,
    timeout: int,
) -> dict[str, object]:
    command = [
        str(pcc1),
        "-m",
        "pip",
        "install",
        str(artifact),
        "--target",
        str(stage_site),
        "--cache-dir",
        str(cache_root),
        "--acquire",
        "offline",
        "--abi",
        "pcc-native",
    ]
    try:
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-INSTALL-TIMEOUT",
            f"pcc1 install timed out after {timeout}s: {artifact.name}",
        ) from exc
    if process.returncode != 0:
        output = process.stdout + "\n" + process.stderr
        if "PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED" in output:
            raise UvLockSyncError(
                "PCC-PKG-UVLOCK-BUILD-ISOLATION-UNSUPPORTED",
                f"locked source requires unsupported build isolation: {artifact.name}",
                returncode=process.returncode,
            )
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-INSTALL-FAILED",
            f"pcc1 failed to install locked artifact: {artifact.name}",
            returncode=process.returncode,
            stdout=process.stdout[-4000:],
            stderr=process.stderr[-4000:],
        )
    try:
        report = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-INSTALL-REPORT-INVALID",
            f"pcc1 returned invalid install JSON for {artifact.name}",
            stdout=process.stdout[-2000:],
        ) from exc
    if not report.get("ok"):
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-INSTALL-FAILED",
            f"pcc1 rejected locked artifact: {artifact.name}",
            install_report=report,
        )
    return report


def _read_sync_state(environment_root: Path) -> dict[str, object] | None:
    path = environment_root / "installed.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _publish_environment(
    environment_root: Path,
    staging_root: Path,
    environment_manifest: dict[str, object],
    installed_manifest: dict[str, object],
) -> None:
    stage_site = staging_root / "site-packages"
    if not stage_site.is_dir():
        raise OSError(f"staged site-packages is missing: {stage_site}")
    for name, payload in (
        ("environment.json", environment_manifest),
        ("installed.json", installed_manifest),
    ):
        (staging_root / name).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    environment_root.parent.mkdir(parents=True, exist_ok=True)
    backup_root = staging_root.with_name(staging_root.name + ".previous")
    moved_old = False
    try:
        if environment_root.exists():
            os.replace(environment_root, backup_root)
            moved_old = True
        os.replace(staging_root, environment_root)
    except Exception:
        if environment_root.exists() and moved_old:
            shutil.rmtree(environment_root, ignore_errors=True)
        if moved_old and backup_root.exists():
            os.replace(backup_root, environment_root)
        raise
    if backup_root.exists():
        shutil.rmtree(backup_root)


def sync_uv_lock(
    lock_path: str | Path = "uv.lock",
    *,
    project_root: str | Path | None = None,
    groups: tuple[str, ...] = (),
    extras: tuple[str, ...] = (),
    pcc1: str | None = None,
    install_timeout: int = 600,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    environment = resolve_package_environment(environ)
    target_python = str(environment["python_semantic_target"])
    target_triple = str(environment["target_triple"])
    projection = project_uv_lock(
        lock_path,
        project_root=project_root,
        target_python=target_python,
        target_triple=target_triple,
        groups=groups,
        extras=extras,
    )
    environment_root = Path(str(environment["root"]))
    cache_root = Path(str(environment["cache_root"]))
    sync_key = _sync_key(projection, environment)
    previous = _read_sync_state(environment_root)
    if (
        previous is not None
        and previous.get("schema") == SYNC_STATE_SCHEMA
        and previous.get("sync_key") == sync_key
        and (environment_root / "site-packages").is_dir()
    ):
        return {
            "ok": True,
            "schema": SYNC_STATE_SCHEMA,
            "changed": False,
            "sync_key": sync_key,
            "environment_root": str(environment_root),
            "lock_sha256": projection["lock_sha256"],
            "downloads": 0,
            "native_builds": 0,
            "packages": previous.get("packages", []),
            "lock_provenance": previous.get("lock_provenance"),
        }

    native_pcc1 = _find_pcc1(pcc1)
    environment_root.parent.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".pcc-sync-", dir=environment_root.parent)
    )
    source_snapshots_root = Path(
        tempfile.mkdtemp(prefix=".pcc-sync-sources-", dir=environment_root.parent)
    )
    stage_site = staging_root / "site-packages"
    stage_site.mkdir(parents=True)
    package_reports = []
    downloads = 0
    native_builds = 0
    try:
        for package_index, package in enumerate(
            projection["packages"]  # type: ignore[union-attr]
        ):
            artifact = package["artifact"]
            downloaded = False
            if artifact["path"] is not None:
                source_path = Path(str(artifact["path"]))
                snapshot_parent = source_snapshots_root / f"{package_index:04d}"
                snapshot_parent.mkdir()
                artifact_path = snapshot_parent / source_path.name
                _snapshot_local_source(source_path, artifact_path)
            else:
                artifact_path, downloaded = _materialize_remote_artifact(
                    artifact, cache_root
                )
                downloads += int(downloaded)
            try:
                capability_rows = (
                    read_capability_artifacts(artifact_path)
                    if artifact_path.is_dir()
                    else []
                )
            except CapabilityArtifactError as exc:
                raise UvLockSyncError(
                    "PCC-PKG-UVLOCK-CAPABILITY-INVALID",
                    f"invalid capability manifest for {package['name']}: {exc}",
                    capability_code=exc.code,
                ) from exc
            install_report = _install_with_pcc1(
                native_pcc1,
                artifact_path,
                stage_site,
                cache_root,
                install_timeout,
            )
            installs = install_report.get("installs", [])
            built = False
            if isinstance(installs, list):
                for install in installs:
                    if not isinstance(install, dict):
                        continue
                    build_report = install.get("build_report")
                    if isinstance(build_report, dict) and not build_report.get(
                        "skipped", True
                    ):
                        built = True
            native_builds += int(built)
            package_reports.append(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "artifact_kind": artifact["kind"],
                    "artifact_sha256": artifact["sha256"],
                    "artifact_path": artifact["path"],
                    "artifact_url": artifact["url"],
                    "build_key": _build_key(package, environment),
                    "capability_artifacts": capability_rows,
                    "dependencies": package["dependencies"],
                    "downloaded": downloaded,
                    "install_ok": True,
                }
            )
        lock_provenance = {
            "adapter_schema": UV_LOCK_ADAPTER_SCHEMA,
            "lock_path": projection["lock_path"],
            "lock_sha256": projection["lock_sha256"],
            "uv_lock_version": projection["uv_lock_version"],
            "uv_lock_revision": projection["uv_lock_revision"],
            "target_python": target_python,
            "groups": list(groups),
            "extras": list(extras),
        }
        installed_manifest = {
            "schema": SYNC_STATE_SCHEMA,
            "sync_key": sync_key,
            "compatibility_tag": environment["compatibility_tag"],
            "lock_provenance": lock_provenance,
            "packages": package_reports,
        }
        environment_manifest = dict(environment)
        environment_manifest["lock_provenance"] = lock_provenance
        environment_manifest["sync_key"] = sync_key
        _publish_environment(
            environment_root,
            staging_root,
            environment_manifest,
            installed_manifest,
        )
    except UvLockSyncError:
        raise
    except Exception as exc:
        raise UvLockSyncError(
            "PCC-PKG-UVLOCK-PUBLISH-FAILED",
            f"failed to publish locked pcc environment: {exc}",
        ) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(source_snapshots_root, ignore_errors=True)
    return {
        "ok": True,
        "schema": SYNC_STATE_SCHEMA,
        "changed": True,
        "sync_key": sync_key,
        "environment_root": str(environment_root),
        "lock_sha256": projection["lock_sha256"],
        "downloads": downloads,
        "native_builds": native_builds,
        "packages": package_reports,
        "lock_provenance": lock_provenance,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc sync")
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--lock", default="uv.lock")
    parser.add_argument("--project", default=None)
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--pcc1", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--install-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if not args.locked:
        report = UvLockSyncError(
            "PCC-PKG-UVLOCK-LOCKED-REQUIRED",
            "pcc sync currently requires --locked",
        ).as_dict()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    try:
        report = sync_uv_lock(
            args.lock,
            project_root=args.project,
            groups=tuple(args.group),
            extras=tuple(args.extra),
            pcc1=args.pcc1,
            install_timeout=args.install_timeout,
        )
    except UvLockSyncError as exc:
        print(json.dumps(exc.as_dict(), indent=2, sort_keys=True))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
