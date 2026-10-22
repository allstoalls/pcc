from __future__ import annotations

import json
import os
from unittest import mock

from pcc.driver import cli_bootstrap
from pcc.driver import cli_core

_EMPTY_MODE_ENV = {
    "PCC_BACKEND": "",
    "PCC_PYTHON_LIBPYTHON": "",
    "PCC_IR_SCAFFOLD": "",
}


def _source(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("print(1)\n", encoding="utf-8")
    return path


def test_bare_pcc1_python_defaults_to_strict_self_owner(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "app"

    with (
        mock.patch.dict(os.environ, _EMPTY_MODE_ENV),
        mock.patch.object(cli_bootstrap, "_compile_python") as compile_python,
    ):
        status = cli_bootstrap.bootstrap_cli_main([str(source), "-o", str(output)])

    assert status == 0
    assert compile_python.call_args.kwargs["backend"] == "self"
    assert compile_python.call_args.kwargs["libpython_mode"] == "off"
    assert compile_python.call_args.kwargs["ir_scaffold_mode"] == "on"


def test_pcc1_rejects_explicit_removed_llvm_backend(tmp_path, capsys):
    source = _source(tmp_path)
    output = tmp_path / "app"

    with (
        mock.patch.dict(os.environ, _EMPTY_MODE_ENV),
        mock.patch.object(cli_bootstrap, "_compile_python") as compile_python,
    ):
        status = cli_bootstrap.bootstrap_cli_main(
            [
                "--backend=llvm",
                "--python-libpython=auto",
                "--ir-scaffold=off",
                str(source),
                "-o",
                str(output),
            ]
        )

    assert status == 2
    compile_python.assert_not_called()
    assert "invalid --backend 'llvm'; expected self" in capsys.readouterr().err
    assert not output.exists()


def test_pcc1_rejects_removed_llvm_backend_from_environment(tmp_path, capsys):
    source = _source(tmp_path)
    output = tmp_path / "app"
    mode_env = {
        "PCC_BACKEND": "llvm",
        "PCC_PYTHON_LIBPYTHON": "on",
        "PCC_IR_SCAFFOLD": "off",
    }

    with (
        mock.patch.dict(os.environ, mode_env),
        mock.patch.object(cli_bootstrap, "_compile_python") as compile_python,
    ):
        status = cli_bootstrap.bootstrap_cli_main([str(source), "-o", str(output)])

    assert status == 2
    compile_python.assert_not_called()
    assert "invalid --backend 'llvm'; expected self" in capsys.readouterr().err
    assert not output.exists()


def test_pcc1_profile_records_resolved_owner_modes(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "app"
    profile = tmp_path / "profile.json"

    with (
        mock.patch.dict(os.environ, _EMPTY_MODE_ENV),
        mock.patch.object(cli_bootstrap, "_compile_python"),
    ):
        status = cli_bootstrap.bootstrap_cli_main(
            [str(source), "-o", str(output), "--profile-json", str(profile)]
        )

    assert status == 0
    metadata = json.loads(profile.read_text(encoding="utf-8"))["metadata"]
    assert metadata["backend"] == "self"
    assert metadata["python_libpython"] == "off"
    assert metadata["ir_scaffold"] == "on"


def test_pcc1_failure_diagnostic_keeps_resolved_mode_labels(tmp_path, capsys):
    source = _source(tmp_path)
    output = tmp_path / "app"

    with (
        mock.patch.dict(os.environ, _EMPTY_MODE_ENV),
        mock.patch.object(
            cli_bootstrap,
            "_compile_python",
            side_effect=RuntimeError("unsupported self-backend operation"),
        ),
    ):
        status = cli_bootstrap.bootstrap_cli_main(
            [
                str(source),
                "-o",
                str(output),
                "--diagnostic-format=json",
            ]
        )

    assert status == 1
    diagnostic = json.loads(capsys.readouterr().err)
    metadata = diagnostic["diagnostics"][0]["metadata"]
    assert metadata == {
        "backend": "self",
        "python_libpython": "off",
        "ir_scaffold": "on",
    }


def test_host_cli_omitted_backend_remains_unresolved():
    with mock.patch.object(cli_core, "execute_cli", return_value=0) as execute_cli:
        status = cli_core.cli_main(["input.c"])

    assert status == 0
    assert execute_cli.call_args.kwargs["backend"] is None
