"""Execute protocol/iterator helper bodies in a relocating ownership model.

This is source-body model evidence, not a native runtime or five-GC receipt.
The slot-dispatch kernel is mocked at its documented owning-slot boundary.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'pcc/runtime/py'
TAGS = dict(PY_TYPE_INT=1, PY_TYPE_BOOL=2, PY_TYPE_STR=3,
            PY_TYPE_TUPLE=7, PY_TYPE_ITER=12, PY_TYPE_INSTANCE=11,
            PY_TYPE_USER_CLASS_START=100)


class ConsumerModel:
    def __init__(self, relocate=True):
        self.memory = {}
        self.frames = {}
        self.objects = {}
        self.current = {}
        self.next_address = 100
        self.sequence = 0
        self.pending = None
        self.moves = []
        self.disposed = []
        self.calls = []
        self.callback = None
        self.methods = {}
        self.relocate = relocate
        self.failure = None
        self.copy_count = 0
        self.finalize_error = set()
        for name, tag, value in [('py_True', 2, True), ('py_False', 2, False),
                                 ('py_None', 0, None), ('py_NotImplemented', 0, None)]:
            self.new(name, tag, value, immortal=True)

    def alloc(self, size):
        self.next_address += size + 64
        return self.next_address

    def new(self, name, tag=11, value=None, immortal=False):
        self.park('allocate')
        self.sequence += 1
        pointer = name + '#' + str(self.sequence)
        self.objects[pointer] = dict(name=name, tag=tag, value=value, refs=1,
                                     leases=0, pin=0, immortal=immortal, fields={})
        self.current[name] = pointer
        return pointer

    def obj(self, pointer):
        assert pointer in self.objects, ('stale or invalid pointer', pointer)
        assert self.objects[pointer]['refs'] > 0
        return self.objects[pointer]

    def ptr(self, base, offset):
        if isinstance(base, str):
            self.obj(base)
            return (base, offset)
        if isinstance(base, tuple):
            return (base[0], base[1] + offset)
        return base + offset

    def load(self, base, offset=0):
        address = self.ptr(base, offset)
        if isinstance(address, tuple):
            obj = self.obj(address[0])
            if address[1] == 8:
                return obj['tag']
            if address[1] == 12:
                return obj['pin']
            return obj['fields'].get(address[1])
        return self.memory.get(address)

    def store(self, base, offset, value):
        address = self.ptr(base, offset)
        if isinstance(address, tuple):
            self.obj(address[0])['fields'][address[1]] = value
        else:
            self.memory[address] = value

    def registered(self, address, owning=False):
        return any(base <= address < base + abs(count)*8
                   for base, count in self.frames.items()
                   if not owning or count > 0)

    def park(self, reason):
        if not self.relocate:
            return
        for old, obj in list(self.objects.items()):
            if obj['refs'] <= 0 or obj['leases'] or obj['pin'] or obj['immortal']:
                continue
            slots = [slot for slot, value in self.memory.items()
                     if value == old and self.registered(slot)]
            heap_slots = [(owner, offset) for owner, record in self.objects.items()
                          for offset, value in record['fields'].items() if value == old]
            if not slots and not heap_slots and self.pending != old:
                continue
            new = old + '@'
            self.objects[new] = self.objects.pop(old)
            self.current[obj['name']] = new
            for slot in slots:
                self.memory[slot] = new
            for owner, offset in heap_slots:
                owner = new if owner == old else owner
                self.objects[owner]['fields'][offset] = new
            if self.pending == old:
                self.pending = new
            self.moves.append((reason, old, new))

    def inc(self, value):
        if value is not None:
            self.obj(value)['refs'] += 1

    def dec(self, value):
        if value is None:
            return
        obj = self.obj(value)
        obj['refs'] -= 1
        if obj['refs']:
            return
        assert obj['leases'] == 0
        self.disposed.append(obj['name'])
        for value in list(obj['fields'].values()):
            if isinstance(value, str) and value in self.objects:
                self.dec(value)
        if obj['name'] in self.finalize_error:
            self.pending = self.new('finalizer-error', 80, (7, 'finalizer'))

    def acquire(self, slot):
        assert self.registered(slot, owning=True)
        self.park('acquire')
        value = self.load(slot)
        if value is None or self.obj(value)['immortal']:
            return 0
        if self.failure == 'result-lease' and self.obj(value)['name'] == 'result':
            return -1
        self.obj(value)['leases'] += 1
        return 1

    def release(self, slot, token):
        self.park('release')
        if token:
            self.obj(self.load(slot))['leases'] -= 1
            assert self.obj(self.load(slot))['leases'] >= 0
        return 0

    def copy(self, destination, source):
        assert self.registered(destination, owning=True)
        assert self.load(destination) is None
        self.park('copy')
        self.copy_count += 1
        if self.failure == 'input-copy' and self.copy_count == 1:
            return -1
        value = self.load(source)
        self.inc(value)
        self.store(destination, 0, value)
        return self.acquire(destination)

    def drop(self, slot, value):
        assert value is None
        assert self.registered(slot, owning=True)
        self.park('drop')
        old = self.load(slot)
        self.store(slot, 0, None)
        self.dec(old)

    def heap_store(self, owner, slot, value):
        assert self.obj(owner)['leases']
        if value is not None:
            assert self.obj(value)['leases'] or self.obj(value)['immortal']
        self.park('heap-store')
        old = self.load(slot)
        self.inc(value)
        self.store(slot, 0, value)
        self.dec(old)

    def tuple_new(self, count):
        if self.failure == 'args':
            return None
        return self.new('args', TAGS['PY_TYPE_TUPLE'], count)

    def tuple_set(self, owner, index, value):
        self.heap_store(owner, self.ptr(owner, 24 + index*8), value)

    def special(self, receiver, name, args, kwargs, output, handled):
        assert self.registered(receiver, True) and self.registered(output, True)
        assert self.obj(self.load(receiver))['leases']
        self.park('special-entry')
        self.calls.append(name)
        self.store(handled, 0, int(name in self.methods))
        if name not in self.methods:
            return 0
        if self.methods[name] is None:
            self.pending = self.new('callback-error', 80, (3, 'None is not callable'))
            return -1
        value = self.methods[name](receiver, args)
        if value is None:
            return -1
        self.store(output, 0, value)
        return 0

    def binary(self, left, right, name, reflected, mode, output, handled):
        assert self.obj(self.load(left))['leases']
        assert self.obj(self.load(right))['leases']
        self.calls.append((name, reflected, mode))
        return self.special(left, name, None, None, output, handled)

    def tuple_next(self, wrapper, index):
        assert self.obj(wrapper)['leases']
        if index >= len(self.obj(wrapper)['value']):
            self.pending = self.new('stop', 80, (8, ''))
            return None
        return self.new('item', 1, self.obj(wrapper)['value'][index])

    def env(self):
        def enter(count, slots):
            self.park('frame-enter')
            self.frames[slots] = count
        def leave(slots):
            self.park('frame-leave')
            del self.frames[slots]
        def clear():
            old, self.pending = self.pending, None
            self.dec(old)
        def swap(slot):
            self.park('tls-swap')
            old = self.load(slot)
            self.store(slot, 0, self.pending)
            self.pending = old
        def require(value, helper, message):
            if value is None and self.pending is None:
                self.pending = self.new('runtime-error', 80, (7, message))
            return value
        def pin(value):
            self.obj(value)['pin'] = 64
        def take(slot, prior):
            value = self.load(slot)
            if value is not None:
                self.obj(value)['pin'] = prior
            self.store(slot, 0, None)
            return value
        def integer(value, overflow):
            assert self.obj(value)['leases'] or self.obj(value)['immortal']
            self.park('convert')
            n = self.obj(value)['value']
            self.store(overflow, 0, int(not -2**63 <= n < 2**63))
            return n
        def truth(value):
            assert self.obj(value)['leases'] or self.obj(value)['immortal']
            self.park('truth')
            return int(bool(self.obj(value)['value']))
        def raise_owned(value):
            clear()
            self.pending = value
        def index_slots(slot):
            value=self.load(slot)
            assert self.registered(slot,True)
            assert self.obj(value)['leases'] or self.obj(value)['immortal']
            self.park('index-convert')
            value=self.load(slot)
            tag=self.obj(value)['tag']
            if tag not in (1,2):
                self.pending=self.new('index-error',80,(3,'not an integer'))
                return 0
            n=self.obj(value)['value']
            if not -2**63 <= n < 2**63:
                self.pending=self.new('overflow',80,(15,'length overflow'))
                return 0
            return n
        env = dict(c_ptr=object, C_POINTER_SIZE=8, PYOBJECTHEADER_FLAGS_OFFSET=12,
                   PY_FLAG_GC_PINNED=64, **TAGS,
                   null=lambda: None, cstr=lambda x: x, ptr_add=self.ptr,
                   ptr_is_null=lambda x: int(x is None), ptr_eq=lambda a,b: int(a==b),
                   is_tagged_int=lambda x: 0, stack_alloc=self.alloc,
                   load_ptr=self.load, store_ptr=self.store, store_i64=self.store,
                   load_i64=lambda p,o: self.load(p,o) or 0,
                   load_i32=lambda p,o: self.load(p,o) or 0, store_i32=self.store,
                   memset=lambda p,v,n: [self.store(p,o,None) for o in range(0,n,8)],
                   global_addr=lambda x: {'pcc_named_protocol_borrowed_map':-3,
                        'pcc_named_protocol_owned_map':7, 'pcc_tuple_iter_borrowed_map':-1,
                        'pcc_tuple_iter_owned_map':5}[x],
                   global_load_ptr=lambda x: self.current[x],
                   pcc_gc_frame_enter=enter, pcc_gc_frame_leave=leave,
                   pcc_gc_foreign_lease_acquire=self.acquire,
                   pcc_gc_foreign_lease_release=self.release,
                   pcc_gc_root_copy_borrowed_lease=self.copy,
                   pcc_gc_root_copy_lease=self.copy,
                   pcc_gc_store_root=self.drop, pcc_gc_store_ptr=self.heap_store,
                   pcc_gc_note_slot_write_barrier=lambda *args: None,
                   pcc_py_gc_minor_graph_lock=lambda: self.park('graph-lock'),
                   pcc_py_gc_minor_graph_unlock=lambda: None,
                   pcc_gc_load_ptr=lambda owner,slot: self.load(slot),
                   pcc_gc_pin=pin, pcc_gc_take_pinned_slot=take,
                   pcc_platform_abort=lambda: pytest.fail('lease invariant abort'),
                   py_tls_exc_swap_slot=swap, py_clear_exception=clear,
                   py_runtime_error_if_unset=lambda helper,message: require(None,helper,message),
                   py_err_occurred=lambda: int(self.pending is not None),
                   py_exc_new=lambda code,message: self.new('exception',80,(code,message)),
                   py_raise_owned=raise_owned, py_tuple_new=self.tuple_new,
                   py_tuple_set_item=self.tuple_set,
                   py_obj_special_call_slots=self.special,
                   py_obj_binary_special_call_slots=self.binary,
                   py_obj_truthy=truth, py_int_to_i64=integer,
                   py_index_i64_checked_slots=index_slots,
                   py_obj_hash=lambda p: hash(self.obj(p)['value']),
                   py_str_payload=lambda p: p if self.obj(p)['tag']==3 else None,
                   py_gc_track=lambda p: self.obj(p),
                   pcc_gc_publish_initialized=lambda p: self.obj(p),
                   pcc_gc_alloc=lambda size,tag,flags: self.new('iterator',tag),
                   py_tuple_iter_next=self.tuple_next,
                   py_exc_builtin_class=lambda code: code,
                   py_exc_matches=lambda exc,cls: int(self.obj(exc)['value'][0]==cls))
        names = {'_write_handled', '_type_of', '_protocol_unary_pin_result',
                 'py_user_special_dispatch', 'py_tuple_iter_new'}
        for filename, prefixes in [('py_protocol_runtime.py', ('_named_', '_NAMED_')),
                                    ('py_iter.py', ('_tuple_iter_', '_TUPLE_ITER_'))]:
            tree=ast.parse((RUNTIME/filename).read_text())
            selected=[]
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and (node.name in names or node.name.startswith(prefixes)):
                    node.decorator_list=[]
                    selected.append(node)
                elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith(prefixes):
                    selected.append(node)
            exec(compile(ast.Module(body=selected,type_ignores=[]),filename,'exec'),env)
        return env

    def assert_clean(self):
        assert not self.frames
        assert all(o['leases']==0 and o['pin']==0 for o in self.objects.values())


@pytest.mark.parametrize('relocate', [False, True])
@pytest.mark.parametrize('result_kind', ['new', 'self', 'arg', 'args'])
def test_named_result_keeps_owner_through_argument_cleanup(relocate, result_kind):
    m=ConsumerModel(relocate)
    receiver=m.new('receiver'); arg=m.new('argument')
    env=m.env()
    def callback(receiver_slot,args_slot):
        choices={'self':m.load(receiver_slot), 'arg':m.current['argument'],
                 'args':m.load(args_slot)}
        if result_kind=='new':
            return m.new('result',3,'text')
        result=choices[result_kind]
        m.inc(result)
        return result
    m.methods['__getitem__']=callback
    handled=m.alloc(8)
    result=env['py_user_special_dispatch'](receiver,'__getitem__',arg,None,1,0,None,handled)
    assert m.obj(result)['name']=={'new':'result','self':'receiver','arg':'argument','args':'args'}[result_kind]
    assert m.load(handled)==1 and m.pending is None
    assert m.obj(m.current['receiver'])['refs']==1+(result_kind=='self')
    assert m.obj(m.current['argument'])['refs']==1+(result_kind in ('arg','args'))
    m.assert_clean()
    if relocate:
        assert m.moves


@pytest.mark.parametrize('kind', ['absent','none','error','silent','input-copy','args','result-lease'])
def test_absence_errors_and_cleanup_are_distinct(kind):
    m=ConsumerModel(); receiver=m.new('receiver'); arg=m.new('argument'); env=m.env()
    m.failure=kind
    if kind=='none':
        m.methods['__len__']=None
    elif kind!='absent':
        def callback(receiver_slot,args_slot):
            if kind in ('error','silent'):
                if kind=='error':
                    m.pending=m.new('callback-error',80,(2,'callback'))
                return None
            return m.new('result',1,3)
        m.methods['__len__']=callback
    handled=m.alloc(8); scalar=m.alloc(8)
    result=env['py_user_special_dispatch'](receiver,'__len__',arg,None,1,1,scalar,handled)
    assert result is None
    assert m.load(handled)==(kind!='absent')
    assert (m.pending is None)==(kind=='absent')
    assert m.obj(m.current['receiver'])['refs']==1
    assert m.obj(m.current['argument'])['refs']==1
    m.assert_clean()


@pytest.mark.parametrize('conversion,tag,value,expected,error', [
    (1,1,5,5,None), (1,1,-1,0,2), (1,1,2**80,0,15),
    (1,3,'bad',0,3), (2,1,7,1,None), (3,1,1,0,3),
    (4,1,2**80,hash(2**80),None), (4,1,2**62,2**62,None),
    (4,1,-1,-2,None), (4,1,-2**63,-2**63,None), (6,1,1,0,3),
])
def test_scalar_validation_holds_result_lease(conversion,tag,value,expected,error):
    m=ConsumerModel(); receiver=m.new('receiver'); env=m.env()
    m.methods['__method__']=lambda r,a: m.new('result',tag,value)
    scalar=m.alloc(8); handled=m.alloc(8)
    result=env['py_user_special_dispatch'](receiver,'__method__',None,None,0,conversion,scalar,handled)
    assert result is None
    assert m.load(scalar)==expected and m.load(handled)==1
    assert (m.obj(m.pending)['value'][0] if m.pending else None)==error
    assert 'result' in m.disposed
    m.assert_clean()


def test_callback_error_survives_operand_finalizer_and_old_exception():
    m=ConsumerModel(); receiver=m.new('receiver'); env=m.env()
    m.pending=m.new('entry-error',80,(7,'entry'))
    m.finalize_error.add('receiver')
    def callback(receiver_slot,args_slot):
        m.dec(m.load(receiver_slot))  # remove the caller owner during callback
        m.pending=m.new('callback-error',80,(2,'callback'))
        return None
    m.methods['__iter__']=callback
    out=env['py_user_special_dispatch'](receiver,'__iter__',None,None,0,0,None,None)
    assert out is None
    assert m.obj(m.pending)['name']=='callback-error'
    assert 'entry-error' in m.disposed and 'receiver' in m.disposed
    m.assert_clean()


def test_tuple_iterator_owns_wrapper_until_first_exhaustion():
    m=ConsumerModel(); wrapper=m.new('wrapper',11,[42]); env=m.env()
    iterator=env['py_tuple_iter_new'](wrapper)
    assert m.obj(m.current['wrapper'])['refs']==2
    m.dec(m.current['wrapper'])
    item=env['_tuple_iter_transaction'](iterator,0)
    assert m.obj(item)['value']==42 and 'wrapper' not in m.disposed
    m.dec(item)
    iterator=m.current['iterator']
    assert env['_tuple_iter_transaction'](iterator,0) is None
    assert m.obj(m.pending)['value'][0]==8
    assert m.disposed.count('wrapper')==1
    assert m.load(m.current['iterator'],16) is None
    env['py_clear_exception']()
    assert env['_tuple_iter_transaction'](m.current['iterator'],0) is None
    assert m.obj(m.pending)['value'][0]==8
    assert m.disposed.count('wrapper')==1
    m.assert_clean()


def test_consumers_use_owned_dispatch_and_canonical_type_publication():
    protocol=ast.parse((RUNTIME/'py_protocol_runtime.py').read_text())
    methods={n.name:n for n in protocol.body if isinstance(n,ast.FunctionDef)}
    for name in ('py_user_len_dispatch','py_user_bool_dispatch','py_user_contains_dispatch',
                 'py_user_getitem_dispatch','py_user_setitem_dispatch','py_user_delitem_dispatch',
                 'py_user_eq_dispatch','py_user_order_dispatch','py_user_binop_dispatch'):
        text=ast.unparse(methods[name])
        assert '_lookup_dunder' not in text and 'py_class_lookup' not in text
        assert 'py_user_special_dispatch' in text or '_named_binary' in text
    dunder=ast.parse((RUNTIME/'py_dunder.py').read_text())
    methods={n.name:n for n in dunder.body if isinstance(n,ast.FunctionDef)}
    for name in ('str','repr','iter','next'):
        text=ast.unparse(methods[f'py_user_{name}_dispatch'])
        assert 'py_user_special_dispatch' in text and 'py_class_lookup' not in text
    source=(RUNTIME/'py_obj_ops_dispatch.py').read_text()
    assert 'return py_tuple_type_new()' in source
    assert 'cls = py_class_new(cstr("tuple")' not in source


@pytest.mark.parametrize('mode,conversion', [(0,0),(1,0),(1,2)])
def test_binary_adapter_retains_both_operands_and_forwards_selection_mode(mode,conversion):
    m=ConsumerModel(); left=m.new('left'); right=m.new('right'); env=m.env()
    m.methods['__eq__']=lambda receiver,args: m.new('result',3,'subclass-result')
    scalar=m.alloc(8); handled=m.alloc(8)
    result=env['_named_binary'](left,right,'__eq__','__eq__',mode,conversion,scalar,handled)
    assert m.calls[0]==('__eq__','__eq__',mode)
    assert m.load(handled)==1
    if conversion==0:
        assert m.obj(result)['value']=='subclass-result'
    else:
        assert result is None and m.load(scalar)==1
    assert m.obj(m.current['left'])['refs']==1
    assert m.obj(m.current['right'])['refs']==1
    m.assert_clean()


def test_each_named_call_observes_the_current_selection_and_none_suppresses():
    m=ConsumerModel(); receiver=m.new('receiver'); env=m.env()
    scalar=m.alloc(8); handled=m.alloc(8)
    def call():
        env['py_user_special_dispatch'](m.current['receiver'],'__len__',None,None,0,1,scalar,handled)
        return m.load(scalar),m.load(handled)
    m.methods['__len__']=lambda r,a: m.new('first',1,2)
    assert call()==(2,1)
    m.methods['__len__']=lambda r,a: m.new('replacement',1,9)
    assert call()==(9,1)
    m.methods['__len__']=None
    assert call()==(0,1) and m.obj(m.pending)['value'][0]==3
    env['py_clear_exception']()
    del m.methods['__len__']
    assert call()==(0,0) and m.pending is None
    m.assert_clean()


def test_ternary_named_call_retains_both_arguments_through_callback():
    m=ConsumerModel(); receiver=m.new('receiver'); key=m.new('key'); value=m.new('value'); env=m.env()
    seen=[]
    def callback(receiver_slot,args_slot):
        args=m.load(args_slot)
        seen.extend(m.obj(m.load(args,offset))['name'] for offset in (24,32))
        assert m.obj(m.current['key'])['leases']
        assert m.obj(m.current['value'])['leases']
        return m.new('result',0,None)
    m.methods['__setitem__']=callback
    env['py_user_special_dispatch'](receiver,'__setitem__',key,value,2,5,None,None)
    assert seen==['key','value'] and 'result' in m.disposed
    for name in ('receiver','key','value'):
        assert m.obj(m.current[name])['refs']==1
    m.assert_clean()


def test_tuple_exhaustion_keeps_stop_iteration_over_wrapper_finalizer_error():
    m=ConsumerModel(); wrapper=m.new('wrapper',11,[]); env=m.env()
    m.finalize_error.add('wrapper')
    iterator=env['py_tuple_iter_new'](wrapper)
    m.dec(m.current['wrapper'])
    assert env['_tuple_iter_transaction'](iterator,0) is None
    assert m.obj(m.pending)['value'][0]==8
    assert m.disposed.count('wrapper')==1
    assert 'finalizer-error' in m.disposed
    m.assert_clean()


def test_tuple_next_error_preserves_wrapper_for_later_attempt():
    m=ConsumerModel(); wrapper=m.new('wrapper',11,[5]); env=m.env()
    iterator=env['py_tuple_iter_new'](wrapper)
    def bad_next(wrapper,index):
        m.pending=m.new('callback-error',80,(2,'bad next'))
        return None
    env['py_tuple_iter_next']=bad_next
    assert env['_tuple_iter_transaction'](iterator,0) is None
    assert m.obj(m.pending)['value'][0]==2
    assert m.load(m.current['iterator'],16)==m.current['wrapper']
    assert m.load(m.current['iterator'],24)==-3
    env['py_clear_exception']()
    env['py_tuple_iter_next']=m.tuple_next
    result=env['_tuple_iter_transaction'](m.current['iterator'],0)
    assert m.obj(result)['value']==5
    m.assert_clean()


@pytest.mark.parametrize('answer', ['absent','NotImplemented','value','none'])
def test_inplace_result_selection_happens_while_owned(answer):
    m=ConsumerModel(); receiver=m.new('receiver'); other=m.new('other'); env=m.env()
    if answer=='NotImplemented':
        def callback(r,a):
            result=m.current['py_NotImplemented']; m.inc(result); return result
        m.methods['__iadd__']=callback
    elif answer=='value':
        def callback(r,a):
            result=m.load(r); m.inc(result); return result
        m.methods['__iadd__']=callback
    elif answer=='none':
        m.methods['__iadd__']=None
    handled=m.alloc(8)
    result=env['py_user_special_dispatch'](receiver,'__iadd__',other,None,1,7,None,handled)
    if answer=='value':
        assert m.obj(result)['name']=='receiver' and m.load(handled)==1
    elif answer=='none':
        assert result is None and m.load(handled)==1 and m.pending is not None
    else:
        assert result is None and m.load(handled)==0 and m.pending is None
    m.assert_clean()
