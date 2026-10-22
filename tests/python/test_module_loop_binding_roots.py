"""Module loop names use persistent, initialized namespace owners."""
import re

import pytest

from tests.python.test_shared_call_binding import _emit
from tests.python.owned_regression_support import explicit_owned_runtime


@pytest.mark.parametrize('loop', (
    "for item in [{'name': 'first'}, {'name': 'last'}]:\n    result = item.get('name')\n",
    "if True:\n    for item in [{'name': 'last'}]:\n        result = item.get('name')\n",
    "try:\n    for item in [{'name': 'last'}]:\n        result = item.get('name')\nexcept ValueError:\n    pass\n",
    "for item in ({'name': 'last'},):\n    result = item.get('name')\n",
))
def test_module_loop_object_reads_have_a_real_global_root(loop,tmp_path):
    text=_emit(loop)
    (tmp_path/'program.ll').write_text(text)
    assert bool(re.search(r'^@\.modvar\.binding\.item = global ptr null$', text, re.M)), 'missing module target object slot'
    assert '@.modvar.binding.item_initialized' in text
    assert re.search(r'\bcall [^\n]*@pcc_gc_root_copy_lease\([^\n]*',text)
    assert 'item.for.obj.addr' not in text
    assert '.pcc.for.binding.module' in text


@pytest.mark.parametrize('body', (
    "for item in []:\n    pass\nprint(item)\n",
    "item = 9\nfor item in []:\n    pass\nprint(item)\n",
    "for item in range(3):\n    pass\nprint(item)\n",
    "for left, item in [(1, {'name': 'last'})]:\n    result = item.get('name')\n",
))
def test_loop_binding_storage_and_bound_flag_cover_join_edges(body,tmp_path):
    text=_emit(body);(tmp_path/'program.ll').write_text(text)
    assert bool(re.search(r'^@\.modvar\.binding\.item = global ptr null$', text, re.M)), 'missing module target object slot'
    assert '@.modvar.binding.item_initialized' in text
    assert 'item.for.obj.addr' not in text


def test_function_defined_before_loop_reads_the_same_module_binding(tmp_path):
    text=_emit("def read():\n    return item.get('name')\nfor item in [{'name': 'last'}]:\n    result = read()\n")
    (tmp_path/'program.ll').write_text(text)
    body=re.search(r'^define [^\n]*@user_binding_read\([^\n]*\).*?^}',text,re.M|re.S).group(0)
    assert '@.modvar.binding.item' in body
    flag = re.search(r'(%[\w.$]+) = load i1, ptr @\.modvar\.binding\.item_initialized', body)
    assert flag is not None, 'missing authoritative module binding flag'
    assert re.search(r'br i1 ' + re.escape(flag[1]) + r', label %[^,]+, label %', body), 'binding flag does not guard the read'


PROGRAM = "import gc\n\nevents = []\n\nclass Token:\n    def __init__(self, name):\n        self.name = name\n    def __del__(self):\n        events.append(self.name)\n        gc.collect()\n\ndef read_item():\n    gc.collect()\n    return item\n\nfor item in [Token('first'), Token('last')]:\n    assert read_item() is item\n    gc.collect()\nassert read_item().name == 'last'\nassert events == ['first']\ndel item\ngc.collect()\nassert events == ['first', 'last']\n\nprior = 19\nfor prior in []:\n    raise AssertionError('empty loop ran')\nassert prior == 19\nfor missing in []:\n    pass\ntry:\n    missing\nexcept NameError:\n    pass\nelse:\n    raise AssertionError('empty loop created a binding')\n\nfor number, item in [(7, Token('unpacked'))]:\n    assert number == 7 and read_item() is item\nassert read_item().name == 'unpacked'\ndel item\ngc.collect()\nassert events[-1] == 'unpacked'\n\nfor number in range(3):\n    pass\nassert number == 2\nif True:\n    for item in [{'name': 'nested'}]:\n        assert item.get('name') == 'nested'\nassert read_item().get('name') == 'nested'\ndel item\ntry:\n    read_item()\nexcept NameError:\n    pass\nelse:\n    raise AssertionError('deleted module loop binding remained visible')\nprint('MODULE_LOOP_BINDING_OK')\n"


@pytest.mark.parametrize('setup', (
    "for item in []:\n    pass\n",
    "item = 9\nfor item in []:\n    pass\n",
    "for item in [9]:\n    pass\ndel item\n",
))
def test_discarded_module_loop_name_checks_its_initialized_owner(setup, monkeypatch):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    checks = []
    original = L1CodeGen._emit_module_global_bound_check

    def observe(codegen, name, expr):
        checks.append((name, expr.span.line if expr is not None else None))
        return original(codegen, name, expr)

    monkeypatch.setattr(L1CodeGen, '_emit_module_global_bound_check', observe)
    text = _emit(setup + "item\n")
    assert ('item', setup.count('\n') + 1) in checks
    assert re.search(r'\bcall [^\n]*@py_exc_new\(i64 10,', text)
    assert re.search(r'load i1, ptr @\.modvar\.binding\.item_initialized', text)


def test_local_shadow_does_not_read_module_loop_initialized_flag():
    text = _emit("for item in []:\n    pass\ndef read(item):\n    item\n    return item\n")
    body = re.search(r'^define [^\n]*@user_binding_read\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    assert '@.modvar.binding.item_initialized' not in body
    assert '@.modvar.binding.item' not in body


def test_module_loop_binding_reference_program(capsys):
    exec(compile(PROGRAM, '<module-loop-reference>', 'exec'), {})
    assert capsys.readouterr().out == 'MODULE_LOOP_BINDING_OK\n'


def _blocks(function):
    return dict(re.findall(
        r'^([\w.$]+):\n(.*?)(?=^[\w.$]+:|^})', function, re.M | re.S,
    ))


def _reachable(blocks, start, omitted=()):
    seen = set()
    pending = [start]
    while pending:
        label = pending.pop()
        if label in seen or label in omitted:
            continue
        seen.add(label)
        pending.extend(re.findall(r'label %([\w.$]+)', blocks[label]))
    return seen


@pytest.mark.parametrize('scope', ('module', 'function', 'generator'))
@pytest.mark.parametrize('control', ('pass', 'continue', 'break'))
def test_synthetic_unpack_owner_ends_before_loop_body(scope, control, tmp_path):
    source = "def observe(value):\n    return value\n"
    loop = (
        "for number, item in [(1, {'name': 'first'}), (2, {'name': 'last'})]:\n"
        "    observe(item)\n    " + control + "\n"
    )
    if scope != 'module':
        if scope == 'generator':
            loop += "yield item\n"
        source += "def probe():\n" + ''.join('    ' + line + '\n' for line in loop.splitlines())
    else:
        source += loop
    text = _emit(source)
    (tmp_path / 'program.ll').write_text(text)
    function = next(body for body in re.findall(
        r'^define [^\n]+\n.*?^}', text, re.M | re.S,
    ) if re.search(r'%foritem\.[\w.]+ = alloca ptr', body))
    blocks = _blocks(function)
    done_blocks = [name for name in blocks if name.startswith('for.unpack.done.')]
    assert done_blocks, 'the synthetic tuple owner survives into the loop body'
    done = done_blocks[0]
    observe = next(name for name, body in blocks.items()
                   if re.search(r'\bcall [^\n]*@user_binding_observe\(', body))
    assert observe in _reachable(blocks, next(iter(blocks)))
    assert observe not in _reachable(blocks, next(iter(blocks)), (done,))
    releases = [body for name, body in blocks.items()
                if re.match(r'foritem\.[\w.]+\.owned\.release\.', name)]
    # Function exits may also retain an idempotent, flag-guarded cleanup.
    assert len(releases) >= 2, 'normal and error paths must release the hidden tuple'
    for body in releases:
        assert re.search(r'store i1 0, ptr %foritem\.', body)
        assert re.search(r'@pcc_gc_store_root\(ptr %foritem\.[^,]+, ptr null\)', body)
        assert '@pcc_gc_frame_leave' not in body


def test_unpack_failure_reaches_synthetic_owner_cleanup(tmp_path):
    text = _emit("sink = []\ntry:\n"
                 "    for item, sink[1] in [({'name': 'held'}, 7)]:\n"
                 "        pass\nexcept IndexError:\n    pass\n")
    (tmp_path / 'program.ll').write_text(text)
    function = next(body for body in re.findall(
        r'^define [^\n]+\n.*?^}', text, re.M | re.S,
    ) if re.search(r'%foritem\.[\w.]+ = alloca ptr', body))
    blocks = _blocks(function)
    error = next(name for name in blocks if name.startswith('for.unpack.error.'))
    assert error in _reachable(blocks, next(iter(blocks)))
    release = next(name for name in _reachable(blocks, error)
                   if name.startswith('foritem.') and '.owned.release.' in name)
    assert re.search(r'@pcc_gc_store_root\(ptr %foritem\.[^,]+, ptr null\)', blocks[release])


def test_explicit_user_tuple_target_keeps_its_owner():
    text = _emit("for row in [(1, {'name': 'held'})]:\n    pass\n"
                 "number, item = row\ndel item\nprint(row[0])\n")
    assert 'for.unpack.done.' not in text
    assert 'for.unpack.error.' not in text
    assert '@.modvar.binding.row' in text


def test_unpack_error_roots_and_restores_pending_exception(tmp_path):
    text = _emit("sink = []\ntry:\n"
                 "    for sink[1], item in [(7, {'name': 'held'})]:\n"
                 "        pass\nexcept IndexError:\n    pass\n")
    (tmp_path / 'program.ll').write_text(text)
    function = next(body for body in re.findall(
        r'^define [^\n]+\n.*?^}', text, re.M | re.S,
    ) if re.search(r'%foritem\.[\w.]+ = alloca ptr', body))
    blocks = _blocks(function)
    error = next(name for name in blocks if name.startswith('for.unpack.error.'))
    body = blocks[error]
    assert '@py_current_exception(' in body, 'unpack cleanup does not preserve the selecting exception'
    saved = body.index('@py_current_exception(')
    rooted = body.index('@pcc_gc_store_root(', saved)
    cleared = body.index('@py_clear_exception(', rooted)
    release_guard = body.index('label %foritem.', cleared)
    assert saved < rooted < cleared < release_guard
    reachable = _reachable(blocks, error)
    restored = next(name for name in reachable if 'for.unpack.exception.restore' in blocks[name])
    restore_body = blocks[restored]
    assert (restore_body.index('@py_clear_exception(')
            < restore_body.index('@pcc_gc_load_ptr(')
            < restore_body.index('@py_raise(')
            < restore_body.index('@pcc_gc_store_root('))
    # Every owned release on this edge must rejoin the original-exception
    # restoration, before the enclosing loop/handler receives control.
    release = next(name for name in re.findall(r'label %([\w.$]+)', body)
                   if '.owned.release.' in name)
    assert restored in _reachable(blocks, release)
    assert '@pcc_gc_frame_leave_lifo(' in restore_body


def test_unpack_temporary_state_has_initialized_native_host_layout(tmp_path):
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_ATTRS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.py_ast import Module
    from pcc.frontends.python.type_infer import infer_module

    field = '_for_unpack_temporary_names'
    first = L1CodeGen(Module(name='first', body=[]))
    second = L1CodeGen(Module(name='second', body=[]))
    assert first._for_unpack_temporary_names == set()
    assert first._for_unpack_temporary_names is not second._for_unpack_temporary_names
    assert L1_CODEGEN_HOST_ATTRS.count(field) == 1

    module_name = 'pcc.frontends.python.codegen.layer1'
    source = tmp_path / 'layer1.py'
    source.write_text('class L1CodeGen:\n    pass\n')
    modules, exports, _ = build_closed_world_context([str(source)], [module_name])
    typed = infer_module(modules[0])
    codegen = L1CodeGen(typed, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    (tmp_path / 'layout.ll').write_text(str(codegen.generate(typed)))
    fields = codegen.class_lowering.classes['L1CodeGen'].field_names
    exported = exports[module_name]['L1CodeGen']['field_names']
    assert fields.count(field) == exported.count(field) == 1
    assert fields.index(field) == exported.index(field)


def test_unpack_cleanup_native_export_matches_live_signature(tmp_path):
    from inspect import signature
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    name = '_emit_for_unpack_assign'
    static = {entry['name']: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports('pcc.frontends.python.codegen.layer1')
    methods = exports['pcc.frontends.python.codegen.layer1']['L1CodeGen']['methods']
    native = {entry['name']: entry for entry in methods}
    assert tuple(entry['name'] for entry in L1_CODEGEN_STATIC_METHODS) == L1_CODEGEN_HOST_METHODS
    assert static[name] == native[name]
    parameters = tuple(signature(getattr(L1CodeGen, name)).parameters)
    assert parameters == ('self', 'stmt', 'name')
    assert tuple(entry['name'] for entry in static[name]['call_sig']) == parameters
    assert not any(entry['has_default'] for entry in static[name]['call_sig'])
    source = ('from pcc.frontends.python.codegen.layer1 import L1CodeGen\n'
              'def probe(codegen: L1CodeGen, stmt, name):\n'
              '    codegen._emit_for_unpack_assign(stmt, name)\n')
    module = infer_module(parse_and_lift(source, 'binding.py', 'binding'),
                          external_exports=exports)
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._native_module_exports = exports
    text = str(codegen.generate(module))
    (tmp_path / 'caller.ll').write_text(text)
    symbol = 'user_pcc_frontends_python_codegen_layer1_L1CodeGen_' + name
    call = re.search(r'call [^\n]*@' + symbol + r'\(([^\n]*)\)', text)
    assert call and len(call[1].split(',')) == len(parameters)
    assert not re.search(r'\bcall [^\n]*@py_cpy_', text)


UNPACK_LIFETIME_PROGRAM = '''import gc
events = []
class Token:
    def __init__(self, value):
        self.value = value
    def __del__(self):
        events.append(self.value)
        gc.collect()

def exercise(mode):
    try:
        for number, item in [(1, Token(mode))]:
            if mode == 1:
                continue
            if mode == 2:
                break
            if mode == 3:
                raise ValueError('body')
    except ValueError:
        pass
    del item
    gc.collect()
    assert events[-1] == mode

exercise(0)
exercise(1)
exercise(2)
exercise(3)
for number, item in [(1, Token(4)), (2, Token(5))]:
    gc.collect()
    if number == 1:
        continue
    break
del item
gc.collect()
assert events[-2:] == [4, 5]

sink = []
try:
    for item, sink[1] in [(Token(6), 7)]:
        raise AssertionError('failed unpack ran body')
except IndexError:
    pass
del item
gc.collect()
assert events[-1] == 6

for row in [(7, Token(7))]:
    pass
number, item = row
del item
gc.collect()
assert events[-1] == 6
del row
gc.collect()
assert events[-1] == 7
print('UNPACK_LOOP_LIFETIME_OK')
'''


def test_unpack_loop_lifetime_reference_program(capsys):
    exec(compile(UNPACK_LIFETIME_PROGRAM, '<unpack-loop-reference>', 'exec'), {})
    assert capsys.readouterr().out == 'UNPACK_LOOP_LIFETIME_OK\n'


UNPACK_EXCEPTION_PROGRAM = '''import gc
import weakref
events = []
refs = []
expected = IndexError('original unpack failure')

def callback(reference):
    events.append('weakref')
    try:
        raise ValueError('callback')
    except ValueError:
        gc.collect()

class Token:
    def __del__(self):
        events.append('finalizer')
        try:
            raise ValueError('finalizer')
        except ValueError:
            gc.collect()

class Rows:
    def __iter__(self):
        return self
    def __next__(self):
        token = Token()
        refs.append(weakref.ref(token, callback))
        return (7, token)

class Sink:
    def __setitem__(self, index, value):
        raise expected

sink = Sink()
try:
    for sink[0], item in Rows():
        raise AssertionError('failed unpack ran body')
except IndexError as caught:
    assert caught is expected
else:
    raise AssertionError('unpack exception disappeared')
gc.collect()
assert 'weakref' in events and 'finalizer' in events
assert len(events) == 2
assert refs[0]() is None
print('UNPACK_LOOP_EXCEPTION_OK')
'''


def test_unpack_loop_exception_reference_program(capsys):
    exec(compile(UNPACK_EXCEPTION_PROGRAM, '<unpack-loop-exception-reference>', 'exec'), {})
    assert capsys.readouterr().out == 'UNPACK_LOOP_EXCEPTION_OK\n'


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_unpack_loop_exception_native_five_gc(python_program_compiler, request,
                                             explicit_owned_runtime, tmp_path, capfd):
    from tests.python.owned_regression_support import assert_owned_program
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(UNPACK_EXCEPTION_PROGRAM, 'UNPACK_LOOP_EXCEPTION_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_unpack_loop_lifetime_native_five_gc(python_program_compiler, request,
                                            explicit_owned_runtime, tmp_path, capfd):
    from tests.python.owned_regression_support import assert_owned_program
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(UNPACK_LIFETIME_PROGRAM, 'UNPACK_LOOP_LIFETIME_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_module_loop_binding_native_five_gc(python_program_compiler, request,
                                            explicit_owned_runtime, tmp_path, capfd):
    from tests.python.owned_regression_support import assert_owned_program
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'MODULE_LOOP_BINDING_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
