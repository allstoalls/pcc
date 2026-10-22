"""Public integer arguments run the real index and bigint conversion bodies."""
import os
import tempfile
import warnings

import pytest

from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_owned_tempfile_provider import Obj


INVALID_VALUES = ['', '12', 1.25, None, 1 << 100, -(1 << 100), 1 << 31, -(1 << 31) - 1]


def boxed(runtime, value):
    if value is None:return runtime.none
    if isinstance(value, str):return runtime.string(value)
    if isinstance(value, float):return Obj('float',value)
    if value is True:return runtime.true
    if value is False:return runtime.false
    return runtime.integer(value)


def model_call(runtime, api, value, path):
    if api == 'close':return runtime.ns['_os_close_entry'](None,Obj('tuple',[value]))
    if api.startswith('open_'):
        args=[runtime.string(str(path)),os.O_RDONLY,0o600,runtime.none]
        args[{'open_flags':1,'open_mode':2,'open_dir_fd':3}[api]]=value
        return runtime.ns['_os_open_entry'](None,Obj('tuple',args))
    if api == 'fdopen_fd':return runtime.fdopen(value,'rb',closefd=False)
    if api == 'fdopen_buffer':return runtime.fdopen(123,'rb',closefd=False,buffering=value)
    args=Obj('tuple',[runtime.string('w+b'),value,runtime.none,runtime.none,
        runtime.none,runtime.none,runtime.string(str(path.parent)),runtime.true,
        runtime.none,runtime.true])
    return runtime.ns['_nt_entry'](None,args)


def oracle_call(api,value,path):
    if api=='close':return os.close(value)
    if api.startswith('open_'):
        args=[path,os.O_RDONLY,0o600];kwargs={}
        if api=='open_dir_fd':kwargs['dir_fd']=value
        else:args[1 if api=='open_flags' else 2]=value
        fd=os.open(*args,**kwargs);os.close(fd);return
    if api=='fdopen_fd':
        with os.fdopen(value,'rb',closefd=False):pass
        return
    if api=='fdopen_buffer':
        fd=os.open(path,os.O_RDONLY)
        try:
            with os.fdopen(fd,'rb',buffering=value,closefd=False):pass
        finally:os.close(fd)
        return
    with tempfile.NamedTemporaryFile(buffering=value,dir=path.parent):pass


@pytest.mark.parametrize('api,value',[(api,value)
    for api in ('close','open_flags','open_mode','open_dir_fd','fdopen_fd','fdopen_buffer','temp_buffer')
    for value in INVALID_VALUES if not (api=='open_dir_fd' and value is None)])
def test_invalid_public_integer_matches_cpython_without_any_descriptor_operation(tmp_path,api,value):
    path=tmp_path/'existing';path.write_bytes(b'keep')
    with pytest.raises((TypeError,OverflowError)) as expected:oracle_call(api,value,path)
    runtime=FileRuntime();events=[]
    runtime.ns['close']=lambda *args:events.append(('close',args)) or 0
    runtime.ns['open_file_flags']=lambda *args:events.append(('open',args)) or 123
    runtime.ns['fd_control']=lambda *args:events.append(('fd_control',args)) or -9
    runtime.ns['mkstemps']=lambda *args:events.append(('mkstemps',args)) or -9
    assert model_call(runtime,api,boxed(runtime,value),path) is None
    assert runtime.error.value[0]=={TypeError:3,OverflowError:15}[type(expected.value)]
    assert events==[] and path.read_bytes()==b'keep' and sorted(p.name for p in tmp_path.iterdir())==['existing']
    assert not runtime.frames and not runtime.leases and not runtime.handles


@pytest.mark.parametrize('api',('close','open_flags','open_mode','open_dir_fd','fdopen_buffer','temp_buffer'))
@pytest.mark.parametrize('failure',('wrong_result','bigint_result','callback_error'))
def test_index_callback_validation_has_real_owners_and_preserves_exception(tmp_path,api,failure):
    runtime=FileRuntime();original=Obj('exception',(2,'callback failure'));calls=[]
    value=runtime.string('wrong') if failure=='wrong_result' else runtime.integer(1<<100)
    operand=runtime.index_object(value,error=original if failure=='callback_error' else None,
                                 callback=lambda:calls.append('index'))
    runtime.ns['close']=lambda *args:pytest.fail('invalid callback reached close')
    runtime.ns['open_file_flags']=lambda *args:pytest.fail('invalid callback reached open')
    runtime.ns['fd_control']=lambda *args:pytest.fail('invalid callback reached descriptor')
    runtime.ns['mkstemps']=lambda *args:pytest.fail('invalid callback created file')
    assert model_call(runtime,api,operand,tmp_path/'unused') is None
    assert calls==['index']
    assert runtime.error is original if failure=='callback_error' else runtime.error.value[0]==(3 if failure=='wrong_result' else 15)
    assert not runtime.frames and not runtime.leases and not runtime.handles


@pytest.mark.parametrize('api',('close','open_flags','open_mode','open_dir_fd'))
@pytest.mark.parametrize('value',(-2147483648,2147483647,False,True))
def test_c_int_endpoints_and_bool_payloads_reach_only_the_intended_syscall(tmp_path,api,value):
    runtime=FileRuntime();calls=[]
    runtime.ns['close']=lambda fd:calls.append(('close',fd)) or 0
    runtime.ns['open_file_flags']=lambda path,flags,mode,dirfd:calls.append(('open',flags,mode,dirfd)) or 123
    result=model_call(runtime,api,boxed(runtime,value),tmp_path/'unused')
    assert runtime.error is None and result is not None
    assert len(calls)==1
    position={'close':1,'open_flags':1,'open_mode':2,'open_dir_fd':3}[api]
    expected=int(value)|(524288 if api=='open_flags' else 0)
    assert calls[0][position]==expected
    assert not runtime.frames and not runtime.leases and not runtime.handles


def test_fdopen_rejects_index_descriptor_without_invoking_it(tmp_path):
    runtime=FileRuntime();calls=[]
    operand=runtime.index_object(0,callback=lambda:calls.append('index'))
    assert model_call(runtime,'fdopen_fd',operand,tmp_path/'unused') is None
    assert runtime.error.value[0]==3 and calls==[]
    class Index:
        def __index__(self):pytest.fail('os.fdopen must reject this before __index__')
    with pytest.raises(TypeError):os.fdopen(Index())


@pytest.mark.parametrize('api',('close','open_flags','open_mode','open_dir_fd','fdopen_buffer','temp_buffer'))
def test_index_callback_success_matches_cpython(tmp_path,api):
    path=tmp_path/'file';path.write_bytes(b'keep')
    # -1 is valid for buffering and mode, and invalid only at the OS fd layer.
    number=-1 if api in ('close','open_dir_fd','fdopen_buffer','temp_buffer') else 0
    class Index:
        def __index__(self):return number
    if api=='close':
        with pytest.raises(OSError):oracle_call(api,Index(),path)
    else:oracle_call(api,Index(),path)
    runtime=FileRuntime();calls=[];operand=runtime.index_object(number,callback=lambda:calls.append('index'))
    events=[]
    runtime.ns['close']=lambda fd:events.append(('close',fd)) or 0
    runtime.ns['open_file_flags']=lambda *args:events.append(('open',args[1:])) or 123
    runtime.ns['fd_control']=lambda *args:events.append(('fd_control',args)) or -9
    runtime.ns['mkstemps']=lambda *args:events.append(('mkstemps',args)) or -9
    model_call(runtime,api,operand,path)
    assert calls==['index'] and len(events)==1
    assert runtime.error is None or runtime.error.value[0] in (14,35)
    assert not runtime.frames and not runtime.leases and not runtime.handles


@pytest.mark.parametrize('method,index,value',[(method,index,value)
    for method,index in (('read',0),('readline',0),('seek',0),('seek',1))
    for value in ('',1.5,None,1<<100) if not (value is None and method in ('read','readline'))])
def test_dynamic_file_method_invalid_index_never_performs_io(tmp_path,method,index,value):
    runtime=FileRuntime();path=tmp_path/'file';path.write_bytes(b'keep')
    fd=os.open(path,os.O_RDONLY);stream=runtime.fdopen(fd,'rb')
    try:
        args=[0]*(index+1);args[index]=boxed(runtime,value)
        method_object=runtime.getattr(stream,runtime.cstr(method))
        runtime.ns['py_file_read']=lambda *args:pytest.fail('invalid read performed IO')
        runtime.ns['py_file_readline']=lambda *args:pytest.fail('invalid readline performed IO')
        runtime.ns['py_file_seek']=lambda *args:pytest.fail('invalid seek performed IO')
        assert runtime.call(method_object,Obj('tuple',args),None) is None
        assert runtime.error.value[0]==(15 if isinstance(value,int) else 3)
        assert os.lseek(fd,0,os.SEEK_CUR)==0
        assert not runtime.frames and not runtime.leases and not runtime.handles
    finally:
        runtime.error=None;runtime.ns['py_file_close_checked'](stream)


def test_none_integer_defaults_remain_supported(tmp_path):
    path=tmp_path/'file';path.write_bytes(b'keep')
    runtime=FileRuntime()
    fd=model_call(runtime,'open_dir_fd',runtime.none,path)
    stream=runtime.fdopen(fd,'rb')
    fn=runtime.getattr(stream,runtime.cstr('read'))
    assert runtime.call(fn,Obj('tuple',[runtime.none]),None).value==b'keep'
    runtime.ns['py_file_close_checked'](stream)
    assert runtime.error is None and not runtime.frames and not runtime.leases


@pytest.mark.parametrize('value',(-2147483648,-1))
def test_negative_fdopen_int_is_value_error_and_never_checks_descriptor(tmp_path,value):
    runtime=FileRuntime();runtime.ns['fd_control']=lambda *args:pytest.fail('negative fd reached descriptor')
    with pytest.raises(ValueError):oracle_call('fdopen_fd',value,tmp_path/'unused')
    assert model_call(runtime,'fdopen_fd',value,tmp_path/'unused') is None
    assert runtime.error.value[0]==2 and not runtime.frames and not runtime.leases


def test_index_error_stops_later_argument_callbacks_and_preserves_path_owner(tmp_path):
    runtime=FileRuntime();events=[];path=runtime.instance(Obj('class'))
    path.cls.attrs['__fspath__']=Obj('func',(lambda c,a:events.append('path') or runtime.string(str(tmp_path/'file')),None))
    original=Obj('exception',(2,'flags failed'))
    flags=runtime.index_object(error=original,callback=lambda:events.append('flags'))
    mode=runtime.index_object(0o600,callback=lambda:events.append('mode'))
    args=Obj('tuple',[path,flags,mode,runtime.none])
    assert runtime.ns['_os_open_entry'](None,args) is None
    assert runtime.error is original and events==['path','flags']
    assert not runtime.frames and not runtime.leases and not runtime.handles


def test_qualified_cpython_bool_descriptor_warning_surface():
    import json
    import subprocess
    import sys
    program='''
import json, os, tempfile, warnings
result={}
for value in (False,True):
    saved=os.dup(int(value))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            os.close(value)
        result['close_'+str(value)]=[item.category.__name__ for item in caught]
    finally:
        os.dup2(saved,int(value));os.close(saved)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        stream=os.fdopen(value,'rb',closefd=False);stream.close()
    result['fdopen_'+str(value)]=[item.category.__name__ for item in caught]
print(json.dumps(result,sort_keys=True))
'''
    result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    observed=json.loads(result.stdout)
    assert observed=={'close_False':[],'close_True':[],
                      'fdopen_False':['RuntimeWarning'],'fdopen_True':['RuntimeWarning']}


@pytest.mark.parametrize('value',(False,True))
def test_fdopen_accepts_bool_descriptor_payload_without_using_index_protocol(tmp_path,value):
    runtime=FileRuntime();events=[]
    runtime.ns['fd_control']=lambda fd,*args:events.append(fd) or -9
    assert model_call(runtime,'fdopen_fd',boxed(runtime,value),tmp_path/'unused') is None
    assert events==[int(value)] and runtime.error.value[0]==14
    assert not runtime.frames and not runtime.leases and not runtime.handles
