"""Method publication keeps NEW functions and descriptors in class-body owners."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re

import pytest

from class_method_ir_checks import (
    check_method_frames,
    method_owner_copies,
)

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def emit(source):
    module = infer_module(parse_and_lift(source, 'method_roots.py', 'method_roots'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    proof = check_method_frames(text) if 'class.method.publication' in text else []
    output = os.environ.get('PCC_METHOD_IR_DIR')
    if output:
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(source.encode()).hexdigest()
        (directory / (key + '.ll')).write_text(text)
        (directory / (key + '.json')).write_text(json.dumps(proof, indent=2) + '\n')
    return text


METHODS = {
    'instance': '    def first(self, value="first"):\n        return value\n',
    'alias': '    def first(self, value="first"):\n        return value\n    second = first\n',
    'alias_chain': '    def first(self, value="first"):\n        return value\n    second = first\n    third = second\n',
    'static': '    @staticmethod\n    def first(value="first"):\n        return value\n',
    'class': '    @classmethod\n    def first(cls, value="first"):\n        return value\n',
    'static_alias': '    @staticmethod\n    def first(value="first"):\n        return value\n    second = first\n',
    'class_alias': '    @classmethod\n    def first(cls, value="first"):\n        return value\n    second = first\n',
}


@pytest.mark.parametrize('kind', tuple(METHODS))
def test_method_publication_keeps_an_authoritative_result_owner(kind):
    text = emit('class Box:\n' + METHODS[kind])
    assert 'class.method.publication' in text
    assert '@py_class_add_method(' in text and '@py_class_setattr_raw(' in text
    # The terminal callable producer and descriptor constructors must publish
    # before the first lease, error check, or other parking call.
    pattern = re.compile(r'(%[^ ]+) = call [^\n]*@(pcc_gc_take_pinned_slot|py_staticmethod_new|py_classmethod_new)\([^\n]*\)\n'
                         r'\s*store ptr \1, ptr (%[^ ,\n]*class\.method\.[^ ,\n]*)')
    matches = list(pattern.finditer(text))
    assert matches, kind
    if kind.startswith('static'):
        assert any(match[2] == 'py_staticmethod_new' for match in matches)
    elif kind.startswith('class'):
        assert any(match[2] == 'py_classmethod_new' for match in matches)
    # emit() has resolved bitcast aliases and checked LIFO/disposal on all
    # reachable returns and cleanup edges of the emitted module functions.


def test_method_alias_copies_from_the_original_owning_root():
    text = emit('class Box:\n' + METHODS['alias_chain'])
    methods = method_owner_copies(text)
    assert len(methods) >= 2
    assert all(dest != source for _function, dest, source in methods)


def test_property_accessors_retain_their_existing_publication_route():
    text = emit('''class Box:
    @property
    def value(self):
        return "property"
''')
    calls = set(re.findall(r'\bcall\b[^\n]*?@([\w.$]+)\(', text))
    assert 'py_property_new' in calls
    assert 'py_classmethod_new' not in calls
    assert 'py_staticmethod_new' not in calls


def test_method_roots_cover_later_default_failure():
    text = emit('''def failure():
    raise ValueError("later-default")
class Box:
    def first(self, value="first"):
        return value
    def second(self, value=failure()):
        return value
''')
    assert text.count('class.method.publication') > 1
    assert 'call.slot.cleanup' in text
    assert '@pcc_gc_store_root(' in text


def test_method_and_factory_lifetimes_share_reverse_registration_order():
    path = Path(__file__).parents[2] / 'pcc/frontends/python/codegen/class_gen.py'
    tree = ast.parse(path.read_text())
    lifetime_loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)
                      and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name)
                      and node.iter.func.id == 'reversed' and len(node.iter.args) == 1
                      and isinstance(node.iter.args[0], ast.Name)
                      and node.iter.args[0].id == 'class_body_lifetimes']
    assert len(lifetime_loops) == 1
    loop = lifetime_loops[0]
    calls = {node.func.attr for node in ast.walk(loop)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert {'_release_slot_call_roots', '_leave_container_temp_root', '_gc_release'} <= calls
    assert 'pinned_methods' not in path.read_text()


def test_interleaved_dataclass_factory_and_methods_lower():
    text = emit('''from dataclasses import dataclass, field

def factory():
    return []

@dataclass
class Box:
    def first(self):
        return "first"
    value: list = field(default_factory=factory)
    def second(self):
        return "second"
''')
    assert 'class.method.publication' in text
    assert 'class.factory.capture' in text


@pytest.mark.parametrize('static', (False, True))
def test_prepared_namespace_method_reuses_its_original_default(static):
    decorator = '    @staticmethod\n' if static else ''
    parameters = 'value=factory()' if static else 'self, value=factory()'
    source = '''def factory():
    return []
class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
class Box(metaclass=Meta):
''' + decorator + '    def first(' + parameters + '):\n        return value\n    second = first\n'
    text = emit(source)
    for symbol in ('main', '_pcc_py_module_init_method_roots'):
        body = re.search(r'^define [^\n]*@' + re.escape(symbol) + r'\([^\n]*\).*?^}', text, re.M | re.S)
        assert body is not None, symbol
        calls = re.findall(r'\bcall\b[^\n]*@user_method_roots_factory\(', body[0])
        assert len(calls) == 1, (symbol, len(calls))
    assert method_owner_copies(text)
