"""Class-owned visitor tables preserve sliced method keys and calls."""
import os
import subprocess
import sys
from pcc.py_frontend.pipeline import compile_python


def test_class_cached_visitor_table_matches_cpython(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / 'visitor_table.py'
    source.write_text('''
class Left:
    pass
class Right:
    pass
class Visitor:
    _dispatch = None
    def dispatch(self, node):
        if node is None:
            return None, None
        owner = type(self)
        table = owner.__dict__.get("_dispatch")
        if table is None:
            table = {}
            for base in owner.__mro__:
                for name, fn in base.__dict__.items():
                    if not name.startswith("visit_"):
                        continue
                    if not callable(fn):
                        continue
                    table.setdefault(name[len("visit_"):], fn)
            owner._dispatch = table
        fn = table.get(type(node).__name__)
        if fn is None:
            return ("missing", False)
        return fn(self, node)
    def visit_Left(self, node):
        return ("left", True)
    def visit_Right(self, node):
        return ("right", True)
    def generate(self, node):
        result = self.dispatch(node)
        return result

def main():
    visitor = Visitor()
    print(visitor.generate(Left()))
    print(visitor.generate(Right()))
    print(visitor.generate(None))
    print(sorted(Visitor._dispatch))
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'visitor_table'
    compile_python(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
