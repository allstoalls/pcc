"""The compiled C parser collects and invokes real grammar action methods.

This uses the Python frontend's isolated pcc-Python runtime fixture.
"""
import os
from pathlib import Path
import subprocess

from pcc.py_frontend.pipeline import compile_python_multi


def test_compiled_driver_builds_and_calls_native_action_table(tmp_path, pcc_py_runtime_archive):
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / 'parser_probe.py'
    source.write_text('''from pcc.parse.c_parse_driver import CParseDriver
def main():
    parser = CParseDriver()
    tree = parser.parse("int add(int a, int b) { return a+b; } int main(void) { return add(20,22); }")
    print(len(tree.ext), tree.ext[0].decl.name, tree.ext[1].decl.name)
    print(tree.ext[1].body.block_items[0].expr.name.name)
main()
''', encoding='utf-8')
    binary = tmp_path / 'parser_probe'
    compile_python_multi(
        [str(repo / 'pcc/parse/c_parse_driver.py'), str(source)], str(binary),
        module_names=['pcc.parse.c_parse_driver', 'parser_probe'],
        entry_module='parser_probe', recursive_stdlib=True, backend='self',
        libpython_mode='off', runtime_archive=str(pcc_py_runtime_archive),
    )
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == '2 add main\nadd\n', f'GC{gc}: {result.stdout}'
