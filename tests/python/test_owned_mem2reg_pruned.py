"""Promoted slots need PHIs only where an incoming value can reach a load."""

from pathlib import Path
import platform
import subprocess
import sys

import pytest

from pcc.native_ir.mem2reg import mem2reg_text


_OVERWRITTEN = """define i64 @pick(i1 %condition) {
entry:
  %slot = alloca i64
  br i1 %condition, label %left, label %right
left:
  store i64 11, ptr %slot
  br label %merge
right:
  store i64 22, ptr %slot
  br label %merge
merge:
  store i64 7, ptr %slot
  %value = load i64, ptr %slot
  ret i64 %value
}
"""

_LOOP_LOCAL = """declare void @observe(i64)
define i64 @loop(i64 %limit) {
entry:
  %slot = alloca i64
  store i64 0, ptr %slot
  br label %head
head:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %more = icmp slt i64 %index, %limit
  br i1 %more, label %body, label %done
body:
  store i64 %index, ptr %slot
  %value = load i64, ptr %slot
  call void @observe(i64 %value)
  %next = add i64 %index, 1
  br label %head
done:
  ret i64 %index
}
"""


def test_overwrite_before_load_needs_no_incoming_phi():
    result, changed = mem2reg_text(_OVERWRITTEN)
    assert changed
    assert "alloca" not in result
    assert " = phi " not in result
    assert "ret i64 7" in result


def test_load_before_overwrite_retains_the_incoming_phi():
    source = _OVERWRITTEN.replace(
        "  store i64 7, ptr %slot\n  %value = load i64, ptr %slot",
        "  %value = load i64, ptr %slot\n  store i64 7, ptr %slot",
    )
    result, changed = mem2reg_text(source)
    assert changed
    assert "alloca" not in result
    assert result.count(" = phi i64 ") == 1
    assert "[ 11, %left ]" in result
    assert "[ 22, %right ]" in result


def test_loop_local_value_does_not_get_a_loop_carried_phi():
    result, changed = mem2reg_text(_LOOP_LOCAL)
    assert changed
    assert "alloca" not in result
    assert result.count(" = phi i64 ") == 1  # The original index only.
    assert "call void @observe(i64 %index)" in result


def test_unread_stores_do_not_create_a_dead_phi_cycle():
    source = _LOOP_LOCAL.replace(
        "  %value = load i64, ptr %slot\n  call void @observe(i64 %value)\n", "",
    )
    result, changed = mem2reg_text(source)
    assert changed
    assert "alloca" not in result
    assert "slot.phi" not in result
    assert result.count(" = phi i64 ") == 1


@pytest.mark.skipif(sys.platform != "darwin" or platform.machine() != "arm64",
                    reason="executes owned Mach-O/AArch64 output")
def test_pruned_overwrite_and_loop_paths_execute(tmp_path):
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm

    read_first = _OVERWRITTEN.replace("@pick", "@read_first").replace(
        "  store i64 7, ptr %slot\n  %value = load i64, ptr %slot",
        "  %value = load i64, ptr %slot\n  store i64 7, ptr %slot",
    )
    loop = _LOOP_LOCAL.replace("declare void @observe(i64)\n", "")
    source = 'target triple = "arm64-apple-darwin"\n' + _OVERWRITTEN + read_first + loop
    source += '''
@observed = global i64 0
define void @observe(i64 %value) {
entry:
  %old = load i64, ptr @observed
  %next = add i64 %old, %value
  store i64 %next, ptr @observed
  ret void
}
define i32 @main() {
entry:
  %overwritten = call i64 @pick(i1 true)
  %left = call i64 @read_first(i1 true)
  %right = call i64 @read_first(i1 false)
  %count = call i64 @loop(i64 10)
  %seen = load i64, ptr @observed
  %a = add i64 %overwritten, %left
  %b = add i64 %a, %right
  %c = add i64 %b, %count
  %total = add i64 %c, %seen
  %ok = icmp eq i64 %total, 95
  %status = select i1 %ok, i32 0, i32 1
  ret i32 %status
}
'''
    promoted, changed = mem2reg_text(source)
    assert changed
    assembly = tmp_path / "pruned.s"
    assembly.write_text(emit_aarch64_darwin_asm(promoted))
    binary = tmp_path / "pruned"
    root = Path(__file__).resolve().parents[2]
    linked = subprocess.run(
        [sys.executable, str(root / "scripts/pcc_link_macho.py"),
         "--out", str(binary), "--asm", str(assembly)],
        capture_output=True, text=True, timeout=20,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
    assert ran.returncode == 0, ran.stdout + ran.stderr
