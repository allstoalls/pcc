import os
import shlex
import subprocess
import sys
import time

import pytest

from pcc.py_frontend.pipeline_frontend_workers import run_worker_commands


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


def test_native_pool_runs_native_children_and_stops_on_failure(tmp_path, pcc_py_runtime_archive):
    from pathlib import Path
    from pcc.py_frontend.pipeline import compile_python, compile_python_multi
    from pcc.py_frontend import worker_process_pool

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
                   runtime_archive=str(pcc_py_runtime_archive))
    parent_source = tmp_path / "parent.py"
    parent = tmp_path / "parent"
    parent_source.write_text('''
import sys
import subprocess
from pcc.py_frontend.worker_process_pool import run_worker_processes
def main():
    child = sys.argv[1]
    commands = [child + " slow " + sys.argv[2], child + " fail", child + " later " + sys.argv[3]]
    try:
        run_worker_processes(commands, 2)
    except subprocess.CalledProcessError as error:
        print(error.returncode)
main()
''')
    compile_python_multi(
        [str(Path(worker_process_pool.__file__)), str(parent_source)], str(parent),
        module_names=["pcc.py_frontend.worker_process_pool", "pool_parent"],
        entry_module="pool_parent", recursive_stdlib=True,
        backend="self", libpython_mode="off", runtime_archive=str(pcc_py_runtime_archive),
    )
    for gc in range(5):
        pid_file = tmp_path / f"child-{gc}.pid"
        marker = tmp_path / f"later-{gc}"
        result = subprocess.run([str(parent), str(child), str(pid_file), str(marker)],
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "7\n", result.stderr
        assert not marker.exists()
        assert pid_file.is_file()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
