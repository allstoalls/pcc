"""A failing close must reach its own handler before the coroutine is resumed."""
import pytest

_PROGRAM = '''import gc

class Pause:
    def __await__(self):
        yield 'pause'

async def ignores_exit():
    try:
        await Pause()
    except GeneratorExit:
        await Pause()
    finally:
        print('finally')

def main():
    c = ignores_exit()
    print(c.send(None))
    try:
        c.close()
    except RuntimeError:
        print('ignored-exit')
    print('closed-again', c.close() is None)
    gc.collect()

main()
'''


@pytest.mark.parametrize("bulk", ("0", "1"))
def test_coroutine_close_routes_ignored_exit_before_repeated_close(
    tmp_path, monkeypatch, pcc_runtime_archive, bulk,
):
    from tests.python.test_async_suspension_roots import (
        _assert_native_async_program_matches_cpython,
    )
    monkeypatch.setenv("PCC_DISABLE_BULK_GENERATOR_FRAME_INIT", bulk)
    _assert_native_async_program_matches_cpython(
        tmp_path, monkeypatch, pcc_runtime_archive, _PROGRAM,
    )


_RESULT_PROGRAM = '''import gc

events = []

class Token:
    def __init__(self, name):
        self.name = name
    def __del__(self):
        events.append(self.name)

class Pause:
    def __await__(self):
        yield 'pause'

async def returns(name):
    try:
        await Pause()
    except GeneratorExit:
        return Token(name)

def returned():
    c = returns('return')
    c.send(None)
    return c.close()

def take(value, later=None):
    gc.collect()
    return value

def later():
    gc.collect()
    return None

def fail():
    gc.collect()
    raise ValueError('later')

def main():
    c = returns('assignment')
    c.send(None)
    value = c.close()
    gc.collect()
    print(value.name)
    del value
    gc.collect()
    value = returned()
    gc.collect()
    print(value.name)
    del value
    gc.collect()
    c = returns('argument')
    c.send(None)
    value = take(c.close(), later())
    print(value.name)
    del value
    gc.collect()
    c = returns('default')
    c.send(None)
    def target(value=c.close()):
        gc.collect()
        return value
    print(target().name)
    del target
    gc.collect()
    c = returns('list')
    c.send(None)
    values = [c.close()]
    gc.collect()
    print(values[0].name)
    del values
    gc.collect()
    c = returns('discarded')
    c.send(None)
    c.close()
    gc.collect()
    c = returns('later-error')
    c.send(None)
    try:
        take(c.close(), fail())
    except ValueError as error:
        print(str(error))
    gc.collect()
    print(events)

main()
'''


@pytest.mark.parametrize("bulk", ("0", "1"))
def test_coroutine_close_return_owners_positions(
    tmp_path, monkeypatch, pcc_runtime_archive, bulk,
):
    from tests.python.test_async_suspension_roots import (
        _assert_native_async_program_matches_cpython,
    )
    monkeypatch.setenv("PCC_DISABLE_BULK_GENERATOR_FRAME_INIT", bulk)
    _assert_native_async_program_matches_cpython(
        tmp_path, monkeypatch, pcc_runtime_archive, _RESULT_PROGRAM,
    )
