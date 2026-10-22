"""A chained weighted pool runs each follow-up as soon as its primary exits.

``run_chained_worker_processes`` backs frontend -> PCO pipelining: module
``i``'s PCO job follows its frontend job in the same pool, with a reservation
computed from the sidecar the frontend wrote.  These checks pin the contract
on the host implementation and on the pcc-Python runtime entry
``pcc_chained_worker_process_pool`` (native parent), under all collectors.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python.worker_process_pool import run_chained_worker_processes


def _py(code: str) -> str:
    return shlex.join([sys.executable, "-c", code])


def _scenario(tmp_path: Path):
    """Primary 0 waits until follow-up 1 has run: only a pool that starts a
    follow-up while another primary is still running can finish."""
    out = tmp_path / "out"
    out.mkdir()
    size_a = out / "a.pidx"
    size_b = out / "b.pidx"
    done_b = out / "b.pco"
    log = out / "log"
    primary_a = _py(
        "import time;from pathlib import Path;"
        f"Path({str(size_a)!r}).write_bytes(b'x'*10);"
        f"deadline=time.monotonic()+5\n"
        f"while not Path({str(done_b)!r}).exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        f"assert Path({str(done_b)!r}).exists()\n"
    )
    primary_b = _py(f"from pathlib import Path;Path({str(size_b)!r}).write_bytes(b'y'*20)")
    follow_a = _py(
        f"from pathlib import Path;assert Path({str(size_a)!r}).exists();"
        f"open({str(log)!r},'a').write('a\\n')"
    )
    follow_b = _py(
        f"from pathlib import Path;assert Path({str(size_b)!r}).exists();"
        f"Path({str(done_b)!r}).write_text('ok');open({str(log)!r},'a').write('b\\n')"
    )
    return [primary_a, primary_b], [follow_a, follow_b], [size_a, size_b], log


def test_followup_starts_while_another_primary_still_runs(tmp_path):
    primaries, followups, paths, log = _scenario(tmp_path)
    run_chained_worker_processes(
        primaries, [3, 3], followups, paths, (1, 0, 10), 3, 10,
    )
    assert sorted(log.read_text().split()) == ["a", "b"]


def test_followup_failure_names_the_followup_and_skips_nothing_else(tmp_path):
    marker = tmp_path / "never"
    primaries = [_py(f"open({str(tmp_path / 'p.pidx')!r},'w').write('z')")]
    followups = [_py("raise SystemExit(5)")]
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_chained_worker_processes(
            primaries, [1], followups, [tmp_path / "p.pidx"], (1, 0, 10), 2, 10,
        )
    assert error.value.returncode == 5
    assert error.value.cmd == followups[0]
    assert not marker.exists()


def test_failed_primary_never_runs_its_followup(tmp_path):
    ran = tmp_path / "ran"
    primaries = [_py("raise SystemExit(3)")]
    followups = [_py(f"open({str(ran)!r},'w').write('x')")]
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_chained_worker_processes(
            primaries, [1], followups, [tmp_path / "missing"], (1, 0, 10), 2, 10,
        )
    assert error.value.returncode == 3
    assert error.value.cmd == primaries[0]
    assert not ran.exists()


def test_missing_followup_input_is_a_followup_failure(tmp_path):
    primaries = [_py("pass")]
    followups = [_py("pass")]
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_chained_worker_processes(
            primaries, [1], followups, [tmp_path / "missing"], (1, 0, 10), 2, 10,
        )
    assert error.value.returncode == 127
    assert error.value.cmd == followups[0]


def test_native_chained_pool_matches_the_host_contract(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python import worker_process_pool
    from pcc.frontends.python.pipeline import compile_python_multi

    parent_source = tmp_path / "parent.py"
    parent = tmp_path / "parent"
    parent_source.write_text('''
import sys
import subprocess
from pcc.frontends.python.worker_process_pool import run_chained_worker_processes
def main():
    primaries = sys.argv[1].split("|")
    followups = sys.argv[2].split("|")
    paths = sys.argv[3].split("|")
    try:
        run_chained_worker_processes(primaries, [3, 3], followups, paths, (1, 0, 10), 3, 10)
        print("ok")
    except subprocess.CalledProcessError as error:
        print("failed", error.returncode)
main()
''')
    compile_python_multi(
        [str(Path(worker_process_pool.__file__)), str(parent_source)], str(parent),
        module_names=["pcc.frontends.python.worker_process_pool", "chained_parent"],
        entry_module="chained_parent", recursive_stdlib=True,
        backend="self", libpython_mode="off", runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        case = tmp_path / f"gc{gc}"
        case.mkdir()
        primaries, followups, paths, log = _scenario(case)
        result = subprocess.run(
            [str(parent), "|".join(primaries), "|".join(followups),
             "|".join(str(path) for path in paths)],
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok\n", result.stdout + result.stderr
        assert sorted(log.read_text().split()) == ["a", "b"]
