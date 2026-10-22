"""Execute live imported-binding coherence and lifetimes on all five GCs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.python.test_live_import_namespace_rebinding import (
    SCENARIOS,
    run_reference,
    write_sources,
)


pytestmark = pytest.mark.integration


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_live_import_namespace_native_five_gc(tmp_path, monkeypatch, pcc_runtime_archive, scenario):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.tools.runtime_archive_provenance import manifest_is_stale_for_current_codegen

    # The shared archive fixture verifies runtime source and archive members;
    # require its compiler identity to match this codegen as well.
    manifest = json.loads(Path(str(pcc_runtime_archive) + '.provenance.json').read_text())
    assert not manifest_is_stale_for_current_codegen(manifest), 'runtime/codegen identity mismatch'
    for name, value in {
        'PCC_PYTHON_IR_PASSES': 'off',
        'PCC_PY_FRONTEND_JOBS': '1',
        'PCC_SELF_BACKEND_JOBS': '1',
        'PCC_NO_AUTO_PCC1': '1',
        'PCC_SELF_LINK': 'pcc',
        'PCC_SELF_BACKEND_OBJECT_CACHE': '0',
        'PCC_PY_FRONTEND_IR_CACHE': '0',
        'PCC_RUNTIME_ARCHIVE': str(pcc_runtime_archive),
    }.items():
        monkeypatch.setenv(name, value)
    paths, names, expected = write_sources(tmp_path, scenario)
    run_reference(tmp_path, paths, expected)
    binary = tmp_path / ('live-import-' + scenario)
    compile_python_multi(
        paths, str(binary), module_names=names, entry_module='live_entry',
        backend='self', libpython_mode='off', ir_scaffold_mode='on',
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PATH='/nonexistent')
        environment.pop('LC_ALL', None)
        result = subprocess.run(
            [str(binary)], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30,
        )
        (tmp_path / ('gc' + str(backend) + '.stdout')).write_text(result.stdout)
        (tmp_path / ('gc' + str(backend) + '.stderr')).write_text(result.stderr)
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, ''), (
            scenario, backend, result.stdout, result.stderr,
        )
