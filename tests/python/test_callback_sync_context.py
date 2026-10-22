"""Explicit sync callback contexts; no test may build an implicit runtime."""
import os
import re
from pathlib import Path
import subprocess
import pytest
from pcc.frontends.python.pipeline import compile_python

PROGRAMS = {
    'ordinary': ('''import gc

def ordinary(value):
    gc.collect()
    return value

def run():
    value = [40, 2]
    result = [ordinary][0](value)
    assert result is value and result[0] + result[1] == 42
    print("ordinary-ok")
run()
''', 'ordinary-ok\n'),
    'thread_wait': ('''import gc
import threading
ready = threading.Event()
gate = threading.Event()
progress = [0]

def waiting():
    ready.set()
    gate.wait()
    gc.collect()
    progress[0] = 1

def signal():
    ready.wait()
    gate.set()

def run():
    one = threading.Thread(target=waiting)
    two = threading.Thread(target=signal)
    one.start()
    two.start()
    one.join()
    two.join()
    assert progress[0] == 1
    print("thread-wait-ok")
run()
''', 'thread-wait-ok\n'),
    'generators_and_data': ('''import threading
gate = threading.Event()
gate.set()
entered = [0, 0, 0]

def plain_generator():
    entered[0] = 1
    yield 1

def parking_generator():
    entered[1] = 1
    gate.wait()
    yield 2

async def coroutine():
    entered[2] = 1

saved = parking_generator()

def returns_data():
    return saved

def run():
    a = threading.Thread(target=plain_generator)
    b = threading.Thread(target=parking_generator)
    c = threading.Thread(target=coroutine)
    d = threading.Thread(target=returns_data)
    a.start()
    b.start()
    c.start()
    d.start()
    a.join()
    b.join()
    c.join()
    d.join()
    assert entered == [0, 0, 0]
    assert [returns_data][0]() is saved and entered == [0, 0, 0]
    print("generator-data-ok")
run()
''', 'generator-data-ok\n'),
    'transparent': ('''import threading
from functools import partial
gate = threading.Event()
gate.set()
seen = []

def parked(value):
    gate.wait()
    seen.append(value)
    return value

class Worker:
    def parked(self, value):
        gate.wait()
        seen.append(value)
        return value

def run():
    worker = Worker()
    a = threading.Thread(target=partial(parked, 7))
    b = threading.Thread(target=partial(worker.parked, 9))
    a.start()
    b.start()
    a.join()
    b.join()
    assert sorted(seen) == [7, 9]
    print("transparent-ok")
run()
''', 'transparent-ok\n'),
    'deferred_wrapper': ('''import pcc.virtual_thread as vt
import threading
from functools import partial
gate = threading.Event()
gate.set()

def parked(value: int) -> int:
    gate.wait()
    return value

def wrapper(callback):
    return callback() + 1

def handler():
    return vt.call(wrapper, partial(parked, 40))

def run():
    task = vt.spawn(handler)
    vt.run(1, 64)
    assert vt.result(task) == 41
    print("deferred-wrapper-ok")
run()
''', 'deferred-wrapper-ok\n'),
    'virtual_park': ('''import pcc.virtual_thread as vt
from functools import partial
seen = []

def parked(value: int) -> int:
    seen.append(1)
    vt.yield_now()
    seen.append(3)
    return value

def other():
    seen.append(2)

def handler():
    return vt.call(partial(parked, 42))

def run():
    task = vt.spawn(handler)
    companion = vt.spawn(other)
    vt.run(1, 64)
    assert vt.result(task) == 42 and seen == [1, 2, 3]
    print("virtual-park-ok")
run()
''', 'virtual-park-ok\n'),
    'virtual_generator_data': ('''import pcc.virtual_thread as vt
import threading
gate = threading.Event()
gate.set()
entered = [0]

def source_generator():
    entered[0] = 1
    gate.wait()
    yield 7

def handler():
    value = vt.call(source_generator)
    assert entered[0] == 0
    return next(value)

def run():
    task = vt.spawn(handler)
    vt.run(1, 64)
    assert vt.result(task) == 7 and entered[0] == 1
    print("virtual-generator-data-ok")
run()
''', 'virtual-generator-data-ok\n'),
    'error_restore': ('''import pcc.virtual_thread as vt
import threading
gate = threading.Event()
gate.set()

def failing():
    gate.wait()
    raise ValueError("callback-error")

def parked() -> int:
    gate.wait()
    return 42

def wrapper():
    try:
        [failing][0]()
    except ValueError as error:
        assert str(error) == "callback-error"
    else:
        assert False
    return [parked][0]()

def handler():
    return vt.call(wrapper)

def run():
    task = vt.spawn(handler)
    vt.run(1, 64)
    assert vt.result(task) == 42 and [parked][0]() == 42
    print("context-restored-ok")
run()
''', 'context-restored-ok\n'),
}


PROGRAMS.update({
    'bound_signature': ('''import gc
import threading
from functools import partial
gate = threading.Event()
gate.set()
class Worker:
    def parked(self, first, *rest, extra=0, **named):
        gate.wait()
        gc.collect()
        return first + sum(rest) + extra + named["tail"]
def run():
    worker = Worker()
    bound = [worker.parked][0]
    assert bound(1, 2, 3, extra=4, tail=5) == 15
    wrapped = partial(bound, 7, 8, extra=9, tail=10)
    assert [wrapped][0]() == 34
    print("bound-signature-ok")
run()
''', 'bound-signature-ok\n'),
    'callable_alias_error': ('''import gc
def identity(value):
    return value
def callback(fn):
    assert [identity][0](fn) is fn
    gc.collect()
    raise ValueError("after-aliased-callable-collection")
def run():
    fn = [callback][0]
    try:
        fn(fn)
    except ValueError as error:
        assert str(error) == "after-aliased-callable-collection"
    else:
        assert False
    print("callable-alias-error-ok")
run()
''', 'callable-alias-error-ok\n'),
})

PROGRAMS['factory_callback'] = ('''import pcc.virtual_thread as vt
from functools import partial
class Gate:
    @vt.continuation_factory
    def choose(self, value):
        return vt.completed(value)

def handler():
    gate = Gate()
    callback = [gate.choose][0]
    return vt.call(callback, 42)

def run():
    gate = Gate()
    callback = [gate.choose][0]
    assert callback(40) == 40
    assert [partial(callback, 41)][0]() == 41
    task = vt.spawn(handler)
    vt.run(1, 64)
    assert vt.result(task) == 42
    print("factory-callback-ok")
run()
''', 'factory-callback-ok\n')

PROGRAMS['method_roles'] = ('''import threading
from functools import partial
gate = threading.Event()
gate.set()
seen = []
class Worker:
    def parked(self, value):
        gate.wait()
        seen.append(value)
        return value
    def expose(self):
        return self.parked
    @classmethod
    def parked_class(cls, value):
        gate.wait()
        seen.append(value)
        return value

def run():
    worker = Worker()
    bound = worker.expose()
    class_bound = Worker.parked_class
    unbound = Worker.parked
    assert [bound][0](1) == 1
    assert [class_bound][0](2) == 2
    assert [unbound][0](worker, 3) == 3
    thread = threading.Thread(target=partial(bound, 4))
    thread.start()
    thread.join()
    assert seen == [1, 2, 3, 4]
    print("method-roles-ok")
run()
''', 'method-roles-ok\n')

PROGRAMS['thread_alias'] = ('''import gc
import threading
seen = [0]
def identity(value):
    return value

def target(callback):
    assert [identity][0](callback) is callback
    gc.collect()
    seen[0] = 1
    return callback

def run():
    callback = [target][0]
    thread = threading.Thread(target=callback, args=(callback,))
    thread.start()
    thread.join()
    assert seen[0] == 1
    print("thread-alias-ok")
run()
''', 'thread-alias-ok\n')

PROGRAMS['deferred_constructor'] = ('''import gc
import pcc.virtual_thread as vt
import threading
gate = threading.Event()
gate.set()

def parked() -> int:
    gate.wait()
    gc.collect()
    return 42

class Worker:
    def __init__(self, callback):
        # This is an ordinary dynamic call. The constructor itself has no
        # statically known parking operation and retains its synchronous ABI.
        self.value = callback()

def handler():
    value = vt.call(Worker, parked)
    return value.value

def run():
    task = vt.spawn(handler)
    vt.run(1, 64)
    assert vt.result(task) == 42
    print("deferred-constructor-ok")
run()
''', 'deferred-constructor-ok\n')

def _archive():
    value = os.environ.get('PCC_CALLBACK_RUNTIME_ARCHIVE', '')
    if not value:
        pytest.fail('PCC_CALLBACK_RUNTIME_ARCHIVE must name a matching threaded archive')
    archive = Path(value).resolve(strict=True)
    from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
    manifest = verify_runtime_archive_manifest(archive, runtime_root=Path(__file__).resolve().parents[2] / 'pcc/runtime')
    members = {row['member'] for row in manifest['members']}
    assert 'freestanding_thread_kernel_pthread.o' in members
    assert 'freestanding_thread_kernel.o' not in members
    return archive


def _environment(archive, backend):
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PCC_', 'DYLD_'))}
    env.update(PCC_WITH_THREADS='1', PCC_REFCOUNT_KIND='atomic',
               PCC_RUNTIME_ARCHIVE=str(archive), PCC_RUNTIME_CC='/usr/bin/false',
               PCC_SELF_LINK='pcc', PCC_GC_BACKEND=str(backend))
    env.pop('LC_ALL', None)
    return env


def _run(binary, archive, backend, expected):
    result = subprocess.run([str(binary)], env=_environment(archive, backend),
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected, result.stdout + result.stderr


@pytest.mark.integration
@pytest.mark.parametrize('backend', [1, 4])
@pytest.mark.parametrize('case', list(PROGRAMS))
def test_host_frontend_native_callback_context(tmp_path, monkeypatch, backend, case):
    archive = _archive()
    for key in tuple(os.environ):
        if key.startswith(('PCC_', 'DYLD_')) or key == 'LC_ALL':
            monkeypatch.delenv(key)
    for key, value in _environment(archive, 0).items():
        if key.startswith('PCC_'):
            monkeypatch.setenv(key, value)
    source, expected = PROGRAMS[case]
    path = tmp_path / (case + '.py')
    binary = tmp_path / case
    path.write_text("from pcc.extern import extern, c_int64\n"
                    "_threads = extern('pcc_threads_enabled', (), c_int64)\n"
                    "_backend = extern('pcc_gc_backend', (), c_int64)\n"
                    "assert _threads() == 1\n"
                    "assert _backend() == " + str(backend) + "\n" + source)
    compile_python(str(path), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(archive))
    _run(binary, archive, backend, expected)


@pytest.mark.integration
@pytest.mark.parametrize('backend', [1, 4])
@pytest.mark.parametrize('case', list(PROGRAMS))
def test_native_compiler_callback_context(tmp_path, backend, case):
    archive = _archive()
    value = os.environ.get('PCC_CALLBACK_NATIVE_COMPILER', '')
    if not value:
        pytest.fail('PCC_CALLBACK_NATIVE_COMPILER must contain the new role producers')
    compiler = Path(value).resolve(strict=True)
    source, expected = PROGRAMS[case]
    path = tmp_path / (case + '.py')
    binary = tmp_path / case
    path.write_text("from pcc.extern import extern, c_int64\n"
                    "_threads = extern('pcc_threads_enabled', (), c_int64)\n"
                    "_backend = extern('pcc_gc_backend', (), c_int64)\n"
                    "assert _threads() == 1\n"
                    "assert _backend() == " + str(backend) + "\n" + source)
    built = subprocess.run([str(compiler), '--backend', 'self', '--python-libpython', 'off',
                            str(path), '-o', str(binary)], env=_environment(archive, 0),
                           capture_output=True, text=True, timeout=90)
    assert built.returncode == 0, built.stdout + built.stderr
    _run(binary, archive, backend, expected)


ROLE_SOURCES = {
    'ordinary': ('def target(value):\n    return value\n\ndef caller():\n    return [target][0]\n', False, False, False),
    'auto': ('import threading\ngate = threading.Event()\ndef target():\n    gate.wait()\n    return 42\n\ndef caller():\n    return [target][0]\n', True, False, True),
    'source': ('import threading\ngate = threading.Event()\ndef target():\n    gate.wait()\n    yield 42\n\ndef caller():\n    return [target][0]\n', False, True, True),
    'async': ('async def target():\n    return 42\n\ndef caller():\n    return [target][0]\n', False, False, False),
    # continuation_factory is a nonparking method with a continuation result
    # ABI. A top-level decorated function is intentionally unsupported.
    'factory': ('import pcc.virtual_thread as vt\nclass Gate:\n    @vt.continuation_factory\n    def choose(self, value):\n        return vt.completed(value)\ndef caller():\n    gate = Gate()\n    return gate.choose(42)\n', False, False, False),
}


def _callable_role_records(text, entry_suffix):
    """Follow each matching constructor result to its header atomic role OR.

    Other functions in the same module can legitimately have different roles;
    a factory's ordinary caller is itself auto-lifted. Keep that distinction.
    """
    records = []
    for function in re.finditer(r'^define[^\n]*@([^ (]+)\([^\n]*\)[^\n]*\{\n(.*?)^\}', text, re.M | re.S):
        body = function.group(2)
        aliases = dict(re.findall(r'^\s*(%[^ ]+) = bitcast ptr (%[^ ]+) to ptr', body, re.M))

        def resolve(value):
            while value in aliases:
                value = aliases[value]
            return value

        objects = re.findall(r'^\s*(%[^ ]+) = call[^\n]*@py_func_new_(?:named|bound)\(ptr @([^, )]+)', body, re.M)
        # The slot constructor publishes into its output slot; the role is
        # written to the header of a value loaded back from that slot.
        loaded = {}
        for value, slot in re.findall(r'^\s*(%[^ ]+) = load ptr, ptr (%[^ ,\n]+)', body, re.M):
            loaded[value] = resolve(slot)
        for adapter, slot in re.findall(
            r'call i64[^\n]*@py_func_new_signature_slots\(ptr [^,]+, ptr [^,]+, ptr @([^, )]+), ptr [^\n]*?, ptr (%[^ ,)]+)\)',
            body,
        ):
            objects.append((resolve(slot), adapter))
        headers = dict(re.findall(r'^\s*(%[^ ]+) = getelementptr i8, ptr (%[^, ]+), i64 12', body, re.M))
        roles = {}
        for address, mask in re.findall(r'atomicrmw or ptr (%[^, ]+), i32 ([0-9]+)', body):
            owner = headers.get(resolve(address))
            if owner is not None:
                owner = loaded.get(owner, owner)
                roles[owner] = roles.get(owner, 0) | int(mask)
        for owner, adapter in objects:
            if entry_suffix in adapter:
                records.append((function.group(1), owner, adapter, roles.get(owner, 0)))
        # The static constructor takes the role as an argument.
        for adapter, role in re.findall(
            r'call i64[^\n]*@py_func_new_static_def\(ptr @([^, )]+), [^\n]*?, i64 ([0-9]+), ptr',
            body,
        ):
            if entry_suffix in adapter:
                records.append((function.group(1), None, adapter, int(role)))
    return records


@pytest.mark.parametrize('case', list(ROLE_SOURCES))
def test_callable_roles_in_actual_threaded_ir(tmp_path, monkeypatch, case):
    from pcc.backend.owned_object_emit import emit_owned_object
    for key in tuple(os.environ):
        if key.startswith(('PCC_', 'DYLD_')) or key == 'LC_ALL':
            monkeypatch.delenv(key)
    for key, value in {'PCC_WITH_THREADS':'1', 'PCC_REFCOUNT_KIND':'atomic',
                       'PCC_PYTHON_IR_PASSES':'off', 'PCC_PY_FRONTEND_IR_CACHE':'0',
                       'PCC_SELF_BACKEND_OBJECT_CACHE':'0',
                       'PCC_RUNTIME_CC':'/usr/bin/false'}.items():
        monkeypatch.setenv(key, value)
    program, auto, source, parking = ROLE_SOURCES[case]
    path = tmp_path / (case + '.py')
    path.write_text(program)
    output = tmp_path / (case + '.ll')
    compile_python(str(path), str(output), emit_llvm_only=True, python_library=True,
                   backend='self', libpython_mode='off', target_triple='arm64-apple-darwin')
    text = output.read_text()
    suffix = '_Gate_choose_method_native_adapter' if case == 'factory' else '_target_'
    records = _callable_role_records(text, suffix)
    assert records, (case, 'no matching callable constructor')
    expected_role = 67108864 if case == 'factory' else (8388608 if auto else 0)
    assert all(row[3] == expected_role for row in records), (case, expected_role, records)
    target_name = 'user_factory_Gate_choose' if case == 'factory' else 'user_' + case + '_target'
    target = re.search(r'^define[^\n]*@' + target_name + r'\([^\n]*\)[^\n]*\{\n(.*?)^\}', text, re.M | re.S)
    assert target is not None, (case, target_name)
    has_source = bool(re.search(r'atomicrmw or[^\n]*i32 33554432', target.group(1)))
    has_parking = bool(re.search(r'call[^\n]*@py_gen_set_may_park\(', target.group(1)))
    assert has_source == source, (case, 'SOURCE', has_source, source)
    assert has_parking == parking, (case, 'MAY_PARK', has_parking, parking)
    (tmp_path / (case + '.o')).write_bytes(emit_owned_object(text, 'arm64-apple-darwin'))


METHOD_ROLE_SOURCE = """import threading
gate = threading.Event()
class Worker:
    def parked(self, value):
        gate.wait()
        return value
    def expose(self):
        return self.parked
    @classmethod
    def parked_class(cls, value):
        gate.wait()
        return value

def expose_class():
    return Worker.parked_class

def expose_unbound():
    return Worker.parked
"""


def test_all_direct_method_adapter_roles(tmp_path, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    for key in tuple(os.environ):
        if key.startswith(('PCC_', 'DYLD_')) or key == 'LC_ALL':
            monkeypatch.delenv(key)
    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    monkeypatch.setenv('PCC_REFCOUNT_KIND', 'atomic')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    path = tmp_path / 'method_roles.py'
    path.write_text(METHOD_ROLE_SOURCE)
    output = tmp_path / 'method_roles.ll'
    compile_python(str(path), str(output), emit_llvm_only=True, python_library=True,
                   backend='self', libpython_mode='off', target_triple='arm64-apple-darwin')
    text = output.read_text()
    for suffix in ('Worker_expose', 'expose_class', 'expose_unbound'):
        body = re.search(r'^define[^\n]*@[^\n ]*' + suffix + r'\([^\n]*\)[^\n]*\{\n(.*?)^\}', text, re.M | re.S)
        assert body is not None, suffix
        assert re.search(r'atomicrmw or[^\n]*i32 8388608', body.group(1)), (suffix, body.group(1))
    (tmp_path / 'method_roles.o').write_bytes(emit_owned_object(text, 'arm64-apple-darwin'))


UNSUPPORTED_PARKING_DUNDER_SOURCE = 'import pcc.virtual_thread as vt\nimport threading\ngate = threading.Event()\ngate.set()\nclass Worker:\n    def __init__(self):\n        gate.wait()\n        self.value = 42\n\ndef handler():\n    value = vt.call(Worker)\n    return value.value\n\ndef run():\n    task = vt.spawn(handler)\n    vt.run(1, 64)\n    assert vt.result(task) == 42\n    print("deferred-constructor-ok")\nrun()\n'


def test_direct_parking_constructor_keeps_explicit_rejection(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(('PCC_', 'DYLD_')) or key == 'LC_ALL':
            monkeypatch.delenv(key)
    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    monkeypatch.setenv('PCC_REFCOUNT_KIND', 'atomic')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    path = tmp_path / 'direct_parking_constructor.py'
    path.write_text(UNSUPPORTED_PARKING_DUNDER_SOURCE)
    with pytest.raises(Exception, match='implicit descriptor/dunder may_park dispatch is unsupported'):
        compile_python(str(path), str(tmp_path / 'rejected.ll'), emit_llvm_only=True,
                       backend='self', libpython_mode='off', target_triple='arm64-apple-darwin')
