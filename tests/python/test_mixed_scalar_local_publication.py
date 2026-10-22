"""Planned object locals own their scalar boxing result on every binding path."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)


_BODY = '''\
class Boxed:
    def __init__(self, value):
        self.value = value

class Calculator:
    def evaluate(self, left, right, as_float):
        def numeric(value):
            if isinstance(value, Boxed):
                return value.value
            return value
        if as_float:
            lhs = float(numeric(left))
            rhs = float(numeric(right))
            return lhs + rhs
        lhs = Boxed(numeric(left))
        rhs = Boxed(numeric(right))
        return lhs.value + rhs.value

    def replace(self, borrowed, value):
        item = borrowed
        item = float(value)
        if value == 0:
            item = Boxed(9)
            return item.value
        return item + 0.5
'''


class PublicationProbe(L1CodeGen):
    def _slot_call_name_source(self, expr):
        if expr.ident in self._planned_object_local_names:
            slot, ir_ty, _ty = self.env[expr.ident]
            if expr.ident in ('lhs', 'rhs', 'item'):
                assert expr.ident in self._owned_local_names
                assert self._owned_local_flag_for(expr.ident, slot) is not None
                assert any(entry[1] is slot for entry in
                           self._fn_gc_root_slot_registry[self.current_function.name])
        return super()._slot_call_name_source(expr)


@pytest.mark.parametrize('module_name', ('scalar_locals', 'pcc.frontends.c.codegen.c_codegen'))
def test_class_method_mixed_scalars_publish_into_owning_roots(module_name):
    module = infer_module(parse_and_lift(_BODY, 'scalar_locals.py', module_name))
    codegen = PublicationProbe(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@py_float_from_f64\([^\n]*\)\n', text, re.M))
    assert len(calls) >= 3
    for call in calls:
        following = text[call.end():].splitlines()[0].strip()
        assert following.startswith('store ptr ' + call.group(1) + ','), following
        assert '.scalar.box' in following, following
    assert text.count('.scalar.move') >= 3
    assert '@pcc_gc_root_move(' in text
    assert '@py_obj_add(' in text


PROGRAM = '''\
import gc
''' + _BODY + '''\
def main():
    calc = Calculator()
    for index in range(5):
        assert calc.evaluate(1.25, 2.5, True) == 3.75
        assert calc.evaluate(3, 4, False) == 7
        shared = Boxed(23)
        assert calc.replace(shared, 2.5) == 3.0
        gc.collect()
        assert shared.value == 23
        assert calc.replace(shared, 0.0) == 9
        assert shared.value == 23
    print('MIXED_SCALAR_LOCAL_OWNER_OK')
main()
'''


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_mixed_scalar_local_native_ownership(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(
        PROGRAM, 'MIXED_SCALAR_LOCAL_OWNER_OK\n', tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe='2',
    )
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''


def test_scalar_box_error_edge_precedes_releasing_previous_local():
    source = '''class Boxed:
    def __init__(self, value):
        self.value = value
class Allocator:
    def probe(self, value):
        item = Boxed(23)
        try:
            item = float(value)
        except MemoryError:
            return item.value
        return item + 0.5
'''
    module = infer_module(parse_and_lift(source, 'box_error.py', 'box_error'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    match = re.search(
        r'^define [^\n]*@user_box_error_Allocator_probe\([^\n]*\).*?^}',
        text, re.M | re.S,
    )
    assert match is not None
    blocks = {
        block.group(1): block.group(2)
        for block in re.finditer(
            r'^([\w.]+):\n(.*?)(?=^[\w.]+:|^})',
            match.group(0), re.M | re.S,
        )
    }
    boxing = [name for name, body in blocks.items() if '@py_float_from_f64(' in body]
    assert len(boxing) == 1
    name = boxing[0]
    body = blocks[name].split('@py_float_from_f64(', 1)[1]
    visited = set()
    while True:
        assert name not in visited
        visited.add(name)
        # Follow successful publication leases. A pending allocation error
        # must get its own normal-path check before either old-owner release
        # or clearing/replacing the local, even when both leases succeed.
        assert '.owned.load' not in body, body
        assert '.scalar.move' not in body, body
        assert not re.search(r'store ptr null, ptr %item\.addr', body), body
        if '@py_err_occurred(' in body:
            assert 'icmp ne i64' in body
            edge = re.search(r'br i1 [^,]+, label %([^,]+), label %([^\s]+)', body)
            assert edge is not None
            error_name = edge.group(1)
            success_name = edge.group(2)
            break
        edge = re.search(r'br i1 [^,]+, label %([^,]+), label %([^\s]+)', body)
        assert edge is not None, body
        assert 'icmp slt i64' in body, body
        name = edge.group(2)
        body = blocks[name]
    # The error edge first disposes only the freshly published scratch owner.
    # The previous item remains available to the surrounding except handler.
    seen = set()
    while not error_name.startswith('call.slot.cleanup.'):
        assert error_name not in seen
        seen.add(error_name)
        error_body = blocks[error_name]
        assert '.owned.release' not in error_body
        assert '.scalar.move' not in error_body
        edge = re.search(r'br label %([^\s]+)', error_body)
        assert edge is not None, error_body
        error_name = edge.group(1)
    cleanup = blocks[error_name]
    assert 'scalar.box' in cleanup
    assert 'item.addr' not in cleanup
    # The allocator is also allowed to report NULL without a pending TLS
    # exception. That path must raise before replacing the old binding.
    success = blocks[success_name]
    assert 'local.scalar.current' in success
    assert 'icmp eq ptr' in success
    assert '.owned.load' not in success
    null_edge = re.search(r'br i1 [^,]+, label %([^,]+), label %([^\s]+)', success)
    assert null_edge is not None
    null_error = blocks[null_edge.group(1)]
    assert '@py_exc_new(' in null_error
    assert '@py_raise(' in null_error
    assert 'exc.MemoryError' in null_error
    assert 'br label %' + error_name in null_error
    assert '.owned.release' not in null_error
    assert '.scalar.move' not in null_error
    assert 'item.addr' not in null_error
