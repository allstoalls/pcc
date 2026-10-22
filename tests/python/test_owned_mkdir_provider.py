"""Real mkdir/fspath/index/codec bodies over a bounded raw-ABI model.

These are host semantic and ownership checks, not native qualification.
"""
import os
from pathlib import Path
import re
import stat

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_owned_tempfile_provider import Obj

ROOT = Path(__file__).resolve().parents[2]


class MkdirRuntime(FileRuntime):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.ns.update(mkdir_at=self.mkdir_at, py_unicode_encode_error=self.encoding_error)
        self.load_selected(ROOT/'pcc/runtime/py/py_str_accessors.py', {
            '_utf8_ord_at_byte', '_encode_width', '_encode_invalid',
            '_encode_raise_unicode', '_str_encode_codec', '_encode_guard_error',
            '_str_encode_guarded', 'py_text_encode_ids',
        })
        self.load(ROOT/'pcc/runtime/py/py_os_mkdir.py')

    def read(self, value, offset):
        if isinstance(value, Obj) and value.kind == 'str' and offset == abi.PYSTROBJECT_BYTE_LEN_OFFSET:
            return len(value.value.encode('utf-8', 'surrogatepass'))
        return super().read(value, offset)

    def ptr_add(self, value, offset):
        if isinstance(value, Obj) and value.kind == 'str':
            assert offset == abi.PYSTROBJECT_DATA_OFFSET
            raw = value.value.encode('utf-8', 'surrogatepass')
            pointer = self.alloc(len(raw))
            pointer.memory.data[:] = raw
            return pointer
        return super().ptr_add(value, offset)

    def encoding_error(self, value, codec, start, end, reason):
        self.error = Obj('exception', ('UnicodeEncodeError', value.value, start, end))

    def mkdir_at(self, path, mode, directory):
        raw = self.raw(path)
        self.calls.append((raw, mode, directory))
        try:
            os.mkdir(raw, mode, dir_fd=None if directory == -100 else directory)
            return 0
        except OSError as error:
            return -error.errno

    def box(self, value):
        if value is None: return self.none
        if value is True: return self.true
        if value is False: return self.false
        if isinstance(value, str): return self.string(value)
        if isinstance(value, bytes): return Obj('bytes', value)
        if isinstance(value, float): return Obj('float', value)
        if isinstance(value, int): return self.integer(value)
        return value

    def invoke(self, path, mode=0o777, directory=None):
        return self.ns['_mkdir_entry'](None, Obj('tuple', [
            self.box(path), self.box(mode), self.box(directory)]))

    def pathlike(self, value, events=None, error=None):
        owner = self.instance(Obj('class'))
        def fspath(captures, args):
            assert self.leases.get(id(owner), 0) > 0
            assert self.frames
            if events is not None: events.append('path')
            if error is not None: self.error = error; return None
            return self.box(value)
        owner.cls.attrs['__fspath__'] = Obj('func', (fspath, None))
        owner.attrs['__fspath__'] = Obj('func', (lambda *_: pytest.fail('instance lookup'), None))
        return owner

    def clean(self):
        assert not self.frames and not self.leases


@pytest.mark.parametrize('kind', ['str', 'bytes', 'path-str', 'path-bytes', 'surrogate'])
def test_real_mkdir_path_domains_permissions_and_owned_none(tmp_path, kind):
    runtime = MkdirRuntime()
    path = str(tmp_path/'résultat')
    if kind == 'surrogate': path = str(tmp_path) + '/raw-\udcff'
    argument = os.fsencode(path) if kind in ('bytes', 'path-bytes') else path
    if kind.startswith('path-'): argument = runtime.pathlike(argument)
    if kind == 'surrogate':
        # Filesystems that require UTF-8 names (APFS) reject this byte
        # sequence in CPython too; the provider must report the same errno.
        try: os.mkdir(str(tmp_path) + '/oracle-\udcff', 0o700)
        except OSError as expected:
            assert runtime.invoke(argument, 0o700) is None
            assert runtime.error.attrs['errno'] == expected.errno
            assert runtime.calls == [(os.fsencode(path), 0o700, -100)]
            runtime.clean()
            return
    assert runtime.invoke(argument, 0o700) is runtime.none
    assert runtime.error is None and os.path.isdir(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    assert runtime.calls == [(os.fsencode(path), 0o700, -100)]
    runtime.clean()


def test_directory_fd_relative_and_absolute_semantics(tmp_path):
    runtime = MkdirRuntime()
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert runtime.invoke(b'child', 0o700, fd) is runtime.none
        assert (tmp_path/'child').is_dir()
        assert runtime.invoke(str(tmp_path/'absolute'), 0o700, -999) is runtime.none
        assert (tmp_path/'absolute').is_dir()
    finally: os.close(fd)
    runtime.clean()


_INVALID_INTEGERS = ['', '12', 1.25, 1<<100, -(1<<100), 1<<31, -(1<<31)-1, None]


# None is the documented dir_fd default, so it is invalid only as a mode.
@pytest.mark.parametrize('which,value', [
    (which, value) for which in ('mode', 'directory') for value in _INVALID_INTEGERS
    if not (which == 'directory' and value is None)
])
def test_invalid_integer_matches_cpython_before_io(tmp_path, which, value):
    runtime = MkdirRuntime()
    kwargs = {'mode' if which == 'mode' else 'dir_fd': value}
    with pytest.raises((TypeError, OverflowError)) as expected:
        os.mkdir(tmp_path/'oracle', **kwargs)
    assert runtime.invoke(str(tmp_path/'model'), **{which: value}) is None
    assert runtime.error.value[0] == {TypeError:3, OverflowError:15}[type(expected.value)]
    assert runtime.calls == [] and list(tmp_path.iterdir()) == []
    runtime.clean()


@pytest.mark.parametrize('which', ['mode', 'directory'])
@pytest.mark.parametrize('value', [-2147483648, 2147483647, False, True])
def test_c_int_endpoints_and_bools_reach_platform(tmp_path, which, value):
    runtime = MkdirRuntime(); calls = []
    runtime.ns['mkdir_at'] = lambda path, mode, fd: calls.append((mode, fd)) or 0
    assert runtime.invoke(str(tmp_path/'unused'), **{which:value}) is runtime.none
    assert calls[0][0 if which == 'mode' else 1] == int(value)
    assert runtime.error is None
    runtime.clean()


@pytest.mark.parametrize('failure', ['wrong', 'overflow', 'callback'])
@pytest.mark.parametrize('which', ['mode', 'directory'])
def test_real_index_protocol_failures_preserve_exception_and_owners(tmp_path, which, failure):
    runtime = MkdirRuntime(); events=[]
    original = Obj('exception', (2, 'index failed'))
    value = runtime.string('wrong') if failure == 'wrong' else runtime.integer(1<<100)
    index = runtime.index_object(value, original if failure == 'callback' else None,
                                 lambda: events.append('index'))
    assert runtime.invoke(str(tmp_path/'unused'), **{which:index}) is None
    assert events == ['index'] and runtime.calls == []
    if failure == 'callback': assert runtime.error is original
    else: assert runtime.error.value[0] == (3 if failure == 'wrong' else 15)
    runtime.clean()


def test_conversion_order_and_successful_index_callbacks(tmp_path):
    runtime=MkdirRuntime();events=[]
    path=runtime.pathlike(str(tmp_path/'created'), events)
    mode=runtime.index_object(0o700, callback=lambda:events.append('mode'))
    directory=runtime.index_object(-100, callback=lambda:events.append('dir_fd'))
    assert runtime.invoke(path, mode, directory) is runtime.none
    assert events == ['path', 'mode', 'dir_fd'] and (tmp_path/'created').is_dir()
    runtime.clean()


@pytest.mark.parametrize('bad', [b'a\0b', 'a\0b', '\ud800'])
def test_path_validation_precedes_integer_callbacks(tmp_path, bad):
    runtime=MkdirRuntime();events=[]
    class Index:
        def __index__(self): events.append('oracle mode'); return 0o700
    with pytest.raises((ValueError, UnicodeEncodeError)): os.mkdir(bad, Index())
    assert events == []
    mode=runtime.index_object(0o700, callback=lambda:events.append('model mode'))
    assert runtime.invoke(runtime.pathlike(bad), mode) is None
    assert runtime.error.value[0] == ('UnicodeEncodeError' if bad == '\ud800' else 2)
    assert events == [] and runtime.calls == []
    runtime.clean()


@pytest.mark.parametrize('bad', [None, 2, 1.5])
def test_fspath_invalid_returns_never_reach_integer_or_io(bad):
    runtime=MkdirRuntime();events=[]
    mode=runtime.index_object(0o700, callback=lambda:events.append('mode'))
    assert runtime.invoke(runtime.pathlike(bad), mode) is None
    assert runtime.error.value[0] == 3 and events == [] and runtime.calls == []
    runtime.clean()


@pytest.mark.parametrize('failure', ['exists', 'missing', 'notdir', 'badfd'])
@pytest.mark.parametrize('bytes_path', [False, True])
def test_real_os_errors_have_exact_errno_args_and_normalized_filename(tmp_path, failure, bytes_path):
    runtime=MkdirRuntime(); directory=None
    if failure == 'exists': path=tmp_path
    elif failure == 'missing': path=tmp_path/'missing'/'child'
    elif failure == 'notdir':
        (tmp_path/'file').write_text('keep'); path=tmp_path/'file'/'child'
    else: path=Path('relative'); directory=-999
    argument=os.fsencode(path) if bytes_path else str(path)
    with pytest.raises(OSError) as expected: os.mkdir(argument, dir_fd=directory)
    assert runtime.invoke(runtime.pathlike(argument), directory=directory) is None
    error=runtime.error
    assert error.attrs['errno'] == expected.value.errno
    assert error.attrs['strerror'].value == expected.value.strerror
    assert error.attrs['filename'].value == expected.value.filename
    assert [v.value if isinstance(v,Obj) else v for v in error.attrs['args'].value] == list(expected.value.args)
    assert error.value[0] == {'exists':35,'missing':34,'notdir':38,'badfd':14}[failure]
    runtime.clean()


def test_callable_factory_is_canonical_registered_and_returns_independent_owner():
    runtime=MkdirRuntime()
    first=runtime.ns['py_os_mkdir_function']()
    second=runtime.ns['py_os_mkdir_function']()
    assert first is second and first.kind == 'func'
    entry,captures=first.value
    assert entry is runtime.ns['_mkdir_entry']
    signature=captures.value[1].value
    assert signature[0].value == '__pcc_func_signature_v1__'
    assert [v.value for v in signature[1].value] == ['path','mode','dir_fd']
    assert signature[2].value == [0,0,2]
    assert [v.value for v in signature[3].value] == [False,True,True]
    assert signature[4].value == [runtime.none,511,runtime.none]
    assert len(runtime.handles) == 1 and first.pins == 0
    runtime.clean()


def test_eintr_retry_and_darwin_default_descriptor(tmp_path):
    runtime=MkdirRuntime();calls=[]
    runtime.ns['target_sys_platform']=lambda:runtime.cstr('darwin')
    def interrupted(path,mode,directory):
        calls.append((mode,directory)); return -4 if len(calls)==1 else 0
    runtime.ns['mkdir_at']=interrupted
    assert runtime.invoke(str(tmp_path/'unused')) is runtime.none
    assert calls == [(511,-2),(511,-2)]
    runtime.clean()


def test_windows_capability_gap_is_explicit_before_io():
    runtime=MkdirRuntime()
    runtime.ns['target_sys_platform']=lambda:runtime.cstr('win32')
    assert runtime.invoke('unused') is None
    assert runtime.error.value[0] == 11 and 'Windows' in runtime.error.value[1]
    assert runtime.calls == []
    runtime.clean()


@pytest.mark.parametrize('target', ['x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu', 'arm64-apple-macosx11.0', 'x86_64-pc-windows-msvc'])
def test_mkdir_at_owned_target_lowering(tmp_path,target):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source=tmp_path/'mkdir_at.py';out=tmp_path/'mkdir_at.ll'
    source.write_text('''from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import mkdir_at
__pcc_freestanding__ = True
@c_abi_export('probe_mkdir_at')
def probe(path: c_ptr, mode: i64, directory: i64) -> i64:
    return mkdir_at(path, mode, directory)
''')
    compile_python(str(source), str(out), emit_llvm_only=True, python_library=True,
                   libpython_mode='off', target_triple=target)
    text=out.read_text();verify_ir_text(text)
    assert not re.search(r'\bcall[^\n]*@py_cpy_',text)
    if 'linux' in target:
        assert '@mkdirat(' not in text
        assert ('syscall' in text and 'i64 258' in text) if target.startswith('x86') else ('svc' in text and 'i64 34' in text)
    elif 'apple' in target:
        assert '@mkdirat(i32, ptr, i32)' in text and '@__error()' in text
    else:
        assert '@mkdirat(' not in text and 'ret i64 -38' in text


@pytest.mark.parametrize('target', ['x86_64-unknown-linux-gnu', 'arm64-apple-macosx11.0'])
def test_complete_provider_compiles_to_owned_ir(tmp_path, target, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    monkeypatch.setenv('PCC_WITH_THREADS','0')
    out=tmp_path/'py_os_mkdir.ll'
    compile_python(str(ROOT/'pcc/runtime/py/py_os_mkdir.py'), str(out),
        emit_llvm_only=True, python_library=True, libpython_mode='off', target_triple=target)
    text=out.read_text();verify_ir_text(text)
    assert re.search(r'^define[^\n]*@py_os_mkdir_function\(\)',text,re.M)
    assert re.search(r'^define[^\n]*@pcc_os_mkdir_entry\(ptr[^,]*, ptr[^)]*\)',text,re.M)
    assert not re.search(r'\bcall[^\n]*@py_cpy_',text)
    assert '@py_file_fspath(' in text and '@py_index_i64_checked_slots(' in text
    assert '@py_text_encode_ids(' in text


@pytest.mark.parametrize('phase', ['', 'allocation', 'string', 'frame_enter', 'frame_leave',
                                  'load_root', 'field_store', 'publish', 'graph_unlock', 'tuple_set', 'tuple_get'])
def test_mkdir_error_uses_real_shared_metadata_and_exception_owners(phase):
    from test_os_error_attribute_owner import ErrorMemory, bodies
    memory=ErrorMemory(phase)
    memory.maps['pcc_mkdir_frame_map']=12
    n=memory.namespace.copy()
    bodies(ROOT/'pcc/runtime/py/py_os_mkdir.py', n)
    slots=n['stack_alloc'](12*8);pins=n['stack_alloc'](12*8)
    n['memset'](slots,0,12*8);n['memset'](pins,0,12*8)
    n['pcc_gc_frame_enter'](n['global_addr']('pcc_mkdir_frame_map'),slots)
    n['_mkdir_hold'](slots,pins,n['_MKDIR_NORMALIZED'],memory.string('/missing/owned',0))
    n['_mkdir_os_error'](slots,pins,-2)
    assert n['_mkdir_finish'](slots,pins) is None
    error=memory.pending.fields[0]
    assert error.fields[16].fields['exception_tag']==34
    memory.store_root(memory.handled,error);n['py_clear_exception']()
    expected={'errno':2,'strerror':os.strerror(2),'filename':'/missing/owned','filename2':None,'args':(2,os.strerror(2))}
    for name,value in expected.items():
        result=memory.dispatch['py_obj_getattr'](memory.handled.fields[0],name)
        assert memory.python(result)==value
        memory.decref(result)
    oracle=FileNotFoundError(2,os.strerror(2),'/missing/owned')
    assert memory.render()==str(oracle) and memory.render(1)==repr(oracle)
    assert not memory.pin_metric and not memory.graph_depth


from tests.python.test_dataclass_factory_binding import host_binder as _existing_host_binder


@pytest.fixture
def host_binder(monkeypatch):
    import pcc.unsafe as unsafe
    monkeypatch.setattr(unsafe, 'define_global_ptr_null', lambda *_: None)
    monkeypatch.setattr(unsafe, 'define_global_i64', lambda *_: None)
    return _existing_host_binder.__wrapped__(monkeypatch)


@pytest.mark.parametrize('args,kwargs,expected', [
    (('path',), None, ('path',511,None)),
    (('path',0o700), None, ('path',0o700,None)),
    ((), {'path':'path'}, ('path',511,None)),
    (('path',), {'mode':0o700,'dir_fd':9}, ('path',0o700,9)),
    ((), None, None),
    (('path',0o700,9), None, None),
    (('path',), {'path':'duplicate'}, None),
    (('path',), {'unknown':1}, None),
])
def test_factory_metadata_runs_through_real_argument_binders(host_binder, monkeypatch, args, kwargs, expected):
    binder, _unused, none, state = host_binder
    runtime=MkdirRuntime();fn=runtime.ns['py_os_mkdir_function']()
    def plain(value):
        if value is runtime.none:return none
        if isinstance(value,Obj):
            return tuple(plain(v) for v in value.value) if value.kind=='tuple' else value.value
        return value
    signature=plain(fn.value[1].value[1])
    monkeypatch.setattr(binder,'py_call_validate_kwargs',lambda _:0)
    monkeypatch.setattr(binder,'_signature_error',lambda *_:state.__setitem__('error',TypeError()) or None)
    result=(binder._bind_signature_no_kwargs(signature,args,None) if kwargs is None
            else binder._bind_signature(signature,args,kwargs,None))
    if expected is None:
        assert result is None and isinstance(state['error'],TypeError)
    else:
        assert tuple(None if v is none else v for v in result)==expected
        assert state['error'] is None
    runtime.clean()


@pytest.mark.parametrize('allocation',range(1,17))
def test_callable_factory_allocation_failure_stops_and_cleans_slots(allocation):
    runtime=MkdirRuntime();count=0;original=Obj('exception',(19,'injected allocation'))
    def wrap(fn):
        def allocate(*args):
            nonlocal count
            count+=1
            assert count<=allocation, 'allocated again after failure'
            if count==allocation:
                runtime.error=original;return None
            return fn(*args)
        return allocate
    for name in ('py_tuple_new','py_str_new','py_int_from_i64','py_func_new_named'):
        runtime.ns[name]=wrap(runtime.ns[name])
    assert runtime.ns['py_os_mkdir_function']() is None
    assert runtime.error is original and count==allocation and not runtime.handles
    runtime.clean()


@pytest.mark.parametrize('field',['errno','strerror','filename','args'])
def test_metadata_failure_stops_later_setters_and_preserves_error(tmp_path,field):
    runtime=MkdirRuntime();calls=[];original=Obj('exception',(19,'metadata failed'))
    setter=runtime.ns['py_obj_setattr']
    def failing_setter(owner,name,value):
        key=runtime.raw(name).decode();calls.append(key)
        if key==field:runtime.error=original;return -1
        return setter(owner,name,value)
    runtime.ns['py_obj_setattr']=failing_setter
    assert runtime.invoke(str(tmp_path)) is None
    assert runtime.error is original
    assert calls==['errno','strerror','filename','args'][:['errno','strerror','filename','args'].index(field)+1]
    runtime.clean()
