"""Actual graph-lock and thread-kernel bodies suppress lock-held polls."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT=Path(__file__).parents[2]/'pcc/runtime/py'


class SuspensionPathReached(Exception):
    pass


class LockTripwire(Exception):
    pass


class GraphPollModel:
    def __init__(self,stop_requested):
        self.state={'g_tls_pcc_py_gc_minor_graph_lock_depth':0,
                    'g_pcc_py_gc_minor_graph_lock':0,
                    'pcc_tls_no_park_depth_py':0,
                    'pcc_tls_scheduler_lock_held_py':0,
                    'pcc_tls_thread_id_py':7,
                    'pcc_thread_stop_requested':stop_requested}
        self.world_entries=[]
        self.env=dict(i64=int,c_ptr=int,
            global_addr=lambda name:name,
            load_i32=lambda address,offset:self.state[address],
            store_i32=lambda address,offset,value:self.state.__setitem__(address,value),
            atomic_store_i32=lambda address,offset,value,order:self.state.__setitem__(address,value),
            atomic_cas_i32=self.compare_exchange,
            pcc_threads_enabled=lambda:1,pcc_current_thread_id=lambda:7,
            _tls_i64=lambda address:self.state[address],
            _finish_deferred_tripwire=lambda:None,
            _world_init=self.world_init,
            pcc_platform_abort=self.abort)
        self.load('freestanding_thread_kernel_pthread.py',{
            'pcc_thread_no_park_enter','pcc_thread_no_park_exit','pcc_thread_safepoint',
        })
        self.env['thread_safepoint']=self.env['pcc_thread_safepoint']
        self.load('freestanding_runtime_high_substrate.py',{
            'pcc_py_gc_minor_graph_lock','pcc_py_gc_minor_graph_unlock',
        })

    def load(self,filename,names):
        path=ROOT/filename;tree=ast.parse(path.read_text())
        nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
        assert {node.name for node in nodes}==names
        for node in nodes:node.decorator_list=[]
        exec(compile(ast.fix_missing_locations(ast.Module(nodes,[])),str(path),'exec'),self.env)

    def compare_exchange(self,address,offset,expected,desired,success,failure):
        old=self.state[address]
        if old==expected:self.state[address]=desired
        return old

    def world_init(self):
        self.world_entries.append(dict(self.state))
        raise SuspensionPathReached

    def abort(self):
        raise LockTripwire


@pytest.mark.parametrize('nested',(1,2))
@pytest.mark.parametrize('stop_requested',(0,1))
def test_actual_graph_transaction_suppresses_injected_thread_poll(nested,stop_requested):
    model=GraphPollModel(stop_requested)
    for _ in range(nested):model.env['pcc_py_gc_minor_graph_lock']()
    assert model.state['g_tls_pcc_py_gc_minor_graph_lock_depth']==nested
    assert model.state['g_pcc_py_gc_minor_graph_lock']==1
    assert model.state['pcc_tls_no_park_depth_py']==1
    # This is the exact callee emitted at function/loop polling gates. Even
    # with a stop request, its no-park branch precedes initialization/parking.
    model.env['pcc_thread_safepoint']()
    assert model.world_entries==[]
    for _ in range(nested-1):model.env['pcc_py_gc_minor_graph_unlock']()
    assert model.state['g_tls_pcc_py_gc_minor_graph_lock_depth']==1
    with pytest.raises(SuspensionPathReached):
        model.env['pcc_py_gc_minor_graph_unlock']()
    reached=model.world_entries[0]
    assert reached['g_pcc_py_gc_minor_graph_lock']==0
    assert reached['g_tls_pcc_py_gc_minor_graph_lock_depth']==0
    assert reached['pcc_tls_no_park_depth_py']==0


@pytest.mark.parametrize('stop_requested',(0,1))
def test_unleased_scheduler_lock_still_trips_before_suspension(stop_requested):
    model=GraphPollModel(stop_requested)
    model.state['pcc_tls_scheduler_lock_held_py']=1
    with pytest.raises(LockTripwire):model.env['pcc_thread_safepoint']()
    assert model.world_entries==[]
