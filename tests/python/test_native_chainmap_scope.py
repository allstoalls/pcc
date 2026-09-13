"""ChainMap writes belong to the active scope; reads and membership see parents."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_chainmap_scope_operations_match_cpython(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'chainmap_scope.py'
    source.write_text('''from collections import ChainMap
def main():
    parent = {"outer": 7, "shadow": 1}
    env = ChainMap(parent)
    child = env.new_child()
    child["a"] = 20
    child["b"] = 22
    child["shadow"] = 2
    print(child["a"] + child["b"], child["outer"], child["shadow"])
    print("a" in child, "a" in env, "outer" in child)
    print(parent, child.maps[0])
    print(sorted(child), len(child), child.get("missing", 99))
    del child["shadow"]
    print(child["shadow"], child.parents["outer"])
    try:
        del child["outer"]
    except KeyError:
        print("parent retained")
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'chainmap_scope'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
