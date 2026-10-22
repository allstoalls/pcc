import pytest
from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin
from pcc.frontends.python.codegen.errors import L1CodegenError

class Slot:
    def __eq__(self, other):
        raise AssertionError('IR equality must not select an ownership record')
    __hash__ = None


def test_identity_lookup_retains_distinct_equal_looking_slots():
    first,second=Slot(),Slot()
    model=CallObjectLoweringMixin()
    a=(first,None,True);b=(second,object(),False)
    model._slot_call_root_records=[a,b]
    assert model._slot_call_root_record(first) is a
    assert model._slot_call_root_record(second) is b
    assert model._slot_call_root_record(first) is a
    with pytest.raises(L1CodegenError,match='was not registered'):
        model._slot_call_root_record(Slot())


def test_authoritative_ledger_replacement_and_reordering():
    first,second=Slot(),Slot()
    model=CallObjectLoweringMixin()
    a=(first,None,True);b=(second,object(),False)
    model._slot_call_root_records=[a,b]
    assert model._slot_call_root_record(first) is a
    model._slot_call_root_records=[b,a]
    assert model._slot_call_root_record(first) is a
    replacement=(first,object(),False)
    model._slot_call_root_records[1]=replacement
    assert model._slot_call_root_record(first) is replacement
    model._slot_call_root_records.pop()
    with pytest.raises(L1CodegenError,match='was not registered'):
        model._slot_call_root_record(first)


def test_index_hit_avoids_ledger_scan():
    class IndexedOnly(list):
        def __iter__(self):
            raise AssertionError('a registered identity must use the index')
    first=Slot();record=(first,None,True)
    model=CallObjectLoweringMixin();model._slot_call_root_records=[record]
    assert model._slot_call_root_record(first) is record
    model._slot_call_root_records=IndexedOnly([record])
    assert model._slot_call_root_record(first) is record


def test_stale_identity_index_cannot_select_another_slots_flags():
    first,second=Slot(),Slot()
    a=(first,object(),False);b=(second,None,True)
    model=CallObjectLoweringMixin();model._slot_call_root_records=[a,b]
    model._slot_call_root_record_index={id(second):(0,a)}
    assert model._slot_call_root_record(second) is b
    model._slot_call_root_records=[a]
    with pytest.raises(L1CodegenError,match='was not registered'):
        model._slot_call_root_record(second)
