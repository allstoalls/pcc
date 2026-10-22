"""Weakref constructors retire operand owners after rooted publication.

IR checks are not native qualification. The unchanged constructor lifetime
fixtures remain the primary integration regressions for captured referents.
"""
from __future__ import annotations

import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import (
    _assert_immediate_publication,
)


@pytest.mark.parametrize("expression", (
    "weakref.ref(owner)",
    "weakref.ref(items[0])",
    "weakref.ref(make())",
    "weakref.ref(object())",
    "weakref.ref(owner, callback)",
    "weakref.ref(owner, local_callback)",
    "weakref.ref(owner, lambda reference: None)",
    "weakref.ref(owner, callback_factory())",
    "weakref.proxy(owner)",
    "weakref.proxy(items[0])",
))
@pytest.mark.parametrize("site", ("return", "argument", "later-error"))
def test_weakref_result_is_published_before_operand_cleanup(expression, site):
    prelude = (
        "import weakref\n"
        "class Witness:\n    pass\n"
        "def make():\n    return Witness()\n"
        "def callback(reference):\n    return None\n"
        "def callback_factory():\n    return callback\n"
        "def fail():\n    raise ValueError('later')\n"
        "def take(*, value, later=None, **extras):\n    return value\n"
    )
    if site == "return":
        body = "    return " + expression + "\n"
    else:
        tail = ", later=fail()" if site == "later-error" else ""
        body = "    return take(value=" + expression + tail + ")\n"
    text = _emit(prelude + "def probe(owner, items, local_callback):\n" + body)
    _assert_immediate_publication(text, "py_weakref_new")
    assert ".target.operand" in text
    if ", " in expression:
        assert ".callback.operand" in text
    assert "strict.nolib.stub" not in text


def test_captured_referent_stays_owned_in_constructor_operand_root():
    text = _emit(
        "import weakref\n"
        "class Witness:\n    pass\n"
        "def probe():\n"
        "    owner = Witness()\n"
        "    def keep():\n        return owner\n"
        "    return keep, weakref.ref(owner)\n"
    )
    _assert_immediate_publication(text, "py_weakref_new")
    _assert_immediate_publication(text, "py_list_getitem")
    assert "weakref.ref.target.operand" in text


PROGRAM = textwrap.dedent('''\
    import gc
    import weakref
    events = []
    class Witness:
        def __init__(self, label):
            self.label = label
        def __del__(self):
            events.append(self.label)
    def callback(reference):
        gc.collect()
        assert reference() is None
        events.append('callback')
    def later_failure():
        gc.collect()
        raise ValueError('callback expression')
    def captured():
        owner = Witness('captured')
        reference = weakref.ref(owner)
        def keep():
            return owner
        return keep, reference
    def main():
        keep, reference = captured()
        assert keep() is reference()
        del keep
        gc.collect()
        gc.collect()
        assert reference() is None
        assert events == ['captured']
        temporary = weakref.ref(Witness('temporary'), callback)
        gc.collect()
        gc.collect()
        assert temporary() is None
        assert events == ['captured', 'temporary', 'callback']
        try:
            weakref.ref(Witness('failed'), later_failure())
        except ValueError as error:
            assert str(error) == 'callback expression'
        else:
            raise AssertionError('callback expression error lost')
        gc.collect()
        gc.collect()
        assert events == ['captured', 'temporary', 'callback', 'failed']
        local = Witness('borrowed')
        borrowed = weakref.ref(local, lambda reference: events.append('lambda'))
        assert borrowed() is local
        del local
        gc.collect()
        gc.collect()
        assert borrowed() is None
        assert events == ['captured', 'temporary', 'callback', 'failed', 'borrowed', 'lambda']
        try:
            weakref.ref(object())
        except TypeError:
            pass
        else:
            raise AssertionError('invalid target exception lost')
        print('WEAKREF_CONSTRUCTOR_OWNERS_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_weakref_constructor_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, "WEAKREF_CONSTRUCTOR_OWNERS_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
