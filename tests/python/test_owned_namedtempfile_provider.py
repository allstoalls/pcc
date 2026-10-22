"""Managed NamedTemporaryFile bodies; native execution remains a separate gate."""
import os
from pathlib import Path

import pytest

from tests.python.test_owned_tempfile_provider import Obj
from tests.python.test_owned_fdopen_provider import FileRuntime


def create(runtime,root,*,delete=True,on_close=True,mode='w',prefix='owned_',suffix='_tail'):
    directory=root if isinstance(root,Obj) else runtime.string(str(root))
    args=Obj('tuple',[runtime.string(mode),-1,runtime.string('utf-8') if 'b' not in mode else runtime.none,
        runtime.none,runtime.string(suffix),runtime.string(prefix),directory,
        runtime.true if delete else runtime.false,runtime.none,runtime.true if on_close else runtime.false])
    return runtime.ns['_nt_entry'](None,args)


def invoke(runtime,manager,name,*args):
    fn=runtime.getattr(manager,runtime.cstr(name))
    return runtime.call(fn,Obj('tuple',list(args)),None)


def test_named_tempfile_name_write_close_delete_false(tmp_path):
    runtime=FileRuntime();manager=create(runtime,tmp_path,delete=False)
    assert runtime.error is None and manager is not None
    name=Path(manager.attrs['name'].value)
    assert name.parent==tmp_path and name.name.startswith('owned_') and name.name.endswith('_tail')
    assert name.stat().st_mode&0o777==0o600
    assert invoke(runtime,manager,'write',runtime.string('native'))==6
    assert invoke(runtime,manager,'close') is runtime.none
    assert name.read_text()=='native'
    assert runtime.getattr(manager,runtime.cstr('closed')) is runtime.true
    name.unlink()
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize('on_close',(True,False))
def test_delete_policy_close_and_context_exit(tmp_path,on_close):
    runtime=FileRuntime();manager=create(runtime,tmp_path,on_close=on_close)
    name=Path(manager.attrs['name'].value)
    assert invoke(runtime,manager,'__enter__') is manager
    invoke(runtime,manager,'close')
    assert name.exists() is (not on_close)
    assert invoke(runtime,manager,'__exit__',runtime.none,runtime.none,runtime.none) is runtime.false
    assert not name.exists()
    invoke(runtime,manager,'close')
    assert runtime.error is None and not runtime.frames and not runtime.leases


def test_delegated_bound_method_keeps_wrapper_owner(tmp_path):
    runtime=FileRuntime();manager=create(runtime,tmp_path)
    fn=runtime.getattr(manager,runtime.cstr('write'))
    assert fn.value[1].value[0] is manager
    assert runtime.call(fn,Obj('tuple',[runtime.string('kept')]),None)==4
    invoke(runtime,manager,'close')
    assert list(tmp_path.iterdir())==[]


def test_pathlike_directory_calls_fspath_once(tmp_path):
    runtime=FileRuntime();cls=Obj('class');path=runtime.instance(cls);events=[]
    def fspath(captures,args):events.append('fspath');return runtime.string(str(tmp_path))
    cls.attrs['__fspath__']=Obj('func',(fspath,None))
    manager=create(runtime,path)
    assert runtime.error is None and events==['fspath']
    invoke(runtime,manager,'close')
    assert list(tmp_path.iterdir())==[] and not runtime.frames and not runtime.leases


def test_finalizer_preserves_pending_exception_and_removes_file(tmp_path):
    runtime=FileRuntime();manager=create(runtime,tmp_path,on_close=False)
    original=Obj('exception',(2,'original'));runtime.error=original
    runtime.ns['_nt_del'](None,Obj('tuple',[manager]))
    assert runtime.error is original and list(tmp_path.iterdir())==[]
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize('failure',('mode','setattr'))
def test_construction_failure_rolls_back_file_and_descriptor(tmp_path,failure):
    runtime=FileRuntime();runtime.fail_setattr=failure=='setattr'
    descriptors='/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
    before=set(os.listdir(descriptors))
    assert create(runtime,tmp_path,mode='rr' if failure=='mode' else 'w') is None
    assert runtime.error is not None and list(tmp_path.iterdir())==[]
    assert set(os.listdir(descriptors))==before
    assert not runtime.frames and not runtime.leases


def test_public_signature_keyword_only_delete_on_close_and_errors():
    runtime=FileRuntime();fn=runtime.ns['py_namedtempfile_function']()
    assert fn is runtime.ns['py_namedtempfile_function']()
    signature=fn.value[1].value[1]
    assert [x.value for x in signature.value[1].value]==[
        'mode','buffering','encoding','newline','suffix','prefix','dir','delete','errors','delete_on_close']
    assert signature.value[2].value==[0]*8+[2,2]
    assert not runtime.frames and not runtime.leases


def test_fspath_rejects_wrong_result_and_preserves_user_error():
    runtime=FileRuntime();cls=Obj('class');path=runtime.instance(cls)
    cls.attrs['__fspath__']=Obj('func',(lambda c,a:12,None))
    assert runtime.ns['py_file_fspath'](path) is None
    assert runtime.error.value[0]==3
    runtime.error=None
    original=Obj('exception',(2,'path error'))
    def fail(c,a):runtime.error=original;return None
    cls.attrs['__fspath__']=Obj('func',(fail,None))
    assert runtime.ns['py_file_fspath'](path) is None and runtime.error is original
    assert not runtime.frames and not runtime.leases


def test_fspath_does_not_accept_instance_override():
    runtime=FileRuntime();path=Obj('instance')
    path.attrs['__fspath__']=Obj('func',(lambda c,a:runtime.string('/tmp'),None))
    assert runtime.ns['py_file_fspath'](path) is None
    assert runtime.error.value[0]==3 and not runtime.frames and not runtime.leases


def test_cleanup_keeps_original_resource_when_public_attributes_change(tmp_path):
    runtime=FileRuntime();manager=create(runtime,tmp_path)
    original=Path(manager.attrs['name'].value)
    other=tmp_path/'untouched';other.write_text('keep')
    manager.attrs['name']=runtime.string(str(other))
    manager.attrs['file']=Obj('instance')
    invoke(runtime,manager,'close')
    assert runtime.error is None and not original.exists() and other.read_text()=='keep'
    assert not runtime.frames and not runtime.leases
