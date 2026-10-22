"""Selection and durable standalone diagnostics without running the compiler."""

import hashlib
import json

import pytest

from scripts import probe_stage1_closure_on_mode as tool


@pytest.fixture
def closure(tmp_path, monkeypatch):
    sources = []
    for name in ("first", "second", "third"):
        path = tmp_path / (name + ".py")
        path.write_text("value = " + repr(name) + "\n", encoding="utf-8")
        sources.append(path)
    modules = ["pcc.first", "pcc.second", "pcc.third"]
    monkeypatch.setattr(tool, "_tightened_closure",
                        lambda entry: ([str(path) for path in sources], modules))
    return sources, modules


def test_unknown_module_fails_before_any_compile_or_artifact(closure, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(tool, "_compile_standalone", lambda *args: called.append(args))
    output = tmp_path / "unknown"
    with pytest.raises(SystemExit) as failure:
        tool.main(["--module", "pcc.missing", "--emit-ir-dir", str(output)])
    assert failure.value.code == 2
    assert called == []
    assert not output.exists()


@pytest.mark.parametrize("mode,modes", [("off", ["off"]), ("on", ["on"]),
                                       ("both", ["off", "on"])])
def test_only_selected_modules_and_modes_emit_exact_ir(closure, tmp_path, monkeypatch, mode, modes):
    sources, modules = closure
    called = []
    ir = (
        '; exact standalone output\n'
        '%cpy.import.alpha.1 = call ptr @py_cpy_import(ptr null)\n'
        '%cpy.fn.beta.2 = call ptr @py_cpy_getattr(ptr null, ptr null)\n'
        'call void @py_cpy_decref(ptr null)\n'
    )

    def compile_standalone(source, src, module, selected_mode):
        called.append((module, selected_mode))
        assert source == sources[modules.index(module)].read_text()
        return ir + '; mode: ' + selected_mode  # No final newline added by the tool.

    monkeypatch.setattr(tool, "_compile_standalone", compile_standalone)
    output = tmp_path / "artifacts"
    assert tool.main(["--module", "pcc.third", "--module", "pcc.first",
                      "--module", "pcc.third", "--mode", mode,
                      "--emit-ir-dir", str(output)]) == 0
    assert called == [(module, selected_mode) for module in ("pcc.first", "pcc.third")
                      for selected_mode in modes]
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "OK"
    assert receipt["scope"] == "standalone_frontend_ir"
    assert receipt["selected_modules"] == ["pcc.first", "pcc.third"]
    assert receipt["modes"] == modes
    assert receipt["closure_module_count"] == 3
    assert receipt["tool_source_sha256"] == hashlib.sha256(
        tool.Path(tool.__file__).read_bytes()).hexdigest()
    for record in receipt["results"]:
        expected = (ir + '; mode: ' + record["mode"]).encode()
        assert tool.Path(record["ir_path"]).read_bytes() == expected
        assert record["ir_sha256"] == hashlib.sha256(expected).hexdigest()
        assert record["source_sha256"] == hashlib.sha256(
            tool.Path(record["source_path"]).read_bytes()).hexdigest()
        assert record["scan"]["total"] == 3
        assert record["scan"]["actions_total"] == 2
        assert record["scan"]["by_action"] == {"getattr": 1, "import": 1}
        assert record["scan"]["by_target"] == {"alpha": 1, "beta": 1}
        assert record["scan"]["action_target_pairs"] == [
            {"action": "getattr", "target": "beta", "count": 1},
            {"action": "import", "target": "alpha", "count": 1},
        ]


def test_failure_receipt_identifies_source_mode_and_cause(closure, tmp_path, monkeypatch):
    sources, _modules = closure

    def compile_standalone(source, src, module, mode):
        if mode == "off":
            raise ValueError("injected standalone failure")
        return "; successful ON\n"

    monkeypatch.setattr(tool, "_compile_standalone", compile_standalone)
    output = tmp_path / "failure"
    assert tool.main(["--module", "pcc.first", "--emit-ir-dir", str(output)]) == 1
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "ERROR"
    failed, passed = receipt["results"]
    assert (failed["module"], failed["mode"], failed["status"]) == ("pcc.first", "off", "ERROR")
    assert failed["source_path"] == str(sources[0])
    assert failed["source_sha256"] == hashlib.sha256(sources[0].read_bytes()).hexdigest()
    assert failed["error"] == "ValueError: injected standalone failure"
    assert "ir_path" not in failed
    error_text = tool.Path(failed["error_path"]).read_text()
    for expected in ("pcc.first", "mode: off", str(sources[0]), failed["source_sha256"], failed["error"]):
        assert expected in error_text
    assert passed["mode"] == "on" and passed["status"] == "OK"
    assert tool.Path(passed["ir_path"]).read_text() == "; successful ON\n"


def test_scan_failure_retains_exact_ir_and_reports_unclassified_symbol(closure, tmp_path, monkeypatch):
    ir = "%unknown.1 = call ptr @py_cpy_future_edge()\n"
    monkeypatch.setattr(tool, "_compile_standalone", lambda *args: ir)
    output = tmp_path / "unclassified"
    assert tool.main(["--module", "pcc.second", "--mode", "off",
                      "--emit-ir-dir", str(output)]) == 1
    record = json.loads((output / "receipt.json").read_text())["results"][0]
    assert record["status"] == "ERROR"
    assert "unclassified py_cpy symbol: py_cpy_future_edge" in record["error"]
    assert tool.Path(record["ir_path"]).read_text() == ir


def test_no_arguments_preserve_all_module_both_mode_diagnostic_run(closure, monkeypatch, capsys):
    _sources, modules = closure
    called = []

    def compile_standalone(source, src, module, mode):
        called.append((module, mode))
        if module == "pcc.second":
            raise ValueError("diagnostic failure")
        return "; no fallback\n"

    monkeypatch.setattr(tool, "_compile_standalone", compile_standalone)
    assert tool.main([]) == 0
    assert called == [(module, mode) for module in modules for mode in ("off", "on")]
    output = capsys.readouterr().out
    assert "closure: 3 files (production discovery; standalone codegen)" in output
    assert "OFF FAIL: ValueError: diagnostic failure" in output
