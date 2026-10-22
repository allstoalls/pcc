"""Actual eager-dict materialization bodies against traced ownership memory."""
import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_dict_super_slot_roots import DictMemory
from tests.python.test_set_call_slot_roots import Block, Object


PORT = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_dict.py'


class MaterializeMemory(DictMemory):
    def __init__(self, phase='register', **faults):
        super().__init__(phase, **faults)
        self.fail_stage = None
        self.runtime_active = False
        self.after_append = None
        self.retired_tables = set()
        self.frames = []
        self.ns.update(
            pcc_gc_frame_enter=self.frame_enter,
            pcc_gc_frame_leave=self.frame_leave,
            pcc_gc_pin=self.pin,
            pcc_gc_take_pinned_slot=self.take,
            global_addr=lambda name: name,
            py_list_new=self.new_list,
            py_tuple_new=self.new_tuple,
            py_list_append=self.append_list,
            py_tuple_set_item=self.set_tuple,
            pcc_gc_store_root=self.store_root,
        )
        tree = ast.parse(PORT.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id.startswith('_DICT_MATERIALIZE_'):
                    self.ns[node.targets[0].id] = ast.literal_eval(node.value)
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and (node.name.startswith('_dict_materialize_') or node.name in ('py_dict_keys','py_dict_values','py_dict_items'))]
        for node in selected:
            node.decorator_list = []
        exec(compile(ast.Module(body=selected,type_ignores=[]),str(PORT),'exec'),self.ns)

    def read(self, pointer, offset):
        if isinstance(pointer, str):
            assert pointer == "pcc_gc_read_barrier_enabled" and offset == 0
            return 0
        block, _ = self.location(pointer, offset)
        assert block not in getattr(self, 'retired_tables', ()), 'stale dictionary table'
        return super().read(pointer, offset)

    def fail(self, stage):
        if self.runtime_active and self.fail_stage == stage:
            self.error = (19, stage)
            return True
        return False

    def new_list(self, size):
        if self.fail('list'):
            return None
        return super().new_list(size)

    def new_tuple(self, size):
        if self.fail('pair'):
            return None
        return super().new_tuple(size)

    def set_tuple(self, obj, index, value):
        assert obj.leases > 0
        if isinstance(value, Object):
            assert value.leases > 0
        if self.fail('pair'+str(index)):
            return
        super().set_tuple(obj,index,value)

    def append_list(self, obj, value):
        self.collect('callback')
        if self.fail('append'):
            return
        super().append_list(obj,value)
        if self.after_append:
            callback,self.after_append=self.after_append,None
            callback()

    def drop(self, obj):
        obj.refs -= 1
        assert obj.refs >= 0
        if obj.refs:
            return
        if obj.tag == abi.PY_TYPE_LIST:
            count=obj.fields[abi.PYLISTOBJECT_LENGTH_OFFSET]
            children=[self.read(obj.fields[abi.PYLISTOBJECT_ITEMS_OFFSET],index*8) for index in range(count)]
        elif obj.tag == abi.PY_TYPE_TUPLE:
            children=[self.read(obj,abi.PYTUPLEOBJECT_ITEMS_OFFSET+index*8) for index in range(obj.fields[abi.PYTUPLEOBJECT_LEN_OFFSET])]
        else:
            children=[]
        for child in children:
            if isinstance(child,Object):
                self.drop(child)

    def store_root(self, slot, value):
        self.collect('drop')
        old=self.read(slot,0)
        self.write(slot,0,value)
        if isinstance(old,Object):
            self.drop(old)
        if isinstance(value,Object):
            value.refs+=1

    def frame_enter(self, name, slots):
        count=1 if name.endswith('borrowed_map') else 2
        handles=[]
        for index in range(count):
            handle=object();self.roots[handle]=self.add(slots,index*8);handles.append(handle)
        self.frames.append((slots,handles))
        self.collect('frame')

    def frame_leave(self, slots):
        actual,handles=self.frames.pop()
        assert actual is slots
        for handle in handles:
            del self.roots[handle]
        self.collect('frame')

    def pin(self,obj):
        assert obj.leases>0
        obj.fields[abi.PYOBJECTHEADER_FLAGS_OFFSET] |= abi.PY_FLAG_GC_PINNED
        obj.leases+=1

    def take(self,slot,prior):
        value=self.read(slot,0)
        if isinstance(value,Object):
            assert value.fields[abi.PYOBJECTHEADER_FLAGS_OFFSET] & abi.PY_FLAG_GC_PINNED
            value.fields[abi.PYOBJECTHEADER_FLAGS_OFFSET]=prior
            value.leases-=1
        self.write(slot,0,None)
        return value

    def materialize(self, data, kind, raw=False):
        receiver=self.wrap(data)
        caller=self.input_roots([receiver,None])
        source,result=caller,self.add(caller,8)
        expected_handles=set(self.roots)
        self.runtime_active=True
        if raw:
            token=self.acquire(source)
            value=self.ns['py_dict_'+kind](self.read(source,0))
            self.release(source,token)
        else:
            status=self.ns['_dict_materialize_bound'](source,result,{'keys':0,'values':1,'items':2}[kind])
            value=self.read(result,0)
            assert status == (-1 if self.error is not None else 0)
        assert set(self.roots)==expected_handles and not self.frames
        assert all(obj.leases==0 for obj in self.objects if obj is not self.none)
        return value,source


@pytest.mark.parametrize('kind', ['keys','values','items'])
@pytest.mark.parametrize('phase', ['register','copy','acquire','release','drop','callback'])
def test_materialized_dict_children_survive_every_boundary(kind,phase):
    model=MaterializeMemory(phase)
    value,source=model.materialize({'a':'first','b':'second'},kind)
    expected={'keys':['a','b'],'values':['first','second'],'items':[('a','first'),('b','second')]}[kind]
    assert model.unwrap(value)==expected
    assert value.refs==1
    assert model.unwrap(model.read(source,0))=={'a':'first','b':'second'}
    assert model.error is None


@pytest.mark.parametrize('stage', ['list','pair','pair0','pair1','append'])
def test_allocation_and_append_errors_release_partial_outputs(stage):
    model=MaterializeMemory('release');model.fail_stage=stage
    value,source=model.materialize({'a':'first'},'items')
    assert value is None and model.error==(19,stage)
    assert model.unwrap(model.read(source,0))=={'a':'first'}
    assert all(obj.refs==0 for obj in model.objects if obj.alive and obj.tag in (abi.PY_TYPE_LIST,abi.PY_TYPE_TUPLE))


@pytest.mark.parametrize('failed_copy',[1,2,3])
def test_failed_source_or_pair_copy_does_not_leak_a_lease(failed_copy):
    model=MaterializeMemory('release',fail_copy=failed_copy)
    result,_=model.materialize({'a':'first'},'items')
    assert result is None and model.error


@pytest.mark.parametrize('registration',[1,2,8,16])
def test_partial_frame_registration_is_balanced(registration):
    model=MaterializeMemory('register',fail_register=registration)
    result,_=model.materialize({'a':'first'},'items')
    assert result is None and model.error


@pytest.mark.parametrize('kind',['keys','values','items'])
def test_existing_raw_abi_keeps_result_owned_through_terminal_return(kind):
    model=MaterializeMemory('frame')
    result,_=model.materialize({'a':'first'},kind,raw=True)
    assert model.unwrap(result)=={'keys':['a'],'values':['first'],'items':[('a','first')]}[kind]
    assert result.refs==1


@pytest.mark.parametrize('kind',['keys','values','items'])
def test_table_replacement_does_not_leave_a_borrowed_payload_address(kind):
    model=MaterializeMemory('none')
    receiver=model.wrap({'a':'first','b':'second'})
    caller=model.input_roots([receiver,None]);result=model.add(caller,8)
    def replace_table():
        current=model.read(caller,0)
        old=current.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]
        replacement=Block();replacement.fields=old.fields.copy()
        offset=abi.DICTENTRY_SIZE+abi.DICTENTRY_VALUE_OFFSET
        prior=replacement.fields[offset]
        replacement.fields[offset]=model.wrap('changed')
        replacement.fields[offset].refs+=1
        prior.refs-=1
        current.fields[abi.PYDICTOBJECT_ENTRIES_OFFSET]=replacement
        model.retired_tables.add(old)
    model.after_append=replace_table
    model.runtime_active=True
    assert model.ns['_dict_materialize_bound'](caller,result,{'keys':0,'values':1,'items':2}[kind])==0
    assert model.unwrap(model.read(result,0))=={
        'keys':['a','b'],'values':['first','changed'],'items':[('a','first'),('b','changed')]}[kind]
    assert len(model.roots)==2 and model.depth==0


@pytest.mark.parametrize('failed',[False,True])
def test_output_cleanup_preserves_selected_or_entry_exception(failed):
    model=MaterializeMemory('release')
    receiver=model.wrap({'a':'first'})
    caller=model.input_roots([receiver,None]);result=model.add(caller,8)
    previous=(2,'entry');cleanup=(7,'cleanup')
    model.error=previous
    model.fail_stage='append' if failed else None
    model.runtime_active=True
    drop=model.ns['pcc_gc_store_root']
    def drop_with_error(slot,value):
        old=model.read(slot,0)
        drop(slot,value)
        if isinstance(old,Object) and old.tag==abi.PY_TYPE_DICT and value is None:
            model.error=cleanup
    model.ns['pcc_gc_store_root']=drop_with_error
    assert model.ns['_dict_materialize_bound'](caller,result,2)==(-1 if failed else 0)
    assert model.error==((19,'append') if failed else previous)
    assert len(model.roots)==2


def test_terminal_return_lease_failure_preserves_error_while_dropping_output():
    model=MaterializeMemory('none')
    acquire=model.ns['pcc_gc_foreign_lease_acquire']
    def acquire_final(slot):
        value=model.read(slot,0)
        if isinstance(value,Object) and value.tag==abi.PY_TYPE_LIST and len(model.roots)==5:
            return -2
        return acquire(slot)
    model.ns['pcc_gc_foreign_lease_acquire']=acquire_final
    drop=model.ns['pcc_gc_store_root']
    def drop_with_error(slot,value):
        old=model.read(slot,0)
        drop(slot,value)
        if isinstance(old,Object) and old.tag==abi.PY_TYPE_LIST and value is None:
            model.error=(7,'output finalizer')
    model.ns['pcc_gc_store_root']=drop_with_error
    result,_=model.materialize({'a':'first'},'items',raw=True)
    assert result is None
    assert model.error==(15,'dictionary result return lease failed')
