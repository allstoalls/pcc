"""Production sorted slot transaction under relocation, aliasing and callbacks."""
from pathlib import Path
import pytest
from tests.python.test_list_extend_iterator_ownership import _ExtensionModel
from tests.python.test_foreign_address_leases import _functions


class SortModel(_ExtensionModel):
    def __init__(self, values, *, key=False, compare=False, reverse=False, relocate=False, failure=None, key_alias=False):
        super().__init__(None, relocate, False)
        self.failure, self.reverse, self.key_alias = failure, reverse, key_alias
        self.key_calls = self.compare_calls = self.allocations = self.list_allocations = 0
        self.output_seen_at_disposal = False
        self.external = 10000
        self.source[0].payload = []
        for index, value in enumerate(values):
            obj = self.new('element-' + str(index), fresh=False)
            obj[0].value, obj[0].label = value, index
            self.source[0].payload.append(obj)
        self.key = self.new('key-callable', fresh=False) if key else None
        self.compare = self.new('compare-callable', fresh=False) if compare else None
        self.frames = {self.external: 4}
        for i, value in enumerate([self.source, self.key, self.compare, None]):
            self.store(self.external, i * 8, value)
        self.ns.update({
            '_SORT_SOURCE': 0, '_SORT_KEY': 1, '_SORT_COMPARE': 2,
            '_SORT_VALUES': 3, '_SORT_KEYS': 4, '_SORT_RESULT': 5,
            '_SORT_ARGS': 6, '_SORT_ITEM': 7, '_SORT_LEFT': 8,
            '_SORT_RIGHT': 9, '_SORT_ERROR': 10, '_SORT_COUNT': 11, '_SORT_BYTES': 8,
            'global_addr': lambda name: 11,
            'pcc_gc_root_copy_lease': self.copy,
            'pcc_gc_root_move': self.move,
            'py_list_new': self.list_new, 'py_list_len': self.length,
            'py_list_get': self.get, 'py_list_extend': self.extend,
            'py_tuple_new': self.tuple_new, 'py_tuple_set_item': self.set_item,
            'py_obj_call_slots': self.call,
            'py_obj_truthy': self.truth,
            'py_obj_lt': self.less,
            '_type_of': lambda pointer: 1, 'PY_TYPE_NONE': 0,
            'malloc': self.malloc, 'free': self.free,
        })
        _functions(Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_obj_ops_compare.py',
                   {'_sorted_error', '_sorted_drop', '_sorted_adopt', '_sorted_copy',
                    '_sorted_arguments', '_sorted_prepare_keys', '_sorted_less',
                    '_sorted_merge_indices', 'py_obj_sorted_slots'}, self.ns)

    def boundary(self):
        super().boundary()
        if self.relocate:
            for obj in self.objects:
                if hasattr(obj, 'payload'):
                    obj.payload = [value[0].pointer() if value is not None else None for value in obj.payload]

    def fresh_copy(self, value):
        self.check(value)
        value[0].refs += 1
        self.fresh = value
        return value

    def list_new(self, size):
        self.boundary()
        self.list_allocations += 1
        if self.failure == 'list-allocation' and self.list_allocations == 3:
            return None
        obj = self.new('list')
        obj[0].payload = []
        return obj

    def tuple_new(self, size):
        self.boundary()
        obj = self.new('arguments')
        obj[0].payload = [None] * size
        return obj

    def length(self, value):
        self.boundary()
        return len(self.check(value)[0].payload)

    def get(self, value, index):
        self.boundary()
        return self.fresh_copy(self.check(value)[0].payload[index])

    def set_item(self, container, index, value):
        self.boundary()
        self.check(container)
        self.check(value)
        value[0].refs += 1
        container[0].payload[index] = value

    def append(self, container, value):
        self.boundary()
        self.check(container)
        self.check(value)
        value[0].refs += 1
        container[0].payload.append(value)

    def extend(self, destination, source):
        self.boundary()
        self.check(destination)
        self.check(source)
        for value in source[0].payload:
            value[0].refs += 1
            destination[0].payload.append(value)
        if self.failure == 'iteration':
            self.pending = self.new('iteration-error', fresh=False)

    def call(self, callable_slot, args_slot, kwargs_slot, result_slot):
        self.boundary()
        fn = self.check(self.load(callable_slot, 0))
        args = self.check(self.load(args_slot, 0))[0].payload
        if fn[0].name == 'key-callable':
            self.key_calls += 1
            if self.failure == 'key' and self.key_calls == 2:
                self.pending = self.new('key-error', fresh=False)
                return -1
            if self.key_alias:
                result = self.fresh_copy(args[0])
            else:
                result = self.new('key-result')
                result[0].value = args[0][0].value
        else:
            self.compare_calls += 1
            if self.failure == 'compare':
                self.pending = self.new('compare-error', fresh=False)
                return -1
            result = self.new('comparison-result')
            result[0].value = args[0][0].value < args[1][0].value
        self.store(result_slot, 0, result)
        return 0

    def less(self, left, right):
        self.boundary()
        self.check(left)
        self.check(right)
        self.compare_calls += 1
        if self.failure == 'compare':
            self.pending = self.new('compare-error', fresh=False)
            return 0
        return int(left[0].value < right[0].value)

    def truth(self, value):
        self.boundary()
        return int(self.check(value)[0].value)

    def malloc(self, size):
        self.boundary()
        self.allocations += 1
        if self.failure == 'allocation' and self.allocations == 2:
            return None
        return self.alloc(size)

    def free(self, ptr):
        self.boundary()

    def move(self, destination, source):
        self.boundary()
        if self.failure == 'publish':
            return -1
        assert self.load(destination, 0) is None
        self.store(destination, 0, self.load(source, 0))
        self.store(source, 0, None)
        if source in self.leases:
            self.leases[destination] = self.leases.pop(source)
        return 0

    def drop(self, value):
        if value is None:
            return
        self.check(value)
        obj = value[0]
        obj.refs -= 1
        assert obj.refs >= 0
        if obj.refs == 0:
            for index in range(len(getattr(obj, 'payload', []))):
                child = obj.payload[index]
                obj.payload[index] = None
                self.drop(child)
            obj.payload = []
            if obj.name == 'key-result':
                self.boundary()
                if self.load(self.external, 24) is not None:
                    self.output_seen_at_disposal = True
                self.clear_error()
                self.pending = self.new('disposal-error', fresh=False)

    def run(self):
        return self.ns['py_obj_sorted_slots'](
            self.external, self.external + 8 if self.key else None,
            self.external + 16 if self.compare else None,
            int(self.reverse), self.external + 24,
        )


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('reverse', (False, True))
@pytest.mark.parametrize('key,compare,key_alias', ((False, False, False), (True, False, False), (True, False, True), (False, True, False)))
@pytest.mark.parametrize('values', ([], [2], [3, 1, 2, 1, 3, 0]))
def test_sorted_slots_stable_owners(values, key, compare, key_alias, reverse, relocate):
    model = SortModel(values, key=key, compare=compare, reverse=reverse, relocate=relocate, key_alias=key_alias)
    assert model.run() == 0
    result = model.check(model.load(model.external, 24))
    observed = [(v[0].value, v[0].label) for v in result[0].payload]
    assert observed == sorted(zip(values, range(len(values))), key=lambda v: v[0], reverse=reverse)
    assert model.key_calls == (len(values) if key else 0)
    assert model.pending is None
    assert model.leases == {} and model.frames == {model.external: 4}
    assert all(v[0].refs == 2 for v in result[0].payload)
    assert all(obj.refs == 0 for obj in model.objects
               if obj.name in ('list', 'arguments', 'key-result', 'comparison-result') and obj is not result[0])
    if key and values and not key_alias:
        assert model.output_seen_at_disposal


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', ('iteration', 'key', 'compare', 'allocation', 'publish', 'copy-1', 'lease-list', 'list-allocation'))
def test_sorted_slots_error_preserves_primary_during_disposal(failure, relocate):
    model = SortModel([3, 1, 2], key=True, relocate=relocate, failure=failure)
    assert model.run() < 0
    assert model.load(model.external, 24) is None
    assert model.pending[0].name == {
        'iteration': 'iteration-error', 'key': 'key-error', 'compare': 'compare-error',
        'allocation': 'sorted: cannot allocate index buffer',
        'publish': 'sorted: cannot transfer result owner',
        'copy-1': 'sorted: cannot retain input',
        'lease-list': 'sorted: cannot lease owned value',
        'list-allocation': 'sorted: missing owned value',
    }[failure]
    assert model.leases == {} and model.frames == {model.external: 4}
    source = model.load(model.external, 0)
    assert all(v[0].refs == 1 for v in source[0].payload)
    assert all(obj.refs == 0 for obj in model.objects
               if obj.name in ('list', 'arguments', 'key-result', 'comparison-result'))
