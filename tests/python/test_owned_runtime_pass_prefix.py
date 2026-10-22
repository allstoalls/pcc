"""Runtime-only no-op prefix elision, with bounded real IR/object proofs."""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.frontends.python import pipeline
from pcc.frontends.python.compiled_owned_passes import run_owned_passes
from pcc.ir.optimization.driver import optimize_ir


PREFIX = "mem2reg,sroa"
TAIL = "instsimplify,inline-defined,instsimplify,instcombine,dce"
PASSES = PREFIX + "," + TAIL
IR = "define i64 @probe() {\nentry:\n  ret i64 7\n}\n"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_runtime_pass_environment(monkeypatch):
    for key in (
        "PCC_RUNTIME_PYTHON_IR_PASSES", "PCC_RUNTIME_IR_PASSES",
        "PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES", "PCC_PY_DEBUG_INFO",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("mode, expected", [
    (None, PREFIX), ("default", PREFIX), (" DEFAULT ", PREFIX),
    ("off", ""), ("false", ""), ("no", ""), ("0", ""),
    ("none", ""), (PREFIX, ""), (PREFIX + ",dce", ""), ("fast", ""),
])
def test_frontend_prefix_evidence_respects_runtime_mode(tmp_path, monkeypatch, mode, expected):
    if mode is not None:
        monkeypatch.setenv("PCC_RUNTIME_PYTHON_IR_PASSES", mode)
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "caller-custom")
    observed = []

    def compile_member(source, output, **options):
        observed.append(os.environ["PCC_PYTHON_IR_PASSES"])
        assert options == dict(emit_llvm_only=True, python_library=True,
                               libpython_mode="off", backend="self", target_triple="arm64-apple-darwin")

    monkeypatch.setattr(pipeline, "compile_python", compile_member)
    result = owned._compile_runtime_module("probe", str(tmp_path / "probe.py"),
                                           str(tmp_path / "probe.ll"), "arm64-apple-darwin")
    assert result == expected
    assert observed == [(mode or "default").strip()]
    assert os.environ["PCC_PYTHON_IR_PASSES"] == "caller-custom"


@pytest.mark.parametrize("bypass", ["skip", "unsafe-module"])
def test_frontend_bypasses_supply_no_prefix_evidence(tmp_path, monkeypatch, bypass):
    source = tmp_path / "pcc" / "frontends" / "python" / "codegen" / "probe.py"
    source.parent.mkdir(parents=True)
    parent = source.parent
    while parent != tmp_path:
        (parent / "__init__.py").write_text("")
        parent = parent.parent
    if bypass == "skip":
        monkeypatch.setenv("PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES", "package.")
        source = tmp_path / "package" / "probe.py"
        source.parent.mkdir()
        (source.parent / "__init__.py").write_text("")
    monkeypatch.setattr(pipeline, "compile_python", lambda *args, **kwargs: None)
    assert owned._compile_runtime_module("probe", str(source), str(tmp_path / "probe.ll"),
                                          "arm64-apple-darwin") == ""


@pytest.mark.parametrize("fail", [False, True])
def test_pthread_kernel_poll_override_and_caller_mode_restore(tmp_path, monkeypatch, fail):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "caller-mode")

    def compile_member(*args, **kwargs):
        # Suppress implicit kernel polls, not the threaded runtime identity.
        assert os.environ["PCC_WITH_THREADS"] == "1"
        assert os.environ["PCC_IMPLICIT_THREAD_SAFEPOINTS"] == "0"
        assert os.environ["PCC_PYTHON_IR_PASSES"] == "default"
        if fail:
            raise ValueError("controlled compile failure")

    monkeypatch.setattr(pipeline, "compile_python", compile_member)
    args = (owned._THREAD_KERNEL_MODULE, str(tmp_path / "kernel.py"),
            str(tmp_path / "kernel.ll"), "arm64-apple-darwin")
    for caller_polls in (None, "0", "1"):
        if caller_polls is None:
            monkeypatch.delenv("PCC_IMPLICIT_THREAD_SAFEPOINTS", raising=False)
        else:
            monkeypatch.setenv("PCC_IMPLICIT_THREAD_SAFEPOINTS", caller_polls)
        if fail:
            with pytest.raises(ValueError, match="controlled compile failure"):
                owned._compile_runtime_module(*args)
        else:
            owned._compile_runtime_module(*args)
        assert os.environ["PCC_WITH_THREADS"] == "1"
        assert os.environ.get("PCC_IMPLICIT_THREAD_SAFEPOINTS") == caller_polls
        assert os.environ["PCC_PYTHON_IR_PASSES"] == "caller-mode"


@pytest.mark.parametrize("passes, expected", [
    (PASSES, TAIL), (PREFIX, None), ("dce," + PREFIX, "dce," + PREFIX),
    ("mem2reg, sroa,dce", "mem2reg, sroa,dce"), ("sroa,mem2reg", "sroa,mem2reg"),
    ("", ""), (PREFIX + ",", PREFIX + ","), (PREFIX + ",,dce", ",dce"),
])
def test_only_exact_leading_pair_is_elided(passes, expected):
    assert owned._runtime_passes_after_frontend(passes, PREFIX, IR) == expected


@pytest.mark.parametrize("evidence", [None, "", "mem2reg", "sroa,mem2reg"])
def test_unknown_or_custom_frontend_keeps_full_passes(evidence):
    assert owned._runtime_passes_after_frontend(PASSES, evidence, IR) == PASSES


@pytest.mark.parametrize("override", [PASSES, PREFIX, "dce", "none"])
def test_explicit_runtime_override_is_preserved(monkeypatch, override):
    monkeypatch.setenv("PCC_RUNTIME_IR_PASSES", override)
    assert owned._runtime_passes_after_frontend(override, PREFIX, IR) == override


def test_strict_frontend_fallback_cannot_claim_applied_prefix():
    fallback = IR.replace("  ret i64 7", "  %r = call i64 @py_cpy_probe()\n  ret i64 %r")
    assert run_owned_passes(fallback, ["mem2reg", "sroa"], True) == fallback
    assert owned._runtime_passes_after_frontend(PASSES, PREFIX, fallback) == PASSES


def test_pointer_spill_exposes_second_round_phi_and_is_not_elided():
    text = """define i64 @branch_spill(i1 %cond) {
entry:
  %a = alloca i64
  %p = alloca ptr
  store ptr %a, ptr %p
  %q = load ptr, ptr %p
  br i1 %cond, label %yes, label %no
yes:
  store i64 1, ptr %q
  br label %merge
no:
  store i64 2, ptr %q
  br label %merge
merge:
  %r = load i64, ptr %q
  ret i64 %r
}
"""
    first = run_owned_passes(text, ["mem2reg", "sroa"], True)
    second = run_owned_passes(first, ["mem2reg", "sroa"], False)
    assert first != second and "alloca" in first and " phi " in second
    assert owned._runtime_passes_after_frontend(PASSES, PREFIX, first) == PASSES


@pytest.mark.parametrize("passes, evidence, expected", [
    (PASSES, PREFIX, [TAIL]), (PREFIX, PREFIX, []), (PASSES, None, [PASSES]),
])
def test_archive_applies_remaining_tail_but_never_to_pthread_kernel(
    tmp_path, monkeypatch, passes, evidence, expected,
):
    from pcc.backend import ar_writer, owned_object_emit
    from pcc.frontends.python import pipeline_runtime_archive as locks
    from pcc.ir.optimization import driver
    from pcc.tools import runtime_archive_provenance as provenance

    # Isolate orchestration from provisioning: these are fake members/objects,
    # not a reduced production archive or a claim about full-runtime admission.
    # Parent-local mocks exercise the serial pass pipeline, not worker builds.
    monkeypatch.setenv("PCC_RUNTIME_BUILD_JOBS", "1")
    source_dir = tmp_path / "py"
    source_dir.mkdir()
    for name in ("probe", owned._THREAD_KERNEL_MODULE):
        (source_dir / (name + ".py")).write_text("")

    def reject_workers(*args, **kwargs):
        pytest.fail("mocked runtime pass fixture must not start workers")

    monkeypatch.setattr(owned, "_build_runtime_modules_in_workers", reject_workers)
    monkeypatch.setattr(owned, "require_native_provisioning_allowed", lambda: None)
    monkeypatch.setattr(provenance, "codegen_checksum", lambda: "a" * 64)
    monkeypatch.setattr(owned, "runtime_ir_passes", lambda _root: passes)
    monkeypatch.setattr(owned, "runtime_modules", lambda *args: ["probe", owned._THREAD_KERNEL_MODULE])
    monkeypatch.setattr(locks, "_acquire_runtime_build_lock", lambda *args: None)
    monkeypatch.setattr(locks, "_remove_runtime_build_lock", lambda *args: None)
    monkeypatch.setattr(owned_object_emit, "emit_owned_object", lambda *args: b"object")
    monkeypatch.setattr(ar_writer, "write_archive", lambda members: b"archive")
    monkeypatch.setattr(ar_writer, "_defined_symbols", lambda data: ("test", []))
    monkeypatch.setattr(provenance, "write_pcc_python_receipt", lambda **kwargs: None)
    monkeypatch.setattr(provenance, "assemble_runtime_archive_manifest",
                        lambda archive, *args, **kwargs: Path(str(archive) + ".provenance.json").write_text("{}"))

    def frontend(name, source, output, target):
        Path(output).write_text(IR)
        return evidence

    observed = []
    monkeypatch.setattr(owned, "_compile_runtime_module", frontend)
    monkeypatch.setattr(driver, "optimize_ir", lambda text, selected: observed.append(selected) or text)
    owned.build_runtime_archive(str(tmp_path), str(tmp_path / "runtime.a"), "x86_64-unknown-linux-gnu")
    assert observed == expected
    assert (tmp_path / "runtime.a").read_bytes() == b"archive"


def _probe_source(tmp_path):
    source = tmp_path / "runtime" / "py" / "probe.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "__pcc_freestanding__ = True\n"
        "@c_abi_export('runtime_prefix_probe')\n"
        "def probe(value: i64) -> i64:\n"
        "    result: i64 = 17\n"
        "    if value > 0:\n"
        "        result = 29\n"
        "    return result + 3\n"
    )
    return source


def _assert_same_ir_and_object_bytes(tmp_path, text, selected):
    from pcc.backend.owned_object_emit import emit_owned_object

    old = optimize_ir(text, PASSES)
    new = optimize_ir(text, selected)
    (tmp_path / "remaining-passes.txt").write_text(selected)
    (tmp_path / "before.ll").write_text(old)
    (tmp_path / "after.ll").write_text(new)
    assert old.encode() == new.encode()
    old_object = emit_owned_object(old, "x86_64-unknown-linux-gnu")
    new_object = emit_owned_object(new, "x86_64-unknown-linux-gnu")
    (tmp_path / "before.o").write_bytes(old_object)
    (tmp_path / "after.o").write_bytes(new_object)
    assert old_object == new_object


@pytest.mark.parametrize("mode, skip, expected", [
    (None, False, PREFIX), ("off", False, ""), (PREFIX + ",dce", False, ""),
    (None, True, ""),
])
def test_real_frontend_tail_and_owned_object_match_old_pipeline(tmp_path, monkeypatch, mode, skip, expected):
    source = _probe_source(tmp_path)
    if mode is not None:
        monkeypatch.setenv("PCC_RUNTIME_PYTHON_IR_PASSES", mode)
    if skip:
        monkeypatch.setenv("PCC_PYTHON_IR_PASS_SKIP_MODULE_PREFIXES", "probe")
    ir_path = tmp_path / "probe.ll"
    evidence = owned._compile_runtime_module("probe", str(source), str(ir_path), "x86_64-unknown-linux-gnu")
    assert evidence == expected
    text = ir_path.read_text()
    selected = owned._runtime_passes_after_frontend(PASSES, evidence, text)
    if expected:
        assert "alloca " not in text
        assert selected == TAIL
    else:
        assert selected == PASSES
    _assert_same_ir_and_object_bytes(tmp_path, text, selected)


def test_invalid_none_frontend_mode_still_fails(tmp_path, monkeypatch):
    source = _probe_source(tmp_path)
    monkeypatch.setenv("PCC_RUNTIME_PYTHON_IR_PASSES", "none")
    with pytest.raises(pipeline.PyPipelineError, match="does not own requested passes: none"):
        owned._compile_runtime_module("probe", str(source), str(tmp_path / "probe.ll"), "x86_64-unknown-linux-gnu")


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_real_default_prefix_output_executes(tmp_path):
    from pcc.backend.owned_object_emit import emit_owned_object

    compiler = shutil.which("cc")
    assert compiler, "runtime prefix execution proof requires the host C harness linker"
    source = _probe_source(tmp_path)
    ir_path = tmp_path / "probe.ll"
    evidence = owned._compile_runtime_module("probe", str(source), str(ir_path), "x86_64-unknown-linux-gnu")
    text = ir_path.read_text()
    selected = owned._runtime_passes_after_frontend(PASSES, evidence, text)
    assert selected == TAIL
    data = emit_owned_object(optimize_ir(text, selected), "x86_64-unknown-linux-gnu")
    obj = tmp_path / "probe.o"
    obj.write_bytes(data)
    harness = tmp_path / "main.c"
    harness.write_text("long long runtime_prefix_probe(long long);\n"
                       "int main(void) { return runtime_prefix_probe(1) == 32 && "
                       "runtime_prefix_probe(0) == 20 && runtime_prefix_probe(-1) == 20 ? 0 : 1; }\n")
    executable = tmp_path / "probe"
    built = subprocess.run([compiler, str(harness), str(obj), "-o", str(executable)],
                           capture_output=True, text=True, timeout=15)
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", ["freestanding_signal_set", owned._THREAD_KERNEL_MODULE, "py_capi_stdio_runtime"])
def test_real_runtime_members_preserve_ir_and_object_bytes(tmp_path, name):
    source = ROOT / "pcc/runtime/py" / (name + ".py")
    ir_path = tmp_path / (name + ".ll")
    evidence = owned._compile_runtime_module(name, str(source), str(ir_path), "x86_64-unknown-linux-gnu")
    text = ir_path.read_text()
    selected = owned._runtime_passes_after_frontend(PASSES, evidence, text)
    _assert_same_ir_and_object_bytes(tmp_path, text, selected)


def test_short_ci_includes_prefix_regressions_and_path_contract():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    path = "tests/python/test_owned_runtime_pass_prefix.py"
    assert workflow.count('      - "' + path + '"') == 2
    host = workflow.split("- name: Verify host Stage2 export-retention, AArch64 call-emission, and ELF assembly contracts\n", 1)[1].split("\n\n", 1)[0]
    assert path in host
    assert "python -m pytest -x -q" in host and "-n0" not in host and "addopts" not in host


@pytest.mark.parametrize("fail", [False, True])
def test_runtime_frontend_isolates_and_restores_application_debug_flag(tmp_path, monkeypatch, fail):
    monkeypatch.setenv("PCC_PY_DEBUG_INFO", "1")

    def compile_member(*args, **kwargs):
        assert "PCC_PY_DEBUG_INFO" not in os.environ
        if fail:
            raise ValueError("controlled debug-isolation failure")

    monkeypatch.setattr(pipeline, "compile_python", compile_member)
    args = ("probe", str(tmp_path / "probe.py"), str(tmp_path / "probe.ll"), "arm64-apple-darwin")
    if fail:
        with pytest.raises(ValueError, match="controlled debug-isolation"):
            owned._compile_runtime_module(*args)
    else:
        assert owned._compile_runtime_module(*args) == PREFIX
    assert os.environ["PCC_PY_DEBUG_INFO"] == "1"


def test_real_runtime_member_has_valid_phi_and_application_keeps_debug(tmp_path, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.backend.macho_spec import parse_object
    monkeypatch.setenv("PCC_PY_DEBUG_INFO", "1")
    source = tmp_path / "probe.py"
    source.write_text("def choose(flag):\n    value = 1\n    if flag:\n        value = 2\n    return value\n")
    runtime_ir = tmp_path / "runtime.ll"
    prefix = owned._compile_runtime_module("probe", str(source), str(runtime_ir), "arm64-apple-darwin")
    text = runtime_ir.read_text()
    assert "!dbg" not in text and "!DICompileUnit" not in text
    remaining = owned._runtime_passes_after_frontend(PASSES, prefix, text)
    optimized = optimize_ir(text, remaining) if remaining else text
    assert emit_owned_object(optimized, "arm64-apple-darwin")
    assert os.environ["PCC_PY_DEBUG_INFO"] == "1"
    application_ir = tmp_path / "application.ll"
    pipeline.compile_python(str(source), str(application_ir), emit_llvm_only=True,
                            libpython_mode="off", backend="self", target_triple="arm64-apple-darwin")
    application = application_ir.read_text()
    assert "!dbg" in application and "!DICompileUnit" in application
    obj = parse_object(emit_owned_object(application, "arm64-apple-darwin"))
    assert any(section["sectname_str"] == "__debug_line" for section in obj.sections())
