"""Plain literal-format temporaries have bounded and observable lifetimes."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.format_lowering import FormatLoweringMixin
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.test_lambda_adapter_scope_lifetime import check_frames
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


def _emit_counted(count, distinct=False):
    template = '|'.join('{0!s:>4}' for _ in range(count))
    parameters = ', '.join('value' + str(index) for index in range(count)) if distinct else 'value'
    if distinct:
        template = '|'.join('{' + str(index) + '!s:>4}' for index in range(count))
    source = 'def probe(' + parameters + '):\n    ' + repr(template) + '.format(' + parameters + ')\n    return 0\n'
    module = infer_module(parse_and_lift(source, 'scratch.py', 'scratch'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    roots, cleanup_sizes = [], []
    new_root = codegen._new_slot_call_root
    new_cleanup = codegen._slot_call_cleanup_block
    def count_root(*args, **kwargs):
        roots.append(args[0])
        return new_root(*args, **kwargs)
    def count_cleanup(owned, *args, **kwargs):
        cleanup_sizes.append(len(owned))
        return new_cleanup(owned, *args, **kwargs)
    codegen._new_slot_call_root = count_root
    codegen._slot_call_cleanup_block = count_cleanup
    text = str(codegen.generate(module))
    assert 'strict.nolib.stub' not in text
    body = re.search(r'^define[^\n]*@user_scratch_probe\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    return body, roots, cleanup_sizes


def test_repeated_fields_reuse_scratch_owners_and_one_shared_cleanup():
    small, small_roots, small_cleanup = _emit_counted(4)
    large, large_roots, large_cleanup = _emit_counted(16)
    assert large_roots == small_roots
    assert sum(large_cleanup) == sum(small_cleanup)
    assert max(large_cleanup) == max(small_cleanup)
    assert len(large) < 5 * len(small)
    _assert_immediate_publication(large, 'py_obj_format')
    _assert_immediate_publication(large, 'py_str_concat')
    check_frames(large)


class _Slot:
    def __init__(self):
        self.value = None


class _Builder:
    def __init__(self):
        self.active = True
        self.blocks = {}

    def icmp_signed(self, op, left, right):
        assert op in ('==', '!=')
        equal = left == int(str(right))
        return equal if op == '==' else not equal

    def cbranch(self, condition, yes, no):
        self.blocks[yes] = self.active and condition
        self.blocks[no] = self.active and not condition

    def position_at_end(self, block):
        self.active = self.blocks.get(block, False)

    def branch(self, block):
        self.blocks[block] = self.blocks.get(block, False) or self.active

    def call(self, runtime, args, **kwargs):
        if not self.active:
            return 0
        if runtime == 'py_incref':
            return None
        if runtime == 'pcc_gc_store_root':
            args[0].value = None
            return None
        assert runtime == 'pcc_gc_root_move', runtime
        assert args[0].value is None
        args[0].value = args[1].value
        args[1].value = None
        return 0

    def load(self, slot, **kwargs):
        return slot.value


class _FormatHost(FormatLoweringMixin):
    """Execute the real emitter's owner operations with actual Python values.

    This tests emitter lifetime decisions, not the native GC implementation.
    Runtime publication/leases and error preservation have separate IR/runtime
    regressions; real compiled five-GC coverage remains an integration gate.
    """
    def __init__(self, value):
        self.value = value
        self.builder = _Builder()
        self.current_function = self
        self.serial = 0
        self.runtime = {name:name for name in ('py_incref', 'pcc_gc_store_root', 'pcc_gc_root_move')}
        self._try_err_block = None
        self._cpy_operand_cleanup_block = None
        self.slots = []

    def _resolve_str_literal_value(self, expr): return expr.value
    def _is_starred_unpack_expr(self, expr): return False
    def _expr_looks_cpython(self, expr): return False
    def _slot_call_result_sink(self, expr): return None
    def _current_try_err_block(self): return self._try_err_block
    def _ensure_fn_err_exit(self): return None
    def _fresh(self, label):
        self.serial += 1
        return label + str(self.serial)
    def append_basic_block(self, name): return name
    def _emit_builtin_exception_and_branch(self, name, message, span):
        if self.builder.active:
            raise TypeError(message)
    def _as_gc_ptr(self, value): return value
    def _slot_call_note_published(self, output): pass
    def _slot_call_check_status(self, status, *args): assert status == 0
    def _emit_str_literal(self, value): return value

    def _new_slot_call_root(self, label):
        slot = _Slot()
        self.slots.append(slot)
        return slot

    def _slot_call_cleanup_block(self, roots, target):
        return (roots, target)

    def _emit_slot_call_operand(self, expr, label):
        slot = self._new_slot_call_root(label)
        slot.value = self.value if hasattr(expr, 'ident') else expr.value
        return slot

    def _publish_slot_call_owned(self, slot, value, **kwargs):
        if not self.builder.active:
            return
        assert slot.value is None
        slot.value = value

    def _slot_call_runtime_call(self, runtime, roots, *, result_slot=None, **kwargs):
        if not self.builder.active:
            return 0
        if runtime == 'py_obj_type_tag':
            return 4 if isinstance(roots[0].value, str) else -1
        if runtime == 'py_str_check':
            assert result_slot is None
            return int(isinstance(roots[0].value, str))
        if runtime == 'py_str_byte_len':
            assert result_slot is None
            return len(str.encode(roots[0].value, 'utf-8'))
        assert result_slot.value is None
        try:
            if runtime == 'py_obj_format':
                result_slot.value = format(roots[0].value, roots[1].value)
            elif runtime == 'py_str_concat':
                result_slot.value = roots[0].value + roots[1].value
            else:
                assert runtime in ('py_obj_str', 'py_obj_repr', 'py_obj_ascii')
                convert = {'py_obj_str':str, 'py_obj_repr':repr, 'py_obj_ascii':ascii}[runtime]
                result_slot.value = convert(roots[0].value)
        except Exception:
            cleanup = self._try_err_block
            while cleanup is not None:
                owned, cleanup = cleanup
                self._release_slot_call_roots(owned)
            raise

    def _release_slot_call_roots(self, roots):
        for slot in reversed(roots):
            slot.value = None

    def _take_slot_call_root(self, slot):
        result = slot.value
        slot.value = None
        return result

    def render(self, template):
        module = parse_and_lift(repr(template) + '.format(value)', 'model.py', 'model')
        return self._maybe_emit_literal_str_format(module.body[0].expr)


@pytest.mark.parametrize('template', ('{0:a}|{0:b}', 'prefix{0:a}{0:b}suffix', '{0:a}', '{0:}'))
def test_format_field_finalizer_order_and_single_field_identity_match_cpython(template):
    def outcome(use_emitter):
        events = []
        class Rendered(str):
            def __del__(self): events.append('drop:' + str(self))
        class Value:
            def __format__(self, spec):
                events.append('format:' + spec)
                return Rendered(spec)
        value = Value()
        host = _FormatHost(value)
        result = host.render(template) if use_emitter else template.format(value)
        before = list(events)
        is_subclass = type(result) is Rendered
        text = str(result)
        del result
        return text, is_subclass, before, events
    assert outcome(True) == outcome(False)


def test_format_callback_error_retires_previous_field_before_failing_callback():
    events = []
    class Rendered(str):
        def __del__(self): events.append('drop:' + str(self))
    class Value:
        def __format__(self, spec):
            events.append('format:' + spec)
            if spec == 'b':
                raise ValueError('field-error')
            return Rendered(spec)
    host = _FormatHost(Value())
    with pytest.raises(ValueError, match='field-error'):
        host.render('{0:a}{0:b}')
    assert events == ['format:a', 'drop:a', 'format:b']
    assert all(slot.value is None for slot in host.slots)


def test_distinct_arguments_use_one_shared_prefix_cleanup():
    small, _, small_cleanup = _emit_counted(4, distinct=True)
    large, _, large_cleanup = _emit_counted(16, distinct=True)
    assert sum(large_cleanup) < 5 * sum(small_cleanup)
    assert len(large) < 5 * len(small)
    check_frames(large)


def test_format_arguments_are_eager_once_and_fields_remain_in_order():
    from tests.python.test_shared_call_binding import _emit, _function
    body = _function(_emit("def first():\n    return 1\ndef second():\n    return 2\n"
                           "def probe():\n    return '{0:x}|{0:y}|{1}'.format(first(), second())\n"))
    first = list(re.finditer(r'call [^\n]*@user_binding_first\(', body))
    second = list(re.finditer(r'call [^\n]*@user_binding_second\(', body))
    fields = list(re.finditer(r'call [^\n]*@py_obj_format\(', body))
    assert len(first) == len(second) == 1 and len(fields) == 3
    assert first[0].start() < second[0].start() < fields[0].start()
    check_frames(body)


@pytest.mark.parametrize('template', ('{0:}', '{0:a}', 'literal', ''))
def test_single_or_empty_format_paths_have_balanced_owners(template):
    from tests.python.test_shared_call_binding import _emit, _function
    body = _function(_emit('def probe(value):\n    return ' + repr(template) + '.format(value)\n'))
    check_frames(body)
    if '{' in template:
        _assert_immediate_publication(body, 'py_obj_format')
        assert body.index('@py_str_check(') < body.index('@py_str_byte_len(')
