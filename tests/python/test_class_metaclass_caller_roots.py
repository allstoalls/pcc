"""The actual compiler caller supplies live registered owners and leases."""
from collections import deque
import re

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def test_metaclass_setter_uses_registered_live_leased_sources():
    module = infer_module(parse_and_lift('''class Meta(type):
    pass
class Example(metaclass=Meta):
    value = ('kept',)
''', 'metaclass_owners.py', 'metaclass_owners'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    selected = [body for body in re.findall(r'^define [^\n]*\{\n.*?^}', text, re.M | re.S)
                if re.search(r'\bcall [^\n]*@py_class_set_metaclass\(', body)]
    assert selected
    for body in selected:
        aliases = dict(re.findall(r'(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr', body))

        def physical(value):
            seen = set()
            while value in aliases:
                assert value not in seen
                seen.add(value)
                value = aliases[value]
            return value

        loaded = dict(re.findall(r'(%[-\w.]+) = load ptr, ptr (%[-\w.]+)', body))
        setters = re.findall(r'@py_class_set_metaclass\(ptr (%[-\w.]+), ptr (%[-\w.]+)\)', body)
        required = {physical(loaded[value]) for arguments in setters for value in arguments}
        assert len(required) >= 2
        blocks = {}
        current = None
        for line in body.splitlines()[1:-1]:
            label = re.match(r'^([-\w.$]+):', line)
            if label:
                current = label.group(1)
                blocks[current] = []
            elif current is not None:
                blocks[current].append(line.strip())
        queue = deque([(next(iter(blocks)), frozenset(), frozenset(), frozenset())])
        visited = set()
        calls = 0
        while queue:
            block, live, registered, leases = queue.popleft()
            state = (block, live, registered, leases)
            if state in visited:
                continue
            visited.add(state)
            live, registered, leases = set(live), set(registered), set(leases)
            successors = []
            for line in blocks[block]:
                enter = re.search(r'@pcc_gc_frame_enter(?:_lifo)?\(ptr [^,]+, ptr (%[-\w.]+)\)', line)
                if enter:
                    registered.add(physical(enter.group(1)))
                store = re.search(r'store ptr (null|%[-\w.]+), ptr (%[-\w.]+)', line)
                if store and physical(store.group(2)) in required:
                    root = physical(store.group(2))
                    live.discard(root) if store.group(1) == 'null' else live.add(root)
                publication = re.search(r'@pcc_gc_store_root\(ptr (%[-\w.]+), ptr (null|%[-\w.]+)\)', line)
                if publication and physical(publication.group(1)) in required:
                    root = physical(publication.group(1))
                    live.discard(root) if publication.group(2) == 'null' else live.add(root)
                copy = re.search(r'(%[-\w.]+) = call [^\n]*@pcc_gc_root_copy(?:_borrowed)?_lease\(ptr (%[-\w.]+), ptr [^\n]*\)', line)
                if copy and physical(copy.group(2)) in required:
                    root = physical(copy.group(2))
                    live.add(root)
                    leases.add((copy.group(1), root))
                acquire = re.search(r'(%[-\w.]+) = call [^\n]*@pcc_gc_foreign_lease_acquire\(ptr (%[-\w.]+)\)', line)
                if acquire:
                    leases.add((acquire.group(1), physical(acquire.group(2))))
                release = re.search(r'@pcc_gc_foreign_lease_release\(ptr (%[-\w.]+), i64 (%[-\w.]+)\)', line)
                if release:
                    leases.discard((release.group(2), physical(release.group(1))))
                setter = re.search(r'@py_class_set_metaclass\(ptr (%[-\w.]+), ptr (%[-\w.]+)\)', line)
                if setter:
                    for value in setter.groups():
                        root = physical(loaded[value])
                        assert root in live and root in registered, (block, root, live, registered)
                        assert any(slot == root for token, slot in leases), (block, root, leases)
                    calls += 1
                leave = re.search(r'@pcc_gc_frame_leave(?:_lifo)?\(ptr (%[-\w.]+)\)', line)
                if leave:
                    registered.discard(physical(leave.group(1)))
                if line.startswith('br '):
                    successors.extend(re.findall(r'label %([-\w.$]+)', line))
            queue.extend((label, frozenset(live), frozenset(registered), frozenset(leases)) for label in successors)
        assert calls
