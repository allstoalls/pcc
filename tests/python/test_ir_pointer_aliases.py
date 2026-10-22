"""The ownership test resolver preserves real slot and SSA distinctions."""
import pytest

from ir_pointer_aliases import (
    canonical_pointer,
    function_bodies,
    pointer_bitcast_aliases,
)


def test_bitcast_chains_preserve_distinct_roots():
    aliases = pointer_bitcast_aliases('''  %first = bitcast ptr %left to ptr
  %second = bitcast ptr %first to ptr
  %other = bitcast ptr %right to ptr
''')
    assert canonical_pointer('%second', aliases) == '%left'
    assert canonical_pointer('%other', aliases) == '%right'


@pytest.mark.parametrize('instruction', (
    '%alias = getelementptr i8, ptr %root, i64 0',
    '%alias = getelementptr i8, ptr %root, i64 8',
    '%alias = addrspacecast ptr addrspace(1) %root to ptr',
    '%alias = ptrtoint ptr %root to i64',
))
def test_other_operations_do_not_establish_pointer_identity(instruction):
    aliases = pointer_bitcast_aliases('  ' + instruction + '\n')
    assert canonical_pointer('%alias', aliases) == '%alias'


def test_repeated_ssa_names_are_resolved_in_their_own_function():
    text = '''declare ptr @declaration(ptr)
define ptr @first(ptr %left) {
entry:
  %alias = bitcast ptr %left to ptr
  ret ptr %alias
}
define ptr @second(ptr %right) {
entry:
  %alias = bitcast ptr %right to ptr
  ret ptr %alias
}
'''
    assert [(name, canonical_pointer('%alias', pointer_bitcast_aliases(body)))
            for name, body in function_bodies(text)] == [('first', '%left'), ('second', '%right')]
    with pytest.raises(AssertionError, match='multiple SSA scopes'):
        pointer_bitcast_aliases(text)


def test_alias_cycles_are_rejected():
    aliases = pointer_bitcast_aliases('  %one = bitcast ptr %two to ptr\n  %two = bitcast ptr %one to ptr\n')
    with pytest.raises(AssertionError, match='cyclic pointer alias'):
        canonical_pointer('%one', aliases)
