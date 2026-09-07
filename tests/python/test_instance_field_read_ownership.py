"""Instance getters return owned references, including dynamically typed fields."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("consumer", ["untouched", "bound", "iterated"])
def test_dynamic_field_read_releases_its_owner(tmp_path, pcc_py_runtime_archive, consumer):
    from pcc.py_frontend.pipeline import compile_python

    action = {"untouched": "pass", "bound": "values = holder.values",
              "iterated": "for item in holder.values:\n        pass"}[consumer]
    source = tmp_path / "field_owner.py"
    source.write_text('''import gc
events = []
class Item:
    def __del__(self):
        events.append("released")
class Holder:
    def __init__(self, values):
        self.values = values
def exercise():
    item = Item()
    original = [item]
    holder = Holder(original)
    ''' + action + '''
exercise()
gc.collect()
print(events)
''')
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, check=True, timeout=10)
    source.write_text('from pcc.extern import c_int64, extern\n'
                      'backend = extern("pcc_gc_backend", (), c_int64)\n'
                      'print(backend())\n' + source.read_text())
    executable = tmp_path / "field_owner"
    compile_python(str(source), str(executable), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(executable)], capture_output=True, text=True,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)), timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == str(backend) + "\n" + oracle.stdout


@pytest.mark.parametrize("exit_kind", ["exhausted", "break", "return", "error", "close"])
def test_owned_field_iterator_survives_suspension_and_releases_on_exit(
    tmp_path, pcc_py_runtime_archive, exit_kind,
):
    from pcc.py_frontend.pipeline import compile_python

    action = {"exhausted": "pass", "break": "break", "return": "return",
              "error": 'raise ValueError("stop")', "close": "pass"}[exit_kind]
    source = tmp_path / "suspended_field.py"
    source.write_text('''import gc
events = []
class Item:
    def __del__(self):
        events.append("released")
class Holder:
    def __init__(self, values):
        self.values = values
def items():
    item = Item()
    original = [item]
    holder = Holder(original)
    for value in holder.values:
        gc.collect()
        yield value
        ''' + action + '''
def exercise():
    iterator = items()
    value = next(iterator)
    gc.collect()
    print(events)
    ''' + ('iterator.close()' if exit_kind == "close" else '''try:
        next(iterator)
    except (StopIteration, ValueError):
        pass''') + '''
exercise()
gc.collect()
print(events)
''')
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, check=True, timeout=10)
    source.write_text('from pcc.extern import c_int64, extern\n'
                      'backend = extern("pcc_gc_backend", (), c_int64)\n'
                      'print(backend())\n' + source.read_text())
    executable = tmp_path / "suspended_field"
    compile_python(str(source), str(executable), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_py_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(executable)], capture_output=True, text=True,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)), timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == str(backend) + "\n" + oracle.stdout
