"""Owned dynamic handled-exception provider and its raw-ABI root protocol."""
from __future__ import annotations

import io
from pathlib import Path
import traceback as host_traceback

import pytest

from test_foreign_address_leases import _functions
from test_traceback_class_roots import Model
from pcc.stdlib import traceback as provider

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'pcc/runtime/py/py_exc_traceback.py'


def test_interpreted_provider_delegates_to_cpython_with_options():
    try:
        raise ValueError('host-provider')
    except ValueError:
        assert provider.format_exc() == host_traceback.format_exc()
        assert provider.format_exc(limit=0, chain=False) == host_traceback.format_exc(limit=0, chain=False)
        actual, expected = io.StringIO(), io.StringIO()
        provider.print_exc(limit=0, file=actual, chain=False)
        host_traceback.print_exc(limit=0, file=expected, chain=False)
        assert actual.getvalue() == expected.getvalue()
    assert provider.format_exc() == 'NoneType: None\n'


class CurrentModel(Model):
    def __init__(self, *, handled=True, fail_lease=False, fail_format=False):
        super().__init__()
        self.frames = []
        self.pinned = set()
        self.received = []
        self.printed = []
        self.fail_lease = fail_lease
        self.fail_format = fail_format
        self.source = self.root(self.object('exception', immortal=False)) if handled else 0
        self.ns.update(
            stack_alloc=lambda size: self.alloc(), memset=self.zero,
            py_handled_exception_slot=lambda: self.source,
            pcc_gc_frame_enter=self.enter, pcc_gc_frame_leave=self.leave,
            pcc_gc_root_copy_lease=self.copy_lease,
            py_exc_traceback_format_exc_abi=self.format_current,
            py_exc_traceback_print_exc_abi=self.print_current,
            pcc_gc_pin=self.pin, pcc_gc_take_pinned_slot=self.take,
            is_tagged_int=lambda value: False,
            global_load_ptr=lambda name: self.none,
            py_cleanup_one_root_preserving_exception=self.clear_owned,
        )
        self.none = self.object('None')
        self.none_root = self.root(self.none)
        _functions(SOURCE, {'_tb_current_call', '_tb_current_finish'}, self.ns)

    def zero(self, base, value, size):
        assert value == 0
        for offset in range(0, size, 8):
            self.memory[base + offset] = 0

    def enter(self, frame_map, slots):
        assert frame_map == 'pcc_traceback_current_map'
        assert self.load(slots) == self.load(slots + 8) == 0
        self.roots.update({slots, slots + 8})
        self.frames.append(slots)
        self.move()

    def leave(self, slots):
        assert self.frames.pop() == slots
        self.roots.remove(slots)
        self.roots.remove(slots + 8)
        self.move()

    def move(self):
        # Reuse the hostile moving collector; its lease table also models
        # the bounded legacy pin used only for final frame-leave/return.
        super().move()
        if hasattr(self, 'none_root'):
            self.none = self.load(self.none_root)

    def copy_lease(self, destination, source):
        self.move()
        if self.fail_lease:
            return -1
        assert source in self.roots and destination in self.roots
        assert not self.load(destination)
        self.root_store(destination, self.load(source))
        return self.acquire(destination)

    def release(self, slot, token):
        if token == 0:
            assert not self.leases[slot]
            return
        super().release(slot, token)

    def clear_owned(self, slot):
        self.move()
        self.check(self.load(slot))
        self.memory[slot] = 0

    def format_current(self, exc):
        self.move()
        self.check(exc)
        self.received.append(self.objects.get(exc))
        if exc:
            assert exc in {self.load(s) for s, count in self.leases.items() if count}
        if self.fail_format:
            self.error = 'formatter-error'
            return 0
        return self.object('formatted' if exc else 'no-exception', immortal=False)

    def print_current(self, exc):
        self.move()
        self.check(exc)
        self.printed.append(self.objects.get(exc))
        if exc:
            assert exc in {self.load(s) for s, count in self.leases.items() if count}

    def pin(self, value):
        self.check(value)
        slot = next(root for root in self.roots if self.load(root) == value)
        self.leases[slot] += 1
        self.pinned.add(slot)

    def take(self, result, prior):
        value = self.load(result)
        self.check(value)
        matching = [slot for slot in self.pinned if self.load(slot) == value]
        assert matching or not value
        if value:
            pinned_slot = matching[0]
            self.leases[pinned_slot] -= 1
            self.pinned.remove(pinned_slot)
        self.memory[result] = 0
        return value


@pytest.mark.parametrize('handled', [False, True])
@pytest.mark.parametrize('printing', [False, True])
def test_current_adapter_keeps_dynamic_owner_and_result_through_moves(handled, printing):
    m = CurrentModel(handled=handled)
    initial_roots = set(m.roots)
    result = m.ns['_tb_current_call'](int(printing))
    assert m.objects[result][0] == ('None' if printing else 'formatted' if handled else 'no-exception')
    assert set(m.roots) == initial_roots and not m.frames
    assert not any(m.leases.values()) and not m.pinned
    assert m.error is None
    assert len(m.printed if printing else m.received) == 1


@pytest.mark.parametrize('failure', ['fail_lease', 'fail_format'])
def test_current_adapter_error_preserves_owner_and_balances_frames(failure):
    m = CurrentModel(**{failure: True})
    initial_roots = set(m.roots)
    assert m.ns['_tb_current_call'](0) == 0
    assert set(m.roots) == initial_roots and not m.frames
    assert not any(m.leases.values()) and not m.pinned
    assert m.objects[m.load(m.source)][0] == 'exception'
    assert m.error


def test_provider_routes_through_handled_slot_without_raw_pointer_escape(tmp_path, monkeypatch):
    import re
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module

    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    output = tmp_path / 'traceback.ll'
    _compile_runtime_module('py_exc_traceback', str(SOURCE), str(output), 'x86_64-unknown-linux-gnu')
    text = output.read_text()
    body = re.search(r'^define[^\n]*@user_py_exc_traceback__tb_current_call\([^\n]*\)[^\n]*\{\n(.*?)^}', text, re.M | re.S).group(1)
    assert body.index('@pcc_gc_frame_enter(') < body.index('@py_handled_exception_slot(')
    assert '@pcc_gc_root_copy_lease(' in body
    assert '@py_current_exception(' not in body
    assert '@pcc_thread_safepoint(' not in body
    assert 'call ptr () @py_exc_traceback_format_current(' not in body
    assert re.search(r'(%[\w.]+) = call ptr[^\n]*@py_exc_traceback_format_exc\([^\n]*\)\n\s+store ptr \1', body)
    provider_text = (ROOT / 'pcc/stdlib/traceback.py').read_text()
    assert 'c_ptr' not in provider_text and 'load_ptr' not in provider_text


def test_native_provider_defaults_and_unsupported_options_are_explicit(monkeypatch):
    from types import SimpleNamespace

    events = []
    monkeypatch.setattr(provider, '_native_sys', SimpleNamespace(implementation=SimpleNamespace(name='pcc')))
    monkeypatch.setattr(provider, '_format_current', lambda: 'owned-text')
    monkeypatch.setattr(provider, '_print_current', lambda: events.append('printed'))
    assert provider.format_exc(limit=None, chain=True) == 'owned-text'
    assert provider.print_exc(limit=None, file=None, chain=True) is None
    assert events == ['printed']
    for options in ({'limit': 0}, {'chain': False}):
        with pytest.raises(NotImplementedError, match='not implemented'):
            provider.format_exc(**options)
    for options in ({'limit': 0}, {'file': io.StringIO()}, {'chain': False}):
        with pytest.raises(NotImplementedError, match='not implemented'):
            provider.print_exc(**options)
    with pytest.raises(TypeError):
        provider.format_exc(unexpected=True)


PROVIDER_PROGRAM = '''import traceback
import traceback as tb
from traceback import format_exc as report

def module_helper():
    return traceback.format_exc()

def imported_helper():
    return report()

def alias_helper():
    return tb.format_exc()

def local_helper():
    from traceback import format_exc as local_report
    return local_report()

def exercise():
    assert module_helper() == "NoneType: None\\n"
    assert imported_helper() == "NoneType: None\\n"
    try:
        raise ValueError("outer-dynamic-handler")
    except ValueError:
        assert "ValueError: outer-dynamic-handler" in module_helper()
        assert "ValueError: outer-dynamic-handler" in imported_helper()
        assert "ValueError: outer-dynamic-handler" in alias_helper()
        assert "ValueError: outer-dynamic-handler" in local_helper()
        try:
            raise TypeError("inner-dynamic-handler")
        except TypeError:
            assert "TypeError: inner-dynamic-handler" in module_helper()
            assert "TypeError: inner-dynamic-handler" in imported_helper()
        assert "ValueError: outer-dynamic-handler" in module_helper()
        assert "TypeError: inner-dynamic-handler" not in imported_helper()
    assert module_helper() == "NoneType: None\\n"
    assert traceback.format_exc(limit=None, chain=True) == "NoneType: None\\n"

exercise()
class Replacement:
    def format_exc(self):
        return "replacement-module"

def replacement():
    return "replacement-function"

traceback = Replacement()
report = replacement
assert module_helper() == "replacement-module"
assert imported_helper() == "replacement-function"
print("TRACEBACK_DYNAMIC_PROVIDER_OK")
'''


def test_managed_provider_calls_do_not_use_lexical_exception_intrinsic(tmp_path, monkeypatch):
    import re
    from pcc.frontends.python.pipeline import compile_python_multi

    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS', '1')
    source = tmp_path / 'route.py'
    source.write_text(PROVIDER_PROGRAM)
    output = tmp_path / 'route.ll'
    compile_python_multi([str(source)], str(output), emit_llvm_only=True,
                         entry_module='route', module_names=['route'], recursive_stdlib=True,
                         backend='self', libpython_mode='off', ir_scaffold_mode='on')
    text = output.read_text()
    for function, runtime in [('format_exc', 'py_exc_traceback_format_current'),
                              ('print_exc', 'py_exc_traceback_print_current')]:
        body = re.search(r'^define[^\n]*@user_traceback_' + function + r'\([^\n]*\)[^\n]*\{\n(.*?)^}', text, re.M | re.S)
        assert body
        assert '@' + runtime + '(' in body.group(1)
        assert 'strict.nolib' not in body.group(1)
    for helper in ['module_helper', 'imported_helper', 'alias_helper', 'local_helper']:
        body = re.search(r'^define[^\n]*@user_route_' + helper + r'\([^\n]*\)[^\n]*\{\n(.*?)^}', text, re.M | re.S)
        assert body
        assert '@py_exc_traceback_format_exc(' not in body.group(1)
        # Ordinary error landings legitimately read pending TLS. The old
        # lexical formatter fallback is specifically named traceback.cur_exc.
        assert 'traceback.cur_exc' not in body.group(1)
    assert 'format_exc awaits frame-table marshalling' not in text


@pytest.mark.integration
def test_dynamic_provider_and_before_after_rebinding_native(tmp_path, pcc_runtime_archive):
    import subprocess
    import sys
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / 'provider.py'
    source.write_text(PROVIDER_PROGRAM)
    executable = tmp_path / 'provider.out'
    compile_python(str(source), str(executable), backend='self', libpython_mode='off',
                   ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    actual = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=30)
    assert actual.returncode == expected.returncode == 0, actual.stderr
    assert actual.stdout == expected.stdout == 'TRACEBACK_DYNAMIC_PROVIDER_OK\n'
    assert actual.stderr == expected.stderr == ''


@pytest.mark.integration
def test_native_provider_print_defaults_and_fail_closed_options(tmp_path, pcc_runtime_archive):
    import subprocess
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / 'options.py'
    source.write_text('''import traceback

def main():
    try:
        raise ValueError("print-provider")
    except ValueError:
        assert traceback.print_exc(limit=None, file=None, chain=True) is None
    failures = 0
    try:
        traceback.format_exc(limit=0)
    except NotImplementedError:
        failures += 1
    try:
        traceback.format_exc(chain=False)
    except NotImplementedError:
        failures += 1
    try:
        traceback.print_exc(limit=0)
    except NotImplementedError:
        failures += 1
    try:
        traceback.print_exc(file=42)
    except NotImplementedError:
        failures += 1
    try:
        traceback.print_exc(chain=False)
    except NotImplementedError:
        failures += 1
    try:
        traceback.format_exception(None, None, None)
    except NotImplementedError:
        failures += 1
    assert failures == 6
    print("TRACEBACK_OPTIONS_OK")
main()
''')
    executable = tmp_path / 'options.out'
    compile_python(str(source), str(executable), backend='self', libpython_mode='off',
                   ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    actual = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    assert actual.returncode == 0, actual.stderr
    assert actual.stdout == 'TRACEBACK_OPTIONS_OK\n'
    assert actual.stderr.startswith('Traceback (most recent call last):\n')
    assert actual.stderr.endswith('ValueError: print-provider\n')
    assert actual.stderr.count('Traceback (most recent call last):') == 1


@pytest.mark.integration
def test_local_traceback_provider_keeps_ordinary_module_semantics(tmp_path, pcc_runtime_archive):
    import subprocess
    import sys
    from pcc.frontends.python.pipeline import compile_python

    (tmp_path / 'traceback.py').write_text('def format_exc():\n    return "local-module"\n')
    source = tmp_path / 'entry.py'
    source.write_text('import traceback\ndef main():\n    print(traceback.format_exc())\nmain()\n')
    executable = tmp_path / 'entry.out'
    compile_python(str(source), str(executable), backend='self', libpython_mode='off',
                   ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    actual = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=30)
    assert actual.returncode == expected.returncode == 0, actual.stderr
    assert actual.stdout == expected.stdout == 'local-module\n'
    assert actual.stderr == expected.stderr == ''
