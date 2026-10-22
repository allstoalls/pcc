"""C ABI visibility does not remove Python exception propagation."""
import os
import re
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python


SOURCE = '''from pcc.extern import c_abi_export

events = []
def fail():
    raise ValueError("inner")

@c_abi_export("application_exported_fail")
def exported_fail(value: int) -> int:
    fail()
    events.append("unreachable exported")
    return value

def ordinary_fail():
    fail()
    events.append("unreachable ordinary")

def main():
    try:
        ordinary_fail()
    except ValueError as error:
        assert str(error) == "inner"
        events.append("ordinary")
    else:
        raise AssertionError("ordinary error was lost")
    try:
        exported_fail(7)
    except ValueError as error:
        assert str(error) == "inner"
        events.append("exported")
    else:
        raise AssertionError("exported error was lost")
    assert events == ["ordinary", "exported"]
    print("APPLICATION_EXPORT_EXCEPTIONS_OK")
main()
'''


def test_application_export_keeps_python_error_edges(tmp_path):
    source = tmp_path / "export_application.py"
    output = source.with_suffix(".ll")
    source.write_text(SOURCE)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True)
    text = output.read_text()
    for symbol in ("user_export_application_ordinary_fail", "application_exported_fail",
                   "user_export_application_main"):
        body = re.search(r"^define[^\n]*@" + symbol + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}",
                         text, re.M | re.S)
        assert body, symbol
        assert "@py_err_occurred(" in body[1], symbol


def test_runtime_port_preserves_explicit_error_transport(tmp_path):
    source = tmp_path / "runtime_transport.py"
    output = source.with_suffix(".ll")
    source.write_text('''__pcc_runtime_port__ = True
from pcc.extern import c_abi_export, c_int64, extern
current_error = extern("py_err_occurred", (), c_int64)
def helper() -> int:
    return 42
@c_abi_export("runtime_transport_probe")
def probe() -> int:
    helper()
    return current_error()
''')
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    body = re.search(r"^define[^\n]*@runtime_transport_probe\([^\n]*\)[^\n]*\{\n(.*?)^\}",
                     text, re.M | re.S)[1]
    assert body.count("@py_err_occurred(") == 1
    assert "err.flag" not in body


@pytest.mark.integration
def test_application_export_exceptions_execute_all_collectors(tmp_path, pcc_runtime_archive):
    source = tmp_path / "export_application.py"
    output = source.with_suffix("")
    source.write_text(SOURCE)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    for collector in range(5):
        env = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                   PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(collector))
        result = subprocess.run([str(output)], env=env, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (collector, result.stdout, result.stderr)
        assert result.stdout == "APPLICATION_EXPORT_EXCEPTIONS_OK\n"
        assert result.stderr == ""
