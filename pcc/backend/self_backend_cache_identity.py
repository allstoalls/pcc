"""Content identities for self-backend emission and owned Mach-O linking."""

from __future__ import annotations

import hashlib
from pathlib import Path

_IDENTITY_SCHEMA = b"pcc.self-backend-emitter-source.v1\0"
_MACHO_LINK_IDENTITY_SCHEMA = b"pcc.macho-linker-source.v2\0"

# Keep this finite rather than hashing all of pcc/backend: an unrelated
# register allocator or target backend change cannot affect Mach-O bytes and
# must not evict an otherwise reusable link state.  The driver is included
# because its input normalization and semantic options are part of the link
# action just as much as the backend modules are.
MACHO_LINK_SOURCE_PATHS = (
    "pcc/backend/arm64_asm_driver.py",
    "pcc/backend/arm64_encode.py",
    "pcc/backend/macho_archive.py",
    "pcc/backend/macho_assemble_worker.py",
    "pcc/backend/macho_codesign.py",
    "pcc/backend/macho_exec.py",
    "pcc/backend/macho_incremental.py",
    "pcc/backend/macho_link.py",
    "pcc/backend/macho_obj.py",
    "pcc/backend/macho_parallel.py",
    "pcc/backend/macho_semantic_layout.py",
    "pcc/backend/macho_spec.py",
    "pcc/backend/native_object.py",
    "pcc/backend/self_backend_cache_identity.py",
    "scripts/pcc_link_macho.py",
)


def self_backend_emitter_source_identity(source_root: str | Path | None = None) -> str:
    """Hash only sources that can change self-backend emission.

    Per-object cache keys separately bind the normalized IR shard, target,
    object-cache schema, assembler command, and assembler version.  Hashing the
    whole compiler source tree here would invalidate every object after an
    unrelated frontend, package, runtime, or documentation change.
    """

    if source_root is None:
        backend_dir = Path(__file__).resolve().parent
    else:
        backend_dir = Path(source_root).resolve() / "pcc" / "backend"
    sources = sorted(
        (
            path
            for path in backend_dir.glob("self_backend*.py")
            if path.name != Path(__file__).name
        ),
        key=lambda path: path.name,
    )
    if not sources:
        raise FileNotFoundError(f"no self-backend sources under {backend_dir}")

    digest = hashlib.sha256()
    digest.update(_IDENTITY_SCHEMA)
    for path in sources:
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def macho_linker_source_identity(
    source_root: str | Path | None = None,
) -> str:
    """Hash the complete source surface that can change a pcc Mach-O link.

    Incremental state is never reused across this boundary.  Inputs and link
    options are bound separately by the incremental cache keys; this digest
    protects the implementation side of the action key.
    """

    if source_root is None:
        root = Path(__file__).resolve().parents[2]
    else:
        root = Path(source_root).resolve()

    digest = hashlib.sha256()
    digest.update(_MACHO_LINK_IDENTITY_SCHEMA)
    for relative in MACHO_LINK_SOURCE_PATHS:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"Mach-O linker source is missing: {path}")
        name = relative.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(path.read_bytes())
    return digest.hexdigest()


__all__ = [
    "macho_linker_source_identity",
    "self_backend_emitter_source_identity",
]
