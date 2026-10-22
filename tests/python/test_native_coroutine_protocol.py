"""Native dynamic coroutine protocol against the running CPython oracle."""
import pytest

_PROGRAM = r'''import gc

class Pause:
    def __await__(self):
        yield 'pause'
        return 7

marker = ValueError('cancel')

async def recover():
    try:
        await Pause()
    except ValueError as error:
        print('caught', error is marker)
        return 19
    finally:
        print('finally')

async def return_from_close():
    try:
        await Pause()
    except GeneratorExit:
        return 29

async def propagate():
    try:
        await Pause()
    finally:
        print('propagate-finally')

async def ignore_exit():
    try:
        await Pause()
    except GeneratorExit:
        await Pause()
    finally:
        print('ignore-finally')

async def never_started():
    print('unexpected-start')
    await Pause()

async def recursive():
    try:
        current.send(None)
    except ValueError:
        print('reentrant')
    await Pause()

class Custom:
    def __init__(self):
        self.started = False

    def __await__(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self.started:
            raise StopIteration(31)
        self.started = True
        return 'custom-start'

    def throw(self, error):
        gc.collect()
        print('original-argument', error == 17, error is marker)
        return 'custom-yield'

    def send(self, value):
        raise StopIteration(31)

    def close(self):
        print('custom-close')

async def custom_child():
    try:
        return await Custom()
    except BaseException as error:
        print('unexpected-carrier', error is marker)
        raise

async def custom_parent():
    return await custom_child()

async def closes_custom():
    try:
        await Custom()
    finally:
        print('custom-outer-finally')

async def catches_bad_throw():
    try:
        await Pause()
    except TypeError:
        print('caught-bad-throw')
        return 41

def main():
    c = recover()
    send = c.send
    throw = c.throw
    close = c.close
    try:
        send()
    except TypeError:
        print('send-arity')
    try:
        send(value=None)
    except TypeError:
        print('send-keyword')
    try:
        throw(typ=marker)
    except TypeError:
        print('throw-keyword')
    try:
        send(1)
    except TypeError:
        print('send-start')
    try:
        throw('invalid')
    except TypeError:
        print('throw-type')
    print(send(None))
    try:
        throw()
    except TypeError:
        print('throw-arity')
    try:
        close(1)
    except TypeError:
        print('close-arity')
    try:
        throw(marker)
    except StopIteration as error:
        print('recovered', error.value)
    try:
        throw(marker)
    except RuntimeError:
        print('no-reuse')
    print('closed', close() is None)
    c = recover()
    print(c.send(None))
    try:
        c.throw(ValueError)
    except StopIteration as error:
        print('class', error.value)
    c = propagate()
    print(c.send(None))
    try:
        c.throw(marker)
    except ValueError as error:
        print('identity', error is marker)
    c = never_started()
    try:
        c.throw(marker)
    except ValueError as error:
        print('fresh', error is marker)
    c = return_from_close()
    print(c.send(None))
    print('close-value', c.close())
    c = ignore_exit()
    print(c.send(None))
    try:
        c.close()
    except RuntimeError:
        print('ignored-exit')
    print('closed-again', c.close() is None)
    c = closes_custom()
    print(c.send(None))
    try:
        c.throw(GeneratorExit)
    except GeneratorExit:
        print('custom-exit')
    c = closes_custom()
    print(c.send(None))
    print('custom-close-result', c.close() is None)
    c = catches_bad_throw()
    print(c.send(None))
    try:
        c.throw(17)
    except StopIteration as error:
        print('bad-throw-result', error.value)
    c = custom_parent()
    print(c.send(None))
    print(c.throw(17))
    print(c.throw(marker))
    try:
        c.send(None)
    except StopIteration as error:
        print('nested-result', error.value)
    gc.collect()

main()
current = recursive()
print(current.send(None))
current.close()
gc.collect()
'''


@pytest.mark.parametrize('bulk', ('0', '1'))
def test_native_coroutine_protocol_matches_cpython(tmp_path, monkeypatch, pcc_runtime_archive, bulk):
    from tests.python.test_async_suspension_roots import _assert_native_async_program_matches_cpython
    monkeypatch.setenv('PCC_DISABLE_BULK_GENERATOR_FRAME_INIT', bulk)
    _assert_native_async_program_matches_cpython(tmp_path, monkeypatch, pcc_runtime_archive, _PROGRAM)
