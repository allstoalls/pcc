"""Literal destructuring preserves RHS order and authoritative object owners.

Host IR and moving-root models only; native execution remains a separate gate.
"""
from types import SimpleNamespace
import re

import pytest

from pcc.frontends.python.codegen.assignment_statement_lowering import AssignmentStatementLoweringMixin
from pcc.frontends.python.codegen.assignment_store_lowering import AssignmentStoreLoweringMixin
from pcc.frontends.python.py_ast import Assign, Call, DynType, Name, TupleExpr, TupleType
from pcc.ir.compat import ir
from tests.python.test_slot_call_operand_roots import _emit, _probe_function


@pytest.mark.parametrize('prefix', ['', '    left = [0]\n    right = [1]\n'])
def test_subscript_results_are_retained_until_both_rhs_evaluations_finish(prefix):
    text = _probe_function(_emit('def probe(parts: list):\n' + prefix + '''    left, right = parts[0], parts[1]
    return slot_args_probe(left, right)
'''))
    result_calls = list(re.finditer(r'call[^\n]*@py_obj_subscript[^\n]*', text))
    assert len(result_calls) == 2
    # Result name precedes call in owned IR, so use the whole instruction.
    target_moves = list(re.finditer(r'^  %unpack.literal.move[^\n]*@pcc_gc_root_move\([^\n]*',text,re.M))
    assert len(target_moves) == 2
    assert result_calls[-1].start() < target_moves[0].start() < target_moves[1].start()
    assert 'left.owned' in text and 'right.owned' in text
    assert '@pcc_gc_root_copy_lease' in text


@pytest.mark.parametrize('statement', ['left, right = right, left', 'left, left = right, left'])
def test_aliases_are_copied_before_target_replacement(statement):
    text = _probe_function(_emit('''def probe(left, right):
    ''' + statement + '''
    return slot_args_probe(left, right)
'''))
    first_move = text.index('%unpack.literal.move')
    assert len(re.findall(r'@pcc_gc_root_copy_(?:borrowed_)?lease\(', text[:first_move])) >= 2
    assert text[first_move:].count('@pcc_gc_root_move(') >= 2


@pytest.mark.parametrize('target', ['values[0]', 'holder.value'])
def test_container_target_keeps_rhs_address_leased_during_publication(target):
    source='''def probe(values, holder):
    TARGET, other = values[0], values[1]
    return slot_args_probe(other)
'''.replace('TARGET',target)
    text=_probe_function(_emit(source))
    assert '%unpack.literal.lease' in text
    assert '%unpack.literal.current' in text
    assert '%unpack.literal.release' in text
    setter = '@py_obj_setitem(' if target.startswith('values') else '@py_obj_setattr('
    assert setter in text


class MovingModel(AssignmentStatementLoweringMixin, AssignmentStoreLoweringMixin):
    """Execute the production literal branch and local transfer under relocation."""
    def __init__(self, bindings, results, fail_at=None):
        self.events=[]; self.generation=0; self.serial=0; self.results=results
        self.fail_at=fail_at; self.evaluations=0
        self.env={name:(name,ir.IntType(8).as_pointer(),DynType('dyn')) for name in bindings}
        self.values={name:(value,0) for name,value in bindings.items()}
        self.registry=set(bindings);self.temp_roots=[];self.flags={name:True for name in bindings}
        self._module_globals={};self._current_global_names=set();self._current_param_names=set()
        self._borrowed_gc_rooted_local_names=set();self._planned_exact_int_local_names=set()
        self._owned_local_names=set(bindings);self._owned_local_has_value=set(bindings)
        self._cpy_env_flags={};self.env_class_hint={};self._for_target_owned_names=set()
        self._exact_int_env_flags={};self._try_err_block='handler';self._cpy_operand_cleanup_block='handler'
        self._local_bound_flags={};self._local_bound_names=set();self.current_func_def=object()
        self.runtime={'pcc_gc_root_move':'move'}
        self.builder=SimpleNamespace(call=self.call,store=self.store)
    def _fresh(self,label):self.serial+=1;return label+'.'+str(self.serial)
    def _current_try_err_block(self):return self._try_err_block
    def _is_starred_unpack_expr(self,expr):return False
    def _is_object(self,ty):return True
    def _is_valueclass_payload_type(self,ty):return False
    def _expr_returns_unsafe_raw_pointer(self,expr):return False
    def _expr_looks_cpython(self,expr):return False
    def _slot_call_cleanup_block(self,roots,target):return (tuple(roots),target)
    def _emit_slot_call_operand(self,expr,label):
        self.collect();self.evaluations+=1
        if self.evaluations==self.fail_at:raise LookupError('later RHS failed')
        if isinstance(expr,Name):value=self.values[self.env[expr.ident][0]][0]
        else:value=self.results[expr.func.ident]
        name=self._fresh(label);self.values[name]=(value,self.generation)
        self.temp_roots.append(name);self.registry.add(name);self.events.append(('evaluate',value))
        return name
    def collect(self):
        self.generation+=1
        for slot in self.registry:
            if self.values.get(slot) is not None:self.values[slot]=(self.values[slot][0],self.generation)
    def _local_slot_ir_type(self,*args):return ir.IntType(8).as_pointer()
    def _local_slot_decl_type(self,name,ty):return ty
    def _ir_type_matches(self,a,b):return str(a)==str(b)
    def _alloca_in_entry(self,ty,*,name,init_null):
        slot=self._fresh(name);self.values[slot]=None;return slot
    def _ensure_local_gc_frame_root(self,name,slot,ty):self.registry.add(slot);self.collect()
    def _owned_local_flag_for(self,name,slot):return name if name in self.flags else None
    def _ensure_owned_local_flag(self,name,slot):self.flags.setdefault(name,False);return ('flag',name)
    def _emit_release_owned_local_if_flagged(self,name,slot):
        if self.flags.get(name):self.events.append(('release',self.values[slot][0]));self.values[slot]=None
        self.flags[name]=False;self.collect()
    def store(self,value,slot):
        if isinstance(slot,tuple):self.flags[slot[1]]=str(value) == '1'
        else:self.values[slot]=None
    def _as_gc_ptr(self,slot):return slot
    def _slot_call_check_status(self,status,*args):assert status==0
    def call(self,callee,args,**kw):
        assert callee=='move';destination,source=args
        self.collect();assert self.values[destination] is None
        assert source in self.registry and destination in self.registry
        current=self.values[source];assert current[1]==self.generation
        self.values[destination]=current;self.values[source]=None
        self.events.append(('store',destination,current[0]));return 0
    def _release_slot_call_roots(self,roots):
        for root in reversed(roots):
            if self.values[root] is not None:self.events.append(('release',self.values[root][0]))
            self.values[root]=None;self.registry.remove(root);self.collect()
    def assign(self,targets,expressions):
        ty=DynType('dyn');tt=TupleType('tuple',tuple(ty for _ in targets))
        target=TupleExpr(None,tt,tuple(Name(None,ty,n) for n in targets))
        rhs=TupleExpr(None,tt,tuple(Name(None,ty,x) if isinstance(x,str) else Call(span=None,ty=ty,func=Name(None,ty,x[0]),args=()) for x in expressions))
        self._emit_tuple_unpack_assign(SimpleNamespace(value=rhs),target)
    def bound(self,name):return self.values[self.env[name][0]][0]


@pytest.mark.parametrize('fresh',[False,True])
def test_production_branch_evaluates_complete_rhs_before_any_target_write(fresh):
    model=MovingModel({} if fresh else {'left':'old-left','right':'old-right'},{'first':'new-left','second':'new-right'})
    model.assign(('left','right'),(('first',),('second',)))
    assert model.bound('left')=='new-left' and model.bound('right')=='new-right'
    assert [event[0] for event in model.events[:2]]==['evaluate','evaluate']
    assert all(model.values[root] is None for root in model.temp_roots)


@pytest.mark.parametrize('fresh',[False,True])
def test_later_rhs_exception_never_publishes_earlier_target(fresh):
    model=MovingModel({} if fresh else {'left':'old-left','right':'old-right'},{'first':'new-left'},fail_at=2)
    with pytest.raises(LookupError,match='later RHS'):
        model.assign(('left','right'),(('first',),('second',)))
    assert not any(event[0]=='store' for event in model.events)
    if fresh:assert 'left' not in model.env and 'right' not in model.env
    else:assert (model.bound('left'),model.bound('right'))==('old-left','old-right')
    # The existing cleanup-block model separately executes the error edge,
    # preserving its selecting exception through callbacks and relocation.


@pytest.mark.parametrize('targets,expected',[(('left','right'),('R','L')),(('left','left'),('L','R'))])
def test_swap_and_repeated_targets_keep_independent_owners_across_finalizers(targets,expected):
    model=MovingModel({'left':'L','right':'R'}, {})
    model.assign(targets,('right','left'))
    assert (model.bound('left'),model.bound('right'))==expected
    assert model.events[:2]==[('evaluate','R'),('evaluate','L')]
    assert all(model.values[root] is None for root in model.temp_roots)
