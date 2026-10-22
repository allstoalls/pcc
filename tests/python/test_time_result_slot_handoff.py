"""Existing clock and strftime producers keep their native output owners."""
import pytest
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize('name', ('time', 'monotonic', 'perf_counter', 'time_ns', 'monotonic_ns', 'perf_counter_ns', 'strftime'))
@pytest.mark.parametrize('alias', (False, True))
def test_time_object_producer_publishes_before_consumer_cleanup(name, alias):
    imported = 'from time import ' + name + ' as clock\n' if alias else 'import time\n'
    call = ('clock' if alias else 'time.' + name) + ("('%Y')" if name == 'strftime' else '()')
    source = imported + 'def consume(*, value, later=None, **extras):\n    return value\n' + 'def probe():\n    return consume(value=' + call + ')\n'
    text = _emit(source)
    _assert_immediate_publication(text, 'py_time_' + name)
    assert 'strict.nolib.stub' not in text
