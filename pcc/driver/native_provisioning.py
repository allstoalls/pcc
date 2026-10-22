"""One reservation boundary for native runtime and test-compiler provisioning.

PCC_TEST_NO_NATIVE_PROVISIONING and the coordinator marker block provisioning,
not compilation/execution with an admitted prebuilt runtime. PCC_NO_AUTO_PCC1
has a narrower test-stage1 contract and is intentionally not consulted here.
"""

from __future__ import annotations

import os
from pathlib import Path


GUARD_NAME = ".pcc-test-no-native-provisioning"


class NativeProvisioningError(RuntimeError):
    """A native build attempted to cross an active reservation."""


def require_native_provisioning_allowed(repo_root: Path | None = None) -> None:
    """Check before creating build directories, taking locks or invoking tools."""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    guard = root / "build" / GUARD_NAME
    disabled = os.environ.get("PCC_TEST_NO_NATIVE_PROVISIONING", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"} or guard.exists():
        raise NativeProvisioningError(
            "automatic native test provisioning is disabled; select an explicitly "
            "verified prebuilt runtime or wait for the active build reservation "
            f"to finish ({guard})"
        )
