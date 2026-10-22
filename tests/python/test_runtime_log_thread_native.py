"""Actual pthread lifecycle logging through the owned native compiler routes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import textwrap

import pytest

from tests.python.process_timeout import run_process_group_timeout
from tests.runtime_fixture_provenance import _verified_test_runtime_archive


ROUNDS = 8
WORKERS = 3
PROGRAM = textwrap.dedent('''
    from pcc.extern import (
        c_abi_typed_export,
        c_int64,
        c_ptr,
        c_rawptr,
        c_void,
        extern,
    )
    from pcc.unsafe import (
        atomic_load_i32, atomic_rmw_i32, atomic_store_i32, cstr,
        define_global_i32, function_addr, global_addr, load_i8, load_i64, load_ptr,
        null, ptr_add, ptr_is_null, ptr_to_int, stack_alloc, store_i64, store_ptr,
    )
    start = extern('pcc_thread_start', (c_ptr, c_ptr, c_ptr), c_int64)
    join = extern('pcc_thread_join', (c_ptr, c_ptr), c_int64)
    enabled = extern('pcc_threads_enabled', (), c_int64)
    identity = extern('pcc_current_thread_id', (), c_int64)
    safepoint = extern('pcc_thread_safepoint', (), c_void)
    stop_world = extern('pcc_stop_the_world', (), c_int64)
    resume_world = extern('pcc_resume_world', (), c_int64)
    scheduler_count = extern('py_virtual_thread_ready_count', (), c_int64)
    scheduler_acquired = extern('pcc_thread_scheduler_lock_acquired', (), c_void)
    scheduler_released = extern('pcc_thread_scheduler_lock_released', (), c_void)
    no_park_enter = extern('pcc_thread_no_park_enter', (), c_void)
    no_park_exit = extern('pcc_thread_no_park_exit', (), c_void)
    getenv = extern('pcc_platform_getenv', (c_ptr,), c_rawptr)
    log_event = extern('pcc_diagnostics_runtime_log_event',
                       (c_ptr, c_ptr, c_int64, c_int64, c_ptr), c_void)
    log_code = extern('pcc_diagnostics_runtime_log_event_code',
                      (c_int64, c_int64, c_int64, c_int64, c_ptr), c_void)
    define_global_i32('thread_log_probe_ready', 0)
    define_global_i32('thread_log_probe_release', 0)

    @c_abi_typed_export('thread_log_probe_worker', 'ptr', ('ptr',))
    def worker(arg: c_ptr) -> c_ptr:
        store_i64(arg, 0, identity())
        atomic_rmw_i32('add', global_addr('thread_log_probe_ready'), 0, 1, 'acq_rel')
        while atomic_load_i32(global_addr('thread_log_probe_release'), 0, 'acquire') == 0:
            safepoint()
        return arg

    def main():
        assert enabled() == 1
        parent = identity()
        tripwire = getenv(cstr('PCC_THREAD_PROBE_TRIPWIRE'))
        if ptr_is_null(tripwire) == 0:
            mode = load_i8(tripwire, 0)
            if mode == 49:
                scheduler_acquired()
                safepoint()
            elif mode == 50:
                no_park_enter()
                scheduler_acquired()
                safepoint()
                scheduler_released()
                no_park_exit()
                print('NO_PARK_OK')
                return
            elif mode == 51:
                no_park_enter()
                scheduler_acquired()
                stop_world()
            print('TRIPWIRE_MISSED')
            return
        handles = stack_alloc(24)
        arguments = stack_alloc(24)
        returned = stack_alloc(8)
        entry = function_addr('thread_log_probe_worker')
        log_event(cstr('thread'), cstr('probe_begin'), 0, 0, null())
        assert scheduler_count() >= 0
        assert start(null(), entry, arguments) == -1
        assert start(handles, null(), arguments) == -1
        assert join(null(), returned) == -1
        round_index = 0
        while round_index < 8:
            atomic_store_i32(global_addr('thread_log_probe_ready'), 0, 0, 'release')
            atomic_store_i32(global_addr('thread_log_probe_release'), 0, 0, 'release')
            index = 0
            while index < 3:
                store_i64(arguments, index * 8, 0)
                store_ptr(handles, index * 8, null())
                assert start(ptr_add(handles, index * 8), entry, ptr_add(arguments, index * 8)) == 0
                index = index + 1
            while atomic_load_i32(global_addr('thread_log_probe_ready'), 0, 'acquire') != 3:
                safepoint()
            assert stop_world() == 0
            assert resume_world() == 0
            atomic_store_i32(global_addr('thread_log_probe_release'), 0, 1, 'release')
            index = 0
            while index < 3:
                assert join(load_ptr(handles, index * 8), returned) == 0
                assert ptr_to_int(load_ptr(returned, 0)) == ptr_to_int(ptr_add(arguments, index * 8))
                assert load_i64(arguments, index * 8) > parent
                previous = 0
                while previous < index:
                    assert load_i64(arguments, previous * 8) != load_i64(arguments, index * 8)
                    previous = previous + 1
                index = index + 1
            assert stop_world() == 0
            assert resume_world() == 0
            round_index = round_index + 1
        log_event(cstr('thread'), cstr('probe_end'), 0, 0, null())
        log_code(2, 1, 123456, 0, null())
        print('THREAD_LOG_OK')
    main()
''')


def _check_lifecycles(events, expected=ROUNDS * WORKERS):
    thread_events = [event for event in events if event['category'] == 'thread']
    begins = [index for index, event in enumerate(thread_events) if event['event'] == 'probe_begin']
    ends = [index for index, event in enumerate(thread_events) if event['event'] == 'probe_end']
    assert len(begins) == len(ends) == 1 and begins[0] < ends[0]
    phase = thread_events[begins[0] + 1:ends[0]]
    lifecycle_names = {'start', 'enter', 'exit', 'join', 'joined', 'start_failed',
                       'join_failed', 'detach'}
    phase = [event for event in phase if event['event'] in lifecycle_names]
    handles = {event['ptr'] for event in phase if event['event'] == 'start'}
    # A GC collector started before main may enter during the measured phase.
    # Keep the complete log, but validate exactly the application's 24 starts.
    phase = [event for event in phase if event['ptr'] in handles or int(event['ptr'], 16) == 0]
    assert len(phase) == expected * 5 + 3
    active = {}
    completed = []
    failures = []
    for event in phase:
        assert event['value1'] == 0
        handle = event['ptr']
        name = event['event']
        if int(handle, 16) == 0:
            failures.append((name, event['value0']))
            continue
        assert event['value0'] == 0 and event['thread'] > 0
        if name == 'start':
            assert handle not in active
            active[handle] = []
        assert handle in active
        lifecycle = active[handle]
        assert name not in [row['event'] for row in lifecycle]
        lifecycle.append(event)
        if name == 'joined':
            names = [row['event'] for row in lifecycle]
            assert set(names) == {'start', 'enter', 'exit', 'join', 'joined'}
            assert names.index('start') < names.index('enter') < names.index('exit') < names.index('joined')
            assert names.index('start') < names.index('join') < names.index('joined')
            callers = {row['thread'] for row in lifecycle if row['event'] in {'start', 'join', 'joined'}}
            children = {row['thread'] for row in lifecycle if row['event'] in {'enter', 'exit'}}
            assert len(callers) == len(children) == 1 and callers.isdisjoint(children)
            completed.append(next(iter(children)))
            del active[handle]
    assert not active and len(completed) == expected
    assert len(set(completed)) == expected
    assert failures == [('start_failed', -1), ('start_failed', -1), ('join_failed', -1)]


def _check_transitions(events, expected_workers=ROUNDS * WORKERS):
    rows = [event for event in events if event['category'] == 'thread']
    begin = next(index for index, event in enumerate(rows) if event['event'] == 'probe_begin')
    end = next(index for index, event in enumerate(rows) if event['event'] == 'probe_end')
    parent = rows[begin]['thread']
    phase = rows[begin + 1:end]
    parent_rows = [event for event in phase if event['thread'] == parent]
    known = {'probe_begin', 'probe_end', 'start', 'enter', 'exit', 'join', 'joined',
             'start_failed', 'join_failed', 'detach', 'scheduler_lock_request',
             'scheduler_acquired_deferred', 'scheduler_lock_released', 'scheduler_lock_failed',
             'safepoint_stop_observed', 'safepoint_suspend_deferred', 'safepoint_resume_deferred',
             'stop_world_request', 'world_stopped', 'stop_world_nested', 'resume_world_request',
             'world_resumed', 'resume_world_nested', 'stop_world_failed', 'resume_world_failed',
             'trace_dropped', 'safepoint_epochs_omitted'}
    assert all(event['event'] in known for event in rows), 'unknown thread event'
    assert not any(event['event'] in {'scheduler_lock_failed', 'stop_world_failed', 'resume_world_failed'}
                   for event in phase), 'unexpected synchronization failure'
    for name in ('scheduler_lock_request', 'scheduler_acquired_deferred', 'scheduler_lock_released',
                 'stop_world_request', 'world_stopped', 'resume_world_request', 'world_resumed'):
        assert any(event['event'] == name for event in parent_rows), name
    application_handles = {event['ptr'] for event in parent_rows if event['event'] == 'start'}
    worker_ids = {event['thread'] for event in phase
                  if event['event'] == 'enter' and event['ptr'] in application_handles}
    assert len(worker_ids) == expected_workers and parent not in worker_ids
    assert any(event['event'] == 'safepoint_stop_observed' and event['thread'] in worker_ids
               for event in phase), 'no application worker observed a stop'
    suspended = {(event['thread'], event['value0'], event['value1']) for event in phase
                 if event['event'] == 'safepoint_suspend_deferred' and event['thread'] in worker_ids}
    resumed = {(event['thread'], event['value0'], event['value1']) for event in phase
               if event['event'] == 'safepoint_resume_deferred' and event['thread'] in worker_ids}
    assert suspended & resumed, 'no matched application suspension/resumption evidence'
    assert all(epoch > 0 and waits > 0 for _, epoch, waits in suspended | resumed)
    assert any(event['event'] == 'world_stopped' and event['value1'] >= WORKERS + 1
               for event in parent_rows), 'no stopped world with the three workers and caller'
    stopped = {event['value0'] for event in parent_rows if event['event'] == 'world_stopped'}
    restarted = {event['value0'] for event in parent_rows if event['event'] == 'world_resumed'}
    assert stopped & restarted, 'no matched native stop/resume epoch'
    assert all(epoch > 0 for epoch in stopped | restarted)
    missing = len(suspended ^ resumed) + len(stopped ^ restarted)
    drops = [event['value0'] for event in phase if event['event'] == 'trace_dropped']
    assert all(count > 0 for count in drops)
    assert sum(drops) >= missing, 'missing transition records exceed reported trace loss'
    counts = {}
    for event in events:
        key = event['category'] + '.' + event['event']
        counts[key] = counts.get(key, 0) + 1
    return {'all_event_counts': counts, 'worker_ids': sorted(worker_ids),
            'matched_worker_suspensions': len(suspended & resumed),
            'matched_stw_epochs': sorted(stopped & restarted),
            'missing_transition_records': missing, 'trace_dropped_total': sum(drops)}


def _source_identity(compiler_root, runtime_root):
    from scripts.run_pcc_compile_ab import build_source_files

    paths = set(build_source_files(compiler_root))
    paths.add(runtime_root / 'Makefile')
    for part in ('py', 'include', 'src'):
        paths.update(path for path in (runtime_root / part).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts)
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def _read_events(path, format_name):
    lines = path.read_text().splitlines()
    if format_name == 'json':
        events = [json.loads(line) for line in lines]
        assert all(event['schema'] == 'pcc.diagnostics.runtime_log.v1' for event in events)
        return events
    pattern = re.compile(r'\[pcc\.(\w+)\] ts=(\d+) thread=(\d+) event=(\w+) value0=(-?\d+) value1=(-?\d+) ptr=(0x[0-9a-f]+)')
    events = []
    for line in lines:
        match = pattern.fullmatch(line)
        assert match, line
        category, timestamp, thread, event, first, second, pointer = match.groups()
        events.append({'category': category, 'ts': int(timestamp), 'thread': int(thread),
                       'event': event, 'value0': int(first), 'value1': int(second), 'ptr': pointer})
    return events


def _sample_events():
    def event(name, thread=1, pointer='0x0', status=0):
        return {'category': 'thread', 'event': name, 'thread': thread,
                'ptr': pointer, 'value0': status, 'value1': 0}

    rows = [event('probe_begin'), event('start_failed', status=-1),
            event('start_failed', status=-1), event('join_failed', status=-1)]
    # A collector entered inside the window, having started before it.
    rows.append(event('enter', 99, '0xc0'))
    for child in (2, 3):
        rows.extend([event('start', pointer='0x20'), event('join', pointer='0x20'),
                     event('enter', child, '0x20'), event('exit', child, '0x20'),
                     event('joined', pointer='0x20')])
    rows.append(event('probe_end'))
    return rows


def test_lifecycle_validation_allows_join_before_entry_and_handle_reuse():
    _check_lifecycles(_sample_events(), expected=2)


@pytest.mark.parametrize('fault', ['missing-start', 'missing-exit', 'wrong-child',
                                  'reused-identity', 'error-status', 'late-exit'])
def test_lifecycle_validation_rejects_missing_or_mismatched_events(fault):
    rows = _sample_events()
    if fault == 'missing-start':
        del rows[5]
    elif fault == 'missing-exit':
        del rows[8]
    elif fault == 'wrong-child':
        rows[8]['thread'] = 4
    elif fault == 'reused-identity':
        rows[12]['thread'] = rows[13]['thread'] = 2
    elif fault == 'error-status':
        rows[5]['value0'] = 11
    else:
        rows[8], rows[9] = rows[9], rows[8]
    with pytest.raises(AssertionError):
        _check_lifecycles(rows, expected=2)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_native_thread_lifecycle_logging_five_gc(python_program_compiler, request, monkeypatch, tmp_path, capfd):
    import pcc

    selected = os.environ.get('PCC_THREADED_RUNTIME_ARCHIVE') or os.environ.get('PCC_RUNTIME_ARCHIVE')
    assert selected, 'Supply an explicitly source-matched threaded runtime archive'
    compiler_root = Path(pcc.__file__).resolve().parent.parent
    runtime_root = Path(os.environ.get('PCC_THREAD_LOG_RUNTIME_SOURCE_ROOT', str(compiler_root / 'pcc/runtime'))).resolve(strict=True)
    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    monkeypatch.setenv('PCC_REFCOUNT_KIND', 'atomic')
    archive, manifest = _verified_test_runtime_archive(selected, threads=True, runtime_root=runtime_root)
    for key, value in {'PCC_RUNTIME_DIR': str(runtime_root), 'PCC_RUNTIME_HIGH': 'py',
                       'PCC_RUNTIME_CC': 'pcc', 'PCC_SELF_LINK': 'pcc', 'PCC_SELF_OBJ': 'pcc',
                       'PCC_IR_TO_OBJ_EMITTER': 'pcc', 'PCC_PYTHON_IR_PASSES': 'off',
                       'PCC_NO_AUTO_PCC1': '1', 'PCC_TEST_NO_NATIVE_PROVISIONING': '1',
                       'PCC_HOST_PYTHON': '/nonexistent/host-python',
                       'PCC_HOST_PCC': '/nonexistent/host-pcc',
                       'PCC_GC_REFCOUNT_PROVENANCE_PROBE': '2', 'PATH': ''}.items():
        monkeypatch.setenv(key, value)
    before = _source_identity(compiler_root, runtime_root)
    source = tmp_path / 'thread_log_probe.py'
    source.write_text(PROGRAM)
    binary = tmp_path / 'thread_log_probe'
    receipt = {'compiler_parameter': request.node.callspec.params['python_program_compiler'],
               'compiler_root': str(compiler_root), 'runtime_root': str(runtime_root),
               'runtime_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
               'runtime_manifest': manifest, 'source_identity': before,
               'program_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
               'qualification': 'Native correctness only; timings are not controlled performance evidence',
               'executions': []}
    receipt_path = tmp_path / 'thread-log-receipt.json'
    try:
        python_program_compiler(str(source), str(binary), backend='self', libpython_mode='off',
                                ir_scaffold_mode='on', runtime_archive=str(archive))
        magic = binary.read_bytes()[:4]
        assert magic in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf') or magic[:2] == b'MZ'
        receipt['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        selections = [None, '', 'threaded', 'gc', 'thread', 'thread,gc', 'all', '1']
        for backend in range(5):
            for index, (selection, format_name) in enumerate([(value, 'json') for value in selections] + [('thread', 'text')]):
                label = f'gc{backend}-{index}'
                log = tmp_path / (label + '.log')
                environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_LOG_FORMAT=format_name,
                                   PCC_LOG_FILE=str(log), PATH='')
                environment.pop('LC_ALL', None)
                if selection is None:
                    environment.pop('PCC_LOG', None)
                else:
                    environment['PCC_LOG'] = selection
                result = run_process_group_timeout([str(binary)], env=environment, timeout=20)
                (tmp_path / (label + '.stdout')).write_text(result.stdout)
                (tmp_path / (label + '.stderr')).write_text(result.stderr)
                receipt['executions'].append({'gc': backend, 'selection': selection, 'format': format_name,
                                               'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                assert (result.returncode, result.stdout, result.stderr) == (0, 'THREAD_LOG_OK\n', '')
                if selection in (None, '', 'threaded'):
                    assert not log.exists()
                    continue
                events = _read_events(log, format_name)
                if selection == 'gc':
                    assert events and all(event['category'] == 'gc' for event in events)
                else:
                    _check_lifecycles(events)
                    _check_transitions(events)
                if selection in ('gc', 'thread,gc', 'all', '1'):
                    assert any(event['category'] == 'gc' and event['value0'] == 123456 for event in events)
                if selection == 'thread':
                    assert all(event['category'] == 'thread' for event in events)
                    assert log.stat().st_size < len(events) * 512
        for backend in range(5):
            for mode in ('1', '2', '3'):
                label = f'gc{backend}-tripwire-{mode}'
                environment = dict(os.environ, PCC_GC_BACKEND=str(backend),
                                   PCC_THREAD_PROBE_TRIPWIRE=mode, PCC_LOG='thread',
                                   PCC_LOG_FORMAT='json', PCC_LOG_FILE=str(tmp_path / (label + '.log')),
                                   PATH='')
                environment.pop('LC_ALL', None)
                result = run_process_group_timeout([str(binary)], env=environment, timeout=20)
                (tmp_path / (label + '.stdout')).write_text(result.stdout)
                (tmp_path / (label + '.stderr')).write_text(result.stderr)
                receipt['executions'].append({'gc': backend, 'tripwire_mode': mode,
                                               'returncode': result.returncode, 'stdout': result.stdout,
                                               'stderr': result.stderr})
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                if mode == '2':
                    assert (result.returncode, result.stdout, result.stderr) == (0, 'NO_PARK_OK\n', '')
                else:
                    assert result.returncode == -signal.SIGABRT
                    assert 'TRIPWIRE_MISSED' not in result.stdout
        receipt['state'] = 'PASS'
    finally:
        captured = capfd.readouterr()
        (tmp_path / 'compile.stdout').write_text(captured.out)
        (tmp_path / 'compile.stderr').write_text(captured.err)
        receipt['source_stable'] = _source_identity(compiler_root, runtime_root) == before
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        assert receipt['source_stable']


def _sample_transitions():
    def event(name, thread=1, first=0, second=0, pointer='0x0'):
        return {'category': 'thread', 'event': name, 'thread': thread,
                'ptr': pointer, 'value0': first, 'value1': second}

    return [event('probe_begin'), event('scheduler_lock_request'),
            event('scheduler_acquired_deferred'), event('scheduler_lock_released'),
            event('stop_world_request'), event('safepoint_stop_observed', 2),
            event('world_stopped', first=7, second=4), event('resume_world_request'),
            event('world_resumed', first=7),
            event('safepoint_suspend_deferred', 2, 7, 1),
            event('safepoint_resume_deferred', 2, 7, 1),
            event('start', pointer='0x20'), event('enter', 2, pointer='0x20'), event('probe_end')]


def test_transition_validator_requires_captured_native_epoch_evidence():
    _check_transitions(_sample_transitions(), expected_workers=1)


@pytest.mark.parametrize('fault', ['missing-lock', 'intent-only', 'wrong-resume-epoch',
                                  'zero-waits', 'unreported-loss', 'wrong-stop-epoch',
                                  'collector-only', 'no-worker-stop', 'unknown-event',
                                  'unexpected-lock-failure', 'insufficient-loss-budget'])
def test_transition_validator_rejects_missing_or_inconsistent_evidence(fault):
    rows = _sample_transitions()
    if fault == 'missing-lock':
        del rows[2]
    elif fault == 'intent-only':
        del rows[9:11]
    elif fault == 'wrong-resume-epoch':
        rows[10]['value0'] = 8
    elif fault == 'zero-waits':
        rows[9]['value1'] = rows[10]['value1'] = 0
    elif fault == 'unreported-loss':
        rows.insert(-1, dict(rows[9], value0=8))
    elif fault == 'wrong-stop-epoch':
        rows[8]['value0'] = 8
    elif fault == 'collector-only':
        rows[9]['thread'] = rows[10]['thread'] = 99
    elif fault == 'no-worker-stop':
        rows[6]['value1'] = 1
    elif fault == 'unknown-event':
        rows.insert(-1, dict(rows[0], event='thread_event'))
    elif fault == 'unexpected-lock-failure':
        rows.insert(-1, dict(rows[0], event='scheduler_lock_failed', value0=-1))
    else:
        rows.insert(-1, dict(rows[9], value0=8))
        rows.insert(-1, dict(rows[9], value0=9))
        rows.insert(-1, dict(rows[9], event='trace_dropped', value0=1, value1=0))
    with pytest.raises(AssertionError):
        _check_transitions(rows, expected_workers=1)


def test_transition_validator_allows_explicit_loss_only_with_some_complete_evidence():
    rows = _sample_transitions()
    rows.insert(-1, dict(rows[9], value0=8))
    rows.insert(-1, dict(rows[9], event='trace_dropped', value0=1, value1=0))
    summary = _check_transitions(rows, expected_workers=1)
    assert summary['missing_transition_records'] == 1
    assert summary['trace_dropped_total'] == 1
    assert summary['worker_ids'] == [2]
    assert summary['all_event_counts']['thread.trace_dropped'] == 1
