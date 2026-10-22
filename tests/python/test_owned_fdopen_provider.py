"""Execute the actual owned fdopen/FILE bodies over a bounded host ABI model."""
import ast
import errno
import fcntl
import os
import sys
from pathlib import Path

import pytest

from pcc.runtime.freestanding_abi_spec import ABI_SPEC
from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_owned_tempfile_provider import Obj, Ptr, Runtime

ROOT=Path(__file__).resolve().parents[2]


# The model runs the runtime with target_sys_platform() == "linux", so open
# flags arrive in Linux's encoding; translate them for the host's os.open.
_LINUX_OPEN_FLAGS = (
    (0o100, "O_CREAT"), (0o200, "O_EXCL"), (0o400, "O_NOCTTY"), (0o1000, "O_TRUNC"),
    (0o2000, "O_APPEND"), (0o4000, "O_NONBLOCK"), (0o200000, "O_DIRECTORY"),
    (0o400000, "O_NOFOLLOW"), (0o2000000, "O_CLOEXEC"),
)


def _host_open_flags(flags):
    if sys.platform.startswith("linux"):
        return flags
    host = flags & 3  # O_RDONLY/O_WRONLY/O_RDWR are 0/1/2 everywhere
    remaining = flags & ~3
    for bit, name in _LINUX_OPEN_FLAGS:
        if remaining & bit:
            host |= getattr(os, name)
            remaining &= ~bit
    if remaining:
        raise AssertionError(f"untranslated Linux open flags {remaining:#o}")
    return host


class FileRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.errno=0
        self.ns.update({
            'abi_constant':lambda key:ABI_SPEC[key],
            'define_global_i64_array':self.define_array,
            'define_global_ptr_to_global':lambda a,b:self.global_store(a,self.global_addr(b)),
            'atomic_cas_i32':self.cas,'atomic_store_i32':lambda p,n,v,*_:self.write(p,n,v),
            'fd_control':self.fd_control,'seek_file':self.seek,'close':self.close,
            'read':self.read_fd,'write':self.write_fd,
            'pcc_errno_set':lambda value:setattr(self,'errno',value),
            'pcc_errno_get':lambda:self.errno,'pcc_errno_message_into':self.errno_message,
            'ptr_diff':lambda a,b:0 if a is b else 1,
            'unsigned_div_i64':lambda a,b:a//b,'wrapping_mul_i64':lambda a,b:a*b,
            'mul_overflow_i64':lambda a,b:int(a*b>2**63-1),
            'pcc_platform_abort':lambda:pytest.fail('owned runtime aborted'),
            'pcc_gc_alloc':self.alloc_object,
            'pcc_gc_root_copy_borrowed_lease':self.copy_lease,
            'pcc_gc_foreign_lease_acquire':self.acquire,
            'py_bool_from_bit':lambda bit:self.true if bit else self.false,
            'py_text_codec_id':lambda obj:0 if obj.value.lower() in ('utf-8','utf8') else 1,
            'py_text_error_id':lambda obj:0 if obj.value=='strict' else 3,
            'py_text_encode_ids':lambda obj,*_:Obj('bytes',obj.value.encode('utf-8')),
            'py_bytes_from_obj':lambda obj:obj,
            'py_bytes_new':lambda source,size:Obj('bytes',self.raw_count(source,size)),
            'py_str_byte_len':lambda obj:len(obj.value.encode('utf-8')),
            'py_str_len':lambda obj:len(obj.value),
            'strcmp':lambda a,b:0 if self.raw(a)==self.raw(b) else 1,
            'py_func_new_bound':lambda entry,cap,name,owner:Obj('func',(entry,cap)),
            'ptr_add':self.ptr_add, 'realloc':self.reallocate,
            'load_i32':lambda p,n:self.read(p,n) or 0,
            'load_i64':lambda p,n:self.read(p,n) or 0,
            'py_instance_new':self.instance,
            'py_builtin_callable':lambda obj:self.true if isinstance(obj,Obj) and obj.kind in ('func','class') else self.false,
            'py_exc_builtin_class':lambda tag:Obj('class',tag),
            'py_exc_matches':lambda error,cls:int(error.value[0]==cls.value),
            'mkstemps':self.ns['pcc_platform_mkstemp_suffix'],
            'open_file_flags':self.open_file_flags,
            'py_obj_special_call_slots':self.special_call,
        })
        self.load(ROOT/'pcc/runtime/py/freestanding_stdio.py')
        self.load(ROOT/'pcc/runtime/py/py_file.py')
        self.load_selected(ROOT/'pcc/runtime/py/freestanding_errno.py',
                           {'pcc_errno_exception_kind'})
        self.install_integer_runtime()

    def load_selected(self, path, names, constants=()):
        kept=[]
        for node in ast.parse(path.read_text()).body:
            if isinstance(node,ast.FunctionDef) and node.name in names:
                node.decorator_list=[];kept.append(node)
            elif isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant):
                if any(isinstance(t,ast.Name) and t.id.startswith(constants) for t in node.targets):
                    kept.append(node)
        exec(compile(ast.Module(kept,type_ignores=[]),str(path),'exec'),self.ns)

    def install_integer_runtime(self):
        # These are the production conversion/protocol bodies. In particular,
        # py_int_value_i64 is not an identity conversion or a type validator.
        self.ns.update({
            'untag_int':lambda value:value,
            'pcc_capi_is_cext_type_tag':lambda tag:0,
            'pcc_gc_note_slot_write_barrier':lambda *args:None,
            'py_runtime_error_if_unset':self.error_if_unset,
        })
        self.load_selected(ROOT/'pcc/runtime/py/py_int_core.py',
            {'_bigint_i64_clamped','py_int_value_i64','_load_u32'})
        self.load_selected(ROOT/'pcc/runtime/py/py_int_convert.py',
            {'_set_overflow','_load_u32','py_int_to_i64'})
        self.load_selected(ROOT/'pcc/runtime/py/py_protocol_runtime.py',
            {'_index_slot_error','_index_slot_open','_index_slot_copy','_index_slot_adopt',
             '_index_slot_close','_index_slot_dispatch','_index_slot_checked',
             'py_index_i64_checked_slots'}, ('_INDEX_',))

    def error_if_unset(self, context, message):
        if self.error is None:self.error=Obj('exception',(7,self.raw(message).decode()))

    def integer(self,value):
        if -(1<<62)<=value<(1<<62):return value
        magnitude=abs(value);limbs=[]
        while magnitude:
            limbs.append(magnitude&0xffffffff);magnitude>>=32
        out=self.alloc(abi.PYINTOBJECT_DIGITS_OFFSET+4*len(limbs))
        self.write(out,abi.PYOBJECTHEADER_TYPE_TAG_OFFSET,abi.PY_TYPE_INT)
        self.write(out,abi.PYINTOBJECT_SIGN_OFFSET,1 if value>0 else -1)
        self.write(out,abi.PYINTOBJECT_NDIGITS_OFFSET,len(limbs))
        for i,limb in enumerate(limbs):self.write(out,abi.PYINTOBJECT_DIGITS_OFFSET+4*i,limb)
        return out

    def index_object(self,value=None,error=None,callback=None):
        obj=Obj('instance');obj.cls=Obj('class')
        def index(captures,args):
            assert self.leases.get(id(obj),0)>0, '__index__ receiver has no counted lease'
            assert self.handles, '__index__ receiver has no registered root'
            if callback is not None:callback()
            if error is not None:self.error=error;return None
            return value
        obj.cls.attrs['__index__']=Obj('func',(index,None))
        return obj

    def reallocate(self,source,size):
        destination=self.alloc(size)
        if source is not None:
            count=min(len(source.memory.data)-source.offset,size)
            destination.memory.data[:count]=source.memory.data[source.offset:source.offset+count]
            self.free(source)
        return destination

    def instance(self,cls):
        obj=Obj('instance');obj.cls=cls;return obj

    def special_call(self,receiver_slot,name,args_slot,kwargs_slot,result_slot,handled):
        receiver=self.read(receiver_slot,0);cls=getattr(receiver,'cls',None)
        key=self.raw(name).decode();fn=cls.attrs.get(key) if cls else None
        if fn is None:
            self.write(handled,0,0);return 0
        self.write(handled,0,1)
        entry,captures=fn.value
        result=entry(captures,Obj('tuple',[receiver]))
        self.write(result_slot,0,result)
        return -1 if self.error is not None else 0

    def getattr(self,obj,name):
        if isinstance(obj,Ptr):
            return self.ns['py_file_getattr'](obj,name)
        key=self.raw(name).decode()
        if isinstance(obj,Obj):
            if key in obj.attrs:return obj.attrs[key]
            cls=getattr(obj,'cls',None)
            if cls and key in cls.attrs:
                entry,captures=cls.attrs[key].value
                return Obj('func',(entry,captures,obj))
            if cls and '__getattr__' in cls.attrs:
                entry,captures=cls.attrs['__getattr__'].value
                return entry(captures,Obj('tuple',[obj,self.string(key)]))
        self.error=Obj('exception',(6,key));return None

    def open_file_flags(self,path,flags,permissions,dir_fd):
        try:
            return os.open(self.raw(path),_host_open_flags(flags),permissions,
                           dir_fd=None if dir_fd==-100 else dir_fd)
        except OSError as error:return -error.errno

    def define_array(self,name,*values):
        pointer=self.alloc(8*len(values))
        for i,value in enumerate(values):self.write(pointer,i*8,value)
        self.globals['address:'+name]=pointer

    def alloc_object(self,size,tag,_):
        pointer=self.alloc(size);self.write(pointer,0,1);self.write(pointer,8,tag);self.write(pointer,12,0)
        return pointer

    def ptr_add(self,p,n):
        if isinstance(p,Obj) and p.kind=='bytes':
            assert n==abi.PYBYTESOBJECT_DATA_OFFSET
            out=self.alloc(len(p.value));out.memory.data[:]=p.value;return out
        return Ptr(p.memory,p.offset+n)

    def read(self,p,n):
        if isinstance(p,Obj) and n==abi.PYOBJECTHEADER_TYPE_TAG_OFFSET and p.kind in ('bool','none','float'):
            return {'bool':abi.PY_TYPE_BOOL,'none':abi.PY_TYPE_NONE,'float':abi.PY_TYPE_FLOAT}[p.kind]
        if isinstance(p,Obj) and p.kind=='bytes' and n==abi.PYBYTESOBJECT_BYTE_LEN_OFFSET:
            return len(p.value)
        return super().read(p,n)

    def acquire(self,slot):
        key=id(self.read(slot,0));self.leases[key]=self.leases.get(key,0)+1;return 1

    def copy_lease(self,d,s):
        if self.read(s,0) is None:
            self.write(d,0,None);return 0
        return super().copy_lease(d,s)

    def release_lease(self,s,t):
        return 0 if t==0 else super().release_lease(s,t)

    def fd_control(self,fd,command,value):
        try:return fcntl.fcntl(fd,command,value)
        except OSError as error:return -error.errno

    def seek(self,fd,offset,whence):
        try:return os.lseek(fd,offset,whence)
        except OSError as error:return -error.errno

    def close(self,fd):
        try:os.close(fd);return 0
        except OSError as error:self.errno=error.errno;return -error.errno

    def read_fd(self,fd,out,count):
        try:data=os.read(fd,count)
        except OSError as error:return -error.errno
        out.memory.data[out.offset:out.offset+len(data)]=data;return len(data)

    def write_fd(self,fd,src,count):
        try:return os.write(fd,self.raw_count(src,count))
        except OSError as error:return -error.errno

    def fdopen(self,fd,mode='w',closefd=True,buffering=-1):
        args=Obj('tuple',[fd,self.string(mode),buffering,self.string('utf-8') if 'b' not in mode else self.none,
                          self.none,self.none,self.true if closefd else self.false,self.none])
        return self.ns['_fdopen_entry'](None,args)


def test_fdopen_adopts_without_truncation_and_closes_once(tmp_path):
    runtime=FileRuntime();path=tmp_path/'file';path.write_bytes(b'keep')
    fd=os.open(path,os.O_RDWR)
    stream=runtime.fdopen(fd)
    assert runtime.error is None and stream is not None
    assert runtime.ns['py_file_write'](stream,runtime.string('X'))==1, runtime.error.value if runtime.error else None
    runtime.ns['py_file_close_checked'](stream)
    assert path.read_bytes()==b'Xeep'
    with pytest.raises(OSError):os.fstat(fd)
    runtime.ns['py_file_close_checked'](stream)
    assert runtime.error is None
    assert runtime.ns['py_file_getattr'](stream,runtime.cstr('name'))==fd
    assert runtime.ns['py_file_getattr'](stream,runtime.cstr('closed')) is runtime.true
    assert not runtime.frames and not runtime.leases


def test_fdopen_borrowed_descriptor_and_append(tmp_path):
    runtime=FileRuntime();path=tmp_path/'file';path.write_bytes(b'keep')
    fd=os.open(path,os.O_RDWR)
    try:
        stream=runtime.fdopen(fd,'a',closefd=False)
        runtime.ns['py_file_write'](stream,runtime.string('X'))
        runtime.ns['py_file_close_checked'](stream)
        assert path.read_bytes()==b'keepX' and os.fstat(fd)
    finally:os.close(fd)
    assert not runtime.frames and not runtime.leases


def test_fdopen_invalid_descriptor_preserves_errno():
    runtime=FileRuntime()
    assert runtime.fdopen(999999) is None
    assert runtime.error.value[0]==14 and runtime.error.attrs['errno']==errno.EBADF
    assert not runtime.frames and not runtime.leases


def test_errno_metadata_failure_preserves_allocation_exception():
    runtime=FileRuntime();error=Obj('exception',(19,'metadata allocation'))
    def fail_metadata(*args):runtime.error=error;return -1
    runtime.ns['py_obj_setattr']=fail_metadata
    assert runtime.fdopen(999999) is None
    assert runtime.error is error and not runtime.frames and not runtime.leases


@pytest.mark.parametrize('mode',('rr','w++','rbt','', 'q'))
def test_fdopen_invalid_mode_does_not_take_descriptor(tmp_path,mode):
    runtime=FileRuntime();fd=os.open(tmp_path/'file',os.O_CREAT|os.O_RDWR,0o600)
    try:
        assert runtime.fdopen(fd,mode) is None
        assert runtime.error.value[0]==2 and os.fstat(fd)
    finally:os.close(fd)
    assert not runtime.frames and not runtime.leases


def test_fdopen_signature_is_canonical_and_contains_forwarded_options():
    runtime=FileRuntime();fn=runtime.ns['py_file_fdopen_function']()
    assert fn is runtime.ns['py_file_fdopen_function']()
    signature=fn.value[1].value[1]
    assert [n.value for n in signature.value[1].value]==[
        'fd','mode','buffering','encoding','errors','newline','closefd','opener']
    assert signature.value[3].value==[runtime.false]+[runtime.true]*7
    assert not runtime.frames and not runtime.leases


def test_dynamic_file_methods_publish_before_releasing_captures(tmp_path):
    runtime=FileRuntime();path=tmp_path/'file';fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    stream=runtime.fdopen(fd)
    write=runtime.ns['py_file_getattr'](stream,runtime.cstr('write'))
    close=runtime.ns['py_file_getattr'](stream,runtime.cstr('close'))
    entry,captures=write.value
    assert entry(captures,Obj('tuple',[runtime.string('owned')]))==5
    entry,captures=close.value
    assert entry(captures,Obj('tuple',[])) is runtime.none
    assert path.read_bytes()==b'owned'
    assert not runtime.frames and not runtime.leases


def test_fdopen_reordered_binary_mode_and_context_identity(tmp_path):
    runtime=FileRuntime();fd=os.open(tmp_path/'file',os.O_CREAT|os.O_RDWR,0o600)
    stream=runtime.fdopen(fd,'br+')
    enter=runtime.ns['py_file_getattr'](stream,runtime.cstr('__enter__'))
    entry,captures=enter.value
    assert entry(captures,Obj('tuple',[])) is stream
    exit=runtime.ns['py_file_getattr'](stream,runtime.cstr('__exit__'))
    entry,captures=exit.value
    assert entry(captures,Obj('tuple',[runtime.none]*3)) is runtime.false
    with pytest.raises(OSError):os.fstat(fd)
    assert not runtime.frames and not runtime.leases
