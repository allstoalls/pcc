"""Emitted execution of actual positional providers and production writer.

Run only with an explicitly supplied source-matched owned runtime; no implicit
provisioning. The writer control imports the actual production implementation.
"""
import os
from pathlib import Path
import subprocess
import pytest

CONTROLS = Path(__file__).resolve().parents[1] / 'fixtures' / 'owned_output_writer'

@pytest.mark.integration
@pytest.mark.parametrize('name,expected', [
    ('native_positional_providers', 'OWNED_POSITIONAL_PROVIDERS_OK\n'),
    ('native_output_writer', 'OWNED_OUTPUT_WRITER_OK\n'),
])
def test_owned_output_writer_changed_shape_all_collectors(
        tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name, expected):
    source = CONTROLS / (name + '.py')
    binary = tmp_path / name
    monkeypatch.setenv('PCC_TEST_NO_NATIVE_PROVISIONING', '1')
    monkeypatch.setenv('PCC_NO_AUTO_PCC1', '1')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    python_program_compiler(str(source), str(binary), backend='self', libpython_mode='off',
                            ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        path = tmp_path / ('gc' + str(backend))
        result = subprocess.run([str(binary), str(path)], capture_output=True, text=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, ''), (
            backend, result.returncode, result.stdout, result.stderr)
        assert not path.exists()
