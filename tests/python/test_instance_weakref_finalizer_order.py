"""Execute the real refcount/dealloc/finalizer chain, including resurrection."""
from pathlib import Path

import pytest

from tests.python.test_finalizer_callback_lease import FinalizerModel, TERMINAL
from tests.python.test_foreign_address_leases import _functions

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py'


class DeallocationModel(FinalizerModel):
    def __init__(self, backend, *, resurrect=False, pending=False, tag=104):
        self.lifecycle = []
        self.weakref_live = True
        self.freed = False
        self.tracked = []
        # pcc_gc_free_watch_begin/end records: [object, noted].
        self.watches = []
        super().__init__(backend, resurrect=resurrect, pending=pending)
        self.tag = tag
        self.store(1000, 8, tag)
        self.ns.update(
            PYOBJECTHEADER_REFCOUNT_OFFSET=0, PYOBJECTHEADER_TYPE_TAG_OFFSET=8,
            PYINSTANCEOBJECT_CLS_OFFSET=16, PYCLASSOBJECT_N_FIELDS_OFFSET=24,
            PYINSTANCEOBJECT_FIELDS_OFFSET=32, PYCLASSOBJECT_INSTANCE_SIZE_OFFSET=88,
            _pcc_debug_bad_incref=lambda *args: pytest.fail('invalid refcount tail'),
            py_gen_finalize_from_dealloc=lambda value: 0,
            pcc_refcount_forget=lambda value: None,
            pcc_capi_is_cext_type_tag=lambda value: int(value >= 65536),
            py_weakref_invalidate=self.invalidate,
            pcc_gc_note_object_freeing=self.note_freeing,
            GC_FREE_WATCH_BYTES=24,
            pcc_gc_free_watch_begin=self.watch_begin,
            pcc_gc_free_watch_end=self.watch_end,
            py_gc_untrack=lambda value: self.lifecycle.append('untrack'),
            py_gc_track=self.tracked.append,
            pcc_dealloc_with_trash=self.dispatch,
            _gc_backend_selected_fast=lambda: backend,
            pcc_gc_pointer_is_managed=lambda value: int(value in self.nodes),
            _dealloc_ptr_is_instance=lambda value: True,
            pcc_gc_load_ptr=lambda owner, slot: self.load(slot),
            pcc_gc_store_ptr=lambda owner, slot, value: self.store(slot, 0, value),
            pcc_gc_free_object_memory=self.free,
        )
        _functions(RUNTIME / 'py_obj.py', {'_py_decref_finish'}, self.ns)
        _functions(RUNTIME / 'py_class.py', {
            '_instance_storage_slot_count', '_instance_reserved_owner_slot',
            '_instance_builtin_payload_slot', 'py_instance_dealloc',
        }, self.ns)

    def callback(self, function, value):
        self.lifecycle.append('finalizer')
        # Finalization must see the same live weakref as a resurrecting user.
        assert self.weakref_live
        super().callback(function, value)

    def note_freeing(self, value):
        self.lifecycle.append('metadata')
        if self.watches and self.watches[-1][0] == value:
            self.watches[-1][1] = True

    def watch_begin(self, record, value):
        self.watches.append([value, False])

    def watch_end(self, record):
        value, noted = self.watches.pop()
        # A watch directly inside one of the same object hands its outcome out.
        if noted and self.watches and self.watches[-1][0] == value:
            self.watches[-1][1] = True
        return int(noted)

    def invalidate(self, value):
        assert value == 1000
        if self.weakref_live:
            self.lifecycle.append('weakref')
            self.weakref_live = False
            # Match the real weakref callback's unraisable TLS boundary.
            self.pending = 0

    def dispatch(self, value, tag):
        if tag == 11 or 104 <= tag < 65536:
            self.ns['py_instance_dealloc'](value)
        else:
            self.lifecycle.append('generic-dealloc')
            self.free(value)

    def free(self, value):
        assert value == 1000 and not self.freed
        self.lifecycle.append('free')
        self.freed = True

    def finish(self):
        prepared = self.alloc(64)
        for offset, value in ((0, 1000), (8, self.tag), (16, self.load(1000, 12)),
                              (24, self.load('pcc_gc_backend_selected')), (32, 0),
                              (40, 1), (48, 0)):
            self.store(prepared, offset, value)
        self.ns['_py_decref_finish'](prepared)


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('pending', (False, True))
def test_actual_deallocation_finalizes_before_weakrefs(backend, pending):
    model = DeallocationModel(backend, pending=pending)
    model.finish()
    assert model.lifecycle.index('finalizer') < model.lifecycle.index('weakref')
    assert model.lifecycle.index('weakref') < model.lifecycle.index('free')
    assert model.calls == 1 and model.freed and not model.weakref_live
    assert model.count() == model.active() == model.depth == 0


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('pending', (False, True))
def test_actual_resurrection_keeps_weakrefs_until_final_drop(backend, pending):
    model = DeallocationModel(backend, resurrect=True, pending=pending)
    model.finish()
    assert model.lifecycle == ['finalizer']
    assert model.tracked == [1000]
    assert model.weakref_live and not model.freed and model.load(1000, 0) == 1
    assert model.pending == (4000 if pending else 0)
    assert not model.load(1000, 12) & TERMINAL
    assert model.count() == model.active() == model.depth == 0
    # The finalizer's one-shot bit remains set when the resurrected owner dies.
    model.store(1000, 0, 0)
    model.store(1000, 12, model.load(1000, 12) | TERMINAL)
    model.finish()
    assert model.calls == 1 and model.lifecycle.count('weakref') == 1
    assert model.freed and not model.weakref_live


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('tag', (5, 65536))
def test_builtin_and_extension_keep_generic_invalidation(backend, tag):
    model = DeallocationModel(backend, tag=tag)
    model.finish()
    assert model.calls == 0 and model.freed
    assert model.lifecycle.index('weakref') < model.lifecycle.index('generic-dealloc')


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('resurrect', (False, True))
def test_builtin_payload_owner_is_detached_only_on_terminal_deallocation(backend, resurrect):
    model = DeallocationModel(backend, resurrect=resurrect)
    cls = model.load(1000, 16)
    # One declared field, the reserved owner and one builtin payload owner.
    model.store(cls, model.ns['PYCLASSOBJECT_N_FIELDS_OFFSET'], 1)
    model.store(cls, model.ns['PYCLASSOBJECT_INSTANCE_SIZE_OFFSET'], 32 + 3 * 8)
    reserved = model.ns['_instance_reserved_owner_slot'](1000, cls)
    payload = model.ns['_instance_builtin_payload_slot'](1000, cls)
    assert reserved == 1040 and payload == 1048
    model.store(reserved, 0, 7000)
    model.store(payload, 0, 8000)
    detached = []

    def detach(owner, slot, value):
        assert owner == 1000 and value == 0
        assert model.calls == 1 and not model.weakref_live and not model.freed
        detached.append((slot, model.load(slot)))
        model.store(slot, 0, value)

    model.ns['pcc_gc_store_ptr'] = detach
    model.finish()
    if resurrect:
        assert detached == []
        assert model.load(reserved) == 7000 and model.load(payload) == 8000
        model.store(1000, 0, 0)
        model.store(1000, 12, model.load(1000, 12) | TERMINAL)
        model.finish()
    assert detached == [(reserved, 7000), (payload, 8000)]
    assert model.load(reserved) == model.load(payload) == 0
    assert model.calls == 1 and model.freed
    assert model.count() == model.active() == model.depth == 0
