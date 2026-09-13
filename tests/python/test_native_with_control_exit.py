"""Native context exits run once, in order, on return/break/continue."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_with_control_exit_order_and_exit_exception(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'with_control.py'
    source.write_text('''
class Context:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
    def __enter__(self):
        print("enter", self.name)
        return self
    def __exit__(self, et, ev, tb):
        print("exit", self.name, et is None)
        if self.fail:
            raise ValueError("exit failed")
        return False
def returning():
    with Context("outer"), Context("inner"):
        try:
            return 42
        finally:
            print("finally")
def exit_raises():
    with Context("outer"), Context("inner", True):
        return 10
def main():
    print(returning())
    for i in range(3):
        with Context("loop"):
            if i == 0:
                continue
            break
    try:
        exit_raises()
    except ValueError:
        print("caught")
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'with_control'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
