"""Matched-runtime native acceptance. Host/model checks cannot replace it."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout

SOURCE = Path(__file__).resolve().parents[1] / 'fixtures/time_struct_sequence.py'
ZONES = ('UTC0', 'GMT0', 'America/New_York', 'Asia/Kolkata', 'Australia/Sydney',
         'XST-5:30XDT-6:30,M3.2.0/2,M11.1.0/2')


def run(command, environment):
    result = run_process_group_timeout(command, env=environment, timeout=30)
    return dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


@pytest.mark.parametrize('zone', ZONES)
def test_time_native_input_reference(zone):
    result = run([sys.executable, '-B', str(SOURCE)], dict(os.environ, TZ=zone))
    assert result['returncode'] == 0, result
    assert result['stdout'].endswith('OWNED_TIME_STRUCTSEQ_OK\n'), result
    assert result['stderr'] == '', result


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0','pcc1'), indirect=True)
def test_owned_time_structseq_on_observed_collectors(tmp_path, request, explicit_owned_runtime, python_program_compiler, capfd):
    binary = tmp_path / 'time_struct_sequence'
    receipt = dict(status='COMPILING', compiler=request.node.callspec.params['python_program_compiler'],
                   source_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                   runtime_sha256=hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(), executions=[])
    receipt_path = tmp_path / 'time-native-receipt.json'
    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2)+'\n')
    save()
    try:
        python_program_compiler(str(SOURCE), str(binary), backend='self', libpython_mode='off',
                                ir_scaffold_mode='on', runtime_archive=str(explicit_owned_runtime))
    except Exception as error:
        receipt.update(status='COMPILE_FAILED', error=type(error).__name__+': '+str(error))
        save()
        raise
    finally:
        output = capfd.readouterr()
        (tmp_path/'compile.stdout').write_text(output.out)
        (tmp_path/'compile.stderr').write_text(output.err)
    receipt['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    for zone in ZONES:
        expected = run([sys.executable,'-B',str(SOURCE)], dict(os.environ,TZ=zone))
        assert expected['returncode'] == 0 and expected['stderr'] == '', expected
        for collector in range(5):
            log = tmp_path / ('gc-'+str(len(receipt['executions']))+'.jsonl')
            actual = run([str(binary)], dict(os.environ,TZ=zone,PCC_GC_BACKEND=str(collector),
                         PCC_LOG='gc',PCC_LOG_FORMAT='json',PCC_LOG_FILE=str(log),
                         PCC_HOST_PYTHON='/unavailable/host-python',PATH=str(tmp_path/'no-host-tools')))
            observed = []
            if log.is_file():
                observed = sorted({event.fields['value1'] for event in parse_log_lines(log.read_text().splitlines())
                                   if event.fields.get('category') == 'gc'
                                   and event.event in ('collect_start','collect_stop','collect_end')})
            receipt['executions'].append(dict(zone=zone,requested_backend=collector,observed_backends=observed,**actual))
            receipt['status'] = 'RUNNING' if actual == expected and observed == [collector] else 'FAILED'
            save()
            assert actual == expected, receipt['executions'][-1]
            assert observed == [collector], receipt['executions'][-1]
            assert hashlib.sha256(binary.read_bytes()).hexdigest() == receipt['binary_sha256']
    receipt['status'] = 'PASS'
    save()
