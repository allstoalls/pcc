"""Host-side identities for the bootstrap content-addressed compiler caches.

The frontend IR cache (``PCC_PY_FRONTEND_IR_CACHE_IDENTITY``) and the
self-backend object cache (``PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY``) only
activate when the invoking host supplies an identity namespace, because a
compiled stage binary cannot cheaply hash its own implementation sources.
Both ``scripts/bootstrap.sh`` and the pytest bootstrap helper derive their
namespaces here so equivalent invocations share one cache.

This module is host-side tooling; it is not part of the pcc1 bootstrap
closure.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pcc.backend.self_backend_cache_identity import (
    MACHO_LINK_SOURCE_PATHS,
    self_backend_emitter_source_identity,
)

_SOURCE_SUFFIXES = (".py", ".c", ".h", ".sh")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# Subtrees that cannot change the frontend IR a module compiles to.  The
# per-module cache key already binds the module's own sources, its transitive
# imports, the compiler binary's sha256, the target and the options; this
# identity is only the surrounding namespace.  Hashing all 999 bootstrap
# sources into it meant an edit to a GUI window, a runtime C file, or a
# reporting tool invalidated every cached frontend IR module and forced a fully
# cold stage — which is what made ordinary development pay cold-build cost.
#
# `pcc/backend/self_backend_cache_identity.py` already draws this line for the
# object cache, and its docstring states the same reason.  Keep the two in
# agreement: something excluded here must still be bound by whichever key
# actually depends on it (runtime sources by the runtime archive and the link,
# backend sources by the object-cache identity).
_FRONTEND_IRRELEVANT_PREFIXES = (
    "pcc/gui",           # desktop app; never imported by the compiler pipeline
    "pcc/py_runtime",    # runtime C + pcc-Python ports: bound by the archive
                         # provenance and the link, not by frontend IR shape
    "pcc/dist",          # distributed oracles
    "pcc/gpu_gc",        # GPU GC oracles
    "pcc/kernel_ir",     # GPU kernel IR thread
    "pcc/tools",
    # The C toolchain.  `pcc/codegen` is the C code generator and
    # `pcc/evaluater` the C driver/evaluator; `pcc/py_frontend` imports
    # neither (verified: zero references), so neither can change the IR a
    # *Python* module compiles to.  If one of them changes, its own module's
    # per-module cache key changes and that module recompiles -- which is the
    # correct scope.  Before this line, editing the C driver invalidated every
    # cached frontend IR module and forced a fully cold stage.
    #
    # `pcc/parse` is deliberately NOT here: `parse/py_lift.py` and
    # `parse/py_parse.py` are the Python front door, not the C parser.
    "pcc/codegen",
    "pcc/evaluater",
)

# The Mach-O object/link surface consumes IR; it cannot change the IR a module
# compiles to.  `macho_linker_source_identity` already binds exactly these
# files for the link action, which is the key that does depend on them, so
# counting them here only forced a cold frontend after every assembler,
# object-writer or linker edit -- 170 runtime objects and every cached
# frontend IR module, measured at ~9.5 minutes per edit.  Reuse that tuple
# rather than restating it, so the two lists cannot drift apart.
_FRONTEND_IRRELEVANT_FILES = frozenset(MACHO_LINK_SOURCE_PATHS)


def _is_frontend_relevant(relative: str) -> bool:
    if relative in _FRONTEND_IRRELEVANT_FILES:
        return False
    for prefix in _FRONTEND_IRRELEVANT_PREFIXES:
        if relative == prefix or relative.startswith(prefix + "/"):
            return False
    return True


def bootstrap_source_files(root: str | Path | None = None) -> tuple[Path, ...]:
    """Sources that can change the frontend IR a bootstrap stage produces."""

    base = repo_root() if root is None else Path(root).resolve()
    roots = (base / "pcc", base / "scripts" / "bootstrap.sh")
    files: list[Path] = []
    for entry in roots:
        if entry.is_file():
            files.append(entry)
            continue
        files.extend(
            path
            for path in entry.rglob("*")
            if path.is_file()
            and path.suffix in _SOURCE_SUFFIXES
            and _is_frontend_relevant(
                str(path.relative_to(base)).replace(os.sep, "/")
            )
        )
    return tuple(sorted(files, key=lambda path: str(path.relative_to(base))))


def bootstrap_source_sha256(root: str | Path | None = None) -> str:
    """Frontend IR cache namespace: hash of the bootstrap-relevant sources."""

    base = repo_root() if root is None else Path(root).resolve()
    digest = hashlib.sha256()
    for path in bootstrap_source_files(base):
        relative = str(path.relative_to(base)).encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def bootstrap_object_cache_identity(root: str | Path | None = None) -> str:
    """Self-backend object cache namespace: emitter source identity."""

    return self_backend_emitter_source_identity(
        repo_root() if root is None else root
    )


def main() -> int:
    """Print the two identities, one per line, for shell consumption.

    Line 1: ``PCC_PY_FRONTEND_IR_CACHE_IDENTITY`` value.
    Line 2: ``PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY`` value.
    """

    print(bootstrap_source_sha256())
    print(bootstrap_object_cache_identity())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
