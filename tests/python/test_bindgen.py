from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.bindgen import BindgenError, generate_bindings, main

HEADER = """
typedef unsigned long length_t;
typedef struct connection connection;
int compute(int a, unsigned int b);
connection *connect(void);
int write_data(connection *c, const char *data, length_t length);
void close_connection(connection *c);
double average(const double samples[], length_t n);
int log_message(const char *format, ...);
"""


def test_generates_executable_declaration_module():
    result = generate_bindings(HEADER, target="arm64-apple-darwin")
    assert result == generate_bindings(HEADER, target="arm64-apple-darwin")
    ast.parse(result)
    namespace = {}
    exec(result, namespace)
    assert namespace["compute"].symbol == "compute"
    assert [t.name for t in namespace["compute"].argtypes] == ["int32", "uint32"]
    assert namespace["connect"].restype.name == "rawptr"
    assert namespace["connect"].argtypes == ()
    assert [t.name for t in namespace["write_data"].argtypes] == [
        "rawptr",
        "rawptr",
        "uint64",
    ]
    assert namespace["average"].restype.name == "double"
    assert namespace["log_message"].variadic is True


@pytest.mark.parametrize(
    "source, message",
    [
        ("#include <stddef.h>\nint f(void);", "preprocessing"),
        ("int f();", "explicit prototype"),
        ("int global;", "global variables"),
        ("long double f(void);", "unsupported scalar"),
        ("struct Pair { int a; int b; }; struct Pair f(void);", "unsupported C ABI"),
        ("int f(int (*callback)(int));", "callback"),
        ("typedef int (*callback_t)(int); int f(callback_t c);", "callback"),
        ("static int f(int a);", "static/inline"),
        ("int f(int a) { return a; }", "function prototype"),
        ("int from(int a);", "cannot be exported"),
        ("int f(int a); int f(int b);", "duplicate"),
        ("typedef int number;", "no supported"),
    ],
)
def test_rejects_unimplemented_or_ambiguous_surfaces(source, message):
    with pytest.raises(BindgenError, match=message):
        generate_bindings(source, target="arm64-apple-darwin", filename="api.h")


def test_rejects_non_lp64_target():
    with pytest.raises(BindgenError, match="unsupported target"):
        generate_bindings("int f(void);", target="x86_64-w64-mingw32")


def test_host_cli_and_public_api_do_real_generation(tmp_path, capsys):
    import pcc
    from pcc.cli_core import cli_main

    source = tmp_path / "api.h"
    source.write_text("int abs(int value);\n")
    result = pcc.generate_bindings(source.read_text(), target="arm64-apple-darwin")
    output = tmp_path / "bindings.py"
    assert (
        cli_main(
            [
                "bindgen",
                str(source),
                "-o",
                str(output),
                "--target",
                "arm64-apple-darwin",
            ]
        )
        == 0
    )
    assert output.read_text() == result
    assert main([str(source), "-o", str(output)]) == 2
    assert "PCC-BINDGEN-001" in capsys.readouterr().err
    assert output.read_text() == result
    assert source.read_text() == "int abs(int value);\n"


def test_public_api_without_third_party_parser_or_toolchain():
    code = """
import sys
import pcc
print(pcc.generate_bindings('int abs(int value);', target='arm64-apple-darwin'))
for forbidden in ('pycparser', 'ply', 'llvmlite', 'cffi', 'pcc.ply'):
    assert not any(n == forbidden or n.startswith(forbidden + '.') for n in sys.modules), forbidden
"""
    proc = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert "abs = extern(" in proc.stdout


def test_native_dispatch_keeps_c_header_arguments_off_host_path(monkeypatch):
    import pcc.cli_bootstrap as cli

    calls = []
    monkeypatch.setattr(
        cli,
        "_run_compiled_python_module_from_pcc1",
        lambda module, args: calls.append((module, args)) or 9,
    )
    monkeypatch.setattr(
        cli, "_run_host_pcc_from_pcc1", lambda args: pytest.fail("host delegation")
    )
    args = ["api.h", "-o", "bindings.py", "--target", "arm64-apple-darwin"]
    assert cli.bootstrap_cli_main(["bindgen"] + args) == 9
    assert calls == [("pcc.bindgen", args)]
