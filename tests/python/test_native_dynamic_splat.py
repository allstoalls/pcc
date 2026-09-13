"""Dynamic callable arguments normalize starred iterables to the tuple ABI."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_dynamic_starred_iterables_match_cpython(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'dynamic_splat.py'
    source.write_text('''
import gc
events = []
def total(a, b, offset=0):
    return a + b + offset
def invoke(fn, values):
    return fn(*values)
def invoke_kw(fn, values):
    return fn(*values, offset=1)
def numbers():
    yield 20
    yield 22
class Items:
    def __iter__(self):
        gc.collect()
        return iter([20, 22])
class Box:
    def __init__(self, *values):
        self.values = values
class Callable:
    def __call__(self, a, b):
        return a + b
def noargs():
    events.append("called")
def broken():
    try:
        yield 1
        raise ValueError("iteration failed")
    finally:
        events.append("closed")

def main():
    print(invoke(total, [20, 22]), invoke(total, (20, 22)))
    print(invoke(total, numbers()), invoke(total, Items()))
    print(invoke_kw(total, [20, 22]))
    print(invoke(Box, [20, 22]).values)
    print(invoke(Callable(), [20, 22]))
    for i in range(20):
        assert invoke(total, [i, 1]) == i + 1
    for value in [42, None]:
        try:
            invoke(noargs, value)
        except TypeError:
            print("not iterable")
    try:
        invoke(noargs, broken())
    except ValueError:
        print("iteration failed")
    gc.collect()
    print(events)
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'dynamic_splat'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
