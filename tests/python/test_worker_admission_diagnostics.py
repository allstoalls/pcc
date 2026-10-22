"""Opt-in admission evidence survives temporary manifests without payload data."""

import io
import shlex
import sys
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


def diagnostic_task(report='private-report-path'):
    return {
        'class': 'private-execution-class', 'inputs': [1], 'estimate_bytes': 256 * 1024 ** 2,
        'report_path': str(report), 'source_identity': 'private-source-identity',
        'diagnostic_phase': 'codegen', 'diagnostic_indices': [7],
        'diagnostic_modules': ['example.module'], 'restartable': True,
    }


@pytest.mark.parametrize('value', ['', '0', 'false', 'unexpected'])
def test_admission_diagnostic_is_opt_in(monkeypatch, capsys, value):
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', value)
    pool._resource_diagnostic(diagnostic_task(), 'start', 2, 123, 400, 800, 0)
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('value', ['1', 'true', 'yes', 'on', ' TRUE '])
def test_admission_diagnostic_is_safe_single_line_and_flushed(monkeypatch, value):
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', value)
    monkeypatch.setattr(pool.time, 'monotonic', lambda: 123.5)

    class Stream(io.StringIO):
        flushed = False
        def flush(self):
            self.flushed = True

    stream = Stream()
    monkeypatch.setattr(pool.sys, 'stderr', stream)
    task = diagnostic_task()
    task['diagnostic_modules'] = ['example.module\nforged-line']
    pool._resource_diagnostic(task, 'start', 2, 123, 400, 800, 0)
    output = stream.getvalue()
    assert len(output.splitlines()) == 1
    assert "phase='codegen' indices=[7] modules=['example.module\\nforged-line']" in output
    assert 'task=2 pid=123' in output
    assert 'reservation_bytes=400 available_bytes=800 peak_bytes=0 monotonic_s=123.5' in output
    assert 'private-' not in output
    assert stream.flushed


@pytest.mark.parametrize('result,event', [(0, 'retire'), (3, 'failed')])
def test_mock_pool_logs_start_and_terminal_mapping(tmp_path, monkeypatch, capsys, result, event):
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', '1')
    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.setattr(workers, '_coordinator_rss_bytes', lambda: 1024 ** 2)
    monkeypatch.setattr(pool, '_start_resource_worker', lambda specs, index: 123)
    monkeypatch.setattr(pool, '_poll_resource_worker', lambda pid: result)
    monkeypatch.setattr(pool, '_retire_resource_worker', lambda pid: None)
    monkeypatch.setattr(pool, '_stop_resource_worker', lambda pid: None)
    monkeypatch.setattr(pool.time, 'sleep', lambda value: None)
    monkeypatch.setattr(policy, 'read_worker_resource', lambda *args: ('complete', 10, 20))
    observations = []
    task = diagnostic_task(tmp_path / 'rss')
    def run():
        pool.run_resource_worker_processes(
            ['private-argument=secret private-executable'], [task], 1,
            1024 ** 3, observations=observations,
        )
    if result:
        with pytest.raises(subprocess.CalledProcessError):
            run()
    else:
        run()
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert 'event=start task=0 pid=123' in lines[0]
    assert 'event=' + event + ' task=0 pid=123' in lines[1]
    assert all("phase='codegen' indices=[7] modules=['example.module']" in line for line in lines)
    assert all('private-' not in line and 'secret' not in line for line in lines)
    assert len(observations) == (0 if result else 1)


def test_macos_workflow_retains_opt_in_timing_stderr():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / '.github/workflows/pcc1-package-parity.yml').read_text()
    macos = workflow.split('\n  pcc1-package-parity:\n', 1)[1]
    assert 'PCC_PY_FRONTEND_WORKER_TIMING: "1"' in macos
    assert 'build/bootstrap/stage*.process.*/target.stderr' in macos
    assert 'PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES: "4294967296"' in macos
    assert 'PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES: "2147483648"' in macos
    assert 'PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES: "536870912"' in macos
    assert 'PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES:' not in macos
    assert 'PCC_BOOTSTRAP_STAGE_TIMEOUT: "6000"' in macos


def test_resource_task_diagnostics_retain_only_assigned_modules(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline_stage1_checkpoint as checkpoint

    manifest_path = str(tmp_path / "worker.manifest")
    manifest = {
        "assigned_indices": [2, 0], "job_kind": "codegen", "ast_dir": "",
        "exports_path": "", "src_paths": ["one.py", "other.py", "three.py"],
        "module_names": ["pkg.one", "pkg.unassigned", "pkg.three"],
    }
    monkeypatch.setattr(workers, "read_worker_manifest", lambda path: manifest)
    monkeypatch.setattr(workers, "_artifact_size", lambda path: 123)
    monkeypatch.setattr(checkpoint, "file_sha256", lambda path: "a" * 64)
    items = workers.resource_tasks_for_commands([
        shlex.join([sys.executable, "--pcc-python-multi-codegen-worker", manifest_path]),
    ])
    assert items[0]["diagnostic_phase"] == "codegen"
    assert items[0]["diagnostic_modules"] == ["pkg.three", "pkg.one"]
    assert items[0]["diagnostic_indices"] == [2, 0]
    assert items[0]["estimate_bytes"] == 0
    roots = tmp_path / "roots"
    roots.write_text("pkg.one\npkg.three\n")
    items = workers.resource_tasks_for_commands([
        shlex.join([sys.executable, "--pcc-preload-delta-worker", "exports", str(roots), "output"]),
    ])
    assert items[0]["diagnostic_phase"] == "preload-delta"
    assert items[0]["diagnostic_modules"] == ["pkg.one", "pkg.three"]
    assert items[0]["diagnostic_indices"] == []
    assert items[0]["estimate_bytes"] == 0



def test_batched_diagnostic_chunks_preserve_complete_mapping(monkeypatch, capsys):
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', '1')
    task = diagnostic_task()
    task['diagnostic_modules'] = ['pkg.module' + str(i) for i in range(35)]
    task['diagnostic_indices'] = list(range(35))
    pool._resource_diagnostic(task, 'start', 0, 123, 400, 800, 0)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    for offset, line in zip((0, 16, 32), lines):
        assert 'mapping_offset=' + str(offset) in line
        assert 'module_count=35' in line
        assert 'indices=' + repr(list(range(35))[offset:offset + 16]) in line
        assert 'modules=' + repr(task['diagnostic_modules'][offset:offset + 16]) in line


def test_portable_workflow_enables_and_retains_existing_timing():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / '.github/workflows/pcc1-package-parity.yml').read_text()
    portable = workflow.split('  other-platform-native-wheel:\n', 1)[1].split('  pcc1-package-parity:\n', 1)[0]
    assert 'PCC_PY_FRONTEND_WORKER_TIMING: "1"' in portable
    assert 'build/platform-qualification/**/*.log' in portable
