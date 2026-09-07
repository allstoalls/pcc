"""Read native artifacts without executing them or invoking platform tools.

The public API is usable from CPython. Reports describe declared load
dependencies, not transitive dependencies or the tools that built the file.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


class ArtifactInspectionError(ValueError):
    """An artifact is malformed or outside the supported format boundary."""


def _command_string(raw: bytes, minimum: int) -> str:
    if len(raw) < minimum:
        raise ArtifactInspectionError("truncated Mach-O string command")
    offset = struct.unpack_from("<I", raw, 8)[0]
    if offset < minimum or offset >= len(raw):
        raise ArtifactInspectionError("Mach-O string offset is outside its command")
    end = raw.find(b"\0", offset)
    if end < 0 or end == offset:
        raise ArtifactInspectionError("Mach-O command string is empty or unterminated")
    return raw[offset:end].decode("utf-8", "strict")


def _inspect_macho(data: bytes) -> dict:
    from pcc.backend import macho_spec as spec

    if len(data) < spec.MACH_HEADER_64.size:
        raise ArtifactInspectionError("truncated Mach-O header")
    header = spec.MACH_HEADER_64.unpack(data)
    end = spec.MACH_HEADER_64.size + header["sizeofcmds"]
    if end > len(data) or header["ncmds"] > header["sizeofcmds"] // 8:
        raise ArtifactInspectionError("Mach-O load-command table is outside the file")
    # Validate command-local bounds before the shared parser materializes
    # variable-length section/tool arrays.
    offset = spec.MACH_HEADER_64.size
    for _ in range(header["ncmds"]):
        if offset + 8 > end:
            raise ArtifactInspectionError("truncated Mach-O load command")
        cmd, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 8 or size > end - offset:
            raise ArtifactInspectionError("invalid Mach-O load-command size")
        if cmd == spec.LC_SEGMENT_64:
            minimum = spec.SEGMENT_COMMAND_64.size
            if size < minimum:
                raise ArtifactInspectionError("truncated Mach-O segment command")
            segment = spec.SEGMENT_COMMAND_64.unpack(data, offset)
            if segment["nsects"] > (size - minimum) // spec.SECTION_64.size:
                raise ArtifactInspectionError("Mach-O sections exceed their command")
        elif cmd == spec.LC_BUILD_VERSION:
            minimum = spec.BUILD_VERSION_COMMAND.size
            if size < minimum:
                raise ArtifactInspectionError("truncated Mach-O build-version command")
            version = spec.BUILD_VERSION_COMMAND.unpack(data, offset)
            if version["ntools"] > (size - minimum) // spec.BUILD_TOOL_VERSION.size:
                raise ArtifactInspectionError("Mach-O build tools exceed their command")
        elif cmd == spec.LC_SYMTAB and size != spec.SYMTAB_COMMAND.size:
            raise ArtifactInspectionError("invalid Mach-O symbol-table command")
        elif cmd == spec.LC_DYSYMTAB and size != spec.DYSYMTAB_COMMAND.size:
            raise ArtifactInspectionError("invalid Mach-O dynamic-symbol-table command")
        offset += size
    if offset != end:
        raise ArtifactInspectionError("Mach-O command count and sizeofcmds disagree")

    try:
        obj = spec.parse_object(data)
    except spec.MachOFormatError as exc:
        raise ArtifactInspectionError(str(exc)) from exc
    architecture = {
        spec.CPU_TYPE_ARM64: "aarch64",
        spec.CPU_TYPE_X86_64: "x86_64",
    }.get(header["cputype"])
    kind = {
        spec.MH_OBJECT: "object",
        spec.MH_EXECUTE: "executable",
        spec.MH_DYLIB: "shared-library",
        8: "bundle",
    }.get(header["filetype"])
    if architecture is None or kind is None:
        raise ArtifactInspectionError("unsupported Mach-O CPU or file type")
    load_kinds = {
        spec.LC_LOAD_DYLIB: "required",
        0x80000018: "weak",
        0x8000001F: "reexport",
        0x20: "lazy",
        0x80000023: "upward",
    }
    dependencies = []
    rpaths = []
    interpreter = None
    for command in obj.commands:
        if command.cmd in load_kinds:
            dependencies.append(
                {
                    "name": _command_string(command.raw, 24),
                    "kind": load_kinds[command.cmd],
                }
            )
        elif command.cmd == spec.LC_LOAD_DYLINKER:
            interpreter = _command_string(command.raw, 12)
        elif command.cmd == 0x8000001C:  # LC_RPATH
            rpaths.append(_command_string(command.raw, 12))
    return {
        "format": "mach-o",
        "bits": 64,
        "endianness": "little",
        "architecture": architecture,
        "kind": kind,
        "declared_dependencies": dependencies,
        "rpaths": rpaths,
        "interpreter": interpreter,
    }


def _inspect_elf(data: bytes) -> dict:
    from pcc.backend import elf_x86_64 as elf

    if len(data) < 64:
        raise ArtifactInspectionError("truncated ELF header")
    file_type = struct.unpack_from("<H", data, 16)[0]
    try:
        if file_type == elf.ET_REL:
            elf.parse_relocatable(data)
            kind = "object"
        elif file_type == elf.ET_EXEC:
            elf.parse_static_executable(data)
            kind = "executable"
        else:
            raise ArtifactInspectionError(
                "ELF inspection currently supports pcc ELF64 x86_64 objects and "
                "static executables; dynamic ELF/PIE is not yet supported"
            )
    except elf.ElfError as exc:
        raise ArtifactInspectionError(str(exc)) from exc
    return {
        "format": "elf",
        "bits": 64,
        "endianness": "little",
        "architecture": "x86_64",
        "kind": kind,
        "declared_dependencies": [],
        "rpaths": [],
        "interpreter": None,
    }


def inspect_artifact(path: str | Path) -> dict:
    """Inspect a supported artifact; no subprocess, dlopen or execution.

    No dependencies in an object file do not establish the dependencies of a
    future executable. Likewise, no libpython load command does not prove
    absence of runtime dlopen or host-assisted build steps.
    """
    source = Path(path)
    data = source.read_bytes()
    try:
        if data[:4] == b"\xcf\xfa\xed\xfe":
            report = _inspect_macho(data)
        elif data[:4] == b"\x7fELF":
            report = _inspect_elf(data)
        else:
            raise ArtifactInspectionError(
                "unsupported artifact: expected thin little-endian Mach-O64 "
                "or a pcc ELF64 x86_64 artifact"
            )
    except (struct.error, UnicodeError) as exc:
        raise ArtifactInspectionError("malformed artifact: " + str(exc)) from exc
    report["schema"] = "pcc.artifact-inspection.v1"
    report["path"] = str(source)
    report["size_bytes"] = len(data)
    report["dependency_scope"] = "declared-load-commands-only"
    report["build_provenance"] = "unknown"
    report["runtime_dynamic_loading"] = "not-inspected"
    report["inspected_artifact_executed"] = False
    return report


def format_report(report: dict) -> str:
    lines = [
        "pcc artifact inspection",
        "  path: " + report["path"],
        "  format: " + report["format"],
        "  architecture: " + report["architecture"],
        "  kind: " + report["kind"],
        "  size: " + str(report["size_bytes"]) + " bytes",
        "  declared dependencies:",
    ]
    for dependency in report["declared_dependencies"]:
        lines.append("    " + dependency["name"] + " (" + dependency["kind"] + ")")
    if not report["declared_dependencies"]:
        lines.append("    none declared in this artifact")
    if report["interpreter"]:
        lines.append("  loader: " + report["interpreter"])
    for rpath in report["rpaths"]:
        lines.append("  rpath: " + rpath)
    lines.append("  build provenance: unknown (no matching receipt supplied)")
    lines.append("  transitive dependencies and runtime dlopen: not inspected")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pcc inspect", description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="write versioned JSON")
    args = parser.parse_args(argv)
    try:
        report = inspect_artifact(args.path)
    except (OSError, ValueError) as exc:
        print("PCC-INSPECT-001: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
