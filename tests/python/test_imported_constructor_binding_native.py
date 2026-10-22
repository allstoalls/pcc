"""Execute original imported-constructor call shapes on a supplied runtime."""
import os
import subprocess

import pytest

pytestmark = pytest.mark.integration

PROVIDER_SOURCE = '''class Required:
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
        self.second = second
        self.third = third
'''

ENTRY_SOURCE = '''import binding_provider as provider
def mark(events, name, value):
    events.append(name)
    return value
def mapping(events, name, key, value):
    events.append(name)
    return {key: value}
def main():
    x = provider.Required(1, c=3, b=2)
    assert (x.a, x.b, x.c) == (1, 2, 3)
    y = provider.AllKinds(1, 2, 4, 5, c=3, extra=6)
    assert (y.a, y.b, y.items, y.c, y.extras) == (1, 2, (4, 5), 3, {'extra': 6})
    z = provider.AllKinds(1, a=7)
    assert z.a == 1 and z.b == 2 and z.c == 3 and z.items == () and z.extras == {'a': 7}
    d = provider.AllKinds(1)
    assert d.b == 2 and d.c == 3 and d.items == () and d.extras == {}
    s = provider.AllKinds(*(1, 2, 4), **{'c': 3, 'extra': 6})
    assert s.items == (4,) and s.extras == {'extra': 6}
    events = []
    o = provider.Ordered(first=mark(events, 'first', 1), third=mark(events, 'third', 3), second=mark(events, 'second', 2))
    assert events == ['first', 'third', 'second']
    assert (o.first, o.second, o.third) == (1, 2, 3)
    events = []
    q = provider.AllKinds(mark(events, 'pos', 1), **mapping(events, 'map1', 'c', 3), extra=mark(events, 'extra', 6), **mapping(events, 'map2', 'last', 7))
    assert events == ['pos', 'map1', 'extra', 'map2']
    assert q.c == 3 and q.extras == {'extra': 6, 'last': 7}
    errors = 0
    try:
        provider.Required(1, 2, b=3, c=4)
    except TypeError:
        errors += 1
    try:
        provider.Required(a=1, b=2, c=3)
    except TypeError:
        errors += 1
    try:
        provider.Required(1, c=3)
    except TypeError:
        errors += 1
    try:
        provider.Required(1, 2, **{'c': 3}, **{'c': 4})
    except TypeError:
        errors += 1
    events = []
    try:
        provider.Required(mark(events, 'pos', 1), 2, **mapping(events, 'map', 'c', 3), c=mark(events, 'duplicate', 4))
    except TypeError:
        errors += 1
    assert events == ['pos', 'map', 'duplicate']
    assert errors == 5
    assert provider.Required(1, 2, c=3).c == 3
    print('IMPORTED_CONSTRUCTOR_BINDING_OK')
main()
'''


def test_imported_constructor_binding_native_five_gc(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    provider = tmp_path / 'binding_provider.py'
    entry = tmp_path / 'entry.py'
    binary = tmp_path / 'imported_constructor_binding'
    provider.write_text(PROVIDER_SOURCE)
    entry.write_text(ENTRY_SOURCE)
    compile_python_multi([str(provider), str(entry)], str(binary),
                         module_names=['binding_provider', 'entry'], entry_module='entry',
                         backend='self', libpython_mode='off', ir_scaffold_mode='on',
                         runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PATH='/nonexistent', PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'IMPORTED_CONSTRUCTOR_BINDING_OK\n'
        assert result.stderr == ''
