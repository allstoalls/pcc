"""Static preflight for the pcc1-native Metal launcher path.

The Level-5 GPU claim requires a no-libpython pcc1 process to execute the same
launcher path that proves the host-harness Level-4 Metal result. This module
keeps that check tied to the real launcher closure without importing the
launcher modules or running host-side setup.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


STATUS_PCC1_METAL_PREFLIGHT_READY = "pcc1_metal_launcher_preflight_ready"
STATUS_PCC1_METAL_PREFLIGHT_BLOCKED = "pcc1_metal_launcher_preflight_blocked"

DEFAULT_PCC1_METAL_LAUNCHER_ENTRY_MODULES = ("pcc.kernel_ir.metal_source_runtime",)
PCC1_METAL_RUNTIME_ABI_ENTRY_MODULES = ("pcc.kernel_ir.metal_runtime_abi",)

_IMPORT_BLOCKERS = {
    "ctypes": (
        "ctypes_dynamic_ffi",
        "current launcher uses CPython ctypes for dylib loading, callbacks, "
        "pointer arrays, and scalar ABI packing",
    ),
    "subprocess": (
        "host_subprocess_toolchain",
        "current launcher build helpers shell out to xcrun/clang from host Python",
    ),
}

_CTYPES_ATTRIBUTE_BLOCKERS = {
    "CDLL": (
        "ctypes_cdll_load",
        "current launcher loads Objective-C/Metal bridge dylibs through ctypes.CDLL",
    ),
    "CFUNCTYPE": (
        "ctypes_callback",
        "current launcher registers a Python callback as a native fence completion callback",
    ),
    "pythonapi": (
        "ctypes_pythonapi",
        "current capsule interop uses ctypes.pythonapi and is not no-libpython",
    ),
}

_MODULE_PHASES = {
    "pcc.backend.metal": "build",
}


@dataclass(frozen=True)
class Pcc1MetalLauncherBlocker:
    code: str
    module: str
    line: int
    detail: str
    phase: str = "runtime"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "module": self.module,
            "line": self.line,
            "detail": self.detail,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class Pcc1MetalLauncherPreflight:
    status: str
    entry_modules: tuple[str, ...]
    visited_modules: tuple[str, ...]
    blockers: tuple[Pcc1MetalLauncherBlocker, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)

    @property
    def reason(self) -> str:
        if not self.blockers:
            return "pcc1 Metal launcher closure has no known static blockers"
        codes = ", ".join(sorted({blocker.code for blocker in self.blockers}))
        return f"pcc1 Metal launcher closure is blocked by: {codes}"

    @property
    def runtime_blockers(self) -> tuple[Pcc1MetalLauncherBlocker, ...]:
        return tuple(blocker for blocker in self.blockers if blocker.phase != "build")

    @property
    def build_blockers(self) -> tuple[Pcc1MetalLauncherBlocker, ...]:
        return tuple(blocker for blocker in self.blockers if blocker.phase == "build")

    @property
    def runtime_reason(self) -> str:
        if not self.runtime_blockers:
            return "pcc1 Metal prebuilt runtime path has no known static blockers"
        codes = ", ".join(sorted({blocker.code for blocker in self.runtime_blockers}))
        return f"pcc1 Metal prebuilt runtime path is blocked by: {codes}"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "entry_modules": list(self.entry_modules),
            "visited_modules": list(self.visited_modules),
            "blocked": self.blocked,
            "reason": self.reason,
            "runtime_reason": self.runtime_reason,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


class _PreflightVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.module_phase = _MODULE_PHASES.get(module, "runtime")
        self.import_aliases: dict[str, str] = {}
        self.follow_modules: set[str] = set()
        self.blockers: list[Pcc1MetalLauncherBlocker] = []
        self._seen_blockers: set[tuple[str, str, int]] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            imported = alias.name
            root = imported.split(".", 1)[0]
            self.import_aliases[alias.asname or root] = imported
            self._record_import_blocker(imported, node.lineno)
            self._maybe_follow(imported)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            self.generic_visit(node)
            return
        self._record_import_blocker(node.module, node.lineno)
        self._maybe_follow(node.module)
        for alias in node.names:
            local = alias.asname or alias.name
            imported = f"{node.module}.{alias.name}"
            self.import_aliases[local] = imported
            self._record_import_blocker(imported, node.lineno)
            self._maybe_follow(imported)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            imported = self.import_aliases.get(node.value.id)
            if imported == "ctypes" and node.attr in _CTYPES_ATTRIBUTE_BLOCKERS:
                code, detail = _CTYPES_ATTRIBUTE_BLOCKERS[node.attr]
                self._add_blocker(code, node.lineno, detail)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        imported = self.import_aliases.get(node.id)
        if imported is not None:
            parent, _, attr = imported.rpartition(".")
            if parent == "ctypes" and attr in _CTYPES_ATTRIBUTE_BLOCKERS:
                code, detail = _CTYPES_ATTRIBUTE_BLOCKERS[attr]
                self._add_blocker(code, node.lineno, detail)
        self.generic_visit(node)

    def _record_import_blocker(self, imported: str, line: int) -> None:
        root = imported.split(".", 1)[0]
        if root in _IMPORT_BLOCKERS:
            code, detail = _IMPORT_BLOCKERS[root]
            self._add_blocker(code, line, detail)

    def _maybe_follow(self, imported: str) -> None:
        if imported == "pcc" or imported.startswith("pcc."):
            self.follow_modules.add(imported)

    def _add_blocker(self, code: str, line: int, detail: str) -> None:
        key = (code, self.module, line)
        if key in self._seen_blockers:
            return
        self._seen_blockers.add(key)
        self.blockers.append(
            Pcc1MetalLauncherBlocker(
                code=code,
                module=self.module,
                line=line,
                detail=detail,
                phase=self.module_phase,
            )
        )


def analyze_pcc1_metal_launcher_preflight(
    repo: str | Path,
    entry_modules: Iterable[str] = DEFAULT_PCC1_METAL_LAUNCHER_ENTRY_MODULES,
) -> Pcc1MetalLauncherPreflight:
    """Return static pcc1 blockers for the real Metal launcher module closure."""
    root = Path(repo)
    entries = tuple(entry_modules)
    pending = list(entries)
    visited: set[str] = set()
    blockers: list[Pcc1MetalLauncherBlocker] = []

    while pending:
        module = pending.pop(0)
        if module in visited:
            continue
        path = _module_path(root, module)
        if path is None:
            continue
        visited.add(module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _PreflightVisitor(module)
        visitor.visit(tree)
        blockers.extend(visitor.blockers)
        for imported in sorted(visitor.follow_modules):
            normalized = _nearest_existing_module(root, imported)
            if normalized is not None and normalized not in visited:
                pending.append(normalized)

    status = (
        STATUS_PCC1_METAL_PREFLIGHT_BLOCKED
        if blockers
        else STATUS_PCC1_METAL_PREFLIGHT_READY
    )
    return Pcc1MetalLauncherPreflight(
        status=status,
        entry_modules=entries,
        visited_modules=tuple(sorted(visited)),
        blockers=tuple(blockers),
    )


def _module_path(root: Path, module: str) -> Path | None:
    path = root / (module.replace(".", "/") + ".py")
    if path.is_file():
        return path
    package_init = root / module.replace(".", "/") / "__init__.py"
    if package_init.is_file():
        return package_init
    return None


def _nearest_existing_module(root: Path, module: str) -> str | None:
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if _module_path(root, candidate) is not None:
            return candidate
    return None


__all__ = [
    "DEFAULT_PCC1_METAL_LAUNCHER_ENTRY_MODULES",
    "PCC1_METAL_RUNTIME_ABI_ENTRY_MODULES",
    "Pcc1MetalLauncherBlocker",
    "Pcc1MetalLauncherPreflight",
    "STATUS_PCC1_METAL_PREFLIGHT_BLOCKED",
    "STATUS_PCC1_METAL_PREFLIGHT_READY",
    "analyze_pcc1_metal_launcher_preflight",
]
