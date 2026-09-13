"""Forwarding *values to a known bound method preserves runtime arity."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_known_method_splat_forwarding_matches_cpython(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'method_forward.py'
    source.write_text('''
class Formatter:
    def pack(self, *values):
        return values
    def forward(self, *values):
        return self.pack(*values)
formatter = Formatter()
def cached():
    return formatter
def pack(*values):
    return cached().pack(*values)
def main():
    print(formatter.pack(42), formatter.forward(42), pack(42))
    print(formatter.forward(1, 2, 3), pack(1, 2, 3))
    print(pack())
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'method_forward'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
