"""Per-module compiler settings at the owned runtime construction boundary."""

import os

import pytest

from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, _RUNTIME_IR_OUTPUT_ENV


@pytest.mark.parametrize("module", ["freestanding_thread_kernel_pthread", "py_runtime_log", "py_tempfile", "py_os_mkdir", "py_cleanup_runtime"])
@pytest.mark.parametrize("previous", [None, "0", "1"])
@pytest.mark.parametrize("fail", [False, True])
def test_raw_runtime_disables_implicit_polls_and_restores_environment(monkeypatch, module, previous, fail):
    from pcc.frontends.python import pipeline
    if previous is None:
        monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    else:
        monkeypatch.setenv("PCC_WITH_THREADS", previous)
    calls = []

    def compile_source(source, output, **options):
        calls.append((source, output, options, os.environ.get("PCC_WITH_THREADS")))
        if fail:
            raise RuntimeError("source diagnostic")

    monkeypatch.setattr(pipeline, "compile_python", compile_source)
    if fail:
        with pytest.raises(RuntimeError, match="source diagnostic"):
            _compile_runtime_module(module, "kernel.py", "kernel.ll",
                                    "x86_64-pc-windows-msvc")
    else:
        _compile_runtime_module(module, "kernel.py", "kernel.ll",
                                "x86_64-pc-windows-msvc")
    assert len(calls) == 1 and calls[0][3] == "0"
    assert calls[0][2]["target_triple"] == "x86_64-pc-windows-msvc"
    assert calls[0][2]["libpython_mode"] == "off"
    assert os.environ.get("PCC_WITH_THREADS") == previous


def test_managed_runtime_modules_retain_thread_polls(monkeypatch):
    from pcc.frontends.python import pipeline
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    calls = []
    monkeypatch.setattr(pipeline, "compile_python",
                        lambda *_args, **_kwargs: calls.append(os.environ.get("PCC_WITH_THREADS")))
    _compile_runtime_module("py_threading", "threading.py", "threading.ll", "aarch64-unknown-linux-gnu")
    assert calls == ["1"]


@pytest.mark.parametrize("module", ["py_threading", "freestanding_thread_kernel_pthread", "py_runtime_log", "py_tempfile", "py_os_mkdir", "py_cleanup_runtime"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_runtime_ir_masks_direct_and_deferred_modes_and_restores(monkeypatch, module, configured, fail):
    from pcc.frontends.python import pipeline

    for key in _RUNTIME_IR_OUTPUT_ENV:
        if configured:
            monkeypatch.setenv(key, "pending.json" if "PLAN" in key or "OUTPUT" in key else "1")
        else:
            monkeypatch.delenv(key, raising=False)
    before = {key: os.environ.get(key) for key in _RUNTIME_IR_OUTPUT_ENV}
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "atomic")
    seen = []

    def compile_source(source, output, **options):
        assert options["emit_llvm_only"] is True
        assert options["python_library"] is True
        assert options["backend"] == "self"
        assert options["target_triple"] == "aarch64-unknown-linux-gnu"
        assert all(key not in os.environ for key in _RUNTIME_IR_OUTPUT_ENV)
        assert os.environ["PCC_WITH_THREADS"] == ("1" if module == "py_threading" else "0")
        assert os.environ["PCC_GC_BACKEND"] == "4"
        assert os.environ["PCC_REFCOUNT_KIND"] == "atomic"
        seen.append((source, output))
        # A failed nested compile must not leak changes to the masked options.
        os.environ["PCC_DIRECT_INDEXED_KERNEL_EMIT"] = "changed during compile"
        if fail:
            raise RuntimeError("runtime compile failed")

    monkeypatch.setattr(pipeline, "compile_python", compile_source)
    if fail:
        with pytest.raises(RuntimeError, match="runtime compile failed"):
            _compile_runtime_module(module, "module.py", "module.ll", "aarch64-unknown-linux-gnu")
    else:
        _compile_runtime_module(module, "module.py", "module.ll", "aarch64-unknown-linux-gnu")
    assert seen == [("module.py", "module.ll")]
    assert {key: os.environ.get(key) for key in _RUNTIME_IR_OUTPUT_ENV} == before
    assert os.environ["PCC_WITH_THREADS"] == "1"


def test_bootstrap_overrides_inherited_deferred_plans(monkeypatch):
    from scripts.bootstrap_platform import _stage_environment, ROOT

    inherited = {
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN": "other-task-front.json",
        "PCC_DEFER_FRONTEND_OUTPUT": "other-task-output",
        "PCC_DEFER_SELF_LINK_PLAN": "other-task-link.json",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_RUNTIME_DIR", "other-runtime-tree")
    environment = _stage_environment(4)
    assert all(environment[key] == "" for key in inherited)
    assert environment["PCC_DIRECT_INDEXED_KERNEL_EMIT"] == "1"
    assert environment["PCC_WITH_THREADS"] == "1"
    assert environment["PCC_GC_BACKEND"] == "4"
    assert environment["PCC_RUNTIME_DIR"] == str(ROOT / "pcc" / "runtime")
    assert os.environ["PCC_RUNTIME_DIR"] == "other-runtime-tree"
    assert {key: os.environ[key] for key in inherited} == inherited
