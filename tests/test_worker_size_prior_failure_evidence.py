"""Host-only coverage of the existing prior gate's failure receipt writer."""

import errno
import hashlib
import json
import linecache
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.python import test_worker_size_prior_processes as prior
from tests.python import process_timeout


@pytest.fixture
def reporting_inputs(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source.py"
    source.write_text("synthetic source identity only\n")
    archive = tmp_path / "synthetic-runtime.a"
    archive.write_bytes(b"synthetic archive identity only")
    monkeypatch.setattr(prior, "DRIVER", source)

    def no_process(*args, **kwargs):
        pytest.fail("receipt tests must never launch a process")

    monkeypatch.setattr(prior, "run_process_group_timeout", no_process)
    monkeypatch.setattr(prior, "_run", no_process)
    return archive


def _write_receipt(tmp_path, archive, compiler, capfd, owner="pcc0"):
    request = SimpleNamespace(node=SimpleNamespace(callspec=SimpleNamespace(
        params={"python_program_compiler": owner},
    )))
    prior.test_size_prior_native_processes_five_collectors(
        tmp_path, archive, compiler, request, capfd,
    )


def _receipt(tmp_path):
    return json.loads((tmp_path / "native-worker-size-priors.json").read_text())


def _synthetic_compile(source, output, **options):
    # This is inert header-shaped data for the existing format check. No
    # compiler/runtime fixture is requested, and these bytes are never run.
    header = (b"MZ" if sys.platform == "win32" else
              b"\xcf\xfa\xed\xfe" if sys.platform == "darwin" else b"\x7fELF")
    Path(output).write_bytes(header + b"synthetic receipt test")


@pytest.mark.parametrize("owner", ["pcc0", "pcc1"])
def test_compile_failure_retains_context_and_original_exception(
    tmp_path, reporting_inputs, capfd, owner,
):
    error = PermissionError(errno.EPERM, "synthetic compile permission failure")

    def denied():
        raise error

    def compiler(*args, **kwargs):
        print("synthetic compiler stdout")
        print("synthetic compiler stderr", file=sys.stderr)
        denied()

    with pytest.raises(PermissionError) as raised:
        _write_receipt(tmp_path, reporting_inputs, compiler, capfd, owner)
    assert raised.value is error
    receipt = _receipt(tmp_path)
    assert receipt["status"] == "COMPILE_FAILED"
    assert receipt["error"] == "PermissionError: " + str(error)
    assert receipt["compiler_parameter"] == owner
    assert receipt["compiler_module"] == compiler.__module__
    assert receipt["pcc1_stage2_dispatch"] is False
    assert receipt["executions"] == [] and "binary_sha256" not in receipt
    assert receipt["source_sha256"] == hashlib.sha256(prior.DRIVER.read_bytes()).hexdigest()
    assert receipt["runtime_archive_sha256"] == hashlib.sha256(reporting_inputs.read_bytes()).hexdigest()
    context = receipt["exception_context"]
    assert context["type"] == "PermissionError"
    assert [frame["name"] for frame in context["traceback"]] == [
        "test_size_prior_native_processes_five_collectors", "compiler", "denied",
    ]
    assert context["traceback"][-1]["filename"] == __file__
    traceback = error.__traceback__
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert context["traceback"][-1]["lineno"] == traceback.tb_lineno
    assert context["frames_truncated"] is context["text_truncated"] is False
    assert (tmp_path / "compile.stdout").read_text() == "synthetic compiler stdout\n"
    assert (tmp_path / "compile.stderr").read_text() == "synthetic compiler stderr\n"


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_compile_base_exception_keeps_existing_classification(
    tmp_path, reporting_inputs, capfd, kind,
):
    error = kind("synthetic interruption")

    def compiler(*args, **kwargs):
        print("synthetic interrupted compile")
        raise error

    with pytest.raises(kind) as raised:
        _write_receipt(tmp_path, reporting_inputs, compiler, capfd)
    assert raised.value is error
    receipt = _receipt(tmp_path)
    assert receipt["status"] == "COMPILING"
    assert "error" not in receipt and "exception_context" not in receipt
    assert receipt["executions"] == []
    assert (tmp_path / "compile.stdout").read_text() == "synthetic interrupted compile\n"
    assert (tmp_path / "compile.stderr").read_text() == ""


@pytest.mark.parametrize(("kind", "failed_index"), [(TimeoutError, 0), (KeyboardInterrupt, 3)])
def test_execution_failure_retains_partial_receipt_without_reclassification(
    tmp_path, reporting_inputs, capfd, monkeypatch, kind, failed_index,
):
    error = kind("synthetic execution failure")
    calls = []

    def run(prefix, case, directory, owner, collector, compiler_parameter):
        calls.append((case, collector))
        if len(calls) == failed_index + 1:
            raise error
        return {"case": case, "requested_collector": collector, "status": "PASS"}

    monkeypatch.setattr(prior, "_run", run)
    with pytest.raises(kind) as raised:
        _write_receipt(tmp_path, reporting_inputs, _synthetic_compile, capfd)
    assert raised.value is error
    receipt = _receipt(tmp_path)
    assert receipt["status"] == "NATIVE_EXECUTION_FAILED"
    assert receipt["error"] == kind.__name__ + ": " + str(error)
    assert (receipt["failed_case"], receipt["failed_collector"]) == calls[-1]
    assert len(receipt["executions"]) == failed_index
    assert receipt["pcc1_stage2_dispatch"] is False
    context = receipt["exception_context"]
    assert context["type"] == kind.__name__
    assert [frame["name"] for frame in context["traceback"]] == [
        "test_size_prior_native_processes_five_collectors", "run",
    ]
    assert context["frames_truncated"] is context["text_truncated"] is False


def test_success_receipt_does_not_add_exception_context(
    tmp_path, reporting_inputs, capfd, monkeypatch,
):
    calls = []

    def run(prefix, case, directory, owner, collector, compiler_parameter):
        calls.append((case, collector, owner, compiler_parameter))
        return {"case": case, "requested_collector": collector, "status": "PASS"}

    monkeypatch.setattr(prior, "_run", run)
    _write_receipt(tmp_path, reporting_inputs, _synthetic_compile, capfd)
    receipt = _receipt(tmp_path)
    assert receipt["status"] == "PASS" and len(receipt["executions"]) == 10
    assert "exception_context" not in receipt and "error" not in receipt
    assert calls == [(case, collector, "pcc", "pcc0")
                     for collector in range(5) for case in prior.CASES]


@pytest.mark.parametrize("depth", [0, 14, 15, 40])
def test_exception_context_keeps_bounded_innermost_frames(depth):
    def descend(remaining):
        if remaining:
            descend(remaining - 1)
        else:
            raise ValueError("synthetic deep error")

    try:
        descend(depth)
    except ValueError as error:
        context = prior._exception_context(error)
    assert len(context["traceback"]) == min(depth + 2, 16)
    assert context["frames_truncated"] is (depth + 2 > 16)
    assert context["text_truncated"] is False
    assert context["traceback"][-1]["name"] == "descend"
    if depth + 2 > 16:
        assert {frame["name"] for frame in context["traceback"]} == {"descend"}


def test_exception_context_bounds_names_without_reading_source_or_values(monkeypatch):
    secret = "synthetic-credential-do-not-record"
    monkeypatch.setenv("PCC_TEST_SYNTHETIC_SECRET", secret)
    name = "synthetic_" + "n" * 700
    filename = "synthetic-" + "p" * 2000 + ".py"
    kind = type("Synthetic" + "E" * 200, (Exception,), {})
    namespace = {"error": kind(secret), "local_value": secret}
    # If traceback formatting loaded a source line, it would copy this literal.
    source = ("def " + name + "():\n"
              "    local_secret = local_value\n"
              "    raise error  # " + secret + "\n")
    exec(compile(source, filename, "exec"), namespace)
    cause = RuntimeError(secret)
    namespace["error"].__cause__ = cause
    namespace["error"].add_note(secret)
    try:
        namespace[name]()
    except kind as error:
        def no_source_lookup(*args, **kwargs):
            pytest.fail("exception context must not load source lines")

        with monkeypatch.context() as patch:
            patch.setattr(linecache, "getline", no_source_lookup)
            patch.setattr(linecache, "getlines", no_source_lookup)
            context = prior._exception_context(error)
    assert context["type"] == kind.__name__[:128]
    assert context["traceback"][-1] == {
        "filename": filename[-512:], "lineno": 3, "name": name[-128:],
    }
    assert context["text_truncated"] is True
    assert context["frames_truncated"] is False
    assert secret not in json.dumps(context)
    assert all(set(frame) == {"filename", "lineno", "name"} for frame in context["traceback"])


def test_exception_context_without_traceback_is_explicitly_empty():
    assert prior._exception_context(ValueError("synthetic unraised error")) == {
        "type": "ValueError", "traceback": [],
        "frames_truncated": False, "text_truncated": False,
    }


@pytest.mark.parametrize("kind", [
    KeyboardInterrupt, SystemExit, TimeoutError, subprocess.TimeoutExpired,
])
def test_compile_cleanup_failure_keeps_original_exception_chain(
    tmp_path, reporting_inputs, capfd, monkeypatch, kind,
):
    secret = "synthetic-interruption-message-do-not-record"
    original = (kind([secret], 20) if kind is subprocess.TimeoutExpired else kind(secret))
    cleanup_error = PermissionError(errno.EPERM, "synthetic cleanup permission failure")
    calls = []

    class Child:
        pid = 123456789  # Never passed to an operating-system signal function.

        def communicate(self, timeout):
            calls.append(("communicate", timeout))
            raise original

        def poll(self):
            calls.append(("poll",))
            return None

    child = Child()

    def popen(command, **options):
        calls.append(("popen", command, options["start_new_session"]))
        return child

    def denied(pid, signum):
        calls.append(("killpg", pid, signum))
        raise cleanup_error

    def no_process_or_signal(*args, **kwargs):
        pytest.fail("chain receipt tests must never launch a process or send a signal")

    # Replace module dependencies rather than mutating the global os/subprocess
    # modules used by pytest. Exercise the POSIX helper on every host with only
    # inert doubles, including no real signal-handler installation.
    monkeypatch.setattr(process_timeout, "os", SimpleNamespace(name="posix", killpg=denied))
    monkeypatch.setattr(process_timeout, "subprocess", SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess, run=no_process_or_signal,
    ))
    monkeypatch.setattr(process_timeout, "signal", SimpleNamespace(
        SIGINT=2, SIGTERM=15, SIGKILL=9,
        getsignal=no_process_or_signal, signal=no_process_or_signal,
    ))
    monkeypatch.setattr(process_timeout, "threading_is_main_thread", lambda: False)

    def compiler(*args, **kwargs):
        print("synthetic interrupted compiler stdout")
        print("synthetic interrupted compiler stderr", file=sys.stderr)
        process_timeout.run_process_group_timeout(["synthetic-command-never-run"], timeout=20)

    with pytest.raises(PermissionError) as raised:
        _write_receipt(tmp_path, reporting_inputs, compiler, capfd)
    assert raised.value is cleanup_error
    assert cleanup_error.__context__ is original
    assert cleanup_error.__cause__ is None
    receipt = _receipt(tmp_path)
    assert receipt["status"] == "COMPILE_FAILED"
    assert receipt["error"] == "PermissionError: " + str(cleanup_error)
    assert receipt["executions"] == [] and "binary_sha256" not in receipt
    context = receipt["exception_context"]
    assert context["type"] == "PermissionError"
    assert [frame["name"] for frame in context["traceback"]] == [
        "test_size_prior_native_processes_five_collectors", "compiler",
        "run_process_group_timeout", "_run_process_group",
        "_finish_after_process_group_kill", "_kill_process_group", "denied",
    ]
    assert context["chain_truncated"] is context["chain_cycle_detected"] is False
    assert len(context["chain"]) == 1
    linked = context["chain"][0]
    assert linked["relation"] == "context" and linked["type"] == kind.__name__
    assert [frame["name"] for frame in linked["traceback"]] == [
        "_run_process_group", "communicate",
    ]
    assert linked["traceback"][0]["filename"] == process_timeout.__file__
    assert linked["traceback"][-1]["filename"] == __file__
    assert linked["frames_truncated"] is linked["text_truncated"] is False
    assert secret not in json.dumps(receipt)
    assert calls == [
        ("popen", ["synthetic-command-never-run"], True),
        ("communicate", 20), ("killpg", child.pid, 15), ("poll",),
    ]
    assert (tmp_path / "compile.stdout").read_text() == "synthetic interrupted compiler stdout\n"
    assert (tmp_path / "compile.stderr").read_text() == "synthetic interrupted compiler stderr\n"


def test_exception_chain_prefers_explicit_cause_and_respects_suppression():
    error = RuntimeError("synthetic outer error")
    error.__context__ = KeyboardInterrupt("synthetic hidden interruption")
    error.__suppress_context__ = True
    unchained = prior._exception_context(error)
    assert unchained == {
        "type": "RuntimeError", "traceback": [],
        "frames_truncated": False, "text_truncated": False,
    }
    error.__cause__ = ValueError("synthetic explicit cause")
    context = prior._exception_context(error)
    assert {key: context[key] for key in unchained} == unchained
    assert context["chain"] == [{
        "relation": "cause", "type": "ValueError", "traceback": [],
        "frames_truncated": False, "text_truncated": False,
    }]
    assert context["chain_truncated"] is context["chain_cycle_detected"] is False


@pytest.mark.parametrize("depth", [0, 1, 7, 8, 9, 20])
def test_exception_chain_bounds_linked_exceptions(depth):
    errors = [RuntimeError("synthetic chain message") for _ in range(depth + 1)]
    for index in range(depth):
        errors[index].__context__ = errors[index + 1]
    context = prior._exception_context(errors[0])
    if depth == 0:
        assert set(context) == {"type", "traceback", "frames_truncated", "text_truncated"}
    else:
        assert len(context["chain"]) == min(depth, 8)
        assert context["chain_truncated"] is (depth > 8)
        assert context["chain_cycle_detected"] is False
        assert all(item["relation"] == "context" for item in context["chain"])


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize("length", [1, 2, 9])
def test_exception_chain_stops_at_identity_cycles(link, length):
    class CyclicError(Exception):
        def __eq__(self, other):
            pytest.fail("exception chaining must compare identity only")

    errors = [CyclicError("synthetic cycle") for _ in range(length)]
    for index, error in enumerate(errors):
        setattr(error, link, errors[(index + 1) % length])
    context = prior._exception_context(errors[0])
    assert len(context["chain"]) == length - 1
    assert context["chain_cycle_detected"] is True
    assert context["chain_truncated"] is False


def test_exception_chain_bounds_each_snapshot_without_capturing_values(monkeypatch):
    secret = "synthetic-chained-credential-do-not-record"
    filename = "synthetic-" + "p" * 2000 + ".py"
    name = "synthetic_" + "n" * 700

    def no_message_or_source(*args, **kwargs):
        pytest.fail("chained snapshots must not format messages or read source lines")

    kind = type("Synthetic" + "E" * 200, (Exception,), {
        "__str__": no_message_or_source, "__repr__": no_message_or_source,
    })
    linked = kind(secret)
    linked.add_note(secret)
    namespace = {"error": linked, "secret_value": secret}
    source = ("def " + name + "(remaining):\n"
              "    local_secret = secret_value\n"
              "    if remaining:\n"
              "        " + name + "(remaining - 1)\n"
              "    else:\n"
              "        raise error  # " + secret + "\n")
    exec(compile(source, filename, "exec"), namespace)
    monkeypatch.setenv("PCC_TEST_SYNTHETIC_CHAIN_SECRET", secret)
    try:
        namespace[name](40)
    except kind:
        pass
    outer = RuntimeError("synthetic outer message")
    outer.__context__ = linked
    with monkeypatch.context() as patch:
        patch.setattr(linecache, "getline", no_message_or_source)
        patch.setattr(linecache, "getlines", no_message_or_source)
        context = prior._exception_context(outer)
    snapshot = context["chain"][0]
    assert snapshot["type"] == kind.__name__[:128]
    assert len(snapshot["traceback"]) == 16
    assert snapshot["traceback"][-1] == {
        "filename": filename[-512:], "lineno": 6, "name": name[-128:],
    }
    assert snapshot["frames_truncated"] is snapshot["text_truncated"] is True
    assert all(set(frame) == {"filename", "lineno", "name"} for frame in snapshot["traceback"])
    assert secret not in json.dumps(context)


def test_exception_context_requires_actual_exceptions():
    decoy = SimpleNamespace(__traceback__=None, __context__=None, __cause__=None)
    with pytest.raises(TypeError, match="requires a BaseException"):
        prior._exception_context(decoy)

    class DecoyCauseError(Exception):
        @property
        def __cause__(self):
            return decoy

    assert prior._exception_context(DecoyCauseError()) == {
        "type": "DecoyCauseError", "traceback": [],
        "frames_truncated": False, "text_truncated": False,
    }


def test_compile_receipt_preserves_error_with_raising_chain_attribute_hooks(
    tmp_path, reporting_inputs, capfd,
):
    accessed = []

    class ChainAttributeError(PermissionError):
        def __getattribute__(self, name):
            if name in {"__cause__", "__context__", "__suppress_context__"}:
                accessed.append(name)
                raise RuntimeError("synthetic chain attribute hook must not run")
            return super().__getattribute__(name)

    error = ChainAttributeError(errno.EPERM, "synthetic original permission failure")
    BaseException.__context__.__set__(error, KeyboardInterrupt("synthetic hidden message"))

    def compiler(*args, **kwargs):
        raise error

    with pytest.raises(ChainAttributeError) as raised:
        _write_receipt(tmp_path, reporting_inputs, compiler, capfd)
    assert raised.value is error
    receipt = _receipt(tmp_path)
    assert receipt["status"] == "COMPILE_FAILED"
    assert receipt["error"] == "ChainAttributeError: " + str(error)
    context = receipt["exception_context"]
    assert context["type"] == "ChainAttributeError"
    assert [frame["name"] for frame in context["traceback"]] == [
        "test_size_prior_native_processes_five_collectors", "compiler",
    ]
    assert context["chain"][0]["relation"] == "context"
    assert context["chain"][0]["type"] == "KeyboardInterrupt"
    assert "synthetic hidden message" not in json.dumps(receipt)
    assert accessed == []


def test_exception_chain_reads_linked_builtin_slots_without_attribute_hooks():
    accessed = []
    secret = "synthetic-linked-secret-do-not-record"

    class HostileName(type):
        @property
        def __name__(cls):
            accessed.append("type name")
            raise RuntimeError("synthetic type-name hook must not run")

    class HostileLinkedError(Exception, metaclass=HostileName):
        def __getattribute__(self, name):
            if name in {"__cause__", "__context__", "__suppress_context__", "__traceback__"}:
                accessed.append(name)
                raise RuntimeError("synthetic exception attribute hook must not run")
            return super().__getattribute__(name)

        def __str__(self):
            accessed.append("str")
            raise RuntimeError("synthetic exception str must not run")

        def __repr__(self):
            accessed.append("repr")
            raise RuntimeError("synthetic exception repr must not run")

    linked = HostileLinkedError(secret)
    linked.add_note(secret)

    def raise_linked():
        raise linked

    try:
        raise_linked()
    except BaseException:
        pass
    BaseException.__cause__.__set__(linked, ValueError(secret))
    outer = PermissionError("synthetic cleanup permission failure")
    outer.__context__ = linked
    context = prior._exception_context(outer)
    assert [(item["relation"], item["type"]) for item in context["chain"]] == [
        ("context", "HostileLinkedError"), ("cause", "ValueError"),
    ]
    assert [frame["name"] for frame in context["chain"][0]["traceback"]] == [
        "test_exception_chain_reads_linked_builtin_slots_without_attribute_hooks", "raise_linked",
    ]
    assert context["chain_truncated"] is context["chain_cycle_detected"] is False
    assert secret not in json.dumps(context)
    assert accessed == []
