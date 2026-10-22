"""Execute imported constructor integer boundaries with a supplied runtime."""
import os
import subprocess

import pytest

pytestmark = pytest.mark.integration

PROVIDER_SOURCE = '''class Box:
    def __init__(self, count: int = 18446744073709551615):
        self.count = count
class Failing:
    def __init__(self, count: int):
        raise ValueError('constructor failure')
'''

ENTRY_SOURCE = '''import count_provider as provider
def main():
    values = ['a', 'b', 'c']
    assert provider.Box(len(values)).count == 3
    assert provider.Box(1 << 100).count == 1 << 100
    assert provider.Box().count == 18446744073709551615
    assert provider.Box(count=-(1 << 100)).count == -(1 << 100)
    caught = 0
    try:
        provider.Failing(len(values))
    except ValueError:
        caught = 1
    assert caught == 1
    assert provider.Box(len(values)).count == 3
    print('IMPORTED_CONSTRUCTOR_ABI_OK')
main()
'''


def test_imported_constructor_integer_native_five_gc(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    provider = tmp_path / 'count_provider.py'
    entry = tmp_path / 'entry.py'
    binary = tmp_path / 'imported_constructor'
    provider.write_text(PROVIDER_SOURCE)
    entry.write_text(ENTRY_SOURCE)
    compile_python_multi([str(provider), str(entry)], str(binary),
                         module_names=['count_provider', 'entry'], entry_module='entry',
                         backend='self', libpython_mode='off', ir_scaffold_mode='on',
                         runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PATH='/nonexistent', PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'IMPORTED_CONSTRUCTOR_ABI_OK\n'
        assert result.stderr == ''
