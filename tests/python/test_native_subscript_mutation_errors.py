"""Python subscript mutation must translate primitive misses into exceptions."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_subscript_mutation_errors_and_wide_delete_keys(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'mutation_errors.py'
    source.write_text('''
class Empty:
    pass
class DictChild(dict):
    pass
class Failing:
    def __setitem__(self, key, value):
        raise ValueError("set failed")
    def __delitem__(self, key):
        raise ValueError("delete failed")
def assign(target, key):
    target[key] = 7
def erase(target, key):
    del target[key]
def main():
    for target in [Empty(), (1, 2), None]:
        try:
            assign(target, "key")
            print("assignment ignored")
        except TypeError:
            print("assignment rejected")
        try:
            erase(target, "key")
            print("deletion ignored")
        except TypeError:
            print("deletion rejected")
    key = (1 << 80) + 19
    data = {key: 3}
    erase(data, (1 << 80) + 19)
    print(data)
    try:
        erase(data, key)
    except KeyError as exc:
        print(exc.args[0] == key)
    try:
        erase(DictChild(), key)
    except KeyError as exc:
        print(exc.args[0] == key)
    try:
        assign(Failing(), 1)
    except ValueError as exc:
        print(str(exc))
    try:
        erase(Failing(), 1)
    except ValueError as exc:
        print(str(exc))
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'mutation_errors'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
