"""Two-process locks with a supplied native compiler and matching runtime.

Run beneath the parent's process-tree watchdog. These tests never unlink the
lock inode, never run a host compiler, and require native emitted execution.
"""
import os
from pathlib import Path
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

PROGRAM = '''import os
import sys
import time
if os.name == "nt":
    import msvcrt
else:
    import fcntl

def take(file, shared):
    if os.name == "nt":
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        # Test file-object coercion as well as the raw descriptor path.
        fcntl.flock(file, operation | fcntl.LOCK_NB)

def release(file):
    if os.name == "nt":
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)

class BadIndex:
    def __index__(self):
        raise ValueError("index callback error")


def check_arguments(file):
    fd = file.fileno()
    if os.name == "nt":
        try:
            msvcrt.locking(fd, 2.5, 1)
        except TypeError:
            pass
        else:
            raise AssertionError("float mode accepted")
        try:
            msvcrt.locking(fd, 2, 1 << 70)
        except OverflowError:
            pass
        else:
            raise AssertionError("oversized count accepted")
        try:
            msvcrt.locking(fd, BadIndex(), 1)
        except ValueError:
            pass
        else:
            raise AssertionError("index exception lost")
    else:
        try:
            fcntl.flock(-1, fcntl.LOCK_EX)
        except ValueError:
            pass
        else:
            raise AssertionError("negative descriptor accepted")
        try:
            fcntl.flock(fd, 2.5)
        except TypeError:
            pass
        else:
            raise AssertionError("float operation accepted")
        try:
            fcntl.flock(fd, 1 << 70)
        except OverflowError:
            pass
        else:
            raise AssertionError("oversized operation accepted")
        try:
            fcntl.flock(fd, BadIndex())
        except ValueError:
            pass
        else:
            raise AssertionError("index exception lost")
    take(file, False)
    release(file)
    print("arguments-ok")


def main():
    path = sys.argv[1]
    mode = sys.argv[2]
    ready = sys.argv[3]
    finish = sys.argv[4]
    offset = int(sys.argv[5])
    shared = sys.argv[6] == "shared"
    file = open(path, "r+b")
    file.seek(offset)
    if mode == "arguments":
        check_arguments(file)
        file.close()
        return
    if mode == "wait":
        with open(ready, "w") as marker:
            marker.write("waiting")
        if os.name == "nt":
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(file, fcntl.LOCK_EX)
        release(file)
        file.close()
        print("locked")
        return
    try:
        take(file, shared)
    except OSError:
        print("busy")
        file.close()
        return
    if mode == "try":
        release(file)
        file.close()
        print("locked")
        return
    with open(ready, "w") as marker:
        marker.write("held")
    while not os.path.exists(finish):
        time.sleep(0.005)
    if mode == "close":
        file.close()
    else:
        release(file)
    with open(ready + ".released", "w") as marker:
        marker.write("released-while-alive")
    while not os.path.exists(finish + ".exit"):
        time.sleep(0.005)
    if mode != "close":
        file.close()
    print("released")
main()
'''


@pytest.fixture
def executable(tmp_path):
    compiler = os.environ.get("PCC_FILE_LOCKING_COMPILER", "")
    runtime = os.environ.get("PCC_RUNTIME_ARCHIVE", "")
    assert compiler and Path(compiler).is_file(), "set PCC_FILE_LOCKING_COMPILER"
    assert runtime and Path(runtime).is_file(), "set matching PCC_RUNTIME_ARCHIVE"
    source = tmp_path / "lock.py"
    source.write_text(PROGRAM)
    out = tmp_path / ("lock.exe" if os.name == "nt" else "lock")
    env = dict(os.environ, PCC_SELF_LINK="pcc", PCC_RUNTIME_CC=str(tmp_path / "forbidden-cc"), PCC_HOST_PYTHON=str(tmp_path / "forbidden-python"))
    env.pop("LC_ALL", None)
    result = subprocess.run([compiler, "--backend", "self", "--python-libpython", "off", str(source), "-o", str(out)], capture_output=True, text=True, env=env, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def _invoke(executable, path, ready, finish, *, action="try", offset=0, shared=False, gc=0):
    return [str(executable), str(path), action, str(ready), str(finish), str(offset), "shared" if shared else "exclusive"]


def _contender(command, gc):
    return subprocess.run(command, capture_output=True, text=True,
                          env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="/nonexistent"), timeout=10)


@pytest.mark.parametrize("gc", range(5))
@pytest.mark.parametrize("ending", ["unlock", "close", "crash"])
def test_real_mutex_and_automatic_release(executable, tmp_path, gc, ending):
    path = tmp_path / "stable-lockfile"
    path.write_bytes(b"anchor")
    inode = path.stat().st_ino
    ready, finish = tmp_path / "ready", tmp_path / "finish"
    command = _invoke(executable, path, ready, finish, action="close" if ending == "close" else "hold")
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="/nonexistent"))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None
            assert time.monotonic() < deadline, "native owner did not acquire lock"
            time.sleep(0.01)
        busy = _contender(_invoke(executable, path, ready, finish), gc)
        assert busy.returncode == 0 and busy.stdout == "busy\n", busy.stdout + busy.stderr
        if ending == "crash":
            child.kill()
        else:
            finish.write_text("release")
            released = Path(str(ready) + ".released")
            deadline = time.monotonic() + 10
            while not released.exists():
                assert child.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.01)
            # This acquisition must succeed while the original owner remains
            # alive. Otherwise a broken unlock/close could pass via process exit.
            assert child.poll() is None
            while_alive = _contender(_invoke(executable, path, ready, finish), gc)
            assert while_alive.returncode == 0 and while_alive.stdout == "locked\n", while_alive.stdout + while_alive.stderr
            Path(str(finish) + ".exit").write_text("exit")
        out, err = child.communicate(timeout=10)
        if ending != "crash":
            assert child.returncode == 0 and out == "released\n", out + err
        acquired = _contender(_invoke(executable, path, ready, finish), gc)
        assert acquired.returncode == 0 and acquired.stdout == "locked\n", acquired.stdout + acquired.stderr
        assert path.stat().st_ino == inode
        assert path.read_bytes() == b"anchor"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.pcc_gate(unavailable="POSIX shared flock" if os.name == "nt" else None)
def test_shared_locks_coexist_but_exclude_writer(executable, tmp_path):
    path = tmp_path / "shared-lockfile"
    path.write_bytes(b"a")
    ready, finish = tmp_path / "ready", tmp_path / "finish"
    owner = subprocess.Popen(_invoke(executable, path, ready, finish, action="hold", shared=True), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        read = _contender(_invoke(executable, path, ready, finish, shared=True), 0)
        write = _contender(_invoke(executable, path, ready, finish), 0)
        assert read.returncode == 0 and read.stdout == "locked\n"
        assert write.returncode == 0 and write.stdout == "busy\n"
    finally:
        finish.write_text("release")
        Path(str(finish) + ".exit").write_text("exit")
        try:
            owner.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.communicate(timeout=5)


@pytest.mark.pcc_gate(unavailable="Windows byte-region locking" if os.name != "nt" else None)
def test_windows_disjoint_byte_offsets_do_not_conflict(executable, tmp_path):
    path = tmp_path / "byte-lockfile"
    path.write_bytes(b"ab")
    ready, finish = tmp_path / "ready", tmp_path / "finish"
    owner = subprocess.Popen(_invoke(executable, path, ready, finish, action="hold", offset=0), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        other = _contender(_invoke(executable, path, ready, finish, offset=1), 0)
        same = _contender(_invoke(executable, path, ready, finish, offset=0), 0)
        assert other.returncode == 0 and other.stdout == "locked\n"
        assert same.returncode == 0 and same.stdout == "busy\n"
    finally:
        finish.write_text("release")
        Path(str(finish) + ".exit").write_text("exit")
        try:
            owner.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.communicate(timeout=5)


def test_native_lock_argument_errors_and_retry_after_exception(executable, tmp_path):
    path = tmp_path / "argument-lockfile"
    path.write_bytes(b"a")
    command = _invoke(executable, path, tmp_path / "ready", tmp_path / "finish", action="arguments")
    for gc in range(5):
        result = _contender(command, gc)
        assert result.returncode == 0 and result.stdout == "arguments-ok\n", (gc, result.stdout, result.stderr)


def test_host_stdlib_and_native_lock_interoperate(executable, tmp_path):
    path = tmp_path / "host-native-lockfile"
    path.write_bytes(b"a")
    with path.open("r+b") as stream:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = _contender(_invoke(executable, path, tmp_path / "ready", tmp_path / "finish"), 0)
            assert result.returncode == 0 and result.stdout == "busy\n", result.stdout + result.stderr
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
    result = _contender(_invoke(executable, path, tmp_path / "ready", tmp_path / "finish"), 0)
    assert result.returncode == 0 and result.stdout == "locked\n", result.stdout + result.stderr


def test_blocking_waiter_acquires_after_live_owner_unlocks(executable, tmp_path):
    path = tmp_path / "blocking-lockfile"
    path.write_bytes(b"a")
    ready, finish = tmp_path / "owner-ready", tmp_path / "owner-finish"
    waiting = tmp_path / "waiter-ready"
    owner = subprocess.Popen(_invoke(executable, path, ready, finish, action="hold"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    waiter = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        waiter = subprocess.Popen(_invoke(executable, path, waiting, finish, action="wait"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while not waiting.exists():
            assert waiter.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.1)
        assert waiter.poll() is None, "blocking lock returned while owner held it"
        finish.write_text("unlock")
        out, err = waiter.communicate(timeout=5)
        assert waiter.returncode == 0 and out == "locked\n", out + err
        assert owner.poll() is None, "owner exited instead of merely unlocking"
    finally:
        Path(str(finish) + ".exit").write_text("exit")
        finish.write_text("finish")
        for child in (waiter, owner):
            if child is not None and child.poll() is None:
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)


def test_blocked_file_lock_participates_in_threaded_gc(tmp_path, request):
    # Use the existing fixture only after requiring its explicit prebuilt
    # archive; the test must never implicitly build a threaded runtime.
    assert os.environ.get("PCC_THREADED_RUNTIME_ARCHIVE"), "set prebuilt PCC_THREADED_RUNTIME_ARCHIVE"
    runtime = request.getfixturevalue("threaded_pcc_runtime_archive")
    compiler = os.environ.get("PCC_FILE_LOCKING_COMPILER", "")
    assert compiler and Path(compiler).is_file(), "set PCC_FILE_LOCKING_COMPILER"
    path = tmp_path / "thread-lockfile"
    path.write_bytes(b"a")
    started, collected = tmp_path / "started", tmp_path / "collected"
    source = tmp_path / "threaded_lock.py"
    source.write_text(f'''import os
import time
import threading
import gc
from pcc.extern import extern, c_int64
threads_enabled = extern("pcc_threads_enabled", (), c_int64)
if os.name == "nt":
    import msvcrt
else:
    import fcntl

def waiter():
    with open({str(path)!r}, "r+b") as file:
        with open({str(started)!r}, "w") as marker:
            marker.write("started")
        if os.name == "nt":
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(file, fcntl.LOCK_EX)
            fcntl.flock(file, fcntl.LOCK_UN)

def main():
    assert threads_enabled() == 1
    worker = threading.Thread(target=waiter)
    worker.start()
    while not os.path.exists({str(started)!r}):
        time.sleep(0.005)
    time.sleep(0.05)
    for i in range(20):
        gc.collect()
    with open({str(collected)!r}, "w") as marker:
        marker.write("collected-while-waiting")
    worker.join()
    print("thread-lock-gc-ok")
main()
''')
    executable = tmp_path / ("threaded_lock.exe" if os.name == "nt" else "threaded_lock")
    env = dict(os.environ, PCC_WITH_THREADS="1", PCC_RUNTIME_ARCHIVE=str(runtime), PCC_SELF_LINK="pcc")
    built = subprocess.run([compiler, "--backend", "self", "--python-libpython", "off", str(source), "-o", str(executable)], env=env, capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stdout + built.stderr
    for gc_kind in (1, 2, 3, 4):
        for marker in (started, collected):
            marker.unlink(missing_ok=True)
        with path.open("r+b") as file:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = subprocess.Popen([str(executable)], env=dict(env, PCC_GC_BACKEND=str(gc_kind)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 7
                while not collected.exists():
                    assert child.poll() is None, "native threaded lock failed before GC"
                    assert time.monotonic() < deadline, "blocked lock prevented GC progress"
                    time.sleep(0.01)
            finally:
                if os.name == "nt":
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file, fcntl.LOCK_UN)
                try:
                    out, err = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    out, err = child.communicate(timeout=5)
        assert child.returncode == 0 and out == "thread-lock-gc-ok\n", (gc_kind, out, err)
