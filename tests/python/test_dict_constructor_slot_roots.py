"""Dict producer publication and operand lifetime before caller cleanup.

These execute host lowering and a relocating boundary model. They do not
qualify helper-internal GC behavior or substitute for emitted native execution.
"""
from types import SimpleNamespace
import re

import pytest

from pcc.frontends.python.codegen.dict_lowering import DictLoweringMixin
from pcc.frontends.python.codegen.unary_call_lowering import UnaryCallLoweringMixin
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Attr, Call, DynType, Name, StrLit
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


EXPRESSIONS = (
    'dict()', 'dict(answer=value)', 'dict(source)',
    'dict(source, answer=value)', 'dict.fromkeys(source)',
    'dict.fromkeys(source, value)',
)


@pytest.mark.parametrize('expression', EXPRESSIONS)
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'discard', 'later-error'))
def test_dict_producer_contexts_publish_immediately(expression, site):
    prefix = ('def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def saved(item=' + expression + '):\n        return item\n    return saved()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'discard':
        body = '    ' + expression + '\n    return None\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _emit(prefix + 'def probe(source: dict, value):\n' + body)
    function = _function(text)
    runtime = 'py_dict_fromkeys' if '.fromkeys' in expression else 'py_dict_new'
    _assert_immediate_publication(function, runtime)
    if 'source' in expression and '.fromkeys' not in expression:
        for helper in ('py_dict_keys', 'py_list_get', 'py_dict_get'):
            _assert_immediate_publication(function, helper)
        assert '@py_dict_set_slots(' in function
    if site in ('argument', 'default', 'later-error'):
        assert 'dict.constructor.result.operand' not in function
        assert 'dict.fromkeys.result.operand' not in function
    else:
        assert '@pcc_gc_take_pinned_slot(' in function


@pytest.mark.parametrize('expression', EXPRESSIONS)
def test_dict_constructor_exception_edges_balance_roots(expression):
    from tests.python.test_slot_call_lexical_roots import _emit as emit_roots
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    source = ('def take(*, value, later, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n'
              'def probe(source, value):\n    try:\n'
              '        return take(value=' + expression + ', later=fail())\n'
              '    except Exception:\n        return None\n')
    codegen, text = emit_roots(source)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)
    assert all(flag is None and lifo for _slot, flag, lifo in codegen._slot_call_root_records)


def test_copy_roots_operands_and_publishes_before_checks(monkeypatch):
    original = L1CodeGen._slot_call_runtime_call
    observed = []
    def runtime_call(self, name, roots, **kwargs):
        if name in ('py_dict_new', 'py_dict_keys', 'py_list_get', 'py_dict_get'):
            for root in roots:
                self._slot_call_root_record(root)
            output = kwargs.get('result_slot')
            assert output is not None
            self._slot_call_root_record(output)
            observed.append(name)
        return original(self, name, roots, **kwargs)
    monkeypatch.setattr(L1CodeGen, '_slot_call_runtime_call', runtime_call)
    text = _emit('def probe(source):\n    return dict(source)\n')
    assert observed == ['py_dict_new', 'py_dict_keys', 'py_list_get', 'py_dict_get']
    function = _function(text)
    assert 'dict.copy.notmapping' in function
    assert re.search(r'call i64[^\n]*@py_dict_set_slots\(', function)
    assert not re.search(r'call void[^\n]*@py_dict_set\(', function)


class _Root:
    def __init__(self, name):
        self.name, self.current = name, None


class _MovingBoundaryModel(DictLoweringMixin, UnaryCallLoweringMixin):
    """Run the real non-loop lowering bodies against a slot-only contract.

    Boundary events invalidate every prior raw view. Root loads must happen
    after operand disposal; the caller's result root must remain registered.
    """
    def __init__(self, values, sink):
        self.values, self.events, self.roots = values, [], []
        self.generation = 0
        self._try_err_block = self._cpy_operand_cleanup_block = None
        self.current_class = None
        self.runtime = {'py_dict_set_slots': 'py_dict_set_slots'}
        self.sink = self._new_slot_call_root('caller') if sink else None
        self.builder = SimpleNamespace(load=self.load, call=self.call)

    def boundary(self, event):
        self.generation += 1
        self.events.append(event)

    def _fresh(self, label):
        return label

    def _current_try_err_block(self):
        return self._try_err_block

    def _ensure_fn_err_exit(self):
        return 'error'

    def _slot_call_result_sink(self, expr):
        return self.sink

    def _new_slot_call_root(self, label):
        self.boundary('register:' + label)
        root = _Root(label)
        self.roots.append(root)
        return root

    def _slot_call_cleanup_block(self, roots, target):
        assert all(root in self.roots for root in roots)
        return target

    def _has_starred_unpack(self, args):
        return False

    def _has_unpacked_operands(self, args, kwargs):
        return False

    def _emit_slot_call_operand(self, expr, label):
        root = self._new_slot_call_root(label)
        self.boundary('evaluate:' + label)
        root.current = expr.value if isinstance(expr, StrLit) else self.values[expr.ident]
        return root

    def _emit_none_literal(self):
        return None

    def _publish_slot_call_owned(self, root, value, **kwargs):
        assert root in self.roots
        root.current = value
        self.boundary('publish:' + root.name)

    def _slot_call_runtime_call(self, name, roots, result_slot=None, **kwargs):
        assert result_slot in self.roots
        assert all(root in self.roots for root in roots)
        self.boundary(name)
        if name == 'py_dict_new':
            value = {}
        else:
            assert name == 'py_dict_fromkeys'
            value = dict.fromkeys(roots[0].current, roots[1].current)
        self._publish_slot_call_owned(result_slot, value)

    def _as_gc_ptr(self, root):
        assert root in self.roots
        return root

    def call(self, runtime, roots, **kwargs):
        assert runtime == 'py_dict_set_slots'
        assert all(root in self.roots for root in roots)
        self.boundary(runtime)
        roots[0].current[roots[1].current] = roots[2].current
        return 0

    def _slot_call_check_status(self, *args):
        pass

    def _emit_post_call_err_check(self, *args):
        pass

    def _release_slot_call_roots(self, roots):
        for root in reversed(roots):
            assert self.roots[-1] is root
            root.current = None
            self.roots.pop()
            self.boundary('dispose:' + root.name)

    def load(self, root, **kwargs):
        assert root in self.roots
        return root.current, self.generation

    def _take_slot_call_root(self, root):
        assert self.roots == [root]
        value = self.load(root)
        self.roots.pop()
        return value


@pytest.mark.parametrize('sink', (False, True))
@pytest.mark.parametrize('kind', ('empty', 'keywords', 'fromkeys-default', 'fromkeys-value'))
def test_real_lowerer_boundary_model_keeps_alias_and_caller_owner(kind, sink):
    value = []
    source = ['first', 'second', 'first']
    model = _MovingBoundaryModel({'source': source, 'value': value}, sink)
    name = lambda ident: Name(span=None, ty=DynType(name='dyn'), ident=ident)
    args, kwargs = (), ()
    func = name('dict')
    if kind == 'keywords':
        kwargs = (('first', name('value')), ('second', name('value')))
    if kind.startswith('fromkeys'):
        func = Attr(span=None, ty=DynType(name='dyn'), obj=func, name='fromkeys')
        args = (name('source'),) + ((name('value'),) if kind.endswith('value') else ())
    expression = Call(span=None, ty=DynType(name='dyn'), func=func, args=args, kwargs=kwargs)
    result, generation = (model._maybe_emit_builtin_type_method(expression)
                          if kind.startswith('fromkeys') else model._maybe_emit_dict_builtin(expression))
    assert generation == model.generation, 'returned a raw view from before cleanup'
    assert model.roots == ([model.sink] if sink else [])
    expected = {} if kind == 'empty' else {'first': None, 'second': None}
    if kind in ('keywords', 'fromkeys-value'):
        expected = {'first': value, 'second': value}
        assert result['first'] is result['second'] is value
    assert result == expected
    evaluations = [event for event in model.events if event.startswith('evaluate:')]
    if kind == 'fromkeys-value':
        assert evaluations == ['evaluate:dict.fromkeys.iterable', 'evaluate:dict.fromkeys.value']
    if kind == 'keywords':
        assert max(i for i, event in enumerate(model.events) if event.startswith('evaluate:')) < model.events.index('py_dict_new')
