"""Coroutine close owns its receiver and publishes before checking errors."""
import pytest
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize('site', ('assignment', 'return', 'argument', 'default', 'list', 'discarded', 'later-error'))
def test_coroutine_close_owned_result_positions(site):
    prefix = (
        'async def child():\n    return [29]\n'
        'def take(value, later=None):\n    return value\n'
        'def fail():\n    raise ValueError("later")\n'
        'def probe():\n    c = child()\n'
    )
    body = {
        'assignment': '    value = c.close()\n    return value\n',
        'return': '    return c.close()\n',
        'argument': '    return take(c.close())\n',
        'default': '    def target(value=c.close()):\n        return value\n    return target()\n',
        'list': '    return [c.close()]\n',
        'discarded': '    c.close()\n',
        'later-error': '    return take(c.close(), fail())\n',
    }[site]
    text = _emit(prefix + body)
    verify_parsed_module(parse_self_backend_module(text))
    _assert_immediate_publication(text, 'py_coroutine_close')
