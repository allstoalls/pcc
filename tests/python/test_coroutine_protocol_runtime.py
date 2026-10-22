"""Coroutine protocol validation and owner cleanup, independent of native builds."""
from __future__ import annotations

import ast
import inspect
import os
import re
from pathlib import Path

import pytest


class Memory:
    def __init__(self):
        self.fields = {}


class NativeTuple(list):
    pass


class Generator(Memory):
    def __init__(self, value):
        super().__init__()
        self.value = value


class Runtime:
    """Execute the real port functions over a checked slot/lease model."""
    def __init__(self):
        self.none = object()
        self.error = None
        self.leases = {}
        self.next_token = 1
        self.flags = {}
        self.frames = set()
        self.finalizer_error = None
        self.saw_pending_args = 0
        path = Path(os.environ.get('PCC_COROUTINE_MODEL_SOURCE', Path(__file__).parents[2] / 'pcc/runtime/py/py_coroutine.py'))
        header = path.parent.parent / 'include/py_runtime.h'
        tags = {name: int(value, 0) for name, value in re.findall(r'(PY_(?:TYPE|EXC)_[A-Z_]+)\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)', header.read_text())}
        self.tags = tags
        exception_kinds = {tags['PY_EXC_TYPEERROR']: TypeError, tags['PY_EXC_VALUEERROR']: ValueError, tags['PY_EXC_RUNTIMEERROR']: RuntimeError}
        tree = ast.parse(path.read_text())
        selected = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
                selected.append(node)
            elif isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id.startswith(('_CORO_', '_GEN_')) for x in node.targets):
                selected.append(node)
        self.ns = dict(
            PYOBJECTHEADER_FLAGS_OFFSET=12, PYOBJECTHEADER_TYPE_TAG_OFFSET=8,
            PY_TYPE_INT=1, PY_TYPE_COROUTINE=2, PY_TYPE_GEN=3, PY_TYPE_CLASS=4, PY_TYPE_TUPLE=5, PY_TYPE_EXC=6,
            null=lambda: None, ptr_is_null=lambda x: x is None, ptr_eq=lambda a,b: a is b,
            is_tagged_int=lambda x: type(x) is int, cstr=lambda x: x,
            stack_alloc=lambda size: Memory(), memset=lambda p,v,n: None,
            ptr_add=lambda p,o: (p[0],p[1]+o) if isinstance(p,tuple) else (p,o),
            load_ptr=self.load, store_ptr=self.store, load_i32=lambda p,o: self.load(p,o) or 0, load_i64=lambda p,o: self.load(p,o) or 0,
            store_i32=self.store, store_i64=self.store,
            global_addr=lambda name: name, global_load_ptr=lambda name: self.none,
            pcc_gc_frame_enter=lambda m,s: self.frames.add(s), pcc_gc_frame_leave=lambda s: self.frames.remove(s),
            pcc_gc_store_root=self.store_root, pcc_gc_load_ptr=lambda owner,p: self.load(p,0),
            pcc_gc_store_ptr=self.store_field,
            pcc_gc_root_copy_borrowed_lease=self.borrow,
            pcc_gc_foreign_lease_acquire=self.acquire, pcc_gc_foreign_lease_release=self.release,
            pcc_gc_pin=lambda v: self.flags.__setitem__(id(v),64),
            pcc_gc_unpin=lambda v: self.flags.__setitem__(id(v),0),
            atomic_rmw_i32=lambda op,v,o,b,order: self.flags.__setitem__(id(v), self.flags.get(id(v),0)|b),
            pcc_gc_take_pinned_slot=self.take, pcc_platform_abort=lambda: pytest.fail('owner contract aborted'),
            py_tls_exc_swap_slot=self.swap, py_clear_exception=lambda: setattr(self,'error',None),
            py_current_exception=lambda: self.error, py_raise=lambda e: setattr(self,'error',e),
            py_raise_owned=lambda e: setattr(self,'error',e),
            py_exc_new=lambda tag,msg: exception_kinds[tag](msg or ''),
            py_exc_new_with_value=lambda tag,value: StopIteration(value),
            py_exc_builtin_class=lambda tag: {0:BaseException,6:AttributeError,8:StopIteration,55:GeneratorExit}[tag],
            py_exc_matches=lambda e,c: int(issubclass(e,c) if isinstance(e,type) else isinstance(e,c)),
            py_exc_get_message=lambda e: e.value if isinstance(e,StopIteration) else self.none,
            py_incref=lambda o: None, py_decref=lambda o: None,
            py_tuple_len=lambda o: len(o) if o is not None else 0,
            py_tuple_get=lambda o,i: o[i], py_tuple_new=lambda n: NativeTuple([None]*n),
            py_tuple_set_item=lambda o,i,v: o.__setitem__(i,v),
            py_obj_call=self.call, py_obj_getattr=self.getattr,
            py_exc_new_with_class=lambda cls,msg: cls(),
            py_func_new_bound=lambda entry,captures,name,coro: lambda *args: entry(captures,NativeTuple(args)),
            py_gen_send=lambda gen,v: self.resume(gen,'send',None if v is self.none else v),
            py_gen_throw=lambda gen,e: self.resume(gen,'throw',e), py_gen_close=lambda gen: self.resume(gen,'close'),
            call_ptr2=lambda entry,captures,args: entry(captures,args),
            py_runtime_error_if_unset=lambda helper,msg: setattr(self,'error',self.error or RuntimeError(msg)),
        )
        self.ns.update({name:value for name,value in tags.items() if name.startswith('PY_TYPE_')})
        exec(compile(ast.Module(selected,type_ignores=[]),str(path),'exec'),self.ns)


    def load(self, obj, offset):
        if isinstance(obj,tuple):
            obj,base = obj
            offset += base
        if offset == 12:
            return self.flags.get(id(obj),0)
        if offset == 8 and not isinstance(obj,Memory):
            if isinstance(obj,type): return self.tags['PY_TYPE_CLASS']
            if isinstance(obj,NativeTuple): return self.tags['PY_TYPE_TUPLE']
            if isinstance(obj,BaseException): return self.tags['PY_TYPE_EXC']
            return 0
        if isinstance(obj,BaseException):
            if offset == 16: return type(obj)
            if offset == 24: return obj.value if isinstance(obj,StopIteration) else getattr(obj,'carrier_args',None)
        if isinstance(obj,Generator) and offset == 40:
            return int(inspect.getgeneratorstate(obj.value) == 'GEN_CLOSED')
        return obj.fields.get(offset,0 if offset in (8,12,56,60) else None)

    def store(self,obj,offset,value):
        if isinstance(obj,tuple):
            obj,base = obj
            offset += base
        if isinstance(obj,BaseException) and offset == 24:
            obj.carrier_args=value
        else:
            obj.fields[offset]=value

    def store_root(self,ptr,value):
        self.store(ptr,0,value)

    def store_field(self,owner,ptr,value):
        self.store(ptr,0,value)
        if isinstance(owner,Memory) and owner.fields.get(60) == 1 and self.finalizer_error is not None:
            self.error = self.finalizer_error

    def acquire(self,ptr):
        if self.load(ptr,0) is None: return 0
        token=self.next_token
        self.next_token+=1
        self.leases[token]=ptr
        return token

    def borrow(self,dest,source):
        self.store(dest,0,self.load(source,0))
        return self.acquire(dest)

    def release(self,ptr,token):
        if token:
            assert self.leases.pop(token) == ptr
        return 0

    def take(self,ptr,prior):
        result=self.load(ptr,0)
        if result is not None:
            self.flags[id(result)]=max(prior,0)
        self.store(ptr,0,None)
        return result

    def swap(self,slot):
        value=self.load(slot,0)
        self.store(slot,0,self.error)
        self.error=value

    def getattr(self,obj,name):
        try: return getattr(obj,name)
        except BaseException as error:
            self.error=error
            return None

    def call(self,cls,args,kwargs):
        try: return cls(*args)
        except BaseException as error:
            self.error=error
            return None

    def resume(self,gen,method,*args):
        try:
            result=getattr(gen.value,method)(*args)
            if method != 'close': gen.fields[32]=1
            return self.none if result is None else result
        except BaseException as error:
            self.error=error
            return None

    def coro(self,body):
        coro=Memory()
        def factory(captures,args):
            gen=Generator(body())
            gen.fields.update({8:self.tags['PY_TYPE_GEN'],16:self.resume_pending,24:Memory(),32:0,48:self.none})
            return gen
        coro.fields.update({8:self.tags['PY_TYPE_COROUTINE'],24:factory,32:NativeTuple(),40:NativeTuple(),48:None,56:0,60:-1})
        return coro

    def resume_pending(self,gen,frame):
        assert self.error is None, 'throw metadata must never enter pending TLS'
        assert self.ns['py_coroutine_has_throw_arguments'](gen) == 1
        arguments=self.ns['py_coroutine_take_throw_arguments'](gen)
        assert self.ns['py_coroutine_has_throw_arguments'](gen) == 0
        assert gen.fields[48] is self.none
        assert self.error is None
        self.saw_pending_args += 1
        args=tuple(None if item is self.none else tuple(item) if isinstance(item,NativeTuple) else item for item in arguments)
        return self.resume(gen,'throw',*args)

    def method(self,coro,name,*args):
        self.error=None
        bound=self.ns['py_coroutine_bound_method'](coro, {'send':0,'throw':1,'close':2}[name])
        assert callable(bound)
        result=bound(*args)
        assert not self.frames
        assert not self.leases
        return result


def test_invalid_protocol_arguments_leave_fresh_and_suspended_coroutines_usable():
    runtime=Runtime()
    def body():
        yield 'pause'
        return 17
    for started in (False,True):
        coro=runtime.coro(body)
        if started: assert runtime.method(coro,'send',runtime.none) == 'pause'
        state=coro.fields[60]
        for name,args in [('send',()),('send',(runtime.none,1)),('throw',()),('throw',(ValueError,1,2,3)),('close',(1,))] + ([] if started else [('throw',(17,)),('throw',(ValueError('x'),1)),('throw',(ValueError,runtime.none,17))]):
            assert runtime.method(coro,name,*args) is None
            assert isinstance(runtime.error,TypeError)
            assert coro.fields[60] == state
        assert runtime.method(coro,'send',runtime.none) == (None if started else 'pause')
        if started: assert isinstance(runtime.error,StopIteration) and runtime.error.value == 17
        else: runtime.method(coro,'close')


def test_throw_class_instance_and_legacy_value_normalization():
    runtime=Runtime()
    seen=[]
    def body():
        try: yield 'pause'
        except ValueError as error:
            seen.append(error)
            return 19
    marker=ValueError('marker')
    for arguments in [(marker,),(ValueError,),(ValueError,'text'),(ValueError,NativeTuple(['a','b'])),(ValueError,marker),(ValueError,'three',runtime.none)]:
        coro=runtime.coro(body)
        assert runtime.method(coro,'send',runtime.none) == 'pause'
        assert runtime.method(coro,'throw',*arguments) is None
        assert isinstance(runtime.error,StopIteration) and runtime.error.value == 19
        assert isinstance(seen[-1],ValueError)
        if marker in arguments: assert seen[-1] is marker
        if len(arguments)>1 and isinstance(arguments[1],NativeTuple): assert seen[-1].args == ('a','b')


def test_throw_propagates_identity_and_runs_finally_before_finalizer_cleanup():
    runtime=Runtime()
    events=[]
    marker=ValueError('cancel')
    def body():
        try: yield 'pause'
        finally: events.append('finally')
    coro=runtime.coro(body)
    assert runtime.method(coro,'send',runtime.none) == 'pause'
    runtime.finalizer_error=RuntimeError('unrelated finalizer')
    assert runtime.method(coro,'throw',marker) is None
    assert runtime.error is marker
    assert events == ['finally']
    assert coro.fields[32] is None and coro.fields[40] is None
    assert coro.fields[60] == 1


def test_throw_into_fresh_closes_without_executing_and_close_returns_none():
    runtime=Runtime()
    events=[]
    def body():
        events.append('started')
        try: yield 'pause'
        finally: events.append('finally')
    marker=ValueError('fresh')
    coro=runtime.coro(body)
    assert runtime.method(coro,'throw',marker) is None
    assert runtime.error is marker and not events
    assert runtime.method(coro,'send',runtime.none) is None
    assert isinstance(runtime.error,RuntimeError)
    assert runtime.method(coro,'close') is runtime.none
    coro=runtime.coro(body)
    assert runtime.method(coro,'send',runtime.none) == 'pause'
    assert runtime.method(coro,'close') is runtime.none
    assert events == ['started','finally']
    assert runtime.method(coro,'close') is runtime.none


def test_ignored_generator_exit_stays_suspended_and_second_close_completes():
    runtime=Runtime()
    events=[]
    def body():
        try: yield 'pause'
        except GeneratorExit: yield 'ignored'
        finally: events.append('finally')
    coro=runtime.coro(body)
    assert runtime.method(coro,'send',runtime.none) == 'pause'
    assert runtime.method(coro,'close') is None
    assert isinstance(runtime.error,RuntimeError)
    assert coro.fields[60] == -2
    assert runtime.method(coro,'close') is runtime.none
    assert events == ['finally']


def test_send_non_none_and_reentrant_send_leave_state_intact():
    runtime=Runtime()
    def body():
        assert runtime.method(coro,'send',runtime.none) is None
        assert isinstance(runtime.error,ValueError)
        runtime.error=None
        yield 'pause'
    coro=runtime.coro(body)
    assert runtime.method(coro,'send',1) is None
    assert isinstance(runtime.error,TypeError) and coro.fields[60] == -1
    # Nested invocation legitimately has the outer call's registered frames.
    original_method=runtime.method
    def nested(c,name,*args):
        if c.fields[60] == -3:
            return runtime.ns['py_coroutine_send'](c,args[0],None)
        return original_method(c,name,*args)
    runtime.method=nested
    assert runtime.method(coro,'send',runtime.none) == 'pause'
    assert runtime.method(coro,'close') is runtime.none


def test_close_preserves_explicit_return_value():
    runtime=Runtime()
    def body():
        try: yield 'pause'
        except GeneratorExit: return 29
    coro=runtime.coro(body)
    assert runtime.method(coro,'send',runtime.none) == 'pause'
    assert runtime.method(coro,'close') == 29
    assert runtime.method(coro,'close') is runtime.none


def test_custom_await_throw_receives_original_arguments_without_normalizing():
    runtime=Runtime()
    seen=[]
    class Iterator:
        def throw(self,*args):
            seen.append(args)
            return 'custom-yield'
    iterator=Iterator()
    marker=ValueError('marker')
    for arguments in [(17,), (marker,), (ValueError,'original',runtime.none)]:
        assert runtime.ns['py_await_throw_arguments'](iterator,NativeTuple(arguments)) == 'custom-yield'
        assert seen[-1] == arguments
        assert not runtime.frames and not runtime.leases
        assert runtime.error is None


def test_generator_exit_closes_custom_delegate_before_raising_original():
    runtime=Runtime()
    calls=[]
    class Iterator:
        def throw(self,*args):
            pytest.fail('GeneratorExit must select close, not throw')
        def close(self):
            calls.append('close')
            return 99
    marker=GeneratorExit()
    for original in (marker,GeneratorExit):
        runtime.error=None
        assert runtime.ns['py_await_throw_arguments'](Iterator(),NativeTuple([original])) is None
        assert isinstance(runtime.error,GeneratorExit)
        if original is marker: assert runtime.error is marker
        assert not runtime.frames and not runtime.leases
    runtime.error=None
    assert runtime.ns['py_await_step'](Iterator(),runtime.none,marker) is None
    assert runtime.error is marker
    assert calls == ['close','close','close']
    assert not runtime.frames and not runtime.leases


def test_fresh_throw_normalizes_class_values_without_executing_the_body():
    runtime=Runtime()
    def body():
        pytest.fail('fresh throw must not execute coroutine body')
        yield
    marker=ValueError('marker')
    for arguments,expected in [((ValueError,),()),((ValueError,'text'),('text',)),((ValueError,NativeTuple(['a','b'])),('a','b')),((ValueError,marker),('marker',)),((ValueError,'three',runtime.none),('three',))]:
        coro=runtime.coro(body)
        assert runtime.method(coro,'throw',*arguments) is None
        assert isinstance(runtime.error,ValueError) and runtime.error.args == expected
        if marker in arguments: assert runtime.error is marker
        assert coro.fields[60] == 1


def test_exception_constructor_cannot_reenter_fresh_coroutine():
    runtime=Runtime()
    def body():
        pytest.fail('fresh throw must not run the body')
        yield
    coro=runtime.coro(body)
    class Reentrant(ValueError):
        def __init__(self):
            assert runtime.ns['py_coroutine_close'](coro) is None
            assert isinstance(runtime.error,ValueError)
            runtime.error=None
    assert runtime.method(coro,'throw',Reentrant) is None
    assert isinstance(runtime.error,Reentrant)
    assert coro.fields[60] == 1


def test_failed_exception_constructor_restores_fresh_coroutine():
    runtime=Runtime()
    def body():
        yield 'pause'
    coro=runtime.coro(body)
    marker=ValueError('constructor failure')
    class Broken(ValueError):
        def __init__(self):
            raise marker
    assert runtime.method(coro,'throw',Broken) is None
    assert runtime.error is marker and coro.fields[60] == -1
    assert runtime.method(coro,'send',runtime.none) == 'pause'
    assert runtime.method(coro,'close') is runtime.none
