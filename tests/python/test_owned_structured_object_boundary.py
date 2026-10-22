from pathlib import Path
import os
import platform
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object


IR = '''target triple = "arm64-apple-darwin"
@value = global i32 42, align 4
define i32 @helper() {
entry:
  %result = load i32, ptr @value
  ret i32 %result
}
define i32 @main() {
entry:
  %result = call i32 @helper()
  ret i32 %result
}
'''


def test_darwin_structured_public_object_is_standard_macho_and_archiveable():
    from pcc.backend import macho_spec
    from pcc.backend.ar_writer import write_archive
    from pcc.backend.ar import read_members
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.native_object import NativeObject
    from pcc.backend.self_backend_dispatch import emit_self_asm

    data = emit_owned_object(IR, "arm64-apple-darwin")
    assert data[:4] == b"\xcf\xfa\xed\xfe"
    assert macho_spec.parse_object(data).header["filetype"] == macho_spec.MH_OBJECT
    archive = write_archive([("program.o", data)])
    assert read_members(archive) == [("program.o", data)]
    sections, undefined = assemble_file(emit_self_asm(IR, "arm64-apple-darwin"))
    assert data == NativeObject.from_sections(sections, undefined=undefined).to_macho()


@pytest.mark.parametrize("triple", ["arm64-apple-darwin", "x86_64-unknown-linux-gnu"])
@pytest.mark.parametrize("setting,value", [("PCC_SELF_TARGET_PASS_TRANSPORT", "invalid"), ("PCC_SELF_TARGET_PASSES", "bogus-pass")])
def test_object_emission_rejects_invalid_target_pass_settings(monkeypatch, triple, setting, value):
    from pcc.backend import BackendUnavailable

    monkeypatch.setenv(setting, value)
    monkeypatch.delenv("PCC_SELF_TARGET_PASS_TRANSPORT" if setting.endswith("PASSES") else "PCC_SELF_TARGET_PASSES", raising=False)
    text = IR.replace("arm64-apple-darwin", triple)
    with pytest.raises(BackendUnavailable, match="unknown self target pass"):
        emit_owned_object(text, triple)


def test_owned_object_rejects_conflicting_declared_target():
    with pytest.raises(ValueError, match="target triple mismatch"):
        emit_owned_object(IR, "x86_64-unknown-linux-gnu")


def test_explicit_text_target_pass_executes_and_publishes_standard_object(monkeypatch):
    from pcc.backend import macho_spec, self_backend_target_passes

    calls = []
    original = self_backend_target_passes.FoldImmediatePass.run

    def run(self, assembly, context):
        calls.append(context.target_id)
        return original(self, assembly, context)

    monkeypatch.setattr(self_backend_target_passes.FoldImmediatePass, "run", run)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "aarch64-fold-immediate")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")
    data = emit_owned_object(IR, "arm64-apple-darwin")
    assert calls == ["self-aarch64-darwin-v0"]
    assert macho_spec.parse_object(data).header["filetype"] == macho_spec.MH_OBJECT


@pytest.mark.pcc_gate(unavailable=None if os.sys.platform == "darwin" and platform.machine() == "arm64" else "requires Darwin arm64")
def test_published_structured_object_links_and_executes(tmp_path: Path):
    from pcc.backend.macho_exec import link_executable

    data = emit_owned_object(IR, "arm64-apple-darwin")
    image = link_executable([data], entry="_main")
    output = tmp_path / "native"
    output.write_bytes(image)
    output.chmod(0o755)
    assert subprocess.run([str(output)], timeout=10).returncode == 42
