"""Actual native Python reader of a CPython-stamped worker-state snapshot.

The small native program compiles the exact production reader body, rather
than importing the compiler's entire worker/reporting dependency graph. Its
source hashes identify that boundary. RSS values are synthetic test inputs;
the timestamps and process IDs are real. This is not Stage2 qualification.
"""

import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import time

import pytest

from pcc.frontends.python import worker_resource_plan as policy
from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout


def _reader_source():
    body = inspect.getsource(policy.read_tree_state)
    helper = inspect.getsource(policy._set_tree_state_diagnostic)
    program = (
        "import os\nimport sys\nimport time\n"
        + "TREE_STATE_SCHEMA = " + repr(policy.TREE_STATE_SCHEMA) + "\n"
        + "STATE_MAX_AGE_SECONDS = " + repr(policy.STATE_MAX_AGE_SECONDS) + "\n\n"
        + helper + "\n" + body + "\n"
        + '''
def main():
    if sys.implementation.name != "pcc":
        raise RuntimeError("native worker clock reader requires pcc")
    state_path = sys.argv[1]
    budget = int(sys.argv[2])
    rejected_detail = {}
    if read_tree_state("", budget, os.getpid(), [], diagnostic=rejected_detail) is not None:
        raise RuntimeError("empty state path was not rejected")
    if rejected_detail["reason"] != "empty_path" or rejected_detail["line_count"] != 0:
        raise RuntimeError("native diagnostic helper did not publish rejection fields")
    before = time.monotonic()
    accepted_detail = {}
    state = read_tree_state(state_path, budget, os.getpid(), [], diagnostic=accepted_detail)
    if state is None:
        raise RuntimeError("host worker state is missing, stale, or incompatible")
    if state != (0, {}):
        raise RuntimeError("unexpected native worker-state accounting")
    if (accepted_detail["reason"] != "accepted" or accepted_detail["owner_rss"] != 1048576
            or accepted_detail["expected_budget"] != budget):
        raise RuntimeError("native diagnostic helper did not publish accepted fields")
    inclusive = read_tree_state(state_path, budget, os.getpid(), [], include_owner=True)
    if inclusive != (0, {os.getpid(): 1048576}):
        raise RuntimeError("native reader did not retain same-snapshot owner RSS")
    sleep_start = time.monotonic()
    deadline = sleep_start + 0.001
    time.sleep(0.005)
    after = time.monotonic()
    if after < deadline:
        raise RuntimeError("native monotonic deadline did not advance during awake sleep")
    print("PCC_WORKER_CLOCK_OK", before, after, after - sleep_start, os.getpid())

main()
'''
    )
    return body, program


def test_native_reader_source_is_the_exact_production_body():
    body, program = _reader_source()
    assert body == inspect.getsource(policy.read_tree_state)
    assert program.count(body) == 1
    helper = inspect.getsource(policy._set_tree_state_diagnostic)
    assert program.count(helper) == 1
    namespace = {}
    exec(program[:program.index("def main():")], namespace)
    detail = {}
    assert namespace["read_tree_state"]("", 1000, 11, [], diagnostic=detail) is None
    assert detail["reason"] == "empty_path"
    assert "STATE_MAX_AGE_SECONDS = " + repr(policy.STATE_MAX_AGE_SECONDS) in program
    compile(program, "native-worker-clock-reader.py", "exec")


def test_native_reader_rejects_cpython_fallback(tmp_path):
    _body, program = _reader_source()
    source = tmp_path / "reader.py"
    source.write_text(program, encoding="utf-8")
    result = run_process_group_timeout(
        [sys.executable, "-B", str(source)], timeout=10,
    )
    assert result.returncode != 0
    assert "native worker clock reader requires pcc" in result.stderr


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: os.name == "posix")
def test_native_worker_state_reader_matches_host_clock(
    tmp_path, explicit_owned_runtime, python_program_compiler, request, capfd,
):
    body, program = _reader_source()
    source = tmp_path / "native-worker-clock-reader.py"
    source.write_text(program, encoding="utf-8")
    binary = tmp_path / "native-worker-clock-reader.out"
    receipt = {
        "status": "COMPILING",
        "platform": sys.platform,
        "compiler_parameter": request.node.callspec.params["python_program_compiler"],
        "compiler_module": python_program_compiler.__module__,
        "backend": "self", "libpython": "off", "requested_collector": 0,
        "production_module": str(Path(policy.__file__).resolve()),
        "production_module_sha256": hashlib.sha256(Path(policy.__file__).read_bytes()).hexdigest(),
        "production_reader_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "production_diagnostic_helper_sha256": hashlib.sha256(
            inspect.getsource(policy._set_tree_state_diagnostic).encode()).hexdigest(),
        "generated_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_archive": str(explicit_owned_runtime),
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "snapshot_rss_scope": "synthetic owner RSS; actual host timestamp and PID",
        "identity_contract": "execve preserves the producer PID and kernel process-start identity",
        "qualification_scope": "native production reader body and platform clock",
        "pcc1_stage2_worker_dispatch_proved": False,
        "native_compiler_source_match_verified": False,
        "native_compiler_provenance_scope": "source match requires a separate aggregate build receipt",
    }
    if receipt["compiler_parameter"] == "pcc1":
        compiler = request.getfixturevalue("native_pcc1_compiler")
        receipt["native_compiler_path"] = str(compiler)
        receipt["native_compiler_sha256"] = hashlib.sha256(compiler.read_bytes()).hexdigest()
    receipt_path = tmp_path / "native-worker-clock.json"

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    save()
    try:
        python_program_compiler(
            str(source), str(binary), backend="self", libpython_mode="off",
            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
        )
    except BaseException as error:
        receipt.update(status="COMPILE_FAILED", error=type(error).__name__ + ": " + str(error))
        save()
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compile.stdout").write_text(captured.out)
        (tmp_path / "compile.stderr").write_text(captured.err)
    assert binary.is_file(), "native worker reader executable is missing"
    assert binary.read_bytes()[:4] in (b"\x7fELF", b"\xcf\xfa\xed\xfe"), (
        "native reader must be an emitted ELF or Mach-O executable"
    )
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()

    # The host timestamp is taken only after compilation. This CPython
    # launcher stamps its real PID, then execs the native reader with that
    # identity preserved. A separate CPython parent brackets native clocks.
    launcher = tmp_path / "publish-and-exec.py"
    launcher.write_text('''
import os
from pathlib import Path
import sys
import time
''' + "SCHEMA = " + repr(policy.TREE_STATE_SCHEMA) + "\n" + '''
binary, state, budget = sys.argv[1:]
stamp = time.monotonic()
Path(state).write_text(
    SCHEMA + "\\n" + repr(stamp) + "\\n" + budget + "\\n"
    + str(os.getpid()) + "\\t" + str(os.getppid()) + "\\t1048576\\n",
    encoding="utf-8",
)
os.execve(binary, [binary, state, budget], dict(os.environ))
''', encoding="utf-8")
    state_path = tmp_path / "host-worker-state.tsv"
    environment = dict(os.environ, PCC_GC_BACKEND="0")
    environment.pop("LC_ALL", None)
    receipt["status"] = "RUNNING"
    save()
    host_before = time.monotonic()
    result = run_process_group_timeout(
        [sys.executable, "-B", str(launcher), str(binary), str(state_path), "536870912"],
        env=environment, timeout=10,
    )
    host_after = time.monotonic()
    (tmp_path / "native.stdout").write_text(result.stdout)
    (tmp_path / "native.stderr").write_text(result.stderr)
    receipt.update(
        status="VERIFYING", returncode=result.returncode, stdout=result.stdout,
        stderr=result.stderr, host_before=host_before, host_after=host_after,
    )
    save()
    try:
        assert result.returncode == 0 and result.stderr == "", receipt
        fields = result.stdout.split()
        assert len(fields) == 5 and fields[0] == "PCC_WORKER_CLOCK_OK", receipt
        native_before, native_after = float(fields[1]), float(fields[2])
        elapsed, native_pid = float(fields[3]), int(fields[4])
        rows = state_path.read_text().splitlines()
        host_sample = float(rows[1])
        assert native_pid == int(rows[3].split("\t")[0])
        assert host_before <= host_sample <= host_after
        # Native monotonic currently has microsecond precision.
        assert host_before - 0.000001 <= native_before <= native_after <= host_after
        assert -0.000001 <= native_before - host_sample <= policy.STATE_MAX_AGE_SECONDS
        # Lower bound only: scheduler load cannot invalidate a correct timer.
        assert elapsed >= 0.001
        receipt.update(status="PASS", native_before=native_before,
                       native_after=native_after, host_sample=host_sample, native_pid=native_pid,
                       awake_sleep_elapsed_seconds=elapsed)
    except BaseException as error:
        receipt.update(status="NATIVE_VERIFICATION_FAILED",
                       error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        save()
