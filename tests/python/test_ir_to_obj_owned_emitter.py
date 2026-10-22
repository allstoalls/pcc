"""The runtime archive's objects must not be emitted through llvmlite.

`libpy_runtime_pcc_py.a`'s members are the objects pcc1 links.  Emitting them
with llvmlite made an llvmlite-free pcc1 depend on llvmlite to exist at all.
pcc owns the supported targets and rejects retired emitter selections.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import pcc.tools.ir_to_obj as ir_to_obj


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE_IR = textwrap.dedent(
    """\
    target triple = "arm64-apple-darwin"

    define i64 @pcc_probe_add(i64 %a, i64 %b) {
    entry:
      %r = add i64 %a, %b
      ret i64 %r
    }
    """
)


def _blocked_llvmlite_dir(tmp_path: Path) -> Path:
    blocker = tmp_path / "no_llvmlite"
    (blocker / "llvmlite").mkdir(parents=True)
    (blocker / "llvmlite" / "__init__.py").write_text(
        "raise ImportError('llvmlite is blocked for this test')\n",
        encoding="utf-8",
    )
    return blocker


def test_emitter_env_name_does_not_collide_with_the_tool_command():
    """`PCC_IR_TO_OBJ` is the Makefile's *command* for running this tool
    (pcc/runtime/Makefile), so reading it as an emitter name would compare a
    path against 'pcc'/'llvmlite'."""
    assert ir_to_obj._EMITTER_ENV == "PCC_IR_TO_OBJ_EMITTER"
    makefile = (REPO_ROOT / "pcc" / "runtime" / "Makefile").read_text(
        encoding="utf-8"
    )
    assert "PCC_IR_TO_OBJ ?= $(PYTHON) -m pcc.tools.ir_to_obj" in makefile


def test_default_emission_does_not_import_llvmlite(tmp_path):
    blocker = _blocked_llvmlite_dir(tmp_path)
    ir_path = tmp_path / "probe.ll"
    ir_path.write_text(_PROBE_IR, encoding="utf-8")
    out_path = tmp_path / "probe.o"

    process = subprocess.run(
        [
            sys.executable, "-m", "pcc.tools.ir_to_obj",
            str(ir_path), str(out_path),
        ],
        cwd=str(REPO_ROOT),
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(blocker) + ":" + str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert out_path.exists() and out_path.stat().st_size > 0
    assert out_path.read_bytes()[:4] == b"\xcf\xfa\xed\xfe"  # MH_MAGIC_64


def test_removed_llvmlite_emitter_is_rejected_before_creating_output(tmp_path, monkeypatch, capsys):
    ir_path = tmp_path / "probe.ll"
    ir_path.write_text(_PROBE_IR, encoding="utf-8")
    oracle = tmp_path / "oracle.o"
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "llvmlite")
    with pytest.raises(ir_to_obj.ObjectEmissionContractError, match="expected 'pcc'"):
        ir_to_obj.emit_object(_PROBE_IR)
    assert ir_to_obj.main([str(ir_path), str(oracle)]) == 1
    assert "unknown PCC_IR_TO_OBJ_EMITTER value 'llvmlite'; expected 'pcc'" in capsys.readouterr().err
    assert not oracle.exists()


def test_unknown_emitter_selection_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "system-as")
    with pytest.raises(ir_to_obj.ObjectEmissionContractError) as excinfo:
        ir_to_obj.emit_object(_PROBE_IR)
    assert "system-as" in str(excinfo.value)


def test_unsupported_target_does_not_select_an_implicit_llvm_oracle(monkeypatch):
    monkeypatch.delenv(ir_to_obj._EMITTER_ENV, raising=False)
    with pytest.raises(ir_to_obj.ObjectEmissionContractError, match="does not own"):
        ir_to_obj.emit_object(_PROBE_IR.replace("arm64-apple-darwin", "riscv64-unknown-linux-gnu"))


def test_owned_elf_emission_uses_the_existing_x86_backend(monkeypatch):
    class ForbiddenLLVM:
        def __getattr__(self, name):
            pytest.fail("owned ELF emission consulted LLVM: " + name)

    monkeypatch.setitem(sys.modules, "llvmlite", ForbiddenLLVM())
    monkeypatch.setitem(sys.modules, "llvmlite.binding", ForbiddenLLVM())
    data = ir_to_obj.emit_object(_PROBE_IR.replace("arm64-apple-darwin", "x86_64-unknown-linux-gnu"))
    assert data[:4] == b"\x7fELF"
    assert int.from_bytes(data[18:20], "little") == 62  # EM_X86_64


def test_owned_target_and_layout_contracts_are_not_bypassed():
    with pytest.raises(ir_to_obj.ObjectEmissionContractError, match="target triple mismatch"):
        ir_to_obj.emit_object(_PROBE_IR, target_triple="x86_64-unknown-linux-gnu")
    for layout in ("e-p:32:32", "E-p:64:64", "e-i64:32"):
        with pytest.raises(ir_to_obj.ObjectEmissionContractError, match="target data layout mismatch"):
            ir_to_obj.emit_object('target datalayout = "' + layout + '"\n' + _PROBE_IR)


def test_receipt_names_the_emitter_that_actually_ran(tmp_path):
    ir_path = tmp_path / "probe.ll"
    ir_path.write_text(_PROBE_IR, encoding="utf-8")
    source = tmp_path / "runtime" / "py" / "probe.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    out_path = tmp_path / "probe.o"
    receipt = tmp_path / "probe.o.provenance.json"

    rc = ir_to_obj.main([
        str(ir_path), str(out_path),
        "--provenance", str(receipt),
        "--source", str(source),
        "--runtime-root", str(tmp_path / "runtime"),
        "--member", "probe.o",
    ])
    assert rc == 0, "emission failed"
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["object_emitter"] == "pcc-self-backend-object-writer"
    assert record["uses_host_cc"] is False
