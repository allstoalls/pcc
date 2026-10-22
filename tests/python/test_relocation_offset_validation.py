"""Dense relocation validation must not retain one boxed integer per offset."""

import os
import subprocess
import inspect
import weakref

import pytest

from pcc.backend import macho_spec as spec
from pcc.backend.macho_obj import (
    DATA_SECTION_FLAGS, MachOEmitError, Relocation, Section, TextSymbol, emit_object,
    _relocation_offset_bitmap, _claim_relocation_offset,
)
from pcc.backend.native_object import NativeObject, NativeObjectError, encode_native_object_from_sections


def test_owned_conversion_retires_only_transferred_buffers():
    rows = [Relocation(0, "_data", spec.ARM64_RELOC_UNSIGNED, False, length=3),
            Relocation(8, "_data", spec.ARM64_RELOC_UNSIGNED, False, length=3)]
    section = Section(sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
                      align_log2=3, data=b"\0" * 16,
                      symbols=(TextSymbol("_data", 0),), relocations=rows)
    borrowed = NativeObject.from_sections([section])
    assert [row.offset for row in rows] == [0, 8]
    owned = NativeObject.from_sections([section], _consume_relocations=True)
    assert rows == [None, None]
    assert owned == borrowed
    assert owned.to_macho() == borrowed.to_macho()


def test_owned_conversion_validates_before_consuming_any_section():
    good = [Relocation(0, "_data", spec.ARM64_RELOC_UNSIGNED, False, length=3)]
    duplicate = [good[0], good[0]]
    first = Section(sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
                    align_log2=3, data=b"\0" * 16,
                    symbols=(TextSymbol("_data", 0),), relocations=good)
    second = Section(sectname="__const", segname="__DATA", flags=DATA_SECTION_FLAGS,
                     align_log2=3, data=b"\0" * 16, relocations=duplicate)
    with pytest.raises(NativeObjectError, match="multiple relocation requests"):
        NativeObject.from_sections([first, second], _consume_relocations=True)
    assert good[0] is duplicate[0] is duplicate[1]


@pytest.mark.parametrize("subtractor", [False, True])
def test_large_owned_relocations_preserve_records_encoding_and_execution(tmp_path, subtractor):
    from pcc.backend.macho_obj import TEXT_SECTION_FLAGS
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.native_object import encode_native_object

    # Preserve ordinary, section, and subtractor targets through large owned
    # conversion and executable relocation passes.
    rows = [Relocation(i * 8, "_main", spec.ARM64_RELOC_UNSIGNED, False, length=3)
            for i in range(4099)]
    rows[4096] = Relocation(4096 * 8, "", spec.ARM64_RELOC_UNSIGNED, False,
                            length=3, section=("__TEXT", "__text"), target_offset=4)
    if subtractor:
        rows[4097] = Relocation(4097 * 8, "_main", spec.ARM64_RELOC_SUBTRACTOR, False,
                                length=3, minuend="_tail")
    sections = [
        Section(sectname="__text", segname="__TEXT", flags=TEXT_SECTION_FLAGS,
                align_log2=2, data=b"\x40\x05\x80\x52\xc0\x03\x5f\xd6",
                symbols=(TextSymbol("_main", 0), TextSymbol("_tail", 4)),
                relocations=[]),
        Section(sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
                align_log2=3, data=b"\0" * (4099 * 8), relocations=rows),
    ]
    borrowed = NativeObject.from_sections(sections)
    owned = NativeObject.from_sections(sections, _consume_relocations=True)
    records = owned.sections[1].relocations
    assert len(records) == 4099
    assert records[-1] == borrowed.sections[1].relocations[-1]
    assert owned == borrowed
    assert encode_native_object(owned) == encode_native_object(borrowed)
    assert owned.to_macho() == borrowed.to_macho()
    assert rows == [None] * 4099
    if subtractor:
        # The relocatable boundary supports this pair; executable application
        # currently rejects it. Owned conversion must preserve that diagnostic.
        from pcc.backend.macho_link import LinkError
        for obj in (owned, borrowed):
            with pytest.raises(LinkError, match="relocation type 1 not applied"):
                link_executable([obj])
        return
    image = link_executable([owned])
    assert image == link_executable([borrowed])
    binary = tmp_path / "large-relocations"
    binary.write_bytes(image)
    binary.chmod(0o755)
    run = subprocess.run([str(binary)], capture_output=True, timeout=10)
    assert run.returncode == 42, run.stderr


def test_owned_conversion_releases_previous_record_before_building_next(monkeypatch):
    from pcc.backend import native_object

    class SourceRelocation(Relocation):
        pass

    rows = [SourceRelocation(offset, "_data", spec.ARM64_RELOC_UNSIGNED, False, length=3)
            for offset in (0, 8)]
    previous = weakref.ref(rows[0])
    original = native_object.NativeRelocation

    class ObservedRelocation(original):
        def __new__(cls, *args, **kwargs):
            if kwargs["offset"] == 8:
                assert previous() is None
            return object.__new__(cls)

    monkeypatch.setattr(native_object, "NativeRelocation", ObservedRelocation)
    section = Section(sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
                      align_log2=3, data=b"\0" * 16,
                      symbols=(TextSymbol("_data", 0),), relocations=rows)
    result = NativeObject.from_sections([section], _consume_relocations=True)
    assert [relocation.offset for relocation in result.sections[0].relocations] == [0, 8]


def test_symbol_partition_stream_preserves_stable_canonical_order():
    first = Section(
        sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
        align_log2=3, data=b"\0" * 32,
        symbols=(TextSymbol("_e8a", 8), TextSymbol("_l16", 16, False),
                 TextSymbol("_e0", 0), TextSymbol("_e8b", 8), TextSymbol("_l0", 0, False)),
    )
    second = Section(
        sectname="__const", segname="__DATA", flags=DATA_SECTION_FLAGS,
        align_log2=3, data=b"\0" * 32,
        symbols=(TextSymbol("_e_second", 0), TextSymbol("_l_second", 0, False)),
    )
    obj = NativeObject.from_sections([first, second], undefined=["_z", "_a"])
    assert [symbol.name for symbol in obj.symbols] == [
        "_l0", "_l16", "_l_second", "_e0", "_e8a", "_e8b", "_e_second", "_a", "_z",
    ]


@pytest.mark.parametrize("emit", [NativeObject.from_sections, encode_native_object_from_sections, emit_object])
def test_duplicate_offsets_still_rejected_in_input_order(emit):
    offsets = [9, 0, 8, 1, 7, 16, 9]
    section = Section(
        sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
        align_log2=3, data=b"\0" * 32, symbols=(TextSymbol("_data", 0),),
        relocations=tuple(Relocation(
            offset, "_data", spec.ARM64_RELOC_UNSIGNED, False, length=3,
        ) for offset in offsets),
    )
    with pytest.raises((MachOEmitError, NativeObjectError), match="multiple relocation requests at offset 9"):
        emit([section])


def test_dense_and_sparse_offset_tracking_execute_natively(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "offsets.py"
    # Exercise the production helpers in a standalone native program; pcc's
    # private compiler modules are not runtime-importable application modules.
    source.write_text(inspect.getsource(_relocation_offset_bitmap) + "\n" +
                      inspect.getsource(_claim_relocation_offset) + '''
def main():
    bitmap = _relocation_offset_bitmap(80000, 10000)
    sparse = set()
    assert len(bitmap) == 10000
    for index in range(10000):
        assert _claim_relocation_offset(index * 8 + 7, bitmap, sparse)
    assert len(sparse) == 0
    for index in range(10000):
        assert not _claim_relocation_offset(index * 8 + 7, bitmap, sparse)
    # Adjacent byte offsets are distinct, including a bitmap byte boundary.
    assert _claim_relocation_offset(8, bitmap, sparse)
    assert not _claim_relocation_offset(7, bitmap, sparse)
    tiny = _relocation_offset_bitmap(1 << 31, 3)
    assert len(tiny) == 0
    for offset in [2147483640, 0, 4096]:
        assert _claim_relocation_offset(offset, tiny, sparse)
        assert not _claim_relocation_offset(offset, tiny, sparse)
    print(len(bitmap), len(sparse))
main()
''')
    output = tmp_path / "offsets"
    python_program_compiler(str(source), str(output), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        run = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stdout == "10000 3\n", (backend, run.stdout)
