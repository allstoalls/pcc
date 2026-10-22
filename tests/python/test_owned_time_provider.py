"""Reference controls for the semantic pieces of the owned time provider.

Extern publication/GC is covered separately by test_structseq_slot_owners;
these tests do not substitute CPython records for a native acceptance run.
"""
from __future__ import annotations
import ast
import math
import operator
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAMES = ('tm_year', 'tm_mon', 'tm_mday', 'tm_hour', 'tm_min', 'tm_sec',
         'tm_wday', 'tm_yday', 'tm_isdst', 'tm_zone', 'tm_gmtoff')
NS_CLOCKS = ('time_ns', 'monotonic_ns', 'perf_counter_ns')


def functions(path, names, namespace):
    body = [node for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


@pytest.fixture
def constructor():
    ns = functions(ROOT / 'pcc/stdlib/_structseq.py', ('new',),
                   {'_new': lambda cls, visible, hidden: (visible, hidden),
                    '_storage': lambda value: value, '_dict_keys': dict.keys, '_dict_get_default': dict.get})
    return lambda sequence, mapping: ns['new'](object, sequence, mapping, NAMES, 9, 'time.struct_time')


def outcome(fn):
    try:
        return ('value', fn())
    except Exception as error:
        return ('error', type(error), str(error))


@pytest.mark.parametrize('length', (0, 8, 9, 10, 11, 12))
@pytest.mark.parametrize('mapping', ({}, {'tm_zone': 'X'}, {'tm_gmtoff': 19800},
                                   {'tm_zone': 'X', 'tm_gmtoff': None},
                                   {'tm_year': 2000}, {'extra': 1}, None, []))
def test_structseq_constructor_shape_and_optional_fields_match_reference(constructor, length, mapping):
    def reference():
        value = time.struct_time(tuple(range(length)), mapping)
        return tuple(value), (value.tm_zone, value.tm_gmtoff)
    assert outcome(lambda: constructor(tuple(range(length)), mapping)) == outcome(reference)


def test_structseq_mapping_subclasses_do_not_override_storage_reads(constructor):
    class Mapping(dict):
        def __iter__(self):
            raise AssertionError('iter override')
        def keys(self):
            raise AssertionError('keys override')
        def get(self, *args):
            raise AssertionError('get override')
    mapping = Mapping(tm_zone='X', tm_gmtoff=3600)
    value = time.struct_time(range(9), mapping)
    assert constructor(range(9), mapping) == (tuple(value), (value.tm_zone, value.tm_gmtoff))


def test_structseq_iterator_error_order_matches_reference(constructor):
    class NonIterable:
        def __iter__(self):
            raise TypeError('body TypeError')
    class BrokenIterator:
        def __iter__(self):
            return self
        def __next__(self):
            raise RuntimeError('next body')
    for sequence in (1, NonIterable(), BrokenIterator()):
        assert outcome(lambda: constructor(sequence, [])) == outcome(lambda: time.struct_time(sequence, []))


@pytest.fixture
def timestamp():
    ns = functions(ROOT / 'pcc/stdlib/time.py', ('_timestamp_seconds',),
                   {'time': time.time, '_index_value': operator.index,
                    '_float_value': float.__float__})
    return ns['_timestamp_seconds']


@pytest.mark.parametrize('value', (False, True, 0, -1, -0.25, -1.25, 0.25, 1.25,
                                  2**53+1, float('nan'), float('inf'), -float('inf'),
                                  2**63, -2**63-1, 10**100, '1', b'1', object()))
def test_timestamp_argument_and_floor_semantics_match_reference(timestamp, value):
    def expected():
        record = time.gmtime(value)
        # Huge representable time_t values can exceed struct-tm calendar range;
        # these are a later platform error, outside this conversion stage.
        return record
    actual = outcome(lambda: timestamp(value))
    reference = outcome(expected)
    if reference[0] == 'error' and reference[1] is not OSError:
        assert actual == reference
    else:
        assert actual == ('value', math.floor(value))


def test_timestamp_uses_builtin_payloads_and_custom_numeric_protocols(timestamp):
    class Integer(int):
        def __int__(self):
            return 0
        def __index__(self):
            return 1
        def __lt__(self, value):
            raise AssertionError('int comparison override')
    class Float(float):
        def __float__(self):
            return 0.0
        def __lt__(self, value):
            raise AssertionError('float comparison override')
    class Index:
        def __index__(self):
            return -1
    class Real:
        def __float__(self):
            return -1.25
    for value, expected in ((Integer(42),42), (Float(42.25),42), (Index(),-1), (Real(),-2)):
        assert timestamp(value) == expected
        assert time.gmtime(value) == time.gmtime(expected)


@pytest.mark.parametrize('returned', (42, 2**53+1, 2**63-1))
def test_timestamp_index_protocol_precedes_float_without_losing_precision(timestamp, returned):
    class Both:
        def __index__(self):
            return returned
        def __float__(self):
            raise AssertionError('index protocol must take precedence')
    value = Both()
    assert timestamp(value) == returned
    try:
        expected = time.gmtime(returned)
    except OSError as reference:
        with pytest.raises(OSError) as actual:
            time.gmtime(value)
        assert actual.value.errno == reference.errno
    else:
        assert time.gmtime(value) == expected


def test_timestamp_float_subclass_can_supply_index_protocol(timestamp):
    class IndexedFloat(float):
        def __index__(self):
            return 18
    value = IndexedFloat(42.0)
    assert timestamp(value) == 18
    assert time.gmtime(value) == time.gmtime(18)


@pytest.mark.parametrize('platform', ('darwin', 'linux', 'win32'))
@pytest.mark.parametrize('seconds', (-1, 0, 951782400))
def test_owned_gmtime_keeps_exact_calendar_utc_arguments(platform, seconds):
    buffer = object()
    record = object()
    calls = []

    def allocate(size):
        assert size == 64
        return buffer

    def breakdown(value, offset, daylight, zone, output):
        calls.append((value, offset, daylight, zone, output))
        return 0

    def from_tm(output):
        assert output is buffer
        return record

    ns = functions(ROOT / 'pcc/stdlib/time.py', ('gmtime',), {
        '_timestamp_seconds': lambda value: value,
        'stack_alloc': allocate,
        '_breakdown': breakdown,
        '_record_from_tm': from_tm,
        '_native_sys': SimpleNamespace(platform=platform),
    })
    assert ns['gmtime'](seconds) is record
    # The cross-platform acceptance oracle allows libc's UTC spelling too;
    # it must not weaken the owned provider's explicit GMT/zero-offset ABI.
    assert calls == [(seconds, 0, 0, 'GMT', buffer)]


@pytest.mark.parametrize('name', ('time', 'monotonic', 'perf_counter'))
def test_owned_clock_wrapper_uses_abi_result_and_propagates_error(name):
    calls = []
    def clock():
        calls.append(name)
        return 12.5
    ns = functions(ROOT / 'pcc/stdlib/time.py', (name,),
                   {'_time_' + name: clock,
                    '_native_sys': SimpleNamespace(implementation=SimpleNamespace(name='pcc'))})
    assert ns[name]() == 12.5
    assert calls == [name]
    error = ValueError('clock ABI error')
    def fail():
        raise error
    ns['_time_' + name] = fail
    with pytest.raises(ValueError) as caught:
        ns[name]()
    assert caught.value is error


@pytest.mark.parametrize('name', NS_CLOCKS)
@pytest.mark.parametrize('value', (0, 1, 2**53 + 1, 2**63 - 1))
@pytest.mark.parametrize('implementation', ('pcc', 'cpython'))
def test_owned_nanosecond_clocks_preserve_integer_result_and_error(name, value, implementation):
    calls = []
    def clock():
        calls.append(name)
        return value
    def wrong_route():
        raise AssertionError('clock used the wrong execution owner')
    owned = implementation == 'pcc'
    native_time = SimpleNamespace(**{name: wrong_route if owned else clock})
    ns = functions(ROOT / 'pcc/stdlib/time.py', (name,), {
        '_time_' + name: clock if owned else wrong_route,
        '_native_time': native_time,
        '_native_sys': SimpleNamespace(implementation=SimpleNamespace(name=implementation)),
    })
    assert name in ns, 'owned time provider is missing ' + name
    result = ns[name]()
    assert type(result) is int and result == value
    assert calls == [name]
    error = RuntimeError('nanosecond clock ABI error')
    def fail():
        raise error
    if owned:
        ns['_time_' + name] = fail
    else:
        setattr(native_time, name, fail)
    with pytest.raises(RuntimeError) as caught:
        ns[name]()
    assert caught.value is error


def test_owned_format_and_sleep_wrappers_preserve_abi_protocol():
    calls = []
    def formatter(fmt):
        calls.append(('format', fmt))
        return 'owned-clock'
    def sleeper(seconds):
        calls.append(('sleep', seconds))
        if seconds < 0:
            raise ValueError('sleep length must be non-negative')
        return None
    ns = functions(ROOT / 'pcc/stdlib/time.py', ('strftime', 'sleep'),
                   {'_time_strftime': formatter, '_time_sleep': sleeper,
                    '_native_sys': SimpleNamespace(implementation=SimpleNamespace(name='pcc'))})
    assert ns['strftime']('owned-clock') == 'owned-clock'
    assert ns['sleep'](0) is None
    with pytest.raises(ValueError, match='^sleep length must be non-negative$'):
        ns['sleep'](-1)
    with pytest.raises(NotImplementedError, match='explicit-tuple formatting'):
        ns['strftime']('%Y', tuple(range(9)))
    assert calls == [('format', 'owned-clock'), ('sleep', 0), ('sleep', -1)]


@pytest.mark.parametrize('target', ('x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu'))
def test_owned_time_provider_context_emits_owned_calls_without_recursion(tmp_path, target):
    import re
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    # Match the real sibling-import publication that makes time's own alias
    # live; standalone lowering can shortcut the original bug to the ABI.
    names = ['time', 'sys', 'pcc.stdlib._structseq']
    paths = [str(ROOT / 'pcc/stdlib' / name)
             for name in ('time.py', 'sys.py', '_structseq.py')]
    modules, exports, derived = build_closed_world_context(paths, names)
    assert all(exports['time'][name]['kind'] == 'function' for name in NS_CLOCKS)
    external = {name: value for name, value in exports.items() if name != 'time'}
    module = infer_module(modules[0], external_exports=external, derived_class_map=derived)
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._target_triple = target
    generator._strict_no_libpython = True
    generator._prefer_native_callable_values = True
    generator._skip_program_main = True
    generator._sibling_module_inits = names
    generator._native_module_exports = external
    text = str(generator.generate())
    (tmp_path / 'time.ll').write_text(text)
    from tests.owned_ir_validation import verify_ir_text
    verify_ir_text(text)
    assert '@py_compiled_module_import_by_name(' in text
    assert not re.search(r'call[^\n]*@py_cpy_', text)
    for name in ('time', 'monotonic', 'perf_counter', 'strftime', 'sleep') + NS_CLOCKS:
        body = re.search(r'^define [^\n]*@user_time_' + name + r'\([^\n]*\n.*?^}',
                         text, re.M | re.S).group()
        assert re.search(r'call ptr[^\n]*@py_time_' + name + r'\(', body)
        assert not re.search(r'call[^\n]*@py_obj_call_slots\(', body)
        assert not re.search(r'call[^\n]*@py_module_attr_get\(', body)
        if name in ('time', 'monotonic', 'perf_counter'):
            finish = re.search(r'^err.finish:\n(.*?)(?=\n\n)', body, re.M | re.S).group(1)
            assert 'ret double 0x0000000000000000' in finish
            assert 'unreachable' not in finish


@pytest.mark.parametrize('target', ('x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu'))
def test_owned_nanosecond_consumers_use_real_compiled_time_bindings(tmp_path, target):
    import re
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    consumer = tmp_path / 'ns_consumer.py'
    consumer.write_text(
        'import time\n'
        'from time import time_ns as wall, monotonic_ns as mono, perf_counter_ns as perf\n'
        'def direct():\n'
        '    return (time.time_ns(), time.monotonic_ns(), time.perf_counter_ns())\n'
        'def imported():\n'
        '    return (wall(), mono(), perf())\n'
        'def callbacks():\n'
        '    return (time.time_ns, time.monotonic_ns, time.perf_counter_ns, wall, mono, perf)\n'
    )
    names = ['ns_consumer', 'time', 'sys', 'pcc.stdlib._structseq']
    paths = [str(consumer)] + [str(ROOT / 'pcc/stdlib' / name)
                              for name in ('time.py', 'sys.py', '_structseq.py')]
    modules, exports, derived = build_closed_world_context(paths, names)
    assert all(exports['time'][name]['kind'] == 'function' for name in NS_CLOCKS)
    external = {name: value for name, value in exports.items() if name != 'ns_consumer'}
    module = infer_module(modules[0], external_exports=external, derived_class_map=derived)
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._target_triple = target
    generator._strict_no_libpython = True
    generator._prefer_native_callable_values = True
    generator._skip_program_main = True
    generator._sibling_module_inits = names
    generator._native_module_exports = external
    text = str(generator.generate())
    (tmp_path / 'ns_consumer.ll').write_text(text)
    verify_ir_text(text)
    assert '@py_compiled_module_import_by_name(' in text
    assert not re.search(r'call[^\n]*@py_cpy_', text)
    for name in ('direct', 'imported', 'callbacks'):
        body = re.search(r'^define [^\n]*@user_ns_consumer_' + name + r'\([^\n]*\n.*?^}',
                         text, re.M | re.S).group()
        assert re.search(r'call[^\n]*@py_(?:obj_getattr|module_attr_get)\(', body)
        if name != 'callbacks':
            assert len(re.findall(r'call[^\n]*@py_obj_call_slots\(', body)) == 3
        # A standalone builtin shortcut would miss the provider's namespace.
        assert not re.search(r'call[^\n]*@py_time_\w+_ns\(', body)


@pytest.mark.integration
def test_native_owned_time_provider_callbacks(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    import os
    import subprocess

    source = ROOT / 'tests/fixtures/owned_time_provider_callbacks.py'
    output = tmp_path / 'owned_time_provider_callbacks'
    python_program_compiler(str(source), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'LOOP_CLOCK_ENTER\nOWNED_TIME_OK\n'


def test_owned_time_provider_preserves_plain_cpython_calls():
    from pcc.stdlib import time as owned_time

    assert owned_time.time() > 1_000_000_000.0
    for clock in (owned_time.monotonic, owned_time.perf_counter):
        first = clock()
        assert isinstance(first, float)
        assert clock() >= first
    assert type(owned_time.time_ns()) is int and owned_time.time_ns() > 0
    for clock in (owned_time.monotonic_ns, owned_time.perf_counter_ns):
        first = clock()
        assert type(first) is int and first > 0
        assert clock() >= first
    assert owned_time.strftime('owned-clock') == time.strftime('owned-clock')
    assert owned_time.sleep(0) is None
    with pytest.raises(ValueError, match='^sleep length must be non-negative$'):
        owned_time.sleep(-1)
    with pytest.raises(NotImplementedError, match='explicit-tuple formatting'):
        owned_time.strftime('%Y', tuple(range(9)))
