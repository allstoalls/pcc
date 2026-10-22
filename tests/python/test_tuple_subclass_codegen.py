"""Small IR checks; no native runtime, linking or emitted execution."""
import ast
from pathlib import Path
import re
import pytest
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from pcc.frontends.python.codegen.layer1 import L1CodeGen


def emit(source):
    module=infer_module(parse_and_lift(source,'tuple_boundary.py','tuple_boundary'))
    generator=L1CodeGen(module,emit_cpy_main_exitcode=False,ir_scaffold_mode='on')
    generator._strict_no_libpython=True
    text=str(generator.generate())
    assert not re.search(r'call[^\n]*@py_cpy_',text)
    return text


def test_original_iso_calendar_tuple_class_reaches_owned_super_lookup():
    path=Path(__file__).resolve().parents[2]/'pcc/stdlib/datetime.py'
    source=path.read_text()
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='IsoCalendarDate')
    original=ast.get_source_segment(source,node)
    text=emit(original+'\ndef check(value):\n    return isinstance(value,tuple)\n')
    assert re.search(r'call[^\n]*@py_super_new_lookup_slots',text)
    assert re.search(r'call[^\n]*@py_tuple_check',text)


@pytest.mark.parametrize('base',('tuple','Sequence'))
def test_generic_tuple_alias_super_lookup_precedes_argument_evaluation(base):
    source='from builtins import tuple as Sequence\n' if base=='Sequence' else ''
    source+='''def operand():
    return (1,2)
class Pair(BASE):
    def __new__(cls):
        return super().__new__(cls,operand())
'''.replace('BASE',base)
    text=emit(source)
    bodies=[body for body in text.split('\ndefine ') if re.search(r'call[^\n]*@py_super_new_lookup_slots',body)]
    assert len(bodies)==1
    lookup=re.search(r'call[^\n]*@py_super_new_lookup_slots',bodies[0]).start()
    operand=[match.start() for match in re.finditer(r'call[^\n]*@[^ \n(]*_operand\(',bodies[0])]
    assert operand and min(operand)>lookup


def test_user_class_named_tuple_keeps_ordinary_super_dispatch():
    text=emit('''class tuple:
    pass
class Child(tuple):
    def __new__(cls):
        return super().__new__(cls)
''')
    assert not re.search(r'call[^\n]*@py_super_new_lookup_slots',text)
