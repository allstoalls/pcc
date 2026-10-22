"""Fail-closed IR-text check for strict functions' NotImplementedError uses.

This is deliberately smaller than an IR verifier: only a builtin catch-class
lookup, pointer copies, local owned roots, and handler matching are admitted.
Unknown uses stay rejected. The integration gate still verifies the whole IR.
"""

import re


_LOCAL = r'%(?:"[^"\n]+"|[-\w.$]+)'
_GLOBAL = r'@(?:"[^"\n]+"|[-\w.$]+)'
_TOKEN = re.compile(_LOCAL)
_ASSIGN = re.compile(r'\s*(' + _LOCAL + r')\s*=\s*(.*)')
_CALL = re.compile(r'\bcall\b[^@\n]*@("[^"\n]+"|[-\w.$]+)\((.*)\)')
_CLASS = re.compile(r'@"?py_exc_builtin_class"?\(\s*i64\s+11\s*\)')


def _pointer(argument):
    match = re.fullmatch(r'ptr\s+(' + _LOCAL + '|' + _GLOBAL + r'|null)', argument.strip())
    return match[1] if match else None


def require_no_notimplemented_stub(body, symbol):
    # Both literal-message and value-message constructors create instances.
    assert not re.search(r'@"?py_exc_new(?:_with_value)?"?\(\s*i64\s+11\s*,', body), symbol
    # First-class construction can discard the builtin-class lookup's result,
    # then load/copy this cache slot. It is not the catch-only lowering below.
    assert not re.search(r'@"?py_subs_exc_cache_(?:get|slot)"?\(\s*i64\s+11\s*\)', body), symbol
    assert not re.search(r'@"?py_exc_classes"?(?![\w.$])', body), symbol
    if not _CLASS.search(body):
        assert "NotImplementedError" not in body, symbol
        return

    instructions, classes, allocated, addresses, selected = [], set(), set(), {}, set()
    for line in body.splitlines():
        assigned = _ASSIGN.fullmatch(line)
        result, operation = (assigned[1], assigned[2]) if assigned else (None, line.strip())
        call = _CALL.search(operation)
        callee = call[1].strip('"') if call else None
        arguments = [part.strip() for part in call[2].split(',')] if call else []
        pointers = [_pointer(part) for part in arguments]
        bitcast = re.fullmatch(r'bitcast ptr (' + _LOCAL + r') to ptr', operation)
        select = re.fullmatch(r'select i1 ' + _LOCAL + r', ptr (' + _LOCAL + r'), ptr (' + _LOCAL + r')', operation)
        load = re.fullmatch(r'load ptr, ptr (' + _LOCAL + r')', operation)
        store = re.fullmatch(r'store ptr (' + _LOCAL + r'|null), ptr (' + _LOCAL + r')', operation)
        if result and operation == 'alloca ptr':
            allocated.add(result)
        if result and bitcast:
            addresses[result] = {bitcast[1]}
        if result and select:
            addresses[result] = {select[1], select[2]}
            selected.add(result)
        if _CLASS.search(operation):
            assert result and callee == 'py_exc_builtin_class' and len(arguments) == 1, symbol
            assert _CALL.fullmatch(operation), symbol
            assert re.fullmatch(r'i64\s+11', arguments[0]), symbol
            classes.add(result)
        instructions.append((result, operation, callee, pointers, bitcast, select, load, store))

    def slots(pointer, seen=frozenset()):
        if pointer in seen:
            return set()
        if pointer in addresses:
            return set().union(*(slots(item, seen | {pointer}) for item in addresses[pointer]))
        return {pointer}

    # A monotone may-flow analysis retains aliases after null cleanup and joins.
    # This over-approximation rejects possible escapes rather than hiding them.
    values = {value: {value} for value in classes}
    roots = {}
    changed = True
    while changed:
        changed = False
        for result, operation, callee, pointers, bitcast, select, load, store in instructions:
            incoming = set()
            if bitcast:
                incoming |= values.get(bitcast[1], set())
            source = load[1] if load else (pointers[1] if callee == 'pcc_gc_load_ptr' and len(pointers) == 2 else None)
            if source:
                for slot in slots(source):
                    incoming |= roots.get(slot, set())
            if result and incoming - values.get(result, set()):
                values.setdefault(result, set()).update(incoming)
                changed = True
            destination, value = (store[2], store[1]) if store else (None, None)
            if callee == 'pcc_gc_store_root' and len(pointers) == 2:
                destination, value = pointers
            if destination and value in values:
                physical = slots(destination)
                assert physical and physical <= allocated, (symbol, 'class escapes local root')
                for slot in physical:
                    if values[value] - roots.get(slot, set()):
                        roots.setdefault(slot, set()).update(values[value])
                        changed = True

    def rooted(pointer):
        return bool(pointer and slots(pointer) & roots.keys())

    def cleanup(pointer, seen=frozenset()):
        if pointer in seen:
            return False
        return pointer in selected or any(cleanup(item, seen | {pointer})
                                          for item in addresses.get(pointer, ()))

    caught = set()
    for result, operation, callee, pointers, bitcast, select, load, store in instructions:
        tokens = set(_TOKEN.findall(operation))
        if not (tokens & values.keys() or any(rooted(token) for token in tokens)):
            continue
        valid = False
        if callee:
            assert _CALL.fullmatch(operation) and all(pointer is not None for pointer in pointers), (symbol, 'unrecognized class operand')
        if any(cleanup(token) for token in tokens):
            # Conditional error-cleanup addresses may only clear their slot.
            # They cannot reload a class or merge it with an exception owner.
            valid = (callee == 'pcc_gc_store_root' and len(pointers) == 2
                     and rooted(pointers[0]) and cleanup(pointers[0])
                     and pointers[1] == 'null')
        elif bitcast and result:
            valid = ((bitcast[1] in values and result in values)
                     or (rooted(bitcast[1]) and rooted(result)))
        elif select and result:
            valid = rooted(result) and slots(result) <= allocated and not tokens & values.keys()
        elif load and result:
            valid = rooted(load[1]) and result in values
        elif store:
            valid = rooted(store[2]) and (store[1] == 'null' or store[1] in values)
        elif callee == 'pcc_gc_store_root' and len(pointers) == 2:
            valid = rooted(pointers[0]) and (pointers[1] == 'null' or pointers[1] in values)
        elif callee == 'pcc_gc_load_ptr' and len(pointers) == 2:
            valid = pointers[0] == 'null' and rooted(pointers[1]) and result in values
        elif callee == 'pcc_gc_frame_enter' and len(pointers) == 2:
            valid = pointers[0] is not None and pointers[0] not in values and not rooted(pointers[0]) and rooted(pointers[1])
        elif callee == 'pcc_gc_frame_leave' and len(pointers) == 1:
            valid = rooted(pointers[0])
        elif callee == 'py_exc_match_handler' and len(pointers) == 2:
            valid = pointers[0] is not None and pointers[1] in values and pointers[0] not in values and not rooted(pointers[0])
            if valid:
                caught.update(values[pointers[1]])
        assert valid, (symbol, 'non-catch NotImplementedError use', operation)
    assert caught == classes, (symbol, 'unmatched NotImplementedError class')
    # Keep the previous name-level defense for globals, calls, literals, and
    # identifiers outside the proved class-value/local-root transport above.
    sanitized = _TOKEN.sub(lambda match: '%catch_class' if match[0] in values or rooted(match[0]) else match[0], body)
    assert 'NotImplementedError' not in sanitized, symbol
