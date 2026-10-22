"""The owned linker entry accepts the same ordered PCO stream as codegen."""

import subprocess
import sys
import platform
import os

import pytest

from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import NativeObject, encode_native_object
from pcc.backend import owned_link_driver

pytestmark = pytest.mark.pcc_gate(unavailable=(
    None if sys.platform == "darwin" and platform.machine() == "arm64"
    else "native Mach-O execution requires Darwin arm64"
))


def _inputs(tmp_path):
    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".globl _main\n.p2align 2\n_main:\n  movz w0, #42\n  ret\n"
    )
    object_file = tmp_path / "main.pco"
    object_file.write_bytes(encode_native_object(
        NativeObject.from_sections(sections, undefined=undefined)))
    manifest = tmp_path / "inputs.txt"
    manifest.write_text("pcc.macho-internal-inputs.v1\n1\nPCO\t" + str(object_file) + "\n")
    return object_file, manifest


def test_owned_link_driver_manifest_matches_direct_pco(tmp_path, monkeypatch):
    object_file, manifest = _inputs(tmp_path)
    outputs = []
    for flag, value in (("--native-object", object_file), ("--internal-input-manifest", manifest)):
        output = tmp_path / ("linked" + str(len(outputs)))
        monkeypatch.setattr(sys, "argv", ["link", flag, str(value), "--out", str(output)])
        owned_link_driver.main()
        outputs.append(output.read_bytes())
        run = subprocess.run([str(output)], capture_output=True, timeout=10)
        assert run.returncode == 42, run.stderr
    assert outputs[0] == outputs[1]


def test_owned_link_driver_rejects_mixed_manifest_and_direct_input(tmp_path, monkeypatch):
    object_file, manifest = _inputs(tmp_path)
    output = tmp_path / "rejected"
    monkeypatch.setattr(sys, "argv", ["link", "--native-object", str(object_file),
        "--internal-input-manifest", str(manifest), "--out", str(output)])
    with pytest.raises(ValueError, match="cannot be combined"):
        owned_link_driver.main()
    assert not output.exists()


@pytest.mark.integration
def test_owned_link_driver_large_relocations_execute_natively(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    from pathlib import Path
    from pcc.backend import macho_spec as spec
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.macho_obj import (
        DATA_SECTION_FLAGS, TEXT_SECTION_FLAGS, Relocation, Section, TextSymbol,
    )

    # Exercise indexed reads, repeated validation, sorted relocation
    # application and section targets across all five GC backends.
    rows = [Relocation(i * 8, "_main", spec.ARM64_RELOC_UNSIGNED, False, length=3)
            for i in range(8195)]
    rows[4096] = Relocation(4096 * 8, "", spec.ARM64_RELOC_UNSIGNED, False,
                            length=3, section=("__TEXT", "__text"), target_offset=4)
    obj = NativeObject.from_sections([
        Section(sectname="__text", segname="__TEXT", flags=TEXT_SECTION_FLAGS,
                align_log2=2, data=b"\x40\x05\x80\x52\xc0\x03\x5f\xd6",
                symbols=(TextSymbol("_main", 0), TextSymbol("_tail", 4))),
        Section(sectname="__data", segname="__DATA", flags=DATA_SECTION_FLAGS,
                align_log2=3, data=b"\0" * (8195 * 8), relocations=tuple(rows)),
    ])
    golden = link_executable([obj])
    source = tmp_path / "large.pco"
    source.write_bytes(encode_native_object(obj))
    linker = tmp_path / "pcc-link"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(
        str(Path(owned_link_driver.__file__)), str(linker), backend="self",
        libpython_mode="off", runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        output = tmp_path / ("linked-gc" + str(backend))
        env = dict(os.environ, PCC_GC_BACKEND=str(backend), PATH="/nonexistent",
                   PCC_HOST_PYTHON="/usr/bin/false", PCC_HOST_PCC="/usr/bin/false",
                   PCC_RUNTIME_CC="/usr/bin/false")
        run = subprocess.run(
            [str(linker), "--native-object", str(source), "--out", str(output)],
            env=env, capture_output=True, timeout=60,
        )
        assert run.returncode == 0, (backend, run.stderr)
        assert output.read_bytes() == golden
        emitted = subprocess.run([str(output)], env=env, capture_output=True, timeout=10)
        assert emitted.returncode == 42, (backend, emitted.stderr)
