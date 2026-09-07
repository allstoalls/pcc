"""Runtime optimizer policy, emitted behavior and cache identity."""

from pathlib import Path
import subprocess

from llvmlite import binding as llvm

from pcc.tools import ir_to_obj
from pcc.tools import runtime_archive_provenance as provenance


def test_only_qualified_runtime_modules_select_full_optimization(tmp_path):
    root = tmp_path / "runtime"
    for name in ("py_obj.py", "py_list.py", "py_gen.py", "py_gc_backend.py",
                 "freestanding_gc_index_table.py"):
        assert ir_to_obj.runtime_optimization_level(root / "py" / name, root) == 2
    for name in ("freestanding_allocator.py", "freestanding_mem_str.py", "py_str.py"):
        assert ir_to_obj.runtime_optimization_level(root / "py" / name, root) == 0
    assert ir_to_obj.runtime_optimization_level(tmp_path / "py_obj.py", root) == 0
    assert ir_to_obj.runtime_optimization_level(root / "py" / "py_obj.py", None) == 0


def test_optimizer_removes_helper_stack_work_and_preserves_result(tmp_path):
    source = '''define internal void @prepare(ptr %p) {
entry:
  store i64 42, ptr %p
  ret void
}
define i64 @execute() {
entry:
  %p = alloca i64
  call void @prepare(ptr %p)
  %v = load i64, ptr %p
  ret i64 %v
}
'''
    driver = tmp_path / "driver.c"
    driver.write_text('extern long long execute(void);\nint main(void) { return execute() == 42 ? 0 : 1; }\n')
    symbols = []
    for level in (0, 2):
        obj, _ = ir_to_obj._emit_object_with_triple(source, optimization_level=level)
        object_path = tmp_path / ("level" + str(level) + ".o")
        object_path.write_bytes(obj)
        symbols.append(subprocess.check_output(["nm", str(object_path)], text=True, timeout=10))
        executable = object_path.with_suffix("")
        subprocess.run(["clang", str(driver), str(object_path), "-o", str(executable)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run([str(executable)], check=True, capture_output=True, timeout=10)
    assert "prepare" in symbols[0]
    assert "prepare" not in symbols[1]


def test_runtime_receipts_invalidate_when_emitter_policy_changes(monkeypatch):
    monkeypatch.setattr(provenance, "_CODEGEN_CHECKSUM_CACHE", {})
    monkeypatch.setattr(ir_to_obj, "runtime_emitter_identity", lambda: "first")
    before = provenance.codegen_checksum()
    provenance._CODEGEN_CHECKSUM_CACHE.clear()
    monkeypatch.setattr(ir_to_obj, "runtime_emitter_identity", lambda: "second")
    after = provenance.codegen_checksum()
    assert before != after
    assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": before}]})
