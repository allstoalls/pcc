"""Wheel admission through the canonical selector, with a tiny frontend fixture.

Objects, archives, source/configuration receipts and the wheel's copied payloads
are real. Runtime frontend compilation and the build-hook child are simulated;
these tests do not qualify a full runtime or an installed native compiler.
"""

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
from tests.test_runtime_archive_consumers import _load_hatch_build, _new_build_hook


TARGETS = (
    "arm64-apple-darwin", "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
)


def _tiny_runtime(tmp_path, monkeypatch, target):
    root = tmp_path / "source"
    runtime = root / "pcc" / "runtime"
    (runtime / "py").mkdir(parents=True)
    (runtime / "Makefile").write_text(
        "PY_MODULES = wheel_probe\nPCC_RUNTIME_IR_PASSES ?= mem2reg,sroa\n",
        encoding="utf-8",
    )
    for threaded in (False, True):
        for name in owned.runtime_modules(str(runtime), target, threaded):
            (runtime / "py" / (name + ".py")).write_text(
                "def probe():\n    return 37\n", encoding="utf-8",
            )
    compiled = []

    def frontend(name, source, ir_path, selected_target):
        compiled.append((name, Path(source), selected_target))
        value = int(Path(source).read_text().split("return ", 1)[1].strip())
        Path(ir_path).write_text(
            'target triple = "' + selected_target + '"\n'
            + "define i32 @PyWheelProbe_" + name + "() {\n"
            + "entry:\n  ret i32 " + str(value) + "\n}\n",
            encoding="utf-8",
        )

    # Parent-local mocks exercise the serial build, not worker processes.
    monkeypatch.setenv("PCC_RUNTIME_BUILD_JOBS", "1")
    monkeypatch.setattr(owned, "_compile_runtime_module", frontend)
    for name in ("PCC_RUNTIME_BUILD", "PCC_WITH_THREADS", "PCC_RUNTIME_IR_PASSES",
                 "PCC_REFCOUNT_KIND", "PCC_RUNTIME_DIR", "PCC_RUNTIME_ARCHIVE"):
        monkeypatch.delenv(name, raising=False)
    module = _load_hatch_build(monkeypatch)
    hook = _new_build_hook(module, root)
    monkeypatch.setattr(module._build_targets, "host_target_triple", lambda: target)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:2] == [sys.executable, "-c"]
        assert Path(kwargs["cwd"]) == root
        assert kwargs["env"]["PCC_RUNTIME_DIR"] == str(runtime)
        assert command[3:] == [str(runtime), str(runtime / "libpy_runtime_pcc_py.a"), target]
        with monkeypatch.context() as child:
            child.setattr(sys, "argv", command[2:])
            child.setenv("PCC_RUNTIME_DIR", kwargs["env"]["PCC_RUNTIME_DIR"])
            child.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
            exec(command[2], {"__name__": "__main__"})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        run=run, CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    return module, hook, runtime, compiled, calls


@pytest.mark.parametrize("target", TARGETS)
def test_self_wheel_reuses_owned_source_and_rebuilds_changed_configuration(
    tmp_path, monkeypatch, target,
):
    _module, hook, runtime, compiled, calls = _tiny_runtime(tmp_path, monkeypatch, target)
    # Neither an unrelated runtime tree nor a stale explicit compiler archive
    # may replace the source selected by this wheel build.
    monkeypatch.setenv("PCC_RUNTIME_DIR", str(tmp_path / "foreign-source"))
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(tmp_path / "foreign-runtime.a"))
    archive = runtime / "libpy_runtime_pcc_py.a"
    assert hook._run_make(runtime, archive.name, "self")
    receipt = verify_runtime_archive_manifest(archive, runtime_root=runtime)
    assert receipt["target_triple"] == target
    assert {item["member"] for item in receipt["members"]} == {
        name + ".o" for name in owned.runtime_modules(str(runtime), target, False)
    }
    assert all(item["runtime_build_config"] == {"threads": False, "refcount": "atomic"}
               for item in receipt["members"])
    assert all(source.parent == runtime / "py" and selected == target
               for _name, source, selected in compiled)
    assert hook._require_runtime_archive_manifest(archive).is_file()
    first_count = len(compiled)
    assert hook._run_make(runtime, archive.name, "self")
    assert len(compiled) == first_count

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "local")
    assert hook._run_make(runtime, archive.name, "self")
    receipt = verify_runtime_archive_manifest(archive, runtime_root=runtime)
    assert len(compiled) > first_count
    assert all(item["runtime_build_config"] == {"threads": True, "refcount": "local"}
               for item in receipt["members"])
    assert {item["member"] for item in receipt["members"]} == {
        name + ".o" for name in owned.runtime_modules(str(runtime), target, True)
    }
    before_source_change = archive.read_bytes()
    (runtime / "py" / "wheel_probe.py").write_text(
        "def probe():\n    return 41\n", encoding="utf-8",
    )
    assert hook._run_make(runtime, archive.name, "self")
    verify_runtime_archive_manifest(archive, runtime_root=runtime)
    assert archive.read_bytes() != before_source_change
    assert len(calls) == 4


def test_wheel_make_requires_an_explicit_labeled_host_oracle(tmp_path, monkeypatch):
    module = _load_hatch_build(monkeypatch)
    hook = _new_build_hook(module, tmp_path)
    monkeypatch.setenv("PCC_RUNTIME_BUILD", "make")
    calls = []
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        run=lambda command, **_kwargs: calls.append(command),
        CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    assert hook._run_make(tmp_path, "reference.a", "self", force=True)
    assert calls == [["make", "-B", "-C", str(tmp_path), "reference.a"]]
    assert any("explicit host runtime reference oracle" in line for line in hook.app.info)
    monkeypatch.setattr(module, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
    with pytest.raises(RuntimeError, match="CPython-host reference oracle"):
        hook._run_make(tmp_path, "reference.a", "self")
    assert len(calls) == 1


@pytest.mark.parametrize("mode,backend,diagnostic", [
    ("bad", "self", "invalid PCC_RUNTIME_BUILD"),
    ("owned", "llvm", "PCC_BUILD_BACKEND=self"),
])
def test_invalid_wheel_owner_fails_before_tools(tmp_path, monkeypatch, mode, backend, diagnostic):
    module = _load_hatch_build(monkeypatch)
    hook = _new_build_hook(module, tmp_path)
    monkeypatch.setenv("PCC_RUNTIME_BUILD", mode)
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        run=lambda *_args, **_kwargs: pytest.fail("unexpected external command"),
    ))
    with pytest.raises(RuntimeError, match=diagnostic):
        hook._run_make(tmp_path, "runtime.a", backend)


def test_owned_wheel_failure_never_retries_make(tmp_path, monkeypatch):
    module = _load_hatch_build(monkeypatch)
    hook = _new_build_hook(module, tmp_path)
    monkeypatch.delenv("PCC_RUNTIME_BUILD", raising=False)
    calls = []

    def fail(command, **_kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr="owned build failed")

    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        run=fail, CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    assert hook._run_make(tmp_path, "runtime.a", "self") is False
    assert len(calls) == 1 and calls[0][:2] == [sys.executable, "-c"]
    assert hook.app.warnings
