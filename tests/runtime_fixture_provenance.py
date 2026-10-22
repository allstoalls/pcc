"""Strict admission of explicitly supplied test runtimes, without provisioning."""

from __future__ import annotations

from pathlib import Path

from pcc.frontends.python import owned_runtime_build, pipeline_targets
from pcc.tools import runtime_archive_provenance


_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "pcc" / "runtime"


def _verified_test_runtime_archive(
    explicit: str | Path,
    *,
    threads: bool | None = None,
    runtime_root: Path | None = None,
) -> tuple[Path, dict]:
    """Require current source, codegen, target, inventory and build settings.

    ``threads=None`` selects the effective production configuration. A threaded
    fixture passes ``True`` explicitly without changing the caller's environment.
    Missing receipt configuration is never filled in from requested settings.
    """
    archive = Path(explicit).resolve(strict=True)
    if archive.name != "libpy_runtime_pcc_py.a":
        raise ValueError("explicit test runtime must be named libpy_runtime_pcc_py.a")
    runtime_root = (_RUNTIME_ROOT if runtime_root is None else runtime_root).resolve(strict=True)
    config = owned_runtime_build.runtime_build_config()
    if threads is not None:
        if type(threads) is not bool:
            raise ValueError("explicit test runtime threads setting must be boolean")
        config = dict(config, threads=threads)
    target = pipeline_targets.host_target_triple()
    manifest = runtime_archive_provenance.verify_runtime_archive_manifest(
        archive,
        runtime_root=runtime_root,
    )
    if not owned_runtime_build._manifest_matches_config(
        manifest, str(runtime_root), target, config,
    ):
        raise ValueError(
            "explicit test runtime target, member inventory or build configuration "
            f"does not match {target}: {config}"
        )
    if runtime_archive_provenance.manifest_is_stale_for_current_codegen(manifest):
        raise ValueError("explicit test runtime is stale for current codegen")
    return archive, manifest
