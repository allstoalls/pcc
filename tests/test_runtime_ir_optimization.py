"""Runtime optimizer policy, emitted behavior and cache identity."""

from pathlib import Path
import subprocess
import sys

from llvmlite import binding as llvm

from pcc.tools import ir_to_obj
from pcc.tools import runtime_archive_provenance as provenance


def test_receipt_freshness_does_not_load_llvm():
    source = '''import importlib.abc
import sys
class BlockLLVM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "llvmlite" or fullname.startswith("llvmlite."):
            raise ImportError("LLVM imports forbidden by dependency gate")
sys.meta_path.insert(0, BlockLLVM())
from pcc.tools.runtime_archive_provenance import codegen_checksum
value = codegen_checksum()
assert len(value) == 64, value
assert not any(name == "llvmlite" or name.startswith("llvmlite.") for name in sys.modules)
print("llvm-free-receipt-check-ok")
'''
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "llvm-free-receipt-check-ok"


def test_default_runtime_emission_does_not_enable_llvm_o2(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    (root / "py").mkdir(parents=True)
    ir_path = root / "module.ll"
    ir_path.write_text('define i64 @execute() {\nentry:\n ret i64 42\n}\n')
    original = ir_to_obj._emit_object_with_triple
    levels = []
    def observe(text, **options):
        levels.append(options.get("optimization_level", 0))
        return original(text, **options)
    monkeypatch.setattr(ir_to_obj, "_emit_object_with_triple", observe)
    for name in ("py_obj.py", "py_list.py", "py_gen.py", "py_gc_backend.py",
                 "freestanding_gc_index_table.py"):
        source = root / "py" / name
        source.write_text('def execute():\n    return 42\n')
        obj = root / (name + ".o")
        assert ir_to_obj.main([str(ir_path), str(obj), "--source", str(source),
            "--runtime-root", str(root), "--provenance", str(obj) + ".provenance.json"]) == 0
    assert levels == [0] * 5


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
    monkeypatch.setattr(provenance, "_runtime_emitter_source_identity", lambda: "first")
    before = provenance.codegen_checksum()
    provenance._CODEGEN_CHECKSUM_CACHE.clear()
    monkeypatch.setattr(provenance, "_runtime_emitter_source_identity", lambda: "second")
    after = provenance.codegen_checksum()
    assert before != after
    assert provenance.manifest_is_stale_for_current_codegen({"members": [{"codegen_checksum": before}]})
