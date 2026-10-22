"""Cheap selection/caching contracts for the expensive fallback baseline gates."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def phase_harness(monkeypatch):
    source = Path(__file__).with_name("test_fallback_baseline.py")
    spec = importlib.util.spec_from_file_location("fallback_baseline_phase_test", source)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)

    from pcc.frontends.python import py_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    events = []
    controls = {"multi_error": None, "multi_ok": True}
    sources = ["/fixture/owner.py"]
    modules = ["owner"]
    standalone_ir = "call ptr @py_cpy_import(ptr null)\ncall void @py_cpy_decref(ptr null)\n"
    multi_ir = "call ptr @py_cpy_import(ptr null)\ncall ptr @py_cpy_to_pcc_obj(ptr null)\n"

    class FakeCodegen:
        def __init__(self, typed, *, emit_cpy_main_exitcode, ir_scaffold_mode):
            assert emit_cpy_main_exitcode is False
            self.mode = ir_scaffold_mode

        def generate(self, typed):
            events.append(("standalone", self.mode))
            return standalone_ir

    def multi(srcs, mods):
        assert list(srcs) == sources and list(mods) == modules
        events.append(("multi", os.environ.get("PCC_IR_SCAFFOLD")))
        assert os.environ["PCC_PYTHON_IR_PASSES"] == "off"
        if controls["multi_error"]:
            raise controls["multi_error"]
        return controls["multi_ok"], multi_ir, ""

    def contextual(srcs, mods, *, ir_scaffold_mode):
        assert list(srcs) == sources and list(mods) == modules
        events.append(("contextual", ir_scaffold_mode))
        return {"owner": 0}

    def closure(entry):
        assert entry == str(baseline._REPO_ROOT / "pcc" / "__main__.py")
        events.append(("closure", None))
        return sources, modules

    real_spec = importlib.util.spec_from_file_location

    def probe_spec(name, location, *args, **kwargs):
        result = real_spec(name, location, *args, **kwargs)
        if Path(location).name == "probe_stage1_closure.py":
            def populate(module):
                module._tightened_closure = closure
                module._try_full_multi_compile = multi
            result.loader.exec_module = populate
        return result

    real_open = open

    def source_open(path, *args, **kwargs):
        if path == sources[0]:
            import io
            return io.StringIO("value = 1\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", probe_spec)
    monkeypatch.setattr(baseline, "open", source_open, raising=False)
    monkeypatch.setattr(baseline, "_contextual_per_module_counts", contextual)
    monkeypatch.setattr(py_lift, "parse_and_lift", lambda source, path, name: SimpleNamespace(name=name))
    monkeypatch.setattr(type_infer, "infer_module", lambda module: module)
    monkeypatch.setattr(layer1, "L1CodeGen", FakeCodegen)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    return SimpleNamespace(module=baseline, events=events, controls=controls)


@pytest.mark.parametrize("mode", ("off", "on"))
@pytest.mark.parametrize("phase", ("standalone", "multi", "contextual"))
def test_selected_phase_is_lazy_isolated_and_cached(phase_harness, monkeypatch, capsys, mode, phase):
    harness = phase_harness
    monkeypatch.setenv("PCC_TEST_LIVE_PROGRESS", "1")
    fixture = harness.module.closure_compile_on if mode == "on" else harness.module.closure_compile
    result = fixture.__wrapped__()
    assert harness.events == [("closure", None)]
    assert result["files"] == 1
    assert tuple(result["module_names"]) == ("owner",)
    assert harness.events == [("closure", None)]

    if phase == "standalone":
        assert result["per_module_ok"] == 1
        assert result["per_module"] == {"owner": 2}
        assert result["per_module_actions"] == {"owner": 1}
        assert result["per_module_plumbing"] == {"owner": 1}
        assert result["per_module_ok"] == 1
    elif phase == "multi":
        assert result["multi_ok"] is True
        assert result["total_fallbacks"] == 2
        assert result["bridge_calls"] == 1
        assert result["non_bridge_fallbacks"] == 1
        assert result["ir_lines"] == 2
        assert result["multi_ok"] is True
    else:
        assert result["contextual_per_module"] == {"owner": 0}
        assert result["contextual_per_module"] == {"owner": 0}
    expected_mode = None if phase == "multi" and mode == "off" else mode
    assert harness.events == [("closure", None), (phase, expected_mode)]
    progress = capsys.readouterr().out
    assert f"[fallback:{mode}]" in progress
    assert "complete" in progress


@pytest.mark.parametrize("mode", ("off", "on"))
def test_contextual_baseline_node_does_not_request_standalone_names(phase_harness, monkeypatch, mode):
    harness = phase_harness
    monkeypatch.setattr(harness.module, "_load_baseline", lambda: {})
    monkeypatch.setattr(harness.module, "_contextual_policy_modules", lambda names: set(names))
    fixture = harness.module.closure_compile_on if mode == "on" else harness.module.closure_compile
    result = fixture.__wrapped__()
    test = (
        harness.module.test_on_mode_contextual_per_module_fallbacks_under_ratchet
        if mode == "on"
        else harness.module.test_contextual_per_module_fallbacks_under_ratchet
    )
    test(result)
    assert harness.events == [("closure", None), ("contextual", mode)]


@pytest.mark.parametrize("mode", ("off", "on"))
@pytest.mark.parametrize("previous", (None, "previous-value"))
def test_multi_phase_restores_environment_when_it_raises(phase_harness, monkeypatch, mode, previous):
    harness = phase_harness
    for name in ("PCC_IR_SCAFFOLD", "PCC_PYTHON_IR_PASSES"):
        if previous is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, previous)
    fixture = harness.module.closure_compile_on if mode == "on" else harness.module.closure_compile
    result = fixture.__wrapped__()
    harness.controls["multi_error"] = RuntimeError("injected multi failure")
    with pytest.raises(RuntimeError, match="injected multi failure"):
        result["multi_ok"]
    for name in ("PCC_IR_SCAFFOLD", "PCC_PYTHON_IR_PASSES"):
        assert os.environ.get(name) == previous
    assert [phase for phase, _mode in harness.events] == ["closure", "multi"]


def test_failed_multi_result_is_cached_without_manufacturing_counts(phase_harness):
    harness = phase_harness
    result = harness.module.closure_compile.__wrapped__()
    harness.controls["multi_ok"] = False
    assert result["multi_ok"] is False
    assert result["total_fallbacks"] is None
    assert result["bridge_calls"] is None
    assert result["non_bridge_fallbacks"] is None
    assert result["ir_lines"] == 0
    assert result["multi_ok"] is False
    assert [phase for phase, _mode in harness.events] == ["closure", "multi"]
