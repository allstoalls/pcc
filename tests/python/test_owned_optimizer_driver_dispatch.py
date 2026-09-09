"""Standalone pass selection must retain the production pipeline's effects."""

import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

from pcc.native_ir.driver import optimize_ir
from pcc.py_frontend.compiled_owned_passes import run_owned_passes


_BRANCH = """declare void @observe(ptr)
define i64 @pick(i1 %condition) {
entry:
  %slot = alloca i64
  %escaped = alloca i64
  store volatile i64 3, ptr %escaped
  call void @observe(ptr %escaped)
  br i1 %condition, label %yes, label %no
yes:
  store i64 11, ptr %slot
  br label %done
no:
  store i64 22, ptr %slot
  br label %done
done:
  %value = load i64, ptr %slot
  %dead = add i64 %value, 0
  ret i64 %value
}
"""


@pytest.mark.parametrize("passes", [
    "mem2reg", "sroa", "sroa,mem2reg", "mem2reg,sroa",
    "mem2reg,sroa,instsimplify,simplifycfg,instcombine,dce",
    "instsimplify,simplifycfg,inline-defined,mem2reg,sroa,dce",
])
def test_driver_preserves_order_and_escaping_memory(passes):
    actual = optimize_ir(_BRANCH, passes)
    assert actual == run_owned_passes(_BRANCH, passes.split(","), False)
    assert "store volatile i64 3, ptr %escaped" in actual
    assert "call void @observe(ptr %escaped)" in actual
    if "mem2reg" in passes:
        assert "%slot = alloca" not in actual
        assert " = phi i64 " in actual or "select i1 %condition, i64 11, i64 22" in actual


def test_standalone_memory_cli_without_llvm(tmp_path):
    source = tmp_path / "input.ll"
    output = tmp_path / "output.ll"
    source.write_text(_BRANCH)
    # Exercise the file/argv entry and reject even an attempted LLVM import.
    (tmp_path / "sitecustomize.py").write_text('''import importlib.abc
import sys
class RejectLLVM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "llvmlite" or fullname.startswith("llvmlite.") or fullname == "pcc.llvm_capi.binding":
            raise AssertionError("external LLVM import: " + fullname)
sys.meta_path.insert(0, RejectLLVM())
''')
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(tmp_path), str(root))))
    ran = subprocess.run(
        [sys.executable, "-m", "pcc.native_ir.driver", "mem2reg,sroa", str(source), str(output)],
        cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=20,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert output.read_text() == run_owned_passes(_BRANCH, ["mem2reg", "sroa"], False)
    assert "mem2reg,sroa changed=True" in ran.stderr


def test_driver_rejects_unknown_pass():
    with pytest.raises(ValueError, match="unsupported owned IR pass: unknown"):
        optimize_ir(_BRANCH, "unknown")


@pytest.mark.skipif(sys.platform != "darwin" or platform.machine() != "arm64",
                    reason="executes owned Mach-O/AArch64 output")
def test_memory_driver_output_executes_with_self_emission(tmp_path):
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm

    source = _BRANCH.replace("declare void @observe(ptr)", """define void @observe(ptr %slot) {
entry:
  ret void
}""") + """
define i64 @total(i64 %limit) {
entry:
  %index = alloca i64
  %sum = alloca i64
  store i64 0, ptr %index
  store i64 0, ptr %sum
  br label %head
head:
  %i = load i64, ptr %index
  %more = icmp slt i64 %i, %limit
  br i1 %more, label %body, label %done
body:
  %old = load i64, ptr %sum
  %nextsum = add i64 %old, %i
  store i64 %nextsum, ptr %sum
  %next = add i64 %i, 1
  store i64 %next, ptr %index
  br label %head
done:
  %value = load i64, ptr %sum
  ret i64 %value
}
define i32 @main() {
entry:
  %left = call i64 @pick(i1 true)
  %right = call i64 @pick(i1 false)
  %sum = add i64 %left, %right
  %loop = call i64 @total(i64 10)
  %combined = add i64 %sum, %loop
  %ok = icmp eq i64 %combined, 78
  %status = select i1 %ok, i32 0, i32 1
  ret i32 %status
}
"""
    source = 'target triple = "arm64-apple-darwin"\n' + source
    promoted = optimize_ir(source, "mem2reg,sroa")
    assembly = tmp_path / "promoted.s"
    assembly.write_text(emit_aarch64_darwin_asm(promoted))
    binary = tmp_path / "promoted"
    root = Path(__file__).resolve().parents[2]
    linked = subprocess.run(
        [sys.executable, str(root / "scripts/pcc_link_macho.py"),
         "--out", str(binary), "--asm", str(assembly)],
        capture_output=True, text=True, timeout=20,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
    assert ran.returncode == 0, ran.stdout + ran.stderr
