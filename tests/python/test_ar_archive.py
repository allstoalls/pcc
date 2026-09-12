"""Archive framing is independent of the enclosed object format."""

import pytest

from pcc.backend.ar import ArchiveFormatError, read_members


def _member(name: bytes, payload: bytes) -> bytes:
    return (
        name.ljust(16) + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
        + b"100644".ljust(8) + str(len(payload)).encode().ljust(10) + b"`\n"
        + payload + (b"\n" if len(payload) % 2 else b"")
    )


def test_sysv_members_preserve_order_and_padding():
    data = b"!<arch>\n" + _member(b"first.o/", b"abc") + _member(b"second.o/", b"defg")
    assert read_members(data) == [("first.o", b"abc"), ("second.o", b"defg")]


def test_bsd_extended_names_and_symbol_indexes():
    name = b"a_long_object_name.o\0\0"
    data = b"!<arch>\n" + _member(b"#1/20", b"__.SYMDEF SORTED\0\0\0\0\0" + b"index")
    data += _member(b"#1/" + str(len(name)).encode(), name + b"object")
    assert read_members(data) == [("a_long_object_name.o", b"object")]


def test_gnu_filename_table_and_symbol_indexes():
    table = b"first_long_object_name.o/\nsecond_long_object_name.o/\n"
    offset = table.index(b"second")
    data = b"!<arch>\n" + _member(b"/", b"index") + _member(b"/SYM64/", b"wide")
    data += _member(b"//", table)
    data += _member(b"/0", b"first") + _member(b"/" + str(offset).encode(), b"second")
    assert read_members(data) == [
        ("first_long_object_name.o", b"first"),
        ("second_long_object_name.o", b"second"),
    ]


@pytest.mark.parametrize("data", [
    b"!<thin>\n",
    b"!<arch>\nshort",
    b"!<arch>\n" + _member(b"x.o/", b"abc")[:-1],
    b"!<arch>\n" + _member(b"#1/20", b"short"),
    b"!<arch>\n" + _member(b"/0", b"object"),
])
def test_malformed_or_external_payloads_fail_closed(data):
    with pytest.raises(ArchiveFormatError):
        read_members(data)
