"""Object parameters that are rebound acquire normal local ownership."""
import os
import subprocess

import pytest


@pytest.mark.parametrize("kind", ["function", "method", "classmethod"])
def test_rebound_parameter_releases_values_on_return_and_error(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, kind,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    body = '''if rows is None:
    rows = holder.values
if fail:
    raise ValueError("probe")
return len(rows)'''
    if kind == "function":
        definition = "def consume(holder, rows=None, fail=False):\n" + "\n".join("    " + line for line in body.splitlines())
        call = "consume"
    else:
        receiver = "self" if kind == "method" else "cls"
        decorator = "    @classmethod\n" if kind == "classmethod" else ""
        definition = "class Consumer:\n" + decorator + "    def consume(" + receiver + ", holder, rows=None, fail=False):\n" + "\n".join("        " + line for line in body.splitlines())
        call = "Consumer().consume" if kind == "method" else "Consumer.consume"
    source = tmp_path / "rebound_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
destroyed = 0
class Item:
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
class Holder:
    def __init__(self, values):
        self.values = values
DEFINITION
def run():
    holder = Holder([Item()] + [None] * 32768)
    borrowed = [Item()]
    assert CALL(holder, borrowed) == 1
    assert len(borrowed) == 1
    assert CALL(holder) == 32769
    try:
        CALL(holder, fail=True)
    except ValueError:
        pass
    else:
        raise AssertionError("missing error")
    assert len(borrowed) == 1
def main():
    run()
    gc.collect()
    gc.collect()
    assert destroyed == 2
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert destroyed == 4
    print(destroyed, heap() - before)
main()
'''.replace("DEFINITION", definition).replace("CALL", call))
    binary = tmp_path / "rebound_owner"
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        destroyed, growth = map(int, ran.stdout.split())
        assert destroyed == 4
        if backend == 0:
            assert growth < 16384, growth


def test_rebound_parameter_early_return_self_assignment_and_loop(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "parameter_edges.py"
    source.write_text('''import gc
created = 0
destroyed = 0
class Item:
    def __init__(self, value):
        global created
        created += 1
        self.value = value
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
def replace(value, mode):
    if mode == 0:
        return value
    for index in range(3):
        if mode == 1:
            value = value
        else:
            value = Item(index)
        gc.collect()
    if mode == 3:
        raise ValueError("loop failed")
    return value
def run():
    original = Item(99)
    result = replace(original, 0)
    result.value = 100
    assert original.value == 100
    result = None
    result = replace(original, 1)
    assert result.value == 100
    result = None
    result = replace(original, 2)
    assert result.value == 2
    assert original.value == 100
    result = None
    try:
        replace(original, 3)
    except ValueError:
        pass
    else:
        raise AssertionError("missing error")
    gc.collect()
    assert created - destroyed == 1
    assert original.value == 100
def main():
    run()
    gc.collect()
    gc.collect()
    assert created == destroyed == 7
    print("parameter-edges-ok")
main()
''')
    binary = tmp_path / "parameter_edges"
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "parameter-edges-ok"
