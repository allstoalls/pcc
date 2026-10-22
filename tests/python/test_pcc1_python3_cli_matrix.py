"""Focused python3-style CLI contract for the bootstrap pcc1 entry."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess


def _fake_execution(monkeypatch, tmp_path: Path):
    import pcc.driver.cli_bootstrap as cli

    compile_calls: list[tuple[str, str]] = []
    run_calls: list[list[str]] = []
    scratch = tmp_path / "inline"

    def observed(path, output, **_kwargs):
        compile_calls.append((str(path), str(output)))
        return None

    def run(command, *, check):
        assert check is True
        run_calls.append(list(command))

    def make_temp(_prefix):
        scratch.mkdir(parents=True, exist_ok=True)
        return str(scratch)

    monkeypatch.setattr(cli, "_observed_compile_python", observed)
    monkeypatch.setattr(cli, "_bootstrap_subprocess_run", run)
    monkeypatch.setattr(cli, "_make_bootstrap_run_tempdir", make_temp)
    monkeypatch.setattr(cli, "_remove_bootstrap_run_tempdir", lambda _path: None)
    monkeypatch.setenv("PCC_DISABLE_PY_RUN_CACHE", "1")
    return cli, compile_calls, run_calls, scratch


def test_parser_keeps_script_arguments_and_post_path_separator():
    from pcc.driver.cli_bootstrap import parse_bootstrap_cli_args

    parsed, status, error = parse_bootstrap_cli_args(
        ["--backend=self", "program.py", "--", "-x", "value"]
    )

    assert (status, error) == (0, None)
    assert parsed is not None
    assert parsed[0] == "program.py"
    assert parsed[8] == ["-x", "value"]
    assert parsed[-3:] == ("text", None, False)


def test_script_runner_publishes_source_argv0_and_program_arguments(
    monkeypatch, tmp_path, capsys
):
    cli, compile_calls, run_calls, _scratch = _fake_execution(
        monkeypatch, tmp_path
    )
    source = tmp_path / "program.py"
    source.write_text("print('ok')\n", encoding="utf-8")

    assert cli.bootstrap_cli_main([str(source), "left", "right"]) == 0
    assert len(compile_calls) == 1
    command = run_calls[-1]
    assert command[1:] == [
        cli._PYTHON_ARGV0_MARKER,
        "script",
        str(source),
        "left",
        "right",
    ]
    assert capsys.readouterr().err == ""


def test_command_mode_materializes_source_and_uses_cpython_argv0(
    monkeypatch, tmp_path
):
    cli, compile_calls, run_calls, scratch = _fake_execution(monkeypatch, tmp_path)

    assert cli.bootstrap_cli_main(
        ["-c", "import sys; print(sys.argv)", "arg"]
    ) == 0

    assert len(compile_calls) == 1
    materialized = Path(compile_calls[0][0])
    assert materialized == scratch / "__pcc_command__.py"
    assert materialized.read_text(encoding="utf-8").endswith("\n")
    assert run_calls[-1][1:] == [
        cli._PYTHON_ARGV0_MARKER,
        "command",
        "-c",
        "arg",
    ]


def test_stdin_mode_materializes_all_input_and_uses_dash_argv0(
    monkeypatch, tmp_path
):
    cli, compile_calls, run_calls, scratch = _fake_execution(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("x = 1\nprint(x)\n"))

    assert cli.bootstrap_cli_main(["-", "tail"]) == 0

    assert Path(compile_calls[0][0]) == scratch / "__pcc_stdin__.py"
    assert (scratch / "__pcc_stdin__.py").read_text(encoding="utf-8") == (
        "x = 1\nprint(x)\n"
    )
    assert run_calls[-1][1:] == [
        cli._PYTHON_ARGV0_MARKER,
        "stdin",
        "-",
        "tail",
    ]


def test_no_argument_interactive_request_fails_closed_without_compilation(
    monkeypatch, capsys
):
    import pcc.driver.cli_bootstrap as cli

    monkeypatch.setattr(
        cli,
        "_observed_compile_python",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("interactive request must not compile or delegate")
        ),
    )

    assert cli.bootstrap_cli_main([]) == 2
    error = capsys.readouterr().err
    assert "PCC-CPY-UNSUPPORTED-L3-TOOLING-INTERACTIVE-REPL" in error
    assert "did not invoke CPython" not in error  # no claim of an invocation


def test_script_exit_status_is_preserved(monkeypatch, tmp_path):
    cli, _compile_calls, _run_calls, _scratch = _fake_execution(
        monkeypatch, tmp_path
    )
    source = tmp_path / "exit_seven.py"
    source.write_text("raise SystemExit(7)\n", encoding="utf-8")

    def fail_run(command, *, check):
        assert check is True
        if cli._PYTHON_ARGV0_MARKER in command:
            raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(cli, "_bootstrap_subprocess_run", fail_run)
    assert cli.bootstrap_cli_main([str(source)]) == 7


def test_runtime_strips_private_logical_argv_envelope(tmp_path, pcc_runtime_archive):
    repo = Path(__file__).resolve().parents[2]
    harness = tmp_path / "argv_contract.c"
    executable = tmp_path / "argv_contract"
    harness.write_text(
        '#include "py_runtime.h"\n'
        "#include <stdio.h>\n"
        "int main(void) {\n"
        "  const char *items[] = {\"artifact\", "
        "\"--pcc-internal-python-argv0-v1\", \"command\", \"-c\", "
        "\"left\", \"right\"};\n"
        "  py_set_program_args(6, items);\n"
        '  printf("%lld:%s:%s:%s:%s:%d\\n", (long long)py_program_argc(),\n'
        "         py_program_argv(0), py_program_argv(1), py_program_argv(2),\n"
        "         py_program_executable(), (int)py_program_mode());\n"
        "  const char *unknown[] = {\"artifact\", "
        "\"--pcc-internal-python-argv0-v1\", \"future-mode\", \"logical\"};\n"
        "  py_set_program_args(4, unknown);\n"
        '  printf("%lld:%s:%s:%d\\n", (long long)py_program_argc(),\n'
        "         py_program_argv(0), py_program_argv(1), "
        "(int)py_program_mode());\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-I",
            str(repo / "pcc" / "runtime" / "include"),
            str(harness),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        check=True,
        timeout=30,
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, check=True, timeout=10
    )
    assert result.stdout == (
        "3:-c:left:right:artifact:3\n"
        "4:artifact:--pcc-internal-python-argv0-v1:0\n"
    )


def test_runtime_port_validates_mode_and_invokes_program_args_hook():
    root = Path(__file__).resolve().parents[2]
    source = (root / "pcc/runtime/py/py_process.py").read_text(
        encoding="utf-8"
    )
    assert "if mode_value != 0:" in source
    assert "call_void_ptr0(hook)" in source


def test_module_tooling_requests_have_stable_no_cpython_diagnostics(
    monkeypatch, capsys
):
    import pcc.driver.cli_bootstrap as cli

    monkeypatch.setattr(
        cli,
        "_run_compiled_python_module_from_pcc1",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("unsupported tooling must not compile")
        ),
    )
    for module, code in (
        ("pdb", "PCC-CPY-UNSUPPORTED-L3-TOOLING-DEBUGGER"),
        ("cProfile", "PCC-CPY-UNSUPPORTED-L3-TOOLING-PROFILER"),
        ("coverage", "PCC-CPY-UNSUPPORTED-L3-TOOLING-COVERAGE"),
    ):
        assert cli._run_python_module_from_pcc1(["-m", module]) == 2
        assert code in capsys.readouterr().err


def test_module_runner_reuses_cached_script_pipeline_with_module_argv0(
    monkeypatch, tmp_path
):
    import pcc.driver.cli_bootstrap as cli

    source = tmp_path / "pkg" / "__main__.py"
    source.parent.mkdir()
    source.write_text("print('module')\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr(cli, "_find_module_entry_source", lambda _name: (str(source), None))
    monkeypatch.setattr(cli, "_bootstrap_cli_main_impl", lambda argv, **kwargs: (
        calls.append((list(argv), dict(kwargs))) or 7
    ))

    assert cli._run_compiled_python_module_from_pcc1("pkg", ["tail"]) == 7
    assert calls == [
        (
            [str(source), "tail"],
            {"_logical_argv0": str(source), "_execution_mode": "module"},
        )
    ]


def test_command_restart_keeps_original_mode_before_materialization(
    monkeypatch,
):
    import pcc.driver.cli_bootstrap as cli

    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli, "apply_locked_environment_resource_defaults", lambda: {"jobs": "2"}
    )
    monkeypatch.setattr(
        cli,
        "_restart_with_locked_environment_defaults",
        lambda argv, _defaults: (seen.append(list(argv)) or 23),
    )
    monkeypatch.setattr(
        cli,
        "_run_inline_python_request",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("restart must happen before source materialization")
        ),
    )

    assert cli.bootstrap_cli_main(["-c", "print(1)", "tail"]) == 23
    assert seen == [["-c", "print(1)", "tail"]]
