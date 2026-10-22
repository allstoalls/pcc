"""Execute declared NEW extern results with an explicitly matched runtime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_extern_owned_result_publication_executes_all_collectors(
    tmp_path, request, python_program_compiler, explicit_owned_runtime,
):
    fixture = Path(__file__).parents[1] / 'fixtures/native/extern_result_publication.py'
    source = tmp_path / 'extern_result_publication.py'
    source.write_bytes(fixture.read_bytes())
    output = tmp_path / 'extern_result_publication'
    receipt = {
        'scope': 'self backend, no libpython, declared NEW extern result execution',
        'compiler_fixture': request.node.callspec.params['python_program_compiler'],
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'runtime_archive': str(explicit_owned_runtime),
        'runtime_sha256': hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        'runs': [],
    }
    receipt_path = tmp_path / 'extern-result-execution.json'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    python_program_compiler(
        str(source), str(output), backend='self', libpython_mode='off',
        ir_scaffold_mode='on', runtime_archive=str(explicit_owned_runtime),
    )
    receipt['executable_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PATH='',
                           PCC_HOST_PYTHON='/nonexistent/host-python',
                           PCC_GC_REFCOUNT_PROVENANCE_PROBE='2')
        environment.pop('LC_ALL', None)
        result = run_process_group_timeout([str(output)], env=environment, timeout=20)
        receipt['runs'].append({'gc_backend': backend, 'returncode': result.returncode,
                                'stdout': result.stdout, 'stderr': result.stderr})
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stderr == '', (backend, result.stderr)
        marker, growth = result.stdout.split()
        assert marker == 'EXTERN_PUBLICATION_OK'
        if backend == 0:
            assert int(growth) < 65536, result.stdout
