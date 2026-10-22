"""Text/byte padding preserves dynamically inferred receiver ownership."""
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


@pytest.mark.parametrize("method", ("ljust", "rjust"))
@pytest.mark.parametrize("annotation", ("", ": str", ": bytes", ": bytearray"))
@pytest.mark.parametrize("site", ("return", "argument", "default", "later-error"))
def test_padding_result_and_arguments_have_owning_slots(method, annotation, site):
    expression = "value." + method + "(width(), fill())"
    prefix = ("def width():\n    return 8\n"
              "def fill():\n    return b'_'\n"
              "def later():\n    raise ValueError('later')\n"
              "def take(*, value, later=None, **extras):\n    return value\n")
    if site == "return":
        body = "    return " + expression + "\n"
    elif site == "default":
        body = "    def target(value=" + expression + "):\n        return value\n    return target()\n"
    else:
        body = "    return take(value=" + expression + (", later=later()" if site == "later-error" else "") + ")\n"
    text = _emit(prefix + "def probe(value" + annotation + "):\n" + body)
    families = ("str", "bytes") if not annotation else (("str",) if annotation == ": str" else ("bytes",))
    for family in families:
        _assert_immediate_publication(text, "py_" + family + "_" + method)
    assert "strict.nolib.stub" not in text
    assert "@py_index_i64_checked_slots(" in text
    if not annotation:
        assert "padding.generic" in text
        assert "padding.invoke" in text
        assert "@py_obj_getattr(" in text


def test_archive_padding_retains_dynamic_encode_result():
    # This is the original archive member's loop shape: the iteration loses
    # field's string type, so encode() and ljust() both infer DynType.
    text = _emit('''def probe(name: str, payload: bytes):
    fields = (name, "0", "0", "0", "100644", str(len(payload)))
    widths = (16, 12, 6, 6, 8, 10)
    header = b""
    for field, width in zip(fields, widths):
        value = field.encode("ascii")
        header += value.ljust(width)
    return header
''')
    _assert_immediate_publication(text, "py_bytes_ljust")
    _assert_immediate_publication(text, "py_str_ljust")
    assert "padding.generic" in text


def test_section_padding_retains_dynamic_chained_receiver():
    text = _emit('''def take(*, value, **extras):
    return value
def probe(sections):
    result = []
    for name, payload in sections:
        result.append(take(value=name.encode().ljust(8, b"\\0")))
    return result
''')
    _assert_immediate_publication(text, "py_bytes_ljust")
    _assert_immediate_publication(text, "py_str_ljust")
    assert "padding.generic" in text


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Width:
        def __index__(self):
            events.append('index')
            gc.collect()
            return 6
    class BadWidth:
        def __index__(self):
            events.append('bad-index')
            gc.collect()
            raise ValueError('width')
    class Custom:
        def ljust(self, width, fill):
            events.append('custom')
            gc.collect()
            return (width, fill)
        def rjust(self, width, fill):
            events.append('custom-right')
            gc.collect()
            return (width, fill)
    def width():
        events.append('width')
        return Width()
    def fill(value):
        events.append('fill')
        gc.collect()
        return value
    def left(value, mark):
        return value.ljust(width(), fill(mark))
    def right(value, mark):
        return value.rjust(width(), fill(mark))
    def bad(value):
        return value.ljust(BadWidth(), fill(b'_'))
    def later():
        events.append('later')
        gc.collect()
        return None
    def fail():
        gc.collect()
        raise ValueError('later')
    def take(*, value, other=None, **extras):
        gc.collect()
        return value
    def main():
        assert take(value=left(b'ab', b'_'), other=later()) == b'ab____'
        assert events == ['width', 'fill', 'index', 'later']
        events.clear()
        assert right('hé', '.') == '....hé'
        assert events == ['width', 'fill', 'index']
        events.clear()
        padded = left(bytearray(b'ab'), b'0')
        assert padded == bytearray(b'ab0000')
        assert type(padded) is bytearray
        assert events == ['width', 'fill', 'index']
        events.clear()
        custom = left(Custom(), b'_')
        assert type(custom[0]) is Width
        assert custom[1] == b'_'
        assert events == ['width', 'fill', 'custom']
        events.clear()
        custom_right = right(Custom(), b'_')
        assert type(custom_right[0]) is Width
        assert events == ['width', 'fill', 'custom-right']
        events.clear()
        try:
            bad(b'ab')
        except ValueError as error:
            assert str(error) == 'width'
        else:
            raise AssertionError('width exception lost')
        assert events == ['fill', 'bad-index']
        events.clear()
        try:
            take(value=left(b'ab', b'_'), other=fail())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('later exception lost')
        assert events == ['width', 'fill', 'index']
        header = b''
        for field, size in zip(('abc', '7'), (5, 3)):
            value = field.encode('ascii')
            header += value.ljust(size)
        assert header == b'abc  7  '
        for name in ('.text', '.data'):
            assert len(name.encode().ljust(8, b'\\0')) == 8
        print('PADDING_PRODUCERS_OK')
    main()
''')


def test_padding_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "PADDING_PRODUCERS_OK\n", tmp_path)


def test_padding_program_owned_lowering():
    text = _emit(PROGRAM)
    for family in ("bytes", "str"):
        for method in ("ljust", "rjust"):
            _assert_immediate_publication(text, "py_" + family + "_" + method)


def test_padding_helper_native_export_and_caller_signature():
    from tests.python.test_native_file_re_result_handoff import (
        test_regex_helper_native_exports_and_callers,
    )
    test_regex_helper_native_exports_and_callers(
        "_emit_owned_padding_method", ("self", "expr"), (False, False),
    )


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_padding_program_native(python_program_compiler, request,
                                explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "PADDING_PRODUCERS_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
