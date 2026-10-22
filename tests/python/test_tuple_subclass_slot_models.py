"""Execute tuple subclass bodies in a relocating slot model, not native gates."""
from __future__ import annotations
import ast
from pathlib import Path
import pytest
from pcc.runtime.py import py_abi_constants as abi
from test_set_call_slot_roots import Memory, Object, Block, Ptr

PORT = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_class.py'


class TupleMemory(Memory):
    def __init__(self, phase='callback', fail_kind=None):
        super().__init__(phase=phase)
        self.fail_kind = fail_kind
        self.frame_handles = {}
        self.globals = {}
        self.primitive_calls = []
        self.ns.update({name:getattr(abi,name) for name in dir(abi) if name.isupper()})
        self.ns.update(c_ptr=object, c_int64=int, c_int32=int, c_void=object)
        self.ns.update({
            'c_abi_export':lambda _:lambda f:f, 'strlen':len,
            'load_i64':lambda ptr,offset:self.read(ptr,offset) or 0,
            'load_i32':lambda ptr,offset:self.read(ptr,offset) or 0,
            'load_i8':lambda text,index:ord(text[index]), '_strs_eq':lambda a,b:int(a==b),
            'global_addr':self.global_addr, 'global_load_ptr':self.global_value,
            'pcc_gc_frame_enter':self.frame_enter, 'pcc_gc_frame_leave':self.frame_leave,
            'pcc_gc_root_copy_borrowed_lease':self.copy,
            'pcc_gc_note_slot_write_barrier':lambda *args:None,
            'py_runtime_error_if_unset':self.runtime_error, 'py_raise':self.raise_error,
            'pcc_platform_abort':lambda:pytest.fail('lease abort'),
            'py_tls_exc_swap_slot':self.swap,
            'pcc_gc_pointer_is_managed':lambda obj:int(isinstance(obj,Object) and obj.alive),
            'pcc_gc_note_relocation_read':lambda obj:obj,
            'pcc_capi_is_cext_type_tag':lambda tag:0,
            'pcc_gc_pin':self.pin, 'pcc_gc_take_pinned_slot':self.take,
            'py_tuple_new':self.new_tuple, 'py_tuple_len':lambda obj:self.read(obj,16),
            'py_tuple_set_item':self.tuple_set,
            'py_list_new':self.new_list, 'py_list_append':self.list_append,
            'py_tuple_from_list':self.from_list,
            'py_call_validate_kwargs':lambda obj:0,
            'py_instance_new':self.instance,
            'pcc_gc_store_ptr':self.store_ptr_owner,
            'py_incref':self.retain, 'py_decref':self.release_value,
        })
        self.global_addr('py_None').fields[0] = self.none
        self.builtin = self.klass('tuple', [])
        self.global_addr('pcc_type_cls_tuple').fields[0] = self.builtin
        self.cls = self.klass('Child', [self.builtin])
        self.maps = {'pcc_tuple_new_borrowed_map':-3,'pcc_tuple_new_result_map':1,
                     'pcc_tuple_view_borrowed_map':-2,'pcc_tuple_view_result_map':1,
                     'pcc_tuple_attribute_borrowed_map':-1}
        names={'_ptr_is_class','_ptr_can_have_header','_ptr_is_instance','_ptr_is_class_of_validated_instance',
               'py_class_is_tuple_subclass','_class_is_structseq','_instance_storage_slot_count',
               '_instance_builtin_payload_slot','py_tuple_payload','py_tuple_check',
               '_special_open','_special_error','_special_copy','_special_adopt',
               '_special_drop','_special_close','_special_publish','_special_tuple_item',
               '_special_validate_arguments','_special_lookup_locked','_special_name_equal',
               '_special_select_owned','_tuple_new_collect','_tuple_new_body',
               'py_tuple_subclass_new'}
        tree=ast.parse(PORT.read_text())
        body=[]
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith(('_TUPLE_NEW_', '_STRUCTSEQ_CLASS_FLAG')) for t in node.targets):
                body.append(node)
            elif isinstance(node,ast.FunctionDef) and node.name in names:
                node.decorator_list=[]
                body.append(node)
        exec(compile(ast.Module(body=body,type_ignores=[]),str(PORT),'exec'),self.ns)
        dispatch=PORT.with_name('py_obj_ops_dispatch.py')
        fn=next(n for n in ast.parse(dispatch.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='py_builtin_type_class_tag')
        fn.decorator_list=[]
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(dispatch),'exec'),self.ns)

    def wrap(self,value):
        result=super().wrap(value)
        for obj in self.objects:
            obj.fields.setdefault(12,0)
        return result

    def global_addr(self,name):
        if hasattr(self,'maps') and name in self.maps:return self.maps[name]
        if name not in self.globals:
            slot=Block();slot.fields[0]=None;self.globals[name]=slot
            self.roots[('global',name)]=slot
        return self.globals[name]

    def global_value(self,name):
        return self.read(self.global_addr(name),0)

    def frame_enter(self,count,slots):
        handles=[]
        for index in range(abs(count)):
            handle=object();self.roots[handle]=self.add(slots,index*8);handles.append(handle)
        self.frame_handles[id(slots)]=handles
        self.collect('frame_enter')

    def frame_leave(self,slots):
        for handle in self.frame_handles.pop(id(slots)):del self.roots[handle]
        self.collect('frame_leave')

    def pin(self,obj):
        if isinstance(obj,Object):
            assert obj.alive;obj.leases+=1;obj.fields[12]=(obj.fields.get(12) or 0)|64

    def take(self,slot,prior):
        value=self.read(slot,0);self.write(slot,0,None)
        if isinstance(value,Object):
            assert value.alive and value.leases>0
            value.leases-=1;value.fields[12]=(value.fields.get(12) or 0)&~64|prior
        return value

    def klass(self,name,parents):
        cls=self.make(name,abi.PY_TYPE_CLASS)
        cls.fields[12]=0
        cls.fields[abi.PYCLASSOBJECT_N_FIELDS_OFFSET]=0
        cls.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET]=0
        cls.fields[abi.PYCLASSOBJECT_METHODS_OFFSET]=None
        cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET]=None
        cls.fields[abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET]=abi.PYINSTANCEOBJECT_FIELDS_OFFSET+16
        mro=Block()
        for index,value in enumerate([cls]+parents):mro.fields[index*8]=value
        cls.fields[abi.PYCLASSOBJECT_MRO_OFFSET]=mro
        cls.fields[abi.PYCLASSOBJECT_N_MRO_OFFSET]=len(parents)+1
        return cls

    def allocate(self,value,tag,kind):
        self.collect('allocation')
        self.primitive_calls.append(kind)
        if self.fail_kind==kind:
            self.error=(19,kind+' allocation failed');return None
        obj=self.make(value,tag);obj.fields[12]=0
        return obj

    def new_tuple(self,n):
        obj=self.allocate(None,abi.PY_TYPE_TUPLE,'tuple')
        if obj is not None:
            obj.fields[16]=n
            for index in range(n):obj.fields[24+index*8]=None
        return obj

    def new_list(self,n):
        obj=self.allocate([],abi.PY_TYPE_LIST,'list')
        if obj is not None:obj.fields[16]=0
        return obj

    def retain(self,obj):
        if isinstance(obj,Object):assert obj.alive;obj.refs+=1

    def release_value(self,obj):
        if isinstance(obj,Object):assert obj.alive and obj.refs>0;obj.refs-=1

    def tuple_set(self,obj,index,value):
        assert obj.alive
        self.retain(value);self.release_value(obj.fields.get(24+index*8))
        obj.fields[24+index*8]=value

    def list_append(self,obj,value):
        assert obj.alive and obj.leases>0
        self.collect('callback')
        index=obj.fields[16];self.retain(value)
        obj.fields[24+index*8]=value;obj.fields[16]=index+1

    def from_list(self,obj):
        assert obj.alive and obj.leases>0
        result=self.new_tuple(obj.fields[16])
        if result is not None:
            for index in range(obj.fields[16]):self.tuple_set(result,index,obj.fields[24+index*8])
        return result

    def instance(self,cls):
        assert cls.alive and cls.leases>0
        result=self.allocate(None,abi.PY_TYPE_INSTANCE,'instance')
        if result is not None:result.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET]=cls
        return result

    def store_ptr_owner(self,owner,slot,value):
        assert owner.alive and owner.leases>0
        self.collect('callback')
        self.retain(value);self.release_value(self.read(slot,0));self.write(slot,0,value)

    def runtime_error(self,callee,message):
        if self.error is None:self.error=(7,message)
        return None

    def swap(self,slot):
        current=self.read(slot,0);self.write(slot,0,self.error);self.error=current

    def invoke(self,args=(),kwargs=None,cls=None):
        caller=Block()
        values=(self.cls if cls is None else cls,self.wrap(tuple(args)),self.none if kwargs is None else self.wrap(kwargs))
        for i,value in enumerate(values):
            caller.fields[i*8]=value;self.roots[('caller',i)]=self.add(caller,i*8)
        result=self.ns['py_tuple_subclass_new'](*[caller.fields[i*8] for i in range(3)])
        assert not self.frame_handles
        assert all(isinstance(key,tuple) for key in self.roots)
        assert all(obj.leases==0 for obj in self.objects if obj.alive and obj is not self.none and obj is not getattr(self,'not_implemented',None))
        return result,caller

    def values(self,obj):
        if obj.tag!=abi.PY_TYPE_TUPLE:
            obj=self.ns['py_tuple_payload'](obj)
        return tuple(self.read(obj,24+index*8).value if isinstance(self.read(obj,24+index*8),Object) else self.read(obj,24+index*8) for index in range(self.read(obj,16)))


@pytest.mark.parametrize('phase',('register','copy','acquire','callback','release','drop','allocation','frame_enter','frame_leave'))
def test_tuple_constructor_preserves_class_payload_and_balances_leases(phase):
    memory=TupleMemory(phase)
    result,caller=memory.invoke(([3,5,8],))
    assert result.alive and result.tag==abi.PY_TYPE_INSTANCE
    assert result.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] is caller.fields[0]
    assert memory.values(result)==(3,5,8)
    assert memory.error is None
    assert memory.ns['py_tuple_check'](result)==1


@pytest.mark.parametrize('kind',('tuple','list','instance'))
def test_tuple_constructor_allocation_failure_retires_temporaries(kind):
    memory=TupleMemory('callback',kind)
    result,_=memory.invoke(([1,2],))
    assert result is None
    assert memory.error[0]==19


def test_tuple_constructor_exact_tuple_identity_and_subclass_wrapper():
    memory=TupleMemory('callback')
    result,caller=memory.invoke(((1,2),),cls=memory.builtin)
    source=memory.read(caller.fields[8],24)
    assert result is source and memory.values(result)==(1,2)
    memory=TupleMemory('callback')
    result,caller=memory.invoke(((1,2),))
    source=memory.read(caller.fields[8],24)
    assert result is not source and memory.ns['py_tuple_payload'](result) is source


@pytest.mark.parametrize('args,kwargs',(((1,2),None),((),{'iterable':[]})))
def test_tuple_constructor_argument_errors(args,kwargs):
    memory=TupleMemory('callback')
    result,_=memory.invoke(args,kwargs)
    assert result is None and memory.error[0]==3


def test_tuple_constructor_iteration_error_survives_cleanup():
    def source():
        yield 1
        raise RuntimeError('iterator-failure')
    memory=TupleMemory('callback')
    result,_=memory.invoke((source(),))
    assert result is None and memory.error==(7,'iterator-failure')


class TupleViewMemory(TupleMemory):
    def __init__(self,phase='callback'):
        super().__init__(phase)
        self.ns.update({
            'py_obj_index_i64':self.index,
            'py_obj_is_slice':lambda obj:int(isinstance(self.value(obj),slice)),
            'py_obj_getattr':lambda obj,name:self.wrap(getattr(self.value(obj),name)),
            'py_tuple_getitem':self.getitem,
            'py_tuple_slice':self.slice,
            'py_tuple_concat':lambda a,b:self.copy_items([a,b],1),
            'py_tuple_repeat':lambda a,n:self.copy_items([a],max(n,0)),
            'py_tuple_count':lambda a,b:self.scalar_callback(a,b,'count'),
            'py_tuple_index_range':self.tuple_index,
            'py_obj_contains':lambda a,b:self.scalar_callback(a,b,'contains'),
            'py_obj_eq':lambda a,b:self.scalar_callback(a,b,'eq'),
            'py_obj_lt':lambda a,b:self.scalar_callback(a,b,'lt'),
            'py_obj_le':lambda a,b:self.scalar_callback(a,b,'le'),
            'py_obj_gt':lambda a,b:self.scalar_callback(a,b,'gt'),
            'py_obj_ge':lambda a,b:self.scalar_callback(a,b,'ge'),
            'py_obj_hash':lambda a:self.scalar_callback(a,None,'hash'),
            'py_obj_repr':lambda a:self.wrap(self.scalar_callback(a,None,'repr')),
            'py_bool_from_bit':lambda n:bool(n), 'py_int_from_i64':lambda n:n,
        })
        names={'_tuple_copy_payload','_tuple_view_not_implemented','_tuple_view_body',
               '_tuple_view_rooted','py_tuple_iter_next','_special_native_instance',
               '_special_tuple_new','_tuple_method_name'}
        body=[]
        for node in ast.parse(PORT.read_text()).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('_TUPLE_VIEW_') for t in node.targets):body.append(node)
            elif isinstance(node,ast.FunctionDef) and node.name in names:
                node.decorator_list=[];body.append(node)
        exec(compile(ast.Module(body=body,type_ignores=[]),str(PORT),'exec'),self.ns)
        self.not_implemented=self.make(NotImplemented,abi.PY_TYPE_NONE)
        self.not_implemented.leases=1000
        self.not_implemented.fields[12]=0
        self.global_addr('py_NotImplemented').fields[0]=self.not_implemented

    def wrap(self,value):
        if value is None:self.retain(self.none);return self.none
        if isinstance(value,Object):self.retain(value);return value
        if isinstance(value,str):
            obj=self.allocate(value,abi.PY_TYPE_STR,'str')
            obj.fields[abi.PYSTROBJECT_BYTE_LEN_OFFSET]=len(value)
            for i,char in enumerate(value):obj.fields[abi.PYSTROBJECT_DATA_OFFSET+i]=ord(char)
            return obj
        return super().wrap(value)

    def value(self,obj):
        if obj is self.none:return None
        if not isinstance(obj,Object):return obj
        assert obj.alive
        if obj.tag==abi.PY_TYPE_TUPLE:return tuple(self.value(obj.fields[24+i*8]) for i in range(obj.fields[16]))
        if obj.tag==abi.PY_TYPE_INSTANCE:
            payload=self.ns['py_tuple_payload'](obj)
            if payload is not None:return self.value(payload)
        return obj.value

    def index(self,obj):
        import operator
        self.collect('callback')
        try:return operator.index(self.value(obj))
        except Exception as error:self.error=(3,str(error));return -1

    def getitem(self,obj,index):
        assert obj.alive and obj.leases>0
        n=obj.fields[16]
        if index<0:index+=n
        if not 0<=index<n:self.error=(5,'tuple index out of range');return None
        self.collect('callback')
        result=obj.fields[24+index*8];self.retain(result);return result

    def scalar_callback(self,a,b,operation):
        assert a.alive and a.leases>0
        self.collect('callback')
        left,right=self.value(a),self.value(b)
        try:
            if operation=='eq':return left==right
            if operation=='lt':return left<right
            if operation=='le':return left<=right
            if operation=='gt':return left>right
            if operation=='ge':return left>=right
            if operation=='contains':return right in left
            if operation=='count':return left.count(right)
            if operation=='hash':return hash(left)
            if operation=='repr':return repr(left)
        except Exception as error:self.error=(3,str(error));return -1

    def tuple_index(self,obj,item,start,stop):
        self.collect('callback')
        try:return self.value(obj).index(self.value(item),0 if start is None else self.value(start),obj.fields[16] if stop is None else self.value(stop))
        except ValueError as error:self.error=(2,str(error));return -1

    def copy_items(self,objects,count):
        result=self.new_tuple(sum(obj.fields[16] for obj in objects)*count)
        if result is None:return None
        index=0
        for _ in range(count):
            for obj in objects:
                assert obj.alive and obj.leases>0
                for i in range(obj.fields[16]):self.tuple_set(result,index,obj.fields[24+i*8]);index+=1
        return result

    def slice(self,obj,start,stop,step):
        indexes=list(range(obj.fields[16]))[slice(self.value(start),self.value(stop),self.value(step))]
        result=self.new_tuple(len(indexes))
        for index,source_index in enumerate(indexes):self.tuple_set(result,index,obj.fields[24+source_index*8])
        return result

    def invoke_view(self,operation,arguments=(),values=(3,5,3)):
        receiver,caller=self.invoke((values,))
        held=Block();held.fields[0]=receiver;self.roots[('view-receiver',0)]=held
        args=self.new_tuple(len(arguments)+1)
        held.fields[8]=args;self.roots[('view-args',0)]=self.add(held,8)
        self.tuple_set(args,0,self.read(held,0))
        for index,value in enumerate(arguments):self.tuple_set(self.read(held,8),index+1,self.wrap(value))
        result=self.ns['_tuple_view_rooted'](None,self.read(held,8),operation,0)
        assert not self.frame_handles
        assert all(obj.leases==0 for obj in self.objects if obj.alive and obj is not self.none and obj is not self.not_implemented)
        return result


@pytest.mark.parametrize('operation,args,expected',[
    (1,(),3),(3,(0,),3),(3,(-1,),3),(3,(slice(None,None,-1),),(3,5,3)),
    (4,(5,),True),(4,(8,),False),(5,(),'(3, 5, 3)'),(6,(),hash((3,5,3))),
    (7,((3,5,3),),True),(8,((3,5,3),),False),(9,((4,),),True),
    (10,((3,5,3),),True),(11,((1,),),True),(12,((3,5,3),),True),
    (13,((7,),),(3,5,3,7)),(14,(2,),(3,5,3,3,5,3)),
    (15,(0,),()),(16,(3,),2),(17,(3,1),2),(18,(),((3,5,3),)),
])
@pytest.mark.parametrize('phase',('callback','allocation','release'))
def test_tuple_default_views_hold_payloads_across_callbacks(operation,args,expected,phase):
    memory=TupleViewMemory(phase)
    result=memory.invoke_view(operation,args)
    assert memory.error is None
    assert memory.value(result)==expected
    if operation in (3,13,14,15) and isinstance(expected,tuple):assert result.tag==abi.PY_TYPE_TUPLE


def test_tuple_default_errors_and_unrelated_comparison():
    memory=TupleViewMemory()
    result=memory.invoke_view(7,(object(),))
    assert result is memory.not_implemented and memory.error is None
    for operation,argument,kind in ((3,99,5),(13,object(),3),(14,object(),3),(17,99,2)):
        memory=TupleViewMemory()
        result=memory.invoke_view(operation,(argument,))
        assert result is None and memory.error[0]==kind


class SelectionMemory(TupleViewMemory):
    def __init__(self,phase='copy'):
        super().__init__(phase)
        self.calls=[]
        self.ns.update({
            'load_i8':lambda value,index:ord(value[index]) if isinstance(value,str) else self.read(value,index),
            'untag_int':lambda value:value,
            'py_obj_issubclass':self.issubclass,
            'py_builtin_type_for_tag':self.builtin_type,
            'py_obj_call_slots':self.call_slots, 'py_obj_truthy':lambda value:int(bool(self.value(value))),
            'py_instance_bind_method':self.bind_method,
            'pcc_gc_root_copy_lease_prepare_locked':lambda d,s,b,p:self.copy_impl(d,s),
            'pcc_gc_root_copy_lease_finish':lambda plan:None,
        })
        names={'_special_prepend','_special_bind_and_call','_special_resolve_type',
               '_special_select_owned','_special_invoke_selected',
               '_binary_selected_attempt','_binary_selected_same','_binary_special_body','_binary_priority_value',
               'py_obj_binary_special_call_slots','_tuple_sequence_default_operation',
               '_tuple_method_entry','_super_new_select','_super_new_bind','_special_bind_attribute','py_tuple_getattr',
               'py_super_new_lookup_slots','py_obj_special_call_slots',
               '_tuple_class_call_body','py_tuple_class_call'}
        body=[]
        for node in ast.parse(PORT.read_text()).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith(('_BINARY_','_TUPLE_METHOD_')) for t in node.targets):body.append(node)
            elif isinstance(node,ast.FunctionDef) and node.name in names:
                node.decorator_list=[];body.append(node)
        exec(compile(ast.Module(body=body,type_ignores=[]),str(PORT),'exec'),self.ns)
        self.ns['py_int_value_i64']=lambda value:value

    def bind_method(self,function,receiver,name):
        assert function.leases>0 and receiver.leases>0
        bound=self.method(name,lambda *args:function.value(receiver,*args))
        self.retain(function);self.retain(receiver)
        bound.fields[200]=function;bound.fields[208]=receiver
        return bound

    def builtin_type(self,tag):
        assert tag==abi.PY_TYPE_TUPLE
        result=self.global_value('pcc_type_cls_tuple');self.retain(result);return result

    def issubclass(self,child,parent):
        assert child.alive and parent.alive
        self.collect('callback')
        mro=child.fields[abi.PYCLASSOBJECT_MRO_OFFSET]
        return int(any(value is parent for value in mro.fields.values()))

    def method(self,name,function=None,operation=None):
        fn=self.make(function,abi.PY_TYPE_FUNC);fn.fields[12]=0
        if operation is not None:
            captures=self.make((operation,),abi.PY_TYPE_TUPLE)
            captures.fields.update({12:0,16:1,24:operation})
            fn.fields[56]=self.ns['_tuple_method_entry'];fn.fields[64]=captures
        return fn

    def install(self,cls,name,value):
        attrs=cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET]
        if attrs is None:
            attrs=self.make({},abi.PY_TYPE_DICT);attrs.fields[12]=0
            attrs.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]=Block()
            attrs.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET]=0
            cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET]=attrs
        entries=attrs.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]
        index=attrs.value.get(name)
        if index is None:
            index=attrs.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET]
            attrs.value[name]=index;attrs.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET]=index+1
            key=self.make(name,abi.PY_TYPE_STR);key.fields[12]=0
            key.fields[abi.PYSTROBJECT_BYTE_LEN_OFFSET]=len(name)
            for i,char in enumerate(name):key.fields[abi.PYSTROBJECT_DATA_OFFSET+i]=ord(char)
            entries.fields[index*abi.DICTENTRY_SIZE+abi.DICTENTRY_KEY_OFFSET]=key
        slot=index*abi.DICTENTRY_SIZE+abi.DICTENTRY_VALUE_OFFSET
        old=entries.fields.get(slot)
        self.release_value(old);self.retain(value);entries.fields[slot]=value

    def call_slots(self,callable_slot,args_slot,kwargs_slot,result_slot):
        fn=self.read(callable_slot,0)
        child=False
        if isinstance(fn,Object) and fn.tag==abi.PY_TYPE_STATICMETHOD:
            # The mocked public slot-call kernel acquires the function child
            # independently, as its actual _special_copy contract requires.
            fn=self.read(fn,abi.PYSTATICMETHODOBJECT_FUNC_OFFSET)
            fn.refs+=1;fn.leases+=1;child=True
        try:
            args=self.read(args_slot,0)
            values=[self.read(args,24+i*8) for i in range(self.read(args,16))] if args is not None else []
            self.collect('callback')
            if not isinstance(fn,Object) or not callable(fn.value):self.error=(3,'not callable');return -1
            assert fn.alive and fn.leases>0
            keywords={}
            if kwargs_slot is not None:
                kwargs=self.read(kwargs_slot,0)
                if kwargs is not None and kwargs is not self.none:keywords=dict(kwargs.value)
            try:result=fn.value(*values,**keywords)
            except Exception as error:self.error=(7,str(error));return -1
            if result is NotImplemented:self.retain(self.not_implemented);result=self.not_implemented
            else:result=self.wrap(result)
            self.write(result_slot,0,result)
            return 0
        finally:
            if child:fn.leases-=1;fn.refs-=1

    def make_instance(self,cls):
        result=self.make(None,abi.PY_TYPE_INSTANCE)
        result.fields.update({12:0,abi.PYINSTANCEOBJECT_CLS_OFFSET:cls})
        return result

    def roots_for(self,*values):
        block=Block()
        serial=len(self.roots)
        for index,value in enumerate(values):
            block.fields[index*8]=value;self.roots[('selection',serial,index)]=self.add(block,index*8)
        return block

    def binary(self,left,right,name='__add__',rname='__radd__',mode=0):
        caller=self.roots_for(left,right,None)
        handled=Block();handled.fields[0]=0
        status=self.ns['py_obj_binary_special_call_slots'](caller,self.add(caller,8),name,rname,mode,self.add(caller,16),handled)
        assert not self.frame_handles and self.depth==0
        assert all(obj.leases==0 for obj in self.objects if obj.alive and obj not in (self.none,self.not_implemented))
        return status,self.read(caller,16),self.read(handled,0)


def test_binary_owned_selection_observes_c3_and_reflected_priority():
    memory=SelectionMemory('callback')
    base=memory.klass('Base',[]);derived=memory.klass('Derived',[base])
    memory.install(base,'__add__',memory.method('add',lambda self,other:memory.calls.append('left') or 'left'))
    memory.install(base,'__radd__',memory.method('radd',lambda self,other:memory.calls.append('inherited') or 'base-reflected'))
    memory.install(derived,'__radd__',memory.method('radd',lambda self,other:memory.calls.append('right') or 'right'))
    status,result,handled=memory.binary(memory.make_instance(base),memory.make_instance(derived))
    assert status==0 and handled==1 and memory.value(result)=='right' and memory.calls==['right']


def test_binary_selected_method_survives_namespace_replacement_and_notimplemented():
    memory=SelectionMemory('callback')
    left_cls=memory.klass('Left',[]);right_cls=memory.klass('Right',[])
    def first(left,right):
        memory.install(right.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET],'__radd__',memory.method('new',lambda self,other:'replacement'))
        return NotImplemented
    memory.install(left_cls,'__add__',memory.method('first',first))
    memory.install(right_cls,'__radd__',memory.method('old',lambda self,other:'selected-owner'))
    status,result,handled=memory.binary(memory.make_instance(left_cls),memory.make_instance(right_cls))
    assert status==0 and handled==1 and memory.value(result)=='selected-owner'


@pytest.mark.parametrize('mode,expected_calls',((0,['left']),(1,['right','left'])))
def test_binary_inherited_reflection_priority_differs_for_comparison(mode,expected_calls):
    memory=SelectionMemory('callback')
    base=memory.klass('Base',[]);derived=memory.klass('Derived',[base])
    memory.install(base,'__add__',memory.method('left',lambda self,other:memory.calls.append('left') or 'done'))
    memory.install(base,'__radd__',memory.method('right',lambda self,other:memory.calls.append('right') or NotImplemented))
    status,result,handled=memory.binary(memory.make_instance(base),memory.make_instance(derived),mode=mode)
    assert status==0 and handled==1 and memory.calls==expected_calls


def test_binary_none_is_present_and_same_type_suppresses_arithmetic_reflection():
    memory=SelectionMemory('callback')
    cls=memory.klass('One',[])
    memory.install(cls,'__add__',memory.none)
    status,result,handled=memory.binary(memory.make_instance(cls),memory.make_instance(cls))
    assert status==-1 and result is None and handled==1 and memory.error[0]==3
    memory=SelectionMemory('callback');cls=memory.klass('One',[])
    memory.install(cls,'__add__',memory.method('left',lambda self,other:NotImplemented))
    memory.install(cls,'__radd__',memory.method('right',lambda self,other:pytest.fail('same-type reflection')))
    status,result,handled=memory.binary(memory.make_instance(cls),memory.make_instance(cls))
    assert status==0 and result is None and handled==0


@pytest.mark.parametrize('operation,name,rname',((13,'__add__','__radd__'),(14,'__mul__','__rmul__')))
def test_builtin_tuple_sequence_fallback_runs_foreign_reflection_first(operation,name,rname):
    memory=SelectionMemory('callback')
    foreign=memory.klass('Foreign',[])
    default=memory.method('sequence',lambda self,other:pytest.fail('sequence ran before reflected'),operation=operation)
    memory.install(memory.builtin,name,default)
    memory.install(foreign,rname,memory.method('reflected',lambda self,other:'reflected'))
    plain=memory.wrap((1,))
    status,result,handled=memory.binary(plain,memory.make_instance(foreign),name,rname)
    assert status==0 and handled==1 and memory.value(result)=='reflected'


def test_super_owned_lookup_starts_after_origin_and_retains_selected_callable():
    memory=SelectionMemory('callback')
    parent=memory.klass('Parent',[]);child=memory.klass('Child',[parent])
    parent_new=memory.method('parent',lambda cls,value:value)
    static=memory.make(None,abi.PY_TYPE_STATICMETHOD);static.fields[12]=0
    static.fields[abi.PYSTATICMETHODOBJECT_FUNC_OFFSET]=parent_new
    memory.install(parent,'__new__',static)
    memory.install(child,'__new__',memory.method('child',lambda *args:pytest.fail('selected current class')))
    caller=memory.roots_for(child,child,None)
    status=memory.ns['py_super_new_lookup_slots'](caller,memory.add(caller,8),memory.add(caller,16))
    assert status==0 and memory.error is None
    selected=memory.read(caller,16)
    assert selected.value is parent_new.value
    actual_parent=memory.read(memory.read(caller,0).fields[abi.PYCLASSOBJECT_MRO_OFFSET],8)
    memory.install(actual_parent,'__new__',None)
    memory.collect('callback')
    assert memory.read(caller,16).alive
    assert not memory.frame_handles


def test_super_invalid_binding_fails_without_selecting_a_method():
    memory=SelectionMemory('callback')
    cls=memory.klass('One',[]);unrelated=memory.klass('Other',[])
    caller=memory.roots_for(cls,unrelated,None)
    status=memory.ns['py_super_new_lookup_slots'](caller,memory.add(caller,8),memory.add(caller,16))
    assert status==-1 and memory.read(caller,16) is None and memory.error[0]==3


def test_tuple_new_keywords_belong_to_overridden_initializer():
    memory=SelectionMemory('callback')
    memory.install(memory.cls,'__init__',memory.method('init',lambda *args:None))
    result,_=memory.invoke(([1,2],),{'note':'retained-by-init'})
    assert result is not None and memory.values(result)==(1,2) and memory.error is None


class CanonicalTupleMemory(SelectionMemory):
    def __init__(self,phase='copy',failure=None,warm=False):
        super().__init__(phase)
        self.failure=failure;self.locked=False;self.installed=[]
        self.initializations=0
        if not warm:self.global_addr('pcc_type_cls_tuple').fields[0]=None
        self.ns.update({
            '_object_new_cache_mutex':lambda:'owned-mutex',
            'pcc_mutex_lock':self.mutex_lock,'pcc_mutex_unlock':self.mutex_unlock,
            'py_class_new':self.class_new,
            '_object_new_captures':lambda:self.new_tuple(0),
            'py_func_new_bound':self.function_new,
            'py_staticmethod_new':self.staticmethod_new,
            'py_class_write_namespace_slots':self.write_namespace,
            'py_class_add_method':self.add_method,
        })
        names={'_tuple_new_entry','_tuple_type_install_method','_tuple_type_fill','py_tuple_type_new'}
        body=[]
        for node in ast.parse(PORT.read_text()).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('_TUPLE_TYPE_') for t in node.targets):body.append(node)
            elif isinstance(node,ast.FunctionDef) and node.name in names:node.decorator_list=[];body.append(node)
        exec(compile(ast.Module(body=body,type_ignores=[]),str(PORT),'exec'),self.ns)

    def mutex_lock(self,mutex):
        assert mutex=='owned-mutex' and not self.locked
        self.collect('mutex_lock');self.locked=True;return 0

    def mutex_unlock(self,mutex):
        assert self.locked;self.locked=False;return 0

    def class_new(self,name,*args):
        assert self.locked and self.global_value('pcc_type_cls_tuple') is None
        self.initializations+=1
        if self.failure=='class':self.error=(19,'class allocation');return None
        self.collect('allocation')
        return self.klass(name,[])

    def function_new(self,entry,captures,name,self_obj):
        assert self.locked and captures.alive and captures.leases>0
        fn=self.allocate(name,abi.PY_TYPE_FUNC,'function')
        self.retain(captures);fn.fields[56]=entry;fn.fields[64]=captures
        return fn

    def staticmethod_new(self,fn):
        assert fn.alive and fn.leases>0
        result=self.allocate(None,abi.PY_TYPE_STATICMETHOD,'staticmethod')
        self.retain(fn);result.fields[abi.PYSTATICMETHODOBJECT_FUNC_OFFSET]=fn
        return result

    def write_namespace(self,class_slot,name,value_slot,remove):
        assert self.locked and self.global_value('pcc_type_cls_tuple') is None and remove==0
        cls=self.read(class_slot,0);value=self.read(value_slot,0)
        assert cls.leases>0 and value.leases>0
        self.collect('callback')
        if self.failure=='namespace' and name=='__hash__':self.error=(7,'namespace write');return -1
        self.install(cls,name,value)
        self.installed.append(name);return 0

    def add_method(self,cls,name,fn):
        assert self.locked and cls.leases>0 and fn.leases>0
        attrs=cls.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET]
        entry=attrs.value[name]
        owned=attrs.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET].fields[entry*abi.DICTENTRY_SIZE+abi.DICTENTRY_VALUE_OFFSET]
        if owned.tag==abi.PY_TYPE_STATICMETHOD:owned=owned.fields[abi.PYSTATICMETHODOBJECT_FUNC_OFFSET]
        assert owned is fn, 'method alias published without namespace owner'
        if self.failure=='table' and name=='__hash__':return
        count=cls.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET]
        methods=cls.fields[abi.PYCLASSOBJECT_METHODS_OFFSET]
        if methods is None:methods=Block();cls.fields[abi.PYCLASSOBJECT_METHODS_OFFSET]=methods
        methods.fields[count*abi.PYCLASSMETHOD_SIZE+abi.PYCLASSMETHOD_NAME_OFFSET]=name
        methods.fields[count*abi.PYCLASSMETHOD_SIZE+abi.PYCLASSMETHOD_FUNC_OFFSET]=fn
        cls.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET]=count+1


@pytest.mark.parametrize('phase',('register','copy','acquire','callback','release','allocation','mutex_lock'))
def test_canonical_tuple_class_publishes_only_completed_owned_descriptors(phase):
    memory=CanonicalTupleMemory(phase)
    result=memory.ns['py_tuple_type_new']()
    assert result.alive and result is memory.global_value('pcc_type_cls_tuple')
    assert memory.initializations==1 and result.refs==2
    assert len(memory.installed)==19
    assert not {'__bool__','__str__','__radd__'} & set(memory.installed)
    assert result.fields[abi.PYCLASSOBJECT_N_METHODS_OFFSET]==19
    methods=result.fields[abi.PYCLASSOBJECT_METHODS_OFFSET]
    for index in range(19):
        fn=methods.fields[index*abi.PYCLASSMETHOD_SIZE+abi.PYCLASSMETHOD_FUNC_OFFSET]
        assert fn.alive and fn.refs==1 and fn.fields[64].refs==1
    assert not memory.locked and not memory.frame_handles
    assert all(obj.leases==0 for obj in memory.objects if obj.alive and obj not in (memory.none,memory.not_implemented))


@pytest.mark.parametrize('failure',('class','namespace','table'))
def test_canonical_tuple_failure_never_publishes_partial_class(failure):
    memory=CanonicalTupleMemory('callback',failure)
    result=memory.ns['py_tuple_type_new']()
    assert result is None and memory.global_value('pcc_type_cls_tuple') is None
    assert memory.error is not None and not memory.locked and not memory.frame_handles


def test_canonical_tuple_warm_read_occurs_after_moving_mutex_acquisition():
    memory=CanonicalTupleMemory('mutex_lock',warm=True)
    old=memory.global_value('pcc_type_cls_tuple')
    result=memory.ns['py_tuple_type_new']()
    assert memory.moves>0 and result is not old and result.alive
    assert result is memory.global_value('pcc_type_cls_tuple') and memory.initializations==0
    assert not memory.locked and not memory.frame_handles


@pytest.mark.parametrize('phase',('copy','callback','release'))
def test_tuple_class_call_owns_dynamic_new_init_and_explicit_class_once(phase):
    memory=SelectionMemory(phase)
    cls=memory.cls
    def make(actual,value,**kwargs):
        assert actual.leases>0 and value==7
        memory.calls.append(('new',actual.value,kwargs))
        return memory.make_instance(actual)
    def initialize(receiver,value,**kwargs):
        assert receiver.leases>0 and value==7
        memory.calls.append(('init',receiver.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET].value,kwargs))
        return None
    new=memory.method('new',make)
    descriptor=memory.make(None,abi.PY_TYPE_STATICMETHOD);descriptor.fields[12]=0
    descriptor.fields[abi.PYSTATICMETHODOBJECT_FUNC_OFFSET]=new
    memory.install(cls,'__new__',descriptor)
    memory.install(cls,'__init__',memory.method('init',initialize))
    args=memory.wrap((7,));kwargs=memory.wrap({'note':9})
    caller=memory.roots_for(cls,args,kwargs)
    result=memory.ns['py_tuple_class_call'](*[memory.read(caller,i*8) for i in range(3)])
    assert result is not None and memory.error is None
    assert result.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] is memory.read(caller,0)
    assert memory.calls==[('new','Child',{'note':9}),('init','Child',{'note':9})]
    assert not memory.frame_handles


def test_tuple_class_call_skips_init_for_foreign_new_result():
    memory=SelectionMemory('callback')
    memory.install(memory.cls,'__new__',memory.method('new',lambda actual,*args:37))
    memory.install(memory.cls,'__init__',memory.method('init',lambda *args:pytest.fail('foreign result initialized')))
    caller=memory.roots_for(memory.cls,memory.wrap(()),memory.none)
    result=memory.ns['py_tuple_class_call'](*[memory.read(caller,i*8) for i in range(3)])
    assert result==37 and memory.error is None


def test_tuple_class_call_rejects_non_none_initializer_result():
    memory=SelectionMemory('callback')
    memory.install(memory.cls,'__new__',memory.method('new',lambda actual,*args:memory.make_instance(actual)))
    memory.install(memory.cls,'__init__',memory.method('init',lambda *args:1))
    caller=memory.roots_for(memory.cls,memory.wrap(()),memory.none)
    result=memory.ns['py_tuple_class_call'](*[memory.read(caller,i*8) for i in range(3)])
    assert result is None and memory.error[0]==3 and not memory.frame_handles


def test_arithmetic_priority_compares_staticmethod_functions_not_wrapper_identity():
    memory=SelectionMemory('callback')
    base=memory.klass('Base',[]);child=memory.klass('Child',[base])
    fn=memory.method('radd',lambda other:memory.calls.append('right') or 'right')
    for cls in (base,child):
        descriptor=memory.make(None,abi.PY_TYPE_STATICMETHOD);descriptor.fields[12]=0
        descriptor.fields[abi.PYSTATICMETHODOBJECT_FUNC_OFFSET]=fn
        memory.install(cls,'__radd__',descriptor)
    memory.install(base,'__add__',memory.method('add',lambda self,other:memory.calls.append('left') or 'left'))
    status,result,handled=memory.binary(memory.make_instance(base),memory.make_instance(child))
    assert status==0 and handled==1 and memory.value(result)=='left' and memory.calls==['left']


def test_arithmetic_priority_binds_inherited_classmethod_to_each_class():
    memory=SelectionMemory('callback')
    base=memory.klass('Base',[]);child=memory.klass('Child',[base])
    fn=memory.method('radd',lambda cls,other:memory.calls.append(cls.value) or 'right')
    descriptor=memory.make(None,abi.PY_TYPE_CLASSMETHOD);descriptor.fields[12]=0
    descriptor.fields[abi.PYCLASSMETHODOBJECT_FUNC_OFFSET]=fn
    memory.install(base,'__radd__',descriptor)
    memory.install(base,'__add__',memory.method('add',lambda self,other:memory.calls.append('left') or 'left'))
    status,result,handled=memory.binary(memory.make_instance(base),memory.make_instance(child))
    assert status==0 and handled==1 and memory.value(result)=='right' and memory.calls==['Child']


def test_exact_tuple_attribute_produces_bound_owned_callable_and_absence():
    memory=SelectionMemory('callback')
    method=memory.method('count',lambda self,item:1)
    memory.install(memory.builtin,'count',method)
    caller=memory.roots_for(memory.wrap((1,2)))
    bound=memory.ns['py_tuple_getattr'](memory.read(caller,0),'count')
    assert bound is not None and bound.alive and memory.error is None
    assert bound.fields[208] is memory.read(caller,0)
    assert bound.fields[200].value is method.value
    missing=memory.ns['py_tuple_getattr'](memory.read(caller,0),'missing')
    assert missing is None and memory.error is None and not memory.frame_handles
