"""Runtime isinstance checks borrow their operand and release temporary owners."""
import os
import subprocess

import pytest


@pytest.mark.parametrize("payload", ["objects", "scalars"])
@pytest.mark.parametrize("classinfo", ["list", "(list, dict)"])
def test_isinstance_releases_attribute_operand(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, classinfo, payload,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "isinstance_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
destroyed = 0
class Item:
    def __del__(self):
        global destroyed
        destroyed += 1
class Holder:
    def __init__(self, values):
        self.values = values
def check(holder):
    return isinstance(holder.values, CLASSINFO)
def run():
    values = PAYLOAD
    holder = Holder(values)
    assert check(holder)
    assert len(values) == LENGTH
    gc.collect()
    assert len(values) == LENGTH
def main():
    run()
    gc.collect()
    gc.collect()
    assert destroyed == EXPECTED
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert destroyed == EXPECTED * 2
    print(destroyed, heap() - before)
main()
'''.replace("CLASSINFO", classinfo)
       .replace("PAYLOAD", "[Item()]" if payload == "objects" else "[None] * 131072")
       .replace("LENGTH", "1" if payload == "objects" else "131072")
       .replace("EXPECTED", "1" if payload == "objects" else "0"))
    binary = tmp_path / "isinstance_owner"
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        destroyed, growth = map(int, ran.stdout.split())
        assert destroyed == (2 if payload == "objects" else 0)
        if backend == 0:
            assert growth < 16384, growth
