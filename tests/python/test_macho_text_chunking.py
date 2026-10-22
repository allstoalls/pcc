"""Merged __TEXT,__text splits at input boundaries so thunk islands interleave.

AArch64 b/bl reach +-128 MiB and the executable linker places branch thunk
islands only between output sections. A self-hosted pcc1 merges more than
300 MiB of __text from its native objects; as one section, call sites in its
middle could not reach any island ("range-extension thunk itself out of
range ... __text needs more than one thunk island"). The relocatable merge now
starts a new __TEXT,__textN section when an input would push the current one
past ``_TEXT_CHUNK_BYTES``. These tests shrink the limits so three tiny inputs
exercise the same split, symbol, relocation and island layout.
"""
import platform
import subprocess
import sys

import pytest

from pcc.backend import macho_exec, macho_link, macho_spec as spec
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import NativeObject


_TEXT = ".section __TEXT,__text,regular,pure_instructions\n"
_SOURCES = (
    _TEXT + ".globl _main\n_main:\n stp x29, x30, [sp, #-16]!\n mov x29, sp\n"
    " bl _f1\n ldp x29, x30, [sp], #16\n ret\n",
    _TEXT + ".globl _f1\n_f1:\n stp x29, x30, [sp, #-16]!\n mov x29, sp\n"
    " bl _f2\n add x0, x0, #10\n ldp x29, x30, [sp], #16\n ret\n",
    _TEXT + ".globl _f2\n_f2:\n mov x0, #32\n ret\n",
)


def _objects():
    objects = []
    for source in _SOURCES:
        sections, undefined = assemble_file(source)
        objects.append(NativeObject.from_sections(sections, undefined=undefined))
    return objects


def test_text_splits_per_input_when_over_the_chunk_limit(monkeypatch):
    monkeypatch.setattr(macho_link, "_TEXT_CHUNK_BYTES", 1)
    merged = macho_link.link_relocatable_native(_objects())
    text = [section for section in merged.sections if section.segname == "__TEXT"]
    assert [section.sectname for section in text] == ["__text", "__text1", "__text2"]
    owner = {symbol.name: merged.sections[symbol.section_index - 1].sectname
             for symbol in merged.symbols if symbol.section_index}
    assert owner == {"_main": "__text", "_f1": "__text1", "_f2": "__text2"}
    calls = {(section.sectname, merged.symbols[relocation.symbol_index].name)
             for section in text for relocation in section.relocations}
    assert calls == {("__text", "_f1"), ("__text1", "_f2")}


def test_text_stays_one_section_under_the_chunk_limit():
    merged = macho_link.link_relocatable_native(_objects())
    text = [section for section in merged.sections if section.segname == "__TEXT"]
    assert [section.sectname for section in text] == ["__text"]


@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin" and platform.machine() == "arm64")
def test_chunked_text_with_interleaved_islands_executes(tmp_path, monkeypatch):
    monkeypatch.setattr(macho_link, "_TEXT_CHUNK_BYTES", 1)
    # A span above one island table (12 bytes per branch target) but below
    # two tiny chunks places islands between text sections, not only after
    # the last one; the island count iteration still converges.
    monkeypatch.setattr(macho_exec, "_VENEER_ISLAND_SPAN", 32)
    image = macho_exec.link_executable(_objects())
    names = [section["sectname_str"] for section in spec.parse_object(image).sections()
             if section["segname_str"] == "__TEXT"]
    first, last = names.index("__text"), names.index("__text2")
    assert "__veneers" in names[first + 1:last], names
    path = tmp_path / "chunked"
    path.write_bytes(image)
    path.chmod(0o755)
    result = subprocess.run([str(path)], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr
