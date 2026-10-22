"""Native text and byte producers publish before argument retirement."""
from __future__ import annotations

import textwrap
import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize("expression,runtime", (
    ("re.sub(r'\\W+', '_', text)", "py_re_engine_sub"),
    ("re.sub(r'\\W+', '_', text, width)", "py_re_engine_sub"),
    ("raw.rstrip()", "py_bytes_rstrip"),
    ("raw.rstrip(b'\\0')", "py_bytes_rstrip_chars"),
    ("raw.lstrip(b' ')", "py_bytes_lstrip_chars"),
    ("raw.strip()", "py_bytes_strip"),
    ("raw.ljust(width)", "py_bytes_ljust"),
    ("raw.ljust(width, b'\\0')", "py_bytes_ljust"),
    ("raw.rjust(width, b' ')", "py_bytes_rjust"),
    ("chr(width)", "py_chr_from_i64"),
    ("'{:02}'.format(width)", "py_obj_format"),
    ("'prefix-{0!s}-{0}'.format(text)", "py_str_concat"),
    ("'{value:04}'.format(value=width)", "py_obj_format"),
))
@pytest.mark.parametrize("site", ("return", "argument", "later-error", "default"))
def test_native_text_immediate_publication(expression, runtime, site):
    prefix = ("import re\ndef take(*, value, later=None, **extras):\n    return value\n"
              "def fail():\n    raise ValueError('later')\n")
    if site == "return":
        body = "    return " + expression + "\n"
    elif site == "default":
        body = "    def target(value=" + expression + "):\n        return value\n    return target()\n"
    else:
        body = "    return take(value=" + expression + (", later=fail()" if site == "later-error" else "") + ")\n"
    text = _emit(prefix + "def probe(text: str, raw: bytes, width):\n" + body)
    _assert_immediate_publication(text, runtime)
    assert "strict.nolib.stub" not in text


@pytest.mark.parametrize("source,runtime", (
    ("import re\ndef probe(original):\n    return 'temp.' + re.sub(r'\\W+', '_', original[0])\n", "py_re_engine_sub"),
    ("import re\ndef probe(expr):\n    expr = re.sub(r'/\\*.*?\\*/', '', expr).strip()\n    return re.sub(r'//.*$', '', expr).strip()\n", "py_re_engine_sub"),
    ("def probe(value: bytes, width: int):\n    header = b''\n    header += value.ljust(width)\n    return header\n", "py_bytes_ljust"),
    ("def probe(payload: bytes, length: int):\n    return payload[:length].rstrip(b'\\0').decode('utf-8', 'surrogateescape')\n", "py_bytes_rstrip_chars"),
    ("def probe(data: bytes, body: int, name_len: int):\n    return data[body:body + name_len].rstrip(b'\\0').decode('utf-8', 'surrogateescape')\n", "py_bytes_rstrip_chars"),
    ("import struct\ndef probe(name: str):\n    image = bytearray(40)\n    struct.pack_into('<8sIIIIIIHHI', image, 0, name.encode().ljust(8, b'\\0'), 0, 0, 0, 0, 0, 0, 0, 0, 0)\n    return image\n", "py_bytes_ljust"),
    ("def probe(raw):\n    text = ''\n    for value in raw:\n        text = text + chr(value)\n    return text\n", "py_chr_from_i64"),
    ("def probe(year):\n    result = []\n    push = result.append\n    push('{:02}'.format(year // 100))\n    push('{:04}'.format(year))\n    return result\n", "py_obj_format"),
))
def test_preserved_bootstrap_text_shapes(source, runtime, tmp_path):
    if source.startswith("import struct"):
        from pathlib import Path
        import pcc
        from pcc.frontends.python.codegen.layer1 import L1CodeGen
        from pcc.frontends.python.pipeline_context import build_closed_world_context
        from pcc.frontends.python.type_infer import infer_module

        provider = Path(pcc.__file__).resolve().parent / "stdlib/struct.py"
        entry = tmp_path / "binding.py"
        entry.write_text(source)
        modules, exports, _derived = build_closed_world_context(
            [str(provider), str(entry)], ["struct", "binding"],
        )
        module = infer_module(modules[1], external_exports={"struct": exports["struct"]})
        codegen = L1CodeGen(module, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        text = str(codegen.generate(module))
    else:
        text = _emit(source)
    _assert_immediate_publication(text, runtime)
    assert "strict.nolib.stub" not in text


PROGRAMS = {
    "regex": textwrap.dedent(r'''
        import gc
        import re
        from re import sub
        from re import sub as replace
        events = []
        def source():
            events.append('source')
            gc.collect()
            return 'a-b c'
        def later():
            events.append('later')
            gc.collect()
            return None
        def take(*, value, other=None, **extras):
            gc.collect()
            return value
        def replacement(match):
            events.append('replacement')
            gc.collect()
            return '_'
        def fail():
            gc.collect()
            raise ValueError('later-error')
        def main():
            result = take(value='temp.' + re.sub(r'\W+', '_', source()), other=later())
            assert result == 'temp.a_b_c'
            assert take(value=sub('-', '_', 'c-d')) == 'c_d'
            assert take(value=replace('-', '_', 'e-f')) == 'e_f'
            assert events == ['source', 'later']
            assert re.sub(r'/\*.*?\*/', '', ' a /* c */ ').strip() == 'a'
            assert re.sub(r'//.*$', '', ' b // c').strip() == 'b'
            assert take(value=re.sub('-', replacement, 'a-b-c'), other=later()) == 'a_b_c'
            assert events == ['source', 'later', 'replacement', 'replacement', 'later']
            try:
                take(value=re.sub('-', '_', source()), other=fail())
            except ValueError as error:
                assert str(error) == 'later-error'
            else:
                raise AssertionError('later exception lost')
            assert result == 'temp.a_b_c'
            print('TEXT_PRODUCER_OK')
        main()
    '''),
    "bytes": textwrap.dedent(r'''
        import gc
        events = []
        class Width:
            def __index__(self):
                events.append('index')
                gc.collect()
                return 6
        class BadWidth:
            def __index__(self):
                gc.collect()
                raise ValueError('width-error')
        def width():
            events.append('width')
            return Width()
        def fill():
            events.append('fill')
            gc.collect()
            return b'_'
        def later():
            events.append('later')
            gc.collect()
            return None
        def take(*, value, other=None, **extras):
            gc.collect()
            return value
        def fail():
            gc.collect()
            raise ValueError('later-error')
        def main():
            raw = b'ab\0\0'
            assert take(value=raw[:4].rstrip(b'\0').decode('utf-8', 'surrogateescape'), other=later()) == 'ab'
            value = b'ab'
            result = take(value=value.ljust(width(), fill()), other=later())
            assert result == b'ab____'
            assert events == ['later', 'width', 'fill', 'index', 'later']
            assert value.ljust(4) == b'ab  '
            assert value.rjust(4, b'0') == b'00ab'
            assert b'  ab  '.strip() == b'ab'
            assert b'  ab  '.lstrip(b' ') == b'ab  '
            assert b'  ab  '.rstrip() == b'  ab'
            assert bytearray(b'xxabxx').strip(b'x') == bytearray(b'ab')
            try:
                value.ljust(BadWidth(), b'_')
            except ValueError as error:
                assert str(error) == 'width-error'
            else:
                raise AssertionError('index exception lost')
            try:
                take(value=value.ljust(5), other=fail())
            except ValueError as error:
                assert str(error) == 'later-error'
            else:
                raise AssertionError('later exception lost')
            assert result == b'ab____'
            print('TEXT_PRODUCER_OK')
        main()
    '''),
    "chr": textwrap.dedent('''
        import gc
        events = []
        class Codepoint:
            def __index__(self):
                events.append('index')
                gc.collect()
                return 65
        class BadCodepoint:
            def __index__(self):
                gc.collect()
                raise ValueError('index-error')
        def source():
            events.append('source')
            return Codepoint()
        def later():
            events.append('later')
            gc.collect()
            return None
        def take(*, value, other=None, **extras):
            gc.collect()
            return value
        def fail():
            gc.collect()
            raise ValueError('later-error')
        def main():
            result = take(value='prefix' + chr(source()), other=later())
            assert result == 'prefixA'
            assert events == ['source', 'index', 'later']
            text = ''
            for value in [65, 66, 67]:
                text = text + chr(value)
            assert text == 'ABC'
            assert chr(233) == 'é'
            try:
                chr(BadCodepoint())
            except ValueError as error:
                assert str(error) == 'index-error'
            else:
                raise AssertionError('index exception lost')
            try:
                take(value=chr(66), other=fail())
            except ValueError as error:
                assert str(error) == 'later-error'
            else:
                raise AssertionError('later exception lost')
            assert result == 'prefixA'
            print('TEXT_PRODUCER_OK')
        main()
    '''),
    "format": textwrap.dedent('''
        import gc
        events = []
        class Formatted:
            def __format__(self, spec):
                events.append('format:' + spec)
                gc.collect()
                return 'v' + spec
        class BadFormat:
            def __format__(self, spec):
                gc.collect()
                raise ValueError('format-error')
        def first():
            events.append('first')
            return Formatted()
        def second():
            events.append('second')
            gc.collect()
            return 7
        def later():
            events.append('later')
            gc.collect()
            return None
        def take(*, value, other=None, **extras):
            gc.collect()
            return value
        def fail():
            gc.collect()
            raise ValueError('later-error')
        def main():
            result = take(value='[{0:x}|{0:y}|{1:02}]'.format(first(), second()), other=later())
            assert result == '[vx|vy|07]'
            assert events == ['first', 'second', 'format:x', 'format:y', 'later']
            year = 923
            values = []
            push = values.append
            push('{:02}'.format(year // 100))
            push('{:04}'.format(year))
            assert values == ['09', '0923']
            assert '{value:04}'.format(value=12) == '0012'
            assert '{0!s}:{0!r}'.format('ok') == "ok:'ok'"
            try:
                '{}'.format(BadFormat())
            except ValueError as error:
                assert str(error) == 'format-error'
            else:
                raise AssertionError('format exception lost')
            try:
                take(value='{:02}'.format(5), other=fail())
            except ValueError as error:
                assert str(error) == 'later-error'
            else:
                raise AssertionError('later exception lost')
            assert result == '[vx|vy|07]'
            print('TEXT_PRODUCER_OK')
        main()
    '''),
}


PROGRAMS['format_lifetime'] = textwrap.dedent('''
    import gc
    events = []
    class Rendered(str):
        def __del__(self):
            events.append('drop')
    class Value:
        def __format__(self, spec):
            events.append('format:' + spec)
            gc.collect()
            return Rendered(spec)
    def main():
        text = '{0:a}|{0:b}'.format(Value())
        assert text == 'a|b'
        assert events == ['format:a', 'drop', 'format:b', 'drop']
        events.clear()
        single = '{0:z}'.format(Value())
        assert type(single) is Rendered
        assert str(single) == 'z'
        assert events == ['format:z']
        del single
        gc.collect()
        assert events == ['format:z', 'drop']
        events.clear()
        empty = '{0:}'.format(Value())
        assert type(empty) is str
        assert empty == ''
        assert events == ['format:', 'drop']
        print('TEXT_PRODUCER_OK')
    main()
''')


@pytest.mark.parametrize('shape', tuple(PROGRAMS))
def test_native_text_program_reference(shape, tmp_path):
    assert_reference_program(PROGRAMS[shape], 'TEXT_PRODUCER_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('shape', tuple(PROGRAMS))
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_native_text_five_gc(shape, python_program_compiler, request,
                             explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAMS[shape], 'TEXT_PRODUCER_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)


@pytest.mark.parametrize("shape,runtime", (
    ("regex", "py_re_engine_sub"), ("bytes", "py_bytes_ljust"),
    ("chr", "py_chr_from_i64"), ("format", "py_obj_format"),
    ("format_lifetime", "py_obj_format"),
))
def test_native_text_program_lowering(shape, runtime):
    text = _emit(PROGRAMS[shape])
    _assert_immediate_publication(text, runtime)
    assert "strict.nolib.stub" not in text


def test_regex_sub_helper_native_export_and_caller_signature():
    from tests.python.test_native_file_re_result_handoff import (
        test_regex_helper_native_exports_and_callers,
    )
    test_regex_helper_native_exports_and_callers(
        "_emit_native_re_sub_call", ("self", "args", "kwargs", "expr"),
        (False, False, False, True),
    )


@pytest.mark.parametrize("imports,expression", (
    ("import re", "re.sub(pattern, '_', text)"),
    ("import re as regex", "regex.sub(pattern, '_', text)"),
    ("from re import sub", "sub(pattern, '_', text)"),
    ("from re import sub as replace", "replace(pattern, '_', text)"),
))
def test_regex_sub_import_dispatch_preserves_result_sink(imports, expression):
    text = _emit(imports + "\ndef take(*, value, **extras):\n    return value\n"
                 "def probe(pattern: str, text: str):\n    return take(value=" + expression + ")\n")
    _assert_immediate_publication(text, "py_re_engine_sub")
    assert "strict.nolib.stub" not in text


@pytest.mark.parametrize('names,accepted', (
    ('sub', True), ('sub as replace', True), ('sub, findall', True),
    ('subn', False), ('sub, unknown_regex_symbol', False),
))
def test_regex_sub_import_registration_is_bounded(names, accepted):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    module = infer_module(parse_and_lift('from re import ' + names + '\n', 'imports.py', 'imports'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    statement = module.body[0]
    assert codegen._all_import_from_names_are_native_builtins(statement, 're') is accepted
    assert codegen._register_native_builtin_import_from_aliases(statement, 're') is accepted
    if accepted:
        local = 'replace' if ' as replace' in names else 'sub'
        assert codegen._native_builtin_value_for_name(local) == 're.sub'
    else:
        assert 'sub' not in codegen._native_builtin_value_aliases


@pytest.mark.parametrize('name', ('sub', 'replace'))
def test_shadowed_regex_sub_alias_preserves_ordinary_callable(name):
    from tests.python.test_shared_call_binding import _function

    body = _function(_emit('from re import sub as ' + name + '\n'
                           'def probe(' + name + ', pattern, text):\n'
                           '    return ' + name + '(pattern, "_", text)\n'))
    assert '@py_re_engine_sub(' not in body
    assert '@py_obj_call_slots(' in body
