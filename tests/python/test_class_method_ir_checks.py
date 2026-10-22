"""The class-method proof models disposal without relaxing frame ownership."""

import pytest

from class_method_ir_checks import check_method_frames


ROOT = '%class.method.publication'
ALIAS = '%method.alias'
HELPER = 'py_cleanup_one_root_preserving_exception'
LEAVE = f'call void @pcc_gc_frame_leave_lifo(ptr {ROOT})'


def method_owner_ir(cleanup, *, after_cleanup='', leave=LEAVE):
    return f'''define void @method_owner(ptr %value, i1 %condition) {{
entry:
  {ROOT} = alloca ptr
  %other = alloca ptr
  store ptr null, ptr {ROOT}
  store ptr null, ptr %other
  %first.alias = bitcast ptr {ROOT} to ptr
  {ALIAS} = bitcast ptr %first.alias to ptr
  %other.alias = bitcast ptr %other to ptr
  %offset = getelementptr i8, ptr {ROOT}, i64 0
  call void @pcc_gc_frame_enter_lifo(ptr @map, ptr {ROOT})
  store ptr %value, ptr {ROOT}
  br i1 %condition, label %normal, label %cleanup
normal:
  call void @pcc_gc_store_root(ptr {ROOT}, ptr null)
  {LEAVE}
  ret void
cleanup:
  {cleanup}
  {after_cleanup}
  {leave}
  ret void
}}
'''


@pytest.mark.parametrize('slot', (ROOT, ALIAS))
@pytest.mark.parametrize('signature', ('', '(ptr) '))
def test_single_root_helper_clears_its_exact_owner(slot, signature):
    proof = check_method_frames(method_owner_ir(
        f'call void {signature}@{HELPER}(ptr {slot})',
    ))
    assert proof == [{
        'function': 'method_owner', 'reachable_states': 3,
        'balanced_returns': 2, 'method_frame_leaves': 2,
    }]


@pytest.mark.parametrize('slot', (ROOT, ALIAS))
def test_inline_root_clear_keeps_its_existing_meaning(slot):
    assert check_method_frames(method_owner_ir(
        f'call void @pcc_gc_store_root(ptr {slot}, ptr null)',
    ))


@pytest.mark.parametrize('cleanup', (
    '',
    f'call void @{HELPER}(ptr %other.alias)',
    f'call void @{HELPER}(ptr null)',
    f'call void @{HELPER}(ptr %offset)',
    f'call void @py_cleanup_one_lease_preserving_exception(ptr {ALIAS}, i64 1)',
    f'call void @{HELPER}_unrelated(ptr {ALIAS})',
    f'; call void @{HELPER}(ptr {ALIAS})',
))
def test_unrelated_or_missing_disposal_cannot_clear_the_owner(cleanup):
    with pytest.raises(AssertionError, match='owner left before disposal'):
        check_method_frames(method_owner_ir(cleanup))


def test_disposal_must_precede_frame_retirement():
    with pytest.raises(AssertionError, match='owner left before disposal'):
        check_method_frames(method_owner_ir(
            LEAVE + f'\n  call void @{HELPER}(ptr {ALIAS})', leave='',
        ))


def test_republication_after_helper_requires_another_disposal():
    with pytest.raises(AssertionError, match='owner left before disposal'):
        check_method_frames(method_owner_ir(
            f'call void @{HELPER}(ptr {ALIAS})',
            after_cleanup=f'store ptr %value, ptr {ROOT}',
        ))


def test_helper_does_not_retire_the_caller_frame():
    with pytest.raises(AssertionError, match='return with registered roots'):
        check_method_frames(method_owner_ir(
            f'call void @{HELPER}(ptr {ALIAS})', leave='',
        ))


def test_helper_does_not_relax_lifo_order():
    with pytest.raises(AssertionError, match='non-LIFO release'):
        check_method_frames(method_owner_ir(
            f'call void @pcc_gc_frame_enter_lifo(ptr @map, ptr %other)\n'
            f'  call void @{HELPER}(ptr {ALIAS})',
        ))
