import gc
from pcc.extern import extern, c_ptr, c_int64, c_obj
from pcc.unsafe import stack_alloc, store_i8

make_bytes = extern('py_bytes_new', (c_ptr, c_int64), c_obj)
positive = extern('py_obj_pos', (c_obj,), c_obj)
live_bytes = extern('pcc_os_heap_in_use_bytes', (), c_int64)
freed_counts = 0
freed_aliases = 0

class Count:
    def __index__(self):
        gc.collect()
        return 2
    def __del__(self):
        global freed_counts
        freed_counts += 1
        gc.collect()

class Alias:
    def __pos__(self):
        gc.collect()
        return self
    def __del__(self):
        global freed_aliases
        freed_aliases += 1

def later():
    gc.collect()
    return None

def fail():
    gc.collect()
    raise ValueError('later')

def take(*, value, other=None, **extras):
    gc.collect()
    return value

def decode(count: int):
    storage = stack_alloc(32)
    store_i8(storage, 0, 65)
    store_i8(storage, 1, 66)
    return make_bytes(storage, count).decode('utf-8', 'ignore')

def scan(count: int):
    storage = stack_alloc(32)
    store_i8(storage, 0, 65)
    store_i8(storage, 1, 66)
    for index in range(count):
        assert take(value=make_bytes(storage, 2), other=later()) == b'AB'
        try:
            take(value=make_bytes(storage, 2), other=fail())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('later exception disappeared')

def main():
    assert decode(2) == 'AB'
    storage = stack_alloc(32)
    store_i8(storage, 0, 65)
    store_i8(storage, 1, 66)
    assert make_bytes(storage, Count()).decode('utf-8', 'ignore') == 'AB'
    gc.collect()
    assert freed_counts == 1
    def default(value=make_bytes(storage, 2)):
        gc.collect()
        return value
    assert default() == b'AB'
    value = Alias()
    alias = take(value=positive(positive(value)), other=later())
    assert alias is value
    value = None
    gc.collect()
    assert freed_aliases == 0
    alias = None
    gc.collect()
    assert freed_aliases == 1
    scan(8)
    before = live_bytes()
    scan(50)
    growth = live_bytes() - before
    print('EXTERN_PUBLICATION_OK', growth)
main()
