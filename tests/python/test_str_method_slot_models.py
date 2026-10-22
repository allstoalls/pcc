"""Execute the actual str descriptor bodies with relocation at ABI boundaries.

These are deterministic owner models and CPython controls, not native evidence.
The memory kernel is shared with the existing set-slot regressions.
"""
from __future__ import annotations
import ast
import operator
from pathlib import Path
import random
import pytest
from pcc.runtime.py import py_abi_constants as abi
from test_set_call_slot_roots import Memory, Object, Block, Ptr

ROOT = Path(__file__).resolve().parents[2]
PORT = ROOT / "pcc/runtime/py/py_class.py"
_TREES = {name:ast.parse(PORT.with_name(name).read_text()) for name in ("py_class.py","py_obj_ops_dispatch.py","py_str_accessors.py")}

_COMPILED = {}

def _compile_once(body, path):
    key = (str(path), tuple(id(n) for n in body))
    if key not in _COMPILED:
        _COMPILED[key] = compile(ast.Module(body=body,type_ignores=[]),str(path),"exec")
    return _COMPILED[key]

class StringMemory(Memory):
    def __init__(self, phase="callback", fail_kind=None, fail_copy=0, fail_register=0):
        super().__init__(phase=phase, fail_copy=fail_copy, fail_register=fail_register)
        self.fail_kind = fail_kind
        self.frame_handles = {}
        self.globals = {}
        self.primitive_calls = []
        self.events = []
        self.ns.update(vars(abi))
        self.ns.update(c_ptr=object, c_int64=int, c_int32=int, c_void=object)
        self.ns.update({
            "c_abi_export":lambda _:lambda fn:fn, "strlen":len,
            "load_i64":lambda ptr, offset:self.read(ptr,offset) or 0,
            "load_i32":lambda ptr, offset:self.read(ptr,offset) or 0,
            "load_i8":self.byte, "_strs_eq":lambda a,b:int(a==b),
            "global_addr":self.global_addr, "global_load_ptr":self.global_value,
            "pcc_gc_frame_enter":self.frame_enter, "pcc_gc_frame_leave":self.frame_leave,
            "pcc_gc_root_copy_borrowed_lease":self.copy,
            "pcc_gc_note_slot_write_barrier":lambda *args:None,
            "py_runtime_error_if_unset":self.runtime_error,
            "pcc_platform_abort":lambda:pytest.fail("lease abort"),
            "py_tls_exc_swap_slot":self.swap,
            "pcc_gc_pointer_is_managed":lambda obj:int(isinstance(obj,Object) and obj.alive),
            "pcc_gc_note_relocation_read":lambda obj:obj,
            "pcc_capi_is_cext_type_tag":lambda tag:0,
            "pcc_gc_pin":self.pin, "pcc_gc_take_pinned_slot":self.take,
            "py_tuple_new":self.new_tuple, "py_tuple_len":lambda obj:self.read(obj,16),
            "py_tuple_set_item":self.tuple_set,
            "py_incref":self.retain, "py_decref":self.release_value,
            "py_int_value_i64":lambda value:value.value if isinstance(value,Object) else value, "py_int_from_i64":self.integer,
            "py_bool_from_bit":lambda value:bool(value),
            "py_slice_index_i64":self.index,
            "py_call_validate_kwargs":lambda obj:0,
            "function_addr":lambda name:name,
            "atomic_rmw_i32":self.atomic_or,
            "py_tuple_get":self.tuple_get,
            "py_func_new_bound":self.function_new,
            "pcc_gc_root_copy_lease_prepare_locked":lambda d,s,b,p:self.copy_impl(d,s),
            "pcc_gc_root_copy_lease_finish":lambda plan:None,
        })
        self.global_addr("py_None").fields[0] = self.none
        self.maps = {"pcc_str_method_borrowed_map":-1,"pcc_str_method_result_map":1,"pcc_bound_bind_borrowed_map":-2,"pcc_bound_bind_result_map":1}
        self.builtin=self.klass("str",[])
        self.global_addr("pcc_type_cls_str").fields[0]=self.builtin
        self.child=self.klass("Child",[self.builtin])
        self.roots[("child",0)]=self.global_addr("child")
        self.global_addr("child").fields[0]=self.child
        names={"_ptr_is_class","_ptr_can_have_header","_ptr_is_instance",
            "_ptr_is_class_of_validated_instance","py_class_is_str_subclass",
            "_instance_storage_slot_count","_instance_builtin_payload_slot",
            "_special_open","_special_error","_special_copy","_special_adopt",
            "_special_drop","_special_close","_special_publish","_special_tuple_item",
            "_special_tuple_new","_special_lookup_locked","_special_name_equal",
            "_special_bind_attribute","py_str_getattr","py_str_type_new",
            "py_instance_bind_method","_instance_bind_method_body","_wrap_bound_captures",
            "_bound_signature","_func_signature","_func_signature_valid"}
        body=[]
        for node in _TREES["py_class.py"].body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith(("_STR_METHOD_","_STR_TYPE_","_BOUND_BIND_","_BOUND_SIGNATURE_")) for t in node.targets):body.append(node)
            elif isinstance(node,ast.FunctionDef) and (node.name in names or node.name.startswith(("_str_method_","_str_type_","_bound_signature_"))):
                node.decorator_list=[];body.append(node)
        exec(_compile_once(body,PORT),self.ns)
        dispatch=PORT.with_name("py_obj_ops_dispatch.py")
        fn=next(n for n in _TREES["py_obj_ops_dispatch.py"].body if isinstance(n,ast.FunctionDef) and n.name=="py_builtin_type_class_tag")
        fn.decorator_list=[]
        exec(_compile_once([fn],dispatch),self.ns)
        # Execute the actual byte-scanning bodies as well. Host str methods are
        # used only on the reference side of each assertion.
        primitives={"_bytes_eq","_utf8_codepoint_count","_str_cp_len",
            "_utf8_byte_offset_for_codepoint","_str_tailmatch_one",
            "py_str_tailmatch_range","py_str_count_range","_int_or_default","_type_of"}
        p=PORT.with_name("py_str_accessors.py")
        body=[n for n in _TREES["py_str_accessors.py"].body if isinstance(n,ast.FunctionDef) and n.name in primitives]
        for n in body:n.decorator_list=[]
        exec(_compile_once(body,p),self.ns)

    def global_addr(self, name):
        if hasattr(self, 'maps') and name in self.maps:
            return self.maps[name]
        if name not in self.globals:
            slot = Block()
            slot.fields[0] = None
            self.globals[name] = slot
            self.roots['global', name] = slot
        return self.globals[name]

    def global_value(self, name):
        return self.read(self.global_addr(name), 0)

    def frame_enter(self, count, slots):
        handles = []
        for index in range(abs(count)):
            handle = object()
            self.roots[handle] = self.add(slots, index * 8)
            handles.append(handle)
        self.frame_handles[id(slots)] = handles
        self.collect('frame_enter')

    def frame_leave(self, slots):
        for handle in self.frame_handles.pop(id(slots)):
            del self.roots[handle]
        self.collect('frame_leave')

    def pin(self, obj):
        if isinstance(obj, Object):
            assert obj.alive
            obj.leases += 1
            obj.fields[12] = (obj.fields.get(12) or 0) | 64

    def take(self, slot, prior):
        value = self.read(slot, 0)
        self.write(slot, 0, None)
        if isinstance(value, Object):
            assert value.alive and value.leases > 0
            value.leases -= 1
            value.fields[12] = (value.fields.get(12) or 0) & ~64 | prior
        return value

    def klass(self, name, parents):
        cls = self.make(name, abi.PY_TYPE_CLASS)
        cls.fields[12] = 0
        cls.fields[abi.PYCLASSOBJECT_N_FIELDS_OFFSET] = 0
        cls.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET] = 0
        cls.fields[abi.PYCLASSOBJECT_METHODS_OFFSET] = None
        cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET] = None
        cls.fields[abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET] = abi.PYINSTANCEOBJECT_FIELDS_OFFSET + 16
        mro = Block()
        for index, value in enumerate([cls] + parents):
            mro.fields[index * 8] = value
        cls.fields[abi.PYCLASSOBJECT_MRO_OFFSET] = mro
        cls.fields[abi.PYCLASSOBJECT_N_MRO_OFFSET] = len(parents) + 1
        return cls

    def allocate(self, value, tag, kind):
        self.collect('allocation')
        self.primitive_calls.append(kind)
        if self.fail_kind == kind:
            self.error = (19, kind + ' allocation failed')
            return None
        obj = self.make(value, tag)
        obj.fields[12] = 0
        return obj

    def new_tuple(self, n):
        obj = self.allocate(None, abi.PY_TYPE_TUPLE, 'tuple')
        if obj is not None:
            obj.fields[16] = n
            for index in range(n):
                obj.fields[24 + index * 8] = None
        return obj

    def retain(self, obj):
        if isinstance(obj, Object):
            assert obj.alive
            obj.refs += 1

    def release_value(self, obj):
        if isinstance(obj, Object):
            assert obj.alive and obj.refs > 0
            obj.refs -= 1

    def tuple_set(self, obj, index, value):
        assert obj.alive
        self.retain(value)
        self.release_value(obj.fields.get(24 + index * 8))
        obj.fields[24 + index * 8] = value

    def runtime_error(self, callee, message):
        if self.error is None:
            self.error = (7, message)
        return None

    def swap(self, slot):
        current = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = current

    def integer(self,value):
        if -(1<<62)<=value<(1<<62):return value
        return self.allocate(value,abi.PY_TYPE_INT,"integer")

    def atomic_or(self, op, obj, offset, value, order):
        assert op == "or" and obj.alive and obj.leases > 0
        previous=self.read(obj,offset) or 0
        self.write(obj,offset,previous|value)
        return previous

    def tuple_get(self,obj,index):
        assert obj.alive
        value=self.read(obj,24+index*8)
        self.retain(value)
        return value

    def function_new(self,entry,captures,name,self_obj):
        assert captures.alive and captures.leases>0
        fn=self.allocate(name,abi.PY_TYPE_FUNC,"function")
        if fn is None:return None
        self.retain(captures);self.retain(self_obj)
        fn.fields.update({56:entry,64:captures,72:name,80:self_obj})
        return fn

    def byte(self, ptr, offset):
        if isinstance(ptr,str):return ord(ptr[offset])
        return self.read(ptr,offset)

    def wrap(self,value):
        if value is None:return self.none
        if isinstance(value,Object):return value
        if isinstance(value,int):return value
        if isinstance(value,tuple):
            obj=self.make(value,abi.PY_TYPE_TUPLE);obj.fields[12]=0
            obj.fields[16]=len(value)
            for index,item in enumerate(value):obj.fields[24+index*8]=self.wrap(item)
            return obj
        if isinstance(value,str):
            payload=self.make(str.__str__(value),abi.PY_TYPE_STR);payload.fields[12]=0
            data=str.__str__(value).encode("utf-8","surrogatepass")
            payload.fields[abi.PYSTROBJECT_BYTE_LEN_OFFSET]=len(data)
            payload.fields[abi.PYSTROBJECT_CP_LEN_OFFSET]=-1
            for index,byte in enumerate(data):payload.fields[abi.PYSTROBJECT_DATA_OFFSET+index]=byte
            if type(value) is str:return payload
            obj=self.make(value,abi.PY_TYPE_INSTANCE);obj.fields[12]=0
            obj.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET]=self.global_value("child")
            obj.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET+8]=payload
            return obj
        obj=self.make(value,abi.PY_TYPE_FLOAT);obj.fields[12]=0
        return obj

    def index(self,obj,default):
        self.collect("callback")
        if obj is None or obj is self.none:return default
        if isinstance(obj,Object):
            assert obj.alive and obj.leases>0
            obj=obj.value
        try:return max(-(1<<63),min((1<<63)-1,operator.index(obj)))
        except Exception as error:
            self.error=(3 if isinstance(error,TypeError) else 2,str(error))
            return -1

    def invoke(self,method,value,*args):
        caller=Block();self.roots[("caller",0)]=caller
        caller.fields[0]=self.wrap((value,)+args)
        captures=self.wrap(({"count":0,"startswith":1,"endswith":2}[method],))
        result=self.ns["_str_method_entry"](captures,self.read(caller,0))
        assert not self.frame_handles
        assert all(isinstance(key,tuple) for key in self.roots)
        assert all(obj.leases==0 for obj in self.objects if obj.alive and obj is not self.none)
        return ("value",result) if self.error is None else ("error",{3:TypeError,2:ValueError,7:RuntimeError,19:MemoryError}[self.error[0]])

    def bind_method(self,function,receiver,name):
        assert function.alive and function.leases>0 and receiver.alive and receiver.leases>0
        self.collect("callback")
        bound=self.allocate(name,abi.PY_TYPE_FUNC,"bound")
        if bound is None:return None
        self.retain(function);self.retain(receiver)
        bound.fields[200]=function;bound.fields[208]=receiver
        return bound


def outcome(call):
    try:return ("value",call())
    except Exception as error:return ("error",type(error))

class StringSubclass(str):
    def __str__(self):raise AssertionError("must read builtin payload")

@pytest.mark.parametrize("method",("startswith","endswith","count"))
def test_real_descriptor_matches_cpython_windows_and_prefixes(method):
    texts=("","a","héllo","🙂é\0日本","A\ud800B","x"*257)
    bounds=((),(None,),(None,None),(-100,),(-1,),(-3,-1),(0,0),(2,1),(100,),
            (10**100,),(-(10**100),10**100),(0,-(10**100)),(1.5,))
    needles=("","h","é","🙂", "\0", "\ud800",42,b"h",(),("x","h"),("h",42),(42,"h"),(("h",),))
    for text in texts:
        for needle in needles:
            for window in bounds:
                actual=StringMemory("callback").invoke(method,text,needle,*window)
                expected=outcome(lambda:getattr(text,method)(needle,*window))
                assert actual==expected,(method,repr(text),needle,window,actual,expected)

@pytest.mark.parametrize("phase",("register","copy","acquire","callback","release","drop","allocation","frame_enter","frame_leave"))
@pytest.mark.parametrize("method",("count","startswith","endswith"))
def test_method_lifetime_under_relocation_and_index_callbacks(phase,method):
    events=[]
    class Index:
        def __init__(self,name,value):self.name,self.value=name,value
        def __index__(self):events.append(self.name);return self.value
    mem=StringMemory(phase)
    result=mem.invoke(method,"héllo","é",Index("start",1),Index("end",2))
    assert result==("value",1 if method=="count" else True)
    assert events==["start","end"]
    if phase != "allocation":
        assert mem.moves>0

@pytest.mark.parametrize("method",("count","startswith","endswith"))
def test_prefix_validation_and_index_exception_order(method):
    events=[]
    class Index:
        def __init__(self,name,fail=False):self.name,self.fail=name,fail
        def __index__(self):
            events.append(self.name)
            if self.fail:raise ValueError(self.name)
            return 0
    mem=StringMemory()
    actual=mem.invoke(method,"abc",42,Index("start"),Index("end"))
    trace=list(events);events.clear()
    expected=outcome(lambda:getattr("abc",method)(42,Index("start"),Index("end")))
    assert actual==expected and trace==events
    events.clear()
    mem=StringMemory()
    assert mem.invoke(method,"abc","a",Index("start",True),Index("end"))==("error",ValueError)
    assert events==["start"]

@pytest.mark.parametrize("method",("count","startswith","endswith"))
def test_descriptor_arity_rejection_precedes_conversion(method):
    for args in ((),("a",0,1,2)):
        assert StringMemory().invoke(method,"abc",*args)==("error",TypeError)

@pytest.mark.parametrize("failure",range(1,8))
def test_method_copy_failure_retires_acquired_owners(failure):
    mem=StringMemory(fail_copy=failure)
    assert mem.invoke("startswith","abc","a",0,3)[0]=="error"

@pytest.mark.parametrize("failure",(1,4,14))
def test_method_root_registration_failure_retires_handles(failure):
    mem=StringMemory(fail_register=failure)
    assert mem.invoke("startswith","abc","a")[0]=="error"


class CanonicalStringMemory(StringMemory):
    def __init__(self,phase="copy",failure=None,warm=False):
        super().__init__(phase)
        self.failure=failure;self.locked=False;self.installed=[];self.initializations=0
        if not warm:self.global_addr("pcc_type_cls_str").fields[0]=None
        self.ns.update({
            "_object_new_cache_mutex":lambda:"owned-mutex",
            "pcc_mutex_lock":self.mutex_lock,"pcc_mutex_unlock":self.mutex_unlock,
            "py_class_new":self.class_new,
            "py_class_write_namespace_slots":self.write_namespace,
            "py_class_add_method":self.add_method,
        })

    def mutex_lock(self, mutex):
        assert mutex == 'owned-mutex' and (not self.locked)
        self.collect('mutex_lock')
        self.locked = True
        return 0

    def mutex_unlock(self, mutex):
        assert self.locked
        self.locked = False
        return 0

    def class_new(self, name, *args):
        assert self.locked and self.global_value('pcc_type_cls_str') is None
        self.initializations += 1
        if self.failure == 'class':
            self.error = (19, 'class allocation')
            return None
        self.collect('allocation')
        return self.klass(name, [])

    def write_namespace(self, class_slot, name, value_slot, remove):
        assert self.locked and self.global_value('pcc_type_cls_str') is None and (remove == 0)
        cls = self.read(class_slot, 0)
        value = self.read(value_slot, 0)
        assert cls.leases > 0 and value.leases > 0
        self.collect('callback')
        if self.failure == 'namespace' and name == 'startswith':
            self.error = (7, 'namespace write')
            return -1
        self.install(cls, name, value)
        self.installed.append(name)
        return 0

    def add_method(self, cls, name, fn):
        assert self.locked and cls.leases > 0 and (fn.leases > 0)
        attrs = cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET]
        entry = attrs.value[name]
        owned = attrs.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET].fields[entry * abi.DICTENTRY_SIZE + abi.DICTENTRY_VALUE_OFFSET]
        if owned.tag == abi.PY_TYPE_STATICMETHOD:
            owned = owned.fields[abi.PYSTATICMETHODOBJECT_FUNC_OFFSET]
        assert owned is fn, 'method alias published without namespace owner'
        if self.failure == 'table' and name == 'startswith':
            return
        count = cls.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET]
        methods = cls.fields[abi.PYCLASSOBJECT_METHODS_OFFSET]
        if methods is None:
            methods = Block()
            cls.fields[abi.PYCLASSOBJECT_METHODS_OFFSET] = methods
        methods.fields[count * abi.PYCLASSMETHOD_SIZE + abi.PYCLASSMETHOD_NAME_OFFSET] = name
        methods.fields[count * abi.PYCLASSMETHOD_SIZE + abi.PYCLASSMETHOD_FUNC_OFFSET] = fn
        cls.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET] = count + 1

    def install(self, cls, name, value):
        attrs = cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET]
        if attrs is None:
            attrs = self.make({}, abi.PY_TYPE_DICT)
            attrs.fields[12] = 0
            attrs.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET] = Block()
            attrs.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET] = 0
            cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET] = attrs
        entries = attrs.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]
        index = attrs.value.get(name)
        if index is None:
            index = attrs.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET]
            attrs.value[name] = index
            attrs.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET] = index + 1
            key = self.make(name, abi.PY_TYPE_STR)
            key.fields[12] = 0
            key.fields[abi.PYSTROBJECT_BYTE_LEN_OFFSET] = len(name)
            for i, char in enumerate(name):
                key.fields[abi.PYSTROBJECT_DATA_OFFSET + i] = ord(char)
            entries.fields[index * abi.DICTENTRY_SIZE + abi.DICTENTRY_KEY_OFFSET] = key
        slot = index * abi.DICTENTRY_SIZE + abi.DICTENTRY_VALUE_OFFSET
        old = entries.fields.get(slot)
        self.release_value(old)
        self.retain(value)
        entries.fields[slot] = value


@pytest.mark.parametrize("phase",("register","copy","acquire","callback","release","allocation","mutex_lock","frame_leave"))
def test_canonical_string_class_owns_supported_method_family(phase):
    mem=CanonicalStringMemory(phase)
    result=mem.ns["py_str_type_new"]()
    assert result.alive and result is mem.global_value("pcc_type_cls_str")
    assert mem.installed == ["count","startswith","endswith"]
    assert mem.initializations == 1
    methods=result.fields[abi.PYCLASSOBJECT_METHODS_OFFSET]
    for index in range(3):
        fn=methods.fields[index*abi.PYCLASSMETHOD_SIZE+abi.PYCLASSMETHOD_FUNC_OFFSET]
        assert fn.alive and fn.refs==1 and fn.fields[64].refs==1
    assert not mem.locked and not mem.frame_handles
    assert all(obj.leases==0 for obj in mem.objects if obj.alive and obj is not mem.none)

@pytest.mark.parametrize("failure",("class","namespace","table"))
def test_canonical_string_failure_does_not_publish_partial_type(failure):
    mem=CanonicalStringMemory("callback",failure)
    assert mem.ns["py_str_type_new"]() is None
    assert mem.global_value("pcc_type_cls_str") is None
    assert mem.error is not None and not mem.locked and not mem.frame_handles

@pytest.mark.parametrize("phase",("register","copy","acquire","callback","release","allocation","frame_enter","frame_leave"))
def test_detached_string_method_binds_receiver_and_canonical_function(phase):
    mem=CanonicalStringMemory(phase)
    cls=mem.ns["py_str_type_new"]()
    caller=Block();mem.roots[("caller",0)]=caller
    caller.fields[0]=mem.wrap("héllo")
    bound=mem.ns["py_str_getattr"](mem.read(caller,0),"startswith")
    assert mem.error is None and bound.alive
    receiver=mem.read(caller,0)
    assert bound.fields[80] is receiver
    captures=bound.fields[64]
    canonical=captures.fields[24]
    assert captures.fields[32] is receiver
    assert canonical.fields[72]=="startswith"
    assert canonical.fields[64].fields[24]==1
    assert not mem.frame_handles
    assert all(obj.leases==0 for obj in mem.objects if obj.alive and obj is not mem.none)

@pytest.mark.parametrize("method",("startswith","endswith","count"))
def test_subclass_payload_preserves_identity_and_bypasses_str_override(method):
    value=StringSubclass("héllo")
    needle=StringSubclass("é")
    assert StringMemory("copy").invoke(method,value,needle,1,2)==("value",1 if method=="count" else True)


def test_count_boxed_bound_allocation_failure_propagates():
    mem=StringMemory("allocation",fail_kind="integer")
    assert mem.invoke("count","abc","a")==("error",MemoryError)

@pytest.mark.parametrize('kind',('tuple','function'))
def test_detached_method_allocation_failure_keeps_original_error(kind):
    mem=CanonicalStringMemory('callback')
    mem.ns['py_str_type_new']()
    caller=Block();mem.roots[('caller',0)]=caller
    caller.fields[0]=mem.wrap('abc')
    mem.fail_kind=kind
    assert mem.ns['py_str_getattr'](mem.read(caller,0),'startswith') is None
    assert mem.error[0]==19
    assert not mem.frame_handles
    assert all(obj.leases==0 for obj in mem.objects if obj.alive and obj is not mem.none)


def test_index_failure_survives_cleanup_errors():
    mem=StringMemory('callback')
    original_drop=mem.ns['pcc_gc_store_root']
    def drop(slot,value):
        prior=mem.read(slot,0)
        original_drop(slot,value)
        if isinstance(prior,Object) and prior.tag==abi.PY_TYPE_FLOAT:
            mem.error=(7,'cleanup exception')
    mem.ns['pcc_gc_store_root']=drop
    class Broken:
        def __index__(self):raise ValueError('index exception')
    assert mem.invoke('startswith','abc','a',Broken())==('error',ValueError)
    assert mem.error==(2,'index exception')


def test_unimplemented_string_attribute_does_not_bind_base_object_descriptor():
    mem=CanonicalStringMemory()
    assert mem.ns['py_str_getattr'](mem.wrap('abc'),'__repr__') is None
    assert mem.error is None and mem.initializations==0
