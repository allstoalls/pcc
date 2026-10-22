"""Malformed diagnostics must not erase outcomes or weaken exact comparisons."""
from dataclasses import asdict
import json
import pickle
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from tests import clang_c_cases as cases
from tests.c import test_clang_c as corpus
from utils.internal import refresh_clang_c_manifest as refresh


pytest_plugins = ["pytester"]


@pytest.mark.parametrize("raw", [b"", b"plain\r\n", "é雪".encode(), b"\xff", b"\\xff"])
def test_captured_equality_retains_bytes_and_hashes(raw):
    value = cases.CapturedOutput(raw)
    equal = cases.CapturedOutput(raw)
    assert value.raw_bytes == raw
    assert value == equal and not (value != equal)
    assert hash(value) == hash(equal)
    assert pickle.loads(pickle.dumps(value)).raw_bytes == raw
    original = raw.decode("utf-8", "surrogateescape")
    assert value == original and original == value
    assert not (value != original) and not (original != value)
    assert hash(value) == hash(original)


def test_invalid_byte_does_not_equal_literal_display_escape_or_replacement():
    invalid = cases.CapturedOutput(b"\xff")
    literal = cases.CapturedOutput(b"\\xff")
    replacement = cases.CapturedOutput("�")
    assert str(invalid) == str(literal) == r"\xff"
    for other in (literal, r"\xff", replacement, "�"):
        assert invalid != other and other != invalid
        assert not (invalid == other) and not (other == invalid)
    assert len({invalid, literal, replacement}) == 3
    assert cases.CapturedOutput(b"a\r\nb") != "a\nb"


def test_real_process_preserves_stdout_stderr_and_status():
    command = [sys.executable, "-c", (
        "import os; os.write(1, b'out\\r\\n\\xff'); "
        "os.write(2, b'error \\xe9\\r\\n'); raise SystemExit(7)"
    )]
    result = cases._run_captured(command, timeout=10)
    assert result.args == command and result.returncode == 7
    assert result.stdout.raw_bytes == b"out\r\n\xff"
    assert result.stderr.raw_bytes == b"error \xe9\r\n"
    stage = cases._stage_result("run", result)
    payload = pickle.loads(pickle.dumps(asdict(stage)))
    restored = cases.CStageResult(**payload)
    assert restored.stdout.raw_bytes == result.stdout.raw_bytes
    assert restored.stderr.raw_bytes == result.stderr.raw_bytes
    assert restored == stage


def test_reference_compilation_preserves_argv_and_reaches_original_pcc_assertion(tmp_path, monkeypatch):
    path = tmp_path / "case.c"
    source = b"// RUN: %clang_cc1 -std=c++11 -fsyntax-only -verify -x c++ %s\nint main() { return '\xe9'; }\n"
    path.write_bytes(source)
    monkeypatch.setattr(cases.shutil, "which", lambda name: "/reference/cc")
    calls = []

    def execute(argv, **options):
        calls.append((argv, options))
        assert options["text"] is False
        return subprocess.CompletedProcess(argv, 1, b"", b"bad character '\xe9'\n")

    monkeypatch.setattr(cases.subprocess, "run", execute)
    monkeypatch.setattr(corpus, "CLANG_C_TESTS_DIR", tmp_path)
    pcc_calls = []

    def owned(case_path):
        pcc_calls.append(case_path)
        return cases.CCaseResult((cases.CStageResult("compile", 0),))

    monkeypatch.setattr(corpus, "compile_pcc", owned)
    corpus.test_clang_c_compile_only_native_rejects_but_pcc_accepts_case("case.c")
    assert pcc_calls == [path]
    assert calls[0][0][:4] == ["/reference/cc", "-std=c++11", str(path), "-c"]
    assert calls[0][0][-2] == "-o" and calls[0][0][-1].endswith("a.o")
    assert path.read_bytes() == source


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_original_exact_comparison_and_refresh_reject_display_collisions(tmp_path, monkeypatch, stream):
    path = tmp_path / "case.c"
    path.write_text("int main(void) { return 0; }\n")
    monkeypatch.setattr(corpus, "CLANG_C_TESTS_DIR", tmp_path)

    def outcome(raw):
        output = {"stdout": b"", "stderr": b"", stream: raw}
        result = subprocess.CompletedProcess(["program"], 0, **output)
        return cases.CCaseResult((cases._stage_result("run", result),))

    native, owned = outcome(b"\xff"), outcome(b"\\xff")
    for module in (corpus, refresh):
        monkeypatch.setattr(module, "run_native", lambda *args: native)
        monkeypatch.setattr(module, "run_pcc", lambda *args: owned)
    with pytest.raises(AssertionError, match=stream + " mismatch"):
        corpus.test_clang_c_runtime_matches_native_exactly("case.c")
    assert refresh._classify_runtime(path) == "runtime_returncode_match_only"


def test_owned_worker_requests_bytes_and_preserves_them_in_transport(tmp_path, monkeypatch):
    path = tmp_path / "case.c"
    path.write_text("int main(void) { return 0; }\n")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", "/model/archive.a")

    class Evaluator:
        backend = "self"
        is_cross = False

        def _normalize_opt_level(self, optimize):
            return 2

        def compile_translation_units(self, units, **options):
            return units

        def emit_executable(self, units, output, **options):
            pass

    class Connection:
        def send(self, payload):
            self.payload = pickle.loads(pickle.dumps(payload))

        def close(self):
            pass

    def execute(argv, **options):
        assert options["text"] is False
        return subprocess.CompletedProcess(argv, 0, b"\xff\r\n", b"\xe9")

    monkeypatch.setattr(cases, "CEvaluator", Evaluator)
    monkeypatch.setattr(subprocess, "run", execute)
    connection = Connection()
    cases._pcc_worker_entry("run", str(path), 10, connection)
    result = cases.CCaseResult(tuple(cases.CStageResult(**s) for s in connection.payload["stages"]))
    assert result.executed
    assert result.stdout.raw_bytes == b"\xff\r\n"
    assert result.stderr.raw_bytes == b"\xe9"


def test_diagnostics_are_safe_in_json_and_pytest_junit(pytester):
    value = cases.CapturedOutput(b"invalid \xff\r\n")
    receipt = {"text": value, "raw_hex": value.raw_bytes.hex()}
    encoded = json.dumps(receipt, ensure_ascii=False).encode("utf-8")
    loaded = json.loads(encoded)
    assert bytes.fromhex(loaded["raw_hex"]) == value.raw_bytes
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in loaded["text"])
    pytester.makepyfile("""
from tests.clang_c_cases import CapturedOutput

def test_diagnostic():
    value = CapturedOutput(b"invalid \\xff")
    assert value == "different", f"diagnostic: {value}"
""")
    junit = pytester.path / "junit.xml"
    result = pytester.runpytest_subprocess("-x", "-vv", "--tb=short", "--junitxml=" + str(junit))
    result.assert_outcomes(failed=1)
    xml = junit.read_bytes().decode("utf-8")
    ET.fromstring(xml)
    assert "\\xff" in xml
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in xml)
