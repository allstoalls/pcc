"""Host-only observations of the real worker with every compiler callback mocked."""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_self_backend_emit as emit


PREFIX = "pcc self backend worker phase "
SOURCE = "private source payload: é\n"
ASSEMBLY = "private assembly payload\n"
PAYLOAD = b"mock object\x00\xff"


# Frozen 0272b8f4fdcb999eb14ee63fb257b27dfcc0dd49 worker; parsed, never executed.
_WORKER_PREIMAGE = r'''def run_emit_worker(
    ir_path: str,
    result_path: str,
    obj_path: str = "",
    cc: str = "",
    *,
    normalize_ir,
    emit_native,
    emit_native_object=None,
) -> int:
    try:
        if cc and sys.implementation.name == "pcc":
            raise SelfBackendEmitError("native self emission cannot invoke an external assembler")
        with open(ir_path, "r", encoding="utf-8") as stream:
            ir_text = normalize_ir(stream.read())
        packed_result = None
        if obj_path and not cc and obj_path.endswith(".pco") and emit_native_object is not None:
            packed_result = emit_native_object(ir_text)
        asm_text = ""
        if packed_result is None:
            native_result = emit_native(ir_text)
            if native_result is None:
                raise SelfBackendEmitError(
                    "native emitter does not support the module target"
                )
            target_id, asm_text = native_result
        else:
            target_id, payload = packed_result
        result_payload = asm_text
        if obj_path and not cc and obj_path.endswith(".pco"):
            # Owned link: assemble here, in parallel with the other emit
            # workers, and hand the linker a packed native object.  The link
            # used to assemble every module's text serially in one process;
            # under pcc1 that was most of a multi-module compile.
            if packed_result is None:
                from pcc.backend.arm64_asm_driver import assemble_file
                from pcc.backend.native_object import encode_native_object_from_sections

                if target_id in ("self-aarch64-linux-v0", "self-x86_64-linux-v0"):
                    from pcc.backend.owned_elf_link import assemble as assemble_elf
                    from pcc.backend.elf_x86_64 import emit_relocatable
                    triple = "aarch64-unknown-linux-gnu" if target_id == "self-aarch64-linux-v0" else "x86_64-unknown-linux-gnu"
                    payload = emit_relocatable(assemble_elf(asm_text, triple))
                elif target_id == "self-x86_64-windows-v0":
                    from pcc.backend.coff_x86_64 import assemble_object
                    payload = assemble_object(asm_text)
                else:
                    sections, undefined = assemble_file(asm_text)
                    payload = encode_native_object_from_sections(sections, undefined=undefined)
            asm_text = ""
            with open(obj_path, "wb") as stream:
                stream.write(payload)
            result_payload = obj_path
        elif obj_path:
            asm_path = obj_path if not cc else result_path + ".s"
            with open(asm_path, "w", encoding="utf-8") as stream:
                stream.write(asm_text)
            if cc:
                subprocess.run([cc, "-c", asm_path, "-o", obj_path], check=True)
            result_payload = obj_path
        with open(result_path, "w", encoding="utf-8") as stream:
            stream.write(target_id + "\n")
            stream.write(result_payload)
        return 0
    except Exception as exc:
        sys.stderr.write("self backend emit worker failed: " + str(exc) + "\n")
        return 1
'''


def _forbidden(*_args, **_kwargs):
    raise AssertionError("this host test must not compile, assemble, or spawn a real worker")


@pytest.fixture(autouse=True)
def _mock_only(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    for name in ("system", "fork", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _forbidden)
    monkeypatch.setattr(emit, "emit_in_process", _forbidden)
    monkeypatch.setattr(emit, "emit_packed_object_in_process", _forbidden)
    monkeypatch.setattr(emit, "run_split_worker", _forbidden)


class _Sink:
    def __init__(self):
        self.lines = []
        self.flushes = 0
        self.failure = None

    def write(self, text):
        if text.startswith(PREFIX) and self.failure is not None:
            raise self.failure
        self.lines.append(text)

    def flush(self):
        if self.failure is not None:
            raise self.failure
        self.flushes += 1

    def records(self):
        return [dict(field.split("=", 1) for field in line[len(PREFIX):].split())
                for line in self.lines if line.startswith(PREFIX)]


class _Harness:
    def __init__(self, monkeypatch, route="packed", flag="1", failure_at="", failure=None):
        self.calls = []
        self.outputs = {}
        self.sink = _Sink()
        self.clock_calls = 0
        self.pid_calls = 0
        self.failure_at = failure_at
        self.failure = failure if failure is not None else RuntimeError("private operation failure")
        self.route = route
        self.input = "/private/input.ll"
        self.result = "/private/output.result"
        self.obj = "" if route == "result_only" else "/private/output.pco"
        if route in ("assembly", "external"):
            self.obj = "/private/output.s"
        self.cc = "private-cc" if route in ("external", "native_cc") else ""
        self.target = {
            "macho": "self-aarch64-darwin-v0", "coff": "self-x86_64-windows-v0",
        }.get(route, "self-aarch64-linux-v0")
        self.packed = None if route == "absent" else self._packed
        self.texts = {self.input: SOURCE}
        environment = {} if flag is None else {"PCC_PY_FRONTEND_WORKER_TIMING": flag}
        monkeypatch.setattr(emit, "os", SimpleNamespace(environ=environment, getpid=self._pid))
        monkeypatch.setattr(emit, "time", SimpleNamespace(monotonic=self._clock))
        monkeypatch.setattr(emit._worker_phase_observer, "time", emit.time)
        monkeypatch.setattr(emit, "sys", SimpleNamespace(
            stderr=self.sink, implementation=SimpleNamespace(name="cpython" if route == "external" else "pcc"),
        ))
        monkeypatch.setattr(emit._worker_phase_observer, "sys", emit.sys)
        monkeypatch.setattr(emit, "subprocess", SimpleNamespace(run=self._external))
        monkeypatch.setattr(emit, "open", self._open, raising=False)
        monkeypatch.setattr(emit, "_worker_timing_item", 0)
        modules = {
            "pcc.backend.arm64_asm_driver": {"assemble_file": self._assemble_file},
            "pcc.backend.native_object": {"encode_native_object_from_sections": self._encode_sections},
            "pcc.backend.owned_elf_link": {"assemble": self._assemble_elf},
            "pcc.backend.elf_x86_64": {"emit_relocatable": self._encode_elf},
            "pcc.backend.coff_x86_64": {"assemble_object": self._coff},
        }
        for name, values in modules.items():
            module = ModuleType(name)
            module.__dict__.update(values)
            monkeypatch.setitem(sys.modules, name, module)

    def _step(self, name, *args):
        self.calls.append((name, args))
        if name == self.failure_at:
            raise self.failure

    def _pid(self):
        self.pid_calls += 1
        return 7422

    def _clock(self):
        self.clock_calls += 1
        return 100.0 + self.clock_calls * 0.25

    def _open(self, path, mode, encoding=None):
        self._step("open", path, mode, encoding)
        harness = self

        class Stream:
            def __enter__(self):
                harness._step("enter", path)
                return self

            def __exit__(self, kind, error, traceback):
                harness._step("close", path)

            def read(self):
                harness._step("read", path)
                return harness.texts[path]

            def write(self, data):
                operation = "write_result" if path == harness.result else "write_object"
                harness._step(operation, path, data)
                empty = b"" if isinstance(data, bytes) else ""
                harness.outputs[path] = harness.outputs.get(path, empty) + data

        return Stream()

    def _normalize(self, value):
        self._step("normalize", value)
        assert value is SOURCE
        return value

    def _packed(self, value):
        self._step("packed", value)
        if self.route == "packed":
            return "self-x86_64-linux-v0", PAYLOAD
        return None

    def _emit(self, value):
        self._step("emit", value)
        return None if self.route == "unsupported" else (self.target, ASSEMBLY)

    def _assemble_elf(self, assembly, triple):
        self._step("assemble", assembly, triple)
        return "mock-sections"

    def _encode_elf(self, sections):
        self._step("encode", sections)
        return PAYLOAD

    def _assemble_file(self, assembly):
        self._step("assemble", assembly)
        return "mock-sections", "mock-undefined"

    def _encode_sections(self, sections, *, undefined):
        self._step("encode", sections, undefined)
        return PAYLOAD

    def _coff(self, assembly):
        self._step("assemble", assembly)
        self._step("encode", "mock-sections")
        return PAYLOAD

    def _external(self, args, *, check):
        self._step("external", args, check)

    def run(self):
        return emit.run_emit_worker(
            self.input, self.result, self.obj, self.cc,
            normalize_ir=self._normalize, emit_native=self._emit, emit_native_object=self.packed,
        )


@pytest.mark.parametrize("route,phases", [
    ("packed", ["read_normalize", "packed_emit_assemble", "object_write", "result_write"]),
    ("declined", ["read_normalize", "packed_emit_assemble", "emit", "assemble_encode", "object_write", "result_write"]),
    ("absent", ["read_normalize", "emit", "assemble_encode", "object_write", "result_write"]),
    ("macho", ["read_normalize", "packed_emit_assemble", "emit", "assemble", "encode", "object_write", "result_write"]),
    ("coff", ["read_normalize", "packed_emit_assemble", "emit", "assemble_encode", "object_write", "result_write"]),
    ("assembly", ["read_normalize", "emit", "assembly_write", "result_write"]),
    ("result_only", ["read_normalize", "emit", "result_write"]),
    ("external", ["read_normalize", "emit", "assembly_write", "external_assemble", "result_write"]),
])
def test_worker_events_preserve_callbacks_and_output_bytes(monkeypatch, route, phases):
    runs = []
    for flag in ("0", "1"):
        harness = _Harness(monkeypatch, route, flag)
        assert harness.run() == 0
        runs.append((harness.calls, harness.outputs))
        records = harness.sink.records()
        if flag == "0":
            assert records == []
            assert harness.clock_calls == harness.pid_calls == emit._worker_timing_item == 0
            continue
        assert [(row["event"], row["phase"]) for row in records] == [
            (event, phase) for phase in phases for event in ("begin", "complete")
        ]
        assert {row["pid"] for row in records} == {"7422"}
        assert {row["item"] for row in records} == {"1"}
        assert all(row["clock_ok"] == "1" for row in records)
        assert harness.sink.flushes == len(records) == harness.clock_calls
        assert "private" not in "".join(harness.sink.lines)
        if "packed_emit_assemble" in phases:
            complete = next(row for row in records if row["phase"] == "packed_emit_assemble" and row["event"] == "complete")
            assert complete["outcome"] == ("returned" if route == "packed" else "declined")
    assert runs[0] == runs[1]


@pytest.mark.parametrize("route,operation,phase", [
    ("packed", "open", "read_normalize"),
    ("packed", "read", "read_normalize"),
    ("packed", "normalize", "read_normalize"),
    ("packed", "packed", "packed_emit_assemble"),
    ("declined", "emit", "emit"),
    ("declined", "assemble", "assemble_encode"),
    ("declined", "encode", "assemble_encode"),
    ("macho", "assemble", "assemble"),
    ("macho", "encode", "encode"),
    ("packed", "write_object", "object_write"),
    ("assembly", "write_object", "assembly_write"),
    ("external", "external", "external_assemble"),
    ("packed", "write_result", "result_write"),
])
def test_worker_failure_never_completes_failed_scope(monkeypatch, route, operation, phase):
    runs = []
    for flag in ("0", "1"):
        harness = _Harness(monkeypatch, route, flag, operation)
        assert harness.run() == 1
        errors = [line for line in harness.sink.lines if not line.startswith(PREFIX)]
        runs.append((harness.calls, harness.outputs, errors))
        records = harness.sink.records()
        if flag == "1":
            assert [(row["event"], row["phase"]) for row in records[-2:]] == [("begin", phase), ("failed", phase)]
            assert "private" not in "".join(line for line in harness.sink.lines if line.startswith(PREFIX))
        else:
            assert not records and harness.clock_calls == harness.pid_calls == 0
    assert runs[0] == runs[1]


def test_context_close_failure_is_not_reported_complete(monkeypatch):
    harness = _Harness(monkeypatch, failure_at="close")
    assert harness.run() == 1
    assert [(row["event"], row["phase"]) for row in harness.sink.records()] == [
        ("begin", "read_normalize"), ("failed", "read_normalize"),
    ]


@pytest.mark.parametrize("failure", [KeyboardInterrupt("private"), SystemExit(17)])
def test_base_exception_identity_is_preserved(monkeypatch, failure):
    harness = _Harness(monkeypatch, failure_at="packed", failure=failure)
    with pytest.raises(type(failure)) as caught:
        harness.run()
    assert caught.value is failure
    assert harness.sink.records()[-1]["event"] == "failed"
    assert harness.sink.records()[-1]["phase"] == "packed_emit_assemble"


@pytest.mark.parametrize("flag", [None, "", "0", "false", "unrecognized"])
def test_disabled_diagnostics_are_cold(monkeypatch, flag):
    harness = _Harness(monkeypatch, flag=flag)
    monkeypatch.setattr(emit, "_worker_item_timing_context", _forbidden)
    monkeypatch.setattr(emit, "_worker_item_phase_diagnostic", _forbidden)
    monkeypatch.setattr(emit.time, "monotonic", _forbidden)
    monkeypatch.setattr(emit.os, "getpid", _forbidden)
    monkeypatch.setattr(harness.sink, "write", _forbidden)
    monkeypatch.setattr(harness.sink, "flush", _forbidden)
    assert harness.run() == 0
    assert harness.sink.lines == []
    assert harness.clock_calls == harness.pid_calls == emit._worker_timing_item == 0


@pytest.mark.parametrize("failure", [OSError("private sink"), KeyboardInterrupt(), SystemExit(7)])
def test_diagnostic_sink_failures_do_not_change_success_or_original_failure(monkeypatch, failure):
    for operation in ("", "packed"):
        harness = _Harness(monkeypatch, failure_at=operation)
        harness.sink.failure = failure
        assert harness.run() == (1 if operation else 0)
        assert harness.sink.lines == (["self backend emit worker failed: private operation failure\n"] if operation else [])


@pytest.mark.parametrize("clock", [lambda: 0.0, lambda: -1.0, lambda: float("nan"), lambda: float("inf"), _forbidden])
def test_clock_failure_marks_timing_unavailable_without_changing_work(monkeypatch, clock):
    for operation in ("", "packed"):
        harness = _Harness(monkeypatch, failure_at=operation)
        monkeypatch.setattr(emit.time, "monotonic", clock)
        assert harness.run() == (1 if operation else 0)
        assert all(row["clock_ok"] == "0" and row["monotonic_s"] == "0.0" for row in harness.sink.records())
        assert harness.sink.records()[-1]["event"] == ("failed" if operation else "complete")


def test_flag_read_failure_is_optional(monkeypatch):
    harness = _Harness(monkeypatch)
    monkeypatch.setattr(emit, "_worker_phase_timing_enabled", _forbidden)
    assert harness.run() == 0
    assert harness.clock_calls == harness.pid_calls == 0
    assert not harness.sink.lines


def test_missing_pid_is_explicit_and_item_ordinals_are_process_local(monkeypatch):
    harness = _Harness(monkeypatch)
    monkeypatch.setattr(emit.os, "getpid", _forbidden)
    assert harness.run() == harness.run() == 0
    records = harness.sink.records()
    assert {row["pid"] for row in records} == {"-1"}
    assert [row["item"] for row in records if row["event"] == "begin" and row["phase"] == "read_normalize"] == ["1", "2"]
    harness.failure_at = "packed"
    assert harness.run() == 1
    assert harness.sink.records()[-1]["event"] == "failed"
    assert harness.sink.records()[-1]["item"] == "3"


def test_diagnostics_cannot_replace_base_exception_during_unwinding(monkeypatch):
    original = KeyboardInterrupt("private original")
    harness = _Harness(monkeypatch, failure_at="packed", failure=original)
    harness.sink.failure = SystemExit(7)
    with pytest.raises(KeyboardInterrupt) as caught:
        harness.run()
    assert caught.value is original


@pytest.mark.parametrize("route", ["native_cc", "unsupported"])
def test_existing_target_contracts_still_fail_closed(monkeypatch, route):
    harness = _Harness(monkeypatch, route)
    assert harness.run() == 1
    assert not harness.outputs
    if route == "native_cc":
        assert harness.calls == harness.sink.records() == []
    else:
        assert harness.sink.records()[-1]["event"] == "failed"
        assert harness.sink.records()[-1]["phase"] == "emit"


def test_records_are_bounded_and_only_fixed_labels_are_allowed(monkeypatch):
    harness = _Harness(monkeypatch)
    emit._worker_item_phase_diagnostic("complete", "encode", 10 ** 1200, 1, "private outcome")
    assert len(harness.sink.lines) == 1
    assert len(harness.sink.lines[0]) <= 1024 and harness.sink.lines[0].count("\n") == 1
    emit._worker_item_phase_diagnostic("private event", "encode", 1, 1)
    emit._worker_item_phase_diagnostic("begin", "private phase", 1, 1)
    assert len(harness.sink.lines) == 1
    assert "private" not in "".join(harness.sink.lines)


def test_flag_off_operational_ast_matches_frozen_preimage():
    class RemoveDiagnostics(ast.NodeTransformer):
        def visit_If(self, node):
            if isinstance(node.test, ast.Name) and node.test.id.startswith("timing_"):
                return None
            if isinstance(node.test, ast.BoolOp) and all(
                isinstance(value, ast.Name) and value.id.startswith("timing_")
                for value in node.test.values
            ):
                return None
            return self.generic_visit(node)

        def visit_Assign(self, node):
            if all(isinstance(target, ast.Name) and target.id.startswith("timing_") for target in node.targets):
                return None
            return self.generic_visit(node)

        def visit_Try(self, node):
            if (len(node.body) == 1 and isinstance(node.body[0], ast.Assign)
                    and isinstance(node.body[0].targets[0], ast.Name)
                    and node.body[0].targets[0].id == "timing_enabled"):
                return None
            return self.generic_visit(node)

    tree = ast.parse(inspect.getsource(emit.run_emit_worker))
    stripped = RemoveDiagnostics().visit(tree)
    assert ast.dump(stripped, include_attributes=False) == ast.dump(
        ast.parse(_WORKER_PREIMAGE), include_attributes=False,
    )
