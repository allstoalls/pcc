"""A dynamic receiver selects dict/set update without hiding user methods."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_dynamic_update_preserves_receiver_semantics(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'dynamic_update.py'
    source.write_text('''
def apply(target, values):
    return target.update(values)
class Recorder:
    def update(self, values):
        return ("custom", values)
def main():
    mapping = {"old": 1}
    members = {1}
    print(apply(mapping, [("new", 2)]), mapping)
    print(apply(members, [2, 3]), sorted(members))
    print(apply(Recorder(), 7))
    try:
        apply(mapping, [("before", 3), ("bad",)])
    except ValueError:
        print(mapping)
    try:
        apply(members, [[]])
    except TypeError:
        print(sorted(members))
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'dynamic_update'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
