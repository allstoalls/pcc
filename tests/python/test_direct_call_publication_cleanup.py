"""Publication failures must unwind the actual direct-call argument owners."""
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize('arguments,owned_count', (
    ('value, value', 0), ('[1], value', 1),
    ('value, [2]', 1), ('[1], [2]', 2),
))
@pytest.mark.parametrize('handled', (False, True))
def test_publication_edges_release_arguments_once(arguments, owned_count, handled, monkeypatch):
    call_user = L1CodeGen._call_user
    publish = L1CodeGen._publish_slot_call_owned
    active = []
    publications = []

    def observe_call(host, function, *args, **kwargs):
        selected = function.name == 'user_binding_callee'
        if selected:
            active.append(tuple(kwargs.get('pinned_arg_temps', ())))
        try:
            return call_user(host, function, *args, **kwargs)
        finally:
            if selected:
                active.pop()

    def observe_publication(host, slot, value, **kwargs):
        if active and kwargs.get('label') == 'user call':
            publications.append((host._current_try_err_block().name,
                                 active[-1], str(value)))
        return publish(host, slot, value, **kwargs)

    monkeypatch.setattr(L1CodeGen, '_call_user', observe_call)
    monkeypatch.setattr(L1CodeGen, '_publish_slot_call_owned', observe_publication)
    source = ('def callee(left, right):\n    return right\n'
              'def take(*, value, **extras):\n    return value\n'
              'def probe(value):\n')
    statement = 'return take(value=callee(' + arguments + '))\n'
    if handled:
        source += '    try:\n        ' + statement + '    except RuntimeError:\n        return None\n'
    else:
        source += '    ' + statement
    body = _function(_emit(source))
    assert len(publications) == 1
    target, arguments, returned = publications[0]
    assert len(arguments) == 2
    assert sum(owned for value, owned in arguments) == owned_count
    cleanup = re.search(r'^' + re.escape(target) + r':\n(.*?)(?=^\S|\Z)', body, re.M | re.S)
    assert cleanup is not None
    assert len(re.findall(r'\bcall [^\n]*@pcc_gc_unpin\(', cleanup[1])) == 2
    assert len(re.findall(r'\bcall [^\n]*@pcc_gc_release\(', cleanup[1])) == owned_count
    assert re.search(r'\bbr label %', cleanup[1])
    # Acquire and release status failures both target that exact cleanup.
    assert len(re.findall(r'label %' + re.escape(target) + r'(?=[,\s])', body)) >= 2
    call = re.search(r'^\s*' + re.escape(returned) + r' = call [^\n]*@user_binding_callee\([^\n]*\)\n', body, re.M)
    assert call is not None
    assert body[call.end():].lstrip().startswith('store ptr ' + returned + ', ptr ')
