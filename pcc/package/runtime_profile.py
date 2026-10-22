"""Runtime-profile invariance and capability-tagged artifact contract.

Package environment identity is keyed only by the semantic Python target,
pcc-native ABI, target triple, and package ABI mode
(``pcc/package/environment.py``). Runtime policy — GC backend, execution
backend owner, threaded runtime, virtual-thread scheduling, accelerator
availability — must never key the environment root, the sync key, or the
per-package build keys: switching policy reuses the same installed CPU
package graph with zero acquisition and zero rebuild work.

Optional device payloads (Metal today) are capability-tagged artifacts
declared *inside* a package's distribution manifest, not extra environment
dimensions. Selecting a capability that is unavailable on the host or not
shipped by the package fails closed with a stable diagnostic; there is no
silent fallback to another capability, package site, or implementation.

This module is host-side package tooling; it is intentionally not part of
the pcc1 bootstrap closure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

RUNTIME_PROFILE_SCHEMA = "pcc.runtime-profile.v1"

# Runtime-policy environment variables that must never affect package
# environment identity, sync keys, build keys, or installed-artifact
# digests. tests/python/test_package_runtime_profile_environment.py defends
# this list against pcc/package/environment.py and the uv-lock sync keys.
RUNTIME_PROFILE_ENV_VARS = (
    "PCC_GC_BACKEND",  # GC backend 0..4
    "PCC_REFCOUNT_KIND",  # refcount strategy variant
    "PCC_BACKEND",  # owned self backend execution owner
    "PCC_WITH_THREADS",  # threaded runtime on/off
    "PCC_VTHREAD_PARKED",  # virtual-thread scheduler parking policy
    "PCC_GPU_BACKEND",  # accelerator execution owner
    "PCC_METAL",  # Metal toolchain/runtime availability toggle
    "PCC_DS",  # distributed session policy
)

CAPABILITY_TAGS = ("cpu", "metal")

CAPABILITY_MANIFEST_NAME = "pcc-capabilities.json"


class CapabilityArtifactError(ValueError):
    """Stable, fail-closed capability diagnostic with a machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def runtime_profile(environ: dict[str, str]) -> dict[str, object]:
    """Return the declared runtime-policy axes read from ``environ``."""

    values: dict[str, str] = {}
    for name in RUNTIME_PROFILE_ENV_VARS:
        values[name] = str(environ.get(name) or "")
    return {"schema": RUNTIME_PROFILE_SCHEMA, "values": values}


def normalize_capability_artifacts(rows: object) -> list[dict[str, str]]:
    """Validate capability artifact rows into ``capability/path/sha256``."""

    if rows is None:
        return []
    if not isinstance(rows, list):
        raise CapabilityArtifactError(
            "PCC-PKG-CAPABILITY-ROW-INVALID",
            f"capability artifacts must be a list, got {type(rows).__name__}",
        )
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-ROW-INVALID",
                f"capability artifact row must be an object, got {row!r}",
            )
        capability = str(row.get("capability") or "")
        if capability not in CAPABILITY_TAGS:
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-UNKNOWN",
                f"unknown capability tag {capability!r}"
                f" (known: {', '.join(CAPABILITY_TAGS)})",
            )
        path = str(row.get("path") or "")
        sha256 = str(row.get("sha256") or "")
        if not path:
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-ROW-INVALID",
                f"capability artifact row for {capability!r} is missing a path",
            )
        if len(sha256) != 64:
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-ROW-INVALID",
                f"capability artifact {path!r} needs a sha256 content digest",
            )
        out.append({"capability": capability, "path": path, "sha256": sha256})
    return out


def read_capability_artifacts(artifact_root: str | Path) -> list[dict[str, str]]:
    """Read a package's optional capability payload declarations.

    ``pcc-capabilities.json`` at the artifact root declares
    ``{"artifacts": [{"capability": "metal", "path": "pkg/kernel.metallib"}]}``
    with artifact-root-relative payload paths. Payloads are hashed here so
    the distribution manifest records content digests. A package without
    the manifest ships no optional capability payloads.
    """

    root = Path(artifact_root)
    manifest_path = root / CAPABILITY_MANIFEST_NAME
    if not manifest_path.is_file():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CapabilityArtifactError(
            "PCC-PKG-CAPABILITY-MANIFEST-INVALID",
            f"unreadable capability manifest {manifest_path}: {exc}",
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
        raise CapabilityArtifactError(
            "PCC-PKG-CAPABILITY-MANIFEST-INVALID",
            f"capability manifest {manifest_path} must declare an"
            ' "artifacts" list',
        )
    rows: list[dict[str, str]] = []
    for declared in data["artifacts"]:
        if not isinstance(declared, dict):
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-ROW-INVALID",
                f"capability artifact row must be an object, got {declared!r}",
            )
        relative = str(declared.get("path") or "")
        payload = (root / relative).resolve() if relative else None
        if payload is None or not payload.is_relative_to(root.resolve()):
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-MANIFEST-INVALID",
                f"capability payload path {relative!r} escapes the artifact"
                f" root {root}",
            )
        if not payload.is_file():
            raise CapabilityArtifactError(
                "PCC-PKG-CAPABILITY-ARTIFACT-MISSING",
                f"declared capability payload does not exist: {payload}",
            )
        rows.append(
            {
                "capability": str(declared.get("capability") or ""),
                "path": relative,
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
            }
        )
    return normalize_capability_artifacts(rows)


def select_capability_artifact(
    capability: str,
    artifact_rows: object,
    available_capabilities: tuple[str, ...] | list[str],
) -> dict[str, str]:
    """Select the payload for ``capability`` fail-closed.

    Never falls back: an unknown tag, a capability the host does not
    provide, or a capability the package does not ship each raise with a
    stable diagnostic instead of silently choosing another capability,
    package site, or implementation.
    """

    requested = str(capability or "")
    if requested not in CAPABILITY_TAGS:
        raise CapabilityArtifactError(
            "PCC-PKG-CAPABILITY-UNKNOWN",
            f"unknown capability tag {requested!r}"
            f" (known: {', '.join(CAPABILITY_TAGS)})",
        )
    available = [str(item) for item in available_capabilities]
    if requested not in available:
        raise CapabilityArtifactError(
            "PCC-PKG-CAPABILITY-UNAVAILABLE",
            f"runtime capability {requested!r} is not available on this host"
            f" (available: {', '.join(available) or 'none'});"
            " refusing to fall back to another capability, package site,"
            " or implementation",
        )
    for row in normalize_capability_artifacts(artifact_rows):
        if row["capability"] == requested:
            return row
    raise CapabilityArtifactError(
        "PCC-PKG-CAPABILITY-ARTIFACT-MISSING",
        f"package ships no artifact for capability {requested!r};"
        " refusing to fall back to another capability, package site,"
        " or implementation",
    )
