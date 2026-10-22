"""Comprehension target boundness is independent of its enclosing local."""

import os
import re
import subprocess
import sys

import pytest


def test_comprehension_uses_distinct_lexical_bound_flag():
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift

    source = '''def main():
    values = ['a']
    result = [value for value in values if value == 'a']
    print(value)
    for value in values:
        pass
main()
'''
    module = type_infer.infer_module(parse_and_lift(source, 'bound_flags.py', 'bound_flags'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    flags = re.findall(r'(%\.bound\.value\.owned\.[0-9]+) = alloca i1', text)
    assert len(flags) == 2, flags
    outer, inner = flags
    assert f'load i1, ptr {inner}' in text
    assert f'load i1, ptr {outer}' in text
    assert f'store i1 1, ptr {inner}' in text
    assert f'store i1 1, ptr {outer}' in text
    assert text.index(f'store i1 1, ptr {inner}') < text.index(f'load i1, ptr {inner}')


BOOTSTRAP_SOURCE = '''class Signature:
    def __init__(self, name):
        self.name = name
def main():
    signatures = [Signature('b'), Signature('a')]
    overlaps = sorted(sig.name for sig in signatures if sig.name == 'a')
    assert overlaps == ['a']
    try:
        print(sig)
        raise AssertionError('comprehension bound the enclosing local')
    except UnboundLocalError:
        pass
    for sig in signatures:
        pass
    assert sig.name == 'a'
    print('COMPREHENSION_BOOTSTRAP_BINDING_OK')
main()
'''


SCOPE_SOURCE = '''def fail(value):
    raise ValueError('stop')
def parameter(value):
    assert [value for value in [1, 2]] == [1, 2]
    assert value == 99
def dynamic(values):
    result = [value for value in values if value > 0]
    value = 99
    assert result == [1, 2]
    assert value == 99
def main():
    result = [number for number in range(3) if number > 0]
    assert result == [1, 2]
    number = 99
    assert number == 99
    result = [letter for letter in 'ab' if letter == 'b']
    assert result == ['b']
    letter = 'outer'
    result = {key: key + 1 for key in range(2)}
    assert result == {0: 1, 1: 2}
    key = 99
    result = {member for member in [1, 2]}
    assert sorted(result) == [1, 2]
    member = 99
    result = [left + right for left, right in [(1, 2), (3, 4)]]
    assert result == [3, 7]
    try:
        print(left)
        raise AssertionError('tuple target bound the enclosing local')
    except UnboundLocalError:
        pass
    left = 99
    right = 100
    result = [index + value for index, value in enumerate([3, 4])]
    assert result == [3, 5]
    index = 99
    value = 100
    result = [empty for empty in range(0)]
    assert result == []
    try:
        print(empty)
        raise AssertionError('empty comprehension bound the enclosing local')
    except UnboundLocalError:
        pass
    empty = 99
    outer = [7, 8]
    assert [outer for outer in outer] == [7, 8]
    assert outer == [7, 8]
    nested = 99
    assert [[nested for nested in range(2)] for nested in range(2)] == [[0, 1], [0, 1]]
    assert nested == 99
    try:
        result = [fail(raising) for raising in [1]]
        raise AssertionError('missing error')
    except ValueError:
        pass
    try:
        print(raising)
        raise AssertionError('raising comprehension bound the enclosing local')
    except UnboundLocalError:
        pass
    raising = 99
    dynamic([1, 2])
    parameter(99)
    print('COMPREHENSION_SCOPE_BINDINGS_OK')
main()
'''


@pytest.mark.parametrize('source, expected', [
    pytest.param(BOOTSTRAP_SOURCE, 'COMPREHENSION_BOOTSTRAP_BINDING_OK\n', id='bootstrap'),
    pytest.param(SCOPE_SOURCE, 'COMPREHENSION_SCOPE_BINDINGS_OK\n', id='scope'),
])
def test_comprehension_binding_flags_native(
    source, expected, tmp_path, pcc_runtime_archive, python_program_compiler,
):
    path = tmp_path / 'comprehension_bindings.py'
    output = tmp_path / 'comprehension_bindings'
    path.write_text(source)
    reference = subprocess.run([sys.executable, str(path)], capture_output=True,
                               text=True, timeout=20)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == expected
    python_program_compiler(str(path), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True,
                                timeout=20, env=dict(os.environ, PATH='',
                                                    PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected, (backend, result.stdout, result.stderr)
        assert result.stderr == ''
