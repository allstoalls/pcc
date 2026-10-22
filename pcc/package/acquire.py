"""Explicit package-acquisition backends for ``pcc -m pip``.

Acquisition stops at an immutable local artifact.  Building and installing the
artifact remain pcc operations.  The two online backends deliberately expose
different provenance:

``owned``
    implements the PEP 503/691 HTML link-selection path in pcc and uses only a
    byte transport beneath it.  It requires a repository-provided SHA-256.
``host``
    is an explicit compatibility mode that asks a host Python's pip to select
    and download one artifact, without dependencies.  ``auto`` prefers the
    owned path so acquiring an sdist cannot trigger pip's PEP 517 metadata
    build before pcc performs its own native build.

Neither backend claims dependency resolution or PEP 517 build isolation.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

from pcc.package.environment import DEFAULT_PYTHON_SEMANTIC_TARGET
from pcc.package.schema import distribution_filename_fields

from .install import (
    _ARTIFACT_SUFFIXES,
    _artifact_compatibility_reason_from_name,
    _artifact_project_name,
    _artifact_selection_key,
    _normalized_package_name,
    artifact_requires_dist,
)

DEFAULT_INDEX_URL = "https://pypi.org/simple"
DEFAULT_TARGET_PYTHON = DEFAULT_PYTHON_SEMANTIC_TARGET
ACQUIRE_MODES = ("auto", "host", "owned", "offline")
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?)"
    r"(?:==(?P<version>[A-Za-z0-9][A-Za-z0-9_.!+-]*))?$"
)


class _SimpleLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        href = values.get("href", "")
        if href:
            self.hrefs.append(href)
            self.links.append((href, values.get("data-requires-python", "")))


def target_python_version(value: str | None = None) -> str:
    """Return the explicit Python language version used for package choice."""
    selected = (
        value or os.environ.get("PCC_PACKAGE_TARGET_PYTHON") or DEFAULT_TARGET_PYTHON
    )
    selected = selected.strip()
    if re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", selected) is None:
        raise ValueError(
            "PCC-PKG-ACQUIRE-TARGET-PYTHON-INVALID: expected major.minor[.patch]"
        )
    return selected


def _numeric_version(value: str) -> tuple[int, ...] | None:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) is None:
        return None
    return tuple(int(part) for part in value.split("."))


def _compare_numeric_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    lhs = left + (0,) * (width - len(left))
    rhs = right + (0,) * (width - len(right))
    return (lhs > rhs) - (lhs < rhs)


def requires_python_allows(specifier: str, target_python: str) -> bool:
    """Evaluate the final-release subset used by Simple Repository metadata.

    Unsupported clauses fail closed. This is target selection, not a claim to
    implement the complete PEP 440 resolver.
    """
    if not specifier.strip():
        return True
    target = _numeric_version(target_python)
    if target is None:
        return False
    for raw_clause in specifier.split(","):
        clause = raw_clause.strip()
        match = re.fullmatch(r"(===|~=|==|!=|<=|>=|<|>)([^\s]+)", clause)
        if match is None:
            return False
        op, raw_bound = match.groups()
        if raw_bound.endswith(".*") and op in ("==", "!="):
            prefix = _numeric_version(raw_bound[:-2])
            if prefix is None:
                return False
            same_prefix = target[: len(prefix)] == prefix
            if (op == "==" and not same_prefix) or (op == "!=" and same_prefix):
                return False
            continue
        bound = _numeric_version(raw_bound)
        if bound is None or op == "===":
            return False
        comparison = _compare_numeric_versions(target, bound)
        if op == "==" and comparison != 0:
            return False
        if op == "!=" and comparison == 0:
            return False
        if op == "<=" and comparison > 0:
            return False
        if op == ">=" and comparison < 0:
            return False
        if op == "<" and comparison >= 0:
            return False
        if op == ">" and comparison <= 0:
            return False
        if op == "~=":
            if comparison < 0:
                return False
            prefix_len = max(1, len(bound) - 1)
            if target[:prefix_len] != bound[:prefix_len]:
                return False
    return True


def parse_requirement(spec: str) -> tuple[str, str | None]:
    """Accept the fail-closed subset owned acquisition can resolve."""
    match = _REQUIREMENT_RE.fullmatch(spec.strip())
    if match is None:
        raise ValueError(
            "PCC-PKG-ACQUIRE-REQUIREMENT-UNSUPPORTED: only a package name or "
            "an exact name==version requirement is supported"
        )
    return match.group("name"), match.group("version")


def looks_like_local_spec(spec: str) -> bool:
    return (
        "/" in spec
        or "\\" in spec
        or spec.startswith(".")
        or spec.lower().endswith(_ARTIFACT_SUFFIXES)
    )


def selected_acquire_mode(requested: str) -> str:
    if requested not in ACQUIRE_MODES:
        raise ValueError(
            "PCC-PKG-ACQUIRE-MODE-INVALID: expected auto, host, owned, or offline"
        )
    return "owned" if requested == "auto" else requested


def _artifact_version(filename: str) -> str:
    return distribution_filename_fields(filename)[1]


def _matching_artifacts(
    paths: list[Path], *, name: str, version: str | None, abi: str
) -> list[tuple[Path, str]]:
    expected = _normalized_package_name(name)
    matches: list[tuple[Path, str]] = []
    for path in paths:
        # Owned Simple-API selection operates on published filenames before
        # bytes exist locally; host acquisition passes real staging paths.
        if not path.name.endswith(_ARTIFACT_SUFFIXES):
            continue
        if _normalized_package_name(_artifact_project_name(path)) != expected:
            continue
        if version is not None and _artifact_version(path.name) != version:
            continue
        allowed, reason = _artifact_compatibility_reason_from_name(path.name, abi=abi)
        if allowed:
            matches.append((path, reason))
    return matches


def _best_artifact(
    paths: list[Path], *, name: str, version: str | None, abi: str
) -> Path | None:
    matches = _matching_artifacts(paths, name=name, version=version, abi=abi)
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: _artifact_selection_key(item[0], item[1]),
    )[
        -1
    ][0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_python_candidates(explicit: str | None) -> list[str]:
    configured = explicit or os.environ.get("PCC_HOST_PYTHON")
    if configured:
        return [configured]
    candidates: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    add(sys.executable)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for basename in ("python3", "python"):
            candidate = str(Path(directory) / basename)
            if Path(candidate).is_file():
                add(candidate)
    return candidates


def _archive_pyproject_text(path: Path) -> str:
    """Read an sdist's build contract without executing its build backend."""
    lower = path.name.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.endswith("/pyproject.toml") or name == "pyproject.toml":
                        return zf.read(name).decode("utf-8", errors="replace")
        elif lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
            with tarfile.open(path) as tf:
                for member in tf.getmembers():
                    if member.isfile() and (
                        member.name.endswith("/pyproject.toml")
                        or member.name == "pyproject.toml"
                    ):
                        extracted = tf.extractfile(member)
                        if extracted is not None:
                            return extracted.read().decode("utf-8", errors="replace")
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return ""
    return ""


def owned_shape_diagnostic(path: str | Path) -> tuple[str, list[str]] | None:
    """Reject resolver/build-isolation work the narrow owned backend lacks."""
    artifact = Path(path)
    dependencies = list(artifact_requires_dist(artifact))
    if dependencies:
        return "PCC-PKG-ACQUIRE-DEPENDENCY-RESOLUTION-UNSUPPORTED", dependencies
    pyproject = _archive_pyproject_text(artifact)
    if "[build-system]" in pyproject and "requires" in pyproject:
        return "PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED", []
    return None


def _publish_immutable(
    temp_path: Path, cache_dir: Path, *, filename: str | None = None
) -> tuple[Path, str]:
    digest = _sha256(temp_path)
    dest_dir = cache_dir / "acquired" / "sha256" / digest
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (filename or temp_path.name)
    if dest.exists():
        if _sha256(dest) != digest:
            raise RuntimeError("PCC-PKG-ACQUIRE-CACHE-CORRUPT")
        temp_path.unlink(missing_ok=True)
    else:
        os.replace(temp_path, dest)
    return dest.resolve(), digest


def _base_report(
    spec: str,
    *,
    requested_mode: str,
    mode: str,
    index_urls: list[str],
    target_python: str,
) -> dict[str, object]:
    return {
        "ok": False,
        "requested_spec": spec,
        "acquire_mode_requested": requested_mode,
        "acquire_mode": mode,
        "host_assisted": mode == "host",
        "index_urls": index_urls,
        "target_python": target_python,
        "artifact_origin": None,
        "artifact_url": None,
        "artifact_path": None,
        "resolved_version": None,
        "sha256": None,
        "hash_verified": False,
        "immutable_cache": True,
        "dependency_resolution": "not_attempted",
        "build_isolation": "not_attempted",
    }


def _host_acquire(
    spec: str,
    *,
    requested_mode: str,
    cache_dir: Path,
    index_urls: list[str],
    abi: str,
    host_python: str | None,
    target_python: str,
    timeout: int,
) -> dict[str, object]:
    report = _base_report(
        spec,
        requested_mode=requested_mode,
        mode="host",
        index_urls=index_urls,
        target_python=target_python,
    )
    try:
        name, version = parse_requirement(spec)
    except ValueError as exc:
        report["error"] = str(exc)
        report["diagnostic"] = "PCC-PKG-ACQUIRE-REQUIREMENT-UNSUPPORTED"
        return report

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="host-", dir=cache_dir) as raw_stage:
        stage = Path(raw_stage)
        outputs: list[str] = []
        artifact: Path | None = None
        selected_python: str | None = None
        for python in _host_python_candidates(host_python):
            base = [
                python,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--no-deps",
                "--python-version",
                target_python,
                "--dest",
                str(stage),
            ]
            if index_urls:
                base.extend(["--index-url", index_urls[0]])
                for extra in index_urls[1:]:
                    base.extend(["--extra-index-url", extra])
            command = (
                base + ["--no-binary=:all:", spec]
                if abi == "pcc-native"
                else base + [spec]
            )
            for child in stage.iterdir():
                if child.is_file():
                    child.unlink()
            try:
                proc = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                outputs.append(str(exc))
                continue
            outputs.append(proc.stdout + proc.stderr)
            if proc.returncode != 0:
                continue
            artifact = _best_artifact(
                list(stage.iterdir()), name=name, version=version, abi=abi
            )
            if artifact is not None:
                selected_python = python
                break
        if artifact is None:
            report["error"] = "PCC-PKG-ACQUIRE-HOST-FAILED"
            report["diagnostic"] = "PCC-PKG-ACQUIRE-HOST-FAILED"
            report["host_output_tail"] = "\n".join(outputs)[-2000:]
            return report
        published, digest = _publish_immutable(artifact, cache_dir)

    artifact_url = None
    for match in re.finditer(
        r"(?:Downloading|Using cached)\s+(https?://\S+)", "\n".join(outputs)
    ):
        artifact_url = match.group(1).rstrip("),")
    report.update(
        {
            "ok": True,
            "artifact_origin": "host-pip",
            "artifact_url": artifact_url,
            "artifact_path": str(published),
            "resolved_version": _artifact_version(published.name),
            "sha256": digest,
            "transport_provider": "host-python-pip",
            "host_python": selected_python,
        }
    )
    return report


def _owned_acquire(
    spec: str,
    *,
    requested_mode: str,
    cache_dir: Path,
    index_urls: list[str],
    abi: str,
    target_python: str,
    timeout: int,
) -> dict[str, object]:
    report = _base_report(
        spec,
        requested_mode=requested_mode,
        mode="owned",
        index_urls=index_urls,
        target_python=target_python,
    )
    try:
        name, version = parse_requirement(spec)
    except ValueError as exc:
        report["error"] = str(exc)
        report["diagnostic"] = "PCC-PKG-ACQUIRE-REQUIREMENT-UNSUPPORTED"
        return report

    candidates: list[tuple[str, str, str, str]] = []
    for index_url in index_urls:
        page_url = urllib.parse.urljoin(
            index_url.rstrip("/") + "/", _normalized_package_name(name) + "/"
        )
        try:
            request = urllib.request.Request(
                page_url,
                headers={
                    "Accept": "text/html, application/vnd.pypi.simple.v1+html",
                    "User-Agent": "pcc-owned-acquire/1",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                page = response.read().decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            report["error"] = f"PCC-PKG-ACQUIRE-INDEX-FAILED: {exc}"
            report["diagnostic"] = "PCC-PKG-ACQUIRE-INDEX-FAILED"
            continue
        parser = _SimpleLinks()
        parser.feed(page)
        for href, requires_python in parser.links:
            if not requires_python_allows(requires_python, target_python):
                continue
            artifact_url = urllib.parse.urljoin(page_url, href)
            clean_url, fragment = urllib.parse.urldefrag(artifact_url)
            filename = Path(
                urllib.parse.unquote(urllib.parse.urlparse(clean_url).path)
            ).name
            paths = [Path(filename)]
            match = _best_artifact(paths, name=name, version=version, abi=abi)
            if match is None:
                continue
            expected_hash = urllib.parse.parse_qs(fragment).get("sha256", [""])[0]
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
                continue
            allowed, reason = _artifact_compatibility_reason_from_name(
                filename, abi=abi
            )
            if allowed:
                candidates.append((clean_url, filename, expected_hash.lower(), reason))
    if not candidates:
        report["error"] = (
            "PCC-PKG-ACQUIRE-HASH-REQUIRED: no compatible artifact with a "
            "sha256 fragment was published"
        )
        report["diagnostic"] = "PCC-PKG-ACQUIRE-HASH-REQUIRED"
        return report

    best_url, filename, expected_hash, _reason = sorted(
        candidates,
        key=lambda item: _artifact_selection_key(Path(item[1]), item[3]),
    )[-1]
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix="owned-", suffix="-" + filename, dir=cache_dir
    )
    os.close(fd)
    temp_path = Path(raw_temp)
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(
            best_url, headers={"User-Agent": "pcc-owned-acquire/1"}
        )
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            temp_path.open("wb") as out,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            report["error"] = "PCC-PKG-ACQUIRE-HASH-MISMATCH"
            report["diagnostic"] = "PCC-PKG-ACQUIRE-HASH-MISMATCH"
            report["sha256"] = actual_hash
            return report
        published, published_hash = _publish_immutable(
            temp_path, cache_dir, filename=filename
        )
    except (OSError, ValueError) as exc:
        report["error"] = f"PCC-PKG-ACQUIRE-DOWNLOAD-FAILED: {exc}"
        report["diagnostic"] = "PCC-PKG-ACQUIRE-DOWNLOAD-FAILED"
        return report
    finally:
        temp_path.unlink(missing_ok=True)

    report.update(
        {
            "ok": True,
            "artifact_origin": "simple-repository",
            "artifact_url": best_url,
            "artifact_path": str(published),
            "resolved_version": _artifact_version(published.name),
            "sha256": published_hash,
            "hash_verified": True,
            "transport_provider": "python-stdlib-urllib",
        }
    )
    unsupported = owned_shape_diagnostic(published)
    if unsupported is not None:
        diagnostic, dependencies = unsupported
        if (
            requested_mode == "auto"
            and diagnostic == "PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED"
        ):
            # Acquisition only selected and verified the immutable source.
            # The pcc-native installer owns the subsequent supported-source
            # build and will fail closed if it cannot satisfy that contract.
            report["build_isolation"] = "delegated-to-pcc-native-builder"
        else:
            report["ok"] = False
            report["diagnostic"] = diagnostic
            report["error"] = diagnostic
            if dependencies:
                report["dependencies"] = dependencies
    return report


def acquire_requirement(
    spec: str,
    *,
    mode: str = "auto",
    cache_dir: str | Path | None = None,
    index_urls: list[str] | tuple[str, ...] = (),
    abi: str = "pcc-native",
    host_python: str | None = None,
    target_python: str | None = None,
    timeout: int = 180,
) -> dict[str, object]:
    """Acquire one requirement into the content-addressed artifact cache."""
    try:
        selected = selected_acquire_mode(mode)
    except ValueError as exc:
        return {
            "ok": False,
            "requested_spec": spec,
            "acquire_mode_requested": mode,
            "acquire_mode": None,
            "error": str(exc),
            "diagnostic": "PCC-PKG-ACQUIRE-MODE-INVALID",
        }
    try:
        target = target_python_version(target_python)
    except ValueError as exc:
        return {
            "ok": False,
            "requested_spec": spec,
            "acquire_mode_requested": mode,
            "acquire_mode": selected,
            "error": str(exc),
            "diagnostic": "PCC-PKG-ACQUIRE-TARGET-PYTHON-INVALID",
        }
    indexes = list(index_urls) or [DEFAULT_INDEX_URL]
    cache = (
        Path(cache_dir).expanduser().resolve()
        if cache_dir is not None
        else Path.home() / ".cache" / "pcc" / "package-cache"
    )
    if selected == "offline":
        report = _base_report(
            spec,
            requested_mode=mode,
            mode=selected,
            index_urls=[],
            target_python=target,
        )
        report["error"] = "PCC-PKG-ACQUIRE-OFFLINE: package is not available locally"
        report["diagnostic"] = "PCC-PKG-ACQUIRE-OFFLINE"
        return report
    if selected == "host":
        return _host_acquire(
            spec,
            requested_mode=mode,
            cache_dir=cache,
            index_urls=indexes,
            abi=abi,
            host_python=host_python,
            target_python=target,
            timeout=timeout,
        )
    return _owned_acquire(
        spec,
        requested_mode=mode,
        cache_dir=cache,
        index_urls=indexes,
        abi=abi,
        target_python=target,
        timeout=timeout,
    )
