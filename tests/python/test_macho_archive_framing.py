"""Finite host-only malformed-archive checks; no native toolchain required."""

import subprocess
import sys
from pathlib import Path

import pytest

from pcc.backend import macho_archive
from pcc.backend.ar_writer import write_archive
from pcc.backend.macho_obj import Section, TextSymbol, emit_object


ROOT = Path(__file__).resolve().parents[2]


def _header(name, size):
    fields = (name, b"0", b"0", b"0", b"100644", str(size).encode("ascii"))
    widths = (16, 12, 6, 6, 8, 10)
    assert all(len(value) <= width for value, width in zip(fields, widths))
    return b"".join(value.ljust(width, b" ") for value, width in zip(fields, widths)) + b"`\n"


def _object(name="_main", undefined=()):
    return emit_object([Section(
        sectname="__text", segname="__TEXT", data=b"\xc0\x03\x5f\xd6",
        symbols=(TextSymbol(name, 0),),
    )], undefined=undefined)


@pytest.mark.parametrize("boundary", ("parser", "linker"))
def test_negative_index_member_size_cannot_repeat_its_header(boundary):
    # body = 8 + 60, size = -60: the old parser resets offset to 8, then
    # skips this index and reads the same header forever. Keep the regression
    # in a killable child so a future missing guard cannot hang a pytest worker.
    data = macho_archive.ARMAG + _header(b"__.SYMDEF/", -60)
    script = """
import sys
from pcc.backend.macho_archive import ArchiveError, read_archive
from pcc.backend.macho_exec import prepare_executable_object
from pcc.backend.macho_link import LinkError
try:
    if sys.argv[2] == 'parser':
        read_archive(bytes.fromhex(sys.argv[1]))
    else:
        prepare_executable_object([bytes.fromhex(sys.argv[3])],
                                  archives=[bytes.fromhex(sys.argv[1])])
except (ArchiveError, LinkError) as exc:
    assert type(exc) is (ArchiveError if sys.argv[2] == 'parser' else LinkError)
    print(str(exc))
else:
    raise AssertionError('negative-size index member was accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, data.hex(), boundary, _object().hex()], cwd=ROOT,
        capture_output=True, text=True, timeout=3,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = "archive 0 is outside the proven subset: " if boundary == "linker" else ""
    assert result.stdout.strip() == prefix + "negative member size at 8"


@pytest.mark.parametrize("name", (b"__.SYMDEF/", b"__.SYMDEF SORTED", b"object.o/", b"#1/4"))
@pytest.mark.parametrize("size", (-1, -59, -60, -61, -999999999))
def test_negative_size_is_rejected_before_name_or_payload_slicing(name, size):
    class HeaderOnlyBytes(bytes):
        def __getitem__(self, key):
            assert key == slice(8, 68), "negative member reached name/payload slicing"
            return super().__getitem__(key)

    data = HeaderOnlyBytes(macho_archive.ARMAG + _header(name, size))
    with pytest.raises(macho_archive.ArchiveError, match="^negative member size at 8$"):
        macho_archive.read_archive(data)


@pytest.mark.parametrize("name_length,size", ((-1, 0), (-60, 32), (1, 0), (4, 3), (32, 8), (999999, 32)))
def test_extended_name_length_is_bounded_before_slicing(name_length, size):
    class HeaderOnlyBytes(bytes):
        def __getitem__(self, key):
            assert key == slice(8, 68), "invalid extended name reached name/payload slicing"
            return super().__getitem__(key)

    data = HeaderOnlyBytes(
        macho_archive.ARMAG + _header(b"#1/" + str(name_length).encode("ascii"), size)
        + b"x" * size,
    )
    with pytest.raises(macho_archive.ArchiveError, match="^bad extended name length at 8$"):
        macho_archive.read_archive(data)


@pytest.mark.parametrize("size", (0, 1, 2, 3))
def test_nonnegative_index_members_preserve_padding_and_following_objects(size):
    obj = _object("_helper")
    index = _header(b"__.SYMDEF/", size) + b"x" * size + b"\n" * (size % 2)
    following = _header(b"helper.o/", len(obj)) + obj + b"\n" * (len(obj) % 2)
    members = macho_archive.read_archive(macho_archive.ARMAG + index + following)
    assert [(member.name, member.data) for member in members] == [("helper.o", obj)]
    assert members[0].defines == frozenset({"_helper"})


def test_valid_bsd_archive_keeps_long_names_and_selection_order():
    sources = [
        ("early-member-with-a-long-name.o", _object("_early")),
        ("late-member-with-a-long-name.o", _object("_late", ("_early",))),
        ("unused-member-with-a-long-name.o", _object("_unused")),
    ]
    members = macho_archive.read_archive(write_archive(sources))
    assert [(member.name, member.data) for member in members] == sources
    selected = []
    pulled, pending = macho_archive.select_members(
        members, {"_late"}, selected_members=selected,
    )
    assert [member.name for member in selected] == [name for name, _ in sources[:2]]
    assert pulled == [data for _, data in sources[:2]] and pending == set()


@pytest.mark.parametrize("name", (b"", b"a.o", b"long-object-name.o\0\0"))
def test_bsd_name_prefix_keeps_accepted_nonnegative_lengths(name):
    obj = _object()
    size = len(name) + len(obj)
    data = (macho_archive.ARMAG + _header(b"#1/" + str(len(name)).encode("ascii"), size)
            + name + obj + b"\n" * (size % 2))
    members = macho_archive.read_archive(data)
    assert [(member.name, member.data) for member in members] == [(name.rstrip(b"\0").decode(), obj)]


def test_bsd_index_name_may_occupy_the_entire_member():
    name = b"__.SYMDEF"
    data = (macho_archive.ARMAG + _header(b"#1/9", len(name))
            + name + b"\n")
    assert macho_archive.read_archive(data) == []


@pytest.mark.parametrize("name", (b"/", b"//"))
def test_unimplemented_gnu_metadata_is_not_silently_admitted(name):
    # GNU/SysV short object names are covered above. GNU indexes and long-name
    # tables are not an accepted Mach-O archive input; keep that boundary.
    with pytest.raises(macho_archive.ArchiveError, match="is too small to be an object"):
        macho_archive.read_archive(macho_archive.ARMAG + _header(name, 0))


def test_framing_errors_precede_all_member_symbol_parsing(monkeypatch):
    obj = _object()
    first = _header(b"main.o/", len(obj)) + obj + b"\n" * (len(obj) % 2)
    offset = len(macho_archive.ARMAG) + len(first)

    def forbidden(*args):
        pytest.fail("member symbols were parsed before framing validation finished")

    monkeypatch.setattr(macho_archive, "_inspect_member", forbidden)
    with pytest.raises(macho_archive.ArchiveError, match=f"^negative member size at {offset}$"):
        macho_archive.read_archive(macho_archive.ARMAG + first + _header(b"bad.o/", -1))


@pytest.mark.parametrize("data,message", (
    (b"not an archive", "not an ar archive"),
    (macho_archive.ARMAG + b"short", "truncated member header at 8"),
    (macho_archive.ARMAG + _header(b"object.o/", "invalid"), "bad member size at 8"),
    (macho_archive.ARMAG + _header(b"object.o/", 1), "member at 8 runs past end of archive"),
    (macho_archive.ARMAG + _header(b"object.o/", 0), "is too small to be an object"),
    (macho_archive.ARMAG + _header(b"#1/invalid", 0), "bad extended name at 8"),
))
def test_other_archive_errors_keep_their_existing_contract(data, message):
    with pytest.raises(macho_archive.ArchiveError, match=message):
        macho_archive.read_archive(data)
