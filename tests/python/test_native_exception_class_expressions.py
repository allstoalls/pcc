"""Except clauses use the evaluated exception class, including module attrs."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python_multi


def test_module_and_local_exception_class_selection(tmp_path, pcc_py_runtime_archive):
    errors = tmp_path / 'error_types.py'
    errors.write_text('''class First(Exception):
    pass
class Second(Exception):
    pass
class FailingConstructor:
    def __init__(self):
        raise First("constructor")
def trigger(first):
    if first:
        raise First("first")
    raise Second("second")
''', encoding='utf-8')
    source = tmp_path / 'error_use.py'
    source.write_text('''import error_types
def main():
    for first in [True, False]:
        try:
            error_types.trigger(first)
        except error_types.First:
            print("first")
        except error_types.Second:
            print("second")
    selected = error_types.Second
    try:
        error_types.trigger(False)
    except selected:
        print("selected")
    try:
        error_types.FailingConstructor()
    except error_types.First:
        print("constructor")
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'error_use'
    compile_python_multi([str(errors), str(source)], str(binary),
                         module_names=['error_types', 'error_use'], entry_module='error_use',
                         backend='self', libpython_mode='off', runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
