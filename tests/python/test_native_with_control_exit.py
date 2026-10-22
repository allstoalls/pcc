"""Native context exits run once, in order, on return/break/continue."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("parenthesized", [False, True])
def test_multi_with_as_bindings(
    tmp_path, pcc_runtime_archive, python_program_compiler, parenthesized,
):
    header = 'with Context(7) as first, Context(8) as second:'
    if parenthesized:
        header = 'with (\n        Context(7) as first,\n        Context(8) as second,\n    ):'
    source = tmp_path / 'with_bindings.py'
    source.write_text('''
class Context:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        print("enter", self.value)
        return self.value
    def __exit__(self, et, ev, tb):
        print("exit", self.value)
        return False
def main():
    ''' + header + '''
        print(first, second)
main()
''', encoding='utf-8')
    binary = tmp_path / 'with_bindings'
    python_program_compiler(str(source), str(binary), backend='self', libpython_mode='off',
                            runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout.splitlines() == [
            'enter 7', 'enter 8', '7 8', 'exit 8', 'exit 7',
        ], f'GC{gc}: {result.stdout}'


def test_native_with_invalid_target_diagnostic_has_source_location():
    from types import SimpleNamespace
    from pcc.frontends.python.codegen.async_with_lowering import AsyncWithLoweringMixin
    from pcc.frontends.python.py_ast import DynType, Name, SourceSpan, TupleExpr, With

    span = SourceSpan('with_bindings.py', 17, 4, 17, 30)
    ty = DynType('dyn')
    ctx = Name(span, ty, 'manager')
    target = TupleExpr(span, ty, ())
    stmt = With(span, ((ctx, target),), ())
    host = SimpleNamespace(
        _generator_ctx_stack=[], _fresh=lambda name: name,
        _gc_retain=lambda value: value, _gc_release_if_owned=lambda *args: None,
        _store_unpack_target=lambda *args, **kwargs: None,
    )
    with pytest.raises(NotImplementedError, match=(
        r'as-clause must be a bare name at with_bindings.py:17:4 \(got TupleExpr\)'
    )):
        AsyncWithLoweringMixin._emit_native_context_body(host, stmt, None, None)


def test_with_control_exit_order_and_exit_exception(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / 'with_control.py'
    source.write_text('''
class Context:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
    def __enter__(self):
        print("enter", self.name)
        return self
    def __exit__(self, et, ev, tb):
        print("exit", self.name, et is None)
        if self.fail:
            raise ValueError("exit failed")
        return False
def returning():
    with Context("outer"), Context("inner"):
        try:
            return 42
        finally:
            print("finally")
def exit_raises():
    with Context("outer"), Context("inner", True):
        return 10
def main():
    print(returning())
    for i in range(3):
        with Context("loop"):
            if i == 0:
                continue
            break
    try:
        exit_raises()
    except ValueError:
        print("caught")
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'with_control'
    python_program_compiler(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
