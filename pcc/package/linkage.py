"""Generic native artifact linkage scanner for pcc package builds.

The scanner enforces the pcc-native no-libpython claim at the package boundary:
link commands and produced native artifacts must not mention libpython or
Python.framework unless the package is explicitly installed in libpython mode.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tarfile
import zipfile
from pathlib import Path

from pcc.package.schema import capability_profile

_LIBPYTHON_PATTERNS = (
    re.compile(r"(?:^|[\s/:=,-])libpython\d+(?:\.\d+)*", re.IGNORECASE),
    re.compile(r"(?:^|\s)-lpython\d*(?:\.\d+)*", re.IGNORECASE),
    re.compile(r"(?:^|[\s/:=,-])Python\.framework(?:[/\s:=,-]|$)"),
    re.compile(r"python\d+(?:\.\d+)*\.dll", re.IGNORECASE),
)
_RUNTIME_NATIVE_SUFFIXES = (".so", ".dylib", ".pyd", ".dll")
_STATIC_ARCHIVE_SUFFIXES = (".a",)
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")
_CPYTHON_EXTENSION_ABI_RE = re.compile(
    r"(?:^|[./\\_-])(?:cpython-\d+|cp\d+-cp\d+|abi3)(?:[._-]|$)",
    re.IGNORECASE,
)


def _decode_probe(data: bytes) -> str:
    # The libpython patterns are ASCII, and pcc's native regex subset rejects
    # other text. A non-ASCII character becomes a space when it is whitespace
    # and an inert control character otherwise, so every match boundary of
    # the decoded artifact is unchanged.
    text = data.decode("utf-8", errors="ignore")
    if text.isascii():
        return text
    return "".join(
        character if character.isascii() else (" " if character.isspace() else "\x01")
        for character in text
    )


def _libpython_edges(text: str) -> tuple[str, ...]:
    edges: list[str] = []
    for pattern in _LIBPYTHON_PATTERNS:
        for match in pattern.finditer(text):
            edge = match.group(0).strip()
            if edge:
                edges.append(edge)
    return tuple(dict.fromkeys(edges))


def _diagnostics_for_edges(
    edges: tuple[str, ...], *, message: str, path: str
) -> list[dict[str, object]]:
    return [
        {
            "code": "PCC-PKG-003",
            "message": message,
            "edge": edge,
            "path": path,
        }
        for edge in edges
    ]


def _uses_cpython_extension_abi(path: str) -> bool:
    name = str(path or "")
    return _CPYTHON_EXTENSION_ABI_RE.search(name) is not None


def _diagnostic_for_cpython_extension_abi(path: str) -> dict[str, object]:
    return {
        "code": "PCC-PKG-004",
        "message": (
            "native artifact name declares a CPython extension ABI; "
            "pcc-native mode requires a pcc-native extension ABI or a source rebuild"
        ),
        "path": path,
    }


def scan_link_command(command: str) -> dict[str, object]:
    edges = _libpython_edges(command)
    return {
        "kind": "link_command",
        "path": None,
        "links_libpython": bool(edges),
        "link_libpython_edges": list(edges),
        "diagnostics": _diagnostics_for_edges(
            edges,
            message="link command mentions libpython under pcc-native mode",
            path="<link-command>",
        ),
    }


def _archive_member_scans(
    source: Path, *, max_bytes: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    lower = source.name.lower()
    scans: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    try:
        if lower.endswith(".whl") or lower.endswith(".zip"):
            with zipfile.ZipFile(source) as zf:
                for info in zf.infolist():
                    name = info.filename
                    if not name.lower().endswith(_RUNTIME_NATIVE_SUFFIXES):
                        continue
                    data = zf.read(info)[:max_bytes]
                    member_path = f"{source}!{name}"
                    edges = _libpython_edges(_decode_probe(data))
                    uses_cpython_abi = _uses_cpython_extension_abi(name)
                    member_diagnostics = _diagnostics_for_edges(
                        edges,
                        message="native artifact inside archive mentions libpython under pcc-native mode",
                        path=member_path,
                    )
                    if uses_cpython_abi:
                        member_diagnostics.append(
                            _diagnostic_for_cpython_extension_abi(member_path)
                        )
                    scans.append(
                        {
                            "kind": "archive_member",
                            "path": member_path,
                            "links_libpython": bool(edges),
                            "uses_cpython_extension_abi": uses_cpython_abi,
                            "link_libpython_edges": list(edges),
                            "diagnostics": member_diagnostics,
                        }
                    )
                    diagnostics.extend(member_diagnostics)
        elif lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
            with tarfile.open(source) as tf:
                for member in tf.getmembers():
                    if not member.isfile() or not member.name.lower().endswith(
                        _RUNTIME_NATIVE_SUFFIXES
                    ):
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    data = extracted.read(max_bytes)
                    member_path = f"{source}!{member.name}"
                    edges = _libpython_edges(_decode_probe(data))
                    uses_cpython_abi = _uses_cpython_extension_abi(member.name)
                    member_diagnostics = _diagnostics_for_edges(
                        edges,
                        message="native artifact inside archive mentions libpython under pcc-native mode",
                        path=member_path,
                    )
                    if uses_cpython_abi:
                        member_diagnostics.append(
                            _diagnostic_for_cpython_extension_abi(member_path)
                        )
                    scans.append(
                        {
                            "kind": "archive_member",
                            "path": member_path,
                            "links_libpython": bool(edges),
                            "uses_cpython_extension_abi": uses_cpython_abi,
                            "link_libpython_edges": list(edges),
                            "diagnostics": member_diagnostics,
                        }
                    )
                    diagnostics.extend(member_diagnostics)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as exc:
        diagnostics.append(
            {
                "code": "PCC-PKG-LINKAGE-ARCHIVE-SCAN-FAILED",
                "message": str(exc),
                "path": str(source),
            }
        )
    return scans, diagnostics


def scan_artifact(path: str | Path, *, max_bytes: int = 2_000_000) -> dict[str, object]:
    source = Path(path).expanduser().resolve()
    if source.name.lower().endswith(_STATIC_ARCHIVE_SUFFIXES):
        return {
            "kind": "static_archive",
            "path": str(source),
            "links_libpython": False,
            "link_libpython_edges": [],
            "archive_scans": [],
            "diagnostics": [],
        }
    try:
        data = source.read_bytes()[:max_bytes]
    except OSError as exc:
        return {
            "kind": "artifact",
            "path": str(source),
            "links_libpython": False,
            "link_libpython_edges": [],
            "diagnostics": [
                {
                    "code": "PCC-PKG-LINKAGE-READ-FAILED",
                    "message": str(exc),
                }
            ],
        }
    outer_edges = _libpython_edges(_decode_probe(data))
    edges = outer_edges
    uses_cpython_abi = _uses_cpython_extension_abi(source.name)
    archive_scans: list[dict[str, object]] = []
    archive_diagnostics: list[dict[str, object]] = []
    archive_uses_cpython_abi = False
    if source.name.lower().endswith(_ARCHIVE_SUFFIXES):
        archive_scans, archive_diagnostics = _archive_member_scans(
            source, max_bytes=max_bytes
        )
        for member_scan in archive_scans:
            edges = tuple(
                dict.fromkeys(
                    list(edges)
                    + [
                        str(edge)
                        for edge in member_scan.get("link_libpython_edges", [])
                    ]
                )
            )
            if bool(member_scan.get("uses_cpython_extension_abi")):
                archive_uses_cpython_abi = True
    diagnostics = _diagnostics_for_edges(
        outer_edges,
        message="native artifact mentions libpython under pcc-native mode",
        path=str(source),
    )
    if uses_cpython_abi:
        diagnostics.append(_diagnostic_for_cpython_extension_abi(str(source)))
    diagnostics.extend(archive_diagnostics)
    return {
        "kind": "artifact",
        "path": str(source),
        "links_libpython": bool(edges),
        "uses_cpython_extension_abi": uses_cpython_abi or archive_uses_cpython_abi,
        "link_libpython_edges": list(edges),
        "archive_scans": archive_scans,
        "diagnostics": diagnostics,
    }


def _iter_native_artifacts(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return (root,) if root.name.lower().endswith(_RUNTIME_NATIVE_SUFFIXES) else ()
    if not root.exists():
        return ()
    return tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name.lower().endswith(_RUNTIME_NATIVE_SUFFIXES)
    )


def linkage_report(
    *,
    artifacts: tuple[str, ...] | list[str] = (),
    roots: tuple[str, ...] | list[str] = (),
    commands: tuple[str, ...] | list[str] = (),
    abi_mode: str = "pcc-native",
) -> dict[str, object]:
    scans: list[dict[str, object]] = []
    for command in commands:
        scans.append(scan_link_command(str(command)))
    for artifact in artifacts:
        scans.append(scan_artifact(artifact))
    for root_text in roots:
        for artifact in _iter_native_artifacts(Path(root_text).expanduser()):
            scans.append(scan_artifact(artifact))
    edges: list[str] = []
    diagnostics: list[dict[str, object]] = []
    cpython_abi_paths: list[str] = []
    for scan in scans:
        edges.extend(str(edge) for edge in scan.get("link_libpython_edges", []))
        diagnostics.extend(scan.get("diagnostics", []))  # type: ignore[arg-type]
        if bool(scan.get("uses_cpython_extension_abi")):
            path = scan.get("path")
            if path is not None:
                cpython_abi_paths.append(str(path))
    links_libpython = bool(edges)
    uses_cpython_extension_abi = bool(cpython_abi_paths)
    has_scans = bool(scans)
    no_libpython_runtime = (
        not links_libpython
        and not uses_cpython_extension_abi
        and abi_mode == "pcc-native"
    )
    abi_allows_cpython_extension = abi_mode in ("libpython", "cpython-compat")
    # PKG-P0-ABI-MODE-LABELS: every import/linkage result carries explicit
    # execution-mode labels so an A-mode (libpython / cpython-compat)
    # compatibility SUCCESS can never silently promote to a B-mode
    # (no-libpython / pcc-native) package claim. These labels are additive;
    # existing keys (abi_mode, links_libpython, diagnostics, ...) are untouched.
    # Mapping (generic, from the abi mode -- no package name special-cases):
    #   abi_mode in (libpython, cpython-compat) -> execution_mode=cpython-compat,
    #       native_package_claim=False (a compat import never claims native).
    #   abi_mode == pcc-native -> execution_mode=pcc-native; native_package_claim
    #       is True only when the artifact actually passed the pcc-native gate
    #       with NO cpython-abi / libpython edge (i.e. no PCC-PKG-003/004 firing).
    execution_mode = "cpython-compat" if abi_allows_cpython_extension else "pcc-native"
    native_package_claim = (
        execution_mode == "pcc-native"
        and has_scans
        and not links_libpython
        and not uses_cpython_extension_abi
    )
    profile = capability_profile(
        abi_mode, has_scans, links_libpython, uses_cpython_extension_abi
    )
    for scan in scans:
        scan_links_libpython = bool(scan.get("links_libpython"))
        scan_uses_cpython_abi = bool(scan.get("uses_cpython_extension_abi"))
        scan["execution_mode"] = execution_mode
        scan["native_package_claim"] = (
            execution_mode == "pcc-native"
            and not scan_links_libpython
            and not scan_uses_cpython_abi
        )
    return {
        "ok": (
            (not links_libpython or abi_mode == "libpython")
            and (not uses_cpython_extension_abi or abi_allows_cpython_extension)
        ),
        "abi_mode": abi_mode,
        "execution_mode": execution_mode,
        "links_libpython": links_libpython,
        "native_package_claim": native_package_claim,
        "capability_profile": profile,
        "uses_cpython_extension_abi": uses_cpython_extension_abi,
        "cpython_extension_abi_paths": list(dict.fromkeys(cpython_abi_paths)),
        "no_libpython_runtime": no_libpython_runtime,
        "link_libpython_edges": list(dict.fromkeys(edges)),
        "scans": scans,
        "diagnostics": diagnostics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package linkage")
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--command", action="append", default=[])
    parser.add_argument("--abi", dest="abi_mode", default="pcc-native")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    report = linkage_report(
        artifacts=ns.artifact,
        roots=ns.root,
        commands=ns.command,
        abi_mode=ns.abi_mode,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
