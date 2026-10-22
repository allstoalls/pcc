"""Native text conversions own outputs before checks or operand disposal."""
from __future__ import annotations
import textwrap
import re
import pytest
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


@pytest.mark.parametrize('builtin', ('repr', 'ascii', 'bin', 'hex', 'oct'))
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error', 'concatenation', 'discard'))
def test_text_conversion_publishes_before_cleanup(builtin, site):
    prefix = ('from pcc.unsafe import null\n'
              'def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    expression = builtin + '(item)'
    if site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'later-error':
        body = '    return take(value=' + expression + ', later=fail())\n'
    elif site == 'concatenation':
        body = '    return "prefix:" + ' + expression + '\n'
    elif site == 'discard':
        body = '    ' + expression + '\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    text = _emit(prefix + 'def probe(item):\n' + body)
    runtime = ('py_builtin_' if builtin in ('bin', 'hex', 'oct') else 'py_obj_') + builtin
    _assert_immediate_publication(text, runtime)


def test_dynamic_alloca_alignment_text_uses_owned_hex_producer():
    # Exact failing shape from emit_dynamic_alloca_call. Keep the big integer
    # arithmetic and nested conversion in place; fixing this consumer would
    # hide the shared producer contract failure.
    text = _emit('''def probe(align: int):
    lines = []
    if align > 16:
        lines.append("  and x10, x10, #" + hex((1 << 64) - align))
    return lines
''')
    _assert_immediate_publication(text, 'py_builtin_hex')


@pytest.mark.parametrize('builtin', ('bin', 'hex', 'oct'))
@pytest.mark.parametrize('binding', ('function', 'parameter', 'local', 'global'))
def test_shadowed_base_conversion_preserves_user_callable(builtin, binding):
    helper = 'def custom(value):\n    return value\n'
    if binding == 'function':
        source = ('def ' + builtin + '(value):\n    return value\n'
                  'def probe(item):\n    return "prefix:" + ' + builtin + '(item)\n')
    elif binding == 'parameter':
        source = ('def probe(' + builtin + ', item):\n'
                  '    return "prefix:" + ' + builtin + '(item)\n')
    elif binding == 'local':
        source = (helper + 'def probe(item):\n    ' + builtin + ' = custom\n'
                  '    return "prefix:" + ' + builtin + '(item)\n')
    else:
        source = (helper + builtin + ' = custom\ndef probe(item):\n'
                  '    return "prefix:" + ' + builtin + '(item)\n')
    text = _function(_emit(source))
    assert not bool(re.search(r'\bcall [^\n]*@py_builtin_' + builtin + r'\(', text))
    assert bool(re.search(r'\bcall [^\n]*@(?:user_binding_' + builtin + r'|py_obj_call_slots)\(', text))


@pytest.mark.parametrize('builtin', ('bin', 'hex', 'oct'))
def test_base_conversion_roots_temporary_argument_before_runtime(builtin):
    text = _function(_emit('def number():\n    return 1 << 100\n'
                 'def take(*, value, **extras):\n    return value\n'
                 'def probe():\n    return take(value=' + builtin + '(number()))\n'))
    _assert_immediate_publication(text, 'user_binding_number')
    _assert_immediate_publication(text, 'py_builtin_' + builtin)


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Token:
        def __repr__(self):
            gc.collect()
            events.append('repr')
            return 'TOKEN'
        def __del__(self):
            events.append('disposed')
    class Bad:
        def __repr__(self):
            gc.collect()
            raise ValueError('representation')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def later():
        gc.collect()
        events.append('later')
        raise ValueError('later')
    def returned(item):
        return repr(item)
    def main():
        assert take(value=repr(Token())) == 'TOKEN'
        gc.collect()
        assert events.count('disposed') == 1
        assert take(value=ascii(Token())) == 'TOKEN'
        gc.collect()
        assert events.count('disposed') == 2
        token = Token()
        assert returned(token) == 'TOKEN'
        def target(value=repr(token)):
            gc.collect()
            return value
        assert target() == 'TOKEN'
        large = 1234567890123456789012345678901234567890
        assert take(value=repr(large)) == '1234567890123456789012345678901234567890'
        try:
            take(value=repr(Token()), later=later())
        except ValueError:
            pass
        else:
            raise AssertionError('missing later exception')
        gc.collect()
        assert events.count('disposed') == 3
        before = events.count('later')
        try:
            take(value=ascii(Bad()), later=later())
        except ValueError:
            pass
        else:
            raise AssertionError('missing representation exception')
        assert events.count('later') == before
        print('TEXT_CONVERSIONS_OK')
    main()
''')


BASE_FORMAT_PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Invalid:
        def __del__(self):
            gc.collect()
            events.append('disposed')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def later():
        events.append('later')
        gc.collect()
        raise ValueError('later')
    def number():
        gc.collect()
        return 1 << 100
    def alignment_text(align: int):
        lines = []
        if align > 16:
            lines.append("  and x10, x10, #" + hex((1 << 64) - align))
        return lines
    def returned(value):
        return hex(value)
    def main():
        assert alignment_text(16) == []
        assert alignment_text(32) == ['  and x10, x10, #0xffffffffffffffe0']
        assert alignment_text(4096) == ['  and x10, x10, #0xfffffffffffff000']
        assert take(value=hex(number())) == '0x10000000000000000000000000'
        assert take(value=bin(number())) == '0b1' + '0' * 100
        assert take(value=oct(number())) == '0o2' + '0' * 33
        assert take(value=hex(-42)) == '-0x2a'
        assert take(value=bin(True)) == '0b1'
        assert take(value=oct(0)) == '0o0'
        assert returned(1 << 100) == '0x10000000000000000000000000'
        def target(value=hex(number())):
            gc.collect()
            return value
        assert target() == '0x10000000000000000000000000'
        try:
            take(value=hex(number()), later=later())
        except ValueError:
            pass
        else:
            raise AssertionError('missing later exception')
        assert events == ['later']
        try:
            take(value=hex(Invalid()), later=later())
        except TypeError:
            pass
        else:
            raise AssertionError('missing conversion exception')
        gc.collect()
        assert events == ['later', 'disposed']
        try:
            oct(1.5)
        except TypeError:
            pass
        else:
            raise AssertionError('missing float conversion exception')
        hex(number())
        bin(number())
        oct(number())
        gc.collect()
        print('BASE_FORMAT_OWNERSHIP_OK')
    main()
''')


SHADOW_FORMAT_PROGRAM = textwrap.dedent('''\
    import gc
    def custom(value):
        gc.collect()
        return 'custom:' + value
    def hex(value):
        return custom(value)
    def parameter(bin, value):
        return bin(value)
    def local(value):
        oct = custom
        return oct(value)
    def main():
        assert hex('function') == 'custom:function'
        assert parameter(custom, 'parameter') == 'custom:parameter'
        assert local('local') == 'custom:local'
        print('BASE_FORMAT_SHADOW_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_text_conversions_native_five_gc(python_program_compiler, request,
                                         explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'TEXT_CONVERSIONS_OK\n', tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path/'compiler-wrapper.stderr').read_text() == ''


@pytest.mark.integration
@pytest.mark.parametrize('program, expected', (
    (BASE_FORMAT_PROGRAM, 'BASE_FORMAT_OWNERSHIP_OK\n'),
    (SHADOW_FORMAT_PROGRAM, 'BASE_FORMAT_SHADOW_OK\n'),
), ids=('base-format-ownership', 'base-format-shadow'))
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_base_format_conversions_native_five_gc(program, expected, python_program_compiler, request,
                                               explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(program, expected, tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path/'compiler-wrapper.stderr').read_text() == ''
