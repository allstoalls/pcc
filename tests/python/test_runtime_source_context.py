"""Explicit runtime roots retain the compile-time ABI context of their sources."""
import os

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.frontends.python import pipeline


@pytest.mark.parametrize('configured', (False, True))
def test_runtime_library_source_uses_explicit_root_without_accepting_neighbors(tmp_path, monkeypatch, configured):
    canonical = tmp_path / 'compiler' / 'pcc' / 'runtime'
    relocated = tmp_path / 'separate' / 'runtime'
    monkeypatch.setattr(pipeline, '_PY_RUNTIME_DIR', str(canonical))
    if configured:
        monkeypatch.setenv('PCC_RUNTIME_DIR', str(relocated))
    else:
        monkeypatch.delenv('PCC_RUNTIME_DIR', raising=False)
    assert pipeline._is_py_runtime_library_source(str(canonical / 'py' / 'module.py'))
    assert pipeline._is_py_runtime_library_source(str(relocated / 'py' / 'module.py')) is configured
    assert not pipeline._is_py_runtime_library_source(str(relocated / 'py-other' / 'module.py'))
    assert not pipeline._is_py_runtime_library_source(str(relocated / 'neighbor.py'))


@pytest.mark.parametrize('previous', (None, '/previous/runtime'))
@pytest.mark.parametrize('fail', (False, True))
def test_owned_builder_scopes_explicit_source_context_and_restores_it(tmp_path, monkeypatch, previous, fail):
    source = tmp_path / 'runtime' / 'py' / 'module.py'
    source.parent.mkdir(parents=True)
    source.write_text('')
    if previous is None:
        monkeypatch.delenv('PCC_RUNTIME_DIR', raising=False)
    else:
        monkeypatch.setenv('PCC_RUNTIME_DIR', previous)
    observed = []

    def compile_member(input_path, output_path, **options):
        observed.append(input_path)
        assert options['python_library'] and options['emit_llvm_only']
        assert os.environ['PCC_RUNTIME_DIR'] == str(source.parent.parent)
        assert pipeline._is_py_runtime_library_source(input_path)
        if fail:
            raise ValueError('controlled compile failure')

    monkeypatch.setattr(pipeline, 'compile_python', compile_member)
    if fail:
        with pytest.raises(ValueError, match='controlled compile failure'):
            owned._compile_runtime_module('module', str(source), str(tmp_path / 'out.ll'), 'x86_64-unknown-linux-gnu')
    else:
        owned._compile_runtime_module('module', str(source), str(tmp_path / 'out.ll'), 'x86_64-unknown-linux-gnu')
    assert observed == [str(source)]
    assert os.environ.get('PCC_RUNTIME_DIR') == previous
