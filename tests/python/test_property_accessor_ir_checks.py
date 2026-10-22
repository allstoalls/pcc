"""Property ownership proofs reject disposal and publication regressions."""

import re

import pytest

from ir_pointer_aliases import canonical_pointer, pointer_bitcast_aliases
from test_property_accessor_ownership import (
    _emit,
    _owners_and_cfg,
    _property_bodies,
    _source,
)


ACCESSORS = ('getter', 'setter')
HELPER = 'py_cleanup_one_root_preserving_exception'


@pytest.fixture(scope='module')
def property_body():
    return _property_bodies(_emit(_source(ACCESSORS)))[0]


def singleton_disposal(body):
    aliases = pointer_bitcast_aliases(body)
    calls = re.finditer(
        r'^  call void (?:\(ptr\) )?@' + HELPER + r'\(ptr (%[-\w.$]+)\)$',
        body, re.M,
    )
    selected = [call for call in calls if canonical_pointer(call[1], aliases).startswith(
        '%class.property.descriptor.operand',
    )]
    # The first descriptor remains live across the later setter construction.
    assert len(selected) == len(ACCESSORS)
    call = selected[0]
    return call[0], call[1], canonical_pointer(call[1], aliases)


def replace_once(body, old, new):
    assert body.count(old) == 1, old
    return body.replace(old, new)


@pytest.mark.parametrize('signature', ('', '(ptr) '))
@pytest.mark.parametrize('slot_kind', ('direct', 'bitcast-chain'))
def test_exact_singleton_disposal_is_recognized(property_body, signature, slot_kind):
    old, slot, root = singleton_disposal(property_body)
    prefix = ''
    if slot_kind == 'direct':
        slot = root
    else:
        prefix = (f'  %first.alias = bitcast ptr {slot} to ptr\n'
                  '  %second.alias = bitcast ptr %first.alias to ptr\n')
        slot = '%second.alias'
    changed = replace_once(property_body, old, prefix + f'  call void {signature}@{HELPER}(ptr {slot})')
    _owners_and_cfg(changed, ACCESSORS)


def test_inline_disposal_remains_valid(property_body):
    old, slot, _root = singleton_disposal(property_body)
    changed = replace_once(property_body, old, f'  call void @pcc_gc_store_root(ptr {slot}, ptr null)')
    _owners_and_cfg(changed, ACCESSORS)


@pytest.mark.parametrize('mutation', (
    'missing', 'wrong-root', 'null', 'gep', 'lease-only', 'lookalike', 'comment',
    'comment-alias', 'unreachable',
))
def test_unrelated_disposal_cannot_clear_property_owner(property_body, mutation):
    old, slot, root = singleton_disposal(property_body)
    other = re.search(r'(%class\.property\.getter\.operand[-\w.]*) = alloca ptr', property_body)[1]
    replacements = {
        'missing': '',
        'wrong-root': f'  call void @{HELPER}(ptr {other})',
        'null': f'  call void @{HELPER}(ptr null)',
        'gep': (f'  %offset = getelementptr i8, ptr {root}, i64 0\n'
                f'  call void @{HELPER}(ptr %offset)'),
        'lease-only': f'  call void @py_cleanup_one_lease_preserving_exception(ptr {slot}, i64 1)',
        'lookalike': f'  call void @{HELPER}_unrelated(ptr {slot})',
        'comment': '  ; ' + old.strip(),
        'comment-alias': (f'  ; %unrelated.slot = bitcast ptr {root} to ptr\n'
                          f'  call void @{HELPER}(ptr %unrelated.slot)'),
        'unreachable': '',
    }
    changed = replace_once(property_body, old, replacements[mutation])
    if mutation == 'comment-alias':
        changed = replace_once(changed, 'entry:\n', (
            'entry:\n  %unrelated.slot = alloca ptr\n'
            '  store ptr null, ptr %unrelated.slot\n'
        ))
    if mutation == 'unreachable':
        changed = changed[:-1] + f'never.reached:\n{old}\n  ret void\n}}'
    with pytest.raises(AssertionError, match='err.finish'):
        _owners_and_cfg(changed, ACCESSORS)


def test_republication_after_helper_requires_another_disposal(property_body):
    old, slot, root = singleton_disposal(property_body)
    changed = replace_once(property_body, old, old + f'\n  store ptr {slot}, ptr {root}')
    with pytest.raises(AssertionError, match='err.finish'):
        _owners_and_cfg(changed, ACCESSORS)


@pytest.mark.parametrize('producer', ('pcc_gc_take_pinned_slot', 'py_property_new'))
def test_owned_result_must_be_published_before_another_call(property_body, producer):
    call = re.search(
        r'^  (%[-\w.$]+) = call [^\n]*@' + producer + r'\([^\n]*\)\n'
        r'(?=  store ptr \1, ptr %class\.property\.[-\w.]+)', property_body, re.M,
    )[0]
    changed = replace_once(property_body, call, call + '  call void @py_clear_exception()\n')
    with pytest.raises(AssertionError):
        _owners_and_cfg(changed, ACCESSORS)


def test_constructor_requires_live_accessor_owners(property_body):
    call = re.search(r'^  %[-\w.$]+ = call [^\n]*@py_property_new\([^\n]*\)\n', property_body, re.M)[0]
    getter = re.search(r'(%class\.property\.getter\.operand[-\w.]*) = alloca ptr', property_body)[1]
    changed = replace_once(property_body, call, f'  call void @pcc_gc_store_root(ptr {getter}, ptr null)\n' + call)
    with pytest.raises(AssertionError):
        _owners_and_cfg(changed, ACCESSORS)


def test_constructor_requires_the_corresponding_accessor_slots(property_body):
    call = re.search(r'^  %[-\w.$]+ = call [^\n]*@py_property_new\([^\n]*\)\n', property_body, re.M)[0]
    arguments = re.findall(r'ptr (%[-\w.$]+)', call)
    assert len(arguments) == 3
    changed_call = call.replace('ptr ' + arguments[1], 'ptr ' + arguments[2])
    changed = replace_once(property_body, call, changed_call)
    with pytest.raises(AssertionError):
        _owners_and_cfg(changed, ACCESSORS)


def test_property_descriptor_requires_a_lease(property_body):
    _old, _slot, root = singleton_disposal(property_body)
    aliases = pointer_bitcast_aliases(property_body)
    selected = [match[0] for match in re.finditer(
        r'@pcc_gc_foreign_lease_acquire\(ptr (%[-\w.$]+)\)', property_body,
    ) if canonical_pointer(match[1], aliases) == root]
    assert selected
    changed = property_body
    # Acquisitions on the root share one spelling once identity aliases are
    # gone, so drop every such call rather than one unique occurrence each.
    for call in sorted(set(selected)):
        changed = changed.replace(call, call.replace('pcc_gc_foreign_lease_acquire', 'unrelated'))
    with pytest.raises(AssertionError):
        _owners_and_cfg(changed, ACCESSORS)


def publication_guard(body):
    _old, _slot, root = singleton_disposal(body)
    loads = dict(re.findall(r'(%[-\w.$]+) = load ptr, ptr (%[-\w.$]+)', body))
    aliases = pointer_bitcast_aliases(body)
    publications = [match for match in re.finditer(
        r'^  (%[-\w.$]+) = call i64 (?:\(ptr, ptr, ptr\) )?@py_class_setattr_raw'
        r'\(ptr [^,\n]+, ptr [^,\n]+, ptr (%[-\w.$]+)\)$', body, re.M,
    ) if canonical_pointer(loads.get(match[2], match[2]), aliases) == root]
    assert len(publications) == 1
    publication = publications[0]
    guard = re.search(
        r'^  (%[-\w.$]+) = icmp slt i64 ' + re.escape(publication[1]) + r', 0\n'
        r'  br i1 \1, label %([-\w.$]+), label %([-\w.$]+)$', body, re.M,
    )
    assert guard is not None
    error = re.search(r'^' + re.escape(guard[2]) + r':\n(.*?)(?=^[-\w.$]+:|^})', body, re.M | re.S)[0]
    return publication, guard, error


def test_cleanup_trampoline_preserves_failure_route(property_body):
    _publication, _guard, error = publication_guard(property_body)
    cleanup = re.search(r'br label %([-\w.$]+)', error)[1]
    changed_error = re.sub(r'br label %[-\w.$]+', 'br label %cleanup.trampoline', error)
    changed = replace_once(property_body, error, changed_error)
    changed = changed[:-1] + f'cleanup.trampoline:\n  br label %{cleanup}\n}}'
    _owners_and_cfg(changed, ACCESSORS)


@pytest.mark.parametrize('mutation', (
    'missing-publication', 'wrong-status', 'wrong-predicate', 'unused-condition',
    'reversed-edges', 'missing-reporter', 'wrong-reporter', 'comment-reporter',
    'skip-error-cleanup', 'trampoline-to-ready',
))
def test_property_publication_must_guard_its_own_status(property_body, mutation):
    publication, guard, error = publication_guard(property_body)
    reporter = next(line for line in error.splitlines() if '@pcc_slot_call_status_report.' in line)
    if mutation == 'missing-publication':
        changed = replace_once(property_body, publication[0], publication[0].replace('@py_class_setattr_raw', '@unrelated'))
    elif mutation == 'wrong-status':
        changed = replace_once(property_body, guard[0], guard[0].replace('i64 ' + publication[1], 'i64 0'))
    elif mutation == 'wrong-predicate':
        changed = replace_once(property_body, guard[0], guard[0].replace('icmp slt', 'icmp sgt'))
    elif mutation == 'unused-condition':
        changed = replace_once(property_body, guard[0], guard[0].replace('br i1 ' + guard[1], 'br i1 true'))
    elif mutation == 'reversed-edges':
        changed = replace_once(property_body, guard[0], guard[0].replace(
            f'label %{guard[2]}, label %{guard[3]}', f'label %{guard[3]}, label %{guard[2]}',
        ))
    elif mutation == 'skip-error-cleanup':
        changed_error = re.sub(r'br label %[-\w.$]+', 'br label %' + guard[3], error)
        changed = replace_once(property_body, error, changed_error)
    elif mutation == 'trampoline-to-ready':
        changed_error = re.sub(r'br label %[-\w.$]+', 'br label %success.trampoline', error)
        changed = replace_once(property_body, error, changed_error)
        changed = changed[:-1] + f'success.trampoline:\n  br label %{guard[3]}\n}}'
    else:
        changed_reporter = {
            'missing-reporter': '',
            'wrong-reporter': reporter.replace('@pcc_slot_call_status_report.', '@unrelated.'),
            'comment-reporter': '  ; ' + reporter.strip(),
        }[mutation]
        changed = replace_once(property_body, error, error.replace(reporter, changed_reporter))
    with pytest.raises(AssertionError, match='property publication'):
        _owners_and_cfg(changed, ACCESSORS)
