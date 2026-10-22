#!/usr/bin/env python3
"""No-libpython ratchet: build the runtime archive, link a C smoke, inspect it.

Python port of ``scripts/verify_nolibpython.sh``.  The linkage inspection is
now parsed from the artifact itself (ELF dynamic section, Mach-O load
commands, PE import table) instead of shelling out to ``ldd``/``readelf``/``nm``,
so the same gate runs on macOS, Linux and Windows.

Usage: scripts/verify_nolibpython.py [--keep]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "pcc" / "runtime"
ARCHIVE = RUNTIME / "libpy_runtime_pcc_py.a"

_SMOKE_C = """#include "py_runtime.h"

int main(void) {
    PyObject *x = py_int_from_i64(123);
    if (x == 0) return 10;
    py_print(x);
    py_decref(x);
    if (pcc_threads_enabled() < 0) return 11;
    if (pcc_refcount_strategy() < 0) return 12;
    if (pcc_stop_the_world() != 0) return 13;
    if (pcc_resume_world() != 0) return 14;
    return 0;
}
"""


def _elf_needed_and_undefined(data: bytes) -> tuple[list[str], list[str]]:
    if len(data) < 0x40 or data[:4] != b"\x7fELF":
        return [], []
    elf_class, endian_flag = data[4], data[5]
    endian = "<" if endian_flag == 1 else ">"
    if elf_class == 2:
        shoff = struct.unpack_from(endian + "Q", data, 0x28)[0]
        shentsize = struct.unpack_from(endian + "H", data, 0x3A)[0]
        shnum = struct.unpack_from(endian + "H", data, 0x3C)[0]
        word, sym_size = endian + "Q", 24
    else:
        shoff = struct.unpack_from(endian + "I", data, 0x20)[0]
        shentsize = struct.unpack_from(endian + "H", data, 0x2E)[0]
        shnum = struct.unpack_from(endian + "H", data, 0x30)[0]
        word, sym_size = endian + "I", 16
    sections = []
    for index in range(shnum):
        header = shoff + index * shentsize
        if header + shentsize > len(data):
            break
        sh_type = struct.unpack_from(endian + "I", data, header + 4)[0]
        offset = struct.unpack_from(word, data, header + 24)[0]
        size = struct.unpack_from(word, data, header + 32)[0]
        link = struct.unpack_from(endian + "I", data, header + 40)[0]
        entsize = struct.unpack_from(word, data, header + 56)[0]
        sections.append((sh_type, offset, size, link, entsize))
    # SHT_DYNAMIC(6) for DT_NEEDED(1), SHT_DYNSYM(11) for undefined symbols.
    strtabs = {index: (off, size) for index, (_t, off, size, _l, _e) in enumerate(sections)}
    needed: list[str] = []
    undefined: list[str] = []
    for sh_type, offset, size, link, entsize in sections:
        if sh_type == 6:
            str_off, str_size = strtabs.get(link, (0, 0))
            for entry in range(offset, offset + size, 16 if elf_class == 2 else 8):
                if entry + (16 if elf_class == 2 else 8) > len(data):
                    break
                tag = struct.unpack_from(word, data, entry)[0]
                value = struct.unpack_from(word, data, entry + (8 if elf_class == 2 else 4))[0]
                if tag == 0:
                    break
                if tag == 1 and str_off <= value < str_off + str_size:
                    end = data.find(b"\0", value)
                    needed.append(data[value:end].decode("utf-8", "replace"))
        elif sh_type == 11 and entsize:
            str_off, str_size = strtabs.get(link, (0, 0))
            for entry in range(offset, offset + size, entsize):
                if entry + sym_size > len(data):
                    break
                name_at = struct.unpack_from(endian + "I", data, entry)[0]
                shndx = struct.unpack_from(endian + "H", data, entry + 6)[0]
                if shndx != 0 or not (str_off <= name_at < str_off + str_size):
                    continue
                end = data.find(b"\0", name_at)
                undefined.append(data[name_at:end].decode("utf-8", "replace"))
    return needed, undefined


def _macho_dylibs_and_undefined(data: bytes) -> tuple[list[str], list[str]]:
    if len(data) < 32 or data[:4] not in (
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
    ):
        return [], []
    endian = "<" if data[:4] == b"\xcf\xfa\xed\xfe" else "<"
    ncmds = struct.unpack_from(endian + "I", data, 16)[0]
    header_size = 32 if data[:4] == b"\xcf\xfa\xed\xfe" else 28
    cursor = header_size
    dylibs: list[str] = []
    undefined: list[str] = []
    for _ in range(ncmds):
        if cursor + 8 > len(data):
            break
        cmd, cmdsize = struct.unpack_from(endian + "II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > len(data):
            break
        if cmd in (0xC, 0x18, 0x1F, 0x23):  # LOAD_DYLIB / WEAK / REEXPORT / LAZY
            name_off = struct.unpack_from(endian + "I", data, cursor + 8)[0]
            end = data.find(b"\0", cursor + name_off)
            if end > 0:
                dylibs.append(data[cursor + name_off : end].decode("utf-8", "replace"))
        elif cmd == 0x2:  # LC_SYMTAB
            symoff, nsyms, stroff, strsize = struct.unpack_from(
                endian + "IIII", data, cursor + 8
            )
            for index in range(nsyms):
                entry = symoff + index * 16
                if entry + 16 > len(data):
                    break
                strx = struct.unpack_from(endian + "I", data, entry)[0]
                n_type = data[entry + 4]
                if n_type & 0x0E or not (stroff <= strx < stroff + strsize):
                    continue
                end = data.find(b"\0", strx)
                undefined.append(data[strx:end].decode("utf-8", "replace"))
        cursor += cmdsize
    return dylibs, undefined


def _pe_imports(data: bytes) -> list[str]:
    if not data.startswith(b"MZ") or len(data) < 64:
        return []
    pe = struct.unpack_from("<I", data, 60)[0]
    if pe + 24 > len(data) or data[pe : pe + 4] != b"PE\0\0":
        return []
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    ranges = []
    for index in range(count):
        section = optional + optional_size + index * 40
        if section + 40 > len(data):
            break
        _virtual_size, rva, raw_size, raw = struct.unpack_from(
            "<IIII", data, section + 8
        )
        ranges.append((rva, raw_size, raw))

    def offset(rva: int) -> int:
        for start, size, raw in ranges:
            if start <= rva < start + size:
                return raw + rva - start
        raise ValueError("PE RVA outside file-backed sections")

    import_rva, import_size = struct.unpack_from("<II", data, optional + 112 + 8)
    libraries: list[str] = []
    if import_rva:
        for index in range(import_size // 20):
            at = offset(import_rva + index * 20)
            descriptor = struct.unpack_from("<IIIII", data, at)
            if descriptor == (0, 0, 0, 0, 0):
                break
            name_at = offset(descriptor[3])
            end = data.find(b"\0", name_at)
            libraries.append(data[name_at:end].decode("ascii", "replace").lower())
    return libraries


def linked_libraries(data: bytes) -> list[str]:
    if data[:4] == b"\x7fELF":
        return _elf_needed_and_undefined(data)[0]
    if data[:2] == b"MZ":
        return _pe_imports(data)
    return _macho_dylibs_and_undefined(data)[0]


def undefined_symbols(data: bytes) -> list[str]:
    if data[:4] == b"\x7fELF":
        return _elf_needed_and_undefined(data)[1]
    if data[:2] == b"MZ":
        return []
    return _macho_dylibs_and_undefined(data)[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the work dir")
    args = parser.parse_args(argv)
    cc = os.environ.get("CC") or shutil.which("cc")
    if not cc:
        print("verify_nolibpython: a C compiler is required (set CC)", file=sys.stderr)
        return 127
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    make = shutil.which("make")
    if not make:
        print("verify_nolibpython: make is required", file=sys.stderr)
        return 127
    built = subprocess.run(
        [make, "-C", str(RUNTIME), f"PCC={os.environ.get('PCC', 'pcc')}",
         f"PYTHON={os.environ.get('PYTHON', sys.executable)}",
         "libpy_runtime_pcc_py.a"],
        cwd=str(ROOT),
        env=environment,
        check=False,
    )
    if built.returncode != 0:
        return built.returncode

    work = Path(tempfile.mkdtemp(prefix="pcc-nolibpython."))
    try:
        source = work / "nolibpython_smoke.c"
        binary = work / "nolibpython_smoke"
        source.write_text(_SMOKE_C, encoding="utf-8")
        compile_command = [
            cc,
            "-std=c11",
            f"-I{RUNTIME / 'include'}",
            str(source),
            str(ARCHIVE),
            "-o",
            str(binary),
        ]
        if os.name != "nt":
            compile_command += ["-lm", "-ldl", "-lpthread"]
        linked = subprocess.run(compile_command, check=False)
        if linked.returncode != 0:
            return linked.returncode
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, check=False, timeout=120
        )
        if ran.stdout.strip() != "123":
            print(f"unexpected smoke output: {ran.stdout!r}", file=sys.stderr)
            return 20
        data = binary.read_bytes()
        libraries = linked_libraries(data)
        python_libraries = [name for name in libraries if "python" in name.lower()]
        if python_libraries:
            print(
                "binary links libpython according to its load commands: "
                + repr(python_libraries),
                file=sys.stderr,
            )
            return 21
        python_symbols = [
            name
            for name in undefined_symbols(data)
            if name.lstrip("_").startswith("Py") or name.lstrip("_").startswith("_Py")
        ]
        if python_symbols:
            print(
                "binary imports CPython C-API symbols: " + repr(sorted(python_symbols)[:8]),
                file=sys.stderr,
            )
            return 23
        print(
            "no-libpython smoke passed: "
            + str(binary)
            + " (libraries: " + (", ".join(libraries) or "none") + ")"
        )
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
