"""Runtime classinfo tuples must behave like the literal tuple fast path."""

from pathlib import Path
import os
import re
import subprocess
import sys

import pytest


def test_isinstance_dynamic_nested_tuple(tmp_path: Path, pcc_runtime_archive, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    archive = pcc_runtime_archive
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    source = tmp_path / "classinfo.py"
    executable = tmp_path / "classinfo"
    source.write_text('''from dataclasses import dataclass
@dataclass(frozen=True)
class Base:
    name: str
@dataclass(frozen=True)
class Child(Base):
    pass
TYPES = (str, (Base, int))
EMPTY = ()
def check(value, classes):
    print(isinstance(value, classes))
check(Child("child"), TYPES)
check("text", TYPES)
check(True, TYPES)
check(1.5, TYPES)
check(Child("empty"), EMPTY)
''', encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   ir_scaffold_mode="on", libpython_mode="off")
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.splitlines() == ["True", "True", "True", "False", "False"]


LITERAL_PROGRAM = r'''import gc
from builtins import staticmethod as Static

events = []
class Token:
    pass
class Other:
    pass
Global = Token

def pair(value, kind):
    assert isinstance(value, kind)
    assert isinstance(value, (str, kind))
    assert isinstance(value, (str, (bytes, kind)))
    assert isinstance(value, ((kind,), ()))
    assert not isinstance(Other(), (str, (bytes, kind)))

def shadow(value, int):
    return isinstance(value, (bytes, (int,)))

def closure(kind):
    def check(value):
        return isinstance(value, (str, (kind,)))
    assert check(Token())
    kind = Other
    gc.collect()
    assert check(Other())
    assert not check(Token())

class Choices:
    @property
    def first(self):
        events.append('first')
        gc.collect()
        return Token
    @property
    def second(self):
        events.append('second')
        gc.collect()
        return Other
    @property
    def invalid_read(self):
        events.append('error')
        gc.collect()
        raise ValueError('classinfo evaluation')

def operand():
    events.append('operand')
    gc.collect()
    return Token()

def rebind_operand():
    global Global
    Global = Token
    return Token()

def main():
    global Global
    pair(Token(), Token)
    pair(5, int)
    pair(Static(operand), Static)
    pair(classmethod(operand), classmethod)
    pair(property(operand), property)
    assert shadow(Token(), Token)
    assert not shadow(1, Token)
    closure(Token)
    Global = Other
    assert isinstance(rebind_operand(), (str, Global))
    choices = Choices()
    assert isinstance(operand(), (choices.first, choices.second))
    assert events == ['operand', 'first', 'second']
    events.clear()
    try:
        isinstance(operand(), (choices.first, choices.invalid_read))
    except ValueError as error:
        assert str(error) == 'classinfo evaluation'
    else:
        raise AssertionError('classinfo tuple skipped its later member')
    assert events == ['operand', 'first', 'error']
    gc.collect()
    print('LITERAL_CLASSINFO_OK')
main()
'''


def _literal_ir(source):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from ir_pointer_aliases import function_bodies

    module = infer_module(parse_and_lift(source, '<literal-classinfo>', 'literal'))
    text = str(L1CodeGen(module, ir_scaffold_mode='on').generate(module))
    bodies = dict(function_bodies(text))
    return bodies['user_literal_check']


@pytest.mark.parametrize('classinfo', (
    '(str, kind)', '(kind, str)', '(str, (bytes, kind))',
    '((kind,), ())', '(str, kinds[0])', '(str, provider.kind)',
))
def test_literal_dynamic_classinfo_uses_one_rooted_runtime_predicate(classinfo):
    body = _literal_ir('def check(value, kind, kinds, provider):\n'
                       '    return isinstance(value, ' + classinfo + ')\n')
    predicate = body.index('@py_obj_isinstance(')
    assert body.count('@py_obj_isinstance(') == 1
    assert '@py_str_check(' not in body
    assert '@py_tuple_new(' in body[:predicate]
    assert '@py_tuple_set_item(' in body[:predicate]
    assert 'isinstance.operand' in body[:predicate]
    assert 'isinstance.classinfo' in body[:predicate]
    assert '@pcc_gc_take_pinned_slot(' in body[predicate:]
    assert 'extern.argument.cleanup' in body
    from ir_pointer_aliases import canonical_pointer, pointer_bitcast_aliases

    aliases = pointer_bitcast_aliases(body)
    loads = dict(re.findall(
        r'(%[\w.$]+) = call ptr \(ptr, ptr\) @pcc_gc_load_ptr\(ptr null, ptr (%[\w.$]+)\)',
        body,
    ))
    args = re.search(r'@py_obj_isinstance\(ptr (%[\w.$]+), ptr (%[\w.$]+)\)', body)
    operand_slot = canonical_pointer(loads[args[1]], aliases)
    classinfo_slot = canonical_pointer(loads[args[2]], aliases)
    assert 'isinstance.operand' in operand_slot
    assert 'isinstance.classinfo' in classinfo_slot
    takes = [value for value, slot in re.findall(
        r'(%[\w.$]+) = call ptr \(ptr, i64\) @pcc_gc_take_pinned_slot\(ptr (%[\w.$]+),',
        body,
    ) if canonical_pointer(slot, aliases) == classinfo_slot]
    # The normal continuation and the predicate-error cleanup both consume
    # the tuple owner from the same authoritative slot used by the predicate.
    assert len(takes) == 2
    for value in takes:
        assert '@pcc_gc_release_known(ptr ' + value + ')' in body


def test_classinfo_tuple_reads_all_members_before_the_first_predicate():
    body = _literal_ir('def check(value, first, second):\n'
                       '    return isinstance(value, (first.kind, second.kind))\n')
    predicate = body.index('@py_obj_isinstance(')
    reads = list(re.finditer(r'call[^\n]*@py_obj_getattr\(', body))
    assert len(reads) == 2, body
    assert reads[0].start() < reads[1].start() < predicate
    assert body.count('@py_obj_isinstance(') == 1


def test_literal_dynamic_classinfo_reference(tmp_path):
    source = tmp_path / 'literal_reference.py'
    source.write_text(LITERAL_PROGRAM)
    result = subprocess.run([sys.executable, str(source)], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'LITERAL_CLASSINFO_OK\n'


@pytest.mark.integration
def test_literal_dynamic_classinfo_executes_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / 'literal_classinfo.py'
    source.write_text(LITERAL_PROGRAM)
    output = tmp_path / 'literal_classinfo'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    python_program_compiler(str(source), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True,
                                timeout=30, env=dict(os.environ,
                                    PATH='/nonexistent', PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'LITERAL_CLASSINFO_OK\n'


QUALIFIED_ENTRY = (
    'import tuple_left as first\nimport tuple_right as second\n'
    'def left(value, kind):\n'
    '    return isinstance(value, (first.Token, kind))\n'
    'def right(value, kind):\n'
    '    return isinstance(value, (second.Token, kind))\n'
)


def _write_qualified_modules(tmp_path, reverse, entry_tail=''):
    names = ['tuple_left', 'tuple_right']
    if reverse:
        names.reverse()
    paths = []
    for name in names:
        path = tmp_path / (name + '.py')
        path.write_text('class Token:\n    pass\n')
        paths.append(str(path))
    entry = tmp_path / 'tuple_main.py'
    entry.write_text(QUALIFIED_ENTRY + entry_tail)
    return paths + [str(entry)], names + ['tuple_main']


@pytest.mark.parametrize('reverse', (False, True))
def test_dynamic_tuple_reads_each_module_alias_once(tmp_path, monkeypatch, reverse):
    """``first.Token`` is a live import read (CPython looks the attribute up
    when the call runs), so each body reads its own alias, never a class
    symbol of either module, and tests the tuple with one runtime predicate."""
    from pcc.frontends.python.pipeline import compile_python_multi
    from ir_pointer_aliases import function_bodies

    paths, names = _write_qualified_modules(tmp_path, reverse)
    output = tmp_path / 'tuple_owner.ll'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    compile_python_multi(paths, str(output), module_names=names, entry_module='tuple_main',
                         emit_llvm_only=True, backend='self', libpython_mode='off',
                         ir_scaffold_mode='on')
    text = output.read_text()
    names_by_symbol = {
        symbol: re.sub(r'\\([0-9A-Fa-f]{2})', lambda match: chr(int(match.group(1), 16)), payload).rstrip('\x00')
        for symbol, payload in re.findall(
            r'^(@\.name\.dynamic\.attr\.\d+) = internal constant \[\d+ x i8\] c"([^"]*)"', text, re.M)
    }
    bodies = dict(function_bodies(text))
    for function, alias in (('left', 'first'), ('right', 'second')):
        body = bodies['user_tuple_main_' + function]
        assert '@.class.tuple_left.Token' not in body
        assert '@.class.tuple_right.Token' not in body
        read = {names_by_symbol.get(symbol)
                for symbol in re.findall(r'@\.name\.dynamic\.attr\.\d+', body)}
        assert read == {alias}, (function, read)
        assert body.count('@py_module_attr_get(') == 1
        assert body.count('@py_obj_isinstance(') == 1


@pytest.mark.integration
@pytest.mark.parametrize('reverse', (False, True))
def test_dynamic_tuple_keeps_qualified_module_class_identity(
    tmp_path, monkeypatch, reverse, pcc_runtime_archive,
):
    """Two modules define ``Token``; each tuple names its own module's class."""
    from pcc.frontends.python.pipeline import compile_python_multi

    paths, names = _write_qualified_modules(tmp_path, reverse, (
        'print(left(first.Token(), int), left(second.Token(), int), left(3, int),\n'
        '      right(second.Token(), int), right(first.Token(), int), right("x", int))\n'
    ))
    binary = tmp_path / 'tuple_owner'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    compile_python_multi(paths, str(binary), module_names=names, entry_module='tuple_main',
                         backend='self', libpython_mode='off', ir_scaffold_mode='on',
                         runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PATH='/nonexistent',
                                         PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'True False True True False False\n', backend
