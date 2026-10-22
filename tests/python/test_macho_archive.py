"""Archive reading and member selection, checked against `ar` and `nm`.

Two properties matter and neither is structural:

1. **Only what is needed gets pulled.** An archive member nobody references
   must not enter the link — otherwise every binary carries the whole runtime.
2. **The scan repeats.** A member pulled late can reference a symbol defined
   by a member *earlier* in the archive; a single forward pass walks past it
   and leaves the symbol undefined. The archive below is built so that a
   one-pass linker fails and a repeated-scan one succeeds.

The real runtime archive (`libpy_runtime_pcc_py.a`, ~2.9MB, BSD extended
names, `__.SYMDEF SORTED` index) is parsed too, so the format handling is not
only exercised on toys.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pcc.backend import macho_obj
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_archive import ArchiveError, read_archive, select_members

_CC = shutil.which(os.environ.get("CC", "cc"))
_AR = shutil.which("ar")
_IS_ARM64_DARWIN = os.uname().sysname == "Darwin" and os.uname().machine == "arm64"
_GATE = None if (_CC and _AR and _IS_ARM64_DARWIN) else "needs cc/ar on Darwin arm64"

pytestmark = pytest.mark.pcc_gate(unavailable=_GATE)


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


def _unit(name: str, calls: str | None = None) -> str:
    body = [
        ".section __TEXT,__text,regular,pure_instructions\n",
        f".p2align 2\n.globl _{name}\n_{name}:\n",
        "  paciasp\n  stp x29, x30, [sp, #-16]!\n  mov x29, sp\n",
    ]
    if calls:
        body.append(f"  bl _{calls}\n")
    body.append("  movz w0, #1\n")
    body.append("  ldp x29, x30, [sp], #16\n  autiasp\n  ret\n")
    body.append(".subsections_via_symbols\n")
    return "".join(body)


def _object(unit: str) -> bytes:
    sections, undefined = assemble_file(unit)
    return macho_obj.emit_object(sections, undefined=undefined)


def _archive(tmp_path: Path, members: list[tuple[str, bytes]]) -> bytes:
    paths = []
    for name, data in members:
        path = tmp_path / name
        path.write_bytes(data)
        paths.append(str(path))
    out = tmp_path / "lib.a"
    run = subprocess.run(
        [_AR, "rcs", str(out)] + paths,
        capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    return out.read_bytes()


# `early` defines _early; `late` defines _late and CALLS _early. An archive
# ordered [early, late] with only _late initially undefined forces a second
# scan: the first pass pulls `late` (which needs _early), and _early's member
# is behind it in the file.
def _ordered_archive(tmp_path: Path) -> bytes:
    return _archive(tmp_path, [
        ("early.o", _object(_unit("early"))),
        ("late.o", _object(_unit("late", calls="early"))),
        ("unused.o", _object(_unit("unused"))),
    ])


def test_reads_members_and_their_symbols(tmp_path):
    members = read_archive(_ordered_archive(tmp_path))
    by_name = {m.name: m for m in members}
    assert set(by_name) == {"early.o", "late.o", "unused.o"}, sorted(by_name)
    assert "_early" in by_name["early.o"].defines
    assert "_late" in by_name["late.o"].defines
    assert "_early" in by_name["late.o"].undefined


def test_only_needed_members_are_pulled(tmp_path):
    members = read_archive(_ordered_archive(tmp_path))
    pulled, pending = select_members(members, {"_late"})
    assert pending == set(), pending
    names = [m.name for m in members if m.data in pulled]
    assert set(names) == {"early.o", "late.o"}, names
    assert "unused.o" not in names, "an unreferenced member entered the link"


def test_the_scan_repeats(tmp_path):
    """A single forward pass leaves _early undefined; this must not."""
    members = read_archive(_ordered_archive(tmp_path))

    # One forward pass, for contrast: it pulls `late`, discovers _early, and
    # has already walked past `early.o`.
    pending_one_pass = {"_late"}
    provided = set()
    for member in members:
        if member.defines & pending_one_pass:
            provided |= member.defines
            pending_one_pass -= member.defines
            pending_one_pass |= member.undefined - provided
    assert "_early" in pending_one_pass, (
        "the fixture no longer distinguishes one-pass from repeated scanning"
    )

    _pulled, pending = select_members(members, {"_late"})
    assert pending == set(), pending


def test_nothing_is_pulled_for_an_empty_request(tmp_path):
    members = read_archive(_ordered_archive(tmp_path))
    pulled, pending = select_members(members, set())
    assert pulled == [] and pending == set()


def test_unsatisfiable_symbols_are_reported_not_swallowed(tmp_path):
    members = read_archive(_ordered_archive(tmp_path))
    _pulled, pending = select_members(members, {"_late", "_nowhere"})
    assert pending == {"_nowhere"}, pending


def test_parses_the_real_runtime_archive():
    """~2.9MB, BSD extended names, __.SYMDEF SORTED index."""
    archive = _repo_root() / "pcc" / "runtime" / "libpy_runtime_pcc_py.a"
    if not archive.exists():
        raise AssertionError(
            f"{archive} missing; build it with "
            "`make -C pcc/runtime libpy_runtime_pcc_py.a`"
        )
    members = read_archive(archive.read_bytes())
    assert len(members) > 50, len(members)
    # The index member must not appear as an object.
    assert not any(m.name.startswith("__.SYMDEF") for m in members)
    # Extended names survive.
    assert any(m.name.endswith(".o") for m in members)
    # Pulling one well-known runtime entry point drags in its dependencies
    # and leaves the rest behind.
    defines_all = set().union(*(m.defines for m in members))
    assert "py_list_len" in defines_all or "_py_list_len" in defines_all, (
        "expected a known runtime symbol in the archive"
    )
    target = "py_list_len" if "py_list_len" in defines_all else "_py_list_len"
    pulled, _pending = select_members(members, {target})
    assert 0 < len(pulled) < len(members), (len(pulled), len(members))


def test_fails_closed_on_non_archives(tmp_path):
    with pytest.raises(ArchiveError):
        read_archive(b"not an archive at all")
    with pytest.raises(ArchiveError):
        read_archive(b"!<arch>\n" + b"\x00" * 30)  # truncated header
