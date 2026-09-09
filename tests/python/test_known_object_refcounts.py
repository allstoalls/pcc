"""Compiler-proven object references preserve cleanup and diagnostic checks."""
import os
from pathlib import Path
import subprocess
import pytest

@pytest.mark.parametrize("enabled", ["0", "1"])
@pytest.mark.parametrize("verify_checks", [0, 1])
def test_known_object_cleanup_gc_backends(tmp_path: Path, monkeypatch, pcc_py_runtime_archive, enabled, verify_checks):
    from pcc.py_frontend.pipeline import compile_python
    monkeypatch.setenv("PCC_KNOWN_OBJECT_REFS", enabled)
    source = tmp_path / "known_objects.py"
    source.write_text('''import gc
import weakref
from pcc.extern import extern, c_int64, c_void
verify = extern("pcc_gc_set_known_ref_checks", (c_int64,), c_void)
metric = extern("pcc_gc_telemetry", (c_int64,), c_int64)
events = []
saved = None
class Payload:
    def __init__(self, value: int):
        self.value = value
    def __del__(self):
        events.append(self.value)
class Resurrect:
    def __init__(self):
        self.value = 99
    def __del__(self):
        global saved
        saved = self
def make(value: int):
    return Payload(value)
def sequence():
    item = make(4)
    yield item
    item = make(5)
    yield item
def flow():
    value = make(1)
    alias = value
    reference = weakref.ref(alias)
    value = make(2)
    gc.collect()
    if reference() is not alias or alias.value != 1:
        raise RuntimeError("alias lost")
    try:
        value = make(3)
        raise ValueError("expected")
    except ValueError:
        pass
    iterator = sequence()
    first = next(iterator)
    second = next(iterator)
    if first.value != 4 or second.value != 5:
        raise RuntimeError("generator owner lost")
def resurrect():
    value = Resurrect()
def main():
    verify(VERIFY_CHECKS)
    flow()
    resurrect()
    gc.collect()
    if saved.value != 99:
        raise RuntimeError("resurrection lost")
    if metric(116) != 0:
        raise RuntimeError("non-object reached reference counting")
    print(sorted(events))
main()
'''.replace("VERIFY_CHECKS", str(verify_checks)))
    binary = tmp_path / "known_objects"
    ir = tmp_path / "known_objects.ll"
    options = dict(backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    compile_python(str(source), str(ir), emit_llvm_only=True, **options)
    text = ir.read_text()
    has_known = any("call " in line and "@pcc_gc_release_known(" in line for line in text.splitlines())
    assert has_known == (enabled == "1")
    has_frame = any("call " in line and "@py_gen_frame_set(" in line for line in text.splitlines())
    assert has_frame == (enabled == "1")
    compile_python(str(source), str(binary), **options)
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ,PCC_GC_BACKEND=str(backend)), capture_output=True,text=True,timeout=20)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout.strip() == "[1, 2, 3, 4, 5]"


def test_runtime_port_generator_keeps_checked_frame_access(tmp_path: Path, monkeypatch):
    from pcc.py_frontend.pipeline import compile_python
    monkeypatch.setenv("PCC_KNOWN_OBJECT_REFS", "1")
    source = tmp_path / "raw_frame.py"
    source.write_text('''__pcc_runtime_port__ = True
from pcc.unsafe import malloc, free
def sequence():
    pointer = malloc(16)
    yield pointer
    free(pointer)
''')
    output = tmp_path / "raw_frame.ll"
    compile_python(str(source), str(output), backend="self",libpython_mode="off",ir_scaffold_mode="on",emit_llvm_only=True)
    text = output.read_text()
    assert not any("call " in line and "@py_gen_frame_set(" in line for line in text.splitlines())
    assert any("call " in line and "@py_list_set(" in line for line in text.splitlines())
