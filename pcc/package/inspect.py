"""Static package inspection for pcc package planning.

This intentionally does not import the target package. Large packages can
execute arbitrary build/runtime code during import; the package planner needs
a repeatable source-tree audit and dry-run install shape first.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pcc.package.compat import get_package_target, level_name
from pcc.package.metadata import inspect_artifact

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PackageInspection:
    name: str
    path: str | None
    files: int
    python_files: int
    c_files: int
    cxx_files: int
    pyx_files: int
    shared_objects: int
    header_files: int
    package_level: str | None
    package_summary: str | None
    smoke_tests: tuple[str, ...]
    artifact_metadata: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "files": self.files,
            "python_files": self.python_files,
            "c_files": self.c_files,
            "cxx_files": self.cxx_files,
            "pyx_files": self.pyx_files,
            "shared_objects": self.shared_objects,
            "header_files": self.header_files,
            "package_level": self.package_level,
            "package_summary": self.package_summary,
            "smoke_tests": list(self.smoke_tests),
            "artifact_metadata": self.artifact_metadata,
        }


def _iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        # Build/vendor caches are large and not useful for the L0 source audit.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", ".mypy_cache", ".pytest_cache", "__pycache__"}
        ]
        base = Path(dirpath)
        for filename in filenames:
            yield base / filename


def _resolve_path(name: str, path: str | None) -> Path | None:
    if path:
        return Path(path).expanduser().resolve()
    direct = Path(name).expanduser()
    if direct.exists():
        return direct.resolve()
    projects = _REPO_ROOT / "projects"
    if projects.exists():
        matches = sorted(projects.glob(name + "-*"))
        if matches:
            return matches[-1].resolve()
    return None


def inspect_package(name: str, path: str | None = None) -> PackageInspection:
    name = (name or "").strip() or "package"
    root = _resolve_path(name, path)
    metadata_name = name
    if path is None and root is not None and Path(name).expanduser().exists():
        metadata_name = ""
    artifact_metadata = inspect_artifact(metadata_name, root).as_dict()
    report_name = str(artifact_metadata.get("name") or name)
    files = python_files = c_files = cxx_files = pyx_files = 0
    shared_objects = header_files = 0
    if root is not None and root.exists():
        for file_path in _iter_files(root):
            files += 1
            suffix = file_path.suffix.lower()
            if suffix == ".py":
                python_files += 1
            elif suffix == ".c":
                c_files += 1
            elif suffix in {".cc", ".cpp", ".cxx"}:
                cxx_files += 1
            elif suffix == ".pyx":
                pyx_files += 1
            elif suffix in {".so", ".pyd", ".dylib"}:
                shared_objects += 1
            elif suffix in {".h", ".hpp", ".hh"}:
                header_files += 1

    target = get_package_target(report_name)
    package_level = level_name(target.level) if target is not None else None
    return PackageInspection(
        name=report_name,
        path=str(root) if root is not None else None,
        files=files,
        python_files=python_files,
        c_files=c_files,
        cxx_files=cxx_files,
        pyx_files=pyx_files,
        shared_objects=shared_objects,
        header_files=header_files,
        package_level=package_level,
        package_summary=target.summary if target is not None else None,
        smoke_tests=target.smoke_tests if target is not None else (),
        artifact_metadata=artifact_metadata,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package.inspect")
    parser.add_argument("name", nargs="?", default="package")
    parser.add_argument("--path", default=None)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(argv)
    report = inspect_package(ns.name, ns.path).as_dict()
    if ns.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key in sorted(report):
            print(f"{key}: {report[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
