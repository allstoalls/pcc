"""Independent codegen ordering never weakens byte admission or task identity."""

import copy
import subprocess

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy

MIB = 1024 ** 2


def task(size, index=0, export=10 ** 8):
    return {'class': 'same-private-invocation', 'inputs': [size, size, size, size, export, 1],
            'estimate_bytes': 0, 'report_path': 'unused', 'restartable': True,
            'diagnostic_phase': 'codegen', 'diagnostic_indices': [index],
            'diagnostic_modules': ['module' + str(index)]}


def test_bands_small_first_largest_within_band_exactly_once():
    tasks = [task(n, i) for i, n in enumerate([16, 3, 2, 8, 3, 0])]
    before = copy.deepcopy(tasks)
    order, bands = policy.resource_task_order(tasks)
    assert order == [5, 2, 1, 4, 3, 0]
    assert bands == [12, 7, 6, 10, 7, 0]
    assert sorted(order) == list(range(len(tasks)))
    assert tasks == before
    assert policy.resource_task_order(tasks) == (order, bands)
    # Full-export size must not collapse every singleton into one band.
    for item in tasks:
        item['inputs'][4] = 10 ** 12
    assert policy.resource_task_order(tasks) == (order, bands)


@pytest.mark.parametrize('change', ['phase', 'class', 'batch', 'schema', 'dependency', 'lazy', 'negative', 'nonsingleton', 'export-view'])
def test_unknown_inventory_preserves_old_order_and_readiness(change):
    tasks = [task(8), task(2)]
    if change == 'phase': tasks[0]['diagnostic_phase'] = 'export'
    elif change == 'class': tasks[0]['class'] = 'other'
    elif change == 'batch': tasks[0]['diagnostic_indices'] = [0, 1]
    elif change == 'schema': tasks[0]['inputs'] = [8]
    elif change == 'dependency': tasks[0]['depends_on'] = 1
    elif change == 'lazy': tasks[0]['input_path'] = 'future-input'
    elif change == 'negative': tasks[0]['inputs'][0] = -1
    elif change == 'export-view': tasks[0]['inputs'][4] = 17
    else: tasks[0]['inputs'][5] = 2
    expected = sorted(range(len(tasks)), key=lambda i: (-sum(tasks[i]['inputs']), i))
    order, bands = policy.resource_task_order(tasks)
    assert order == expected and bands == []
    ready = [1, 0]
    assert policy.ready_resource_cohort(ready, order, [], bands) is ready


def test_active_cohort_drains_before_next_cohort_readiness():
    tasks = [task(2), task(3), task(16)]
    order, bands = policy.resource_task_order(tasks)
    assert policy.ready_resource_cohort([2], [2], [0, 1], bands) == []
    assert policy.ready_resource_cohort([2], [2], [1], bands) == []
    assert policy.ready_resource_cohort([2], [2], [], bands) == [2]


def test_oversized_forecast_cannot_skip_lower_band_or_lower_maximum():
    tasks = [task(2), task(16)]
    order, bands = policy.resource_task_order(tasks)
    observations = [(tasks[0]['class'], list(tasks[0]['inputs']), 300 * MIB)]
    available = 256 * MIB
    # The baseline chooser prefers the unknown higher band over forecast-only.
    assert policy.choose_task(order, tasks, observations, [], 2, available, True)[0] == 1
    ready = policy.ready_resource_cohort(order, order, [], bands)
    index, demand, exclusive = policy.choose_task(ready, tasks, observations, [], 2, available, True)
    assert index == 0 and exclusive
    assert demand == policy.peak_reservation(300 * MIB) > available
    # No reduction to MAX observations or explicit/incomplete floors.
    observations.append((tasks[0]['class'], list(tasks[0]['inputs']), 10 * MIB))
    assert policy.estimated_task_bytes(tasks[0], observations) == demand
    tasks[0]['incomplete_peak_bytes'] = 400 * MIB
    assert policy.minimum_task_bytes(tasks[0]) == 400 * MIB
    assert policy.estimated_task_bytes(tasks[0], observations) == policy.peak_reservation(400 * MIB)


def mock_pool(tmp_path, monkeypatch, failure=False, growth=False):
    # Keep the original overlapping/growth witnesses inside each new half-band.
    tasks = [task(n, i) for i, n in enumerate([70, 44, 40, 11, 10, 9, 300])]
    for i, item in enumerate(tasks): item['report_path'] = str(tmp_path / ('rss' + str(i)))
    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.delenv('PCC_PY_FRONTEND_WORKER_TIMING', raising=False)
    clock = [0.0]
    live = {}
    records = []
    attempts = {}
    stopped = []
    monkeypatch.setattr(pool.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(pool.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(workers, '_coordinator_rss_bytes', lambda: MIB)

    def start(specs, index):
        assert specs[index][0] == ['command' + str(index)]
        attempts[index] = attempts.get(index, 0) + 1
        pid = 100 + len(records)
        live[pid] = [index, clock[0] + (0.08 if index == 4 else 0.04)]
        records.append(('start', index, clock[0], pid))
        return pid

    def peak(index):
        return (170 if growth and index == 4 else 16) * MIB

    def report(path, pid, token):
        index, end = live[pid]
        # Grow after the same-band peer has been launched.
        value = peak(index) if index != 4 or clock[0] >= end - 0.04 else 16 * MIB
        return ('complete' if clock[0] >= end else 'live', value, value)

    def poll(pid):
        index, end = live[pid]
        return (1 if failure and index == 4 else 0) if clock[0] >= end else pool._WORKER_RUNNING

    def retire(pid):
        records.append(('retire', live[pid][0], clock[0], pid))
        # Keep report identity readable for the required post-success read.

    def stop(pid):
        stopped.append(pid)
        records.append(('stop', live[pid][0], clock[0], pid))

    monkeypatch.setattr(pool, '_start_resource_worker', start)
    monkeypatch.setattr(pool, '_poll_resource_worker', poll)
    monkeypatch.setattr(pool, '_retire_resource_worker', retire)
    monkeypatch.setattr(pool, '_stop_resource_worker', stop)
    monkeypatch.setattr(policy, 'read_worker_resource', report)
    commands = ['command' + str(i) for i in range(len(tasks))]
    observations = []
    budget = MIB + policy.RSS_HEADROOM_BYTES + 400 * MIB
    error = None
    try:
        pool.run_resource_worker_processes(commands, tasks, 2, budget, observations=observations)
    except subprocess.CalledProcessError as exc:
        error = exc
    return tasks, records, attempts, observations, error, stopped


def test_mock_pool_preserves_command_result_identity_and_drains(tmp_path, monkeypatch):
    tasks, records, attempts, observations, error, stopped = mock_pool(tmp_path, monkeypatch)
    assert error is None and not stopped
    assert attempts == {i: 1 for i in range(len(tasks))}
    assert len(observations) == len(tasks)
    bands = policy.resource_task_order(tasks)[1]
    starts = [r for r in records if r[0] == 'start']
    assert [r[1] for r in starts] == [3, 4, 5, 1, 2, 0, 6]
    for event, index, when, pid in starts:
        prior_band_finishes = [r[2] for r in records if r[0] == 'retire' and bands[r[1]] < bands[index]]
        assert not prior_band_finishes or max(prior_band_finishes) <= when
    assert [t['diagnostic_indices'] for t in tasks] == [[i] for i in range(len(tasks))]


def test_mock_pool_failure_does_not_advance_larger_band(tmp_path, monkeypatch):
    tasks, records, attempts, observations, error, stopped = mock_pool(tmp_path, monkeypatch, failure=True)
    assert error is not None and error.cmd == 'command4'
    assert set(attempts) == {3, 4, 5}
    assert len(observations) < len(tasks)


def test_mock_peak_growth_retries_same_band_before_advancing(tmp_path, monkeypatch):
    tasks, records, attempts, observations, error, stopped = mock_pool(tmp_path, monkeypatch, growth=True)
    assert error is None
    assert attempts[5] == 2
    assert all(count == 1 for index, count in attempts.items() if index != 5)
    assert len(stopped) == 1 and len(observations) == len(tasks)
    retry_start = [r for r in records if r[0] == 'start' and r[1] == 5][-1]
    grown_retire = next(r for r in records if r[0] == 'retire' and r[1] == 4)
    retry_retire = [r for r in records if r[0] == 'retire' and r[1] == 5][-1]
    next_band = next(r for r in records if r[0] == 'start' and r[1] == 1)
    assert grown_retire[2] <= retry_start[2] < retry_retire[2] <= next_band[2]
    assert tasks[5]['incomplete_peak_bytes'] == 16 * MIB


def test_small_real_children_keep_cohort_barrier_and_original_outputs(tmp_path, monkeypatch):
    import shlex
    import sys

    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    tasks = [task(n, i) for i, n in enumerate([70, 11, 10, 9])]
    commands = []
    for index, item in enumerate(tasks):
        item['report_path'] = str(tmp_path / ('rss' + str(index)))
        output = tmp_path / ('original-output' + str(index))
        code = (
            'import time;from pathlib import Path;'
            'from pcc.frontends.python.worker_resource_plan import publish_worker_resource as report;'
            'payload=bytearray(4*1024**2);report("allocated");time.sleep(0.15);'
            + 'Path(' + repr(str(output)) + ').write_text(' + repr(str(index)) + ');report("complete")'
        )
        commands.append(shlex.join([sys.executable, '-B', '-c', code]))
    trace = tmp_path / 'trace'
    observations = []
    pool.run_resource_worker_processes(
        commands, tasks, 2,
        workers._coordinator_rss_bytes() + policy.RSS_HEADROOM_BYTES + 512 * MIB,
        observations=observations, trace_path=str(trace),
    )
    rows = [line.split('\t') for line in trace.read_text().splitlines()]
    launches = [int(row[1]) for row in rows if row[0] in ('start', 'calibrate')]
    assert launches == [1, 2, 3, 0]
    later = next(i for i, row in enumerate(rows) if row[0] in ('start', 'calibrate') and row[1] == '0')
    assert all(next(i for i, row in enumerate(rows) if row[:2] == ['retire', str(index)]) < later for index in (1, 2, 3))
    assert all((tmp_path / ('original-output' + str(index))).read_text() == str(index) for index in range(4))
    assert len(observations) == 4 and pool._HOST_WORKERS == {}


@pytest.mark.parametrize("size,expected", [
    (0, 0), (1, 2), (2, 4), (3, 5), (4, 6), (5, 6),
    (6, 7), (7, 7), (8, 8), (11, 8), (12, 9), (15, 9),
    (16, 10), (23, 10), (24, 11), (31, 11), (32, 12),
])
def test_half_band_integer_boundaries(size, expected):
    item = task(0)
    item['inputs'][:4] = [size, size, 0, 0]
    assert policy.resource_task_order([item]) == ([0], [expected])


def test_finer_order_keeps_larger_peak_after_smaller_half_band():
    # 20 and 26 used to share [16,32); a 26-byte high-RSS task would
    # calibrate first. The lower half now drains before it can contribute.
    tasks = [task(13, 0), task(10, 1), task(9, 2)]
    order, bands = policy.resource_task_order(tasks)
    assert order == [1, 2, 0] and bands[1] == bands[2] < bands[0]
    low = (tasks[1]['class'], list(tasks[1]['inputs']), 100 * MIB)
    assert policy.estimated_task_bytes(tasks[2], [low]) == policy.peak_reservation(100 * MIB)
    assert policy.estimated_task_bytes(tasks[0], [low]) == 0
    high = (tasks[0]['class'], list(tasks[0]['inputs']), 900 * MIB)
    # Sorting never filters, deletes or substitutes a MAX observation.
    assert policy.estimated_task_bytes(tasks[2], [low, high]) == policy.peak_reservation(900 * MIB)
    assert policy.ready_resource_cohort([0], [0], [1, 2], bands) == []
    assert policy.ready_resource_cohort([0], [0], [], bands) == [0]


def test_smaller_completed_cohort_cannot_cover_larger_pending_vector():
    # Source/AST can trade off; the sum argument holds in both dimensions.
    tasks = []
    for source in range(9):
        for wire in range(9):
            item = task(0, len(tasks))
            item['inputs'][:4] = [source, source, wire, wire]
            tasks.append(item)
    _order, bands = policy.resource_task_order(tasks)
    for before, observed in enumerate(tasks):
        for after, pending in enumerate(tasks):
            if bands[before] < bands[after]:
                assert not policy.input_envelope_covers(observed['inputs'], pending['inputs'])


def test_half_bands_preserve_huge_integer_and_equal_vector_order():
    size = 2 ** 80
    tasks = [task(size, 0), task(size, 1), task(size - 1, 2)]
    order, bands = policy.resource_task_order(tasks)
    assert order == [2, 0, 1] and bands[0] == bands[1] > bands[2]
    assert tasks[0]['inputs'][0] == size


def test_admission_diagnostic_records_bounded_inputs_and_private_class_digest(monkeypatch, capsys):
    import hashlib

    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', '1')
    item = task(10, 4)
    item['class'] = '/private/manifest|/private/compiler|codegen|secret-token'
    item['report_path'] = '/private/report'
    pool._resource_diagnostic(item, 'start', 4, 123, 500, 1000, 0, cohort=10)
    text = capsys.readouterr().err
    assert text.count('pcc frontend admission event=') == 1
    assert 'input_count=6 inputs=[10, 10, 10, 10, 100000000, 1]' in text
    assert 'class_sha256=' + hashlib.sha256(item['class'].encode()).hexdigest() in text
    assert 'cohort=10' in text and 'module_count=1 mapping_offset=0' in text
    assert '/private' not in text and 'secret-token' not in text


@pytest.mark.parametrize('enabled', ['', '0', 'false'])
def test_disabled_admission_diagnostic_never_reads_task(enabled, monkeypatch, capsys):
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', enabled)
    pool._resource_diagnostic(object(), 'start', 0, 1, 1, 1, 0)
    assert capsys.readouterr().err == ''


def test_admission_diagnostic_bounds_unknown_input_shape_and_keeps_batch_mapping(monkeypatch, capsys):
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', '1')
    item = task(10)
    item['inputs'] = list(range(20))
    item['diagnostic_modules'] = ['module' + str(i) for i in range(18)]
    item['diagnostic_indices'] = list(range(18))
    pool._resource_diagnostic(item, 'retire', 2, 99, 500, 0, 300)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert all('input_count=20 inputs=[0, 1, 2, 3, 4, 5, 6, 7]' in line for line in lines)
    assert 'mapping_offset=0' in lines[0] and 'mapping_offset=16' in lines[1]
    assert 'indices=[16, 17]' in lines[1] and "modules=['module16', 'module17']" in lines[1]
    assert all('cohort=-1' in line for line in lines)


def test_original_quotient_scan_handles_nonfinite_and_subunit_sizes():
    # Real source/AST byte counts are integers. Retain the old recognizer
    # and quotient-loop behavior for its previously accepted numeric inputs.
    for axis in (0, 2):
        for value, expected in (
            (float('inf'), [1]),
            (float('nan'), []),
            (float('-inf'), []),
            (0.25, [2]),
        ):
            item = task(0)
            item['inputs'][axis:axis + 2] = [value, value]
            assert policy.resource_task_order([item]) == ([0], expected)
