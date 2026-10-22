"""Generic pcc package install manifest flow.

This is a real local/cache install skeleton, not a NumPy-specific shortcut.
It copies local source trees or extracts wheel/sdist artifacts into a pcc site
directory and writes a manifest that future import/build steps can consume.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

from pcc.package.schema import PACKAGE_MANIFEST_SCHEMA, PACKAGE_MANIFEST_SCHEMA_VERSION, capability_profile, distribution_filename_fields, source_build_policy, wheel_tag_fields, wheel_tags
from pcc.package.environment import default_package_cache, default_package_site
from pcc.package.metadata_paths import package_metadata_member_paths, package_metadata_paths

from .inspect import inspect_package
from .linkage import linkage_report
from .metadata import inspect_artifact, pcc_native_wheel_tag

_REQ_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+")
_VERSION_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")
_REPOSITORY_MANIFEST = "pcc-wheel-repository.json"
_ARTIFACT_SUFFIXES = (".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip")
_IMPORTABLE_SOURCE_SUFFIXES = (".py", ".pyi")
_IMPORTABLE_NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")
_PACKAGE_METADATA_DIR_SUFFIXES = (".dist-info", ".egg-info")
BUILD_MODES = ("owned", "host")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_site_dir() -> Path:
    return Path(default_package_site())


def _default_cache_dir() -> Path:
    return Path(default_package_cache())


def _normalized_package_name(name: str) -> str:
    return name.lower().replace("_", "-")


def _artifact_project_name(path: Path) -> str:
    return distribution_filename_fields(str(path))[0]


def _artifact_version_text(path: Path) -> str:
    return distribution_filename_fields(str(path))[1]


def _wheel_tags_from_name(name: str) -> tuple[str | None, str | None, str | None]:
    fields = wheel_tag_fields(name)
    return fields[1] or None, fields[2] or None, fields[3] or None


def _version_key(version: str) -> tuple[tuple[int, object], ...]:
    key: list[tuple[int, object]] = []
    for token in _VERSION_TOKEN_RE.findall(version):
        if token.isdigit():
            key.append((0, int(token)))
        else:
            key.append((1, token.lower()))
    return tuple(key) or ((0, 0),)


def _artifact_selection_key(
    path: Path,
    compatibility_reason: str | None = None,
) -> tuple[tuple[tuple[int, object], ...], int, str]:
    ranks = {
        "source_artifact": 1,
        "pure_python_wheel": 2,
        "pcc_native_wheel": 3,
    }
    return (
        _version_key(_artifact_version_text(path)),
        ranks.get(compatibility_reason or "", 0),
        path.name,
    )


def _artifact_compatibility_reason(path: Path, *, abi: str) -> tuple[bool, str]:
    return _artifact_compatibility_reason_from_name(path.name, abi=abi)


def _artifact_compatibility_reason_from_name(
    name: str, *, abi: str
) -> tuple[bool, str]:
    if abi != "pcc-native":
        return True, "abi_mode_allows_artifact"
    lower = name.lower()
    if lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip")):
        return True, "source_artifact"
    if not lower.endswith(".whl"):
        return False, "unsupported_artifact_kind"
    python_tag, abi_tag, platform_tag = _wheel_tags_from_name(name)
    if python_tag == "py3" and abi_tag == "none" and platform_tag == "any":
        return True, "pure_python_wheel"
    if f"{python_tag}-{abi_tag}-{platform_tag}" == pcc_native_wheel_tag():
        return True, "pcc_native_wheel"
    return False, "wheel_tag_not_pcc_native_compatible"


def _repository_manifest_candidates(
    root: Path,
    expected: str,
    *,
    abi: str,
) -> list[tuple[Path, str]]:
    manifest_path = root / _REPOSITORY_MANIFEST
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = manifest.get("artifacts")
    if not isinstance(rows, list):
        return []
    candidates: list[tuple[Path, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if _normalized_package_name(name) != expected:
            continue
        path_text = row.get("path")
        if not isinstance(path_text, str):
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            continue
        reason = str(row.get("compatibility_reason") or "")
        if abi == "pcc-native" and (
            not bool(row.get("pcc_native_compatible"))
            or bool(row.get("links_libpython"))
        ):
            continue
        candidates.append((path.resolve(), reason))
    return candidates


def _parse_requires_dist_name(line: str) -> str | None:
    if not line.startswith("Requires-Dist:"):
        return None
    rest = line[len("Requires-Dist:") :].strip()
    match = _REQ_NAME_RE.match(rest)
    if match is None:
        return None
    return match.group(0)


def _requires_dist_diagnostics_text(text: str) -> tuple[str, ...]:
    diagnostics: list[str] = []
    for line in text.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        rest = line[len("Requires-Dist:") :].strip()
        dep = _parse_requires_dist_name(line) or "dependency"
        name_end = len(dep)
        if "[" in rest[: rest.find(";") if ";" in rest else len(rest)]:
            diagnostics.append(f"unsupported_extras:{dep}")
        if any(op in rest[name_end:] for op in ("<", ">", "=", "!", "~")):
            diagnostics.append(f"unresolved_version_constraint:{dep}")
        if ";" in rest:
            diagnostics.append(f"unsupported_environment_marker:{dep}")
    return tuple(dict.fromkeys(diagnostics))


def _metadata_requires_dist_text(text: str) -> tuple[str, ...]:
    deps: list[str] = []
    for line in text.splitlines():
        dep = _parse_requires_dist_name(line)
        if dep:
            deps.append(dep)
    return tuple(deps)


def _artifact_metadata_texts(source: Path) -> list[str]:
    """Read only metadata selected by the common artifact-location policy."""
    texts: list[str] = []
    if source.is_dir():
        for name in package_metadata_paths(str(source)):
            try:
                texts.append(Path(name).read_text(encoding="utf-8"))
            except OSError:
                continue
        return texts
    lower = source.name.lower()
    try:
        if lower.endswith((".whl", ".zip")):
            with zipfile.ZipFile(source) as archive:
                for name in package_metadata_member_paths(archive.namelist()):
                    texts.append(archive.read(name).decode("utf-8", errors="replace"))
        elif lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
            with tarfile.open(source) as archive:
                members = archive.getmembers()
                selected = set(package_metadata_member_paths([member.name for member in members]))
                for member in members:
                    if member.isfile() and member.name in selected:
                        extracted = archive.extractfile(member)
                        if extracted is not None:
                            texts.append(extracted.read().decode("utf-8", errors="replace"))
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return []
    return texts


def artifact_requires_dist_diagnostics(path: str | Path) -> tuple[str, ...]:
    diagnostics: list[str] = []
    for text in _artifact_metadata_texts(Path(path).expanduser().resolve()):
        diagnostics.extend(_requires_dist_diagnostics_text(text))
    return tuple(dict.fromkeys(diagnostics))


def artifact_requires_dist(path: str | Path) -> tuple[str, ...]:
    deps: list[str] = []
    for text in _artifact_metadata_texts(Path(path).expanduser().resolve()):
        deps.extend(_metadata_requires_dist_text(text))
    return tuple(dict.fromkeys(deps))


def _find_links_artifact_with_origin(
    spec: str,
    find_links: list[str] | tuple[str, ...],
    *,
    abi: str = "pcc-native",
) -> tuple[Path | None, str | None]:
    expected = _normalized_package_name(spec)
    manifest_matches: list[tuple[Path, str]] = []
    matches: list[tuple[Path, str]] = []
    for link in find_links:
        root = Path(link).expanduser()
        if root.is_dir():
            manifest_matches.extend(
                _repository_manifest_candidates(root, expected, abi=abi)
            )
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = [child for child in root.iterdir() if child.is_file()]
        else:
            candidates = []
        for candidate in candidates:
            if not candidate.name.endswith(_ARTIFACT_SUFFIXES):
                continue
            if _normalized_package_name(_artifact_project_name(candidate)) == expected:
                allowed, reason = _artifact_compatibility_reason(candidate, abi=abi)
                if allowed:
                    matches.append((candidate.resolve(), reason))
    if manifest_matches:
        best = sorted(
            manifest_matches,
            key=lambda item: _artifact_selection_key(item[0], item[1]),
        )[-1]
        return best[0], "wheel-repository"
    if matches:
        best = sorted(
            matches,
            key=lambda item: _artifact_selection_key(item[0], item[1]),
        )[-1]
        return best[0], "find-links"
    return None, None


def _find_links_artifact(
    spec: str,
    find_links: list[str] | tuple[str, ...],
    *,
    abi: str = "pcc-native",
) -> Path | None:
    path, _origin = _find_links_artifact_with_origin(spec, find_links, abi=abi)
    return path


def _simple_index_package_url(index_url: str, package: str) -> str:
    base = index_url.rstrip("/") + "/"
    return urllib.parse.urljoin(base, _normalized_package_name(package) + "/")


def _simple_index_links(page_text: str, page_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in re.finditer(
        r"""href\s*=\s*['"]([^'"]+)['"]""", page_text, re.IGNORECASE
    ):
        href = match.group(1)
        url = urllib.parse.urljoin(page_url, href)
        name = Path(urllib.parse.urlparse(url).path).name
        if name:
            links.append((url, name))
    return links


def _download_index_artifact(
    spec: str,
    cache_dir: Path,
    index_urls: list[str] | tuple[str, ...],
    *,
    abi: str,
) -> Path | None:
    expected = _normalized_package_name(spec)
    candidates: list[tuple[str, str, str]] = []
    for index_url in index_urls:
        page_url = _simple_index_package_url(index_url, spec)
        try:
            with urllib.request.urlopen(page_url, timeout=20) as response:
                page_text = response.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for artifact_url, filename in _simple_index_links(page_text, page_url):
            if not filename.endswith(_ARTIFACT_SUFFIXES):
                continue
            if (
                _normalized_package_name(_artifact_project_name(Path(filename)))
                != expected
            ):
                continue
            allowed, reason = _artifact_compatibility_reason_from_name(
                filename, abi=abi
            )
            if allowed:
                candidates.append((artifact_url, filename, reason))
    if not candidates:
        return None
    best_url, best_name, _best_reason = sorted(
        candidates,
        key=lambda item: _artifact_selection_key(Path(item[1]), item[2]),
    )[-1]
    download_dir = cache_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / best_name
    try:
        with urllib.request.urlopen(best_url, timeout=60) as response:
            dest.write_bytes(response.read())
    except (OSError, ValueError):
        return None
    return dest.resolve()


def local_resolver_diagnostics(
    packages: list[str] | tuple[str, ...],
    *,
    cache_dir: str | Path | None = None,
    find_links: list[str] | tuple[str, ...] = (),
    index_urls: list[str] | tuple[str, ...] = (),
    abi: str = "pcc-native",
) -> list[str]:
    cache = (
        Path(cache_dir).expanduser().resolve() if cache_dir else _default_cache_dir()
    )
    diagnostics: list[str] = []
    for package in resolve_local_install_order(
        packages,
        cache_dir=cache,
        find_links=find_links,
        index_urls=index_urls,
        abi=abi,
    ):
        source = _resolve_spec(
            str(package), cache, find_links, index_urls=index_urls, abi=abi
        )
        if source is None:
            continue
        for diagnostic in artifact_requires_dist_diagnostics(source):
            diagnostics.append(f"{package}:{diagnostic}")
    return list(dict.fromkeys(diagnostics))


def resolve_local_install_order(
    packages: list[str] | tuple[str, ...],
    *,
    cache_dir: str | Path | None = None,
    find_links: list[str] | tuple[str, ...] = (),
    index_urls: list[str] | tuple[str, ...] = (),
    abi: str = "pcc-native",
) -> list[str]:
    cache = (
        Path(cache_dir).expanduser().resolve() if cache_dir else _default_cache_dir()
    )
    ordered: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(pkg: str) -> None:
        key = _normalized_package_name(pkg)
        if key in done or key in visiting:
            return
        visiting.add(key)
        source = _resolve_spec(pkg, cache, find_links, index_urls=index_urls, abi=abi)
        if source is not None:
            for dep in artifact_requires_dist(source):
                if (
                    _resolve_spec(
                        dep, cache, find_links, index_urls=index_urls, abi=abi
                    )
                    is not None
                ):
                    visit(dep)
        visiting.remove(key)
        done.add(key)
        ordered.append(pkg)

    for package in packages:
        visit(str(package))
    return ordered


def _resolve_spec(
    spec: str,
    cache_dir: Path | None = None,
    find_links: list[str] | tuple[str, ...] = (),
    index_urls: list[str] | tuple[str, ...] = (),
    *,
    abi: str = "pcc-native",
) -> Path | None:
    path, _origin = _resolve_spec_with_origin(
        spec,
        cache_dir,
        find_links,
        index_urls=index_urls,
        abi=abi,
    )
    return path


def _resolve_spec_with_origin(
    spec: str,
    cache_dir: Path | None = None,
    find_links: list[str] | tuple[str, ...] = (),
    index_urls: list[str] | tuple[str, ...] = (),
    *,
    abi: str = "pcc-native",
) -> tuple[Path | None, str | None]:
    candidate = Path(spec).expanduser()
    if candidate.exists():
        return candidate.resolve(), "direct"
    projects = _repo_root() / "projects"
    matches = sorted(projects.glob(spec + "-*")) if projects.exists() else []
    if matches:
        return matches[-1].resolve(), "projects"
    linked, origin = _find_links_artifact_with_origin(spec, find_links, abi=abi)
    if linked is not None:
        return linked, origin
    if cache_dir is not None and cache_dir.exists():
        direct = cache_dir / spec
        if direct.exists():
            return direct.resolve(), "cache"
        for suffix in (".whl", ".tar.gz", ".tgz", ".zip"):
            matches = sorted(cache_dir.glob(spec + "-*" + suffix))
            if matches:
                return matches[-1].resolve(), "cache"
    if cache_dir is not None and index_urls:
        downloaded = _download_index_artifact(spec, cache_dir, index_urls, abi=abi)
        if downloaded is not None:
            return downloaded, "index-url"
    return None, None


def _iter_importable_roots(source: Path) -> Iterable[Path]:
    """Yield top-level importable package/module payloads from a source tree."""
    bases = [source, source / "src"]
    visible_dirs = (
        [
            child
            for child in sorted(source.iterdir())
            if source.is_dir()
            if child.is_dir() and not _skip_importable_dir(child)
        ]
        if source.is_dir()
        else []
    )
    if len(visible_dirs) == 1:
        bases.append(visible_dirs[0])
        bases.append(visible_dirs[0] / "src")

    seen: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        source_project = (base / "pyproject.toml").is_file() or (base / "setup.py").is_file()
        if (base / "__init__.py").is_file() or (
            _has_direct_importable_payload(base) and not source_project
        ):
            resolved = base.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield base
            continue
        for child in sorted(base.iterdir()):
            if _skip_importable_dir(child):
                continue
            if child.is_dir() and (
                (child / "__init__.py").is_file()
                or _has_direct_importable_payload(child)
            ):
                resolved = child.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield child
            elif child.is_file() and child.suffix == ".py" and child.name != "setup.py":
                resolved = child.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield child


def _skip_importable_dir(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name == "__pycache__":
        return True
    return _name_endswith_any(name, _PACKAGE_METADATA_DIR_SUFFIXES)


def _has_direct_importable_payload(path: Path) -> bool:
    if not path.is_dir():
        return False
    path_text = str(path)
    try:
        names = os.listdir(path_text)
    except OSError:
        return False
    for name in names:
        if name.startswith(".") or name == "__pycache__":
            continue
        if name == "setup.py":
            continue
        child_path = os.path.join(path_text, name)
        if not os.path.isfile(child_path):
            continue
        if _name_endswith_any(name, _IMPORTABLE_SOURCE_SUFFIXES):
            return True
        if _name_endswith_any(name.lower(), _IMPORTABLE_NATIVE_SUFFIXES):
            return True
    return False


def _name_endswith_any(name: str, suffixes: tuple[str, ...]) -> bool:
    for suffix in suffixes:
        if name.endswith(suffix):
            return True
    return False


def _copy_importable_payload(
    source: Path, site: Path, metadata_name: str
) -> tuple[Path, list[str]]:
    importable = list(_iter_importable_roots(source))
    preferred = [
        payload
        for payload in importable
        if payload.is_dir()
        and _normalized_package_name(payload.name)
        == _normalized_package_name(metadata_name)
    ]
    if preferred:
        importable = preferred
    if not importable:
        install_root = site / metadata_name
        if install_root.exists():
            shutil.rmtree(install_root)
        install_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, install_root / "source", dirs_exist_ok=True)
        return install_root, [str(install_root / "source")]

    first = importable[0]
    install_root = site / (first.name if first.is_dir() else metadata_name)
    if not first.is_dir():
        install_root.mkdir(parents=True, exist_ok=True)
    payloads: list[str] = []
    for payload in importable:
        if payload.is_dir():
            dest = site / payload.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(payload, dest)
        else:
            dest = site / payload.name
            if dest.exists():
                dest.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload, dest)
        payloads.append(str(dest))
    return install_root, payloads


def _overlay_meson_build_payloads(
    source: Path, site: Path, installed: list[str], metadata_name: str
) -> list[str]:
    """Overlay pcc-managed Meson build outputs onto an installed source tree.

    Meson build directories often contain generated Python files, headers, and
    extension modules while the source tree contains the rest of the package.
    A pip-style local install needs the merged view. This stays package
    agnostic by copying any importable payload from pcc's Meson build dir.
    """
    build_root = source / "build" / "pcc-package" / "meson-build"
    if not build_root.is_dir():
        return installed
    payloads = list(installed)
    seen = set(payloads)
    importable = list(_iter_importable_roots(build_root))
    preferred = [
        payload
        for payload in importable
        if payload.is_dir()
        and _normalized_package_name(payload.name)
        == _normalized_package_name(metadata_name)
    ]
    if preferred:
        importable = preferred
    for payload in importable:
        if payload.is_dir():
            dest = site / payload.name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(payload, dest, dirs_exist_ok=True)
        else:
            dest = site / payload.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload, dest)
        dest_text = str(dest)
        if dest_text not in seen:
            seen.add(dest_text)
            payloads.append(dest_text)
    return payloads


def _archive_importable_roots(artifact_dir: Path) -> list[Path]:
    return list(_iter_importable_roots(artifact_dir))


def _copy_payloads_to_site(
    payloads: list[Path],
    site: Path,
    metadata_name: str,
) -> tuple[Path, list[str]]:
    install_root = site / metadata_name
    installed: list[str] = []
    for payload in payloads:
        if payload.is_dir():
            dest = site / payload.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(payload, dest)
            if payload.name == metadata_name:
                install_root = dest
        else:
            dest = site / payload.name
            if dest.exists():
                dest.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload, dest)
        installed.append(str(dest))
    install_root.mkdir(parents=True, exist_ok=True)
    return install_root, installed


def _copy_or_extract(
    source: Path, site: Path, metadata_name: str
) -> tuple[Path, list[str]]:
    site.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        install_root, installed = _copy_importable_payload(source, site, metadata_name)
        installed = _overlay_meson_build_payloads(
            source, site, installed, metadata_name
        )
        preferred_root = site / metadata_name.replace("-", "_")
        if preferred_root.is_dir():
            install_root = preferred_root
        return install_root, installed

    lower = source.name.lower()
    install_root = site / metadata_name
    if install_root.exists():
        shutil.rmtree(install_root)
    if lower.endswith(".whl") or lower.endswith(".zip"):
        with tempfile.TemporaryDirectory(prefix="pcc_pkg_extract_") as tmp:
            artifact_dir = Path(tmp) / "artifact"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(source) as zf:
                zf.extractall(artifact_dir)
            importable = _archive_importable_roots(artifact_dir)
            if importable:
                return _copy_payloads_to_site(importable, site, metadata_name)
            install_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(artifact_dir, install_root / "artifact")
            return install_root, [str(install_root / "artifact")]
    if lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        with tempfile.TemporaryDirectory(prefix="pcc_pkg_extract_") as tmp:
            artifact_dir = Path(tmp) / "artifact"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(source) as tf:
                tf.extractall(artifact_dir, filter="data")
            importable = _archive_importable_roots(artifact_dir)
            if importable:
                return _copy_payloads_to_site(importable, site, metadata_name)
            install_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(artifact_dir, install_root / "artifact")
            return install_root, [str(install_root / "artifact")]
    artifact_dir = install_root / "artifact"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, artifact_dir / source.name)
    return install_root, [str(artifact_dir / source.name)]


def _build_requirement_tool_wrappers(
    requires: tuple[str, ...],
) -> tempfile.TemporaryDirectory[str] | None:
    cython_requirement = None
    for requirement in requires:
        if requirement.lower().startswith("cython"):
            cython_requirement = requirement
            break
    if cython_requirement is None:
        return None
    temp_dir = tempfile.TemporaryDirectory(prefix="pcc_build_tools_")
    bin_dir = Path(temp_dir.name)
    script = (
        "#!/bin/sh\n"
        "exec uv run --no-project --with " + repr(cython_requirement) + ' cython "$@"\n'
    )
    for name in ("cython", "cython3"):
        tool = bin_dir / name
        tool.write_text(script, encoding="utf-8")
        tool.chmod(0o755)
    return temp_dir


def _meson_setup_command(
    source: Path, build_dir: Path, path_env: str
) -> list[str] | None:
    meson_tool = shutil.which("meson", path=path_env)
    if meson_tool is not None:
        return [meson_tool, "setup", str(build_dir), str(source)]
    vendored = source / "vendored-meson" / "meson" / "meson.py"
    if vendored.exists():
        return [sys.executable, str(vendored), "setup", str(build_dir), str(source)]
    return None


def _ensure_meson_build_outputs(
    source: Path,
    requires: tuple[str, ...],
    *,
    build_mode: str = "owned",
) -> dict[str, object]:
    if not (source / "meson.build").is_file():
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_meson_build",
            "actions": [],
            "build_mode_requested": build_mode,
            "build_ownership": "not-required",
            "host_assisted": False,
            "host_python": None,
            "host_free_build_claim": True,
        }
    if build_mode not in BUILD_MODES:
        return {
            "ok": False,
            "skipped": False,
            "reason": "build_mode_invalid",
            "actions": [],
            "build_mode_requested": build_mode,
            "build_ownership": "unresolved",
            "host_assisted": False,
            "host_python": None,
            "host_free_build_claim": False,
            "diagnostics": ["PCC-PKG-BUILD-MODE-INVALID"],
        }
    if build_mode == "owned":
        # A Meson source build is a Python-program execution boundary.  The
        # host implementation cannot make that boundary pcc-owned merely by
        # hiding python3 from PATH or pointing PCC_HOST_PYTHON at a failing
        # executable.  Until a pcc-compiled Meson/build-exec artifact is
        # supplied, owned mode must stop before creating wrappers, configuring
        # Meson, or starting Ninja.
        return {
            "ok": False,
            "skipped": False,
            "reason": "owned_build_tool_required",
            "actions": [],
            "build_backend": "meson",
            "build_mode_requested": build_mode,
            "build_ownership": "owned-unavailable",
            "host_assisted": False,
            "host_python": None,
            "host_free_build_claim": False,
            "diagnostics": ["PCC-PKG-OWNED-BUILD-TOOL-REQUIRED"],
        }
    build_dir = source / "build" / "pcc-package" / "meson-build"
    timeout = int(os.environ.get("PCC_PACKAGE_BUILD_TIMEOUT", "600"))
    wrappers = _build_requirement_tool_wrappers(requires)
    prefix = wrappers.name if wrappers is not None else ""
    env = os.environ.copy()
    if prefix:
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    path_env = env.get("PATH", "")
    actions: list[dict[str, object]] = []
    try:
        if not (build_dir / "build.ninja").is_file():
            setup_command = _meson_setup_command(source, build_dir, path_env)
            if setup_command is None:
                return {
                    "ok": False,
                    "skipped": False,
                    "reason": "meson_not_available",
                    "actions": [],
                    "build_backend": "meson",
                    "build_mode_requested": build_mode,
                    "build_ownership": "host",
                    "host_assisted": True,
                    "host_python": sys.executable,
                    "host_free_build_claim": False,
                    "diagnostics": ["PCC-PKG-HOST-BUILD-TOOL-MISSING"],
                }
            build_dir.mkdir(parents=True, exist_ok=True)
            setup = subprocess.run(
                setup_command,
                cwd=source,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            actions.append(
                {
                    "kind": "meson_setup",
                    "command": setup_command,
                    "returncode": setup.returncode,
                    "status": "passed" if setup.returncode == 0 else "failed",
                    "stdout": setup.stdout[-4000:],
                    "stderr": setup.stderr[-4000:],
                }
            )
            if setup.returncode != 0:
                return {
                    "ok": False,
                    "skipped": False,
                    "actions": actions,
                    "build_backend": "meson",
                    "build_mode_requested": build_mode,
                    "build_ownership": "host",
                    "host_assisted": True,
                    "host_python": sys.executable,
                    "host_free_build_claim": False,
                }
        ninja = shutil.which("ninja", path=path_env) or "ninja"
        try:
            build_jobs = max(1, int(os.environ.get("PCC_PACKAGE_BUILD_JOBS", "2")))
        except ValueError:
            build_jobs = 2
        build_command = [ninja, "-C", str(build_dir), "-j", str(build_jobs)]
        build = subprocess.run(
            build_command,
            cwd=source,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        actions.append(
            {
                "kind": "meson_build",
                "command": build_command,
                "returncode": build.returncode,
                "status": "passed" if build.returncode == 0 else "failed",
                "stdout": build.stdout[-4000:],
                "stderr": build.stderr[-4000:],
            }
        )
        return {
            "ok": build.returncode == 0,
            "skipped": False,
            "actions": actions,
            "build_backend": "meson",
            "build_mode_requested": build_mode,
            "build_ownership": "host",
            "host_assisted": True,
            "host_python": sys.executable,
            "host_free_build_claim": False,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        actions.append(
            {
                "kind": "meson_build",
                "command": [],
                "returncode": None,
                "status": "failed",
                "stdout": "",
                "stderr": str(exc),
            }
        )
        return {
            "ok": False,
            "skipped": False,
            "actions": actions,
            "build_backend": "meson",
            "build_mode_requested": build_mode,
            "build_ownership": "host",
            "host_assisted": True,
            "host_python": sys.executable,
            "host_free_build_claim": False,
        }
    finally:
        if wrappers is not None:
            wrappers.cleanup()


def _existing_payload_build_report(
    source: Path, *, build_mode: str
) -> dict[str, object] | None:
    """Recover persisted host provenance without trusting source self-claims.

    A source tree is an input, not a trust root.  The host installer cannot
    prove that a JSON file claiming ``build_ownership=owned`` was emitted by a
    pcc-native build, so owned mode always rejects this reuse boundary.  The
    pcc1 path owns its separate compiler/tool/source receipt and may rebuild
    from that closed input set.
    """
    manifest_path = source / "pcc-package.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    persisted = manifest.get("build_report")
    if not isinstance(persisted, dict) or not persisted.get("ok"):
        return None
    if build_mode == "owned":
        return {
            "ok": False,
            "skipped": False,
            "reason": "existing_build_provenance_unverified",
            "actions": [],
            "build_backend": "existing",
            "build_mode_requested": build_mode,
            "build_ownership": "prebuilt-unverified",
            "host_assisted": persisted.get("host_assisted"),
            "host_python": persisted.get("host_python"),
            "host_free_build_claim": False,
            "diagnostics": ["PCC-PKG-BUILD-PROVENANCE-UNVERIFIED"],
        }
    persisted_host = (
        persisted.get("build_ownership") == "host"
        and persisted.get("host_assisted") is True
        and isinstance(persisted.get("host_python"), str)
        and bool(str(persisted.get("host_python")).strip())
        and persisted.get("host_free_build_claim") is False
    )
    report = dict(persisted)
    if not persisted_host:
        report["build_ownership"] = "prebuilt-unverified"
        report["host_assisted"] = None
        report["host_python"] = None
        report["host_free_build_claim"] = False
    report["build_backend"] = "existing"
    report["build_mode_requested"] = build_mode
    report["reason"] = "existing_build_outputs"
    report["skipped"] = True
    report["actions"] = []
    return report


def _populate_cache_payload(cache_record: Path, installed_payloads: list[str]) -> None:
    cache_record.mkdir(parents=True, exist_ok=True)
    for payload_text in installed_payloads:
        payload = Path(payload_text)
        if not payload.exists():
            continue
        if payload.name == cache_record.name:
            dest = cache_record
        else:
            dest = cache_record / payload.name
        try:
            if dest.resolve() == payload.resolve():
                continue
        except FileNotFoundError:
            pass
        if payload.is_dir():
            if dest.exists():
                for child in dest.iterdir():
                    if child.name == "pcc-package.json":
                        continue
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                shutil.copytree(payload, dest, dirs_exist_ok=True)
            else:
                shutil.copytree(payload, dest)
        else:
            if dest.exists():
                dest.unlink()
            shutil.copy2(payload, dest)


def _artifact_sha256(source: Path) -> str | None:
    """Content digest of a wheel/sdist artifact.

    Directory sources deliberately return None: hashing a source tree either
    costs as much as the reinstall it would save, or degrades to a
    size/mtime approximation that can report "already satisfied" for changed
    content. A wheel is one immutable file, so its digest is both cheap and
    exact — that is the only case the reinstall fast path claims.
    """
    if not source.is_file():
        return None
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_manifests(site: Path) -> Iterable[dict]:
    if not site.is_dir():
        return
    for child in sorted(site.iterdir()):
        manifest_path = child / "pcc-package.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict):
            manifest["manifest_path"] = str(manifest_path)
            yield manifest


def _already_satisfied(site: Path, name: str, digest: str | None, abi: str):
    """The installed manifest that makes this install a no-op, or None.

    Matching on (name, artifact sha256, abi mode) plus a liveness check on the
    recorded payloads. The payload check is what keeps this from reporting
    success for a manifest whose files someone deleted.
    """
    if not digest:
        return None
    for manifest in _installed_manifests(site):
        if manifest.get("name") != name:
            continue
        if manifest.get("artifact_sha256") != digest:
            continue
        if manifest.get("abi_mode") != abi:
            continue
        if not manifest.get("install_success"):
            continue
        payloads = manifest.get("installed_payloads") or []
        if payloads and not all(os.path.exists(p) for p in payloads):
            continue
        if not os.path.isdir(str(manifest.get("installed_path") or "")):
            continue
        return manifest
    return None


def install_package(
    spec: str,
    *,
    force: bool = False,
    target_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    find_links: list[str] | tuple[str, ...] = (),
    index_urls: list[str] | tuple[str, ...] = (),
    abi: str = "pcc-native",
    use_cache: bool = True,
    build_source: bool = False,
    build_mode: str = "owned",
    resolved_from_override: str | None = None,
) -> dict[str, object]:
    cache = (
        Path(cache_dir).expanduser().resolve() if cache_dir else _default_cache_dir()
    )
    source, resolved_from = _resolve_spec_with_origin(
        spec,
        cache if use_cache else None,
        find_links,
        index_urls=index_urls,
        abi=abi,
    )
    if resolved_from_override is not None:
        resolved_from = resolved_from_override
    if source is None:
        return {
            "ok": False,
            "spec": spec,
            "error": "package artifact not found locally or in pcc cache",
        }

    metadata_name = "" if Path(spec).expanduser().exists() else spec
    metadata = inspect_artifact(metadata_name, source)
    site = (
        Path(target_dir).expanduser().resolve() if target_dir else _default_site_dir()
    )
    artifact_sha256 = _artifact_sha256(source)

    # Reinstalling the identical artifact used to redo everything — extract,
    # copy, rescan every binary, rebuild native sources — because nothing
    # compared the resolved artifact against what was already installed
    # (PKG-P2-REINSTALL-FASTPATH; measured at 168s for numpy). Everything
    # below this point is that work, so the match happens here.
    # `force` keeps upgrade/reinstall semantics able to redo it.
    if not force:
        satisfied = _already_satisfied(site, metadata.name, artifact_sha256, abi)
        if satisfied is not None:
            satisfied["install_action"] = "already-satisfied"
            satisfied["spec"] = spec
            satisfied["resolved_from"] = resolved_from or "unresolved"
            return satisfied

    with _prepared_source_tree(source, build_source=build_source) as install_source:
        return _install_prepared_package(
            spec=spec, source=source, install_source=install_source,
            metadata=metadata, site=site, cache=cache, abi=abi,
            build_source=build_source, build_mode=build_mode,
            resolved_from=resolved_from, artifact_sha256=artifact_sha256,
        )


@contextmanager
def _prepared_source_tree(source: Path, *, build_source: bool):
    """Keep an sdist's extracted build tree alive until payload publication.

    Wheels already contain built payloads. Source archives use the same build
    policy and output checks as source directories; extracting directly into
    site-packages would bypass those checks.
    """
    archive = source.name.lower().endswith(_ARTIFACT_SUFFIXES[1:])
    if not build_source or source.is_dir() or not archive:
        yield source
        return
    with tempfile.TemporaryDirectory(prefix="pcc_pkg_build_") as tmp:
        root = Path(tmp)
        if source.name.lower().endswith(".zip"):
            with zipfile.ZipFile(source) as zf:
                zf.extractall(root)
        else:
            with tarfile.open(source) as tf:
                tf.extractall(root, filter="data")
        markers = ("pyproject.toml", "setup.py", "setup.cfg", "meson.build", "PKG-INFO")
        if not any((root / marker).is_file() for marker in markers):
            candidates = [
                child for child in root.iterdir()
                if child.is_dir() and any((child / marker).is_file() for marker in markers)
            ]
            if len(candidates) == 1:
                root = candidates[0]
        yield root


def _install_prepared_package(
    *, spec, source, install_source, metadata, site, cache, abi,
    build_source, build_mode, resolved_from, artifact_sha256,
) -> dict[str, object]:
    inspection = inspect_package(
        metadata.name, str(install_source) if install_source.is_dir() else None
    )
    build_report: dict[str, object] = {
        "ok": True,
        "skipped": True,
        "reason": "build_source_disabled",
        "actions": [],
        "build_mode_requested": build_mode,
        "build_ownership": "not-attempted",
        "host_assisted": None,
        "host_python": None,
        "host_free_build_claim": False,
    }
    source_policy = "unrecognized"
    if build_source and install_source.is_dir():
        project_config = install_source / "pyproject.toml"
        if project_config.is_file() and not (install_source / "meson.build").exists():
            try:
                source_policy = source_build_policy(project_config.read_text())
                if (install_source / "hatch.toml").exists() or (install_source / "hatch_build.py").exists():
                    source_policy = "requires_build_hook"
            except ValueError:
                source_policy = "unsupported_build_metadata"
    if source_policy in ("requires_build_hook", "unsupported_build_metadata"):
        build_report = {
            "ok": False,
            "skipped": False,
            "actions": [],
            "reason": ("declared_build_hook_requires_owner" if source_policy == "requires_build_hook"
                       else "unsupported_build_metadata"),
            "diagnostics": [("PCC-PKG-BUILD-HOOK-UNSUPPORTED" if source_policy == "requires_build_hook"
                             else "PCC-PKG-PROJECT-METADATA-UNSUPPORTED")],
            "build_mode_requested": build_mode,
            "build_ownership": "unresolved",
            "host_assisted": False,
            "host_python": None,
            "host_free_build_claim": False,
        }
    elif build_source and source_policy == "declarative_python_source":
        build_report = {
            "ok": True,
            "skipped": True,
            "reason": "declarative_python_source",
            "actions": [],
            "build_backend": "hatchling.build",
            "build_mode_requested": build_mode,
            "build_ownership": "not-required",
            "host_assisted": False,
            "host_python": None,
            "host_free_build_claim": True,
        }
    elif build_source and install_source.is_dir():
        persisted_report = _existing_payload_build_report(
            install_source, build_mode=build_mode
        )
        if persisted_report is not None:
            build_report = persisted_report
        else:
            build_report = _ensure_meson_build_outputs(
                install_source,
                inspect_artifact(metadata.name, install_source).pyproject_requires,
                build_mode=build_mode,
            )
    if not bool(build_report.get("ok", True)):
        # A failed source build is not an installable payload.  In particular,
        # owned mode deliberately stops before invoking a host Meson process;
        # copying that unbuilt tree into site-packages would turn the honest
        # failure into a partially published import shadow.  Return the build
        # provenance without mutating either the install target or cache.
        return {
            "ok": False,
            "install_success": False,
            "import_attempted": False,
            "import_success": None,
            "install_native_package_claim": False,
            "native_package_claim": False,
            "name": metadata.name,
            "spec": spec,
            "abi_mode": abi,
            "build_mode_requested": build_mode,
            "source_path": str(source),
            "resolved_from": resolved_from or "unresolved",
            "build_report": build_report,
            "diagnostics": list(build_report.get("diagnostics", [])),
        }
    install_root, installed_payloads = _copy_or_extract(install_source, site, metadata.name)

    cache_record = cache / metadata.name
    scan_roots = installed_payloads if installed_payloads else [str(install_root)]
    linkage = linkage_report(roots=scan_roots, abi_mode=abi)
    install_ok = bool(linkage.get("ok")) and bool(build_report.get("ok", True))

    # Claim separation (PKG-P0-INSTALL-IMPORT-SEPARATION): installing an
    # artifact (placing files + recording its wheel/ABI tags) proves nothing
    # about import, pcc-native ABI support, or no-libpython package support.
    # These fields are recorded as SEPARATE, honestly named facts so that an
    # install-success reader can never mistake it for import success or a
    # pcc-native support claim. Import is a distinct, later gate that this
    # install flow does not run, so it is never attempted here.
    artifact_wheel_tags = wheel_tags(source.name)
    import_attempted = False
    # import_success stays None precisely because import was not attempted; it
    # is a tri-state (True/False only after a real import gate runs).
    import_success = None
    # Install success (even of a CPython-ABI wheel accepted in cpython-compat/
    # libpython mode) is NEVER a pcc-native package claim. A native package
    # claim would require a separate, proven import/ABI gate under pcc-native
    # mode, which this flow does not perform.
    native_package_claim = False
    install_native_package_claim = False
    linkage_native_package_claim = bool(linkage.get("native_package_claim"))

    if install_ok:
        cache.mkdir(parents=True, exist_ok=True)
        source_is_cache_record = False
        try:
            source_is_cache_record = source.resolve() == cache_record.resolve()
        except FileNotFoundError:
            source_is_cache_record = False
        if not source_is_cache_record:
            _populate_cache_payload(cache_record, installed_payloads)
        else:
            cache_record.mkdir(parents=True, exist_ok=True)

    manifest = {
        "manifest_schema": PACKAGE_MANIFEST_SCHEMA,
        "schema_version": PACKAGE_MANIFEST_SCHEMA_VERSION,
        "ok": install_ok,
        "install_success": install_ok,
        "import_attempted": import_attempted,
        "import_success": import_success,
        "install_native_package_claim": install_native_package_claim,
        "linkage_native_package_claim": linkage_native_package_claim,
        "native_package_claim": native_package_claim,
        "wheel_tags": artifact_wheel_tags,
        "name": metadata.name,
        "spec": spec,
        "abi_mode": abi,
        "build_mode_requested": build_mode,
        # None for directory sources — see _artifact_sha256. A manifest
        # without it simply never matches the reinstall fast path.
        "artifact_sha256": artifact_sha256,
        "install_action": "installed",
        "source_path": str(source),
        "resolved_from": resolved_from or "unresolved",
        "installed_path": str(install_root),
        "installed_payload": (
            installed_payloads[0] if installed_payloads else str(install_root)
        ),
        "installed_payloads": installed_payloads,
        "cache_record": str(cache_record),
        "links_libpython": bool(linkage.get("links_libpython")),
        "no_libpython_runtime": bool(linkage.get("no_libpython_runtime")),
        "link_libpython_edges": list(linkage.get("link_libpython_edges", [])),
        "linkage": linkage,
        "pcc_native_wheel_tag": pcc_native_wheel_tag(),
        "metadata": metadata.as_dict(),
        "dependency_diagnostics": list(artifact_requires_dist_diagnostics(source)),
        "diagnostics": list(linkage.get("diagnostics", [])),
        "inspection": inspection.as_dict(),
        "build_report": build_report,
        "capability_profile": capability_profile(
            abi,
            bool(linkage.get("scans")),
            bool(linkage.get("links_libpython")),
            bool(linkage.get("uses_cpython_extension_abi")),
        ),
    }
    manifest_path = install_root / "pcc-package.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    if install_ok:
        (cache_record / "pcc-package.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package install")
    parser.add_argument("spec")
    parser.add_argument("--target", "--target-dir", dest="target_dir", default=None)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--find-links", "-f", action="append", default=[])
    parser.add_argument("--index-url", "-i", action="append", default=[])
    parser.add_argument("--extra-index-url", action="append", default=[])
    parser.add_argument("--abi", default="pcc-native")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--build-source", action="store_true")
    parser.add_argument("--build", choices=BUILD_MODES, default="owned")
    parser.add_argument(
        "--force",
        action="store_true",
        help="reinstall even when the identical artifact is already installed",
    )
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    result = install_package(
        ns.spec,
        force=ns.force,
        target_dir=ns.target_dir,
        cache_dir=ns.cache_dir,
        find_links=ns.find_links,
        index_urls=ns.index_url + ns.extra_index_url,
        abi=ns.abi,
        use_cache=not ns.no_cache,
        build_source=ns.build_source,
        build_mode=ns.build,
    )
    if ns.json or True:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
