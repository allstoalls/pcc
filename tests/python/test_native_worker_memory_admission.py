"""Execute the native RSS boundary and the resulting admission decision."""

import os
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline import compile_python, compile_python_multi
from pcc.frontends.python import pipeline_frontend_workers, worker_process_pool


PROBE = r'''
import os
from pcc.extern import extern, c_int64
from pcc.frontends.python.pipeline_frontend_workers import compiled_native_export_jobs, compiled_native_summary_plan, compiled_native_worker_budget

rss = extern("pcc_os_current_rss_bytes", (), c_int64)
collector = extern("pcc_gc_backend", (), c_int64)


def main() -> None:
    observed = rss()
    print("rss-positive", observed > 0)
    print("collector", collector())
    # This small probe must sit below the existing 3 GiB phase floor.  If it
    # does not, fail the precondition rather than masking it with a wide cap.
    print("fits-phase-floor", observed + 134217728 < 3221225472)
    os.environ["PCC_WORKER_TREE_BUDGET_BYTES"] = "4294967296"
    print("jobs", compiled_native_export_jobs(10))
    os.environ["PCC_WORKER_TREE_BUDGET_BYTES"] = "1"
    refused = False
    try:
        compiled_native_export_jobs(1)
    except ValueError:
        refused = True
    print("insufficient-refused", refused)
    os.environ["PCC_WORKER_TREE_BUDGET_BYTES"] = "8589934592"
    os.environ["PCC_GC_BACKEND"] = "0"  # the real runtime selector must win
    batch = [3253019] * 7 + [3253021]
    chunks, jobs, admission = compiled_native_summary_plan(10, batch * 10, 2381604)
    print("summary-collector", admission["collector"])
    print("summary-jobs", jobs)
    print("summary-chunks", len(chunks))
    print("driver-fits-floor", rss() + 134217728 < 1073741824)
    print("driver-budget", compiled_native_worker_budget(8589934592, 1073741824))

main()
'''


# the existing owned RSS boundary covers Darwin/Linux
@pytest.mark.pcc_gate(probe=lambda: not (os.name != "posix"))
def test_native_rss_selects_budget_and_refuses_an_unfit_worker(
    tmp_path, pcc_runtime_archive,
):
    source = tmp_path / "memory_admission.py"
    executable = tmp_path / "memory_admission"
    source.write_text(PROBE, encoding="utf-8")
    compile_python_multi(
        [str(Path(pipeline_frontend_workers.__file__)), str(source)], str(executable),
        module_names=["pcc.frontends.python.pipeline_frontend_workers", "memory_admission"],
        entry_module="memory_admission", recursive_stdlib=True,
        backend="self", libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in (0, 1, 2, 3, 4):
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = str(backend)
        env["PCC_WORKER_TREE_BUDGET_BYTES"] = str(8 * 1024**3)
        result = subprocess.run(
            [str(executable)], env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout.strip().splitlines() == [
            "rss-positive True", "collector " + str(backend),
            "fits-phase-floor True", "jobs 2", "insufficient-refused True",
            "summary-collector " + str(backend),
            "summary-jobs " + str((5, 3, 3, 4, 2)[backend]), "summary-chunks 10",
            "driver-fits-floor True", "driver-budget 7516192768",
        ], (backend, result.stdout, result.stderr)


CHILD_PROBE = r'''
import sys
from pcc.extern import extern, c_int64
collector = extern("pcc_gc_backend", (), c_int64)
def main() -> None:
    with open(sys.argv[1], "w") as stream:
        stream.write(str(collector()))
main()
'''

PARENT_PROBE = r'''
import os
import sys
from pcc.extern import extern, c_int64
from pcc.frontends.python.pipeline_frontend_workers import compiled_native_summary_plan, worker_env_prefix, shell_quote_arg
from pcc.frontends.python.worker_process_pool import run_worker_processes
collector = extern("pcc_gc_backend", (), c_int64)
def main() -> None:
    actual = collector()  # initialize actual selector before changing its env hint
    os.environ["PCC_GC_BACKEND"] = sys.argv[3]
    os.environ["PCC_WORKER_TREE_BUDGET_BYTES"] = "8589934592"
    chunks, jobs, admission = compiled_native_summary_plan(1, [1024], 1024)
    command = worker_env_prefix(timing_enabled=False) + " " + admission["worker_env"]
    command += " " + shell_quote_arg(sys.argv[1]) + " " + shell_quote_arg(sys.argv[2])
    run_worker_processes([command], jobs)
    print("parent", actual)
    print("planned", admission["collector"])
    with open(sys.argv[2], "r") as stream:
        print("child", stream.read())
main()
'''


# the existing owned RSS boundary covers Darwin/Linux
@pytest.mark.pcc_gate(probe=lambda: not (os.name != "posix"))
def test_native_summary_child_uses_the_planned_collector_snapshot(
    tmp_path, pcc_runtime_archive, monkeypatch,
):
    child_source, child = tmp_path / "collector_child.py", tmp_path / "collector_child"
    child_source.write_text(CHILD_PROBE, encoding="utf-8")
    compile_python(str(child_source), str(child), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    parent_source, parent = tmp_path / "collector_parent.py", tmp_path / "collector_parent"
    parent_source.write_text(PARENT_PROBE, encoding="utf-8")
    compile_python_multi(
        [str(Path(pipeline_frontend_workers.__file__)), str(Path(worker_process_pool.__file__)), str(parent_source)],
        str(parent), module_names=["pcc.frontends.python.pipeline_frontend_workers",
                                  "pcc.frontends.python.worker_process_pool", "collector_parent"],
        entry_module="collector_parent", recursive_stdlib=True, backend="self",
        libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    for actual, conflicting in ((0, 4), (1, 0)):
        output = tmp_path / ("child-" + str(actual))
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = str(actual)
        result = subprocess.run([str(parent), str(child), str(output), str(conflicting)],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.read_text() == str(actual)
        assert result.stdout.strip().splitlines() == [
            "parent " + str(actual), "planned " + str(actual), "child " + str(actual),
        ]
    # The CPython projection must bind the same selected snapshot too.
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    _chunks, jobs, admission = pipeline_frontend_workers.compiled_native_summary_plan(1, [1024], 1024)
    monkeypatch.setenv("PCC_GC_BACKEND", "0")
    output = tmp_path / "host-planned-child"
    command = (pipeline_frontend_workers.worker_env_prefix(timing_enabled=False) + " "
               + admission["worker_env"] + " " + pipeline_frontend_workers.shell_quote_arg(str(child))
               + " " + pipeline_frontend_workers.shell_quote_arg(str(output)))
    worker_process_pool.run_worker_processes([command], jobs)
    assert admission["collector"] == 4
    assert output.read_text() == "4"
