"""Structured decode payload, copied raw bytes and Python 3.15 parity."""
from __future__ import annotations
import ast
import os
from pathlib import Path
import subprocess
import sys
import pytest
from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots
from test_unicode_encode_error_payload import UnicodeMemory, _unicode_capi_model
ROOT=Path(__file__).resolve().parents[2]
PHASES=['allocation','string','frame_enter','frame_leave','load_root','field_store','publish','graph_unlock','tuple_set','tuple_get','bytes','raise','bytes_data']

class _BytePointer:
    def __init__(self,owner): self.owner=owner

class DecodeMemory(UnicodeMemory):
    def __init__(self,phase):
        super().__init__(phase)
        self.bytes_fail=False
        self.input_copies=[]
        self.namespace.update(py_bytes_new=self.bytes_new,py_bytes_from_obj=self.bytes_from_obj,py_tuple_from_splat=self.tuple_from_splat)
        self.format_namespace.update(py_bytes_len=lambda o:len(self.read(o,'data')),py_bytes_data_ptr=self.bytes_data)
        self.class_cache=_Slots(65*8)
        self.frames['class_cache']=self.class_cache
        self.maps['py_exc_classes']=self.class_cache
        for tag in (1,2,3,15,19,57,58,59):
            cls=self.make(abi.PY_TYPE_CLASS,65)
            cls.fields['exception_tag']=tag
            self.class_cache.fields[tag*8]=cls
        def lookup(tag):
            self.gc('lookup')
            return self.class_cache.fields[tag*8]
        self.namespace['py_exc_builtin_class']=lookup
        self.namespace['py_exc_matches']=lambda value,cls:int(value.fields[16] is cls)
        self.namespace['py_obj_type_name']=lambda x:self.string({abi.PY_TYPE_STR:'str',abi.PY_TYPE_BYTES:'bytes',abi.PY_TYPE_LIST:'list',abi.PY_TYPE_NONE:'NoneType',abi.PY_TYPE_INT:'int',abi.PY_TYPE_BYTEARRAY:'bytearray',abi.PY_TYPE_MEMORYVIEW:'memoryview'}.get(self.namespace['_type_of'](x),'object'),0)
    def load_byte(self,value,index):
        if isinstance(value,_BytePointer): return self.read(value.owner,'data')[index]
        if isinstance(value,(bytes,bytearray)): return value[index]
        return super().load_byte(value,index)
    def bytes_new(self,data,count):
        self.gc('bytes')
        if self.bytes_fail:
            self.namespace['py_raise_owned'](self.namespace['py_exc_new'](19,'bytes: out of memory'))
            return None
        result=self.make(abi.PY_TYPE_BYTES)
        result.fields['data']=bytes(self.load_byte(data,i) for i in range(count))
        self.input_copies.append(result.fields['data'])
        return result
    def bytes_from_obj(self,value):
        assert value.alive
        raw=self.read(value,'data')
        return self.bytes_new(bytes(raw),len(raw))
    def bytes_data(self,value):
        self.gc('bytes_data')
        assert value.alive,'movable bytes interior crossed projection call'
        return _BytePointer(value)
    def python(self,value):
        if isinstance(value,_Object):
            if value.tag==abi.PY_TYPE_BYTES:return self.read(value,'data')
            if value.tag==abi.PY_TYPE_BYTEARRAY:return bytearray(self.read(value,'data'))
            if value.tag==abi.PY_TYPE_LIST:return [self.python(value.fields[24+i*8]) for i in range(value.fields[16])]
        return super().python(value)
    def tuple_from_splat(self,value):
        try:items=tuple(self.python(value))
        except TypeError as exc:
            self.namespace['py_raise_owned'](self.namespace['py_exc_new'](3,str(exc)))
            return None
        return self.argument_tuple(items)
    def object(self,value):
        if value is None:return self.none
        if isinstance(value,(bytes,bytearray,memoryview)):
            tag={bytes:abi.PY_TYPE_BYTES,bytearray:abi.PY_TYPE_BYTEARRAY,memoryview:abi.PY_TYPE_MEMORYVIEW}[type(value)]
            result=self.make(tag);result.fields['data']=bytes(value);return result
        if isinstance(value,str):return self.string(value,0)
        if isinstance(value,(tuple,list)):
            result=self.argument_tuple(value)
            if isinstance(value,list):result.tag=abi.PY_TYPE_LIST
            return result
        return value
    def argument_tuple(self,values):
        phase,self.phase=self.phase,''
        result=self.tuple_new(len(values))
        for i,value in enumerate(values):
            item=self.object(value);self.tuple_set(result,i,item);self.decref(item)
        self.phase=phase
        return result
    def raise_error(self,error):
        assert error.alive
        super().raise_error(error)
        self.gc('raise')
    def make_decode(self,data=b'a\xff\x00z',start=1,end=2):
        self.namespace['py_unicode_decode_error_from_buffer'](data,len(data),'utf-8',start,end,'invalid start byte')
        assert self.error is not None
        return self.error
    def render(self,mode=0):
        result=self.format_namespace['py_unicode_error_format'](self.error,mode)
        if result is None:return None
        text=self.python(result);self.decref(result);return text

@pytest.mark.parametrize('phase',PHASES)
def test_decode_raw_buffer_copy_and_tls_publication_survive_relocation(phase):
    memory=DecodeMemory(phase);data=bytearray(b'a\xff\x00z');memory.make_decode(data)
    data[:]=b'gone';memory.gc(phase);error=memory.error
    assert error.fields[16].fields['exception_tag']==58
    assert error.flags&abi.PY_FLAG_EXC_UNICODE_PAYLOAD
    payload=error.fields[24];original=payload.fields[24]
    assert memory.python(original)==('utf-8',b'a\xff\x00z',1,2,'invalid start byte')
    assert payload.fields[40] is original.fields[32]
    assert original.references == 1
    assert payload.fields[40].references == 2
    assert payload.references == 1
    assert memory.input_copies==[b'a\xff\x00z']
    assert not memory.graph_depth and not memory.pin_metric
    assert set(memory.frames)=={'error','class_cache'}
    assert error.references==1
    oracle=UnicodeDecodeError('utf-8',b'a\xff\x00z',1,2,'invalid start byte')
    assert memory.render()==str(oracle)
    assert memory.render(1)==repr(oracle)
    assert memory.error.references==1

@pytest.mark.parametrize('data,start,end',[(b'\xff',0,1),(b'\x00',0,1),(b'\x7f',0,1),(b'\xff\xfe',0,2),(b'',0,1),(b'x',-1,0),(b'x',1,2),(b'x',0,0),(b'x',-(2**63),1),(b'x',0,-(2**63))])
@pytest.mark.parametrize('phase',['graph_unlock','bytes_data','frame_leave'])
def test_decode_format_matches_cpython_315(data,start,end,phase):
    memory=DecodeMemory(phase);memory.make_decode(data,start,end)
    oracle=UnicodeDecodeError('utf-8',data,start,end,'invalid start byte')
    assert memory.render()==str(oracle)
    assert memory.render(1)==repr(oracle)
    assert not memory.graph_depth and not memory.pin_metric

@pytest.mark.parametrize('arguments',[(),('utf8',),(7,b'a',0,1,'r'),('utf8','a',0,1,'r'),('utf8',7,0,1,'r'),('utf8',None,0,1,'r'),('utf8',[97],0,1,'r'),('utf8',b'a',2**100,1,'r'),('utf8',b'a',0,1,7)])
def test_decode_constructor_validation_matches_cpython_315(arguments):
    memory=DecodeMemory('graph_unlock');args=memory.argument_tuple(arguments)
    assert memory.namespace['py_unicode_decode_error_new'](args) is None
    with pytest.raises((TypeError,OverflowError)) as caught:UnicodeDecodeError(*arguments)
    assert memory.python(memory.error.fields[24])==str(caught.value)
    assert memory.error.fields[16].fields['exception_tag']==(15 if isinstance(caught.value,OverflowError) else 3)
    assert not memory.graph_depth and not memory.pin_metric

@pytest.mark.parametrize('factory',[bytes,bytearray,memoryview])
@pytest.mark.parametrize('phase',['bytes','graph_unlock','frame_leave','tuple_set'])
def test_decode_buffer_constructor_preserves_args_and_snapshots_object(factory,phase):
    memory=DecodeMemory(phase);args=memory.argument_tuple(('utf8',factory(b'a\xff'),0,1,'r'))
    owner=_Slots(8);owner.fields[0]=args;memory.frames['args']=owner
    error=memory.namespace['py_unicode_decode_error_new'](args);payload=error.fields[24];args=owner.fields[0]
    assert payload.fields[24] is args
    assert payload.fields[40].tag==abi.PY_TYPE_BYTES
    assert memory.python(payload.fields[40])==b'a\xff'
    assert (payload.fields[40] is args.fields[32])==(factory is bytes)
    assert not memory.graph_depth and not memory.pin_metric

@pytest.mark.parametrize('name,index,value',[('encoding',1,'changed'),('object',2,b'new'),('start',3,0),('end',4,1),('reason',5,'different'),('encoding',1,None),('reason',5,42)])
def test_decode_mutable_fields_preserve_constructor_args(name,index,value):
    memory=DecodeMemory('graph_unlock');memory.make_decode();replacement=memory.object(value)
    memory.namespace['py_unicode_error_set_field'](memory.error,index,replacement)
    oracle=UnicodeDecodeError('utf-8',b'a\xff\x00z',1,2,'invalid start byte');setattr(oracle,name,value)
    assert memory.render()==str(oracle)
    assert memory.render(1)==repr(oracle)
    assert memory.python(memory.error.fields[24].fields[24])==oracle.args
    assert not memory.graph_depth and not memory.pin_metric

@pytest.mark.parametrize('value',[(),('one',),('one','two'),['list','args'],'ab'])
@pytest.mark.parametrize('kind',['decode','encode'])
def test_unicode_args_assignment_has_independent_fields_and_cpython_repr(value,kind):
    memory=DecodeMemory('graph_unlock')
    if kind=='decode':
        memory.make_decode();oracle=UnicodeDecodeError('utf-8',b'a\xff\x00z',1,2,'invalid start byte')
    else:
        memory.make_error();oracle=UnicodeEncodeError('ascii','aé€z',1,3,'ordinal not in range(128)')
    replacement=memory.object(value)
    assert memory.namespace['py_unicode_error_set_field'](memory.error,0,replacement)==0
    oracle.args=value
    assert memory.python(memory.error.fields[24].fields[24])==oracle.args
    assert memory.render()==str(oracle)
    assert memory.render(1)==repr(oracle)
    assert not memory.graph_depth and not memory.pin_metric

@pytest.mark.parametrize('value',[bytearray(b'x'),'str',None,7])
def test_decode_invalid_reassigned_object_has_precise_error(value):
    memory=DecodeMemory('graph_unlock');memory.make_decode();replacement=memory.object(value)
    memory.namespace['py_unicode_error_set_field'](memory.error,2,replacement)
    assert memory.render() is None
    assert memory.python(memory.error.fields[24])=="UnicodeError 'object' attribute must be a bytes"
    assert not memory.graph_depth and not memory.pin_metric

def test_decode_copy_failure_preserves_memory_error_and_releases_frames():
    memory=DecodeMemory('graph_unlock');memory.bytes_fail=True;memory.make_decode()
    assert memory.error.fields[16].fields['exception_tag']==19
    assert memory.python(memory.error.fields[24])=='bytes: out of memory'
    assert set(memory.frames)=={'error','class_cache'}
    assert not memory.graph_depth and not memory.pin_metric

def test_decode_capi_sentinel_and_builtin_class_normalize_full_payload():
    for operation in ['PyErr_SetObject','PyErr_SetNone','PyErr_SetString']:
        for direct_class in (False,True):
            memory=DecodeMemory('graph_unlock');_,_,symbols=_unicode_capi_model();namespace=memory.namespace.copy()
            memory.maps['pcc_capi_unicode_string_owned_map']=1
            namespace.update(global_load_ptr=lambda name:symbols.get(name,memory.none),c_abi_typed_export=lambda *_args:lambda fn:fn)
            tree=ast.parse((ROOT/'pcc/runtime/py/py_capi_exc_runtime.py').read_text())
            names={'pcc_capi_exception_tag','pcc_capi_exception_class','_capi_is_unicode_sentinel','_capi_is_unicode_encode_type','_capi_set_unicode_encode_value','_capi_is_unicode_decode_type','_capi_set_unicode_decode_value','PyErr_SetObject','PyErr_SetString','PyErr_SetNone'}
            exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'capi_decode','exec'),namespace)
            type_obj=memory.class_cache.fields[58*8] if direct_class else symbols['PyExc_UnicodeDecodeError']
            if operation=='PyErr_SetObject':
                args=memory.argument_tuple(('utf8',b'\xff',0,1,'r'));namespace[operation](type_obj,args)
                assert memory.error.fields[16].fields['exception_tag']==58
                assert memory.render()==str(UnicodeDecodeError('utf8',b'\xff',0,1,'r'))
            elif operation=='PyErr_SetNone':
                namespace[operation](type_obj)
                assert memory.python(memory.error.fields[24])=='function takes exactly 5 arguments (0 given)'
            else:
                namespace[operation](type_obj,'r')
                assert memory.python(memory.error.fields[24])=='function takes exactly 5 arguments (1 given)'
            assert not memory.graph_depth and not memory.pin_metric

def test_decode_payload_uses_shared_exception_trace_and_update_contract():
    source=(ROOT/'pcc/runtime/py/py_exc_objects.py').read_text()
    assert '_exc_store_constructed_slot(owned, owned, 8, 24)' in source
    assert 'py_exc_new(type_tag, null())' in source
    body=source[source.index('def py_unicode_decode_error_from_buffer('):source.index('@c_abi_export("py_unicode_encode_error")')]
    assert body.index('py_raise_owned(result)')<body.index('_unicode_finish(borrowed, owned, 0)')
    assert 'py_bytes_new(data, count)' in body and 'py_incref(result)' in body

PROGRAM=r'''import gc
def main():
    source=b'a\xff\x00z'
    error=UnicodeDecodeError('utf-8',source,1,2,'invalid start byte')
    print(error.args,error.object is source,str(error),repr(error))
    alias=UnicodeDecodeError
    other=alias('utf-8',bytearray(source),1,3,'r')
    print(other.args,type(other.object).__name__,str(other))
    for category in (UnicodeDecodeError,UnicodeError,ValueError,Exception,BaseException):
        try:raise error
        except category as caught:
            gc.collect()
            print(caught is error,caught.object)
    for args in ((),('one',),['one','two']):
        error.args=args
        gc.collect()
        print(error.args,str(error),repr(error))
    error.object=b'\xfe'
    error.start=0
    error.end=1
    error.encoding=None
    error.reason=42
    print(str(error))
    print('UNICODE_DECODE_PAYLOAD_OK')
main()
'''

@pytest.mark.integration
def test_decode_payload_native_all_collectors(tmp_path,pcc_diagnostic_runtime_archive,python_program_compiler,monkeypatch):
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES','off');path=tmp_path/'unicode_decode_payload.py';path.write_text(PROGRAM)
    oracle=subprocess.run([sys.executable,str(path)],capture_output=True,text=True,timeout=15)
    assert oracle.returncode==0,oracle.stderr
    binary=tmp_path/'unicode_decode_payload'
    python_program_compiler(str(path),str(binary),backend='self',libpython_mode='off',runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        actual=subprocess.run([str(binary)],env=dict(os.environ,PCC_GC_BACKEND=str(backend)),capture_output=True,text=True,timeout=30)
        assert actual.returncode==0,(backend,actual.stdout,actual.stderr)
        assert actual.stdout==oracle.stdout,(backend,actual.stdout,oracle.stdout)


def test_decode_raw_helper_and_constructor_owned_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    import re
    for module in ('py_exc_objects','py_format_runtime','py_capi_exc_runtime'):
        output=tmp_path/(module+'.ll')
        compile_python(str(ROOT/'pcc/runtime/py'/(module+'.py')),str(output),backend='self',libpython_mode='off',emit_llvm_only=True,python_library=True)
        text=output.read_text();verify_ir_text(text)
        assert 'strict.nolib.stub' not in text
        assert not re.search(r'\bcall\b[^\n]*@py_cpy_',text)
        if module=='py_exc_objects':
            assert re.search(r'^define[^\n]*@py_unicode_decode_error_from_buffer\(ptr[^,]*, i64[^,]*, ptr[^,]*, i64[^,]*, i64[^,]*, ptr[^)]*\)',text,re.M)
    source=tmp_path/'decode_constructor.py'
    source.write_text(PROGRAM)
    output=tmp_path/'decode_constructor.ll'
    compile_python(str(source),str(output),backend='self',libpython_mode='off',emit_llvm_only=True,python_library=True)
    text=output.read_text();verify_ir_text(text)
    assert '@py_obj_call(' in text
    assert not re.search(r'\bcall\b[^\n]*@py_exc_new\(i64 58,',text)
