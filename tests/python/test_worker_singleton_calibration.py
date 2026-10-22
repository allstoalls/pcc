"""Degenerate native cohorts reuse only a completed same-invocation sample."""

import copy

import pytest

from pcc.frontends.python import worker_resource_plan as policy
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import pipeline_frontend_workers as workers
from tests.python.test_worker_codegen_cohorts import task, MIB


def inventory():
    return [task(size, i) for i, size in enumerate([1, 4, 16, 64, 256])]


def test_singleton_bands_calibrate_covering_input_first_without_mutation():
    tasks = inventory()
    before = copy.deepcopy(tasks)
    assert policy.resource_task_order(tasks, guarded_calibration=True) == ([4, 3, 2, 1, 0], [])
    assert tasks == before
    assert policy.estimated_task_bytes(tasks[0], []) == 0
    sample = policy.completed_task_observation(tasks[4], 500 * MIB)
    assert all(policy.estimated_task_bytes(item, [sample]) == policy.peak_reservation(500 * MIB)
               for item in tasks)


def test_sum_size_cannot_substitute_for_componentwise_coverage():
    tasks = inventory()
    tasks[0]['inputs'][2:4] = [1400, 1400]
    # This input has the largest sum but does not cover source bytes of task 4.
    order, bands = policy.resource_task_order(tasks, guarded_calibration=True)
    assert bands and len(set(bands)) == len(tasks)
    assert order[0] != 0


def test_repeated_band_keeps_bootstrap_small_first_policy():
    tasks = inventory() + [task(1, 5)]
    order, bands = policy.resource_task_order(tasks, guarded_calibration=True)
    assert bands and order[:2] == [0, 5] and order[-1] == 4


@pytest.mark.parametrize('change', ['owner', 'inputs', 'incomplete'])
def test_unrelated_or_incomplete_sample_never_enables_peer_admission(change):
    tasks = inventory()
    sample = policy.completed_task_observation(tasks[4], 500 * MIB)
    if change == 'owner':
        sample = ('other-invocation', sample[1], sample[2])
    elif change == 'inputs':
        sample = (sample[0], [0] * 6, sample[2])
    else:
        tasks[4]['incomplete_peak_bytes'] = 500 * MIB
        sample = None
    observations = [sample] if sample is not None else []
    assert policy.estimated_task_bytes(tasks[0], observations) == 0
    index, demand, exclusive = policy.choose_task([0], tasks, observations, [], 4, 1800 * MIB)
    assert index == 0 and demand == 1800 * MIB and exclusive


@pytest.mark.parametrize('fail_largest', [False, True])
@pytest.mark.parametrize('available_mib', [1800, 650])
def test_pool_waits_for_retired_success_then_reuses_sample(tmp_path, monkeypatch, fail_largest, available_mib):
    import subprocess
    tasks = inventory()
    for i, item in enumerate(tasks):
        item['report_path'] = str(tmp_path / ('rss' + str(i)))
    clock = [0.0]
    live = {}
    events = []
    observations = []
    monkeypatch.setenv(policy.TREE_STATE_ENV, str(tmp_path / 'guard-state'))
    monkeypatch.delenv(policy.TREE_RECOVERY_ENV, raising=False)

    def read_state(path, budget, owner_pid, active_pids, not_before=0.0, include_owner=False, diagnostic=None):
        assert path == str(tmp_path / 'guard-state') and include_owner
        assert not_before <= clock[0]
        return (0, {owner_pid: MIB, **{pid: 500 * MIB for pid in active_pids}})

    monkeypatch.setattr(policy, 'read_tree_state', read_state)
    monkeypatch.setenv('PCC_PY_FRONTEND_WORKER_TIMING', '0')
    monkeypatch.setattr(pool.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(pool.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(workers, '_coordinator_rss_bytes', lambda: MIB)

    def start(specs, index):
        pid = 100 + index
        assert specs[index][0] == ['command' + str(index)]
        live[pid] = (index, clock[0] + .05)
        events.append(('start', index, clock[0], len(live)))
        return pid

    def poll(pid):
        index, end = live[pid]
        if clock[0] < end:
            return pool._WORKER_RUNNING
        return 7 if fail_largest and index == 4 else 0

    def report(path, pid, token):
        index, end = live[pid]
        return ('complete' if clock[0] >= end else 'live', 500 * MIB, 500 * MIB)

    def retire(pid):
        events.append(('retire', live[pid][0], clock[0], len(live)))
        # Post-success report is read after retirement, so retain metadata.
        retired.add(pid)

    retired = set()
    monkeypatch.setattr(pool, '_start_resource_worker', start)
    monkeypatch.setattr(pool, '_poll_resource_worker', poll)
    monkeypatch.setattr(policy, 'read_worker_resource', report)
    monkeypatch.setattr(pool, '_retire_resource_worker', retire)
    monkeypatch.setattr(pool, '_stop_resource_worker', lambda pid: retired.add(pid))
    commands = ['command' + str(i) for i in range(len(tasks))]
    def run():
        pool.run_resource_worker_processes(commands, tasks, 4,
            MIB + policy.RSS_HEADROOM_BYTES + available_mib * MIB,
            observations=observations, trace_path=str(tmp_path / 'admission'))
    if fail_largest:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            run()
        assert caught.value.returncode == 7 and caught.value.cmd == 'command4'
        assert [event[1] for event in events if event[0] == 'start'] == [4]
        assert observations == []
    else:
        run()
        rows = [line.split('\t') for line in (tmp_path / 'admission').read_text().splitlines()]
        if available_mib == 650:
            assert [row[1] for row in rows if row[0] == 'calibrate'] == ['4', '3', '2', '1', '0']
            assert len(observations) == 5 and retired == set(live)
            return
        assert [row[1] for row in rows if row[0] == 'calibrate'] == ['4']
        first_retire = next(i for i, row in enumerate(rows) if row[:2] == ['retire', '4'])
        starts = [i for i, row in enumerate(rows) if row[0] == 'start']
        assert len(starts) == 4 and min(starts) > first_retire
        assert rows[starts[0]][3] == str(policy.peak_reservation(500 * MIB))
        # Two peers start before either retires, with the existing budget.
        assert rows[starts[0]][0:2] == ['start', '3']
        assert rows[starts[1]][0:2] == ['start', '2']
        first_peer_retire = next(i for i, row in enumerate(rows) if row[:2] == ['retire', '3'])
        assert starts[1] < first_peer_retire
        assert len(observations) == 5 and retired == set(live)
        assert [item['diagnostic_indices'] for item in tasks] == [[i] for i in range(5)]


def test_default_unguarded_order_stays_small_first():
    tasks = inventory()
    order, bands = policy.resource_task_order(tasks)
    assert order == [0, 1, 2, 3, 4] and bands
    assert policy.resource_task_order(tasks, guarded_calibration=False) == (order, bands)
