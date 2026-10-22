"""The parser and semantic AST have separate class identities and layouts."""

import contextlib
import io
import os
import subprocess

import pytest


PROGRAM = '''from pcc.frontends.python.py_lift import parse_and_lift
def main():
    module = parse_and_lift("callee(17)\\n", "probe.py", "probe")
    call = module.body[0].expr
    assert call.func.ident == "callee"
    assert len(call.args) == 1
    assert call.args[0].value == 17
    source = "class Item:\\n    token = 23\\n    def method(self, value=token):\\n        return value\\n"
    module = parse_and_lift(source, "probe.py", "probe")
    record = module.body[0]
    assert record.name == "Item"
    assert record.bases == ()
    assert record.keywords == ()
    assert record.decorators == ()
    assert len(record.body) == 2
    assert record.body[0].value.value == 23
    method = record.body[1]
    assert method.name == "method"
    assert method.args[1].default.ident == "token"
    assert method.body[0].value.ident == "value"
    print("PARSER_LIFT_IDENTITIES_OK")
main()
'''


def test_parser_lift_class_identity_reference():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAM, {})
    assert output.getvalue() == "PARSER_LIFT_IDENTITIES_OK\n"


@pytest.mark.integration
def test_native_parser_lift_preserves_both_class_families(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "parser_lift.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "parser_lift"
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", ir_scaffold_mode="on",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "PARSER_LIFT_IDENTITIES_OK\n"
