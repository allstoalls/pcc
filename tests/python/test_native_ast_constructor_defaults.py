"""Native module-alias AST constructors must run omitted field defaults."""
import os
from pathlib import Path
import subprocess


def test_ast_class_constructor_keeps_omitted_defaults(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    source = tmp_path / "ast_defaults.py"
    source.write_text('''from . import py_ast as pa

def main():
    value = pa.ClassType("Item", "", (), ())
    print(value.properties, value.valueclass)
main()
''')
    definitions = tmp_path / "py_ast.py"
    definitions.write_text('''from dataclasses import dataclass
@dataclass(frozen=True)
class ClassType:
    name: str
    module: str
    fields: tuple = ()
    bases: tuple = ()
    properties: tuple = ()
    valueclass: bool = False
''')
    binary = tmp_path / "ast_defaults"
    compile_python_multi([str(source), str(definitions)], str(binary),
        module_names=["pcc.frontends.python.entry", "pcc.frontends.python.py_ast"],
        entry_module="pcc.frontends.python.entry", backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "() False"
