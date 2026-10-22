"""Exercise the actual tempfile runtime bodies with owned filesystem controls.

The model supplies raw memory/managed ABI operations; the provider's constructor,
weak finalizer, cleanup and context methods run unchanged. Native execution is a
separate qualification and is not implied by these model checks.
"""
from __future__ import annotations

import ast
import gc
import os
from pathlib import Path
import weakref

import pytest

from pcc.runtime.py import py_abi_constants as abi

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'pcc/runtime/py/py_tempfile.py'
PLATFORM = ROOT / 'pcc/runtime/py/freestanding_platform_fs.py'


class Memory:
    def __init__(self, size=0):
        self.data = bytearray(size)
        self.cells = {}
        self.freed = False


class Ptr:
    def __init__(self, memory, offset=0):
        self.memory, self.offset = memory, offset


class Obj:
    def __init__(self, kind, value=None):
        self.kind, self.value = kind, value
        self.flags, self.pins, self.attrs = 0, 0, {}
        self.methods = {}


class Runtime:
    def __init__(self):
        self.globals = {}
        self.exports = {}
        self.handles = {}
        self.frames = []
        self.error = None
        self.exits = []
        self.memory = []
        self.fail_state = False
        self.fail_setattr = False
        self.leases = {}
        self.remove_failure = 0
        self.none = Obj('none')
        self.true, self.false = Obj('bool', True), Obj('bool', False)
        self.globals.update(py_None=self.none, py_True=self.true, py_False=self.false)
        ns = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
        ns.update({
            'c_ptr': object, 'i64': int, 'null': lambda: None,
            'ptr_is_null': lambda x: int(x is None), 'ptr_eq': lambda a,b: int(a is b),
            'is_tagged_int': lambda x: isinstance(x,int),
            'ptr_add': lambda p,n: Ptr(p.memory,p.offset+n),
            'malloc': self.alloc, 'stack_alloc': self.alloc, 'free': self.free,
            'memcpy': self.memcpy, 'memset': self.memset, 'cstr': self.cstr,
            'strlen': lambda p: len(self.raw(p)),
            'load_ptr': self.read, 'store_ptr': self.write,
            'load_i32': self.read, 'load_i64': self.read,
            'store_i32': self.write, 'store_i64': self.write,
            'load_i8': self.byte, 'store_i8': self.store_byte,
            'global_load_ptr': self.global_load,
            'global_store_ptr': self.global_store,
            'global_addr': self.global_addr,
            'define_global_i32': lambda k,v:self.define(k,v),
            'define_global_i64': lambda k,v:self.define(k,v),
            'define_global_ptr_null': lambda k:self.globals.setdefault(k,None),
            'atomic_load_i64': lambda p,n,*_: self.read(p,n),
            'atomic_cas_i64': self.cas, 'atomic_rmw_i64': self.rmw,
            'int_to_ptr': lambda n: n, 'ptr_to_int': lambda p:p,
            'target_sys_platform': lambda:self.cstr('linux'),
            'function_addr':lambda name:self.exports[name],
            'pcc_mutex_new': lambda:self.alloc(8), 'pcc_mutex_free': self.free,
            'pcc_mutex_lock': lambda p:0, 'pcc_mutex_unlock':lambda p:0,
            'pcc_gc_pin': self.pin, 'pcc_gc_unpin': self.unpin,
            'pcc_gc_frame_enter': lambda m,s:self.frames.append(s),
            'pcc_gc_frame_leave': lambda s:self.frames.remove(s),
            'pcc_gc_store_root': lambda s,v:self.write(s,0,v),
            'pcc_gc_take_pinned_slot': self.take,
            'pcc_gc_note_write_barrier': lambda *args:None,
            'pcc_gc_load_ptr': lambda owner,p:self.read(p,0),
            'pcc_py_gc_minor_graph_lock':lambda:None,
            'pcc_py_gc_minor_graph_unlock':lambda:None,
            'pcc_gc_scheduler_root_register_handle': self.register,
            'pcc_gc_scheduler_root_unregister_handle': lambda h:self.handles.pop(h),
            'pcc_gc_root_copy_lease':self.copy_lease,
            'pcc_gc_foreign_lease_release':self.release_lease,
            'py_incref':lambda x:None, 'py_decref':lambda x:None,
            'py_err_occurred':lambda:int(self.error is not None),
            'py_exc_new':lambda tag,msg:Obj('exception',(tag,self.raw(msg).decode())),
            'py_raise_owned':lambda exc:setattr(self,'error',exc),
            'py_clear_exception':lambda:setattr(self,'error',None),
            'py_tls_exc_swap_slot':self.swap_error,
            'py_str_new':lambda p,n:Obj('str',self.raw_count(p,n).decode()),
            'py_str_utf8':lambda o:self.cstr(o.value),
            'py_obj_type_name':lambda o:Obj('str','int' if isinstance(o,int) else
                                            'NoneType' if o is None else o.kind),
            'py_str_concat':lambda a,b:Obj('str',a.value+b.value),
            'py_obj_repr':lambda o:Obj('str',repr(o.value)),
            'py_obj_truthy':lambda o:int(bool(o.value)),
            'py_int_from_i64':lambda n:n, 'py_int_value_i64':lambda n:n,
            'py_list_new':self.new_list, 'py_list_get':self.list_get,
            'py_list_len':lambda o:len(o.value),
            'py_list_set':self.list_set, 'py_list_append':self.list_append,
            'py_list_setitem':self.list_setitem,
            'py_tuple_new':lambda n:Obj('tuple',[None]*n),
            'py_tuple_get':lambda o,i:o.value[i],
            'py_tuple_len':lambda o:len(o.value),
            'py_tuple_set_item':lambda o,i,v:o.value.__setitem__(i,v),
            'py_obj_getattr':self.getattr,
            'py_obj_call':self.call,
            'py_obj_setattr':self.setattr,
            'py_class_new':lambda *args:Obj('class'),
            'py_class_setattr':self.setattr,
            'py_class_add_method':lambda cls,name,func:cls.methods.__setitem__(self.raw(name).decode(),func),
            'py_func_new_named':lambda entry,cap,name:Obj('func',(entry,cap)),
            'py_weakref_new':self.new_weakref,
            'py_os_path_abspath':lambda o:Obj('str',os.path.abspath(o.value)),
            'py_os_path_exists':lambda o:int(os.path.exists(o.value)),
            'getenv':lambda name:None,
            'atexit':lambda callback:self.exits.append(callback) or 0,
            'errno_message':self.errno_message,
            'access':self.access, 'getpid':lambda:123,
            'mkdir':self.mkdir, 'stat_kind':self.stat_kind,
            'readlink':self.readlink, 'unlinkat':self.unlinkat,
            'directory_open':self.directory_open,
            'directory_next':self.directory_next, 'directory_error':lambda s:0,
            'directory_close':lambda s:None, 'chmod_file':self.chmod,
            'logical_shift_right_i64':lambda n,b:(n&((1<<64)-1))>>b,
        })
        self.ns=ns
        self.load(PLATFORM)
        ns['mkdtemps']=ns['pcc_platform_mkdtemp_suffix']
        ns['create_owned']=ns['pcc_platform_tempdir_create_owned']
        ns['remove_owned']=ns['pcc_platform_tempdir_remove_owned']
        ns['close_owned']=ns['pcc_platform_tempdir_close_owned']
        ns['remove_tree']=self.remove_tree
        self.load(SOURCE)

    def load(self,path):
        tree=ast.parse(path.read_text())
        kept=[]
        exports={}
        for node in tree.body:
            if isinstance(node,ast.FunctionDef):
                for decorator in node.decorator_list:
                    if (isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Name)
                            and decorator.func.id=='c_abi_export'):
                        exports[decorator.args[0].value]=node.name
                node.decorator_list=[]; kept.append(node)
            elif isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant):
                kept.append(node)
            elif isinstance(node,ast.Expr) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Name) and node.value.func.id.startswith('define_global_'):
                kept.append(node)
        exec(compile(ast.Module(kept,type_ignores=[]),str(path),'exec'),self.ns)
        self.exports.update({symbol:self.ns[name] for symbol,name in exports.items()})

    def alloc(self,n):
        m=Memory(n);self.memory.append(m);return Ptr(m)
    def free(self,p):
        if p is not None:
            assert not p.memory.freed; p.memory.freed=True;p.memory.cells.clear()
    def cstr(self,s):
        p=self.alloc(len(s.encode())+1);p.memory.data[:]=s.encode()+b'\0';return p
    def raw(self,p):
        return bytes(p.memory.data[p.offset:]).split(b'\0',1)[0]
    def raw_count(self,p,n):
        return bytes(p.memory.data[p.offset:p.offset+n]) if p is not None else b'\0'*n
    def byte(self,p,n):return p.memory.data[p.offset+n]
    def store_byte(self,p,n,v):p.memory.data[p.offset+n]=v&255
    def read(self,p,n):
        if isinstance(p,Obj):
            if n==12:return p.flags
            if n==8:return {'str':abi.PY_TYPE_STR,'list':abi.PY_TYPE_LIST,'bytes':abi.PY_TYPE_BYTES}.get(p.kind,1000)
            if n==16:return len(p.value.encode())
        return p.memory.cells.get(p.offset+n,0 if n!=0 else None)
    def write(self,p,n,v):
        if isinstance(p,Obj):assert n==12;p.flags=v;return
        p.memory.cells[p.offset+n]=v
    def memset(self,p,v,n):
        p.memory.data[p.offset:p.offset+n]=bytes([v])*n
        for offset in range(0,n,8):self.write(p,offset,None)
    def memcpy(self,d,s,n):d.memory.data[d.offset:d.offset+n]=self.raw_count(s,n)
    def global_addr(self,k):
        key='address:'+k
        if key not in self.globals:self.globals[key]=self.alloc(8);self.write(self.globals[key],0,self.globals.get(k,0))
        return self.globals[key]
    def global_load(self,k):
        address=self.globals.get('address:'+k)
        return self.read(address,0) if address is not None else self.globals.get(k)
    def global_store(self,k,v):
        self.globals[k]=v
        address=self.globals.get('address:'+k)
        if address is not None:self.write(address,0,v)
    def define(self,k,v):self.globals[k]=v
    def cas(self,p,n,old,new,*_):
        was=self.read(p,n)
        if was==old:self.write(p,n,new)
        return was
    def rmw(self,op,p,n,v,*_):was=self.read(p,n);self.write(p,n,was+v);return was
    def pin(self,o):
        if isinstance(o,Obj):o.pins+=1;o.flags|=64
    def unpin(self,o):
        if isinstance(o,Obj):
            assert o.pins>0,(o.kind,o.value,'unbalanced pin')
            o.pins-=1;o.flags&=~64
    def take(self,p,prior):
        o=self.read(p,0);self.write(p,0,None)
        self.unpin(o)
        if isinstance(o,Obj) and prior:o.flags|=64
        return o
    def register(self,p):h=object();self.handles[h]=p;return h
    def copy_lease(self,d,s):
        o=self.read(s,0);self.write(d,0,o)
        key=id(o);self.leases[key]=self.leases.get(key,0)+1
        return 1
    def release_lease(self,s,t):
        assert t==1
        key=id(self.read(s,0));assert self.leases[key]>0
        self.leases[key]-=1
        if not self.leases[key]:del self.leases[key]
        return 0
    def swap_error(self,s):previous=self.read(s,0);self.write(s,0,self.error);self.error=previous
    def new_list(self,n):
        # py_list_new reserves capacity; its initial logical length is zero.
        if self.fail_state:return None
        result=Obj('list',[]);result.capacity=max(4,n);return result
    def list_get(self,o,i):
        if i<0:i+=len(o.value)
        return o.value[i] if 0<=i<len(o.value) else None
    def list_set(self,o,i,v):
        # Internal py_list_set is deliberately non-raising on invalid indices.
        if i<0:i+=len(o.value)
        if 0<=i<len(o.value):o.value[i]=v
    def list_setitem(self,o,i,v):
        if i<0:i+=len(o.value)
        if 0<=i<len(o.value):o.value[i]=v;return 0
        self.error=Obj('exception',(5,'list assignment index out of range'));return -1
    def list_append(self,o,v):
        o.value.append(v);o.capacity=max(o.capacity,len(o.value))
    def getattr(self,o,name):
        name=self.raw(name).decode()
        if name in o.attrs:return o.attrs[name]
        if o.kind=='instance' and name=='cleanup':
            return Obj('func',(self.ns['_td_cleanup'],None,o))
        raise AttributeError(name)
    def call(self,fn,args,kwargs):
        entry,captures,*bound=fn.value
        return entry(captures,Obj('tuple',bound+args.value))
    def setattr(self,o,name,v):
        if self.fail_setattr and o.kind=='instance':self.error=Obj('exception',(19,'injected setattr'));return -1
        o.attrs[self.raw(name).decode()]=v;return 0
    def new_weakref(self,target,callback):
        def call(_):
            saved=self.error;self.error=None
            callback.value[0](callback.value[1],Obj('tuple',[self.none]))
            self.error=saved
        return Obj('weakref',weakref.ref(target,call))
    def path_call(self,func,*args):
        try:return func(*args) or 0
        except OSError as exc:return -exc.errno
    def errno_message(self,number,buffer,size):
        data=os.strerror(number).encode()[:size-1]+b"\0"
        buffer.memory.data[:len(data)]=data
        return 0
    def mkdir(self,p,mode):return self.path_call(os.mkdir,self.raw(p),mode)
    def chmod(self,p,mode):return self.path_call(os.chmod,self.raw(p),mode)
    def access(self,p,mode):return 0 if os.path.exists(self.raw(p)) else -2
    def stat_kind(self,p):
        path=self.raw(p);return 2 if os.path.isdir(path) else 1 if os.path.exists(path) else 0
    def readlink(self,p,buffer,size):
        try:data=os.fsencode(os.readlink(self.raw(p)))
        except OSError as exc:return -exc.errno
        buffer.memory.data[buffer.offset:buffer.offset+min(size,len(data))]=data[:size];return len(data)
    def unlinkat(self,p,directory):return self.path_call(os.rmdir if directory else os.unlink,self.raw(p))
    def directory_open(self,p):
        try:return iter(os.listdir(self.raw(p)))
        except OSError:return None
    def directory_next(self,it):
        try:return self.cstr(os.fsdecode(next(it)))
        except StopIteration:return None
    def remove_tree(self,p):return self.remove_failure or self.ns['pcc_platform_tempdir_remove_tree'](p)
    def string(self,s):return Obj('str',s)
    def create(self,root,prefix='pcc_owned_',suffix='',delete=True,ignore=False):
        manager=Obj('instance')
        args=Obj('tuple',[manager,self.string(suffix),self.string(prefix),self.string(str(root)),self.true if ignore else self.false,self.true if delete else self.false])
        self.ns['_td_init'](None,args)
        assert not self.frames
        return manager
    def method(self,manager,name,*args):
        result=self.ns['_td_'+name](None,Obj('tuple',[manager,*args]))
        assert not self.frames
        return result
    def name(self,manager):return manager.attrs['name'].value


@pytest.fixture
def runtime():
    model=Runtime()
    yield model
    model.error=None;model.remove_failure=0
    model.ns['_td_shutdown']()
    retained={model.global_load(name) for name in (
        'pcc_tempfile_mkdtemp_root_handle','pcc_tempdir_class_root_handle')}
    retained.discard(None)
    assert set(model.handles)==retained
    assert not model.frames
    assert not model.leases


def test_manager_enter_name_identity_and_repeat_cleanup(runtime,tmp_path):
    manager=runtime.create(tmp_path)
    name=manager.attrs['name'];assert Path(name.value).is_dir()
    assert runtime.method(manager,'enter') is name
    assert runtime.method(manager,'cleanup') is runtime.none
    assert not Path(name.value).exists()
    assert runtime.method(manager,'cleanup') is runtime.none
    assert manager.attrs['name'] is name


def test_cleanup_removes_recreated_directory(runtime,tmp_path):
    manager=runtime.create(tmp_path)
    runtime.method(manager,'cleanup');Path(runtime.name(manager)).mkdir()
    runtime.method(manager,'cleanup');assert not Path(runtime.name(manager)).exists()


@pytest.mark.parametrize('delete',(True,False))
def test_exit_delete_policy_and_explicit_cleanup(runtime,tmp_path,delete):
    manager=runtime.create(tmp_path,delete=delete)
    assert runtime.method(manager,'exit',runtime.none,runtime.none,runtime.none) is runtime.none
    assert Path(runtime.name(manager)).exists() is (not delete)
    runtime.method(manager,'cleanup');assert not Path(runtime.name(manager)).exists()


def test_weak_finalizer_does_not_retain_manager(runtime,tmp_path):
    manager=runtime.create(tmp_path);name=runtime.name(manager)
    ref=weakref.ref(manager)
    del manager;gc.collect()
    assert ref() is None
    assert not Path(name).exists()
    assert not runtime.handles


def test_alias_retains_resource_until_last_reference(runtime,tmp_path):
    manager=runtime.create(tmp_path);name=runtime.name(manager);alias=manager
    del manager;gc.collect();assert Path(name).is_dir()
    del alias;gc.collect();assert not Path(name).exists()


def test_implicit_finalizer_captures_original_name(runtime,tmp_path):
    manager=runtime.create(tmp_path);original=runtime.name(manager)
    untouched=tmp_path/'untouched';untouched.mkdir()
    manager.attrs['name']=runtime.string(str(untouched))
    del manager;gc.collect()
    assert not Path(original).exists();assert untouched.is_dir()


def test_suffix_empty_prefix_and_relative_directory(runtime,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager=runtime.create('.',prefix='',suffix='_tail')
    name=runtime.name(manager)
    assert os.path.isabs(name) and Path(name).name.endswith('_tail')
    assert len(Path(name).name)==11


@pytest.mark.parametrize('failure',(False,True))
def test_constructor_failure_rolls_back_directory(runtime,tmp_path,failure):
    runtime.fail_state=not failure;runtime.fail_setattr=failure
    runtime.create(tmp_path,delete=False)
    assert runtime.error is not None and runtime.error.value[0]==19
    assert list(tmp_path.iterdir())==[]
    assert not runtime.handles


@pytest.mark.parametrize('ignore',(False,True))
def test_cleanup_error_policy_detaches_and_can_retry(runtime,tmp_path,ignore):
    manager=runtime.create(tmp_path,ignore=ignore)
    runtime.remove_failure=-13
    runtime.method(manager,'cleanup')
    assert (runtime.error is None) is ignore
    assert not runtime.handles
    runtime.error=None;runtime.remove_failure=0
    runtime.method(manager,'cleanup');assert not Path(runtime.name(manager)).exists()


def test_root_symlink_is_not_followed_or_unlinked(runtime,tmp_path):
    manager=runtime.create(tmp_path);name=Path(runtime.name(manager))
    name.rmdir();target=tmp_path/'target';target.mkdir();(target/'keep').write_text('keep')
    name.symlink_to(target,target_is_directory=True)
    runtime.method(manager,'cleanup')
    assert runtime.error is not None
    assert name.is_symlink() and (target/'keep').read_text()=='keep'
    name.unlink()


def test_shutdown_cleans_live_managers(runtime,tmp_path):
    first=runtime.create(tmp_path);second=runtime.create(tmp_path,delete=False)
    runtime.ns['_td_shutdown']()
    assert not Path(runtime.name(first)).exists()
    assert Path(runtime.name(second)).is_dir()
    runtime.method(second,'cleanup')


def test_missing_parent_raises_instead_of_empty_name(runtime,tmp_path):
    manager=runtime.create(tmp_path/'missing')
    assert runtime.error is not None and runtime.error.value[0]==34
    assert 'name' not in manager.attrs


def test_canonical_type_has_real_binder_and_methods(runtime):
    cls=runtime.ns['py_tempdir_type']()
    assert cls is runtime.ns['py_tempdir_type']()
    assert {'__init__','__enter__','__exit__','cleanup','__repr__'} <= cls.attrs.keys()
    # Construction finds __init__ in the method table (py_class_lookup).
    assert cls.methods == {'__init__': cls.attrs['__init__']}
    signature=cls.attrs['__init__'].value[1].value[1]
    assert [x.value for x in signature.value[1].value]==['self','suffix','prefix','dir','ignore_cleanup_errors','delete']
    assert signature.value[2].value==[0,0,0,0,0,2]
    assert len(runtime.exits)==1


def test_embedded_nul_raises_value_error_before_filesystem(runtime,tmp_path):
    runtime.create(tmp_path,prefix='bad\0prefix')
    assert runtime.error is not None and runtime.error.value[0]==2
    assert list(tmp_path.iterdir())==[]


def test_missing_parent_preserves_errno_filename_and_args(runtime,tmp_path):
    runtime.create(tmp_path/'missing')
    error=runtime.error
    assert error.attrs['errno']==2
    assert error.attrs['filename'].value.startswith(str(tmp_path/'missing')+'/pcc_owned_')
    assert error.attrs['args'].value[0]==2


def test_exit_resolves_instance_cleanup_override(runtime,tmp_path):
    manager=runtime.create(tmp_path)
    called=[]
    def cleanup(captures,args):
        assert args.value==[]
        called.append('override')
        return runtime.true
    manager.attrs['cleanup']=Obj('func',(cleanup,None))
    assert runtime.method(manager,'exit',runtime.none,runtime.none,runtime.none) is runtime.none
    assert called==['override']
    assert Path(runtime.name(manager)).is_dir()
    runtime.method(manager,'cleanup')


def test_root_symlink_error_matches_rmtree_base_error(runtime,tmp_path):
    manager=runtime.create(tmp_path);name=Path(runtime.name(manager))
    name.rmdir();target=tmp_path/'target';target.mkdir();name.symlink_to(target)
    runtime.method(manager,'cleanup')
    assert runtime.error.value[0]==14
    assert 'errno' not in runtime.error.attrs
    name.unlink()


def test_mkdtemp_callable_identity_signature_and_owned_result(runtime,tmp_path):
    fn=runtime.ns['py_tempfile_mkdtemp_function']()
    assert fn is runtime.ns['py_tempfile_mkdtemp_function']()
    entry,captures=fn.value
    signature=captures.value[1]
    assert [x.value for x in signature.value[1].value]==['suffix','prefix','dir']
    assert signature.value[2].value==[0,0,0]
    assert signature.value[3].value==[runtime.true]*3
    assert signature.value[4].value==[runtime.none]*3
    args=Obj('tuple',[runtime.string('_end'),runtime.string('owned_'),runtime.string(str(tmp_path))])
    name=entry(captures,args)
    assert runtime.error is None
    assert name.kind=='str' and os.path.isabs(name.value)
    assert Path(name.value).name.startswith('owned_') and name.value.endswith('_end')
    assert Path(name.value).is_dir()
    assert name.pins==0 and fn.pins==0 and not (name.flags&64) and not (fn.flags&64)
    runtime.ns['_td_shutdown']()
    assert Path(name.value).is_dir(), 'mkdtemp must not register automatic cleanup'
    Path(name.value).rmdir()


def test_mkdtemp_relative_directory_empty_prefix_and_unique_names(runtime,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    entry=runtime.ns['_td_mkdtemp_entry']
    args=Obj('tuple',[runtime.string(''),runtime.string(''),runtime.string('.')])
    first=entry(None,args);second=entry(None,args)
    assert first.value!=second.value
    for name in (first,second):
        assert os.path.isabs(name.value) and len(Path(name.value).name)==6
        Path(name.value).rmdir()


@pytest.mark.parametrize('prefix', ('bad\0name','missing/child'))
def test_mkdtemp_failure_preserves_filesystem_error(runtime,tmp_path,prefix):
    args=Obj('tuple',[runtime.none,runtime.string(prefix),runtime.string(str(tmp_path))])
    assert runtime.ns['_td_mkdtemp_entry'](None,args) is None
    assert runtime.error is not None
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('bad', (Obj('bytes',b'bytes_'),Obj('instance')))
def test_mkdtemp_unimplemented_path_domains_fail_explicitly(runtime,tmp_path,bad):
    args=Obj('tuple',[runtime.none,bad,runtime.string(str(tmp_path))])
    assert runtime.ns['_td_mkdtemp_entry'](None,args) is None
    assert runtime.error.value[0]==11
    assert 'owned' in runtime.error.value[1]
    assert list(tmp_path.iterdir())==[]


def _mkstemp_model_with_native_open():
    value = Runtime()
    calls = []
    def open_flags(path, flags, permissions, directory):
        from tests.python.test_owned_fdopen_provider import _host_open_flags

        calls.append((value.raw(path), flags, permissions, directory))
        try:
            # The model's runtime targets Linux; translate for the host open.
            return os.open(value.raw(path), _host_open_flags(flags), permissions,
                           dir_fd=None if directory == -100 else directory)
        except OSError as error:
            return -error.errno
    value.ns['open_file_flags'] = open_flags
    return value, calls


def test_owned_mkstemp_has_exclusive_owner_permissions_suffix_and_no_auto_delete(tmp_path, monkeypatch):
    import stat

    model, calls = _mkstemp_model_with_native_open()
    template = model.cstr(str(tmp_path / 'file-XXXXXX.tail'))
    fd = model.ns['pcc_platform_mkstemp_suffix'](template, 5)
    assert fd >= 0
    path = Path(os.fsdecode(model.raw(template)))
    try:
        assert path.name.endswith('.tail') and 'XXXXXX' not in path.name
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        assert not os.get_inheritable(fd)
        from tests.python.test_owned_fdopen_provider import _host_open_flags
        assert _host_open_flags(calls[-1][1]) == os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        assert calls[-1][2:] == (0o600, -100)
        os.write(fd, b'first owner')
    finally:
        os.close(fd)
    assert path.read_bytes() == b'first owner'
    # Reuse the original seed so the first name collides. The original file
    # must remain untouched while a subsequent stem gets a new owner.
    model.global_store('pcc_platform_mkdtemp_counter', 0)
    second = model.cstr(str(tmp_path / 'file-XXXXXX.tail'))
    fd = model.ns['pcc_platform_mkstemp_suffix'](second, 5)
    try:
        assert fd >= 0 and model.raw(second) != model.raw(template)
        assert path.read_bytes() == b'first owner'
        assert len(calls) == 3
    finally:
        if fd >= 0:
            os.close(fd)


@pytest.mark.parametrize('name,suffix', [('short',0),('aXXXXX',0),('XXXXXX',-1),('XXXXXX',1),('XXXXXX.tail',6)])
def test_owned_mkstemp_rejects_invalid_template_before_open(name, suffix, monkeypatch):
    model,calls=_mkstemp_model_with_native_open()
    template=model.cstr(name)
    assert model.ns['pcc_platform_mkstemp_suffix'](template,suffix)==-22
    assert calls==[] and model.raw(template)==name.encode()


def test_owned_mkstemp_preserves_noncollision_error_and_bounds_retries(monkeypatch):
    model,calls=_mkstemp_model_with_native_open()
    seen=[]
    def denied(*args):
        seen.append(args)
        return -13
    model.ns['open_file_flags']=denied
    assert model.ns['pcc_platform_mkstemp_suffix'](model.cstr('XXXXXX'),0)==-13
    assert len(seen)==1
    model.ns['open_file_flags']=lambda *args: seen.append(args) or -17
    seen.clear()
    assert model.ns['pcc_platform_mkstemp_suffix'](model.cstr('XXXXXX'),0)==-17
    assert len(seen)==256


def test_owned_mkstemp_selects_target_flags_without_host_translation(monkeypatch):
    model,calls=_mkstemp_model_with_native_open()
    model.ns['target_sys_platform']=lambda:model.cstr('darwin')
    seen=[]
    model.ns['open_file_flags']=lambda *args:seen.append(args) or 42
    assert model.ns['pcc_platform_mkstemp_suffix'](model.cstr('XXXXXX.end'),4)==42
    assert seen[0][1:]==(2|512|2048|16777216,0o600,-2)
    model.ns['target_sys_platform']=lambda:model.cstr('win32')
    seen.clear()
    assert model.ns['pcc_platform_mkstemp_suffix'](model.cstr('XXXXXX'),0)==-38
    assert seen==[]
