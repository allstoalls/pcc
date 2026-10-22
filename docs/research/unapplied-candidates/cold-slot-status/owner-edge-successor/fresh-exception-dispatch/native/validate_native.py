#!/usr/bin/env python3
"""Read-only native outcome validation after the outer supervisor is terminal."""
import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--compile-output', type=Path, required=True)
    parser.add_argument('--arm', choices=('combined-probes',), required=True)
    parser.add_argument('--gc', type=int, choices=(0, 1), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--guard-output', type=Path, required=True)
    args = parser.parse_args()
    errors = []
    result = {'schema': 'pcc.cold_slot_native_outcome.v1', 'status': 'FAIL', 'arm': args.arm,
              'requested_gc': args.gc, 'scope': 'two-call dispatch-corrected C fixture containing both reporting probes; observed GC0/1 only',
              'unrun': ['GC2', 'GC3', 'GC4', 'pcc1', 'macOS', 'full async/gateway'],
              'old_executor_termination': 'UNKNOWN'}
    def check(value, message):
        if not value:
            errors.append(message)
    try:
        check(sha(args.manifest) == args.manifest_sha256, 'manifest changed')
        manifest = json.loads(args.manifest.read_text())
        check(sha(Path(__file__)) == manifest['files']['validate_native.py']['sha256'], 'validator changed')
        arm = manifest['arms'][args.arm]
        check(sha(args.compile_output / 'result.json') == arm['compile_result_sha256'], 'compile receipt changed')
        check(sha(args.compile_output / 'status-runtime') == arm['executable_sha256'], 'ELF changed after execution')
        launch = json.loads((args.output / 'launch.json').read_text())
        guard = json.loads((args.guard_output / 'result.json').read_text())
        check(launch['status'] == 'EXEC_READY' and launch['arm'] == args.arm and launch['requested_gc'] == args.gc,
              'wrong launch identity')
        check(launch['manifest_sha256'] == args.manifest_sha256 and launch['executable_sha256'] == arm['executable_sha256'],
              'wrong launch hash binding')
        check(guard['child_pid'] == launch['pid'], 'guard did not own the launched PID')
        check(guard['status'] == 'COMPLETE' and guard['returncode'] == 0 and not guard['finalization_errors'], 'native process did not complete successfully')
        cleanup = guard['cleanup']
        check(cleanup['status'] == 'CLEAN' and cleanup['root_reaped'] and cleanup['echild'] and not cleanup['active_handles'] and not cleanup['errors'], 'cleanup incomplete')
        check(guard['timeout_s'] == 15.0 and guard['rss_threshold_bytes'] == 4294967296 and guard['min_free_bytes'] == 4294967296,
              'incorrect outer caps')
        check(launch['hard_nproc'] == 0 and launch['hard_address_space_bytes'] == 4294967296 and launch['native_alarm_seconds'] == 10 and launch['sigalrm_unblocked'],
              'incorrect native caps or blocked alarm')
        stdout = (args.guard_output / 'stdout').read_bytes()
        stderr = (args.guard_output / 'stderr').read_bytes()
        check(stdout == manifest['expected_stdout'].encode() and stderr == b'', 'native output mismatch')
        log = args.output / 'gc.jsonl'
        events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []
        selected = [event for event in events if event.get('category') == 'gc' and event.get('event') in ('collect_start', 'collect_stop', 'collect_end')]
        check(all(type(event.get('value1')) is int for event in selected), 'collector IDs are not integers')
        observed = sorted({event['value1'] for event in selected})
        check(observed == [args.gc], 'observed collector differs or is absent')
        result.update(observed_gc=observed, collection_events=len(selected), returncode=guard['returncode'],
                      stdout=stdout.decode('utf-8', errors='replace'), stderr=stderr.decode('utf-8', errors='replace'),
                      elapsed_seconds=guard['elapsed_s'], peak_sampled_rss_bytes=guard['peak_sampled_tree_rss_bytes'],
                      executable_sha256=arm['executable_sha256'], manifest_sha256=args.manifest_sha256,
                      cleanup=cleanup['status'], root_reaped=cleanup['root_reaped'], echild=cleanup['echild'])
        result['artifacts'] = {name: {'sha256': sha(path), 'bytes': path.stat().st_size} for name, path in (
            ('launch', args.output / 'launch.json'), ('guard', args.guard_output / 'result.json'),
            ('stdout', args.guard_output / 'stdout'), ('stderr', args.guard_output / 'stderr'),
            ('gc_log', log)) if path.is_file()}
    except Exception as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
    result['errors'] = errors
    result['status'] = 'PASS' if not errors else 'FAIL'
    with (args.output.parent / (args.output.name + '-result.json')).open('x') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps(result, sort_keys=True))
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
