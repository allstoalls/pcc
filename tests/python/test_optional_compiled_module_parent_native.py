"""A registered dotted provider need not register a compile-time parent."""
import os
import subprocess
import pytest


@pytest.mark.integration
def test_dotted_provider_without_parent_native_five_gc(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    provider = tmp_path / 'child.py'
    entry = tmp_path / 'entry.py'
    provider.write_text('def value():\n    return 7\n')
    entry.write_text('from filtered_parent.child import value\ndef main():\n    assert value() == 7\n    print("OPTIONAL_COMPILED_PARENT_OK")\nmain()\n')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    binary = tmp_path / 'optional-parent'
    compile_python_multi([str(provider), str(entry)], str(binary),
        module_names=['filtered_parent.child', 'entry'], entry_module='entry',
        backend='self', libpython_mode='off', ir_scaffold_mode='on',
        runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend), PATH='/nonexistent'))
        assert (result.returncode, result.stdout, result.stderr) == (0, 'OPTIONAL_COMPILED_PARENT_OK\n', ''), (backend, result)
