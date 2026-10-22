"""Test-facing admission for the shared native provisioning reservation."""

from __future__ import annotations

import os
from pathlib import Path

from pcc.driver.native_provisioning import (
    GUARD_NAME,
    require_native_provisioning_allowed,
)


def native_test_runtime_options(repo_root: Path | None = None) -> dict:
    """Admit an explicit runtime, or reject provisioning before test outputs.

    This does not skip tests or prohibit compiling their own source. Archives
    must satisfy source, compiler, target, inventory and configuration checks.
    """
    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE", "").strip()
    if explicit:
        from tests.runtime_fixture_provenance import _verified_test_runtime_archive

        archive, _manifest = _verified_test_runtime_archive(explicit)
        return {"runtime_archive": str(archive)}
    require_native_provisioning_allowed(repo_root)
    return {}
