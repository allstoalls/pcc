"""Generic package/build artifact metadata for pcc package planning.

The metadata here is intentionally package-agnostic. NumPy is one consumer of
these diagnostics, but the scanner reports reusable build surfaces: wheels,
sdists, pyproject build backends, Meson, compile_commands, generated C,
Fortran, BLAS/LAPACK indicators, and pcc-native tag routing.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import sysconfig
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pcc.package.schema import directory_project_name, distribution_filename_fields, pcc_native_wheel_tag as pcc_native_wheel_tag_for_platform, validate_project_name, wheel_tag_fields

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - old host fallback
    tomllib = None  # type: ignore[assignment]


_SKIP_DIRS = {".git", ".mypy_cache", ".pytest_cache", "__pycache__", "build"}
_NON_BUILD_SOURCE_DIRS = {
    ".github",
    ".spin",
    "benchmark",
    "benchmarks",
    "doc",
    "docs",
    "example",
    "examples",
    "test",
    "tests",
    "tools",
    "vendored-meson",
}
_FORTRAN_SUFFIXES = {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"}
_CXX_SUFFIXES = {".cc", ".cpp", ".cxx", ".c++"}
_HEADER_SUFFIXES = {".h", ".hpp", ".hh", ".hxx"}
_SHARED_SUFFIXES = {".so", ".pyd", ".dylib"}


@dataclass(frozen=True)
class CompileCommandSummary:
    path: str | None
    entries: int
    c_entries: int
    cxx_entries: int
    fortran_entries: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "entries": self.entries,
            "c_entries": self.c_entries,
            "cxx_entries": self.cxx_entries,
            "fortran_entries": self.fortran_entries,
        }


@dataclass(frozen=True)
class PackageArtifactMetadata:
    name: str
    path: str | None
    source_kind: str
    python_tag: str | None
    abi_tag: str | None
    platform_tag: str | None
    current_platform_tag: str
    pcc_native_wheel_tag: str
    pyproject_build_backend: str | None
    pyproject_requires: tuple[str, ...]
    meson_build: bool
    compile_commands: CompileCommandSummary
    generated_c_artifacts: tuple[str, ...]
    cython_sources: tuple[str, ...]
    fortran_sources: tuple[str, ...]
    native_extensions: tuple[str, ...]
    blas_indicators: tuple[str, ...]
    lapack_indicators: tuple[str, ...]
    native_library_fallbacks: tuple[str, ...]
    generated_c_policy: str
    requires_cython_regeneration: bool
    diagnostics: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "source_kind": self.source_kind,
            "python_tag": self.python_tag,
            "abi_tag": self.abi_tag,
            "platform_tag": self.platform_tag,
            "current_platform_tag": self.current_platform_tag,
            "pcc_native_wheel_tag": self.pcc_native_wheel_tag,
            "pyproject_build_backend": self.pyproject_build_backend,
            "pyproject_requires": list(self.pyproject_requires),
            "meson_build": self.meson_build,
            "compile_commands": self.compile_commands.as_dict(),
            "generated_c_artifacts": list(self.generated_c_artifacts),
            "cython_sources": list(self.cython_sources),
            "fortran_sources": list(self.fortran_sources),
            "native_extensions": list(self.native_extensions),
            "blas_indicators": list(self.blas_indicators),
            "lapack_indicators": list(self.lapack_indicators),
            "native_library_fallbacks": list(self.native_library_fallbacks),
            "generated_c_policy": self.generated_c_policy,
            "requires_cython_regeneration": self.requires_cython_regeneration,
            "diagnostics": list(self.diagnostics),
        }


def current_platform_tag() -> str:
    raw = sysconfig.get_platform() or platform.system().lower()
    return re.sub(r"[^A-Za-z0-9_]+", "_", raw.replace("-", "_").replace(".", "_"))


def pcc_native_wheel_tag() -> str:
    return pcc_native_wheel_tag_for_platform(current_platform_tag())


def _iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        base = Path(dirpath)
        for filename in filenames:
            yield base / filename


def is_build_relevant_name(name: str) -> bool:
    """Return whether a package path should influence native build planning.

    Source trees often contain docs, upstream test corpora, vendored build-system
    tests, and examples with Cython/Fortran/BLAS-looking files. Those files are
    useful for source inspection, but they must not become required build inputs
    for the package itself.
    """
    parts = Path(name).parts
    for part in parts[:-1]:
        normalized = part.lower().strip("_")
        if normalized in _NON_BUILD_SOURCE_DIRS or normalized.startswith("."):
            return False
    return True


def is_build_relevant_path(root: Path, path: Path) -> bool:
    try:
        return is_build_relevant_name(str(path.relative_to(root)))
    except ValueError:
        return is_build_relevant_name(str(path))


def _read_pyproject(root: Path) -> tuple[str | None, tuple[str, ...]]:
    path = root / "pyproject.toml"
    if not path.exists() or tomllib is None:
        return None, ()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, ()
    build_system = data.get("build-system", {})
    backend = build_system.get("build-backend")
    requires = tuple(str(item) for item in build_system.get("requires", ()))
    return (str(backend) if backend is not None else None), requires


def _read_meson_boolean_options(root: Path) -> dict[str, bool]:
    options: dict[str, bool] = {}
    for path in (root / "meson.options", root / "meson_options.txt"):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in re.finditer(
            r"option\(\s*['\"]([^'\"]+)['\"](?P<body>.*?)\)",
            text,
            re.IGNORECASE | re.DOTALL,
        ):
            name = match.group(1)
            body = match.group("body")
            if re.search(r"type\s*:\s*['\"]boolean['\"]", body, re.IGNORECASE) is None:
                continue
            value_match = re.search(r"value\s*:\s*(true|false)", body, re.IGNORECASE)
            if value_match is None:
                continue
            options[name] = value_match.group(1).lower() == "true"
    return options


def _native_library_fallbacks_from_options(options: dict[str, bool]) -> tuple[str, ...]:
    fallbacks: list[str] = []
    if options.get("allow-noblas") is True:
        fallbacks.extend(("blas", "lapack"))
    return tuple(dict.fromkeys(fallbacks))


def _summarize_compile_commands(root: Path) -> CompileCommandSummary:
    path = root / "compile_commands.json"
    if not path.exists():
        return CompileCommandSummary(None, 0, 0, 0, 0)
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return CompileCommandSummary(str(path), 0, 0, 0, 0)
    if not isinstance(entries, list):
        return CompileCommandSummary(str(path), 0, 0, 0, 0)
    c_entries = cxx_entries = fortran_entries = 0
    for entry in entries:
        file_name = str(entry.get("file", "") if isinstance(entry, dict) else "")
        suffix = Path(file_name).suffix.lower()
        if suffix == ".c":
            c_entries += 1
        elif suffix in _CXX_SUFFIXES:
            cxx_entries += 1
        elif suffix in _FORTRAN_SUFFIXES:
            fortran_entries += 1
    return CompileCommandSummary(
        str(path),
        len(entries),
        c_entries,
        cxx_entries,
        fortran_entries,
    )


def _wheel_tags(path: Path) -> tuple[str | None, str | None, str | None]:
    fields = wheel_tag_fields(str(path))
    return fields[1] or None, fields[2] or None, fields[3] or None


def _wheel_name(path: Path) -> str:
    stem = path.name[:-4] if path.name.endswith(".whl") else path.stem
    parts = stem.split("-")
    if len(parts) >= 5:
        return parts[0]
    return path.stem


def _sdist_name(path: Path) -> str:
    return distribution_filename_fields(str(path))[0]


def _local_project_name(root: Path) -> str:
    config = root / "pyproject.toml"
    if not config.is_file() or tomllib is None:
        return directory_project_name(root.name)
    project = tomllib.loads(config.read_text(encoding="utf-8")).get("project", {})
    name = project.get("name")
    if name is None:
        return directory_project_name(root.name)
    if not isinstance(name, str):
        raise ValueError("PCC-PKG-PROJECT-NAME-INVALID")
    validate_project_name(name)
    return name


def _source_kind(path: Path | None) -> str:
    if path is None:
        return "unresolved"
    if path.is_dir():
        return "local_source"
    name = path.name.lower()
    if name.endswith(".whl"):
        return "wheel"
    if name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip")):
        return "sdist"
    return "artifact"


def _archive_names(path: Path) -> tuple[str, ...]:
    try:
        if path.name.lower().endswith(".whl") or path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                return tuple(zf.namelist())
        if path.name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
            with tarfile.open(path) as tf:
                return tuple(m.name for m in tf.getmembers())
    except Exception:
        return ()
    return ()


def _scan_names(names: Iterable[str]) -> dict[str, tuple[str, ...]]:
    generated_c: list[str] = []
    pyx: list[str] = []
    fortran: list[str] = []
    native_ext: list[str] = []
    blas: list[str] = []
    lapack: list[str] = []
    relevant_names = [name for name in names if is_build_relevant_name(name)]
    c_names = {name[:-2] for name in relevant_names if name.lower().endswith(".c")}
    for name in relevant_names:
        lower = name.lower()
        suffix = Path(lower).suffix
        if lower.endswith(".pyx"):
            pyx.append(name)
            if name[:-4] in c_names:
                generated_c.append(name[:-4] + ".c")
        elif suffix in _FORTRAN_SUFFIXES:
            fortran.append(name)
        elif suffix in _SHARED_SUFFIXES:
            native_ext.append(name)
        if "blas" in lower:
            blas.append(name)
        if "lapack" in lower:
            lapack.append(name)
    return {
        "generated_c": tuple(sorted(set(generated_c))),
        "pyx": tuple(sorted(pyx)),
        "fortran": tuple(sorted(fortran)),
        "native_ext": tuple(sorted(native_ext)),
        "blas": tuple(sorted(set(blas))),
        "lapack": tuple(sorted(set(lapack))),
    }


def _looks_generated_c(path: Path) -> bool:
    if path.suffix.lower() != ".c":
        return False
    sibling_pyx = path.with_suffix(".pyx")
    if sibling_pyx.exists():
        return True
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
    except Exception:
        return False
    return "generated by cython" in head or "cython" in head and "generated" in head


def inspect_artifact(
    name: str, path: str | Path | None = None
) -> PackageArtifactMetadata:
    raw_path = Path(path).expanduser().resolve() if path is not None else None
    source_kind = _source_kind(raw_path)
    package_name = (name or "").strip()
    python_tag = abi_tag = platform_tag = None
    pyproject_backend = None
    pyproject_requires: tuple[str, ...] = ()
    meson_build = False
    compile_commands = CompileCommandSummary(None, 0, 0, 0, 0)
    meson_options: dict[str, bool] = {}
    diagnostics: list[str] = []

    names: tuple[str, ...] = ()
    generated_from_content: list[str] = []
    if raw_path is not None and raw_path.is_dir():
        package_name = package_name or _local_project_name(raw_path)
        file_paths = tuple(_iter_files(raw_path))
        names = tuple(str(p.relative_to(raw_path)) for p in file_paths)
        generated_from_content = [
            str(p.relative_to(raw_path))
            for p in file_paths
            if is_build_relevant_path(raw_path, p) and _looks_generated_c(p)
        ]
        pyproject_backend, pyproject_requires = _read_pyproject(raw_path)
        meson_build = (raw_path / "meson.build").exists() or (
            pyproject_backend is not None and "meson" in pyproject_backend.lower()
        )
        meson_options = _read_meson_boolean_options(raw_path)
        compile_commands = _summarize_compile_commands(raw_path)
    elif raw_path is not None and raw_path.exists():
        if source_kind == "wheel":
            package_name = package_name or _wheel_name(raw_path)
            python_tag, abi_tag, platform_tag = _wheel_tags(raw_path)
        elif source_kind == "sdist":
            package_name = package_name or _sdist_name(raw_path)
        else:
            package_name = package_name or raw_path.stem
        names = _archive_names(raw_path)
    else:
        package_name = package_name or "package"
        diagnostics.append("artifact path is unresolved")

    scan = _scan_names(names)
    generated_c = tuple(
        sorted(set(scan["generated_c"] + tuple(generated_from_content)))
    )
    pyx = scan["pyx"]
    generated_c_policy = "none"
    requires_regen = False
    if generated_c:
        generated_c_policy = "consume_generated_c"
    elif pyx:
        generated_c_policy = "requires_cython_regeneration"
        requires_regen = True

    return PackageArtifactMetadata(
        name=package_name,
        path=str(raw_path) if raw_path is not None else None,
        source_kind=source_kind,
        python_tag=python_tag,
        abi_tag=abi_tag,
        platform_tag=platform_tag,
        current_platform_tag=current_platform_tag(),
        pcc_native_wheel_tag=pcc_native_wheel_tag(),
        pyproject_build_backend=pyproject_backend,
        pyproject_requires=pyproject_requires,
        meson_build=meson_build,
        compile_commands=compile_commands,
        generated_c_artifacts=generated_c,
        cython_sources=pyx,
        fortran_sources=scan["fortran"],
        native_extensions=scan["native_ext"],
        blas_indicators=scan["blas"],
        lapack_indicators=scan["lapack"],
        native_library_fallbacks=_native_library_fallbacks_from_options(meson_options),
        generated_c_policy=generated_c_policy,
        requires_cython_regeneration=requires_regen,
        diagnostics=tuple(diagnostics),
    )
