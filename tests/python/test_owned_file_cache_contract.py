"""Registered temporary type caches and descriptor-adoption failure boundaries."""
import errno
import fcntl
import os

import pytest

from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_owned_tempfile_provider import Obj


@pytest.mark.parametrize('kind',(0,1))
def test_temporary_type_cache_follows_registered_root_after_remap(kind):
    runtime=FileRuntime();entry=runtime.ns['_temporary_type']
    first=entry(kind)
    name='pcc_namedtempfile_wrapper_type' if kind else 'pcc_tempdir_class'
    root=runtime.global_addr(name)
    assert any(slot.memory is root.memory and slot.offset==root.offset for slot in runtime.handles.values())
    assert first.pins==0 and not runtime.frames and not runtime.leases
    moved=Obj('class');moved.attrs=first.attrs.copy()
    # A remapper updates registered roots, then the old shell becomes invalid.
    for slot in runtime.handles.values():
        if runtime.read(slot,0) is first:runtime.write(slot,0,moved)
    first.attrs.clear();first.value='retired shell'
    assert entry(kind) is moved
    assert len(runtime.handles)==1 and moved.pins==0 and not runtime.frames and not runtime.leases
    assert len(runtime.exits)==(0 if kind else 1)


@pytest.mark.parametrize('kind',(0,1))
def test_failed_type_root_registration_never_publishes_partial_class(kind):
    runtime=FileRuntime();runtime.ns['pcc_gc_scheduler_root_register_handle']=lambda slot:None
    assert runtime.ns['_temporary_type'](kind) is None
    assert runtime.error.value[0]==19
    name='pcc_namedtempfile_wrapper_type' if kind else 'pcc_tempdir_class'
    assert runtime.global_load(name) is None and not runtime.exits
    assert not runtime.frames and not runtime.leases and not runtime.handles
    runtime.error=None;runtime.ns['pcc_gc_scheduler_root_register_handle']=runtime.register
    assert runtime.ns['_temporary_type'](kind) is not None
    assert len(runtime.handles)==1 and not runtime.frames and not runtime.leases


def test_shutdown_registration_failure_removes_root_and_preserves_error():
    runtime=FileRuntime();runtime.ns['atexit']=lambda callback:1
    assert runtime.ns['_temporary_type'](0) is None
    assert runtime.error.value[0]==19
    assert runtime.global_load('pcc_tempdir_class') is None
    assert runtime.global_load('pcc_tempdir_class_root_handle') is None
    assert not runtime.frames and not runtime.leases and not runtime.handles


@pytest.mark.parametrize('kind',(0,1))
def test_competing_type_construction_returns_one_published_owner(kind):
    runtime=FileRuntime();original=runtime.ns['py_class_new'];nested=[];entered=False;candidates=[]
    def construct(*args):
        nonlocal entered
        candidate=original(*args);candidates.append(candidate)
        if not entered:
            entered=True;nested.append(runtime.ns['_temporary_type'](kind))
        return candidate
    runtime.ns['py_class_new']=construct
    result=runtime.ns['_temporary_type'](kind)
    assert result is nested[0] and result is candidates[1]
    assert result is runtime.ns['_temporary_type'](kind)
    assert len(runtime.handles)==1 and len(runtime.exits)==(0 if kind else 1)
    assert all(candidate.pins==0 for candidate in candidates)
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize('kind',(0,1))
def test_type_initialization_error_prevents_publication_and_preserves_first_exception(kind):
    runtime=FileRuntime();original=Obj('exception',(19,'method construction failed'));calls=[]
    def fail(*args):calls.append(args);runtime.error=original;return None
    runtime.ns['py_func_new_named']=fail
    assert runtime.ns['_temporary_type'](kind) is None
    assert runtime.error is original and len(calls)==1
    assert not runtime.frames and not runtime.leases and not runtime.handles and not runtime.exits


@pytest.mark.parametrize('failure',('stream','buffer','registry','custom_buffer','set_append'))
def test_append_failure_leaves_descriptor_flags_position_and_ownership_unchanged(tmp_path,failure):
    runtime=FileRuntime();path=tmp_path/'file';path.write_bytes(b'keep')
    fd=os.open(path,os.O_RDWR);os.lseek(fd,2,os.SEEK_SET)
    before_flags=fcntl.fcntl(fd,fcntl.F_GETFL);original=runtime.ns['malloc'];calls=[]
    fail_at={'stream':1,'buffer':2,'registry':3,'custom_buffer':4}.get(failure,0)
    def allocate(size):
        calls.append(size)
        return None if len(calls)==fail_at else original(size)
    runtime.ns['malloc']=allocate
    if failure=='set_append':
        runtime.ns['fd_control']=lambda fd,cmd,value:-errno.EPERM if cmd==fcntl.F_SETFL else fcntl.fcntl(fd,cmd,value)
    try:
        assert runtime.ns['pcc_stdio_fdopen'](fd,runtime.cstr('a'),1,8192) is None
        assert runtime.errno==(errno.EPERM if failure=='set_append' else errno.ENOMEM)
        assert fcntl.fcntl(fd,fcntl.F_GETFL)==before_flags
        assert os.lseek(fd,0,os.SEEK_CUR)==2 and os.fstat(fd)
        assert runtime.global_load('pcc_stdio_registry_head') is None
    finally:os.close(fd)
    assert path.read_bytes()==b'keep'


def test_type_failure_cleanup_preserves_original_error_over_finalizer_error():
    runtime=FileRuntime();original=Obj('exception',(19,'class method failed'));later=Obj('exception',(2,'retirement'))
    def fail(*args):runtime.error=original;return None
    runtime.ns['py_func_new_named']=fail
    def clear(slot,value):
        prior=runtime.read(slot,0)
        runtime.write(slot,0,value)
        if value is None and isinstance(prior,Obj) and prior.kind=='class':runtime.error=later
    runtime.ns['pcc_gc_store_root']=clear
    assert runtime.ns['_temporary_type'](1) is None
    assert runtime.error is original and not runtime.frames and not runtime.leases and not runtime.handles
