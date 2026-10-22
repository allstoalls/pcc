import os
import shlex
import subprocess
import sys
import time

import pytest

from pcc.frontends.python.pipeline_frontend_workers import run_worker_commands


def test_pool_stops_on_first_failure_and_cleans_running_children(tmp_path):
    slow_pid = tmp_path / "slow.pid"
    queued = tmp_path / "must-not-run"
    slow = "import os,time;from pathlib import Path;Path(" + repr(str(slow_pid)) + ").write_text(str(os.getpid()));time.sleep(20)"
    fail = "import time;time.sleep(0.15);raise SystemExit(7)"
    later = "from pathlib import Path;Path(" + repr(str(queued)) + ").write_text('bad')"
    commands = [shlex.join([sys.executable, "-c", code]) for code in (slow, fail, later)]
    started = time.monotonic()
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_worker_commands(commands, max_parallel=2)
    assert error.value.returncode == 7
    assert error.value.cmd == commands[1]
    assert time.monotonic() - started < 3
    assert not queued.exists()
    pid = int(slow_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_pool_preserves_environment_prefix_and_empty_quoted_arguments(tmp_path):
    output = tmp_path / "result"
    code = "import os,sys;from pathlib import Path;Path(sys.argv[1]).write_text(os.environ['PCC_POOL_VALUE'] + '|' + repr(sys.argv[2:]))"
    command = "PCC_POOL_VALUE='value with spaces' " + shlex.join([sys.executable, "-c", code, str(output), "", "quote'word"])
    run_worker_commands([command], max_parallel=1)
    assert output.read_text() == "value with spaces|['', \"quote'word\"]"


def test_cleanup_reaps_leader_exit_race_without_masking_worker_failure(monkeypatch):
    from pcc.frontends.python import worker_process_pool as pool

    class Process:
        def __init__(self, pid, failed):
            self.pid = pid
            self.failed = failed
            self.exited = False
            self.returncode = None

        def poll(self):
            if self.failed:
                self.returncode = 7
            elif self.exited:
                self.returncode = 0
            return self.returncode

        def wait(self, timeout):
            return self.poll()

    failure = Process(1001, True)
    peer = Process(1002, False)
    processes = iter((failure, peer))
    monkeypatch.setattr(pool.subprocess, "Popen", lambda *args, **kwargs: next(processes))
    monkeypatch.setattr(pool.time, "sleep", lambda seconds: None)

    def signal(pid, number):
        if pid == peer.pid and not peer.exited:
            peer.exited = True
            raise PermissionError("group contains only unreaped zombies")
        raise ProcessLookupError("leader has exited")

    monkeypatch.setattr(pool.os, "killpg", signal)
    result = pool._host_pool([(["failure"], []), (["peer"], [])], 2)
    assert result == (1 << 32) | 7
    assert peer.returncode == 0


def test_weighted_pool_starts_a_fitting_worker_before_its_predecessor_exits(tmp_path):
    from pcc.frontends.python.worker_process_pool import run_weighted_worker_processes

    ready = tmp_path / "ready"
    done = tmp_path / "done"
    tail = tmp_path / "tail"
    first = (
        "import time;from pathlib import Path;"
        f"ready=Path({str(ready)!r});done=Path({str(done)!r});"
        "deadline=time.monotonic()+5\n"
        "while not ready.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        "assert ready.exists()\n"
        "done.write_text('ok')\n"
    )
    second = f"from pathlib import Path;Path({str(ready)!r}).write_text('ok')"
    third = (
        f"from pathlib import Path;assert Path({str(ready)!r}).exists();"
        f"Path({str(tail)!r}).write_text('ok')"
    )
    commands = [shlex.join([sys.executable, "-c", code])
                for code in (first, second, third)]
    run_weighted_worker_processes(commands, [7, 3, 3], 3, 10)
    assert done.read_text() == tail.read_text() == "ok"


def test_native_pool_runs_native_children_and_stops_on_failure(tmp_path, pcc_runtime_archive):
    from pathlib import Path
    from pcc.frontends.python.pipeline import compile_python, compile_python_multi
    from pcc.frontends.python import worker_process_pool

    child_source = tmp_path / "child.py"
    child = tmp_path / "child"
    child_source.write_text('''
import os
import sys
import time
def main():
    mode = sys.argv[1]
    if mode == "slow":
        with open(sys.argv[2], "w") as stream:
            stream.write(str(os.getpid()))
        time.sleep(20)
    elif mode == "fail":
        time.sleep(0.15)
        sys.exit(7)
    else:
        with open(sys.argv[2], "w") as stream:
            stream.write("must not run")
main()
''')
    compile_python(str(child_source), str(child), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    parent_source = tmp_path / "parent.py"
    parent = tmp_path / "parent"
    parent_source.write_text('''
import sys
import subprocess
from pcc.frontends.python.worker_process_pool import run_worker_processes, run_weighted_worker_processes
def main():
    child = sys.argv[1]
    commands = [child + " slow " + sys.argv[2], child + " fail", child + " later " + sys.argv[3]]
    try:
        run_worker_processes(commands, 2)
    except subprocess.CalledProcessError as error:
        print(error.returncode)
    try:
        run_weighted_worker_processes(commands, [7, 3, 3], 3, 10)
    except subprocess.CalledProcessError as error:
        print(error.returncode)
main()
''')
    compile_python_multi(
        [str(Path(worker_process_pool.__file__)), str(parent_source)], str(parent),
        module_names=["pcc.frontends.python.worker_process_pool", "pool_parent"],
        entry_module="pool_parent", recursive_stdlib=True,
        backend="self", libpython_mode="off", runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        pid_file = tmp_path / f"child-{gc}.pid"
        marker = tmp_path / f"later-{gc}"
        result = subprocess.run([str(parent), str(child), str(pid_file), str(marker)],
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "7\n7\n", result.stderr
        assert not marker.exists()
        assert pid_file.is_file()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
