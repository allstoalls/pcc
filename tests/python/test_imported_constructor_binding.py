"""Module-qualified constructors keep Python operand shape until binding."""
import re

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module

PROVIDER = '''class Required:
    def __init__(self, a, /, b, *, c):
        self.a = a
        self.b = b
        self.c = c
class AllKinds:
    def __init__(self, a, /, b=2, *items, c=3, **extras):
        self.a = a
        self.b = b
        self.items = items
        self.c = c
        self.extras = extras
class Ordered:
    def __init__(self, first, second, third):
        self.first = first
'''


def _compile(tmp_path, monkeypatch, body, provider_source=PROVIDER):
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    monkeypatch.setenv('PCC_DIRECT_INDEXED_KERNEL_EMIT', '0')
    monkeypatch.setenv('PCC_DIRECT_INDEXED_KERNEL_CAPTURE', '0')
    provider = tmp_path / 'binding_provider.py'
    entry = tmp_path / 'entry.py'
    provider.write_text(provider_source)
    entry.write_text('import binding_provider as provider\n' + body)
    modules, exports, _ = build_closed_world_context(
        [str(provider), str(entry)], ['binding_provider', 'entry'])
    outputs = []
    for module in modules:
        typed = infer_module(module, external_exports={
            key: value for key, value in exports.items() if key != module.name})
        codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        emitted = str(codegen.generate(typed))
        (tmp_path / (module.name + '.ll')).write_text(emitted)
        assert emit_owned_object(emitted, 'x86_64-unknown-linux-gnu')[:4] == b'\x7fELF'
        assert not re.search(r'\bcall [^\n]*@py_cpy_', emitted)
        outputs.append(emitted)
    return outputs[-1]


def _body(text, name='probe'):
    match = re.search(r'^define [^\n]*@user_entry_' + name + r'\([^\n]*\).*?^}', text, re.M | re.S)
    assert match is not None, name
    return match.group(0)


@pytest.mark.parametrize('expression', [
    'provider.Required(1, 2, c=3)',
    'provider.Required(1, c=3, b=2)',
    'provider.AllKinds(1, 2, 4, 5, c=3, extra=6)',
    'provider.AllKinds(1, a=7)',  # positional-only name belongs in **extras
    'provider.AllKinds(1)',
    'provider.AllKinds(*(1, 2, 4), **{"c": 3, "extra": 6})',
    'provider.AllKinds(1, **{"c": 3}, extra=6, **{"last": 7})',
    'provider.Required(1, 2, b=3, c=4)',  # runtime duplicate binding
    'provider.Required(a=1, b=2, c=3)',  # runtime positional-only diagnostic
    'provider.Required(1, c=3)',  # runtime missing argument
    'provider.Required(1, 2, **{"c": 3}, **{"c": 4})',
    'provider.Required(1, 2, c=3, **{"c": 4})',
])
def test_imported_constructor_uses_one_published_binder(tmp_path, monkeypatch, expression):
    text = _compile(tmp_path, monkeypatch, 'def probe():\n    return ' + expression + '\n')
    body = _body(text)
    calls = list(re.finditer(r'\bcall [^\n]*@py_obj_call_slots\(([^\n)]*)\)', body))
    assert len(calls) == 1
    assert not re.search(r'\bcall [^\n]*@py_obj_call\(', body)
    assert not re.search(r'\bcall [^\n]*@user_binding_provider_[^\n]*__init__\(', body)
    aliases = dict(re.findall(r'(%[^\s]+) = bitcast ptr (%[^\s]+) to ptr', body))

    def root_slot(value):
        seen = set()
        while value in aliases:
            assert value not in seen, value
            seen.add(value)
            value = aliases[value]
        return value

    slots = [root_slot(value) for value in re.findall(r'ptr (%[^,\s]+)', calls[0].group(1))]
    assert len(slots) == len(set(slots)) == 4
    class_name = expression.split('provider.', 1)[1].split('(', 1)[0]
    registrations = list(re.finditer(
        r'\bcall [^\n]*@pcc_gc_frame_enter_lifo\(ptr [^,\n]+, ptr ([^\n)]+)\)', body))
    # Each distinct owning cell uses the lexical LIFO protocol, including
    # the result cell that survives operand cleanup until the final take.
    for role, slot in zip(('callable', 'args', 'kwargs', 'result'), slots):
        assert slot.startswith('%compiled.module.call.' + class_name + '.' + role + '.operand.')
        allocation = re.search(re.escape(slot) + r' = alloca ptr\b', body)
        initialized = re.search(r'store ptr null, ptr ' + re.escape(slot) + r'(?=\s|$)', body)
        registered = [match for match in registrations if root_slot(match.group(1)) == slot]
        assert allocation is not None and initialized is not None
        assert len(registered) == 1
        assert allocation.start() < initialized.start() < registered[0].start() < calls[0].start()
    # The slot ABI returns status; its fourth argument retains the new owner
    # through operand cleanup and transfers that same owner at the return.
    takes = list(re.finditer(r'\bcall [^\n]*@pcc_gc_take_pinned_slot\(ptr ([^,\n]+),', body))
    assert len(takes) == 1
    assert root_slot(takes[0].group(1)) == slots[3]
    assert calls[0].start() < takes[0].start()
    # Follow successful CFG edges, since error cleanup blocks can appear
    # between the invocation and take in textual order.
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module
    function = next(fn for fn in parse_self_backend_module(text).functions
                    if fn.name == 'user_entry_probe')
    blocks = get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    by_name = {block.name: block for block in blocks}
    block = next(block for block in blocks if any(
        ins.kind == 'call' and ins.data[2] == 'py_obj_call_slots' for ins in block.instructions))
    left, cleared, seen = [], [], set()
    finished = False
    while not finished:
        assert block.name not in seen
        seen.add(block.name)
        for ins in block.instructions:
            if ins.kind != 'call':
                continue
            runtime = ins.data[2]
            if runtime == 'pcc_gc_take_pinned_slot':
                finished = True
                break
            if runtime in ('pcc_gc_frame_leave_lifo', 'pcc_gc_store_root'):
                owner = root_slot('%' + ins.data[4][0][1])
                if owner in slots:
                    (left if runtime == 'pcc_gc_frame_leave_lifo' else cleared).append(owner)
        if not finished:
            term = block.terminator
            assert term.kind in ('br', 'br_cond')
            block = by_name[term.data[0] if term.kind == 'br' else term.data[2]]
    assert left == [slots[2], slots[1], slots[0], slots[3]]
    assert slots[3] not in cleared, 'result owner must survive operand cleanup until take'



@pytest.mark.parametrize('expression,order', [
    ('provider.Ordered(first=step_first(), third=step_third(), second=step_second())',
     ['first', 'third', 'second']),
    ('provider.Ordered(step_first(), third=step_third(), second=step_second())',
     ['first', 'third', 'second']),
    ('provider.AllKinds(step_first(), **mapping_one(), extra=step_second(), **mapping_two())',
     ['first', 'mapping_one', 'second', 'mapping_two']),
])
def test_imported_constructor_evaluates_operands_once_in_source_order(tmp_path, monkeypatch, expression, order):
    source = '''def step_first():
    print("first")
    return 1
def step_second():
    print("second")
    return 2
def step_third():
    print("third")
    return 3
def mapping_one():
    print("mapping_one")
    return {"c": 3}
def mapping_two():
    print("mapping_two")
    return {"last": 4}
def probe():
    return ''' + expression + '\n'
    body = _body(_compile(tmp_path, monkeypatch, source))
    calls = re.findall(r'\bcall [^\n]*@user_entry_(step_first|step_second|step_third|mapping_one|mapping_two)\(', body)
    assert [call.removeprefix('step_') for call in calls] == order


def test_native_binding_fixture_verifies_owned_ir(tmp_path, monkeypatch):
    import ast
    from pathlib import Path
    source = Path(__file__).with_name('test_imported_constructor_binding_native.py').read_text()
    constants = {node.targets[0].id: ast.literal_eval(node.value)
                 for node in ast.parse(source).body if isinstance(node, ast.Assign)
                 and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id in ('PROVIDER_SOURCE', 'ENTRY_SOURCE')}
    _compile(tmp_path, monkeypatch, constants['ENTRY_SOURCE'], constants['PROVIDER_SOURCE'])
