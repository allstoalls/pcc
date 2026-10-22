"""Final-link import membership uses the existing index without changing output.

These are host-only linker tests. They neither run emitted Mach-O images nor
claim native compiler throughput.
"""

import builtins
import hashlib

import pytest

from pcc.backend import macho_exec, macho_spec as spec
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_codesign import parse_signature
from pcc.backend.macho_link import LinkError
from pcc.backend.native_object import (
    NativeObject,
    decode_packed_native_object,
    encode_native_object,
)


_IMPORTS = ["_abs", "_getpid", "_optional", "_puts"]
_ASSEMBLY = """\
.section __TEXT,__text,regular,pure_instructions
.globl _main
_main:
  bl _puts
  bl _getpid
  bl _abs
  bl _optional
  bl _puts
  bl _helper
  adrp x9, _puts@GOTPAGE
  ldr x9, [x9, _puts@GOTPAGEOFF]
  ret
_helper:
  ret
.section __DATA,__data
.p2align 3
  .quad _puts+7
  .quad _getpid
  .quad _optional
  .quad _helper
"""


def _input(representation, *, unreferenced=False):
    sections, undefined = assemble_file(_ASSEMBLY)
    if unreferenced:
        undefined.append("_unreferenced")
    obj = NativeObject.from_sections(
        sections, undefined=undefined, weak_undefined=("_optional",),
    )
    if representation == "raw":
        return obj.to_macho()
    if representation == "packed":
        return decode_packed_native_object(encode_native_object(obj))
    return obj


def _link(representation, source_view, *, unreferenced=False):
    return macho_exec.link_executable(
        [_input(representation, unreferenced=unreferenced)],
        _consume_inputs=source_view, _direct_source_view=source_view,
    )


def _track_import_list(monkeypatch):
    captured = []

    class ImportNames(list):
        membership_calls = 0

        def __contains__(self, value):
            self.membership_calls += 1
            return super().__contains__(value)

    def tracked_sorted(values, *args, **kwargs):
        result = builtins.sorted(values, *args, **kwargs)
        if not captured and result == _IMPORTS:
            result = ImportNames(result)
            captured.append(result)
        return result

    monkeypatch.setattr(macho_exec, "sorted", tracked_sorted, raising=False)
    return captured


@pytest.mark.parametrize("representation", ["native", "raw", "packed"])
@pytest.mark.parametrize("source_view", [False, True])
def test_import_relocations_reuse_existing_set(
    representation, source_view, monkeypatch,
):
    monkeypatch.setattr(macho_exec, "_libsystem_exports_symbol", lambda name: True)
    reference = _link(representation, source_view)
    captured = _track_import_list(monkeypatch)
    image = _link(representation, source_view)
    assert image == reference
    assert len(captured) == 1
    assert captured[0] == _IMPORTS
    assert captured[0].membership_calls == 0


@pytest.mark.parametrize("representation", ["native", "raw", "packed"])
@pytest.mark.parametrize("source_view", [False, True])
def test_import_lookup_preserves_image(
    representation, source_view, monkeypatch, record_property,
):
    looked_up = []

    def provider(name):
        looked_up.append(name)
        return True

    monkeypatch.setattr(macho_exec, "_libsystem_exports_symbol", provider)
    image = _link(representation, source_view)
    assert looked_up == ["_abs", "_getpid", "_puts"]
    assert image == _link("native", False)
    imports = [
        symbol for symbol in spec.parse_object(image).symbols()
        if (symbol["n_type"] & spec.N_TYPE) == spec.N_UNDF
    ]
    assert [symbol["name"] for symbol in imports] == _IMPORTS
    assert [symbol["name"] for symbol in imports
            if symbol["n_desc"] & spec.N_WEAK_REF] == ["_optional"]
    assert parse_signature(image).identifier == b"pcc-linked"
    record_property("image_sha256", hashlib.sha256(image).hexdigest())


@pytest.mark.parametrize("source_view", [False, True])
@pytest.mark.parametrize("unreferenced", [False, True])
def test_import_lookup_preserves_errors(
    source_view, unreferenced, monkeypatch, record_property,
):
    looked_up = []

    def provider(name):
        looked_up.append(name)
        return name == "_puts"

    monkeypatch.setattr(macho_exec, "_libsystem_exports_symbol", provider)
    with pytest.raises(LinkError) as error:
        _link("packed", source_view, unreferenced=unreferenced)
    if unreferenced:
        assert str(error.value) == (
            "undefined symbol(s) have no relocation naming an import: "
            "'_unreferenced'"
        )
        assert looked_up == []
    else:
        assert str(error.value) == (
            "undefined Mach-O symbol(s): '_abs', '_getpid' "
            "(not exported by /usr/lib/libSystem.B.dylib)"
        )
        assert looked_up == ["_abs", "_getpid", "_puts"]
    record_property("link_error", str(error.value))
