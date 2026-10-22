"""Read-only control-flow checks for class method owner frames."""
import re

from ir_pointer_aliases import (
    canonical_pointer,
    function_bodies,
    pointer_bitcast_aliases,
)


def check_method_frames(text):
    checked = []
    for name, body in function_bodies(text):
        if 'class.method.publication' not in body:
            continue
        aliases = pointer_bitcast_aliases(body)
        def canonical(value):
            return canonical_pointer(value, aliases)
        blocks = {}
        current = None
        for line in body.splitlines()[1:]:
            label = re.match(r'^([\w.$]+):', line)
            if label:
                current = label[1]
                blocks[current] = []
            elif current is not None:
                blocks[current].append(line.strip())
        assert blocks
        pending = [(next(iter(blocks)), (), frozenset())]
        seen = set()
        returns = 0
        leaves = 0
        while pending:
            block, incoming, dirty_before = pending.pop()
            state = block, incoming, dirty_before
            if state in seen:
                continue
            seen.add(state)
            assert len(seen) <= len(blocks) * 8, (name, 'unbounded frame states')
            stack, dirty = list(incoming), set(dirty_before)
            successors = []
            for line in blocks[block]:
                entered = re.search(r'@pcc_gc_frame_enter_lifo\(ptr [^,]+, ptr (%[\w.$]+)\)', line)
                if entered:
                    root = canonical(entered[1])
                    assert root not in stack, (name, block, 'double registration', root)
                    stack.append(root)
                stored = re.search(r'^store ptr ([^,]+), ptr (%[\w.$]+)', line)
                if stored:
                    root = canonical(stored[2])
                    if 'class.method.' in root:
                        if stored[1] == 'null': dirty.discard(root)
                        else: dirty.add(root)
                copied = re.search(r'@pcc_gc_root_copy(?:_borrowed)?_lease\(ptr (%[\w.$]+),', line)
                if copied:
                    root = canonical(copied[1])
                    if 'class.method.' in root: dirty.add(root)
                cleared = re.search(r'@pcc_gc_store_root\(ptr (%[\w.$]+), ptr null\)', line)
                if cleared:
                    dirty.discard(canonical(cleared[1]))
                # Singleton error cleanup clears this exact owner inside the
                # runtime helper, then the caller retires its frame below.
                # The lease helper does not clear ownership. Its production
                # body contract is exercised by test_owned_cleanup_helpers.
                disposed = re.search(
                    r'^call void (?:\(ptr\) )?@py_cleanup_one_root_preserving_exception'
                    r'\(ptr (%[\w.$]+)\)$', line,
                )
                if disposed:
                    dirty.discard(canonical(disposed[1]))
                leaving = re.search(r'@pcc_gc_frame_leave_lifo\(ptr (%[\w.$]+)\)', line)
                if leaving:
                    root = canonical(leaving[1])
                    assert stack and stack[-1] == root, (name, block, 'non-LIFO release', root, stack)
                    assert root not in dirty, (name, block, 'owner left before disposal', root)
                    stack.pop()
                    if 'class.method.' in root: leaves += 1
                if line.startswith('br '):
                    successors = re.findall(r'label %([\w.$]+)', line)
                if line.startswith('ret '):
                    assert not stack, (name, block, 'return with registered roots', stack)
                    assert not dirty, (name, block, 'return with uncleared owners', sorted(dirty))
                    returns += 1
            for successor in successors:
                assert successor in blocks, (name, successor)
                pending.append((successor, tuple(stack), frozenset(dirty)))
        assert returns and leaves, (name, returns, leaves)
        checked.append({'function': name, 'reachable_states': len(seen),
                        'balanced_returns': returns, 'method_frame_leaves': leaves})
    assert checked, 'no method publication frame found'
    return checked


def method_owner_copies(text):
    copies = []
    for name, body in function_bodies(text):
        aliases = pointer_bitcast_aliases(body)
        def canonical(value):
            return canonical_pointer(value, aliases)
        for dest, source in re.findall(r'@pcc_gc_root_copy_lease\(ptr (%[^ ,]+), ptr (%[^ ,)]+)\)', body):
            dest, source = canonical(dest), canonical(source)
            if 'class.method.publication' in dest and 'class.method.publication' in source:
                copies.append((name, dest, source))
    return copies
