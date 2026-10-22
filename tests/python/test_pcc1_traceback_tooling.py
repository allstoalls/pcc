"""Static/focused contract for pcc1 diagnostics and developer tooling."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_tooling_manifest_is_mode_labelled_and_fail_closed():
    from pcc.driver.cli_bootstrap import python_tooling_capabilities_json

    manifest = json.loads(python_tooling_capabilities_json())
    assert manifest["schema"] == "pcc.python-tooling.v1"
    assert manifest["claim_mode"] == "pcc1/pcc-native/self/no-libpython"
    assert {
        "traceback",
        "source-map",
        "inspect",
        "compiler-profile",
        "warning-filters",
    } <= set(manifest["supported"])
    assert manifest["unsupported"] == {
        "interactive-repl": "PCC-CPY-UNSUPPORTED-L3-TOOLING-INTERACTIVE-REPL",
        "line-debugger": "PCC-CPY-UNSUPPORTED-L3-TOOLING-DEBUGGER",
        "runtime-profiler": "PCC-CPY-UNSUPPORTED-L3-TOOLING-PROFILER",
        "coverage": "PCC-CPY-UNSUPPORTED-L3-TOOLING-COVERAGE",
    }


def test_tooling_manifest_cli_is_native_and_machine_readable(capsys):
    from pcc.driver.cli_bootstrap import bootstrap_cli_main

    assert bootstrap_cli_main(["--tooling-capabilities"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "pcc.python-tooling.v1"


def test_traceback_runtime_mirrors_keep_source_line_and_function_frames():
    py_source = (ROOT / "pcc/runtime/py/py_exc_traceback.py").read_text(
        encoding="utf-8"
    )
    for source in (py_source,):
        assert "source_line" in source
        assert "func_name" in source
        assert "line" in source
        assert "Traceback (most recent call last):" in source


def test_inspect_and_warning_boundaries_are_honest():
    inspect_source = (ROOT / "pcc/stdlib/inspect.py").read_text(encoding="utf-8")
    inspect_lowering = (
        ROOT / "pcc/frontends/python/codegen/native_modules.py"
    ).read_text(encoding="utf-8")
    warnings_source = (ROOT / "pcc/stdlib/warnings.py").read_text(
        encoding="utf-8"
    )
    for name in ("signature", "isfunction", "ismethod", "isclass"):
        assert "def " + name + "(" in inspect_source
    for name in ("getsource", "getmro", "getfullargspec"):
        assert 'attr_name == "' + name + '"' in inspect_lowering
    for name in (
        "filterwarnings",
        "simplefilter",
        "resetwarnings",
        "warn_explicit",
        "formatwarning",
        "showwarning",
    ):
        assert "def " + name + "(" in warnings_source
    assert '"ignore"' in warnings_source
    assert '"error"' in warnings_source
    assert "destination.write(formatwarning(" in warnings_source


def test_native_warnings_filters_record_restore_and_raise():
    from pcc.stdlib import warnings as native_warnings

    native_warnings.resetwarnings()
    with native_warnings.catch_warnings(record=True) as caught:
        native_warnings.simplefilter("always")
        native_warnings.warn_explicit("first", UserWarning, "probe.py", 7)
        native_warnings.filterwarnings("ignore", message="skip")
        native_warnings.warn_explicit("skip this", UserWarning, "probe.py", 8)
        assert len(caught) == 1
        assert str(caught[0].message) == "first"
        assert caught[0].filename == "probe.py"
        assert caught[0].lineno == 7

    with native_warnings.catch_warnings(record=True) as caught:
        native_warnings.simplefilter("once")
        native_warnings.warn_explicit("once", UserWarning, "one.py", 1)
        native_warnings.warn_explicit("once", UserWarning, "two.py", 2)
        assert len(caught) == 1

    with native_warnings.catch_warnings():
        native_warnings.simplefilter("error")
        try:
            native_warnings.warn_explicit("boom", UserWarning, "probe.py", 9)
        except UserWarning as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("error filter did not raise")


def test_compiler_profile_schema_names_mode_and_phase(tmp_path, monkeypatch):
    import pcc.driver.cli_bootstrap as cli

    source = tmp_path / "profile.py"
    output = tmp_path / "profile.out"
    profile = tmp_path / "profile.json"
    source.write_text("print('profile')\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_compile_python", lambda *_a, **_k: None)

    assert cli.bootstrap_cli_main(
        ["--profile-json", str(profile), "-o", str(output), str(source)]
    ) == 0
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert payload["schema"] == "pcc.profile.v1"
    assert payload["metadata"]["entry"] == "cli_bootstrap"
    assert payload["metadata"]["backend"] == "self"
    assert payload["metadata"]["python_libpython"] == "off"
