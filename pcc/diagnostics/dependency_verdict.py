"""Structured prerequisite verdicts for claim-bearing tests and tools."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable


STATUS_AVAILABLE = "AVAILABLE"
STATUS_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DependencyVerdict:
    dependency: str
    status: str
    resolved_path: str | None
    reason: str
    feature_claimed: bool = False
    runtime_executed: bool = False

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "dependency": self.dependency,
            "status": self.status,
            "resolved_path": self.resolved_path,
            "reason": self.reason,
            "feature_claimed": self.feature_claimed,
            "runtime_executed": self.runtime_executed,
        }

    def skip_reason(self) -> str:
        if self.available:
            return f"AVAILABLE[{self.dependency}]: decorator inactive"
        return (
            f"UNAVAILABLE[{self.dependency}]: {self.reason}; "
            "feature_claimed=false; runtime_executed=false"
        )


def probe_executable_dependency(
    name: str,
    *,
    resolver: Callable[[str], str | None] = shutil.which,
) -> DependencyVerdict:
    if not name or not name.strip():
        raise ValueError("dependency executable name must be non-empty")
    dependency = f"executable:{name}"
    path = resolver(name)
    if path is None:
        return DependencyVerdict(
            dependency=dependency,
            status=STATUS_UNAVAILABLE,
            resolved_path=None,
            reason=f"{name!r} was not found on PATH",
        )
    return DependencyVerdict(
        dependency=dependency,
        status=STATUS_AVAILABLE,
        resolved_path=path,
        reason=f"resolved {name!r} to {path}",
    )


def probe_first_executable_dependency(
    names: tuple[str, ...] | list[str],
    *,
    resolver: Callable[[str], str | None] = shutil.which,
) -> DependencyVerdict:
    """Probe an ordered alternative set (e.g. cc/clang/gcc); first hit wins.

    UNAVAILABLE names the whole alternative set so the skip reason does not
    misreport a single candidate as the only prerequisite.
    """
    if not names:
        raise ValueError("dependency executable alternatives must be non-empty")
    for name in names:
        verdict = probe_executable_dependency(name, resolver=resolver)
        if verdict.available:
            return verdict
    joined = "|".join(names)
    return DependencyVerdict(
        dependency=f"executable:{joined}",
        status=STATUS_UNAVAILABLE,
        resolved_path=None,
        reason=f"none of {joined!r} was found on PATH",
    )


def probe_artifact_dependency(
    path: object,
    *,
    kind: str = "artifact",
) -> DependencyVerdict:
    """Probe a prebuilt on-disk artifact (archive, baseline binary, ...).

    A missing artifact is an UNAVAILABLE prerequisite — never evidence about
    the behavior the artifact would have exhibited.
    """
    text = str(path)
    if not text.strip():
        raise ValueError("dependency artifact path must be non-empty")
    dependency = f"{kind}:{text}"
    if not os.path.exists(text):
        return DependencyVerdict(
            dependency=dependency,
            status=STATUS_UNAVAILABLE,
            resolved_path=None,
            reason=f"artifact does not exist: {text}",
        )
    return DependencyVerdict(
        dependency=dependency,
        status=STATUS_AVAILABLE,
        resolved_path=text,
        reason=f"artifact present: {text}",
    )


def probe_platform_capability(
    name: str,
    *,
    supported: bool,
    detail: str,
) -> DependencyVerdict:
    """Classify a platform/OS capability separately from feature behavior.

    ``supported`` reflects the CURRENT interpreter's platform capability
    (e.g. POSIX process groups, the macOS-arm64 bootstrap-baseline capture
    platform); the verdict never claims the guarded feature itself.
    """
    if not name or not name.strip():
        raise ValueError("platform capability name must be non-empty")
    dependency = f"capability:{name}"
    if not supported:
        return DependencyVerdict(
            dependency=dependency,
            status=STATUS_UNAVAILABLE,
            resolved_path=None,
            reason=detail,
        )
    return DependencyVerdict(
        dependency=dependency,
        status=STATUS_AVAILABLE,
        resolved_path=None,
        reason=detail,
    )


__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_UNAVAILABLE",
    "DependencyVerdict",
    "probe_executable_dependency",
    "probe_first_executable_dependency",
    "probe_artifact_dependency",
    "probe_platform_capability",
]
