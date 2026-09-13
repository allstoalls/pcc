"""type(value).__name__ evaluates value and uses its actual runtime type."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python_multi


def test_type_name_preserves_calls_errors_and_dynamic_default(tmp_path, pcc_py_runtime_archive):
    owner = tmp_path / 'type_names.py'
    owner.write_text('''def runtime_name(value=None):
    return type(value).__name__
''', encoding='utf-8')
    source = tmp_path / 'type_name_entry.py'
    source.write_text('''from type_names import runtime_name
import gc
calls = []
class Node:
    pass
class Temporary:
    def __del__(self):
        calls.append("finalized")
def temporary():
    return Temporary()
def produce() -> int:
    calls.append("produce")
    return 42
def fail() -> int:
    calls.append("fail")
    raise ValueError("expected")
def main():
    print(runtime_name(Node()), runtime_name(), runtime_name(7))
    print(type(produce()).__name__, calls)
    try:
        print(type(fail()).__name__)
    except ValueError:
        print("caught", calls)
    name = type(temporary()).__name__
    gc.collect()
    print(name, calls)
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'type_name_entry'
    compile_python_multi([str(owner), str(source)], str(binary),
                         module_names=['type_names', 'type_name_entry'], entry_module='type_name_entry',
                         backend='self', libpython_mode='off', runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
