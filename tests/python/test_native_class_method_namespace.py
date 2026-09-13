"""Class namespaces expose their own unbound methods for MRO collection."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_class_namespace_method_collection_matches_cpython(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'method_namespace.py'
    source.write_text('''
import gc
class Base:
    def p_first(self, value):
        return value + 1
    def p_second(self, value):
        return value * 2
class Derived(Base):
    def p_first(self, value):
        return value + 2

def collect(instance):
    table = {}
    cls = type(instance)
    for base in cls.__mro__:
        for name, function in base.__dict__.items():
            if name.startswith("p_") and callable(function):
                table.setdefault(name, function)
    return table

def main():
    obj = Derived()
    table = collect(obj)
    print(sorted(table))
    print("p_first" in Derived.__dict__, "p_second" in Derived.__dict__)
    gc.collect()
    print(table["p_first"](obj, 20), table["p_second"](obj, 21))
    print(getattr(obj, "p_first")(20), getattr(obj, "p_second")(21))
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'method_namespace'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
