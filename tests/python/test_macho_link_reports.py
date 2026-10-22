"""A successful Mach-O link is quiet unless a report is actionable.

Every macOS link wrote two stderr lines: "dropped unwind metadata" for the
frame-pointer compact-unwind rows pcc's own backend emits on every object, and
the __text span against the branch reach. Native tests that require an empty
compiler stderr therefore failed on macOS only. The rows describe frames the
frame chain already describes, so dropping them loses no behavior; a row that
names a personality routine or LSDA, or an __eh_frame section, carries
exception handling this link cannot keep and is still reported. The span is
reported when its margin to the reach is small, or on request.
"""

from __future__ import annotations

import struct

import pytest

from pcc.backend import macho_exec, macho_obj, macho_spec as spec
from pcc.backend.macho_exec import link_executable
from pcc.backend.macho_obj import (
    COMPACT_UNWIND_SECTION_FLAGS,
    EH_FRAME_SECTION_FLAGS,
    Relocation,
    Section,
    TextSymbol,
)

_TEXT = struct.pack("<II", 0x52800540, 0xD65F03C0)  # mov w0,#42 ; ret


def _object(*extra):
    return macho_obj.emit_object([
        Section(sectname="__text", segname="__TEXT", data=_TEXT, align_log2=2,
                flags=macho_obj.TEXT_SECTION_FLAGS,
                symbols=(TextSymbol("_main", 0), TextSymbol("_handler", 4))),
        *extra,
    ])


def _unwind(*offsets):
    return Section(
        sectname="__compact_unwind", segname="__LD", data=b"\0" * 32,
        align_log2=3, flags=COMPACT_UNWIND_SECTION_FLAGS,
        relocations=tuple(
            Relocation(offset, "_main" if offset == 0 else "_handler",
                       spec.ARM64_RELOC_UNSIGNED, pcrel=False, length=3)
            for offset in offsets
        ),
    )


def test_frame_pointer_unwind_rows_drop_quietly(capfd, monkeypatch):
    monkeypatch.delenv("PCC_LINK_REPORT_TEXT_SPAN", raising=False)
    link_executable([_object(_unwind(0))])
    assert capfd.readouterr().err == ""


@pytest.mark.parametrize("offset", (16, 24))
def test_personality_or_lsda_rows_are_reported(capfd, monkeypatch, offset):
    monkeypatch.delenv("PCC_LINK_REPORT_TEXT_SPAN", raising=False)
    link_executable([_object(_unwind(0, offset))])
    assert capfd.readouterr().err == (
        "pcc link: dropped unwind metadata (no __unwind_info synthesis): "
        "__LD,__compact_unwind\n"
    )


def test_eh_frame_drop_is_reported(capfd, monkeypatch):
    monkeypatch.delenv("PCC_LINK_REPORT_TEXT_SPAN", raising=False)
    eh_frame = Section(sectname="__eh_frame", segname="__TEXT", data=b"\0" * 16,
                       align_log2=3, flags=EH_FRAME_SECTION_FLAGS)
    link_executable([_object(eh_frame)])
    assert capfd.readouterr().err == (
        "pcc link: dropped unwind metadata (no __unwind_info synthesis): "
        "__TEXT,__eh_frame\n"
    )


@pytest.mark.parametrize(("setting", "margin", "wanted"), [
    (None, 120 << 20, False),
    (None, (32 << 20) - 1, True),
    (None, -1, True),
    ("1", 120 << 20, True),
    ("on", 120 << 20, True),
    ("0", -1, False),
    ("off", -1, False),
    ("", -1, False),
])
def test_text_span_report_policy(setting, margin, wanted):
    assert macho_exec._text_span_report_wanted(setting, margin) is wanted


def test_text_span_report_on_request(capfd, monkeypatch):
    monkeypatch.setenv("PCC_LINK_REPORT_TEXT_SPAN", "1")
    link_executable([_object()])
    err = capfd.readouterr().err
    assert err.startswith("pcc link: __text span ") and err.endswith((" island\n", " islands\n"))
