#!/usr/bin/env python3
"""Execute one reviewed ELF in this same, externally supervised process."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import stat
import struct
import sys
import time


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--compile-output', type=Path, required=True)
    parser.add_argument('--arm', choices=('candidate',), required=True)
    parser.add_argument('--gc', type=int, choices=(0, 1), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode and not sys.flags.optimize,
            'require isolated -I -S -B without optimization')
    require(sha(args.manifest) == args.manifest_sha256, 'manifest changed')
    manifest = json.loads(args.manifest.read_text())
    require(sha(Path(__file__)) == manifest['files']['launch_elf.py']['sha256'], 'launcher changed')
    require(sha(Path(sys.executable)) == manifest['host_interpreter_sha256'], 'host interpreter changed')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    require(os.getuid() != 0 and os.geteuid() != 0, 'unprivileged UID required')
    require(all(int(status[key].strip(), 16) == 0 for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd')), 'capabilities must be zero')
    require(status['NoNewPrivs'].strip() == '1', 'NoNewPrivs required')
    resource.setrlimit(resource.RLIMIT_AS, (4294967296, 4294967296))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    require(os.execve in os.supports_fd, 'descriptor execve is unavailable')
    require(os.environ.get('PCC_WORKER_TREE_BUDGET_BYTES') == '4294967296', 'live outer budget missing')
    state = Path(os.environ['PCC_WORKER_TREE_STATE_PATH']).resolve(strict=True)
    require(state.name == 'worker-rss.tsv', 'unexpected supervisor state')
    deadline = time.monotonic() + 0.5
    while True:
        guard = json.loads((state.parent / 'result.json').read_text())
        if guard.get('status') == 'RUNNING' and guard.get('child_pid') == os.getpid():
            break
        require(time.monotonic() < deadline, 'supervisor did not publish this running root')
        time.sleep(0.01)
    require(guard['timeout_s'] == 15.0 and guard['rss_threshold_bytes'] == 4294967296 and guard['min_free_bytes'] == 4294967296,
            'wrong enclosing guard limits')
    require(guard['supervisor_sha256'] == manifest['supervisor_sha256'] and guard['lock']['exclusive'], 'wrong supervisor or lock')
    require(state.parent.parent == args.output.resolve().parent, 'payload must be a fresh sibling of the guard output')
    arm = manifest['arms'][args.arm]
    compile_output = args.compile_output.resolve(strict=True)
    require(sha(compile_output / 'result.json') == arm['compile_result_sha256'], 'compile receipt changed')
    compiled = json.loads((compile_output / 'result.json').read_text())
    require(compiled['status'] == 'PASS' and compiled['arm'] == args.arm, 'wrong compile arm')
    for key in ('program_sha256', 'runtime_sha256', 'codegen_checksum'):
        require(compiled[key] == manifest[key], 'compile provenance mismatch: ' + key)
    require(compiled['source_manifest_sha256'] == manifest['candidate_source_manifest_sha256'], 'compiled source inventory differs')
    executable = compile_output / 'program.out'
    fd = os.open(executable, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o755 and before.st_uid == os.getuid(),
                'expected owned regular 0755 ELF')
        require(before.st_size == arm['executable_bytes'], 'ELF size changed')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        require(digest == arm['executable_sha256'], 'ELF bytes changed')
        os.lseek(fd, 0, os.SEEK_SET)
        header = os.read(fd, 64)
        fields = struct.unpack('<16sHHIQQQIHHHHHH', header)
        require(header[:7] == b'\x7fELF\x02\x01\x01' and fields[1:4] == (2, 62, 1), 'expected x86-64 ELF64 executable')
        require(fields[9] == 56 and 0 < fields[10] <= 64, 'unexpected ELF program headers')
        os.lseek(fd, fields[5], os.SEEK_SET)
        ph = os.read(fd, fields[9] * fields[10])
        require(len(ph) == fields[9] * fields[10], 'truncated program headers')
        require(all(struct.unpack_from('<I', ph, index * 56)[0] != 3 for index in range(fields[10])), 'dynamic interpreter is not admitted')
        after = os.fstat(fd)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'ELF changed while verifying')
        output = args.output.resolve()
        output.mkdir(parents=False, exist_ok=False)
        temporary = output / 'tmp'
        temporary.mkdir(mode=0o700)
        environment = {'PATH': '', 'PCC_GC_BACKEND': str(args.gc), 'PCC_LOG': 'gc',
                       'PCC_LOG_FORMAT': 'json', 'PCC_LOG_FILE': str(output / 'gc.jsonl'),
                       'PCC_WITH_THREADS': '1', 'PCC_REFCOUNT_KIND': 'atomic',
                       'PCC_NO_AUTO_PCC1': '1', 'PCC_GC_REFCOUNT_PROVENANCE_PROBE': '2',
                       'TMPDIR': str(temporary)}
        require(resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296) and
                resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0), 'hard limits changed')
        receipt = {'schema': 'pcc.same_pid_elf_admission.v1', 'status': 'EXEC_READY',
                   'arm': args.arm, 'requested_gc': args.gc, 'pid': os.getpid(), 'ppid': os.getppid(),
                   'uid': os.getuid(), 'euid': os.geteuid(), 'capabilities': 'all zero', 'no_new_privileges': True,
                   'hard_address_space_bytes': 4294967296, 'hard_nproc': 0,
                   'native_alarm_seconds': 10, 'outer_timeout_seconds': 15,
                   'executable_sha256': digest, 'executable_bytes': before.st_size,
                   'file_identity': {'device': before.st_dev, 'inode': before.st_ino, 'mode': '100755'},
                   'manifest_sha256': args.manifest_sha256, 'native_environment': environment,
                   'execution_scope': 'unchanged open-errno static ELF, same supervised PID, GC0 or GC1 only'}
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        require(signal.SIGALRM not in signal.pthread_sigmask(signal.SIG_BLOCK, set()), 'SIGALRM remains blocked')
        receipt['sigalrm_unblocked'] = True
        write_new(output / 'launch.json', receipt)
        signal.alarm(10)
        # Descriptor execution preserves the known root identity. The CLOEXEC
        # descriptor closes after a successful ELF exec; no child is created.
        os.execve(fd, [str(executable)], environment)
        raise RuntimeError('execve unexpectedly returned')
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
